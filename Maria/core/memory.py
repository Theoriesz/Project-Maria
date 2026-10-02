"""SQLite-backed memory layer for Maria."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from logging import Logger
from pathlib import Path
from typing import Any

from core.audit import (
    AUDIT_QUERY_LIMIT,
    AUDIT_SUMMARY_ID_MESSAGE,
    AUDIT_SUMMARY_MESSAGE,
    AuditDecision,
    AuditOutcome,
    AuditSummary,
    AuditTarget,
    audit_timestamp,
    is_audit_summary_id,
    is_audit_timestamp,
    is_audited_action,
)
from core.constants import (
    DATABASE_SCHEMA_VERSION,
    DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS,
)
from core.context import (
    SEARCH_MAX_TERMS,
    SEARCH_TERM_MAX_CHARACTERS,
    SUPPLIED_CONTEXT_MAX_RECORDS,
    ContextConsentOutcome,
    note_digest,
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
# per-memory context-consent column. Version 4 adds the per-note context
# consent column, which holds the approved text's identity. Version 5 adds the
# two audit-summary tables. The current version lives in ``core.constants`` as
# ``DATABASE_SCHEMA_VERSION``.
_SCHEMA_BEFORE_EXPLICIT_MEMORIES = 1
_SCHEMA_V0_2_BASELINE = 2
_SCHEMA_MEMORY_CONTEXT_CONSENT = 3
_SCHEMA_NOTE_CONTEXT_CONSENT = 4
_SCHEMA_AUDIT_SUMMARIES = 5

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

# ``context_digest`` is the owner's per-note consent to supply that note to
# the local reasoning model, and the identity of exactly the title and body
# the owner approved: the SHA-256 of both, or NULL while consent is off. A
# note whose current text no longer has that identity is not eligible, even
# if another connection changed it and left the value in place.
_CREATE_NOTES_TABLE = """
    CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        context_digest TEXT
            CHECK (context_digest IS NULL OR length(context_digest) = 64)
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

# ``audit_summaries`` holds one row for each approved model-proposed execution
# attempt Maria authorised for dispatch, and ``audit_outcomes`` the outcome
# appended after dispatch, at most one per summary, keyed by the summary's id.
# Every stored value is Maria's own, from the closed vocabulary in
# ``core/audit.py``. AUTOINCREMENT keeps a deleted summary's id from ever
# being reused, so an outcome can never attach to a later summary.
_CREATE_AUDIT_SUMMARIES_TABLE = """
    CREATE TABLE IF NOT EXISTS audit_summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        action TEXT NOT NULL,
        decision TEXT NOT NULL,
        target TEXT NOT NULL
    )
"""

_CREATE_AUDIT_OUTCOMES_TABLE = """
    CREATE TABLE IF NOT EXISTS audit_outcomes (
        summary_id INTEGER PRIMARY KEY,
        outcome TEXT NOT NULL
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
    _CREATE_AUDIT_SUMMARIES_TABLE,
    _CREATE_AUDIT_OUTCOMES_TABLE,
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

_HISTORICAL_NOTES_TABLE_WITH_CONTEXT_CONSENT = """
    CREATE TABLE notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        context_digest TEXT
            CHECK (context_digest IS NULL OR length(context_digest) = 64)
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

# The one statement the version 4 step runs, frozen the same way. With no
# DEFAULT the new column is NULL for every existing note, which is consent
# off, and the statement rewrites no row.
_ADD_NOTE_CONTEXT_CONSENT_COLUMN = """
    ALTER TABLE notes ADD COLUMN context_digest TEXT
        CHECK (context_digest IS NULL OR length(context_digest) = 64)
"""

# The two tables the version 5 step creates, frozen with the version 5
# schema. Both start empty, and the step touches no existing table or row.
_HISTORICAL_AUDIT_SUMMARIES_TABLE = """
    CREATE TABLE audit_summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        action TEXT NOT NULL,
        decision TEXT NOT NULL,
        target TEXT NOT NULL
    )
"""

_HISTORICAL_AUDIT_OUTCOMES_TABLE = """
    CREATE TABLE audit_outcomes (
        summary_id INTEGER PRIMARY KEY,
        outcome TEXT NOT NULL
    )
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
        _SCHEMA_AUDIT_SUMMARIES,
        (
            _HISTORICAL_INTERACTIONS_TABLE,
            _HISTORICAL_NOTES_TABLE_WITH_CONTEXT_CONSENT,
            _HISTORICAL_MEMORIES_TABLE_WITH_CONTEXT_CONSENT,
            _HISTORICAL_AUDIT_SUMMARIES_TABLE,
            _HISTORICAL_AUDIT_OUTCOMES_TABLE,
            _HISTORICAL_INTERACTIONS_INDEX,
            _HISTORICAL_NOTES_INDEX,
        ),
    ),
    (
        _SCHEMA_NOTE_CONTEXT_CONSENT,
        (
            _HISTORICAL_INTERACTIONS_TABLE,
            _HISTORICAL_NOTES_TABLE_WITH_CONTEXT_CONSENT,
            _HISTORICAL_MEMORIES_TABLE_WITH_CONTEXT_CONSENT,
            _HISTORICAL_INTERACTIONS_INDEX,
            _HISTORICAL_NOTES_INDEX,
        ),
    ),
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
    _Migration(
        version=_SCHEMA_NOTE_CONTEXT_CONSENT,
        name="add_note_context_consent",
        statements=(_ADD_NOTE_CONTEXT_CONSENT_COLUMN,),
    ),
    _Migration(
        version=_SCHEMA_AUDIT_SUMMARIES,
        name="create_audit_summaries",
        statements=(
            _HISTORICAL_AUDIT_SUMMARIES_TABLE,
            _HISTORICAL_AUDIT_OUTCOMES_TABLE,
        ),
    ),
)



# A fact digest is the lowercase hexadecimal SHA-256 of the fact's exact UTF-8
# text, and a note's text digest that of its title and body together: the
# identity an enable approval is bound to, never the text itself.
_FACT_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")

# The SQL functions a context search registers, on its own connection only:
# one tests a bounded text against the request's casefolded terms, and the
# other tests whether a note's title and body are still its approved text.
_CONTEXT_MATCH_FUNCTION = "maria_context_match"
_NOTE_APPROVED_FUNCTION = "maria_note_approved"

# The reasons an audit call's log line may give. Each is this module's own
# fixed phrase: nothing SQLite reports and nothing a stored row holds ever
# joins one, so an audit failure logs no stored value and no SQLite text.
_AUDIT_DATABASE_BUSY = "database busy"
_AUDIT_DATABASE_ERROR = "database error"
_AUDIT_NO_ROW_INSERTED = "no row was inserted"
_AUDIT_INVALID_ID = "no valid id was returned"
_AUDIT_UNEXPECTED_VALUE = "a stored value is outside the audit vocabulary"
_AUDIT_LIMIT_MESSAGE = (
    "An audit summary listing limit must be a whole number from 1 to "
    f"{AUDIT_QUERY_LIMIT}."
)

# The largest value SQLite can store as an INTEGER, and so the largest id any
# row can have. A larger id names no row, so it is answered without being
# handed to SQLite, which could not even bind it.
_SQLITE_MAXIMUM_INTEGER = 2**63 - 1


@dataclass(frozen=True)
class MemoryUpdate:
    """What one memory update did.

    ``updated`` is whether a row matched and received the new fact,
    ``conflicting_id`` names another memory already holding the same
    casefolded fact, and ``context_consent_cleared`` says the updated
    memory's context consent had been on and was turned off.
    """

    updated: bool
    conflicting_id: int | None = None
    context_consent_cleared: bool = False


@dataclass(frozen=True)
class NoteUpdate:
    """What one note retitle or edit did.

    ``updated`` is whether a row matched and received the new text, and
    ``context_consent_cleared`` says the note's context consent had been on
    and was turned off.
    """

    updated: bool
    context_consent_cleared: bool = False


@dataclass(frozen=True, repr=False)
class NoteSnapshot:
    """One note's exact title and body, read together, and their identity.

    ``text_digest`` is ``core.context.note_digest`` of exactly this
    ``title`` and ``body``, all from one read, so what an approval shows
    the owner is precisely the text it is bound to. Neither text nor the
    identity is ever shown by ``repr``.
    """

    title: str
    body: str
    text_digest: str

    def __repr__(self) -> str:
        """Name the type without showing the text or its identity."""
        return "NoteSnapshot(<redacted>)"


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
        interaction_id = 0
        failed = False
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
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to record interaction.",
                "Unable to record interaction.",
            )
        self.logger.info("Recorded interaction %s.", interaction_id)
        return interaction_id

    def get_recent_interactions(self, limit: int = 5) -> list[dict[str, Any]]:
        """Return recent interactions in newest-first order."""
        safe_limit = max(1, min(limit, 50))
        rows: list[sqlite3.Row] = []
        failed = False
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
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to fetch recent interactions.",
                "Unable to fetch recent interactions.",
            )
        return [dict(row) for row in rows]

    def get_interaction(self, interaction_id: int) -> dict[str, Any] | None:
        """Return one interaction by its database id."""
        row = None
        failed = False
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
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to read interaction.",
                "Unable to read interaction.",
            )
        return dict(row) if row else None

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
        rows: list[sqlite3.Row] = []
        failed = False
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
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to search interactions.",
                "Unable to search interactions.",
            )
        return [dict(row) for row in rows]

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

        deleted = False
        failed = False
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
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to delete interaction.",
                "Unable to delete interaction.",
            )
        if deleted:
            self.logger.info("Deleted interaction %s.", interaction_id)
        return deleted

    def store_memory(self, fact: str) -> tuple[int, bool]:
        """Store a fact and return its database id and creation status."""
        clean_fact = fact.strip()
        if not clean_fact:
            raise ValueError("Memory fact is required.")

        normalized_fact = clean_fact.casefold()
        timestamp = _utc_timestamp()
        memory_id = 0
        created = False
        failed = False
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
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to store memory.",
                "Unable to store memory.",
            )
        if created:
            self.logger.info("Stored memory %s.", memory_id)
        else:
            self.logger.info("Memory %s already exists.", memory_id)
        return memory_id, created

    def get_memory(self, memory_id: int) -> dict[str, Any] | None:
        """Return one memory by its database id."""
        row = None
        failed = False
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
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to read memory.",
                "Unable to read memory.",
            )
        return dict(row) if row else None

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
        rows: list[sqlite3.Row] = []
        failed = False
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
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to search memories.",
                "Unable to search memories.",
            )
        return [dict(row) for row in rows]

    def list_memories(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return stored memories in newest-first order."""
        safe_limit = max(1, min(limit, 100))
        rows: list[sqlite3.Row] = []
        failed = False
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
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to list memories.",
                "Unable to list memories.",
            )
        return [dict(row) for row in rows]

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

        An update also turns the memory's context consent off, in the same
        transaction; ``update_memory_reporting_consent`` is the same update
        and also says whether consent had been on.
        """
        result = self.update_memory_reporting_consent(memory_id, new_fact)
        return result.updated, result.conflicting_id

    def update_memory_reporting_consent(
        self,
        memory_id: int,
        new_fact: str,
    ) -> MemoryUpdate:
        """Replace one memory's fact and report whether consent was cleared.

        This is ``update_memory`` itself, with the same validation, the same
        statements, and the same outcomes, returned as a ``MemoryUpdate``.
        Context consent belongs to the exact text the owner approved, so an
        update always leaves the memory's consent off. It is turned off in
        the same transaction as the new fact is written, and
        ``context_consent_cleared`` reports whether it had been on, which is
        the one case the owner needs to be told about. A conflicting or
        missing row changes nothing, consent included. When SQLite fails it
        raises ``RuntimeError("Unable to update memory.")`` with no SQLite
        detail as its message, cause, or context, or in the log, because
        SQLite's message can quote the old or the new fact.
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
        failed = False
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
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to update memory.",
                "Unable to update memory.",
            )
        if updated:
            self.logger.info("Updated memory %s.", memory_id)
        if consent_cleared:
            self.logger.info(
                "Turned off context consent for updated memory %s.",
                memory_id,
            )
        return MemoryUpdate(updated, conflicting_id, consent_cleared)

    def memory_fact_digest(self, memory_id: int) -> str | None:
        """Return the identity of one memory's exact fact, or None if absent.

        The identity is the lowercase hexadecimal SHA-256 of the fact's exact
        UTF-8 text, so an approval can be bound to precisely the text that
        was stored when it was requested. The fact itself is never returned
        or logged. Raises ValueError for a non-positive-integer id, and
        ``RuntimeError("Unable to read memory.")`` when SQLite fails, with no
        SQLite detail as its message, cause, or context, or in the log.
        """
        _require_memory_id(memory_id)
        row = None
        failed = False
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT fact
                    FROM memories
                    WHERE id = ?
                    """,
                    (memory_id,),
                ).fetchone()
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to read memory identity.",
                "Unable to read memory.",
            )
        return None if row is None else _fact_digest(row["fact"])

    def enable_memory_context(
        self,
        memory_id: int,
        expected_fact_digest: str | None,
    ) -> ContextConsentOutcome:
        """Turn one memory's context consent on, only for the approved text.

        ``expected_fact_digest`` is the identity ``memory_fact_digest``
        returned when confirmation was requested, or None when no such memory
        existed then. The check and the change are one write transaction:
        ``BEGIN IMMEDIATE`` takes the write lock before the fact is read, so
        no other connection can change it in between, and the UPDATE itself
        applies only while the fact is still exactly the text that was
        checked. A matching memory is enabled; a memory that no longer
        exists, or never did, is ``NOT_FOUND``; and a memory whose text is
        not the approved text, or that appeared after an approval requested
        for a missing id, is ``TARGET_CHANGED`` and is left unchanged.

        Raises ValueError for a non-positive-integer id or an identity that
        is neither None nor a lowercase hexadecimal SHA-256, and
        ``RuntimeError("Unable to change memory context consent.")`` when
        SQLite fails, with no SQLite detail as its message, cause, or
        context, or in the log. The fact is never returned or logged.
        """
        _require_memory_id(memory_id)
        if expected_fact_digest is not None and not (
            type(expected_fact_digest) is str
            and _FACT_DIGEST_PATTERN.fullmatch(expected_fact_digest)
        ):
            raise ValueError("A memory fact identity must be a SHA-256 digest.")

        outcome = ContextConsentOutcome.NOT_FOUND
        failed = False
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT fact
                    FROM memories
                    WHERE id = ?
                    """,
                    (memory_id,),
                ).fetchone()
                if row is None:
                    outcome = ContextConsentOutcome.NOT_FOUND
                elif (
                    expected_fact_digest is None
                    or _fact_digest(row["fact"]) != expected_fact_digest
                ):
                    outcome = ContextConsentOutcome.TARGET_CHANGED
                else:
                    cursor = connection.execute(
                        """
                        UPDATE memories
                        SET context_enabled = 1
                        WHERE id = ? AND fact = ?
                        """,
                        (memory_id, row["fact"]),
                    )
                    outcome = (
                        ContextConsentOutcome.ENABLED
                        if cursor.rowcount == 1
                        else ContextConsentOutcome.TARGET_CHANGED
                    )
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to change memory context consent.",
                "Unable to change memory context consent.",
            )
        if outcome is ContextConsentOutcome.ENABLED:
            self.logger.info("Enabled context consent for memory %s.", memory_id)
        elif outcome is ContextConsentOutcome.TARGET_CHANGED:
            self.logger.info(
                "Refused context consent for memory %s: approved text changed.",
                memory_id,
            )
        return outcome

    def disable_memory_context(self, memory_id: int) -> bool:
        """Turn one memory's context consent off by its database id.

        Returns True when a row matched memory_id, including when its consent
        was already off, and False when none did. At most one row can
        change, because the id is the primary key. Turning consent off only
        narrows what can be sent, so it needs no text check. Raises
        ValueError for a non-positive-integer id, and the same fixed
        RuntimeError as ``enable_memory_context`` when SQLite fails. The
        memory's fact is never read, logged, or returned.
        """
        _require_memory_id(memory_id)
        matched = False
        failed = False
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE memories
                    SET context_enabled = 0
                    WHERE id = ?
                    """,
                    (memory_id,),
                )
                matched = cursor.rowcount == 1
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to change memory context consent.",
                "Unable to change memory context consent.",
            )
        if matched:
            self.logger.info("Disabled context consent for memory %s.", memory_id)
        return matched

    def list_context_memories(
        self,
        limit: int,
        max_fact_bytes: int,
    ) -> list[dict[str, Any]]:
        """Return context-enabled memories in ascending id order, bounded.

        This is the only read of Tier A context, and it is bounded in both
        directions. At most ``limit`` rows come back, clamped to 1 through
        100: a caller that wants to know whether more exist asks for one more
        row than it will use, rather than counting them all. Each row holds
        ``id``, ``oversized``, and ``fact``. A fact whose UTF-8 size is at
        most ``max_fact_bytes`` comes back whole with ``oversized`` False;
        any longer one comes back with ``fact`` None and ``oversized`` True,
        so the caller can name the memory without ever holding its text.

        The bound is UTF-8 whatever text encoding the database file uses.
        SQLite measures a fact in the file's own encoding, so the size test
        runs there first: in a UTF-8 database it is the exact bound, and an
        oversized fact never reaches Python at all. A UTF-16 database stores
        a fact in at most twice its UTF-8 size, so the test there admits
        every fact stored in at most twice the bound — never more than
        ``max_fact_bytes`` characters — and each admitted fact's exact UTF-8
        size is then measured here before anything is returned.

        Raises ValueError when ``max_fact_bytes`` is not a positive int, and
        ``RuntimeError("Unable to read memory context.")`` when SQLite fails
        or a returned fact is not text, so any read that cannot be trusted
        ends the same way. That failure carries no SQLite detail, as its
        message, its cause, its context, or a logged traceback, because
        SQLite's message for a stored text it cannot decode quotes the text.
        """
        return self._context_memory_rows(limit, max_fact_bytes)

    def context_memories_by_id(
        self,
        memory_ids: Sequence[int],
        max_fact_bytes: int,
    ) -> list[dict[str, Any]]:
        """Return which of ``memory_ids`` are context-enabled now, with text.

        This is the recheck of memories already supplied to a conversation:
        only the named ids are read, at most ``SUPPLIED_CONTEXT_MAX_RECORDS``
        of them, and the rows are exactly those ``list_context_memories``
        returns for enabled memories, bounded the same way. A named memory
        that is disabled or gone is simply absent. Raises ValueError for ids
        that are not distinct positive ints, and the same RuntimeError as
        ``list_context_memories`` for a read that cannot be trusted.
        """
        identifiers = _require_record_ids(memory_ids)
        return self._context_memory_rows(
            len(identifiers),
            max_fact_bytes,
            identifiers=identifiers,
        )

    def search_context_memories(
        self,
        terms: Sequence[str],
        after_id: int,
        limit: int,
        max_fact_bytes: int,
    ) -> list[dict[str, Any]]:
        """Return context-enabled memories after ``after_id`` holding a term.

        This is Tier B's memory search. A fact within ``max_fact_bytes``
        UTF-8 bytes matches when one of the casefolded ``terms`` occurs,
        literally, in that fact as it is now, casefolded; there are no
        wildcards, ranking, or fuzziness. Only memories whose id is above
        ``after_id`` are considered, in ascending id order, and at most
        ``limit`` rows come back, clamped to 1 through 100: the limit bounds
        what is returned, not how many rows SQLite examines to find them.
        Rows are bounded and shaped exactly as ``list_context_memories``
        returns them.

        A fact too large to supply is never read into Python, so it cannot
        be casefolded here. It is matched instead, inside SQLite, against the
        casefolded copy stored beside it, and comes back by id only. Maria
        writes that copy with every fact she stores, but cannot check it
        against a fact too large to read: a copy changed outside Maria can
        name an oversized memory that no longer holds the term, or miss one
        that does. Such a row still counts toward ``limit``.

        Raises ValueError for terms that are not 1 to ``SEARCH_MAX_TERMS``
        non-empty texts or for an ``after_id`` that is not a positive int,
        and the same RuntimeError as ``list_context_memories`` otherwise.
        """
        searched = _require_search_terms(terms)
        _require_memory_id(after_id)
        return self._context_memory_rows(
            limit,
            max_fact_bytes,
            after_id=after_id,
            terms=searched,
        )

    def _context_memory_rows(
        self,
        limit: int,
        max_fact_bytes: int,
        *,
        identifiers: tuple[int, ...] = (),
        after_id: int = 0,
        terms: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """Run one bounded read of context-enabled memories.

        With neither ``identifiers`` nor ``terms`` every enabled memory is
        read; ``identifiers`` narrows the read to those ids, and ``terms`` to
        memories after ``after_id`` holding a term. Every value travels as a
        bound parameter; the statement text around them is this module's
        own. The rows are enabled memories in ascending id order, at most
        ``limit`` of them, each fact whole only within the UTF-8 bound.
        """
        if type(max_fact_bytes) is not int or max_fact_bytes < 1:
            raise ValueError("A fact size bound must be a positive whole number.")
        safe_limit = max(1, min(limit, 100))
        rows: list[sqlite3.Row] = []
        failed = False
        try:
            with self._connection() as connection:
                stored_bound = _stored_bound(connection, max_fact_bytes)
                condition = ""
                parameters: tuple[Any, ...] = ()
                if identifiers:
                    placeholders = ", ".join("?" for _ in identifiers)
                    condition = f"AND id IN ({placeholders})"
                    parameters = identifiers
                elif terms:
                    connection.create_function(
                        _CONTEXT_MATCH_FUNCTION,
                        1,
                        _term_matcher(terms, max_fact_bytes),
                        deterministic=True,
                    )
                    stored_copy = " OR ".join(
                        "instr(normalized_fact, ?) > 0" for _ in terms
                    )
                    # A fact within the stored bound is handed to the matcher,
                    # which answers for its current text, or NULL when it is
                    # not text within the exact UTF-8 bound. Only then does
                    # the stored casefolded copy decide, so a fact too large
                    # to read never leaves SQLite.
                    condition = f"""
                        AND id > ?
                        AND coalesce(
                            CASE
                                WHEN length(CAST(fact AS BLOB)) <= ?
                                THEN {_CONTEXT_MATCH_FUNCTION}(fact)
                            END,
                            {stored_copy}
                        )
                    """
                    parameters = (after_id, stored_bound, *terms)
                # The size test runs inside SQLite on the stored bytes, so a
                # fact far beyond the bound is never materialised in Python:
                # only its id and the flag cross the boundary.
                rows = connection.execute(
                    f"""
                    SELECT
                        id,
                        CASE
                            WHEN length(CAST(fact AS BLOB)) <= ? THEN fact
                        END AS fact,
                        length(CAST(fact AS BLOB)) > ? AS oversized
                    FROM memories
                    WHERE context_enabled = 1 {condition}
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (stored_bound, stored_bound, *parameters, safe_limit),
                ).fetchall()
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to read memory context.",
                "Unable to read memory context.",
            )

        # The column is declared TEXT, but SQLite still stores a value of
        # another type as it was given. A fact that is not text cannot be
        # measured or supplied as text, so the whole read is untrustworthy:
        # it fails with the same fixed message as any other unreadable read,
        # and neither the message nor the log line carries the value.
        if any(
            row["fact"] is not None and type(row["fact"]) is not str
            for row in rows
        ):
            self.logger.error("Refused a memory context read: a fact is not text.")
            raise RuntimeError("Unable to read memory context.")
        return [_bounded_context_row(row, max_fact_bytes) for row in rows]

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

        forgotten = False
        failed = False
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
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to forget memory.",
                "Unable to forget memory.",
            )
        if forgotten:
            self.logger.info("Forgot memory %s.", memory_id)
        return forgotten

    def create_note(self, title: str, body: str) -> int:
        """Create a note and return its database id."""
        clean_title = title.strip()
        clean_body = body.strip()
        if not clean_title:
            raise ValueError("Note title is required.")
        if not clean_body:
            raise ValueError("Note body is required.")

        timestamp = _utc_timestamp()
        note_id = 0
        failed = False
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
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to create note.",
                "Unable to create note.",
            )
        self.logger.info("Created note %s.", note_id)
        return note_id

    def list_notes(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent notes in newest-first order."""
        safe_limit = max(1, min(limit, 100))
        rows: list[sqlite3.Row] = []
        failed = False
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
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to list notes.",
                "Unable to list notes.",
            )
        return [dict(row) for row in rows]

    def get_note(self, note_id: int | None = None, title: str | None = None) -> dict[str, Any] | None:
        """Return one note by id or exact title."""
        if note_id is None and not title:
            raise ValueError("A note id or title is required.")

        row = None
        failed = False
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
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to read note.",
                "Unable to read note.",
            )
        return dict(row) if row else None

    def retitle_note(self, note_id: int, new_title: str) -> bool:
        """Replace one note's title, keeping its body and created_at.

        Returns True if a row was updated, False if no row matched note_id.
        Raises ValueError for a non-positive-integer id or a blank title,
        since this persistent mutation is validated independently of its
        callers. updated_at receives the current UTC timestamp even when
        new_title already matches the stored title.

        A retitle also turns the note's context consent off, in the same
        transaction; ``retitle_note_reporting_consent`` is the same retitle
        and also says whether consent had been on.
        """
        return self.retitle_note_reporting_consent(note_id, new_title).updated

    def retitle_note_reporting_consent(
        self,
        note_id: int,
        new_title: str,
    ) -> NoteUpdate:
        """Replace one note's title and report whether consent was cleared.

        This is ``retitle_note`` itself, with the same validation, the same
        outcome, and the same log line, returned as a ``NoteUpdate``. Context
        consent belongs to the exact title and body the owner approved, so a
        retitle always leaves the note's consent off, turned off in the same
        transaction as the new title is written. When SQLite fails it raises
        ``RuntimeError("Unable to retitle note.")`` with no SQLite detail as
        its message, cause, or context, or in the log, because SQLite's
        message can quote the old or the new text.
        """
        _require_note_id(note_id)
        if not isinstance(new_title, str) or not new_title.strip():
            raise ValueError("New note title is required.")

        result = NoteUpdate(False)
        failed = False
        try:
            result = self._rewrite_note(note_id, "title", new_title.strip())
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to retitle note.",
                "Unable to retitle note.",
            )
        if result.updated:
            self.logger.info("Retitled note %s.", note_id)
        if result.context_consent_cleared:
            self.logger.info(
                "Turned off context consent for retitled note %s.",
                note_id,
            )
        return result

    def edit_note(self, note_id: int, new_body: str) -> bool:
        """Replace one note's body, keeping its title and created_at.

        Returns True if a row was updated, False if no row matched note_id.
        Raises ValueError for a non-positive-integer id or a blank body,
        since this persistent mutation is validated independently of its
        callers. updated_at receives the current UTC timestamp even when
        new_body already matches the stored body.

        An edit also turns the note's context consent off, in the same
        transaction; ``edit_note_reporting_consent`` is the same edit and
        also says whether consent had been on.
        """
        return self.edit_note_reporting_consent(note_id, new_body).updated

    def edit_note_reporting_consent(
        self,
        note_id: int,
        new_body: str,
    ) -> NoteUpdate:
        """Replace one note's body and report whether consent was cleared.

        This is ``edit_note`` itself, with the same validation, the same
        outcome, and the same log line, returned as a ``NoteUpdate``. An
        edit always leaves the note's consent off, turned off in the same
        transaction as the new body is written. When SQLite fails it raises
        ``RuntimeError("Unable to edit note.")`` with no SQLite detail as its
        message, cause, or context, or in the log.
        """
        _require_note_id(note_id)
        if not isinstance(new_body, str) or not new_body.strip():
            raise ValueError("New note body is required.")

        result = NoteUpdate(False)
        failed = False
        try:
            result = self._rewrite_note(note_id, "body", new_body.strip())
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to edit note.",
                "Unable to edit note.",
            )
        if result.updated:
            self.logger.info("Edited note %s.", note_id)
        if result.context_consent_cleared:
            self.logger.info(
                "Turned off context consent for edited note %s.",
                note_id,
            )
        return result

    def _rewrite_note(self, note_id: int, column: str, value: str) -> NoteUpdate:
        """Write one note's title or body and turn its consent off together.

        ``column`` is one of the two names this module passes, never a value.
        Both statements run in one transaction. Consent is turned off first:
        that UPDATE takes the write lock, so no other connection can enable
        consent before the new text is written, and a failure writing the
        text rolls the consent change back with it. A note that does not
        exist changes in neither statement.
        """
        statement = {
            "title": "UPDATE notes SET title = ?, updated_at = ? WHERE id = ?",
            "body": "UPDATE notes SET body = ?, updated_at = ? WHERE id = ?",
        }[column]
        with self._connection() as connection:
            consent_cursor = connection.execute(
                """
                UPDATE notes
                SET context_digest = NULL
                WHERE id = ? AND context_digest IS NOT NULL
                """,
                (note_id,),
            )
            cursor = connection.execute(
                statement,
                (value, _utc_timestamp(), note_id),
            )
            return NoteUpdate(cursor.rowcount == 1, consent_cursor.rowcount == 1)

    def note_text_digest(self, note_id: int) -> str | None:
        """Return the identity of one note's exact title and body, or None.

        This is ``note_snapshot``'s identity alone, with the same validation,
        the same None, and the same failure. Neither text is returned or
        logged.
        """
        snapshot = self.note_snapshot(note_id)
        return None if snapshot is None else snapshot.text_digest

    def note_snapshot(self, note_id: int) -> NoteSnapshot | None:
        """Return one note's exact title and body with their identity, or None.

        Both texts come from one read, and the identity is
        ``core.context.note_digest`` of exactly them, so an approval can show
        the owner precisely the text it is bound to. None means no such note
        exists, or that its title or body is not text and so could never be
        approved. Neither text is logged. Raises ValueError for a
        non-positive-integer id, and ``RuntimeError("Unable to read note.")``
        when SQLite fails; that failure carries no SQLite detail, as its
        message, its cause, its context, or a logged traceback, because
        SQLite's message for a stored text it cannot decode quotes the text.
        """
        _require_note_id(note_id)
        row = None
        failed = False
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT title, body
                    FROM notes
                    WHERE id = ?
                    """,
                    (note_id,),
                ).fetchone()
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to read note identity.",
                "Unable to read note.",
            )
        if row is None or not _is_note_text(row["title"], row["body"]):
            return None
        title, body = row["title"], row["body"]
        return NoteSnapshot(title, body, note_digest(title, body))

    def enable_note_context(
        self,
        note_id: int,
        expected_text_digest: str | None,
    ) -> ContextConsentOutcome:
        """Turn one note's context consent on, only for the approved text.

        ``expected_text_digest`` is the identity ``note_text_digest`` returned
        when confirmation was requested, or None when there was none. The
        check and the change are one write transaction: ``BEGIN IMMEDIATE``
        takes the write lock before the note is read, and the UPDATE applies
        only while the title and body are still exactly the text checked. It
        stores that identity as the note's consent, so a later change made
        anywhere, even one that leaves the stored identity in place, makes
        the note ineligible rather than approving the new text. A matching
        note is enabled; a note that no longer exists, or never did, is
        ``NOT_FOUND``; and a note whose text is not the approved text, or
        that appeared after an approval requested for a missing id, is
        ``TARGET_CHANGED`` and is left unchanged.

        Raises ValueError for a non-positive-integer id or an identity that
        is neither None nor a lowercase hexadecimal SHA-256, and
        ``RuntimeError("Unable to change note context consent.")`` when
        SQLite fails, with no SQLite detail as its message, cause, or
        context, or in the log. Neither text is returned or logged.
        """
        _require_note_id(note_id)
        if expected_text_digest is not None and not (
            type(expected_text_digest) is str
            and _FACT_DIGEST_PATTERN.fullmatch(expected_text_digest)
        ):
            raise ValueError("A note text identity must be a SHA-256 digest.")

        outcome = ContextConsentOutcome.NOT_FOUND
        failed = False
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT title, body
                    FROM notes
                    WHERE id = ?
                    """,
                    (note_id,),
                ).fetchone()
                if row is None:
                    outcome = ContextConsentOutcome.NOT_FOUND
                elif (
                    expected_text_digest is None
                    or not _is_note_text(row["title"], row["body"])
                    or note_digest(row["title"], row["body"])
                    != expected_text_digest
                ):
                    outcome = ContextConsentOutcome.TARGET_CHANGED
                else:
                    cursor = connection.execute(
                        """
                        UPDATE notes
                        SET context_digest = ?
                        WHERE id = ? AND title = ? AND body = ?
                        """,
                        (expected_text_digest, note_id, row["title"], row["body"]),
                    )
                    outcome = (
                        ContextConsentOutcome.ENABLED
                        if cursor.rowcount == 1
                        else ContextConsentOutcome.TARGET_CHANGED
                    )
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to change note context consent.",
                "Unable to change note context consent.",
            )
        if outcome is ContextConsentOutcome.ENABLED:
            self.logger.info("Enabled context consent for note %s.", note_id)
        elif outcome is ContextConsentOutcome.TARGET_CHANGED:
            self.logger.info(
                "Refused context consent for note %s: approved text changed.",
                note_id,
            )
        return outcome

    def disable_note_context(self, note_id: int) -> bool:
        """Turn one note's context consent off by its database id.

        Returns True when a row matched note_id, including when its consent
        was already off, and False when none did. At most one row can change,
        because the id is the primary key. Turning consent off only narrows
        what can be sent, so it needs no text check. Raises ValueError for a
        non-positive-integer id, and the same fixed RuntimeError as
        ``enable_note_context`` when SQLite fails. Neither text is read,
        logged, or returned.
        """
        _require_note_id(note_id)
        matched = False
        failed = False
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE notes
                    SET context_digest = NULL
                    WHERE id = ?
                    """,
                    (note_id,),
                )
                matched = cursor.rowcount == 1
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to change note context consent.",
                "Unable to change note context consent.",
            )
        if matched:
            self.logger.info("Disabled context consent for note %s.", note_id)
        return matched

    def context_notes_by_id(
        self,
        note_ids: Sequence[int],
        max_text_bytes: int,
    ) -> list[dict[str, Any]]:
        """Return which of ``note_ids`` are context-enabled now, with text.

        This is the recheck of notes already supplied to a conversation: only
        the named ids are read, at most ``SUPPLIED_CONTEXT_MAX_RECORDS`` of
        them, each row holding ``id``, ``title``, ``body``, and
        ``oversized``. A named note comes back whole only while its title and
        body are both within ``max_text_bytes`` UTF-8 bytes and still exactly
        its approved text, and by id only, with ``oversized`` True, when a
        text is too large, which can never be the approved text. A named note
        that is disabled, deleted, or no longer its approved text is simply
        absent. Raises ValueError for ids that are not distinct positive
        ints, and the same RuntimeError as ``search_context_notes`` for a
        read that cannot be trusted.
        """
        identifiers = _require_record_ids(note_ids)
        return self._context_note_rows(
            max_text_bytes,
            len(identifiers),
            identifiers=identifiers,
        )

    def search_context_notes(
        self,
        terms: Sequence[str],
        limit: int,
        max_text_bytes: int,
    ) -> list[dict[str, Any]]:
        """Return context-enabled notes whose title or body holds a term.

        This is Tier B's note search. Only notes whose consent is on, whose
        title and body are both text within ``max_text_bytes`` UTF-8 bytes,
        and whose title and body are still exactly the text the owner
        approved are searched, in ascending id order. A note matches when one
        of the casefolded ``terms`` occurs, literally, wholly within its
        casefolded title or wholly within its casefolded body; the two are
        never joined into one text.

        Every check runs inside SQLite, row by row, before the matcher sees
        anything: consent, type, and stored size first, then the approved
        identity, recomputed from the current title and body. A note changed
        outside Maria is therefore never matched and never counts toward
        ``limit``. A note with a text beyond the bound is never searched at
        all: its approved identity cannot be verified without reading that
        text, so it is neither matched nor named. At most ``limit`` rows come
        back, clamped to 1 through 100: the limit bounds what is returned,
        not how many rows SQLite examines to find them. Each row holds
        ``id``, ``title``, ``body``, and ``oversized``, which is always
        False here.

        Raises ValueError for terms that are not 1 to ``SEARCH_MAX_TERMS``
        non-empty texts or a bound that is not a positive int, and
        ``RuntimeError("Unable to read note context.")`` when SQLite fails;
        that failure carries no SQLite detail, as its message, its cause, its
        context, or a logged traceback, because SQLite's message for a stored
        text it cannot decode quotes the text.
        """
        return self._context_note_rows(
            max_text_bytes,
            limit,
            terms=_require_search_terms(terms),
        )

    def _context_note_rows(
        self,
        max_text_bytes: int,
        limit: int,
        *,
        identifiers: tuple[int, ...] = (),
        terms: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """Run one bounded read of context-enabled notes, by id or by terms.

        Exactly one of ``identifiers`` and ``terms`` is given. Every value
        travels as a bound parameter; the statement text around them is this
        module's own.
        """
        if type(max_text_bytes) is not int or max_text_bytes < 1:
            raise ValueError("A text size bound must be a positive whole number.")
        safe_limit = max(1, min(limit, 100))
        rows: list[sqlite3.Row] = []
        failed = False
        try:
            with self._connection() as connection:
                stored_bound = _stored_bound(connection, max_text_bytes)
                if terms:
                    connection.create_function(
                        _NOTE_APPROVED_FUNCTION,
                        3,
                        _approved_note_check(max_text_bytes),
                        deterministic=True,
                    )
                    connection.create_function(
                        _CONTEXT_MATCH_FUNCTION,
                        1,
                        _term_matcher(terms, max_text_bytes),
                        deterministic=True,
                    )
                    # CASE settles its conditions in order, so the approval
                    # check sees only a consented note whose texts both fit
                    # the stored bound, and the matcher sees only a note the
                    # approval check confirmed. A row that fails either is
                    # never returned and so never counts toward the limit.
                    condition = f"""
                        AND CASE
                            WHEN context_digest IS NOT NULL
                                AND typeof(title) = 'text'
                                AND typeof(body) = 'text'
                                AND length(CAST(title AS BLOB)) <= ?
                                AND length(CAST(body AS BLOB)) <= ?
                            THEN CASE
                                WHEN {_NOTE_APPROVED_FUNCTION}(
                                    title,
                                    body,
                                    context_digest
                                )
                                THEN {_CONTEXT_MATCH_FUNCTION}(title)
                                    OR {_CONTEXT_MATCH_FUNCTION}(body)
                                ELSE 0
                            END
                            ELSE 0
                        END
                    """
                    parameters: tuple[Any, ...] = (stored_bound, stored_bound)
                else:
                    placeholders = ", ".join("?" for _ in identifiers)
                    condition = f"AND id IN ({placeholders})"
                    parameters = identifiers
                rows = connection.execute(
                    f"""
                    SELECT
                        id,
                        CASE
                            WHEN length(CAST(title AS BLOB)) <= ? THEN title
                        END AS title,
                        CASE
                            WHEN length(CAST(body AS BLOB)) <= ? THEN body
                        END AS body,
                        context_digest
                    FROM notes
                    WHERE context_digest IS NOT NULL
                        AND typeof(title) = 'text'
                        AND typeof(body) = 'text'
                        {condition}
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (stored_bound, stored_bound, *parameters, safe_limit),
                ).fetchall()
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to read note context.",
                "Unable to read note context.",
            )
        settled = (_bounded_note_row(row, max_text_bytes) for row in rows)
        return [row for row in settled if row is not None]

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

        deleted = False
        failed = False
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
        except sqlite3.Error:
            failed = True
        if failed:
            raise self._fixed_failure(
                "Failed to delete note.",
                "Unable to delete note.",
            )
        if deleted:
            self.logger.info("Deleted note %s.", note_id)
        return deleted

    def record_audit_summary(
        self,
        action: str,
        decision: AuditDecision,
        target: AuditTarget,
    ) -> int:
        """Append one audit summary, commit it, and return its new id.

        ``action`` must be an audited action's canonical identifier, and
        ``decision`` and ``target`` exact members of their vocabularies;
        anything else raises ``ValueError`` with a fixed message before the
        database is opened. The timestamp is Maria's own, and it is
        validated against the audit vocabulary too. The summary is
        committed before this returns, so a caller that dispatches only once
        it holds the id can never run an action whose summary is not stored.

        Every failure raises ``RuntimeError`` with a fixed message and with
        no cause or context: SQLite refusing or failing the insert or its
        commit, a database still busy or locked once the busy timeout has
        passed, an insert that added no row, and an id that is not an exact
        positive int. The log records one fixed line and nothing stored.
        """
        if not (
            is_audited_action(action)
            and type(decision) is AuditDecision
            and type(target) is AuditTarget
        ):
            raise ValueError(AUDIT_SUMMARY_MESSAGE)

        # Maria's own timestamp is validated like every other field, so no
        # value outside the vocabulary is ever written.
        created_at = audit_timestamp()
        if not is_audit_timestamp(created_at):
            raise ValueError(AUDIT_SUMMARY_MESSAGE)
        summary_id: object = None
        inserted = False
        failure: str | None = None
        try:
            with self._connection(classify_contention=False) as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO audit_summaries (created_at, action, decision, target)
                    VALUES (?, ?, ?, ?)
                    """,
                    (created_at, action, decision.value, target.value),
                )
                inserted = cursor.rowcount == 1
                summary_id = cursor.lastrowid
        except sqlite3.Error as exc:
            failure = _audit_failure_reason(exc)
        if failure is None and not inserted:
            failure = _AUDIT_NO_ROW_INSERTED
        if failure is None and not is_audit_summary_id(summary_id):
            failure = _AUDIT_INVALID_ID
        if failure is not None:
            raise self._audit_failure("record audit summary", failure)
        self.logger.info("Recorded audit summary %s.", summary_id)
        return summary_id

    def record_audit_outcome(
        self,
        summary_id: int,
        outcome: AuditOutcome,
    ) -> None:
        """Append the one outcome of an audit summary that still exists.

        The outcome is inserted only while its summary exists, in the same
        statement that checks it, so no outcome can be stored for a summary
        that was deleted, and a second outcome for one summary is refused by
        its key. An id that is not an exact positive int, or an outcome that
        is not exactly an ``AuditOutcome``, raises ``ValueError`` before the
        database is opened. A failed or refused write, a write that added no
        row, and an id larger than any row can have, raise ``RuntimeError``
        with a fixed message and with no cause or context.
        """
        if not is_audit_summary_id(summary_id):
            raise ValueError(AUDIT_SUMMARY_ID_MESSAGE)
        if type(outcome) is not AuditOutcome:
            raise ValueError(AUDIT_SUMMARY_MESSAGE)
        if summary_id > _SQLITE_MAXIMUM_INTEGER:
            raise self._audit_failure(
                "record audit outcome",
                _AUDIT_NO_ROW_INSERTED,
            )

        inserted = False
        failure: str | None = None
        try:
            with self._connection(classify_contention=False) as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO audit_outcomes (summary_id, outcome)
                    SELECT ?, ?
                    WHERE EXISTS (
                        SELECT 1 FROM audit_summaries WHERE id = ?
                    )
                    """,
                    (summary_id, outcome.value, summary_id),
                )
                inserted = cursor.rowcount == 1
        except sqlite3.Error as exc:
            failure = _audit_failure_reason(exc)
        if failure is None and not inserted:
            failure = _AUDIT_NO_ROW_INSERTED
        if failure is not None:
            raise self._audit_failure("record audit outcome", failure)
        self.logger.info("Recorded the outcome of audit summary %s.", summary_id)

    def list_audit_summaries(
        self,
        limit: int = AUDIT_QUERY_LIMIT,
    ) -> list[AuditSummary]:
        """Return at most ``limit`` audit summaries, newest id first.

        Each comes with its outcome, or None when none was recorded. Every
        stored value is validated against the audit vocabulary after it is
        read; one that is outside it fails the whole read with a fixed
        message, and neither that value nor any other is repeated or logged.
        A limit that is not an exact int from 1 to ``AUDIT_QUERY_LIMIT``
        raises ``ValueError``; a failed read raises ``RuntimeError`` with a
        fixed message and with no cause or context.
        """
        if type(limit) is not int or not 1 <= limit <= AUDIT_QUERY_LIMIT:
            raise ValueError(_AUDIT_LIMIT_MESSAGE)

        rows: list[sqlite3.Row] = []
        failure: str | None = None
        try:
            with self._connection(classify_contention=False) as connection:
                rows = connection.execute(
                    """
                    SELECT summary.id, summary.created_at, summary.action,
                           summary.decision, summary.target, result.outcome
                    FROM audit_summaries AS summary
                    LEFT JOIN audit_outcomes AS result
                        ON result.summary_id = summary.id
                    ORDER BY summary.id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            failure = _audit_failure_reason(exc)
        if failure is not None:
            raise self._audit_failure("list audit summaries", failure)

        summaries = _settled_audit_summaries(rows)
        if summaries is None:
            raise self._audit_failure(
                "list audit summaries",
                _AUDIT_UNEXPECTED_VALUE,
            )
        return summaries

    def get_audit_summary(self, summary_id: int) -> AuditSummary | None:
        """Return one audit summary with its outcome, or None if absent.

        Validation and failures are exactly those of
        ``list_audit_summaries``, and an id that is not an exact positive int
        raises ``ValueError``. An id larger than any row can have is absent
        and is answered without opening the database.
        """
        if not is_audit_summary_id(summary_id):
            raise ValueError(AUDIT_SUMMARY_ID_MESSAGE)
        if summary_id > _SQLITE_MAXIMUM_INTEGER:
            return None

        row: sqlite3.Row | None = None
        failure: str | None = None
        try:
            with self._connection(classify_contention=False) as connection:
                row = connection.execute(
                    """
                    SELECT summary.id, summary.created_at, summary.action,
                           summary.decision, summary.target, result.outcome
                    FROM audit_summaries AS summary
                    LEFT JOIN audit_outcomes AS result
                        ON result.summary_id = summary.id
                    WHERE summary.id = ?
                    """,
                    (summary_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            failure = _audit_failure_reason(exc)
        if failure is not None:
            raise self._audit_failure("read audit summary", failure)
        if row is None:
            return None

        summaries = _settled_audit_summaries([row])
        if summaries is None:
            raise self._audit_failure(
                "read audit summary",
                _AUDIT_UNEXPECTED_VALUE,
            )
        return summaries[0]

    def delete_audit_summary(self, summary_id: int) -> bool:
        """Delete one audit summary and its outcome together, by id.

        Returns True when the summary existed and was deleted, and False when
        no summary has that id, in which case nothing changes. The summary
        and its outcome are removed in one write transaction, so neither can
        outlive the other, and because the summary's id is never reused, no
        later summary can take its place. An id larger than any row can have
        deletes nothing and is answered without opening the database. An id
        that is not an exact positive int raises ``ValueError``; a failed
        delete raises ``RuntimeError`` with a fixed message and with no cause
        or context.
        """
        if not is_audit_summary_id(summary_id):
            raise ValueError(AUDIT_SUMMARY_ID_MESSAGE)
        if summary_id > _SQLITE_MAXIMUM_INTEGER:
            return False

        deleted = False
        failure: str | None = None
        try:
            with self._connection(classify_contention=False) as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    DELETE FROM audit_summaries
                    WHERE id = ?
                    """,
                    (summary_id,),
                )
                deleted = cursor.rowcount == 1
                if deleted:
                    connection.execute(
                        """
                        DELETE FROM audit_outcomes
                        WHERE summary_id = ?
                        """,
                        (summary_id,),
                    )
        except sqlite3.Error as exc:
            failure = _audit_failure_reason(exc)
        if failure is not None:
            raise self._audit_failure("delete audit summary", failure)
        if deleted:
            self.logger.info("Deleted audit summary %s.", summary_id)
        return deleted

    @contextmanager
    def _connection(
        self,
        *,
        classify_contention: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        """Open a SQLite connection and close it after use.

        Contention is normally recognised and converted here, so every method
        answers a busy database with the same message. ``classify_contention``
        turns that off for the initialization boundary and for every audit
        method, which classify the error themselves: the conversion carries
        an exception log with it, and those callers record a fixed event code
        or reason instead. Rollback and close are unaffected either way, and
        every other error still propagates unchanged.
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

    def _fixed_failure(self, log_line: str, message: str) -> RuntimeError:
        """Log one fixed line and return the fixed failure to raise.

        SQLite's own message can quote stored text: a text it cannot decode,
        or whatever a trigger's ``RAISE`` put in it. A caller therefore
        raises this only after its ``except`` block has ended, so the failure
        has neither a cause nor a context, and nothing from SQLite reaches
        the log, the failure, or its traceback.
        """
        self.logger.error(log_line)
        return RuntimeError(message)

    def _audit_failure(self, operation: str, reason: str) -> RuntimeError:
        """Log one fixed line and return the failure an audit call raises.

        ``operation`` and ``reason`` are always this module's own fixed
        phrases, never anything SQLite or a stored row supplied. Contention
        keeps the shared busy message, which says that retrying may help;
        every other failure names the operation that could not complete. A
        caller raises the result only after its ``except`` block has ended,
        so the failure has neither a cause nor a context.
        """
        self.logger.error("Failed to %s: %s.", operation, reason)
        if reason == _AUDIT_DATABASE_BUSY:
            return RuntimeError(_DATABASE_BUSY_MESSAGE)
        return RuntimeError(f"Unable to {operation}.")

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


def _require_memory_id(memory_id: object) -> None:
    """Refuse anything but an exact positive int memory id."""
    if (
        not isinstance(memory_id, int)
        or isinstance(memory_id, bool)
        or memory_id < 1
    ):
        raise ValueError("Memory id must be a positive whole number.")


def _bounded_context_row(row: sqlite3.Row, max_fact_bytes: int) -> dict[str, Any]:
    """Return one Tier A row, holding its fact only within the UTF-8 bound.

    SQLite admitted the fact against its stored size; this settles the exact
    UTF-8 size, which in a UTF-8 database never changes the answer and in a
    UTF-16 database turns any fact over the bound into an oversized row with
    no text.
    """
    fact = row["fact"]
    oversized = bool(row["oversized"])
    if fact is not None and (
        len(fact.encode("utf-8", "surrogatepass")) > max_fact_bytes
    ):
        fact, oversized = None, True
    return {"id": int(row["id"]), "fact": fact, "oversized": oversized}


def _fact_digest(fact: str) -> str:
    """Return the lowercase hexadecimal SHA-256 of a fact's exact UTF-8 text."""
    return hashlib.sha256(fact.encode("utf-8", "surrogatepass")).hexdigest()


def _require_note_id(note_id: object) -> None:
    """Refuse anything but an exact positive int note id."""
    if (
        not isinstance(note_id, int)
        or isinstance(note_id, bool)
        or note_id < 1
    ):
        raise ValueError("Note id must be a positive whole number.")


def _require_record_ids(record_ids: object) -> tuple[int, ...]:
    """Return 1 to ``SUPPLIED_CONTEXT_MAX_RECORDS`` distinct ids, or refuse.

    Each id must be an exact positive int. The message is fixed and names
    none of them.
    """
    if (
        not isinstance(record_ids, Sequence)
        or isinstance(record_ids, (str, bytes))
        or not 1 <= len(record_ids) <= SUPPLIED_CONTEXT_MAX_RECORDS
        or any(
            type(record_id) is not int or record_id < 1
            for record_id in record_ids
        )
        or len(set(record_ids)) != len(record_ids)
    ):
        raise ValueError("Record ids must be distinct positive whole numbers.")
    return tuple(record_ids)


def _require_search_terms(terms: object) -> tuple[str, ...]:
    """Return 1 to ``SEARCH_MAX_TERMS`` search terms, or refuse them.

    Each term must be exact, non-empty text of at most
    ``SEARCH_TERM_MAX_CHARACTERS`` characters. The message is fixed and
    repeats none of them.
    """
    if (
        not isinstance(terms, Sequence)
        or isinstance(terms, (str, bytes))
        or not 1 <= len(terms) <= SEARCH_MAX_TERMS
        or any(
            type(term) is not str
            or not 0 < len(term) <= SEARCH_TERM_MAX_CHARACTERS
            for term in terms
        )
    ):
        raise ValueError(
            f"Search terms must be 1 to {SEARCH_MAX_TERMS} non-empty texts."
        )
    return tuple(terms)


def _stored_bound(connection: sqlite3.Connection, max_bytes: int) -> int:
    """Return the stored-size bound that admits every text within max_bytes.

    SQLite measures a text in the file's own encoding. In a UTF-8 database
    that is the exact bound. A UTF-16 database stores a text in at most twice
    its UTF-8 size, so twice the bound admits every fitting text - never more
    than ``max_bytes`` characters - and each admitted text's exact UTF-8 size
    is settled afterwards, in Python.
    """
    encoding = connection.execute("PRAGMA encoding").fetchone()[0]
    return max_bytes if encoding == "UTF-8" else 2 * max_bytes


def _term_matcher(
    terms: tuple[str, ...],
    max_bytes: int,
) -> Callable[[object], int | None]:
    """Return a context search's matcher for one set of casefolded terms.

    The matcher answers 1 when a term occurs, literally, in the casefolded
    text and 0 otherwise. SQLite hands it only a text within the stored
    bound, which in a UTF-16 database can still be over the UTF-8 bound, so
    it settles the exact UTF-8 size itself first. For anything that is not
    text within ``max_bytes`` UTF-8 bytes it answers None, SQL NULL, and
    never casefolds it.
    """

    def matches(value: object) -> int | None:
        if not _is_within_bound(value, max_bytes):
            return None
        folded = value.casefold()
        return int(any(term in folded for term in terms))

    return matches


def _approved_note_check(max_bytes: int) -> Callable[[object, object, object], int]:
    """Return the note search's check that a note is still its approved text.

    The check answers 1 only when the title and body are both non-empty text
    within ``max_bytes`` UTF-8 bytes whose identity, ``note_digest`` of
    exactly them, is the one the note's consent holds, and 0 otherwise. It
    runs before the matcher sees either text, so text changed outside Maria
    is never searched, and a text too large to hash within the bound can
    never be taken as approved.
    """

    def approved(title: object, body: object, approved_digest: object) -> int:
        return int(
            _is_note_text(title, body)
            and _is_within_bound(title, max_bytes)
            and _is_within_bound(body, max_bytes)
            and type(approved_digest) is str
            and note_digest(title, body) == approved_digest
        )

    return approved


def _is_within_bound(value: object, max_bytes: int) -> bool:
    """Return whether value is text of at most ``max_bytes`` UTF-8 bytes.

    Text with more characters than the bound is over it and is never
    encoded, so a text of any length is never copied to be measured.
    """
    return (
        type(value) is str
        and len(value) <= max_bytes
        and _utf8_size(value) <= max_bytes
    )


def _is_note_text(title: object, body: object) -> bool:
    """Return whether a note's title and body are both non-empty text."""
    return type(title) is str and type(body) is str and bool(title and body)


def _bounded_note_row(
    row: sqlite3.Row,
    max_text_bytes: int,
) -> dict[str, Any] | None:
    """Return one note context row, or None when it is not the approved text.

    SQLite admitted each text against its stored size; this settles the
    exact UTF-8 size. A note with a text over the bound comes back by id
    only, as oversized. A note within the bound comes back whole only while
    its title and body are still exactly the text whose identity the owner's
    consent holds, so text changed by any other means is never eligible.
    """
    title, body = row["title"], row["body"]
    if title is not None and _utf8_size(title) > max_text_bytes:
        title = None
    if body is not None and _utf8_size(body) > max_text_bytes:
        body = None
    record_id = int(row["id"])
    if title is None or body is None:
        return {"id": record_id, "title": None, "body": None, "oversized": True}
    approved = row["context_digest"]
    if (
        not _is_note_text(title, body)
        or type(approved) is not str
        or note_digest(title, body) != approved
    ):
        return None
    return {"id": record_id, "title": title, "body": body, "oversized": False}


def _utf8_size(text: str) -> int:
    """Return the exact UTF-8 size of text, lone surrogates included."""
    return len(text.encode("utf-8", "surrogatepass"))


def _audit_failure_reason(exc: sqlite3.Error) -> str:
    """Return the fixed reason an audit call gives for a SQLite failure."""
    if _is_busy_or_locked(exc):
        return _AUDIT_DATABASE_BUSY
    return _AUDIT_DATABASE_ERROR


def _settled_audit_summaries(
    rows: Sequence[sqlite3.Row],
) -> list[AuditSummary] | None:
    """Return stored audit rows as validated summaries, or None for any bad row.

    A row holding any value outside the audit vocabulary makes the whole
    result None rather than raising, so the caller's fixed failure is raised
    outside every handler and the refused value travels nowhere.
    """
    summaries: list[AuditSummary] = []
    for row in rows:
        try:
            summaries.append(
                AuditSummary.from_stored(
                    row["id"],
                    row["created_at"],
                    row["action"],
                    row["decision"],
                    row["target"],
                    row["outcome"],
                )
            )
        except ValueError:
            return None
    return summaries


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
