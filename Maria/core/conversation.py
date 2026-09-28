"""Deterministic-first conversation with Maria's local reasoning model.

The deterministic planner always answers first. Only a request it answers
with its generic no-action fallback may instead be answered by the owner's
local model, and this module settles that one conversational turn: it settles
the owner's enabled memory context and what a bounded search found, fits that
context and the current session's earlier exchanges beside the request within
a hard budget, asks the provider once through ``request_reasoning``, and
produces the plan Brain runs. That plan always has zero steps and is always
transient, whether it carries the model's reply or a fixed failure message,
so no conversational request or reply is ever recorded and no tool can be
reached through it.

The reply is inert text. Nothing here parses it, and it can only ever become
the no-action response of a zero-step plan or the assistant half of an
exchange. What the owner is told about context is Maria's own text, built
from the ids she selected and appended after the reply; it never enters the
model's request or the exchange the session keeps, and nothing a model writes
can change it. There is no import path from here to permissions, tool
dispatch, or persistence.

The conversation itself belongs to the caller's session, together with the
fingerprints of every memory and note supplied since that conversation was
last cleared. This module keeps no state between turns; it only computes what
a turn sends and what the session should keep afterwards. Before anything is
sent, those fingerprints are checked against what a bounded read by id finds
enabled now, and a conversation whose supplied record was disabled, changed,
deleted, or forgotten is discarded whole, so an earlier reply cannot carry
that text back to the model. The check sees current state only: a record
changed away and back to the identical approved text between two checks is
indistinguishable from one never changed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from logging import Logger

from core.context import (
    CONTEXT_LIMIT_CLEARED_NOTICE,
    CONTEXT_UNREADABLE_NOTICE,
    CONVERSATION_CLEARED_NOTICE,
    NO_MEMORY_CONTEXT_NOTICE,
    SEARCH_QUERY_LIMIT,
    SUPPLIED_CONTEXT_MAX_RECORDS,
    ContextFingerprint,
    ContextItem,
    ContextSelection,
    SearchSelection,
    divergent_notices,
    enabled_memories,
    enabled_notes,
    omission_notice,
    search_omission_notice,
    search_selected_notice,
    search_supplied_notice,
    searched_memories_follow,
    searched_notes_whole,
    select_memory_context,
    select_search_context,
    selected_notice,
    supplied_notice,
    supplied_still_current,
    validated_supplied_context,
)
from core.failures import Failure, FailureKind
from core.logger import get_logger
from core.planner import Plan, RecordingPolicy
from core.reasoning import (
    REASONING_HISTORY_MESSAGE,
    ReasoningExchange,
    ReasoningProvider,
    ReasoningRequest,
    ReasoningSettings,
    history_content_bytes,
    request_content_bytes,
    request_reasoning,
)


# The most completed exchanges a session keeps, and the most UTF-8 content
# bytes a conversational request may carry: the current request, the rendered
# memory context, and the retained exchanges' user and assistant text,
# counted as ``request_content_bytes`` counts them. A configured input budget
# narrower than this narrows it further.
CONVERSATION_MAX_EXCHANGES = 8
CONVERSATION_MAX_BYTES = 8 * 1024

# The planner's exact generic no-action response, named here so Brain can
# recognise that one fallback without the planner changing. A test holds this
# to what the planner really returns for unrecognised input.
GENERIC_FALLBACK_RESPONSE = (
    "I do not have an executable plan for that yet. "
    "Type 'help' to see supported commands."
)

# Fixed replies for turns the model does not answer. Neither interpolates
# anything, so neither can carry the request, the reply, or provider text.
CONVERSATION_EXCHANGE_TOO_LARGE_MESSAGE = (
    "The local reasoning model's reply was too long to keep in this "
    "conversation, so it was discarded."
)
CONVERSATION_UNSENDABLE_MESSAGE = (
    "That request cannot be sent to the local reasoning model."
)


@dataclass(frozen=True)
class ConversationTurn:
    """What one conversational request produced.

    ``plan`` is the zero-step transient plan Brain runs. ``conversation`` is
    what the session keeps afterwards and ``supplied_context`` the
    fingerprints of every memory and note supplied since that conversation
    was last cleared. Both are None when the session keeps what it had, and both are
    set together otherwise: an explicitly empty conversation clears the
    session's exchanges and fingerprints alike.
    """

    plan: Plan
    conversation: tuple[ReasoningExchange, ...] | None = None
    supplied_context: frozenset[ContextFingerprint] | None = None

    def __post_init__(self) -> None:
        """Refuse a turn that would change one session structure alone."""
        if (self.conversation is None) != (self.supplied_context is None):
            raise ValueError(
                "A conversation turn must set its conversation and its "
                "supplied context together."
            )


@dataclass(frozen=True)
class _SettledReads:
    """Every bounded context read of one turn, validated.

    ``enabled`` is Tier A's read of enabled memories; ``supplied_*`` are the
    rechecks, by id, of records supplied earlier in the conversation; and
    ``searched_*`` are Tier B's search results. Each is empty when that read
    was not needed.
    """

    enabled: tuple[tuple[int, str | None], ...] = ()
    supplied_memories: tuple[tuple[int, str | None], ...] = ()
    supplied_notes: tuple[tuple[int, str | None, str | None], ...] = ()
    searched_memories: tuple[tuple[int, str | None], ...] = ()
    searched_notes: tuple[tuple[int, str | None, str | None], ...] = ()


def is_generic_fallback(plan: object) -> bool:
    """Return whether a plan is exactly the planner's generic fallback.

    That is a default-recorded plan with no steps whose no-action response is
    exactly ``GENERIC_FALLBACK_RESPONSE``. Help, usage errors, and every
    other no-action response differ in their text, and a plan with a step or
    another recording policy is never the fallback.
    """
    return type(plan) is Plan and plan == Plan(
        plan.original_request,
        (),
        GENERIC_FALLBACK_RESPONSE,
    )


def conversation_budget(settings: ReasoningSettings) -> int:
    """Return the content bytes one conversational request may carry.

    That is ``CONVERSATION_MAX_BYTES`` or the configured input budget,
    whichever is smaller. Disabled settings have no configured budget and
    are never asked anything, so the hard limit alone applies to them.
    """
    if settings.max_input_bytes is None:
        return CONVERSATION_MAX_BYTES
    return min(CONVERSATION_MAX_BYTES, settings.max_input_bytes)


def bounded_conversation(
    exchanges: tuple[ReasoningExchange, ...],
    budget: int = CONVERSATION_MAX_BYTES,
) -> tuple[ReasoningExchange, ...]:
    """Keep the newest whole exchanges within the retention limits.

    At most ``CONVERSATION_MAX_EXCHANGES`` exchanges are kept, and the oldest
    are then dropped, whole, until their text fits within ``budget``.
    Nothing is ever truncated. Anything but an exact tuple of exact
    exchanges raises ``ValueError`` with the history message.
    """
    if type(exchanges) is not tuple:
        raise ValueError(REASONING_HISTORY_MESSAGE)
    kept = exchanges[-CONVERSATION_MAX_EXCHANGES:]
    while kept and history_content_bytes(kept, budget) > budget:
        kept = kept[1:]
    return kept


def fitted_history(
    request: str,
    conversation: tuple[ReasoningExchange, ...],
    budget: int,
    context: tuple[ContextItem, ...] = (),
) -> tuple[ReasoningExchange, ...]:
    """Return the newest whole exchanges that fit beside the request.

    The oldest exchanges are dropped, whole, until the request, the rendered
    ``context``, and what is left fit within ``budget`` together. The request
    and the context must already fit on their own; if they do not, the
    result is empty.
    """
    kept = bounded_conversation(conversation, budget)
    while kept and request_content_bytes(request, kept, budget, context) > budget:
        kept = kept[1:]
    return kept


def transient_plan(request: str, response: str) -> Plan:
    """Return the zero-step plan for one conversational turn.

    The plan is transient by the choice of this producer, so Brain's one
    recording gate writes neither the request nor the response.
    """
    return Plan(
        original_request=request,
        steps=(),
        response_if_no_action=response,
        recording=RecordingPolicy.TRANSIENT,
    )


def with_notices(text: str, notices: Sequence[str]) -> str:
    """Return ``text`` followed by Maria's own notices, one per line.

    A blank line separates them from the text above, so what the model or a
    fixed message said and what Maria reports about context stay visibly
    apart.
    """
    return f"{text}\n\n" + "\n".join(notices)


def converse(
    provider: ReasoningProvider,
    settings: ReasoningSettings,
    request: str,
    conversation: tuple[ReasoningExchange, ...],
    logger: Logger | None = None,
    *,
    supplied_context: frozenset[ContextFingerprint] = frozenset(),
    enabled_memories_read: Sequence[object] | None = (),
    supplied_memories_read: Sequence[object] = (),
    supplied_notes_read: Sequence[object] = (),
    searched_memories_read: Sequence[object] = (),
    searched_notes_read: Sequence[object] = (),
) -> ConversationTurn:
    """Answer one request conversationally, as text only.

    ``enabled_memories_read`` is what Brain's bounded Tier A read returned,
    or None when Brain's context reads failed. The ``supplied_*`` reads are
    the rechecks, by id, of every record ``supplied_context`` names, and the
    ``searched_*`` reads are Tier B's search results. All of them are settled
    first. A failed or untrustworthy read sends no context and no earlier
    exchange, clears the session's conversation and fingerprints, and says
    so. Otherwise, when a record in ``supplied_context`` is no longer among
    the enabled records rechecked with the same text, the whole earlier
    conversation is discarded before anything is sent.

    A request that cannot be sent, or that is over the budget on its own, is
    then refused before the provider is asked anything. Otherwise the
    request comes first, then as much Tier A context as its rule fits, then
    as many Tier B search matches as fit, memories before notes, then the
    newest earlier exchanges that fit, and the provider is asked exactly
    once. When the conversation would then carry more supplied records than
    Maria rechecks, it is discarded before sending instead. A typed failure
    becomes its fixed message. A reply whose exchange, request and reply
    together, would not fit the budget is discarded whole with a fixed
    message rather than shown, kept, or cut down. Only a reply that passes
    all of that is shown, and the session then keeps it as its newest
    exchange, with the fingerprints of the records it was given.

    Every outcome is a zero-step transient plan, and every one ends with
    Maria's notices about context: which records were supplied, or selected
    when delivery cannot be established, or that none was.
    """
    log = logger or get_logger(__name__)
    budget = conversation_budget(settings)
    supplied = validated_supplied_context(supplied_context)
    before: list[str] = []
    cleared = False

    reads: _SettledReads | None = None
    if enabled_memories_read is not None:
        try:
            reads = _settled_reads(
                enabled_memories_read,
                supplied_memories_read,
                supplied_notes_read,
                searched_memories_read,
                searched_notes_read,
            )
        except ValueError:
            reads = None
    if reads is None:
        log.warning("Memory context unavailable; earlier conversation cleared.")
        before.append(CONTEXT_UNREADABLE_NOTICE)
        reads = _SettledReads()
        cleared = True
    elif not supplied_still_current(
        supplied,
        reads.supplied_memories,
        reads.supplied_notes,
    ):
        log.info("Earlier conversation cleared: supplied memory context changed.")
        before.append(CONVERSATION_CLEARED_NOTICE)
        cleared = True
    if cleared:
        conversation = ()
        supplied = frozenset()

    def turn(
        response: str,
        notices: Sequence[str],
        kept: tuple[ReasoningExchange, ...] | None = None,
        kept_context: frozenset[ContextFingerprint] | None = None,
    ) -> ConversationTurn:
        """Build the turn, clearing the session when this turn cleared it."""
        if kept is None and cleared:
            kept, kept_context = (), frozenset()
        return ConversationTurn(
            transient_plan(request, with_notices(response, (*before, *notices))),
            kept,
            kept_context,
        )

    try:
        request_size = request_content_bytes(request, (), budget)
    except ValueError:
        # Text the interface cannot carry, such as a lone surrogate. The
        # refusal is not bound to a name, so nothing about it is kept.
        log.warning("Conversational request refused: unsendable.")
        return turn(CONVERSATION_UNSENDABLE_MESSAGE, (NO_MEMORY_CONTEXT_NOTICE,))
    if request_size > budget:
        log.warning("Conversational request refused: over budget.")
        return turn(
            Failure(FailureKind.REASONING_INPUT_OVER_BUDGET).message,
            (NO_MEMORY_CONTEXT_NOTICE,),
        )

    selection = select_memory_context(reads.enabled, budget - request_size)
    search = select_search_context(
        selection.items,
        reads.searched_memories,
        reads.searched_notes,
        budget - request_size,
    )
    context = selection.items + search.items
    if len(_records(supplied) | _records(context)) > SUPPLIED_CONTEXT_MAX_RECORDS:
        # Counted by record, not by digest, so nothing is hashed before the
        # request is known to complete.
        log.info("Earlier conversation cleared: context recheck limit reached.")
        before.append(CONTEXT_LIMIT_CLEARED_NOTICE)
        cleared = True
        conversation = ()
        supplied = frozenset()
    history = fitted_history(request, conversation, budget, context)
    log.info("Memory context selected: %s item(s).", len(selection.items))
    log.info("Search context selected: %s item(s).", len(search.items))

    outcome = request_reasoning(
        provider,
        ReasoningRequest(request, history, context),
        settings,
        log,
    )
    if type(outcome) is Failure:
        # Delivery cannot be established, so nothing is claimed as supplied
        # and nothing new is tracked: no reply is kept that could repeat it.
        return turn(
            outcome.message,
            _context_notices(selection, search, delivered=False),
        )

    notices = _context_notices(selection, search, delivered=True)
    exchange = ReasoningExchange(request, outcome.text)
    if history_content_bytes((exchange,), budget) > budget:
        log.warning("Conversational reply discarded: exchange over budget.")
        return turn(CONVERSATION_EXCHANGE_TOO_LARGE_MESSAGE, notices)

    kept_context = supplied | frozenset(item.fingerprint for item in context)
    if len(kept_context) > SUPPLIED_CONTEXT_MAX_RECORDS:
        # Only a record changed between its recheck and its selection can
        # get here. The reply is shown but nothing is kept, so the session
        # never holds more fingerprints than it can recheck.
        return turn(outcome.text, notices, (), frozenset())
    return turn(
        outcome.text,
        notices,
        bounded_conversation(history + (exchange,), budget),
        kept_context,
    )


def _settled_reads(
    enabled: object,
    supplied_memories: object,
    supplied_notes: object,
    searched_memories: object,
    searched_notes: object,
) -> _SettledReads:
    """Validate every bounded read of one turn, or raise ``ValueError``.

    Each read must have the shape and size its bound allows, a memory
    search must hold only memories after those Tier A considered, so no
    memory can be supplied twice, and a note search must hold only notes
    whose title and body it returned whole.
    """
    settled = _SettledReads(
        enabled_memories(enabled),
        enabled_memories(supplied_memories, SUPPLIED_CONTEXT_MAX_RECORDS),
        enabled_notes(supplied_notes, SUPPLIED_CONTEXT_MAX_RECORDS),
        enabled_memories(searched_memories, SEARCH_QUERY_LIMIT),
        enabled_notes(searched_notes, SEARCH_QUERY_LIMIT),
    )
    searched_memories_follow(settled.enabled, settled.searched_memories)
    searched_notes_whole(settled.searched_notes)
    return settled


def _records(
    values: frozenset[ContextFingerprint] | tuple[ContextItem, ...],
) -> set[tuple[object, int]]:
    """Return the (source, id) pairs of fingerprints or items."""
    return {(value.source, value.record_id) for value in values}


def _context_notices(
    selection: ContextSelection,
    search: SearchSelection,
    *,
    delivered: bool,
) -> tuple[str, ...]:
    """Return Maria's notices about what one request carried.

    Tier A's notice comes first, then what its limits left out, then Tier
    B's notice, what its limits left out, and any possible divergent
    versions among the selected notes. ``delivered`` says whether the model
    is known to have received the request; when it is not, the records are
    named as selected rather than supplied. With no context at all the one
    notice says none was supplied.
    """
    enabled, searched = selection.items, search.items
    notices: list[str] = []
    if enabled:
        notices.append(
            supplied_notice(enabled) if delivered else selected_notice(enabled)
        )
    elif not searched:
        notices.append(NO_MEMORY_CONTEXT_NOTICE)
    omitted = omission_notice(selection)
    if omitted is not None:
        notices.append(omitted)
    if searched:
        notices.append(
            search_supplied_notice(searched)
            if delivered
            else search_selected_notice(searched)
        )
    omitted = search_omission_notice(search)
    if omitted is not None:
        notices.append(omitted)
    notices.extend(divergent_notices(searched))
    return tuple(notices)
