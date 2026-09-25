"""Focused regression tests for Maria's notes tool."""

from __future__ import annotations

import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

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
            self.tool.description,
            "Creates, lists, reads, retitles, edits, and deletes notes.",
        )
        self.assertEqual(
            self.tool.actions,
            frozenset({"create", "list", "read", "retitle", "edit", "delete"}),
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

    def test_absent_title_uses_untitled(self) -> None:
        result = self.tool.execute("create", {"body": "Missing title"})

        self.assertTrue(result.success)
        self.assertEqual(result.data["title"], "Untitled")
        self.assertEqual(
            self.memory.get_note(note_id=result.data["id"])["title"],
            "Untitled",
        )

    def test_provided_blank_title_is_a_controlled_failure(self) -> None:
        """A supplied blank title is malformed rather than repaired.

        Before per-action argument contracts this silently became
        ``Untitled``. V1-06 removes that repair: only a genuinely absent
        title takes the tool-owned default.
        """
        for blank in ("", " \t ", " \r\n "):
            with self.subTest(blank=repr(blank)):
                result = self.tool.execute(
                    "create",
                    {"title": blank, "body": "Blank"},
                )

                self.assertEqual(
                    result,
                    ToolExecutionResult(False, "Note title is required."),
                )
        self.assertEqual(self.memory.list_notes(limit=5), [])

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
        unknown = self.tool.execute("archive", {"target": "1"})

        self.assertEqual(
            blank,
            ToolExecutionResult(False, "Note id or title is required."),
        )
        self.assertEqual(
            unknown,
            ToolExecutionResult(False, "Unsupported notes action: archive"),
        )

    def test_invalid_list_limit_is_a_controlled_failure(self) -> None:
        for limit in (None, "not-a-number"):
            with self.subTest(limit=limit):
                result = self.tool.execute("list", {"limit": limit})
                self.assertEqual(
                    result,
                    ToolExecutionResult(False, "Notes limit must be a number."),
                )

    def test_retitle_replaces_the_title_and_returns_only_the_id(self) -> None:
        note_id = self.memory.create_note("Original Title", "Untouched body")

        result = self.tool.execute(
            "retitle",
            {"id": note_id, "title": "  Renamed Title  "},
        )

        self.assertEqual(
            result,
            ToolExecutionResult(
                True,
                f"Retitled note {note_id}.",
                {"id": note_id},
            ),
        )
        self.assertIsNone(result.recorded_message)
        stored = self.memory.get_note(note_id=note_id)
        self.assertEqual(stored["title"], "Renamed Title")
        self.assertEqual(stored["body"], "Untouched body")

    def test_edit_replaces_the_body_and_returns_only_the_id(self) -> None:
        note_id = self.memory.create_note("Untouched Title", "Original body")

        result = self.tool.execute(
            "edit",
            {"id": note_id, "body": "\n  Replacement body.  \t"},
        )

        self.assertEqual(
            result,
            ToolExecutionResult(
                True,
                f"Edited note {note_id}.",
                {"id": note_id},
            ),
        )
        self.assertIsNone(result.recorded_message)
        stored = self.memory.get_note(note_id=note_id)
        self.assertEqual(stored["body"], "Replacement body.")
        self.assertEqual(stored["title"], "Untouched Title")

    def test_delete_removes_the_note_and_returns_only_the_id(self) -> None:
        note_id = self.memory.create_note("Disposable", "Disposable body")
        survivor_id = self.memory.create_note("Survivor", "Survivor body")

        result = self.tool.execute("delete", {"id": note_id})

        self.assertEqual(
            result,
            ToolExecutionResult(
                True,
                f"Deleted note {note_id}.",
                {"id": note_id},
            ),
        )
        self.assertIsNone(result.recorded_message)
        self.assertIsNone(self.memory.get_note(note_id=note_id))
        self.assertIsNotNone(self.memory.get_note(note_id=survivor_id))

    def test_mutations_on_a_missing_note_use_the_not_found_result(self) -> None:
        cases = (
            ("retitle", {"id": 99999, "title": "New Title"}),
            ("edit", {"id": 99999, "body": "New body"}),
            ("delete", {"id": 99999}),
        )

        for action, arguments in cases:
            with self.subTest(action=action):
                result = self.tool.execute(action, arguments)
                self.assertEqual(
                    result,
                    ToolExecutionResult(False, "Note not found: 99999."),
                )
                self.assertIsNone(result.recorded_message)

    def test_mutation_ids_must_be_positive_integers_and_never_reach_memory(
        self,
    ) -> None:
        mutations = (
            ("retitle", "retitle_note", {"title": "New Title"}),
            ("edit", "edit_note", {"body": "New body"}),
            ("delete", "delete_note", {}),
        )
        invalid_ids = (0, -1, "1", 1.0, True, False, None)
        expected = ToolExecutionResult(
            False,
            "Note id must be a positive whole number.",
        )

        for action, method_name, content in mutations:
            for note_id in invalid_ids:
                with self.subTest(action=action, note_id=repr(note_id)):
                    with patch.object(self.memory, method_name) as memory_method:
                        result = self.tool.execute(
                            action,
                            {"id": note_id, **content},
                        )
                    self.assertEqual(result, expected)
                    memory_method.assert_not_called()
            with self.subTest(action=action, note_id="missing"):
                with patch.object(self.memory, method_name) as memory_method:
                    result = self.tool.execute(action, dict(content))
                self.assertEqual(result, expected)
                memory_method.assert_not_called()

    def test_mutation_content_must_be_a_non_blank_string_and_never_reaches_memory(
        self,
    ) -> None:
        note_id = self.memory.create_note("Original Title", "Original body")
        mutations = (
            ("retitle", "retitle_note", "title", "New note title is required."),
            ("edit", "edit_note", "body", "New note body is required."),
        )
        invalid_values = (None, 5, True, [], object(), "", "   ")

        for action, method_name, key, expected_message in mutations:
            expected = ToolExecutionResult(False, expected_message)
            for value in invalid_values:
                with self.subTest(action=action, value=repr(value)):
                    with patch.object(self.memory, method_name) as memory_method:
                        result = self.tool.execute(
                            action,
                            {"id": note_id, key: value},
                        )
                    self.assertEqual(result, expected)
                    memory_method.assert_not_called()
            with self.subTest(action=action, value="missing"):
                with patch.object(self.memory, method_name) as memory_method:
                    result = self.tool.execute(action, {"id": note_id})
                self.assertEqual(result, expected)
                memory_method.assert_not_called()

    def test_rejected_mutation_content_is_never_coerced_into_persisted_text(
        self,
    ) -> None:
        note_id = self.memory.create_note("Original Title", "Original body")

        retitled = self.tool.execute("retitle", {"id": note_id, "title": 5})
        edited = self.tool.execute("edit", {"id": note_id, "body": True})

        self.assertEqual(
            retitled,
            ToolExecutionResult(False, "New note title is required."),
        )
        self.assertEqual(
            edited,
            ToolExecutionResult(False, "New note body is required."),
        )
        stored = self.memory.get_note(note_id=note_id)
        self.assertEqual(stored["title"], "Original Title")
        self.assertEqual(stored["body"], "Original body")

    def test_successful_mutation_results_and_logs_exclude_title_and_body(
        self,
    ) -> None:
        distinctive_title = "PRIVATE-NOTE-TITLE-2b41c0d9-complete"
        distinctive_body = "PRIVATE-NOTE-BODY-9c72e51a-complete"
        note_id = self.memory.create_note("Original", "Original body")
        self.logger.reset_mock()

        retitled = self.tool.execute(
            "retitle",
            {"id": note_id, "title": distinctive_title},
        )
        edited = self.tool.execute(
            "edit",
            {"id": note_id, "body": distinctive_body},
        )
        deleted = self.tool.execute("delete", {"id": note_id})

        self.assertTrue(retitled.success)
        self.assertTrue(edited.success)
        self.assertTrue(deleted.success)
        logged_calls = " ".join(str(entry) for entry in self.logger.method_calls)
        for secret in (distinctive_title, distinctive_body):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, logged_calls)
                for result in (retitled, edited, deleted):
                    self.assertNotIn(secret, result.message)
                    self.assertNotIn(secret, str(result.data))

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
            (
                "retitle",
                "retitle_note",
                ValueError("New note title is required."),
                {"id": 1, "title": "Title"},
                "Note retitle failed: New note title is required.",
            ),
            (
                "retitle",
                "retitle_note",
                RuntimeError("Unable to retitle note."),
                {"id": 1, "title": "Title"},
                "Note retitle failed: Unable to retitle note.",
            ),
            (
                "edit",
                "edit_note",
                ValueError("New note body is required."),
                {"id": 1, "body": "Body"},
                "Note edit failed: New note body is required.",
            ),
            (
                "edit",
                "edit_note",
                RuntimeError("Unable to edit note."),
                {"id": 1, "body": "Body"},
                "Note edit failed: Unable to edit note.",
            ),
            (
                "delete",
                "delete_note",
                RuntimeError("Unable to delete note."),
                {"id": 1},
                "Note deletion failed: Unable to delete note.",
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

    def test_expected_busy_database_failures_are_controlled(self) -> None:
        busy_text = "Database is busy. Try again shortly."
        cases = (
            (
                "create",
                "create_note",
                {"title": "Title", "body": "Body"},
                f"Note creation failed: {busy_text}",
            ),
            (
                "list",
                "list_notes",
                {},
                f"Note listing failed: {busy_text}",
            ),
            (
                "read",
                "get_note",
                {"target": "Title"},
                f"Note read failed: {busy_text}",
            ),
            (
                "retitle",
                "retitle_note",
                {"id": 1, "title": "Title"},
                f"Note retitle failed: {busy_text}",
            ),
            (
                "edit",
                "edit_note",
                {"id": 1, "body": "Body"},
                f"Note edit failed: {busy_text}",
            ),
            (
                "delete",
                "delete_note",
                {"id": 1},
                f"Note deletion failed: {busy_text}",
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
                self.assertEqual(
                    result,
                    ToolExecutionResult(False, expected_message),
                )
                self.assertNotIn(str(self.database_path), result.message)

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


if __name__ == "__main__":
    unittest.main()
