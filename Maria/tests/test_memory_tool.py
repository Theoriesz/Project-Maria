"""Focused tests for Maria's explicit memory tool."""

from __future__ import annotations

import logging
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
            (
                "update",
                "update_memory",
                {"id": 1, "fact": "A fact"},
                "Unable to update memory.",
                "Memory update failed: Unable to update memory.",
            ),
            (
                "forget",
                "forget_memory",
                {"id": 1},
                "Unable to forget memory.",
                "Memory forget failed: Unable to forget memory.",
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
            (
                "update",
                "update_memory",
                {"id": 1, "fact": "A fact"},
                f"Memory update failed: {busy_text}",
            ),
            (
                "forget",
                "forget_memory",
                {"id": 1},
                f"Memory forget failed: {busy_text}",
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
        result = self.tool.execute("purge", {"id": 1})

        self.assertFalse(result.success)
        self.assertEqual(result.message, "Unsupported memory action: purge")
        self.assertIsNone(result.data)

    def test_tool_identity_supported_actions_and_factory_contract(self) -> None:
        built = memory_tool_module.build_tool(memory=self.memory)

        self.assertIsInstance(built, MemoryTool)
        self.assertEqual(built.name, "memory")
        self.assertEqual(
            built.description,
            "Stores, searches, lists, updates, and forgets explicit memories, "
            "and turns one memory's context consent on or off.",
        )
        self.assertEqual(
            built.actions,
            frozenset(
                {
                    "store",
                    "search",
                    "list",
                    "update",
                    "forget",
                    "enable_context",
                    "disable_context",
                }
            ),
        )
        self.assertIs(built.memory, self.memory)

    def test_update_success_message_and_data(self) -> None:
        stored = self.tool.execute("store", {"fact": "My editor is VS Code"})
        memory_id = stored.data["id"]

        result = self.tool.execute(
            "update",
            {"id": memory_id, "fact": "  My editor is Neovim  "},
        )

        self.assertTrue(result.success)
        self.assertEqual(result.message, f"Updated memory {memory_id}.")
        self.assertEqual(result.data, {"id": memory_id})
        self.assertIsNone(result.recorded_message)
        self.assertEqual(
            self.memory.get_memory(memory_id)["fact"], "My editor is Neovim"
        )

    def test_forget_success_message_and_data(self) -> None:
        stored = self.tool.execute("store", {"fact": "My editor is VS Code"})
        memory_id = stored.data["id"]

        result = self.tool.execute("forget", {"id": memory_id})

        self.assertTrue(result.success)
        self.assertEqual(result.message, f"Forgot memory {memory_id}.")
        self.assertEqual(result.data, {"id": memory_id})
        self.assertIsNone(result.recorded_message)
        self.assertIsNone(self.memory.get_memory(memory_id))

    def test_mutation_not_found_messages(self) -> None:
        for action, arguments in (
            ("update", {"id": 404, "fact": "Replacement fact"}),
            ("forget", {"id": 404}),
        ):
            with self.subTest(action=action):
                result = self.tool.execute(action, arguments)

                self.assertFalse(result.success)
                self.assertEqual(result.message, "Memory not found: 404.")
                self.assertIsNone(result.data)
                self.assertIsNone(result.recorded_message)

    def test_update_conflict_message_names_only_numeric_ids(self) -> None:
        target = self.tool.execute("store", {"fact": "My editor is VS Code"})
        other = self.tool.execute("store", {"fact": "Straße in Berlin"})
        target_id = target.data["id"]
        other_id = other.data["id"]

        result = self.tool.execute(
            "update",
            {"id": target_id, "fact": "STRASSE IN BERLIN"},
        )

        self.assertFalse(result.success)
        self.assertEqual(
            result.message,
            f"Memory {target_id} conflicts with existing memory {other_id}.",
        )
        self.assertIsNone(result.data)
        self.assertIsNone(result.recorded_message)
        self.assertNotIn("STRASSE", result.message)
        self.assertNotIn("Straße", result.message)
        self.assertEqual(
            self.memory.get_memory(target_id)["fact"], "My editor is VS Code"
        )
        self.assertEqual(
            self.memory.get_memory(other_id)["fact"], "Straße in Berlin"
        )

    def test_mutation_ids_must_be_positive_integers_and_never_reach_memory(
        self,
    ) -> None:
        cases = (
            ("update", "update_memory", {"fact": "Replacement fact"}),
            ("forget", "forget_memory", {}),
        )
        invalid_ids = (0, -1, "1", 1.0, True, False, None)

        for action, method_name, extra_arguments in cases:
            for invalid_id in invalid_ids:
                with self.subTest(action=action, invalid_id=invalid_id):
                    with patch.object(self.memory, method_name) as memory_method:
                        result = self.tool.execute(
                            action,
                            {"id": invalid_id, **extra_arguments},
                        )
                    self.assertFalse(result.success)
                    self.assertEqual(
                        result.message,
                        "Memory id must be a positive whole number.",
                    )
                    self.assertIsNone(result.data)
                    memory_method.assert_not_called()

            with self.subTest(action=action, invalid_id="missing"):
                with patch.object(self.memory, method_name) as memory_method:
                    result = self.tool.execute(action, dict(extra_arguments))
                self.assertFalse(result.success)
                self.assertEqual(
                    result.message,
                    "Memory id must be a positive whole number.",
                )
                memory_method.assert_not_called()

    def test_update_fact_must_be_a_non_blank_string_and_never_reaches_memory(
        self,
    ) -> None:
        stored = self.tool.execute("store", {"fact": "Original fact"})
        memory_id = stored.data["id"]

        for new_fact in (None, 5, True, [], object(), "", "   "):
            with self.subTest(new_fact=new_fact):
                with patch.object(self.memory, "update_memory") as memory_method:
                    result = self.tool.execute(
                        "update",
                        {"id": memory_id, "fact": new_fact},
                    )
                self.assertFalse(result.success)
                self.assertEqual(result.message, "New memory fact is required.")
                self.assertIsNone(result.data)
                memory_method.assert_not_called()

        with self.subTest(new_fact="missing"):
            with patch.object(self.memory, "update_memory") as memory_method:
                result = self.tool.execute("update", {"id": memory_id})
            self.assertFalse(result.success)
            self.assertEqual(result.message, "New memory fact is required.")
            memory_method.assert_not_called()

        self.assertEqual(self.memory.get_memory(memory_id)["fact"], "Original fact")

    def test_rejected_update_facts_are_never_coerced_into_persisted_text(
        self,
    ) -> None:
        stored = self.tool.execute("store", {"fact": "Original fact"})
        memory_id = stored.data["id"]

        for new_fact in (None, 5, True, []):
            with self.subTest(new_fact=new_fact):
                result = self.tool.execute(
                    "update",
                    {"id": memory_id, "fact": new_fact},
                )
                self.assertFalse(result.success)
                self.assertNotIn(str(new_fact), result.message)

        self.assertEqual(self.memory.get_memory(memory_id)["fact"], "Original fact")

    def test_mutation_results_and_logs_exclude_fact_content(self) -> None:
        logger = _quiet_logger("memory-tool-log-privacy")
        tool = MemoryTool(memory=self.memory, logger=logger)
        target, _ = self.memory.store_memory("secret stored fact")
        other, _ = self.memory.store_memory("secret other fact")

        with patch.object(logger, "info") as info_mock, patch.object(
            logger, "warning"
        ) as warning_mock:
            updated = tool.execute(
                "update", {"id": target, "fact": "secret new fact"}
            )
            conflicted = tool.execute(
                "update", {"id": target, "fact": "SECRET OTHER FACT"}
            )
            forgotten = tool.execute("forget", {"id": target})
            missing = tool.execute("forget", {"id": target})

        self.assertTrue(updated.success)
        self.assertFalse(conflicted.success)
        self.assertTrue(forgotten.success)
        self.assertFalse(missing.success)
        self.assertEqual(conflicted.message, f"Memory {target} conflicts with existing memory {other}.")

        logged = [
            " ".join(str(argument) for argument in call.args)
            for call in list(info_mock.call_args_list) + list(warning_mock.call_args_list)
        ]
        texts = logged + [
            result.message for result in (updated, conflicted, forgotten, missing)
        ] + [
            str(result.data) for result in (updated, conflicted, forgotten, missing)
        ]
        for text in texts:
            self.assertNotIn("secret stored fact", text)
            self.assertNotIn("secret other fact", text)
            self.assertNotIn("secret new fact", text)
            self.assertNotIn("SECRET OTHER FACT", text)


class MemoryToolContextConsentTests(unittest.TestCase):
    """Verify the consent actions name an id, never a fact, and change one row."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        database_path = Path(self.temporary_directory.name) / "maria.db"
        self.memory = Memory(database_path, logger=_quiet_logger("consent-memory"))
        self.memory.initialize()
        self.tool = MemoryTool(memory=self.memory, logger=_quiet_logger("consent"))
        self.memory_id, _ = self.memory.store_memory("secret consent fact")

    def _enabled(self) -> list[int]:
        return [row["id"] for row in self.memory.list_context_memories(limit=100)]

    def test_enable_and_disable_report_the_id_and_never_the_fact(self) -> None:
        enabled = self.tool.execute("enable_context", {"id": self.memory_id})
        self.assertEqual(self._enabled(), [self.memory_id])
        disabled = self.tool.execute("disable_context", {"id": self.memory_id})
        self.assertEqual(self._enabled(), [])

        self.assertTrue(enabled.success)
        self.assertEqual(enabled.message, f"Enabled context for memory {self.memory_id}.")
        self.assertEqual(
            enabled.data,
            {"id": self.memory_id, "context_enabled": True},
        )
        self.assertTrue(disabled.success)
        self.assertEqual(
            disabled.message,
            f"Disabled context for memory {self.memory_id}.",
        )
        for result in (enabled, disabled):
            self.assertNotIn("secret consent fact", result.message)
            self.assertNotIn("secret consent fact", str(result.data))

    def test_a_missing_memory_cannot_report_success(self) -> None:
        for action in ("enable_context", "disable_context"):
            with self.subTest(action=action):
                result = self.tool.execute(action, {"id": 999})

                self.assertFalse(result.success)
                self.assertEqual(result.message, "Memory not found: 999.")
        self.assertEqual(self._enabled(), [])

    def test_invalid_ids_never_reach_memory(self) -> None:
        for action in ("enable_context", "disable_context"):
            for invalid_id in (0, -1, "1", 1.0, True, False, None):
                with self.subTest(action=action, invalid_id=invalid_id):
                    with patch.object(self.memory, "set_memory_context") as setter:
                        result = self.tool.execute(action, {"id": invalid_id})
                    self.assertFalse(result.success)
                    self.assertEqual(
                        result.message,
                        "Memory id must be a positive whole number.",
                    )
                    setter.assert_not_called()

    def test_a_memory_failure_becomes_a_fixed_message(self) -> None:
        with patch.object(
            self.memory,
            "set_memory_context",
            side_effect=RuntimeError("Unable to change memory context consent."),
        ):
            result = self.tool.execute("enable_context", {"id": self.memory_id})

        self.assertFalse(result.success)
        self.assertEqual(
            result.message,
            "Memory context change failed: Unable to change memory context consent.",
        )

    def test_the_update_message_changes_only_when_consent_was_on(self) -> None:
        plain = self.tool.execute(
            "update",
            {"id": self.memory_id, "fact": "secret revised fact"},
        )
        self.tool.execute("enable_context", {"id": self.memory_id})
        noticed = self.tool.execute(
            "update",
            {"id": self.memory_id, "fact": "secret final fact"},
        )

        self.assertEqual(plain.message, f"Updated memory {self.memory_id}.")
        self.assertEqual(
            noticed.message,
            f"Updated memory {self.memory_id}. Context consent for memory "
            f"{self.memory_id} was turned off because its text changed.",
        )
        self.assertEqual(noticed.data, {"id": self.memory_id})
        self.assertEqual(self._enabled(), [])


def _quiet_logger(name: str) -> logging.Logger:
    """Return an isolated logger that never writes an application log file."""
    logger = logging.getLogger(f"tests.{name}")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


if __name__ == "__main__":
    unittest.main()
