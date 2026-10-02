"""Owner session state shared by every Maria interface.

A session owns the confirmation lifecycle. It plans a request exactly once,
holds the single plan awaiting approval, and later executes that same plan
rather than the words that produced it. It performs no terminal work, so the
command line and any future interface share one contract.

A session also owns its conversation: the completed conversational exchanges
of this session alone, bounded in count and size, held in process memory, and
discarded when the session ends. Beside it the session keeps the fingerprints
of every memory and note supplied to the model since that conversation was
last cleared, at most ``SUPPLIED_CONTEXT_MAX_RECORDS`` of them and never the
records' text, so a later turn can discard the whole conversation before an
earlier reply carries a revoked record back. Brain keeps nothing between
requests, so a new session always starts with no conversation and can never
observe another's.

A plan the local model proposed is rendered here, by the session that binds
its approval: the declared tool and action and every argument exactly, quoted
by ``approval_quoted``, from the very plan whose fingerprint the approval is
then bound to, and before that binding exists. Brain supplies only the
turn's context notices, which are shown once below the rendering and are
never kept with the approval or shown again when it is confirmed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from logging import Logger

from core.brain import BrainResponse, MariaBrain
from core.context import SUPPLIED_CONTEXT_MAX_RECORDS, ContextFingerprint
from core.conversation import bounded_conversation
from core.logger import get_logger
from core.planner import Plan, PlanOrigin, is_model_plan
from core.reasoning import ReasoningExchange


CONFIRMATION_REFUSED_MESSAGE = (
    "No pending action matches that confirmation. Please make the request again."
)

# The first line of every model-originated plan shown for approval.
MODEL_APPROVAL_HEADER = (
    "Maria's local reasoning model proposed this action. It runs only if you "
    "approve it."
)
MODEL_APPROVAL_MESSAGE = (
    "Only a well-formed model-originated plan can be shown for approval."
)

# The two characters the approval quoter writes as a backslash pair rather
# than a four-digit escape. Every other character it escapes is written as a
# standard four-digit escape, so the quoted text decodes as one JSON string.
_SHORT_ESCAPES = {'"': '\\"', "\\": "\\\\"}

# The longest tool or action name the approval shows. Every declared name is
# far shorter.
_MAXIMUM_NAME_LENGTH = 64


@dataclass(frozen=True, repr=False)
class PendingApproval:
    """The one plan a session holds while it waits for the owner's answer.

    It carries the immutable plan and the fingerprint captured when that plan
    was presented, which together are the whole proof of approval. Nothing
    else is kept: not the text the owner was shown, and not the notices
    shown with it. The object stays in process memory: it is never written
    to SQLite, a file, or configuration, never serialized, and never logged,
    because the plan it wraps can contain note bodies, memory facts, and file
    paths. Its ``repr`` therefore shows none of the request, the arguments,
    what was shown, or the fingerprint.
    """

    plan: Plan
    fingerprint: str

    def __repr__(self) -> str:
        """Name the type without showing the plan or its fingerprint."""
        return "PendingApproval(<redacted>)"


class MariaSession:
    """Bind an owner's approval to one exact plan, for any interface."""

    def __init__(self, brain: MariaBrain, logger: Logger | None = None) -> None:
        self.brain = brain
        self.logger = logger or get_logger(__name__)
        self._pending: PendingApproval | None = None
        self._conversation: tuple[ReasoningExchange, ...] = ()
        self._supplied_context: frozenset[ContextFingerprint] = frozenset()

    @property
    def pending_approval(self) -> PendingApproval | None:
        """Return the plan awaiting approval, or None when none is pending."""
        return self._pending

    @property
    def conversation(self) -> tuple[ReasoningExchange, ...]:
        """Return this session's completed conversational exchanges."""
        return self._conversation

    @property
    def supplied_context(self) -> frozenset[ContextFingerprint]:
        """Return the fingerprints of records supplied to this conversation."""
        return self._supplied_context

    def submit(self, user_input: str) -> BrainResponse:
        """Plan one request, discarding any approval still pending.

        A new request always invalidates the previous pending plan, so an
        approval can never be transferred to, or replayed against, a plan the
        owner was not shown.

        The session's conversation goes with the request, and its supplied
        record fingerprints go with it whenever there are any; a conversation
        that never carried context has nothing to verify, so the call is then
        exactly what it always was. Only a conversational turn replaces them,
        and it replaces both together: a completed reply with the
        conversation after it, and a turn that had to clear the conversation
        with nothing. A turn that ended in a proposal changes them only when
        it cleared them. Whatever comes back is held to the retention limits
        again here, so the session never keeps more than they allow, whoever
        computed it: fingerprints beyond what Maria rechecks clear both.

        A model-originated plan awaiting approval is rendered here, by
        ``render_model_approval``, from the very plan about to be bound and
        before it is bound, with Brain's notices below the rendering. A plan
        that cannot be rendered raises ``ValueError`` and is never pending.
        """
        self._pending = None
        if self._supplied_context:
            response = self.brain.process(
                user_input,
                *self._conversation,
                supplied_context=self._supplied_context,
            )
        else:
            response = self.brain.process(user_input, *self._conversation)

        if response.conversation is not None:
            self._conversation = bounded_conversation(response.conversation)
            # Fingerprints only ever describe a conversation that exists, so
            # an emptied conversation takes them with it.
            self._supplied_context = (
                frozenset(response.supplied_context)
                if self._conversation
                else frozenset()
            )
            if len(self._supplied_context) > SUPPLIED_CONTEXT_MAX_RECORDS:
                self._conversation = ()
                self._supplied_context = frozenset()

        if not response.needs_confirmation:
            return response

        if response.plan is None:
            raise ValueError(
                "A confirmation-required response must carry the plan it describes."
            )

        if response.plan.origin is not PlanOrigin.DETERMINISTIC:
            # Rendered from the very plan the approval is about to bind, and
            # before it binds, so what the owner is shown is exactly what
            # they approve. A plan that cannot be rendered is never pending,
            # and any origin but exactly DETERMINISTIC must render as a
            # well-formed model plan or fail here.
            response = replace(
                response,
                text=_with_model_approval(response.plan, response.text),
            )

        self._pending = PendingApproval(response.plan, response.plan.fingerprint)
        return response

    def confirm(self) -> BrainResponse:
        """Execute the pending plan exactly as it was presented for approval.

        The approval is consumed before anything else, so neither a failing
        tool nor a raised exception can leave it replayable. When nothing is
        pending, or ``_binds_its_plan`` finds the pending plan is no longer
        exactly the one approved, the confirmation is refused with a fixed
        content-free message and runs nothing.
        """
        pending = self._pending
        self._pending = None

        if pending is None or not _binds_its_plan(pending):
            self.logger.warning("Refused a confirmation that binds no pending plan.")
            return BrainResponse(CONFIRMATION_REFUSED_MESSAGE)

        return self.brain.execute_confirmed_plan(pending.plan)

    def cancel(self) -> bool:
        """Discard the pending plan, planning and executing nothing.

        Returns whether an approval was actually pending, so a second
        cancellation is visibly a no-op rather than silently accepted.
        """
        had_pending = self._pending is not None
        self._pending = None

        if had_pending:
            self.logger.info("Pending approval cancelled.")

        return had_pending

    def end(self) -> None:
        """End the session, discarding its conversation and pending approval.

        The supplied-context fingerprints go with the conversation. Nothing
        the session held survives it or is written anywhere. Ending an
        already-ended session changes nothing.
        """
        self._conversation = ()
        self._supplied_context = frozenset()
        self._pending = None
        self.logger.info("Session ended.")


def _binds_its_plan(pending: PendingApproval) -> bool:
    """Return whether a pending approval still binds exactly its plan.

    The plan must still be an exact ``Plan`` whose origin is exactly
    DETERMINISTIC, or still exactly a well-formed model-originated plan, and
    only then is it fingerprinted. The fingerprint reads the origin and every
    step, so checking them first means a plan altered after it became
    pending is refused rather than failing inside the fingerprint. A plan
    that still cannot be fingerprinted is refused as well, with its failure
    neither raised, logged, nor shown, and so is one whose fingerprint
    differs from the value captured when it became pending.
    """
    plan = pending.plan
    if not (
        (type(plan) is Plan and plan.origin is PlanOrigin.DETERMINISTIC)
        or is_model_plan(plan)
    ):
        return False
    try:
        fingerprint = plan.fingerprint
    except Exception:  # noqa: BLE001 - an altered plan is refused, not raised.
        return False
    return fingerprint == pending.fingerprint


def render_model_approval(plan: Plan) -> str:
    """Render a model-originated plan exactly, for the owner's approval.

    The text names the step's declared tool and action, then every argument
    as one JSON object with its names in sorted order: ``{}`` when there are
    none, and otherwise each name and each text value quoted by
    ``approval_quoted`` and each whole number written out in full. Nothing
    is truncated, omitted, or normalised, and the plan itself is not
    changed. Anything but a well-formed model-originated plan named by plain
    identifiers raises ``ValueError`` with a fixed message.
    """
    if not is_model_plan(plan):
        raise ValueError(MODEL_APPROVAL_MESSAGE)
    step = plan.steps[0]
    if not (_is_plain_name(step.tool_name) and _is_plain_name(step.action)):
        raise ValueError(MODEL_APPROVAL_MESSAGE)
    return "\n".join(
        (
            MODEL_APPROVAL_HEADER,
            f"Tool: {step.tool_name}",
            f"Action: {step.action}",
            f"Arguments: {approval_arguments(step.arguments)}",
        )
    )


def approval_arguments(arguments: Mapping[str, str | int]) -> str:
    """Render plan arguments exactly, as one JSON object on one line.

    Decoding the result as JSON gives back exactly the arguments rendered.
    A value that is not exactly text or a whole number raises
    ``ValueError`` with a fixed message.
    """
    entries = [
        f"{approval_quoted(name)}: {_approval_value(arguments[name])}"
        for name in sorted(arguments)
    ]
    return "{" + ", ".join(entries) + "}"


def approval_quoted(text: str) -> str:
    """Quote text so the owner sees exactly what an approval covers.

    The text is written between quotation marks with every printable ASCII
    character as itself, except that the quotation mark and the backslash
    are each preceded by a backslash. Every other character is written as a
    four-digit ``\\uXXXX`` escape: each control character, the delete
    character, each other character outside ASCII, a character beyond the
    Basic Multilingual Plane as its surrogate pair, and every space that
    leads, trails, or stands in a run of two or more. A single space between
    other characters stays a space. The result is printable ASCII, so
    nothing in it can hide, reorder, or start a new line, and it is one JSON
    string literal, so decoding it gives back exactly the original text:
    nothing is dropped, folded, or truncated. Text holding a surrogate code
    point is refused rather than quoted, because a high and a low surrogate
    side by side would decode as one different character; no model plan can
    carry one, since the compiler refuses that category. Anything but exact
    text, and text holding a surrogate, raises ``ValueError`` with a fixed
    message.
    """
    if type(text) is not str or any(
        0xD800 <= ord(character) <= 0xDFFF for character in text
    ):
        raise ValueError(MODEL_APPROVAL_MESSAGE)
    last = len(text) - 1
    pieces: list[str] = []
    for index, character in enumerate(text):
        if character == " ":
            shown = (
                0 < index < last
                and text[index - 1] != " "
                and text[index + 1] != " "
            )
            pieces.append(" " if shown else _escape(0x20))
        elif character in _SHORT_ESCAPES:
            pieces.append(_SHORT_ESCAPES[character])
        elif " " < character < chr(0x7F):
            pieces.append(character)
        elif ord(character) > 0xFFFF:
            offset = ord(character) - 0x10000
            pieces.append(_escape(0xD800 + (offset >> 10)))
            pieces.append(_escape(0xDC00 + (offset & 0x3FF)))
        else:
            pieces.append(_escape(ord(character)))
    return '"' + "".join(pieces) + '"'


def _escape(code: int) -> str:
    """Return the four-digit escape of one UTF-16 code unit."""
    return "\\u" + format(code, "04x")


def _approval_value(value: object) -> str:
    """Render one argument value exactly, or refuse a value of another type."""
    if type(value) is str:
        return approval_quoted(value)
    if type(value) is int:
        return str(value)
    raise ValueError(MODEL_APPROVAL_MESSAGE)


def _is_plain_name(value: object) -> bool:
    """Return whether a value is a short, plain ASCII identifier."""
    return (
        type(value) is str
        and 0 < len(value) <= _MAXIMUM_NAME_LENGTH
        and value.isascii()
        and value.isidentifier()
    )


def _with_model_approval(plan: Plan, notices: str) -> str:
    """Return a model plan's rendering, with Brain's notices below it."""
    rendered = render_model_approval(plan)
    if not notices:
        return rendered
    return f"{rendered}\n\n{notices}"
