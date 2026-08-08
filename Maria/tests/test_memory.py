"""Tests for Maria's SQLite-backed Memory API."""

from __future__ import annotations

import logging
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
