"""Frozen historical database schemas for Maria's migration tests.

A migration test is only worth anything if the database it starts from is the
one owners actually have, so this module rebuilds Maria's historical schemas
from their own text rather than by calling the code under test. Nothing here
imports ``core.memory``, constructs a ``Memory``, or reads a schema constant
from it: if the live schema and the frozen baseline ever stop agreeing, a test
built on this module is the thing that notices, which it could not do if it
asked the live schema what the baseline was.

Two shapes are declared, and the set is closed. Maria stamped no version at
all before schema versioning existed, so a database with no marker can only
have come from one of them:

``V0_2_0_BASELINE_SCHEMA``
    The published v0.2.0 baseline: interaction history, notes, explicit
    memories, and the two secondary indexes. Every release from v0.1.0 to
    v0.2.0 created exactly this.

``PRE_MEMORIES_SCHEMA``
    The same without ``memories``, which is what Maria created before explicit
    memories existed.

Both are written out in full, including the statements they share, because a
frozen schema that is assembled from pieces somebody else may edit is not
frozen. Later schema-touching stages are expected to import this module and
reuse it unchanged rather than to copy it or to regenerate a baseline from
whatever the schema has become by then.

The representative rows exist to make preservation provable rather than
plausible: explicit ids, distinct timestamps, a note whose ``updated_at``
differs from its ``created_at``, and a memory whose ``normalized_fact`` is the
casefolded form of a mixed-case ``fact``. Every helper writes to a caller
supplied path that must be temporary; Maria's live database directory is
refused outright, in the same spirit as ``tests.log_assertions``.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from core.constants import DATABASE_DIR


# The published v0.2.0 schema, exactly as v0.1.0 through v0.2.0 created it.
# Frozen: never edit these to match a newer schema.
V0_2_0_BASELINE_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE interactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_input TEXT NOT NULL,
        response TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact TEXT NOT NULL CHECK (length(fact) > 0),
        normalized_fact TEXT NOT NULL UNIQUE
            CHECK (length(normalized_fact) > 0),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_interactions_created_at
        ON interactions (created_at)
    """,
    """
    CREATE INDEX idx_notes_title
        ON notes (title)
    """,
)


# The schema Maria created before explicit memories existed. Also frozen.
PRE_MEMORIES_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE interactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_input TEXT NOT NULL,
        response TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_interactions_created_at
        ON interactions (created_at)
    """,
    """
    CREATE INDEX idx_notes_title
        ON notes (title)
    """,
)


# Representative owner records. The ids are explicit and non-consecutive so a
# migration that renumbered or reinserted rows could not pass by accident, and
# every timestamp is distinct so ordering columns cannot be confused with one
# another.
BASELINE_INTERACTIONS: tuple[tuple[int, str, str, str], ...] = (
    (1, "remember my editor is VS Code", "Remembered memory 1.", "2026-01-01T09:00:00+00:00"),
    (4, "history", "1: remember my editor is VS Code", "2026-01-02T09:30:00+00:00"),
)

BASELINE_NOTES: tuple[tuple[int, str, str, str, str], ...] = (
    (
        2,
        "Release checklist",
        "Tag only after the audit passes.",
        "2026-01-03T10:00:00+00:00",
        "2026-01-05T18:45:00+00:00",
    ),
    (
        7,
        "Reading list",
        "SQLite file format internals.",
        "2026-01-04T11:15:00+00:00",
        "2026-01-04T11:15:00+00:00",
    ),
)

BASELINE_MEMORIES: tuple[tuple[int, str, str, str], ...] = (
    (3, "My Editor Is VS Code", "my editor is vs code", "2026-01-01T09:00:00+00:00"),
    (5, "Straße", "strasse", "2026-01-06T08:20:00+00:00"),
)


def create_v0_2_0_baseline(database_path: Path, *, with_rows: bool = True) -> None:
    """Build an unversioned v0.2.0 baseline database at a temporary path.

    The file is left with no schema marker, which is exactly how every release
    up to v0.2.0 left it, so the code under test has to classify it rather
    than being told what it is.
    """
    _create(database_path, V0_2_0_BASELINE_SCHEMA, with_rows, with_memories=True)


def create_pre_memories_baseline(
    database_path: Path,
    *,
    with_rows: bool = True,
) -> None:
    """Build an unversioned pre-``memories`` database at a temporary path."""
    _create(database_path, PRE_MEMORIES_SCHEMA, with_rows, with_memories=False)


def create_schema(
    database_path: Path,
    statements: tuple[str, ...],
    *,
    with_rows: bool = False,
    with_memories: bool = False,
) -> None:
    """Build an arbitrary schema at a temporary path, for drift cases.

    Tests that need a deliberately wrong database - a missing table, a dropped
    uniqueness constraint, a renamed column - describe it as statements and
    build it here, so no test opens SQLite itself just to be wrong on purpose.
    """
    _create(database_path, statements, with_rows, with_memories)


def stamp_schema_version(database_path: Path, version: int) -> None:
    """Write a schema marker directly, without going through Maria.

    A test proving that a newer database is refused has to be able to write a
    version this build could not have produced, which the code under test will
    never do for it.
    """
    _refuse_live_database(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(f"PRAGMA user_version = {int(version):d}")
        connection.commit()


def read_schema_version(database_path: Path) -> int:
    """Read a database's schema marker directly, without going through Maria."""
    _refuse_live_database(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def table_names(database_path: Path) -> frozenset[str]:
    """Return a database's own table names, excluding SQLite's internal ones."""
    _refuse_live_database(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite~_%' ESCAPE '~'
            """
        ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def _create(
    database_path: Path,
    statements: tuple[str, ...],
    with_rows: bool,
    with_memories: bool,
) -> None:
    """Create a database from statements and optionally seed owner records."""
    _refuse_live_database(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        for statement in statements:
            connection.execute(statement)
        if with_rows:
            connection.executemany(
                """
                INSERT INTO interactions (id, user_input, response, created_at)
                VALUES (?, ?, ?, ?)
                """,
                BASELINE_INTERACTIONS,
            )
            connection.executemany(
                """
                INSERT INTO notes (id, title, body, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                BASELINE_NOTES,
            )
            if with_memories:
                connection.executemany(
                    """
                    INSERT INTO memories (id, fact, normalized_fact, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    BASELINE_MEMORIES,
                )
        connection.commit()


def _refuse_live_database(database_path: Path) -> None:
    """Refuse any path inside Maria's live database directory.

    Containment is settled on the fully resolved path and before anything is
    opened, so the live file itself, a sibling beside it, a descendant at any
    depth, a ``".."`` traversal, and a link that lands inside it are all
    refused rather than created or read. A fixture that reached the real
    database would have gone wrong long before its assertions ran.
    """
    live_root = DATABASE_DIR.resolve()
    resolved = Path(database_path).resolve()
    if resolved == live_root or live_root in resolved.parents:
        raise ValueError(
            "Schema fixtures must target a temporary database, "
            "never Maria's live database directory."
        )
