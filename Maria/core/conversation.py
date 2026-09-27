"""Deterministic-first conversation with Maria's local reasoning model.

The deterministic planner always answers first. Only a request it answers
with its generic no-action fallback may instead be answered by the owner's
local model, and this module settles that one conversational turn: it settles
the owner's enabled memory context, fits that context and the current
session's earlier exchanges beside the request within a hard budget, asks the
provider once through ``request_reasoning``, and produces the plan Brain runs.
That plan always has zero steps and is always transient, whether it carries
the model's reply or a fixed failure message, so no conversational request or
reply is ever recorded and no tool can be reached through it.

The reply is inert text. Nothing here parses it, and it can only ever become
the no-action response of a zero-step plan or the assistant half of an
exchange. What the owner is told about memory context is Maria's own text,
built from the ids she selected and appended after the reply; it never enters
the model's request or the exchange the session keeps, and nothing a model
writes can change it. There is no import path from here to permissions, tool
dispatch, or persistence.

The conversation itself belongs to the caller's session, together with the
fingerprints of every memory supplied since that conversation was last
cleared. This module keeps no state between turns; it only computes what a
turn sends and what the session should keep afterwards. Before anything is
sent, those fingerprints are checked against the memories enabled now, and a
conversation whose supplied memory was disabled, changed, or forgotten is
discarded whole, so an earlier reply cannot carry that text back to the
model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from logging import Logger

from core.context import (
    CONTEXT_UNREADABLE_NOTICE,
    CONVERSATION_CLEARED_NOTICE,
    NO_MEMORY_CONTEXT_NOTICE,
    ContextFingerprint,
    ContextItem,
    current_fingerprints,
    enabled_memories,
    omission_notice,
    select_memory_context,
    selected_notice,
    supplied_notice,
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
    fingerprints of every memory supplied since that conversation was last
    cleared. Both are None when the session keeps what it had, and both are
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
) -> ConversationTurn:
    """Answer one request conversationally, as text only.

    ``enabled_memories_read`` is what Brain's bounded read of enabled
    memories returned, or None when that read failed. It is settled first. A
    failed or untrustworthy read sends no memory context and no earlier
    exchange, clears the session's conversation and fingerprints, and says
    so. Otherwise, when a memory in ``supplied_context`` is no longer among
    the enabled memories with the same text, the whole earlier conversation
    is discarded before anything is sent.

    A request that cannot be sent, or that is over the budget on its own, is
    then refused before the provider is asked anything. Otherwise the
    request comes first, then as much memory context as the selection rule
    fits, then the newest earlier exchanges that fit, and the provider is
    asked exactly once. A typed failure becomes its fixed message. A reply
    whose exchange, request and reply together, would not fit the budget is
    discarded whole with a fixed message rather than shown, kept, or cut
    down. Only a reply that passes all of that is shown, and the session
    then keeps it as its newest exchange, with the fingerprints of the
    memories it was given.

    Every outcome is a zero-step transient plan, and every one ends with
    Maria's notice about memory context: which memories were supplied, or
    selected when delivery cannot be established, or that none was.
    """
    log = logger or get_logger(__name__)
    budget = conversation_budget(settings)
    supplied = validated_supplied_context(supplied_context)
    before: list[str] = []
    cleared = False

    memories: tuple[tuple[int, str], ...] | None = None
    if enabled_memories_read is not None:
        try:
            memories = enabled_memories(enabled_memories_read)
        except ValueError:
            memories = None
    if memories is None:
        log.warning("Memory context unavailable; earlier conversation cleared.")
        before.append(CONTEXT_UNREADABLE_NOTICE)
        memories = ()
        cleared = True
    elif not supplied <= current_fingerprints(memories):
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

    selection = select_memory_context(memories, budget - request_size)
    context = selection.items
    omitted = omission_notice(selection)
    after = () if omitted is None else (omitted,)
    history = fitted_history(request, conversation, budget, context)
    log.info("Memory context selected: %s item(s).", len(context))

    outcome = request_reasoning(
        provider,
        ReasoningRequest(request, history, context),
        settings,
        log,
    )
    if type(outcome) is Failure:
        # Delivery cannot be established, so nothing is claimed as supplied
        # and nothing new is tracked: no reply is kept that could repeat it.
        notice = selected_notice(context) if context else NO_MEMORY_CONTEXT_NOTICE
        return turn(outcome.message, (notice, *after))

    notice = supplied_notice(context) if context else NO_MEMORY_CONTEXT_NOTICE
    exchange = ReasoningExchange(request, outcome.text)
    if history_content_bytes((exchange,), budget) > budget:
        log.warning("Conversational reply discarded: exchange over budget.")
        return turn(CONVERSATION_EXCHANGE_TOO_LARGE_MESSAGE, (notice, *after))

    return turn(
        outcome.text,
        (notice, *after),
        bounded_conversation(history + (exchange,), budget),
        supplied | frozenset(item.fingerprint for item in context),
    )
