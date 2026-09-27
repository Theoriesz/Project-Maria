"""Tests for Maria's SQLite-backed Memory API."""

from __future__ import annotations

import ast
import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, Mock, patch

from core import memory as memory_module
from core.context import ContextConsentOutcome
from core.memory import Memory, MemoryUpdate
from tests.schema_fixtures import V0_2_0_BASELINE_SCHEMA


TEST_LOGGER = logging.getLogger("tests.memory")
TEST_LOGGER.addHandler(logging.NullHandler())
TEST_LOGGER.propagate = False


PREVIOUS_SCHEMA = """
CREATE TABLE interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_input TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_interactions_created_at
    ON interactions (created_at);

CREATE INDEX idx_notes_title
    ON notes (title);
"""


# --- SQL ownership boundary ----------------------------------------------
#
# The generalised guard at the end of this module finds the production modules
# itself rather than trusting a list of the ones somebody remembered to check.
# Its inventories are deliberately small: which top-level names are production
# code, which imports reach SQLite, and which name the memory facade exports.

MARIA_ROOT = Path(__file__).resolve().parents[1]

# The importable production roots. A new top-level production package fails
# the unscanned-root guard until its stage adds it here, which is also what
# brings it under every ownership check.
PRODUCTION_PACKAGES = ("core", "tools")
PRODUCTION_ENTRY_POINTS = ("main.py",)
TEST_PACKAGE = "tests"

# Imports that reach SQLite. ``_sqlite3`` is the CPython extension ``sqlite3``
# wraps, and ``dbm`` and ``shelve`` are listed because Python 3.13 and later
# back both with SQLite by default.
DATABASE_DRIVERS = frozenset({"sqlite3", "_sqlite3", "dbm", "shelve"})

# The functions that import a module named by a string, and the standard
# modules a caller can import them from under another name.
DYNAMIC_IMPORT_FUNCTIONS = frozenset({"import_module", "__import__"})
DYNAMIC_IMPORT_PROVIDERS = frozenset({"importlib", "builtins"})

# The only name a module outside the memory boundary may import from it.
MEMORY_FACADE = frozenset({"Memory"})

# Names that, imported from the ``core`` package itself, would reach memory
# without going through ``from core.memory import Memory``.
CORE_MEMORY_ROUTES = frozenset({"memory", "Memory", "*"})

# Calls whose first argument is a statement handed to SQLite.
SQL_EXECUTION_METHODS = frozenset({"execute", "executemany", "executescript"})

# One SQL name: quoted in any of SQLite's styles, a bare word, or the ``?``
# that stands in for an f-string substitution.
_SQL_NAME = r"""(?:"[^"]*"|\[[^\]]*\]|`[^`]*`|[^\s;(),'"`\[\]]+)"""

# Executable SQL, recognised by the shape of a statement rather than by a
# keyword alone, so a message that merely says "update" or "delete" is not
# mistaken for one.
SQL_STATEMENT = re.compile(
    "|".join(
        (
            rf"\bSELECT\s+\S[\s\S]*?\s+FROM\s+{_SQL_NAME}",
            rf"\b(?:INSERT|REPLACE)\s+(?:OR\s+\w+\s+)?INTO\s+{_SQL_NAME}",
            rf"\bUPDATE\s+(?:OR\s+\w+\s+)?{_SQL_NAME}\s+SET\s+"
            rf"(?:{_SQL_NAME}|\([^)]*\))\s*=",
            rf"\bDELETE\s+FROM\s+{_SQL_NAME}(?:\s+AS\s+{_SQL_NAME})?\s*"
            r"(?:;|$|\b(?:WHERE|RETURNING|ORDER|LIMIT|INDEXED|NOT)\b)",
            r"\bCREATE\s+(?:(?:TEMP|TEMPORARY|UNIQUE|VIRTUAL)\s+)?"
            r"(?:TABLE|INDEX|VIEW|TRIGGER)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
            rf"{_SQL_NAME}",
            r"\bDROP\s+(?:TABLE|INDEX|VIEW|TRIGGER)\s+(?:IF\s+EXISTS\s+)?"
            rf"{_SQL_NAME}",
            rf"\bALTER\s+TABLE\s+{_SQL_NAME}\s+(?:RENAME|ADD|DROP)\b",
            rf"\bPRAGMA\s+{_SQL_NAME}\s*(?:;|$|=|\()",
            r"\bBEGIN\s+(?:DEFERRED|IMMEDIATE|EXCLUSIVE)\b",
            r"\b(?:BEGIN|COMMIT|END|ROLLBACK)\s+TRANSACTION\b",
            rf"\bROLLBACK\s+(?:TRANSACTION\s+)?TO\s+SAVEPOINT\s+{_SQL_NAME}",
            rf"\bRELEASE\s+SAVEPOINT\s+{_SQL_NAME}",
            rf"\bVACUUM\s+(?:{_SQL_NAME}\s+)?INTO\b",
            r"\bATTACH\s+(?:DATABASE\s+)?(?:'[^']*'|\?|:\w+)\s+AS\s+"
            rf"{_SQL_NAME}",
            rf"\bDETACH\s+DATABASE\s+{_SQL_NAME}",
            r"\b(?:user_version|sqlite_(?:master|schema|sequence"
            r"|temp_master|temp_schema))\b",
        )
    ),
    re.IGNORECASE,
)

# A statement that can be a single word, such as ``COMMIT``, counts only when
# it is the whole string and written in capitals, so a message consisting of
# the word "Commit" is not mistaken for one.
SQL_STANDALONE_STATEMENT = re.compile(
    r"\s*(?:BEGIN|COMMIT|ROLLBACK|VACUUM"
    rf"|(?:SAVEPOINT|RELEASE|ROLLBACK\s+TO|DETACH|VACUUM)\s+{_SQL_NAME})"
    r"\s*;?\s*"
)

# A string handed to an execution method is a statement by position, so there
# a short statement is recognised in any case: SQLite reads ``commit`` exactly
# as it reads ``COMMIT``. Each ``;``-separated statement is judged on its own.
SQL_EXECUTED_STATEMENT = re.compile(
    r"\s*(?:(?:BEGIN(?:\s+(?:DEFERRED|IMMEDIATE|EXCLUSIVE))?|COMMIT|END|ROLLBACK)"
    r"(?:\s+TRANSACTION)?"
    r"|(?:SAVEPOINT|RELEASE(?:\s+SAVEPOINT)?|ROLLBACK(?:\s+TRANSACTION)?\s+TO"
    rf"(?:\s+SAVEPOINT)?|DETACH(?:\s+DATABASE)?)\s+{_SQL_NAME}"
    rf"|(?:VACUUM|ANALYZE|REINDEX)(?:\s+{_SQL_NAME})?)\s*",
    re.IGNORECASE,
)


class MemoryTests(unittest.TestCase):
    """Exercise the public Memory interface using only temporary databases."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = (
            Path(self.temporary_directory.name) / "database" / "maria-test.db"
        )
        self.memory = Memory(self.database_path, logger=TEST_LOGGER)

    def test_fresh_initialize_creates_memories_alongside_existing_tables(self) -> None:
        self.memory.initialize()

        with closing(sqlite3.connect(self.database_path)) as connection:
            table_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            memory_columns = {
                row[1]: (row[2], row[3], row[5])
                for row in connection.execute("PRAGMA table_info(memories)")
            }

        self.assertTrue({"interactions", "notes", "memories"} <= table_names)
        self.assertEqual(
            memory_columns,
            {
                "id": ("INTEGER", 0, 1),
                "fact": ("TEXT", 1, 0),
                "normalized_fact": ("TEXT", 1, 0),
                "created_at": ("TEXT", 1, 0),
                "context_enabled": ("INTEGER", 1, 0),
            },
        )

    def test_initialize_is_idempotent(self) -> None:
        self.memory.initialize()
        memory_id, created = self.memory.store_memory("Persistent fact")

        self.memory.initialize()
        stored = self.memory.list_memories()

        self.assertTrue(created)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["id"], memory_id)
        self.assertEqual(stored[0]["fact"], "Persistent fact")

    def test_previous_schema_migrates_additively_without_data_loss(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(PREVIOUS_SCHEMA)
            connection.execute(
                """
                INSERT INTO interactions (user_input, response, created_at)
                VALUES (?, ?, ?)
                """,
                ("system", "System information", "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(
                """
                INSERT INTO notes (title, body, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "Existing note",
                    "Keep this body",
                    "2026-01-02T00:00:00+00:00",
                    "2026-01-02T00:00:00+00:00",
                ),
            )
            connection.commit()

        self.memory.initialize()

        with closing(sqlite3.connect(self.database_path)) as connection:
            table_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            interaction = connection.execute(
                "SELECT user_input, response, created_at FROM interactions"
            ).fetchone()
            note = connection.execute(
                "SELECT title, body, created_at, updated_at FROM notes"
            ).fetchone()

        self.assertIn("memories", table_names)
        self.assertEqual(
            interaction,
            ("system", "System information", "2026-01-01T00:00:00+00:00"),
        )
        self.assertEqual(
            note,
            (
                "Existing note",
                "Keep this body",
                "2026-01-02T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            ),
        )

    def test_store_memory_strips_fact_and_returns_created_id(self) -> None:
        self.memory.initialize()

        memory_id, created = self.memory.store_memory("  My editor is VS Code  ")
        stored = self.memory.list_memories()

        self.assertGreater(memory_id, 0)
        self.assertTrue(created)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["id"], memory_id)
        self.assertEqual(stored[0]["fact"], "My editor is VS Code")
        self.assertEqual(set(stored[0]), {"id", "fact", "created_at"})

    def test_get_memory_returns_exact_row_or_none_for_missing_id(self) -> None:
        self.memory.initialize()
        created_at = "2026-02-01T00:00:00+00:00"
        with patch("core.memory._utc_timestamp", return_value=created_at):
            memory_id, _ = self.memory.store_memory("My editor is VS Code")

        stored = self.memory.get_memory(memory_id)

        self.assertEqual(
            stored,
            {
                "id": memory_id,
                "fact": "My editor is VS Code",
                "created_at": created_at,
            },
        )
        self.assertIsNone(self.memory.get_memory(memory_id + 1))

    def test_get_memory_wraps_database_errors(self) -> None:
        self.memory.initialize()

        with patch.object(
            self.memory,
            "_connect",
            side_effect=sqlite3.OperationalError("database unavailable"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to read memory\.$",
            ) as raised:
                self.memory.get_memory(1)

        self.assertIsInstance(raised.exception.__cause__, sqlite3.OperationalError)

    def test_connect_passes_explicit_busy_timeout(self) -> None:
        self.memory.initialize()

        with patch(
            "core.memory.sqlite3.connect",
            wraps=sqlite3.connect,
        ) as connect_spy:
            self.memory.list_memories()

        connect_spy.assert_called_once_with(
            self.database_path,
            timeout=memory_module.DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS,
        )

    def test_busy_error_during_execution_becomes_fixed_message(self) -> None:
        self.memory.initialize()
        busy_exc = sqlite3.OperationalError("database is locked")
        busy_exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
        busy_exc.sqlite_errorname = "SQLITE_BUSY"

        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = busy_exc

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Database is busy\. Try again shortly\.$",
            ) as raised:
                self.memory.list_memories()

        self.assertIs(raised.exception.__cause__, busy_exc)
        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_busy_error_during_commit_becomes_fixed_message(self) -> None:
        self.memory.initialize()
        locked_exc = sqlite3.OperationalError("database is locked")
        locked_exc.sqlite_errorcode = sqlite3.SQLITE_LOCKED
        locked_exc.sqlite_errorname = "SQLITE_LOCKED"

        mock_cursor = MagicMock()
        mock_cursor.lastrowid = 1
        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.return_value = mock_cursor
        mock_connection.commit.side_effect = locked_exc

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Database is busy\. Try again shortly\.$",
            ) as raised:
                self.memory.record_interaction("hello", "hi there")

        self.assertIs(raised.exception.__cause__, locked_exc)
        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_non_busy_error_during_execution_keeps_existing_message(self) -> None:
        self.memory.initialize()
        io_error = sqlite3.OperationalError("disk I/O error")
        io_error.sqlite_errorcode = 10  # SQLITE_IOERR: unrelated to busy/locked.

        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = io_error

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to list memories\.$",
            ) as raised:
                self.memory.list_memories()

        self.assertIs(raised.exception.__cause__, io_error)
        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_successful_operation_commits_and_closes(self) -> None:
        self.memory.initialize()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.return_value = mock_cursor

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            result = self.memory.list_memories()

        self.assertEqual(result, [])
        mock_connection.commit.assert_called_once()
        mock_connection.rollback.assert_not_called()
        mock_connection.close.assert_called_once()

    def test_busy_logging_retains_original_exception_and_response_is_path_free(
        self,
    ) -> None:
        mock_logger = Mock(spec=logging.Logger)
        memory = Memory(self.database_path, logger=mock_logger)
        memory.initialize()
        mock_logger.reset_mock()

        busy_exc = sqlite3.OperationalError("database is locked")
        busy_exc.sqlite_errorcode = sqlite3.SQLITE_BUSY

        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = busy_exc

        with patch.object(memory, "_connect", return_value=mock_connection):
            with self.assertRaises(RuntimeError) as raised:
                memory.list_memories()

        mock_logger.exception.assert_called_once()
        self.assertEqual(
            str(raised.exception),
            "Database is busy. Try again shortly.",
        )
        self.assertNotIn(str(self.database_path), str(raised.exception))

    def test_operation_completes_normally_after_lock_releases_within_window(
        self,
    ) -> None:
        """A brief real lock resolves silently once it clears within the window."""
        self.memory.initialize()
        test_timeout = 2.0

        holder = sqlite3.connect(
            self.database_path,
            timeout=test_timeout,
            check_same_thread=False,
        )
        releaser: threading.Thread | None = None
        try:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute(
                """
                INSERT INTO memories (fact, normalized_fact, created_at)
                VALUES (?, ?, ?)
                """,
                ("Held fact", "held fact", "2026-01-01T00:00:00+00:00"),
            )

            def release_holder() -> None:
                time.sleep(0.2)
                holder.commit()

            releaser = threading.Thread(target=release_holder)
            releaser.start()

            with patch(
                "core.memory.DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS",
                test_timeout,
            ):
                memory_id, created = self.memory.store_memory("Contends briefly")

            self.assertTrue(created)
            self.assertGreater(memory_id, 0)
        finally:
            if releaser is not None:
                releaser.join(timeout=5)
            try:
                holder.close()
            except sqlite3.Error:
                pass

    def test_casefolded_duplicate_keeps_original_fact_and_timestamp(self) -> None:
        self.memory.initialize()
        original_timestamp = "2026-02-01T00:00:00+00:00"
        with patch(
            "core.memory._utc_timestamp",
            side_effect=[original_timestamp, "2026-03-01T00:00:00+00:00"],
        ):
            first_id, first_created = self.memory.store_memory(
                "My editor is VS Code"
            )
            duplicate_id, duplicate_created = self.memory.store_memory(
                "  MY EDITOR IS VS CODE  "
            )

        stored = self.memory.list_memories()
        self.assertTrue(first_created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate_id, first_id)
        self.assertEqual(
            stored,
            [
                {
                    "id": first_id,
                    "fact": "My editor is VS Code",
                    "created_at": original_timestamp,
                }
            ],
        )

    def test_search_is_case_insensitive_using_casefold(self) -> None:
        self.memory.initialize()
        memory_id, _ = self.memory.store_memory("Stra\u00dfe in Berlin")

        matches = self.memory.search_memories("STRASSE")

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["id"], memory_id)
        self.assertEqual(matches[0]["fact"], "Stra\u00dfe in Berlin")
        self.assertEqual(set(matches[0]), {"id", "fact", "created_at"})

    def test_search_treats_percent_and_underscore_as_literals(self) -> None:
        self.memory.initialize()
        percent_id, _ = self.memory.store_memory("Progress is 100% complete")
        underscore_id, _ = self.memory.store_memory("Use snake_case names")
        self.memory.store_memory("A plain fact")

        percent_matches = self.memory.search_memories("%")
        underscore_matches = self.memory.search_memories("_")

        self.assertEqual([row["id"] for row in percent_matches], [percent_id])
        self.assertEqual(
            [row["id"] for row in underscore_matches], [underscore_id]
        )

    def test_search_is_substring_based_and_not_fuzzy(self) -> None:
        self.memory.initialize()
        memory_id, _ = self.memory.store_memory("My editor is VS Code")

        substring_matches = self.memory.search_memories("editor is")
        fuzzy_matches = self.memory.search_memories("edtor")

        self.assertEqual([row["id"] for row in substring_matches], [memory_id])
        self.assertEqual(fuzzy_matches, [])

    def test_search_returns_empty_list_when_there_is_no_match(self) -> None:
        self.memory.initialize()
        self.memory.store_memory("My editor is VS Code")

        self.assertEqual(self.memory.search_memories("terminal"), [])

    def test_list_orders_by_newest_timestamp_then_descending_id(self) -> None:
        self.memory.initialize()
        with patch(
            "core.memory._utc_timestamp",
            side_effect=[
                "2026-01-01T00:00:00+00:00",
                "2026-02-01T00:00:00+00:00",
                "2026-02-01T00:00:00+00:00",
            ],
        ):
            old_id, _ = self.memory.store_memory("Old fact")
            first_new_id, _ = self.memory.store_memory("First new fact")
            second_new_id, _ = self.memory.store_memory("Second new fact")

        listed_ids = [row["id"] for row in self.memory.list_memories()]
        self.assertEqual(listed_ids, [second_new_id, first_new_id, old_id])

    def test_empty_fact_raises_value_error(self) -> None:
        self.memory.initialize()

        for fact in ("", "   ", "\t\r\n"):
            with self.subTest(fact=fact):
                with self.assertRaises(ValueError):
                    self.memory.store_memory(fact)

    def test_empty_search_query_raises_value_error(self) -> None:
        self.memory.initialize()

        for query in ("", "   ", "\t\r\n"):
            with self.subTest(query=query):
                with self.assertRaises(ValueError):
                    self.memory.search_memories(query)

    def test_second_memory_instance_retrieves_persisted_fact(self) -> None:
        self.memory.initialize()
        memory_id, _ = self.memory.store_memory("Survives a restart")

        restarted_memory = Memory(self.database_path, logger=TEST_LOGGER)
        restarted_memory.initialize()
        matches = restarted_memory.search_memories("RESTART")

        self.assertEqual([row["id"] for row in matches], [memory_id])
        self.assertEqual(matches[0]["fact"], "Survives a restart")

    def test_existing_interaction_and_note_methods_still_work(self) -> None:
        self.memory.initialize()

        interaction_id = self.memory.record_interaction("system", "System details")
        note_id = self.memory.create_note("Editor", "VS Code")

        interactions = self.memory.get_recent_interactions()
        notes = self.memory.list_notes()
        note = self.memory.get_note(note_id=note_id)

        self.assertEqual(interactions[0]["id"], interaction_id)
        self.assertEqual(interactions[0]["user_input"], "system")
        self.assertEqual(interactions[0]["response"], "System details")
        self.assertEqual(notes[0]["id"], note_id)
        self.assertEqual(notes[0]["title"], "Editor")
        self.assertIsNotNone(note)
        self.assertEqual(note["body"], "VS Code")

    def test_get_interaction_returns_exact_row_or_none_for_missing_id(self) -> None:
        self.memory.initialize()
        created_at = "2026-02-01T00:00:00+00:00"
        with patch("core.memory._utc_timestamp", return_value=created_at):
            interaction_id = self.memory.record_interaction("system", "System details")

        stored = self.memory.get_interaction(interaction_id)

        self.assertEqual(
            stored,
            {
                "id": interaction_id,
                "user_input": "system",
                "response": "System details",
                "created_at": created_at,
            },
        )
        self.assertIsNone(self.memory.get_interaction(interaction_id + 1))

    def test_get_interaction_wraps_database_errors(self) -> None:
        self.memory.initialize()

        with patch.object(
            self.memory,
            "_connect",
            side_effect=sqlite3.OperationalError("database unavailable"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to read interaction\.$",
            ) as raised:
                self.memory.get_interaction(1)

        self.assertIsInstance(raised.exception.__cause__, sqlite3.OperationalError)

    def test_search_interactions_matches_user_input_column(self) -> None:
        self.memory.initialize()
        interaction_id = self.memory.record_interaction(
            "remember my editor is VS Code",
            "Remembered memory 1: my editor is VS Code",
        )
        self.memory.record_interaction("time", "2026-01-01T00:00:00+00:00")

        matches = self.memory.search_interactions("editor")

        self.assertEqual([row["id"] for row in matches], [interaction_id])

    def test_search_interactions_matches_response_column(self) -> None:
        self.memory.initialize()
        interaction_id = self.memory.record_interaction(
            "recall editor",
            "1: my editor is VS Code",
        )
        self.memory.record_interaction("time", "2026-01-01T00:00:00+00:00")

        matches = self.memory.search_interactions("VS Code")

        self.assertEqual([row["id"] for row in matches], [interaction_id])

    def test_search_interactions_treats_percent_and_underscore_as_literals(
        self,
    ) -> None:
        self.memory.initialize()
        percent_id = self.memory.record_interaction(
            "progress", "Progress is 100% complete"
        )
        underscore_id = self.memory.record_interaction(
            "naming", "Use snake_case names"
        )
        self.memory.record_interaction("plain", "A plain fact")

        percent_matches = self.memory.search_interactions("%")
        underscore_matches = self.memory.search_interactions("_")

        self.assertEqual([row["id"] for row in percent_matches], [percent_id])
        self.assertEqual(
            [row["id"] for row in underscore_matches], [underscore_id]
        )

    def test_search_interactions_is_ascii_case_insensitive_not_full_unicode_casefold(
        self,
    ) -> None:
        self.memory.initialize()
        interaction_id = self.memory.record_interaction("note", "Straße in Berlin")

        ascii_matches = self.memory.search_interactions("BERLIN")
        unicode_casefold_matches = self.memory.search_interactions("STRASSE")

        self.assertEqual([row["id"] for row in ascii_matches], [interaction_id])
        self.assertEqual(unicode_casefold_matches, [])

    def test_search_interactions_is_substring_based_and_not_fuzzy(self) -> None:
        self.memory.initialize()
        interaction_id = self.memory.record_interaction("note", "My editor is VS Code")

        substring_matches = self.memory.search_interactions("editor is")
        fuzzy_matches = self.memory.search_interactions("edtor")

        self.assertEqual([row["id"] for row in substring_matches], [interaction_id])
        self.assertEqual(fuzzy_matches, [])

    def test_search_interactions_orders_newest_first_then_descending_id(self) -> None:
        self.memory.initialize()
        with patch(
            "core.memory._utc_timestamp",
            side_effect=[
                "2026-01-01T00:00:00+00:00",
                "2026-02-01T00:00:00+00:00",
                "2026-02-01T00:00:00+00:00",
            ],
        ):
            old_id = self.memory.record_interaction("marker", "old marker")
            first_new_id = self.memory.record_interaction("marker", "first new marker")
            second_new_id = self.memory.record_interaction(
                "marker", "second new marker"
            )

        matched_ids = [row["id"] for row in self.memory.search_interactions("marker")]
        self.assertEqual(matched_ids, [second_new_id, first_new_id, old_id])

    def test_search_interactions_clamps_limit_to_one_through_fifty(self) -> None:
        self.memory.initialize()
        for index in range(55):
            self.memory.record_interaction(f"marker {index}", "marker response")

        self.assertEqual(len(self.memory.search_interactions("marker", limit=0)), 1)
        self.assertEqual(len(self.memory.search_interactions("marker", limit=200)), 50)

    def test_search_interactions_raises_on_empty_or_whitespace_query(self) -> None:
        self.memory.initialize()

        for query in ("", "   ", "\t\r\n"):
            with self.subTest(query=query):
                with self.assertRaisesRegex(
                    ValueError,
                    r"^Search query is required\.$",
                ):
                    self.memory.search_interactions(query)

    def test_search_interactions_wraps_database_errors(self) -> None:
        self.memory.initialize()

        with patch.object(
            self.memory,
            "_connect",
            side_effect=sqlite3.OperationalError("database unavailable"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to search interactions\.$",
            ) as raised:
                self.memory.search_interactions("marker")

        self.assertIsInstance(raised.exception.__cause__, sqlite3.OperationalError)

    def test_delete_interaction_removes_target_and_leaves_others(self) -> None:
        self.memory.initialize()
        keep_id = self.memory.record_interaction("keep", "keep response")
        delete_id = self.memory.record_interaction("delete", "delete response")

        deleted = self.memory.delete_interaction(delete_id)

        self.assertTrue(deleted)
        self.assertIsNone(self.memory.get_interaction(delete_id))
        self.assertIsNotNone(self.memory.get_interaction(keep_id))

    def test_delete_interaction_returns_false_for_missing_id(self) -> None:
        self.memory.initialize()

        self.assertFalse(self.memory.delete_interaction(999))

    def test_delete_interaction_does_not_renumber_or_reuse_ids(self) -> None:
        self.memory.initialize()
        first_id = self.memory.record_interaction("first", "first response")

        self.assertTrue(self.memory.delete_interaction(first_id))
        second_id = self.memory.record_interaction("second", "second response")

        self.assertGreater(second_id, first_id)
        self.assertNotEqual(second_id, first_id)

    def test_delete_interaction_does_not_affect_notes_or_memories(self) -> None:
        self.memory.initialize()
        interaction_id = self.memory.record_interaction("system", "details")
        note_id = self.memory.create_note("Editor", "VS Code")
        memory_id, _ = self.memory.store_memory("My editor is VS Code")

        self.assertTrue(self.memory.delete_interaction(interaction_id))

        self.assertIsNotNone(self.memory.get_note(note_id=note_id))
        self.assertIsNotNone(self.memory.get_memory(memory_id))

    def test_delete_interaction_rejects_invalid_direct_ids_without_connecting(
        self,
    ) -> None:
        self.memory.initialize()
        invalid_ids = (0, -1, "1", 1.0, True, False, None)

        for invalid_id in invalid_ids:
            with self.subTest(invalid_id=invalid_id):
                with patch.object(self.memory, "_connect") as connect_mock:
                    with self.assertRaisesRegex(
                        ValueError,
                        r"^Interaction id must be a positive whole number\.$",
                    ):
                        self.memory.delete_interaction(invalid_id)
                    connect_mock.assert_not_called()

    def test_delete_interaction_binds_id_as_a_parameter_and_commits(self) -> None:
        self.memory.initialize()
        interaction_id = self.memory.record_interaction("system", "details")

        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.return_value = mock_cursor

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            deleted = self.memory.delete_interaction(interaction_id)

        self.assertTrue(deleted)
        query, params = mock_connection.execute.call_args.args
        self.assertNotIn(str(interaction_id), query)
        self.assertEqual(params, (interaction_id,))
        self.assertIn("DELETE FROM interactions", query)
        self.assertIn("WHERE id = ?", query)
        mock_connection.commit.assert_called_once()
        mock_connection.rollback.assert_not_called()
        mock_connection.close.assert_called_once()

    def test_delete_interaction_wraps_database_errors(self) -> None:
        self.memory.initialize()
        interaction_id = self.memory.record_interaction("system", "details")

        with patch.object(
            self.memory,
            "_connect",
            side_effect=sqlite3.OperationalError("database unavailable"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to delete interaction\.$",
            ) as raised:
                self.memory.delete_interaction(interaction_id)

        self.assertIsInstance(raised.exception.__cause__, sqlite3.OperationalError)

    def test_delete_interaction_ordinary_failure_rolls_back_and_closes(self) -> None:
        self.memory.initialize()
        io_error = sqlite3.OperationalError("disk I/O error")
        io_error.sqlite_errorcode = 10  # SQLITE_IOERR: unrelated to busy/locked.

        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = io_error

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to delete interaction\.$",
            ):
                self.memory.delete_interaction(1)

        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_delete_interaction_busy_error_becomes_fixed_message(self) -> None:
        self.memory.initialize()
        busy_exc = sqlite3.OperationalError("database is locked")
        busy_exc.sqlite_errorcode = sqlite3.SQLITE_BUSY

        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = busy_exc

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Database is busy\. Try again shortly\.$",
            ) as raised:
                self.memory.delete_interaction(1)

        self.assertIs(raised.exception.__cause__, busy_exc)
        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_delete_interaction_logs_contain_no_request_or_response_content(
        self,
    ) -> None:
        mock_logger = Mock(spec=logging.Logger)
        memory = Memory(self.database_path, logger=mock_logger)
        memory.initialize()
        interaction_id = memory.record_interaction("secret request", "secret response")
        mock_logger.reset_mock()

        deleted = memory.delete_interaction(interaction_id)
        missing = memory.delete_interaction(interaction_id)

        self.assertTrue(deleted)
        self.assertFalse(missing)
        for logger_call in mock_logger.method_calls:
            call_text = " ".join(str(arg) for arg in logger_call.args)
            self.assertNotIn("secret request", call_text)
            self.assertNotIn("secret response", call_text)

    def test_retitle_note_replaces_title_and_keeps_body_and_created_at(self) -> None:
        self.memory.initialize()
        created_at = "2026-01-01T00:00:00+00:00"
        retitled_at = "2026-02-01T00:00:00+00:00"
        with patch("core.memory._utc_timestamp", return_value=created_at):
            note_id = self.memory.create_note("Original title", "Original body")

        with patch("core.memory._utc_timestamp", return_value=retitled_at):
            retitled = self.memory.retitle_note(note_id, "  Replacement title  ")

        self.assertTrue(retitled)
        self.assertEqual(
            self.memory.get_note(note_id=note_id),
            {
                "id": note_id,
                "title": "Replacement title",
                "body": "Original body",
                "created_at": created_at,
                "updated_at": retitled_at,
            },
        )

    def test_edit_note_replaces_body_and_keeps_title_and_created_at(self) -> None:
        self.memory.initialize()
        created_at = "2026-01-01T00:00:00+00:00"
        edited_at = "2026-02-01T00:00:00+00:00"
        with patch("core.memory._utc_timestamp", return_value=created_at):
            note_id = self.memory.create_note("Original title", "Original body")

        with patch("core.memory._utc_timestamp", return_value=edited_at):
            edited = self.memory.edit_note(note_id, "  Replacement body  ")

        self.assertTrue(edited)
        self.assertEqual(
            self.memory.get_note(note_id=note_id),
            {
                "id": note_id,
                "title": "Original title",
                "body": "Replacement body",
                "created_at": created_at,
                "updated_at": edited_at,
            },
        )

    def test_retitle_note_accepts_a_title_another_note_already_uses(self) -> None:
        self.memory.initialize()
        first_id = self.memory.create_note("Shared title", "First body")
        second_id = self.memory.create_note("Second title", "Second body")

        retitled = self.memory.retitle_note(second_id, "Shared title")

        first = self.memory.get_note(note_id=first_id)
        second = self.memory.get_note(note_id=second_id)
        self.assertTrue(retitled)
        self.assertEqual(first["title"], "Shared title")
        self.assertEqual(first["body"], "First body")
        self.assertEqual(second["title"], "Shared title")
        self.assertEqual(second["body"], "Second body")

    def test_note_updates_to_unchanged_values_still_report_success(self) -> None:
        self.memory.initialize()
        note_id = self.memory.create_note("Same title", "Same body")

        retitled = self.memory.retitle_note(note_id, "Same title")
        edited = self.memory.edit_note(note_id, "Same body")

        self.assertTrue(retitled)
        self.assertTrue(edited)
        self.assertEqual(
            self.memory.get_note(note_id=note_id)["title"], "Same title"
        )
        self.assertEqual(self.memory.get_note(note_id=note_id)["body"], "Same body")

    def test_unchanged_note_updates_with_stored_timestamp_still_report_success(
        self,
    ) -> None:
        """Matching every stored column still updates a row, proving rowcount."""
        self.memory.initialize()
        note_id = self.memory.create_note("Same title", "Same body")
        stored_updated_at = self.memory.get_note(note_id=note_id)["updated_at"]

        with patch("core.memory._utc_timestamp", return_value=stored_updated_at):
            retitled = self.memory.retitle_note(note_id, "Same title")
            edited = self.memory.edit_note(note_id, "Same body")

        self.assertTrue(retitled)
        self.assertTrue(edited)
        self.assertEqual(
            self.memory.get_note(note_id=note_id)["updated_at"],
            stored_updated_at,
        )

    def test_note_updates_call_the_clock_once_and_store_that_timestamp(self) -> None:
        self.memory.initialize()
        note_id = self.memory.create_note("Original title", "Original body")
        retitled_at = "2026-03-01T00:00:00+00:00"
        edited_at = "2026-04-01T00:00:00+00:00"

        with patch(
            "core.memory._utc_timestamp",
            return_value=retitled_at,
        ) as retitle_clock:
            self.memory.retitle_note(note_id, "Replacement title")

        retitle_updated_at = self.memory.get_note(note_id=note_id)["updated_at"]

        with patch(
            "core.memory._utc_timestamp",
            return_value=edited_at,
        ) as edit_clock:
            self.memory.edit_note(note_id, "Replacement body")

        edit_updated_at = self.memory.get_note(note_id=note_id)["updated_at"]

        self.assertEqual(retitle_clock.call_count, 1)
        self.assertEqual(retitle_updated_at, retitled_at)
        self.assertEqual(edit_clock.call_count, 1)
        self.assertEqual(edit_updated_at, edited_at)

    def test_note_mutations_return_false_for_missing_id(self) -> None:
        self.memory.initialize()

        self.assertFalse(self.memory.retitle_note(999, "Replacement title"))
        self.assertFalse(self.memory.edit_note(999, "Replacement body"))
        self.assertFalse(self.memory.delete_note(999))

    def test_note_mutations_reject_invalid_direct_ids_without_connecting(
        self,
    ) -> None:
        self.memory.initialize()
        invalid_ids = (0, -1, "1", 1.0, True, False, None)
        mutations = (
            ("retitle_note", ("Replacement title",)),
            ("edit_note", ("Replacement body",)),
            ("delete_note", ()),
        )

        for method_name, extra_arguments in mutations:
            mutate = getattr(self.memory, method_name)
            for invalid_id in invalid_ids:
                with self.subTest(method=method_name, invalid_id=invalid_id):
                    with patch.object(self.memory, "_connect") as connect_mock, patch(
                        "core.memory._utc_timestamp"
                    ) as timestamp_mock:
                        with self.assertRaisesRegex(
                            ValueError,
                            r"^Note id must be a positive whole number\.$",
                        ):
                            mutate(invalid_id, *extra_arguments)
                        connect_mock.assert_not_called()
                        timestamp_mock.assert_not_called()

    def test_retitle_note_rejects_invalid_titles_without_connecting(self) -> None:
        self.memory.initialize()
        note_id = self.memory.create_note("Original title", "Original body")

        for new_title in (None, 5, True, [], object(), "", "   "):
            with self.subTest(new_title=new_title):
                with patch.object(self.memory, "_connect") as connect_mock, patch(
                    "core.memory._utc_timestamp"
                ) as timestamp_mock:
                    with self.assertRaisesRegex(
                        ValueError,
                        r"^New note title is required\.$",
                    ):
                        self.memory.retitle_note(note_id, new_title)
                    connect_mock.assert_not_called()
                    timestamp_mock.assert_not_called()

        self.assertEqual(
            self.memory.get_note(note_id=note_id)["title"], "Original title"
        )

    def test_edit_note_rejects_invalid_bodies_without_connecting(self) -> None:
        self.memory.initialize()
        note_id = self.memory.create_note("Original title", "Original body")

        for new_body in (None, 5, True, [], object(), "", "   "):
            with self.subTest(new_body=new_body):
                with patch.object(self.memory, "_connect") as connect_mock, patch(
                    "core.memory._utc_timestamp"
                ) as timestamp_mock:
                    with self.assertRaisesRegex(
                        ValueError,
                        r"^New note body is required\.$",
                    ):
                        self.memory.edit_note(note_id, new_body)
                    connect_mock.assert_not_called()
                    timestamp_mock.assert_not_called()

        self.assertEqual(
            self.memory.get_note(note_id=note_id)["body"], "Original body"
        )

    def test_retitle_note_binds_values_as_parameters_and_commits(self) -> None:
        self.memory.initialize()
        note_id = self.memory.create_note("Original title", "Original body")
        timestamp = "2026-05-01T00:00:00+00:00"

        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.return_value = mock_cursor

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with patch("core.memory._utc_timestamp", return_value=timestamp):
                retitled = self.memory.retitle_note(note_id, "  Replacement title  ")

        self.assertTrue(retitled)
        query, params = mock_connection.execute.call_args.args
        self.assertNotIn(str(note_id), query)
        self.assertNotIn("Replacement title", query)
        self.assertEqual(params, ("Replacement title", timestamp, note_id))
        self.assertIn("UPDATE notes", query)
        self.assertIn("SET title = ?", query)
        self.assertIn("WHERE id = ?", query)
        mock_connection.commit.assert_called_once()
        mock_connection.rollback.assert_not_called()
        mock_connection.close.assert_called_once()

    def test_edit_note_binds_values_as_parameters_and_commits(self) -> None:
        self.memory.initialize()
        note_id = self.memory.create_note("Original title", "Original body")
        timestamp = "2026-05-01T00:00:00+00:00"

        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.return_value = mock_cursor

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with patch("core.memory._utc_timestamp", return_value=timestamp):
                edited = self.memory.edit_note(note_id, "  Replacement body  ")

        self.assertTrue(edited)
        query, params = mock_connection.execute.call_args.args
        self.assertNotIn(str(note_id), query)
        self.assertNotIn("Replacement body", query)
        self.assertEqual(params, ("Replacement body", timestamp, note_id))
        self.assertIn("UPDATE notes", query)
        self.assertIn("SET body = ?", query)
        self.assertIn("WHERE id = ?", query)
        mock_connection.commit.assert_called_once()
        mock_connection.rollback.assert_not_called()
        mock_connection.close.assert_called_once()

    def test_delete_note_binds_id_as_a_parameter_and_commits(self) -> None:
        self.memory.initialize()
        note_id = self.memory.create_note("Original title", "Original body")

        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.return_value = mock_cursor

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            deleted = self.memory.delete_note(note_id)

        self.assertTrue(deleted)
        query, params = mock_connection.execute.call_args.args
        self.assertNotIn(str(note_id), query)
        self.assertEqual(params, (note_id,))
        self.assertIn("DELETE FROM notes", query)
        self.assertIn("WHERE id = ?", query)
        mock_connection.commit.assert_called_once()
        mock_connection.rollback.assert_not_called()
        mock_connection.close.assert_called_once()

    def test_retitle_note_ordinary_failure_rolls_back_and_closes(self) -> None:
        self.memory.initialize()
        io_error = sqlite3.OperationalError("disk I/O error")
        io_error.sqlite_errorcode = 10  # SQLITE_IOERR: unrelated to busy/locked.

        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = io_error

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to retitle note\.$",
            ) as raised:
                self.memory.retitle_note(1, "Replacement title")

        self.assertIs(raised.exception.__cause__, io_error)
        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_edit_note_ordinary_failure_rolls_back_and_closes(self) -> None:
        self.memory.initialize()
        io_error = sqlite3.OperationalError("disk I/O error")
        io_error.sqlite_errorcode = 10  # SQLITE_IOERR: unrelated to busy/locked.

        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = io_error

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to edit note\.$",
            ) as raised:
                self.memory.edit_note(1, "Replacement body")

        self.assertIs(raised.exception.__cause__, io_error)
        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_delete_note_ordinary_failure_rolls_back_and_closes(self) -> None:
        self.memory.initialize()
        io_error = sqlite3.OperationalError("disk I/O error")
        io_error.sqlite_errorcode = 10  # SQLITE_IOERR: unrelated to busy/locked.

        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = io_error

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to delete note\.$",
            ) as raised:
                self.memory.delete_note(1)

        self.assertIs(raised.exception.__cause__, io_error)
        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_retitle_note_busy_error_becomes_fixed_message(self) -> None:
        self.memory.initialize()
        busy_exc = sqlite3.OperationalError("database is locked")
        busy_exc.sqlite_errorcode = sqlite3.SQLITE_BUSY

        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = busy_exc

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Database is busy\. Try again shortly\.$",
            ) as raised:
                self.memory.retitle_note(1, "Replacement title")

        self.assertIs(raised.exception.__cause__, busy_exc)
        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_edit_note_busy_error_becomes_fixed_message(self) -> None:
        self.memory.initialize()
        busy_exc = sqlite3.OperationalError("database is locked")
        busy_exc.sqlite_errorcode = sqlite3.SQLITE_BUSY

        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = busy_exc

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Database is busy\. Try again shortly\.$",
            ) as raised:
                self.memory.edit_note(1, "Replacement body")

        self.assertIs(raised.exception.__cause__, busy_exc)
        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_delete_note_busy_error_becomes_fixed_message(self) -> None:
        self.memory.initialize()
        locked_exc = sqlite3.OperationalError("database is locked")
        locked_exc.sqlite_errorcode = sqlite3.SQLITE_LOCKED

        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = locked_exc

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Database is busy\. Try again shortly\.$",
            ) as raised:
                self.memory.delete_note(1)

        self.assertIs(raised.exception.__cause__, locked_exc)
        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_delete_note_removes_target_and_leaves_others(self) -> None:
        self.memory.initialize()
        keep_id = self.memory.create_note("Keep", "Keep body")
        delete_id = self.memory.create_note("Delete", "Delete body")

        deleted = self.memory.delete_note(delete_id)

        self.assertTrue(deleted)
        self.assertIsNone(self.memory.get_note(note_id=delete_id))
        self.assertIsNotNone(self.memory.get_note(note_id=keep_id))

    def test_delete_note_does_not_affect_interactions_or_memories(self) -> None:
        self.memory.initialize()
        note_id = self.memory.create_note("Editor", "VS Code")
        interaction_id = self.memory.record_interaction("system", "details")
        memory_id, _ = self.memory.store_memory("My editor is VS Code")

        self.assertTrue(self.memory.delete_note(note_id))

        self.assertIsNotNone(self.memory.get_interaction(interaction_id))
        self.assertIsNotNone(self.memory.get_memory(memory_id))

    def test_delete_note_does_not_renumber_or_reuse_ids(self) -> None:
        self.memory.initialize()
        first_id = self.memory.create_note("First", "First body")

        self.assertTrue(self.memory.delete_note(first_id))
        second_id = self.memory.create_note("Second", "Second body")

        self.assertGreater(second_id, first_id)
        self.assertNotEqual(second_id, first_id)

    def test_note_mutation_logs_contain_no_title_or_body_content(self) -> None:
        mock_logger = Mock(spec=logging.Logger)
        memory = Memory(self.database_path, logger=mock_logger)
        memory.initialize()
        note_id = memory.create_note("secret title", "secret body")
        mock_logger.reset_mock()

        retitled = memory.retitle_note(note_id, "secret new title")
        edited = memory.edit_note(note_id, "secret new body")
        deleted = memory.delete_note(note_id)
        missing_retitle = memory.retitle_note(note_id, "secret missing title")
        missing_edit = memory.edit_note(note_id, "secret missing body")
        missing_delete = memory.delete_note(note_id)

        self.assertTrue(retitled)
        self.assertTrue(edited)
        self.assertTrue(deleted)
        self.assertFalse(missing_retitle)
        self.assertFalse(missing_edit)
        self.assertFalse(missing_delete)
        for logger_call in mock_logger.method_calls:
            call_text = " ".join(str(arg) for arg in logger_call.args)
            self.assertNotIn("secret title", call_text)
            self.assertNotIn("secret body", call_text)
            self.assertNotIn("secret new title", call_text)
            self.assertNotIn("secret new body", call_text)
            self.assertNotIn("secret missing title", call_text)
            self.assertNotIn("secret missing body", call_text)


    def _stored_memory_row(self, memory_id: int) -> tuple[str, str, str]:
        """Read one memory's raw columns straight from the temporary database."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT fact, normalized_fact, created_at FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        return row

    def test_update_memory_replaces_fact_and_normalized_fact(self) -> None:
        self.memory.initialize()
        memory_id, _ = self.memory.store_memory("My editor is VS Code")
        original_created_at = self._stored_memory_row(memory_id)[2]

        updated, conflicting_id = self.memory.update_memory(
            memory_id, "My editor is Neovim"
        )

        self.assertTrue(updated)
        self.assertIsNone(conflicting_id)
        fact, normalized_fact, created_at = self._stored_memory_row(memory_id)
        self.assertEqual(fact, "My editor is Neovim")
        self.assertEqual(normalized_fact, "my editor is neovim")
        self.assertEqual(created_at, original_created_at)

    def test_update_memory_keeps_created_at_and_newest_first_ordering(self) -> None:
        self.memory.initialize()
        oldest_id, _ = self.memory.store_memory("Oldest fact")
        time.sleep(0.01)
        newest_id, _ = self.memory.store_memory("Newest fact")
        original_created_at = self._stored_memory_row(oldest_id)[2]

        self.assertEqual(
            self.memory.update_memory(oldest_id, "Edited oldest fact"),
            (True, None),
        )

        self.assertEqual(self._stored_memory_row(oldest_id)[2], original_created_at)
        self.assertEqual(
            [row["id"] for row in self.memory.list_memories()],
            [newest_id, oldest_id],
        )
        self.assertEqual(
            [row["id"] for row in self.memory.search_memories("fact")],
            [newest_id, oldest_id],
        )

    def test_update_memory_to_the_identical_fact_reports_success(self) -> None:
        self.memory.initialize()
        memory_id, _ = self.memory.store_memory("My editor is VS Code")
        original_created_at = self._stored_memory_row(memory_id)[2]

        self.assertEqual(
            self.memory.update_memory(memory_id, "My editor is VS Code"),
            (True, None),
        )

        fact, normalized_fact, created_at = self._stored_memory_row(memory_id)
        self.assertEqual(fact, "My editor is VS Code")
        self.assertEqual(normalized_fact, "my editor is vs code")
        self.assertEqual(created_at, original_created_at)

    def test_update_memory_casing_only_change_reports_success(self) -> None:
        self.memory.initialize()
        memory_id, _ = self.memory.store_memory("My editor is VS Code")

        self.assertEqual(
            self.memory.update_memory(memory_id, "MY EDITOR IS VS CODE"),
            (True, None),
        )

        fact, normalized_fact, _ = self._stored_memory_row(memory_id)
        self.assertEqual(fact, "MY EDITOR IS VS CODE")
        self.assertEqual(normalized_fact, "my editor is vs code")

    def test_update_memory_strips_surrounding_whitespace(self) -> None:
        self.memory.initialize()
        memory_id, _ = self.memory.store_memory("Original fact")

        self.assertEqual(
            self.memory.update_memory(memory_id, "   My   edited   fact   "),
            (True, None),
        )

        fact, normalized_fact, _ = self._stored_memory_row(memory_id)
        self.assertEqual(fact, "My   edited   fact")
        self.assertEqual(normalized_fact, "my   edited   fact")

    def test_update_memory_conflict_returns_conflicting_id_and_changes_nothing(
        self,
    ) -> None:
        self.memory.initialize()
        target_id, _ = self.memory.store_memory("My editor is VS Code")
        other_id, _ = self.memory.store_memory("Straße in Berlin")

        updated, conflicting_id = self.memory.update_memory(
            target_id, "  STRASSE IN BERLIN  "
        )

        self.assertFalse(updated)
        self.assertEqual(conflicting_id, other_id)
        self.assertEqual(
            self._stored_memory_row(target_id)[:2],
            ("My editor is VS Code", "my editor is vs code"),
        )
        self.assertEqual(
            self._stored_memory_row(other_id)[:2],
            ("Straße in Berlin", "straße in berlin".casefold()),
        )

    def test_update_memory_returns_not_found_for_missing_id(self) -> None:
        self.memory.initialize()
        memory_id, _ = self.memory.store_memory("Only fact")

        self.assertEqual(
            self.memory.update_memory(memory_id + 1000, "Replacement fact"),
            (False, None),
        )
        self.assertEqual(self._stored_memory_row(memory_id)[0], "Only fact")

    def test_memory_mutations_reject_invalid_direct_ids_without_connecting(
        self,
    ) -> None:
        self.memory.initialize()
        invalid_ids = (0, -1, "1", 1.0, True, False, None)
        mutations = (
            ("update_memory", ("Replacement fact",)),
            ("forget_memory", ()),
        )

        for method_name, extra_arguments in mutations:
            mutate = getattr(self.memory, method_name)
            for invalid_id in invalid_ids:
                with self.subTest(method=method_name, invalid_id=invalid_id):
                    with patch.object(self.memory, "_connect") as connect_mock, patch(
                        "core.memory._utc_timestamp"
                    ) as timestamp_mock:
                        with self.assertRaisesRegex(
                            ValueError,
                            r"^Memory id must be a positive whole number\.$",
                        ):
                            mutate(invalid_id, *extra_arguments)
                        connect_mock.assert_not_called()
                        timestamp_mock.assert_not_called()

    def test_update_memory_rejects_invalid_facts_without_connecting(self) -> None:
        self.memory.initialize()
        memory_id, _ = self.memory.store_memory("Original fact")

        for new_fact in (None, 5, True, [], object(), "", "   "):
            with self.subTest(new_fact=new_fact):
                with patch.object(self.memory, "_connect") as connect_mock, patch(
                    "core.memory._utc_timestamp"
                ) as timestamp_mock:
                    with self.assertRaisesRegex(
                        ValueError,
                        r"^New memory fact is required\.$",
                    ):
                        self.memory.update_memory(memory_id, new_fact)
                    connect_mock.assert_not_called()
                    timestamp_mock.assert_not_called()

        self.assertEqual(self._stored_memory_row(memory_id)[0], "Original fact")

    def test_update_memory_binds_values_as_parameters_and_commits(self) -> None:
        """The fact and the consent reset share one connection and one commit."""
        self.memory.initialize()
        memory_id, _ = self.memory.store_memory("Original fact")

        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.return_value = mock_cursor

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            result = self.memory.update_memory(memory_id, "  My Edited Fact  ")

        self.assertEqual(result, (True, None))
        self.assertEqual(mock_connection.execute.call_count, 2)
        (query, params), (consent_query, consent_params) = (
            call.args for call in mock_connection.execute.call_args_list
        )
        self.assertIn("UPDATE memories", query)
        self.assertIn("SET fact = ?, normalized_fact = ?", query)
        self.assertIn("WHERE id = ?", query)
        self.assertNotIn("SELECT", query)
        self.assertNotIn("My Edited Fact", query)
        self.assertNotIn(str(memory_id), query)
        self.assertEqual(params, ("My Edited Fact", "my edited fact", memory_id))
        self.assertIn("SET context_enabled = 0", consent_query)
        self.assertIn("WHERE id = ? AND context_enabled = 1", consent_query)
        self.assertNotIn("SELECT", consent_query)
        self.assertEqual(consent_params, (memory_id,))
        mock_connection.commit.assert_called_once()
        mock_connection.rollback.assert_not_called()
        mock_connection.close.assert_called_once()

    def test_update_memory_missing_row_runs_only_the_update_statement(self) -> None:
        self.memory.initialize()

        mock_cursor = MagicMock()
        mock_cursor.rowcount = 0
        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.return_value = mock_cursor

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            result = self.memory.update_memory(7, "Replacement fact")

        self.assertEqual(result, (False, None))
        mock_connection.execute.assert_called_once()
        mock_connection.commit.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_update_memory_conflict_identifies_the_row_by_normalized_fact(
        self,
    ) -> None:
        self.memory.initialize()
        unique_exc = sqlite3.IntegrityError(
            "UNIQUE constraint failed: memories.normalized_fact"
        )
        unique_exc.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_UNIQUE
        lookup_cursor = MagicMock()
        lookup_cursor.fetchone.return_value = {"id": 9}
        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = [unique_exc, lookup_cursor]

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            result = self.memory.update_memory(4, "Duplicate Fact")

        self.assertEqual(result, (False, 9))
        lookup_query, lookup_params = mock_connection.execute.call_args.args
        self.assertIn("SELECT id", lookup_query)
        self.assertIn("WHERE normalized_fact = ?", lookup_query)
        self.assertEqual(lookup_params, ("duplicate fact",))
        mock_cursor_calls = mock_connection.execute.call_count
        self.assertEqual(mock_cursor_calls, 2)
        mock_connection.commit.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_update_memory_unresolvable_conflict_becomes_ordinary_failure(
        self,
    ) -> None:
        self.memory.initialize()
        unique_exc = sqlite3.IntegrityError(
            "UNIQUE constraint failed: memories.normalized_fact"
        )
        unique_exc.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_UNIQUE
        lookup_cursor = MagicMock()
        lookup_cursor.fetchone.return_value = None
        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = [unique_exc, lookup_cursor]

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to update memory\.$",
            ) as raised:
                self.memory.update_memory(4, "Duplicate Fact")

        self.assertNotIn(
            "Unable to resolve conflicting memory.", str(raised.exception)
        )
        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_update_memory_non_unique_constraint_is_not_a_conflict(self) -> None:
        self.memory.initialize()
        check_exc = sqlite3.IntegrityError("CHECK constraint failed")
        check_exc.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_CHECK

        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = check_exc

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to update memory\.$",
            ) as raised:
                self.memory.update_memory(1, "Replacement fact")

        self.assertIs(raised.exception.__cause__, check_exc)
        mock_connection.execute.assert_called_once()
        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_update_memory_ordinary_failure_rolls_back_and_closes(self) -> None:
        self.memory.initialize()
        io_error = sqlite3.OperationalError("disk I/O error")
        io_error.sqlite_errorcode = 10  # SQLITE_IOERR: unrelated to busy/locked.

        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = io_error

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to update memory\.$",
            ) as raised:
                self.memory.update_memory(1, "Replacement fact")

        self.assertIs(raised.exception.__cause__, io_error)
        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_update_memory_busy_error_becomes_fixed_message(self) -> None:
        self.memory.initialize()
        busy_exc = sqlite3.OperationalError("database is locked")
        busy_exc.sqlite_errorcode = sqlite3.SQLITE_BUSY

        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = busy_exc

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Database is busy\. Try again shortly\.$",
            ) as raised:
                self.memory.update_memory(1, "Replacement fact")

        self.assertIs(raised.exception.__cause__, busy_exc)
        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_forget_memory_removes_target_and_leaves_others(self) -> None:
        self.memory.initialize()
        keep_id, _ = self.memory.store_memory("Keep this fact")
        forget_id, _ = self.memory.store_memory("Forget this fact")

        self.assertTrue(self.memory.forget_memory(forget_id))

        self.assertIsNone(self.memory.get_memory(forget_id))
        self.assertIsNotNone(self.memory.get_memory(keep_id))

    def test_forget_memory_returns_false_for_missing_id(self) -> None:
        self.memory.initialize()
        memory_id, _ = self.memory.store_memory("Only fact")

        self.assertTrue(self.memory.forget_memory(memory_id))
        self.assertFalse(self.memory.forget_memory(memory_id))

    def test_forget_memory_does_not_affect_notes_or_interactions(self) -> None:
        self.memory.initialize()
        memory_id, _ = self.memory.store_memory("My editor is VS Code")
        note_id = self.memory.create_note("Editor", "VS Code")
        interaction_id = self.memory.record_interaction("system", "details")

        self.assertTrue(self.memory.forget_memory(memory_id))

        self.assertIsNotNone(self.memory.get_note(note_id=note_id))
        self.assertIsNotNone(self.memory.get_interaction(interaction_id))

    def test_forget_memory_does_not_renumber_or_reuse_ids(self) -> None:
        self.memory.initialize()
        first_id, _ = self.memory.store_memory("First fact")

        self.assertTrue(self.memory.forget_memory(first_id))
        second_id, _ = self.memory.store_memory("Second fact")

        self.assertGreater(second_id, first_id)
        self.assertNotEqual(second_id, first_id)

    def test_forget_memory_binds_id_as_a_parameter_and_commits(self) -> None:
        self.memory.initialize()
        memory_id, _ = self.memory.store_memory("Original fact")

        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.return_value = mock_cursor

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            forgotten = self.memory.forget_memory(memory_id)

        self.assertTrue(forgotten)
        mock_connection.execute.assert_called_once()
        query, params = mock_connection.execute.call_args.args
        self.assertIn("DELETE FROM memories", query)
        self.assertIn("WHERE id = ?", query)
        self.assertNotIn("SELECT", query)
        self.assertNotIn(str(memory_id), query)
        self.assertEqual(params, (memory_id,))
        mock_connection.commit.assert_called_once()
        mock_connection.rollback.assert_not_called()
        mock_connection.close.assert_called_once()

    def test_forget_memory_ordinary_failure_rolls_back_and_closes(self) -> None:
        self.memory.initialize()
        io_error = sqlite3.OperationalError("disk I/O error")
        io_error.sqlite_errorcode = 10  # SQLITE_IOERR: unrelated to busy/locked.

        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = io_error

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to forget memory\.$",
            ) as raised:
                self.memory.forget_memory(1)

        self.assertIs(raised.exception.__cause__, io_error)
        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_forget_memory_busy_error_becomes_fixed_message(self) -> None:
        self.memory.initialize()
        locked_exc = sqlite3.OperationalError("database is locked")
        locked_exc.sqlite_errorcode = sqlite3.SQLITE_LOCKED

        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = locked_exc

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Database is busy\. Try again shortly\.$",
            ) as raised:
                self.memory.forget_memory(1)

        self.assertIs(raised.exception.__cause__, locked_exc)
        mock_connection.rollback.assert_called_once()
        mock_connection.close.assert_called_once()

    def test_memory_mutation_logs_contain_no_fact_content(self) -> None:
        mock_logger = Mock(spec=logging.Logger)
        memory = Memory(self.database_path, logger=mock_logger)
        memory.initialize()
        memory_id, _ = memory.store_memory("secret stored fact")
        other_id, _ = memory.store_memory("secret other fact")
        mock_logger.reset_mock()

        updated = memory.update_memory(memory_id, "secret new fact")
        conflicted = memory.update_memory(memory_id, "SECRET OTHER FACT")
        forgotten = memory.forget_memory(memory_id)
        missing_update = memory.update_memory(memory_id, "secret missing fact")
        missing_forget = memory.forget_memory(memory_id)

        self.assertEqual(updated, (True, None))
        self.assertEqual(conflicted, (False, other_id))
        self.assertTrue(forgotten)
        self.assertEqual(missing_update, (False, None))
        self.assertFalse(missing_forget)
        for logger_call in mock_logger.method_calls:
            call_text = " ".join(str(arg) for arg in logger_call.args)
            self.assertNotIn("secret stored fact", call_text)
            self.assertNotIn("secret other fact", call_text)
            self.assertNotIn("secret new fact", call_text)
            self.assertNotIn("secret missing fact", call_text)


class MemoryContextConsentTests(unittest.TestCase):
    """Exercise consent bound to exact text and the bounded Tier A read."""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "database" / "maria.db"
        self.memory = Memory(self.database_path, logger=TEST_LOGGER)
        self.memory.initialize()

    def _flags(self) -> list[tuple[int, int]]:
        """Read every memory's consent flag straight from the temporary database."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            return [
                (int(row[0]), int(row[1]))
                for row in connection.execute(
                    "SELECT id, context_enabled FROM memories ORDER BY id"
                )
            ]

    def _store(self, *facts: str) -> list[int]:
        return [self.memory.store_memory(fact)[0] for fact in facts]

    def _enable(self, memory_id: int) -> ContextConsentOutcome:
        """Enable one memory for the text it holds now."""
        return self.memory.enable_memory_context(
            memory_id,
            self.memory.memory_fact_digest(memory_id),
        )

    def test_new_memories_start_with_consent_off(self) -> None:
        self._store("first fact", "second fact")

        self.assertEqual(self._flags(), [(1, 0), (2, 0)])
        self.assertEqual(
            self.memory.list_context_memories(limit=100, max_fact_bytes=2048),
            [],
        )

    def test_the_identity_is_the_digest_of_the_exact_text(self) -> None:
        (memory_id,) = self._store("Straße im Süden")

        self.assertEqual(
            self.memory.memory_fact_digest(memory_id),
            hashlib.sha256("Straße im Süden".encode("utf-8")).hexdigest(),
        )
        self.assertIsNone(self.memory.memory_fact_digest(99))

    def test_enabling_the_approved_text_changes_exactly_one_row(self) -> None:
        self._store("first fact", "second fact", "third fact")

        self.assertIs(self._enable(2), ContextConsentOutcome.ENABLED)
        self.assertEqual(self._flags(), [(1, 0), (2, 1), (3, 0)])
        self.assertIs(self._enable(2), ContextConsentOutcome.ENABLED)
        self.assertEqual(self._flags(), [(1, 0), (2, 1), (3, 0)])
        self.assertTrue(self.memory.disable_memory_context(2))
        self.assertEqual(self._flags(), [(1, 0), (2, 0), (3, 0)])

    def test_changed_text_is_refused_and_left_off(self) -> None:
        (memory_id,) = self._store("synthetic approved text")
        approved = self.memory.memory_fact_digest(memory_id)

        for label, new_text in (
            ("different text", "synthetic replacement text"),
            ("case only", "SYNTHETIC APPROVED TEXT"),
        ):
            with self.subTest(change=label):
                self.memory.update_memory(memory_id, new_text)

                self.assertIs(
                    self.memory.enable_memory_context(memory_id, approved),
                    ContextConsentOutcome.TARGET_CHANGED,
                )
                self.assertEqual(self._flags(), [(memory_id, 0)])

    def test_text_changed_away_and_back_is_the_approved_text(self) -> None:
        """The guarantee is exact text, not an unchanged edit history."""
        (memory_id,) = self._store("synthetic approved text")
        approved = self.memory.memory_fact_digest(memory_id)
        self.memory.update_memory(memory_id, "synthetic interim text")
        self.memory.update_memory(memory_id, "synthetic approved text")

        self.assertIs(
            self.memory.enable_memory_context(memory_id, approved),
            ContextConsentOutcome.ENABLED,
        )

    def test_an_enable_without_an_identity_never_enables_an_existing_memory(
        self,
    ) -> None:
        (memory_id,) = self._store("synthetic fact")

        self.assertIs(
            self.memory.enable_memory_context(memory_id, None),
            ContextConsentOutcome.TARGET_CHANGED,
        )
        self.assertIs(
            self.memory.enable_memory_context(99, None),
            ContextConsentOutcome.NOT_FOUND,
        )
        self.assertEqual(self._flags(), [(memory_id, 0)])

    def test_a_missing_id_matches_nothing_and_changes_nothing(self) -> None:
        self._store("only fact")

        self.assertIs(
            self.memory.enable_memory_context(99, "a" * 64),
            ContextConsentOutcome.NOT_FOUND,
        )
        self.assertFalse(self.memory.disable_memory_context(99))
        self.assertEqual(self._flags(), [(1, 0)])

    def test_invalid_arguments_are_refused_without_connecting(self) -> None:
        good = "a" * 64
        calls = [
            *(
                (method, (invalid_id, *extra))
                for invalid_id in (0, -1, "1", 1.0, True, False, None)
                for method, extra in (
                    ("memory_fact_digest", ()),
                    ("enable_memory_context", (good,)),
                    ("disable_memory_context", ()),
                )
            ),
            *(
                ("enable_memory_context", (1, digest))
                for digest in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, b"a" * 64, 7)
            ),
            *(
                ("list_context_memories", (17, bound))
                for bound in (0, -1, True, "2048", None, 2048.0)
            ),
        ]
        for method, arguments in calls:
            with self.subTest(method=method, arguments=repr(arguments)[:40]):
                with patch.object(self.memory, "_connect") as connect_mock:
                    with self.assertRaises(ValueError):
                        getattr(self.memory, method)(*arguments)
                    connect_mock.assert_not_called()

    def test_the_enable_is_one_immediate_transaction_bound_to_the_text(self) -> None:
        digest = hashlib.sha256(b"synthetic fact").hexdigest()
        for rowcount, expected in (
            (1, ContextConsentOutcome.ENABLED),
            (0, ContextConsentOutcome.TARGET_CHANGED),
        ):
            with self.subTest(rowcount=rowcount):
                select_cursor = MagicMock()
                select_cursor.fetchone.return_value = {"fact": "synthetic fact"}
                update_cursor = MagicMock()
                update_cursor.rowcount = rowcount
                mock_connection = MagicMock(spec=sqlite3.Connection)
                mock_connection.execute.side_effect = [
                    MagicMock(),
                    select_cursor,
                    update_cursor,
                ]

                with patch.object(
                    self.memory,
                    "_connect",
                    return_value=mock_connection,
                ):
                    outcome = self.memory.enable_memory_context(3, digest)

                self.assertIs(outcome, expected)
                begin, select, update = (
                    call.args for call in mock_connection.execute.call_args_list
                )
                self.assertEqual(begin, ("BEGIN IMMEDIATE",))
                self.assertIn("SELECT fact", select[0])
                self.assertIn("SET context_enabled = 1", update[0])
                self.assertIn("WHERE id = ? AND fact = ?", update[0])
                self.assertEqual(update[1], (3, "synthetic fact"))
                mock_connection.commit.assert_called_once()
                mock_connection.close.assert_called_once()

    def test_the_read_returns_only_enabled_memories_by_ascending_id(self) -> None:
        identifiers = self._store(*(f"fact {index}" for index in range(1, 21)))
        for memory_id in reversed(identifiers[2:]):
            self._enable(memory_id)

        rows = self.memory.list_context_memories(limit=17, max_fact_bytes=2048)

        self.assertEqual([row["id"] for row in rows], list(range(3, 20)))
        self.assertEqual(rows[0], {"id": 3, "fact": "fact 3", "oversized": False})
        self.assertEqual(
            len(self.memory.list_context_memories(limit=0, max_fact_bytes=2048)),
            1,
        )
        self.assertEqual(
            len(self.memory.list_context_memories(limit=500, max_fact_bytes=2048)),
            18,
        )

    def test_an_oversized_fact_never_leaves_sqlite(self) -> None:
        """In a UTF-8 database the size test is exact and runs in SQLite."""
        facts = (
            "B" * (3 * 1024 * 1024),
            "x" * 2048,
            "x" * 2049,
            "é" * 1024,
            "é" * 1025,
            "small fact",
        )
        for memory_id in self._store(*facts):
            self._enable(memory_id)

        rows = self.memory.list_context_memories(limit=17, max_fact_bytes=2048)

        self.assertEqual(
            [(row["id"], row["oversized"]) for row in rows],
            [(1, True), (2, False), (3, True), (4, False), (5, True), (6, False)],
        )
        for row in rows:
            with self.subTest(memory_id=row["id"]):
                if row["oversized"]:
                    self.assertIsNone(row["fact"])
                else:
                    self.assertEqual(row["fact"], facts[row["id"] - 1])
                    self.assertLessEqual(len(row["fact"].encode("utf-8")), 2048)

    def test_a_utf16_database_is_accepted_and_the_read_bound_stays_utf8(
        self,
    ) -> None:
        """SQLite measures stored bytes; the bound is on UTF-8 bytes.

        A v0.2.0 database whose file encoding is UTF-16 matches Maria's
        schema, so initialization accepts and migrates it and it keeps its
        encoding. There a fact stores in as little as half, or as much as
        twice, its UTF-8 size, so the read must still settle each fact by
        its exact UTF-8 size.
        """
        facts = (
            ("\u0800" * 1000, True),  # 3,000 UTF-8 bytes; 2,000 stored.
            ("a" * 1500, False),  # 1,500 UTF-8 bytes; 3,000 stored.
            ("é" * 1024, False),
            ("é" * 1025, True),
            ("\U0001F600" * 512, False),
            ("\U0001F600" * 513, True),
            ("x" * 2048, False),  # Stored in exactly twice the bound.
            ("x" * 2049, True),
            ("B" * (3 * 1024 * 1024), True),
        )
        for encoding in ("UTF-16le", "UTF-16be"):
            with self.subTest(encoding=encoding):
                path = Path(self.temp_dir.name) / encoding / "maria.db"
                path.parent.mkdir()
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute(f"PRAGMA encoding = '{encoding}'")
                    for statement in V0_2_0_BASELINE_SCHEMA:
                        connection.execute(statement)
                    connection.commit()
                memory = Memory(path, logger=TEST_LOGGER)

                memory.initialize()
                memory.initialize()

                self.assertEqual(memory.schema_version(), 3)
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual(
                        connection.execute("PRAGMA encoding").fetchone()[0],
                        encoding,
                    )
                for fact, _ in facts:
                    memory_id, _ = memory.store_memory(fact)
                    memory.enable_memory_context(
                        memory_id,
                        memory.memory_fact_digest(memory_id),
                    )

                rows = memory.list_context_memories(limit=17, max_fact_bytes=2048)

                self.assertEqual(
                    [row["oversized"] for row in rows],
                    [oversized for _, oversized in facts],
                )
                for row, (fact, oversized) in zip(rows, facts):
                    self.assertEqual(row["fact"], None if oversized else fact)

                # Only facts stored within twice the bound ever reach Python,
                # so no more than 2,048 characters of any one of them do.
                raw_sizes: list[int | None] = []
                original = memory_module._bounded_context_row

                def measuring(row, max_fact_bytes):
                    raw = row["fact"]
                    raw_sizes.append(None if raw is None else len(raw))
                    return original(row, max_fact_bytes)

                with patch.object(memory_module, "_bounded_context_row", measuring):
                    memory.list_context_memories(limit=17, max_fact_bytes=2048)
                self.assertIsNone(raw_sizes[-1])
                self.assertLessEqual(
                    max(size for size in raw_sizes if size is not None),
                    2048,
                )

    def test_existing_read_shapes_are_unchanged_for_an_enabled_memory(self) -> None:
        (memory_id,) = self._store("My editor is VS Code")
        self._enable(memory_id)

        for row in (
            self.memory.get_memory(memory_id),
            *self.memory.list_memories(),
            *self.memory.search_memories("editor"),
        ):
            self.assertEqual(set(row), {"id", "fact", "created_at"})

    def test_update_memory_keeps_its_released_two_value_result(self) -> None:
        first, second = self._store("first fact", "second fact")
        self._enable(first)

        for new_fact, expected in (
            ("first fact, revised", (True, None)),
            ("SECOND FACT", (False, second)),
        ):
            with self.subTest(new_fact=new_fact):
                result = self.memory.update_memory(first, new_fact)
                self.assertIs(type(result), tuple)
                updated, conflicting_id = result
                self.assertEqual((updated, conflicting_id), expected)
        self.assertEqual(self.memory.update_memory(99, "missing"), (False, None))
        self.assertEqual(self._flags(), [(1, 0), (2, 0)])

    def test_an_update_turns_consent_off_and_reports_it_only_when_it_was_on(
        self,
    ) -> None:
        first, second = self._store("first fact", "second fact")
        self._enable(first)
        self._enable(second)

        self.assertEqual(
            self.memory.update_memory_reporting_consent(first, "first, revised"),
            MemoryUpdate(True, None, True),
        )
        self.assertEqual(self._flags(), [(1, 0), (2, 1)])
        self.assertEqual(
            self.memory.update_memory_reporting_consent(first, "first, again"),
            MemoryUpdate(True, None, False),
        )

    def test_a_conflicting_update_leaves_consent_untouched(self) -> None:
        first, second = self._store("first fact", "second fact")
        self._enable(first)

        self.assertEqual(
            self.memory.update_memory_reporting_consent(first, "SECOND FACT"),
            MemoryUpdate(False, second, False),
        )
        self.assertEqual(self._flags(), [(1, 1), (2, 0)])

    def test_a_failed_consent_reset_rolls_the_new_fact_back(self) -> None:
        io_error = sqlite3.OperationalError("disk I/O error")
        io_error.sqlite_errorcode = 10
        updated_cursor = MagicMock()
        updated_cursor.rowcount = 1
        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = [updated_cursor, io_error]

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(RuntimeError, r"^Unable to update memory\.$"):
                self.memory.update_memory(1, "Replacement fact")

        mock_connection.rollback.assert_called_once()
        mock_connection.commit.assert_not_called()
        mock_connection.close.assert_called_once()

    def test_consent_operations_log_ids_and_never_facts(self) -> None:
        mock_logger = Mock(spec=logging.Logger)
        memory = Memory(self.database_path, logger=mock_logger)
        memory_id, _ = memory.store_memory("secret consent fact")
        mock_logger.reset_mock()

        stale = memory.memory_fact_digest(memory_id)
        memory.enable_memory_context(memory_id, stale)
        memory.list_context_memories(limit=17, max_fact_bytes=2048)
        memory.update_memory(memory_id, "secret revised fact")
        memory.enable_memory_context(memory_id, stale)
        memory.disable_memory_context(memory_id)

        logged = [
            logger_call.args[0] % logger_call.args[1:]
            for logger_call in mock_logger.method_calls
        ]
        for line in (
            "Enabled context consent for memory 1.",
            "Turned off context consent for updated memory 1.",
            "Refused context consent for memory 1: approved text changed.",
            "Disabled context consent for memory 1.",
        ):
            self.assertIn(line, logged)
        for line in logged:
            self.assertNotIn("secret consent fact", line)
            self.assertNotIn("secret revised fact", line)
            self.assertNotIn(stale, line)

    def test_a_failed_read_raises_a_fixed_message(self) -> None:
        io_error = sqlite3.OperationalError("disk I/O error secret consent fact")
        io_error.sqlite_errorcode = 10
        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.side_effect = io_error

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to read memory context\.$",
            ):
                self.memory.list_context_memories(limit=17, max_fact_bytes=2048)

    def _insert_enabled_blob(self, blob: bytes) -> int:
        """Store a non-text fact directly, as SQLite allows in a TEXT column."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            cursor = connection.execute(
                """
                INSERT INTO memories
                    (fact, normalized_fact, created_at, context_enabled)
                VALUES (?, ?, ?, 1)
                """,
                (blob, f"blob-{len(blob)}", "2026-09-27T00:00:00+00:00"),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def test_a_fact_that_is_not_text_makes_the_read_untrustworthy(self) -> None:
        """A nonempty BLOB fits the TEXT column's shape but is not a fact."""
        blob = b"synthetic-blob-fact-3d9a"
        (text_id,) = self._store("synthetic text fact")
        self._enable(text_id)
        self._insert_enabled_blob(blob)
        mock_logger = Mock(spec=logging.Logger)

        with self.assertRaisesRegex(
            RuntimeError,
            r"^Unable to read memory context\.$",
        ) as raised:
            Memory(self.database_path, logger=mock_logger).list_context_memories(
                limit=17,
                max_fact_bytes=2048,
            )

        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("synthetic-blob", repr(raised.exception.args))
        mock_logger.error.assert_called_once_with(
            "Refused a memory context read: a fact is not text."
        )
        for logger_call in mock_logger.method_calls:
            self.assertNotIn(
                "synthetic-blob",
                " ".join(str(arg) for arg in logger_call.args),
            )

    def test_an_oversized_non_text_fact_is_only_ever_metadata(self) -> None:
        """A BLOB beyond the byte bound never reaches Python to be checked."""
        blob_id = self._insert_enabled_blob(b"B" * 4096)

        self.assertEqual(
            self.memory.list_context_memories(limit=17, max_fact_bytes=2048),
            [{"id": blob_id, "fact": None, "oversized": True}],
        )


class UniqueViolationClassificationTests(unittest.TestCase):
    """Exercise _is_unique_violation's exact extended-result-code matching."""

    def test_classifies_extended_unique_constraint_code(self) -> None:
        exc = sqlite3.IntegrityError("synthetic unique violation")
        exc.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_UNIQUE

        self.assertTrue(memory_module._is_unique_violation(exc))

    def test_rejects_primary_constraint_code(self) -> None:
        exc = sqlite3.IntegrityError("synthetic constraint violation")
        exc.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT

        self.assertFalse(memory_module._is_unique_violation(exc))

    def test_rejects_check_constraint_code(self) -> None:
        exc = sqlite3.IntegrityError("synthetic check violation")
        exc.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_CHECK

        self.assertFalse(memory_module._is_unique_violation(exc))

    def test_rejects_primary_key_constraint_code(self) -> None:
        exc = sqlite3.IntegrityError("synthetic primary key violation")
        exc.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY

        self.assertFalse(memory_module._is_unique_violation(exc))

    def test_rejects_missing_sqlite_errorcode(self) -> None:
        self.assertFalse(
            memory_module._is_unique_violation(sqlite3.Error("no error code"))
        )

    def test_rejects_none_error_code(self) -> None:
        exc = sqlite3.IntegrityError("synthetic")
        exc.sqlite_errorcode = None

        self.assertFalse(memory_module._is_unique_violation(exc))

    def test_rejects_non_integer_error_code(self) -> None:
        exc = sqlite3.IntegrityError("synthetic")
        exc.sqlite_errorcode = str(sqlite3.SQLITE_CONSTRAINT_UNIQUE)

        self.assertFalse(memory_module._is_unique_violation(exc))

    def test_rejects_boolean_error_code(self) -> None:
        exc = sqlite3.IntegrityError("synthetic")
        exc.sqlite_errorcode = True

        self.assertFalse(memory_module._is_unique_violation(exc))

    def test_does_not_mask_extended_code_to_its_primary_byte(self) -> None:
        exc = sqlite3.IntegrityError("synthetic")
        exc.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_UNIQUE & 0xFF

        self.assertFalse(memory_module._is_unique_violation(exc))

    def test_ignores_exception_text(self) -> None:
        exc = sqlite3.IntegrityError(
            "UNIQUE constraint failed: memories.normalized_fact"
        )
        exc.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_CHECK

        self.assertFalse(memory_module._is_unique_violation(exc))


class BusyLockedClassificationTests(unittest.TestCase):
    """Exercise _is_busy_or_locked's numeric primary-result-code matching."""

    def test_classifies_primary_busy_code(self) -> None:
        exc = sqlite3.OperationalError("synthetic busy")
        exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
        self.assertTrue(memory_module._is_busy_or_locked(exc))

    def test_classifies_primary_locked_code(self) -> None:
        exc = sqlite3.OperationalError("synthetic locked")
        exc.sqlite_errorcode = sqlite3.SQLITE_LOCKED
        self.assertTrue(memory_module._is_busy_or_locked(exc))

    def test_classifies_extended_busy_code_by_primary_low_byte(self) -> None:
        exc = sqlite3.OperationalError("synthetic extended busy")
        exc.sqlite_errorcode = sqlite3.SQLITE_BUSY | (1 << 8)
        self.assertTrue(memory_module._is_busy_or_locked(exc))

    def test_classifies_extended_locked_code_by_primary_low_byte(self) -> None:
        exc = sqlite3.OperationalError("synthetic extended locked")
        exc.sqlite_errorcode = sqlite3.SQLITE_LOCKED | (1 << 8)
        self.assertTrue(memory_module._is_busy_or_locked(exc))

    def test_rejects_non_busy_operational_error_code(self) -> None:
        exc = sqlite3.OperationalError("synthetic disk I/O error")
        exc.sqlite_errorcode = 10  # SQLITE_IOERR: unrelated to busy/locked.
        self.assertFalse(memory_module._is_busy_or_locked(exc))

    def test_rejects_missing_sqlite_errorcode(self) -> None:
        exc = sqlite3.OperationalError("synthetic, no error code attribute")
        self.assertFalse(memory_module._is_busy_or_locked(exc))

    def test_rejects_none_error_code(self) -> None:
        exc = sqlite3.OperationalError("synthetic, None error code")
        exc.sqlite_errorcode = None
        self.assertFalse(memory_module._is_busy_or_locked(exc))

    def test_rejects_non_integer_error_code(self) -> None:
        exc = sqlite3.OperationalError("synthetic, non-integer error code")
        exc.sqlite_errorcode = "SQLITE_BUSY"
        self.assertFalse(memory_module._is_busy_or_locked(exc))


class SqlOwnershipBoundaryTests(unittest.TestCase):
    """Prove the memory boundary alone reaches the database.

    This one guard replaces the hand-written per-module checks. It finds the
    production modules itself, so a module added at any depth beneath a
    production package is covered without anyone writing a test for it, and a
    new top-level production package is refused until it is inventoried.
    Every module is parsed, never imported or run.
    """

    def _modules_outside_the_boundary(self) -> tuple[Path, ...]:
        """Return every production module outside the memory boundary."""
        modules = modules_outside_memory_boundary()
        # A scan that reached nothing would pass by reaching nothing.
        self.assertGreater(len(modules), 0)
        return modules

    def test_every_production_location_is_discovered_recursively(self) -> None:
        modules = production_modules()
        discovered = {_relative(module) for module in modules}

        for sentinel in (
            "main.py",
            "core/brain.py",
            "core/tool_manager.py",
            "tools/history.py",
            "tools/memory.py",
            "tools/notes.py",
        ):
            with self.subTest(sentinel=sentinel):
                self.assertIn(sentinel, discovered)
        self.assertEqual(
            [
                module
                for module in discovered
                if not module.endswith(".py")
                or module.startswith(f"{TEST_PACKAGE}/")
            ],
            [],
        )

        # A second, independent walk has to find exactly the same modules, so
        # nothing beneath a production package is missed at any depth.
        walked = {MARIA_ROOT / name for name in PRODUCTION_ENTRY_POINTS}
        for package in PRODUCTION_PACKAGES:
            walked.update(
                module
                for module in (MARIA_ROOT / package).rglob("*.py")
                if not any(
                    part == "__pycache__" or part.startswith(".")
                    for part in module.relative_to(MARIA_ROOT).parts
                )
            )
        self.assertEqual(set(modules), walked)

        # Exactly one boundary shape exists, and the scan sees it.
        shapes = memory_boundary_shapes()
        self.assertEqual(len(shapes), 1)
        self.assertIn(shapes[0], modules)

    def test_no_unscanned_importable_production_root_exists(self) -> None:
        self.assertEqual(
            importable_top_level_roots() - {TEST_PACKAGE},
            frozenset(PRODUCTION_PACKAGES + PRODUCTION_ENTRY_POINTS),
        )

    def test_only_the_memory_boundary_imports_a_database_driver(self) -> None:
        for module in self._modules_outside_the_boundary():
            with self.subTest(module=_relative(module)):
                self.assertEqual(
                    database_driver_imports(_parse(module), _package_of(module)),
                    [],
                )

    def test_only_the_memory_boundary_contains_executable_sql(self) -> None:
        for module in self._modules_outside_the_boundary():
            with self.subTest(module=_relative(module)):
                self.assertEqual(executable_sql(_parse(module)), [])

    def test_modules_outside_the_boundary_use_only_the_memory_facade(self) -> None:
        for module in self._modules_outside_the_boundary():
            with self.subTest(module=_relative(module)):
                self.assertEqual(
                    memory_facade_bypasses(_parse(module), _package_of(module)),
                    [],
                )

    def test_the_boundary_owns_the_driver_the_sql_and_the_schema_marker(
        self,
    ) -> None:
        """Positive controls, so the checks above cannot pass by seeing nothing."""
        boundary = [
            module
            for module in production_modules()
            if inside_memory_boundary(module)
        ]
        self.assertGreater(len(boundary), 0)
        drivers = [
            driver
            for module in boundary
            for driver in database_driver_imports(
                _parse(module),
                _package_of(module),
            )
        ]
        statements = [
            statement
            for module in boundary
            for statement in executable_sql(_parse(module))
        ]

        self.assertIn("sqlite3", drivers)
        self.assertGreater(len(statements), 0)
        self.assertTrue(
            any("PRAGMA user_version" in statement for statement in statements)
        )
        self.assertTrue(callable(getattr(Memory, "schema_version", None)))

    def test_the_detectors_recognise_every_approved_spelling(self) -> None:
        """Prove each detector on synthetic sources and a synthetic tree.

        Nothing is written into the repository: sources are parsed from
        memory, and the fixture tree lives in a temporary directory.
        """
        core = ("core",)

        for source, expected in (
            ("import sqlite3", ["sqlite3"]),
            ("import json, sqlite3", ["sqlite3"]),
            ("import sqlite3 as database", ["sqlite3"]),
            ("from sqlite3 import connect", ["sqlite3"]),
            ("import dbm", ["dbm"]),
            ("import dbm.sqlite3", ["dbm.sqlite3"]),
            ("from dbm import sqlite3", ["dbm"]),
            ("import shelve", ["shelve"]),
            ("from shelve import open as open_shelf", ["shelve"]),
            ('__import__("sqlite3")', ["sqlite3"]),
            ('importlib.import_module("sqlite3")', ["sqlite3"]),
            ('importlib.import_module("dbm")', ["dbm"]),
            ('import_module("dbm.sqlite3")', ["dbm.sqlite3"]),
            ('__import__("shelve")', ["shelve"]),
            ("import sqlite3_helpers", []),
            ("from sqlite3_helpers import connect", []),
            ('"""Why nothing here imports sqlite3."""\n# import sqlite3', []),
            ("import _sqlite3", ["_sqlite3"]),
            ("from _sqlite3 import connect", ["_sqlite3"]),
            ('importlib.import_module("_sqlite3")', ["_sqlite3"]),
            ("import _sqlite3_helpers", []),
            ('import importlib\nimportlib.import_module("sqlite3")', ["sqlite3"]),
            (
                'import importlib\nimportlib.import_module(name="sqlite3")',
                ["sqlite3"],
            ),
            ('import importlib as il\nil.import_module("sqlite3")', ["sqlite3"]),
            (
                'from importlib import import_module\nimport_module("sqlite3")',
                ["sqlite3"],
            ),
            (
                'from importlib import import_module as load\nload("sqlite3")',
                ["sqlite3"],
            ),
            (
                'from importlib import import_module as load\nload(name="_sqlite3")',
                ["_sqlite3"],
            ),
            (
                'from importlib import import_module as load\nload("dbm")',
                ["dbm"],
            ),
            (
                'from importlib import import_module as load\nload("shelve")',
                ["shelve"],
            ),
            ('__import__(name="sqlite3")', ["sqlite3"]),
            ('import builtins\nbuiltins.__import__("shelve")', ["shelve"]),
            ('from builtins import __import__ as fetch\nfetch("dbm")', ["dbm"]),
            (
                'import importlib\nload = importlib.import_module\nload("sqlite3")',
                ["sqlite3"],
            ),
            ('importlib.import_module("sql" + "ite3")', ["sqlite3"]),
            ('load("sqlite3")', []),
            ('from json import load\nload("sqlite3")', []),
            ("importlib.import_module(module_name)", []),
        ):
            with self.subTest(driver_source=source):
                self.assertEqual(
                    database_driver_imports(ast.parse(source), core),
                    expected,
                )

        for source in (
            'QUERY = "SELECT id FROM notes"',
            'QUERY = """\n    SELECT id, title\n    FROM notes\n    WHERE id = ?\n"""',
            'QUERY = f"SELECT id FROM {table}"',
            'QUERY = "DELETE FROM " + table',
            'QUERY = "INSERT INTO notes (title) " + "VALUES (?)"',
            'QUERY = ("UPDATE notes " "SET title = ? WHERE id = ?")',
            'connection.execute("delete from notes where id = ?")',
            'QUERY = "REPLACE INTO notes (id) VALUES (?)"',
            'QUERY = "CREATE TABLE IF NOT EXISTS probe (id INTEGER)"',
            'QUERY = "CREATE UNIQUE INDEX idx_probe ON probe (id)"',
            'QUERY = "DROP TABLE probe"',
            'QUERY = "ALTER TABLE probe ADD COLUMN extra TEXT"',
            'QUERY = "PRAGMA user_version"',
            'QUERY = f"PRAGMA user_version = {version:d}"',
            'QUERY = "BEGIN IMMEDIATE"',
            'QUERY = "COMMIT"',
            'QUERY = "ROLLBACK"',
            'QUERY = "ROLLBACK TO SAVEPOINT step"',
            'QUERY = "VACUUM"',
            'QUERY = "VACUUM INTO ?"',
            'QUERY = "ATTACH DATABASE ? AS backup"',
            'QUERY = "DETACH DATABASE backup"',
            'QUERY = "SELECT name FROM sqlite_master"',
        ):
            with self.subTest(sql_source=source):
                self.assertNotEqual(executable_sql(ast.parse(source)), [])

        # Handed to an execution method, a short statement is SQL in any case.
        for statement in (
            "commit",
            "COMMIT",
            "Commit",
            "rollback",
            "ROLLBACK",
            "RollBack",
            "begin",
            "BEGIN",
            "Begin",
            "begin immediate transaction",
            "end",
            "End Transaction",
            "vacuum",
            "savepoint step",
            "release step",
            "release savepoint step",
            "rollback to step",
            "detach backup",
            "analyze",
        ):
            for method in sorted(SQL_EXECUTION_METHODS):
                source = f'connection.{method}("{statement}")'
                with self.subTest(executed_source=source):
                    self.assertEqual(
                        executable_sql(ast.parse(source)),
                        [statement],
                    )
        for source, expected in (
            ('connection.executescript("begin; commit;")', ["begin; commit;"]),
            ('connection.execute("com" "mit")', ["commit"]),
            ('connection.execute("com" + "mit")', ["commit"]),
            ('connection.execute(f"rollback")', ["rollback"]),
        ):
            with self.subTest(executed_source=source):
                self.assertEqual(executable_sql(ast.parse(source)), expected)

        for source in (
            '"""SELECT id FROM notes, in a module docstring."""',
            'class Probe:\n    """DELETE FROM notes WHERE id = ?, in a class."""',
            'def probe():\n    """UPDATE notes SET title = ?, in a function."""',
            'async def probe():\n    """PRAGMA user_version, in a coroutine."""',
            "# SELECT id FROM notes WHERE id = ?\nvalue = 1",
            'MESSAGE = "Commit your changes before continuing."',
            'MESSAGE = "Commit"',
            'MESSAGE = "Update complete."',
            'MESSAGE = "Update the set of roots."',
            'MESSAGE = "Deleted note 3."',
            'MESSAGE = "Delete from the list."',
            'MESSAGE = "Begin by typing help."',
            'MESSAGE = "Rollback is not available."',
            'MESSAGE = "Select one option."',
            'MESSAGE = "Create a note first."',
            'MESSAGE = "Drop the table from the plan."',
            'MESSAGE = "Alter nothing."',
            'MESSAGE = "Vacuum the floor."',
            'MESSAGE = "Attach a file to the note."',
            'MESSAGE = "Detach the reminder."',
            'MESSAGE = "Pragma notes apply."',
            'MESSAGE = f"Updated memory {memory_id}."',
            'MESSAGE = "commit"',
            'MESSAGE = "Rollback"',
            'print("begin")',
            'logger.info("Vacuum")',
            'tool.execute("list", {})',
            'tool.execute("delete", {"id": 1})',
            "connection.execute(statement)",
            'connection.execute(f"{statement}")',
        ):
            with self.subTest(not_sql_source=source):
                self.assertEqual(executable_sql(ast.parse(source)), [])

        for source, expected in (
            ("import core.memory", ["core.memory"]),
            ("import core.memory as memory", ["core.memory"]),
            ("import core.memory.schema", ["core.memory.schema"]),
            ("from core import memory", ["core.memory"]),
            ("from core.memory import *", ["core.memory.*"]),
            (
                "from core.memory import _utc_timestamp",
                ["core.memory._utc_timestamp"],
            ),
            (
                "from core.memory import Memory, _connect",
                ["core.memory._connect"],
            ),
            ("from core.memory.schema import MIGRATIONS", ["core.memory.schema"]),
            ("from .memory import _utc_timestamp", ["core.memory._utc_timestamp"]),
            ("from . import memory", ["core.memory"]),
            ('importlib.import_module("core.memory")', ["core.memory"]),
            ("from core.memory import Memory", []),
            ("from core.memory import Memory as Store", []),
            ("import tools.memory", []),
            ("from tools.memory import MemoryTool", []),
            ("from core import brain", []),
            ("from core import Memory", ["core.Memory"]),
            ("from core import *", ["core.*"]),
            ("from . import Memory", ["core.Memory"]),
            (
                "import core\nvalue = core.memory._utc_timestamp",
                ["core.memory._utc_timestamp"],
            ),
            ("import core\nstore = core.memory.Memory", ["core.memory.Memory"]),
            ("import core\nmodule = core.memory", ["core.memory"]),
            (
                "import core.brain\nvalue = core.memory._MIGRATIONS",
                ["core.memory._MIGRATIONS"],
            ),
            (
                "import core as maria_core\nvalue = maria_core.memory._MIGRATIONS",
                ["core.memory._MIGRATIONS"],
            ),
            (
                'import core\nvalue = getattr(core, "memory")._connect',
                ["core.memory._connect"],
            ),
            ('__import__(name="core.memory")', ["core.memory"]),
            (
                'from importlib import import_module as load\nload("core.memory")',
                ["core.memory"],
            ),
            ("import core", []),
            ("import core\nvalue = core.brain", []),
            ('self.memory.store_memory("A fact")', []),
            ("value = memory._utc_timestamp", []),
        ):
            with self.subTest(facade_source=source):
                self.assertEqual(
                    memory_facade_bypasses(ast.parse(source), core),
                    expected,
                )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "main.py", "")
            _write(root / "core" / "__init__.py", "")
            _write(root / "core" / "memory.py", "import sqlite3\n")
            _write(root / "core" / "nested" / "__init__.py", "")
            nested = root / "core" / "nested" / "deeper" / "probe.py"
            _write(nested, "import sqlite3\n")
            _write(root / "tools" / "__init__.py", "")
            _write(root / "tests" / "test_probe.py", "import sqlite3\n")
            _write(root / "core" / "__pycache__" / "stale.py", "import sqlite3\n")
            _write(root / "tools" / "venv" / "pyvenv.cfg", "")
            _write(root / "tools" / "venv" / "Lib" / "site.py", "import sqlite3\n")
            _write(root / ".venv" / "Lib" / "site.py", "import sqlite3\n")
            _write(root / "docs" / "notes.md", "SELECT id FROM notes\n")

            # Nested production subpackages are scanned; tests, caches, and
            # local environments are not.
            self.assertEqual(
                {_relative(module, root) for module in production_modules(root)},
                {
                    "main.py",
                    "core/__init__.py",
                    "core/memory.py",
                    "core/nested/__init__.py",
                    "core/nested/deeper/probe.py",
                    "tools/__init__.py",
                },
            )
            self.assertFalse(inside_memory_boundary(nested, root))
            self.assertEqual(
                database_driver_imports(_parse(nested), _package_of(nested, root)),
                ["sqlite3"],
            )

            # A new top-level package, regular or namespace, is an unscanned
            # production root until it is inventoried.
            self.assertEqual(
                importable_top_level_roots(root) - {TEST_PACKAGE},
                {"core", "tools", "main.py"},
            )
            _write(root / "gui" / "__init__.py", "")
            _write(root / "reasoning" / "adapter.py", "")
            self.assertEqual(
                importable_top_level_roots(root) - {TEST_PACKAGE},
                {"core", "tools", "main.py", "gui", "reasoning"},
            )

            # Either boundary shape is recognised, and having both is visible.
            package = root / "core" / "memory"
            self.assertEqual(memory_boundary_shapes(root), (root / "core" / "memory.py",))
            self.assertTrue(inside_memory_boundary(root / "core" / "memory.py", root))
            self.assertTrue(inside_memory_boundary(package / "__init__.py", root))
            self.assertTrue(inside_memory_boundary(package / "schema" / "frozen.py", root))
            self.assertFalse(
                inside_memory_boundary(root / "core" / "memory_helpers.py", root)
            )
            _write(package / "__init__.py", "")
            self.assertEqual(len(memory_boundary_shapes(root)), 2)

        # The combined regressions: one outside module reaches SQLite only
        # through an aliased dynamic import and a lowercase statement, and
        # another walks from an aliased ``core`` package into memory. Both are
        # among the modules the guard checks, and both are reported there.
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "main.py", "")
            _write(root / "core" / "__init__.py", "")
            _write(root / "core" / "memory.py", "import sqlite3\n")
            database_probe = root / "core" / "database_probe.py"
            _write(
                database_probe,
                "from importlib import import_module as load\n"
                "\n"
                'database = load("sqlite3")\n'
                'connection = database.connect("example.db")\n'
                'connection.execute("commit")\n',
            )
            facade_probe = root / "tools" / "facade_probe.py"
            _write(
                facade_probe,
                "import core as maria_core\n"
                "value = maria_core.memory._MIGRATIONS\n",
            )

            checked = modules_outside_memory_boundary(root)
            self.assertIn(database_probe, checked)
            self.assertIn(facade_probe, checked)
            probe_tree = _parse(database_probe)
            self.assertEqual(
                database_driver_imports(
                    probe_tree,
                    _package_of(database_probe, root),
                ),
                ["sqlite3"],
            )
            self.assertEqual(executable_sql(probe_tree), ["commit"])
            self.assertEqual(
                memory_facade_bypasses(
                    _parse(facade_probe),
                    _package_of(facade_probe, root),
                ),
                ["core.memory._MIGRATIONS"],
            )


def production_modules(maria_root: Path = MARIA_ROOT) -> tuple[Path, ...]:
    """Return every production module, found rather than listed.

    That is each entry point plus every ``.py`` file at any depth beneath each
    production package. Tests are never production code, and caches and local
    environments are pruned wherever they appear.
    """
    modules = [
        maria_root / name
        for name in PRODUCTION_ENTRY_POINTS
        if (maria_root / name).is_file()
    ]
    for package in PRODUCTION_PACKAGES:
        modules.extend(_python_files(maria_root / package))
    return tuple(sorted(modules))


def importable_top_level_roots(maria_root: Path = MARIA_ROOT) -> frozenset[str]:
    """Return every name directly under the Maria root that Python could import.

    A top-level ``.py`` file is a module, and a directory is a package, regular
    or namespace, as soon as it holds Python source at any depth. Caches and
    local environments are not Maria's source and are skipped.
    """
    roots: set[str] = set()
    for entry in maria_root.iterdir():
        if entry.is_file():
            if entry.suffix == ".py":
                roots.add(entry.name)
        elif not _is_excluded_directory(entry) and _python_files(entry):
            roots.add(entry.name)
    return frozenset(roots)


def inside_memory_boundary(module: Path, maria_root: Path = MARIA_ROOT) -> bool:
    """Return whether a module belongs to the memory boundary.

    The boundary is ``core/memory.py`` or, equally, every module beneath a
    ``core/memory/`` package, so any ownership or import guard can share this
    one definition whichever shape the boundary has.
    """
    boundary = maria_root / "core" / "memory"
    return module == boundary.with_suffix(".py") or boundary in module.parents


def modules_outside_memory_boundary(
    maria_root: Path = MARIA_ROOT,
) -> tuple[Path, ...]:
    """Return every production module the ownership checks apply to."""
    return tuple(
        module
        for module in production_modules(maria_root)
        if not inside_memory_boundary(module, maria_root)
    )


def memory_boundary_shapes(maria_root: Path = MARIA_ROOT) -> tuple[Path, ...]:
    """Return each memory-boundary shape present: the module, the package, or both."""
    boundary = maria_root / "core" / "memory"
    return tuple(
        shape
        for shape in (boundary.with_suffix(".py"), boundary / "__init__.py")
        if shape.is_file()
    )


def imported_modules(
    tree: ast.Module,
    package: tuple[str, ...] = (),
) -> list[tuple[str, tuple[str, ...]]]:
    """Return every import in a tree as ``(absolute module, imported names)``.

    ``import a.b`` gives ``("a.b", ())`` and ``from a import x`` gives
    ``("a", ("x",))``. A relative import is resolved against ``package``, and
    one reaching above the production tree is left out because it cannot
    import anything. A dynamic import counts when its function is recognisably
    an import function and the module it names is a literal, passed by
    position or as ``name=``. Nothing is imported or run to find any of this out.
    """
    importers = _dynamic_importers(tree)
    found: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute_module(node, package)
            if module is not None:
                found.append((module, tuple(alias.name for alias in node.names)))
        elif isinstance(node, ast.Call):
            module = _dynamically_imported_module(node, importers)
            if module is not None:
                found.append((module, ()))
    return found


def database_driver_imports(
    tree: ast.Module,
    package: tuple[str, ...] = (),
) -> list[str]:
    """Return every imported module whose top-level package reaches SQLite."""
    return [
        module
        for module, _ in imported_modules(tree, package)
        if module.partition(".")[0] in DATABASE_DRIVERS
    ]


def memory_facade_bypasses(
    tree: ast.Module,
    package: tuple[str, ...] = (),
) -> list[str]:
    """Return every route that reaches memory other than through its facade.

    Only ``from core.memory import Memory`` is allowed. Importing the module
    object, a submodule, a private name, or everything at once, importing
    memory's names from the ``core`` package instead, or walking to
    ``core.memory`` from an imported ``core`` package would each let a caller
    reach past ``Memory`` into what it owns.
    """
    bypasses: list[str] = []
    for module, names in imported_modules(tree, package):
        if module == "core":
            bypasses.extend(
                f"core.{name}" for name in names if name in CORE_MEMORY_ROUTES
            )
        elif module == "core.memory" and names:
            bypasses.extend(
                f"core.memory.{name}"
                for name in names
                if name not in MEMORY_FACADE
            )
        elif module == "core.memory" or module.startswith("core.memory."):
            bypasses.append(module)
    bypasses.extend(_core_memory_attribute_routes(tree))
    return bypasses


def runnable_strings(tree: ast.Module) -> list[str]:
    """Return the text of every string expression a module can run.

    Docstrings document code rather than run, and comments never reach the
    tree at all, so neither is returned. An f-string is returned with each
    substitution written as ``?``, and a ``+`` chain holding a string is joined
    the same way, so SQL assembled either way still reads as one statement.
    Adjacent literals already arrive joined.
    """
    docstrings = _docstring_nodes(tree)
    return [
        text
        for node in ast.walk(tree)
        if id(node) not in docstrings and (text := _string_text(node)) is not None
    ]


def executable_sql(tree: ast.Module) -> list[str]:
    """Return every runnable string in a module that is a SQL statement.

    Anywhere in a module, a string is SQL when it has a statement's shape, and
    a statement that can be a single word counts only when written in
    capitals, so a message that is just "Commit" is not mistaken for one. A
    string handed to an execution method is a statement by its position, so
    there a short statement is recognised in any case.
    """
    statements = [text for text in runnable_strings(tree) if _is_sql(text)]
    statements.extend(
        text
        for text in _executed_statements(tree)
        if not _is_sql(text) and _is_executed_sql(text)
    )
    return statements


def _python_files(directory: Path) -> list[Path]:
    """Return every ``.py`` file beneath a directory, pruning excluded ones."""
    found: list[Path] = []
    for current, subdirectories, files in os.walk(directory):
        here = Path(current)
        subdirectories[:] = [
            name
            for name in subdirectories
            if not _is_excluded_directory(here / name)
        ]
        found.extend(here / name for name in files if name.endswith(".py"))
    return found


def _is_excluded_directory(directory: Path) -> bool:
    """Return whether a directory holds caches or an environment, not source."""
    return (
        directory.name == "__pycache__"
        or directory.name.startswith(".")
        or (directory / "pyvenv.cfg").is_file()
    )


def _absolute_module(
    node: ast.ImportFrom,
    package: tuple[str, ...],
) -> str | None:
    """Return the absolute module a ``from`` import names, or None if none."""
    if node.level == 0:
        return node.module
    if node.level > len(package):
        return None
    base = package[: len(package) - node.level + 1]
    return ".".join(base + tuple(node.module.split(".") if node.module else ()))


def _dynamic_importers(tree: ast.Module) -> frozenset[str]:
    """Return every bare name a call can use to import a module dynamically.

    ``import_module`` and ``__import__`` always count. So does a name an import
    from ``importlib`` or ``builtins`` binds to one of them, such as ``load``
    in ``from importlib import import_module as load``, and a name a plain
    assignment binds to one. An attribute call needs no binding: an attribute
    named ``import_module`` or ``__import__`` counts whatever it is looked up
    on, which covers ``importlib`` and every alias of it. Data flow beyond a
    direct assignment - a container, a return value, ``getattr`` - is not
    followed.
    """
    importers = set(DYNAMIC_IMPORT_FUNCTIONS)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module in DYNAMIC_IMPORT_PROVIDERS
        ):
            importers.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in DYNAMIC_IMPORT_FUNCTIONS
            )

    assignments = [
        (target.id, node.value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
        for target in (
            node.targets if isinstance(node, ast.Assign) else (node.target,)
        )
        if isinstance(target, ast.Name)
    ]
    # One assignment can alias another, so repeat until nothing new is bound.
    growing = True
    while growing:
        growing = False
        for name, value in assignments:
            if name not in importers and _is_import_function(value, importers):
                importers.add(name)
                growing = True
    return frozenset(importers)


def _is_import_function(node: ast.AST, importers: set[str] | frozenset[str]) -> bool:
    """Return whether an expression names a function that imports by string."""
    if isinstance(node, ast.Name):
        return node.id in importers
    return isinstance(node, ast.Attribute) and node.attr in DYNAMIC_IMPORT_FUNCTIONS


def _dynamically_imported_module(
    node: ast.Call,
    importers: frozenset[str],
) -> str | None:
    """Return the literal, absolute module a call imports, or None if none."""
    if not _is_import_function(node.func, importers):
        return None
    if node.args and not isinstance(node.args[0], ast.Starred):
        argument: ast.AST | None = node.args[0]
    else:
        argument = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "name"),
            None,
        )
    module = None if argument is None else _literal_text(argument)
    if module is None or module.startswith("."):
        return None
    return module


def _core_memory_attribute_routes(tree: ast.Module) -> list[str]:
    """Return every attribute route from an imported ``core`` package to memory.

    ``import core``, ``import core as alias``, and ``import core.brain`` all
    bind the package, and once anything has imported ``core.memory`` it is an
    attribute of that package, so a caller holding the package could walk
    past the facade. Every such route counts, ``getattr`` with a literal
    ``"memory"`` included.
    """
    packages = _core_package_names(tree)
    if not packages:
        return []
    routes: list[str] = []
    continued: set[int] = set()
    # The walk visits an outer attribute before the ``core.memory`` inside it,
    # so a route that goes on to a name is reported once, with that name.
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _is_core_memory(node.value, packages):
            routes.append(f"core.memory.{node.attr}")
            continued.add(id(node.value))
        elif _is_core_memory(node, packages) and id(node) not in continued:
            routes.append("core.memory")
    return routes


def _core_package_names(tree: ast.Module) -> set[str]:
    """Return every name an ``import`` statement binds to the ``core`` package."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        for alias in node.names:
            if alias.asname is None and alias.name.partition(".")[0] == "core":
                names.add("core")
            elif alias.asname is not None and alias.name == "core":
                names.add(alias.asname)
    return names


def _is_core_memory(node: ast.AST, packages: set[str]) -> bool:
    """Return whether an expression reaches ``core.memory`` through the package."""
    if isinstance(node, ast.Attribute):
        return (
            node.attr == "memory"
            and isinstance(node.value, ast.Name)
            and node.value.id in packages
        )
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in packages
        and _literal_text(node.args[1]) == "memory"
    )


def _executed_statements(tree: ast.Module) -> list[str]:
    """Return the text of every statement handed to an execution method."""
    statements: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and _callable_name(node.func) in SQL_EXECUTION_METHODS
            and node.args
        ):
            text = _string_text(node.args[0])
            if text is not None:
                statements.append(text)
    return statements


def _callable_name(function: ast.AST) -> str | None:
    """Return the final name a call is made through, such as ``execute``."""
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


def _is_sql(text: str) -> bool:
    """Return whether a string is SQL wherever in a module it appears."""
    return bool(
        SQL_STATEMENT.search(text) or SQL_STANDALONE_STATEMENT.fullmatch(text)
    )


def _is_executed_sql(text: str) -> bool:
    """Return whether a string handed to an execution method is a short statement."""
    return any(
        SQL_EXECUTED_STATEMENT.fullmatch(statement)
        for statement in text.split(";")
        if statement.strip()
    )


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Return the identities of every module, class, and function docstring."""
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, owners) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def _string_text(node: ast.AST) -> str | None:
    """Return a string expression's text, or None if the node is not one.

    Each f-string substitution, and each operand of a ``+`` chain that is not
    itself a string, is written as ``?``: its value cannot be known without
    running the module, and a placeholder keeps the statement around it whole.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else "?"
            for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _string_text(node.left)
        right = _string_text(node.right)
        if left is None and right is None:
            return None
        return ("?" if left is None else left) + ("?" if right is None else right)
    return None


def _literal_text(node: ast.AST) -> str | None:
    """Return a string expression's value if it is known without running it.

    That is a literal, an f-string with no substitution, or a ``+`` chain of
    those. Unlike ``_string_text`` nothing is stood in for: a value computed
    at run time makes the whole expression unknown.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts = [_literal_text(part) for part in node.values]
        return None if None in parts else "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_text(node.left)
        right = _literal_text(node.right)
        return None if left is None or right is None else left + right
    return None


def _parse(module: Path) -> ast.Module:
    """Parse one module's source without importing or running it."""
    return ast.parse(module.read_text(encoding="utf-8"), filename=str(module))


def _package_of(module: Path, maria_root: Path = MARIA_ROOT) -> tuple[str, ...]:
    """Return the package a module's relative imports resolve against."""
    return module.relative_to(maria_root).with_suffix("").parts[:-1]


def _relative(module: Path, maria_root: Path = MARIA_ROOT) -> str:
    """Return a module's path relative to the Maria root, for messages."""
    return module.relative_to(maria_root).as_posix()


def _write(path: Path, text: str) -> None:
    """Write one file of a temporary fixture tree, creating its directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
