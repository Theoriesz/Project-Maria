"""Deterministic-first conversation with Maria's local reasoning model.

The deterministic planner always answers first. Only a request it answers
with its generic no-action fallback may instead be answered by the owner's
local model, and this module settles that one conversational turn: it fits the
current session's earlier exchanges beside the request within a hard budget,
asks the provider once through ``request_reasoning``, and produces the plan
Brain runs. That plan always has zero steps and is always transient, whether it
carries the model's reply or a fixed failure message, so no conversational
request or reply is ever recorded and no tool can be reached through it.

The reply is inert text. Nothing here parses it, and it can only ever become
the no-action response of a zero-step plan or the assistant half of an
exchange. There is no import path from here to permissions, tool dispatch, or
persistence.

The conversation itself belongs to the caller's session. This module keeps no
state between turns; it only computes what a turn sends and what the session
should keep afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from logging import Logger

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
# bytes a conversational request may carry: the retained exchanges' user and
# assistant text plus the current request, counted as
# ``request_content_bytes`` counts them. A configured input budget narrower
# than this narrows it further.
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
    the session's conversation after a completed exchange, or None when the
    turn completed nothing and the session keeps what it had.
    """

    plan: Plan
    conversation: tuple[ReasoningExchange, ...] | None = None


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
) -> tuple[ReasoningExchange, ...]:
    """Return the newest whole exchanges that fit beside the request.

    The oldest exchanges are dropped, whole, until the request and what is
    left fit within ``budget`` together. The request must already fit on its
    own; if it does not, the result is empty.
    """
    kept = bounded_conversation(conversation, budget)
    while kept and request_content_bytes(request, kept, budget) > budget:
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


def converse(
    provider: ReasoningProvider,
    settings: ReasoningSettings,
    request: str,
    conversation: tuple[ReasoningExchange, ...],
    logger: Logger | None = None,
) -> ConversationTurn:
    """Answer one request conversationally, as text only.

    A request that cannot be sent, or that is over the budget on its own, is
    refused before the provider is asked anything. Otherwise the provider is
    asked exactly once, with the newest earlier exchanges that fit. A typed
    failure becomes its fixed message. A reply whose exchange, request and
    reply together, would not fit the budget is discarded whole with a fixed
    message rather than shown, kept, or cut down. Only a reply that passes all
    of that is shown, and the session then keeps it as its newest exchange.

    Every outcome is a zero-step transient plan, and only a completed
    exchange changes the conversation.
    """
    log = logger or get_logger(__name__)
    budget = conversation_budget(settings)

    try:
        request_size = request_content_bytes(request, (), budget)
    except ValueError:
        # Text the interface cannot carry, such as a lone surrogate. The
        # refusal is not bound to a name, so nothing about it is kept.
        log.warning("Conversational request refused: unsendable.")
        return ConversationTurn(
            transient_plan(request, CONVERSATION_UNSENDABLE_MESSAGE)
        )
    if request_size > budget:
        log.warning("Conversational request refused: over budget.")
        return ConversationTurn(
            transient_plan(
                request,
                Failure(FailureKind.REASONING_INPUT_OVER_BUDGET).message,
            )
        )

    history = fitted_history(request, conversation, budget)
    outcome = request_reasoning(
        provider,
        ReasoningRequest(request, history),
        settings,
        log,
    )
    if type(outcome) is Failure:
        return ConversationTurn(transient_plan(request, outcome.message))

    exchange = ReasoningExchange(request, outcome.text)
    if history_content_bytes((exchange,), budget) > budget:
        log.warning("Conversational reply discarded: exchange over budget.")
        return ConversationTurn(
            transient_plan(request, CONVERSATION_EXCHANGE_TOO_LARGE_MESSAGE)
        )

    return ConversationTurn(
        transient_plan(request, outcome.text),
        bounded_conversation(history + (exchange,), budget),
    )
