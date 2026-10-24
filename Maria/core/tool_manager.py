"""Tool discovery, loading, and execution for Maria."""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from logging import Logger
from typing import Any, Protocol

from core.action_contracts import (
    BUILT_IN_ACTION_CONTRACTS,
    ActionContractRegistry,
    ArgumentContract,
    ContractViolation,
)
from core.config import MariaConfig
from core.failures import Failure, FailureKind
from core.logger import get_logger
from core.memory import Memory
from core.planner import Plan, PlanOrigin, PlanStep


BUILT_IN_TOOL_MODULES = (
    "tools.audit",
    "tools.calculator",
    "tools.conversation_summaries",
    "tools.conversations",
    "tools.filesystem",
    "tools.history",
    "tools.memory",
    "tools.notes",
    "tools.projects",
    "tools.reminders",
    "tools.system",
    "tools.tasks",
)


@dataclass(frozen=True)
class ToolExecutionResult:
    """Result returned by a tool execution."""

    success: bool
    message: str
    data: Any = None
    recorded_message: str | None = None


@dataclass(frozen=True, repr=False)
class ConfirmationBinding:
    """What a tool pins to one step awaiting approval, and what it shows.

    ``arguments`` are added to the step, and so to the plan the owner
    approves. ``preview`` is owner-visible text shown with the confirmation
    request, such as the exact stored text an approval covers, read in the
    same read as the state ``arguments`` pin. It is never added to the plan,
    so it is not part of the approval's fingerprint and is never recorded,
    logged, or kept once the request is shown. Neither is shown by ``repr``.
    """

    arguments: Mapping[str, Any]
    preview: str | None = None

    def __post_init__(self) -> None:
        """Refuse anything but a mapping of arguments and optional text."""
        if not isinstance(self.arguments, Mapping) or not (
            self.preview is None or type(self.preview) is str
        ):
            raise ValueError(
                "A confirmation binding must be arguments and optional text."
            )

    def __repr__(self) -> str:
        """Name the type without showing the arguments or the preview."""
        return "ConfirmationBinding(<redacted>)"


@dataclass(frozen=True, repr=False)
class PreparedConfirmation:
    """A plan pinned for approval, with the previews shown alongside it.

    ``plan`` is what the owner approves and what the approval is bound to.
    ``previews`` are the tools' owner-visible texts, in plan order, for the
    confirmation request alone: they never enter the plan. Neither is shown
    by ``repr``.
    """

    plan: Plan
    previews: tuple[str, ...] = ()

    def __repr__(self) -> str:
        """Name the type without showing the plan or the previews."""
        return "PreparedConfirmation(<redacted>)"


class Tool(Protocol):
    """Minimal interface every Maria tool must provide.

    A tool whose action depends on stored state that could change while the
    owner decides may also provide ``confirmation_binding(action,
    arguments)``, returning a mapping of arguments that pin that state into
    the plan the owner approves, or a ``ConfirmationBinding`` that also
    carries text to show the owner; see ``ToolManager.prepare_confirmation``.
    It is optional and not part of this protocol's required surface.
    """

    name: str
    description: str
    actions: frozenset[str]

    def execute(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> ToolExecutionResult:
        """Execute a supported action."""


class ToolManager:
    """Discover, load, and execute Maria tools."""

    def __init__(
        self,
        config: MariaConfig,
        memory: Memory,
        logger: Logger | None = None,
        contracts: ActionContractRegistry | None = None,
    ) -> None:
        self.config = config
        self.memory = memory
        self.logger = logger or get_logger(__name__)
        self.contracts = (
            BUILT_IN_ACTION_CONTRACTS if contracts is None else contracts
        )
        self._tools: dict[str, Tool] = {}

    def discover_tools(self) -> tuple[str, ...]:
        """Return import paths for built-in tools."""
        return BUILT_IN_TOOL_MODULES

    def load_tools(self) -> None:
        """Import and register built-in tools."""
        for module_name in self.discover_tools():
            module = importlib.import_module(module_name)
            tool = module.build_tool(
                config=self.config,
                memory=self.memory,
                logger=get_logger(module_name),
            )
            self._tools[tool.name] = tool
            self.logger.info("Loaded tool: %s", tool.name)

    def loaded_action_identifiers(self) -> frozenset[str]:
        """Return a read-only snapshot of every loaded tool action.

        The snapshot exists so the declared registry can be checked against
        the tools actually loaded. The registry is never populated from it.
        """
        return frozenset(
            f"{tool_name}.{action}"
            for tool_name, tool in self._tools.items()
            for action in tool.actions
        )

    def validate_action(
        self,
        tool_name: str,
        action: str,
        arguments: Mapping[str, Any],
    ) -> ContractViolation | None:
        """Return the first contract violation for a call, without executing it.

        This is the non-executing half of the dispatch boundary, so a caller
        can settle whether an action and its arguments are supported before
        anything else in the pipeline runs.
        """
        return self.contracts.validate(tool_name, action, arguments)

    def session_input_argument(
        self,
        tool_name: str,
        action: str,
    ) -> ArgumentContract | None:
        """Return the argument Maria supplies from the session, or None.

        This is the active registry's declaration for one action: the one
        argument whose text Maria herself supplies from the owner's session,
        or None when the action declares none or is not declared at all. It
        executes nothing and reads no session; Brain uses it to learn whether
        a planner's plan asks for session text without naming any tool.
        """
        contract = self.contracts.contract_for(tool_name, action)
        return None if contract is None else contract.session_input_argument

    def prepare_confirmation(self, plan: Plan) -> PreparedConfirmation:
        """Return the plan to present for approval, pinned to its exact targets.

        Brain calls this once, when a plan is about to wait for the owner's
        answer, so the plan the owner approves, and whose fingerprint binds
        that approval, already names the exact state each step acts on.
        Each step's tool may add arguments through its optional
        ``confirmation_binding``. A binding can only add: it cannot replace
        an argument the step already has, change a step's tool or action, or
        add or remove a step, and a tool that binds nothing leaves its step
        exactly as it was. A plan no tool binds comes back as the very same
        object.

        A ``ConfirmationBinding`` may also carry a preview for the owner.
        Previews come back beside the plan, in plan order, and never enter
        it, so the plan holds only what was pinned.

        A binding that fails is contained and simply adds nothing. A tool
        that relies on a binding therefore has to treat an unbound step as
        unapproved for the state it would have pinned, which is how every
        failure here stays safe. The contract check at confirmation
        validates whatever was added.

        A model-originated plan is never prepared: the owner approves it
        exactly as the compiler built it, so it is refused here with a fixed
        ``ValueError`` rather than given anything a binding could add.
        """
        if plan.origin is not PlanOrigin.DETERMINISTIC:
            raise ValueError(
                "A model-originated plan is approved exactly as compiled and "
                "is never prepared for confirmation."
            )
        steps: list[PlanStep] = []
        previews: list[str] = []
        for step in plan.steps:
            binder = getattr(
                self._tools.get(step.tool_name),
                "confirmation_binding",
                None,
            )
            added: Mapping[str, Any] = {}
            if binder is not None:
                try:
                    added = binder(step.action, step.arguments)
                except Exception:  # noqa: BLE001 - binding must not crash Maria.
                    self.logger.error(
                        "Confirmation binding failed: %s.%s",
                        step.tool_name,
                        step.action,
                    )
                    added = {}
            if isinstance(added, ConfirmationBinding):
                if added.preview is not None:
                    previews.append(added.preview)
                added = added.arguments
            if not added:
                steps.append(step)
                continue
            if not isinstance(added, Mapping) or set(added) & set(step.arguments):
                raise ValueError(
                    "A confirmation binding may only add new arguments."
                )
            steps.append(
                PlanStep(
                    step.tool_name,
                    step.action,
                    {**step.arguments, **added},
                )
            )

        bound = tuple(steps)
        if all(new is old for new, old in zip(bound, plan.steps)):
            return PreparedConfirmation(plan, tuple(previews))
        return PreparedConfirmation(replace(plan, steps=bound), tuple(previews))

    def execute_tool(
        self,
        tool_name: str,
        action: str,
        arguments: Mapping[str, Any],
    ) -> ToolExecutionResult:
        """Execute an action on a loaded tool after validating its contract.

        Contract validation runs first and unconditionally, so a direct
        caller cannot reach a tool with arguments the central registry
        rejects. Brain validates a whole plan earlier for ordering reasons;
        this pass is the defensive one that no caller can skip.

        An unexpected exception from a tool is contained here rather than
        crashing Maria, and neither the exception nor its text reaches the
        owner or the log. The result carries the shared typed failure's
        fixed message, and the log records the validated action identity
        and the failure code only. A tool that has a safe, specific
        diagnostic still logs it itself, where it knows what its own
        failure is allowed to say.
        """
        violation = self.validate_action(tool_name, action, arguments)
        if violation is not None:
            self.logger.warning(
                "Rejected action by contract: %s %s.%s field=%s",
                violation.kind.value,
                violation.tool_name,
                violation.action,
                violation.field,
            )
            return ToolExecutionResult(False, violation.message)

        tool = self._tools.get(tool_name)
        if tool is None:
            self.logger.error("Tool not loaded: %s", tool_name)
            return ToolExecutionResult(False, f"Tool not loaded: {tool_name}")

        if action not in tool.actions:
            self.logger.warning("Unsupported action requested: %s.%s", tool_name, action)
            return ToolExecutionResult(
                False,
                f"Unsupported action for {tool_name}: {action}",
            )

        try:
            self.logger.info("Executing tool action: %s.%s", tool_name, action)
            return tool.execute(action, arguments)
        except Exception:  # noqa: BLE001 - tools must not crash the app.
            # The exception is deliberately not bound, so no path here can
            # format its text, its args, or its traceback into a response or
            # a log record. Both names logged below already matched a
            # declared contract entry above, so neither carries caller text.
            failure = Failure(FailureKind.TOOL_EXECUTION_FAILED)
            self.logger.error(
                "Tool execution failed: %s.%s code=%s",
                tool_name,
                action,
                failure.code,
            )
            return ToolExecutionResult(False, failure.message)
