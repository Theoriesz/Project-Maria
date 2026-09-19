"""Focused non-memory tests for Maria's application controller."""

from __future__ import annotations

import inspect
import logging
import re
import unittest
from typing import Any
from unittest.mock import Mock, call

import core.brain as brain_module
from core.brain import BrainResponse, MariaBrain
from core.config import MariaConfig
from core.memory import Memory
from core.permissions import (
    PermissionDecision,
    PermissionManager,
    PermissionResult,
)
from core.planner import Plan, Planner, PlanStep
from core.tool_manager import ToolExecutionResult, ToolManager


class MariaBrainTests(unittest.TestCase):
    """Verify controller orchestration through mocked component boundaries."""

    def setUp(self) -> None:
        self.config = Mock(spec=MariaConfig)
        self.planner = Mock(spec=Planner)
        self.memory = Mock(spec=Memory)
        self.tool_manager = Mock(spec=ToolManager)
        self.permissions = Mock(spec=PermissionManager)
        self.logger = Mock(spec=logging.Logger)

        self.memory.get_recent_interactions.return_value = []
        self.permissions.check.return_value = PermissionResult(
            PermissionDecision.ALLOWED,
            "allowed",
        )
        self.brain = MariaBrain(
            config=self.config,
            planner=self.planner,
            memory=self.memory,
            tool_manager=self.tool_manager,
            permissions=self.permissions,
            logger=self.logger,
        )

    def test_programmatic_empty_input_returns_without_touching_components(self) -> None:
        response = self.brain.process(" \t\r\n ")

        self.assertEqual(response, BrainResponse("Please enter a request."))
        self.memory.get_recent_interactions.assert_not_called()
        self.memory.record_interaction.assert_not_called()
        self.planner.create_plan.assert_not_called()
        self.permissions.check.assert_not_called()
        self.tool_manager.execute_tool.assert_not_called()
        self.logger.info.assert_not_called()

    def test_input_is_stripped_before_planning_and_recording(self) -> None:
        plan = _single_step_plan(
            "calculate 2 + 2",
            arguments={"expression": "2 + 2"},
        )
        self.planner.create_plan.return_value = plan
        self.tool_manager.execute_tool.return_value = ToolExecutionResult(
            True,
            "4",
        )

        response = self.brain.process("  calculate 2 + 2 \n")

        self.assertEqual(response.text, "4")
        self.planner.create_plan.assert_called_once_with(
            "calculate 2 + 2",
            context={"recent_interactions": []},
        )
        self.memory.record_interaction.assert_called_once_with(
            "calculate 2 + 2",
            "4",
        )

    def test_recent_interactions_are_passed_to_planner_as_context(self) -> None:
        interactions = [
            {"user_input": "help", "response": "commands"},
            {"user_input": "time", "response": "mock time"},
        ]
        plan = Plan("help", (), "available commands")
        self.memory.get_recent_interactions.return_value = interactions
        self.planner.create_plan.return_value = plan

        response = self.brain.process("help")

        self.assertEqual(response.text, "available commands")
        self.memory.get_recent_interactions.assert_called_once_with(limit=3)
        self.planner.create_plan.assert_called_once_with(
            "help",
            context={"recent_interactions": interactions},
        )

    def test_recent_interaction_runtime_error_is_logged_and_context_is_empty(
        self,
    ) -> None:
        error = RuntimeError("temporary interaction read failure")
        self.memory.get_recent_interactions.side_effect = error
        plan = Plan("help", (), "help text")
        self.planner.create_plan.return_value = plan

        response = self.brain.process("help")

        self.assertEqual(response.text, "help text")
        self.planner.create_plan.assert_called_once_with(
            "help",
            context={"recent_interactions": []},
        )
        self.logger.error.assert_called_once_with(
            "Unable to retrieve recent memory: %s",
            error,
        )

    def test_zero_step_responses_are_returned_and_recorded(self) -> None:
        cases = (
            ("help", "supported commands"),
            ("sing", "unsupported request"),
        )

        plans = [Plan(user_input, (), expected) for user_input, expected in cases]
        self.planner.create_plan.side_effect = plans

        responses = [
            self.brain.process(user_input) for user_input, _expected in cases
        ]

        self.assertEqual(
            responses,
            [
                BrainResponse(expected, plan=plan)
                for (_user_input, expected), plan in zip(cases, plans)
            ],
        )
        self.permissions.check.assert_not_called()
        self.tool_manager.execute_tool.assert_not_called()
        self.assertEqual(
            self.memory.record_interaction.call_args_list,
            [call(user_input, expected) for user_input, expected in cases],
        )

    def test_allowed_step_delegates_arguments_unchanged_and_returns_output(
        self,
    ) -> None:
        arguments = {"expression": "2 + 2", "precision": 2}
        plan = _single_step_plan("calculate 2 + 2", arguments=arguments)
        result = ToolExecutionResult(True, "4", {"value": 4})
        self.planner.create_plan.return_value = plan
        self.tool_manager.execute_tool.return_value = result

        response = self.brain.process("calculate 2 + 2")

        self.assertEqual(response, BrainResponse("4", plan=plan))
        self.permissions.check.assert_called_once_with("calculator", "calculate")
        self.tool_manager.execute_tool.assert_called_once_with(
            "calculator",
            "calculate",
            arguments,
        )
        forwarded_arguments = self.tool_manager.execute_tool.call_args.args[2]
        self.assertIs(forwarded_arguments, plan.steps[0].arguments)
        self.assertEqual(forwarded_arguments, arguments)
        self.memory.record_interaction.assert_called_once_with(
            "calculate 2 + 2",
            "4",
        )

    def test_unconfirmed_confirmation_required_step_is_not_executed_or_recorded(
        self,
    ) -> None:
        plan = _single_step_plan(
            "rename old.txt to new.txt",
            tool_name="filesystem",
            action="rename",
            arguments={"source": "old.txt", "destination": "new.txt"},
        )
        permission = PermissionResult(
            PermissionDecision.REQUIRES_CONFIRMATION,
            "filesystem.rename requires explicit confirmation.",
        )
        self.planner.create_plan.return_value = plan
        self.permissions.check.return_value = permission

        response = self.brain.process("rename old.txt to new.txt")

        self.assertEqual(
            response,
            BrainResponse(
                "Confirmation required: "
                "filesystem.rename requires explicit confirmation.",
                needs_confirmation=True,
                plan=plan,
            ),
        )
        self.permissions.check.assert_called_once_with("filesystem", "rename")
        self.tool_manager.execute_tool.assert_not_called()
        self.memory.record_interaction.assert_not_called()

    def test_confirmation_executes_the_approved_plan_without_replanning(self) -> None:
        """Lock in the deliberate V1-04 reversal of this test's contract.

        This test previously asserted the opposite behavior: confirming
        re-sent the raw request, so Planner and context retrieval each ran
        a second time and Brain executed whatever that second pass
        produced. Confirmation now executes the exact plan that was
        presented for approval, so planning and context retrieval happen
        exactly once across the whole lifecycle.
        """
        arguments = {"source": "old.txt", "destination": "new.txt"}
        plan = _single_step_plan(
            "rename old.txt to new.txt",
            tool_name="filesystem",
            action="rename",
            arguments=arguments,
        )
        self.planner.create_plan.return_value = plan
        self.permissions.check.return_value = PermissionResult(
            PermissionDecision.REQUIRES_CONFIRMATION,
            "filesystem.rename requires explicit confirmation.",
        )
        self.tool_manager.execute_tool.return_value = ToolExecutionResult(
            True,
            "Renamed old.txt to new.txt",
        )

        unconfirmed = self.brain.process("rename old.txt to new.txt")
        confirmed = self.brain.execute_confirmed_plan(unconfirmed.plan)

        self.assertTrue(unconfirmed.needs_confirmation)
        self.assertIs(unconfirmed.plan, plan)
        self.assertEqual(confirmed.text, "Renamed old.txt to new.txt")
        self.assertFalse(confirmed.needs_confirmation)
        self.assertEqual(self.planner.create_plan.call_count, 1)
        self.assertEqual(self.memory.get_recent_interactions.call_count, 1)
        self.tool_manager.execute_tool.assert_called_once_with(
            "filesystem",
            "rename",
            arguments,
        )
        self.memory.record_interaction.assert_called_once_with(
            "rename old.txt to new.txt",
            "Renamed old.txt to new.txt",
        )

    def test_process_exposes_no_raw_text_confirmation_bypass(self) -> None:
        parameters = inspect.signature(MariaBrain.process).parameters

        self.assertEqual(list(parameters), ["self", "user_input"])
        self.assertNotIn("confirmed", parameters)

    def test_denied_step_prevents_dispatch_and_records_denial(self) -> None:
        plan = _single_step_plan(
            "write file",
            tool_name="filesystem",
            action="write",
            arguments={"path": "file.txt", "content": "data"},
        )
        permission = PermissionResult(
            PermissionDecision.DENIED,
            "filesystem.write is denied by configuration.",
        )
        self.planner.create_plan.return_value = plan
        self.permissions.check.return_value = permission

        response = self.brain.process("write file")

        expected = (
            "Permission denied: filesystem.write is denied by configuration."
        )
        self.assertEqual(response, BrainResponse(expected, plan=plan))
        self.permissions.check.assert_called_once_with("filesystem", "write")
        self.tool_manager.execute_tool.assert_not_called()
        self.memory.record_interaction.assert_called_once_with(
            "write file",
            expected,
        )

    def test_failed_tool_result_stops_later_plan_steps_and_is_recorded(self) -> None:
        first_arguments = {"path": "missing.txt"}
        plan = Plan(
            original_request="synthetic failure",
            steps=(
                PlanStep("filesystem", "read", first_arguments),
                PlanStep("system", "current_time", {}),
            ),
        )
        self.planner.create_plan.return_value = plan
        self.tool_manager.execute_tool.return_value = ToolExecutionResult(
            False,
            "File not found: missing.txt",
        )

        response = self.brain.process("synthetic failure")

        self.assertEqual(response.text, "File not found: missing.txt")
        self.permissions.check.assert_called_once_with("filesystem", "read")
        self.tool_manager.execute_tool.assert_called_once_with(
            "filesystem",
            "read",
            first_arguments,
        )
        self.memory.record_interaction.assert_called_once_with(
            "synthetic failure",
            "File not found: missing.txt",
        )

    def test_successful_multi_step_results_are_joined_and_each_step_checked(
        self,
    ) -> None:
        first_arguments = {"expression": "1 + 1"}
        second_arguments: dict[str, Any] = {}
        plan = Plan(
            original_request="synthetic success",
            steps=(
                PlanStep("calculator", "calculate", first_arguments),
                PlanStep("system", "current_time", second_arguments),
            ),
        )
        self.planner.create_plan.return_value = plan
        self.tool_manager.execute_tool.side_effect = (
            ToolExecutionResult(True, "2"),
            ToolExecutionResult(True, "2030-01-02T03:04:05+08:00"),
        )

        response = self.brain.process("synthetic success")

        expected = "2\n2030-01-02T03:04:05+08:00"
        self.assertEqual(response, BrainResponse(expected, plan=plan))
        self.assertEqual(
            self.permissions.check.call_args_list,
            [
                call("calculator", "calculate"),
                call("system", "current_time"),
            ],
        )
        self.assertEqual(
            self.tool_manager.execute_tool.call_args_list,
            [
                call("calculator", "calculate", first_arguments),
                call("system", "current_time", second_arguments),
            ],
        )
        self.memory.record_interaction.assert_called_once_with(
            "synthetic success",
            expected,
        )

    def test_interaction_write_runtime_error_does_not_replace_tool_response(
        self,
    ) -> None:
        error = RuntimeError("temporary interaction write failure")
        plan = _single_step_plan("calculate 3 * 3")
        self.planner.create_plan.return_value = plan
        self.tool_manager.execute_tool.return_value = ToolExecutionResult(
            True,
            "9",
        )
        self.memory.record_interaction.side_effect = error

        response = self.brain.process("calculate 3 * 3")

        self.assertEqual(response, BrainResponse("9", plan=plan))
        self.memory.record_interaction.assert_called_once_with(
            "calculate 3 * 3",
            "9",
        )
        self.logger.error.assert_called_once_with(
            "Unable to record interaction: %s",
            error,
        )

    def test_recorded_message_none_persists_displayed_message_unchanged(self) -> None:
        plan = _single_step_plan("calculate 2 + 2", arguments={"expression": "2 + 2"})
        self.planner.create_plan.return_value = plan
        self.tool_manager.execute_tool.return_value = ToolExecutionResult(True, "4")

        response = self.brain.process("calculate 2 + 2")

        self.assertEqual(response.text, "4")
        self.memory.record_interaction.assert_called_once_with(
            "calculate 2 + 2",
            "4",
        )

    def test_recorded_message_overrides_persisted_text_for_a_single_step(self) -> None:
        plan = _single_step_plan(
            "read interaction 5",
            tool_name="history",
            action="read",
            arguments={"id": 5},
        )
        self.planner.create_plan.return_value = plan
        self.tool_manager.execute_tool.return_value = ToolExecutionResult(
            True,
            "Interaction 5\nCreated at: 2026-01-01T00:00:00+00:00\n"
            "Request:\nsecret\nResponse:\nsecret answer",
            {"id": 5},
            recorded_message="Displayed interaction 5.",
        )

        response = self.brain.process("read interaction 5")

        self.assertIn("secret answer", response.text)
        self.memory.record_interaction.assert_called_once_with(
            "read interaction 5",
            "Displayed interaction 5.",
        )

    def test_recorded_message_empty_string_is_persisted_verbatim_not_treated_as_unset(
        self,
    ) -> None:
        plan = _single_step_plan("calculate 2 + 2", arguments={"expression": "2 + 2"})
        self.planner.create_plan.return_value = plan
        self.tool_manager.execute_tool.return_value = ToolExecutionResult(
            True,
            "4",
            None,
            recorded_message="",
        )

        self.brain.process("calculate 2 + 2")

        self.memory.record_interaction.assert_called_once_with(
            "calculate 2 + 2",
            "",
        )

    def test_recorded_message_is_applied_per_step_in_multi_step_plans(self) -> None:
        first_arguments = {"expression": "1 + 1"}
        second_arguments: dict[str, Any] = {}
        plan = Plan(
            original_request="synthetic multi step",
            steps=(
                PlanStep("calculator", "calculate", first_arguments),
                PlanStep("history", "read", second_arguments),
            ),
        )
        self.planner.create_plan.return_value = plan
        self.tool_manager.execute_tool.side_effect = (
            ToolExecutionResult(True, "2"),
            ToolExecutionResult(
                True,
                "Interaction 9\nCreated at: 2026-01-01T00:00:00+00:00\n"
                "Request:\nx\nResponse:\ny",
                {"id": 9},
                recorded_message="Displayed interaction 9.",
            ),
        )

        response = self.brain.process("synthetic multi step")

        expected_display = (
            "2\nInteraction 9\nCreated at: 2026-01-01T00:00:00+00:00\n"
            "Request:\nx\nResponse:\ny"
        )
        self.assertEqual(response.text, expected_display)
        self.memory.record_interaction.assert_called_once_with(
            "synthetic multi step",
            "2\nDisplayed interaction 9.",
        )

    def test_recorded_message_failure_after_success_uses_executed_result_prefix(
        self,
    ) -> None:
        first_arguments = {"expression": "1 + 1"}
        second_arguments = {"path": "missing.txt"}
        plan = Plan(
            original_request="synthetic partial failure",
            steps=(
                PlanStep("calculator", "calculate", first_arguments),
                PlanStep("filesystem", "read", second_arguments),
            ),
        )
        self.planner.create_plan.return_value = plan
        self.tool_manager.execute_tool.side_effect = (
            ToolExecutionResult(True, "4", None, recorded_message="Custom four"),
            ToolExecutionResult(False, "File not found: missing.txt"),
        )

        response = self.brain.process("synthetic partial failure")

        self.assertEqual(response.text, "4\nFile not found: missing.txt")
        self.memory.record_interaction.assert_called_once_with(
            "synthetic partial failure",
            "Custom four\nFile not found: missing.txt",
        )

    def test_brain_delegates_to_manager_not_tool_implementations(self) -> None:
        plan = _single_step_plan("time", tool_name="system", action="current_time")
        self.planner.create_plan.return_value = plan
        self.tool_manager.execute_tool.return_value = ToolExecutionResult(
            True,
            "mocked time",
        )

        response = self.brain.process("time")

        self.assertEqual(response.text, "mocked time")
        self.tool_manager.execute_tool.assert_called_once_with(
            "system",
            "current_time",
            {},
        )
        source = inspect.getsource(brain_module)
        self.assertNotRegex(
            source,
            r"(?m)^\s*(?:import\s+tools(?:\.|\b)|from\s+tools(?:\.|\b))",
        )
        self.assertNotRegex(source, r"\.execute\s*\(")

    def test_brain_contains_no_memory_specific_sql_or_command_dispatch(self) -> None:
        source = inspect.getsource(brain_module)

        self.assertNotRegex(source, r"(?m)^\s*(?:import|from)\s+sqlite3\b")
        self.assertIsNone(
            re.search(
                r"\b(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|"
                r"DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE)\b",
                source,
                flags=re.IGNORECASE | re.DOTALL,
            )
        )
        for method_name in (
            "store_memory",
            "get_memory",
            "search_memories",
            "list_memories",
        ):
            with self.subTest(method_name=method_name):
                self.assertNotIn(method_name, source)
        self.assertNotRegex(
            source,
            r"(?:tool_name|step\.tool_name)\s*==\s*['\"]memory['\"]",
        )
        self.assertNotRegex(
            source,
            r"(?:tool_name|step\.tool_name|action)\s*==\s*['\"]history['\"]",
        )
        self.assertNotRegex(
            source,
            r"(?:action|step\.action)\s*==\s*['\"]read['\"]",
        )
        self.assertNotRegex(
            source,
            r"(?:action|step\.action)\s*==\s*['\"]delete['\"]",
        )


def _single_step_plan(
    request: str,
    *,
    tool_name: str = "calculator",
    action: str = "calculate",
    arguments: dict[str, Any] | None = None,
) -> Plan:
    """Return a deterministic one-step plan for controller tests."""
    return Plan(
        original_request=request,
        steps=(
            PlanStep(
                tool_name,
                action,
                {} if arguments is None else arguments,
            ),
        ),
    )


if __name__ == "__main__":
    unittest.main()
