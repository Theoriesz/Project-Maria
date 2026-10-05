"""Focused tests for schema version 5 and its create_audit_summaries migration.

V1-18 appends one forward step to Maria's migration ladder. It creates the two
tables Maria's content-free audit summaries live in, empty, and touches no
existing table or row. Every database here is built under a
``TemporaryDirectory``: the two unstamped historical shapes come from
``tests.schema_fixtures``, reused unchanged, and the tables versions 3, 4, 5,
and 6 introduced are written out by hand below, so each migration starts from
the database an owner actually has and ends at a schema this file states
rather than one it reads back from the memory layer. The frozen statements are
also pinned word for word, so an edit to history fails here even where it
would not change the schema it creates.

V1-20 appends the create_conversation_records step after this one, so version
5 is now a frozen version every climb passes through on its way to the
current version 6. That step has its own focused tests in
``tests.test_conversation_records_migration``; here it only completes each
climb, and the audit step itself is pinned exactly as before.

Every helper that opens or builds a database refuses Maria's live database
directory before anything is opened, and Maria's live log and private
configuration override are never read.
"""

from __future__ import annotations

import logging
import sqlite3
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from core import memory as memory_module
from core.audit import AuditDecision, AuditOutcome, AuditSummary, AuditTarget
from core.brain import MariaBrain
from core.config import MariaConfig
from core.constants import (
    APP_VERSION,
    DATABASE_DIR,
    DATABASE_PATH,
    DATABASE_SCHEMA_VERSION,
)
from core.failures import Failure, FailureKind, TypedFailureError
from core.filesystem_roots import (
    FilesystemProtections,
    FilesystemRoot,
    FilesystemRoots,
    RootPolicy,
)
from core.logger import configure_logging, get_logger
from core.memory import Memory
from core.permissions import PermissionManager
from core.planner import PlanOrigin, Planner
from core.reasoning import (
    DISABLED_REASONING,
    ReasoningRequest,
    ReasoningResponse,
    ReasoningSettings,
    ReasoningTransport,
)
from core.session import MariaSession
from core.tool_manager import ToolManager
from tests.log_assertions import assert_log_omits
from tests.schema_fixtures import (
    BASELINE_INTERACTIONS,
    BASELINE_MEMORIES,
    BASELINE_NOTES,
    PRE_MEMORIES_SCHEMA,
    V0_2_0_BASELINE_SCHEMA,
    create_pre_memories_baseline,
    create_schema,
    create_v0_2_0_baseline,
    read_schema_version,
    stamp_schema_version,
    table_names,
)


def _quiet_logger(name: str) -> logging.Logger:
    """Return an isolated logger that never writes an application log file."""
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


TEST_LOGGER = _quiet_logger("tests.audit_migration")

# Every schema version is written out rather than imported, so each number is
# pinned by this file instead of agreed with.
UNVERSIONED = 0
PRE_MEMORIES_VERSION = 1
BASELINE_SCHEMA_VERSION = 2
MEMORY_CONTEXT_CONSENT_VERSION = 3
NOTE_CONTEXT_CONSENT_VERSION = 4
AUDIT_SUMMARIES_VERSION = 5
CONVERSATION_RECORDS_VERSION = 6
TASKS_AND_REMINDERS_VERSION = 7
NEWER_SCHEMA_VERSION = 8

# The whole forward ladder, in the order it runs.
LADDER_NAMES = (
    "create_memories",
    "add_memory_context_consent",
    "add_note_context_consent",
    "create_audit_summaries",
    "create_conversation_records",
    "create_tasks_and_reminders",
)

RECORD_TABLES = ("interactions", "notes", "memories")
AUDIT_TABLES = ("audit_summaries", "audit_outcomes")
CONVERSATION_TABLES = ("saved_conversations", "conversation_summaries")
TASK_TABLES = ("tasks", "reminders")
ALL_TABLES = (*RECORD_TABLES, *AUDIT_TABLES, *CONVERSATION_TABLES, *TASK_TABLES)
CURRENT_TABLES = frozenset(ALL_TABLES)

# A busy timeout short enough that real contention fails at once.
SHORT_BUSY_TIMEOUT_SECONDS = 0.05

# The memory layer's fixed refusals and log lines, spelled out by hand.
DATABASE_REFUSED_PATTERN = r"^Unable to initialize Maria database\.$"
DATABASE_BUSY_PATTERN = r"^Database is busy\. Try again shortly\.$"
LIVE_DATABASE_PATTERN = r"never Maria's live database directory\.$"
SCHEMA_TOO_NEW_MESSAGE = (
    "This database was written by a newer version of Maria and cannot be "
    "opened by this one."
)
APPLIED_AUDIT_STEP_LINE = (
    "Applied migration create_audit_summaries; schema version is now 5."
)
# Every other line a successful climb logs at info, spelled out the same way.
CLASSIFIED_LINES = {
    PRE_MEMORIES_VERSION: (
        "Classified an unversioned database as schema version 1 and stamped it."
    ),
    BASELINE_SCHEMA_VERSION: (
        "Classified an unversioned database as schema version 2 and stamped it."
    ),
}
APPLIED_STEP_LINES = {
    "create_memories": (
        "Applied migration create_memories; schema version is now 2."
    ),
    "add_memory_context_consent": (
        "Applied migration add_memory_context_consent; schema version is now 3."
    ),
    "add_note_context_consent": (
        "Applied migration add_note_context_consent; schema version is now 4."
    ),
    "create_audit_summaries": APPLIED_AUDIT_STEP_LINE,
    "create_conversation_records": (
        "Applied migration create_conversation_records; schema version is now 6."
    ),
    "create_tasks_and_reminders": (
        "Applied migration create_tasks_and_reminders; schema version is now 7."
    ),
}
READY_LINE = "Database ready at schema version 7."
CREATED_LINE = "Created a new database at schema version 7."
FAILED_AUDIT_STEP_LINE = (
    "Migration create_audit_summaries to schema version 5 failed and was "
    "rolled back; the database remains at version 4."
)
BUSY_AUDIT_STEP_LINE = (
    "Migration create_audit_summaries to schema version 5 did not run "
    "because the database was busy; nothing was applied."
)
DRIFTED_VERSION_FIVE_LINE = (
    "Refused a database stamped at schema version 5 whose structure is not "
    "the frozen schema for that version."
)
TOO_NEW_LINE = (
    "Refused a database stamped at schema version 8; this build expects 7."
)
UNRECOGNISED_UNVERSIONED_LINE = (
    "Refused an unversioned database whose structure matches no schema "
    "version Maria has shipped."
)
# The one line configure_logging() writes itself, as level, logger, message.
LOGGING_CONFIGURED_ENTRY = ("INFO", "maria", "Logging configured.")

# A statement that cannot be parsed, so a step that runs it fails part-way.
NEVER_COMMITTED = "CREATE TABLE never_committed (INVALID"

# A well-formed note consent value: the 64-character identity of approved text.
APPROVED_NOTE_DIGEST = "0123456789abcdef" * 4


# --- Frozen history, pinned word for word ------------------------------------
#
# Each text is the exact statement the memory layer froze, layout included,
# copied rather than imported, so editing a frozen statement fails here even
# where the edit would leave the schema it creates unchanged.

FROZEN_INTERACTIONS_TABLE = """
    CREATE TABLE interactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_input TEXT NOT NULL,
        response TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
"""

FROZEN_NOTES_TABLE = """
    CREATE TABLE notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
"""

FROZEN_NOTES_TABLE_WITH_CONTEXT_CONSENT = """
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

FROZEN_MEMORIES_TABLE = """
    CREATE TABLE memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact TEXT NOT NULL CHECK (length(fact) > 0),
        normalized_fact TEXT NOT NULL UNIQUE
            CHECK (length(normalized_fact) > 0),
        created_at TEXT NOT NULL
    )
"""

FROZEN_MEMORIES_TABLE_WITH_CONTEXT_CONSENT = """
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

FROZEN_INTERACTIONS_INDEX = """
    CREATE INDEX idx_interactions_created_at
        ON interactions (created_at)
"""

FROZEN_NOTES_INDEX = """
    CREATE INDEX idx_notes_title
        ON notes (title)
"""

FROZEN_ADD_MEMORY_CONTEXT_CONSENT_COLUMN = """
    ALTER TABLE memories ADD COLUMN context_enabled INTEGER NOT NULL DEFAULT 0
        CHECK (context_enabled IN (0, 1))
"""

FROZEN_ADD_NOTE_CONTEXT_CONSENT_COLUMN = """
    ALTER TABLE notes ADD COLUMN context_digest TEXT
        CHECK (context_digest IS NULL OR length(context_digest) = 64)
"""

FROZEN_AUDIT_SUMMARIES_TABLE = """
    CREATE TABLE audit_summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        action TEXT NOT NULL,
        decision TEXT NOT NULL,
        target TEXT NOT NULL
    )
"""

FROZEN_AUDIT_OUTCOMES_TABLE = """
    CREATE TABLE audit_outcomes (
        summary_id INTEGER PRIMARY KEY,
        outcome TEXT NOT NULL
    )
"""

FROZEN_SAVED_CONVERSATIONS_TABLE = """
    CREATE TABLE saved_conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        transcript TEXT NOT NULL CHECK (length(transcript) > 0)
    )
"""

FROZEN_CONVERSATION_SUMMARIES_TABLE = """
    CREATE TABLE conversation_summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        body TEXT NOT NULL CHECK (length(CAST(body AS BLOB)) > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        context_digest TEXT
            CHECK (context_digest IS NULL OR length(context_digest) = 64)
    )
"""

FROZEN_TASKS_TABLE = """
    CREATE TABLE tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL CHECK (length(CAST(text AS BLOB)) > 0),
        due_date TEXT CHECK (
            due_date IS NULL
            OR due_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        status TEXT NOT NULL CHECK (status IN ('open', 'completed')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        context_digest TEXT
            CHECK (context_digest IS NULL OR length(context_digest) = 64)
    )
"""

FROZEN_REMINDERS_TABLE = """
    CREATE TABLE reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL CHECK (length(CAST(text AS BLOB)) > 0),
        remind_time TEXT NOT NULL,
        remind_utc TEXT NOT NULL CHECK (
            remind_utc GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
        ),
        status TEXT NOT NULL CHECK (status IN ('open', 'completed')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        context_digest TEXT
            CHECK (context_digest IS NULL OR length(context_digest) = 64)
    )
"""

# Every version's complete frozen schema, statement for statement.
EXPECTED_FROZEN_SCHEMAS: dict[int, tuple[str, ...]] = {
    PRE_MEMORIES_VERSION: (
        FROZEN_INTERACTIONS_TABLE,
        FROZEN_NOTES_TABLE,
        FROZEN_INTERACTIONS_INDEX,
        FROZEN_NOTES_INDEX,
    ),
    BASELINE_SCHEMA_VERSION: (
        FROZEN_INTERACTIONS_TABLE,
        FROZEN_NOTES_TABLE,
        FROZEN_MEMORIES_TABLE,
        FROZEN_INTERACTIONS_INDEX,
        FROZEN_NOTES_INDEX,
    ),
    MEMORY_CONTEXT_CONSENT_VERSION: (
        FROZEN_INTERACTIONS_TABLE,
        FROZEN_NOTES_TABLE,
        FROZEN_MEMORIES_TABLE_WITH_CONTEXT_CONSENT,
        FROZEN_INTERACTIONS_INDEX,
        FROZEN_NOTES_INDEX,
    ),
    NOTE_CONTEXT_CONSENT_VERSION: (
        FROZEN_INTERACTIONS_TABLE,
        FROZEN_NOTES_TABLE_WITH_CONTEXT_CONSENT,
        FROZEN_MEMORIES_TABLE_WITH_CONTEXT_CONSENT,
        FROZEN_INTERACTIONS_INDEX,
        FROZEN_NOTES_INDEX,
    ),
    AUDIT_SUMMARIES_VERSION: (
        FROZEN_INTERACTIONS_TABLE,
        FROZEN_NOTES_TABLE_WITH_CONTEXT_CONSENT,
        FROZEN_MEMORIES_TABLE_WITH_CONTEXT_CONSENT,
        FROZEN_AUDIT_SUMMARIES_TABLE,
        FROZEN_AUDIT_OUTCOMES_TABLE,
        FROZEN_INTERACTIONS_INDEX,
        FROZEN_NOTES_INDEX,
    ),
    CONVERSATION_RECORDS_VERSION: (
        FROZEN_INTERACTIONS_TABLE,
        FROZEN_NOTES_TABLE_WITH_CONTEXT_CONSENT,
        FROZEN_MEMORIES_TABLE_WITH_CONTEXT_CONSENT,
        FROZEN_AUDIT_SUMMARIES_TABLE,
        FROZEN_AUDIT_OUTCOMES_TABLE,
        FROZEN_SAVED_CONVERSATIONS_TABLE,
        FROZEN_CONVERSATION_SUMMARIES_TABLE,
        FROZEN_INTERACTIONS_INDEX,
        FROZEN_NOTES_INDEX,
    ),
    TASKS_AND_REMINDERS_VERSION: (
        FROZEN_INTERACTIONS_TABLE,
        FROZEN_NOTES_TABLE_WITH_CONTEXT_CONSENT,
        FROZEN_MEMORIES_TABLE_WITH_CONTEXT_CONSENT,
        FROZEN_AUDIT_SUMMARIES_TABLE,
        FROZEN_AUDIT_OUTCOMES_TABLE,
        FROZEN_SAVED_CONVERSATIONS_TABLE,
        FROZEN_CONVERSATION_SUMMARIES_TABLE,
        FROZEN_TASKS_TABLE,
        FROZEN_REMINDERS_TABLE,
        FROZEN_INTERACTIONS_INDEX,
        FROZEN_NOTES_INDEX,
    ),
}

# Every forward step: its version, its name, and exactly what it runs.
EXPECTED_LADDER: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (BASELINE_SCHEMA_VERSION, "create_memories", (FROZEN_MEMORIES_TABLE,)),
    (
        MEMORY_CONTEXT_CONSENT_VERSION,
        "add_memory_context_consent",
        (FROZEN_ADD_MEMORY_CONTEXT_CONSENT_COLUMN,),
    ),
    (
        NOTE_CONTEXT_CONSENT_VERSION,
        "add_note_context_consent",
        (FROZEN_ADD_NOTE_CONTEXT_CONSENT_COLUMN,),
    ),
    (
        AUDIT_SUMMARIES_VERSION,
        "create_audit_summaries",
        (FROZEN_AUDIT_SUMMARIES_TABLE, FROZEN_AUDIT_OUTCOMES_TABLE),
    ),
    (
        CONVERSATION_RECORDS_VERSION,
        "create_conversation_records",
        (FROZEN_SAVED_CONVERSATIONS_TABLE, FROZEN_CONVERSATION_SUMMARIES_TABLE),
    ),
    (
        TASKS_AND_REMINDERS_VERSION,
        "create_tasks_and_reminders",
        (FROZEN_TASKS_TABLE, FROZEN_REMINDERS_TABLE),
    ),
)
# The audit step's own entry, found by name rather than by position.
(AUDIT_STEP,) = [
    step for step in EXPECTED_LADDER if step[1] == "create_audit_summaries"
]

# The stamped shapes, built the way test_schema_versioning builds them: the
# frozen v0.2.0 fixture with each later table substituted by its pinned text.
V3_SCHEMA = tuple(
    FROZEN_MEMORIES_TABLE_WITH_CONTEXT_CONSENT
    if "CREATE TABLE memories" in statement
    else statement
    for statement in V0_2_0_BASELINE_SCHEMA
)
V4_SCHEMA = tuple(
    FROZEN_NOTES_TABLE_WITH_CONTEXT_CONSENT
    if "CREATE TABLE notes" in statement
    else statement
    for statement in V3_SCHEMA
)
V5_SCHEMA = V4_SCHEMA + (FROZEN_AUDIT_SUMMARIES_TABLE, FROZEN_AUDIT_OUTCOMES_TABLE)
V6_SCHEMA = V5_SCHEMA + (
    FROZEN_SAVED_CONVERSATIONS_TABLE,
    FROZEN_CONVERSATION_SUMMARIES_TABLE,
)
V7_SCHEMA = V6_SCHEMA + (FROZEN_TASKS_TABLE, FROZEN_REMINDERS_TABLE)

# The version and the schema this build creates, and every climb ends at.
CURRENT_SCHEMA_VERSION = TASKS_AND_REMINDERS_VERSION
CURRENT_SCHEMA = V7_SCHEMA

# The owner rows every migration must keep, as they read once at version 5
# and every version after it: each note's consent off (NULL) and each
# memory's consent off (0).
MIGRATED_INTERACTIONS = tuple(tuple(row) for row in BASELINE_INTERACTIONS)
MIGRATED_NOTES = tuple((*row, None) for row in BASELINE_NOTES)
MIGRATED_MEMORIES = tuple((*row, 0) for row in BASELINE_MEMORIES)
BASELINE_COUNTS = {
    "interactions": len(BASELINE_INTERACTIONS),
    "notes": len(BASELINE_NOTES),
    "memories": len(BASELINE_MEMORIES),
    "audit_summaries": 0,
    "audit_outcomes": 0,
    "saved_conversations": 0,
    "conversation_summaries": 0,
    "tasks": 0,
    "reminders": 0,
}

# Owner-visible texts for the end-to-end audit flows, written out by hand.
FIXED_AUDIT_TIMESTAMP = "2026-10-01T09:46:17Z"
# The first approved calculation's audit summary, exactly as SQLite stores it.
FIRST_CALCULATOR_SUMMARY_ROW = (
    1,
    FIXED_AUDIT_TIMESTAMP,
    "calculator.calculate",
    "owner_approved",
    "none",
)
CALCULATOR_REQUEST = "What is six times seven?"
CALCULATOR_PROPOSAL = (
    '{"tool": "calculator", "action": "calculate", '
    '"arguments": {"expression": "6 * 7"}}'
)
LISTING_REQUEST = "What is in my workspace?"
LISTING_PROPOSAL = '{"tool": "filesystem", "action": "list", "arguments": {}}'
NO_CONTEXT_NOTICE = "No memory context supplied."
APPROVAL_HEADER = (
    "Maria's local reasoning model proposed this action. It runs only if you "
    "approve it."
)
CALCULATOR_APPROVAL = "\n".join(
    (
        APPROVAL_HEADER,
        "Tool: calculator",
        "Action: calculate",
        'Arguments: {"expression": "6 * 7"}',
        "",
        NO_CONTEXT_NOTICE,
    )
)
LISTING_APPROVAL = "\n".join(
    (
        APPROVAL_HEADER,
        "Tool: filesystem",
        "Action: list",
        "Arguments: {}",
        "",
        NO_CONTEXT_NOTICE,
    )
)
CALCULATOR_RESULT = "6 * 7 = 42\n\nRecorded as audit summary 1."
DENIED_CALCULATOR_TEXT = (
    "Permission denied: calculator.calculate is denied by configuration."
    "\n\n" + NO_CONTEXT_NOTICE
)
NO_SUMMARIES_TEXT = "No audit summaries found."
LISTED_RECORD = "Listed audit summaries."
NOT_FOUND_ONE = "Audit summary not found: 1."
DELETE_CONFIRMATION_TEXT = (
    "Confirmation required: audit.delete requires explicit confirmation. "
    "Deleting an audit summary removes it from Maria's database. It does not "
    "undo the action, and Maria's operational log may still show that a "
    "model-proposed action ran."
)
CONFIRMATION_REFUSED_TEXT = (
    "No pending action matches that confirmation. Please make the request again."
)


# --- Guarded temporary-database helpers --------------------------------------


def _refuse_live_database(database_path: Path) -> None:
    """Refuse any path inside Maria's live database directory.

    Containment is settled on the fully resolved path before anything is
    opened or created, so the live file itself, a sibling beside it, a
    descendant at any depth, a ``".."`` traversal, and a link that lands
    inside the directory are all refused. Every helper below calls this
    first.
    """
    live_root = DATABASE_DIR.resolve()
    resolved = Path(database_path).resolve()
    if resolved == live_root or live_root in resolved.parents:
        raise ValueError(
            "Audit migration tests must target a temporary database, "
            "never Maria's live database directory."
        )


def _open_temporary(database_path: Path, **options: object) -> sqlite3.Connection:
    """Open a temporary test database directly, never the live one."""
    _refuse_live_database(database_path)
    return sqlite3.connect(database_path, **options)


def _query(
    database_path: Path,
    statement: str,
    parameters: tuple[object, ...] = (),
) -> list[tuple[object, ...]]:
    """Run one read against a temporary database and return its rows."""
    connection = _open_temporary(database_path)
    try:
        return [tuple(row) for row in connection.execute(statement, parameters)]
    finally:
        connection.close()


def _execute(
    database_path: Path,
    statement: str,
    parameters: tuple[object, ...] = (),
) -> None:
    """Run one write against a temporary database and commit it."""
    connection = _open_temporary(database_path)
    try:
        connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


def _describe(database_path: Path) -> tuple[object, ...]:
    """Return the memory layer's structural fingerprint of a temporary database."""
    connection = _open_temporary(database_path)
    connection.row_factory = sqlite3.Row
    try:
        return memory_module._describe_schema(connection)
    finally:
        connection.close()


def _object_names(database_path: Path) -> tuple[str, ...]:
    """Return a temporary database's user-defined object names, in name order."""
    return tuple(
        str(row[0])
        for row in _query(
            database_path,
            """
            SELECT name
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite~_%' ESCAPE '~'
            ORDER BY name
            """,
        )
    )


def _row_counts(database_path: Path) -> dict[str, int]:
    """Count the rows of all nine record, audit, conversation, and task tables."""
    return {
        table: int(_query(database_path, f"SELECT COUNT(*) FROM {table}")[0][0])
        for table in ALL_TABLES
    }


def _record_rows(database_path: Path) -> dict[str, list[tuple[object, ...]]]:
    """Return every row of each owner record table present, by id."""
    present = {
        str(row[0])
        for row in _query(
            database_path,
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        )
    }
    return {
        table: _query(database_path, f"SELECT * FROM {table} ORDER BY id")
        for table in RECORD_TABLES
        if table in present
    }


def _sequences(database_path: Path) -> dict[str, int]:
    """Return SQLite's AUTOINCREMENT high-water mark for each table it tracks."""
    tracked = _query(
        database_path,
        "SELECT name FROM sqlite_master WHERE name = 'sqlite_sequence'",
    )
    if not tracked:
        return {}
    return {
        str(name): int(sequence)
        for name, sequence in _query(
            database_path,
            "SELECT name, seq FROM sqlite_sequence ORDER BY name",
        )
    }


def _siblings(database_path: Path) -> list[str]:
    """Return every name beside a temporary database, journals included."""
    _refuse_live_database(database_path)
    return sorted(path.name for path in database_path.parent.iterdir())


def _audit_rows(
    database_path: Path,
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    """Return every audit summary and every audit outcome, by id."""
    return (
        _query(
            database_path,
            """
            SELECT id, created_at, action, decision, target
            FROM audit_summaries
            ORDER BY id
            """,
        ),
        _query(
            database_path,
            "SELECT summary_id, outcome FROM audit_outcomes ORDER BY summary_id",
        ),
    )


def _hold_write_lock(
    test: unittest.TestCase,
    database_path: Path,
) -> Callable[[], None]:
    """Hold a write lock on a temporary database from a second connection.

    ``BEGIN IMMEDIATE`` takes SQLite's reserved lock, so other connections
    can still read the file but none can begin writing it. The returned
    callable rolls back and closes the holder; it is also registered as a
    cleanup, and calling it twice is harmless.
    """
    holder = _open_temporary(database_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        try:
            holder.execute("ROLLBACK")
        finally:
            holder.close()

    test.addCleanup(release)
    return release


def _build_schema(
    database_path: Path,
    statements: tuple[str, ...],
    marker: int | None,
) -> None:
    """Build a schema holding the frozen owner rows, stamped unless marker is None."""
    _refuse_live_database(database_path)
    create_schema(database_path, statements, with_rows=True, with_memories=True)
    if marker is not None:
        stamp_schema_version(database_path, marker)


def _build_unversioned_baseline(database_path: Path) -> None:
    """Build the frozen v0.2.0 baseline, unstamped, with its owner rows."""
    _refuse_live_database(database_path)
    create_v0_2_0_baseline(database_path)


def _build_unversioned_pre_memories(database_path: Path) -> None:
    """Build the frozen pre-memories shape, unstamped, with its owner rows."""
    _refuse_live_database(database_path)
    create_pre_memories_baseline(database_path)


def _build_stamped_v1(database_path: Path) -> None:
    """Build the pre-memories shape stamped at version 1."""
    _build_unversioned_pre_memories(database_path)
    stamp_schema_version(database_path, PRE_MEMORIES_VERSION)


def _build_stamped_v2(database_path: Path) -> None:
    """Build the v0.2.0 baseline stamped at version 2."""
    _build_unversioned_baseline(database_path)
    stamp_schema_version(database_path, BASELINE_SCHEMA_VERSION)


def _build_stamped_v3(database_path: Path) -> None:
    """Build the pinned version 3 shape with owner rows, stamped 3."""
    _build_schema(database_path, V3_SCHEMA, MEMORY_CONTEXT_CONSENT_VERSION)


def _build_stamped_v4(database_path: Path) -> None:
    """Build the pinned version 4 shape with owner rows, stamped 4."""
    _build_schema(database_path, V4_SCHEMA, NOTE_CONTEXT_CONSENT_VERSION)


@dataclass(frozen=True)
class _Start:
    """One database a climb through version 5 can begin from.

    ``classified_as`` is the version an unstamped file is recognised as, or
    None for a stamped one; ``steps`` are the migrations it must run, in
    order, up to the current version; ``memories`` are its memory rows as
    they read once migrated.
    """

    label: str
    build: Callable[[Path], None]
    classified_as: int | None
    steps: tuple[str, ...]
    memories: tuple[tuple[object, ...], ...]

    @property
    def directory(self) -> str:
        """Return a directory name of this start's own."""
        return self.label.replace(" ", "-")


START_BASELINE = _Start(
    "unversioned v0.2.0 baseline",
    _build_unversioned_baseline,
    BASELINE_SCHEMA_VERSION,
    (
        "add_memory_context_consent",
        "add_note_context_consent",
        "create_audit_summaries",
        "create_conversation_records",
        "create_tasks_and_reminders",
    ),
    MIGRATED_MEMORIES,
)
START_PRE_MEMORIES = _Start(
    "unversioned pre-memories shape",
    _build_unversioned_pre_memories,
    PRE_MEMORIES_VERSION,
    LADDER_NAMES,
    (),
)
START_V1 = _Start("stamped version 1", _build_stamped_v1, None, LADDER_NAMES, ())
START_V2 = _Start(
    "stamped version 2",
    _build_stamped_v2,
    None,
    (
        "add_memory_context_consent",
        "add_note_context_consent",
        "create_audit_summaries",
        "create_conversation_records",
        "create_tasks_and_reminders",
    ),
    MIGRATED_MEMORIES,
)
START_V3 = _Start(
    "stamped version 3",
    _build_stamped_v3,
    None,
    (
        "add_note_context_consent",
        "create_audit_summaries",
        "create_conversation_records",
        "create_tasks_and_reminders",
    ),
    MIGRATED_MEMORIES,
)
START_V4 = _Start(
    "stamped version 4",
    _build_stamped_v4,
    None,
    (
        "create_audit_summaries",
        "create_conversation_records",
        "create_tasks_and_reminders",
    ),
    MIGRATED_MEMORIES,
)
STAMPED_STARTS = (START_V1, START_V2, START_V3, START_V4)
ALL_STARTS = (START_BASELINE, START_PRE_MEMORIES, *STAMPED_STARTS)


def _messages(logger: Mock, level: str = "info") -> list[str]:
    """Return the lines a recording logger was asked to log at one level."""
    lines: list[str] = []
    for call in getattr(logger, level).call_args_list:
        template, *values = call.args
        lines.append(template % tuple(values) if values else template)
    return lines


def _applied(logger: Mock) -> list[str]:
    """Return the migration step names a recording logger saw applied."""
    return [
        call.args[1]
        for call in logger.info.call_args_list
        if call.args[0].startswith("Applied migration")
    ]


def _real_audit_step() -> object:
    """Return the memory layer's own audit step, found by its name."""
    (step,) = [
        migration
        for migration in memory_module._MIGRATIONS
        if migration.name == "create_audit_summaries"
    ]
    return step


def _broken_ladder(*statements: str) -> tuple[object, ...]:
    """Return the real ladder with its audit step running ``statements`` instead.

    Every other step, the later conversation-record step included, is the
    real one, in its real place.
    """
    return tuple(
        memory_module._Migration(
            version=AUDIT_SUMMARIES_VERSION,
            name="create_audit_summaries",
            statements=statements,
        )
        if migration.name == "create_audit_summaries"
        else migration
        for migration in memory_module._MIGRATIONS
    )


def _with_altered_summaries(old: str, new: str) -> tuple[str, ...]:
    """Return the version 5 schema with one audit_summaries clause changed."""
    altered = FROZEN_AUDIT_SUMMARIES_TABLE.replace(old, new)
    if altered == FROZEN_AUDIT_SUMMARIES_TABLE:
        raise ValueError("The audit_summaries clause to change was not found.")
    return V4_SCHEMA + (altered, FROZEN_AUDIT_OUTCOMES_TABLE)


def _with_altered_outcomes(old: str, new: str) -> tuple[str, ...]:
    """Return the version 5 schema with one audit_outcomes clause changed."""
    altered = FROZEN_AUDIT_OUTCOMES_TABLE.replace(old, new)
    if altered == FROZEN_AUDIT_OUTCOMES_TABLE:
        raise ValueError("The audit_outcomes clause to change was not found.")
    return V4_SCHEMA + (FROZEN_AUDIT_SUMMARIES_TABLE, altered)


# --- The real-component harness ----------------------------------------------


class _ScriptedProvider:
    """A cooperative local model that answers from a fixed script.

    It records every request it receives, so a test can prove how often the
    model was asked. A script that runs out raises, which Maria contains as
    an unavailable model.
    """

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.requests: list[ReasoningRequest] = []

    @property
    def calls(self) -> int:
        """Return how many requests this provider has received."""
        return len(self.requests)

    def respond(self, request: ReasoningRequest) -> ReasoningResponse:
        """Record the request, then answer it with the next scripted reply."""
        self.requests.append(request)
        return ReasoningResponse(self._replies.pop(0))


def _enabled_settings() -> ReasoningSettings:
    """Return valid enabled reasoning settings; no stub here uses the host."""
    return ReasoningSettings(
        enabled=True,
        transport=ReasoningTransport.LOOPBACK,
        host="127.0.0.1",
        model="stub-model:1b",
        request_deadline_seconds=5,
        max_input_bytes=4096,
        max_output_bytes=4096,
    )


def _harness_config(
    root: Path,
    settings: ReasoningSettings,
    *,
    confirmation_required: frozenset[str],
    denied_actions: frozenset[str],
) -> MariaConfig:
    """Return a configuration whose every path lies under a temporary root."""
    database_path = root / "database" / "maria-test.db"
    prompt_path = root / "prompts" / "system.txt"
    return MariaConfig(
        app_name="Maria",
        version=APP_VERSION,
        environment="test",
        database_path=database_path,
        log_dir=root / "logs",
        prompt_path=prompt_path,
        filesystem_roots=FilesystemRoots(
            (
                FilesystemRoot(
                    name="workspace",
                    path=root / "workspace",
                    policy=RootPolicy.ORGANIZE,
                    enabled=True,
                ),
            ),
            "workspace",
        ),
        filesystem_protections=FilesystemProtections(
            database_path=database_path,
            log_dir=root / "logs",
            prompt_path=prompt_path,
        ),
        log_level="DEBUG",
        confirmation_required=frozenset(confirmation_required),
        denied_actions=frozenset(denied_actions),
        reasoning=settings,
    )


def _spy(test: unittest.TestCase, owner: object, name: str) -> Mock:
    """Wrap one method so it still runs but its calls are counted."""
    active = patch.object(owner, name, wraps=getattr(owner, name))
    spy = active.start()
    test.addCleanup(active.stop)
    return spy


class _MigratedHarness:
    """Real planner, contracts, permissions, tools, memory, Brain, and session.

    Everything lives under one temporary directory. ``build`` creates the
    database before the memory layer first opens it, so Maria's own startup
    migrates it exactly as it would an owner's file. Planning, contract
    validation, confirmation preparation, permission checks, dispatch, and
    both audit writes are wrapped by spies that still call the real
    components, so a test can prove how often each was reached.
    """

    def __init__(
        self,
        test: unittest.TestCase,
        *,
        build: Callable[[Path], None] | None = None,
        provider: _ScriptedProvider | None = None,
        confirmation_required: frozenset[str] = frozenset(),
        denied_actions: frozenset[str] = frozenset(),
    ) -> None:
        temporary = TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        (root / "workspace").mkdir()
        settings = DISABLED_REASONING if provider is None else _enabled_settings()
        self.config = _harness_config(
            root,
            settings,
            confirmation_required=confirmation_required,
            denied_actions=denied_actions,
        )
        self.database_path = self.config.database_path
        _refuse_live_database(self.database_path)
        if build is not None:
            build(self.database_path)

        # Tools log beneath Maria's namespace; keep their lines off the
        # console without configuring any log file.
        namespace = logging.getLogger("maria")
        quiet = logging.NullHandler()
        namespace.addHandler(quiet)
        test.addCleanup(namespace.removeHandler, quiet)

        self.logger = _quiet_logger("tests.audit_migration.harness")
        self.memory = Memory(self.database_path, logger=self.logger)
        self.memory.initialize()
        self.tool_manager = ToolManager(
            config=self.config,
            memory=self.memory,
            logger=self.logger,
        )
        self.tool_manager.load_tools()
        self.permissions = PermissionManager(
            confirmation_required=self.config.confirmation_required,
            denied_actions=self.config.denied_actions,
            logger=self.logger,
        )
        self.planner = Planner(logger=self.logger)

        self.create_plan = _spy(test, self.planner, "create_plan")
        self.validate_action = _spy(test, self.tool_manager, "validate_action")
        self.prepare_confirmation = _spy(
            test,
            self.tool_manager,
            "prepare_confirmation",
        )
        self.execute_tool = _spy(test, self.tool_manager, "execute_tool")
        self.check = _spy(test, self.permissions, "check")
        self.record_audit_summary = _spy(test, self.memory, "record_audit_summary")
        self.record_audit_outcome = _spy(test, self.memory, "record_audit_outcome")

        self.provider = provider
        self.brain = MariaBrain(
            config=self.config,
            planner=self.planner,
            memory=self.memory,
            tool_manager=self.tool_manager,
            permissions=self.permissions,
            logger=self.logger,
            reasoning=provider,
        )

    def session(self) -> MariaSession:
        """Return a fresh session over this harness's Brain."""
        return MariaSession(self.brain, logger=self.logger)

    def row_counts(self) -> dict[str, int]:
        """Count the rows of all seven record, audit, and conversation tables."""
        return _row_counts(self.database_path)

    def audit_rows(
        self,
    ) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
        """Return every stored audit summary and outcome, by id."""
        return _audit_rows(self.database_path)

    def interactions(self) -> list[tuple[object, object]]:
        """Return every stored request and response, oldest first."""
        return [
            (row[1], row[2])
            for row in _record_rows(self.database_path)["interactions"]
        ]

    def assert_nothing_dispatched_or_audited(self, test: unittest.TestCase) -> None:
        """Assert no tool ran and no audit value was written."""
        test.assertEqual(self.execute_tool.call_count, 0)
        test.assertEqual(self.record_audit_summary.call_count, 0)
        test.assertEqual(self.record_audit_outcome.call_count, 0)
        test.assertEqual(self.audit_rows(), ([], []))


class _TemporaryDatabaseTestCase(unittest.TestCase):
    """Base case supplying temporary database paths and shared assertions."""

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def database(self, label: str) -> Path:
        """Return a not-yet-created temporary database path of its own."""
        database_path = self.root / label / "maria-test.db"
        _refuse_live_database(database_path)
        return database_path

    def build(self, start: _Start) -> Path:
        """Build one starting database in a directory of its own."""
        database_path = self.database(start.directory)
        start.build(database_path)
        return database_path

    def assert_at_version_four(
        self,
        database_path: Path,
        *,
        before_bytes: bytes,
        before_rows: dict[str, list[tuple[object, ...]]],
        before_siblings: list[str],
    ) -> None:
        """Assert a database is still whole at version 4, byte for byte."""
        self.assertEqual(
            read_schema_version(database_path),
            NOTE_CONTEXT_CONSENT_VERSION,
        )
        self.assertEqual(
            _describe(database_path),
            memory_module._reference_schema(V4_SCHEMA),
        )
        self.assertEqual(
            table_names(database_path),
            frozenset(RECORD_TABLES),
        )
        self.assertEqual(_record_rows(database_path), before_rows)
        self.assertEqual(database_path.read_bytes(), before_bytes)
        # No journal or other file is left beside it.
        self.assertEqual(_siblings(database_path), before_siblings)

    def assert_at_current_version(self, database_path: Path, start: _Start) -> None:
        """Assert a migrated database is current with every owner row kept.

        The climb passed through version 5, so its audit tables exist and are
        empty, and the conversation-record and task steps that follow added
        their tables empty too.
        """
        self.assertEqual(
            read_schema_version(database_path),
            CURRENT_SCHEMA_VERSION,
        )
        self.assertEqual(
            _describe(database_path),
            memory_module._reference_schema(CURRENT_SCHEMA),
        )
        self.assertEqual(table_names(database_path), CURRENT_TABLES)
        self.assertEqual(
            _record_rows(database_path),
            {
                "interactions": list(MIGRATED_INTERACTIONS),
                "notes": list(MIGRATED_NOTES),
                "memories": list(start.memories),
            },
        )
        self.assertEqual(_audit_rows(database_path), ([], []))
        counts = _row_counts(database_path)
        later_tables = (*CONVERSATION_TABLES, *TASK_TABLES)
        self.assertEqual(
            {table: counts[table] for table in later_tables},
            dict.fromkeys(later_tables, 0),
        )

    def assert_refused_untouched(self, database_path: Path, *, marker: int) -> Mock:
        """Assert initialization refuses and leaves the file exactly as found.

        Nothing may be written at all: the transaction runner is replaced by
        a tripwire, and the file's bytes, its marker, its objects, and its
        directory are all compared. The recording logger is returned so a
        caller can check which refusal was logged.
        """
        before_bytes = database_path.read_bytes()
        before_objects = _object_names(database_path)
        before_siblings = _siblings(database_path)
        recording = Mock(spec=logging.Logger)

        with patch.object(
            memory_module,
            "_run_in_one_transaction",
            side_effect=AssertionError("a refused database may not be written"),
        ):
            with self.assertRaisesRegex(RuntimeError, DATABASE_REFUSED_PATTERN):
                Memory(database_path, logger=recording).initialize()

        self.assertEqual(database_path.read_bytes(), before_bytes)
        self.assertEqual(read_schema_version(database_path), marker)
        self.assertEqual(_object_names(database_path), before_objects)
        self.assertEqual(_siblings(database_path), before_siblings)
        self.assertEqual(_applied(recording), [])
        # A refusal is never reported as ready, created, or applied, and it
        # writes no traceback.
        self.assertEqual(_messages(recording), [])
        recording.exception.assert_not_called()
        return recording


# --- The pinned schema and ladder --------------------------------------------


class AuditSchemaPinTests(unittest.TestCase):
    """Pin schema 5, its one step, and the history it must not disturb.

    Version 5 is now frozen history itself: V1-20's conversation-record step
    follows it, and only that step's place in the ladder is pinned here.
    """

    def test_the_schema_version_is_current_and_the_app_version_is_unchanged(
        self,
    ) -> None:
        self.assertEqual(DATABASE_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION)
        self.assertIs(type(DATABASE_SCHEMA_VERSION), int)
        self.assertEqual(
            memory_module.DATABASE_SCHEMA_VERSION,
            CURRENT_SCHEMA_VERSION,
        )
        self.assertEqual(APP_VERSION, "0.2.0")

    def test_the_ladder_is_exactly_six_steps_with_the_audit_step_fourth(self) -> None:
        ladder = memory_module._MIGRATIONS

        self.assertEqual(tuple(step.name for step in ladder), LADDER_NAMES)
        self.assertEqual(
            tuple(step.version for step in ladder),
            (
                BASELINE_SCHEMA_VERSION,
                MEMORY_CONTEXT_CONSENT_VERSION,
                NOTE_CONTEXT_CONSENT_VERSION,
                AUDIT_SUMMARIES_VERSION,
                CONVERSATION_RECORDS_VERSION,
                TASKS_AND_REMINDERS_VERSION,
            ),
        )
        self.assertEqual(ladder[3].name, "create_audit_summaries")
        self.assertIsNone(memory_module._validate_migration_ladder())

    def test_the_audit_step_runs_exactly_its_two_frozen_create_statements(self) -> None:
        step = _real_audit_step()

        self.assertEqual(
            (step.version, step.name, step.statements),
            AUDIT_STEP,
        )
        self.assertIs(type(step.statements), tuple)
        frozen_five = dict(memory_module._FROZEN_SCHEMAS)[AUDIT_SUMMARIES_VERSION]
        for statement in step.statements:
            with self.subTest(statement=statement.split("(")[0].strip()):
                # A frozen statement, never the live IF NOT EXISTS one.
                self.assertIn(statement, frozen_five)
                self.assertNotIn("IF NOT EXISTS", statement)

    def test_the_earlier_migration_steps_are_unchanged_word_for_word(self) -> None:
        self.assertEqual(
            tuple(
                (step.version, step.name, step.statements)
                for step in memory_module._MIGRATIONS[:3]
            ),
            EXPECTED_LADDER[:3],
        )

    def test_frozen_versions_one_to_four_are_unchanged_word_for_word(self) -> None:
        declared = dict(memory_module._FROZEN_SCHEMAS)

        for version in (
            PRE_MEMORIES_VERSION,
            BASELINE_SCHEMA_VERSION,
            MEMORY_CONTEXT_CONSENT_VERSION,
            NOTE_CONTEXT_CONSENT_VERSION,
        ):
            with self.subTest(version=version):
                self.assertEqual(declared[version], EXPECTED_FROZEN_SCHEMAS[version])

    def test_the_frozen_version_five_entry_is_pinned_word_for_word(self) -> None:
        versions = [version for version, _ in memory_module._FROZEN_SCHEMAS]

        self.assertEqual(sorted(versions), [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(len(versions), len(set(versions)))
        self.assertEqual(
            dict(memory_module._FROZEN_SCHEMAS)[AUDIT_SUMMARIES_VERSION],
            EXPECTED_FROZEN_SCHEMAS[AUDIT_SUMMARIES_VERSION],
        )

    def test_the_hand_written_version_five_schema_is_frozen_and_seven_is_current(
        self,
    ) -> None:
        pinned = memory_module._reference_schema(V5_SCHEMA)
        current = memory_module._reference_schema(CURRENT_SCHEMA)

        self.assertEqual(
            pinned,
            memory_module._reference_schema(
                memory_module._frozen_schema(AUDIT_SUMMARIES_VERSION)
            ),
        )
        # The live statements now describe version 7: version 5 with the two
        # conversation-record tables and the two task tables.
        self.assertEqual(
            current,
            memory_module._reference_schema(memory_module._CURRENT_SCHEMA_STATEMENTS),
        )
        self.assertEqual(
            current,
            memory_module._reference_schema(
                memory_module._frozen_schema(CURRENT_SCHEMA_VERSION)
            ),
        )
        self.assertEqual(
            memory_module._reference_schema(V6_SCHEMA),
            memory_module._reference_schema(
                memory_module._frozen_schema(CONVERSATION_RECORDS_VERSION)
            ),
        )

    def test_the_hand_written_earlier_schemas_match_their_frozen_entries(self) -> None:
        for version, schema in (
            (PRE_MEMORIES_VERSION, PRE_MEMORIES_SCHEMA),
            (BASELINE_SCHEMA_VERSION, V0_2_0_BASELINE_SCHEMA),
            (MEMORY_CONTEXT_CONSENT_VERSION, V3_SCHEMA),
            (NOTE_CONTEXT_CONSENT_VERSION, V4_SCHEMA),
        ):
            with self.subTest(version=version):
                self.assertEqual(
                    memory_module._reference_schema(schema),
                    memory_module._reference_schema(
                        memory_module._frozen_schema(version)
                    ),
                )

    def test_version_five_is_version_four_plus_exactly_the_two_audit_tables(
        self,
    ) -> None:
        four = {
            shape.name: shape
            for shape in memory_module._reference_schema(V4_SCHEMA)
        }
        five = {
            shape.name: shape
            for shape in memory_module._reference_schema(
                memory_module._frozen_schema(AUDIT_SUMMARIES_VERSION)
            )
        }

        self.assertEqual(set(five) - set(four), set(AUDIT_TABLES))
        self.assertEqual(set(four) - set(five), set())
        for name, shape in four.items():
            with self.subTest(unchanged=name):
                self.assertEqual(five[name], shape)

    def test_every_step_turns_its_frozen_predecessor_into_its_frozen_successor(
        self,
    ) -> None:
        for step in memory_module._MIGRATIONS:
            with self.subTest(step=step.name):
                predecessor = memory_module._frozen_schema(step.version - 1)
                self.assertEqual(
                    memory_module._reference_schema(predecessor + step.statements),
                    memory_module._reference_schema(
                        memory_module._frozen_schema(step.version)
                    ),
                )
        # The same holds for the shapes pinned in this file.
        self.assertEqual(
            memory_module._reference_schema(
                V4_SCHEMA + _real_audit_step().statements
            ),
            memory_module._reference_schema(V5_SCHEMA),
        )
        steps = {step.name: step for step in memory_module._MIGRATIONS}
        self.assertEqual(
            memory_module._reference_schema(
                V5_SCHEMA + steps["create_conversation_records"].statements
            ),
            memory_module._reference_schema(V6_SCHEMA),
        )
        self.assertEqual(
            memory_module._reference_schema(
                V6_SCHEMA + steps["create_tasks_and_reminders"].statements
            ),
            memory_module._reference_schema(V7_SCHEMA),
        )

    def test_only_versions_two_and_one_can_exist_unstamped(self) -> None:
        self.assertEqual(
            memory_module._UNVERSIONED_SHAPE_VERSIONS,
            (BASELINE_SCHEMA_VERSION, PRE_MEMORIES_VERSION),
        )


# --- Upgrade paths -----------------------------------------------------------


class AuditMigrationUpgradeTests(_TemporaryDatabaseTestCase):
    """Verify every earlier database climbs through version 5 keeping every row."""

    def _migrate(self, start: _Start) -> tuple[Path, Mock, dict[str, int]]:
        """Build a start, migrate it once, and return what the step saw."""
        database_path = self.build(start)
        sequences = _sequences(database_path)
        recording = Mock(spec=logging.Logger)

        Memory(database_path, logger=recording).initialize()

        return database_path, recording, sequences

    def _assert_upgraded(
        self,
        database_path: Path,
        start: _Start,
        recording: Mock,
        sequences: dict[str, int],
    ) -> None:
        """Assert one start's whole climb through version 5 and its log."""
        self.assert_at_current_version(database_path, start)
        self.assertEqual(_applied(recording), list(start.steps))
        self.assertEqual(
            _row_counts(database_path),
            {**BASELINE_COUNTS, "memories": len(start.memories)},
        )
        # No row was inserted anywhere: every id high-water mark is as found,
        # and the new table has none yet, so its first summary will be 1.
        self.assertEqual(_sequences(database_path), sequences)
        self.assertNotIn("audit_summaries", _sequences(database_path))

        # The whole info log, line for line: the classification where one is
        # due, each step's own line in ladder order, then the ready line.
        classified = (
            []
            if start.classified_as is None
            else [CLASSIFIED_LINES[start.classified_as]]
        )
        messages = _messages(recording)
        self.assertEqual(
            messages,
            [
                *classified,
                *(APPLIED_STEP_LINES[name] for name in start.steps),
                READY_LINE,
            ],
        )
        # The audit step is applied, followed only by the conversation-record
        # step, V1-22's task step, and the ready line.
        self.assertEqual(
            messages[-4:],
            [
                APPLIED_AUDIT_STEP_LINE,
                APPLIED_STEP_LINES["create_conversation_records"],
                APPLIED_STEP_LINES["create_tasks_and_reminders"],
                READY_LINE,
            ],
        )
        self.assertEqual(_messages(recording, "warning"), [])
        self.assertEqual(_messages(recording, "error"), [])
        recording.exception.assert_not_called()

    def test_the_v0_2_0_baseline_keeps_every_row_and_gains_empty_audit_tables(
        self,
    ) -> None:
        database_path, recording, sequences = self._migrate(START_BASELINE)

        self._assert_upgraded(database_path, START_BASELINE, recording, sequences)
        # The new tables answer through the memory layer, and hold nothing.
        reopened = Memory(database_path, logger=TEST_LOGGER)
        self.assertEqual(reopened.schema_version(), CURRENT_SCHEMA_VERSION)
        self.assertEqual(reopened.list_audit_summaries(), [])
        self.assertIsNone(reopened.get_audit_summary(1))

    def test_the_pre_memories_shape_climbs_every_step_to_the_current_version(
        self,
    ) -> None:
        database_path, recording, sequences = self._migrate(START_PRE_MEMORIES)

        self._assert_upgraded(database_path, START_PRE_MEMORIES, recording, sequences)
        self.assertEqual(_applied(recording), list(LADDER_NAMES))

    def test_stamped_versions_one_to_four_each_reach_the_current_version(self) -> None:
        for start in STAMPED_STARTS:
            with self.subTest(start=start.label):
                database_path, recording, sequences = self._migrate(start)

                self._assert_upgraded(database_path, start, recording, sequences)

    def test_every_upgraded_database_reopens_without_any_change(self) -> None:
        for start in ALL_STARTS:
            with self.subTest(start=start.label):
                database_path = self.build(start)
                Memory(database_path, logger=TEST_LOGGER).initialize()
                migrated = database_path.read_bytes()
                recording = Mock(spec=logging.Logger)
                reopened = Memory(database_path, logger=recording)

                with patch.object(
                    memory_module,
                    "_run_in_one_transaction",
                    side_effect=AssertionError("a current database is not written"),
                ):
                    reopened.initialize()
                    reopened.initialize()

                self.assertEqual(database_path.read_bytes(), migrated)
                self.assertEqual(_messages(recording), [READY_LINE, READY_LINE])
                self.assertEqual(_messages(recording, "error"), [])
                self.assertEqual(_messages(recording, "warning"), [])
                self.assert_at_current_version(database_path, start)

    def test_the_audit_step_rewrites_no_existing_row_and_keeps_consent_exactly(
        self,
    ) -> None:
        database_path = self.build(START_V4)
        # Consent switched on before the step, to prove it survives as set
        # rather than being reset to the migrated default.
        _execute(
            database_path,
            "UPDATE memories SET context_enabled = 1 WHERE id = ?",
            (BASELINE_MEMORIES[0][0],),
        )
        _execute(
            database_path,
            "UPDATE notes SET context_digest = ? WHERE id = ?",
            (APPROVED_NOTE_DIGEST, BASELINE_NOTES[0][0]),
        )
        before = _record_rows(database_path)
        sequences = _sequences(database_path)
        self.assertEqual(before["memories"][0][-1], 1)
        self.assertEqual(before["notes"][0][-1], APPROVED_NOTE_DIGEST)

        Memory(database_path, logger=TEST_LOGGER).initialize()

        # The conversation-record and task steps that follow rewrite no row
        # either.
        self.assertEqual(
            read_schema_version(database_path),
            CURRENT_SCHEMA_VERSION,
        )
        self.assertEqual(_record_rows(database_path), before)
        self.assertEqual(_sequences(database_path), sequences)
        self.assertEqual(_audit_rows(database_path), ([], []))

    def test_a_migrated_database_numbers_summaries_from_one_and_never_reuses_ids(
        self,
    ) -> None:
        database_path = self.build(START_BASELINE)
        memory = Memory(database_path, logger=TEST_LOGGER)
        memory.initialize()
        timestamp = patch.object(
            memory_module,
            "audit_timestamp",
            return_value=FIXED_AUDIT_TIMESTAMP,
        )
        timestamp.start()
        self.addCleanup(timestamp.stop)

        first = memory.record_audit_summary(
            "calculator.calculate",
            AuditDecision.OWNER_APPROVED,
            AuditTarget.NONE,
        )
        memory.record_audit_outcome(first, AuditOutcome.SUCCEEDED)

        self.assertEqual(first, 1)
        expected = AuditSummary(
            1,
            FIXED_AUDIT_TIMESTAMP,
            "calculator.calculate",
            AuditDecision.OWNER_APPROVED,
            AuditTarget.NONE,
            AuditOutcome.SUCCEEDED,
        )
        self.assertEqual(memory.list_audit_summaries(), [expected])
        self.assertEqual(memory.get_audit_summary(1), expected)
        self.assertEqual(
            _audit_rows(database_path),
            ([FIRST_CALCULATOR_SUMMARY_ROW], [(1, "succeeded")]),
        )

        self.assertTrue(memory.delete_audit_summary(1))
        self.assertEqual(_audit_rows(database_path), ([], []))
        second = memory.record_audit_summary(
            "filesystem.list",
            AuditDecision.OWNER_APPROVED,
            AuditTarget.NAMED_FILESYSTEM_ROOT,
        )

        # The migrated table keeps AUTOINCREMENT, so a deleted id is never
        # handed out again and no outcome can attach to a later summary.
        self.assertEqual(second, 2)
        self.assertEqual(
            _audit_rows(database_path),
            (
                [
                    (
                        2,
                        FIXED_AUDIT_TIMESTAMP,
                        "filesystem.list",
                        "owner_approved",
                        "named_filesystem_root",
                    )
                ],
                [],
            ),
        )
        self.assertEqual(
            _record_rows(database_path),
            {
                "interactions": list(MIGRATED_INTERACTIONS),
                "notes": list(MIGRATED_NOTES),
                "memories": list(MIGRATED_MEMORIES),
            },
        )


class FreshDatabaseTests(_TemporaryDatabaseTestCase):
    """Verify a database Maria creates itself starts at the current version."""

    def _assert_created_directly(self, database_path: Path, recording: Mock) -> None:
        """Assert one creation went straight to version 7 with no ladder."""
        self.assertEqual(_messages(recording), [CREATED_LINE, READY_LINE])
        self.assertEqual(_applied(recording), [])
        self.assertEqual(
            read_schema_version(database_path),
            CURRENT_SCHEMA_VERSION,
        )
        self.assertEqual(table_names(database_path), CURRENT_TABLES)
        self.assertEqual(
            _describe(database_path),
            memory_module._reference_schema(CURRENT_SCHEMA),
        )
        self.assertEqual(_row_counts(database_path), dict.fromkeys(ALL_TABLES, 0))

    def test_a_new_database_is_created_directly_at_the_current_version(self) -> None:
        database_path = self.database("fresh")
        recording = Mock(spec=logging.Logger)

        Memory(database_path, logger=recording).initialize()

        self._assert_created_directly(database_path, recording)

    def test_an_empty_file_is_created_directly_at_the_current_version(self) -> None:
        database_path = self.database("empty")
        database_path.parent.mkdir(parents=True)
        database_path.touch()
        recording = Mock(spec=logging.Logger)

        Memory(database_path, logger=recording).initialize()

        self._assert_created_directly(database_path, recording)

    def test_a_fresh_database_has_the_schema_of_a_migrated_version_four_one(
        self,
    ) -> None:
        fresh_path = self.database("fresh")
        migrated_path = self.build(START_V4)

        Memory(fresh_path, logger=TEST_LOGGER).initialize()
        Memory(migrated_path, logger=TEST_LOGGER).initialize()

        self.assertEqual(_describe(fresh_path), _describe(migrated_path))
        self.assertEqual(_object_names(fresh_path), _object_names(migrated_path))
        self.assertEqual(
            read_schema_version(fresh_path),
            read_schema_version(migrated_path),
        )
        self.assertEqual(
            _describe(fresh_path),
            memory_module._reference_schema(CURRENT_SCHEMA),
        )

    def test_reopening_a_fresh_database_holding_audit_rows_changes_nothing(
        self,
    ) -> None:
        database_path = self.database("fresh")
        memory = Memory(database_path, logger=TEST_LOGGER)
        memory.initialize()
        with patch.object(
            memory_module,
            "audit_timestamp",
            return_value=FIXED_AUDIT_TIMESTAMP,
        ):
            summary_id = memory.record_audit_summary(
                "system.current_time",
                AuditDecision.OWNER_APPROVED,
                AuditTarget.NONE,
            )
        memory.record_audit_outcome(summary_id, AuditOutcome.FAILED)
        before = database_path.read_bytes()
        recording = Mock(spec=logging.Logger)

        Memory(database_path, logger=recording).initialize()
        Memory(database_path, logger=recording).initialize()

        self.assertEqual(database_path.read_bytes(), before)
        self.assertEqual(_messages(recording), [READY_LINE, READY_LINE])
        self.assertEqual(
            _audit_rows(database_path),
            (
                [
                    (
                        1,
                        FIXED_AUDIT_TIMESTAMP,
                        "system.current_time",
                        "owner_approved",
                        "none",
                    )
                ],
                [(1, "failed")],
            ),
        )


# --- Atomicity and contention ------------------------------------------------


class AuditStepAtomicityTests(_TemporaryDatabaseTestCase):
    """Verify a failed audit step leaves version 4 whole and resumes once."""

    def _injections(self) -> dict[str, Callable[[], object]]:
        """Return each way the audit step is made to fail part-way."""
        real_statements = _real_audit_step().statements
        real_write = memory_module._write_schema_version

        def refuse_the_audit_stamp(
            connection: sqlite3.Connection,
            version: int,
        ) -> None:
            if version == AUDIT_SUMMARIES_VERSION:
                raise sqlite3.OperationalError("injected failure stamping version 5")
            real_write(connection, version)

        return {
            "after the summaries table": lambda: patch.object(
                memory_module,
                "_MIGRATIONS",
                _broken_ladder(real_statements[0], NEVER_COMMITTED),
            ),
            "after both tables": lambda: patch.object(
                memory_module,
                "_MIGRATIONS",
                _broken_ladder(*real_statements, NEVER_COMMITTED),
            ),
            "at the version marker": lambda: patch.object(
                memory_module,
                "_write_schema_version",
                side_effect=refuse_the_audit_stamp,
            ),
        }

    def test_a_failure_inside_the_audit_step_leaves_version_four_whole(self) -> None:
        for label, injection in self._injections().items():
            with self.subTest(injected=label):
                database_path = self.database(label.replace(" ", "-"))
                _build_stamped_v4(database_path)
                before_bytes = database_path.read_bytes()
                before_rows = _record_rows(database_path)
                before_siblings = _siblings(database_path)
                recording = Mock(spec=logging.Logger)

                with injection():
                    with self.assertRaisesRegex(
                        RuntimeError,
                        DATABASE_REFUSED_PATTERN,
                    ) as raised:
                        Memory(database_path, logger=recording).initialize()

                self.assertIsInstance(raised.exception.__cause__, sqlite3.Error)
                self.assert_at_version_four(
                    database_path,
                    before_bytes=before_bytes,
                    before_rows=before_rows,
                    before_siblings=before_siblings,
                )
                self.assertNotIn("never_committed", _object_names(database_path))
                self.assertEqual(_applied(recording), [])
                # Neither applied nor ready: the failure is the only line.
                self.assertEqual(_messages(recording), [])
                self.assertEqual(
                    _messages(recording, "error"),
                    [FAILED_AUDIT_STEP_LINE],
                )
                recording.exception.assert_not_called()

    def test_a_retry_after_a_failed_audit_step_applies_it_exactly_once(self) -> None:
        database_path = self.build(START_V4)
        recording = Mock(spec=logging.Logger)
        retrying = Memory(database_path, logger=recording)

        with self._injections()["after both tables"]():
            with self.assertRaisesRegex(RuntimeError, DATABASE_REFUSED_PATTERN):
                retrying.initialize()
        retrying.initialize()
        retrying.initialize()

        self.assertEqual(
            _applied(recording),
            [
                "create_audit_summaries",
                "create_conversation_records",
                "create_tasks_and_reminders",
            ],
        )
        self.assertEqual(_messages(recording).count(APPLIED_AUDIT_STEP_LINE), 1)
        self.assert_at_current_version(database_path, START_V4)

    def test_a_climb_that_fails_at_the_audit_step_stops_whole_at_version_four(
        self,
    ) -> None:
        """Each step commits alone, so the failure undoes only version 5.

        The baseline climbs through versions 3 and 4 first. Had the steps
        shared one transaction, the failure would also undo them and leave
        the database at version 2.
        """
        for label, injection in self._injections().items():
            with self.subTest(injected=label):
                database_path = self.database(f"climb-{label.replace(' ', '-')}")
                _build_unversioned_baseline(database_path)
                recording = Mock(spec=logging.Logger)
                retrying = Memory(database_path, logger=recording)

                with injection():
                    with self.assertRaisesRegex(
                        RuntimeError,
                        DATABASE_REFUSED_PATTERN,
                    ):
                        retrying.initialize()

                self.assertEqual(
                    read_schema_version(database_path),
                    NOTE_CONTEXT_CONSENT_VERSION,
                )
                self.assertEqual(
                    _describe(database_path),
                    memory_module._reference_schema(V4_SCHEMA),
                )
                self.assertEqual(table_names(database_path), frozenset(RECORD_TABLES))
                self.assertEqual(
                    _record_rows(database_path),
                    {
                        "interactions": list(MIGRATED_INTERACTIONS),
                        "notes": list(MIGRATED_NOTES),
                        "memories": list(MIGRATED_MEMORIES),
                    },
                )
                self.assertEqual(
                    _messages(recording),
                    [
                        CLASSIFIED_LINES[BASELINE_SCHEMA_VERSION],
                        APPLIED_STEP_LINES["add_memory_context_consent"],
                        APPLIED_STEP_LINES["add_note_context_consent"],
                    ],
                )
                self.assertEqual(
                    _messages(recording, "error"),
                    [FAILED_AUDIT_STEP_LINE],
                )

                retrying.initialize()

                self.assertEqual(
                    _applied(recording),
                    [
                        "add_memory_context_consent",
                        "add_note_context_consent",
                        "create_audit_summaries",
                        "create_conversation_records",
                        "create_tasks_and_reminders",
                    ],
                )
                self.assert_at_current_version(database_path, START_BASELINE)


class AuditStepContentionTests(_TemporaryDatabaseTestCase):
    """Verify contention during the audit step reads as busy and changes nothing."""

    def test_a_write_lock_during_the_audit_step_gives_the_busy_message(self) -> None:
        database_path = self.build(START_V4)
        before_bytes = database_path.read_bytes()
        before_rows = _record_rows(database_path)
        before_siblings = _siblings(database_path)
        recording = Mock(spec=logging.Logger)
        release = _hold_write_lock(self, database_path)

        with patch.object(
            memory_module,
            "DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS",
            SHORT_BUSY_TIMEOUT_SECONDS,
        ):
            with self.assertRaisesRegex(RuntimeError, DATABASE_BUSY_PATTERN) as raised:
                Memory(database_path, logger=recording).initialize()
        release()

        cause = raised.exception.__cause__
        self.assertIsInstance(cause, sqlite3.OperationalError)
        # Another connection's write lock is SQLite's BUSY; the LOCKED code
        # is proved separately below.
        self.assertEqual(cause.sqlite_errorcode & 0xFF, sqlite3.SQLITE_BUSY)
        self.assertEqual(_messages(recording, "error"), [BUSY_AUDIT_STEP_LINE])
        self.assertEqual(_applied(recording), [])
        self.assertEqual(_messages(recording), [])
        recording.exception.assert_not_called()
        self.assert_at_version_four(
            database_path,
            before_bytes=before_bytes,
            before_rows=before_rows,
            before_siblings=before_siblings,
        )

    def test_the_audit_step_applies_exactly_once_after_the_lock_is_released(
        self,
    ) -> None:
        database_path = self.build(START_V4)
        recording = Mock(spec=logging.Logger)
        retrying = Memory(database_path, logger=recording)
        release = _hold_write_lock(self, database_path)
        with patch.object(
            memory_module,
            "DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS",
            SHORT_BUSY_TIMEOUT_SECONDS,
        ):
            with self.assertRaisesRegex(RuntimeError, DATABASE_BUSY_PATTERN):
                retrying.initialize()
        release()

        retrying.initialize()
        retrying.initialize()

        self.assertEqual(
            _applied(recording),
            [
                "create_audit_summaries",
                "create_conversation_records",
                "create_tasks_and_reminders",
            ],
        )
        self.assert_at_current_version(database_path, START_V4)

    def _locking_the_audit_step(
        self,
    ) -> tuple[AbstractContextManager[object], list[int], sqlite3.OperationalError]:
        """Return a patch reporting SQLITE_LOCKED for the audit step alone.

        Every other transaction runs for real. The versions each transaction
        was asked to reach are recorded, and the exact error raised is
        returned so a caller can prove it is the one kept as the cause.
        """
        locked = sqlite3.OperationalError("database table is locked")
        locked.sqlite_errorcode = sqlite3.SQLITE_LOCKED
        real_runner = memory_module._run_in_one_transaction
        versions: list[int] = []

        def lock_only_the_audit_step(
            connection: sqlite3.Connection,
            statements: tuple[str, ...],
            version: int,
        ) -> None:
            versions.append(version)
            if version == AUDIT_SUMMARIES_VERSION:
                raise locked
            real_runner(connection, statements, version)

        injection = patch.object(
            memory_module,
            "_run_in_one_transaction",
            side_effect=lock_only_the_audit_step,
        )
        return injection, versions, locked

    def test_a_locked_report_during_the_audit_step_is_contention_too(self) -> None:
        database_path = self.build(START_V4)
        before_bytes = database_path.read_bytes()
        before_rows = _record_rows(database_path)
        before_siblings = _siblings(database_path)
        injection, versions, locked = self._locking_the_audit_step()

        recording = Mock(spec=logging.Logger)
        with injection:
            with self.assertRaisesRegex(RuntimeError, DATABASE_BUSY_PATTERN) as raised:
                Memory(database_path, logger=recording).initialize()

        self.assertIs(raised.exception.__cause__, locked)
        self.assertEqual(versions, [AUDIT_SUMMARIES_VERSION])
        self.assertEqual(_messages(recording, "error"), [BUSY_AUDIT_STEP_LINE])
        self.assertEqual(_messages(recording), [])
        recording.exception.assert_not_called()
        self.assert_at_version_four(
            database_path,
            before_bytes=before_bytes,
            before_rows=before_rows,
            before_siblings=before_siblings,
        )

    def test_contention_at_the_audit_step_of_a_climb_keeps_the_steps_before_it(
        self,
    ) -> None:
        database_path = self.build(START_BASELINE)
        injection, versions, _locked = self._locking_the_audit_step()
        recording = Mock(spec=logging.Logger)
        retrying = Memory(database_path, logger=recording)

        with injection:
            with self.assertRaisesRegex(RuntimeError, DATABASE_BUSY_PATTERN):
                retrying.initialize()

        # The classification stamp and the steps to 3 and 4 each committed
        # on their own; only the audit step met contention.
        self.assertEqual(
            versions,
            [
                BASELINE_SCHEMA_VERSION,
                MEMORY_CONTEXT_CONSENT_VERSION,
                NOTE_CONTEXT_CONSENT_VERSION,
                AUDIT_SUMMARIES_VERSION,
            ],
        )
        self.assertEqual(
            read_schema_version(database_path),
            NOTE_CONTEXT_CONSENT_VERSION,
        )
        self.assertEqual(
            _describe(database_path),
            memory_module._reference_schema(V4_SCHEMA),
        )
        self.assertEqual(table_names(database_path), frozenset(RECORD_TABLES))
        self.assertEqual(
            _record_rows(database_path),
            {
                "interactions": list(MIGRATED_INTERACTIONS),
                "notes": list(MIGRATED_NOTES),
                "memories": list(MIGRATED_MEMORIES),
            },
        )
        self.assertEqual(_messages(recording, "error"), [BUSY_AUDIT_STEP_LINE])
        self.assertEqual(
            _applied(recording),
            ["add_memory_context_consent", "add_note_context_consent"],
        )
        recording.exception.assert_not_called()

        retrying.initialize()

        self.assertEqual(
            _applied(recording),
            [
                "add_memory_context_consent",
                "add_note_context_consent",
                "create_audit_summaries",
                "create_conversation_records",
                "create_tasks_and_reminders",
            ],
        )
        self.assert_at_current_version(database_path, START_BASELINE)


# --- Drift and newer schemas -------------------------------------------------


class VersionFiveDriftTests(_TemporaryDatabaseTestCase):
    """Verify a stamped version 5 is checked against its frozen schema first."""

    def _assert_each_refused(self, drifts: dict[str, tuple[str, ...]]) -> None:
        """Build each drifted schema stamped 5 and assert it is refused untouched."""
        exact = memory_module._reference_schema(V5_SCHEMA)
        for label, schema in drifts.items():
            with self.subTest(drift=label):
                self.assertNotEqual(memory_module._reference_schema(schema), exact)
                database_path = self.database(label.replace(" ", "-"))
                _build_schema(database_path, schema, AUDIT_SUMMARIES_VERSION)

                recording = self.assert_refused_untouched(
                    database_path,
                    marker=AUDIT_SUMMARIES_VERSION,
                )

                self.assertEqual(
                    _messages(recording, "error"),
                    [DRIFTED_VERSION_FIVE_LINE],
                )

    def test_the_exact_version_five_schema_is_accepted_and_climbs_two_steps(
        self,
    ) -> None:
        """Version 5 is frozen now, so an exact one climbs two steps and stops.

        Only the conversation-record and task steps run, every owner row is
        kept, and the database it leaves is current and then accepted without
        a write.
        """
        database_path = self.database("exact")
        _build_schema(database_path, V5_SCHEMA, AUDIT_SUMMARIES_VERSION)
        before_rows = _record_rows(database_path)
        before_sequences = _sequences(database_path)
        recording = Mock(spec=logging.Logger)

        Memory(database_path, logger=recording).initialize()

        self.assertEqual(
            _applied(recording),
            ["create_conversation_records", "create_tasks_and_reminders"],
        )
        self.assertEqual(
            _messages(recording),
            [
                APPLIED_STEP_LINES["create_conversation_records"],
                APPLIED_STEP_LINES["create_tasks_and_reminders"],
                READY_LINE,
            ],
        )
        self.assertEqual(_messages(recording, "error"), [])
        self.assertEqual(
            read_schema_version(database_path),
            CURRENT_SCHEMA_VERSION,
        )
        self.assertEqual(
            _describe(database_path),
            memory_module._reference_schema(CURRENT_SCHEMA),
        )
        self.assertEqual(_record_rows(database_path), before_rows)
        self.assertEqual(_sequences(database_path), before_sequences)
        self.assertEqual(_audit_rows(database_path), ([], []))

        migrated = database_path.read_bytes()
        reopening = Mock(spec=logging.Logger)
        with patch.object(
            memory_module,
            "_run_in_one_transaction",
            side_effect=AssertionError("a current database may not be written"),
        ):
            Memory(database_path, logger=reopening).initialize()

        self.assertEqual(database_path.read_bytes(), migrated)
        self.assertEqual(_messages(reopening), [READY_LINE])
        self.assertEqual(_messages(reopening, "error"), [])

    def test_a_version_five_database_missing_an_audit_table_is_refused_untouched(
        self,
    ) -> None:
        self._assert_each_refused(
            {
                "missing audit_outcomes": V4_SCHEMA + (FROZEN_AUDIT_SUMMARIES_TABLE,),
                "missing audit_summaries": V4_SCHEMA + (FROZEN_AUDIT_OUTCOMES_TABLE,),
                "missing both audit tables": V4_SCHEMA,
            }
        )

    def test_an_altered_audit_column_is_refused_untouched(self) -> None:
        self._assert_each_refused(
            {
                "nullable action": _with_altered_summaries(
                    "action TEXT NOT NULL",
                    "action TEXT",
                ),
                "binary target": _with_altered_summaries(
                    "target TEXT NOT NULL",
                    "target BLOB NOT NULL",
                ),
                "renamed decision": _with_altered_summaries(
                    "decision TEXT NOT NULL",
                    "verdict TEXT NOT NULL",
                ),
                "defaulted timestamp": _with_altered_summaries(
                    "created_at TEXT NOT NULL",
                    "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
                ),
                "extra request column": _with_altered_summaries(
                    "target TEXT NOT NULL\n",
                    "target TEXT NOT NULL,\n        request TEXT\n",
                ),
                "reusable summary ids": _with_altered_summaries(
                    "id INTEGER PRIMARY KEY AUTOINCREMENT",
                    "id INTEGER PRIMARY KEY",
                ),
                "unkeyed outcome": _with_altered_outcomes(
                    "summary_id INTEGER PRIMARY KEY",
                    "summary_id INTEGER NOT NULL",
                ),
                "defaulted outcome": _with_altered_outcomes(
                    "outcome TEXT NOT NULL",
                    "outcome TEXT NOT NULL DEFAULT 'succeeded'",
                ),
                "checked outcome": _with_altered_outcomes(
                    "outcome TEXT NOT NULL",
                    "outcome TEXT NOT NULL CHECK (outcome IN ('succeeded', 'failed'))",
                ),
                "cascading outcome": _with_altered_outcomes(
                    "summary_id INTEGER PRIMARY KEY",
                    "summary_id INTEGER PRIMARY KEY "
                    "REFERENCES audit_summaries (id) ON DELETE CASCADE",
                ),
            }
        )

    def test_an_extra_trigger_or_other_object_is_refused_untouched(self) -> None:
        self._assert_each_refused(
            {
                "trigger forging outcomes": V5_SCHEMA
                + (
                    "CREATE TRIGGER audit_summaries_forge "
                    "AFTER INSERT ON audit_summaries BEGIN "
                    "INSERT INTO audit_outcomes (summary_id, outcome) "
                    "VALUES (NEW.id, 'succeeded'); END",
                ),
                "trigger keeping summaries": V5_SCHEMA
                + (
                    "CREATE TRIGGER audit_summaries_keep "
                    "BEFORE DELETE ON audit_summaries BEGIN "
                    "SELECT RAISE(ABORT, 'kept'); END",
                ),
                "extra index": V5_SCHEMA
                + (
                    "CREATE INDEX idx_audit_summaries_action "
                    "ON audit_summaries (action)",
                ),
                "extra view": V5_SCHEMA
                + ("CREATE VIEW audit_view AS SELECT id, action FROM audit_summaries",),
                "extra table": V5_SCHEMA
                + (
                    "CREATE TABLE audit_details "
                    "(summary_id INTEGER PRIMARY KEY, request TEXT)",
                ),
            }
        )

    def test_an_unstamped_file_in_a_later_shape_is_refused_untouched(self) -> None:
        for label, schema in (
            ("unstamped version 5 shape", V5_SCHEMA),
            ("unstamped version 4 shape", V4_SCHEMA),
        ):
            with self.subTest(shape=label):
                database_path = self.database(label.replace(" ", "-"))
                _build_schema(database_path, schema, None)

                recording = self.assert_refused_untouched(
                    database_path,
                    marker=UNVERSIONED,
                )

                self.assertEqual(
                    _messages(recording, "error"),
                    [UNRECOGNISED_UNVERSIONED_LINE],
                )


class NewerSchemaTests(_TemporaryDatabaseTestCase):
    """Verify a database stamped 8 belongs to a newer build and is refused."""

    def _stamped_newer(self, label: str, schema: tuple[str, ...]) -> Path:
        """Build a schema with owner rows and stamp it 8."""
        database_path = self.database(label)
        _build_schema(database_path, schema, NEWER_SCHEMA_VERSION)
        return database_path

    def test_a_newer_stamp_raises_the_typed_too_new_failure(self) -> None:
        database_path = self._stamped_newer("newer", V6_SCHEMA)

        with self.assertRaises(TypedFailureError) as raised:
            Memory(database_path, logger=TEST_LOGGER).initialize()

        self.assertEqual(
            raised.exception.failure,
            Failure(FailureKind.DATABASE_SCHEMA_TOO_NEW),
        )
        self.assertEqual(str(raised.exception), SCHEMA_TOO_NEW_MESSAGE)
        self.assertEqual(raised.exception.args, (raised.exception.failure,))

    def test_the_too_new_refusal_writes_nothing_and_logs_only_versions(self) -> None:
        database_path = self._stamped_newer("newer", V6_SCHEMA)
        before_bytes = database_path.read_bytes()
        before_siblings = _siblings(database_path)
        recording = Mock(spec=logging.Logger)

        with patch.object(
            memory_module,
            "_run_in_one_transaction",
            side_effect=AssertionError("a newer database may not be written"),
        ):
            with self.assertRaises(TypedFailureError):
                Memory(database_path, logger=recording).initialize()

        self.assertEqual(database_path.read_bytes(), before_bytes)
        self.assertEqual(_siblings(database_path), before_siblings)
        self.assertEqual(read_schema_version(database_path), NEWER_SCHEMA_VERSION)
        self.assertEqual(_messages(recording, "error"), [TOO_NEW_LINE])
        self.assertEqual(_messages(recording), [])
        recording.exception.assert_not_called()
        self.assertEqual(
            Memory(database_path, logger=TEST_LOGGER).schema_version(),
            NEWER_SCHEMA_VERSION,
        )

    def test_a_newer_stamp_is_too_new_whatever_its_shape(self) -> None:
        for label, schema in (
            ("version seven shape", V7_SCHEMA),
            ("version six shape", V6_SCHEMA),
            ("version five shape", V5_SCHEMA),
            (
                "unknown later shape",
                V6_SCHEMA
                + (
                    "CREATE TABLE audit_annotations "
                    "(summary_id INTEGER PRIMARY KEY, note TEXT NOT NULL)",
                ),
            ),
            ("version four shape", V4_SCHEMA),
        ):
            with self.subTest(shape=label):
                database_path = self._stamped_newer(label.replace(" ", "-"), schema)
                before_bytes = database_path.read_bytes()
                recording = Mock(spec=logging.Logger)

                with self.assertRaises(TypedFailureError) as raised:
                    Memory(database_path, logger=recording).initialize()

                # Refused on the stamp alone, before the shape is examined.
                self.assertEqual(
                    raised.exception.failure,
                    Failure(FailureKind.DATABASE_SCHEMA_TOO_NEW),
                )
                self.assertEqual(database_path.read_bytes(), before_bytes)
                self.assertEqual(
                    read_schema_version(database_path),
                    NEWER_SCHEMA_VERSION,
                )
                self.assertEqual(_messages(recording, "error"), [TOO_NEW_LINE])
                self.assertEqual(_messages(recording), [])


# --- The real log ------------------------------------------------------------


class AuditMigrationLoggingTests(unittest.TestCase):
    """Prove the audit step writes fixed identifiers to a real log and no more.

    The production ``configure_logging()`` installs the real handler under a
    temporary directory and the assertions read the file it wrote, so Maria's
    live log is never opened.
    """

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database_path = self.root / "database" / "maria-test.db"
        self.log_dir = self.root / "logs"
        self.log_file = self.log_dir / "maria.log"

        self.maria_logger = logging.getLogger("maria")
        self.original_handlers = tuple(self.maria_logger.handlers)
        self.original_level = self.maria_logger.level
        self.original_propagate = self.maria_logger.propagate
        self.original_disabled = self.maria_logger.disabled
        for handler in self.original_handlers:
            self.maria_logger.removeHandler(handler)
        self.addCleanup(self._restore_maria_logger)

    def _restore_maria_logger(self) -> None:
        """Close what this test configured and put the previous state back."""
        for handler in list(self.maria_logger.handlers):
            try:
                handler.flush()
            finally:
                self.maria_logger.removeHandler(handler)
                handler.close()
        for handler in self.original_handlers:
            self.maria_logger.addHandler(handler)
        self.maria_logger.setLevel(self.original_level)
        self.maria_logger.propagate = self.original_propagate
        self.maria_logger.disabled = self.original_disabled

    def _logged(self) -> str:
        """Flush every handler and return what the real log file holds."""
        for handler in self.maria_logger.handlers:
            handler.flush()
        return self.log_file.read_text(encoding="utf-8")

    def _entries(self) -> list[tuple[str, ...]]:
        """Return each line of the real log as its level, logger, and message.

        The timestamp is dropped. A line the log format did not produce, such
        as one from a traceback, is kept whole, so an exact comparison fails
        on it rather than skipping it.
        """
        entries: list[tuple[str, ...]] = []
        for line in self._logged().splitlines():
            fields = line.split(" | ", 3)
            entries.append(tuple(fields[1:]) if len(fields) == 4 else (line,))
        return entries

    def _stored_sentinels(self) -> list[str]:
        """Return every stored value and path the log must never repeat."""
        sentinels = [str(self.database_path), str(self.database_path.parent)]
        for _identifier, request, response, _created in BASELINE_INTERACTIONS:
            sentinels.extend((request, response))
        for _identifier, title, body, _created, _updated in BASELINE_NOTES:
            sentinels.extend((title, body))
        for _identifier, fact, normalized, _created in BASELINE_MEMORIES:
            sentinels.extend((fact, normalized))
        sentinels.extend(
            (
                "Traceback",
                "sqlite3.",
                "CREATE TABLE",
                "INSERT INTO",
                "SELECT ",
                "audit_outcomes",
                "saved_conversations",
                "conversation_summaries",
            )
        )
        return sentinels

    def test_the_audit_step_writes_its_fixed_line_to_a_real_log(self) -> None:
        _build_stamped_v4(self.database_path)
        configure_logging(self.log_dir, "DEBUG")

        Memory(self.database_path, logger=get_logger("memory")).initialize()
        logged = self._logged()

        self.assertIn(f"| INFO | maria.memory | {APPLIED_AUDIT_STEP_LINE}", logged)
        self.assertEqual(logged.count(APPLIED_AUDIT_STEP_LINE), 1)
        # The conversation-record step and V1-22's task step follow it, each
        # with its own one line.
        self.assertEqual(logged.count("Applied migration"), 3)
        self.assertIn(f"| INFO | maria.memory | {READY_LINE}", logged)
        self.assertEqual(
            self._entries(),
            [
                LOGGING_CONFIGURED_ENTRY,
                ("INFO", "maria.memory", APPLIED_AUDIT_STEP_LINE),
                (
                    "INFO",
                    "maria.memory",
                    APPLIED_STEP_LINES["create_conversation_records"],
                ),
                (
                    "INFO",
                    "maria.memory",
                    APPLIED_STEP_LINES["create_tasks_and_reminders"],
                ),
                ("INFO", "maria.memory", READY_LINE),
            ],
        )
        assert_log_omits(self, self.log_file, self._stored_sentinels())

    def test_a_full_upgrade_logs_each_step_in_order_and_nothing_stored(self) -> None:
        _build_unversioned_baseline(self.database_path)
        configure_logging(self.log_dir, "DEBUG")

        Memory(self.database_path, logger=get_logger("memory")).initialize()

        # Every line once, in order, and nothing else.
        self.assertEqual(
            self._entries(),
            [
                LOGGING_CONFIGURED_ENTRY,
                (
                    "INFO",
                    "maria.memory",
                    "Classified an unversioned database as schema version 2 "
                    "and stamped it.",
                ),
                (
                    "INFO",
                    "maria.memory",
                    "Applied migration add_memory_context_consent; schema "
                    "version is now 3.",
                ),
                (
                    "INFO",
                    "maria.memory",
                    "Applied migration add_note_context_consent; schema "
                    "version is now 4.",
                ),
                ("INFO", "maria.memory", APPLIED_AUDIT_STEP_LINE),
                (
                    "INFO",
                    "maria.memory",
                    "Applied migration create_conversation_records; schema "
                    "version is now 6.",
                ),
                (
                    "INFO",
                    "maria.memory",
                    "Applied migration create_tasks_and_reminders; schema "
                    "version is now 7.",
                ),
                ("INFO", "maria.memory", READY_LINE),
            ],
        )
        assert_log_omits(self, self.log_file, self._stored_sentinels())

    def test_a_failed_audit_step_logs_no_sqlite_text_or_traceback(self) -> None:
        _build_stamped_v4(self.database_path)
        configure_logging(self.log_dir, "DEBUG")
        broken = _broken_ladder(
            *_real_audit_step().statements,
            NEVER_COMMITTED,
        )

        with patch.object(memory_module, "_MIGRATIONS", broken):
            with self.assertRaisesRegex(RuntimeError, DATABASE_REFUSED_PATTERN):
                Memory(self.database_path, logger=get_logger("memory")).initialize()
        logged = self._logged()

        self.assertIn(f"| ERROR | maria.memory | {FAILED_AUDIT_STEP_LINE}", logged)
        self.assertNotIn("Applied migration", logged)
        self.assertEqual(
            self._entries(),
            [
                LOGGING_CONFIGURED_ENTRY,
                ("ERROR", "maria.memory", FAILED_AUDIT_STEP_LINE),
            ],
        )
        assert_log_omits(
            self,
            self.log_file,
            (
                *self._stored_sentinels(),
                "OperationalError",
                "never_committed",
                "INVALID",
                "incomplete input",
            ),
        )

    def test_contention_during_the_audit_step_logs_only_its_fixed_line(self) -> None:
        _build_stamped_v4(self.database_path)
        configure_logging(self.log_dir, "DEBUG")
        release = _hold_write_lock(self, self.database_path)

        with patch.object(
            memory_module,
            "DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS",
            SHORT_BUSY_TIMEOUT_SECONDS,
        ):
            with self.assertRaisesRegex(RuntimeError, DATABASE_BUSY_PATTERN):
                Memory(self.database_path, logger=get_logger("memory")).initialize()
        release()
        logged = self._logged()

        self.assertIn(f"| ERROR | maria.memory | {BUSY_AUDIT_STEP_LINE}", logged)
        self.assertNotIn("Applied migration", logged)
        self.assertEqual(
            self._entries(),
            [
                LOGGING_CONFIGURED_ENTRY,
                ("ERROR", "maria.memory", BUSY_AUDIT_STEP_LINE),
            ],
        )
        assert_log_omits(
            self,
            self.log_file,
            (
                *self._stored_sentinels(),
                "OperationalError",
                "database is locked",
            ),
        )


# --- The migrated tables in use ----------------------------------------------


class MigratedDatabaseAuditFlowTests(unittest.TestCase):
    """Drive real audit flows against databases Maria's startup migrated."""

    def setUp(self) -> None:
        timestamp = patch.object(
            memory_module,
            "audit_timestamp",
            return_value=FIXED_AUDIT_TIMESTAMP,
        )
        timestamp.start()
        self.addCleanup(timestamp.stop)

    def test_a_migrated_baseline_records_an_approved_model_action_in_its_new_tables(
        self,
    ) -> None:
        provider = _ScriptedProvider(CALCULATOR_PROPOSAL)
        harness = _MigratedHarness(
            self,
            build=_build_unversioned_baseline,
            provider=provider,
        )
        session = harness.session()
        self.assertEqual(
            harness.memory.schema_version(),
            CURRENT_SCHEMA_VERSION,
        )
        self.assertEqual(harness.row_counts(), BASELINE_COUNTS)
        owner_rows = _record_rows(harness.database_path)

        offered = session.submit(CALCULATOR_REQUEST)

        self.assertTrue(offered.needs_confirmation)
        self.assertEqual(offered.text, CALCULATOR_APPROVAL)
        self.assertIs(offered.plan.origin, PlanOrigin.MODEL)
        self.assertIsNotNone(session.pending_approval)
        self.assertEqual(provider.calls, 1)
        harness.check.assert_called_once_with("calculator", "calculate")
        harness.prepare_confirmation.assert_not_called()
        harness.assert_nothing_dispatched_or_audited(self)
        self.assertEqual(harness.row_counts(), BASELINE_COUNTS)

        confirmed = session.confirm()

        self.assertEqual(confirmed.text, CALCULATOR_RESULT)
        self.assertFalse(confirmed.needs_confirmation)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(harness.create_plan.call_count, 1)
        self.assertEqual(harness.check.call_count, 2)
        harness.prepare_confirmation.assert_not_called()
        harness.record_audit_summary.assert_called_once_with(
            "calculator.calculate",
            AuditDecision.OWNER_APPROVED,
            AuditTarget.NONE,
        )
        harness.record_audit_outcome.assert_called_once_with(1, AuditOutcome.SUCCEEDED)
        harness.execute_tool.assert_called_once()
        tool_name, action, arguments = harness.execute_tool.call_args.args
        self.assertEqual((tool_name, action), ("calculator", "calculate"))
        self.assertIs(arguments, offered.plan.steps[0].arguments)
        self.assertEqual(
            harness.audit_rows(),
            ([FIRST_CALCULATOR_SUMMARY_ROW], [(1, "succeeded")]),
        )
        # A model-proposed action is transient: no interaction row, and every
        # owner record the migration kept is exactly as it was.
        self.assertEqual(_record_rows(harness.database_path), owner_rows)

        listed = session.submit("audit")
        read = session.submit("read audit 1")

        self.assertEqual(
            listed.text,
            "1: 2026-10-01T09:46:17Z calculator.calculate (target: none). "
            "Approved by the owner. Outcome: succeeded.",
        )
        self.assertEqual(
            read.text,
            "\n".join(
                (
                    "Audit summary 1",
                    "Created at: 2026-10-01T09:46:17Z",
                    "Action: calculator.calculate",
                    "Target: none",
                    "Decision: approved by the owner",
                    "Outcome: succeeded",
                )
            ),
        )
        self.assertEqual(
            harness.interactions()[len(BASELINE_INTERACTIONS) :],
            [("audit", LISTED_RECORD), ("read audit 1", "Displayed audit summary 1.")],
        )
        self.assertEqual(provider.calls, 1)

        # Reopening the migrated database changes nothing it now holds.
        before = harness.database_path.read_bytes()
        Memory(harness.database_path, logger=TEST_LOGGER).initialize()
        self.assertEqual(harness.database_path.read_bytes(), before)

    def test_a_migrated_database_never_reuses_a_deleted_summary_id(self) -> None:
        provider = _ScriptedProvider(CALCULATOR_PROPOSAL, LISTING_PROPOSAL)
        harness = _MigratedHarness(self, build=_build_stamped_v4, provider=provider)
        session = harness.session()

        session.submit(CALCULATOR_REQUEST)
        self.assertEqual(session.confirm().text, CALCULATOR_RESULT)
        held = session.submit("delete audit 1")

        self.assertTrue(held.needs_confirmation)
        self.assertEqual(held.text, DELETE_CONFIRMATION_TEXT)
        self.assertEqual(harness.row_counts()["audit_summaries"], 1)
        self.assertEqual(session.confirm().text, "Deleted audit summary 1.")
        self.assertEqual(harness.audit_rows(), ([], []))

        offered = session.submit(LISTING_REQUEST)
        self.assertEqual(offered.text, LISTING_APPROVAL)
        confirmed = session.confirm()

        self.assertEqual(
            confirmed.text,
            "Directory is empty.\n\nRecorded as audit summary 2.",
        )
        self.assertEqual(
            harness.audit_rows(),
            (
                [
                    (
                        2,
                        FIXED_AUDIT_TIMESTAMP,
                        "filesystem.list",
                        "owner_approved",
                        "named_filesystem_root",
                    )
                ],
                [(2, "succeeded")],
            ),
        )
        self.assertEqual(
            session.submit("audit").text,
            "2: 2026-10-01T09:46:17Z filesystem.list "
            "(target: named filesystem root). Approved by the owner. "
            "Outcome: succeeded.",
        )
        # Only the deterministic deletion was ever prepared for confirmation;
        # neither model-proposed plan was.
        harness.prepare_confirmation.assert_called_once()
        prepared = harness.prepare_confirmation.call_args.args[0]
        self.assertIs(prepared.origin, PlanOrigin.DETERMINISTIC)
        self.assertEqual(
            [(step.tool_name, step.action) for step in prepared.steps],
            [("audit", "delete")],
        )
        self.assertEqual(provider.calls, 2)

    def test_audit_commands_on_a_freshly_migrated_database_find_nothing(self) -> None:
        harness = _MigratedHarness(self, build=_build_stamped_v1)
        session = harness.session()

        listed = session.submit("audit")
        read = session.submit("read audit 1")
        held = session.submit("delete audit 1")
        deleted = session.confirm()

        self.assertEqual(listed.text, NO_SUMMARIES_TEXT)
        self.assertEqual(read.text, NOT_FOUND_ONE)
        self.assertEqual(held.text, DELETE_CONFIRMATION_TEXT)
        self.assertEqual(deleted.text, NOT_FOUND_ONE)
        self.assertEqual(
            harness.interactions()[len(BASELINE_INTERACTIONS) :],
            [
                ("audit", LISTED_RECORD),
                ("read audit 1", NOT_FOUND_ONE),
                ("delete audit 1", NOT_FOUND_ONE),
            ],
        )
        self.assertEqual(harness.audit_rows(), ([], []))
        self.assertEqual(harness.record_audit_summary.call_count, 0)
        self.assertEqual(harness.record_audit_outcome.call_count, 0)

    def test_a_denied_model_action_writes_nothing_into_a_migrated_database(
        self,
    ) -> None:
        provider = _ScriptedProvider(CALCULATOR_PROPOSAL)
        harness = _MigratedHarness(
            self,
            build=_build_stamped_v4,
            provider=provider,
            denied_actions=frozenset({"calculator.calculate"}),
        )
        session = harness.session()

        refused = session.submit(CALCULATOR_REQUEST)

        self.assertEqual(refused.text, DENIED_CALCULATOR_TEXT)
        self.assertFalse(refused.needs_confirmation)
        self.assertIsNone(session.pending_approval)
        self.assertEqual(provider.calls, 1)
        harness.check.assert_called_once_with("calculator", "calculate")
        harness.validate_action.assert_not_called()
        harness.prepare_confirmation.assert_not_called()
        harness.assert_nothing_dispatched_or_audited(self)
        self.assertEqual(session.confirm().text, CONFIRMATION_REFUSED_TEXT)
        harness.assert_nothing_dispatched_or_audited(self)
        self.assertEqual(harness.row_counts(), BASELINE_COUNTS)

    def test_a_configured_confirmation_still_takes_one_approval_and_one_summary(
        self,
    ) -> None:
        provider = _ScriptedProvider(CALCULATOR_PROPOSAL)
        harness = _MigratedHarness(
            self,
            build=_build_stamped_v2,
            provider=provider,
            confirmation_required=frozenset({"calculator.calculate"}),
        )
        session = harness.session()

        offered = session.submit(CALCULATOR_REQUEST)

        self.assertTrue(offered.needs_confirmation)
        self.assertEqual(offered.text, CALCULATOR_APPROVAL)
        harness.assert_nothing_dispatched_or_audited(self)

        self.assertEqual(session.confirm().text, CALCULATOR_RESULT)
        self.assertEqual(session.confirm().text, CONFIRMATION_REFUSED_TEXT)

        harness.prepare_confirmation.assert_not_called()
        self.assertEqual(harness.record_audit_summary.call_count, 1)
        self.assertEqual(harness.record_audit_outcome.call_count, 1)
        self.assertEqual(harness.execute_tool.call_count, 1)
        self.assertEqual(
            harness.row_counts(),
            {**BASELINE_COUNTS, "audit_summaries": 1, "audit_outcomes": 1},
        )


# --- The live database is never a target -------------------------------------


class LiveDatabaseRefusalTests(_TemporaryDatabaseTestCase):
    """Prove every database helper in this file refuses Maria's live directory."""

    def _helpers(self) -> tuple[tuple[str, Callable[[Path], object]], ...]:
        """Return every helper here that opens, reads, writes, or builds a database."""
        return (
            ("guard", _refuse_live_database),
            # An absolute label replaces the temporary root entirely.
            ("temporary path", lambda path: self.database(str(path))),
            ("open", _open_temporary),
            ("query", lambda path: _query(path, "SELECT 1")),
            ("execute", lambda path: _execute(path, "SELECT 1")),
            ("describe", _describe),
            ("object names", _object_names),
            ("siblings", _siblings),
            ("row counts", _row_counts),
            ("record rows", _record_rows),
            ("sequences", _sequences),
            ("audit rows", _audit_rows),
            ("write lock", lambda path: _hold_write_lock(self, path)),
            (
                "schema builder",
                lambda path: _build_schema(path, V5_SCHEMA, AUDIT_SUMMARIES_VERSION),
            ),
            *((f"start: {start.label}", start.build) for start in ALL_STARTS),
        )

    def test_every_helper_refuses_the_live_database_directory(self) -> None:
        refused_paths = {
            "live file": DATABASE_PATH,
            "live directory": DATABASE_DIR,
            "sibling": DATABASE_DIR / "audit-copy.db",
            "descendant": DATABASE_DIR / "nested" / "copy.db",
            "traversal": DATABASE_DIR / "nested" / ".." / DATABASE_PATH.name,
        }
        # A helper that failed to refuse would reach SQLite, which is made to
        # fail loudly rather than open anything.
        with patch.object(
            sqlite3,
            "connect",
            side_effect=AssertionError("the live database may never be opened"),
        ):
            for label, helper in self._helpers():
                for kind, refused in refused_paths.items():
                    with self.subTest(helper=label, path=kind):
                        with self.assertRaisesRegex(ValueError, LIVE_DATABASE_PATTERN):
                            helper(refused)

    def test_the_guard_admits_temporary_databases(self) -> None:
        harness = _MigratedHarness(self)

        self.assertIsNone(_refuse_live_database(harness.database_path))
        live_root = DATABASE_DIR.resolve()
        self.assertNotIn(live_root, harness.database_path.resolve().parents)
        self.assertEqual(harness.row_counts(), dict.fromkeys(ALL_TABLES, 0))

        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "database" / "maria-test.db"
            self.assertIsNone(_refuse_live_database(database_path))
            _build_schema(database_path, CURRENT_SCHEMA, CURRENT_SCHEMA_VERSION)
            self.assertEqual(
                read_schema_version(database_path),
                CURRENT_SCHEMA_VERSION,
            )
            self.assertEqual(_row_counts(database_path), BASELINE_COUNTS)


if __name__ == "__main__":
    unittest.main()
