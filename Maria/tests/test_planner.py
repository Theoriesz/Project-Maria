"""Focused tests for Maria's strict command planner."""

from __future__ import annotations

import logging
import unittest
from typing import Any

from core.planner import Plan, Planner, PlanStep


UNSUPPORTED_RESPONSE = (
    "I do not have an executable v0.1 plan for that yet. "
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
                plan = self.planner.create_plan(
                    request,
                    context={"recent_interactions": [{"user_input": "remember editor"}]},
                )
                self.assertEqual(plan.steps, ())
                self.assertEqual(plan.response_if_no_action, UNSUPPORTED_RESPONSE)

    def test_representative_existing_commands_map_exactly_as_before(self) -> None:
        cases: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
            ("calculate 2 + 2", "calculator", "calculate", {"expression": "2 + 2"}),
            ("notes", "notes", "list", {}),
            ("read note Project Plan", "notes", "read", {"target": "Project Plan"}),
            ("read file README.md", "filesystem", "read", {"path": "README.md"}),
            ("system", "system", "overview", {}),
            ("time", "system", "current_time", {}),
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
            "- create note <title>: <body>",
            "- notes",
            "- read note <id or exact title>",
            "- remember <fact>",
            "- recall <query>",
            "- memories",
            "- system",
            "- time",
        }

        self.assertEqual(plan.steps, ())
        lines = plan.response_if_no_action.splitlines()
        self.assertEqual(lines[0], "Supported v0.1 commands:")
        self.assertEqual(set(lines[1:]), expected_commands)
        self.assertEqual(len(lines[1:]), len(expected_commands))

    def _assert_unsupported(self, request: str) -> None:
        """Assert that a request receives the unchanged fallback."""
        plan = self.planner.create_plan(request)
        self.assertEqual(plan.steps, ())
        self.assertEqual(plan.response_if_no_action, UNSUPPORTED_RESPONSE)


def _quiet_logger() -> logging.Logger:
    """Return an isolated logger that never writes an application log file."""
    logger = logging.getLogger("tests.planner")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


if __name__ == "__main__":
    unittest.main()
