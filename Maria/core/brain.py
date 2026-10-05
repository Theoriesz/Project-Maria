"""Main application controller for Maria.

The brain coordinates the configured planner, memory layer, permission
manager, and tool manager, and, when a reasoning provider was supplied, the
conversation module. It does not directly perform database work, tool logic,
or any provider-specific work, and it never imports a reasoning adapter.

Brain reads the current time exactly once per request, through its one clock
seam, after the empty-request check and before planning, and hands that
reference instant with the configured time zone key to the planner and, on
the model path, to the compiler; every timing decision of the request uses
it, and confirming an approved plan reads no clock. Brain holds no date
logic itself. Besides the request text and that timing, the planner receives
nothing. The only stored inputs to reasoning are records the owner enabled
for context - Tier A memories and enabled open tasks and reminders, and what
Tier B's bounded search finds among enabled memories, notes, and
conversation summaries - read through the memory layer's bounded context
queries on the conversational path, and on a summary request only by id, to
recheck the records supplied to the conversation; interaction history and
saved conversations are never read for planning or reasoning.

A planner's plan whose one step's contract declares a session input, from
V1-20, is given that text here, generically, before anything else checks it:
the canonical transcript of exactly the session's retained exchanges for an
explicit save, or, for a summary request, a draft the local model writes from
exactly those exchanges, after every record supplied to them is rechecked and
with no other context. The conversation module builds every resulting plan,
so the choice of recording policy stays with that producer, and nothing here
names a tool or an action.

A model reply the conversation module recognises as a proposal attempt is
compiled here, through ``core/plan_compiler.py`` and against Tool Manager's
active contract registry, into a model-originated plan of one to five steps
or a fixed refusal. Such a plan never runs without exactly one approval from
the owner for the whole plan, whatever PermissionManager decides for its
actions, and a plan with any denied action is refused whole before any
approval is offered. The approved plan is the same immutable plan: every
step is checked again, nothing is rebuilt or reparsed, no provider is asked
anything, and each step runs at most once, in order, only after its own
content-free audit summary is committed. A plan of several steps stops at
the first failure, audit write that cannot be confirmed, or interruption,
and its report says which steps ran and which did not.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from logging import Logger

from core.action_contracts import ActionContract, ContractViolation, SessionInput
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
    declared_session_input,
    draft_summary,
    is_conversational_plan,
    is_generic_fallback,
    saved_conversation_plan,
    summary_draft_plan,
    summary_refused_plan,
    unsupported_session_input_plan,
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
    PLAN_MAX_STEPS,
    Plan,
    PlanOrigin,
    Planner,
    PlanStep,
    RecordingPolicy,
    is_model_plan,
    own_origin,
    plan_snapshot,
)
from core.reasoning import (
    ReasoningExchange,
    ReasoningProvider,
    ReasoningResponse,
    ReasoningSettings,
)
from core.time_resolution import TimingContext
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
PLAN_STEP_LIMIT_REFUSED_MESSAGE = (
    "Maria refused that plan because a plan may hold at most "
    f"{PLAN_MAX_STEPS} steps."
)

# What a step of a model-proposed plan of several steps is reported as, after
# its number and its declared tool action. None names an argument value.
_STEP_COMPLETED = "completed."
_STEP_FAILED = "failed."
_STEP_NOT_RUN = "not run."
_STEP_NOT_AUTHORISED = (
    "not run, because its audit summary could not be recorded."
)
_STEP_INTERRUPTED = "interrupted, so its outcome is unknown."
AUDIT_SUMMARY_UNCONFIRMED_NOTICE = (
    "Maria could not confirm whether its audit summary was recorded."
)


def _reference_instant() -> datetime:
    """Return the current moment as an aware UTC datetime.

    This is Brain's one clock seam. ``process`` calls it exactly once per
    request, before planning, and nothing else in Brain reads a clock.
    """
    return datetime.now(timezone.utc)


def audit_recorded_notice(summary_id: int) -> str:
    """Return the notice that follows a result whose outcome was recorded."""
    return f"Recorded as audit summary {summary_id}."


def audit_outcome_unrecorded_notice(summary_id: int) -> str:
    """Return the notice for a recorded summary whose outcome was not."""
    return (
        f"Recorded as audit summary {summary_id}, but its outcome could not be "
        "recorded, so that summary shows the outcome as unknown."
    )


def audit_outcome_missing_notice(summary_id: int) -> str:
    """Return the notice for a recorded summary never given an outcome.

    That is a step interrupted after its summary was recorded, before its
    tool returned a result, so no outcome was ever written for it.
    """
    return (
        f"Recorded as audit summary {summary_id}, but no outcome was "
        "recorded, so that summary shows the outcome as unknown."
    )


def audit_outcome_unconfirmed_notice(summary_id: int) -> str:
    """Return the notice for a summary whose outcome write was interrupted.

    The write may have committed before the interruption arrived, so Maria
    claims neither that the outcome is stored nor that it is missing.
    """
    return (
        f"Recorded as audit summary {summary_id}, but Maria could not confirm "
        "whether its outcome was recorded."
    )


@dataclass(frozen=True)
class BrainResponse:
    """Response returned by Maria's controller.

    ``conversation`` is set by a completed conversational reply, to the
    session's conversation as it stands after that reply, and to an empty
    conversation by a conversational turn, or a summary request, that had to
    clear it, whatever else that request produced. Every other response
    leaves it None, so no command, its result, a proposal, a summary draft,
    or a failed conversational turn ever becomes conversation.
    ``supplied_context`` travels with it: the fingerprints of every memory,
    note, conversation summary, task, and reminder supplied since that
    conversation was last cleared, which the session keeps beside it.

    A model-originated plan awaiting approval comes back with
    ``needs_confirmation`` set, the plan itself, and as ``text`` the fixed
    approval notice of each step awaiting confirmation, once each, then
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
    supplied_summaries: Sequence[object] = ()
    searched_summaries: Sequence[object] = ()
    enabled_tasks: Sequence[object] = ()
    enabled_reminders: Sequence[object] = ()
    supplied_tasks: Sequence[object] = ()
    supplied_reminders: Sequence[object] = ()


@dataclass(frozen=True)
class _SessionInputBinding:
    """What supplying one planner plan's session input produced.

    Transient: ``plan`` is what then runs, through the ordinary path, and
    ``cleared`` says the session's conversation and supplied-record
    fingerprints must be cleared because a supplied record could not be
    confirmed current. It is never persisted, logged, or handed elsewhere.
    """

    plan: Plan
    cleared: bool = False


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

    @property
    def first_unpermitted_step(self) -> _ResolvedStep | None:
        """Return the earliest step neither allowed nor awaiting approval.

        A model-originated plan may proceed only when every decision is
        exactly ALLOWED or exactly REQUIRES_CONFIRMATION, so anything else,
        DENIED included, refuses the whole plan at this step.
        """
        for resolved_step in self.steps:
            decision = resolved_step.permission.decision
            if decision is not PermissionDecision.ALLOWED and (
                decision is not PermissionDecision.REQUIRES_CONFIRMATION
            ):
                return resolved_step
        return None


class _StepStage(str, Enum):
    """How far one step of a model-proposed plan of several steps has got."""

    WAITING = "waiting"
    AUTHORISING = "authorising"
    AUTHORISED = "authorised"
    DISPATCHING = "dispatching"
    RECORDING = "recording"


class _StopReason(str, Enum):
    """Why a model-proposed plan of several steps stopped before its end."""

    STEP_FAILED = "step_failed"
    SUMMARY_UNRECORDED = "summary_unrecorded"
    OUTCOME_UNRECORDED = "outcome_unrecorded"
    INTERRUPTED = "interrupted"


# What the closing sentence of a stopped plan's report says about why it
# stopped. A failed step is shown failed in its own block.
_STOP_CLAUSES = {
    _StopReason.STEP_FAILED: "",
    _StopReason.SUMMARY_UNRECORDED: (
        " because its audit summary could not be recorded"
    ),
    _StopReason.OUTCOME_UNRECORDED: (
        " because its audit outcome could not be recorded"
    ),
    _StopReason.INTERRUPTED: " because the run was interrupted",
}


class _StepAttempt:
    """What is known about the step of a model-proposed plan now running.

    Transient progress state for one step, replaced whole when the next step
    begins, so an interruption is always reported against the step it
    actually reached. It is never persisted, logged, or handed elsewhere.
    """

    __slots__ = ("index", "stage", "summary_id", "result")

    def __init__(self, index: int) -> None:
        self.index = index
        self.stage = _StepStage.WAITING
        self.summary_id: int | None = None
        self.result: ToolExecutionResult | None = None


@dataclass(frozen=True)
class _StepReport:
    """What the owner is told about one step a plan of several steps reached.

    ``status`` follows the step's number and declared tool action, and
    ``lines`` hold the tool's own message and the step's audit notice when
    there are any. ``completed`` says whether the tool reported success, and
    ``entered`` whether its dispatch was entered at all.
    """

    status: str
    lines: tuple[str, ...] = ()
    completed: bool = False
    entered: bool = False


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
        generic no-action fallback, holding exactly DETERMINISTIC as its own
        origin, is the request answered by the local
        model instead, in the context of ``conversation``: the caller's
        earlier exchanges, oldest first, with ``supplied_context``, the
        fingerprints of the memories, notes, and conversation summaries
        supplied since they began.
        The model may answer conversationally or propose one action or a
        plan of two to five; see ``_converse``. With reasoning disabled the fallback is answered and
        recorded exactly as it always was, whether or not a provider is
        present.

        Every other plan runs exactly as it always has and never sees any
        stored context. The one exception to its never seeing the
        conversation is a plan whose one step's contract declares a session
        input: that plan is given exactly the text it declares, here and
        nowhere else, before contracts, permissions, or any tool see it; see
        ``_with_session_input``. A plan that declares none is untouched.

        The clock is read once, right after the empty-request check and
        before planning, for every request whether or not it involves
        timing, and that one reference instant, with the configured time
        zone key, is what the planner and any model proposal of this request
        resolve timing against, however long the model takes to reply.
        """
        cleaned_input = user_input.strip()
        if not cleaned_input:
            return BrainResponse("Please enter a request.")
        timing = TimingContext(_reference_instant(), _configured_zone(self.config))

        self.logger.info("Received user request.")

        plan = self.planner.create_plan(cleaned_input, timing=timing)

        if (
            self.reasoning is not None
            and self.config.reasoning.enabled
            and own_origin(plan) is PlanOrigin.DETERMINISTIC
            and is_generic_fallback(plan)
        ):
            return self._converse(
                cleaned_input,
                conversation,
                supplied_context,
                timing,
            )

        # A deterministic plan is read once, here, so asking whether it
        # declares a session input never reads its steps a second time: what
        # follows, and ``_run_plan`` itself, work on this one snapshot. A
        # plan whose steps cannot be read within the bound is refused now,
        # exactly as ``_run_plan`` refuses it, and never read again.
        if own_origin(plan) is PlanOrigin.DETERMINISTIC:
            snapshot = plan_snapshot(plan)
            if snapshot is None:
                return self._refused_step_limit(plan)
            plan = snapshot
        binding = self._with_session_input(plan, conversation, supplied_context)
        response = self._run_plan(binding.plan, confirmed=False)
        if not binding.cleared:
            return response
        # Nothing from the refused request is kept, but whatever its recheck
        # cleared stays cleared.
        return replace(response, conversation=(), supplied_context=frozenset())

    def _converse(
        self,
        request: str,
        conversation: tuple[ReasoningExchange, ...],
        supplied_context: frozenset[ContextFingerprint],
        timing: TimingContext | None = None,
    ) -> BrainResponse:
        """Answer one unrecognised request with the local model.

        This is the only place context is read to be sent, and only after
        every gate above has passed; ``_read_context`` says which bounded
        reads run. A summary request only rechecks, by id, records already
        supplied, and sends none of them; see ``_read_supplied_context``.
        When any read here fails, the conversation module sends no context
        and no earlier exchange.

        The conversation module settles the whole turn in proposal mode, so
        the request carries Maria's fixed proposal instructions and the
        provider is asked exactly once. A conversational outcome is a
        zero-step plan that it, as the plan's producer, made transient, and
        Brain accepts nothing else as one, nor one that does not hold exactly
        DETERMINISTIC as its own origin. That plan takes the ordinary
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
            supplied_summaries_read=reads.supplied_summaries,
            searched_summaries_read=reads.searched_summaries,
            enabled_tasks_read=reads.enabled_tasks,
            enabled_reminders_read=reads.enabled_reminders,
            supplied_tasks_read=reads.supplied_tasks,
            supplied_reminders_read=reads.supplied_reminders,
        )
        if turn.proposal is not None:
            response = self._consider_proposal(
                request,
                turn.proposal,
                turn.notices,
                timing,
            )
        elif own_origin(turn.plan) is PlanOrigin.DETERMINISTIC and (
            is_conversational_plan(turn.plan)
        ):
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
        timing: TimingContext | None = None,
    ) -> BrainResponse:
        """Compile one proposal attempt, then offer or refuse the plan.

        The compiler is handed the owner's cleaned request, the reply exactly
        as it arrived, Tool Manager's active contract registry, and the
        request's one timing context, which only a creation step's timing
        uses. A refused proposal answers with its fixed message and nothing
        is repaired, retried, or asked of the model again. A compiled plan is
        offered for approval through ``_run_model_plan``. Either way the
        turn's notices are shown exactly once, with that answer, and are kept
        nowhere.
        """
        outcome = compile_proposal(
            request,
            proposal,
            self.tool_manager.contracts,
            self.logger,
            timing=timing,
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
        ever to be sent. Its reads of enabled open tasks and reminders whose
        text and timing are still the approved ones always run too, the same
        way. Records supplied earlier in the conversation are then rechecked
        by id, and only when there are any. Tier B's searches run only when
        the request yields search terms: its memory search only when Tier A's
        read found more enabled memories than it considers, starting after
        the last one it considered, and its note and summary searches over
        notes and summaries the owner enabled. Every read goes through the
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
            summary_ids = supplied_record_ids(supplied, ContextSource.SUMMARY)
            task_ids = supplied_record_ids(supplied, ContextSource.TASK)
            reminder_ids = supplied_record_ids(supplied, ContextSource.REMINDER)
            return _ContextReads(
                enabled=enabled,
                enabled_tasks=self.memory.list_context_tasks(
                    limit=CONTEXT_QUERY_LIMIT,
                    max_text_bytes=CONTEXT_MAX_BYTES,
                ),
                enabled_reminders=self.memory.list_context_reminders(
                    limit=CONTEXT_QUERY_LIMIT,
                    max_text_bytes=CONTEXT_MAX_BYTES,
                ),
                supplied_tasks=(
                    self.memory.context_tasks_by_id(
                        task_ids,
                        max_text_bytes=CONTEXT_MAX_BYTES,
                    )
                    if task_ids
                    else ()
                ),
                supplied_reminders=(
                    self.memory.context_reminders_by_id(
                        reminder_ids,
                        max_text_bytes=CONTEXT_MAX_BYTES,
                    )
                    if reminder_ids
                    else ()
                ),
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
                supplied_summaries=(
                    self.memory.context_summaries_by_id(
                        summary_ids,
                        max_text_bytes=CONTEXT_MAX_BYTES,
                    )
                    if summary_ids
                    else ()
                ),
                searched_summaries=(
                    self.memory.search_context_summaries(
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

    def _read_supplied_context(
        self,
        supplied: frozenset[ContextFingerprint],
    ) -> _ContextReads:
        """Recheck, by id, every record supplied to the session's conversation.

        These are the only reads a summary draft makes before its exchanges
        are sent again: no Tier A read and no search runs, so nothing beyond
        the retained exchanges can reach the draft request. A record is read
        only when one of its source was supplied. Any read that fails ends
        them all, and the result's ``enabled`` is then None.
        """
        try:
            memory_ids = supplied_record_ids(supplied, ContextSource.MEMORY)
            note_ids = supplied_record_ids(supplied, ContextSource.NOTE)
            summary_ids = supplied_record_ids(supplied, ContextSource.SUMMARY)
            task_ids = supplied_record_ids(supplied, ContextSource.TASK)
            reminder_ids = supplied_record_ids(supplied, ContextSource.REMINDER)
            return _ContextReads(
                enabled=(),
                supplied_tasks=(
                    self.memory.context_tasks_by_id(
                        task_ids,
                        max_text_bytes=CONTEXT_MAX_BYTES,
                    )
                    if task_ids
                    else ()
                ),
                supplied_reminders=(
                    self.memory.context_reminders_by_id(
                        reminder_ids,
                        max_text_bytes=CONTEXT_MAX_BYTES,
                    )
                    if reminder_ids
                    else ()
                ),
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
                supplied_summaries=(
                    self.memory.context_summaries_by_id(
                        summary_ids,
                        max_text_bytes=CONTEXT_MAX_BYTES,
                    )
                    if summary_ids
                    else ()
                ),
            )
        except (RuntimeError, ValueError):
            self.logger.warning("Unable to recheck supplied context.")
            return _ContextReads(enabled=None)

    def _with_session_input(
        self,
        plan: Plan,
        conversation: tuple[ReasoningExchange, ...],
        supplied_context: frozenset[ContextFingerprint],
    ) -> _SessionInputBinding:
        """Supply the one session text a planner's plan declares, if any.

        The contract registry Tool Manager holds says which argument, if
        any, Maria supplies; a plan declaring none comes back untouched. The
        conversation module settles whether the declaration is acceptable,
        and builds every plan returned here, so this method chooses no
        recording policy and names no tool or action.

        A save is given the canonical transcript of exactly the retained
        exchanges, reading no other record. A summary request is first
        checked against PermissionManager, so a denied one never sends the
        exchanges anywhere, then refused when reasoning is unavailable;
        otherwise every supplied record is rechecked by id and the local
        model is asked once for a draft. A declared input of any other kind
        is refused with the fixed session-input refusal, so nothing is
        supplied that this method does not name. This runs only from
        ``process``, for a new request: never at confirmation, for a
        model-originated plan, or on the conversational path.
        """
        request = declared_session_input(
            plan,
            self.tool_manager.session_input_argument,
        )
        if request is None:
            return _SessionInputBinding(plan)
        if request.refusal is not None:
            self.logger.warning("Refused a plan carrying its own session input.")
            return _SessionInputBinding(request.refusal)
        if request.kind is SessionInput.CONVERSATION:
            self.logger.info("Supplying the session's retained exchanges to a save.")
            return _SessionInputBinding(
                saved_conversation_plan(request, conversation)
            )
        if request.kind is not SessionInput.CONVERSATION_SUMMARY_DRAFT:
            self.logger.warning("Refused a plan declaring an unknown session input.")
            return _SessionInputBinding(unsupported_session_input_plan(request))

        permission = self.permissions.check(
            request.step.tool_name,
            request.step.action,
        )
        if permission.decision is PermissionDecision.DENIED:
            self.logger.warning("Refused a summary request before drafting.")
            return _SessionInputBinding(
                summary_refused_plan(
                    request,
                    f"Permission denied: {permission.reason}",
                )
            )
        if self.reasoning is None or not self.config.reasoning.enabled:
            self.logger.warning("Refused a summary request: reasoning unavailable.")
            return _SessionInputBinding(
                summary_refused_plan(
                    request,
                    Failure(FailureKind.REASONING_UNAVAILABLE).message,
                )
            )

        reads = (
            self._read_supplied_context(
                validated_supplied_context(supplied_context)
            )
            if conversation
            else _ContextReads(enabled=())
        )
        draft = draft_summary(
            self.reasoning,
            self.config.reasoning,
            conversation,
            self.logger,
            supplied_context=supplied_context,
            supplied_memories_read=(
                None if reads.enabled is None else reads.supplied_memories
            ),
            supplied_notes_read=reads.supplied_notes,
            supplied_summaries_read=reads.supplied_summaries,
            supplied_tasks_read=reads.supplied_tasks,
            supplied_reminders_read=reads.supplied_reminders,
        )
        return _SessionInputBinding(
            summary_draft_plan(request, draft),
            draft.cleared,
        )

    def execute_confirmed_plan(self, plan: Plan) -> BrainResponse:
        """Run an already-approved plan without planning the request again.

        The caller has bound the owner's approval to this exact plan, so
        Brain retrieves no context, invokes no planner, and asks no reasoning
        provider anything. It validates the same plan against the action
        contracts again and resolves its permissions again before dispatching
        anything, because an approval authorises a plan rather than freezing
        the contract and the decisions that were current when that plan was
        shown, then resumes the ordinary dispatch and recording flow. The
        session hands it the very snapshot whose fingerprint it compared, and
        a plan handed here directly is snapshotted once in the same way, so
        what is validated, resolved, and dispatched is one bounded read. A
        model-originated plan is also checked again against the compiler's
        gates and runs only behind its audit summary; see
        ``_run_model_plan``.
        """
        return self._run_plan(plan, confirmed=True)

    def _run_plan(self, plan: Plan, *, confirmed: bool) -> BrainResponse:
        """Validate, resolve, then dispatch a whole plan generically.

        A plan that does not hold every one of its own fields, with a policy
        and an origin that are exactly members, has no origin of its own,
        as ``own_origin`` reads it, and is refused before either path is
        chosen, with the fixed model-plan refusal and its fixed log line,
        before any contract, permission, audit write, or tool. A plan whose
        own origin is exactly DETERMINISTIC takes the path below; every other
        takes ``_run_model_plan``, where only a well-formed model-originated
        plan can proceed. A deterministic plan is then read once, by
        ``plan_snapshot``, and every phase below, recording included, works
        on that snapshot, so the steps validated, resolved, prepared, and
        dispatched are one sequence of at most ``PLAN_MAX_STEPS``, whatever
        their container reports or would hand out on a later read. A plan
        whose steps cannot be read, which construction allows, or hold more
        than ``PLAN_MAX_STEPS`` is refused before any contract, permission,
        or tool, and nothing is recorded. A plan held for confirmation is
        prepared from the snapshot, so the plan that becomes pending holds
        exactly the steps checked here. Three phases then run in a fixed
        order. Every
        step is first validated against the declared action contracts, then
        every step's permission is decided, and only then is anything
        dispatched. A contract failure anywhere refuses the plan before
        PermissionManager is consulted at all. A denial anywhere denies the
        entire plan, and an outstanding confirmation anywhere holds the
        entire plan, so neither outcome can arrive after a step already ran.
        Every outcome that records goes through _record_interaction, which
        honours the plan's recording policy.
        """
        origin = own_origin(plan)
        if origin is None:
            self.logger.error(
                "Refused a model-originated plan that is not well formed."
            )
            return BrainResponse(MODEL_PLAN_REFUSED_MESSAGE, plan=plan)
        if origin is not PlanOrigin.DETERMINISTIC:
            return self._run_model_plan(plan, confirmed=confirmed)

        snapshot = plan_snapshot(plan)
        if snapshot is None:
            return self._refused_step_limit(plan)
        # Everything below, recording included, works on the snapshot alone.
        plan = snapshot
        steps = plan.steps

        if not steps:
            response = plan.response_if_no_action or "I do not know how to handle that yet."
            self._record_interaction(plan, response)
            return BrainResponse(response, plan=plan)

        violation = self._validate_plan(steps)
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

        resolved = self._resolve_plan(plan, steps)

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
            # preview of that state is shown here and goes nowhere else. It
            # is prepared from the snapshot, so what becomes pending holds
            # exactly the steps checked above, in a tuple that cannot change.
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

    def _refused_step_limit(self, plan: Plan) -> BrainResponse:
        """Refuse a plan whose steps cannot be read within the step bound.

        Nothing is checked, dispatched, or recorded: the reply and the log
        line are fixed, and the plan handed back is the one refused.
        """
        self.logger.error("Refused a plan holding too many steps.")
        return BrainResponse(PLAN_STEP_LIMIT_REFUSED_MESSAGE, plan=plan)

    def _validate_plan(
        self,
        steps: tuple[PlanStep, ...],
    ) -> ContractViolation | None:
        """Check every step against its action contract, in plan order.

        ``steps`` are the plan's steps exactly as ``_run_plan`` read them.
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
        for step in steps:
            violation = self.tool_manager.validate_action(
                step.tool_name,
                step.action,
                step.arguments,
            )
            if violation is not None:
                return violation
        return None

    def _resolve_plan(
        self,
        plan: Plan,
        steps: tuple[PlanStep, ...],
    ) -> _ResolvedPlan:
        """Decide permission for every step before any step is dispatched.

        ``steps`` are the plan's steps as the caller read them: a
        deterministic plan's one bounded read, or a model plan's own exact
        tuple. Steps are inspected in plan order and each is paired with the
        result PermissionManager returned for it. No tool runs during this
        pass, the plan is neither rebuilt nor mutated, and the resolution is
        returned rather than stored, so it cannot outlive the request
        producing it.
        """
        return _ResolvedPlan(
            plan=plan,
            steps=tuple(
                _ResolvedStep(
                    step,
                    self.permissions.check(step.tool_name, step.action),
                )
                for step in steps
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
        """Offer, refuse, or run one model-originated plan, as a whole.

        The same gates run whether the plan is being offered or has been
        approved: its shape, origin, and step count, then
        ``recheck_model_plan`` for every step against Tool Manager's current
        registry, then PermissionManager for every step, all before any audit
        write or tool. A plan that fails any of them is refused whole, and a
        plan with any step that is not allowed or awaiting approval is
        refused before any approval is offered, with the first such step's
        reason, so neither can ever reach an audit summary or a tool. Every
        other plan needs exactly one approval from the owner for the whole
        plan, including when every action is allowed outright: an unapproved
        plan comes back for approval, as the very same immutable plan and
        never prepared or rebound, whose whole text is the fixed notice of
        each step awaiting confirmation, once each, then ``notices``, and an
        approved one goes to ``_dispatch_model_plan``. ``notices`` are shown
        with an offer or a refusal; an approved plan has none.
        """
        refusal = self._model_plan_refusal(plan)
        if refusal is not None:
            return BrainResponse(with_notices(refusal, notices), plan=plan)

        resolved = self._resolve_plan(plan, plan.steps)
        unpermitted = resolved.first_unpermitted_step
        if unpermitted is not None:
            self.logger.warning(
                "Denied model-proposed action: %s.%s",
                unpermitted.step.tool_name,
                unpermitted.step.action,
            )
            return BrainResponse(
                with_notices(
                    f"Permission denied: {unpermitted.permission.reason}",
                    notices,
                ),
                plan=plan,
            )

        if not confirmed:
            for resolved_step in resolved.steps:
                self.logger.info(
                    "Owner approval required for model-proposed action: %s.%s",
                    resolved_step.step.tool_name,
                    resolved_step.step.action,
                )
            return BrainResponse(
                "\n".join((*self._approval_notices(resolved), *notices)),
                needs_confirmation=True,
                plan=plan,
            )
        return self._dispatch_model_plan(plan, confirmed=confirmed)

    def _approval_notices(self, resolved: _ResolvedPlan) -> tuple[str, ...]:
        """Return the fixed notices a model plan's approval shows, once each.

        PermissionManager is asked, by each step's own tool and action, for
        the notice of every step it resolved as needing confirmation, so no
        tool or action is named here; each distinct notice is kept once, in
        plan order. A notice is PermissionManager's fixed text and repeats
        nothing stored, proposed, or requested. It is shown with the offer
        alone, never added to the plan, and kept nowhere.
        """
        shown: list[str] = []
        for resolved_step in resolved.confirmation_required_steps:
            notice = self.permissions.confirmation_notice(
                resolved_step.step.tool_name,
                resolved_step.step.action,
            )
            if type(notice) is str and notice and notice not in shown:
                shown.append(notice)
        return tuple(shown)

    def _model_plan_refusal(self, plan: Plan) -> str | None:
        """Return why a model-originated plan cannot proceed, or None.

        The plan must still be exactly a well-formed model-originated plan,
        of 1 to ``PLAN_MAX_STEPS`` steps, and every one of its steps must
        still pass every compiler gate against the registry Tool Manager
        holds now. Nothing is parsed or rebuilt, and the reason is fixed text
        that repeats nothing the model supplied.
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

        An unapproved plan is refused here whatever called this. A plan of
        several steps runs through ``_dispatch_model_steps``. Otherwise the
        plan's audit summary is inserted and committed first; when it
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

        if len(plan.steps) > 1:
            return self._dispatch_model_steps(plan)

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
        result = _tool_result(
            self.tool_manager.execute_tool(
                step.tool_name,
                step.action,
                step.arguments,
            )
        )
        succeeded = result.success is True
        outcome = AuditOutcome.SUCCEEDED if succeeded else AuditOutcome.FAILED
        if self._record_audit_outcome(summary_id, outcome):
            notice = audit_recorded_notice(summary_id)
        else:
            notice = audit_outcome_unrecorded_notice(summary_id)
        return BrainResponse(f"{result.message}\n\n{notice}", plan=plan)

    def _dispatch_model_steps(self, plan: Plan) -> BrainResponse:
        """Run an approved model-originated plan of several steps, in order.

        Each step reached gets its own audit summary, committed before that
        step is dispatched, and is dispatched at most once, with exactly the
        immutable arguments the owner approved. Its outcome, succeeded or
        failed by the tool's own success flag, is then appended to its
        summary. The next step is reached only when this one succeeded and
        its outcome was recorded, so the plan stops at the first step whose
        summary cannot be recorded, whose tool fails, or whose outcome
        cannot be recorded. A step never reached gets no summary at all.

        ``KeyboardInterrupt`` alone is also handled here, and only so the
        owner is told truthfully what is known: it stops the plan, nothing is
        retried or resumed, and no outcome is written merely because the run
        was interrupted. Every other exception propagates as it always has.
        An abrupt end of the process can leave no report at all, and an
        interruption that lands while the report is being built cannot be
        reported either.

        The report names every step by number and declared tool action,
        never by an argument value, and says which completed, which failed,
        which were interrupted, and which did not run. It never claims a
        failed or interrupted step had no effect, and it is shown once and
        kept nowhere: a model-originated plan is transient.
        """
        steps = plan.steps
        reports: list[_StepReport] = []
        stop: _StopReason | None = None
        attempt: _StepAttempt | None = None
        try:
            for index, step in enumerate(steps):
                attempt = _StepAttempt(index)
                contract = self.tool_manager.contracts.contract_for(
                    step.tool_name,
                    step.action,
                )
                attempt.stage = _StepStage.AUTHORISING
                summary_id = self._authorize_dispatch(contract)
                if summary_id is None:
                    reports.append(_StepReport(_STEP_NOT_AUTHORISED))
                    stop = _StopReason.SUMMARY_UNRECORDED
                    break
                attempt.summary_id = summary_id
                attempt.stage = _StepStage.AUTHORISED

                self.logger.info(
                    "Dispatching approved model-proposed action %s under "
                    "audit summary %s.",
                    contract.identifier,
                    summary_id,
                )
                attempt.stage = _StepStage.DISPATCHING
                attempt.result = _tool_result(
                    self.tool_manager.execute_tool(
                        step.tool_name,
                        step.action,
                        step.arguments,
                    )
                )
                attempt.stage = _StepStage.RECORDING
                succeeded = attempt.result.success is True
                outcome = (
                    AuditOutcome.SUCCEEDED
                    if succeeded
                    else AuditOutcome.FAILED
                )
                recorded = self._record_audit_outcome(summary_id, outcome)
                notice = (
                    audit_recorded_notice(summary_id)
                    if recorded
                    else audit_outcome_unrecorded_notice(summary_id)
                )
                reports.append(
                    _StepReport(
                        _STEP_COMPLETED if succeeded else _STEP_FAILED,
                        (attempt.result.message, notice),
                        completed=succeeded,
                        entered=True,
                    )
                )
                if not succeeded:
                    stop = _StopReason.STEP_FAILED
                    break
                if not recorded:
                    stop = _StopReason.OUTCOME_UNRECORDED
                    break
        except KeyboardInterrupt:
            stop = _StopReason.INTERRUPTED
            if len(reports) < len(steps):
                current = (
                    attempt
                    if attempt is not None and attempt.index == len(reports)
                    else None
                )
                reports.append(_interrupted_step_report(current))

        if stop is not None:
            self.logger.warning(
                "Stopped a model-proposed plan at step %s of %s: %s.",
                len(reports),
                len(steps),
                stop.value,
            )
        return BrainResponse(_plan_report(steps, reports, stop), plan=plan)

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


def _configured_zone(config: object) -> str | None:
    """Return the configured time zone key, or None when there is none.

    A loaded configuration holds a validated key or None; anything else
    counts as no zone, so timing that needs one is refused rather than
    resolved against something that is not a configured key.
    """
    zone = getattr(config, "time_zone", None)
    return zone if type(zone) is str else None


def _tool_result(result: object) -> ToolExecutionResult:
    """Return a dispatch's own result, or the shared fixed failure for it.

    Anything but a real result is neither shown nor trusted: it counts as
    the shared fixed tool failure.
    """
    if type(result) is ToolExecutionResult:
        return result
    return ToolExecutionResult(
        False,
        Failure(FailureKind.TOOL_EXECUTION_FAILED).message,
    )


def _interrupted_step_report(attempt: _StepAttempt | None) -> _StepReport:
    """Report the step an interruption reached, saying only what is known.

    A step not yet begun did not run. While its summary write may still
    have been under way, that write may or may not have committed, so it is
    reported as unconfirmed rather than absent. A step whose summary was
    recorded but whose dispatch was never entered did not run, and that
    summary has no outcome. A step whose tool was entered without returning
    has an unknown outcome, and nothing is recorded as failed merely because
    of the interruption. A step whose tool did return keeps that known
    result, and its outcome is unconfirmed once its write had begun, or
    missing when it had not.
    """
    if attempt is None or attempt.stage is _StepStage.WAITING:
        return _StepReport(_STEP_NOT_RUN)
    summary_id = attempt.summary_id
    if summary_id is None:
        return _StepReport(_STEP_NOT_RUN, (AUDIT_SUMMARY_UNCONFIRMED_NOTICE,))
    result = attempt.result
    if result is not None:
        completed = result.success is True
        notice = (
            audit_outcome_unconfirmed_notice(summary_id)
            if attempt.stage is _StepStage.RECORDING
            else audit_outcome_missing_notice(summary_id)
        )
        return _StepReport(
            _STEP_COMPLETED if completed else _STEP_FAILED,
            (result.message, notice),
            completed=completed,
            entered=True,
        )
    missing = audit_outcome_missing_notice(summary_id)
    if attempt.stage is _StepStage.DISPATCHING:
        return _StepReport(_STEP_INTERRUPTED, (missing,), entered=True)
    return _StepReport(_STEP_NOT_RUN, (missing,))


def _plan_report(
    steps: tuple[PlanStep, ...],
    reports: Sequence[_StepReport],
    stop: _StopReason | None,
) -> str:
    """Return the owner's report of a model-proposed plan of several steps.

    One block per step, in plan order, separated by blank lines: the step's
    number, its declared tool action, and how it ended, followed by the
    tool's message and the step's audit notice when it reached either.
    Every step after those reported did not run. A plan that stopped, for
    whatever reason, ends with one sentence saying where and why it stopped
    and which steps it did not run, and, when any dispatch was entered, that
    nothing that ran was undone; only a plan whose every step completed with
    its outcome recorded has no such sentence.
    """
    total = len(steps)
    blocks: list[str] = []
    for position, step in enumerate(steps, start=1):
        label = f"Step {position} of {total}, {step.tool_name}.{step.action}: "
        if position <= len(reports):
            report = reports[position - 1]
            blocks.append("\n".join((label + report.status, *report.lines)))
        else:
            blocks.append(label + _STEP_NOT_RUN)

    stopped = len(reports)
    if stop is not None and stopped:
        clause = _STOP_CLAUSES[stop]
        where = "after" if reports[-1].completed else "at"
        sentence = f"Maria stopped {where} step {stopped}{clause}"
        later = _later_steps(stopped + 1, total)
        if later:
            sentence += f"{',' if clause else ''} and did not run {later}"
        sentence += "."
        if any(report.entered for report in reports):
            sentence += " Nothing that ran was undone."
        blocks.append(sentence)
    return "\n\n".join(blocks)


def _later_steps(first: int, total: int) -> str:
    """Name the steps from ``first`` to ``total``, or nothing if none."""
    if first > total:
        return ""
    if first == total:
        return f"step {first}"
    if first + 1 == total:
        return f"steps {first} and {total}"
    return f"steps {first} to {total}"
