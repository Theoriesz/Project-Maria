"""Tests for Maria's SQLite-backed Memory API."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, Mock, patch

from core import memory as memory_module
from core.memory import Memory


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
        self.memory.initialize()
        memory_id, _ = self.memory.store_memory("Original fact")

        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_connection = MagicMock(spec=sqlite3.Connection)
        mock_connection.execute.return_value = mock_cursor

        with patch.object(self.memory, "_connect", return_value=mock_connection):
            result = self.memory.update_memory(memory_id, "  My Edited Fact  ")

        self.assertEqual(result, (True, None))
        mock_connection.execute.assert_called_once()
        query, params = mock_connection.execute.call_args.args
        self.assertIn("UPDATE memories", query)
        self.assertIn("SET fact = ?, normalized_fact = ?", query)
        self.assertIn("WHERE id = ?", query)
        self.assertNotIn("SELECT", query)
        self.assertNotIn("My Edited Fact", query)
        self.assertNotIn(str(memory_id), query)
        self.assertEqual(params, ("My Edited Fact", "my edited fact", memory_id))
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


if __name__ == "__main__":
    unittest.main()
