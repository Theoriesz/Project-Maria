"""Owner session state shared by every Maria interface.

A session owns the confirmation lifecycle. It plans a request exactly once,
holds the single plan awaiting approval, and later executes that same plan
rather than the words that produced it. It performs no terminal work, so the
command line and any future interface share one contract.

A session also owns its conversation: the completed conversational exchanges
of this session alone, bounded in count and size, held in process memory, and
discarded when the session ends. Beside it the session keeps the fingerprints
of every memory supplied to the model since that conversation was last
cleared, never the memories' text, so a later turn can discard the whole
conversation before an earlier reply carries a revoked memory back. Brain
keeps nothing between requests, so a new session always starts with no
conversation and can never observe another's.
"""

from __future__ import annotations

from dataclasses import dataclass
from logging import Logger

from core.brain import BrainResponse, MariaBrain
from core.context import ContextFingerprint
from core.conversation import bounded_conversation
from core.logger import get_logger
from core.planner import Plan
from core.reasoning import ReasoningExchange


CONFIRMATION_REFUSED_MESSAGE = (
    "No pending action matches that confirmation. Please make the request again."
)


@dataclass(frozen=True)
class PendingApproval:
    """The one plan a session holds while it waits for the owner's answer.

    It carries the immutable plan and the fingerprint captured when that plan
    was presented, which together are the whole proof of approval. Nothing
    else is kept. The object stays in process memory: it is never written to
    SQLite, a file, or configuration, never serialized, and never logged,
    because the plan it wraps can contain note bodies, memory facts, and file
    paths.
    """

    plan: Plan
    fingerprint: str


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
        """Return the fingerprints of memories supplied to this conversation."""
        return self._supplied_context

    def submit(self, user_input: str) -> BrainResponse:
        """Plan one request, discarding any approval still pending.

        A new request always invalidates the previous pending plan, so an
        approval can never be transferred to, or replayed against, a plan the
        owner was not shown.

        The session's conversation goes with the request, and its supplied
        memory fingerprints go with it whenever there are any; a conversation
        that never carried memory context has nothing to verify, so the call
        is then exactly what it always was. Only a conversational turn
        replaces them, and it replaces both together: a completed reply with
        the conversation after it, and a turn that had to clear the
        conversation with nothing. Whatever comes back is held to the
        retention limits again here, so the session never keeps more than
        they allow, whoever computed it.
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

        if not response.needs_confirmation:
            return response

        if response.plan is None:
            raise ValueError(
                "A confirmation-required response must carry the plan it describes."
            )

        self._pending = PendingApproval(response.plan, response.plan.fingerprint)
        return response

    def confirm(self) -> BrainResponse:
        """Execute the pending plan exactly as it was presented for approval.

        The approval is consumed before execution is attempted, so neither a
        failing tool nor a raised exception can leave it replayable. A
        confirmation that binds no pending plan, or one whose plan no longer
        fingerprints to the value captured when it became pending, is refused
        with a fixed content-free message and runs nothing.
        """
        pending = self._pending
        self._pending = None

        if pending is None or pending.plan.fingerprint != pending.fingerprint:
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
