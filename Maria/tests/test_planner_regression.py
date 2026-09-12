"""Regression tests for Maria's non-memory planner commands."""

from __future__ import annotations

import logging
import unittest

from core.planner import Plan, Planner, PlanStep


UNSUPPORTED_RESPONSE = (
    "I do not have an executable v0.1 plan for that yet. "
    "Type 'help' to see supported commands."
)


class PlannerRegressionTests(unittest.TestCase):
    """Verify the strict v0.1 mappings outside the focused memory suite."""

    def setUp(self) -> None:
        logger = logging.Logger("tests.planner_regression")
        logger.addHandler(logging.NullHandler())
        self.planner = Planner(logger=logger)

    def test_calculate_maps_to_exact_step(self) -> None:
        self._assert_single_step(
            "calculate 12 / 3",
            PlanStep("calculator", "calculate", {"expression": "12 / 3"}),
        )

    def test_list_maps_to_exact_step(self) -> None:
        self._assert_single_step(
            "list reports/2026",
            PlanStep("filesystem", "list", {"path": "reports/2026"}),
        )

    def test_read_file_maps_to_exact_step(self) -> None:
        self._assert_single_step(
            "read file reports/summary.txt",
            PlanStep(
                "filesystem",
                "read",
                {"path": "reports/summary.txt"},
            ),
        )

    def test_rename_maps_to_exact_step(self) -> None:
        self._assert_single_step(
            "rename draft.txt to final.txt",
            PlanStep(
                "filesystem",
                "rename",
                {"source": "draft.txt", "destination": "final.txt"},
            ),
        )

    def test_move_maps_to_exact_step(self) -> None:
        self._assert_single_step(
            "move final.txt to archive/final.txt",
            PlanStep(
                "filesystem",
                "move",
                {
                    "source": "final.txt",
                    "destination": "archive/final.txt",
                },
            ),
        )

    def test_create_note_maps_to_exact_step(self) -> None:
        self._assert_single_step(
            "create note Release: Ship the test suite",
            PlanStep(
                "notes",
                "create",
                {"title": "Release", "body": "Ship the test suite"},
            ),
        )

    def test_create_note_with_only_colon_creates_untitled_note(self) -> None:
        self._assert_single_step(
            "create note : Ship the test suite",
            PlanStep(
                "notes",
                "create",
                {"title": "Untitled", "body": "Ship the test suite"},
            ),
        )

    def test_create_note_missing_colon_returns_controlled_usage_response(
        self,
    ) -> None:
        cases = (
            "create note Project Plan",
            "note Project Plan",
            "CREATE NOTE Project Plan",
        )

        for request in cases:
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(plan.steps, ())
                self.assertEqual(
                    plan.response_if_no_action,
                    "Use: create note <title>: <body>",
                )

    def test_create_note_splits_only_on_the_first_colon(self) -> None:
        self._assert_single_step(
            "create note Links: Documentation: https://example.com/path?a=1",
            PlanStep(
                "notes",
                "create",
                {
                    "title": "Links",
                    "body": "Documentation: https://example.com/path?a=1",
                },
            ),
        )

    def test_create_note_trims_outer_whitespace_and_preserves_internal_spacing(
        self,
    ) -> None:
        self._assert_single_step(
            "create note   Title  :   Body with   spaces",
            PlanStep(
                "notes",
                "create",
                {"title": "Title", "body": "Body with   spaces"},
            ),
        )

    def test_create_note_with_colon_and_empty_body_still_creates_a_step(
        self,
    ) -> None:
        self._assert_single_step(
            "create note Title:",
            PlanStep("notes", "create", {"title": "Title", "body": ""}),
        )

    def test_notes_maps_to_exact_step(self) -> None:
        self._assert_single_step("notes", PlanStep("notes", "list", {}))

    def test_read_note_maps_id_and_exact_title_to_exact_steps(self) -> None:
        cases = (
            ("read note 17", {"target": "17"}),
            ("read note Exact Release Title", {"target": "Exact Release Title"}),
        )

        for request, arguments in cases:
            with self.subTest(request=request):
                self._assert_single_step(
                    request,
                    PlanStep("notes", "read", arguments),
                )

    def test_retitle_note_maps_to_exact_step(self) -> None:
        self._assert_single_step(
            "retitle note 5: New Title",
            PlanStep("notes", "retitle", {"id": 5, "title": "New Title"}),
        )

    def test_edit_note_maps_to_exact_step(self) -> None:
        self._assert_single_step(
            "edit note 5: New body",
            PlanStep("notes", "edit", {"id": 5, "body": "New body"}),
        )

    def test_delete_note_maps_to_exact_step(self) -> None:
        self._assert_single_step(
            "delete note 5",
            PlanStep("notes", "delete", {"id": 5}),
        )

    def test_note_lifecycle_commands_accept_leading_zeroes(self) -> None:
        cases = (
            (
                "retitle note 007: New Title",
                PlanStep("notes", "retitle", {"id": 7, "title": "New Title"}),
            ),
            (
                "edit note 007: New body",
                PlanStep("notes", "edit", {"id": 7, "body": "New body"}),
            ),
            ("delete note 007", PlanStep("notes", "delete", {"id": 7})),
        )

        for request, expected_step in cases:
            with self.subTest(request=request):
                self._assert_single_step(request, expected_step)

    def test_note_lifecycle_keywords_are_case_insensitive(self) -> None:
        cases = (
            (
                "RETITLE NOTE 9: Shout",
                PlanStep("notes", "retitle", {"id": 9, "title": "Shout"}),
            ),
            (
                "EDIT NOTE 9: Shout",
                PlanStep("notes", "edit", {"id": 9, "body": "Shout"}),
            ),
            ("DELETE NOTE 9", PlanStep("notes", "delete", {"id": 9})),
        )

        for request, expected_step in cases:
            with self.subTest(request=request):
                self.assertEqual(
                    self.planner.create_plan(request).steps,
                    (expected_step,),
                )

    def test_note_lifecycle_splits_only_on_the_first_colon(self) -> None:
        cases = (
            (
                "retitle note 005: New: Title",
                PlanStep("notes", "retitle", {"id": 5, "title": "New: Title"}),
            ),
            (
                "edit note 5: Body: with: colons",
                PlanStep(
                    "notes",
                    "edit",
                    {"id": 5, "body": "Body: with: colons"},
                ),
            ),
        )

        for request, expected_step in cases:
            with self.subTest(request=request):
                self._assert_single_step(request, expected_step)

    def test_note_lifecycle_operands_preserve_casing_and_internal_spacing(
        self,
    ) -> None:
        retitled = self.planner.create_plan("retitle note 5:   Spaced   Out   ")
        edited = self.planner.create_plan("edit note 5:   MiXeD   Body   ")

        self.assertEqual(retitled.original_request, "retitle note 5:   Spaced   Out")
        self.assertEqual(
            retitled.steps[0].arguments,
            {"id": 5, "title": "Spaced   Out"},
        )
        self.assertEqual(edited.original_request, "edit note 5:   MiXeD   Body")
        self.assertEqual(
            edited.steps[0].arguments,
            {"id": 5, "body": "MiXeD   Body"},
        )

    def test_bare_note_lifecycle_command_variants_return_exact_usage_response(
        self,
    ) -> None:
        cases = (
            ("retitle note", "Use: retitle note <id>: <new title>"),
            ("retitle note   ", "Use: retitle note <id>: <new title>"),
            ("RETITLE NOTE", "Use: retitle note <id>: <new title>"),
            ("retitle note 5", "Use: retitle note <id>: <new title>"),
            ("edit note", "Use: edit note <id>: <new body>"),
            ("edit note   ", "Use: edit note <id>: <new body>"),
            ("EDIT NOTE", "Use: edit note <id>: <new body>"),
            ("edit note 5", "Use: edit note <id>: <new body>"),
            ("delete note", "Use: delete note <id>"),
            ("delete note   ", "Use: delete note <id>"),
            ("DELETE NOTE", "Use: delete note <id>"),
        )

        for request, expected_response in cases:
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(plan.steps, ())
                self.assertEqual(plan.response_if_no_action, expected_response)

    def test_empty_new_note_content_returns_usage_rather_than_invalid_id(
        self,
    ) -> None:
        # A well-formed id with no content is a usage error, not an id error:
        # the emptiness check runs before the id is parsed.
        cases = (
            ("retitle note 5:", "Use: retitle note <id>: <new title>"),
            ("retitle note 5:    ", "Use: retitle note <id>: <new title>"),
            ("edit note 5:", "Use: edit note <id>: <new body>"),
            ("edit note 5:    ", "Use: edit note <id>: <new body>"),
        )

        for request, expected_response in cases:
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(plan.steps, ())
                self.assertEqual(plan.response_if_no_action, expected_response)

    def test_invalid_note_id_returns_exact_invalid_id_response(self) -> None:
        # The Unicode digit cases matter because str.isdigit() accepts them and
        # int() converts the decimal ones; only the ASCII guard rejects them.
        # The id part is parsed as one value, so an embedded extra token
        # (e.g. "5 extra") is an invalid id, not a separate unsupported command.
        invalid_ids = (
            "0",
            "-1",
            "+1",
            "1.5",
            "abc",
            "1 extra",
            "１２",
            "١٢",
        )
        templates = (
            "retitle note {note_id}: Title",
            "edit note {note_id}: Body",
            "delete note {note_id}",
        )

        for template in templates:
            for invalid_id in invalid_ids:
                request = template.format(note_id=invalid_id)
                with self.subTest(request=request):
                    plan = self.planner.create_plan(request)
                    self.assertEqual(plan.steps, ())
                    self.assertEqual(
                        plan.response_if_no_action,
                        "Note id must be a positive whole number.",
                    )

    def test_blank_note_id_with_content_returns_invalid_id_response(self) -> None:
        # Unlike "create note : body", a blank id is never defaulted; content
        # is present, so this is an id error rather than a usage error.
        cases = (
            "retitle note : Title",
            "edit note : Body",
        )

        for request in cases:
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(plan.steps, ())
                self.assertEqual(
                    plan.response_if_no_action,
                    "Note id must be a positive whole number.",
                )

    def test_note_lifecycle_near_prefix_and_plural_variants_remain_unsupported(
        self,
    ) -> None:
        requests = (
            "retitle notes 1: T",
            "edit notes 1: B",
            "delete notes 1",
            "retitle note1: T",
            "edit note1: B",
            "delete note1",
            "retitling note 1: T",
            "editing note 1: B",
            "deleting note 1",
            "clear notes",
            "clear note 1",
            "purge note 1",
        )

        for request in requests:
            with self.subTest(request=request):
                self._assert_unsupported(request)

    def test_rename_note_still_maps_to_the_filesystem_rename_step(self) -> None:
        # The pre-existing filesystem grammar owns "rename ...", so a path that
        # merely reads like a note must never be routed to the notes tool.
        self._assert_single_step(
            "rename note 1 to title",
            PlanStep(
                "filesystem",
                "rename",
                {"source": "note 1", "destination": "title"},
            ),
        )

    def test_history_maps_to_exact_step(self) -> None:
        self._assert_single_step("history", PlanStep("history", "list", {}))

    def test_search_history_maps_to_exact_step(self) -> None:
        self._assert_single_step(
            "search history weekend trip",
            PlanStep("history", "search", {"query": "weekend trip"}),
        )

    def test_read_interaction_maps_to_exact_step(self) -> None:
        self._assert_single_step(
            "read interaction 42",
            PlanStep("history", "read", {"id": 42}),
        )

    def test_read_interaction_accepts_leading_zeroes(self) -> None:
        self._assert_single_step(
            "read interaction 007",
            PlanStep("history", "read", {"id": 7}),
        )

    def test_delete_interaction_maps_to_exact_step(self) -> None:
        self._assert_single_step(
            "delete interaction 42",
            PlanStep("history", "delete", {"id": 42}),
        )

    def test_delete_interaction_accepts_leading_zeroes(self) -> None:
        self._assert_single_step(
            "delete interaction 007",
            PlanStep("history", "delete", {"id": 7}),
        )

    def test_history_command_keywords_are_case_insensitive(self) -> None:
        cases = (
            ("HISTORY", PlanStep("history", "list", {})),
            ("Search History trip", PlanStep("history", "search", {"query": "trip"})),
            ("READ INTERACTION 9", PlanStep("history", "read", {"id": 9})),
            ("DELETE INTERACTION 9", PlanStep("history", "delete", {"id": 9})),
        )

        for request, expected_step in cases:
            with self.subTest(request=request):
                self.assertEqual(
                    self.planner.create_plan(request).steps,
                    (expected_step,),
                )

    def test_search_history_query_preserves_casing_and_internal_spacing(self) -> None:
        plan = self.planner.create_plan("SEARCH HISTORY  Mixed  CASE   Query")

        self.assertEqual(
            plan.steps[0].arguments,
            {"query": "Mixed  CASE   Query"},
        )

    def test_bare_history_command_variants_return_exact_usage_response(self) -> None:
        cases = (
            ("search history", "Use: search history <query>"),
            ("search history   ", "Use: search history <query>"),
            ("SEARCH HISTORY", "Use: search history <query>"),
            ("read interaction", "Use: read interaction <id>"),
            ("read interaction   ", "Use: read interaction <id>"),
            ("READ INTERACTION", "Use: read interaction <id>"),
            ("delete interaction", "Use: delete interaction <id>"),
            ("delete interaction   ", "Use: delete interaction <id>"),
            ("DELETE INTERACTION", "Use: delete interaction <id>"),
        )

        for request, expected_response in cases:
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(plan.steps, ())
                self.assertEqual(plan.response_if_no_action, expected_response)

    def test_invalid_interaction_id_returns_exact_usage_response(self) -> None:
        # The Unicode digit cases matter because str.isdigit() accepts them and
        # int() converts the decimal ones; only the ASCII guard rejects them.
        invalid_ids = (
            "0",
            "-5",
            "+5",
            "5.0",
            "5a",
            "5 6",
            "²",
            "１２",
            "١٢",
        )

        for invalid_id in invalid_ids:
            with self.subTest(invalid_id=invalid_id):
                plan = self.planner.create_plan(f"read interaction {invalid_id}")
                self.assertEqual(plan.steps, ())
                self.assertEqual(
                    plan.response_if_no_action,
                    "Interaction id must be a positive whole number.",
                )

    def test_invalid_delete_interaction_id_returns_exact_usage_response(self) -> None:
        # Mirrors the read-interaction invalid-id cases: the operand after the
        # fixed "delete interaction " prefix is parsed as one value, so an
        # embedded extra token (e.g. "1 extra") is an invalid id, not a
        # separate unsupported command.
        invalid_ids = (
            "0",
            "-1",
            "+1",
            "1.5",
            "abc",
            "1 extra",
            "１２",
            "١٢",
        )

        for invalid_id in invalid_ids:
            with self.subTest(invalid_id=invalid_id):
                plan = self.planner.create_plan(f"delete interaction {invalid_id}")
                self.assertEqual(plan.steps, ())
                self.assertEqual(
                    plan.response_if_no_action,
                    "Interaction id must be a positive whole number.",
                )

    def test_history_near_prefix_and_plural_variants_remain_unsupported(self) -> None:
        requests = (
            "histories",
            "history list",
            "history 5",
            "search historyfoo",
            "searching history trip",
            "read interactions 5",
            "read interactionfoo 5",
            "reading interaction 5",
            "delete interactions 5",
            "delete interactionfoo 5",
            "deleting interaction 5",
            "delete interaction1",
            "delete history 1",
            "clear interaction 1",
            "clear history",
            "purge interaction 1",
        )

        for request in requests:
            with self.subTest(request=request):
                self._assert_unsupported(request)

    def test_system_maps_to_exact_step(self) -> None:
        self._assert_single_step("system", PlanStep("system", "overview", {}))

    def test_time_maps_to_exact_step(self) -> None:
        self._assert_single_step(
            "time",
            PlanStep("system", "current_time", {}),
        )

    def test_system_and_time_alias_variants_map_to_exact_step(self) -> None:
        overview_step = PlanStep("system", "overview", {})
        current_time_step = PlanStep("system", "current_time", {})
        cases = (
            ("system", overview_step),
            ("system info", overview_step),
            ("status", overview_step),
            ("sys", overview_step),
            ("sys info", overview_step),
            ("time", current_time_step),
            ("current time", current_time_step),
            ("time now", current_time_step),
        )

        for request, expected_step in cases:
            with self.subTest(request=request):
                self._assert_single_step(request, expected_step)

    def test_leading_and_trailing_whitespace_is_stripped_for_aliases(self) -> None:
        system_plan = self.planner.create_plan("  SYSTEM  ")
        self.assertEqual(system_plan.original_request, "SYSTEM")
        self.assertEqual(
            system_plan.steps,
            (PlanStep("system", "overview", {}),),
        )

        time_plan = self.planner.create_plan("  TIME NOW  ")
        self.assertEqual(time_plan.original_request, "TIME NOW")
        self.assertEqual(
            time_plan.steps,
            (PlanStep("system", "current_time", {}),),
        )

    def test_supported_keywords_are_case_insensitive(self) -> None:
        cases = (
            (
                "CaLc 2 + 3",
                PlanStep("calculator", "calculate", {"expression": "2 + 3"}),
            ),
            (
                "LiSt Mixed/Path Name",
                PlanStep("filesystem", "list", {"path": "Mixed/Path Name"}),
            ),
            (
                "READ FILE Mixed/Path Name",
                PlanStep("filesystem", "read", {"path": "Mixed/Path Name"}),
            ),
            (
                "ReNaMe Old Name to New Name",
                PlanStep(
                    "filesystem",
                    "rename",
                    {"source": "Old Name", "destination": "New Name"},
                ),
            ),
            (
                "MoVe Old Name to New Name",
                PlanStep(
                    "filesystem",
                    "move",
                    {"source": "Old Name", "destination": "New Name"},
                ),
            ),
            (
                "CREATE NOTE Mixed Title: Mixed Body",
                PlanStep(
                    "notes",
                    "create",
                    {"title": "Mixed Title", "body": "Mixed Body"},
                ),
            ),
            ("NoTeS", PlanStep("notes", "list", {})),
            (
                "READ NOTE Exact Mixed Title",
                PlanStep("notes", "read", {"target": "Exact Mixed Title"}),
            ),
            ("SyStEm", PlanStep("system", "overview", {})),
            ("STATUS", PlanStep("system", "overview", {})),
            ("SyS", PlanStep("system", "overview", {})),
            ("Sys Info", PlanStep("system", "overview", {})),
            ("TiMe", PlanStep("system", "current_time", {})),
            ("Current Time", PlanStep("system", "current_time", {})),
            ("Time Now", PlanStep("system", "current_time", {})),
        )

        for request, expected_step in cases:
            with self.subTest(request=request):
                self.assertEqual(
                    self.planner.create_plan(request).steps,
                    (expected_step,),
                )

    def test_operand_casing_and_internal_spacing_are_preserved(self) -> None:
        calculate = self.planner.create_plan("CALCULATE MixedCase + VALUE")
        path = self.planner.create_plan("list Folder  With  Spaces/File.TXT")
        note = self.planner.create_plan(
            "create note MiXeD Title: Body  With  Spacing"
        )

        self.assertEqual(
            calculate.steps[0].arguments,
            {"expression": "MixedCase + VALUE"},
        )
        self.assertEqual(
            path.steps[0].arguments,
            {"path": "Folder  With  Spaces/File.TXT"},
        )
        self.assertEqual(
            note.steps[0].arguments,
            {"title": "MiXeD Title", "body": "Body  With  Spacing"},
        )

    def test_paths_with_ordinary_spaces_are_preserved(self) -> None:
        cases = (
            (
                "read file Project Files/Release Notes.txt",
                {"path": "Project Files/Release Notes.txt"},
            ),
            (
                "rename Project Files/Draft One.txt to Archive/Final One.txt",
                {
                    "source": "Project Files/Draft One.txt",
                    "destination": "Archive/Final One.txt",
                },
            ),
            (
                "move Project Files/Final One.txt to Shipped Files/Final One.txt",
                {
                    "source": "Project Files/Final One.txt",
                    "destination": "Shipped Files/Final One.txt",
                },
            ),
        )

        for request, expected_arguments in cases:
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(plan.steps[0].arguments, expected_arguments)

    def test_quote_characters_remain_literal_path_characters(self) -> None:
        listed = self.planner.create_plan('list "Project Files"')
        renamed = self.planner.create_plan(
            'rename "Draft One.txt" to "Final One.txt"'
        )

        self.assertEqual(listed.steps[0].arguments, {"path": '"Project Files"'})
        self.assertEqual(
            renamed.steps[0].arguments,
            {
                "source": '"Draft One.txt"',
                "destination": '"Final One.txt"',
            },
        )

    def test_bare_commands_keep_the_current_unsupported_response(self) -> None:
        requests = (
            "calculate",
            "list",
            "read file",
            "create note",
            "read note",
        )

        for request in requests:
            with self.subTest(request=request):
                self._assert_unsupported(request)

    def test_malformed_rename_and_move_return_exact_usage(self) -> None:
        cases = (
            ("rename source-only", "Use: rename <source> to <destination>"),
            ("rename to destination-only", "Use: rename <source> to <destination>"),
            ("move source-only", "Use: move <source> to <destination>"),
            ("move to destination-only", "Use: move <source> to <destination>"),
        )

        for request, expected_response in cases:
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(plan.steps, ())
                self.assertEqual(plan.response_if_no_action, expected_response)

    def test_near_prefix_commands_do_not_become_executable(self) -> None:
        requests = (
            "calculator 2 + 2",
            "listing reports",
            "reader file.txt",
            "systematic report",
            "systeminfo",
            "sysinfo",
            "syscall",
            "timer",
            "timestamp",
            "sys  info",
            "time  now",
            "system info now",
            "sys info now",
        )

        for request in requests:
            with self.subTest(request=request):
                self._assert_unsupported(request)

    def test_rename_and_move_require_the_exact_lowercase_to_delimiter(self) -> None:
        supported = (
            ("rename Alpha to Beta", "rename"),
            ("move Alpha to Beta", "move"),
        )
        unsupported = (
            (
                "rename Alpha TO Beta",
                "Use: rename <source> to <destination>",
            ),
            (
                "move Alpha TO Beta",
                "Use: move <source> to <destination>",
            ),
        )

        for request, action in supported:
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(plan.steps[0].action, action)
                self.assertEqual(
                    plan.steps[0].arguments,
                    {"source": "Alpha", "destination": "Beta"},
                )

        for request, expected_response in unsupported:
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(plan.steps, ())
                self.assertEqual(plan.response_if_no_action, expected_response)

    def test_conversational_multi_action_requests_do_not_add_planning(self) -> None:
        self._assert_unsupported("please calculate 2 + 2 and then show time")
        self._assert_unsupported("system and then time")

        strict_first_command = self.planner.create_plan(
            "calculate 2 + 2 and then show time"
        )
        self.assertEqual(
            strict_first_command.steps,
            (
                PlanStep(
                    "calculator",
                    "calculate",
                    {"expression": "2 + 2 and then show time"},
                ),
            ),
        )

    def test_help_retains_every_canonical_command(self) -> None:
        plan = self.planner.create_plan("HELP")
        response = plan.response_if_no_action
        self.assertIsNotNone(response)
        assert response is not None
        expected_lines = (
            "- calculate <expression>",
            "- list <path>",
            "- read file <path>",
            "- rename <source> to <destination>",
            "- move <source> to <destination>",
            "- create note <title>: <body>",
            "- notes",
            "- read note <id or exact title>",
            "- retitle note <id>: <new title>",
            "- edit note <id>: <new body>",
            "- delete note <id>",
            "- remember <fact>",
            "- recall <query>",
            "- memories",
            "- system",
            "- time",
            "- history",
            "- search history <query>",
            "- read interaction <id>",
            "- delete interaction <id>",
        )

        self.assertEqual(plan.steps, ())
        for command_line in expected_lines:
            with self.subTest(command=command_line):
                self.assertIn(command_line, response.splitlines())
        self.assertEqual(
            response.splitlines().count("- delete interaction <id>"),
            1,
        )
        for command_line in (
            "- retitle note <id>: <new title>",
            "- edit note <id>: <new body>",
            "- delete note <id>",
        ):
            with self.subTest(command=command_line):
                self.assertEqual(response.splitlines().count(command_line), 1)

    def _assert_single_step(self, request: str, expected_step: PlanStep) -> None:
        """Assert the complete public plan for one deterministic command."""
        self.assertEqual(
            self.planner.create_plan(request),
            Plan(original_request=request, steps=(expected_step,)),
        )

    def _assert_unsupported(self, request: str) -> None:
        """Assert Maria's exact unsupported-plan response."""
        self.assertEqual(
            self.planner.create_plan(request),
            Plan(
                original_request=request,
                steps=(),
                response_if_no_action=UNSUPPORTED_RESPONSE,
            ),
        )


if __name__ == "__main__":
    unittest.main()
