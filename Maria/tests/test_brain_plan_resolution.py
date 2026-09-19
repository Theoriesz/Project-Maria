"""Focused tests for whole-plan permission resolution before any execution.

Production plans still contain at most one step, so every multi-step plan
here is synthetic: it is handed to Brain directly or returned by a mocked
Planner. That is deliberate. These tests exist to prove the execution
pipeline is safe for plans the planner cannot yet emit, before any later
stage teaches it to emit them.
"""

from __future__ import annotations

import logging
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import Mock

from core.brain import BrainResponse, MariaBrain
from core.config import MariaConfig
from core.memory import Memory
from core.permissions import (
    PermissionDecision,
    PermissionManager,
    PermissionResult,
)
from core.planner import Plan, Planner, PlanStep
from core.session import CONFIRMATION_REFUSED_MESSAGE, MariaSession
from core.tool_manager import ToolExecutionResult, ToolManager


ALLOWED = PermissionResult(PermissionDecision.ALLOWED, "the action is allowed.")
DENIED = PermissionResult(
    PermissionDecision.DENIED,
    "filesystem.write is denied by configuration.",
)
DENIED_RENAME = PermissionResult(
    PermissionDecision.DENIED,
    "filesystem.rename is denied by configuration.",
)
NEEDS_CONFIRMATION = PermissionResult(
    PermissionDecision.REQUIRES_CONFIRMATION,
    "filesystem.rename requires explicit confirmation.",
)
NEEDS_CONFIRMATION_EDIT = PermissionResult(
    PermissionDecision.REQUIRES_CONFIRMATION,
    "notes.edit requires explicit confirmation.",
)
SINGLE_STEP_CONFIRMATION_TEXT = (
    "Confirmation required: filesystem.rename requires explicit confirmation."
)
RENAME_STEP = ("filesystem", "rename", {"source": "old.txt", "destination": "new.txt"})
WRITE_STEP = ("filesystem", "write", {"path": "notes.txt", "content": "data"})
CALCULATE_STEP = ("calculator", "calculate", {"expression": "1 + 1"})
TIME_STEP = ("system", "current_time", {})
EDIT_STEP = ("notes", "edit", {"id": 4, "body": "sentinel-body-9f31ac"})


def _plan(request: str, *steps: tuple[str, str, dict[str, Any]]) -> Plan:
    """Return a synthetic plan whose steps keep the order given here."""
    return Plan(
        original_request=request,
        steps=tuple(
            PlanStep(tool_name, action, arguments)
            for tool_name, action, arguments in steps
        ),
    )


class ResolutionTestCase(unittest.TestCase):
    """Drive a real Brain and session over fully mocked collaborators.

    Every permission check and every tool dispatch appends to one shared
    event list, so a single assertion on that list proves the relative
    order of the two phases rather than only their contents.
    """

    def setUp(self) -> None:
        self.planner = Mock(spec=Planner)
        self.memory = Mock(spec=Memory)
        self.tool_manager = Mock(spec=ToolManager)
        self.permissions = Mock(spec=PermissionManager)
        self.logger = Mock(spec=logging.Logger)
        self.events: list[str] = []

        self.memory.get_recent_interactions.return_value = []
        self.brain = MariaBrain(
            config=Mock(spec=MariaConfig),
            planner=self.planner,
            memory=self.memory,
            tool_manager=self.tool_manager,
            permissions=self.permissions,
            logger=self.logger,
        )
        self.session = MariaSession(self.brain, logger=self.logger)
        self._record_dispatches()

    def _resolve_as(self, decisions: dict[str, PermissionResult]) -> None:
        """Answer permission checks from a mapping and record each call."""

        def check(tool_name: str, action: str) -> PermissionResult:
            self.events.append(f"check:{tool_name}.{action}")
            return decisions[f"{tool_name}.{action}"]

        self.permissions.check.side_effect = check

    def _record_dispatches(
        self,
        results: tuple[ToolExecutionResult, ...] = (),
    ) -> None:
        """Record each dispatch and return the queued results in order."""
        queue = list(results)

        def execute(tool_name: str, action: str, arguments: Any) -> ToolExecutionResult:
            del arguments
            self.events.append(f"dispatch:{tool_name}.{action}")
            if queue:
                return queue.pop(0)
            return ToolExecutionResult(True, f"{tool_name}.{action} done")

        self.tool_manager.execute_tool.side_effect = execute

    def _dispatched(self) -> list[str]:
        """Return only the dispatch events recorded so far."""
        return [event for event in self.events if event.startswith("dispatch:")]


class DenialPrecedenceTests(ResolutionTestCase):
    """Verify a denial anywhere in a plan stops the whole plan."""

    def test_denial_at_a_later_step_executes_no_earlier_step(self) -> None:
        plan = _plan("synthetic denial", CALCULATE_STEP, WRITE_STEP)
        self.planner.create_plan.return_value = plan
        self._resolve_as({"calculator.calculate": ALLOWED, "filesystem.write": DENIED})

        response = self.brain.process("synthetic denial")

        expected = "Permission denied: filesystem.write is denied by configuration."
        self.assertEqual(response, BrainResponse(expected, plan=plan))
        self.tool_manager.execute_tool.assert_not_called()
        self.assertEqual(
            self.events,
            ["check:calculator.calculate", "check:filesystem.write"],
        )
        self.memory.record_interaction.assert_called_once_with(
            "synthetic denial",
            expected,
        )

    def test_every_step_is_checked_before_the_first_possible_dispatch(self) -> None:
        plan = _plan("synthetic denial", CALCULATE_STEP, TIME_STEP, WRITE_STEP)
        self.planner.create_plan.return_value = plan
        self._resolve_as(
            {
                "calculator.calculate": ALLOWED,
                "system.current_time": ALLOWED,
                "filesystem.write": DENIED,
            }
        )

        self.brain.process("synthetic denial")

        self.assertEqual(
            self.events,
            [
                "check:calculator.calculate",
                "check:system.current_time",
                "check:filesystem.write",
            ],
        )
        self.assertEqual(self._dispatched(), [])

    def test_confirmation_before_a_denial_is_denied_and_never_pends(self) -> None:
        plan = _plan("synthetic blocked confirmation", RENAME_STEP, WRITE_STEP)
        self.planner.create_plan.return_value = plan
        self._resolve_as(
            {"filesystem.rename": NEEDS_CONFIRMATION, "filesystem.write": DENIED}
        )

        response = self.session.submit("synthetic blocked confirmation")

        expected = "Permission denied: filesystem.write is denied by configuration."
        self.assertFalse(response.needs_confirmation)
        self.assertEqual(response.text, expected)
        self.assertIsNone(self.session.pending_approval)
        self.tool_manager.execute_tool.assert_not_called()
        self.memory.record_interaction.assert_called_once_with(
            "synthetic blocked confirmation",
            expected,
        )

    def test_the_first_denied_step_in_plan_order_supplies_the_reason(self) -> None:
        plan = _plan("synthetic double denial", WRITE_STEP, RENAME_STEP)
        self.planner.create_plan.return_value = plan
        self._resolve_as(
            {"filesystem.write": DENIED, "filesystem.rename": DENIED_RENAME}
        )

        response = self.brain.process("synthetic double denial")

        self.assertEqual(
            response.text,
            "Permission denied: filesystem.write is denied by configuration.",
        )
        self.tool_manager.execute_tool.assert_not_called()


class ConfirmationResolutionTests(ResolutionTestCase):
    """Verify multi-step confirmation runs nothing before approval."""

    def test_multi_step_confirmation_runs_nothing_until_it_is_approved(self) -> None:
        plan = _plan("synthetic approval", CALCULATE_STEP, RENAME_STEP)
        self.planner.create_plan.return_value = plan
        self._resolve_as(
            {"calculator.calculate": ALLOWED, "filesystem.rename": NEEDS_CONFIRMATION}
        )
        self._record_dispatches(
            (
                ToolExecutionResult(True, "2"),
                ToolExecutionResult(True, "Renamed old.txt to new.txt"),
            )
        )

        pending_response = self.session.submit("synthetic approval")

        self.assertTrue(pending_response.needs_confirmation)
        self.assertIs(pending_response.plan, plan)
        self.tool_manager.execute_tool.assert_not_called()
        pending = self.session.pending_approval
        self.assertIs(pending.plan, plan)
        self.assertEqual(pending.fingerprint, plan.fingerprint)

        confirmed = self.session.confirm()

        self.assertEqual(confirmed.text, "2\nRenamed old.txt to new.txt")
        self.assertFalse(confirmed.needs_confirmation)
        self.assertEqual(self.planner.create_plan.call_count, 1)
        self.assertEqual(self.memory.get_recent_interactions.call_count, 1)
        self.assertEqual(
            self.events,
            [
                "check:calculator.calculate",
                "check:filesystem.rename",
                "check:calculator.calculate",
                "check:filesystem.rename",
                "dispatch:calculator.calculate",
                "dispatch:filesystem.rename",
            ],
        )
        self.memory.record_interaction.assert_called_once_with(
            "synthetic approval",
            "2\nRenamed old.txt to new.txt",
        )

    def test_several_confirmation_required_steps_need_one_approval(self) -> None:
        plan = _plan("synthetic two sensitive steps", RENAME_STEP, EDIT_STEP)
        self.planner.create_plan.return_value = plan
        self._resolve_as(
            {
                "filesystem.rename": NEEDS_CONFIRMATION,
                "notes.edit": NEEDS_CONFIRMATION_EDIT,
            }
        )

        pending_response = self.session.submit("synthetic two sensitive steps")
        self.assertTrue(pending_response.needs_confirmation)
        self.assertEqual(self._dispatched(), [])

        self.session.confirm()

        self.assertEqual(
            self._dispatched(),
            ["dispatch:filesystem.rename", "dispatch:notes.edit"],
        )

    def test_denial_found_at_confirmation_time_executes_and_replays_nothing(
        self,
    ) -> None:
        plan = _plan("synthetic revoked approval", RENAME_STEP)
        self.planner.create_plan.return_value = plan
        self._resolve_as({"filesystem.rename": NEEDS_CONFIRMATION})

        pending_response = self.session.submit("synthetic revoked approval")
        self.assertTrue(pending_response.needs_confirmation)

        self._resolve_as({"filesystem.rename": DENIED_RENAME})
        denial = self.session.confirm()

        expected = "Permission denied: filesystem.rename is denied by configuration."
        self.assertEqual(denial.text, expected)
        self.assertFalse(denial.needs_confirmation)
        self.tool_manager.execute_tool.assert_not_called()
        self.assertIsNone(self.session.pending_approval)
        self.assertEqual(self.session.confirm().text, CONFIRMATION_REFUSED_MESSAGE)
        self.tool_manager.execute_tool.assert_not_called()
        self.memory.record_interaction.assert_called_once_with(
            "synthetic revoked approval",
            expected,
        )

    def test_confirmation_time_denial_at_a_later_step_runs_no_earlier_step(
        self,
    ) -> None:
        plan = _plan("synthetic revoked later step", RENAME_STEP, WRITE_STEP)
        self.planner.create_plan.return_value = plan
        self._resolve_as(
            {"filesystem.rename": NEEDS_CONFIRMATION, "filesystem.write": ALLOWED}
        )

        pending_response = self.session.submit("synthetic revoked later step")
        self.assertTrue(pending_response.needs_confirmation)

        self._resolve_as(
            {"filesystem.rename": NEEDS_CONFIRMATION, "filesystem.write": DENIED}
        )
        denial = self.session.confirm()

        self.assertEqual(
            denial.text,
            "Permission denied: filesystem.write is denied by configuration.",
        )
        self.assertEqual(self._dispatched(), [])

    def test_single_step_confirmation_text_is_unchanged(self) -> None:
        plan = _plan("rename old.txt to new.txt", RENAME_STEP)
        self.planner.create_plan.return_value = plan
        self._resolve_as({"filesystem.rename": NEEDS_CONFIRMATION})

        response = self.brain.process("rename old.txt to new.txt")

        self.assertEqual(response.text, SINGLE_STEP_CONFIRMATION_TEXT)
        self.assertTrue(response.needs_confirmation)
        self.assertEqual(self._dispatched(), [])
        self.memory.record_interaction.assert_not_called()


class ExecutionPhaseTests(ResolutionTestCase):
    """Verify dispatch order, argument identity, and stop-on-failure."""

    def test_all_allowed_plan_checks_everything_then_runs_each_step_once(self) -> None:
        plan = _plan("synthetic success", CALCULATE_STEP, TIME_STEP, EDIT_STEP)
        self.planner.create_plan.return_value = plan
        self._resolve_as(
            {
                "calculator.calculate": ALLOWED,
                "system.current_time": ALLOWED,
                "notes.edit": ALLOWED,
            }
        )
        self._record_dispatches(
            (
                ToolExecutionResult(True, "2"),
                ToolExecutionResult(True, "2030-01-02T03:04:05+08:00"),
                ToolExecutionResult(True, "Edited note 4."),
            )
        )

        response = self.brain.process("synthetic success")

        self.assertEqual(
            self.events,
            [
                "check:calculator.calculate",
                "check:system.current_time",
                "check:notes.edit",
                "dispatch:calculator.calculate",
                "dispatch:system.current_time",
                "dispatch:notes.edit",
            ],
        )
        self.assertEqual(
            response.text,
            "2\n2030-01-02T03:04:05+08:00\nEdited note 4.",
        )
        self.assertEqual(self.tool_manager.execute_tool.call_count, 3)

    def test_each_step_forwards_its_own_immutable_arguments_object(self) -> None:
        plan = _plan("synthetic arguments", CALCULATE_STEP, EDIT_STEP)
        self.planner.create_plan.return_value = plan
        self._resolve_as({"calculator.calculate": ALLOWED, "notes.edit": ALLOWED})

        self.brain.process("synthetic arguments")

        forwarded = [
            invocation.args[2]
            for invocation in self.tool_manager.execute_tool.call_args_list
        ]
        self.assertEqual(len(forwarded), 2)
        for position, arguments in enumerate(forwarded):
            with self.subTest(position=position):
                self.assertIs(arguments, plan.steps[position].arguments)
                self.assertNotIsInstance(arguments, dict)
                with self.assertRaises(TypeError):
                    arguments["injected"] = "value"

    def test_failure_stops_the_plan_and_undoes_no_earlier_step(self) -> None:
        plan = _plan("synthetic partial failure", CALCULATE_STEP, EDIT_STEP, TIME_STEP)
        self.planner.create_plan.return_value = plan
        self._resolve_as(
            {
                "calculator.calculate": ALLOWED,
                "notes.edit": ALLOWED,
                "system.current_time": ALLOWED,
            }
        )
        self._record_dispatches(
            (
                ToolExecutionResult(True, "2", None, recorded_message="Custom two"),
                ToolExecutionResult(False, "Note not found: 4."),
            )
        )

        response = self.brain.process("synthetic partial failure")

        self.assertEqual(
            self.events,
            [
                "check:calculator.calculate",
                "check:notes.edit",
                "check:system.current_time",
                "dispatch:calculator.calculate",
                "dispatch:notes.edit",
            ],
        )
        self.assertEqual(response.text, "2\nNote not found: 4.")
        self.memory.record_interaction.assert_called_once_with(
            "synthetic partial failure",
            "Custom two\nNote not found: 4.",
        )
        self.assertEqual(self.tool_manager.execute_tool.call_count, 2)

    def test_zero_step_plan_resolves_and_dispatches_nothing(self) -> None:
        plan = Plan("help", (), "supported commands")
        self.planner.create_plan.return_value = plan

        response = self.brain.process("help")

        self.assertEqual(response, BrainResponse("supported commands", plan=plan))
        self.permissions.check.assert_not_called()
        self.tool_manager.execute_tool.assert_not_called()
        self.assertEqual(self.events, [])
        self.memory.record_interaction.assert_called_once_with(
            "help",
            "supported commands",
        )


class ResolvedPlanRenderingTests(ResolutionTestCase):
    """Verify the multi-step confirmation rendering stays privacy-safe."""

    def _render(self, *steps: tuple[str, str, dict[str, Any]]) -> str:
        """Return the confirmation text for a synthetic multi-step plan."""
        plan = _plan("synthetic rendering", *steps)
        self.planner.create_plan.return_value = plan
        response = self.brain.process("synthetic rendering")
        self.assertTrue(response.needs_confirmation)
        return response.text

    def test_rendering_lists_every_step_in_plan_order_with_its_outcome(self) -> None:
        self._resolve_as(
            {
                "calculator.calculate": ALLOWED,
                "filesystem.rename": NEEDS_CONFIRMATION,
                "notes.edit": NEEDS_CONFIRMATION_EDIT,
            }
        )

        rendered = self._render(CALCULATE_STEP, RENAME_STEP, EDIT_STEP)

        self.assertEqual(
            rendered,
            "Confirmation required for 3 planned actions:\n"
            "1. calculator.calculate: allowed\n"
            "2. filesystem.rename: requires confirmation\n"
            "3. notes.edit: requires confirmation",
        )

    def test_rendering_is_deterministic_for_the_same_plan(self) -> None:
        self._resolve_as(
            {"calculator.calculate": ALLOWED, "filesystem.rename": NEEDS_CONFIRMATION}
        )

        first = self._render(CALCULATE_STEP, RENAME_STEP)
        second = self._render(CALCULATE_STEP, RENAME_STEP)

        self.assertEqual(first, second)

    def test_rendering_follows_step_order_rather_than_a_sorted_order(self) -> None:
        self._resolve_as(
            {"calculator.calculate": ALLOWED, "filesystem.rename": NEEDS_CONFIRMATION}
        )

        rendered = self._render(RENAME_STEP, CALCULATE_STEP)

        self.assertEqual(
            rendered,
            "Confirmation required for 2 planned actions:\n"
            "1. filesystem.rename: requires confirmation\n"
            "2. calculator.calculate: allowed",
        )

    def test_rendering_exposes_no_absolute_host_path_or_argument_value(self) -> None:
        with TemporaryDirectory() as temporary:
            host_path = str(Path(temporary).resolve() / "private" / "secret.txt")
            self._resolve_as(
                {"filesystem.read": ALLOWED, "filesystem.rename": NEEDS_CONFIRMATION}
            )

            rendered = self._render(
                ("filesystem", "read", {"path": host_path}),
                ("filesystem", "rename", {"source": host_path, "destination": "b.txt"}),
            )

            self.assertNotIn(host_path, rendered)
            self.assertNotIn("secret.txt", rendered)
            self.assertNotIn("private", rendered)
            self.assertNotIn(Path(host_path).anchor, rendered)
            self.assertNotIn("/", rendered)
            self.assertNotIn("\\", rendered)

    def test_rendering_exposes_no_fingerprint_or_canonical_plan_content(self) -> None:
        plan = _plan("synthetic rendering", CALCULATE_STEP, RENAME_STEP)
        self.planner.create_plan.return_value = plan
        self._resolve_as(
            {"calculator.calculate": ALLOWED, "filesystem.rename": NEEDS_CONFIRMATION}
        )

        rendered = self.brain.process("synthetic rendering").text

        for secret in (
            plan.fingerprint,
            "synthetic rendering",
            "old.txt",
            "new.txt",
            "1 + 1",
            "original_request",
            "Plan(",
            "PlanStep(",
            "PlanArguments(",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, rendered)

    def test_rendering_is_not_recorded_to_interaction_history(self) -> None:
        self._resolve_as(
            {"calculator.calculate": ALLOWED, "filesystem.rename": NEEDS_CONFIRMATION}
        )

        self._render(CALCULATE_STEP, RENAME_STEP)

        self.memory.record_interaction.assert_not_called()


class RenderingLoggingPrivacyTests(unittest.TestCase):
    """Verify no rendered plan or argument value reaches the logs."""

    def setUp(self) -> None:
        self.records: list[str] = []
        self.logger = logging.getLogger("tests.plan_resolution.privacy")
        self.logger.handlers.clear()
        self.logger.addHandler(_RecordingHandler(self.records))
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.addCleanup(self.logger.handlers.clear)

        self.planner = Mock(spec=Planner)
        self.memory = Mock(spec=Memory)
        self.tool_manager = Mock(spec=ToolManager)
        self.permissions = Mock(spec=PermissionManager)
        self.memory.get_recent_interactions.return_value = []
        self.tool_manager.execute_tool.return_value = ToolExecutionResult(True, "done")
        self.permissions.check.side_effect = lambda tool_name, action: (
            NEEDS_CONFIRMATION if tool_name == "filesystem" else NEEDS_CONFIRMATION_EDIT
        )

        self.brain = MariaBrain(
            config=Mock(spec=MariaConfig),
            planner=self.planner,
            memory=self.memory,
            tool_manager=self.tool_manager,
            permissions=self.permissions,
            logger=self.logger,
        )
        self.session = MariaSession(self.brain, logger=self.logger)
        self.plan = _plan("synthetic logging", RENAME_STEP, EDIT_STEP)
        self.planner.create_plan.return_value = self.plan

    def test_multi_step_confirmation_logs_no_rendering_or_content(self) -> None:
        rendered = self.session.submit("synthetic logging").text
        self.session.confirm()

        self.assertTrue(self.records)
        logged = "\n".join(self.records)
        for secret in (
            rendered,
            "planned actions:",
            "synthetic logging",
            "old.txt",
            "new.txt",
            "sentinel-body-9f31ac",
            self.plan.fingerprint,
            "Plan(",
            "PlanStep(",
            "PlanArguments(",
            "original_request",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, logged)

    def test_confirmation_logging_names_only_the_planned_tool_actions(self) -> None:
        self.session.submit("synthetic logging")

        logged = "\n".join(self.records)
        self.assertIn("Confirmation required for action:", logged)
        self.assertIn("filesystem", logged)
        self.assertIn("notes", logged)


class _LegacyInterleavedBrain(MariaBrain):
    """The pre-V1-05 controller, retained only as a comparison baseline.

    This reproduces the V1-04 loop verbatim: it resolved one step's
    permission and dispatched that same step before looking at the next
    one. Nothing in Maria uses it. The equivalence test below runs the same
    real single-step commands through it and through the real controller,
    then compares the interaction rows each run produced.
    """

    def _run_plan(self, plan: Plan, *, confirmed: bool) -> BrainResponse:
        """Resolve and dispatch one step at a time, as V1-04 did."""
        if not plan.steps:
            response = plan.response_if_no_action or "I do not know how to handle that yet."
            self._record_interaction(plan.original_request, response)
            return BrainResponse(response, plan=plan)

        results: list[ToolExecutionResult] = []

        for step in plan.steps:
            permission = self.permissions.check(step.tool_name, step.action)

            if permission.decision == PermissionDecision.DENIED:
                response = f"Permission denied: {permission.reason}"
                self._record_interaction(plan.original_request, response)
                return BrainResponse(response, plan=plan)

            if (
                permission.decision == PermissionDecision.REQUIRES_CONFIRMATION
                and not confirmed
            ):
                return BrainResponse(
                    text=f"Confirmation required: {permission.reason}",
                    needs_confirmation=True,
                    plan=plan,
                )

            result = self.tool_manager.execute_tool(
                step.tool_name,
                step.action,
                step.arguments,
            )
            results.append(result)

            if not result.success:
                break

        response = self._format_results(results)
        self._record_interaction(
            plan.original_request,
            self._format_recorded_results(results),
        )
        return BrainResponse(response, plan=plan)


class SingleStepDatabaseEquivalenceTests(unittest.TestCase):
    """Compare real single-step interaction rows before and after the split.

    Both runs use real planner, permission, tool, and memory components
    over their own temporary database, so this compares stored rows rather
    than mocked call arguments.
    """

    COMMANDS = (
        "help",
        "sing me an unsupported song",
        "calculate 6 * 7",
        "create note Release Check: Original body text",
        "read note Release Check",
        "remember My editor is EXC-V105-EQUIVALENCE",
        "recall EXC-V105-EQUIVALENCE",
        "memories",
        "history",
        "search history calculate",
        "read interaction 1",
        "retitle note 1: Renamed Release Check",
        "edit note 1: Replaced body text",
        "delete note 1",
        "update memory 1: My editor is EXC-V105-REPLACED",
        "forget memory 1",
        "delete interaction 2",
    )
    DELETED_BY_A_LATER_COMMAND = "sing me an unsupported song"

    def _rows_for(self, brain_class: type[MariaBrain]) -> list[tuple[str, str]]:
        """Run every command through one controller and return its rows."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        database_path = root / "database" / "maria.db"

        logger = logging.getLogger("tests.plan_resolution.equivalence")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        self.addCleanup(logger.handlers.clear)

        config = MariaConfig(
            app_name="Maria",
            version="0.2.0",
            environment="test",
            database_path=database_path,
            log_dir=root / "logs",
            prompt_path=root / "prompts" / "system.txt",
            safe_filesystem_root=root / "safe-filesystem-root",
            log_level="DEBUG",
            confirmation_required=frozenset(),
            denied_actions=frozenset(),
        )

        memory = Memory(database_path, logger=logger)
        memory.initialize()
        tool_manager = ToolManager(config=config, memory=memory, logger=logger)
        tool_manager.load_tools()
        brain = brain_class(
            config=config,
            planner=Planner(logger=logger),
            memory=memory,
            tool_manager=tool_manager,
            permissions=PermissionManager(
                confirmation_required=frozenset(),
                denied_actions=frozenset(),
                logger=logger,
            ),
            logger=logger,
        )
        session = MariaSession(brain, logger=logger)

        for command in self.COMMANDS:
            response = session.submit(command)
            if response.needs_confirmation:
                session.confirm()

        connection = sqlite3.connect(database_path)
        try:
            rows = connection.execute(
                "SELECT user_input, response FROM interactions ORDER BY id"
            ).fetchall()
        finally:
            connection.close()
        return [(str(row[0]), str(row[1])) for row in rows]

    def test_single_step_commands_store_byte_identical_interaction_rows(self) -> None:
        before = self._rows_for(_LegacyInterleavedBrain)
        after = self._rows_for(MariaBrain)

        self.assertTrue(before)
        self.assertEqual(len(before), len(after))
        for position, (old_row, new_row) in enumerate(zip(before, after)):
            with self.subTest(position=position, command=old_row[0]):
                self.assertEqual(old_row, new_row)
                self.assertEqual(
                    old_row[0].encode("utf-8"),
                    new_row[0].encode("utf-8"),
                )
                self.assertEqual(
                    old_row[1].encode("utf-8"),
                    new_row[1].encode("utf-8"),
                )

    def test_every_compared_command_produced_a_recorded_row(self) -> None:
        """Every compared command records a row, bar the one later deleted.

        `delete interaction 2` removes the row the unsupported-song
        command created, which is why that command is included: the
        compared sequence exercises a committed lifecycle deletion as
        well as the ordinary recording paths.
        """
        rows = self._rows_for(MariaBrain)

        requests = [request for request, _response in rows]
        self.assertNotIn(self.DELETED_BY_A_LATER_COMMAND, requests)
        for command in self.COMMANDS:
            if command == self.DELETED_BY_A_LATER_COMMAND:
                continue
            with self.subTest(command=command):
                self.assertIn(command, requests)


class _RecordingHandler(logging.Handler):
    """Collect rendered log output for privacy assertions."""

    def __init__(self, records: list[str]) -> None:
        super().__init__()
        self.records = records

    def emit(self, record: logging.LogRecord) -> None:
        """Store the rendered message together with its raw arguments."""
        self.records.append(f"{record.getMessage()} {record.args!r}")


if __name__ == "__main__":
    unittest.main()
