"""Tests for the desktop GUI's one session worker (V1-21).

The worker is driven directly, with a stand-in session that records which
thread made each call, and every event it delivers is collected in order. The
tests prove that one non-daemon thread creates, uses, and ends the session;
that operations run one at a time in the order asked; that a confirmation
reaches the session's own ``confirm()`` only while the exact pending object it
offered is still pending; that closing approves nothing; and that a failure
of any kind, ending the session included, stays inside the worker, ends the
session as failed, and is logged by exception type alone.

Nothing here imports Qt, contacts a model, or opens a database.
"""

from __future__ import annotations

import queue
import threading
import unittest

from core.session import CONFIRMATION_REFUSED_MESSAGE, PendingApproval
from gui.session_worker import (
    ACTION_CANCELLED_MESSAGE,
    THREAD_NAME,
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
    approval_text,
    capturing_logger,
    reply_text,
    thread_failures_observed,
)


FAILURE_SENTINEL = "private-worker-failure-5e1c9a7d30"
REQUEST_SENTINEL = "private-worker-request-08b2f46c1e"


class SessionWorkerTests(unittest.TestCase):
    """Drive one worker directly and read the events it delivers."""

    def setUp(self) -> None:
        self.events: queue.SimpleQueue[WorkerEvent] = queue.SimpleQueue()
        self.logger, self.messages = capturing_logger("tests.gui.worker")

    def start(
        self,
        session: FakeSession | None = None,
        *,
        factory=None,
        deliver=None,
    ) -> tuple[SessionWorker, FakeSession]:
        """Start a worker over one stand-in session, stopped after the test."""
        session = session or FakeSession()
        worker = SessionWorker(
            factory or (lambda: session),
            deliver or self.events.put,
            logger=self.logger,
        )
        self.addCleanup(self._stop, worker)
        worker.start()
        return worker, session

    def _stop(self, worker: SessionWorker) -> None:
        worker.close()
        self.assertTrue(worker.join(WAIT_SECONDS))

    def next_event(self) -> WorkerEvent:
        """Return the next delivered event, failing rather than hanging."""
        return self.events.get(timeout=WAIT_SECONDS)

    def test_one_worker_thread_creates_uses_and_ends_the_session(self) -> None:
        session = FakeSession()
        created_on: list[int] = []

        def factory() -> FakeSession:
            created_on.append(threading.get_ident())
            return session

        worker, _ = self.start(factory=factory)
        worker.submit("first")
        worker.submit(APPROVAL_PREFIX + "second")
        self.next_event()
        offer = self.next_event()
        worker.confirm(offer.serial)
        worker.submit(APPROVAL_PREFIX + "third")
        self.next_event()
        third = self.next_event()
        worker.cancel()
        self.next_event()
        worker.close()

        self.assertEqual(self.next_event(), SessionEnded(failed=False))
        self.assertTrue(worker.join(WAIT_SECONDS))
        self.assertEqual(
            session.calls,
            ["submit", "submit", "confirm", "submit", "cancel", "end"],
        )
        threads = set(session.threads) | set(created_on)
        self.assertEqual(len(threads), 1)
        self.assertNotIn(threading.get_ident(), threads)
        self.assertGreater(third.serial, offer.serial)

    def test_operations_run_one_at_a_time_in_the_order_asked(self) -> None:
        worker, session = self.start()
        requests = [f"request {number}" for number in range(6)]
        for request in requests:
            worker.submit(request)

        replies = [self.next_event() for _ in requests]

        self.assertEqual(session.requests, requests)
        self.assertEqual(replies, [Reply(reply_text(text)) for text in requests])

    def test_a_request_needing_approval_is_offered_with_the_core_text(
        self,
    ) -> None:
        worker, session = self.start()
        request = APPROVAL_PREFIX + "rename"

        worker.submit(request)
        offer = self.next_event()

        self.assertIsInstance(offer, ApprovalOffer)
        self.assertEqual(offer.text, approval_text(request))
        self.assertIsNotNone(session.pending_approval)
        self.assertNotIn("confirm", session.calls)

    def test_confirming_calls_the_sessions_own_confirm_without_arguments(
        self,
    ) -> None:
        worker, session = self.start()
        worker.submit(APPROVAL_PREFIX + "rename")
        offer = self.next_event()

        worker.confirm(offer.serial)

        self.assertEqual(self.next_event(), Reply(CONFIRMED_TEXT))
        self.assertEqual(session.calls.count("confirm"), 1)

    def test_a_replaced_pending_approval_is_refused_and_dismissed(
        self,
    ) -> None:
        """Only the very object offered may be confirmed, never a lookalike."""
        worker, session = self.start()
        worker.submit(APPROVAL_PREFIX + "rename")
        offer = self.next_event()
        offered = session.pending_approval
        # Equal in every field, but not the object the offer kept.
        session.pending_approval = PendingApproval(
            offered.plan,
            offered.fingerprint,
        )

        worker.confirm(offer.serial)

        self.assertEqual(
            self.next_event(),
            Reply(CONFIRMATION_REFUSED_MESSAGE),
        )
        self.assertNotIn("confirm", session.calls)
        self.assertEqual(session.calls[-1], "cancel")
        self.assertIsNone(session.pending_approval)
        self.assertIn(
            "Refused an approval that no longer matches its offer.",
            self.messages,
        )

    def test_a_stale_or_unknown_offer_is_refused(self) -> None:
        cases = {
            "no offer was ever made": False,
            "the serial names another offer": True,
        }
        for case, offered in cases.items():
            with self.subTest(case=case):
                self.events = queue.SimpleQueue()
                worker, session = self.start()
                serial = 1
                if offered:
                    worker.submit(APPROVAL_PREFIX + "rename")
                    serial = self.next_event().serial + 1

                worker.confirm(serial)

                self.assertEqual(
                    self.next_event(),
                    Reply(CONFIRMATION_REFUSED_MESSAGE),
                )
                self.assertNotIn("confirm", session.calls)
                self.assertIsNone(session.pending_approval)

    def test_a_new_request_retires_the_earlier_offer(self) -> None:
        worker, session = self.start()
        worker.submit(APPROVAL_PREFIX + "first")
        first = self.next_event()
        worker.submit("an ordinary request")
        self.next_event()

        worker.confirm(first.serial)

        self.assertEqual(
            self.next_event(),
            Reply(CONFIRMATION_REFUSED_MESSAGE),
        )
        self.assertNotIn("confirm", session.calls)

    def test_an_offer_can_be_answered_only_once(self) -> None:
        worker, session = self.start()
        worker.submit(APPROVAL_PREFIX + "rename")
        offer = self.next_event()

        worker.confirm(offer.serial)
        worker.confirm(offer.serial)

        self.assertEqual(self.next_event(), Reply(CONFIRMED_TEXT))
        self.assertEqual(
            self.next_event(),
            Reply(CONFIRMATION_REFUSED_MESSAGE),
        )
        self.assertEqual(session.calls.count("confirm"), 1)

    def test_cancel_dismisses_through_the_sessions_own_cancel(self) -> None:
        worker, session = self.start()
        worker.submit(APPROVAL_PREFIX + "rename")
        offer = self.next_event()

        worker.cancel()
        worker.confirm(offer.serial)

        self.assertEqual(self.next_event(), Reply(ACTION_CANCELLED_MESSAGE))
        self.assertEqual(
            self.next_event(),
            Reply(CONFIRMATION_REFUSED_MESSAGE),
        )
        self.assertEqual(session.calls.count("cancel"), 2)
        self.assertNotIn("confirm", session.calls)
        self.assertEqual(ACTION_CANCELLED_MESSAGE, "Action cancelled.")

    def test_closing_ends_the_session_once_and_reports_it_last(self) -> None:
        worker, session = self.start()
        worker.submit("one")
        worker.close()
        worker.submit("never run")

        self.assertEqual(self.next_event(), Reply(reply_text("one")))
        self.assertEqual(self.next_event(), SessionEnded(failed=False))
        self.assertTrue(worker.join(WAIT_SECONDS))
        self.assertEqual(session.calls, ["submit", "end"])
        with self.assertRaises(queue.Empty):
            self.events.get(timeout=0.2)

    def test_an_approval_pending_at_close_is_never_confirmed(self) -> None:
        gate = threading.Event()
        worker, session = self.start(FakeSession(gate=gate))
        # Registered after the worker, so the gate opens before it stops.
        self.addCleanup(gate.set)
        worker.submit(APPROVAL_PREFIX + "move")
        self.assertTrue(session.started_request.wait(WAIT_SECONDS))
        # Closing is asked for while the request that produces the offer
        # is still running.
        worker.close()
        gate.set()

        self.assertIsInstance(self.next_event(), ApprovalOffer)
        self.assertEqual(self.next_event(), SessionEnded(failed=False))
        self.assertEqual(session.calls, ["submit", "end"])
        self.assertIsNone(session.pending_approval)

    def test_the_worker_keeps_no_offer_once_it_has_stopped(self) -> None:
        for end_failure in (None, SystemExit(FAILURE_SENTINEL)):
            with self.subTest(end_failure=type(end_failure).__name__):
                worker, session = self.start(
                    FakeSession(end_failure=end_failure)
                )
                worker.submit(APPROVAL_PREFIX + "move")
                self.assertIsInstance(self.next_event(), ApprovalOffer)
                worker.close()

                self.assertEqual(
                    self.next_event(),
                    SessionEnded(failed=end_failure is not None),
                )
                self.assertTrue(worker.join(WAIT_SECONDS))
                self.assertEqual(session.calls, ["submit", "end"])
                self.assertIsNone(worker._offer)

    def test_an_unexpected_failure_ends_the_session_logged_by_type_only(
        self,
    ) -> None:
        session = FakeSession(failure=RuntimeError(FAILURE_SENTINEL))
        worker, _ = self.start(session)

        worker.submit(REQUEST_SENTINEL)
        worker.submit("never run")

        self.assertEqual(self.next_event(), SessionEnded(failed=True))
        self.assertTrue(worker.join(WAIT_SECONDS))
        self.assertEqual(session.calls, ["submit", "end"])
        self.assertIn(
            "The GUI session stopped after an unexpected RuntimeError.",
            self.messages,
        )
        for message in self.messages:
            self.assertNotIn(FAILURE_SENTINEL, message)
            self.assertNotIn(REQUEST_SENTINEL, message)

    def test_a_failure_of_any_kind_ends_the_session_as_failed(self) -> None:
        """Even an exit raised inside an operation is never an orderly end."""
        session = FakeSession(failure=SystemExit(3))
        worker, _ = self.start(session)

        worker.submit("exit from inside")

        self.assertEqual(self.next_event(), SessionEnded(failed=True))
        self.assertTrue(worker.join(WAIT_SECONDS))
        self.assertEqual(session.calls, ["submit", "end"])

    def test_a_failing_session_factory_ends_the_worker_without_a_session(
        self,
    ) -> None:
        def factory() -> FakeSession:
            raise ValueError(FAILURE_SENTINEL)

        worker, _ = self.start(factory=factory)

        self.assertEqual(self.next_event(), SessionEnded(failed=True))
        self.assertTrue(worker.join(WAIT_SECONDS))
        self.assertTrue(
            any("unexpected ValueError" in message for message in self.messages)
        )
        self.assertFalse(
            any(FAILURE_SENTINEL in message for message in self.messages)
        )

    def test_a_delivery_failure_never_stops_the_worker(self) -> None:
        delivered: list[WorkerEvent] = []

        def deliver(event: WorkerEvent) -> None:
            if isinstance(event, Reply) and not delivered:
                delivered.append(event)
                raise RuntimeError(FAILURE_SENTINEL)
            self.events.put(event)

        worker, session = self.start(deliver=deliver)
        worker.submit("lost")
        worker.submit("kept")

        self.assertEqual(self.next_event(), Reply(reply_text("kept")))
        worker.close()
        self.assertEqual(self.next_event(), SessionEnded(failed=False))
        self.assertEqual(session.calls, ["submit", "submit", "end"])
        self.assertIn("Could not deliver a GUI event.", self.messages)
        self.assertFalse(
            any(FAILURE_SENTINEL in message for message in self.messages)
        )

    def test_the_worker_thread_is_named_and_is_not_a_daemon(self) -> None:
        gate = threading.Event()
        worker, session = self.start(FakeSession(gate=gate))
        self.addCleanup(gate.set)
        worker.submit("hold")
        self.assertTrue(session.started_request.wait(WAIT_SECONDS))

        running = [
            thread
            for thread in threading.enumerate()
            if thread.name == THREAD_NAME
        ]
        gate.set()

        self.assertEqual(len(running), 1)
        self.assertFalse(running[0].daemon)
        self.assertEqual(THREAD_NAME, "maria-session")

    def test_a_worker_that_never_started_has_nothing_to_wait_for(self) -> None:
        worker = SessionWorker(FakeSession, self.events.put, logger=self.logger)

        self.assertTrue(worker.join(0))

    def test_no_event_shows_its_text(self) -> None:
        secret = "private-event-text-6f0d2b9a41"
        for event in (
            Reply(secret),
            ApprovalOffer(3, secret),
            DueReminderNotice(4, secret),
        ):
            with self.subTest(event=type(event).__name__):
                for shown in (repr(event), str(event), f"{event!r}"):
                    self.assertNotIn(secret, shown)


class WorkerFailureContainmentTests(unittest.TestCase):
    """Every failure stays inside the worker, whatever its kind.

    Each case watches Python's thread exception hook and standard error
    without suppressing either, so an escaped failure is seen as a hook call
    and as text. Each also proves the worker really stopped, that the session
    was ended at most once, and that queued work never ran after a failure.
    """

    KINDS = (KeyboardInterrupt, SystemExit, ValueError)

    def setUp(self) -> None:
        self.logger, self.messages = capturing_logger("tests.gui.containment")

    def run_worker(
        self,
        session: FakeSession | None,
        *,
        requests: tuple[str, ...] = (),
        factory=None,
        deliver=None,
    ) -> tuple[list[WorkerEvent], list[str], str, bool]:
        """Run one worker to its end; return events, hook calls, stderr."""
        events: queue.SimpleQueue[WorkerEvent] = queue.SimpleQueue()
        with thread_failures_observed() as (hook_calls, captured):
            worker = SessionWorker(
                factory or (lambda: session),
                deliver or events.put,
                logger=self.logger,
            )
            worker.start()
            for request in requests:
                worker.submit(request)
            worker.close()
            stopped = worker.join(WAIT_SECONDS)
        delivered = []
        while True:
            try:
                delivered.append(events.get_nowait())
            except queue.Empty:
                break
        return delivered, list(hook_calls), captured.getvalue(), stopped

    def assert_contained(
        self,
        hook_calls: list[str],
        stderr: str,
        stopped: bool,
    ) -> None:
        """Assert nothing escaped and the worker thread really ended."""
        self.assertTrue(stopped)
        self.assertFalse(
            any(thread.name == THREAD_NAME for thread in threading.enumerate())
        )
        self.assertEqual(hook_calls, [])
        self.assertEqual(stderr, "")
        for message in self.messages:
            self.assertNotIn(FAILURE_SENTINEL, message)

    def test_an_operation_failure_of_any_kind_ends_the_session_once(
        self,
    ) -> None:
        for kind in self.KINDS:
            with self.subTest(kind=kind.__name__):
                self.messages.clear()
                session = FakeSession(failure=kind(FAILURE_SENTINEL))

                delivered, hook_calls, stderr, stopped = self.run_worker(
                    session,
                    requests=("fails", "queued after the failure"),
                )

                self.assert_contained(hook_calls, stderr, stopped)
                self.assertEqual(delivered, [SessionEnded(failed=True)])
                self.assertEqual(session.calls, ["submit", "end"])
                self.assertEqual(session.requests, ["fails"])
                self.assertIn(
                    "The GUI session stopped after an unexpected "
                    f"{kind.__name__}.",
                    self.messages,
                )

    def test_a_session_factory_failure_of_any_kind_is_contained(self) -> None:
        for kind in self.KINDS:
            with self.subTest(kind=kind.__name__):
                self.messages.clear()

                def factory() -> FakeSession:
                    raise kind(FAILURE_SENTINEL)

                delivered, hook_calls, stderr, stopped = self.run_worker(
                    None,
                    requests=("never run",),
                    factory=factory,
                )

                self.assert_contained(hook_calls, stderr, stopped)
                self.assertEqual(delivered, [SessionEnded(failed=True)])
                self.assertIn(
                    "The GUI session stopped after an unexpected "
                    f"{kind.__name__}.",
                    self.messages,
                )

    def test_a_failing_end_after_the_requested_close_is_a_failed_end(
        self,
    ) -> None:
        for kind in (*self.KINDS, RuntimeError):
            with self.subTest(kind=kind.__name__):
                self.messages.clear()
                session = FakeSession(end_failure=kind(FAILURE_SENTINEL))

                delivered, hook_calls, stderr, stopped = self.run_worker(
                    session,
                    requests=("hello",),
                )

                self.assert_contained(hook_calls, stderr, stopped)
                self.assertEqual(
                    delivered,
                    [Reply(reply_text("hello")), SessionEnded(failed=True)],
                )
                self.assertEqual(session.calls, ["submit", "end"])
                self.assertIn(
                    f"The GUI session did not end cleanly: unexpected "
                    f"{kind.__name__}.",
                    self.messages,
                )

    def test_a_failing_operation_and_a_failing_end_end_the_session_once(
        self,
    ) -> None:
        session = FakeSession(
            failure=KeyboardInterrupt(FAILURE_SENTINEL),
            end_failure=SystemExit(FAILURE_SENTINEL),
        )

        delivered, hook_calls, stderr, stopped = self.run_worker(
            session,
            requests=("fails", "queued after the failure"),
        )

        self.assert_contained(hook_calls, stderr, stopped)
        self.assertEqual(delivered, [SessionEnded(failed=True)])
        self.assertEqual(session.calls, ["submit", "end"])
        self.assertEqual(
            [message for message in self.messages if "unexpected" in message],
            [
                "The GUI session stopped after an unexpected "
                "KeyboardInterrupt.",
                "The GUI session did not end cleanly: unexpected SystemExit.",
            ],
        )

    def test_a_delivery_failure_of_any_kind_never_escapes(self) -> None:
        for kind in self.KINDS:
            with self.subTest(kind=kind.__name__):
                self.messages.clear()
                session = FakeSession()
                delivered: list[WorkerEvent] = []

                def deliver(event: WorkerEvent) -> None:
                    if isinstance(event, Reply):
                        raise kind(FAILURE_SENTINEL)
                    delivered.append(event)

                _, hook_calls, stderr, stopped = self.run_worker(
                    session,
                    requests=("one", "two"),
                    deliver=deliver,
                )

                self.assert_contained(hook_calls, stderr, stopped)
                self.assertEqual(delivered, [SessionEnded(failed=False)])
                self.assertEqual(session.calls, ["submit", "submit", "end"])
                self.assertEqual(
                    self.messages.count("Could not deliver a GUI event."),
                    2,
                )

    def test_an_orderly_close_is_reported_only_when_the_end_succeeds(
        self,
    ) -> None:
        delivered, hook_calls, stderr, stopped = self.run_worker(
            FakeSession(),
            requests=("hello",),
        )

        self.assert_contained(hook_calls, stderr, stopped)
        self.assertEqual(delivered[-1], SessionEnded(failed=False))

    def test_the_observer_sees_a_failure_that_escapes_a_thread(self) -> None:
        """Positive control: the hook and stderr really see a leak."""

        def escape() -> None:
            raise KeyboardInterrupt(FAILURE_SENTINEL)

        with thread_failures_observed() as (hook_calls, captured):
            thread = threading.Thread(target=escape, name="observer-control")
            thread.start()
            thread.join(WAIT_SECONDS)

        self.assertEqual(hook_calls, ["KeyboardInterrupt"])
        self.assertIn(FAILURE_SENTINEL, captured.getvalue())
        self.assertIn("Traceback", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
