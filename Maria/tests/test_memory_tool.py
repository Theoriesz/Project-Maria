"""Focused tests for Maria's explicit memory tool."""

from __future__ import annotations

import inspect
import logging
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tools.memory as memory_tool_module
from core.memory import Memory
from tools.memory import MemoryTool


class MemoryToolTests(unittest.TestCase):
    """Verify the memory tool's public messages and result data."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        database_path = Path(self.temporary_directory.name) / "maria.db"
        self.memory = Memory(database_path, logger=_quiet_logger("memory-tool-memory"))
        self.memory.initialize()
        self.tool = MemoryTool(
            memory=self.memory,
            logger=_quiet_logger("memory-tool"),
        )

    def test_store_success_message_and_data(self) -> None:
        result = self.tool.execute("store", {"fact": "  My editor is VS Code  "})

        self.assertTrue(result.success)
        memory_id = result.data["id"]
        self.assertEqual(result.message, f"Remembered memory {memory_id}: My editor is VS Code")
        self.assertEqual(
            result.data,
            {"id": memory_id, "fact": "My editor is VS Code", "created": True},
        )

    def test_duplicate_success_message_and_data(self) -> None:
        first = self.tool.execute("store", {"fact": "My editor is VS Code"})
        duplicate = self.tool.execute("store", {"fact": "MY EDITOR IS VS CODE"})

        memory_id = first.data["id"]
        self.assertTrue(duplicate.success)
        self.assertEqual(
            duplicate.message,
            f"Already remembered as memory {memory_id}: My editor is VS Code",
        )
        self.assertEqual(
            duplicate.data,
            {
                "id": memory_id,
                "fact": "My editor is VS Code",
                "created": False,
            },
        )
        stored = self.memory.list_memories()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["id"], memory_id)
        self.assertEqual(stored[0]["fact"], "My editor is VS Code")

    def test_duplicate_read_failure_is_controlled(self) -> None:
        first = self.tool.execute("store", {"fact": "My editor is VS Code"})

        with patch.object(
            self.memory,
            "get_memory",
            side_effect=RuntimeError("Unable to read memory."),
        ):
            duplicate = self.tool.execute(
                "store",
                {"fact": "MY EDITOR IS VS CODE"},
            )

        self.assertFalse(duplicate.success)
        self.assertEqual(
            duplicate.message,
            "Memory storage failed: Unable to read memory.",
        )
        self.assertIsNone(duplicate.data)
        stored = self.memory.list_memories()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["id"], first.data["id"])

    def test_duplicate_missing_stored_row_is_controlled(self) -> None:
        with (
            patch.object(self.memory, "store_memory", return_value=(123, False)),
            patch.object(self.memory, "get_memory", return_value=None),
        ):
            duplicate = self.tool.execute(
                "store",
                {"fact": "MY EDITOR IS VS CODE"},
            )

        self.assertFalse(duplicate.success)
        self.assertEqual(
            duplicate.message,
            "Memory storage failed: Stored memory could not be retrieved.",
        )
        self.assertIsNone(duplicate.data)

    def test_search_formats_one_memory_per_line(self) -> None:
        first_id, _ = self.memory.store_memory("Alpha editor")
        second_id, _ = self.memory.store_memory("Beta editor")

        result = self.tool.execute("search", {"query": "EDITOR"})

        self.assertTrue(result.success)
        self.assertEqual(
            result.message,
            f"{second_id}: Beta editor\n{first_id}: Alpha editor",
        )
        self.assertEqual(
            [memory["id"] for memory in result.data],
            [second_id, first_id],
        )

    def test_list_formats_one_memory_per_line(self) -> None:
        first_id, _ = self.memory.store_memory("First fact")
        second_id, _ = self.memory.store_memory("Second fact")

        result = self.tool.execute("list", {})

        self.assertTrue(result.success)
        self.assertEqual(
            result.message,
            f"{second_id}: Second fact\n{first_id}: First fact",
        )
        self.assertEqual(
            [memory["id"] for memory in result.data],
            [second_id, first_id],
        )

    def test_search_with_exactly_twenty_matches_has_no_suffix(self) -> None:
        memory_ids = [
            self.memory.store_memory(f"Fact {index}")[0] for index in range(20)
        ]

        result = self.tool.execute("search", {"query": "FACT"})

        self.assertTrue(result.success)
        self.assertEqual(len(result.data), 20)
        self.assertEqual(
            [memory["id"] for memory in result.data],
            list(reversed(memory_ids)),
        )
        self.assertNotIn("Showing the newest", result.message)

    def test_search_with_more_than_twenty_matches_shows_newest_twenty_and_suffix(
        self,
    ) -> None:
        memory_ids = [
            self.memory.store_memory(f"Fact {index}")[0] for index in range(21)
        ]

        result = self.tool.execute("search", {"query": "FACT"})

        self.assertTrue(result.success)
        self.assertEqual(len(result.data), 20)
        self.assertEqual(
            [memory["id"] for memory in result.data],
            list(reversed(memory_ids[1:])),
        )
        expected_lines = [
            f"{memory_ids[index]}: Fact {index}" for index in range(20, 0, -1)
        ]
        expected_suffix = "Showing the newest 20 matches. More matching memories exist."
        self.assertEqual(
            result.message,
            "\n".join(expected_lines) + "\n" + expected_suffix,
        )
        self.assertEqual(result.message.count(expected_suffix), 1)

    def test_list_with_exactly_twenty_rows_has_no_suffix(self) -> None:
        memory_ids = [
            self.memory.store_memory(f"Fact {index}")[0] for index in range(20)
        ]

        result = self.tool.execute("list", {})

        self.assertTrue(result.success)
        self.assertEqual(len(result.data), 20)
        self.assertEqual(
            [memory["id"] for memory in result.data],
            list(reversed(memory_ids)),
        )
        self.assertNotIn("Showing the newest", result.message)

    def test_list_with_more_than_twenty_rows_shows_newest_twenty_and_suffix(
        self,
    ) -> None:
        memory_ids = [
            self.memory.store_memory(f"Fact {index}")[0] for index in range(21)
        ]

        result = self.tool.execute("list", {})

        self.assertTrue(result.success)
        self.assertEqual(len(result.data), 20)
        self.assertEqual(
            [memory["id"] for memory in result.data],
            list(reversed(memory_ids[1:])),
        )
        expected_lines = [
            f"{memory_ids[index]}: Fact {index}" for index in range(20, 0, -1)
        ]
        expected_suffix = "Showing the newest 20 memories. More memories exist."
        self.assertEqual(
            result.message,
            "\n".join(expected_lines) + "\n" + expected_suffix,
        )
        self.assertEqual(result.message.count(expected_suffix), 1)

    def test_explicit_non_default_limit_preserves_existing_behavior(self) -> None:
        memory_ids = [
            self.memory.store_memory(f"Fact {index}")[0] for index in range(21)
        ]

        search_result = self.tool.execute("search", {"query": "FACT", "limit": 5})
        list_result = self.tool.execute("list", {"limit": 5})

        for result in (search_result, list_result):
            self.assertTrue(result.success)
            self.assertEqual(len(result.data), 5)
            self.assertEqual(
                [memory["id"] for memory in result.data],
                list(reversed(memory_ids[16:])),
            )
            self.assertNotIn("Showing the newest", result.message)

    def test_no_match_is_a_successful_result(self) -> None:
        self.memory.store_memory("My editor is VS Code")

        result = self.tool.execute("search", {"query": "terminal"})

        self.assertTrue(result.success)
        self.assertEqual(result.message, "No matching memories found.")
        self.assertEqual(result.data, [])

    def test_empty_list_is_a_successful_result(self) -> None:
        result = self.tool.execute("list", {})

        self.assertTrue(result.success)
        self.assertEqual(result.message, "No memories found.")
        self.assertEqual(result.data, [])

    def test_expected_validation_failures_are_controlled(self) -> None:
        cases = (
            (
                "store",
                {"fact": "   "},
                "Memory storage failed: Memory fact is required.",
            ),
            (
                "search",
                {"query": "\t"},
                "Memory search failed: Memory search query is required.",
            ),
        )

        for action, arguments, expected_message in cases:
            with self.subTest(action=action):
                result = self.tool.execute(action, arguments)
                self.assertFalse(result.success)
                self.assertEqual(result.message, expected_message)
                self.assertIsNone(result.data)

    def test_expected_database_failures_are_controlled(self) -> None:
        cases = (
            (
                "store",
                "store_memory",
                {"fact": "A fact"},
                "Unable to store memory.",
                "Memory storage failed: Unable to store memory.",
            ),
            (
                "search",
                "search_memories",
                {"query": "fact"},
                "Unable to search memories.",
                "Memory search failed: Unable to search memories.",
            ),
            (
                "list",
                "list_memories",
                {},
                "Unable to list memories.",
                "Memory listing failed: Unable to list memories.",
            ),
        )

        for action, method_name, arguments, error_text, expected_message in cases:
            with self.subTest(action=action):
                with patch.object(
                    self.memory,
                    method_name,
                    side_effect=RuntimeError(error_text),
                ):
                    result = self.tool.execute(action, arguments)
                self.assertFalse(result.success)
                self.assertEqual(result.message, expected_message)
                self.assertIsNone(result.data)

    def test_expected_busy_database_failures_are_controlled(self) -> None:
        busy_text = "Database is busy. Try again shortly."
        cases = (
            (
                "store",
                "store_memory",
                {"fact": "A fact"},
                f"Memory storage failed: {busy_text}",
            ),
            (
                "search",
                "search_memories",
                {"query": "fact"},
                f"Memory search failed: {busy_text}",
            ),
            (
                "list",
                "list_memories",
                {},
                f"Memory listing failed: {busy_text}",
            ),
        )

        for action, method_name, arguments, expected_message in cases:
            with self.subTest(action=action):
                with patch.object(
                    self.memory,
                    method_name,
                    side_effect=RuntimeError(busy_text),
                ):
                    result = self.tool.execute(action, arguments)
                self.assertFalse(result.success)
                self.assertEqual(result.message, expected_message)
                self.assertIsNone(result.data)
                self.assertNotIn(str(self.memory.database_path), result.message)

    def test_duplicate_read_busy_failure_is_controlled(self) -> None:
        busy_text = "Database is busy. Try again shortly."
        first = self.tool.execute("store", {"fact": "My editor is VS Code"})

        with patch.object(
            self.memory,
            "get_memory",
            side_effect=RuntimeError(busy_text),
        ):
            duplicate = self.tool.execute(
                "store",
                {"fact": "MY EDITOR IS VS CODE"},
            )

        self.assertFalse(duplicate.success)
        self.assertEqual(
            duplicate.message,
            f"Memory storage failed: {busy_text}",
        )
        self.assertIsNone(duplicate.data)
        stored = self.memory.list_memories()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["id"], first.data["id"])

    def test_unknown_action_is_rejected(self) -> None:
        result = self.tool.execute("forget", {"id": 1})

        self.assertFalse(result.success)
        self.assertEqual(result.message, "Unsupported memory action: forget")
        self.assertIsNone(result.data)

    def test_memory_tool_contains_no_direct_sql(self) -> None:
        source = inspect.getsource(memory_tool_module)

        self.assertNotRegex(source, r"(?m)^\s*(?:import|from)\s+sqlite3\b")
        self.assertIsNone(
            re.search(
                r"\b(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|"
                r"DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE)\b",
                source,
                flags=re.IGNORECASE | re.DOTALL,
            )
        )


def _quiet_logger(name: str) -> logging.Logger:
    """Return an isolated logger that never writes an application log file."""
    logger = logging.getLogger(f"tests.{name}")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


if __name__ == "__main__":
    unittest.main()
