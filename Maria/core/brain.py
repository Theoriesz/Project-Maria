"""Main application controller for Maria.

The brain coordinates the configured planner, memory layer, permission
manager, and tool manager, and, when a reasoning provider was supplied, the
conversation module. It does not directly perform database work, tool logic,
or any provider-specific work, and it never imports a reasoning adapter.

The planner receives the request text and nothing else. The only stored
inputs to reasoning are records the owner enabled for context - Tier A
memories and what Tier B's bounded search finds among enabled memories and
notes - read through the memory layer's bounded context queries only on the
conversational path; interaction history is never read for planning or
reasoning.

A model reply the conversation module recognises as a proposal attempt is
compiled here, through ``core/plan_compiler.py`` and against Tool Manager's
active contract registry, into a model-originated plan or a fixed refusal.
Such a plan never runs without exactly one approval from the owner, whatever
PermissionManager decides for its action, and a denied action is refused
before any approval is offered. The approved plan is the same immutable
plan: it is checked again, never rebuilt or reparsed, no provider is asked
anything, and it runs at most once, only after its content-free audit
summary is committed.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from logging import Logger

from core.action_contracts import ActionContract, ContractViolation
from core.audit import AuditDecision, AuditOutcome, audit_target
from core.config import MariaConfig, load_config
from core.constants import LOCAL_CONFIG_PATH
from core.context import (
    CONTEXT_MAX_BYTES,
    CONTEXT_QUERY_LIMIT,
    SEARCH_QUERY_LIMIT,
    ContextFingerprint,
    ContextSource,
    enabled_memories,
    memory_search_after,
    search_terms,
    supplied_record_ids,
    validated_supplied_context,
)
from core.conversation import (
    converse,
    is_conversational_plan,
    is_generic_fallback,
    with_notices,
)
from core.failures import Failure, FailureKind
from core.filesystem_roots import prepare_roots
from core.logger import configure_logging, get_logger
from core.memory import Memory
from core.permissions import (
    PermissionDecision,
    PermissionManager,
    PermissionResult,
)
from core.plan_compiler import (
    ProposalRejection,
    compile_proposal,
    recheck_model_plan,
)
from core.planner import (
    Plan,
    PlanOrigin,
    Planner,
    PlanStep,
    RecordingPolicy,
    is_model_plan,
)
from core.reasoning import (
    ReasoningExchange,
    ReasoningProvider,
    ReasoningResponse,
    ReasoningSettings,
)
from core.tool_manager import ToolExecutionResult, ToolManager


# Fixed replies for what goes wrong around a model-originated plan. None
# interpolates anything a request, a model, a tool, or a stored record
# supplied.
CONVERSATION_RESULT_REFUSED_MESSAGE = (
    "Maria could not use that conversational result."
)
MODEL_PLAN_REFUSED_MESSAGE = (
    "Maria refused that plan because it is not a well-formed plan compiled "
    "from one model proposal."
)
MODEL_PLAN_UNAPPROVED_MESSAGE = (
    "Maria runs a model-proposed action only after you approve it."
)
AUDIT_SUMMARY_UNRECORDED_MESSAGE = (
    "Maria did not run the approved action because its audit summary could "
    "not be recorded."
)


def audit_recorded_notice(summary_id: int) -> str:
    """Return the notice that follows a result whose outcome was recorded."""
    return f"Recorded as audit summary {summary_id}."


def audit_outcome_unrecorded_notice(summary_id: int) -> str:
    """Return the notice for a recorded summary whose outcome was not."""
    return (
        f"Recorded as audit summary {summary_id}, but its outcome could not be "
        "recorded, so that summary shows the outcome as unknown."
    )


@dataclass(frozen=True)
class BrainResponse:
    """Response returned by Maria's controller.

    ``conversation`` is set by a completed conversational reply, to the
    session's conversation as it stands after that reply, and to an empty
    conversation by a conversational turn that had to clear it, whatever
    else that turn produced. Every other response leaves it None, so no
    command, its result, a proposal, or a failed conversational turn ever
    becomes conversation. ``supplied_context`` travels with it: the
    fingerprints of every memory and note supplied since that conversation
    was last cleared, which the session keeps beside it.

    A model-originated plan awaiting approval comes back with
    ``needs_confirmation`` set, the plan itself, and as ``text`` only
    Maria's notices about that turn's context. The session renders the plan
    above them, from the very plan it then binds the approval to.
    """

    text: str
    needs_confirmation: bool = False
    plan: Plan | None = None
    conversation: tuple[ReasoningExchange, ...] | None = None
    supplied_context: frozenset[ContextFingerprint] = frozenset()


@dataclass(frozen=True)
class _ContextReads:
    """What one conversational turn's bounded context reads returned.

    Transient: it exists only to hand the rows to the conversation module,
    which validates them. ``enabled`` is None when any read failed, which
    the conversation module treats as a read it cannot trust.
    """

    enabled: Sequence[object] | None
    supplied_memories: Sequence[object] = ()
    supplied_notes: Sequence[object] = ()
    searched_memories: Sequence[object] = ()
    searched_notes: Sequence[object] = ()


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
        generic no-action fallback, is the request answered by the local
        model instead, in the context of ``conversation``: the caller's
        earlier exchanges, oldest first, with ``supplied_context``, the
        fingerprints of the memories and notes supplied since they began.
        The model may answer conversationally or propose one action; see
        ``_converse``. With reasoning disabled the fallback is answered and
        recorded exactly as it always was, whether or not a provider is
        present. Every other plan runs exactly as it always has and never
        sees the conversation or any stored context.
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
        """Answer one unrecognised request with the local model.

        This is the only place context is read, and only after every gate
        above has passed; ``_read_context`` says which bounded reads run.
        When any of them fails, the conversation module sends no context and
        no earlier exchange.

        The conversation module settles the whole turn in proposal mode, so
        the request carries Maria's fixed proposal instructions and the
        provider is asked exactly once. A conversational outcome is a
        zero-step plan that it, as the plan's producer, made transient, and
        Brain accepts nothing else as one. That plan takes the ordinary
        zero-step path, which returns before any contract, permission, or
        tool is consulted and records nothing. A proposal attempt goes to
        ``_consider_proposal`` instead, with the turn's notices. The
        conversation and supplied-context fingerprints the turn returns are
        handed back for the session to keep, and nothing else is: no
        interaction history, no other stored record, and no earlier command
        reaches the model through here.
        """
        self.logger.info("Answering request conversationally.")
        reads = self._read_context(
            request,
            validated_supplied_context(supplied_context),
        )

        turn = converse(
            self.reasoning,
            self.config.reasoning,
            request,
            conversation,
            self.logger,
            supplied_context=supplied_context,
            enabled_memories_read=reads.enabled,
            supplied_memories_read=reads.supplied_memories,
            supplied_notes_read=reads.supplied_notes,
            searched_memories_read=reads.searched_memories,
            searched_notes_read=reads.searched_notes,
            proposals=True,
        )
        if turn.proposal is not None:
            response = self._consider_proposal(
                request,
                turn.proposal,
                turn.notices,
            )
        elif is_conversational_plan(turn.plan):
            response = self._run_plan(turn.plan, confirmed=False)
        else:
            self.logger.error(
                "Refused a conversational result that was not a zero-step "
                "transient plan."
            )
            if turn.conversation is None:
                return BrainResponse(CONVERSATION_RESULT_REFUSED_MESSAGE)
            # Nothing from a refused result is kept, but whatever this turn
            # cleared stays cleared.
            return BrainResponse(
                CONVERSATION_RESULT_REFUSED_MESSAGE,
                conversation=(),
                supplied_context=frozenset(),
            )
        if turn.conversation is None:
            return response
        return replace(
            response,
            conversation=turn.conversation,
            supplied_context=turn.supplied_context,
        )

    def _consider_proposal(
        self,
        request: str,
        proposal: ReasoningResponse,
        notices: tuple[str, ...],
    ) -> BrainResponse:
        """Compile one proposal attempt, then offer or refuse the plan.

        The compiler is handed the owner's cleaned request, the reply exactly
        as it arrived, and Tool Manager's active contract registry. A refused
        proposal answers with its fixed message and nothing is repaired,
        retried, or asked of the model again. A compiled plan is offered for
        approval through ``_run_model_plan``. Either way the turn's notices
        are shown exactly once, with that answer, and are kept nowhere.
        """
        outcome = compile_proposal(
            request,
            proposal,
            self.tool_manager.contracts,
            self.logger,
        )
        if type(outcome) is ProposalRejection:
            return BrainResponse(with_notices(outcome.message, notices))
        return self._run_model_plan(outcome, confirmed=False, notices=notices)

    def _read_context(
        self,
        request: str,
        supplied: frozenset[ContextFingerprint],
    ) -> _ContextReads:
        """Run the bounded context reads one conversational turn needs.

        Tier A's read of enabled memories always runs: in ascending id order,
        one row more than can be sent, and never the text of a fact too long
        ever to be sent. Records supplied earlier in the conversation are
        then rechecked by id, and only when there are any. Tier B's searches
        run only when the request yields search terms: its memory search
        only when Tier A's read found more enabled memories than it
        considers, starting after the last one it considered, and its note
        search over notes the owner enabled. Every read goes through the
        memory layer's own bounded query; nothing is kept once the turn ends.

        Any read that fails, or a Tier A read too malformed to start the
        search from, ends them all: the turn is handed None and fails closed.
        """
        try:
            enabled = self.memory.list_context_memories(
                limit=CONTEXT_QUERY_LIMIT,
                max_fact_bytes=CONTEXT_MAX_BYTES,
            )
            memories_after = memory_search_after(enabled_memories(enabled))
            terms = search_terms(request)
            memory_ids = supplied_record_ids(supplied, ContextSource.MEMORY)
            note_ids = supplied_record_ids(supplied, ContextSource.NOTE)
            return _ContextReads(
                enabled=enabled,
                supplied_memories=(
                    self.memory.context_memories_by_id(
                        memory_ids,
                        max_fact_bytes=CONTEXT_MAX_BYTES,
                    )
                    if memory_ids
                    else ()
                ),
                supplied_notes=(
                    self.memory.context_notes_by_id(
                        note_ids,
                        max_text_bytes=CONTEXT_MAX_BYTES,
                    )
                    if note_ids
                    else ()
                ),
                searched_memories=(
                    self.memory.search_context_memories(
                        terms,
                        after_id=memories_after,
                        limit=SEARCH_QUERY_LIMIT,
                        max_fact_bytes=CONTEXT_MAX_BYTES,
                    )
                    if terms and memories_after is not None
                    else ()
                ),
                searched_notes=(
                    self.memory.search_context_notes(
                        terms,
                        limit=SEARCH_QUERY_LIMIT,
                        max_text_bytes=CONTEXT_MAX_BYTES,
                    )
                    if terms
                    else ()
                ),
            )
        except (RuntimeError, ValueError):
            # The memory layer's messages are fixed, but nothing about the
            # failure is needed here: the turn fails closed either way.
            self.logger.warning("Unable to read memory context.")
            return _ContextReads(enabled=None)

    def execute_confirmed_plan(self, plan: Plan) -> BrainResponse:
        """Run an already-approved plan without planning the request again.

        The caller has bound the owner's approval to this exact plan, so
        Brain retrieves no context, invokes no planner, and asks no reasoning
        provider anything. It validates the same plan against the action
        contracts again and resolves its permissions again before dispatching
        anything, because an approval authorises a plan rather than freezing
        the contract and the decisions that were current when that plan was
        shown, then resumes the ordinary dispatch and recording flow. A
        model-originated plan is also checked again against the compiler's
        gates and runs only behind its audit summary; see
        ``_run_model_plan``.
        """
        return self._run_plan(plan, confirmed=True)

    def _run_plan(self, plan: Plan, *, confirmed: bool) -> BrainResponse:
        """Validate, resolve, then dispatch a whole plan generically.

        A plan whose origin is anything but exactly DETERMINISTIC takes
        ``_run_model_plan`` instead, where only a well-formed model-originated
        plan can proceed, and every other plan the path below. Three phases
        run in a fixed order. Every
        step is first validated against the declared action contracts, then
        every step's permission is decided, and only then is anything
        dispatched. A contract failure anywhere refuses the plan before
        PermissionManager is consulted at all. A denial anywhere denies the
        entire plan, and an outstanding confirmation anywhere holds the
        entire plan, so neither outcome can arrive after a step already ran.
        Every outcome that records goes through _record_interaction, which
        honours the plan's recording policy.
        """
        if plan.origin is not PlanOrigin.DETERMINISTIC:
            return self._run_model_plan(plan, confirmed=confirmed)

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
            # The plan offered for approval is pinned to the exact state its
            # steps act on now, so the approval, bound to this plan's
            # fingerprint, cannot later be spent on different state. Any
            # preview of that state is shown here and goes nowhere else.
            prepared = self.tool_manager.prepare_confirmation(plan)
            return BrainResponse(
                text=self._format_confirmation_request(
                    resolved,
                    prepared.previews,
                ),
                needs_confirmation=True,
                plan=prepared.plan,
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

    def _run_model_plan(
        self,
        plan: Plan,
        *,
        confirmed: bool,
        notices: tuple[str, ...] = (),
    ) -> BrainResponse:
        """Offer, refuse, or run one model-originated plan.

        The same gates run whether the plan is being offered or has been
        approved: its shape and origin, then ``recheck_model_plan`` against
        Tool Manager's current registry, then PermissionManager. A plan that
        fails any of them is refused, and a denied action is refused before
        any approval is offered, so neither can ever reach an audit summary
        or a tool. Every other plan needs exactly one approval from the
        owner, including when its action is allowed outright: an unapproved
        plan comes back for approval, as the very same immutable plan and
        never prepared or rebound, with ``notices`` as its whole text, and
        an approved one goes to ``_dispatch_model_plan``. ``notices`` are
        shown with an offer or a refusal; an approved plan has none.
        """
        refusal = self._model_plan_refusal(plan)
        if refusal is not None:
            return BrainResponse(with_notices(refusal, notices), plan=plan)

        step = plan.steps[0]
        permission = self.permissions.check(step.tool_name, step.action)
        decision = permission.decision
        if decision is not PermissionDecision.ALLOWED and (
            decision is not PermissionDecision.REQUIRES_CONFIRMATION
        ):
            self.logger.warning(
                "Denied model-proposed action: %s.%s",
                step.tool_name,
                step.action,
            )
            return BrainResponse(
                with_notices(f"Permission denied: {permission.reason}", notices),
                plan=plan,
            )

        if not confirmed:
            self.logger.info(
                "Owner approval required for model-proposed action: %s.%s",
                step.tool_name,
                step.action,
            )
            return BrainResponse(
                "\n".join(notices),
                needs_confirmation=True,
                plan=plan,
            )
        return self._dispatch_model_plan(plan, confirmed=confirmed)

    def _model_plan_refusal(self, plan: Plan) -> str | None:
        """Return why a model-originated plan cannot proceed, or None.

        The plan must still be exactly a well-formed model-originated plan,
        and its one step must still pass every compiler gate against the
        registry Tool Manager holds now. Nothing is parsed or rebuilt, and
        the reason is fixed text that repeats nothing the model supplied.
        """
        if not is_model_plan(plan):
            self.logger.error(
                "Refused a model-originated plan that is not well formed."
            )
            return MODEL_PLAN_REFUSED_MESSAGE
        rejection = recheck_model_plan(
            plan,
            self.tool_manager.contracts,
            self.logger,
        )
        if rejection is not None:
            return rejection.message
        return None

    def _dispatch_model_plan(
        self,
        plan: Plan,
        *,
        confirmed: bool,
    ) -> BrainResponse:
        """Run one approved model-originated plan, at most once, under audit.

        An unapproved plan is refused here whatever called this. Otherwise
        the plan's audit summary is inserted and committed first; when it
        cannot be, nothing is dispatched and the attempt is refused, and the
        approval is not given back. The step then runs exactly once, with
        exactly the immutable arguments the owner approved, and its outcome,
        succeeded or failed by the tool's own success flag, is appended to
        that summary. The tool's result is always shown, followed by which
        summary records it, or by a notice that the outcome could not be
        recorded. Nothing is retried, and nothing is recorded as interaction
        history, because a model-originated plan is transient.
        """
        if confirmed is not True:
            self.logger.error(
                "Refused to dispatch a model-proposed action without approval."
            )
            return BrainResponse(MODEL_PLAN_UNAPPROVED_MESSAGE, plan=plan)

        step = plan.steps[0]
        contract = self.tool_manager.contracts.contract_for(
            step.tool_name,
            step.action,
        )
        summary_id = self._authorize_dispatch(contract)
        if summary_id is None:
            return BrainResponse(AUDIT_SUMMARY_UNRECORDED_MESSAGE, plan=plan)

        self.logger.info(
            "Dispatching approved model-proposed action %s under audit "
            "summary %s.",
            contract.identifier,
            summary_id,
        )
        result = self.tool_manager.execute_tool(
            step.tool_name,
            step.action,
            step.arguments,
        )
        if type(result) is not ToolExecutionResult:
            # Anything but a real result is neither shown nor trusted: it
            # counts as the shared fixed tool failure.
            result = ToolExecutionResult(
                False,
                Failure(FailureKind.TOOL_EXECUTION_FAILED).message,
            )
        succeeded = result.success is True
        outcome = AuditOutcome.SUCCEEDED if succeeded else AuditOutcome.FAILED
        if self._record_audit_outcome(summary_id, outcome):
            notice = audit_recorded_notice(summary_id)
        else:
            notice = audit_outcome_unrecorded_notice(summary_id)
        return BrainResponse(f"{result.message}\n\n{notice}", plan=plan)

    def _authorize_dispatch(self, contract: ActionContract | None) -> int | None:
        """Commit the audit summary that authorises one dispatch, or refuse.

        The summary holds only the contract's canonical identifier, the
        owner's approval, and the target class the contract implies. Any
        failure to commit it, and any id that is not an exact positive int,
        returns None, which refuses the dispatch; the failure itself is
        never bound, so nothing of it reaches the log or the owner.
        """
        if contract is None:
            self.logger.error(
                "Refused a model-proposed action: its contract is missing."
            )
            return None
        try:
            summary_id = self.memory.record_audit_summary(
                contract.identifier,
                AuditDecision.OWNER_APPROVED,
                audit_target(contract),
            )
        except Exception:  # noqa: BLE001 - no recorded summary, no dispatch.
            self.logger.error(
                "Refused a model-proposed action: its audit summary was not "
                "recorded."
            )
            return None
        if type(summary_id) is not int or summary_id < 1:
            self.logger.error(
                "Refused a model-proposed action: its audit summary id was "
                "not valid."
            )
            return None
        return summary_id

    def _record_audit_outcome(
        self,
        summary_id: int,
        outcome: AuditOutcome,
    ) -> bool:
        """Append one outcome to its summary, returning whether it was stored.

        The tool has already run, so a failure here changes nothing about
        its result: it is reported to the owner, never retried, and never
        bound, so nothing of it reaches the log.
        """
        try:
            self.memory.record_audit_outcome(summary_id, outcome)
        except Exception:  # noqa: BLE001 - the result is shown either way.
            self.logger.error(
                "The outcome of audit summary %s could not be recorded.",
                summary_id,
            )
            return False
        return True

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
    def _format_confirmation_request(
        resolved: _ResolvedPlan,
        previews: tuple[str, ...] = (),
    ) -> str:
        """Render the confirmation request for a resolved plan.

        A single-step plan keeps its existing wording exactly. A longer plan
        is listed in plan order, naming each step's tool action and the
        permission outcome it resolved to. Neither form repeats an argument
        value, so no host path can reach the owner's interface, an
        interaction row, or a log through this text. ``previews`` are the
        tools' own texts showing the owner what an approval covers; each
        follows on its own lines, and with none the request is exactly as
        before. The request is shown and returned, never recorded or logged.
        """
        if len(resolved.steps) == 1:
            lines = [f"Confirmation required: {resolved.steps[0].permission.reason}"]
        else:
            lines = [
                f"Confirmation required for {len(resolved.steps)} planned actions:"
            ]
            for position, resolved_step in enumerate(resolved.steps, start=1):
                outcome = resolved_step.permission.decision.value.replace("_", " ")
                lines.append(
                    f"{position}. {resolved_step.step.tool_name}."
                    f"{resolved_step.step.action}: {outcome}"
                )
        return "\n".join((*lines, *previews))

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
