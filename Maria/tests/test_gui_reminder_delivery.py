"""Tests for due-reminder notices in the desktop GUI (V1-23-R1, decision D12-A).

The worker and the controller are driven without Qt, with the stand-in
session and recording doubles of ``tests.gui_fakes``, and, where the
session's own containment matters, a real session over a fake store. They
pin when the worker checks for due reminders: once after creating the
session and after each operation that ends with a reply, never after an
approval offer or an acknowledgment, and never once the window has asked to
close, not even the first time. A notice travels under a serial of its own;
the controller shows it without changing state and only then acknowledges
it, and the worker records it as displayed only when that acknowledgment
reaches it, in order. A notice never acknowledged stays unconfirmed.

Nothing here imports Qt, contacts a model, or opens a database, and nothing
sleeps: every wait is on an event or a queue, bounded by ``WAIT_SECONDS``.
"""

from __future__ import annotations

import logging
import queue
import threading
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from unittest.mock import Mock

from core.brain import MariaBrain
from core.reminder_delivery import ReminderDeliverySeam, notice_entry
from core.session import MariaSession
from core.task_records import DELIVERY_CHANNEL_GUI, DeliveryStorageError
from gui.controller import ControllerState, GuiController
from gui.session_worker import (
    ACTION_CANCELLED_MESSAGE,
    ApprovalOffer,
    DueReminderNotice,
    Reply,
    SessionEnded,
    SessionWorker,
    WorkerEvent,
)
from tests.gui_fakes import (
    APPROVAL_PREFIX,
    CONFIRMED_TEXT,
    WAIT_SECONDS,
    FakeSession,
    RecordingView,
    RecordingWorker,
    capturing_logger,
    reply_text,
    thread_failures_observed,
)
from tests.test_reminder_delivery import AS_OF, NOW, FakeStore, candidate


# Private-looking text a failure carries, which must reach no log line.
PRIVATE = "private-gui-notice-detail-91c3e5"


@dataclass(frozen=True)
class StandInNotice:
    """What the worker reads of a claimed notice: its text, and nothing else."""

    text: str


FIRST = StandInNotice('Reminder 1 is due: "call mom"')
SECOND = StandInNotice('Reminder 2 is overdue: "water the plants"')


class _UnreadableNotice:
    """A notice whose text cannot be read: a failure in the worker's own lines."""

    @property
    def text(self) -> str:
        raise RuntimeError(PRIVATE)


class _WorkerCase(unittest.TestCase):
    """Drive one real worker over a session, collecting what it delivers."""

    def setUp(self) -> None:
        self.events: queue.SimpleQueue[WorkerEvent] = queue.SimpleQueue()
        self.logger, self.messages = capturing_logger("tests.gui.reminders")

    def start(
        self,
        session: object,
        *,
        factory: Callable[[], object] | None = None,
        deliver: Callable[[WorkerEvent], None] | None = None,
    ) -> SessionWorker:
        """Start a worker over ``session``; it is ended after the test."""
        worker = SessionWorker(
            factory or (lambda: session),  # type: ignore[arg-type, return-value]
            deliver or self.events.put,
            logger=self.logger,
        )
        self.addCleanup(self._stop, worker)
        worker.start()
        return worker

    def _stop(self, worker: SessionWorker) -> None:
        worker.close()
        self.assertTrue(worker.join(WAIT_SECONDS))

    def next_event(self) -> WorkerEvent:
        """Return the next delivered event, failing rather than hanging."""
        return self.events.get(timeout=WAIT_SECONDS)

    def finish(self, worker: SessionWorker) -> list[WorkerEvent]:
        """Close the worker, wait for its end, and return every event left."""
        worker.close()
        self.assertTrue(worker.join(WAIT_SECONDS))
        return self.drain()

    def drain(self) -> list[WorkerEvent]:
        """Return every event delivered and not yet read."""
        remaining: list[WorkerEvent] = []
        while True:
            try:
                remaining.append(self.events.get_nowait())
            except queue.Empty:
                return remaining


class WorkerCheckTests(_WorkerCase):
    """The worker checks after creating the session and after each reply."""

    def test_the_first_check_runs_once_the_session_exists(self) -> None:
        session = FakeSession(delivers_reminders=True, notices=(FIRST,))
        worker = self.start(session)

        self.assertEqual(self.next_event(), DueReminderNotice(1, FIRST.text))
        remaining = self.finish(worker)

        self.assertEqual(remaining, [SessionEnded(failed=False)])
        self.assertEqual(session.calls, ["claim_due_reminders", "end"])
        # Never acknowledged, so never recorded: it stays unconfirmed.
        self.assertEqual(session.recorded, [])

    def test_a_session_without_a_delivery_seam_is_never_checked(self) -> None:
        session = FakeSession(notices=(FIRST,))
        worker = self.start(session)

        worker.submit("hello")
        self.assertEqual(self.next_event(), Reply(reply_text("hello")))
        remaining = self.finish(worker)

        self.assertEqual(remaining, [SessionEnded(failed=False)])
        self.assertEqual(session.calls, ["submit", "end"])

    def test_a_check_follows_a_reply_once_the_reply_is_handed_over(self) -> None:
        session = FakeSession(delivers_reminders=True, notices=(None, SECOND))
        worker = self.start(session)

        worker.submit("hello")

        self.assertEqual(
            [self.next_event(), self.next_event()],
            [Reply(reply_text("hello")), DueReminderNotice(1, SECOND.text)],
        )
        self.finish(worker)
        self.assertEqual(
            session.calls,
            ["claim_due_reminders", "submit", "claim_due_reminders", "end"],
        )

    def test_replies_to_cancel_and_confirm_are_checked_but_an_offer_is_not(
        self,
    ) -> None:
        session = FakeSession(delivers_reminders=True)
        worker = self.start(session)

        worker.submit(APPROVAL_PREFIX + "move")
        self.assertIsInstance(self.next_event(), ApprovalOffer)
        worker.cancel()
        self.assertEqual(self.next_event(), Reply(ACTION_CANCELLED_MESSAGE))
        worker.submit(APPROVAL_PREFIX + "move")
        offer = self.next_event()
        worker.confirm(offer.serial)
        self.assertEqual(self.next_event(), Reply(CONFIRMED_TEXT))
        self.finish(worker)

        self.assertEqual(
            session.calls,
            [
                "claim_due_reminders",
                "submit",
                "cancel",
                "claim_due_reminders",
                "submit",
                "confirm",
                "claim_due_reminders",
                "end",
            ],
        )


class WorkerAcknowledgmentTests(_WorkerCase):
    """Only the window's acknowledgment records a notice, once, in order."""

    def test_an_acknowledgment_records_its_notice_displayed_once(self) -> None:
        session = FakeSession(delivers_reminders=True, notices=(FIRST,))
        worker = self.start(session)
        notice = self.next_event()

        self.assertEqual(session.recorded, [])
        worker.acknowledge_reminder_notice(notice.serial)
        worker.acknowledge_reminder_notice(notice.serial)
        remaining = self.finish(worker)

        # The acknowledgment delivers no event and starts no check.
        self.assertEqual(remaining, [SessionEnded(failed=False)])
        self.assertEqual(session.recorded, [(FIRST, True)])
        self.assertEqual(
            session.calls,
            ["claim_due_reminders", "record_reminder_notice", "end"],
        )

    def test_an_unknown_serial_records_nothing(self) -> None:
        session = FakeSession(delivers_reminders=True, notices=(FIRST,))
        worker = self.start(session)
        self.next_event()

        for serial in (0, -1, 2, 99):
            worker.acknowledge_reminder_notice(serial)
        remaining = self.finish(worker)

        self.assertEqual(remaining, [SessionEnded(failed=False)])
        self.assertEqual(session.recorded, [])
        self.assertEqual(session.calls, ["claim_due_reminders", "end"])

    def test_notice_serials_are_counted_apart_from_offer_serials(self) -> None:
        session = FakeSession(
            delivers_reminders=True,
            notices=(FIRST, None, SECOND),
        )
        worker = self.start(session)
        first = self.next_event()
        worker.submit(APPROVAL_PREFIX + "move")
        offer = self.next_event()

        self.assertEqual((first.serial, offer.serial), (1, 1))
        worker.acknowledge_reminder_notice(first.serial)
        worker.confirm(offer.serial)
        self.assertEqual(self.next_event(), Reply(CONFIRMED_TEXT))
        worker.submit("hello")
        self.assertEqual(self.next_event(), Reply(reply_text("hello")))
        self.assertEqual(self.next_event(), DueReminderNotice(2, SECOND.text))
        self.finish(worker)

        self.assertEqual(session.recorded, [(FIRST, True)])
        self.assertEqual(
            session.calls,
            [
                "claim_due_reminders",
                "submit",
                "record_reminder_notice",
                "confirm",
                "claim_due_reminders",
                "submit",
                "claim_due_reminders",
                "end",
            ],
        )

    def test_an_acknowledgment_while_an_approval_waits_keeps_it_confirmable(
        self,
    ) -> None:
        session = FakeSession(delivers_reminders=True, notices=(FIRST,))
        worker = self.start(session)
        notice = self.next_event()
        worker.submit(APPROVAL_PREFIX + "move")
        offer = self.next_event()
        pending = session.pending_approval

        worker.acknowledge_reminder_notice(notice.serial)
        self.assertTrue(session.recorded_event.wait(WAIT_SECONDS))

        self.assertIsNotNone(pending)
        self.assertIs(session.pending_approval, pending)
        worker.confirm(offer.serial)
        self.assertEqual(self.next_event(), Reply(CONFIRMED_TEXT))
        self.assertEqual(session.calls.count("confirm"), 1)


class WorkerCloseTests(_WorkerCase):
    """No check runs once the window asked to close; late notices stay open."""

    def test_no_check_runs_once_close_was_called_not_even_the_first(self) -> None:
        session = FakeSession(delivers_reminders=True, notices=(FIRST,))
        creating = threading.Event()
        proceed = threading.Event()

        def factory() -> FakeSession:
            creating.set()
            if not proceed.wait(WAIT_SECONDS):
                raise AssertionError("The test never let the session start.")
            return session

        worker = self.start(session, factory=factory)
        self.assertTrue(creating.wait(WAIT_SECONDS))
        worker.close()
        proceed.set()

        self.assertTrue(worker.join(WAIT_SECONDS))
        self.assertEqual(self.drain(), [SessionEnded(failed=False)])
        self.assertEqual(session.calls, ["end"])

    def test_no_check_follows_a_reply_once_close_was_called(self) -> None:
        gate = threading.Event()
        session = FakeSession(
            gate=gate,
            delivers_reminders=True,
            notices=(None, FIRST),
        )
        worker = self.start(session)
        worker.submit("hello")
        self.assertTrue(session.started_request.wait(WAIT_SECONDS))

        worker.close()
        gate.set()

        self.assertTrue(worker.join(WAIT_SECONDS))
        self.assertEqual(
            self.drain(),
            [Reply(reply_text("hello")), SessionEnded(failed=False)],
        )
        self.assertEqual(session.calls, ["claim_due_reminders", "submit", "end"])

    def test_a_notice_claimed_as_the_close_is_asked_stays_unconfirmed(self) -> None:
        claiming = threading.Event()
        release = threading.Event()

        class ClaimingAsClosed(FakeSession):
            """A session whose first check waits until the close is asked."""

            def claim_due_reminders(self) -> object | None:
                claiming.set()
                if not release.wait(WAIT_SECONDS):
                    raise AssertionError("The test never released the check.")
                return super().claim_due_reminders()

        session = ClaimingAsClosed(delivers_reminders=True, notices=(FIRST,))
        worker = self.start(session)
        self.assertTrue(claiming.wait(WAIT_SECONDS))

        worker.close()
        release.set()
        notice = self.next_event()
        worker.acknowledge_reminder_notice(notice.serial)

        self.assertTrue(worker.join(WAIT_SECONDS))
        self.assertEqual(notice, DueReminderNotice(1, FIRST.text))
        self.assertEqual(self.drain(), [SessionEnded(failed=False)])
        self.assertEqual(session.recorded, [])
        self.assertEqual(session.calls, ["claim_due_reminders", "end"])

    def test_an_acknowledgment_queued_behind_the_close_never_runs(self) -> None:
        session = FakeSession(delivers_reminders=True, notices=(FIRST,))
        worker = self.start(session)
        notice = self.next_event()

        worker.close()
        worker.acknowledge_reminder_notice(notice.serial)

        self.assertTrue(worker.join(WAIT_SECONDS))
        self.assertEqual(self.drain(), [SessionEnded(failed=False)])
        self.assertEqual(session.recorded, [])

    def test_a_session_ended_before_the_acknowledgment_stays_ended(self) -> None:
        session = FakeSession(
            delivers_reminders=True,
            notices=(FIRST,),
            failure=RuntimeError(PRIVATE),
        )
        worker = self.start(session)
        notice = self.next_event()

        worker.submit("this request fails")
        self.assertEqual(self.next_event(), SessionEnded(failed=True))
        worker.acknowledge_reminder_notice(notice.serial)

        self.assertTrue(worker.join(WAIT_SECONDS))
        self.assertEqual(self.drain(), [])
        self.assertEqual(session.calls, ["claim_due_reminders", "submit", "end"])
        self.assertEqual(session.recorded, [])


class WorkerFailureTests(_WorkerCase):
    """Lost handoffs are contained; failures beyond them end the session."""

    def test_a_lost_handoff_leaves_the_notice_unconfirmed_and_goes_on(self) -> None:
        for failure in (RuntimeError(PRIVATE), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                self.messages.clear()
                session = FakeSession(delivers_reminders=True, notices=(FIRST,))

                def deliver(
                    event: WorkerEvent,
                    failure: BaseException = failure,
                ) -> None:
                    if isinstance(event, DueReminderNotice):
                        raise failure
                    self.events.put(event)

                with thread_failures_observed() as (hooked, stderr):
                    worker = self.start(session, deliver=deliver)
                    worker.submit("hello")
                    self.assertEqual(self.next_event(), Reply(reply_text("hello")))
                    remaining = self.finish(worker)

                self.assertEqual(remaining, [SessionEnded(failed=False)])
                self.assertEqual(session.recorded, [])
                self.assertIn("Could not deliver a GUI event.", self.messages)
                self.assertEqual((hooked, stderr.getvalue()), ([], ""))
                self.assertNotIn(PRIVATE, "\n".join(self.messages))

    def test_a_failure_beyond_exception_in_a_check_or_record_ends_it_failed(
        self,
    ) -> None:
        for label, session, acknowledge in (
            (
                "the check",
                FakeSession(
                    delivers_reminders=True,
                    claim_failure=KeyboardInterrupt(),
                ),
                False,
            ),
            (
                "the record",
                FakeSession(
                    delivers_reminders=True,
                    notices=(FIRST,),
                    record_failure=SystemExit(3),
                ),
                True,
            ),
        ):
            with self.subTest(failing=label):
                self.messages.clear()
                with thread_failures_observed() as (hooked, stderr):
                    worker = self.start(session)
                    if acknowledge:
                        worker.acknowledge_reminder_notice(self.next_event().serial)
                    self.assertEqual(self.next_event(), SessionEnded(failed=True))
                    self.assertTrue(worker.join(WAIT_SECONDS))

                self.assertEqual(session.calls[-1], "end")
                self.assertEqual(session.calls.count("end"), 1)
                self.assertEqual((hooked, stderr.getvalue()), ([], ""))
                self.assertTrue(
                    any("stopped after an unexpected" in m for m in self.messages)
                )

    def test_an_exception_in_the_workers_own_notice_lines_ends_it_failed(
        self,
    ) -> None:
        session = FakeSession(delivers_reminders=True, notices=(_UnreadableNotice(),))

        with thread_failures_observed() as (hooked, stderr):
            worker = self.start(session)
            self.assertEqual(self.next_event(), SessionEnded(failed=True))
            self.assertTrue(worker.join(WAIT_SECONDS))

        self.assertEqual(session.calls, ["claim_due_reminders", "end"])
        self.assertIn(
            "The GUI session stopped after an unexpected RuntimeError.",
            self.messages,
        )
        self.assertNotIn(PRIVATE, "\n".join(self.messages))
        self.assertEqual((hooked, stderr.getvalue()), ([], ""))


class ContainedSessionTests(_WorkerCase):
    """A real session contains its store's failures; the window goes on (A26)."""

    def session(self, store: FakeStore) -> MariaSession:
        """Return a real session with a window seam over ``store``."""
        return MariaSession(
            Mock(spec=MariaBrain),
            Mock(spec=logging.Logger),
            delivery=ReminderDeliverySeam(store, DELIVERY_CHANNEL_GUI, lambda: NOW),
        )

    def test_a_failing_store_is_contained_and_the_session_ends_in_order(
        self,
    ) -> None:
        store = FakeStore([candidate(1)])

        def busy(*_arguments: object) -> object:
            raise DeliveryStorageError("Database is busy. Try again shortly.")

        store.reminder_delivery_setting = busy  # type: ignore[method-assign]
        worker = self.start(self.session(store))

        self.assertEqual(self.finish(worker), [SessionEnded(failed=False)])
        self.assertEqual(store.claimed_ids(), [])

    def test_a_failing_outcome_write_leaves_the_claim_unconfirmed(self) -> None:
        store = FakeStore([candidate(1, text="call mom")])
        store.failing_records = {1}

        with self.assertLogs("maria.reminder_delivery", "ERROR") as logs:
            worker = self.start(self.session(store))
            notice = self.next_event()
            worker.acknowledge_reminder_notice(notice.serial)
            remaining = self.finish(worker)

        self.assertEqual(
            notice,
            DueReminderNotice(1, notice_entry(candidate(1, text="call mom"), AS_OF)),
        )
        self.assertEqual(remaining, [SessionEnded(failed=False)])
        self.assertIn(("record", 1, 1, "displayed"), store.calls)
        self.assertEqual(
            logs.output,
            [
                "ERROR:maria.reminder_delivery:Could not record the delivery "
                "of reminder 1: DeliveryStorageError.",
            ],
        )


class ControllerNoticeTests(unittest.TestCase):
    """The controller shows a notice without changing state, then acknowledges."""

    def open(self, enter: Callable[[GuiController], object]) -> GuiController:
        """Start a controller over recording doubles, then bring it to a state."""
        self.view = RecordingView()
        self.worker = RecordingWorker()
        controller = GuiController(self.view, self.worker)
        controller.start()
        enter(controller)
        return controller

    def snapshot(self, controller: GuiController) -> tuple[object, ...]:
        """Return everything a notice must leave as it was."""
        return (
            controller.state,
            list(self.view.statuses),
            self.view.approval_visible,
            list(self.view.shown_offers),
            dict(self.view.controls),
            self.view.cleared,
        )

    def test_a_notice_is_shown_then_acknowledged_in_every_open_state(self) -> None:
        def awaiting(controller: GuiController) -> None:
            controller.send(APPROVAL_PREFIX + "move")
            controller.receive(ApprovalOffer(4, "Confirmation required"))

        def busy(controller: GuiController) -> None:
            controller.send("hello")

        def closing(controller: GuiController) -> None:
            controller.request_close()

        for label, enter, state in (
            ("ready", lambda controller: None, ControllerState.READY),
            ("busy", busy, ControllerState.BUSY),
            ("awaiting approval", awaiting, ControllerState.AWAITING_APPROVAL),
            ("closing", closing, ControllerState.CLOSING),
        ):
            with self.subTest(state=label):
                controller = self.open(enter)
                self.assertIs(controller.state, state)
                before = self.snapshot(controller)
                commands = len(self.worker.commands)

                controller.receive(DueReminderNotice(3, FIRST.text))

                self.assertEqual(self.view.lines[-1], f"Maria: {FIRST.text}")
                self.assertEqual(
                    self.worker.commands[commands:],
                    [("acknowledge_reminder_notice", 3)],
                )
                self.assertEqual(self.snapshot(controller), before)

    def test_an_offer_still_answers_after_a_notice_arrives(self) -> None:
        def awaiting(controller: GuiController) -> None:
            controller.send(APPROVAL_PREFIX + "move")
            controller.receive(ApprovalOffer(4, "Confirmation required"))

        controller = self.open(awaiting)
        controller.receive(DueReminderNotice(4, FIRST.text))

        self.assertTrue(controller.approve(4))
        self.assertEqual(
            self.worker.commands[-2:],
            [("acknowledge_reminder_notice", 4), ("confirm", 4)],
        )

    def test_a_closed_window_neither_shows_nor_acknowledges_a_notice(self) -> None:
        def closed(controller: GuiController) -> None:
            controller.request_close()
            controller.receive(SessionEnded(failed=False))

        controller = self.open(closed)
        lines = list(self.view.lines)
        commands = list(self.worker.commands)

        controller.receive(DueReminderNotice(1, FIRST.text))

        self.assertIs(controller.state, ControllerState.CLOSED)
        self.assertEqual(self.view.lines, lines)
        self.assertEqual(self.worker.commands, commands)

    def test_a_notice_whose_rendering_raises_is_never_acknowledged(self) -> None:
        controller = self.open(lambda controller: None)

        def failing(line: str) -> None:
            raise RuntimeError(PRIVATE)

        self.view.append = failing  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            controller.receive(DueReminderNotice(1, FIRST.text))

        self.assertNotIn(
            "acknowledge_reminder_notice",
            [command[0] for command in self.worker.commands],
        )
        # The window's shield then closes it as a failure.
        controller.close_as_failed()
        controller.receive(SessionEnded(failed=False))
        self.assertEqual(self.worker.commands, [("start",), ("close",)])
        self.assertEqual(controller.exit_status, 1)


class WindowFlowTests(unittest.TestCase):
    """A real worker and controller, with the test playing the GUI thread."""

    def setUp(self) -> None:
        self.events: queue.SimpleQueue[WorkerEvent] = queue.SimpleQueue()
        self.view = RecordingView()

    def open(self, session: FakeSession) -> GuiController:
        """Start a controller over a real worker owning ``session``."""
        worker = SessionWorker(lambda: session, self.events.put)
        controller = GuiController(self.view, worker)
        self.addCleanup(self._stop, worker)
        controller.start()
        return controller

    def _stop(self, worker: SessionWorker) -> None:
        worker.close()
        self.assertTrue(worker.join(WAIT_SECONDS))

    def deliver(self, controller: GuiController) -> WorkerEvent:
        """Hand the worker's next event to the controller."""
        event = self.events.get(timeout=WAIT_SECONDS)
        controller.receive(event)
        return event

    def close(self, controller: GuiController) -> None:
        """Ask to close, then hand over events until the window has closed."""
        controller.request_close()
        while controller.state is not ControllerState.CLOSED:
            self.deliver(controller)

    def test_a_notice_is_recorded_only_after_the_window_shows_it(self) -> None:
        session = FakeSession(delivers_reminders=True, notices=(FIRST,))
        controller = self.open(session)

        event = self.events.get(timeout=WAIT_SECONDS)
        self.assertEqual(event, DueReminderNotice(1, FIRST.text))
        self.assertEqual(session.recorded, [])
        controller.receive(event)
        self.close(controller)

        self.assertIn(f"Maria: {FIRST.text}", self.view.lines)
        self.assertEqual(session.recorded, [(FIRST, True)])
        self.assertEqual(controller.exit_status, 0)

    def test_a_notice_after_a_reply_is_shown_after_it(self) -> None:
        session = FakeSession(delivers_reminders=True, notices=(None, SECOND))
        controller = self.open(session)

        controller.send("hello")
        self.assertEqual(self.deliver(controller), Reply(reply_text("hello")))
        self.assertIs(controller.state, ControllerState.READY)
        self.assertEqual(self.deliver(controller), DueReminderNotice(1, SECOND.text))
        self.close(controller)

        self.assertEqual(
            self.view.maria_lines[-2:],
            [f"Maria: {reply_text('hello')}", f"Maria: {SECOND.text}"],
        )
        self.assertEqual(session.recorded, [(SECOND, True)])


if __name__ == "__main__":
    unittest.main()
