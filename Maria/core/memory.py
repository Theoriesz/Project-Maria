"""SQLite-backed memory layer for Maria."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from logging import Logger
from pathlib import Path
from typing import Any

from core.constants import (
    DATABASE_SCHEMA_VERSION,
    DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS,
)
from core.failures import Failure, FailureKind, TypedFailureError
from core.logger import get_logger

_DATABASE_BUSY_MESSAGE = "Database is busy. Try again shortly."

_SQLITE_PRIMARY_RESULT_CODE_MASK = 0xFF

_BUSY_OR_LOCKED_PRIMARY_CODES = frozenset(
    {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }
)

_DATABASE_REFUSED_MESSAGE = "Unable to initialize Maria database."

_SCHEMA_VERSION_UNREADABLE_MESSAGE = "Unable to read database schema version."

# The stable codes the initialization boundary is allowed to record. A failure
# there can only be described by one of these: the underlying exception is
# never logged, formatted, or attached, because SQLite states what it could
# not read in terms of the file it was reading, and an operational log is not
# where that belongs. The owner-visible message is decided separately and is
# equally fixed.
_EVENT_DATABASE_UNREADABLE = "database_unreadable"
_EVENT_DATABASE_BUSY = "database_busy"
_INITIALIZATION_STOPPED = "Database initialization stopped: %s."

# SQLite's default value for ``PRAGMA user_version``. It is not a Maria schema
# version: it means no marker was ever written, so the database has to be
# classified by its structure before anything is done to it.
_UNVERSIONED_SCHEMA = 0

# Maria's schema history, as a plain counter. Version 1 is the earliest shape
# Maria ever created, before explicit memories existed. Version 2 adds the
# ``memories`` table and is the published v0.2.0 baseline. The current version
# lives in ``core.constants`` as ``DATABASE_SCHEMA_VERSION``.
_SCHEMA_BEFORE_EXPLICIT_MEMORIES = 1
_SCHEMA_V0_2_BASELINE = 2

# The live schema. These statements describe the database this build expects
# and are what a brand-new database is created from, so they move whenever the
# schema advances. They are deliberately separate statements rather than one
# script: ``executescript`` commits before it runs, which would put schema
# creation outside the transaction that stamps it.
_CREATE_INTERACTIONS_TABLE = """
    CREATE TABLE IF NOT EXISTS interactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_input TEXT NOT NULL,
        response TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
"""

_CREATE_NOTES_TABLE = """
    CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
"""

_CREATE_MEMORIES_TABLE = """
    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact TEXT NOT NULL CHECK (length(fact) > 0),
        normalized_fact TEXT NOT NULL UNIQUE
            CHECK (length(normalized_fact) > 0),
        created_at TEXT NOT NULL
    )
"""

_CREATE_INTERACTIONS_INDEX = """
    CREATE INDEX IF NOT EXISTS idx_interactions_created_at
        ON interactions (created_at)
"""

_CREATE_NOTES_INDEX = """
    CREATE INDEX IF NOT EXISTS idx_notes_title
        ON notes (title)
"""

_CURRENT_SCHEMA_STATEMENTS: tuple[str, ...] = (
    _CREATE_INTERACTIONS_TABLE,
    _CREATE_NOTES_TABLE,
    _CREATE_MEMORIES_TABLE,
    _CREATE_INTERACTIONS_INDEX,
    _CREATE_NOTES_INDEX,
)


# Frozen history below this line.
#
# The statements that follow describe databases that already exist on owners'
# disks. They are copies rather than references to the live statements above
# on purpose: the moment the live schema advances, a shared constant would
# silently redefine what "the v0.2.0 baseline" means and drift detection would
# start rejecting every genuine legacy database. Never edit them to match a
# newer schema; add a migration instead.
#
# The set also cannot grow. Every database created from this build onwards is
# stamped, so a file with no marker can only have been produced by one of the
# two shapes Maria shipped before stamping existed.
_HISTORICAL_INTERACTIONS_TABLE = """
    CREATE TABLE interactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_input TEXT NOT NULL,
        response TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
"""

_HISTORICAL_NOTES_TABLE = """
    CREATE TABLE notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
"""

_HISTORICAL_MEMORIES_TABLE = """
    CREATE TABLE memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact TEXT NOT NULL CHECK (length(fact) > 0),
        normalized_fact TEXT NOT NULL UNIQUE
            CHECK (length(normalized_fact) > 0),
        created_at TEXT NOT NULL
    )
"""

_HISTORICAL_INTERACTIONS_INDEX = """
    CREATE INDEX idx_interactions_created_at
        ON interactions (created_at)
"""

_HISTORICAL_NOTES_INDEX = """
    CREATE INDEX idx_notes_title
        ON notes (title)
"""

# Newest shape first, so the most likely match is tried first.
_HISTORICAL_UNVERSIONED_SCHEMAS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        _SCHEMA_V0_2_BASELINE,
        (
            _HISTORICAL_INTERACTIONS_TABLE,
            _HISTORICAL_NOTES_TABLE,
            _HISTORICAL_MEMORIES_TABLE,
            _HISTORICAL_INTERACTIONS_INDEX,
            _HISTORICAL_NOTES_INDEX,
        ),
    ),
    (
        _SCHEMA_BEFORE_EXPLICIT_MEMORIES,
        (
            _HISTORICAL_INTERACTIONS_TABLE,
            _HISTORICAL_NOTES_TABLE,
            _HISTORICAL_INTERACTIONS_INDEX,
            _HISTORICAL_NOTES_INDEX,
        ),
    ),
)

# The oldest schema Maria can recognise at all, and therefore the version the
# migration ladder has to start one step above.
_EARLIEST_KNOWN_SCHEMA = min(
    version for version, _ in _HISTORICAL_UNVERSIONED_SCHEMAS
)


@dataclass(frozen=True)
class _Migration:
    """One forward step that brings a database up to ``version``.

    A step runs against a database already at ``version - 1`` and is applied
    in a single transaction together with the marker that records it, so the
    schema and its version can never disagree. ``name`` is a stable
    identifier that exists for the operational log: it names the step without
    describing, or being derived from, anything the database stores.
    """

    version: int
    name: str
    statements: tuple[str, ...]


# Every forward step, in ascending version order. A step is appended, never
# edited or reordered, and there is deliberately no downgrade direction.
_MIGRATIONS: tuple[_Migration, ...] = (
    _Migration(
        version=_SCHEMA_V0_2_BASELINE,
        name="create_memories",
        statements=(_CREATE_MEMORIES_TABLE,),
    ),
)



class Memory:
    """Provide all database interactions for Maria."""

    def __init__(self, database_path: Path, logger: Logger | None = None) -> None:
        self.database_path = database_path
        self.logger = logger or get_logger(__name__)

    def initialize(self) -> None:
        """Bring the configured database up to the schema this build expects.

        The database's own version is settled first and everything else
        follows from it, so no other method has to decide what shape it is
        looking at. A file stamped newer than this build is refused before a
        single byte is written; an unversioned file is classified once,
        structurally, against the shapes Maria actually shipped; a file
        already at the current version is left completely alone; and anything
        else runs the missing forward migrations in order, each one atomic
        with the marker that records it.

        Raises ``TypedFailureError`` carrying
        ``FailureKind.DATABASE_SCHEMA_TOO_NEW`` when the file belongs to a
        newer build, and ``RuntimeError`` with a fixed, content-free message
        for every other refusal or failure, including an unrecognised shape
        and a corrupt or unreadable file. Contention keeps the shared busy
        message, because it is worth retrying rather than a broken database,
        and is never reported as a newer schema.

        Nothing this boundary logs is derived from what went wrong. Every
        outcome, success and refusal alike, is recorded as a fixed sentence
        with at most a version number or a stable event code in it, so no
        path, row, statement, SQLite message, or traceback can reach the
        operational log from here.
        """
        _validate_migration_ladder()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connection(classify_contention=False) as connection:
                version = _read_schema_version(connection)
                version = self._settle_schema_version(connection, version)
                self._apply_pending_migrations(connection, version)
            self.logger.info(
                "Database ready at schema version %s.",
                DATABASE_SCHEMA_VERSION,
            )
        except sqlite3.Error as exc:
            # One fixed code and nothing else. A corrupt, truncated, or
            # unreadable file makes SQLite describe the file it failed on, so
            # neither its text, its traceback, nor the path may be recorded.
            # The exception stays available as ``__cause__`` for a caller that
            # needs it; it is simply never written down.
            if _is_busy_or_locked(exc):
                self.logger.error(_INITIALIZATION_STOPPED, _EVENT_DATABASE_BUSY)
                raise RuntimeError(_DATABASE_BUSY_MESSAGE) from exc
            self.logger.error(
                _INITIALIZATION_STOPPED,
                _EVENT_DATABASE_UNREADABLE,
            )
            raise RuntimeError(_DATABASE_REFUSED_MESSAGE) from exc

    def _settle_schema_version(
        self,
        connection: sqlite3.Connection,
        version: int,
    ) -> int:
        """Return the version to migrate from, refusing anything unusable.

        This is the single place an unversioned database is classified, so no
        later step has to guess again. It refuses before writing rather than
        after: a newer stamp, a negative stamp, and an unrecognised shape all
        leave the file exactly as it was found.
        """
        if version > DATABASE_SCHEMA_VERSION:
            self.logger.error(
                "Refused a database stamped at schema version %s; "
                "this build expects %s.",
                version,
                DATABASE_SCHEMA_VERSION,
            )
            raise TypedFailureError(Failure(FailureKind.DATABASE_SCHEMA_TOO_NEW))

        if version < _UNVERSIONED_SCHEMA:
            self.logger.error(
                "Refused a database carrying an unrecognised schema marker."
            )
            raise RuntimeError(_DATABASE_REFUSED_MESSAGE)

        if version != _UNVERSIONED_SCHEMA:
            return version

        return self._classify_unversioned_database(connection)

    def _classify_unversioned_database(
        self,
        connection: sqlite3.Connection,
    ) -> int:
        """Classify a database with no marker and stamp what it turns out to be.

        A missing marker proves only that nothing ever wrote one, so the file
        is identified by its structure rather than assumed to be the baseline.
        A file holding no object that SQLite did not create itself is a new
        database and is created directly at the current schema. Emptiness is
        decided on every user-defined object rather than on Maria's own table
        names, so another application's database is never treated as a blank
        one to build inside. A populated file is compared
        against each shape Maria shipped before stamping existed, and is
        stamped only on an exact structural match. Anything else, from a
        half-created database or a drifted column to a missing uniqueness
        constraint or a foreign database that merely shares table names, is
        refused with nothing written.
        """
        if not _user_defined_object_names(connection):
            _run_in_one_transaction(
                connection,
                _CURRENT_SCHEMA_STATEMENTS,
                DATABASE_SCHEMA_VERSION,
            )
            self.logger.info(
                "Created a new database at schema version %s.",
                DATABASE_SCHEMA_VERSION,
            )
            return DATABASE_SCHEMA_VERSION

        actual = _describe_schema(connection)
        for version, statements in _HISTORICAL_UNVERSIONED_SCHEMAS:
            if actual != _reference_schema(statements):
                continue
            _run_in_one_transaction(connection, (), version)
            self.logger.info(
                "Classified an unversioned database as schema version %s "
                "and stamped it.",
                version,
            )
            return version

        self.logger.error(
            "Refused an unversioned database whose structure matches no "
            "schema version Maria has shipped."
        )
        raise RuntimeError(_DATABASE_REFUSED_MESSAGE)

    def _apply_pending_migrations(
        self,
        connection: sqlite3.Connection,
        version: int,
    ) -> None:
        """Run every migration above ``version``, in order, one per transaction.

        Each step commits its statements and its marker together, so a failure
        leaves the database whole at the previous version with no partial
        schema, and re-running initialization resumes at exactly the step that
        failed. A database already at the current version runs nothing.
        """
        for migration in _MIGRATIONS:
            if migration.version <= version:
                continue
            try:
                _run_in_one_transaction(
                    connection,
                    migration.statements,
                    migration.version,
                )
            except sqlite3.Error as exc:
                # Converted here rather than left to the caller's handler,
                # which logs the traceback: a rolled-back migration is an
                # expected refusal, and SQLite's text for it belongs in
                # neither the log nor the owner's message.
                if _is_busy_or_locked(exc):
                    # Contention is transient and worth retrying, which is
                    # what the shared busy message says. Reporting it as a
                    # failed migration instead would tell the owner their
                    # database was unusable when it was merely in use.
                    self.logger.error(
                        "Migration %s to schema version %s did not run "
                        "because the database was busy; nothing was applied.",
                        migration.name,
                        migration.version,
                    )
                    raise RuntimeError(_DATABASE_BUSY_MESSAGE) from exc
                self.logger.error(
                    "Migration %s to schema version %s failed and was "
                    "rolled back; the database remains at version %s.",
                    migration.name,
                    migration.version,
                    migration.version - 1,
                )
                raise RuntimeError(_DATABASE_REFUSED_MESSAGE) from exc
            self.logger.info(
                "Applied migration %s; schema version is now %s.",
                migration.name,
                migration.version,
            )

    def schema_version(self) -> int:
        """Return the schema version currently stamped on the database.

        This is the only way to read the marker from outside the memory
        layer, so no caller needs its own connection or its own PRAGMA to
        find out what the database claims to be. It reports what is stored
        rather than what is expected, so a database no build ever stamped
        reports ``0``. Like every other method here it opens the configured
        file, which SQLite creates when it is missing.
        """
        try:
            with self._connection() as connection:
                return _read_schema_version(connection)
        except sqlite3.Error as exc:
            # Recorded the same way initialization records its failures: this
            # reads the same header of the same file, so an exception log here
            # would reintroduce exactly what that boundary refuses to write.
            self.logger.error(
                "Reading the database schema version stopped: %s.",
                _EVENT_DATABASE_UNREADABLE,
            )
            raise RuntimeError(_SCHEMA_VERSION_UNREADABLE_MESSAGE) from exc

    def record_interaction(self, user_input: str, response: str) -> int:
        """Record a user interaction and return its database id."""
        timestamp = _utc_timestamp()
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO interactions (user_input, response, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (user_input, response, timestamp),
                )
                interaction_id = int(cursor.lastrowid)
            self.logger.info("Recorded interaction %s.", interaction_id)
            return interaction_id
        except sqlite3.Error as exc:
            self.logger.exception("Failed to record interaction.")
            raise RuntimeError("Unable to record interaction.") from exc

    def get_recent_interactions(self, limit: int = 5) -> list[dict[str, Any]]:
        """Return recent interactions in newest-first order."""
        safe_limit = max(1, min(limit, 50))
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id, user_input, response, created_at
                    FROM interactions
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            self.logger.exception("Failed to fetch recent interactions.")
            raise RuntimeError("Unable to fetch recent interactions.") from exc

    def get_interaction(self, interaction_id: int) -> dict[str, Any] | None:
        """Return one interaction by its database id."""
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT id, user_input, response, created_at
                    FROM interactions
                    WHERE id = ?
                    """,
                    (interaction_id,),
                ).fetchone()
            return dict(row) if row else None
        except sqlite3.Error as exc:
            self.logger.exception("Failed to read interaction.")
            raise RuntimeError("Unable to read interaction.") from exc

    def search_interactions(
        self,
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return interactions whose request or response contains a literal,
        case-insensitive query.

        Matching uses SQLite's built-in LOWER(), which is the accepted
        default-build ASCII case-insensitive comparison. It is not full
        Unicode casefold equivalence (unlike explicit-memory search, which
        uses a precomputed casefolded column).
        """
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("Search query is required.")

        safe_limit = max(1, min(limit, 50))
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id, user_input, response, created_at
                    FROM interactions
                    WHERE instr(LOWER(user_input), LOWER(?)) > 0
                       OR instr(LOWER(response), LOWER(?)) > 0
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (clean_query, clean_query, safe_limit),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            self.logger.exception("Failed to search interactions.")
            raise RuntimeError("Unable to search interactions.") from exc

    def delete_interaction(self, interaction_id: int) -> bool:
        """Delete one interaction by its database id.

        Returns True if a row was deleted, False if no row matched
        interaction_id. Raises ValueError for a non-positive-integer id,
        since this is a destructive operation validated independently of
        its callers.
        """
        if (
            not isinstance(interaction_id, int)
            or isinstance(interaction_id, bool)
            or interaction_id < 1
        ):
            raise ValueError("Interaction id must be a positive whole number.")

        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    DELETE FROM interactions
                    WHERE id = ?
                    """,
                    (interaction_id,),
                )
                deleted = cursor.rowcount == 1
            if deleted:
                self.logger.info("Deleted interaction %s.", interaction_id)
            return deleted
        except sqlite3.Error as exc:
            self.logger.exception("Failed to delete interaction.")
            raise RuntimeError("Unable to delete interaction.") from exc

    def store_memory(self, fact: str) -> tuple[int, bool]:
        """Store a fact and return its database id and creation status."""
        clean_fact = fact.strip()
        if not clean_fact:
            raise ValueError("Memory fact is required.")

        normalized_fact = clean_fact.casefold()
        timestamp = _utc_timestamp()
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO memories (fact, normalized_fact, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(normalized_fact) DO NOTHING
                    """,
                    (clean_fact, normalized_fact, timestamp),
                )
                created = cursor.rowcount == 1
                if created:
                    memory_id = int(cursor.lastrowid)
                else:
                    row = connection.execute(
                        """
                        SELECT id
                        FROM memories
                        WHERE normalized_fact = ?
                        """,
                        (normalized_fact,),
                    ).fetchone()
                    if row is None:
                        raise sqlite3.IntegrityError(
                            "Unable to resolve duplicate memory."
                        )
                    memory_id = int(row["id"])
            if created:
                self.logger.info("Stored memory %s.", memory_id)
            else:
                self.logger.info("Memory %s already exists.", memory_id)
            return memory_id, created
        except sqlite3.Error as exc:
            self.logger.exception("Failed to store memory.")
            raise RuntimeError("Unable to store memory.") from exc

    def get_memory(self, memory_id: int) -> dict[str, Any] | None:
        """Return one memory by its database id."""
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT id, fact, created_at
                    FROM memories
                    WHERE id = ?
                    """,
                    (memory_id,),
                ).fetchone()
            return dict(row) if row else None
        except sqlite3.Error as exc:
            self.logger.exception("Failed to read memory.")
            raise RuntimeError("Unable to read memory.") from exc

    def search_memories(
        self,
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return memories containing a literal case-insensitive query."""
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("Memory search query is required.")

        normalized_query = clean_query.casefold()
        safe_limit = max(1, min(limit, 100))
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id, fact, created_at
                    FROM memories
                    WHERE instr(normalized_fact, ?) > 0
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (normalized_query, safe_limit),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            self.logger.exception("Failed to search memories.")
            raise RuntimeError("Unable to search memories.") from exc

    def list_memories(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return stored memories in newest-first order."""
        safe_limit = max(1, min(limit, 100))
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id, fact, created_at
                    FROM memories
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            self.logger.exception("Failed to list memories.")
            raise RuntimeError("Unable to list memories.") from exc

    def update_memory(
        self,
        memory_id: int,
        new_fact: str,
    ) -> tuple[bool, int | None]:
        """Replace one memory's fact, keeping its created_at.

        Returns (True, None) when the row was updated, (False, None) when no
        row matched memory_id, and (False, conflicting_id) when another
        memory already stores the same casefolded fact. Raises ValueError for
        a non-positive-integer id or a blank fact, since this persistent
        mutation is validated independently of its callers. created_at is
        never refreshed and no updated_at exists, so an updated memory keeps
        its place in newest-first results; a fact matching the stored one, or
        differing from it only by casing, still counts as a successful update.
        """
        if (
            not isinstance(memory_id, int)
            or isinstance(memory_id, bool)
            or memory_id < 1
        ):
            raise ValueError("Memory id must be a positive whole number.")
        if not isinstance(new_fact, str) or not new_fact.strip():
            raise ValueError("New memory fact is required.")

        clean_fact = new_fact.strip()
        normalized_fact = clean_fact.casefold()
        updated = False
        conflicting_id: int | None = None
        try:
            with self._connection() as connection:
                try:
                    cursor = connection.execute(
                        """
                        UPDATE memories
                        SET fact = ?, normalized_fact = ?
                        WHERE id = ?
                        """,
                        (clean_fact, normalized_fact, memory_id),
                    )
                    updated = cursor.rowcount == 1
                except sqlite3.IntegrityError as exc:
                    if not _is_unique_violation(exc):
                        raise
                    row = connection.execute(
                        """
                        SELECT id
                        FROM memories
                        WHERE normalized_fact = ?
                        """,
                        (normalized_fact,),
                    ).fetchone()
                    if row is None:
                        raise sqlite3.IntegrityError(
                            "Unable to resolve conflicting memory."
                        )
                    conflicting_id = int(row["id"])
            if updated:
                self.logger.info("Updated memory %s.", memory_id)
            return updated, conflicting_id
        except sqlite3.Error as exc:
            self.logger.exception("Failed to update memory.")
            raise RuntimeError("Unable to update memory.") from exc

    def forget_memory(self, memory_id: int) -> bool:
        """Delete one explicit memory by its database id.

        Returns True if a row was deleted, False if no row matched memory_id.
        Raises ValueError for a non-positive-integer id, since this is a
        destructive operation validated independently of its callers. The
        forgotten fact is never read, logged, or returned.
        """
        if (
            not isinstance(memory_id, int)
            or isinstance(memory_id, bool)
            or memory_id < 1
        ):
            raise ValueError("Memory id must be a positive whole number.")

        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    DELETE FROM memories
                    WHERE id = ?
                    """,
                    (memory_id,),
                )
                forgotten = cursor.rowcount == 1
            if forgotten:
                self.logger.info("Forgot memory %s.", memory_id)
            return forgotten
        except sqlite3.Error as exc:
            self.logger.exception("Failed to forget memory.")
            raise RuntimeError("Unable to forget memory.") from exc

    def create_note(self, title: str, body: str) -> int:
        """Create a note and return its database id."""
        clean_title = title.strip()
        clean_body = body.strip()
        if not clean_title:
            raise ValueError("Note title is required.")
        if not clean_body:
            raise ValueError("Note body is required.")

        timestamp = _utc_timestamp()
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO notes (title, body, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (clean_title, clean_body, timestamp, timestamp),
                )
                note_id = int(cursor.lastrowid)
            self.logger.info("Created note %s.", note_id)
            return note_id
        except sqlite3.Error as exc:
            self.logger.exception("Failed to create note.")
            raise RuntimeError("Unable to create note.") from exc

    def list_notes(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent notes in newest-first order."""
        safe_limit = max(1, min(limit, 100))
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id, title, created_at, updated_at
                    FROM notes
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            self.logger.exception("Failed to list notes.")
            raise RuntimeError("Unable to list notes.") from exc

    def get_note(self, note_id: int | None = None, title: str | None = None) -> dict[str, Any] | None:
        """Return one note by id or exact title."""
        if note_id is None and not title:
            raise ValueError("A note id or title is required.")

        try:
            with self._connection() as connection:
                if note_id is not None:
                    row = connection.execute(
                        """
                        SELECT id, title, body, created_at, updated_at
                        FROM notes
                        WHERE id = ?
                        """,
                        (note_id,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT id, title, body, created_at, updated_at
                        FROM notes
                        WHERE title = ?
                        ORDER BY updated_at DESC, id DESC
                        LIMIT 1
                        """,
                        (title,),
                    ).fetchone()
            return dict(row) if row else None
        except sqlite3.Error as exc:
            self.logger.exception("Failed to read note.")
            raise RuntimeError("Unable to read note.") from exc

    def retitle_note(self, note_id: int, new_title: str) -> bool:
        """Replace one note's title, keeping its body and created_at.

        Returns True if a row was updated, False if no row matched note_id.
        Raises ValueError for a non-positive-integer id or a blank title,
        since this persistent mutation is validated independently of its
        callers. updated_at receives the current UTC timestamp even when
        new_title already matches the stored title.
        """
        if (
            not isinstance(note_id, int)
            or isinstance(note_id, bool)
            or note_id < 1
        ):
            raise ValueError("Note id must be a positive whole number.")
        if not isinstance(new_title, str) or not new_title.strip():
            raise ValueError("New note title is required.")

        clean_title = new_title.strip()
        timestamp = _utc_timestamp()
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE notes
                    SET title = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (clean_title, timestamp, note_id),
                )
                retitled = cursor.rowcount == 1
            if retitled:
                self.logger.info("Retitled note %s.", note_id)
            return retitled
        except sqlite3.Error as exc:
            self.logger.exception("Failed to retitle note.")
            raise RuntimeError("Unable to retitle note.") from exc

    def edit_note(self, note_id: int, new_body: str) -> bool:
        """Replace one note's body, keeping its title and created_at.

        Returns True if a row was updated, False if no row matched note_id.
        Raises ValueError for a non-positive-integer id or a blank body,
        since this persistent mutation is validated independently of its
        callers. updated_at receives the current UTC timestamp even when
        new_body already matches the stored body.
        """
        if (
            not isinstance(note_id, int)
            or isinstance(note_id, bool)
            or note_id < 1
        ):
            raise ValueError("Note id must be a positive whole number.")
        if not isinstance(new_body, str) or not new_body.strip():
            raise ValueError("New note body is required.")

        clean_body = new_body.strip()
        timestamp = _utc_timestamp()
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE notes
                    SET body = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (clean_body, timestamp, note_id),
                )
                edited = cursor.rowcount == 1
            if edited:
                self.logger.info("Edited note %s.", note_id)
            return edited
        except sqlite3.Error as exc:
            self.logger.exception("Failed to edit note.")
            raise RuntimeError("Unable to edit note.") from exc

    def delete_note(self, note_id: int) -> bool:
        """Delete one note by its database id.

        Returns True if a row was deleted, False if no row matched note_id.
        Raises ValueError for a non-positive-integer id, since this is a
        destructive operation validated independently of its callers. The
        deleted title and body are never read, logged, or returned.
        """
        if (
            not isinstance(note_id, int)
            or isinstance(note_id, bool)
            or note_id < 1
        ):
            raise ValueError("Note id must be a positive whole number.")

        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    DELETE FROM notes
                    WHERE id = ?
                    """,
                    (note_id,),
                )
                deleted = cursor.rowcount == 1
            if deleted:
                self.logger.info("Deleted note %s.", note_id)
            return deleted
        except sqlite3.Error as exc:
            self.logger.exception("Failed to delete note.")
            raise RuntimeError("Unable to delete note.") from exc

    @contextmanager
    def _connection(
        self,
        *,
        classify_contention: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        """Open a SQLite connection and close it after use.

        Contention is normally recognised and converted here, so every method
        answers a busy database with the same message. ``classify_contention``
        turns that off for the initialization boundary, which classifies the
        error itself: the conversion carries an exception log with it, and
        that boundary records a fixed event code instead. Rollback and close
        are unaffected either way, and every other error still propagates
        unchanged.
        """
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            if classify_contention and _is_busy_or_locked(exc):
                self.logger.exception("Database busy or locked.")
                raise RuntimeError(_DATABASE_BUSY_MESSAGE) from exc
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        """Open a SQLite connection with row dictionaries enabled."""
        connection = sqlite3.connect(
            self.database_path,
            timeout=DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS,
        )
        connection.row_factory = sqlite3.Row
        return connection


def _is_busy_or_locked(exc: sqlite3.Error) -> bool:
    """Return whether exc's primary SQLite result code is BUSY or LOCKED."""
    error_code = getattr(exc, "sqlite_errorcode", None)
    if not isinstance(error_code, int):
        return False
    primary_code = error_code & _SQLITE_PRIMARY_RESULT_CODE_MASK
    return primary_code in _BUSY_OR_LOCKED_PRIMARY_CODES


def _is_unique_violation(exc: sqlite3.Error) -> bool:
    """Return whether exc is SQLite's extended UNIQUE constraint code."""
    error_code = getattr(exc, "sqlite_errorcode", None)
    if not isinstance(error_code, int):
        return False
    return error_code == sqlite3.SQLITE_CONSTRAINT_UNIQUE


def _utc_timestamp() -> str:
    """Return the current UTC timestamp as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _validate_migration_ladder() -> None:
    """Refuse a migration set that cannot reach the expected schema version.

    A ladder with a gap, a repeat, a step below the oldest recognisable
    schema, or a step beyond what this build expects would migrate a database
    somewhere no code is written for, so it is rejected before the database is
    even opened rather than discovered part-way up. The check is on the set of
    declared steps alone and reads nothing from disk.
    """
    expected = tuple(
        range(_EARLIEST_KNOWN_SCHEMA + 1, DATABASE_SCHEMA_VERSION + 1)
    )
    declared = tuple(migration.version for migration in _MIGRATIONS)
    if declared != expected:
        raise RuntimeError(_DATABASE_REFUSED_MESSAGE)


def _read_schema_version(connection: sqlite3.Connection) -> int:
    """Return the schema marker stored in the database header.

    ``PRAGMA user_version`` is read rather than a table, so an empty file, a
    legacy file, and a stamped file all answer the same way without anything
    being created to ask the question. SQLite answers 0 for a database no
    build ever stamped.
    """
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def _write_schema_version(connection: sqlite3.Connection, version: int) -> None:
    """Stamp the database header with an exact, validated schema version.

    ``PRAGMA`` refuses bound parameters, so the number is interpolated. It is
    settled as a genuine non-negative ``int`` first - booleans included, since
    ``bool`` is an ``int`` subclass - so only a value this module produced can
    reach the statement text.
    """
    if type(version) is not int or version < 0:
        raise ValueError("A schema version must be a non-negative whole number.")
    connection.execute(f"PRAGMA user_version = {version:d}")


def _run_in_one_transaction(
    connection: sqlite3.Connection,
    statements: tuple[str, ...],
    version: int,
) -> None:
    """Apply statements and their version stamp as a single atomic step.

    The transaction is opened explicitly because it has to be. Python's
    sqlite3 driver begins one implicitly only for row-changing statements, so
    schema statements would otherwise each commit on their own and a failure
    part-way through would leave a half-built schema behind. Inside an
    explicit transaction SQLite rolls back schema changes and the header
    marker together, so the database is either entirely at ``version`` or
    entirely at what it was before.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in statements:
            connection.execute(statement)
        _write_schema_version(connection, version)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _user_defined_object_names(
    connection: sqlite3.Connection,
) -> tuple[str, ...]:
    """Return every object the database holds that SQLite did not create.

    Tables, indexes, views, and triggers all count, because this settles
    whether a file is a new database or somebody's existing one, and a file
    holding a view or a trigger is nobody's idea of empty. Deciding that on
    Maria's own table names would be worse still: a database full of another
    application's tables would look empty and have Maria's schema created
    inside it.

    Only SQLite's own objects are excluded, by the ``sqlite_`` prefix it
    reserves for them. ``sqlite_sequence`` appears as soon as any
    ``AUTOINCREMENT`` table exists and can outlive it, so counting it would
    make a database that is genuinely empty look populated.
    """
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite~_%' ESCAPE '~'
        ORDER BY name
        """
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _describe_schema(connection: sqlite3.Connection) -> tuple[Any, ...]:
    """Return a comparable structural description of a database's schema.

    Two databases compare equal here exactly when SQLite reports the same
    tables, the same columns in the same order with the same declared types,
    nullability, defaults, and primary-key positions, the same indexes over
    the same columns with the same uniqueness and origin, and the same foreign
    keys.

    The description is built from SQLite's own catalogue rather than from the
    text of the stored ``CREATE`` statements, so a database that was written
    with different indentation or a reordered constraint clause is recognised
    on what it actually is. The one exception is ``AUTOINCREMENT``, which no
    pragma reports and which changes whether a deleted row's id can come back;
    it is read as a keyword from the stored statement rather than by comparing
    that statement as a whole.

    ``CHECK`` constraints are compared too, because a column whose check is
    missing or weakened accepts rows the real baseline refuses, and no pragma
    reports one. They are the one part read from the stored statement, and
    only the checks themselves are read from it: each is lifted out as its own
    expression and reduced to tokens, so whitespace, line breaks, casing,
    identifier quoting, comments, and redundant parentheses around the same
    condition all compare equal while a different condition does not. The
    statement as a whole is never compared.
    """
    rows = connection.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite~_%' ESCAPE '~'
        ORDER BY name
        """
    ).fetchall()
    return tuple(
        _describe_table(connection, str(row[0]), str(row[1] or ""))
        for row in rows
    )


def _describe_table(
    connection: sqlite3.Connection,
    table_name: str,
    definition: str,
) -> tuple[Any, ...]:
    """Return one table's structural description.

    Every pragma is read through its table-valued function so the table name
    travels as a bound parameter. A drifted or foreign database supplies these
    names, and a name is not something to paste into statement text.

    ``AUTOINCREMENT`` and the table's ``CHECK`` constraints are the two things
    no pragma reports, so both are read from the stored statement's tokens
    rather than from its text. A check declared beside its column and the same
    check declared for the table compare alike, because SQLite evaluates both
    per row and the distinction is a spelling rather than a rule.
    """
    columns = tuple(
        (
            str(row["name"]),
            str(row["type"]).upper(),
            bool(row["notnull"]),
            None if row["dflt_value"] is None else str(row["dflt_value"]),
            int(row["pk"]),
        )
        for row in connection.execute(
            """
            SELECT name, type, "notnull", dflt_value, pk
            FROM pragma_table_info(?)
            ORDER BY cid
            """,
            (table_name,),
        )
    )

    indexes = []
    for row in connection.execute(
        """
        SELECT name, "unique", origin, partial
        FROM pragma_index_list(?)
        """,
        (table_name,),
    ):
        index_name = str(row["name"])
        origin = str(row["origin"])
        index_columns = tuple(
            str(entry["name"])
            for entry in connection.execute(
                """
                SELECT name
                FROM pragma_index_info(?)
                ORDER BY seqno
                """,
                (index_name,),
            )
        )
        indexes.append(
            (
                # An index SQLite created for a UNIQUE or PRIMARY KEY clause
                # carries a generated name that says nothing about the
                # schema, so only an explicitly created index is compared by
                # name. What such an index covers is still compared in full.
                index_name if origin == "c" else "",
                bool(row["unique"]),
                origin,
                bool(row["partial"]),
                index_columns,
            )
        )

    foreign_keys = tuple(
        sorted(
            (
                str(row["table"]),
                str(row["from"]),
                None if row["to"] is None else str(row["to"]),
                str(row["on_update"]),
                str(row["on_delete"]),
            )
            for row in connection.execute(
                """
                SELECT "table", "from", "to", on_update, on_delete
                FROM pragma_foreign_key_list(?)
                """,
                (table_name,),
            )
        )
    )

    tokens = _tokenize_definition(definition)
    return (
        table_name,
        columns,
        tuple(sorted(indexes)),
        foreign_keys,
        _declares_autoincrement(tokens),
        _check_constraints(tokens),
    )


# The closing character for each way SQLite lets an identifier be quoted.
_IDENTIFIER_QUOTES = {'"': '"', "`": "`", "[": "]"}

# Characters that continue a bare identifier or number once one has started.
_WORD_TAIL = "_$"


def _tokenize_definition(definition: str) -> tuple[tuple[str, str], ...]:
    """Reduce a stored CREATE statement to comparable tokens.

    This is the one place a schema statement is read as text, and it exists so
    that nothing else has to. Whitespace disappears, comments disappear,
    keywords and identifiers are casefolded, and an identifier keeps only its
    name whichever of SQLite's quoting styles it arrived in, so two statements
    that declare the same thing produce the same tokens however they were
    written. String literals are kept whole and marked as literals, so text
    that merely looks like SQL inside one can never be mistaken for the real
    thing.

    Nothing here interprets the statement. It has no notion of columns,
    constraints, or clauses; callers that need one work from the tokens.
    """
    tokens: list[tuple[str, str]] = []
    index = 0
    end = len(definition)

    while index < end:
        character = definition[index]

        if character.isspace():
            index += 1
            continue

        # A comment is not part of what the statement declares, and leaving it
        # in the stream would let commented-out text be read as a real clause.
        if definition.startswith("--", index):
            line_end = definition.find("\n", index)
            index = end if line_end == -1 else line_end + 1
            continue
        if definition.startswith("/*", index):
            comment_end = definition.find("*/", index + 2)
            index = end if comment_end == -1 else comment_end + 2
            continue

        if character == "'":
            literal, index = _read_delimited(definition, index, "'")
            tokens.append(("literal", literal))
            continue

        if character in _IDENTIFIER_QUOTES:
            name, index = _read_delimited(
                definition,
                index,
                _IDENTIFIER_QUOTES[character],
            )
            tokens.append(("word", name.casefold()))
            continue

        if character.isalnum() or character in _WORD_TAIL:
            start = index
            while index < end and (
                definition[index].isalnum() or definition[index] in _WORD_TAIL
            ):
                index += 1
            tokens.append(("word", definition[start:index].casefold()))
            continue

        tokens.append(("punctuation", character))
        index += 1

    return tuple(tokens)


def _read_delimited(
    definition: str,
    start: int,
    closing: str,
) -> tuple[str, int]:
    """Return one quoted run's contents and the index just past its close.

    ``start`` addresses the opening delimiter. A doubled closing delimiter is
    SQLite's escape for a literal one, so it is consumed as content rather
    than ending the run. An unterminated run ends at the end of the statement
    instead of raising: a malformed definition has to be describable so that
    the classifier can refuse it, rather than aborting the inspection.
    """
    contents: list[str] = []
    index = start + 1
    end = len(definition)
    while index < end:
        if definition[index] == closing:
            if definition.startswith(closing * 2, index):
                contents.append(closing)
                index += 2
                continue
            return "".join(contents), index + 1
        contents.append(definition[index])
        index += 1
    return "".join(contents), end


def _declares_autoincrement(tokens: tuple[tuple[str, str], ...]) -> bool:
    """Return whether a tokenized CREATE TABLE declares AUTOINCREMENT.

    The keyword is looked for as a whole token, so spacing, line breaks, and
    casing do not matter, while a column merely named something similar, a
    string literal containing the word, and a comment mentioning it all
    cannot match.
    """
    return ("word", "autoincrement") in tokens


def _check_constraints(
    tokens: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    """Return every CHECK expression a tokenized CREATE TABLE declares.

    Each expression is returned as its tokens joined together, which is a
    normalized form rather than the text that produced it: two statements
    spelling the same condition with different spacing, casing, identifier
    quoting, or redundant parentheses yield the same string, while a condition
    that was weakened or removed does not. The results are sorted, so the
    order the checks were written in does not matter either.

    Only the expression inside each ``CHECK`` is taken. The surrounding
    statement contributes nothing, so this can never become an equality test
    on stored SQL by another name.
    """
    expressions: list[str] = []
    position = 0
    total = len(tokens)

    while position < total:
        if tokens[position] != ("word", "check"):
            position += 1
            continue
        opening = position + 1
        if opening >= total or tokens[opening] != ("punctuation", "("):
            position += 1
            continue
        expression, position = _read_parenthesized(tokens, opening)
        expressions.append(_strip_redundant_parentheses(expression))

    return tuple(sorted(expressions))


def _read_parenthesized(
    tokens: tuple[tuple[str, str], ...],
    opening: int,
) -> tuple[str, int]:
    """Return one balanced parenthesized group's contents and the next index.

    ``opening`` addresses the group's own ``(``, which is syntax rather than
    part of the condition and is therefore dropped along with its closing
    partner. Nesting inside is kept, because it is part of the expression. A
    literal contributes its text without its quotes having to be reproduced,
    since a literal is already a single token by the time it arrives here.
    """
    contents: list[str] = []
    depth = 0
    index = opening
    total = len(tokens)

    while index < total:
        kind, value = tokens[index]
        if kind == "punctuation" and value == "(":
            depth += 1
            if depth > 1:
                contents.append(value)
        elif kind == "punctuation" and value == ")":
            depth -= 1
            if depth == 0:
                return "".join(contents), index + 1
            contents.append(value)
        else:
            contents.append(value)
        index += 1

    return "".join(contents), total


def _strip_redundant_parentheses(expression: str) -> str:
    """Remove parentheses that wrap a whole expression without changing it.

    ``(length(fact)>0)`` and ``length(fact)>0`` state the same condition, so
    they have to compare equal. A pair is only removed when it genuinely
    encloses everything: ``(a)>(b)`` opens and closes before the end, so it is
    left exactly as it is.
    """
    while expression.startswith("(") and expression.endswith(")"):
        depth = 0
        for position, character in enumerate(expression):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and position < len(expression) - 1:
                    return expression
        expression = expression[1:-1]
    return expression


def _reference_schema(statements: tuple[str, ...]) -> tuple[Any, ...]:
    """Return the description a set of schema statements produces.

    The statements are built into a private in-memory database and described
    by the same function that describes a real one, so the expected shape is
    whatever that schema genuinely creates rather than a hand-written
    transcription of it that could quietly disagree. Nothing is opened on
    disk and nothing is kept.
    """
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        for statement in statements:
            connection.execute(statement)
        return _describe_schema(connection)
    finally:
        connection.close()
