"""Tests for where Maria enforces per-action argument contracts.

These cover the pipeline the contracts create rather than the declarations
themselves: contract validation completing before any permission decision,
confirmation revalidating the same plan, ToolManager refusing a malformed
direct call, and the tools keeping their own checks as defence in depth.
"""

from __future__ import annotations

import logging
import unittest
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import Any
from unittest.mock import Mock, patch

from core.action_contracts import (
    BUILT_IN_ACTION_CONTRACTS,
    ActionContract,
    ActionContractRegistry,
    ArgumentContract,
    ContractFailure,
    ContractViolation,
)
from core.brain import BrainResponse, MariaBrain
from core.config import MariaConfig
from core.memory import Memory
from core.permissions import (
    PermissionDecision,
    PermissionManager,
    PermissionResult,
)
from core.planner import Plan, PlanArguments, Planner, PlanStep
from core.session import CONFIRMATION_REFUSED_MESSAGE, MariaSession
from core.tool_manager import ToolExecutionResult, ToolManager
from tools.calculator import CalculatorTool
from tools.filesystem import FilesystemTool
from tools.history import HistoryTool
from tools.memory import MemoryTool
from tools.notes import NotesTool
from tools.system import SystemTool


ALLOWED = PermissionResult(PermissionDecision.ALLOWED, "allowed")
DENIED = PermissionResult(
    PermissionDecision.DENIED,
    "filesystem.read is denied by configuration.",
)
NEEDS_CONFIRMATION = PermissionResult(
    PermissionDecision.REQUIRES_CONFIRMATION,
    "filesystem.rename requires explicit confirmation.",
)

CALCULATE_STEP = ("calculator", "calculate", {"expression": "1 + 1"})
TIME_STEP = ("system", "current_time", {})
READ_STEP = ("filesystem", "read", {"path": "notes.txt"})
RENAME_STEP = ("filesystem", "rename", {"source": "old.txt", "destination": "new.txt"})
UNREGISTERED_STEP = ("filesystem", "write", {"path": "notes.txt"})
MALFORMED_STEP = ("history", "read", {})
BLANK_TEXT_STEP = ("memory", "store", {"fact": "   "})
OVER_LIMIT_STEP = ("notes", "list", {"limit": 101})

SENTINEL_PATH = "sentinel-path-5c1d97.txt"
SENTINEL_BODY = "sentinel-body-83af0e"
SENTINEL_NUMBER = 8675309

UNKNOWN_ACTION_REFUSAL = (
    "Unsupported action: filesystem.write is not a supported action."
)
MISSING_IDENTIFIER_REFUSAL = "Missing required argument for history.read: id."
BLANK_TEXT_REFUSAL = "Argument out of supported range for memory.store: fact."
OVER_LIMIT_REFUSAL = "Argument out of supported range for notes.list: limit."


def _plan(request: str, *steps: tuple[str, str, dict[str, Any]]) -> Plan:
    """Return a synthetic plan whose steps keep the order given here."""
    return Plan(
        original_request=request,
        steps=tuple(
            PlanStep(tool_name, action, arguments)
            for tool_name, action, arguments in steps
        ),
    )


def _temporary_config(root: Path) -> MariaConfig:
    """Return a complete config whose paths remain under a temporary root."""
    return MariaConfig(
        app_name="Maria",
        version="0.2.0",
        environment="test",
        database_path=root / "database" / "maria.db",
        log_dir=root / "logs",
        prompt_path=root / "system_prompt.txt",
        safe_filesystem_root=root,
        log_level="INFO",
        confirmation_required=frozenset(),
        denied_actions=frozenset(),
    )


class _RecordingHandler(logging.Handler):
    """Collect formatted log records for privacy assertions."""

    def __init__(self, records: list[str]) -> None:
        super().__init__()
        self.records = records

    def emit(self, record: logging.LogRecord) -> None:
        """Store one fully formatted record."""
        self.records.append(record.getMessage())


class PipelineTestCase(unittest.TestCase):
    """Drive a real Brain and session over fully mocked collaborators.

    Every contract check, permission check, and dispatch appends to one
    shared event list, so a single assertion on that list proves the relative
    order of the three phases rather than only their contents.
    """

    def setUp(self) -> None:
        self.events: list[str] = []
        self.decisions: dict[str, PermissionResult] = {}
        self.planner = Mock(spec=Planner)
        self.memory = Mock(spec=Memory)
        self.tool_manager = Mock(spec=ToolManager)
        self.permissions = Mock(spec=PermissionManager)
        self.logger = Mock(spec=logging.Logger)

        self.memory.get_recent_interactions.return_value = []
        self.permissions.check.side_effect = self._check
        self.tool_manager.execute_tool.side_effect = self._dispatch

        # Brain must reach the contracts through Tool Manager, so the events
        # are recorded on that call rather than on a patched module global.
        self.registry = BUILT_IN_ACTION_CONTRACTS
        self.tool_manager.validate_action.side_effect = self._validate

        self.brain = MariaBrain(
            config=Mock(spec=MariaConfig),
            planner=self.planner,
            memory=self.memory,
            tool_manager=self.tool_manager,
            permissions=self.permissions,
            logger=self.logger,
        )
        self.session = MariaSession(self.brain, logger=self.logger)

    def _validate(
        self,
        tool_name: str,
        action: str,
        arguments: Any,
    ) -> ContractViolation | None:
        """Record one contract check and answer from the active registry."""
        self.events.append(f"contract:{tool_name}.{action}")
        return self.registry.validate(tool_name, action, arguments)

    def _check(self, tool_name: str, action: str) -> PermissionResult:
        """Record and answer one permission check."""
        self.events.append(f"permission:{tool_name}.{action}")
        return self.decisions.get(f"{tool_name}.{action}", ALLOWED)

    def _dispatch(
        self,
        tool_name: str,
        action: str,
        arguments: Any,
    ) -> ToolExecutionResult:
        """Record and answer one tool dispatch."""
        del arguments
        self.events.append(f"dispatch:{tool_name}.{action}")
        return ToolExecutionResult(True, f"ran {tool_name}.{action}")

    def _submit(self, plan: Plan) -> BrainResponse:
        """Plan is returned for the next submitted request."""
        self.planner.create_plan.return_value = plan
        return self.session.submit(plan.original_request)

    def _contract_events(self) -> list[str]:
        """Return only the contract events recorded so far."""
        return [event for event in self.events if event.startswith("contract:")]


class PipelineOrderTests(PipelineTestCase):
    """Verify contract validation completes before any permission check."""

    def test_valid_plan_runs_contracts_then_permissions_then_dispatch(self) -> None:
        plan = _plan("synthetic success", CALCULATE_STEP, TIME_STEP)

        self._submit(plan)

        self.assertEqual(
            self.events,
            [
                "contract:calculator.calculate",
                "contract:system.current_time",
                "permission:calculator.calculate",
                "permission:system.current_time",
                "dispatch:calculator.calculate",
                "dispatch:system.current_time",
            ],
        )

    def test_no_permission_event_precedes_the_last_contract_event(self) -> None:
        plan = _plan("synthetic success", CALCULATE_STEP, TIME_STEP)

        self._submit(plan)

        last_contract = max(
            index
            for index, event in enumerate(self.events)
            if event.startswith("contract:")
        )
        first_permission = min(
            index
            for index, event in enumerate(self.events)
            if event.startswith("permission:")
        )
        self.assertLess(last_contract, first_permission)

    def test_malformed_second_step_causes_no_permission_or_dispatch(self) -> None:
        plan = _plan("synthetic malformed", CALCULATE_STEP, MALFORMED_STEP)

        response = self._submit(plan)

        self.assertEqual(
            self.events,
            ["contract:calculator.calculate", "contract:history.read"],
        )
        self.assertEqual(response.text, MISSING_IDENTIFIER_REFUSAL)
        self.assertFalse(response.needs_confirmation)
        self.permissions.check.assert_not_called()
        self.tool_manager.execute_tool.assert_not_called()
        self.assertIsNone(self.session.pending_approval)

    def test_blank_text_in_a_later_step_causes_no_permission_or_dispatch(
        self,
    ) -> None:
        plan = _plan("synthetic blank text", CALCULATE_STEP, BLANK_TEXT_STEP)

        response = self._submit(plan)

        self.assertEqual(
            self.events,
            ["contract:calculator.calculate", "contract:memory.store"],
        )
        self.assertEqual(response.text, BLANK_TEXT_REFUSAL)
        self.permissions.check.assert_not_called()
        self.tool_manager.execute_tool.assert_not_called()
        self.assertIsNone(self.session.pending_approval)

    def test_out_of_range_limit_in_a_later_step_causes_no_permission_or_dispatch(
        self,
    ) -> None:
        plan = _plan("synthetic over limit", CALCULATE_STEP, OVER_LIMIT_STEP)

        response = self._submit(plan)

        self.assertEqual(
            self.events,
            ["contract:calculator.calculate", "contract:notes.list"],
        )
        self.assertEqual(response.text, OVER_LIMIT_REFUSAL)
        self.permissions.check.assert_not_called()
        self.tool_manager.execute_tool.assert_not_called()
        self.assertIsNone(self.session.pending_approval)

    def test_over_long_expression_is_refused_before_permissions(self) -> None:
        plan = _plan(
            "synthetic long expression",
            ("calculator", "calculate", {"expression": "1" * 201}),
        )

        response = self._submit(plan)

        self.assertEqual(self.events, ["contract:calculator.calculate"])
        self.assertEqual(
            response.text,
            "Argument out of supported range for calculator.calculate: expression.",
        )
        self.permissions.check.assert_not_called()
        self.tool_manager.execute_tool.assert_not_called()

    def test_denied_second_step_still_completes_every_permission_check(self) -> None:
        plan = _plan("synthetic denial", CALCULATE_STEP, READ_STEP)
        self.decisions["filesystem.read"] = DENIED

        response = self._submit(plan)

        self.assertEqual(
            self.events,
            [
                "contract:calculator.calculate",
                "contract:filesystem.read",
                "permission:calculator.calculate",
                "permission:filesystem.read",
            ],
        )
        self.assertEqual(
            response.text,
            "Permission denied: filesystem.read is denied by configuration.",
        )
        self.tool_manager.execute_tool.assert_not_called()

    def test_unregistered_action_is_refused_before_permissions(self) -> None:
        plan = _plan("synthetic unregistered", UNREGISTERED_STEP)

        response = self._submit(plan)

        self.assertEqual(self.events, ["contract:filesystem.write"])
        self.assertEqual(response.text, UNKNOWN_ACTION_REFUSAL)
        self.permissions.check.assert_not_called()
        self.tool_manager.execute_tool.assert_not_called()
        self.assertIsNone(self.session.pending_approval)

    def test_unregistered_action_after_a_valid_step_checks_no_permission(self) -> None:
        plan = _plan("synthetic unregistered later", CALCULATE_STEP, UNREGISTERED_STEP)

        response = self._submit(plan)

        self.assertEqual(
            self.events,
            ["contract:calculator.calculate", "contract:filesystem.write"],
        )
        self.assertEqual(response.text, UNKNOWN_ACTION_REFUSAL)
        self.permissions.check.assert_not_called()
        self.tool_manager.execute_tool.assert_not_called()

    def test_contract_refusal_is_recorded_through_the_generic_path(self) -> None:
        plan = _plan("write notes.txt now", UNREGISTERED_STEP)

        self._submit(plan)

        self.memory.record_interaction.assert_called_once_with(
            "write notes.txt now",
            UNKNOWN_ACTION_REFUSAL,
        )

    def test_contract_refusal_leaves_the_plan_and_fingerprint_untouched(self) -> None:
        plan = _plan("synthetic malformed", CALCULATE_STEP, MALFORMED_STEP)
        before = plan.fingerprint

        response = self._submit(plan)

        self.assertEqual(plan.fingerprint, before)
        self.assertIs(response.plan, plan)
        self.assertEqual(dict(plan.steps[0].arguments), {"expression": "1 + 1"})
        self.assertEqual(dict(plan.steps[1].arguments), {})

    def test_zero_step_plan_runs_no_contract_check(self) -> None:
        plan = Plan("help", (), "supported commands")

        response = self._submit(plan)

        self.assertEqual(self.events, [])
        self.assertEqual(response.text, "supported commands")


class ContractConfirmationTests(PipelineTestCase):
    """Verify a confirmed plan is validated again before it is dispatched."""

    def setUp(self) -> None:
        super().setUp()
        self.decisions["filesystem.rename"] = NEEDS_CONFIRMATION

    def test_confirmation_required_plan_is_validated_before_permissions(self) -> None:
        plan = _plan("rename old.txt to new.txt", CALCULATE_STEP, RENAME_STEP)

        response = self._submit(plan)

        self.assertTrue(response.needs_confirmation)
        self.assertEqual(
            self.events,
            [
                "contract:calculator.calculate",
                "contract:filesystem.rename",
                "permission:calculator.calculate",
                "permission:filesystem.rename",
            ],
        )
        self.tool_manager.execute_tool.assert_not_called()

    def test_session_holds_the_exact_immutable_plan(self) -> None:
        plan = _plan("rename old.txt to new.txt", RENAME_STEP)

        self._submit(plan)

        pending = self.session.pending_approval
        self.assertIs(pending.plan, plan)
        self.assertEqual(pending.fingerprint, plan.fingerprint)

    def test_confirmation_revalidates_the_whole_plan_before_permissions(self) -> None:
        plan = _plan("rename old.txt to new.txt", CALCULATE_STEP, RENAME_STEP)
        self._submit(plan)
        self.events.clear()
        self.planner.create_plan.reset_mock()
        self.memory.get_recent_interactions.reset_mock()

        self.session.confirm()

        self.assertEqual(
            self.events,
            [
                "contract:calculator.calculate",
                "contract:filesystem.rename",
                "permission:calculator.calculate",
                "permission:filesystem.rename",
                "dispatch:calculator.calculate",
                "dispatch:filesystem.rename",
            ],
        )
        self.planner.create_plan.assert_not_called()
        self.memory.get_recent_interactions.assert_not_called()

    def test_each_approved_step_is_dispatched_exactly_once_in_plan_order(self) -> None:
        plan = _plan("rename old.txt to new.txt", CALCULATE_STEP, RENAME_STEP)
        self._submit(plan)

        self.session.confirm()

        dispatched = [
            event for event in self.events if event.startswith("dispatch:")
        ]
        self.assertEqual(
            dispatched,
            ["dispatch:calculator.calculate", "dispatch:filesystem.rename"],
        )

    def test_contract_failure_at_confirmation_dispatches_nothing(self) -> None:
        plan = _plan("rename old.txt to new.txt", RENAME_STEP)
        self._submit(plan)
        self.events.clear()
        self.registry = ActionContractRegistry(
            (ActionContract("filesystem", "read"),)
        )

        response = self.session.confirm()

        self.assertEqual(self.events, ["contract:filesystem.rename"])
        self.assertEqual(
            response.text,
            "Unsupported action: filesystem.rename is not a supported action.",
        )
        self.tool_manager.execute_tool.assert_not_called()

    def test_confirmation_revalidates_text_and_numeric_boundaries(self) -> None:
        plan = _plan("rename old.txt to new.txt", RENAME_STEP)
        self._submit(plan)
        self.events.clear()
        self.registry = ActionContractRegistry(
            (
                ActionContract(
                    "filesystem",
                    "rename",
                    (
                        ArgumentContract(
                            "source",
                            str,
                            required=True,
                            allow_blank=False,
                            minimum_length=None,
                            maximum_length=3,
                        ),
                        ArgumentContract(
                            "destination",
                            str,
                            required=True,
                            allow_blank=False,
                            minimum_length=None,
                            maximum_length=None,
                        ),
                    ),
                ),
            )
        )

        response = self.session.confirm()

        self.assertEqual(self.events, ["contract:filesystem.rename"])
        self.assertEqual(
            response.text,
            "Argument out of supported range for filesystem.rename: source.",
        )
        self.tool_manager.execute_tool.assert_not_called()
        self.planner.create_plan.assert_called_once()
        self.assertIsNone(self.session.pending_approval)
        self.assertEqual(
            self.session.confirm().text,
            CONFIRMATION_REFUSED_MESSAGE,
        )

    def test_consumed_approval_is_not_replayable_after_a_contract_failure(
        self,
    ) -> None:
        plan = _plan("rename old.txt to new.txt", RENAME_STEP)
        self._submit(plan)
        self.registry = ActionContractRegistry(
            (ActionContract("filesystem", "read"),)
        )
        self.session.confirm()

        replay = self.session.confirm()

        self.assertEqual(replay.text, CONFIRMATION_REFUSED_MESSAGE)
        self.assertIsNone(self.session.pending_approval)
        self.tool_manager.execute_tool.assert_not_called()


class ContractPrivacyTests(unittest.TestCase):
    """Verify no submitted value reaches a response or a log record."""

    def setUp(self) -> None:
        self.records: list[str] = []
        self.logger = logging.getLogger("tests.contract_enforcement.privacy")
        self.logger.handlers.clear()
        self.logger.addHandler(_RecordingHandler(self.records))
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.addCleanup(self.logger.handlers.clear)

    def test_brain_refusal_logs_only_the_kind_action_and_field(self) -> None:
        planner = Mock(spec=Planner)
        memory = Mock(spec=Memory)
        tool_manager = Mock(spec=ToolManager)
        tool_manager.validate_action.side_effect = (
            BUILT_IN_ACTION_CONTRACTS.validate
        )
        permissions = Mock(spec=PermissionManager)
        memory.get_recent_interactions.return_value = []
        plan = _plan(
            "synthetic privacy",
            ("notes", "edit", {"id": 4, "body": SENTINEL_BODY, "surplus": 1}),
        )
        planner.create_plan.return_value = plan
        brain = MariaBrain(
            config=Mock(spec=MariaConfig),
            planner=planner,
            memory=memory,
            tool_manager=tool_manager,
            permissions=permissions,
            logger=self.logger,
        )

        response = brain.process("synthetic privacy")

        logged = "\n".join(self.records)
        self.assertEqual(
            response.text,
            "Unsupported argument for notes.edit: surplus.",
        )
        self.assertTrue(self.records)
        self.assertIn("extra_argument", logged)
        self.assertIn("notes", logged)
        self.assertIn("surplus", logged)
        for secret in (
            SENTINEL_BODY,
            plan.fingerprint,
            "synthetic privacy",
            "Plan(",
            "PlanStep(",
            "PlanArguments(",
            "original_request",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, logged)
                self.assertNotIn(secret, response.text)

    def test_tool_manager_refusal_logs_no_submitted_value(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            config = _temporary_config(root)
            memory = Memory(config.database_path, logger=Mock(spec=logging.Logger))
            memory.initialize()
            manager = ToolManager(config, memory, self.logger)
            manager.load_tools()
            self.records.clear()

            result = manager.execute_tool(
                "filesystem",
                "read",
                {"path": SENTINEL_PATH, "surplus": SENTINEL_BODY},
            )

        logged = "\n".join(self.records)
        self.assertFalse(result.success)
        self.assertEqual(
            result.message,
            "Unsupported argument for filesystem.read: surplus.",
        )
        self.assertIn("extra_argument", logged)
        for secret in (SENTINEL_PATH, SENTINEL_BODY, "object at 0x"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, logged)
                self.assertNotIn(secret, result.message)


class DirectToolManagerContractTests(unittest.TestCase):
    """Verify the dispatch boundary refuses malformed direct calls."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.config = _temporary_config(self.root)
        self.memory = Memory(
            self.config.database_path,
            logger=Mock(spec=logging.Logger),
        )
        self.memory.initialize()
        self.manager = ToolManager(
            self.config,
            self.memory,
            Mock(spec=logging.Logger),
        )
        self.manager.load_tools()

    def test_unregistered_filesystem_write_never_reaches_the_tool(self) -> None:
        with patch.object(FilesystemTool, "execute") as execute:
            result = self.manager.execute_tool(
                "filesystem",
                "write",
                {"path": SENTINEL_PATH},
            )

        self.assertEqual(
            result,
            ToolExecutionResult(False, UNKNOWN_ACTION_REFUSAL),
        )
        execute.assert_not_called()
        self.assertNotIn(SENTINEL_PATH, result.message)

    def test_each_malformed_direct_call_is_refused_without_reaching_a_tool(
        self,
    ) -> None:
        cases = (
            (
                "wrong type",
                ("calculator", "calculate", {"expression": 42}),
                "Invalid argument type for calculator.calculate: expression.",
                CalculatorTool,
            ),
            (
                "extra field",
                ("system", "current_time", {"timezone": "UTC"}),
                "Unsupported argument for system.current_time: timezone.",
                None,
            ),
            (
                "missing required field",
                ("history", "read", {}),
                MISSING_IDENTIFIER_REFUSAL,
                HistoryTool,
            ),
            (
                "out of range value",
                ("notes", "delete", {"id": 0}),
                "Argument out of supported range for notes.delete: id.",
                NotesTool,
            ),
            (
                "unknown tool",
                ("scheduler", "run", {}),
                "Unsupported action: scheduler is not a supported tool.",
                None,
            ),
            (
                "unknown action",
                ("memory", "purge", {"id": 1}),
                "Unsupported action: memory.purge is not a supported action.",
                MemoryTool,
            ),
        )
        for label, call, expected, tool_class in cases:
            with self.subTest(case=label):
                tool_name, action, arguments = call
                if tool_class is None:
                    result = self.manager.execute_tool(tool_name, action, arguments)
                else:
                    with patch.object(tool_class, "execute") as execute:
                        result = self.manager.execute_tool(
                            tool_name, action, arguments
                        )
                    execute.assert_not_called()

                self.assertEqual(result, ToolExecutionResult(False, expected))

    def test_boundary_violations_are_refused_at_the_dispatch_boundary(self) -> None:
        cases = (
            (
                "blank text",
                ("memory", "store", {"fact": "   "}),
                BLANK_TEXT_REFUSAL,
                MemoryTool,
            ),
            (
                "blank path",
                ("filesystem", "read", {"path": " \t "}),
                "Argument out of supported range for filesystem.read: path.",
                FilesystemTool,
            ),
            (
                "blank optional title",
                ("notes", "create", {"title": "  ", "body": "Body"}),
                "Argument out of supported range for notes.create: title.",
                NotesTool,
            ),
            (
                "over-long expression",
                ("calculator", "calculate", {"expression": "1" * 201}),
                "Argument out of supported range for calculator.calculate: "
                "expression.",
                CalculatorTool,
            ),
            (
                "limit below the window",
                ("notes", "list", {"limit": 0}),
                "Argument out of supported range for notes.list: limit.",
                NotesTool,
            ),
            (
                "limit above the window",
                ("memory", "list", {"limit": 101}),
                "Argument out of supported range for memory.list: limit.",
                MemoryTool,
            ),
            (
                "text limit",
                ("memory", "list", {"limit": "5"}),
                "Invalid argument type for memory.list: limit.",
                MemoryTool,
            ),
            (
                "bool limit",
                ("notes", "list", {"limit": True}),
                "Invalid argument type for notes.list: limit.",
                NotesTool,
            ),
        )
        for label, call, expected, tool_class in cases:
            with self.subTest(case=label):
                tool_name, action, arguments = call
                with patch.object(tool_class, "execute") as execute:
                    result = self.manager.execute_tool(tool_name, action, arguments)

                execute.assert_not_called()
                self.assertEqual(result, ToolExecutionResult(False, expected))

    def test_limit_window_edges_reach_the_tool_unchanged(self) -> None:
        self.memory.create_note("Kept", "Body")

        for limit in (1, 100):
            with self.subTest(limit=limit):
                result = self.manager.execute_tool("notes", "list", {"limit": limit})

                self.assertTrue(result.success)
                self.assertEqual(len(result.data), 1)

    def test_valid_direct_call_keeps_its_result_and_argument_identity(self) -> None:
        arguments = {"expression": "6 * 7"}
        received: list[Any] = []
        original = CalculatorTool.execute

        def recording(tool, action, forwarded):
            received.append(forwarded)
            return original(tool, action, forwarded)

        with patch.object(CalculatorTool, "execute", recording):
            result = self.manager.execute_tool(
                "calculator",
                "calculate",
                arguments,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.message, "6 * 7 = 42")
        self.assertEqual(arguments, {"expression": "6 * 7"})
        self.assertEqual(len(received), 1)
        self.assertIs(received[0], arguments)

    def test_valid_optional_field_still_reaches_the_tool_default(self) -> None:
        note_id = self.memory.create_note("Kept", "Body")

        listed = self.manager.execute_tool("notes", "list", {})

        self.assertTrue(listed.success)
        self.assertEqual([note["id"] for note in listed.data], [note_id])

    def test_malformed_call_performs_no_persistent_work(self) -> None:
        self.manager.execute_tool("notes", "create", {"body": 7})

        self.assertEqual(self.memory.list_notes(limit=5), [])

    def test_validate_action_reports_without_executing(self) -> None:
        with patch.object(CalculatorTool, "execute") as execute:
            valid = self.manager.validate_action(
                "calculator",
                "calculate",
                {"expression": "1 + 1"},
            )
            invalid = self.manager.validate_action(
                "calculator",
                "calculate",
                {"expression": 1},
            )

        self.assertIsNone(valid)
        self.assertEqual(
            invalid.message,
            "Invalid argument type for calculator.calculate: expression.",
        )
        execute.assert_not_called()


class ActiveRegistryTests(unittest.TestCase):
    """Verify Brain validates against Tool Manager's own active registry."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.config = _temporary_config(self.root)
        self.planner = Mock(spec=Planner)
        self.memory = Mock(spec=Memory)
        self.permissions = Mock(spec=PermissionManager)
        self.memory.get_recent_interactions.return_value = []
        self.permissions.check.return_value = ALLOWED

    def _brain_over(self, registry: ActionContractRegistry) -> MariaBrain:
        """Wire a real Brain over a real ToolManager holding one registry."""
        manager = ToolManager(
            self.config,
            self.memory,
            Mock(spec=logging.Logger),
            contracts=registry,
        )
        manager.load_tools()
        return MariaBrain(
            config=self.config,
            planner=self.planner,
            memory=self.memory,
            tool_manager=manager,
            permissions=self.permissions,
            logger=Mock(spec=logging.Logger),
        )

    def test_brain_uses_the_managers_registry_rather_than_the_global_one(
        self,
    ) -> None:
        narrowed = ActionContractRegistry(
            (
                ActionContract(
                    "calculator",
                    "calculate",
                    (
                        ArgumentContract(
                            "expression",
                            str,
                            required=True,
                            allow_blank=False,
                            minimum_length=None,
                            maximum_length=None,
                        ),
                    ),
                ),
                ActionContract("system", "overview"),
            )
        )
        plan = _plan("synthetic active registry", CALCULATE_STEP, TIME_STEP)
        self.planner.create_plan.return_value = plan
        brain = self._brain_over(narrowed)
        session = MariaSession(brain, logger=Mock(spec=logging.Logger))

        # The shipped registry would accept both steps of this plan.
        self.assertIsNone(
            BUILT_IN_ACTION_CONTRACTS.validate("calculator", "calculate", {
                "expression": "1 + 1",
            })
        )
        self.assertIsNone(
            BUILT_IN_ACTION_CONTRACTS.validate("system", "current_time", {})
        )

        with (
            patch.object(CalculatorTool, "execute") as calculate,
            patch.object(SystemTool, "execute") as current_time,
        ):
            response = session.submit("synthetic active registry")

        self.assertEqual(
            response.text,
            "Unsupported action: system.current_time is not a supported action.",
        )
        self.permissions.check.assert_not_called()
        calculate.assert_not_called()
        current_time.assert_not_called()
        self.assertIsNone(session.pending_approval)
        self.memory.record_interaction.assert_called_once_with(
            "synthetic active registry",
            "Unsupported action: system.current_time is not a supported action.",
        )

    def test_the_same_plan_passes_when_the_registry_declares_both_steps(
        self,
    ) -> None:
        plan = _plan("synthetic active registry", CALCULATE_STEP, TIME_STEP)
        self.planner.create_plan.return_value = plan
        brain = self._brain_over(BUILT_IN_ACTION_CONTRACTS)
        session = MariaSession(brain, logger=Mock(spec=logging.Logger))

        response = session.submit("synthetic active registry")

        self.assertIn("1 + 1 = 2", response.text)
        self.assertEqual(self.permissions.check.call_count, 2)


class MalformedArgumentContainerTests(unittest.TestCase):
    """Verify a non-Mapping or badly keyed container fails closed."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.config = _temporary_config(self.root)
        self.records: list[str] = []
        self.logger = logging.getLogger("tests.contract_enforcement.container")
        self.logger.handlers.clear()
        self.logger.addHandler(_RecordingHandler(self.records))
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.addCleanup(self.logger.handlers.clear)
        self.memory = Memory(
            self.config.database_path,
            logger=Mock(spec=logging.Logger),
        )
        self.memory.initialize()
        self.manager = ToolManager(self.config, self.memory, self.logger)
        self.manager.load_tools()

    def test_every_malformed_container_is_refused_without_raising(self) -> None:
        probe = _ConvertingKey()
        containers = (
            ("none", None),
            ("list", []),
            ("tuple", ()),
            ("text", "expression"),
            ("all int keys", {1: "x"}),
            ("mixed keys", {"a": "x", 1: "y"}),
            ("converting key", {probe: "x"}),
        )
        for label, container in containers:
            with self.subTest(container=label):
                violation = BUILT_IN_ACTION_CONTRACTS.validate(
                    "system", "overview", container
                )

                self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)
                self.assertEqual(violation.field, "arguments")
                self.assertEqual(
                    violation.message,
                    "Invalid argument type for system.overview: arguments.",
                )
        self.assertFalse(probe.converted)

    def test_tool_manager_refuses_a_malformed_container_without_a_tool(
        self,
    ) -> None:
        for label, container in (
            ("none", None),
            ("list", []),
            ("tuple", ()),
            ("bad key beside a valid one", {"path": "a.txt", 2: "b"}),
        ):
            with self.subTest(container=label):
                with patch.object(FilesystemTool, "execute") as execute:
                    result = self.manager.execute_tool(
                        "filesystem", "read", container
                    )

                execute.assert_not_called()
                self.assertEqual(
                    result,
                    ToolExecutionResult(
                        False,
                        "Invalid argument type for filesystem.read: arguments.",
                    ),
                )

    def test_every_hostile_container_is_refused_at_the_dispatch_boundary(
        self,
    ) -> None:
        colliding = _CollidingKey()
        colliding_arguments = {colliding: "x"}
        colliding.calls.clear()
        cases = (
            (
                "class-spoofing container",
                ("system", "overview", _MappingSpoof()),
                "Invalid argument type for system.overview: arguments.",
                SystemTool,
            ),
            (
                "failing iteration",
                ("system", "overview", _FailingIterationMapping()),
                "Invalid argument type for system.overview: arguments.",
                SystemTool,
            ),
            (
                "failing lookup",
                ("calculator", "calculate", _FailingLookupMapping()),
                "Invalid argument type for calculator.calculate: arguments.",
                CalculatorTool,
            ),
            (
                "hostile str subclass key",
                ("system", "overview", {_HostileNameKey("overview"): 1}),
                "Invalid argument type for system.overview: arguments.",
                SystemTool,
            ),
            (
                "colliding key outranked by a required field",
                ("calculator", "calculate", colliding_arguments),
                "Missing required argument for calculator.calculate: expression.",
                CalculatorTool,
            ),
        )
        for label, call, expected, tool_class in cases:
            with self.subTest(case=label):
                tool_name, action, arguments = call
                with patch.object(tool_class, "execute") as execute:
                    result = self.manager.execute_tool(
                        tool_name, action, arguments
                    )

                execute.assert_not_called()
                self.assertEqual(result, ToolExecutionResult(False, expected))

        self.assertEqual(colliding.calls, [])

    def test_ordinary_mapping_kinds_still_dispatch_normally(self) -> None:
        containers = (
            ("dict", {"expression": "6 * 7"}),
            ("mapping proxy", MappingProxyType({"expression": "6 * 7"})),
            ("plan arguments", PlanArguments({"expression": "6 * 7"})),
        )
        for label, container in containers:
            with self.subTest(container=label):
                result = self.manager.execute_tool(
                    "calculator", "calculate", container
                )

                self.assertTrue(result.success)
                self.assertEqual(result.message, "6 * 7 = 42")

    def test_a_memory_error_propagates_and_runs_no_concrete_tool(self) -> None:
        for label, container in (
            ("enumeration", _MemoryErrorIterationMapping()),
            ("retrieval", _MemoryErrorLookupMapping()),
        ):
            with self.subTest(operation=label):
                with patch.object(CalculatorTool, "execute") as execute:
                    with self.assertRaises(MemoryError):
                        self.manager.execute_tool(
                            "calculator", "calculate", container
                        )

                execute.assert_not_called()

    def test_required_fields_are_reported_before_key_safety(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "history", "read", {1: "x"}
        )

        self.assertEqual(
            violation.kind,
            ContractFailure.MISSING_REQUIRED_ARGUMENT,
        )
        self.assertEqual(violation.field, "id")


class UnsafeMetadataTests(unittest.TestCase):
    """Verify no unsafe tool, action, or field name reaches a response."""

    UNSAFE_NAMES = (
        ("windows path", "C:\\Users\\ExC\\secret.txt"),
        ("posix path", "/etc/passwd"),
        ("newline", "field\nInjected: line"),
        ("control", "field\x07bell"),
        ("whitespace", "two words"),
        ("empty", ""),
        ("oversized", "z" * 500),
    )

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.config = _temporary_config(self.root)
        self.records: list[str] = []
        self.logger = logging.getLogger("tests.contract_enforcement.metadata")
        self.logger.handlers.clear()
        self.logger.addHandler(_RecordingHandler(self.records))
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.addCleanup(self.logger.handlers.clear)

    def _brain(self) -> tuple[MariaBrain, Mock, Mock]:
        """Wire a real Brain over mocks that answer from the real registry."""
        planner = Mock(spec=Planner)
        memory = Mock(spec=Memory)
        tool_manager = Mock(spec=ToolManager)
        permissions = Mock(spec=PermissionManager)
        memory.get_recent_interactions.return_value = []
        tool_manager.validate_action.side_effect = (
            BUILT_IN_ACTION_CONTRACTS.validate
        )
        brain = MariaBrain(
            config=Mock(spec=MariaConfig),
            planner=planner,
            memory=memory,
            tool_manager=tool_manager,
            permissions=permissions,
            logger=self.logger,
        )
        return brain, planner, memory

    def test_unsafe_tool_names_never_reach_a_response_or_a_log(self) -> None:
        for label, unsafe in self.UNSAFE_NAMES:
            with self.subTest(name=label):
                self.records.clear()
                brain, planner, memory = self._brain()
                planner.create_plan.return_value = _plan(
                    "synthetic unsafe tool",
                    (unsafe, "run", {}),
                )

                response = brain.process("synthetic unsafe tool")

                logged = "\n".join(self.records)
                recorded = memory.record_interaction.call_args[0][1]
                self.assertEqual(
                    response.text,
                    "Unsupported action: unknown_tool is not a supported tool.",
                )
                self.assertEqual(recorded, response.text)
                for haystack in (response.text, recorded, logged):
                    if unsafe:
                        self.assertNotIn(unsafe, haystack)

    def test_unsafe_action_names_never_reach_a_response_or_a_log(self) -> None:
        for label, unsafe in self.UNSAFE_NAMES:
            with self.subTest(name=label):
                self.records.clear()
                brain, planner, memory = self._brain()
                planner.create_plan.return_value = _plan(
                    "synthetic unsafe action",
                    ("filesystem", unsafe, {}),
                )

                response = brain.process("synthetic unsafe action")

                logged = "\n".join(self.records)
                recorded = memory.record_interaction.call_args[0][1]
                self.assertEqual(
                    response.text,
                    "Unsupported action: filesystem.unknown_action is not a "
                    "supported action.",
                )
                self.assertEqual(recorded, response.text)
                for haystack in (response.text, recorded, logged):
                    if unsafe:
                        self.assertNotIn(unsafe, haystack)

    def test_unsafe_extra_field_names_never_reach_a_response_or_a_log(
        self,
    ) -> None:
        for label, unsafe in self.UNSAFE_NAMES:
            if not unsafe:
                continue
            with self.subTest(name=label):
                self.records.clear()
                brain, planner, memory = self._brain()
                planner.create_plan.return_value = _plan(
                    "synthetic unsafe field",
                    ("system", "overview", {unsafe: "value"}),
                )

                response = brain.process("synthetic unsafe field")

                logged = "\n".join(self.records)
                recorded = memory.record_interaction.call_args[0][1]
                self.assertEqual(
                    response.text,
                    "Unsupported argument for system.overview: "
                    "unsupported_field.",
                )
                self.assertEqual(recorded, response.text)
                for haystack in (response.text, recorded, logged):
                    self.assertNotIn(unsafe, haystack)

    def test_ordinary_identifier_names_keep_their_exact_messages(self) -> None:
        cases = (
            (
                ("scheduler", "run", {}),
                "Unsupported action: scheduler is not a supported tool.",
            ),
            (
                ("filesystem", "write", {"path": "notes.txt"}),
                "Unsupported action: filesystem.write is not a supported action.",
            ),
            (
                ("notes", "edit", {"id": 1, "body": "b", "surplus": 2}),
                "Unsupported argument for notes.edit: surplus.",
            ),
            (
                ("system", "current_time", {"timezone": "UTC"}),
                "Unsupported argument for system.current_time: timezone.",
            ),
            (
                ("calculator", "calculate", {"expression": 7}),
                "Invalid argument type for calculator.calculate: expression.",
            ),
        )
        for step, expected in cases:
            with self.subTest(action=f"{step[0]}.{step[1]}"):
                brain, planner, _ = self._brain()
                planner.create_plan.return_value = _plan("synthetic safe", step)

                self.assertEqual(
                    brain.process("synthetic safe").text,
                    expected,
                )

    def test_tool_manager_also_replaces_unsafe_metadata(self) -> None:
        memory = Memory(
            self.config.database_path,
            logger=Mock(spec=logging.Logger),
        )
        memory.initialize()
        manager = ToolManager(self.config, memory, self.logger)
        manager.load_tools()

        for label, unsafe in self.UNSAFE_NAMES:
            if not unsafe:
                continue
            with self.subTest(name=label):
                self.records.clear()

                result = manager.execute_tool(
                    "system",
                    "overview",
                    {unsafe: "value"},
                )

                logged = "\n".join(self.records)
                self.assertEqual(
                    result.message,
                    "Unsupported argument for system.overview: "
                    "unsupported_field.",
                )
                self.assertNotIn(unsafe, result.message)
                self.assertNotIn(unsafe, logged)


class MalformedIdentifierPipelineTests(unittest.TestCase):
    """Verify a malformed or spoofed identifier is refused before anything runs."""

    UNKNOWN_TOOL_REFUSAL = (
        "Unsupported action: unknown_tool is not a supported tool."
    )
    UNKNOWN_ACTION_REFUSAL = (
        "Unsupported action: filesystem.unknown_action is not a supported action."
    )

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.config = _temporary_config(self.root)
        self.records: list[str] = []
        self.logger = logging.getLogger("tests.contract_enforcement.identifier")
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
        self.tool_manager.validate_action.side_effect = (
            BUILT_IN_ACTION_CONTRACTS.validate
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

    def _submit(self, tool_name: Any, action: Any) -> BrainResponse:
        """Plan one synthetic step carrying the given identifiers."""
        self.planner.create_plan.return_value = _plan(
            "synthetic identifier",
            (tool_name, action, {}),
        )
        return self.session.submit("synthetic identifier")

    def test_a_malformed_tool_name_runs_nothing_through_brain(self) -> None:
        for label, value in (
            ("list", []),
            ("dict", {}),
            ("set", {1}),
            ("none", None),
            ("int", 7),
            ("bool", True),
        ):
            with self.subTest(tool_name=label):
                self.permissions.reset_mock()
                self.tool_manager.execute_tool.reset_mock()
                self.memory.record_interaction.reset_mock()

                response = self._submit(value, "run")

                self.assertEqual(response.text, self.UNKNOWN_TOOL_REFUSAL)
                self.permissions.check.assert_not_called()
                self.tool_manager.execute_tool.assert_not_called()
                self.assertIsNone(self.session.pending_approval)
                self.memory.record_interaction.assert_called_once_with(
                    "synthetic identifier",
                    self.UNKNOWN_TOOL_REFUSAL,
                )

    def test_a_malformed_action_runs_nothing_through_brain(self) -> None:
        for label, value in (
            ("list", []),
            ("dict", {}),
            ("none", None),
            ("int", 7),
        ):
            with self.subTest(action=label):
                self.permissions.reset_mock()
                self.tool_manager.execute_tool.reset_mock()
                self.memory.record_interaction.reset_mock()

                response = self._submit("filesystem", value)

                self.assertEqual(response.text, self.UNKNOWN_ACTION_REFUSAL)
                self.permissions.check.assert_not_called()
                self.tool_manager.execute_tool.assert_not_called()
                self.assertIsNone(self.session.pending_approval)
                self.memory.record_interaction.assert_called_once_with(
                    "synthetic identifier",
                    self.UNKNOWN_ACTION_REFUSAL,
                )

    def test_a_spoofed_identifier_never_reaches_a_real_contract(self) -> None:
        response = self._submit(
            _Impersonator("system"),
            _Impersonator("overview"),
        )

        self.assertEqual(response.text, self.UNKNOWN_TOOL_REFUSAL)
        self.permissions.check.assert_not_called()
        self.tool_manager.execute_tool.assert_not_called()
        self.assertIsNone(self.session.pending_approval)

    def test_a_str_subclass_identifier_never_reaches_a_real_contract(self) -> None:
        response = self._submit(
            _NameSubclass("system"),
            _NameSubclass("overview"),
        )

        self.assertEqual(response.text, self.UNKNOWN_TOOL_REFUSAL)
        self.permissions.check.assert_not_called()
        self.tool_manager.execute_tool.assert_not_called()

    def test_no_user_defined_identifier_method_runs_in_the_pipeline(self) -> None:
        probe = _IdentifierTripwire()

        response = self._submit(probe, "run")

        self.assertEqual(response.text, self.UNKNOWN_TOOL_REFUSAL)
        self.assertEqual(probe.calls, [])

    def test_no_unsafe_identifier_content_reaches_the_logs(self) -> None:
        unsafe = "C:\\Users\\ExC\\secret.txt"

        response = self._submit(unsafe, "/etc/passwd")

        logged = "\n".join(self.records)
        recorded = self.memory.record_interaction.call_args[0][1]
        self.assertEqual(response.text, self.UNKNOWN_TOOL_REFUSAL)
        self.assertEqual(recorded, self.UNKNOWN_TOOL_REFUSAL)
        for haystack in (response.text, recorded, logged):
            self.assertNotIn(unsafe, haystack)
            self.assertNotIn("/etc/passwd", haystack)

    def test_ordinary_identifiers_keep_their_exact_refusals(self) -> None:
        for step, expected in (
            (
                ("scheduler", "run"),
                "Unsupported action: scheduler is not a supported tool.",
            ),
            (
                ("filesystem", "write"),
                "Unsupported action: filesystem.write is not a supported action.",
            ),
        ):
            with self.subTest(action=f"{step[0]}.{step[1]}"):
                self.assertEqual(self._submit(*step).text, expected)


class MalformedIdentifierToolManagerTests(unittest.TestCase):
    """Verify the dispatch boundary refuses a malformed identifier itself."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.config = _temporary_config(self.root)
        self.records: list[str] = []
        self.logger = logging.getLogger("tests.contract_enforcement.dispatch")
        self.logger.handlers.clear()
        self.logger.addHandler(_RecordingHandler(self.records))
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.addCleanup(self.logger.handlers.clear)
        self.memory = Memory(
            self.config.database_path,
            logger=Mock(spec=logging.Logger),
        )
        self.memory.initialize()
        self.manager = ToolManager(self.config, self.memory, self.logger)
        self.manager.load_tools()

    def test_a_malformed_identifier_executes_no_concrete_tool(self) -> None:
        cases = (
            ("list tool", [], "overview", "unknown tool"),
            ("dict tool", {}, "overview", "unknown tool"),
            ("none tool", None, "overview", "unknown tool"),
            ("int tool", 7, "overview", "unknown tool"),
            ("list action", "system", [], "unknown action"),
            ("none action", "system", None, "unknown action"),
        )
        expected = {
            "unknown tool": (
                "Unsupported action: unknown_tool is not a supported tool."
            ),
            "unknown action": (
                "Unsupported action: system.unknown_action is not a supported "
                "action."
            ),
        }
        for label, tool_name, action, kind in cases:
            with self.subTest(case=label):
                with patch.object(SystemTool, "execute") as execute:
                    result = self.manager.execute_tool(tool_name, action, {})

                execute.assert_not_called()
                self.assertEqual(
                    result,
                    ToolExecutionResult(False, expected[kind]),
                )

    def test_a_spoofed_identifier_executes_no_concrete_tool(self) -> None:
        with patch.object(SystemTool, "execute") as execute:
            spoofed = self.manager.execute_tool(
                _Impersonator("system"),
                _Impersonator("overview"),
                {},
            )
            subclassed = self.manager.execute_tool(
                _NameSubclass("system"),
                _NameSubclass("overview"),
                {},
            )

        execute.assert_not_called()
        for result in (spoofed, subclassed):
            self.assertEqual(
                result,
                ToolExecutionResult(
                    False,
                    "Unsupported action: unknown_tool is not a supported tool.",
                ),
            )

    def test_no_raw_identifier_or_representation_reaches_the_logs(self) -> None:
        unsafe = "C:\\Users\\ExC\\secret.txt"
        probe = _IdentifierTripwire()

        with patch.object(SystemTool, "execute") as execute:
            unsafe_result = self.manager.execute_tool(unsafe, "overview", {})
            probe_result = self.manager.execute_tool(probe, "overview", {})

        logged = "\n".join(self.records)
        execute.assert_not_called()
        self.assertEqual(probe.calls, [])
        for result in (unsafe_result, probe_result):
            self.assertEqual(
                result.message,
                "Unsupported action: unknown_tool is not a supported tool.",
            )
        self.assertNotIn(unsafe, logged)
        self.assertNotIn("object at 0x", logged)

    def test_ordinary_identifiers_still_execute_normally(self) -> None:
        result = self.manager.execute_tool(
            "calculator",
            "calculate",
            {"expression": "6 * 7"},
        )

        self.assertTrue(result.success)
        self.assertEqual(result.message, "6 * 7 = 42")


class _CollidingKey:
    """A key that collides with a declared field and refuses comparison."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __hash__(self) -> int:
        """Collide with the calculator's declared expression field."""
        self.calls.append("hash")
        return hash("expression")

    def __eq__(self, other: object) -> bool:
        """Refuse comparison loudly if validation ever attempts it."""
        self.calls.append("eq")
        raise RuntimeError("a submitted key must never be compared")

    def __str__(self) -> str:
        """Record that this key was converted to text."""
        self.calls.append("str")
        return "expression"

    def __repr__(self) -> str:
        """Record that this key was represented."""
        self.calls.append("repr")
        return "expression"


class _HostileNameKey(str):
    """A str subclass key whose equality refuses to cooperate."""

    def __eq__(self, other: object) -> bool:
        """Refuse comparison loudly if validation ever attempts it."""
        raise RuntimeError("a submitted key must never be compared")

    def __hash__(self) -> int:
        """Hash as the plain text this key subclasses."""
        return hash(str(self))


class _MappingSpoof:
    """A non-Mapping whose instance-level __class__ claims to be a dict."""

    @property
    def __class__(self) -> type:
        """Claim to be a dict without being one."""
        return dict


class _FailingIterationMapping(Mapping):
    """A real Mapping whose iteration fails."""

    def __iter__(self) -> Any:
        """Fail while the keys are being enumerated."""
        raise RuntimeError("iteration is unavailable")

    def __len__(self) -> int:
        """Report an empty mapping."""
        return 0

    def __getitem__(self, key: str) -> Any:
        """Never resolve a key."""
        raise KeyError(key)


class _FailingLookupMapping(Mapping):
    """A real Mapping that names a field it then refuses to return."""

    def __iter__(self) -> Any:
        """Offer exactly the calculator's declared field."""
        return iter(("expression",))

    def __len__(self) -> int:
        """Report the single offered key."""
        return 1

    def __getitem__(self, key: str) -> Any:
        """Fail while the declared value is being read."""
        raise RuntimeError("lookup is unavailable")


class _MemoryErrorIterationMapping(Mapping):
    """A real Mapping whose iteration exhausts memory."""

    def __iter__(self) -> Any:
        """Fail with a resource error while enumerating the keys."""
        raise MemoryError("exhausted")

    def __len__(self) -> int:
        """Report an empty mapping."""
        return 0

    def __getitem__(self, key: str) -> Any:
        """Never resolve a key."""
        raise KeyError(key)


class _MemoryErrorLookupMapping(Mapping):
    """A real Mapping whose declared-value retrieval exhausts memory."""

    def __iter__(self) -> Any:
        """Offer exactly the calculator's declared field."""
        return iter(("expression",))

    def __len__(self) -> int:
        """Report the single offered key."""
        return 1

    def __getitem__(self, key: str) -> Any:
        """Fail with a resource error while reading the value."""
        raise MemoryError("exhausted")


class _NameSubclass(str):
    """A str subclass, which is never an exact str identifier."""


class _Impersonator:
    """An identifier whose hash and equality mimic a registered name."""

    def __init__(self, target: str) -> None:
        self.target = target

    def __hash__(self) -> int:
        """Return the hash of the name this object imitates."""
        return hash(self.target)

    def __eq__(self, other: object) -> bool:
        """Compare equal to the name this object imitates."""
        return other == self.target


class _IdentifierTripwire:
    """An identifier whose every user-defined hook records that it ran."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __hash__(self) -> int:
        """Record that this object was hashed."""
        self.calls.append("hash")
        return 0

    def __eq__(self, other: object) -> bool:
        """Record that this object was compared."""
        self.calls.append("eq")
        return True

    def __str__(self) -> str:
        """Record that this object was converted to text."""
        self.calls.append("str")
        return "system"

    def __repr__(self) -> str:
        """Record that this object was represented."""
        self.calls.append("repr")
        return "system"


class _ConvertingKey:
    """A mapping key whose text conversion must never be reached."""

    def __init__(self) -> None:
        self.converted = False

    def __hash__(self) -> int:
        """Return a stable hash so this key can enter a dictionary."""
        return 0

    def __str__(self) -> str:
        """Record that a caller converted this key to text."""
        self.converted = True
        return "converted"


class ToolDefenceInDepthTests(unittest.TestCase):
    """Verify each tightened tool path rejects rather than converting."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.logger = Mock(spec=logging.Logger)
        self.memory = Memory(self.root / "db" / "maria.db", logger=self.logger)
        self.memory.initialize()

    def _assert_value_absent(
        self,
        result: ToolExecutionResult,
        value: Any,
        logger: Mock,
    ) -> None:
        """Assert a rejected value reached neither the response nor the log."""
        self.assertFalse(result.success)
        self.assertNotIn(str(value), result.message)
        logged = " ".join(str(entry) for entry in logger.method_calls)
        self.assertNotIn(str(value), logged)

    def test_calculator_expression_is_not_converted(self) -> None:
        tool_logger = Mock(spec=logging.Logger)
        tool = CalculatorTool(logger=tool_logger)

        rejected = tool.execute("calculate", {"expression": SENTINEL_NUMBER})
        valid = tool.execute("calculate", {"expression": "3 + 4"})

        self.assertEqual(
            rejected,
            ToolExecutionResult(False, "Expression must be text."),
        )
        self._assert_value_absent(rejected, SENTINEL_NUMBER, tool_logger)
        self.assertEqual(valid.message, "3 + 4 = 7")

    def test_filesystem_path_fields_are_not_converted(self) -> None:
        (self.root / "present.txt").write_text("content", encoding="utf-8")
        tool = FilesystemTool(root_path=self.root, logger=self.logger)
        cases = (
            ("read", {"path": 7}, "File read failed: Path must be text."),
            ("list", {"path": 7}, "Directory listing failed: Path must be text."),
            (
                "rename",
                {"source": 7, "destination": "b.txt"},
                "Rename failed: Path must be text.",
            ),
            (
                "rename",
                {"source": "present.txt", "destination": 7},
                "Rename failed: Path must be text.",
            ),
            (
                "move",
                {"source": 7, "destination": "b.txt"},
                "Move failed: Path must be text.",
            ),
            (
                "move",
                {"source": "present.txt", "destination": 7},
                "Move failed: Path must be text.",
            ),
        )
        for action, arguments, expected in cases:
            with self.subTest(action=action, arguments=tuple(arguments)):
                result = tool.execute(action, arguments)

                self.assertEqual(result, ToolExecutionResult(False, expected))

        self.assertTrue((self.root / "present.txt").is_file())
        self.assertEqual(
            tool.execute("read", {"path": "present.txt"}).message,
            "content",
        )

    def test_filesystem_absent_optional_path_still_lists_the_root(self) -> None:
        (self.root / "present.txt").write_text("content", encoding="utf-8")
        tool = FilesystemTool(root_path=self.root, logger=self.logger)

        result = tool.execute("list", {})

        self.assertTrue(result.success)
        self.assertIn("file: present.txt", result.message)

    def test_history_search_query_is_not_converted(self) -> None:
        tool = HistoryTool(memory=self.memory, logger=self.logger)
        self.memory.record_interaction("remembered request", "response")

        rejected = tool.execute("search", {"query": 7})
        valid = tool.execute("search", {"query": "remembered"})

        self.assertEqual(
            rejected,
            ToolExecutionResult(False, "Search query must be text."),
        )
        self.assertTrue(valid.success)
        self.assertIn("remembered request", valid.message)

    def test_memory_fact_and_query_are_not_converted(self) -> None:
        tool = MemoryTool(memory=self.memory, logger=self.logger)

        stored = tool.execute("store", {"fact": 7})
        searched = tool.execute("search", {"query": 7})

        self.assertEqual(
            stored,
            ToolExecutionResult(False, "Memory fact must be text."),
        )
        self.assertEqual(
            searched,
            ToolExecutionResult(False, "Memory search query must be text."),
        )
        self.assertEqual(self.memory.list_memories(limit=5), [])

    def test_memory_and_notes_limits_are_not_converted_or_clamped(self) -> None:
        memory_tool = MemoryTool(memory=self.memory, logger=self.logger)
        notes_tool = NotesTool(memory=self.memory, logger=self.logger)
        cases = (
            (memory_tool, "list", {}, "Memories limit must be a number."),
            (
                memory_tool,
                "search",
                {"query": "term"},
                "Memory search limit must be a number.",
            ),
            (notes_tool, "list", {}, "Notes limit must be a number."),
        )
        for tool, action, base, expected in cases:
            for value in ("5", True, None):
                with self.subTest(action=action, value=value):
                    result = tool.execute(action, {**base, "limit": value})

                    self.assertEqual(result, ToolExecutionResult(False, expected))

        range_cases = (
            (
                memory_tool,
                "list",
                {},
                "Memories limit must be between 1 and 100.",
            ),
            (
                memory_tool,
                "search",
                {"query": "term"},
                "Memory search limit must be between 1 and 100.",
            ),
            (notes_tool, "list", {}, "Notes limit must be between 1 and 100."),
        )
        for tool, action, base, expected in range_cases:
            for value in (0, 101):
                with self.subTest(action=action, value=value):
                    result = tool.execute(action, {**base, "limit": value})

                    self.assertEqual(result, ToolExecutionResult(False, expected))

    def test_valid_limits_keep_their_existing_behavior(self) -> None:
        notes_tool = NotesTool(memory=self.memory, logger=self.logger)
        note_ids = [
            self.memory.create_note(f"Note {index}", f"Body {index}")
            for index in range(3)
        ]

        limited = notes_tool.execute("list", {"limit": 2})
        defaulted = notes_tool.execute("list", {})

        self.assertEqual(
            [note["id"] for note in limited.data],
            list(reversed(note_ids[1:])),
        )
        self.assertEqual(len(defaulted.data), 3)

    def test_notes_title_body_and_target_are_not_converted(self) -> None:
        tool = NotesTool(memory=self.memory, logger=self.logger)

        titled = tool.execute("create", {"title": 7, "body": "Body"})
        bodied = tool.execute("create", {"title": "Title", "body": 7})
        targeted = tool.execute("read", {"target": 7})

        self.assertEqual(
            titled,
            ToolExecutionResult(False, "Note title must be text."),
        )
        self.assertEqual(
            bodied,
            ToolExecutionResult(False, "Note body must be text."),
        )
        self.assertEqual(
            targeted,
            ToolExecutionResult(False, "Note id or title must be text."),
        )
        self.assertEqual(self.memory.list_notes(limit=5), [])

    def test_absent_optional_note_title_still_uses_the_tool_default(self) -> None:
        tool = NotesTool(memory=self.memory, logger=self.logger)

        result = tool.execute("create", {"body": "Body only"})

        self.assertTrue(result.success)
        self.assertEqual(result.data["title"], "Untitled")

    def test_blank_values_keep_their_existing_controlled_failures(self) -> None:
        notes_tool = NotesTool(memory=self.memory, logger=self.logger)
        history_tool = HistoryTool(memory=self.memory, logger=self.logger)
        calculator_tool = CalculatorTool(logger=self.logger)

        self.assertEqual(
            notes_tool.execute("create", {"title": "Title", "body": "  "}),
            ToolExecutionResult(False, "Note body is required."),
        )
        self.assertEqual(
            notes_tool.execute("read", {"target": " \t "}),
            ToolExecutionResult(False, "Note id or title is required."),
        )
        self.assertEqual(
            history_tool.execute("search", {"query": "  "}),
            ToolExecutionResult(False, "Search query is required."),
        )
        self.assertEqual(
            calculator_tool.execute("calculate", {"expression": "  "}),
            ToolExecutionResult(False, "No expression provided."),
        )

    def test_provided_blank_note_title_is_refused_rather_than_repaired(self) -> None:
        tool = NotesTool(memory=self.memory, logger=self.logger)

        for blank in ("", "   ", " \t\r\n "):
            with self.subTest(blank=repr(blank)):
                result = tool.execute("create", {"title": blank, "body": "Body"})

                self.assertEqual(
                    result,
                    ToolExecutionResult(False, "Note title is required."),
                )
        self.assertEqual(self.memory.list_notes(limit=5), [])

    def test_absent_note_title_still_defaults_while_provided_text_is_kept(
        self,
    ) -> None:
        tool = NotesTool(memory=self.memory, logger=self.logger)

        defaulted = tool.execute("create", {"body": "Body"})
        provided = tool.execute("create", {"title": "  Kept  ", "body": "Body two"})

        self.assertEqual(defaulted.data["title"], "Untitled")
        self.assertEqual(provided.data["title"], "Kept")

    def test_calculator_expression_length_defence_is_unchanged(self) -> None:
        tool_logger = Mock(spec=logging.Logger)
        tool = CalculatorTool(logger=tool_logger)
        exact_limit = "+1" + "+0" * 99
        over_limit = "++1" + "+0" * 99

        self.assertEqual(len(exact_limit), 200)
        self.assertEqual(len(over_limit), 201)
        self.assertEqual(tool.execute("calculate", {"expression": exact_limit}).data[
            "result"
        ], 1)
        self.assertEqual(
            tool.execute("calculate", {"expression": over_limit}),
            ToolExecutionResult(
                False,
                "Calculation failed: Expression is too long.",
            ),
        )

    def test_blank_filesystem_path_defence_is_unchanged(self) -> None:
        tool = FilesystemTool(root_path=self.root, logger=self.logger)

        self.assertEqual(
            tool.execute("read", {"path": "  "}),
            ToolExecutionResult(False, "File read failed: Path is required."),
        )
        self.assertEqual(
            tool.execute("list", {"path": "  "}),
            ToolExecutionResult(
                False,
                "Directory listing failed: Path is required.",
            ),
        )


if __name__ == "__main__":
    unittest.main()
