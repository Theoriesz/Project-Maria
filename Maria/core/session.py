"""Owner session state shared by every Maria interface.

A session owns the confirmation lifecycle. It plans a request exactly once,
holds the single plan awaiting approval, and later executes that same plan
rather than the words that produced it. It performs no terminal work, so the
command line and any future interface share one contract.

A session also owns its conversation: the completed conversational exchanges
of this session alone, bounded in count and size, held in process memory, and
discarded when the session ends. Beside it the session keeps the fingerprints
of every memory, note, conversation summary, task, and reminder supplied to
the model since that conversation was last cleared, at most
``SUPPLIED_CONTEXT_MAX_RECORDS`` of them and never the records' text, so a
later turn can discard the whole conversation before an earlier reply carries
a revoked record back. Brain keeps nothing between requests, so a new session
always starts with no conversation and can never observe another's.

The conversation is handed to Brain with every request, exactly as held.
Only an explicit save or summary request makes anything of it durable, and
only through Brain and the ordinary tool path: an explicit save stores
exactly the exchanges held at that moment, and a summary is stored only once
the owner confirms the exact draft shown. Either request is still an
ordinary submit, so it discards any approval still pending. Once the session
ends there is nothing left to save.

A plan the local model proposed is rendered here, by the session that binds
its approval: for every step, in order, the declared tool and action and
every argument exactly, quoted by ``approval_quoted``, from the very plan
whose fingerprint the approval is then bound to, and before that binding
exists. A plan of several steps is approved once, as a whole. Brain supplies
the fixed approval notice of each step awaiting confirmation, once each, and
then the turn's context notices, all shown once below the rendering and never
kept with the approval or shown again when it is confirmed.

What becomes pending is one bounded snapshot of the plan, and confirming
reads the pending plan once more, as a snapshot, compares that snapshot's
fingerprint with the approval, and hands that very snapshot to Brain only if
it still holds exactly the field objects that were fingerprinted. No steps
container is read again after the comparison: Brain re-checks the very
snapshot, whose steps are a tuple.

A session may also hold a reminder-delivery seam (V1-23-R1), which its
interface uses to show due reminders: ``claim_due_reminders`` claims them
through the delivery subsystem and returns one ``ReminderNotice``, and
``record_reminder_notice`` records whether the interface displayed it. Both
contain every ``Exception``, logging only its type; neither calls Brain, a
plan, a tool, Tool Manager, permissions, or a provider, and neither touches
a pending approval, the conversation, or the supplied-context fingerprints.
A notice is never recorded as an interaction, kept in the conversation, or
sent to a model.

A session may also hold a projects seam (V1-24-R1), which the desktop
window's project selector reads through ``project_choices``: at most fifty
projects by id and name, and the active project's id while one is active.
It changes nothing, contains every ``Exception``, logging only its type, and
like the delivery seam never calls Brain, a plan, a tool, Tool Manager,
permissions, or a provider, and never touches a pending approval, the
conversation, or the supplied-context fingerprints. Activating or
deactivating a project is an ordinary submitted command.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from logging import Logger

from core.brain import BrainResponse, MariaBrain
from core.context import SUPPLIED_CONTEXT_MAX_RECORDS, ContextFingerprint
from core.conversation import bounded_conversation
from core.logger import get_logger
from core.memory import Memory
from core.planner import (
    PLAN_STEP_LIMIT_MESSAGE,
    Plan,
    PlanOrigin,
    PlanStep,
    is_model_plan,
    own_origin,
    plan_snapshot,
)
# ``ProjectChoices`` is imported here also so that the desktop window's
# worker and controller, which may import no core module but this one,
# ``core.constants``, and ``core.logger``, can name the choices type.
from core.project_records import ProjectChoices
from core.reasoning import ReasoningExchange
# ``ReminderNotice`` is imported here also so that the desktop window's
# worker, which may import no core module but this one, ``core.constants``,
# and ``core.logger``, can name the notice type.
from core.reminder_delivery import ReminderDeliverySeam, ReminderNotice


CONFIRMATION_REFUSED_MESSAGE = (
    "No pending action matches that confirmation. Please make the request again."
)

# The first line of every model-originated plan of one step shown for
# approval.
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
    because the plan it wraps can contain note bodies, memory facts, file
    paths, a saved conversation's transcript, or a summary draft. Its
    ``repr`` therefore shows none of the request, the arguments, what was
    shown, or the fingerprint.
    """

    plan: Plan
    fingerprint: str

    def __repr__(self) -> str:
        """Name the type without showing the plan or its fingerprint."""
        return "PendingApproval(<redacted>)"


class ProjectChoicesSeam:
    """The one read a window's project selector makes (V1-24-R1).

    It asks the memory layer for the selector's projects and the active id,
    and for nothing else: it changes nothing, and it never calls Brain, a
    plan, a tool, Tool Manager, permissions, or a provider.
    """

    def __init__(self, memory: Memory) -> None:
        self._memory = memory

    def __repr__(self) -> str:
        """Name the type and nothing it holds."""
        return "ProjectChoicesSeam()"

    def choices(self) -> ProjectChoices:
        """Return at most fifty projects by id and name, and the active id."""
        return self._memory.project_choices()


class MariaSession:
    """Bind an owner's approval to one exact plan, for any interface."""

    def __init__(
        self,
        brain: MariaBrain,
        logger: Logger | None = None,
        *,
        delivery: ReminderDeliverySeam | None = None,
        projects: ProjectChoicesSeam | None = None,
    ) -> None:
        self.brain = brain
        self.logger = logger or get_logger(__name__)
        self._pending: PendingApproval | None = None
        self._conversation: tuple[ReasoningExchange, ...] = ()
        self._supplied_context: frozenset[ContextFingerprint] = frozenset()
        self._delivery = delivery
        self._projects = projects

    @property
    def pending_approval(self) -> PendingApproval | None:
        """Return the plan awaiting approval, or None when none is pending."""
        return self._pending

    @property
    def delivers_reminders(self) -> bool:
        """Return whether this session was given a reminder-delivery seam."""
        return self._delivery is not None

    def claim_due_reminders(self) -> ReminderNotice | None:
        """Claim the due reminders this check may show, as one notice.

        With no seam, or with delivery off, this returns None having claimed
        nothing; with delivery off it reads only the setting. Otherwise it
        returns the notice holding exactly the claims this call retained, or
        None. An ``Exception`` from the delivery subsystem is logged by its
        type alone and returns None; a failure that is not an
        ``Exception`` propagates. Brain, the conversation, the
        supplied-context fingerprints, and any pending approval are never
        touched.
        """
        if self._delivery is None:
            return None
        try:
            return self._delivery.claim().notice
        except Exception as error:  # noqa: BLE001 - the seam's boundary.
            self.logger.error(
                "The reminder check failed: %s.",
                type(error).__name__,
            )
            return None

    def record_reminder_notice(
        self,
        notice: ReminderNotice,
        displayed: bool,
    ) -> None:
        """Record whether this session's interface displayed one notice.

        ``displayed`` True records every claim of the notice as displayed,
        and False as not displayed. An ``Exception`` is logged by its type
        alone; a failure that is not an ``Exception`` propagates. Nothing
        else the session holds changes.
        """
        if self._delivery is None:
            return
        try:
            self._delivery.record(notice, displayed)
        except Exception as error:  # noqa: BLE001 - the seam's boundary.
            self.logger.error(
                "Recording a reminder notice failed: %s.",
                type(error).__name__,
            )

    def project_choices(self) -> ProjectChoices | None:
        """Return the projects a window's selector lists, or None.

        That is at most fifty projects by id and name, in ascending id
        order, and the active project's id, given only while a project is
        active. With no seam, or when the read fails, this returns None and
        the selector keeps what it shows. An ``Exception`` is logged by its
        type alone; a failure that is not an ``Exception`` propagates. Brain,
        the conversation, the supplied-context fingerprints, and any pending
        approval are never touched.
        """
        if self._projects is None:
            return None
        try:
            choices = self._projects.choices()
        except Exception as error:  # noqa: BLE001 - the seam's boundary.
            self.logger.error(
                "Reading the project choices failed: %s.",
                type(error).__name__,
            )
            return None
        if type(choices) is not ProjectChoices:
            self.logger.error("Reading the project choices failed: unexpected.")
            return None
        return choices

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
        it cleared them, and so does a summary request, whose recheck of the
        supplied records may clear them but whose draft never joins the
        conversation. Whatever comes back is held to the retention limits
        again here, so the session never keeps more than they allow, whoever
        computed it: fingerprints beyond what Maria rechecks clear both.

        A model-originated plan awaiting approval is rendered here, by
        ``render_model_approval``, from the very plan about to be bound and
        before it is bound, with Brain's notices below the rendering. A plan
        that cannot be rendered raises ``ValueError`` and is never pending.
        What becomes pending is one bounded snapshot of the prepared plan, as
        ``plan_snapshot`` takes it: a well-formed model plan is its own, and
        a deterministic plan is snapshotted here unless it already is one, so
        what is rendered and fingerprinted is exactly what confirmation will
        compare and run. A deterministic plan that cannot be snapshotted
        raises ``ValueError`` and is never pending either.
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

        plan = response.plan
        if own_origin(plan) is not PlanOrigin.DETERMINISTIC:
            # Rendered from the very plan the approval is about to bind, and
            # before it binds, so what the owner is shown is exactly what
            # they approve. A plan that cannot be rendered is never pending,
            # and any plan but a whole one holding exactly DETERMINISTIC must
            # render as a well-formed model plan, its own snapshot, or fail
            # here.
            response = replace(
                response,
                text=_with_model_approval(plan, response.text),
            )
        else:
            snapshot = plan_snapshot(plan)
            if snapshot is None:
                raise ValueError(PLAN_STEP_LIMIT_MESSAGE)
            if snapshot is not plan:
                plan = snapshot
                response = replace(response, plan=snapshot)

        self._pending = PendingApproval(plan, plan.fingerprint)
        return response

    def confirm(self) -> BrainResponse:
        """Execute the pending plan exactly as it was presented for approval.

        The approval is consumed before anything else, so neither a failing
        tool nor a raised exception can leave it replayable. The pending plan
        is then read once, as one bounded snapshot, by ``_approved_snapshot``,
        which compares that snapshot's fingerprint with the one captured when
        the plan was offered; that very snapshot is what Brain receives and
        re-checks, so no steps container is read again after the comparison.
        When nothing is pending, or no snapshot of the pending plan still
        matches the approval, the confirmation is refused with a fixed
        content-free message and runs nothing.
        """
        pending = self._pending
        self._pending = None

        approved = None if pending is None else _approved_snapshot(pending)
        if approved is None:
            self.logger.warning("Refused a confirmation that binds no pending plan.")
            return BrainResponse(CONFIRMATION_REFUSED_MESSAGE)

        return self.brain.execute_confirmed_plan(approved)

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


def _approved_snapshot(pending: PendingApproval) -> Plan | None:
    """Return the snapshot of a pending plan its approval still binds, or None.

    The pending plan must still be an exact ``Plan``, and ``plan_snapshot``
    must take it: whole, holding exactly its own origin, within the step
    bound, and exactly a well-formed model plan if it is not exactly
    deterministic. Only that snapshot is then fingerprinted, so the
    fingerprint compared and the steps Brain runs come from the same single
    read. A deterministic plan's request and steps are not type-checked, so
    fingerprinting can run code they carry; the snapshot is handed on only
    if it still holds exactly the field objects that were fingerprinted, so
    nothing that code does can swap what Brain receives. Checking first
    means a plan altered after it became pending is refused rather than
    failing inside the fingerprint, and an approval missing its plan or its
    fingerprint is refused too. A snapshot that still cannot be
    fingerprinted is refused as well, with its failure neither raised,
    logged, nor shown, and so is one whose fingerprint differs from the
    value captured when the plan became pending.
    """
    try:
        plan = pending.plan
        approved = pending.fingerprint
    except AttributeError:
        return None
    if type(plan) is not Plan:
        return None
    snapshot = plan_snapshot(plan)
    if snapshot is None:
        return None
    fingerprinted = dict(vars(snapshot))
    try:
        fingerprint = snapshot.fingerprint
    except Exception:  # noqa: BLE001 - an altered plan is refused, not raised.
        return None
    if fingerprint != approved or not _still_holds(snapshot, fingerprinted):
        return None
    return snapshot


def _still_holds(plan: Plan, fields: dict[str, object]) -> bool:
    """Return whether a plan still holds exactly these field objects."""
    held = vars(plan)
    return (
        type(held) is dict
        and held.keys() == fields.keys()
        and all(held[name] is value for name, value in fields.items())
    )


def model_plan_approval_header(count: int) -> str:
    """Return the first line of a model-originated plan of several steps."""
    return (
        f"Maria's local reasoning model proposed this plan of {count} "
        "actions. They run in order, and only if you approve the whole plan. "
        "Maria stops if a step fails, an audit write fails, or the run is "
        "interrupted."
    )


def render_model_approval(plan: Plan) -> str:
    """Render a model-originated plan exactly, for the owner's approval.

    The text names each step's declared tool and action, then every
    argument as one JSON object with its names in sorted order: ``{}`` when
    there are none, and otherwise each name and each text value quoted by
    ``approval_quoted`` and each whole number written out in full. A plan of
    one step is those three lines below ``MODEL_APPROVAL_HEADER``; a plan of
    several has its own header and numbers every step, in plan order.
    Nothing is truncated, omitted, or normalised, and the plan itself is not
    changed. Anything but a well-formed model-originated plan whose every
    step is named by plain identifiers raises ``ValueError`` with a fixed
    message, before any of it is rendered.
    """
    if not is_model_plan(plan):
        raise ValueError(MODEL_APPROVAL_MESSAGE)
    steps = plan.steps
    if not all(
        _is_plain_name(step.tool_name) and _is_plain_name(step.action)
        for step in steps
    ):
        raise ValueError(MODEL_APPROVAL_MESSAGE)
    if len(steps) == 1:
        return "\n".join(
            (MODEL_APPROVAL_HEADER, *_approval_step_lines(steps[0]))
        )
    lines = [model_plan_approval_header(len(steps))]
    for position, step in enumerate(steps, start=1):
        lines.append(f"Step {position} of {len(steps)}")
        lines.extend(_approval_step_lines(step))
    return "\n".join(lines)


def _approval_step_lines(step: PlanStep) -> tuple[str, str, str]:
    """Return one step's tool, action, and exact arguments, one per line."""
    return (
        f"Tool: {step.tool_name}",
        f"Action: {step.action}",
        f"Arguments: {approval_arguments(step.arguments)}",
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
