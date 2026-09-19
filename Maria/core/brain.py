"""Main application controller for Maria.

The brain coordinates the configured planner, memory layer, permission
manager, and tool manager. It does not directly perform database work or tool
logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from logging import Logger

from core.action_contracts import ContractViolation
from core.config import MariaConfig, load_config
from core.logger import configure_logging, get_logger
from core.memory import Memory
from core.permissions import (
    PermissionDecision,
    PermissionManager,
    PermissionResult,
)
from core.planner import Plan, Planner, PlanStep
from core.tool_manager import ToolExecutionResult, ToolManager


@dataclass(frozen=True)
class BrainResponse:
    """Response returned by Maria's controller."""

    text: str
    needs_confirmation: bool = False
    plan: Plan | None = None


@dataclass(frozen=True)
class _ResolvedStep:
    """One plan step paired with the permission decision it resolved to.

    Both fields are the original objects rather than copies, so a resolved
    step never duplicates a step's immutable arguments into mutable storage
    and never restates what PermissionManager decided.
    """

    step: PlanStep
    permission: PermissionResult


@dataclass(frozen=True)
class _ResolvedPlan:
    """A plan whose every step was resolved before any step was dispatched.

    This is transient execution state. It is never persisted, never logged,
    and never handed to another module: it exists only so Brain can finish
    resolving permission for a complete plan before dispatching the first
    tool. It reports which decisions came back without making any of them.
    """

    plan: Plan
    steps: tuple[_ResolvedStep, ...]

    @property
    def first_denied_step(self) -> _ResolvedStep | None:
        """Return the earliest denied step in plan order, or None."""
        for resolved_step in self.steps:
            if resolved_step.permission.decision == PermissionDecision.DENIED:
                return resolved_step
        return None

    @property
    def confirmation_required_steps(self) -> tuple[_ResolvedStep, ...]:
        """Return every step awaiting explicit confirmation, in plan order."""
        required = PermissionDecision.REQUIRES_CONFIRMATION
        return tuple(
            resolved_step
            for resolved_step in self.steps
            if resolved_step.permission.decision == required
        )


class MariaBrain:
    """Coordinate Maria's request handling workflow."""

    def __init__(
        self,
        config: MariaConfig,
        planner: Planner,
        memory: Memory,
        tool_manager: ToolManager,
        permissions: PermissionManager,
        logger: Logger | None = None,
    ) -> None:
        self.config = config
        self.planner = planner
        self.memory = memory
        self.tool_manager = tool_manager
        self.permissions = permissions
        self.logger = logger or get_logger(__name__)

    @classmethod
    def from_defaults(cls) -> "MariaBrain":
        """Create a fully wired MariaBrain from config.toml."""
        config = load_config()
        configure_logging(config.log_dir, config.log_level)
        logger = get_logger(__name__)

        memory = Memory(config.database_path, logger=get_logger("memory"))
        memory.initialize()

        tool_manager = ToolManager(
            config=config,
            memory=memory,
            logger=get_logger("tool_manager"),
        )
        tool_manager.load_tools()

        permissions = PermissionManager(
            confirmation_required=config.confirmation_required,
            denied_actions=config.denied_actions,
            logger=get_logger("permissions"),
        )

        return cls(
            config=config,
            planner=Planner(logger=get_logger("planner")),
            memory=memory,
            tool_manager=tool_manager,
            permissions=permissions,
            logger=logger,
        )

    def process(self, user_input: str) -> BrainResponse:
        """Plan one user request and run it unless it needs confirmation."""
        cleaned_input = user_input.strip()
        if not cleaned_input:
            return BrainResponse("Please enter a request.")

        self.logger.info("Received user request.")

        try:
            recent_memory = self.memory.get_recent_interactions(limit=3)
        except RuntimeError as exc:
            self.logger.error("Unable to retrieve recent memory: %s", exc)
            recent_memory = []

        plan = self.planner.create_plan(
            cleaned_input,
            context={"recent_interactions": recent_memory},
        )

        return self._run_plan(plan, confirmed=False)

    def execute_confirmed_plan(self, plan: Plan) -> BrainResponse:
        """Run an already-approved plan without planning the request again.

        The caller has bound the owner's approval to this exact plan, so
        Brain retrieves no context and invokes no planner. It validates the
        same plan against the action contracts again and resolves its
        permissions again before dispatching anything, because an approval
        authorises a plan rather than freezing the contract and the decisions
        that were current when that plan was shown, then resumes the ordinary
        dispatch and recording flow.
        """
        return self._run_plan(plan, confirmed=True)

    def _run_plan(self, plan: Plan, *, confirmed: bool) -> BrainResponse:
        """Validate, resolve, then dispatch a whole plan generically.

        Three phases run in a fixed order. Every step is first validated
        against the declared action contracts, then every step's permission
        is decided, and only then is anything dispatched. A contract failure
        anywhere refuses the plan before PermissionManager is consulted at
        all. A denial anywhere denies the entire plan, and an outstanding
        confirmation anywhere holds the entire plan, so neither outcome can
        arrive after a step already ran.
        """
        if not plan.steps:
            response = plan.response_if_no_action or "I do not know how to handle that yet."
            self._record_interaction(plan.original_request, response)
            return BrainResponse(response, plan=plan)

        violation = self._validate_plan(plan)
        if violation is not None:
            self.logger.warning(
                "Rejected plan step by contract: %s %s.%s field=%s",
                violation.kind.value,
                violation.tool_name,
                violation.action,
                violation.field,
            )
            response = violation.message
            self._record_interaction(plan.original_request, response)
            return BrainResponse(response, plan=plan)

        resolved = self._resolve_plan(plan)

        denied_step = resolved.first_denied_step
        if denied_step is not None:
            self.logger.warning(
                "Denied action: %s.%s",
                denied_step.step.tool_name,
                denied_step.step.action,
            )
            response = f"Permission denied: {denied_step.permission.reason}"
            self._record_interaction(plan.original_request, response)
            return BrainResponse(response, plan=plan)

        awaiting_confirmation = resolved.confirmation_required_steps
        if awaiting_confirmation and not confirmed:
            for resolved_step in awaiting_confirmation:
                self.logger.info(
                    "Confirmation required for action: %s.%s",
                    resolved_step.step.tool_name,
                    resolved_step.step.action,
                )
            return BrainResponse(
                text=self._format_confirmation_request(resolved),
                needs_confirmation=True,
                plan=plan,
            )

        return self._execute_resolved_plan(resolved)

    def _validate_plan(self, plan: Plan) -> ContractViolation | None:
        """Check every step against its action contract, in plan order.

        This is the phase that must complete before any permission decision,
        so a plan naming an unsupported action or carrying a malformed
        argument never reaches PermissionManager or a tool at all. Steps are
        inspected in order and the first violation stops the pass. Each step
        and its immutable arguments are passed through untouched: nothing is
        rebuilt, copied into a mutable mapping, normalized, or defaulted, so
        the plan's fingerprint is the same afterwards as before.

        The check goes through Tool Manager's non-executing validation entry
        point rather than a registry of Brain's own, so this phase and the
        defensive pass inside dispatch can only ever consult one active
        registry.
        """
        for step in plan.steps:
            violation = self.tool_manager.validate_action(
                step.tool_name,
                step.action,
                step.arguments,
            )
            if violation is not None:
                return violation
        return None

    def _resolve_plan(self, plan: Plan) -> _ResolvedPlan:
        """Decide permission for every step before any step is dispatched.

        Steps are inspected in plan order and each is paired with the result
        PermissionManager returned for it. No tool runs during this pass, the
        plan is neither rebuilt nor mutated, and the resolution is returned
        rather than stored, so it cannot outlive the request producing it.
        """
        return _ResolvedPlan(
            plan=plan,
            steps=tuple(
                _ResolvedStep(
                    step,
                    self.permissions.check(step.tool_name, step.action),
                )
                for step in plan.steps
            ),
        )

    def _execute_resolved_plan(self, resolved: _ResolvedPlan) -> BrainResponse:
        """Dispatch an already-resolved plan in order and record the outcome.

        Every step reaching this point resolved to an allowed or an approved
        decision, so dispatch performs no permission work of its own. Each
        step runs at most once and receives the immutable arguments object
        its PlanStep holds. The first unsuccessful result stops the plan
        without undoing the steps that already succeeded.
        """
        results: list[ToolExecutionResult] = []

        for resolved_step in resolved.steps:
            step = resolved_step.step

            if resolved_step.permission.decision == (
                PermissionDecision.REQUIRES_CONFIRMATION
            ):
                self.logger.info(
                    "Confirmed action: %s.%s",
                    step.tool_name,
                    step.action,
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
            resolved.plan.original_request,
            self._format_recorded_results(results),
        )
        return BrainResponse(response, plan=resolved.plan)

    def _record_interaction(self, user_input: str, response: str) -> None:
        """Store an interaction without allowing memory failures to crash Maria."""
        try:
            self.memory.record_interaction(user_input, response)
        except RuntimeError as exc:
            self.logger.error("Unable to record interaction: %s", exc)

    @staticmethod
    def _format_confirmation_request(resolved: _ResolvedPlan) -> str:
        """Render the confirmation request for a resolved plan.

        A single-step plan keeps its existing wording exactly. A longer plan
        is listed in plan order, naming each step's tool action and the
        permission outcome it resolved to. Neither form repeats an argument
        value, so no stored content and no host path can reach the owner's
        interface, an interaction row, or a log through this text.
        """
        if len(resolved.steps) == 1:
            return f"Confirmation required: {resolved.steps[0].permission.reason}"

        lines = [
            f"Confirmation required for {len(resolved.steps)} planned actions:"
        ]
        for position, resolved_step in enumerate(resolved.steps, start=1):
            outcome = resolved_step.permission.decision.value.replace("_", " ")
            lines.append(
                f"{position}. {resolved_step.step.tool_name}."
                f"{resolved_step.step.action}: {outcome}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_results(results: list[ToolExecutionResult]) -> str:
        """Format tool results for the user."""
        if not results:
            return "No action was executed."

        messages = [result.message for result in results]
        return "\n".join(messages)

    @staticmethod
    def _format_recorded_results(results: list[ToolExecutionResult]) -> str:
        """Format tool results for interaction-history recording.

        Each result contributes its recorded_message when set, or its
        displayed message when recorded_message is None, so a tool can
        override what gets persisted without Brain inspecting its name or
        action.
        """
        if not results:
            return "No action was executed."

        recorded_texts = [
            result.message if result.recorded_message is None else result.recorded_message
            for result in results
        ]
        return "\n".join(recorded_texts)
