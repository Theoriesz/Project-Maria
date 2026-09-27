"""Main application controller for Maria.

The brain coordinates the configured planner, memory layer, permission
manager, and tool manager, and, when a reasoning provider was supplied, the
conversation module. It does not directly perform database work, tool logic,
or any provider-specific work, and it never imports a reasoning adapter.

The planner receives the request text and nothing else. The one stored input
to reasoning is the owner's enabled memory context, read through the memory
layer's bounded Tier A query only on the conversational path; interaction
history is never read for planning or reasoning.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from logging import Logger

from core.action_contracts import ContractViolation
from core.config import MariaConfig, load_config
from core.constants import LOCAL_CONFIG_PATH
from core.context import CONTEXT_QUERY_LIMIT, ContextFingerprint
from core.conversation import converse, is_generic_fallback
from core.filesystem_roots import prepare_roots
from core.logger import configure_logging, get_logger
from core.memory import Memory
from core.permissions import (
    PermissionDecision,
    PermissionManager,
    PermissionResult,
)
from core.planner import Plan, Planner, PlanStep, RecordingPolicy
from core.reasoning import (
    ReasoningExchange,
    ReasoningProvider,
    ReasoningSettings,
)
from core.tool_manager import ToolExecutionResult, ToolManager


@dataclass(frozen=True)
class BrainResponse:
    """Response returned by Maria's controller.

    ``conversation`` is set by a completed conversational reply, to the
    session's conversation as it stands after that reply, and to an empty
    conversation by a conversational turn that had to clear it. Every other
    response leaves it None, so no command, its result, or a failed
    conversational turn ever becomes conversation. ``supplied_context``
    travels with it: the fingerprints of every memory supplied since that
    conversation was last cleared, which the session keeps beside it.
    """

    text: str
    needs_confirmation: bool = False
    plan: Plan | None = None
    conversation: tuple[ReasoningExchange, ...] | None = None
    supplied_context: frozenset[ContextFingerprint] = frozenset()


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
        reasoning: ReasoningProvider | None = None,
    ) -> None:
        self.config = config
        self.planner = planner
        self.memory = memory
        self.tool_manager = tool_manager
        self.permissions = permissions
        self.logger = logger or get_logger(__name__)
        self.reasoning = reasoning

    @classmethod
    def from_defaults(
        cls,
        reasoning_factory: (
            Callable[[ReasoningSettings], ReasoningProvider | None] | None
        ) = None,
    ) -> "MariaBrain":
        """Create a fully wired MariaBrain from config.toml.

        ``reasoning_factory`` is how the runtime composition point supplies
        the owner's reasoning provider without Brain naming or importing any
        adapter. It is handed the settled reasoning settings once and returns
        a provider, or None to leave Maria deterministic only, which is also
        what happens without a factory.
        """
        # Startup is the one caller that names the owner's private override,
        # so every other caller of load_config reads the tracked file alone.
        # The override can only change the reasoning settings.
        config = load_config(local_override_path=LOCAL_CONFIG_PATH)

        # The provider is settled before anything is created, so settings a
        # provider refuses stop startup the same way a refused configuration
        # does: with nothing prepared, logged, or opened.
        reasoning = None
        if reasoning_factory is not None:
            reasoning = reasoning_factory(config.reasoning)

        # Loading settles what the roots are; this is the one boundary that
        # makes them usable. It runs before logging is configured because
        # configuring logging creates the active log directory, and a root
        # declaration that names a protected runtime location has to be
        # refused before anything is created rather than after. It still runs
        # before the memory layer opens the database, so a misdeclared root
        # stops startup rather than being discovered later by the first
        # filesystem command. Nothing is logged on this path either way: a
        # refusal raises, and main.py reports it as a sanitized startup
        # failure.
        prepare_roots(config.filesystem_roots, config.filesystem_protections)

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
            reasoning=reasoning,
        )

    def process(
        self,
        user_input: str,
        *conversation: ReasoningExchange,
        supplied_context: frozenset[ContextFingerprint] = frozenset(),
    ) -> BrainResponse:
        """Plan one user request and run it unless it needs confirmation.

        The deterministic planner always answers first, from the request text
        alone. Only when a reasoning provider was supplied, the configured
        reasoning settings are enabled, and the planner returned its exact
        generic no-action fallback, is the request answered conversationally
        instead, in the context of ``conversation``: the caller's earlier
        exchanges, oldest first, with ``supplied_context``, the fingerprints
        of the memories supplied since they began. With reasoning disabled
        the fallback is answered and recorded exactly as it always was,
        whether or not a provider is present. Every other plan runs exactly
        as it always has and never sees the conversation or any memory
        context.
        """
        cleaned_input = user_input.strip()
        if not cleaned_input:
            return BrainResponse("Please enter a request.")

        self.logger.info("Received user request.")

        plan = self.planner.create_plan(cleaned_input)

        if (
            self.reasoning is not None
            and self.config.reasoning.enabled
            and is_generic_fallback(plan)
        ):
            return self._converse(cleaned_input, conversation, supplied_context)

        return self._run_plan(plan, confirmed=False)

    def _converse(
        self,
        request: str,
        conversation: tuple[ReasoningExchange, ...],
        supplied_context: frozenset[ContextFingerprint],
    ) -> BrainResponse:
        """Answer one unrecognised request with the local model, as text.

        This is the only place Tier A context is read, and only after every
        gate above has passed: the memory layer's bounded query returns the
        enabled memories, in ascending id order, one row more than can be
        sent. A read that fails is handed on as None, and the conversation
        module then sends no context and no earlier exchange.

        The conversation module settles the whole turn and produces a
        zero-step plan that it, as the plan's producer, made transient. That
        plan takes the ordinary zero-step path, which returns before any
        contract, permission, or tool is consulted and records nothing. The
        conversation and supplied-context fingerprints it returns are handed
        back for the session to keep, and nothing else is: no interaction
        history, no other stored record, and no earlier command reaches the
        model through here.
        """
        self.logger.info("Answering request conversationally.")
        try:
            enabled_memories_read = self.memory.list_context_memories(
                limit=CONTEXT_QUERY_LIMIT
            )
        except RuntimeError:
            # The memory layer's message is fixed, but nothing about the
            # failure is needed here: the turn fails closed either way.
            self.logger.warning("Unable to read memory context.")
            enabled_memories_read = None

        turn = converse(
            self.reasoning,
            self.config.reasoning,
            request,
            conversation,
            self.logger,
            supplied_context=supplied_context,
            enabled_memories_read=enabled_memories_read,
        )
        response = self._run_plan(turn.plan, confirmed=False)
        if turn.conversation is None:
            return response
        return replace(
            response,
            conversation=turn.conversation,
            supplied_context=turn.supplied_context,
        )

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
        arrive after a step already ran. Every outcome that records goes
        through _record_interaction, which honours the plan's recording
        policy.
        """
        if not plan.steps:
            response = plan.response_if_no_action or "I do not know how to handle that yet."
            self._record_interaction(plan, response)
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
            self._record_interaction(plan, response)
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
            self._record_interaction(plan, response)
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
            resolved.plan,
            self._format_recorded_results(results),
        )
        return BrainResponse(response, plan=resolved.plan)

    def _record_interaction(self, plan: Plan, response: str) -> None:
        """Store a plan's interaction unless its producer made it transient.

        This is Brain's only write to interaction history. It decides from
        the plan's recording policy alone, never from a step's tool or
        action or from a result's content. A transient plan writes no row at
        all: neither its cleaned request nor any placeholder response. A
        memory failure is logged rather than allowed to crash Maria.
        """
        if plan.recording is RecordingPolicy.TRANSIENT:
            return
        try:
            self.memory.record_interaction(plan.original_request, response)
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
