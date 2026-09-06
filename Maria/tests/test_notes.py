"""Focused regression tests for Maria's notes tool."""

from __future__ import annotations

import inspect
import logging
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import tools.notes as notes_module
from core.memory import Memory
from core.tool_manager import ToolExecutionResult
from tools.notes import NotesTool, build_tool


class NotesToolTests(unittest.TestCase):
    """Exercise notes through a shared, temporary Memory instance."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "db" / "notes.db"
        self.logger = Mock(spec=logging.Logger)
        self.memory = Memory(self.database_path, logger=self.logger)
        self.memory.initialize()
        self.tool = NotesTool(memory=self.memory, logger=self.logger)

    def test_tool_identity_supported_actions_and_factory_contract(self) -> None:
        factory_tool = build_tool(
            memory=self.memory,
            config=object(),
            logger=self.logger,
        )

        self.assertEqual(self.tool.name, "notes")
        self.assertEqual(
            self.tool.actions,
            frozenset({"create", "list", "read"}),
        )
        self.assertIsInstance(factory_tool, NotesTool)
        self.assertIs(factory_tool.memory, self.memory)

    def test_empty_note_list_uses_existing_success_message(self) -> None:
        result = self.tool.execute("list", {})

        self.assertEqual(
            result,
            ToolExecutionResult(True, "No notes found.", []),
        )

    def test_create_strips_explicit_title_and_body_and_returns_data(self) -> None:
        result = self.tool.execute(
            "create",
            {"title": "  Release notes  ", "body": "\n  Ready to ship.  \t"},
        )

        self.assertTrue(result.success)
        note_id = result.data["id"]
        self.assertEqual(result.message, f"Created note {note_id}: Release notes")
        self.assertEqual(
            result.data,
            {"id": note_id, "title": "Release notes"},
        )
        self.assertEqual(
            self.memory.get_note(note_id=note_id)["body"],
            "Ready to ship.",
        )

    def test_missing_or_blank_title_uses_untitled(self) -> None:
        cases = ({"body": "Missing title"}, {"title": " \t ", "body": "Blank"})

        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.tool.execute("create", arguments)
                self.assertTrue(result.success)
                self.assertEqual(result.data["title"], "Untitled")
                self.assertEqual(
                    self.memory.get_note(note_id=result.data["id"])["title"],
                    "Untitled",
                )

    def test_missing_or_blank_body_is_a_controlled_failure(self) -> None:
        cases = ({"title": "Missing"}, {"title": "Blank", "body": " \t\r\n "})

        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.tool.execute("create", arguments)
                self.assertEqual(
                    result,
                    ToolExecutionResult(False, "Note body is required."),
                )

    def test_listing_formats_one_note_per_line_in_newest_first_order(self) -> None:
        first_id = self.memory.create_note("First", "First body")
        second_id = self.memory.create_note("Second", "Second body")

        result = self.tool.execute("list", {})

        self.assertTrue(result.success)
        self.assertEqual(
            result.message,
            f"{second_id}: Second\n{first_id}: First",
        )
        self.assertEqual(
            [note["id"] for note in result.data],
            [second_id, first_id],
        )

    def test_listing_with_exactly_twenty_rows_has_no_suffix(self) -> None:
        note_ids = [
            self.memory.create_note(f"Note {index}", f"Body {index}")
            for index in range(20)
        ]

        result = self.tool.execute("list", {})

        self.assertTrue(result.success)
        self.assertEqual(len(result.data), 20)
        self.assertEqual(
            [note["id"] for note in result.data],
            list(reversed(note_ids)),
        )
        self.assertNotIn("More notes exist.", result.message)

    def test_listing_with_more_than_twenty_rows_shows_newest_twenty_and_suffix(
        self,
    ) -> None:
        note_ids = [
            self.memory.create_note(f"Note {index}", f"Body {index}")
            for index in range(21)
        ]

        result = self.tool.execute("list", {})

        self.assertTrue(result.success)
        self.assertEqual(len(result.data), 20)
        self.assertEqual(
            [note["id"] for note in result.data],
            list(reversed(note_ids[1:])),
        )
        expected_lines = [
            f"{note_ids[index]}: Note {index}" for index in range(20, 0, -1)
        ]
        self.assertEqual(
            result.message,
            "\n".join(expected_lines)
            + "\nShowing the newest 20 notes. More notes exist.",
        )

    def test_listing_with_many_more_than_twenty_rows_still_shows_newest_twenty(
        self,
    ) -> None:
        note_ids = [
            self.memory.create_note(f"Note {index}", f"Body {index}")
            for index in range(30)
        ]

        result = self.tool.execute("list", {})

        self.assertTrue(result.success)
        self.assertEqual(len(result.data), 20)
        self.assertEqual(
            [note["id"] for note in result.data],
            list(reversed(note_ids[10:])),
        )
        self.assertTrue(
            result.message.endswith(
                "Showing the newest 20 notes. More notes exist."
            )
        )
        self.assertEqual(
            result.message.count("Showing the newest 20 notes. More notes exist."),
            1,
        )

    def test_explicit_non_default_limit_preserves_existing_behavior(self) -> None:
        note_ids = [
            self.memory.create_note(f"Note {index}", f"Body {index}")
            for index in range(21)
        ]

        result = self.tool.execute("list", {"limit": 5})

        self.assertTrue(result.success)
        self.assertEqual(len(result.data), 5)
        self.assertEqual(
            [note["id"] for note in result.data],
            list(reversed(note_ids[16:])),
        )
        self.assertNotIn("More notes exist.", result.message)

    def test_memory_note_limits_are_clamped_to_public_boundaries(self) -> None:
        note_ids = [
            self.memory.create_note(f"Note {index}", f"Body {index}")
            for index in range(101)
        ]

        minimum = self.memory.list_notes(limit=0)
        maximum = self.memory.list_notes(limit=101)

        self.assertEqual([note["id"] for note in minimum], [note_ids[-1]])
        self.assertEqual(len(maximum), 100)
        self.assertEqual(
            [note["id"] for note in maximum],
            list(reversed(note_ids[1:])),
        )

    def test_read_by_numeric_id_succeeds(self) -> None:
        note_id = self.memory.create_note("By id", "Numeric lookup body")

        result = self.tool.execute("read", {"target": str(note_id)})

        self.assertTrue(result.success)
        self.assertEqual(result.message, "By id\nNumeric lookup body")
        self.assertEqual(result.data["id"], note_id)
        self.assertEqual(result.data["title"], "By id")
        self.assertEqual(result.data["body"], "Numeric lookup body")

    def test_read_by_exact_title_succeeds_and_remains_case_sensitive(self) -> None:
        note_id = self.memory.create_note("Case Sensitive", "Exact title body")

        exact = self.tool.execute("read", {"target": "Case Sensitive"})
        different_case = self.tool.execute("read", {"target": "case sensitive"})

        self.assertTrue(exact.success)
        self.assertEqual(exact.data["id"], note_id)
        self.assertEqual(exact.message, "Case Sensitive\nExact title body")
        self.assertEqual(
            different_case,
            ToolExecutionResult(False, "Note not found: case sensitive"),
        )

    def test_duplicate_titles_are_allowed_and_newest_match_is_returned(self) -> None:
        first_id = self.memory.create_note("Duplicate", "Older body")
        second_id = self.memory.create_note("Duplicate", "Newer body")

        result = self.tool.execute("read", {"target": "Duplicate"})

        self.assertNotEqual(first_id, second_id)
        self.assertTrue(result.success)
        self.assertEqual(result.data["id"], second_id)
        self.assertEqual(result.message, "Duplicate\nNewer body")

    def test_numeric_title_is_interpreted_as_an_id_at_the_tool_boundary(self) -> None:
        created = self.tool.execute(
            "create",
            {"title": "12345", "body": "A numeric title"},
        )

        result = self.tool.execute("read", {"target": "12345"})

        self.assertTrue(created.success)
        self.assertNotEqual(created.data["id"], 12345)
        self.assertEqual(
            result,
            ToolExecutionResult(False, "Note not found: 12345"),
        )

    def test_missing_id_or_title_uses_existing_not_found_result(self) -> None:
        for target in ("99999", "Missing exact title"):
            with self.subTest(target=target):
                result = self.tool.execute("read", {"target": target})
                self.assertEqual(
                    result,
                    ToolExecutionResult(False, f"Note not found: {target}"),
                )

    def test_blank_read_target_and_unknown_action_are_rejected(self) -> None:
        blank = self.tool.execute("read", {"target": " \t "})
        unknown = self.tool.execute("delete", {"target": "1"})

        self.assertEqual(
            blank,
            ToolExecutionResult(False, "Note id or title is required."),
        )
        self.assertEqual(
            unknown,
            ToolExecutionResult(False, "Unsupported notes action: delete"),
        )

    def test_invalid_list_limit_is_a_controlled_failure(self) -> None:
        for limit in (None, "not-a-number"):
            with self.subTest(limit=limit):
                result = self.tool.execute("list", {"limit": limit})
                self.assertEqual(
                    result,
                    ToolExecutionResult(False, "Notes limit must be a number."),
                )

    def test_expected_value_and_runtime_errors_become_controlled_results(self) -> None:
        cases = (
            (
                "create",
                "create_note",
                ValueError("invalid note"),
                {"title": "Title", "body": "Body"},
                "Note creation failed: invalid note",
            ),
            (
                "create",
                "create_note",
                RuntimeError("database unavailable"),
                {"title": "Title", "body": "Body"},
                "Note creation failed: database unavailable",
            ),
            (
                "list",
                "list_notes",
                RuntimeError("database unavailable"),
                {},
                "Note listing failed: database unavailable",
            ),
            (
                "read",
                "get_note",
                ValueError("invalid selector"),
                {"target": "Title"},
                "Note read failed: invalid selector",
            ),
            (
                "read",
                "get_note",
                RuntimeError("database unavailable"),
                {"target": "Title"},
                "Note read failed: database unavailable",
            ),
        )

        for action, method_name, error, arguments, expected_message in cases:
            with self.subTest(action=action, error=type(error).__name__):
                with patch.object(self.memory, method_name, side_effect=error):
                    result = self.tool.execute(action, arguments)
                self.assertEqual(
                    result,
                    ToolExecutionResult(False, expected_message),
                )

    def test_second_memory_and_tool_instance_retrieves_persisted_note(self) -> None:
        created = self.tool.execute(
            "create",
            {"title": "Persistent", "body": "Survives reconstruction"},
        )
        restarted_memory = Memory(self.database_path, logger=self.logger)
        restarted_memory.initialize()
        restarted_tool = NotesTool(restarted_memory, logger=self.logger)

        result = restarted_tool.execute(
            "read",
            {"target": str(created.data["id"])},
        )

        self.assertTrue(result.success)
        self.assertEqual(result.message, "Persistent\nSurvives reconstruction")
        self.assertEqual(result.data["id"], created.data["id"])

    def test_successful_note_logging_does_not_include_the_full_body(self) -> None:
        distinctive_body = "PRIVATE-NOTE-BODY-7f35b3b8-complete"
        self.logger.reset_mock()

        result = self.tool.execute(
            "create",
            {"title": "Private", "body": distinctive_body},
        )

        self.assertTrue(result.success)
        logged_calls = " ".join(str(entry) for entry in self.logger.method_calls)
        self.assertNotIn(distinctive_body, logged_calls)

    def test_notes_tool_contains_no_direct_sql_or_sqlite_import(self) -> None:
        source = inspect.getsource(notes_module)

        self.assertNotRegex(source, r"(?m)^\s*(?:import|from)\s+sqlite3\b")
        self.assertIsNone(
            re.search(
                r"\b(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|"
                r"DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE)\b",
                source,
                flags=re.IGNORECASE | re.DOTALL,
            )
        )


if __name__ == "__main__":
    unittest.main()
