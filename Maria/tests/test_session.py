"""Focused tests for Maria's plan-bound confirmation session."""

from __future__ import annotations

import inspect
import logging
import unittest
from unittest.mock import Mock, patch

import core.session as session_module
from core.brain import BrainResponse, MariaBrain
from core.config import MariaConfig
from core.memory import Memory
from core.permissions import (
    PermissionDecision,
    PermissionManager,
    PermissionResult,
)
from core.planner import Plan, Planner, PlanStep
from core.session import (
    CONFIRMATION_REFUSED_MESSAGE,
    MariaSession,
    PendingApproval,
)
from core.tool_manager import PreparedConfirmation, ToolExecutionResult, ToolManager


ALLOWED = PermissionResult(PermissionDecision.ALLOWED, "allowed")
DENIED = PermissionResult(
    PermissionDecision.DENIED,
    "filesystem.read is denied by configuration.",
)
NEEDS_CONFIRMATION = PermissionResult(
    PermissionDecision.REQUIRES_CONFIRMATION,
    "filesystem.rename requires explicit confirmation.",
)
CONFIRMATION_TEXT = (
    "Confirmation required: filesystem.rename requires explicit confirmation."
)
RENAME_REQUEST = "rename old.txt to new.txt"
RENAME_ARGUMENTS = {"source": "old.txt", "destination": "new.txt"}
SENTINEL_REQUEST = "session-sentinel-request-a31f9c"
SENTINEL_ARGUMENT = "session-sentinel-argument-7d40be"


def _rename_plan(
    request: str = RENAME_REQUEST,
    destination: str = "new.txt",
) -> Plan:
    """Return a deterministic one-step plan whose step needs confirmation."""
    return Plan(
        original_request=request,
        steps=(
            PlanStep(
                "filesystem",
                "rename",
                {"source": "old.txt", "destination": destination},
            ),
        ),
    )


class SessionTestCase(unittest.TestCase):
    """Wire a real Brain and session over fully mocked collaborators."""

    def setUp(self) -> None:
        self.planner = Mock(spec=Planner)
        self.memory = Mock(spec=Memory)
        self.tool_manager = Mock(spec=ToolManager)
        # No mocked tool pins confirmation state, so the plan offered
        # for approval is the plan itself.
        self.tool_manager.prepare_confirmation.side_effect = PreparedConfirmation
        self.tool_manager.validate_action.return_value = None
        self.permissions = Mock(spec=PermissionManager)
        self.brain_logger = Mock(spec=logging.Logger)
        self.session_logger = Mock(spec=logging.Logger)

        self.memory.get_recent_interactions.return_value = []
        self.permissions.check.return_value = ALLOWED
        self.tool_manager.execute_tool.return_value = ToolExecutionResult(
            True,
            "Renamed old.txt to new.txt",
        )

        self.brain = MariaBrain(
            config=Mock(spec=MariaConfig),
            planner=self.planner,
            memory=self.memory,
            tool_manager=self.tool_manager,
            permissions=self.permissions,
            logger=self.brain_logger,
        )
        self.session = MariaSession(self.brain, logger=self.session_logger)

    def _pend(self, plan: Plan | None = None) -> Plan:
        """Submit a request whose single step requires confirmation."""
        plan = _rename_plan() if plan is None else plan
        self.planner.create_plan.return_value = plan
        self.permissions.check.return_value = NEEDS_CONFIRMATION
        response = self.session.submit(plan.original_request)

        self.assertTrue(response.needs_confirmation)
        return plan


class PlanBindingTests(SessionTestCase):
    """Verify approval binds to one exact plan and its fingerprint."""

    def test_approved_request_is_planned_exactly_once(self) -> None:
        plan = self._pend()

        confirmed = self.session.confirm()

        self.assertEqual(confirmed.text, "Renamed old.txt to new.txt")
        self.assertFalse(confirmed.needs_confirmation)
        self.planner.create_plan.assert_called_once_with(RENAME_REQUEST)
        self.assertIs(confirmed.plan, plan)

    def test_no_interaction_history_is_read_for_an_approved_request(
        self,
    ) -> None:
        self._pend()

        self.session.confirm()

        self.memory.get_recent_interactions.assert_not_called()

    def test_confirmation_adds_no_further_planning_call(self) -> None:
        self._pend()
        planning_calls_before = self.planner.create_plan.call_count

        self.session.confirm()

        self.assertEqual(planning_calls_before, 1)
        self.assertEqual(self.planner.create_plan.call_count, 1)

    def test_session_executes_the_exact_plan_object_it_held(self) -> None:
        plan = self._pend()
        pending = self.session.pending_approval

        self.assertIsNotNone(pending)
        self.assertIs(pending.plan, plan)

        with patch.object(
            self.brain,
            "execute_confirmed_plan",
            wraps=self.brain.execute_confirmed_plan,
        ) as execute_plan:
            self.session.confirm()

        execute_plan.assert_called_once()
        self.assertIs(execute_plan.call_args.args[0], plan)

    def test_pending_fingerprint_is_captured_when_approval_becomes_pending(
        self,
    ) -> None:
        plan = self._pend()
        pending = self.session.pending_approval

        self.assertEqual(pending.fingerprint, plan.fingerprint)
        self.assertEqual(len(pending.fingerprint), 64)

    def test_tool_manager_receives_the_exact_approved_call(self) -> None:
        plan = self._pend()

        self.session.confirm()

        self.tool_manager.execute_tool.assert_called_once_with(
            "filesystem",
            "rename",
            RENAME_ARGUMENTS,
        )
        forwarded = self.tool_manager.execute_tool.call_args.args
        self.assertEqual(forwarded[0], plan.steps[0].tool_name)
        self.assertEqual(forwarded[1], plan.steps[0].action)
        self.assertIs(forwarded[2], plan.steps[0].arguments)
        with self.assertRaises(TypeError):
            forwarded[2]["source"] = "hijacked.txt"

    def test_confirmed_execution_records_the_approved_request_and_result(
        self,
    ) -> None:
        self._pend()

        self.session.confirm()

        self.memory.record_interaction.assert_called_once_with(
            RENAME_REQUEST,
            "Renamed old.txt to new.txt",
        )

    def test_pending_approval_holds_only_the_plan_and_its_fingerprint(self) -> None:
        self._pend()
        pending = self.session.pending_approval

        self.assertEqual(sorted(vars(pending)), ["fingerprint", "plan"])
        with self.assertRaises(AttributeError):
            pending.plan = _rename_plan("a different request")


class MismatchRefusalTests(SessionTestCase):
    """Verify a plan that no longer matches its approval runs nothing."""

    def _make_pending_mismatch(self, plan: Plan | None = None) -> Plan:
        """Replace the pending approval's digest with a stale one.

        Plan is immutable, so real drift cannot be produced through the
        public API. Substituting the captured digest proves the binding
        check is a real guard rather than a restatement of immutability.
        """
        plan = self._pend(plan)
        self.session._pending = PendingApproval(plan, "0" * 64)
        return plan

    def test_fingerprint_mismatch_executes_no_tool(self) -> None:
        self._make_pending_mismatch()

        refusal = self.session.confirm()

        self.assertEqual(refusal.text, CONFIRMATION_REFUSED_MESSAGE)
        self.assertFalse(refusal.needs_confirmation)
        self.tool_manager.execute_tool.assert_not_called()
        self.memory.record_interaction.assert_not_called()

    def test_fingerprint_mismatch_clears_the_pending_approval(self) -> None:
        self._make_pending_mismatch()

        self.session.confirm()

        self.assertIsNone(self.session.pending_approval)
        self.assertEqual(self.session.confirm().text, CONFIRMATION_REFUSED_MESSAGE)
        self.tool_manager.execute_tool.assert_not_called()

    def test_refusal_message_is_fixed_and_content_free(self) -> None:
        plan = self._make_pending_mismatch(
            _rename_plan(SENTINEL_REQUEST, destination=SENTINEL_ARGUMENT)
        )

        refusal = self.session.confirm()

        self.assertEqual(refusal.text, CONFIRMATION_REFUSED_MESSAGE)
        for secret in (
            SENTINEL_REQUEST,
            SENTINEL_ARGUMENT,
            "old.txt",
            "filesystem",
            "rename",
            plan.fingerprint,
            "0" * 64,
            "Plan(",
            "PlanStep(",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, refusal.text)
        self.assertIsNone(refusal.plan)

    def test_confirming_with_nothing_pending_returns_the_same_refusal(self) -> None:
        refusal = self.session.confirm()

        self.assertEqual(refusal.text, CONFIRMATION_REFUSED_MESSAGE)
        self.planner.create_plan.assert_not_called()
        self.tool_manager.execute_tool.assert_not_called()


class ReplayAndCancellationTests(SessionTestCase):
    """Verify an approval is consumed once and cancellation runs nothing."""

    def test_second_confirm_after_success_executes_nothing(self) -> None:
        self._pend()
        first = self.session.confirm()

        second = self.session.confirm()

        self.assertEqual(first.text, "Renamed old.txt to new.txt")
        self.assertEqual(second.text, CONFIRMATION_REFUSED_MESSAGE)
        self.tool_manager.execute_tool.assert_called_once()
        self.memory.record_interaction.assert_called_once()
        self.assertIsNone(self.session.pending_approval)

    def test_approval_is_consumed_even_when_the_tool_fails(self) -> None:
        self._pend()
        self.tool_manager.execute_tool.return_value = ToolExecutionResult(
            False,
            "File not found: old.txt",
        )

        failed = self.session.confirm()

        self.assertEqual(failed.text, "File not found: old.txt")
        self.assertIsNone(self.session.pending_approval)
        self.assertEqual(self.session.confirm().text, CONFIRMATION_REFUSED_MESSAGE)
        self.tool_manager.execute_tool.assert_called_once()
        self.memory.record_interaction.assert_called_once_with(
            RENAME_REQUEST,
            "File not found: old.txt",
        )

    def test_approval_is_consumed_even_when_execution_raises(self) -> None:
        self._pend()
        self.tool_manager.execute_tool.side_effect = RuntimeError("private-detail")

        with self.assertRaises(RuntimeError):
            self.session.confirm()

        self.assertIsNone(self.session.pending_approval)
        self.tool_manager.execute_tool.side_effect = None
        self.assertEqual(self.session.confirm().text, CONFIRMATION_REFUSED_MESSAGE)
        self.tool_manager.execute_tool.assert_called_once()

    def test_cancellation_executes_nothing_and_clears_pending_state(self) -> None:
        self._pend()

        cancelled = self.session.cancel()

        self.assertTrue(cancelled)
        self.assertIsNone(self.session.pending_approval)
        self.tool_manager.execute_tool.assert_not_called()
        self.memory.record_interaction.assert_not_called()
        self.planner.create_plan.assert_called_once()

    def test_second_cancellation_reports_that_nothing_was_pending(self) -> None:
        self._pend()
        self.session.cancel()

        self.assertFalse(self.session.cancel())
        self.tool_manager.execute_tool.assert_not_called()

    def test_confirm_after_cancellation_executes_nothing(self) -> None:
        self._pend()
        self.session.cancel()

        refusal = self.session.confirm()

        self.assertEqual(refusal.text, CONFIRMATION_REFUSED_MESSAGE)
        self.tool_manager.execute_tool.assert_not_called()
        self.memory.record_interaction.assert_not_called()


class InterveningRequestTests(SessionTestCase):
    """Verify a later request invalidates an earlier pending approval."""

    def _submit_intervening_request(self) -> BrainResponse:
        """Submit an unrelated allowed request while a plan is pending."""
        self.planner.create_plan.return_value = Plan("help", (), "help text")
        self.permissions.check.return_value = ALLOWED
        return self.session.submit("help")

    def test_new_request_invalidates_the_older_pending_plan(self) -> None:
        self._pend()

        intervening = self._submit_intervening_request()

        self.assertEqual(intervening.text, "help text")
        self.assertIsNone(self.session.pending_approval)

    def test_confirming_after_an_intervening_request_cannot_run_the_old_plan(
        self,
    ) -> None:
        self._pend()
        self._submit_intervening_request()

        refusal = self.session.confirm()

        self.assertEqual(refusal.text, CONFIRMATION_REFUSED_MESSAGE)
        self.tool_manager.execute_tool.assert_not_called()

    def test_approval_never_transfers_between_similar_requests(self) -> None:
        first = self._pend(_rename_plan(RENAME_REQUEST, destination="first.txt"))
        second = self._pend(_rename_plan(RENAME_REQUEST, destination="second.txt"))

        self.assertEqual(first.original_request, second.original_request)
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        self.assertIs(self.session.pending_approval.plan, second)

        self.session.confirm()

        self.tool_manager.execute_tool.assert_called_once_with(
            "filesystem",
            "rename",
            {"source": "old.txt", "destination": "second.txt"},
        )

    def test_only_one_plan_is_ever_pending(self) -> None:
        self._pend(_rename_plan(RENAME_REQUEST, destination="first.txt"))
        second = self._pend(_rename_plan(RENAME_REQUEST, destination="second.txt"))

        self.assertIs(self.session.pending_approval.plan, second)

        self.session.confirm()

        self.assertIsNone(self.session.pending_approval)
        self.assertEqual(self.session.confirm().text, CONFIRMATION_REFUSED_MESSAGE)


class UnchangedBehaviorTests(SessionTestCase):
    """Verify non-confirmation paths keep their existing behavior."""

    def test_allowed_request_executes_immediately_without_pending_state(self) -> None:
        plan = Plan(
            original_request="calculate 2 + 2",
            steps=(PlanStep("calculator", "calculate", {"expression": "2 + 2"}),),
        )
        self.planner.create_plan.return_value = plan
        self.tool_manager.execute_tool.return_value = ToolExecutionResult(True, "4")

        response = self.session.submit("calculate 2 + 2")

        self.assertEqual(response, BrainResponse("4", plan=plan))
        self.assertIsNone(self.session.pending_approval)
        self.memory.record_interaction.assert_called_once_with("calculate 2 + 2", "4")

    def test_denied_request_keeps_its_response_and_recording(self) -> None:
        plan = Plan(
            original_request="read file file.txt",
            steps=(PlanStep("filesystem", "read", {"path": "file.txt"}),),
        )
        self.planner.create_plan.return_value = plan
        self.permissions.check.return_value = DENIED

        response = self.session.submit("read file file.txt")

        expected = "Permission denied: filesystem.read is denied by configuration."
        self.assertEqual(response, BrainResponse(expected, plan=plan))
        self.assertIsNone(self.session.pending_approval)
        self.tool_manager.execute_tool.assert_not_called()
        self.memory.record_interaction.assert_called_once_with(
            "read file file.txt",
            expected,
        )

    def test_zero_step_request_keeps_its_response_and_recording(self) -> None:
        plan = Plan("help", (), "supported commands")
        self.planner.create_plan.return_value = plan

        response = self.session.submit("help")

        self.assertEqual(response, BrainResponse("supported commands", plan=plan))
        self.assertIsNone(self.session.pending_approval)
        self.permissions.check.assert_not_called()
        self.tool_manager.execute_tool.assert_not_called()
        self.memory.record_interaction.assert_called_once_with(
            "help",
            "supported commands",
        )

    def test_blank_request_keeps_its_response_and_touches_nothing(self) -> None:
        response = self.session.submit(" \t\r\n ")

        self.assertEqual(response, BrainResponse("Please enter a request."))
        self.assertIsNone(self.session.pending_approval)
        self.planner.create_plan.assert_not_called()
        self.memory.get_recent_interactions.assert_not_called()
        self.memory.record_interaction.assert_not_called()

    def test_failed_result_stops_later_steps_and_is_still_recorded(self) -> None:
        plan = Plan(
            original_request="synthetic failure",
            steps=(
                PlanStep("filesystem", "read", {"path": "missing.txt"}),
                PlanStep("system", "current_time", {}),
            ),
        )
        self.planner.create_plan.return_value = plan
        self.tool_manager.execute_tool.return_value = ToolExecutionResult(
            False,
            "File not found: missing.txt",
        )

        response = self.session.submit("synthetic failure")

        self.assertEqual(response.text, "File not found: missing.txt")
        self.tool_manager.execute_tool.assert_called_once_with(
            "filesystem",
            "read",
            {"path": "missing.txt"},
        )
        self.memory.record_interaction.assert_called_once_with(
            "synthetic failure",
            "File not found: missing.txt",
        )

    def test_unconfirmed_request_is_not_executed_or_recorded(self) -> None:
        plan = _rename_plan()
        self.planner.create_plan.return_value = plan
        self.permissions.check.return_value = NEEDS_CONFIRMATION

        response = self.session.submit(RENAME_REQUEST)

        self.assertEqual(
            response,
            BrainResponse(CONFIRMATION_TEXT, needs_confirmation=True, plan=plan),
        )
        self.tool_manager.execute_tool.assert_not_called()
        self.memory.record_interaction.assert_not_called()

    def test_cancellation_records_nothing(self) -> None:
        self._pend()

        self.session.cancel()

        self.memory.record_interaction.assert_not_called()


class HeadlessSessionTests(SessionTestCase):
    """Drive complete confirmation lifecycles with no standard input."""

    def test_headless_submit_then_confirm_completes_the_action(self) -> None:
        plan = self._pend()

        pending = self.session.pending_approval
        self.assertIsNotNone(pending)
        self.assertIs(pending.plan, plan)
        self.assertEqual(pending.fingerprint, plan.fingerprint)

        result = self.session.confirm()

        self.assertEqual(result.text, "Renamed old.txt to new.txt")
        self.assertFalse(result.needs_confirmation)
        self.assertIsNone(self.session.pending_approval)
        self.tool_manager.execute_tool.assert_called_once_with(
            "filesystem",
            "rename",
            RENAME_ARGUMENTS,
        )
        self.planner.create_plan.assert_called_once()

    def test_headless_submit_then_cancel_executes_nothing(self) -> None:
        plan = self._pend()

        pending = self.session.pending_approval
        self.assertIs(pending.plan, plan)

        self.assertTrue(self.session.cancel())

        self.assertIsNone(self.session.pending_approval)
        self.tool_manager.execute_tool.assert_not_called()
        self.memory.record_interaction.assert_not_called()
        self.planner.create_plan.assert_called_once()

    def test_session_module_needs_no_terminal_or_persistence(self) -> None:
        source = inspect.getsource(session_module)

        for forbidden in (
            "builtins.input",
            "print(",
            "sys.stdin",
            "sys.stdout",
            "sqlite3",
            "pickle",
            "json",
            "record_interaction",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


class SessionLoggingPrivacyTests(unittest.TestCase):
    """Verify no request, plan, or argument content reaches the logs."""

    def setUp(self) -> None:
        self.records: list[str] = []
        self.logger = logging.getLogger("tests.session.privacy")
        self.logger.handlers.clear()
        self.logger.addHandler(_RecordingHandler(self.records))
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.addCleanup(self.logger.handlers.clear)

        self.planner = Mock(spec=Planner)
        self.memory = Mock(spec=Memory)
        self.tool_manager = Mock(spec=ToolManager)
        # No mocked tool pins confirmation state, so the plan offered
        # for approval is the plan itself.
        self.tool_manager.prepare_confirmation.side_effect = PreparedConfirmation
        self.tool_manager.validate_action.return_value = None
        self.permissions = Mock(spec=PermissionManager)
        self.memory.get_recent_interactions.return_value = []
        self.permissions.check.return_value = NEEDS_CONFIRMATION
        self.tool_manager.execute_tool.return_value = ToolExecutionResult(True, "done")

        self.brain = MariaBrain(
            config=Mock(spec=MariaConfig),
            planner=self.planner,
            memory=self.memory,
            tool_manager=self.tool_manager,
            permissions=self.permissions,
            logger=self.logger,
        )
        self.session = MariaSession(self.brain, logger=self.logger)
        self.plan = _rename_plan(SENTINEL_REQUEST, destination=SENTINEL_ARGUMENT)
        self.planner.create_plan.return_value = self.plan

    def _assert_logs_are_clean(self) -> None:
        """Assert no captured log line leaks plan or request content."""
        logged = "\n".join(self.records)
        for secret in (
            SENTINEL_REQUEST,
            SENTINEL_ARGUMENT,
            "old.txt",
            self.plan.fingerprint,
            "Plan(",
            "PlanStep(",
            "PlanArguments(",
            "PendingApproval(",
            "original_request",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, logged)

    def test_confirmed_lifecycle_logs_no_plan_or_request_content(self) -> None:
        self.session.submit(SENTINEL_REQUEST)
        self.session.confirm()

        self.assertTrue(self.records)
        self._assert_logs_are_clean()

    def test_cancelled_lifecycle_logs_no_plan_or_request_content(self) -> None:
        self.session.submit(SENTINEL_REQUEST)
        self.session.cancel()

        self.assertTrue(self.records)
        self._assert_logs_are_clean()

    def test_refused_lifecycle_logs_no_plan_or_request_content(self) -> None:
        self.session.submit(SENTINEL_REQUEST)
        self.session._pending = PendingApproval(self.plan, "0" * 64)
        self.session.confirm()

        self.assertTrue(self.records)
        self._assert_logs_are_clean()


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
