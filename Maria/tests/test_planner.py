"""Focused tests for Maria's strict command planner."""

from __future__ import annotations

import logging
import unittest
from datetime import datetime, timezone
from typing import Any

from core.planner import (
    ADD_REMINDER_USAGE,
    ADD_TASK_DUE_USAGE,
    ADD_TASK_USAGE,
    REMINDER_ID_USAGE_MESSAGE,
    RESCHEDULE_REMINDER_USAGE,
    RESCHEDULE_TASK_USAGE,
    TASK_ID_USAGE_MESSAGE,
    TIMING_UNAVAILABLE_MESSAGE,
    Plan,
    Planner,
    PlanStep,
)
from core.task_records import TASK_TEXT_MESSAGE
from core.time_resolution import TimingContext, TimingFailure
from tests.zone_fixtures import CENTRAL_RULES, SYNTHETIC_KEY, SyntheticZones


UNSUPPORTED_RESPONSE = (
    "I do not have an executable plan for that yet. "
    "Type 'help' to see supported commands."
)


class PlannerMemoryTests(unittest.TestCase):
    """Verify memory grammar and regression coverage for existing commands."""

    def setUp(self) -> None:
        self.planner = Planner(logger=_quiet_logger())

    def test_remember_maps_to_store_step(self) -> None:
        plan = self.planner.create_plan("remember My editor is VS Code")

        self.assertEqual(
            plan,
            Plan(
                original_request="remember My editor is VS Code",
                steps=(
                    PlanStep(
                        tool_name="memory",
                        action="store",
                        arguments={"fact": "My editor is VS Code"},
                    ),
                ),
            ),
        )

    def test_recall_maps_to_search_step(self) -> None:
        plan = self.planner.create_plan("recall editor")

        self.assertEqual(
            plan,
            Plan(
                original_request="recall editor",
                steps=(
                    PlanStep(
                        tool_name="memory",
                        action="search",
                        arguments={"query": "editor"},
                    ),
                ),
            ),
        )

    def test_memories_maps_to_list_step(self) -> None:
        plan = self.planner.create_plan("memories")

        self.assertEqual(
            plan,
            Plan(
                original_request="memories",
                steps=(PlanStep("memory", "list", {}),),
            ),
        )

    def test_memory_command_keywords_are_case_insensitive(self) -> None:
        cases = (
            ("REMEMBER One Fact", PlanStep("memory", "store", {"fact": "One Fact"})),
            ("ReCaLl One", PlanStep("memory", "search", {"query": "One"})),
            ("MEMORIES", PlanStep("memory", "list", {})),
        )

        for request, expected_step in cases:
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(plan.steps, (expected_step,))

    def test_fact_and_query_preserve_original_casing(self) -> None:
        remember = self.planner.create_plan("Remember My EDITOR is VS Code")
        recall = self.planner.create_plan("RECALL My EdItOr")
        token = self.planner.create_plan(
            "Remember that my token is MARIA-V01-CLI-01"
        )

        self.assertEqual(
            remember.steps[0].arguments,
            {"fact": "My EDITOR is VS Code"},
        )
        self.assertEqual(recall.steps[0].arguments, {"query": "My EdItOr"})
        self.assertEqual(
            token.steps[0].arguments,
            {"fact": "that my token is MARIA-V01-CLI-01"},
        )

    def test_multiple_whitespace_characters_separate_keyword_and_operand(self) -> None:
        remember = self.planner.create_plan("remember \t  My Mixed Case Fact")
        recall = self.planner.create_plan("recall\n  Mixed Case")

        self.assertEqual(
            remember.steps,
            (PlanStep("memory", "store", {"fact": "My Mixed Case Fact"}),),
        )
        self.assertEqual(
            recall.steps,
            (PlanStep("memory", "search", {"query": "Mixed Case"}),),
        )

    def test_bare_remember_returns_exact_usage_response(self) -> None:
        plan = self.planner.create_plan("  REMEMBER \t")

        self.assertEqual(plan.original_request, "REMEMBER")
        self.assertEqual(plan.steps, ())
        self.assertEqual(plan.response_if_no_action, "Use: remember <fact>")

    def test_bare_recall_returns_exact_usage_response(self) -> None:
        plan = self.planner.create_plan("\nrecall   ")

        self.assertEqual(plan.original_request, "recall")
        self.assertEqual(plan.steps, ())
        self.assertEqual(plan.response_if_no_action, "Use: recall <query>")

    def test_remembering_is_not_a_remember_command(self) -> None:
        self._assert_unsupported("remembering something")

    def test_memories_with_an_operand_is_unsupported(self) -> None:
        self._assert_unsupported("memories extra")

    def test_conversational_variants_remain_unsupported(self) -> None:
        requests = (
            "please remember My editor is VS Code",
            "what do you remember about my editor",
            "do you recall my editor",
        )

        for request in requests:
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(plan.steps, ())
                self.assertEqual(plan.response_if_no_action, UNSUPPORTED_RESPONSE)

    def test_representative_existing_commands_map_exactly_as_before(self) -> None:
        cases: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
            ("calculate 2 + 2", "calculator", "calculate", {"expression": "2 + 2"}),
            ("notes", "notes", "list", {}),
            ("read note Project Plan", "notes", "read", {"target": "Project Plan"}),
            ("read file README.md", "filesystem", "read", {"path": "README.md"}),
            ("system", "system", "overview", {}),
            ("status", "system", "overview", {}),
            ("sys", "system", "overview", {}),
            ("time", "system", "current_time", {}),
            ("current time", "system", "current_time", {}),
            ("time now", "system", "current_time", {}),
        )

        for request, tool_name, action, arguments in cases:
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(
                    plan,
                    Plan(
                        original_request=request,
                        steps=(PlanStep(tool_name, action, arguments),),
                    ),
                )

    def test_unknown_command_fallback_text_is_unchanged(self) -> None:
        plan = self.planner.create_plan("sing a song")

        self.assertEqual(
            plan,
            Plan(
                original_request="sing a song",
                steps=(),
                response_if_no_action=UNSUPPORTED_RESPONSE,
            ),
        )

    def test_help_includes_all_old_and_new_commands(self) -> None:
        plan = self.planner.create_plan("help")
        expected_commands = {
            "- calculate <expression>",
            "- list <path>",
            "- read file <path>",
            "- rename <source> to <destination>",
            "- move <source> to <destination>",
            "- organize folder <folder>",
            "- create note <title>: <body>",
            "- notes",
            "- read note <id or exact title>",
            "- retitle note <id>: <new title>",
            "- edit note <id>: <new body>",
            "- delete note <id>",
            "- enable note context <id>",
            "- disable note context <id>",
            "- remember <fact>",
            "- recall <query>",
            "- memories",
            "- update memory <id>: <new fact>",
            "- forget memory <id>",
            "- enable memory context <id>",
            "- disable memory context <id>",
            "- save conversation",
            "- conversations",
            "- read conversation <id>",
            "- delete conversation <id>",
            "- summarize conversation",
            "- summaries",
            "- read summary <id>",
            "- edit summary <id>: <new text>",
            "- delete summary <id>",
            "- enable summary context <id>",
            "- disable summary context <id>",
            # V1-22's twenty-six task and reminder commands.
            "- add task <text>",
            "- add task due <date>: <text>",
            "- tasks",
            "- completed tasks",
            "- due tasks",
            "- read task <id>",
            "- complete task <id>",
            "- reopen task <id>",
            "- edit task <id>: <new text>",
            "- reschedule task <id>: <date or none>",
            "- delete task <id>",
            "- enable task context <id>",
            "- disable task context <id>",
            "- add reminder <date> <time> [<offset>]: <text>",
            "- add reminder in <N> minutes|hours: <text>",
            "- reminders",
            "- completed reminders",
            "- due reminders",
            "- read reminder <id>",
            "- complete reminder <id>",
            "- reopen reminder <id>",
            "- edit reminder <id>: <new text>",
            "- reschedule reminder <id>: <date> <time> [<offset>] or in <N> "
            "minutes|hours",
            "- delete reminder <id>",
            "- enable reminder context <id>",
            "- disable reminder context <id>",
            "- reminder delivery",
            "- enable reminder delivery",
            "- disable reminder delivery",
            # V1-24-R1: one line per project command and one for references.
            "- projects",
            "- create project <name>: workspace/<folder> or project_maria",
            "- read project <id>",
            "- retitle project <id>: <new name>",
            "- delete project <id>",
            "- activate project <id>",
            "- deactivate project",
            "- project facts <project id>",
            "- add project fact <project id>: <text>",
            "- edit project fact <fact id>: <new text>",
            "- delete project fact <fact id>",
            "- project files [<folder>]",
            "- read project file <path> [lines <from>-<to>]",
            "- @<path> or @<path>:<from>-<to> in a question names an active "
            "project's file",
            "- system",
            "- time",
            "- history",
            "- search history <query>",
            "- read interaction <id>",
            "- delete interaction <id>",
            "- audit",
            "- read audit <id>",
            "- delete audit <id>",
        }

        self.assertEqual(plan.steps, ())
        lines = plan.response_if_no_action.splitlines()
        self.assertEqual(lines[0], "Supported commands:")
        self.assertEqual(set(lines[1:]), expected_commands)
        self.assertEqual(len(lines[1:]), len(expected_commands))
        for command_line in (
            "- retitle note <id>: <new title>",
            "- edit note <id>: <new body>",
            "- delete note <id>",
            "- update memory <id>: <new fact>",
            "- forget memory <id>",
        ):
            with self.subTest(command=command_line):
                self.assertEqual(lines.count(command_line), 1)

    def test_update_memory_maps_to_update_step(self) -> None:
        plan = self.planner.create_plan("update memory 5: My editor is Neovim")

        self.assertEqual(
            plan,
            Plan(
                original_request="update memory 5: My editor is Neovim",
                steps=(
                    PlanStep(
                        tool_name="memory",
                        action="update",
                        arguments={"id": 5, "fact": "My editor is Neovim"},
                    ),
                ),
            ),
        )

    def test_forget_memory_maps_to_forget_step(self) -> None:
        plan = self.planner.create_plan("forget memory 5")

        self.assertEqual(
            plan,
            Plan(
                original_request="forget memory 5",
                steps=(
                    PlanStep(
                        tool_name="memory",
                        action="forget",
                        arguments={"id": 5},
                    ),
                ),
            ),
        )

    def test_memory_lifecycle_keywords_are_case_insensitive(self) -> None:
        cases = (
            (
                "UPDATE MEMORY 5: New Fact",
                PlanStep("memory", "update", {"id": 5, "fact": "New Fact"}),
            ),
            (
                "Forget Memory 5",
                PlanStep("memory", "forget", {"id": 5}),
            ),
        )

        for request, expected_step in cases:
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(plan.original_request, request)
                self.assertEqual(plan.steps, (expected_step,))

    def test_memory_lifecycle_accepts_leading_zeroes(self) -> None:
        cases = (
            (
                "update memory 005: New fact",
                PlanStep("memory", "update", {"id": 5, "fact": "New fact"}),
            ),
            ("forget memory 007", PlanStep("memory", "forget", {"id": 7})),
        )

        for request, expected_step in cases:
            with self.subTest(request=request):
                self.assertEqual(
                    self.planner.create_plan(request).steps, (expected_step,)
                )

    def test_update_memory_splits_on_the_first_colon_only(self) -> None:
        plan = self.planner.create_plan("update memory 005: New: fact")

        self.assertEqual(
            plan.steps,
            (PlanStep("memory", "update", {"id": 5, "fact": "New: fact"}),),
        )

    def test_update_memory_preserves_casing_and_internal_spacing(self) -> None:
        plan = self.planner.create_plan("update memory 5:   My   Mixed   Fact   ")

        self.assertEqual(
            plan.original_request, "update memory 5:   My   Mixed   Fact"
        )
        self.assertEqual(
            plan.steps,
            (PlanStep("memory", "update", {"id": 5, "fact": "My   Mixed   Fact"}),),
        )

    def test_memory_lifecycle_bare_and_incomplete_commands_return_usage(self) -> None:
        cases = (
            ("update memory", "Use: update memory <id>: <new fact>"),
            ("update memory   ", "Use: update memory <id>: <new fact>"),
            ("UPDATE MEMORY", "Use: update memory <id>: <new fact>"),
            ("update memory 5", "Use: update memory <id>: <new fact>"),
            ("update memory 5:", "Use: update memory <id>: <new fact>"),
            ("update memory 5:    ", "Use: update memory <id>: <new fact>"),
            ("update memory abc:", "Use: update memory <id>: <new fact>"),
            ("forget memory", "Use: forget memory <id>"),
            ("forget memory   ", "Use: forget memory <id>"),
            ("FORGET MEMORY", "Use: forget memory <id>"),
        )

        for request, expected_response in cases:
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(plan.steps, ())
                self.assertEqual(plan.response_if_no_action, expected_response)

    def test_memory_lifecycle_rejects_invalid_ids(self) -> None:
        invalid_ids = (
            "0",
            "-1",
            "+1",
            "1.5",
            "abc",
            "5 extra",
            "²",
            "１２",
            "١٢",
        )

        for invalid_id in invalid_ids:
            for request in (
                f"update memory {invalid_id}: Fact",
                f"forget memory {invalid_id}",
            ):
                with self.subTest(request=request):
                    plan = self.planner.create_plan(request)
                    self.assertEqual(plan.steps, ())
                    self.assertEqual(
                        plan.response_if_no_action,
                        "Memory id must be a positive whole number.",
                    )

    def test_update_memory_blank_id_with_content_is_an_invalid_id(self) -> None:
        plan = self.planner.create_plan("update memory : Fact")

        self.assertEqual(plan.steps, ())
        self.assertEqual(
            plan.response_if_no_action,
            "Memory id must be a positive whole number.",
        )

    def test_memory_lifecycle_never_reports_the_direct_fact_validation_message(
        self,
    ) -> None:
        for request in ("update memory 5:", "update memory 5:   "):
            with self.subTest(request=request):
                self.assertNotEqual(
                    self.planner.create_plan(request).response_if_no_action,
                    "New memory fact is required.",
                )

    def test_memory_lifecycle_near_prefix_variants_remain_unsupported(self) -> None:
        requests = (
            "update memories 1: Fact",
            "forget memories 1",
            "update memory1: Fact",
            "forget memory1",
            "delete memory 1",
            "clear memories",
            "purge memory 1",
            "updating memory 1: Fact",
            "forgetting memory 1",
            "please forget memory 1",
            "update memory\t5: Fact",
        )

        for request in requests:
            with self.subTest(request=request):
                self._assert_unsupported(request)

    def test_consent_commands_map_to_one_step_with_one_id(self) -> None:
        cases = (
            ("enable memory context 3", "enable_context", 3),
            ("ENABLE Memory Context 12", "enable_context", 12),
            ("enable memory context   007  ", "enable_context", 7),
            ("disable memory context 3", "disable_context", 3),
            ("Disable MEMORY context 41", "disable_context", 41),
        )
        for request, action, memory_id in cases:
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)

                self.assertEqual(
                    plan.steps,
                    (PlanStep("memory", action, {"id": memory_id}),),
                )
                self.assertIs(type(plan.steps[0].arguments["id"]), int)

    def test_consent_commands_without_an_id_answer_with_their_usage(self) -> None:
        for verb in ("enable", "disable"):
            with self.subTest(verb=verb):
                plan = self.planner.create_plan(f"{verb} memory context")

                self.assertEqual(plan.steps, ())
                self.assertEqual(
                    plan.response_if_no_action,
                    f"Use: {verb} memory context <id>",
                )

    def test_consent_commands_refuse_anything_but_a_positive_ascii_id(self) -> None:
        for verb in ("enable", "disable"):
            for operand in ("0", "-1", "+1", "1.0", "abc", "1 2", "٣", "１"):
                request = f"{verb} memory context {operand}"
                with self.subTest(request=request):
                    plan = self.planner.create_plan(request)

                    self.assertEqual(plan.steps, ())
                    self.assertEqual(
                        plan.response_if_no_action,
                        "Memory id must be a positive whole number.",
                    )

    def test_near_miss_consent_phrasings_remain_unsupported(self) -> None:
        for request in (
            "enable context for memory 3",
            "enable memory 3",
            "enable memory contexts 3",
            "please enable memory context 3",
            "turn on memory context 3",
        ):
            with self.subTest(request=request):
                self._assert_unsupported(request)

    def test_create_plan_accepts_the_request_text_and_nothing_else(self) -> None:
        with self.assertRaises(TypeError):
            self.planner.create_plan("help", context={"recent_interactions": []})

    def _assert_unsupported(self, request: str) -> None:
        """Assert that a request receives the unchanged fallback."""
        plan = self.planner.create_plan(request)
        self.assertEqual(plan.steps, ())
        self.assertEqual(plan.response_if_no_action, UNSUPPORTED_RESPONSE)


class ConversationRecordCommandTests(unittest.TestCase):
    """Verify V1-20's eleven commands and the grammar they leave alone."""

    def setUp(self) -> None:
        self.planner = Planner(logger=_quiet_logger())

    def test_each_command_maps_to_one_step_of_its_own_action(self) -> None:
        for request, tool_name, action, arguments in (
            ("save conversation", "conversations", "save", {}),
            ("conversations", "conversations", "list", {}),
            ("read conversation 3", "conversations", "read", {"id": 3}),
            ("delete conversation 3", "conversations", "delete", {"id": 3}),
            ("summarize conversation", "conversation_summaries", "create", {}),
            ("summaries", "conversation_summaries", "list", {}),
            ("read summary 4", "conversation_summaries", "read", {"id": 4}),
            (
                "edit summary 4: New text.",
                "conversation_summaries",
                "edit",
                {"id": 4, "text": "New text."},
            ),
            ("delete summary 4", "conversation_summaries", "delete", {"id": 4}),
            (
                "enable summary context 4",
                "conversation_summaries",
                "enable_context",
                {"id": 4},
            ),
            (
                "disable summary context 4",
                "conversation_summaries",
                "disable_context",
                {"id": 4},
            ),
        ):
            with self.subTest(request=request):
                self.assertEqual(
                    self.planner.create_plan(request),
                    Plan(
                        original_request=request,
                        steps=(PlanStep(tool_name, action, arguments),),
                    ),
                )

    def test_the_argumentless_commands_match_whole_requests_in_any_case(
        self,
    ) -> None:
        for request, action in (
            ("SAVE CONVERSATION", "save"),
            ("Save Conversation", "save"),
            ("Summarize Conversation", "create"),
        ):
            with self.subTest(request=request):
                self.assertEqual(
                    [step.action for step in self.planner.create_plan(request).steps],
                    [action],
                )

    def test_conversational_phrasings_still_reach_the_fallback(self) -> None:
        for request in (
            "save the conversation",
            "please save conversation",
            "save conversation now",
            "save conversations",
            "summarize the conversation",
            "summarize conversation please",
            "summarise conversation",
            "conversation",
            "summary",
        ):
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(plan.steps, ())
                self.assertEqual(plan.response_if_no_action, UNSUPPORTED_RESPONSE)

    def test_list_summaries_stays_a_directory_listing(self) -> None:
        self.assertEqual(
            self.planner.create_plan("list summaries"),
            Plan(
                original_request="list summaries",
                steps=(PlanStep("filesystem", "list", {"path": "summaries"}),),
            ),
        )

    def test_bare_and_malformed_forms_answer_with_their_usage(self) -> None:
        for request, usage in (
            ("read conversation", "Use: read conversation <id>"),
            ("delete conversation", "Use: delete conversation <id>"),
            ("read summary", "Use: read summary <id>"),
            ("edit summary", "Use: edit summary <id>: <new text>"),
            ("edit summary 4", "Use: edit summary <id>: <new text>"),
            ("edit summary 4:", "Use: edit summary <id>: <new text>"),
            ("delete summary", "Use: delete summary <id>"),
            ("enable summary context", "Use: enable summary context <id>"),
            ("disable summary context", "Use: disable summary context <id>"),
        ):
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(plan.steps, ())
                self.assertEqual(plan.response_if_no_action, usage)


# V1-22's reference: Saturday 2027-10-30, 10:03:27 local at +02:00 in the
# synthetic central rules, the day before the local hour from 02:00 repeats.
TIMING_REFERENCE = datetime(2027, 10, 30, 8, 3, 27, tzinfo=timezone.utc)
ZONE = f"[{SYNTHETIC_KEY}]"
NOTHING_SAVED = " Nothing was saved."


class TaskAndReminderCommandTests(unittest.TestCase):
    """Verify V1-22's twenty-six commands and the grammar they leave alone.

    Each timing command resolves its phrase once, against the request's own
    reference instant and configured zone, so the plan carries canonical
    values; a synthetic zone keeps every expectation exact.
    """

    def setUp(self) -> None:
        self.planner = Planner(logger=_quiet_logger())
        SyntheticZones(self, {SYNTHETIC_KEY: CENTRAL_RULES})
        self.timing = TimingContext(TIMING_REFERENCE, SYNTHETIC_KEY)

    def plan(self, request: str, timing: object = None) -> Plan:
        return self.planner.create_plan(
            request,
            timing=self.timing if timing is None else timing,
        )

    def assertNoAction(self, request: str, response: str, **kwargs: Any) -> None:
        plan = self.plan(request, **kwargs)
        self.assertEqual(plan.steps, ())
        self.assertEqual(plan.response_if_no_action, response)

    def test_each_command_maps_to_one_step_of_its_own_action(self) -> None:
        for request, tool_name, action, arguments in (
            ("tasks", "tasks", "list", {"status": "open"}),
            ("completed tasks", "tasks", "list", {"status": "completed"}),
            ("due tasks", "tasks", "due", {"as_of": "2027-10-30"}),
            ("add task buy milk", "tasks", "create", {"text": "buy milk"}),
            (
                "add task due tomorrow: buy milk",
                "tasks",
                "create",
                {"text": "buy milk", "due": "2027-10-31"},
            ),
            (
                "add task due friday: file taxes",
                "tasks",
                "create",
                {"text": "file taxes", "due": "2027-11-05"},
            ),
            (
                "add task due in 3 days: call the bank",
                "tasks",
                "create",
                {"text": "call the bank", "due": "2027-11-02"},
            ),
            (
                "add task due today: water the plants",
                "tasks",
                "create",
                {"text": "water the plants", "due": "2027-10-30"},
            ),
            ("read task 3", "tasks", "read", {"id": 3}),
            ("complete task 3", "tasks", "complete", {"id": 3}),
            ("reopen task 3", "tasks", "reopen", {"id": 3}),
            (
                "edit task 3: buy oat milk",
                "tasks",
                "edit",
                {"id": 3, "text": "buy oat milk"},
            ),
            (
                "reschedule task 3: 2027-12-31",
                "tasks",
                "reschedule",
                {"id": 3, "due": "2027-12-31"},
            ),
            (
                "reschedule task 3: none",
                "tasks",
                "reschedule",
                {"id": 3, "due": "none"},
            ),
            ("delete task 3", "tasks", "delete", {"id": 3}),
            ("enable task context 3", "tasks", "enable_context", {"id": 3}),
            ("disable task context 3", "tasks", "disable_context", {"id": 3}),
            ("reminders", "reminders", "list", {"status": "open"}),
            ("completed reminders", "reminders", "list", {"status": "completed"}),
            (
                "due reminders",
                "reminders",
                "due",
                {"as_of": "2027-10-30T08:03:27Z"},
            ),
            (
                "add reminder tomorrow 9:00: call mom",
                "reminders",
                "create",
                {
                    "text": "call mom",
                    "when": f"2027-10-31T09:00:00+01:00{ZONE}",
                    "repeat": "never",
                },
            ),
            (
                "add reminder tomorrow 02:30 +02:00: check the clocks",
                "reminders",
                "create",
                {
                    "text": "check the clocks",
                    "when": f"2027-10-31T02:30:00+02:00{ZONE}",
                    "repeat": "never",
                },
            ),
            (
                "add reminder in 20 minutes: stretch",
                "reminders",
                "create",
                {
                    "text": "stretch",
                    "when": f"2027-10-30T10:23:00+02:00{ZONE}",
                    "repeat": "never",
                },
            ),
            (
                "add reminder 2027-11-02 18:30: call: dad",
                "reminders",
                "create",
                {
                    "text": "call: dad",
                    "when": f"2027-11-02T18:30:00+01:00{ZONE}",
                    "repeat": "never",
                },
            ),
            ("read reminder 4", "reminders", "read", {"id": 4}),
            ("complete reminder 4", "reminders", "complete", {"id": 4}),
            ("reopen reminder 4", "reminders", "reopen", {"id": 4}),
            (
                "edit reminder 4: call mom back",
                "reminders",
                "edit",
                {"id": 4, "text": "call mom back"},
            ),
            (
                "reschedule reminder 4: monday 08:00",
                "reminders",
                "reschedule",
                {
                    "id": 4,
                    "when": f"2027-11-01T08:00:00+01:00{ZONE}",
                    "repeat": "never",
                },
            ),
            (
                "reschedule reminder 4: in 2 hours",
                "reminders",
                "reschedule",
                {
                    "id": 4,
                    "when": f"2027-10-30T12:03:00+02:00{ZONE}",
                    "repeat": "never",
                },
            ),
            ("delete reminder 4", "reminders", "delete", {"id": 4}),
            ("enable reminder context 4", "reminders", "enable_context", {"id": 4}),
            ("disable reminder context 4", "reminders", "disable_context", {"id": 4}),
        ):
            with self.subTest(request=request):
                self.assertEqual(
                    self.plan(request),
                    Plan(
                        original_request=request,
                        steps=(PlanStep(tool_name, action, arguments),),
                    ),
                )

    def test_keywords_match_in_any_case_and_text_keeps_its_own(self) -> None:
        for request, action, arguments in (
            ("TASKS", "list", {"status": "open"}),
            ("Due Tasks", "due", {"as_of": "2027-10-30"}),
            ("Add Task Buy Milk", "create", {"text": "Buy Milk"}),
            (
                "ADD TASK DUE Tomorrow: Buy Milk",
                "create",
                {"text": "Buy Milk", "due": "2027-10-31"},
            ),
            (
                "Add Reminder Tomorrow 9:00: Call Mom",
                "create",
                {
                    "text": "Call Mom",
                    "when": f"2027-10-31T09:00:00+01:00{ZONE}",
                    "repeat": "never",
                },
            ),
            ("Reschedule Task 3: NONE", "reschedule", {"id": 3, "due": "none"}),
            ("READ REMINDER 007", "read", {"id": 7}),
        ):
            with self.subTest(request=request):
                (step,) = self.plan(request).steps
                self.assertEqual(
                    (step.action, dict(step.arguments)),
                    (action, arguments),
                )

    def test_listings_and_filesystem_verbs_are_unchanged(self) -> None:
        for request, action, arguments in (
            ("list tasks", "list", {"path": "tasks"}),
            ("ls reminders", "list", {"path": "reminders"}),
            ("list todo", "list", {"path": "todo"}),
            (
                "move task 3 to tomorrow",
                "move",
                {"source": "task 3", "destination": "tomorrow"},
            ),
        ):
            with self.subTest(request=request):
                self.assertEqual(
                    self.plan(request),
                    Plan(
                        original_request=request,
                        steps=(PlanStep("filesystem", action, arguments),),
                    ),
                )

    def test_organize_folder_plans_one_filesystem_step_with_the_folder_text(self) -> None:
        for request, folder in (
            ("organize folder inbox", "inbox"),
            ("Organize Folder Inbox", "Inbox"),
            ("ORGANIZE FOLDER docs/old files", "docs/old files"),
            ("organize folder .", "."),
        ):
            with self.subTest(request=request):
                self.assertEqual(
                    self.plan(request),
                    Plan(
                        original_request=request,
                        steps=(PlanStep("filesystem", "organize", {"folder": folder}),),
                    ),
                )

    def test_organize_folder_without_a_folder_returns_its_usage(self) -> None:
        for request in ("organize folder", "Organize Folder", "organize folder   "):
            with self.subTest(request=request):
                plan = self.plan(request)
                self.assertEqual(plan.steps, ())
                self.assertEqual(
                    plan.response_if_no_action,
                    "Use: organize folder <folder>",
                )

    def test_near_organize_forms_are_not_commands(self) -> None:
        for request in (
            "organize",
            "organize files",
            "organize my folder inbox",
            "organizefolder inbox",
            "please organize folder inbox",
            "organize  folder inbox",
            "reorganize folder inbox",
        ):
            with self.subTest(request=request):
                self.assertEqual(
                    self.plan(request),
                    Plan(
                        original_request=request,
                        steps=(),
                        response_if_no_action=UNSUPPORTED_RESPONSE,
                    ),
                )

    def test_the_request_forms_reach_the_generic_fallback(self) -> None:
        for request in (
            "remind me tomorrow at 09:00 to call mom",
            "Remind me in 20 minutes to stretch",
            "remind me to call mom tomorrow at 9:00",
            "add a task to buy milk",
            "add a task to file taxes by friday",
            "remind me",
            "add a task to",
            "task",
            "reminder",
            "todo",
            "add todo buy milk",
            "please add task buy milk",
            "show tasks",
            "tasks due",
        ):
            with self.subTest(request=request):
                self.assertNoAction(request, UNSUPPORTED_RESPONSE)

    def test_bare_and_malformed_forms_answer_with_their_usage(self) -> None:
        for request, usage in (
            ("add task", ADD_TASK_USAGE),
            ("add task due tomorrow:", ADD_TASK_DUE_USAGE),
            ("add task due someday: buy milk", ADD_TASK_DUE_USAGE),
            ("add task due 2027-02-30: buy milk", ADD_TASK_DUE_USAGE),
            ("add task due tomorrow 9:00: buy milk", ADD_TASK_DUE_USAGE),
            ("read task", "Use: read task <id>"),
            ("complete task", "Use: complete task <id>"),
            ("reopen task", "Use: reopen task <id>"),
            ("delete task", "Use: delete task <id>"),
            ("enable task context", "Use: enable task context <id>"),
            ("disable task context", "Use: disable task context <id>"),
            ("edit task", "Use: edit task <id>: <new text>"),
            ("edit task 3", "Use: edit task <id>: <new text>"),
            ("edit task 3:", "Use: edit task <id>: <new text>"),
            ("reschedule task", RESCHEDULE_TASK_USAGE),
            ("reschedule task 3:", RESCHEDULE_TASK_USAGE),
            ("reschedule task 3: someday", RESCHEDULE_TASK_USAGE),
            ("add reminder", ADD_REMINDER_USAGE),
            ("add reminder tomorrow: call mom", ADD_REMINDER_USAGE),
            ("add reminder tomorrow 9:00", ADD_REMINDER_USAGE),
            ("add reminder tomorrow 9:00:", ADD_REMINDER_USAGE),
            ("add reminder tomorrow at 9:00: call mom", ADD_REMINDER_USAGE),
            ("add reminder tomorrow 9am: call mom", ADD_REMINDER_USAGE),
            ("add reminder in 1441 minutes: stretch", ADD_REMINDER_USAGE),
            ("add reminder tomorrow 9:00 CET: call mom", ADD_REMINDER_USAGE),
            ("read reminder", "Use: read reminder <id>"),
            ("complete reminder", "Use: complete reminder <id>"),
            ("reopen reminder", "Use: reopen reminder <id>"),
            ("delete reminder", "Use: delete reminder <id>"),
            ("enable reminder context", "Use: enable reminder context <id>"),
            ("disable reminder context", "Use: disable reminder context <id>"),
            ("edit reminder", "Use: edit reminder <id>: <new text>"),
            ("edit reminder 4", "Use: edit reminder <id>: <new text>"),
            ("reschedule reminder", RESCHEDULE_REMINDER_USAGE),
            ("reschedule reminder 4: tomorrow", RESCHEDULE_REMINDER_USAGE),
            ("reschedule reminder 4: none", RESCHEDULE_REMINDER_USAGE),
        ):
            with self.subTest(request=request):
                self.assertNoAction(request, usage)

    def test_usage_and_timing_replies_are_exactly_this_text(self) -> None:
        # Owner-visible fixed text, pinned word for word.
        for constant, text in (
            (ADD_TASK_USAGE, "Use: add task <text> or add task due <date>: <text>"),
            (
                ADD_TASK_DUE_USAGE,
                "Use: add task due <date>: <text>, where <date> is today, "
                "tomorrow, a weekday, YYYY-MM-DD, or in <N> days",
            ),
            (
                ADD_REMINDER_USAGE,
                "Use: add reminder <date> <time> [<offset>]: <text> or add "
                "reminder in <N> minutes|hours: <text>, where <date> is today, "
                "tomorrow, a weekday, YYYY-MM-DD, or in <N> days and <time> is "
                "HH:MM",
            ),
            (
                RESCHEDULE_TASK_USAGE,
                "Use: reschedule task <id>: <date or none>, where <date> is "
                "today, tomorrow, a weekday, YYYY-MM-DD, or in <N> days",
            ),
            (
                RESCHEDULE_REMINDER_USAGE,
                "Use: reschedule reminder <id>: <date> <time> [<offset>] or in "
                "<N> minutes|hours",
            ),
            (TASK_ID_USAGE_MESSAGE, "Task id must be a positive whole number."),
            (
                REMINDER_ID_USAGE_MESSAGE,
                "Reminder id must be a positive whole number.",
            ),
            (
                TIMING_UNAVAILABLE_MESSAGE,
                "Maria cannot resolve dates and times for this request.",
            ),
        ):
            with self.subTest(text=text[:30]):
                self.assertEqual(constant, text)

    def test_ids_are_positive_ascii_whole_numbers(self) -> None:
        for operand in ("0", "-1", "abc", "1.5", chr(0x0661), "1 2"):
            for words in ("read", "complete", "reopen", "delete"):
                with self.subTest(command=f"{words} task {operand}"):
                    self.assertNoAction(
                        f"{words} task {operand}",
                        TASK_ID_USAGE_MESSAGE,
                    )
                with self.subTest(command=f"{words} reminder {operand}"):
                    self.assertNoAction(
                        f"{words} reminder {operand}",
                        REMINDER_ID_USAGE_MESSAGE,
                    )
        self.assertNoAction("edit task x: buy milk", TASK_ID_USAGE_MESSAGE)
        self.assertNoAction("reschedule task 0: none", TASK_ID_USAGE_MESSAGE)
        self.assertNoAction("edit reminder x: call", REMINDER_ID_USAGE_MESSAGE)
        self.assertNoAction(
            "reschedule reminder -4: tomorrow 9:00",
            REMINDER_ID_USAGE_MESSAGE,
        )

    def test_text_must_be_storable_before_anything_is_offered(self) -> None:
        too_long = "a" * 513
        hidden = f"buy{chr(0x200B)}milk"
        for request in (
            f"add task {too_long}",
            f"add task {hidden}",
            f"add task due tomorrow: {too_long}",
            f"edit task 3: {hidden}",
            f"add reminder tomorrow 9:00: {too_long}",
            f"edit reminder 4: {too_long}",
        ):
            with self.subTest(request=request[:40]):
                self.assertNoAction(request, TASK_TEXT_MESSAGE)

    def test_timing_commands_need_the_requests_timing(self) -> None:
        for request in (
            "due tasks",
            "due reminders",
            "add task due tomorrow: buy milk",
            "reschedule task 3: tomorrow",
            "add reminder tomorrow 9:00: call mom",
            "reschedule reminder 4: in 2 hours",
        ):
            with self.subTest(request=request):
                plan = self.planner.create_plan(request)
                self.assertEqual(plan.steps, ())
                self.assertEqual(plan.response_if_no_action, TIMING_UNAVAILABLE_MESSAGE)
        for request, action in (
            ("add task buy milk", "create"),
            ("reschedule task 3: none", "reschedule"),
            ("tasks", "list"),
            ("read reminder 4", "read"),
            ("complete task 3", "complete"),
        ):
            with self.subTest(request=request):
                (step,) = self.planner.create_plan(request).steps
                self.assertEqual(step.action, action)

    def test_without_a_zone_only_new_dates_and_times_are_refused(self) -> None:
        unzoned = TimingContext(TIMING_REFERENCE, None)
        refusal = TimingFailure.ZONE_UNAVAILABLE.message
        self.assertNoAction("due tasks", refusal, timing=unzoned)
        for request in (
            "add task due tomorrow: buy milk",
            "reschedule task 3: friday",
            "add reminder tomorrow 9:00: call mom",
            "add reminder in 5 minutes: stretch",
            "reschedule reminder 4: in 2 hours",
        ):
            with self.subTest(request=request):
                self.assertNoAction(request, refusal + NOTHING_SAVED, timing=unzoned)
        # The due-reminder query compares saved instants with the reference
        # instant alone, so it needs no zone.
        self.assertEqual(
            self.plan("due reminders", unzoned).steps,
            (PlanStep("reminders", "due", {"as_of": "2027-10-30T08:03:27Z"}),),
        )
        self.assertEqual(
            self.plan("add task buy milk", unzoned).steps,
            (PlanStep("tasks", "create", {"text": "buy milk"}),),
        )

    def test_past_far_skipped_and_repeated_times_are_refused(self) -> None:
        for request, failure in (
            ("add task due 2027-10-29: buy milk", TimingFailure.PAST),
            ("reschedule task 3: 2027-10-29", TimingFailure.PAST),
            ("add reminder today 10:03: call mom", TimingFailure.PAST),
            ("add task due 2037-10-31: buy milk", TimingFailure.TOO_FAR),
            ("add reminder 2028-03-26 02:30: call mom", TimingFailure.NONEXISTENT),
            ("add reminder tomorrow 02:30: check the clocks", TimingFailure.AMBIGUOUS),
            (
                "add reminder tomorrow 02:30 +03:00: check the clocks",
                TimingFailure.AMBIGUOUS,
            ),
            ("add reminder tomorrow 09:00 +02:00: call mom", TimingFailure.AMBIGUOUS),
        ):
            with self.subTest(request=request):
                self.assertNoAction(request, failure.message + NOTHING_SAVED)
        for request, argument in (
            ("add reminder today 10:04: call mom", f"2027-10-30T10:04:00+02:00{ZONE}"),
            ("add task due 2037-10-30: buy milk", "2037-10-30"),
            (
                "add reminder tomorrow 02:30 +01:00: check the clocks",
                f"2027-10-31T02:30:00+01:00{ZONE}",
            ),
        ):
            with self.subTest(request=request):
                (step,) = self.plan(request).steps
                self.assertIn(argument, step.arguments.values())

    def test_timing_is_passed_by_keyword_only(self) -> None:
        with self.assertRaises(TypeError):
            self.planner.create_plan("due tasks", self.timing)


def _quiet_logger() -> logging.Logger:
    """Return an isolated logger that never writes an application log file."""
    logger = logging.getLogger("tests.planner")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


if __name__ == "__main__":
    unittest.main()
