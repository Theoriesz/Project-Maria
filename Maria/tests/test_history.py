"""Tests for Maria's HistoryTool."""

from __future__ import annotations

import logging
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from core.memory import Memory
from core.tool_manager import ToolExecutionResult
from tools.history import HistoryTool, build_tool


def _quiet_logger() -> logging.Logger:
    """Return an isolated logger that never writes an application log file."""
    logger = logging.getLogger("tests.history")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


class HistoryToolTests(unittest.TestCase):
    """Exercise HistoryTool using only a temporary database."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        database_path = Path(self.temporary_directory.name) / "maria.db"
        self.logger = Mock(spec=logging.Logger)
        self.memory = Memory(database_path, logger=_quiet_logger())
        self.memory.initialize()
        self.tool = HistoryTool(memory=self.memory, logger=self.logger)

    # --- identity / factory --------------------------------------------

    def test_tool_identity_supported_actions_and_factory_contract(self) -> None:
        self.assertEqual(self.tool.name, "history")
        self.assertEqual(
            self.tool.description,
            "Lists, searches, and reads interaction-history rows.",
        )
        self.assertEqual(self.tool.actions, frozenset({"list", "search", "read"}))

        built = build_tool(memory=self.memory)
        self.assertIsInstance(built, HistoryTool)
        self.assertIs(built.memory, self.memory)

    def test_unknown_action_is_rejected(self) -> None:
        result = self.tool.execute("delete", {"id": 1})

        self.assertFalse(result.success)
        self.assertEqual(result.message, "Unsupported history action: delete")

    # --- list ------------------------------------------------------------

    def test_list_with_no_interactions_is_a_successful_empty_result(self) -> None:
        result = self.tool.execute("list", {})

        self.assertEqual(
            result,
            ToolExecutionResult(True, "No interactions found.", []),
        )

    def test_list_formats_one_row_per_line_newest_first(self) -> None:
        with patch(
            "core.memory._utc_timestamp",
            side_effect=[
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            ],
        ):
            first_id = self.memory.record_interaction(
                "first request", "first response"
            )
            second_id = self.memory.record_interaction(
                "second request", "second response"
            )

        result = self.tool.execute("list", {})

        self.assertTrue(result.success)
        self.assertEqual(
            result.message,
            f"{second_id}: second request\n{first_id}: first request",
        )
        self.assertEqual(
            result.data,
            [
                {
                    "id": second_id,
                    "user_input": "second request",
                    "created_at": "2026-01-02T00:00:00+00:00",
                },
                {
                    "id": first_id,
                    "user_input": "first request",
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
            ],
        )

    def test_list_with_exactly_twenty_rows_has_no_suffix(self) -> None:
        for index in range(20):
            self.memory.record_interaction(f"request {index}", f"response {index}")

        result = self.tool.execute("list", {})

        lines = result.message.splitlines()
        self.assertEqual(len(lines), 20)
        self.assertNotIn("More interactions exist.", result.message)
        self.assertEqual(len(result.data), 20)

    def test_list_with_twenty_one_rows_shows_newest_twenty_and_suffix_without_leaking_sentinel(
        self,
    ) -> None:
        ids = [
            self.memory.record_interaction(f"request {index}", f"response {index}")
            for index in range(21)
        ]

        result = self.tool.execute("list", {})

        lines = result.message.splitlines()
        self.assertEqual(len(lines), 21)
        self.assertEqual(
            lines[-1],
            "Showing the newest 20 interactions. More interactions exist.",
        )
        self.assertEqual(len(result.data), 20)
        oldest_id = ids[0]
        self.assertNotIn(f"{oldest_id}: request 0", result.message)
        self.assertNotIn(oldest_id, [row["id"] for row in result.data])

    def test_list_data_is_redacted_and_excludes_response(self) -> None:
        self.memory.record_interaction("a request", "a secret response")

        result = self.tool.execute("list", {})

        self.assertEqual(set(result.data[0]), {"id", "user_input", "created_at"})
        self.assertNotIn("response", result.data[0])

    def test_non_printable_characters_are_neutralized_in_list_and_search_display(
        self,
    ) -> None:
        raw_input = (
            "MiXeD marker \u00dc\nline\rtwo\tthree\x00four"
            "\x1bfive\x7fsix\u2028seven  eight"
        )
        interaction_id = self.memory.record_interaction(raw_input, "response")
        expected_preview = (
            "MiXeD marker \u00dc line two three four five six seven  eight"
        )
        expected_message = f"{interaction_id}: {expected_preview}"

        listed = self.tool.execute("list", {})
        searched = self.tool.execute("search", {"query": "marker"})

        # Every non-printable character becomes exactly one ASCII space, the
        # doubled ordinary space is not collapsed, and printable Unicode and
        # casing survive unchanged. list and search render identically.
        self.assertEqual(listed.message, expected_message)
        self.assertEqual(searched.message, expected_message)
        for character in ("\n", "\r", "\t", "\x00", "\x1b", "\x7f", "\u2028"):
            with self.subTest(character=character):
                self.assertNotIn(character, listed.message)
                self.assertNotIn(character, searched.message)
        self.assertIn("MiXeD marker \u00dc", listed.message)
        self.assertIn("seven  eight", listed.message)

        self.assertEqual(listed.data[0]["user_input"], raw_input)
        self.assertEqual(searched.data[0]["user_input"], raw_input)
        stored = self.memory.get_interaction(interaction_id)
        assert stored is not None
        self.assertEqual(stored["user_input"], raw_input)

    def test_list_controlled_runtime_error(self) -> None:
        with patch.object(
            self.memory,
            "_connect",
            side_effect=sqlite3.OperationalError("boom"),
        ):
            result = self.tool.execute("list", {})

        self.assertFalse(result.success)
        self.assertEqual(
            result.message,
            "History listing failed: Unable to fetch recent interactions.",
        )
        self.assertNotIn("boom", result.message)
        self.logger.warning.assert_called_once()

    # --- search ------------------------------------------------------------

    def test_search_requires_non_blank_query(self) -> None:
        for query in ("", "   ", "\t\r\n"):
            with self.subTest(query=query):
                result = self.tool.execute("search", {"query": query})
                self.assertEqual(
                    result,
                    ToolExecutionResult(False, "Search query is required."),
                )

    def test_search_with_no_matches_is_a_successful_empty_result(self) -> None:
        self.memory.record_interaction("unrelated", "unrelated response")

        result = self.tool.execute("search", {"query": "absent-term"})

        self.assertEqual(
            result,
            ToolExecutionResult(True, "No matching interactions found.", []),
        )

    def test_search_matches_response_while_excluding_response_from_data(self) -> None:
        interaction_id = self.memory.record_interaction(
            "recall editor", "1: my editor is VS Code"
        )

        result = self.tool.execute("search", {"query": "VS Code"})

        self.assertTrue(result.success)
        self.assertEqual(len(result.data), 1)
        self.assertEqual(result.data[0]["id"], interaction_id)
        self.assertEqual(result.data[0]["user_input"], "recall editor")
        self.assertEqual(set(result.data[0]), {"id", "user_input", "created_at"})
        self.assertNotIn("VS Code", result.message)

    def test_search_with_exactly_twenty_matches_has_no_suffix(self) -> None:
        for index in range(20):
            self.memory.record_interaction(f"marker {index}", "response")

        result = self.tool.execute("search", {"query": "marker"})

        lines = result.message.splitlines()
        self.assertEqual(len(lines), 20)
        self.assertNotIn("More matching interactions exist.", result.message)

    def test_search_with_twenty_one_matches_shows_newest_twenty_and_suffix_without_leaking_sentinel(
        self,
    ) -> None:
        ids = [
            self.memory.record_interaction(f"marker {index}", "response")
            for index in range(21)
        ]

        result = self.tool.execute("search", {"query": "marker"})

        lines = result.message.splitlines()
        self.assertEqual(len(lines), 21)
        self.assertEqual(
            lines[-1],
            "Showing the newest 20 matches. More matching interactions exist.",
        )
        self.assertEqual(len(result.data), 20)
        oldest_id = ids[0]
        self.assertNotIn(oldest_id, [row["id"] for row in result.data])

    def test_search_controlled_runtime_error(self) -> None:
        with patch.object(
            self.memory,
            "_connect",
            side_effect=sqlite3.OperationalError("boom"),
        ):
            result = self.tool.execute("search", {"query": "marker"})

        self.assertFalse(result.success)
        self.assertEqual(
            result.message,
            "History search failed: Unable to search interactions.",
        )
        self.assertNotIn("boom", result.message)
        self.logger.warning.assert_called_once()

    # --- read ------------------------------------------------------------

    def test_read_returns_complete_multiline_content_and_records_summary(self) -> None:
        interaction_id = self.memory.record_interaction(
            "multi\nline\nrequest", "multi\nline\nresponse"
        )
        stored = self.memory.get_interaction(interaction_id)
        assert stored is not None

        result = self.tool.execute("read", {"id": interaction_id})

        expected_message = (
            f"Interaction {interaction_id}\n"
            f"Created at: {stored['created_at']}\n"
            "Request:\n"
            "multi\nline\nrequest\n"
            "Response:\n"
            "multi\nline\nresponse"
        )
        self.assertEqual(result.message, expected_message)
        self.assertEqual(result.data, stored)
        self.assertEqual(
            result.recorded_message,
            f"Displayed interaction {interaction_id}.",
        )
        self.logger.info.assert_called_once()

    def test_read_missing_interaction_is_a_controlled_failure(self) -> None:
        result = self.tool.execute("read", {"id": 999})

        self.assertEqual(
            result,
            ToolExecutionResult(False, "Interaction not found: 999."),
        )

    def test_read_missing_id_argument_is_a_controlled_failure(self) -> None:
        result = self.tool.execute("read", {})

        self.assertEqual(
            result,
            ToolExecutionResult(
                False, "Interaction id must be a positive whole number."
            ),
        )

    def test_read_rejects_invalid_direct_ids(self) -> None:
        invalid_ids = (0, -1, "5", 5.0, True, False, None)

        for invalid_id in invalid_ids:
            with self.subTest(invalid_id=invalid_id):
                result = self.tool.execute("read", {"id": invalid_id})
                self.assertEqual(
                    result,
                    ToolExecutionResult(
                        False, "Interaction id must be a positive whole number."
                    ),
                )

    def test_read_controlled_runtime_error(self) -> None:
        self.memory.record_interaction("request", "response")
        with patch.object(
            self.memory,
            "_connect",
            side_effect=sqlite3.OperationalError("boom"),
        ):
            result = self.tool.execute("read", {"id": 1})

        self.assertFalse(result.success)
        self.assertEqual(
            result.message,
            "History read failed: Unable to read interaction.",
        )
        self.assertNotIn("boom", result.message)
        self.logger.warning.assert_called_once()

    # --- logging privacy --------------------------------------------------

    def test_logs_never_include_request_response_or_query_content(self) -> None:
        secret_input = "SECRET-REQUEST-TOKEN"
        secret_response = "SECRET-RESPONSE-TOKEN"
        secret_query = "SECRET-QUERY-TOKEN"
        interaction_id = self.memory.record_interaction(secret_input, secret_response)

        self.tool.execute("list", {})
        self.tool.execute("search", {"query": secret_query})
        self.tool.execute("read", {"id": interaction_id})

        logged_calls = " ".join(str(entry) for entry in self.logger.method_calls)
        self.assertNotIn(secret_input, logged_calls)
        self.assertNotIn(secret_response, logged_calls)
        self.assertNotIn(secret_query, logged_calls)


if __name__ == "__main__":
    unittest.main()
