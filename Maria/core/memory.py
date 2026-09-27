"""SQLite-backed memory layer for Maria."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
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
_SCHEMA_VERSION_READ_STOPPED = (
    "Reading the database schema version stopped: %s."
)

# SQLite's default value for ``PRAGMA user_version``. It is not a Maria schema
# version: it means no marker was ever written, so the database has to be
# classified by its structure before anything is done to it.
_UNVERSIONED_SCHEMA = 0

# Maria's schema history, as a plain counter. Version 1 is the earliest shape
# Maria ever created, before explicit memories existed. Version 2 adds the
# ``memories`` table and is the published v0.2.0 baseline. Version 3 adds the
# per-memory context-consent column. The current version lives in
# ``core.constants`` as ``DATABASE_SCHEMA_VERSION``.
_SCHEMA_BEFORE_EXPLICIT_MEMORIES = 1
_SCHEMA_V0_2_BASELINE = 2
_SCHEMA_MEMORY_CONTEXT_CONSENT = 3

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

# ``context_enabled`` is the owner's per-memory consent to supply that memory
# to the local reasoning model. It is off for every row unless the owner turns
# it on for one memory at a time, and the CHECK admits nothing but 0 and 1.
_CREATE_MEMORIES_TABLE = """
    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact TEXT NOT NULL CHECK (length(fact) > 0),
        normalized_fact TEXT NOT NULL UNIQUE
            CHECK (length(normalized_fact) > 0),
        created_at TEXT NOT NULL,
        context_enabled INTEGER NOT NULL DEFAULT 0
            CHECK (context_enabled IN (0, 1))
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

_HISTORICAL_MEMORIES_TABLE_WITH_CONTEXT_CONSENT = """
    CREATE TABLE memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact TEXT NOT NULL CHECK (length(fact) > 0),
        normalized_fact TEXT NOT NULL UNIQUE
            CHECK (length(normalized_fact) > 0),
        created_at TEXT NOT NULL,
        context_enabled INTEGER NOT NULL DEFAULT 0
            CHECK (context_enabled IN (0, 1))
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

# The one statement the version 3 step runs. SQLite records an added column in
# the table's stored definition, and that definition fingerprints exactly as
# the frozen version 3 table above does, so a migrated database and a newly
# created one are the same schema. The DEFAULT is what turns consent off for
# every row that already exists; the statement rewrites no row.
_ADD_MEMORY_CONTEXT_CONSENT_COLUMN = """
    ALTER TABLE memories ADD COLUMN context_enabled INTEGER NOT NULL DEFAULT 0
        CHECK (context_enabled IN (0, 1))
"""

# Every version's complete schema, newest first. A marker is never taken at
# its word: whatever version a database claims, its structure is checked
# against the entry for that version before anything is migrated, so a file
# stamped 1 that is not actually a version 1 database is refused rather than
# migrated into an unknown state. Every version from the earliest recognised
# one up to DATABASE_SCHEMA_VERSION therefore needs an entry here, and the
# migration ladder refuses to run if one is missing.
_FROZEN_SCHEMAS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        _SCHEMA_MEMORY_CONTEXT_CONSENT,
        (
            _HISTORICAL_INTERACTIONS_TABLE,
            _HISTORICAL_NOTES_TABLE,
            _HISTORICAL_MEMORIES_TABLE_WITH_CONTEXT_CONSENT,
            _HISTORICAL_INTERACTIONS_INDEX,
            _HISTORICAL_NOTES_INDEX,
        ),
    ),
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
_EARLIEST_KNOWN_SCHEMA = min(version for version, _ in _FROZEN_SCHEMAS)

# The only shapes that can exist with no marker at all, newest first. Maria
# stamped nothing before schema versioning existed, so an unversioned file can
# only have been produced by one of these two; every database written from
# this build onwards carries a marker. Later versions are frozen above for
# validation but are deliberately not candidates here, because a file at one
# of them could not have reached that shape without also being stamped.
_UNVERSIONED_SHAPE_VERSIONS: tuple[int, ...] = (
    _SCHEMA_V0_2_BASELINE,
    _SCHEMA_BEFORE_EXPLICIT_MEMORIES,
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
#
# A step runs frozen statements, never the live ones at the top of this module.
# The live ``memories`` statement now describes version 3; had the version 2
# step kept using it, a version 1 database would receive a version 3 table
# stamped as version 2, the version 3 step would then fail on the column that
# already exists, and the database would be refused at every later start.
_MIGRATIONS: tuple[_Migration, ...] = (
    _Migration(
        version=_SCHEMA_V0_2_BASELINE,
        name="create_memories",
        statements=(_HISTORICAL_MEMORIES_TABLE,),
    ),
    _Migration(
        version=_SCHEMA_MEMORY_CONTEXT_CONSENT,
        name="add_memory_context_consent",
        statements=(_ADD_MEMORY_CONTEXT_CONSENT_COLUMN,),
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

        This is the single place a database's version is settled, so no later
        step has to guess again. An unversioned file is classified by its
        structure; a stamped one has its structure confirmed against the
        frozen schema for the version it claims, because a marker records what
        wrote a database rather than what it currently is. It refuses before
        writing rather than after: a newer stamp, a negative stamp, an
        unrecognised shape, and a marker that disagrees with the schema all
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
            self._validate_stamped_schema(connection, version)
            return version

        return self._classify_unversioned_database(connection)

    def _validate_stamped_schema(
        self,
        connection: sqlite3.Connection,
        version: int,
    ) -> None:
        """Refuse a stamped database that is not the schema it claims to be.

        A marker records what wrote the database, not what the database
        currently is. Trusting it would let a migration run against a schema
        the step was never written for, which is the one situation a forward
        migration cannot recover from, so the structure is confirmed against
        the frozen schema for that version first and nothing is written if it
        does not match.

        This also settles a database already at the current version: the check
        is entirely read-only, so a valid file is left byte for byte as it was
        found, while a drifted one is refused rather than reported as ready.
        """
        statements = _frozen_schema(version)
        if statements is not None and _describe_schema(
            connection
        ) == _reference_schema(statements):
            return

        self.logger.error(
            "Refused a database stamped at schema version %s whose structure "
            "is not the frozen schema for that version.",
            version,
        )
        raise RuntimeError(_DATABASE_REFUSED_MESSAGE)

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
        for version in _UNVERSIONED_SHAPE_VERSIONS:
            if actual != _reference_schema(_frozen_schema(version)):
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

        A busy or locked database raises the same retryable message every
        other method answers contention with; anything else SQLite refuses on
        raises the read failure below. Neither records anything derived from
        the failure, only a fixed event code.
        """
        try:
            # Contention is classified here rather than by the shared
            # connection, for the same reason initialization classifies its
            # own: the shared conversion logs the exception on its way past,
            # and this boundary records a fixed event code instead.
            with self._connection(classify_contention=False) as connection:
                return _read_schema_version(connection)
        except sqlite3.Error as exc:
            if _is_busy_or_locked(exc):
                self.logger.error(
                    _SCHEMA_VERSION_READ_STOPPED,
                    _EVENT_DATABASE_BUSY,
                )
                raise RuntimeError(_DATABASE_BUSY_MESSAGE) from exc
            self.logger.error(
                _SCHEMA_VERSION_READ_STOPPED,
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
    ) -> tuple[bool, int | None, bool]:
        """Replace one memory's fact, keeping its created_at.

        Returns (True, None, consent_cleared) when the row was updated,
        (False, None, False) when no row matched memory_id, and
        (False, conflicting_id, False) when another memory already stores the
        same casefolded fact. Raises ValueError for a non-positive-integer id
        or a blank fact, since this persistent mutation is validated
        independently of its callers. created_at is never refreshed and no
        updated_at exists, so an updated memory keeps its place in
        newest-first results; a fact matching the stored one, or differing
        from it only by casing, still counts as a successful update.

        Context consent belongs to the exact text the owner approved, so an
        update always leaves the memory's consent off. It is turned off in
        the same transaction as the new fact is written, and
        ``consent_cleared`` reports whether it had been on, which is the one
        case the owner needs to be told about. A conflicting or missing row
        changes nothing, consent included.
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
        consent_cleared = False
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
                    if updated:
                        # Same transaction as the fact: the first UPDATE
                        # already holds the write lock, so no other
                        # connection can enable consent in between, and a
                        # failure here rolls the new fact back with it.
                        consent_cursor = connection.execute(
                            """
                            UPDATE memories
                            SET context_enabled = 0
                            WHERE id = ? AND context_enabled = 1
                            """,
                            (memory_id,),
                        )
                        consent_cleared = consent_cursor.rowcount == 1
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
            if consent_cleared:
                self.logger.info(
                    "Turned off context consent for updated memory %s.",
                    memory_id,
                )
            return updated, conflicting_id, consent_cleared
        except sqlite3.Error as exc:
            self.logger.exception("Failed to update memory.")
            raise RuntimeError("Unable to update memory.") from exc

    def set_memory_context(self, memory_id: int, enabled: bool) -> bool:
        """Turn one memory's context consent on or off by its database id.

        Returns True when a row matched memory_id, including when its consent
        was already in the requested state, and False when none did. At most
        one row can change, because the id is the primary key. Raises
        ValueError for a non-positive-integer id or a non-bool ``enabled``,
        since this persistent mutation is validated independently of its
        callers. The memory's fact is never read, logged, or returned.
        """
        if (
            not isinstance(memory_id, int)
            or isinstance(memory_id, bool)
            or memory_id < 1
        ):
            raise ValueError("Memory id must be a positive whole number.")
        if type(enabled) is not bool:
            raise ValueError("Memory context consent must be True or False.")

        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE memories
                    SET context_enabled = ?
                    WHERE id = ?
                    """,
                    (1 if enabled else 0, memory_id),
                )
                matched = cursor.rowcount == 1
            if matched:
                self.logger.info(
                    "%s context consent for memory %s.",
                    "Enabled" if enabled else "Disabled",
                    memory_id,
                )
            return matched
        except sqlite3.Error as exc:
            self.logger.exception("Failed to change memory context consent.")
            raise RuntimeError("Unable to change memory context consent.") from exc

    def list_context_memories(self, limit: int) -> list[dict[str, Any]]:
        """Return context-enabled memories in ascending id order, bounded.

        This is the only read of Tier A context. At most ``limit`` rows come
        back, clamped to 1 through 100, each holding only ``id`` and
        ``fact``: a caller that wants to know whether more exist asks for one
        more row than it will use, rather than counting them all.
        """
        safe_limit = max(1, min(limit, 100))
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id, fact
                    FROM memories
                    WHERE context_enabled = 1
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            self.logger.exception("Failed to read memory context.")
            raise RuntimeError("Unable to read memory context.") from exc

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
    even opened rather than discovered part-way up. A version with no frozen
    schema is rejected for the same reason: a database claiming it could not
    be checked before being migrated. The check is on the declared steps and
    frozen schemas alone and reads nothing from disk.
    """
    expected = tuple(
        range(_EARLIEST_KNOWN_SCHEMA + 1, DATABASE_SCHEMA_VERSION + 1)
    )
    declared = tuple(migration.version for migration in _MIGRATIONS)
    if declared != expected:
        raise RuntimeError(_DATABASE_REFUSED_MESSAGE)

    # Every version a marker could name has to have a frozen schema, or a
    # database claiming that version could not be checked before it was
    # migrated. A missing entry is a build that cannot validate what it is
    # about to change, so it refuses to start rather than trusting the marker.
    frozen = tuple(sorted(version for version, _ in _FROZEN_SCHEMAS))
    if frozen != tuple(
        range(_EARLIEST_KNOWN_SCHEMA, DATABASE_SCHEMA_VERSION + 1)
    ):
        raise RuntimeError(_DATABASE_REFUSED_MESSAGE)


def _frozen_schema(version: int) -> tuple[str, ...] | None:
    """Return the complete frozen schema for one version, or None.

    A version with no frozen schema cannot be validated, which is a refusal
    rather than a reason to skip the check; returning None rather than raising
    keeps that decision with the caller that knows what it was about to do.
    """
    for frozen_version, statements in _FROZEN_SCHEMAS:
        if frozen_version == version:
            return statements
    return None


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


def _describe_schema(
    connection: sqlite3.Connection,
) -> tuple[_ObjectShape, ...]:
    """Return a comparable fingerprint of everything a database declares.

    Two databases compare equal here exactly when they declare the same
    objects with the same structure. The fingerprint is deliberately
    comprehensive rather than sufficient for the cases anyone thought of: it
    covers every user-defined object, not only tables, and for each one it
    records everything SQLite will report plus a typed canonical form of the
    statement that created it. Anything it does not recognise therefore
    compares unequal and is refused, which is the direction a schema
    classifier has to fail in.

    Objects SQLite maintains itself are excluded by the ``sqlite_`` prefix it
    reserves for them, because they follow from the schema rather than
    defining it. Everything else counts, so a stray view or trigger beside an
    otherwise exact schema is a difference rather than something to overlook.
    """
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite~_%' ESCAPE '~'
        ORDER BY type, name
        """
    ).fetchall()
    return tuple(
        _describe_object(
            connection,
            str(row["type"]),
            str(row["name"]),
            str(row["tbl_name"]),
            str(row["sql"] or ""),
        )
        for row in rows
    )


@dataclass(frozen=True)
class _ObjectShape:
    """Everything one schema object declares, in comparable form.

    Two objects are the same object exactly when every field here matches.
    The fields divide into what SQLite reports about an object and what only
    its statement says, and both halves matter: the catalogue knows about
    columns, indexes, and table options, while a conflict clause, a collation
    on a column no index covers, and a trigger body live only in the
    statement.

    Nothing here is the stored SQL. ``statement`` is that statement reduced to
    typed tokens, which is why formatting cannot make two identical schemas
    differ, and ``checks`` is lifted out of it so that a constraint written
    beside its column and the same constraint written for the table compare
    alike.
    """

    object_type: str
    name: str
    owner: str
    statement: tuple[tuple[str, str], ...]
    checks: tuple[tuple[tuple[str, str], ...], ...]
    autoincrement: bool
    catalogue: tuple[Any, ...] | None = None
    columns: tuple[tuple[Any, ...], ...] = ()
    indexes: tuple[tuple[Any, ...], ...] = ()
    foreign_keys: tuple[tuple[Any, ...], ...] = ()


def _describe_object(
    connection: sqlite3.Connection,
    object_type: str,
    name: str,
    owner: str,
    definition: str,
) -> _ObjectShape:
    """Return one object's full description, whatever kind of object it is.

    Every object contributes its kind, its name, the table it belongs to, and
    the typed canonical form of its statement. A table or a view contributes
    its catalogue metadata as well, which is where SQLite reports the facts it
    is willing to report structurally.

    The canonical statement is what makes this exhaustive. Anything SQLite
    exposes no pragma for - a conflict clause, a collation on a column no
    index covers, a table option, the body of a trigger - still differs here
    when it differs in the schema, so the description does not have to be
    extended every time a new way of writing an incompatible table is found.
    """
    tokens = _tokenize_definition(definition)
    shape = _ObjectShape(
        object_type=object_type,
        name=name,
        owner=owner,
        statement=_canonical_statement(tokens),
        checks=_check_constraints(tokens),
        autoincrement=_declares_autoincrement(tokens),
    )
    if object_type not in _CONTAINER_OBJECT_TYPES:
        return shape

    catalogue, columns, indexes, foreign_keys = _describe_container(
        connection,
        name,
    )
    return replace(
        shape,
        catalogue=catalogue,
        columns=columns,
        indexes=indexes,
        foreign_keys=foreign_keys,
    )


def _describe_container(
    connection: sqlite3.Connection,
    name: str,
) -> tuple[Any, ...]:
    """Return the catalogue metadata SQLite reports for a table or view.

    Every pragma is read through its table-valued function, so the object name
    travels as a bound parameter. A drifted or foreign database supplies these
    names, and a name is not something to paste into statement text.

    ``pragma_table_xinfo`` is used rather than ``pragma_table_info`` because
    the latter omits hidden and generated columns entirely, which would let a
    table carrying an extra generated column describe identically to one
    without it. ``pragma_index_xinfo`` is used rather than
    ``pragma_index_info`` for the same reason: only the extended form reports
    sort direction, collation, and which entries are key columns rather than
    the rowid SQLite appends.
    """
    catalogue_row = connection.execute(
        """
        SELECT type, ncol, wr, "strict"
        FROM pragma_table_list(?)
        """,
        (name,),
    ).fetchone()
    catalogue = (
        None
        if catalogue_row is None
        else (
            str(catalogue_row["type"]),
            int(catalogue_row["ncol"]),
            bool(catalogue_row["wr"]),
            bool(catalogue_row["strict"]),
        )
    )

    columns = tuple(
        (
            int(row["cid"]),
            str(row["name"]),
            str(row["type"]).upper(),
            bool(row["notnull"]),
            None if row["dflt_value"] is None else str(row["dflt_value"]),
            int(row["pk"]),
            int(row["hidden"]),
        )
        for row in connection.execute(
            """
            SELECT cid, name, type, "notnull", dflt_value, pk, hidden
            FROM pragma_table_xinfo(?)
            ORDER BY cid
            """,
            (name,),
        )
    )

    indexes = []
    for row in connection.execute(
        """
        SELECT name, "unique", origin, partial
        FROM pragma_index_list(?)
        """,
        (name,),
    ):
        index_name = str(row["name"])
        origin = str(row["origin"])
        entries = tuple(
            (
                int(entry["seqno"]),
                int(entry["cid"]),
                None if entry["name"] is None else str(entry["name"]),
                bool(entry["desc"]),
                str(entry["coll"]).upper(),
                bool(entry["key"]),
            )
            for entry in connection.execute(
                """
                SELECT seqno, cid, name, "desc", coll, "key"
                FROM pragma_index_xinfo(?)
                ORDER BY seqno
                """,
                (index_name,),
            )
        )
        indexes.append(
            (
                # An index SQLite created for a UNIQUE or PRIMARY KEY clause
                # carries a generated name that says nothing about the schema,
                # so only an explicitly created index is compared by name.
                # What every index covers is compared in full either way.
                index_name if origin == "c" else "",
                bool(row["unique"]),
                origin,
                bool(row["partial"]),
                entries,
            )
        )

    foreign_keys = tuple(
        sorted(
            (
                int(row["id"]),
                int(row["seq"]),
                str(row["table"]),
                str(row["from"]),
                None if row["to"] is None else str(row["to"]),
                str(row["on_update"]),
                str(row["on_delete"]),
                str(row["match"]),
            )
            for row in connection.execute(
                """
                SELECT id, seq, "table", "from", "to",
                       on_update, on_delete, "match"
                FROM pragma_foreign_key_list(?)
                """,
                (name,),
            )
        )
    )

    return (catalogue, columns, tuple(sorted(indexes)), foreign_keys)


# Objects SQLite will describe through ``pragma_table_list``. An index or a
# trigger has no such entry and is compared on its statement alone.
_CONTAINER_OBJECT_TYPES = frozenset({"table", "view"})


# --- Typed canonical tokens ---------------------------------------------
#
# Some of what a schema means is visible only in the statement that created
# it: whether a primary key is an AUTOINCREMENT one, what a CHECK actually
# tests, which conflict resolution a UNIQUE clause carries. Comparing that
# text directly would make indentation a schema change, so the statement is
# reduced to typed tokens first and the tokens are what is compared.
#
# Typed is the operative word. Every token keeps the kind of thing it is, so
# the canonical form can drop everything that carries no meaning - whitespace,
# line breaks, comments, casing, the choice of quoting - without ever letting
# two different things become the same one. A number and a string that both
# read ``0`` stay different, and a quoted identifier never becomes the keyword
# it happens to spell.

_TOKEN_KEYWORD = "keyword"
_TOKEN_IDENTIFIER = "identifier"
_TOKEN_NUMBER = "number"
_TOKEN_STRING = "string"
_TOKEN_BLOB = "blob"
_TOKEN_OPERATOR = "operator"

# SQLite's documented keyword set. A bare word in it is a keyword; a bare word
# outside it, and every quoted word whatever it spells, is an identifier. That
# asymmetry is the point: SQLite lets a keyword be quoted into an ordinary
# name, and a schema that names a constraint "autoincrement" must not read as
# one that declares the AUTOINCREMENT clause.
#
# A word missing from this set can only be typed as an identifier, which makes
# its quoted and bare spellings compare equal - which is what SQLite itself
# means by a keyword usable as an identifier. It can never make a structural
# difference disappear, because the catalogue metadata is compared separately.
_SQL_KEYWORDS = frozenset(
    """
    abort action add after all alter always analyze and as asc attach
    autoincrement before begin between by cascade case cast check collate
    column commit conflict constraint create cross current current_date
    current_time current_timestamp database default deferrable deferred
    delete desc detach distinct do drop each else end escape except exclude
    exclusive exists explain fail filter first following for foreign from
    full generated glob group groups having if ignore immediate in index
    indexed initially inner insert instead intersect into is isnull join key
    last left like limit match materialized natural no not nothing notnull
    null nulls of offset on or order others outer over partition plan pragma
    preceding primary query raise range recursive references regexp reindex
    release rename replace restrict returning right rollback row rows
    savepoint select set table temp temporary then ties to transaction trigger
    unbounded union unique update using vacuum values view virtual when where
    window with without
    """.split()
)

# The closing character for each way SQLite lets an identifier be quoted.
_IDENTIFIER_QUOTES = {'"': '"', "`": "`", "[": "]"}

# Characters that continue a bare word or number once one has started.
_WORD_TAIL = "_$"

# Operators SQLite spells with two characters. Reading them whole keeps
# ``>=`` from comparing as ``>`` followed by something separate.
_TWO_CHARACTER_OPERATORS = frozenset(
    {"!=", "<>", "<=", ">=", "==", "||", "<<", ">>", "->"}
)

# The only tokens SQLite allows between PRIMARY KEY and AUTOINCREMENT. The
# clause is ``PRIMARY KEY [ASC|DESC] [ON CONFLICT resolution] AUTOINCREMENT``,
# so anything else ends the clause and whatever follows is a different one.
_PRIMARY_KEY_CLAUSE_KEYWORDS = frozenset(
    {"asc", "desc", "on", "conflict", "rollback", "abort", "fail", "ignore",
     "replace"}
)


def _tokenize_definition(definition: str) -> tuple[tuple[str, str], ...]:
    """Reduce a stored CREATE statement to typed tokens.

    This is the one place a schema statement is read as text, and it exists so
    that nothing else has to. Whitespace and comments disappear; words are
    casefolded; an identifier keeps only its name whichever of SQLite's
    quoting styles it arrived in. What survives is the kind of each token
    alongside its value, so a later comparison can tell a keyword from a name
    that spells one, and a number from a string that reads like one.

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
            tokens.append((_TOKEN_STRING, literal))
            continue

        if character in _IDENTIFIER_QUOTES:
            name, index = _read_delimited(
                definition,
                index,
                _IDENTIFIER_QUOTES[character],
            )
            tokens.append((_TOKEN_IDENTIFIER, name.casefold()))
            continue

        if character.isdigit() or (
            character == "."
            and index + 1 < end
            and definition[index + 1].isdigit()
        ):
            number, index = _read_number(definition, index)
            tokens.append((_TOKEN_NUMBER, number.casefold()))
            continue

        if character.isalpha() or character in _WORD_TAIL:
            start = index
            while index < end and (
                definition[index].isalnum() or definition[index] in _WORD_TAIL
            ):
                index += 1
            word = definition[start:index].casefold()
            # ``x'...'`` is a blob literal rather than a name beside a string.
            if word == "x" and index < end and definition[index] == "'":
                literal, index = _read_delimited(definition, index, "'")
                tokens.append((_TOKEN_BLOB, literal.casefold()))
                continue
            tokens.append(
                (
                    _TOKEN_KEYWORD if word in _SQL_KEYWORDS
                    else _TOKEN_IDENTIFIER,
                    word,
                )
            )
            continue

        if definition[index:index + 2] in _TWO_CHARACTER_OPERATORS:
            tokens.append((_TOKEN_OPERATOR, definition[index:index + 2]))
            index += 2
            continue

        tokens.append((_TOKEN_OPERATOR, character))
        index += 1

    return tuple(tokens)


def _read_number(definition: str, start: int) -> tuple[str, int]:
    """Return one numeric literal's text and the index just past it.

    Hexadecimal, decimal, and exponent forms are all read as a single token.
    An unusual spelling that this stops short of simply becomes several
    tokens, which compares unequal to the expected one rather than equal to
    it, so the failure direction is the safe one.
    """
    index = start
    end = len(definition)
    while index < end and (
        definition[index].isalnum() or definition[index] in "._"
    ):
        index += 1
        if (
            definition[index - 1] in "eE"
            and index < end
            and definition[index] in "+-"
        ):
            index += 1
    return definition[start:index], index


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


def _canonical_statement(
    tokens: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Return the statement's tokens with its CHECK clauses lifted out.

    The checks are compared separately, because SQLite evaluates a check
    declared beside its column and the same check declared for the table
    identically, and a schema should not be a different schema for having
    written one of them the other way. Removing them leaves the separators
    that punctuated them, so runs of commas and a comma against a bracket are
    collapsed afterwards and the two spellings converge.

    Everything else is left exactly as it was tokenized. A conflict clause, a
    collation, a table option, a generated column's expression, and a
    trigger's body all still have to match token for token.
    """
    kept: list[tuple[str, str]] = []
    position = 0
    total = len(tokens)

    while position < total:
        if tokens[position] == (_TOKEN_KEYWORD, "check"):
            opening = position + 1
            if opening < total and tokens[opening] == (_TOKEN_OPERATOR, "("):
                _, position = _read_parenthesized(tokens, opening)
                continue
        kept.append(tokens[position])
        position += 1

    return _collapse_separators(tuple(kept))


def _collapse_separators(
    tokens: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Remove separators left dangling by a removed clause.

    Only a comma that now has nothing on one side of it is dropped, which is
    exactly the shape lifting a table-level constraint out leaves behind. A
    comma between two things it genuinely separates is untouched.
    """
    comma = (_TOKEN_OPERATOR, ",")
    opening = (_TOKEN_OPERATOR, "(")
    closing = (_TOKEN_OPERATOR, ")")

    collapsed: list[tuple[str, str]] = []
    for token in tokens:
        if token == comma and (
            not collapsed or collapsed[-1] in (comma, opening)
        ):
            continue
        if token == closing:
            while collapsed and collapsed[-1] == comma:
                collapsed.pop()
        collapsed.append(token)
    return tuple(collapsed)


def _check_constraints(
    tokens: tuple[tuple[str, str], ...],
) -> tuple[tuple[tuple[str, str], ...], ...]:
    """Return every CHECK expression a tokenized statement declares.

    Each expression is returned as its own typed token sequence rather than as
    text, so ``> 0`` and ``> '0'`` stay different conditions however alike
    they look. The sequences are sorted, so the order the checks were written
    in does not matter, and parentheses that merely wrap a whole expression
    are dropped, so ``((length(fact) > 0))`` is the condition it states.

    Only the expression inside each ``CHECK`` is taken. The surrounding
    statement contributes nothing here, so this can never become an equality
    test on stored SQL by another name.
    """
    expressions: list[tuple[tuple[str, str], ...]] = []
    position = 0
    total = len(tokens)

    while position < total:
        if tokens[position] != (_TOKEN_KEYWORD, "check"):
            position += 1
            continue
        opening = position + 1
        if opening >= total or tokens[opening] != (_TOKEN_OPERATOR, "("):
            position += 1
            continue
        expression, position = _read_parenthesized(tokens, opening)
        expressions.append(_strip_enclosing_parentheses(expression))

    return tuple(sorted(expressions))


def _read_parenthesized(
    tokens: tuple[tuple[str, str], ...],
    opening: int,
) -> tuple[tuple[tuple[str, str], ...], int]:
    """Return one balanced parenthesized group's tokens and the next index.

    ``opening`` addresses the group's own ``(``, which is syntax rather than
    part of what it encloses and is dropped along with its closing partner.
    Nesting inside is kept, because it is part of the expression.
    """
    contents: list[tuple[str, str]] = []
    depth = 0
    index = opening
    total = len(tokens)

    while index < total:
        token = tokens[index]
        if token == (_TOKEN_OPERATOR, "("):
            depth += 1
            if depth > 1:
                contents.append(token)
        elif token == (_TOKEN_OPERATOR, ")"):
            depth -= 1
            if depth == 0:
                return tuple(contents), index + 1
            contents.append(token)
        else:
            contents.append(token)
        index += 1

    return tuple(contents), total


def _strip_enclosing_parentheses(
    expression: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Remove parentheses that wrap a whole expression without changing it.

    ``(length(fact)>0)`` and ``length(fact)>0`` state the same condition, so
    they have to compare equal. A pair is only removed when it genuinely
    encloses everything: ``(a)>(b)`` opens and closes before the end, so it is
    left exactly as it is.
    """
    opening = (_TOKEN_OPERATOR, "(")
    closing = (_TOKEN_OPERATOR, ")")

    while (
        len(expression) >= 2
        and expression[0] == opening
        and expression[-1] == closing
    ):
        depth = 0
        for position, token in enumerate(expression):
            if token == opening:
                depth += 1
            elif token == closing:
                depth -= 1
                if depth == 0 and position < len(expression) - 1:
                    return expression
        expression = expression[1:-1]
    return expression


def _declares_autoincrement(tokens: tuple[tuple[str, str], ...]) -> bool:
    """Return whether a statement declares a real AUTOINCREMENT primary key.

    The clause is looked for where SQLite defines it, immediately after
    ``PRIMARY KEY`` and past only the sort order and conflict resolution it
    allows in between. The keyword appearing anywhere else in the statement -
    as a constraint's name, inside a string, in a comment, or as a column
    called something similar - is not this clause and does not count.

    Only a genuine keyword token can match, so a quoted ``"autoincrement"``,
    which SQLite reads as an ordinary name, never does.
    """
    total = len(tokens)
    for position in range(total - 1):
        if tokens[position] != (_TOKEN_KEYWORD, "primary"):
            continue
        if tokens[position + 1] != (_TOKEN_KEYWORD, "key"):
            continue
        cursor = position + 2
        while cursor < total:
            kind, value = tokens[cursor]
            if kind != _TOKEN_KEYWORD:
                break
            if value == "autoincrement":
                return True
            if value not in _PRIMARY_KEY_CLAUSE_KEYWORDS:
                break
            cursor += 1
    return False


def _reference_schema(
    statements: tuple[str, ...],
) -> tuple[_ObjectShape, ...]:
    """Return the fingerprint a set of schema statements produces.

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
