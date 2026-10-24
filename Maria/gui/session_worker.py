"""The one worker thread that runs a desktop window's session operations.

Stage V1-21 admits exactly one thread for the GUI, under the owner's
amendment V1-21-R1: this worker. It creates the window's session, performs
each operation the owner asks for, one at a time and in the order asked, and
ends the session, so a single owner holds the session for its whole life and
the GUI thread never touches it. It schedules nothing, starts no work of its
own beyond the V1-23-R1 reminder check and acknowledgment described below,
and stops for good once the session has ended. This is the only production
module that imports ``threading``.

Every result of an owner's operation goes back as one event through
``deliver``, which the window supplies and which must hand the event to the
GUI thread; the worker never touches a widget. When a request leaves an approval pending, the worker keeps
the exact ``PendingApproval`` object the session holds, and confirms only
while that object is still the session's pending approval, through the
session's own ``confirm()``. Dismissing uses the session's own ``cancel()``,
and ending uses ``end()``, which discards any approval still pending, so an
approval that arrives as the window closes is never confirmed.

Under V1-23-R1 the worker also runs the session's due-reminder check, when
the session delivers reminders: once after creating the session, and once
after each operation whose event is a ``Reply``, after that reply has been
handed to the window. It never checks after an approval offer, never on a
timer, and never once ``close()`` was called, not even the first time. A
claimed notice goes to the window as one ``DueReminderNotice`` under a
notice serial of its own, and the worker keeps it until the window
acknowledges that it showed it; the acknowledgment is one more queued
operation, taken in order with the owner's, which records the notice as
displayed, delivers no event, and starts no check. A notice the window never
acknowledges stays unconfirmed, and the kept notices go with the session
when it ends.

Under V1-24-R1 the worker also asks the session for the project selector's
choices, at the same moments as the reminder check and under the same rules:
once after creating the session, once after each operation whose event is a
``Reply``, never after an approval offer, never on a timer, and never once
``close()`` was called. Choices it reads go to the window as one
``ProjectList``; a read that gives none delivers nothing, so the selector
keeps what it shows.

An event carries only text Maria shows the owner, and its ``repr`` shows none
of it. Every failure, whatever its kind, is contained in the worker's thread:
creating the session, an operation, delivering an event, and ending the
session are each caught at that boundary, so nothing reaches Python's thread
exception hook or prints a traceback. Nothing is logged here but fixed lines,
which name at most an exception type. A failure to create the session, of an
operation, or of the session's end stops the worker and reports the end as
failed: an orderly end needs both the requested close and a session end that
succeeded. An event that cannot be delivered is only logged, and the worker
carries on, since the window it was meant for may already be gone; a window
that never sees the end never reports one in order either.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from logging import Logger

from core.logger import get_logger
from core.session import (
    CONFIRMATION_REFUSED_MESSAGE,
    MariaSession,
    PendingApproval,
    ReminderNotice,
)


# What Maria says once the owner dismisses a pending approval, exactly as the
# command line prints it.
ACTION_CANCELLED_MESSAGE = "Action cancelled."

# The worker thread's name, which says what it is and nothing more.
THREAD_NAME = "maria-session"


@dataclass(frozen=True)
class Reply:
    """What one finished operation said, shown as Maria's message."""

    text: str = field(repr=False)


@dataclass(frozen=True)
class ApprovalOffer:
    """A request that left an approval pending, with the core's approval text.

    ``serial`` names this offer among every offer the worker has made, so an
    answer can be checked against the offer it was given for.
    """

    serial: int
    text: str = field(repr=False)


@dataclass(frozen=True)
class SessionEnded:
    """The session has ended and the worker has stopped.

    ``failed`` says the session did not end in order: creating it, an
    operation, or ending it failed unexpectedly, rather than closing as asked
    and ending cleanly.
    """

    failed: bool = False


@dataclass(frozen=True)
class DueReminderNotice:
    """Due reminders the session claimed, for the window to show (V1-23-R1).

    ``serial`` names this notice among the notices the worker has handed
    off, counted apart from offer serials, so the window's acknowledgment
    names exactly it and can never name an offer.
    """

    serial: int
    text: str = field(repr=False)


@dataclass(frozen=True)
class ProjectList:
    """The projects the window's selector lists, and the active one (V1-24-R1).

    ``projects`` holds at most fifty (id, name) pairs in ascending id order,
    and ``active_id`` names the active project only while one is active.
    """

    projects: tuple[tuple[int, str], ...] = field(repr=False)
    active_id: int | None = None


WorkerEvent = (
    Reply | ApprovalOffer | SessionEnded | DueReminderNotice | ProjectList
)

# One queued operation: it runs on the worker thread with the session and
# returns the event to deliver, or None for an operation that delivers none.
# ``None`` in the queue itself asks for the end.
_Operation = Callable[[MariaSession], WorkerEvent | None]


class SessionWorker:
    """Run every session operation of one window, in order, on one thread."""

    def __init__(
        self,
        session_factory: Callable[[], MariaSession],
        deliver: Callable[[WorkerEvent], None],
        logger: Logger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._deliver = deliver
        self.logger = logger or get_logger(__name__)
        self._operations: queue.SimpleQueue[_Operation | None] = (
            queue.SimpleQueue()
        )
        # Not a daemon: the interpreter never abandons a session mid-operation.
        self._thread = threading.Thread(
            target=self._run,
            name=THREAD_NAME,
            daemon=False,
        )
        # The offer last made and the serial it carries. Both are read and
        # written only on the worker thread.
        self._offer: tuple[int, PendingApproval] | None = None
        self._serial = 0
        # Set by ``close()`` before it queues the end, on the window's
        # thread, and read here before every reminder check, so no check
        # runs once the window has asked to close.
        self._closing = threading.Event()
        # The notices handed to the window and not yet acknowledged, by
        # their own serial. Both are touched only on the worker thread.
        self._notices: dict[int, ReminderNotice] = {}
        self._notice_serial = 0

    def start(self) -> None:
        """Start the worker, which first creates the window's session."""
        self._thread.start()

    def submit(self, request: str) -> None:
        """Ask the session to take one request, after any earlier operation."""
        self._operations.put(lambda session: self._submit(session, request))

    def confirm(self, serial: int) -> None:
        """Ask for the offer named by ``serial`` to be confirmed."""
        self._operations.put(lambda session: self._confirm(session, serial))

    def cancel(self) -> None:
        """Ask for the pending approval to be dismissed."""
        self._operations.put(self._cancel)

    def acknowledge_reminder_notice(self, serial: int) -> None:
        """Say that the window showed the notice named by ``serial``.

        Like every command, this only queues work and never raises. The
        acknowledgment runs in order with the owner's operations, and one
        queued after the close never runs.
        """
        self._operations.put(
            lambda session: self._acknowledge(session, serial)
        )

    def close(self) -> None:
        """Ask for the session to end once every earlier operation finishes.

        No reminder check runs once this was called, not even the first.
        """
        self._closing.set()
        self._operations.put(None)

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the worker to stop, and return whether it has.

        A worker that was never started has nothing to wait for. Nothing here
        stops a running operation: waiting is the only way to an end.
        """
        if self._thread.ident is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        """Own the session from creation to end, one operation at a time.

        This method is the whole boundary of the worker's thread, so whatever
        is raised here, of whatever kind, is caught here: by creating the
        session, by an operation, or by ending the session. Nothing reaches
        Python's thread exception hook. A failure stops the queue at once, so
        no later operation runs and none runs twice. The retained offer is
        cleared, and the session is ended exactly once whenever it exists.
        Only reaching the requested close and then ending the session without
        a failure is an orderly end; anything else ends as failed. Each
        failure is logged by its exception type alone.
        """
        session: MariaSession | None = None
        reached_close = False
        try:
            session = self._session_factory()
            self.logger.info("GUI session started.")
            self._check(session)
            self._list_projects(session)
            while (operation := self._operations.get()) is not None:
                event = operation(session)
                if event is None:
                    continue
                self._send(event)
                if isinstance(event, Reply):
                    self._check(session)
                    self._list_projects(session)
            reached_close = True
        except BaseException as error:  # noqa: BLE001 - thread boundary.
            self.logger.error(
                "The GUI session stopped after an unexpected %s.",
                type(error).__name__,
            )
        self._offer = None
        self._notices.clear()
        ended = session is None or self._end(session)
        self._send(SessionEnded(failed=not (reached_close and ended)))

    def _end(self, session: MariaSession) -> bool:
        """End the session once; return whether it ended without failing."""
        try:
            session.end()
        except BaseException as error:  # noqa: BLE001 - thread boundary.
            self.logger.error(
                "The GUI session did not end cleanly: unexpected %s.",
                type(error).__name__,
            )
            return False
        return True

    def _submit(self, session: MariaSession, request: str) -> WorkerEvent:
        """Hand one request to the session, and offer any approval it left."""
        self._offer = None
        response = session.submit(request)
        pending = session.pending_approval
        if not response.needs_confirmation or pending is None:
            return Reply(response.text)
        self._serial += 1
        self._offer = (self._serial, pending)
        return ApprovalOffer(self._serial, response.text)

    def _confirm(self, session: MariaSession, serial: int) -> WorkerEvent:
        """Confirm the offer shown, only while its own approval is pending.

        The offer must be the last one made, and the exact object it kept
        must still be the session's pending approval; only then does the
        session's own ``confirm()`` run, with its own fingerprint check.
        Anything else confirms nothing and dismisses whatever is pending, so
        no approval the owner was not shown can stay waiting.
        """
        offer, self._offer = self._offer, None
        if (
            offer is None
            or offer[0] != serial
            or session.pending_approval is not offer[1]
        ):
            session.cancel()
            self.logger.warning(
                "Refused an approval that no longer matches its offer."
            )
            return Reply(CONFIRMATION_REFUSED_MESSAGE)
        return Reply(session.confirm().text)

    def _cancel(self, session: MariaSession) -> WorkerEvent:
        """Dismiss the pending approval, exactly as the command line does."""
        self._offer = None
        session.cancel()
        return Reply(ACTION_CANCELLED_MESSAGE)

    def _check(self, session: MariaSession) -> None:
        """Hand the window the due reminders the session claims now, if any.

        Nothing runs once the window has asked to close, or for a session
        without a delivery seam. A claimed notice is kept under its own
        serial until the window acknowledges it. The retained offer and the
        session's pending approval are never touched.
        """
        if self._closing.is_set() or not session.delivers_reminders:
            return
        notice = session.claim_due_reminders()
        if notice is None:
            return
        self._notice_serial += 1
        self._notices[self._notice_serial] = notice
        self._send(DueReminderNotice(self._notice_serial, notice.text))

    def _list_projects(self, session: MariaSession) -> None:
        """Hand the window the selector's choices the session reads now.

        Nothing runs once the window has asked to close. A read that gives no
        choices delivers nothing. The retained offer and the session's
        pending approval are never touched.
        """
        if self._closing.is_set():
            return
        choices = session.project_choices()
        if choices is None:
            return
        self._send(ProjectList(choices.projects, choices.active_id))

    def _acknowledge(self, session: MariaSession, serial: int) -> None:
        """Record a notice the window showed as displayed; deliver nothing.

        Only a notice still kept under ``serial`` is recorded, once; a
        repeated or unknown serial changes nothing. No check follows, and
        neither the retained offer nor the pending approval changes.
        """
        notice = self._notices.pop(serial, None)
        if notice is None:
            return None
        session.record_reminder_notice(notice, displayed=True)
        return None

    def _send(self, event: WorkerEvent) -> None:
        """Hand one event to the window; one already gone stops nothing.

        Delivery is part of this thread's boundary too, so nothing it raises,
        of whatever kind, escapes the worker.
        """
        try:
            self._deliver(event)
        except BaseException:  # noqa: BLE001 - thread boundary.
            self.logger.error("Could not deliver a GUI event.")
