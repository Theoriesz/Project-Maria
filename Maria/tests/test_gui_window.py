"""Qt Widgets tests for Maria's desktop window (V1-21).

Every test here needs PyQt6 and Qt's offscreen platform. Each is skipped,
with its reason, when either is unavailable, as on a Python build without the
optional GUI requirements. The window is driven on the offscreen platform, so
nothing appears on a screen and nothing here is visual verification. A
stand-in session answers from a script, except where a real session over a
temporary world is named.

Qt ends the whole process when a callback it runs raises, so every callback
these tests hand to Qt records a failure instead of raising it, and the test
asserts afterwards. No test reads or writes the clipboard, contacts a model,
or reads Maria's live database, live log, or private override.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from collections.abc import Callable
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core.session import MariaSession
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

# Private-looking text a fault carries, which must reach no log or stream.
FAILURE_TEXT = "private-window-cleanup-detail-4e1a7c"

try:
    from PyQt6.QtCore import (
        QLibraryInfo,
        QPoint,
        Qt,
        QtMsgType,
        QTimer,
        qInstallMessageHandler,
    )
    from PyQt6.QtGui import QCloseEvent
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
except ImportError:
    QT_MISSING = "PyQt6 is not installed."
else:
    QT_MISSING = ""


def _unavailable() -> str:
    """Return why these tests cannot run here, or an empty string."""
    if QT_MISSING:
        return QT_MISSING
    plugins = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    platforms = plugins / "platforms"
    if not platforms.is_dir() or not any(
        "qoffscreen" in entry.name for entry in platforms.iterdir()
    ):
        return "Qt's offscreen platform is unavailable."
    return ""


UNAVAILABLE = _unavailable()

if not UNAVAILABLE:
    import main as main_module
    from gui.controller import (
        ONE_LINE_MESSAGE,
        STATUS_BUSY,
        STATUS_CLOSING_BUSY,
        STATUS_READY,
        ControllerState,
        GuiController,
    )
    import gui.window as window_module
    from gui.session_worker import (
        THREAD_NAME,
        ApprovalOffer,
        DueReminderNotice,
        Reply,
        SessionEnded,
        SessionWorker,
        WorkerEvent,
    )
    from gui.window import (
        INPUT_MAXIMUM_LENGTH,
        MariaWindow,
        prepare_application,
        run_window,
    )

# Kept for the whole run: Qt allows one application object per process.
_APPLICATION: list[object] = []


def application() -> QApplication:
    """Return the run's application object, on the offscreen platform."""
    existing = QApplication.instance()
    if existing is None:
        existing = QApplication(["maria-gui-tests", "-platform", "offscreen"])
        _APPLICATION.append(existing)
    return existing


def _quiet_maria_logger(test: unittest.TestCase) -> None:
    """Keep Maria's log lines off the console for one test."""
    quiet = logging.NullHandler()
    maria = logging.getLogger("maria")
    maria.addHandler(quiet)
    test.addCleanup(maria.removeHandler, quiet)


def wait_until(predicate: Callable[[], bool]) -> bool:
    """Let Qt deliver events until the predicate holds, or give up."""
    deadline = time.monotonic() + WAIT_SECONDS
    while not predicate():
        if time.monotonic() > deadline:
            return False
        QApplication.processEvents()
        time.sleep(0.005)
    return True


@unittest.skipIf(UNAVAILABLE, UNAVAILABLE)
class WindowTestCase(unittest.TestCase):
    """A real window, controller, and worker over one stand-in session."""

    def setUp(self) -> None:
        self.application = application()
        # Closing a test's window must never ask Qt to quit; only
        # ``run_window`` runs Qt's own loop.
        self.application.setQuitOnLastWindowClosed(False)
        self.addCleanup(self.application.setQuitOnLastWindowClosed, True)
        _quiet_maria_logger(self)

    def open(self, session: FakeSession | MariaSession) -> MariaWindow:
        """Open a window over the session, closed in order after the test."""
        self.window = MariaWindow()
        self.worker = SessionWorker(
            lambda: session,
            self.window.event_delivered.emit,
        )
        self.controller = GuiController(self.window, self.worker)
        self.window.bind(self.controller)
        self.addCleanup(
            self._shut_down,
            self.window,
            self.controller,
            self.worker,
        )
        self.controller.start()
        self.window.show()
        return self.window

    def _shut_down(
        self,
        window: MariaWindow,
        controller: GuiController,
        worker: SessionWorker,
    ) -> None:
        """Close one window in order, and always end its worker.

        The worker is told to end even when closing through the window
        failed, so a failed test never leaves its thread running.
        """
        try:
            if controller.state is not ControllerState.CLOSED:
                window.close()
                self.assertTrue(
                    wait_until(
                        lambda: controller.state is ControllerState.CLOSED
                    )
                )
        finally:
            worker.close()
            self.assertTrue(worker.join(2 * WAIT_SECONDS))
            window.deleteLater()
            QApplication.processEvents()

    def type_request(self, text: str) -> None:
        """Type text into the input and press Enter."""
        QTest.keyClicks(self.window.request_input, text)
        QTest.keyClick(self.window.request_input, Qt.Key.Key_Return)

    def settled(self) -> bool:
        """Wait until the window waits for the owner again."""
        return wait_until(
            lambda: self.controller.state
            in (ControllerState.READY, ControllerState.AWAITING_APPROVAL)
        )

    def conversation(self) -> str:
        """Return the conversation display's text."""
        return self.window.conversation.toPlainText()


class WindowDisplayTests(WindowTestCase):
    """Everything shown is plain text that cannot be selected or copied."""

    def test_the_displays_are_read_only_unselectable_and_have_no_menu(
        self,
    ) -> None:
        window = self.open(FakeSession())

        for display in (window.conversation, window.approval_text):
            with self.subTest(display=display.objectName() or "display"):
                self.assertTrue(display.isReadOnly())
                self.assertEqual(
                    display.textInteractionFlags(),
                    Qt.TextInteractionFlag.NoTextInteraction,
                )
                self.assertEqual(
                    display.contextMenuPolicy(),
                    Qt.ContextMenuPolicy.NoContextMenu,
                )
                self.assertEqual(display.focusPolicy(), Qt.FocusPolicy.NoFocus)
                self.assertIsNone(display.createStandardContextMenu())
        self.assertEqual(window.status.textFormat(), Qt.TextFormat.PlainText)

    def test_text_that_looks_like_markup_is_shown_exactly_as_text(
        self,
    ) -> None:
        window = self.open(FakeSession())
        markup = (
            "<b>bold</b> <img src='file:///nowhere/missing.png'> "
            "<a href='https://example.invalid/'>link</a> &amp;"
        )

        self.type_request(markup)
        self.assertTrue(self.settled())

        self.assertTrue(
            self.conversation().endswith(f"Maria: {reply_text(markup)}")
        )
        self.assertIn(f"You: {markup}", self.conversation())
        # A QLabel keeps its text as set whatever its format, so the status
        # line's plain-text rendering rests on its PlainText format, which
        # the display test asserts.

    def test_keys_and_drags_select_nothing_in_either_display(self) -> None:
        window = self.open(FakeSession())
        self.type_request(APPROVAL_PREFIX + "move")
        self.assertTrue(self.settled())

        for display in (window.conversation, window.approval_text):
            with self.subTest(display=display is window.conversation):
                QTest.keyClick(
                    display,
                    Qt.Key.Key_A,
                    Qt.KeyboardModifier.ControlModifier,
                )
                QTest.keyClick(
                    display,
                    Qt.Key.Key_C,
                    Qt.KeyboardModifier.ControlModifier,
                )
                viewport = display.viewport()
                QTest.mousePress(
                    viewport,
                    Qt.MouseButton.LeftButton,
                    pos=QPoint(2, 2),
                )
                QTest.mouseMove(viewport, QPoint(200, 40))
                QTest.mouseRelease(
                    viewport,
                    Qt.MouseButton.LeftButton,
                    pos=QPoint(200, 40),
                )

                self.assertFalse(display.textCursor().hasSelection())

    def test_the_window_title_never_changes(self) -> None:
        window = self.open(FakeSession())
        sentinel = "private-title-sentinel-4b7e19d0a2"

        self.type_request(sentinel)
        self.assertTrue(self.settled())
        self.type_request(APPROVAL_PREFIX + sentinel)
        self.assertTrue(self.settled())

        self.assertEqual(window.windowTitle(), "Maria")

    def test_the_input_keeps_long_text_and_line_breaks_for_the_controller(
        self,
    ) -> None:
        session = FakeSession()
        window = self.open(session)
        long_text = "remember " + "x" * 40_000

        self.assertEqual(window.request_input.maxLength(), INPUT_MAXIMUM_LENGTH)
        self.assertFalse(window.request_input.isReadOnly())
        window.request_input.setText(long_text)
        self.assertEqual(window.request_input.text(), long_text)
        QTest.keyClick(window.request_input, Qt.Key.Key_Return)
        self.assertTrue(self.settled())
        self.assertEqual(session.requests, [long_text])

        broken = "edit note 1: first" + chr(10) + "second"
        window.request_input.insert(broken)
        QTest.keyClick(window.request_input, Qt.Key.Key_Return)

        self.assertEqual(window.request_input.text(), broken)
        self.assertTrue(self.conversation().endswith(f"Maria: {ONE_LINE_MESSAGE}"))
        self.assertEqual(session.requests, [long_text])


class WindowFlowTests(WindowTestCase):
    """Requests, approvals, busy work, and closing, through real widgets."""

    def test_a_request_is_answered_and_shown_on_the_gui_thread(self) -> None:
        session = FakeSession()
        window = self.open(session)
        received_on: list[int] = []
        receive = self.controller.receive

        def recording_receive(event: object) -> None:
            received_on.append(threading.get_ident())
            receive(event)

        self.controller.receive = recording_receive

        self.type_request("calculate 2 + 3")
        self.assertTrue(self.settled())

        self.assertTrue(
            self.conversation().endswith(
                "You: calculate 2 + 3\n"
                f"Maria: {reply_text('calculate 2 + 3')}"
            )
        )
        self.assertEqual(window.request_input.text(), "")
        self.assertEqual(window.status.text(), STATUS_READY)
        self.assertEqual(set(received_on), {threading.get_ident()})
        self.assertNotIn(threading.get_ident(), session.threads)

    def test_an_offer_shows_the_complete_text_and_one_click_answers_it(
        self,
    ) -> None:
        session = FakeSession()
        window = self.open(session)
        request = APPROVAL_PREFIX + "rename old.txt to new.txt"

        self.type_request(request)
        self.assertTrue(self.settled())

        self.assertTrue(window.approval.isVisible())
        self.assertEqual(window.approval_text.toPlainText(), approval_text(request))
        self.assertTrue(window.approve_button.isEnabled())
        self.assertTrue(window.cancel_button.isEnabled())
        self.assertFalse(window.send_button.isEnabled())

        QTest.mouseClick(window.approve_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(window.approve_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(window.cancel_button, Qt.MouseButton.LeftButton)
        self.assertTrue(self.settled())

        self.assertEqual(session.calls.count("confirm"), 1)
        self.assertNotIn("cancel", session.calls)
        self.assertFalse(window.approval.isVisible())
        self.assertTrue(self.conversation().endswith(f"Maria: {CONFIRMED_TEXT}"))

    def test_cancel_dismisses_the_offer_without_confirming(self) -> None:
        session = FakeSession()
        window = self.open(session)
        self.type_request(APPROVAL_PREFIX + "delete note 1")
        self.assertTrue(self.settled())

        QTest.mouseClick(window.cancel_button, Qt.MouseButton.LeftButton)
        self.assertTrue(self.settled())

        self.assertNotIn("confirm", session.calls)
        self.assertEqual(session.calls.count("cancel"), 1)
        self.assertTrue(self.conversation().endswith("Maria: Action cancelled."))

    def test_no_key_typed_into_the_input_answers_an_offer(self) -> None:
        session = FakeSession()
        window = self.open(session)
        self.type_request(APPROVAL_PREFIX + "move")
        self.assertTrue(self.settled())

        for key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            QTest.keyClick(window.request_input, key)
        QTest.keyClicks(window.request_input, "y yes")
        QTest.keyClick(window.request_input, Qt.Key.Key_Return)
        QApplication.processEvents()

        for button in (
            window.approve_button,
            window.cancel_button,
            window.send_button,
        ):
            self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.NoFocus)
            self.assertFalse(button.autoDefault())
            self.assertFalse(button.isDefault())
        self.assertNotIn("confirm", session.calls)
        self.assertNotIn("cancel", session.calls)
        self.assertIs(self.controller.state, ControllerState.AWAITING_APPROVAL)

    def test_the_window_stays_responsive_while_the_worker_is_busy(
        self,
    ) -> None:
        gate = threading.Event()
        session = FakeSession(gate=gate)
        window = self.open(session)
        # Registered after the window, so the gate opens before it closes.
        self.addCleanup(gate.set)
        self.type_request("a slow request")
        self.assertTrue(session.started_request.wait(WAIT_SECONDS))

        slowest = 0.0
        for _ in range(20):
            started = time.monotonic()
            QApplication.processEvents()
            slowest = max(slowest, time.monotonic() - started)
        QTest.keyClicks(window.request_input, "next request")
        QTest.keyClick(window.request_input, Qt.Key.Key_Return)
        QApplication.processEvents()

        self.assertLess(slowest, 1.0)
        self.assertEqual(window.request_input.text(), "next request")
        self.assertEqual(window.status.text(), STATUS_BUSY)
        self.assertFalse(window.send_button.isEnabled())
        self.assertEqual(session.requests, ["a slow request"])

        gate.set()
        self.assertTrue(self.settled())
        self.assertEqual(window.request_input.text(), "next request")

    def test_closing_while_busy_waits_ends_the_session_and_approves_nothing(
        self,
    ) -> None:
        gate = threading.Event()
        session = FakeSession(gate=gate)
        window = self.open(session)
        self.addCleanup(gate.set)
        self.type_request(APPROVAL_PREFIX + "a slow move")
        self.assertTrue(session.started_request.wait(WAIT_SECONDS))

        window.close()
        QApplication.processEvents()

        self.assertTrue(window.isVisible())
        self.assertEqual(window.status.text(), STATUS_CLOSING_BUSY)
        self.assertFalse(window.request_input.isEnabled())
        self.assertFalse(window.send_button.isEnabled())

        gate.set()
        self.assertTrue(wait_until(lambda: not window.isVisible()))
        self.assertTrue(self.worker.join(WAIT_SECONDS))
        self.assertEqual(session.calls, ["submit", "end"])
        self.assertFalse(window.approval.isVisible())
        self.assertEqual(self.controller.exit_status, 0)

    def test_an_exit_word_closes_the_window_without_a_request(self) -> None:
        session = FakeSession()
        window = self.open(session)

        self.type_request("exit")

        self.assertTrue(wait_until(lambda: not window.isVisible()))
        self.assertEqual(session.requests, [])
        self.assertEqual(session.calls, ["end"])


class WindowCallbackShieldTests(WindowTestCase):
    """No callback the window hands to Qt can raise into Qt.

    Qt ends the whole process when a callback raises, so each callback is
    called here directly, never through Qt, with its controller call made
    to fail: a regression then fails this test instead of ending the run.
    """

    def test_every_callback_logs_a_failure_and_marks_the_window(self) -> None:
        """Each callback on a window of its own, since a failure closes it."""
        members = ("receive", "send", "approve", "cancel", "request_close")
        for member in members:
            with self.subTest(member=member):
                window = self.open(FakeSession())
                self.type_request(APPROVAL_PREFIX + "move")
                self.assertTrue(self.settled())
                self.assertFalse(window.failed)
                logger, messages = capturing_logger("tests.gui.window.shield")
                close_event = QCloseEvent()
                callback = {
                    "receive": lambda: window._receive(Reply("late")),
                    "send": window._send_request,
                    "approve": window._approve_offer,
                    "cancel": window._cancel_offer,
                    "request_close": lambda: window.closeEvent(close_event),
                }[member]

                with (
                    patch.object(
                        window_module,
                        "get_logger",
                        lambda name: logger,
                    ),
                    patch.object(
                        self.controller,
                        member,
                        side_effect=RuntimeError(FAILURE_TEXT),
                    ) as failing,
                ):
                    callback()

                failing.assert_called_once()
                self.assertTrue(window.failed)
                self.assertEqual(
                    messages,
                    ["A desktop window callback failed: RuntimeError."],
                )
                # The window now closes as a failure, once its session ends.
                self.assertIs(self.controller.state, ControllerState.CLOSING)
                self.assertTrue(window.isVisible())
                if member == "request_close":
                    self.assertFalse(close_event.isAccepted())


def _broken(*arguments: object, **keywords: object) -> None:
    """Stand in for a window method that fails, as a broken widget might."""
    raise RuntimeError(FAILURE_TEXT)


@unittest.skipIf(UNAVAILABLE, UNAVAILABLE)
class FailedWindowTests(unittest.TestCase):
    """A window that failed answers and sends nothing more, and closes.

    The window is real but offscreen, with a real controller and worker over
    a stand-in session. The worker's events queue up here and are handed to
    the window's production slot by direct calls, and every failure is
    injected by replacing window methods, so no injected failure ever passes
    through Qt's own dispatch, where it would end the test run. Everything
    the window asks of the worker is recorded, so a refusal is seen as an
    operation never asked for.
    """

    def setUp(self) -> None:
        self.application = application()
        self.application.setQuitOnLastWindowClosed(False)
        self.addCleanup(self.application.setQuitOnLastWindowClosed, True)
        _quiet_maria_logger(self)
        self.logger, self.messages = capturing_logger("tests.gui.failed")
        self.worker_logger, self.worker_messages = capturing_logger(
            "tests.gui.failed.worker"
        )
        patcher = patch.object(
            window_module,
            "get_logger",
            lambda name: self.logger,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def open(self) -> None:
        """Open a window whose worker's events wait here to be delivered."""
        self.events: queue.SimpleQueue[WorkerEvent] = queue.SimpleQueue()
        self.session = FakeSession()
        self.worker = SessionWorker(
            lambda: self.session,
            self.events.put,
            logger=self.worker_logger,
        )
        self.commands: list[str] = []
        for name in ("submit", "confirm", "cancel", "close"):
            self._record(name)
        self.window = MariaWindow()
        self.controller = GuiController(self.window, self.worker)
        self.window.bind(self.controller)
        self.addCleanup(self._shut_down, self.window, self.worker)
        self.controller.start()
        self.window.show()
        self.messages.clear()

    def _record(self, name: str) -> None:
        """Record each ``name`` asked of the worker, then pass it on."""
        passed_on = getattr(self.worker, name)

        def recorded(*arguments: object) -> None:
            self.commands.append(name)
            passed_on(*arguments)

        setattr(self.worker, name, recorded)

    def _shut_down(self, window: MariaWindow, worker: SessionWorker) -> None:
        """End the worker and discard the window, whatever a test left."""
        worker.close()
        self.assertTrue(worker.join(2 * WAIT_SECONDS))
        window.hide()
        window.deleteLater()
        QApplication.processEvents()

    def deliver(self) -> WorkerEvent:
        """Hand the worker's next event to the window's production slot."""
        event = self.events.get(timeout=WAIT_SECONDS)
        self.window._receive(event)
        return event

    def request(self, text: str) -> None:
        """Send one request through the window's own Send callback."""
        self.window.request_input.setText(text)
        self.window._send_request()

    def fail_late_once(self) -> None:
        """Make the window's next ``set_controls`` fail, and later ones work.

        The controller calls it last when it shows an event, so this is a
        late failure, after the state has already moved on.
        """
        working = self.window.set_controls
        failures = [RuntimeError(FAILURE_TEXT)]

        def late_failure(**controls: bool) -> None:
            if failures:
                raise failures.pop()
            working(**controls)

        self.window.set_controls = late_failure

    def queue_with_qt(
        self,
        *callbacks: Callable[[], None],
    ) -> tuple[list[str], list[str]]:
        """Queue callbacks with Qt, to run only when Qt next gets control.

        Each records that it ran, and any failure, instead of raising into
        Qt. Returns the lists of callbacks run and of failures.
        """
        ran: list[str] = []
        escaped: list[str] = []

        def queue(callback: Callable[[], None]) -> None:
            def run() -> None:
                ran.append(callback.__name__)
                try:
                    callback()
                except Exception as error:  # noqa: BLE001 - asserted later.
                    escaped.append(type(error).__name__)

            QTimer.singleShot(0, run)

        for callback in callbacks:
            queue(callback)
        return ran, escaped

    def assert_ended_as_failed(self, request: str) -> None:
        """Assert the session ended once, unanswered, and the window closed."""
        self.assertTrue(self.worker.join(WAIT_SECONDS))
        self.assertIsInstance(self.deliver(), SessionEnded)
        self.assertEqual(self.session.calls, ["submit", "end"])
        self.assertEqual(self.session.requests, [request])
        self.assertIsNone(self.session.pending_approval)
        self.assertIs(self.controller.state, ControllerState.CLOSED)
        self.assertEqual(self.controller.exit_status, 1)
        self.assertTrue(self.window.failed)
        self.assertFalse(self.window.isVisible())
        self.assertEqual(
            [m for m in self.worker_messages if "unexpected" in m],
            [],
        )

    def test_a_window_that_failed_showing_an_offer_answers_nothing(
        self,
    ) -> None:
        """A late failure while the offer is shown, then both answers.

        Answers queued with Qt before the failure run only after it,
        through Qt; the same answers follow by direct calls, in both orders,
        since only a first answer could ever be taken. None reaches the
        worker, and the session ends through it, discarding the approval.
        """
        for first, second in (("approve", "cancel"), ("cancel", "approve")):
            with self.subTest(first=first):
                self.open()
                self.request(APPROVAL_PREFIX + "move")
                self.fail_late_once()
                answers = {
                    "approve": self.window._approve_offer,
                    "cancel": self.window._cancel_offer,
                }
                ran, escaped = self.queue_with_qt(
                    answers[first],
                    answers[second],
                )

                self.assertIsInstance(self.deliver(), ApprovalOffer)

                self.assertTrue(self.window.failed)
                self.assertIs(self.controller.state, ControllerState.CLOSING)
                self.assertTrue(wait_until(lambda: len(ran) == 2))
                self.assertEqual(escaped, [])
                answers[first]()
                answers[second]()
                self.assertEqual(self.commands, ["submit", "close"])
                self.assert_ended_as_failed(APPROVAL_PREFIX + "move")
                self.assertEqual(
                    self.messages,
                    ["A desktop window callback failed: RuntimeError."],
                )

    def test_a_window_that_failed_showing_a_reply_sends_nothing(self) -> None:
        """A late failure while a reply is shown, then Send, queued and not.

        The window was ready for the next request, so only the failure can
        refuse it.
        """
        self.open()
        self.request("hello")
        self.fail_late_once()
        ran, escaped = self.queue_with_qt(self.window._send_request)

        self.assertIsInstance(self.deliver(), Reply)

        self.assertTrue(self.window.failed)
        self.assertIs(self.controller.state, ControllerState.CLOSING)
        self.window.request_input.setText("another request")
        self.assertTrue(wait_until(lambda: len(ran) == 1))
        self.assertEqual(escaped, [])
        self.window._send_request()
        self.assertEqual(self.commands, ["submit", "close"])
        self.assert_ended_as_failed("hello")
        self.assertEqual(
            self.messages,
            ["A desktop window callback failed: RuntimeError."],
        )

    def test_a_window_still_showing_its_offer_cannot_answer_it(self) -> None:
        """A window too broken to hide its offer: the controller refuses."""
        self.open()
        self.request(APPROVAL_PREFIX + "move")
        self.window.set_controls = _broken
        self.window.hide_approval = _broken

        self.assertIsInstance(self.deliver(), ApprovalOffer)

        self.assertIsNotNone(self.window._offer_serial)
        self.window._approve_offer()
        self.window._cancel_offer()
        self.window.request_input.setText("another request")
        self.window._send_request()
        self.assertIs(self.controller.state, ControllerState.CLOSING)
        self.assertEqual(self.commands, ["submit", "close"])
        self.assert_ended_as_failed(APPROVAL_PREFIX + "move")
        self.assertEqual(
            self.messages,
            [
                "A desktop window callback failed: RuntimeError.",
                "The failed desktop window could not show it is closing: "
                "RuntimeError.",
                "A desktop window callback failed: RuntimeError.",
            ],
        )

    def test_the_window_still_closes_when_clearing_it_fails(self) -> None:
        """Closing is asked for, then the session's end is delivered."""
        for member in ("hide_approval", "set_status", "set_controls"):
            with self.subTest(member=member):
                self.open()
                setattr(self.window, member, _broken)
                close_event = QCloseEvent()

                self.window.closeEvent(close_event)

                self.assertFalse(close_event.isAccepted())
                self.assertTrue(self.worker.join(WAIT_SECONDS))
                self.assertIsInstance(self.deliver(), SessionEnded)
                self.assertFalse(self.window.isVisible())
                self.assertIs(self.controller.state, ControllerState.CLOSED)
                self.assertEqual(self.controller.exit_status, 1)
                self.assertTrue(self.window.failed)
                self.assertEqual(self.session.calls, ["end"])
                self.assertEqual(self.commands, ["close"])
                self.assertEqual(
                    self.messages,
                    ["A desktop window callback failed: RuntimeError."] * 2,
                )

    def test_with_nothing_failing_the_same_path_closes_in_order(self) -> None:
        """Control: the harness alone neither fails nor keeps it open."""
        self.open()
        close_event = QCloseEvent()

        self.window.closeEvent(close_event)

        self.assertFalse(close_event.isAccepted())
        self.assertTrue(self.worker.join(WAIT_SECONDS))
        self.assertIsInstance(self.deliver(), SessionEnded)
        self.assertFalse(self.window.isVisible())
        self.assertIs(self.controller.state, ControllerState.CLOSED)
        self.assertEqual(self.controller.exit_status, 0)
        self.assertFalse(self.window.failed)
        self.assertEqual(self.session.calls, ["end"])
        self.assertEqual(self.commands, ["close"])
        self.assertEqual(self.messages, [])


class _StandInNotice:
    """What the worker reads of a claimed reminder notice: its text."""

    def __init__(self, text: str) -> None:
        self.text = text


@unittest.skipIf(UNAVAILABLE, UNAVAILABLE)
class ReminderNoticeWindowTests(unittest.TestCase):
    """A due-reminder notice in the real window (V1-23-R1, decision D12-A).

    As in ``FailedWindowTests``, the window is real but offscreen, with a
    real controller and worker over a stand-in session whose first check
    claims one notice. Events are handed to the window's production slot by
    direct calls, and the one failure is injected by replacing a window
    method, so it never passes through Qt's own dispatch.
    """

    NOTICE_TEXT = 'Reminder 1 is due: "<b>call</b> mom &amp; dad"'

    def setUp(self) -> None:
        self.application = application()
        self.application.setQuitOnLastWindowClosed(False)
        self.addCleanup(self.application.setQuitOnLastWindowClosed, True)
        _quiet_maria_logger(self)
        self.logger, self.messages = capturing_logger("tests.gui.notice")
        patcher = patch.object(
            window_module,
            "get_logger",
            lambda name: self.logger,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.notice = _StandInNotice(self.NOTICE_TEXT)
        self.events: queue.SimpleQueue[WorkerEvent] = queue.SimpleQueue()
        self.session = FakeSession(delivers_reminders=True, notices=(self.notice,))
        self.worker = SessionWorker(lambda: self.session, self.events.put)
        self.commands: list[str] = []
        for name in ("submit", "confirm", "cancel", "acknowledge_reminder_notice"):
            self._record(name)
        self._record("close")
        self.window = MariaWindow()
        self.controller = GuiController(self.window, self.worker)
        self.window.bind(self.controller)
        self.addCleanup(self._shut_down)
        self.controller.start()
        self.window.show()

    def _record(self, name: str) -> None:
        """Record each ``name`` asked of the worker, then pass it on."""
        passed_on = getattr(self.worker, name)

        def recorded(*arguments: object) -> None:
            self.commands.append(name)
            passed_on(*arguments)

        setattr(self.worker, name, recorded)

    def _shut_down(self) -> None:
        """End the worker and discard the window, whatever a test left."""
        self.worker.close()
        self.assertTrue(self.worker.join(2 * WAIT_SECONDS))
        self.window.hide()
        self.window.deleteLater()
        QApplication.processEvents()

    def deliver(self) -> WorkerEvent:
        """Hand the worker's next event to the window's production slot."""
        event = self.events.get(timeout=WAIT_SECONDS)
        self.window._receive(event)
        return event

    def test_a_notice_is_shown_as_plain_text_and_recorded_once_shown(self) -> None:
        self.assertEqual(self.deliver(), DueReminderNotice(1, self.NOTICE_TEXT))

        self.assertTrue(
            self.window.conversation.toPlainText().endswith(
                f"Maria: {self.NOTICE_TEXT}"
            )
        )
        self.assertEqual(self.commands, ["acknowledge_reminder_notice"])
        self.assertIs(self.controller.state, ControllerState.READY)
        self.window._shielded(self.controller.request_close)
        self.assertTrue(self.worker.join(WAIT_SECONDS))
        self.assertIsInstance(self.deliver(), SessionEnded)

        self.assertEqual(self.session.recorded, [(self.notice, True)])
        self.assertEqual(self.controller.exit_status, 0)
        self.assertFalse(self.window.failed)
        self.assertEqual(self.messages, [])

    def test_a_notice_whose_rendering_fails_closes_the_window_unrecorded(
        self,
    ) -> None:
        shown = self.window.append

        def failing(line: str) -> None:
            if line.startswith("Maria: Reminder"):
                raise RuntimeError(FAILURE_TEXT)
            shown(line)

        self.window.append = failing  # type: ignore[method-assign]

        self.assertIsInstance(self.deliver(), DueReminderNotice)

        # The shield logged the failure by type and closed the window as a
        # failure; no acknowledgment was sent, so nothing was recorded.
        self.assertTrue(self.window.failed)
        self.assertEqual(self.commands, ["close"])
        self.assertTrue(self.worker.join(WAIT_SECONDS))
        self.assertIsInstance(self.deliver(), SessionEnded)
        self.assertIs(self.controller.state, ControllerState.CLOSED)
        self.assertEqual(self.controller.exit_status, 1)
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.session.calls, ["claim_due_reminders", "end"])
        self.assertEqual(self.session.recorded, [])
        self.assertEqual(
            self.messages,
            ["A desktop window callback failed: RuntimeError."],
        )
        self.assertNotIn(FAILURE_TEXT, self.window.conversation.toPlainText())


@unittest.skipIf(UNAVAILABLE, UNAVAILABLE)
class RunWindowTests(unittest.TestCase):
    """``run_window`` runs Qt's loop until the window closes in order."""

    def setUp(self) -> None:
        self.application = application()
        self.application.setQuitOnLastWindowClosed(True)
        self.failures: list[str] = []
        _quiet_maria_logger(self)
        self.addCleanup(self._discard_windows)
        # A window that never closes would keep Qt's loop running for ever;
        # this ends the loop and fails the test instead.
        self.watchdog = QTimer()
        self.watchdog.setSingleShot(True)
        self.watchdog.timeout.connect(self._give_up)
        self.watchdog.start(int(WAIT_SECONDS * 1000))
        self.addCleanup(self.watchdog.stop)

    def _give_up(self) -> None:
        """Record that the window never closed, and end Qt's loop."""
        self.failures.append("The window was still open when the wait ran out.")
        QApplication.exit(1)

    def _discard_windows(self) -> None:
        """Hide and delete any window a failed test left showing."""
        for window in self.windows():
            window.hide()
            window.deleteLater()
        QApplication.processEvents()

    def guarded(self, action: Callable[[], None]) -> Callable[[], None]:
        """Wrap a callback for Qt so a failure is recorded, never raised."""

        def run() -> None:
            try:
                action()
            except Exception as error:  # noqa: BLE001 - asserted afterwards.
                self.failures.append(f"{type(error).__name__}: {error}")
                for window in self.windows():
                    window.close()

        return run

    def windows(self) -> list[MariaWindow]:
        """Return every Maria window now showing."""
        return [
            widget
            for widget in QApplication.topLevelWidgets()
            if isinstance(widget, MariaWindow) and widget.isVisible()
        ]

    def drive(self, steps: Callable[[MariaWindow], None]) -> None:
        """Run steps against the window once Qt's loop has started."""

        def start() -> None:
            (window,) = self.windows()
            steps(window)

        QTimer.singleShot(0, self.guarded(start))

    def close_once(self, window: MariaWindow, done: Callable[[], bool]) -> None:
        """Close the window as soon as ``done`` holds, checking as Qt runs."""

        def check() -> None:
            if done():
                window.close()
            else:
                QTimer.singleShot(10, self.guarded(check))

        QTimer.singleShot(10, self.guarded(check))

    def test_run_window_runs_until_closed_and_restores_ctrl_c(self) -> None:
        session = FakeSession()
        before = signal.getsignal(signal.SIGINT)
        during: list[object] = []

        def steps(window: MariaWindow) -> None:
            during.append(signal.getsignal(signal.SIGINT))
            QTest.keyClicks(window.request_input, "hello")
            QTest.keyClick(window.request_input, Qt.Key.Key_Return)
            self.close_once(
                window,
                lambda: window.conversation.toPlainText().endswith(
                    f"Maria: {reply_text('hello')}"
                ),
            )

        self.drive(steps)
        status = run_window(lambda: session)

        self.assertEqual(self.failures, [])
        self.assertEqual(status, 0)
        self.assertEqual(during, [signal.SIG_IGN])
        self.assertEqual(signal.getsignal(signal.SIGINT), before)
        self.assertEqual(session.calls, ["submit", "end"])
        self.assertFalse(
            any(thread.name == THREAD_NAME for thread in threading.enumerate())
        )
        self.assertEqual(self.windows(), [])

    def test_the_session_ends_in_order_when_qts_loop_stops_while_busy(
        self,
    ) -> None:
        """However Qt's loop stops, the session ends in order and is shown."""
        gate = threading.Event()
        session = FakeSession(gate=gate)
        statuses: list[str] = []

        def steps(window: MariaWindow) -> None:
            set_status = window.set_status

            def recording_status(text: str) -> None:
                statuses.append(text)
                set_status(text)

            window.set_status = recording_status
            QTest.keyClicks(window.request_input, APPROVAL_PREFIX + "slow move")
            QTest.keyClick(window.request_input, Qt.Key.Key_Return)
            self.after(session.started_request.is_set, stop_loop)

        def stop_loop() -> None:
            # The request is released only after Qt's loop has stopped, so
            # its end, and the session's, are seen outside that loop.
            threading.Timer(0.3, gate.set).start()
            QApplication.exit(0)

        self.drive(steps)
        status = run_window(lambda: session)
        gate.set()

        self.assertEqual(self.failures, [])
        self.assertEqual(status, 0)
        self.assertIn(STATUS_CLOSING_BUSY, statuses)
        self.assertEqual(session.calls, ["submit", "end"])
        self.assertEqual(self.windows(), [])
        self.assertFalse(
            any(thread.name == THREAD_NAME for thread in threading.enumerate())
        )

    def test_run_window_reports_a_session_that_ends_unexpectedly(self) -> None:
        session = FakeSession(failure=RuntimeError("private-window-failure"))

        def steps(window: MariaWindow) -> None:
            QTest.keyClicks(window.request_input, "fail now")
            QTest.keyClick(window.request_input, Qt.Key.Key_Return)

        self.drive(steps)
        status = run_window(lambda: session)

        self.assertEqual(self.failures, [])
        self.assertEqual(status, 1)
        self.assertEqual(session.calls, ["submit", "end"])
        self.assertEqual(self.windows(), [])

    def test_a_real_session_over_a_temporary_world_round_trips(self) -> None:
        from tests.test_brain_recording_policy import _quiet_logger
        from tests.test_conversation import _Harness

        harness = _Harness(self, logger=_quiet_logger("tests.gui.window"))

        def answered(window: MariaWindow, text: str) -> Callable[[], bool]:
            return lambda: window.conversation.toPlainText().endswith(text)

        def steps(window: MariaWindow) -> None:
            QTest.keyClicks(window.request_input, "create note Groceries: milk")
            QTest.keyClick(window.request_input, Qt.Key.Key_Return)
            self.after(
                answered(window, "Maria: Created note 1: Groceries"),
                lambda: self.delete_note(window),
            )

        self.drive(steps)
        status = run_window(lambda: MariaSession(harness.brain))

        self.assertEqual(self.failures, [])
        self.assertEqual(status, 0)
        with closing(sqlite3.connect(harness.database_path)) as connection:
            notes = connection.execute("SELECT COUNT(*) FROM notes").fetchone()
        self.assertEqual(notes, (0,))

    def after(self, done: Callable[[], bool], then: Callable[[], None]) -> None:
        """Run ``then`` once ``done`` holds, checking as Qt runs."""

        def check() -> None:
            if done():
                then()
            else:
                QTimer.singleShot(10, self.guarded(check))

        QTimer.singleShot(10, self.guarded(check))

    def delete_note(self, window: MariaWindow) -> None:
        """Ask to delete the note, approve the offer, then close."""
        QTest.keyClicks(window.request_input, "delete note 1")
        QTest.keyClick(window.request_input, Qt.Key.Key_Return)

        def approve() -> None:
            if not window.approval_text.toPlainText().startswith(
                "Confirmation required: notes.delete"
            ):
                raise AssertionError("The offer did not show the core text.")
            QTest.mouseClick(window.approve_button, Qt.MouseButton.LeftButton)
            self.close_once(
                window,
                lambda: window.conversation.toPlainText().endswith(
                    "Maria: Deleted note 1."
                ),
            )

        self.after(lambda: window.approval.isVisible(), approve)

    def test_the_window_then_the_cli_keep_logging_in_one_process(
        self,
    ) -> None:
        from core.brain import MariaBrain
        from core.reasoning import DISABLED_REASONING
        from tests.test_conversation import _test_config

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        maria = logging.getLogger("maria")
        saved = (tuple(maria.handlers), maria.level, maria.propagate)
        for handler in saved[0]:
            maria.removeHandler(handler)

        def restore() -> None:
            for handler in list(maria.handlers):
                maria.removeHandler(handler)
                handler.close()
            for handler in saved[0]:
                maria.addHandler(handler)
            maria.setLevel(saved[1])
            maria.propagate = saved[2]

        self.addCleanup(restore)

        def brain() -> MariaBrain:
            config = _test_config(root, DISABLED_REASONING)
            with patch("core.brain.load_config", return_value=config):
                return MariaBrain.from_defaults()

        def steps(window: MariaWindow) -> None:
            QTest.keyClicks(window.request_input, "calculate 4 + 4")
            QTest.keyClick(window.request_input, Qt.Key.Key_Return)
            self.close_once(
                window,
                lambda: window.conversation.toPlainText().endswith(
                    "Maria: 4 + 4 = 8"
                ),
            )

        self.drive(steps)
        window_brain = brain()
        self.assertEqual(run_window(lambda: MariaSession(window_brain)), 0)
        with (
            patch("builtins.input", side_effect=["calculate 5 + 5", "exit"]),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            self.assertEqual(main_module.run_cli(brain), 0)

        for handler in maria.handlers:
            handler.flush()
        log = (root / "logs" / "maria.log").read_text(encoding="utf-8")
        self.assertEqual(self.failures, [])
        self.assertEqual(log.count("Desktop window opened."), 1)
        self.assertEqual(log.count("Desktop window closed."), 1)
        self.assertEqual(log.count("GUI session started."), 1)
        self.assertEqual(log.count("Received user request."), 2)
        self.assertEqual(log.count("Session ended."), 2)
        self.assertEqual(len(maria.handlers), 1)


class _StandInSignal:
    """Queue what the worker emits, for the stand-in application to deliver.

    An event of a kind in ``refused`` makes ``emit`` raise instead, as a
    delivery to a window that is already gone would.
    """

    refused: tuple[type, ...] = ()

    def __init__(self) -> None:
        self.pending: queue.SimpleQueue[object] = queue.SimpleQueue()

    def emit(self, event: object) -> None:
        """Queue one event, or refuse it."""
        if isinstance(event, _StandInSignal.refused):
            raise RuntimeError(FAILURE_TEXT)
        self.pending.put(event)


class _StandInInput:
    """The one-line input, as far as the real Send callback reads it."""

    def __init__(self) -> None:
        self.typed = ""

    def text(self) -> str:
        """Return what was typed."""
        return self.typed


class _StandInWindow:
    """A plain-Python window that shows nothing and can fail on request.

    It never touches Qt, so a failure injected here can never be raised
    through a real Qt callback. ``hide_failure``, when set, is raised by
    every ``hide_approval()``, which the controller calls when closing and
    once the session has ended, or by the first one only when
    ``hide_failure_once`` is set; ``controls_failure``, when set, is raised
    by the next ``set_controls()`` only. Like the real window, it keeps the
    offer it shows until that is hidden, and ``closed`` counts the attempts
    to close it. ``failed`` is the real window's mark of a callback that
    failed; the tests lend it the real window's event slot, callbacks, and
    shield, which keep the controller as ``_controller``.
    """

    created: list[_StandInWindow] = []
    hide_failure: BaseException | None = None
    hide_failure_once = False
    controls_failure: BaseException | None = None

    def __init__(self) -> None:
        self.event_delivered = _StandInSignal()
        self._controller: GuiController | None = None
        self._offer_serial: int | None = None
        self.request_input = _StandInInput()
        self.failed = False
        self.closed = 0
        _StandInWindow.created.append(self)

    def bind(self, controller: GuiController) -> None:
        """Keep the controller, as the real window does."""
        self._controller = controller

    def show(self) -> None:
        """Show nothing."""

    def append(self, line: str) -> None:
        """Show nothing."""

    def set_status(self, text: str) -> None:
        """Show nothing."""

    def show_approval(self, serial: int, text: str) -> None:
        """Keep the offer, as the real window does."""
        self._offer_serial = serial

    def hide_approval(self) -> None:
        """Forget the offer, or fail on request, as a broken window might."""
        failure = _StandInWindow.hide_failure
        if failure is not None:
            if _StandInWindow.hide_failure_once:
                _StandInWindow.hide_failure = None
            raise failure
        self._offer_serial = None

    def set_controls(self, **controls: bool) -> None:
        """Show nothing, or fail once on request."""
        failure = _StandInWindow.controls_failure
        if failure is not None:
            _StandInWindow.controls_failure = None
            raise failure

    def clear_input(self) -> None:
        """Show nothing."""

    def show_projects(self, items: tuple[str, ...]) -> None:
        """Show nothing (V1-24-R1's project selector)."""

    def set_project_controls(self, *, activating: bool, deactivating: bool) -> None:
        """Show nothing (V1-24-R1's project selector)."""

    def close_window(self) -> None:
        """Count the attempt; there is nothing to close."""
        self.closed += 1


class _StandInApplication:
    """A plain-Python application whose loop ends while a request runs.

    ``exec()`` sends one request through the bound controller, waits until
    the session is inside it, and returns, so Qt's loop has ended while the
    worker is still busy; only then does it start ``release_later`` on a
    0.3-second timer. ``processEvents()`` hands every pending worker event
    to the window's event slot, the real window's own code with its shield,
    as Qt's queued delivery does; only what that slot lets escape surfaces
    from ``processEvents()``, where real Qt would end the process instead.
    ``events_failure``, when set, is raised by every ``processEvents()``
    before anything is delivered, and ``callback_failure`` makes a callback
    fail through the window's real shield while the loop runs. A test that
    sets ``during_loop`` runs that instead, with the window, as the whole of
    Qt's loop.
    """

    current: _StandInApplication | None = None

    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.events_failure: BaseException | None = None
        self.callback_failure = False
        self.during_loop: Callable[[_StandInWindow], None] | None = None
        self.event_rounds = 0
        self.release_later: Callable[[], None] = lambda: None
        self.release_timer: threading.Timer | None = None
        self.loop_ended_at: float | None = None
        self.busy_when_loop_ended = False

    @classmethod
    def instance(cls) -> _StandInApplication | None:
        """Return the stand-in application of the running test."""
        return cls.current

    def exec(self) -> int:
        """End the loop with a request running, and release it only later."""
        window = _StandInWindow.created[-1]
        if self.during_loop is not None:
            self.during_loop(window)
            self.loop_ended_at = time.monotonic()
            return 0
        window._controller.send(APPROVAL_PREFIX + "a slow move")
        self.busy_when_loop_ended = self.session.started_request.wait(
            WAIT_SECONDS
        )
        if self.callback_failure:
            window._shielded(_broken)
        self.loop_ended_at = time.monotonic()
        self.release_timer = threading.Timer(0.3, self.release_later)
        self.release_timer.start()
        return 0

    def processEvents(self) -> None:
        """Deliver every pending worker event, or fail on request."""
        self.event_rounds += 1
        if self.events_failure is not None:
            raise self.events_failure
        window = _StandInWindow.created[-1]
        while True:
            try:
                event = window.event_delivered.pending.get_nowait()
            except queue.Empty:
                return
            window._receive(event)


class _FailingStream:
    """A standard error that fails to write or to flush."""

    def __init__(self, failing: str) -> None:
        self.failing = failing
        self.written: list[str] = []

    def write(self, text: str) -> int:
        """Fail, or keep the text."""
        if self.failing == "write":
            raise OSError("private-stream-detail")
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        """Fail, or do nothing."""
        if self.failing == "flush":
            raise OSError("private-stream-detail")


class _StandInConstruction:
    """Stands in for QApplication when no application object exists yet."""

    failure: BaseException | None = None

    @staticmethod
    def instance() -> None:
        """Report that no application object exists."""
        return None

    def __init__(self, arguments: list[str]) -> None:
        if _StandInConstruction.failure is not None:
            raise _StandInConstruction.failure


@unittest.skipIf(UNAVAILABLE, UNAVAILABLE)
class RunWindowCleanupTests(unittest.TestCase):
    """Closing survives a failing window, event delivery, or session end.

    The window and Qt's application are plain-Python stand-ins, so no
    failure injected here is ever raised through a real Qt callback. In
    most tests Qt's loop ends while a request is still running, and that
    request is released only afterwards, so each proves the launcher waits
    for the worker, attempts the session's end, and restores Ctrl+C. The
    tests of a window that fails while the loop runs script the loop
    instead, with a session that answers at once. Everything the window
    asks of the worker is recorded. Should the launcher ever stop asking its
    worker to end, a rescue ends the worker after ``WAIT_SECONDS``, so the
    test fails instead of hanging.
    """

    def setUp(self) -> None:
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)
        self.session = FakeSession(gate=self.gate)
        self.application = _StandInApplication(self.session)
        self.logger, self.messages = capturing_logger("tests.gui.cleanup")
        self.workers: list[SessionWorker] = []
        self.commands: list[str] = []
        self._reset_stand_ins()
        self.addCleanup(self._reset_stand_ins)
        _StandInApplication.current = self.application
        for name, value in (
            ("QApplication", _StandInApplication),
            ("MariaWindow", _StandInWindow),
            ("SessionWorker", self.recorded_worker),
            ("get_logger", lambda name: self.logger),
        ):
            patcher = patch.object(window_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The stand-in receives events, and answers, through the real
        # window's own slot, callbacks, and shield, so what they contain is
        # what these tests exercise.
        for name in (
            "_receive",
            "_send_request",
            "_approve_offer",
            "_cancel_offer",
            "_shielded",
            "_close_after_failure",
        ):
            patcher = patch.object(
                _StandInWindow,
                name,
                getattr(MariaWindow, name),
                create=True,
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _reset_stand_ins() -> None:
        """Forget the stand-ins' shared state, before and after each test."""
        _StandInWindow.created = []
        _StandInWindow.hide_failure = None
        _StandInWindow.hide_failure_once = False
        _StandInWindow.controls_failure = None
        _StandInSignal.refused = ()
        _StandInApplication.current = None

    def recorded_worker(self, *arguments, **keywords) -> SessionWorker:
        """Create the launcher's worker, kept so that a rescue can end it.

        It logs to the test's own logger, so its lines are checked as well,
        and everything asked of it is recorded before it is passed on.
        """
        worker = SessionWorker(*arguments, logger=self.logger, **keywords)
        for name in ("submit", "confirm", "cancel", "close"):
            passed_on = getattr(worker, name)

            def recorded(
                *arguments: object,
                name: str = name,
                passed_on: Callable[..., None] = passed_on,
            ) -> None:
                self.commands.append(name)
                passed_on(*arguments)

            setattr(worker, name, recorded)
        self.workers.append(worker)
        return worker

    def use_session(self, session: FakeSession) -> None:
        """Run the window over another session held by the same gate."""
        self.session = session
        self.application.session = session

    def run_window_late(self) -> dict[str, object]:
        """Run the window; its request is released only after the loop ends."""
        released: list[float] = []

        def release() -> None:
            released.append(time.monotonic())
            self.gate.set()

        self.application.release_later = release
        outcome = self.run_observed()
        outcome["released_at"] = released[0] if released else None
        return outcome

    def run_observed(self) -> dict[str, object]:
        """Run the window, and record how the launcher ended."""
        rescued: list[bool] = []

        def rescue() -> None:
            rescued.append(True)
            for worker in self.workers:
                worker.close()

        before = signal.getsignal(signal.SIGINT)
        rescuer = threading.Timer(WAIT_SECONDS, rescue)
        outcome: dict[str, object] = {"raised": None, "status": None}
        with thread_failures_observed() as (hook_calls, captured):
            rescuer.start()
            try:
                outcome["status"] = run_window(lambda: self.session)
            except BaseException as error:  # noqa: BLE001 - asserted below.
                outcome["raised"] = type(error).__name__
            outcome["returned_at"] = time.monotonic()
            rescuer.cancel()
            outcome["worker_alive"] = any(
                thread.name == THREAD_NAME and thread.is_alive()
                for thread in threading.enumerate()
            )
            outcome["sigint_restored"] = signal.getsignal(signal.SIGINT) is before
        if self.application.release_timer is not None:
            self.application.release_timer.join(WAIT_SECONDS)
        rescuer.join(WAIT_SECONDS)
        signal.signal(signal.SIGINT, before)
        outcome["rescued"] = bool(rescued)
        outcome["hook_calls"] = list(hook_calls)
        outcome["stderr"] = captured.getvalue()
        return outcome

    def assert_waited_and_restored(self, outcome: dict[str, object]) -> None:
        """Assert the launcher waited for the worker and put Ctrl+C back.

        The wait proves something only because Qt's loop ended while the
        request was running, and the request was released only afterwards.
        """
        self.assertTrue(self.application.busy_when_loop_ended)
        self.assertIsNotNone(outcome["released_at"])
        self.assertLess(self.application.loop_ended_at, outcome["released_at"])
        self.assertGreaterEqual(outcome["returned_at"], outcome["released_at"])
        self.assertEqual(self.session.calls, ["submit", "end"])
        self.assert_ended_and_restored(outcome)

    def assert_ended_and_restored(self, outcome: dict[str, object]) -> None:
        """Assert the worker stopped, Ctrl+C came back, and nothing leaked."""
        self.assertFalse(outcome["rescued"])
        self.assertFalse(outcome["worker_alive"])
        self.assertTrue(outcome["sigint_restored"])
        self.assertEqual(outcome["hook_calls"], [])
        self.assertEqual(outcome["stderr"], "")
        for message in self.messages:
            self.assertNotIn(FAILURE_TEXT, message)

    def window_failures(self) -> list[str]:
        """Return what the window logged about its own failures."""
        return [line for line in self.messages if "desktop window" in line]

    def controller(self) -> GuiController:
        """Return the controller the launcher created."""
        return _StandInWindow.created[-1]._controller

    def test_an_orderly_close_with_stand_ins_reports_success(self) -> None:
        """Control: with nothing failing, the same harness reports 0."""
        outcome = self.run_window_late()

        self.assert_waited_and_restored(outcome)
        self.assertEqual(outcome["status"], 0)
        self.assertIsNone(outcome["raised"])
        self.assertIs(self.controller().state, ControllerState.CLOSED)

    def test_a_session_that_cannot_end_cleanly_reports_failure(self) -> None:
        self.use_session(
            FakeSession(gate=self.gate, end_failure=RuntimeError(FAILURE_TEXT))
        )

        outcome = self.run_window_late()

        self.assert_waited_and_restored(outcome)
        self.assertEqual(outcome["status"], 1)
        self.assertIsNone(outcome["raised"])
        self.assertIs(self.controller().state, ControllerState.CLOSED)
        self.assertIn(
            "The GUI session did not end cleanly: unexpected RuntimeError.",
            self.messages,
        )

    def test_a_session_end_the_window_never_sees_is_no_success(self) -> None:
        """The worker ended in order, but its last event never arrived."""
        _StandInSignal.refused = (SessionEnded,)

        outcome = self.run_window_late()

        self.assert_waited_and_restored(outcome)
        self.assertEqual(outcome["status"], 1)
        self.assertIsNone(outcome["raised"])
        self.assertIs(self.controller().state, ControllerState.CLOSING)
        self.assertIn("Could not deliver a GUI event.", self.messages)

    def test_a_window_callback_that_failed_is_reported(self) -> None:
        """Everything else closes in order, yet the window is reported."""
        self.application.callback_failure = True

        outcome = self.run_window_late()

        self.assert_waited_and_restored(outcome)
        self.assertEqual(outcome["status"], 1)
        self.assertIsNone(outcome["raised"])
        self.assertIs(self.controller().state, ControllerState.CLOSED)
        self.assertEqual(self.controller().exit_status, 1)
        self.assertTrue(_StandInWindow.created[-1].failed)
        self.assertEqual(_StandInWindow.created[-1].closed, 1)

    def run_failing_delivery(
        self,
        request: str,
        answers: tuple[str, ...],
        *,
        hide_failure: bool = False,
    ) -> list[object]:
        """Run the window while it fails showing an event; then use it.

        Inside Qt's loop: Send the request, let the window fail late while
        showing the worker's answer, then call the window's own callbacks
        named in ``answers``, in order. Returns the offer serial the window
        still held when they were called.
        """
        self.use_session(FakeSession())
        self.request = request
        held: list[object] = []

        def during_loop(window: _StandInWindow) -> None:
            window.request_input.typed = request
            window._send_request()
            pending = window.event_delivered.pending
            deadline = time.monotonic() + WAIT_SECONDS
            while pending.empty() and time.monotonic() < deadline:
                time.sleep(0.005)
            _StandInWindow.controls_failure = RuntimeError(FAILURE_TEXT)
            if hide_failure:
                _StandInWindow.hide_failure = RuntimeError(FAILURE_TEXT)
            self.application.processEvents()
            held.append(window._offer_serial)
            window.request_input.typed = "another request"
            callbacks = {
                "approve": window._approve_offer,
                "cancel": window._cancel_offer,
                "send": window._send_request,
            }
            for answer in answers:
                callbacks[answer]()

        self.application.during_loop = during_loop
        self.outcome = self.run_observed()
        return held

    def assert_failed_without_answering(self) -> None:
        """Assert the session ended once, unanswered, and the window closed.

        The worker was asked for the first request and for the session's
        end, by the failed window and again by the launcher, and for
        nothing else.
        """
        self.assert_ended_and_restored(self.outcome)
        self.assertEqual(self.outcome["status"], 1)
        self.assertIsNone(self.outcome["raised"])
        self.assertEqual(self.commands, ["submit", "close", "close"])
        self.assertEqual(self.session.calls, ["submit", "end"])
        self.assertEqual(self.session.requests, [self.request])
        self.assertIsNone(self.session.pending_approval)
        self.assertTrue(_StandInWindow.created[-1].failed)
        self.assertEqual(_StandInWindow.created[-1].closed, 1)
        self.assertIs(self.controller().state, ControllerState.CLOSED)
        self.assertEqual(self.controller().exit_status, 1)

    def test_a_window_that_fails_showing_an_offer_approves_nothing(
        self,
    ) -> None:
        held = self.run_failing_delivery(
            APPROVAL_PREFIX + "a move",
            ("approve", "cancel", "send"),
        )

        self.assert_failed_without_answering()
        self.assertEqual(held, [None])
        self.assertEqual(
            self.window_failures(),
            ["A desktop window callback failed: RuntimeError."],
        )

    def test_a_window_that_fails_showing_an_offer_cancels_nothing(
        self,
    ) -> None:
        held = self.run_failing_delivery(
            APPROVAL_PREFIX + "a move",
            ("cancel", "approve", "send"),
        )

        self.assert_failed_without_answering()
        self.assertEqual(held, [None])

    def test_a_window_that_fails_showing_a_reply_sends_nothing(self) -> None:
        """The window was ready for another request; only failing stops it."""
        held = self.run_failing_delivery("hello", ("send", "send"))

        self.assert_failed_without_answering()
        self.assertEqual(held, [None])
        self.assertEqual(
            self.window_failures(),
            ["A desktop window callback failed: RuntimeError."],
        )

    def test_a_window_still_holding_its_offer_cannot_answer_it(self) -> None:
        """Too broken to hide the offer, yet the controller refuses it."""
        held = self.run_failing_delivery(
            APPROVAL_PREFIX + "a move",
            ("approve", "cancel", "send"),
            hide_failure=True,
        )

        self.assert_failed_without_answering()
        self.assertEqual(held, [1])
        self.assertEqual(
            self.window_failures(),
            [
                "A desktop window callback failed: RuntimeError.",
                "The failed desktop window could not show it is closing: "
                "RuntimeError.",
                "A desktop window callback failed: RuntimeError.",
            ],
        )

    def test_the_window_is_still_closed_when_clearing_it_fails(self) -> None:
        """Closing fails to clear the window, and so does the session's end."""
        _StandInWindow.hide_failure = RuntimeError(FAILURE_TEXT)

        outcome = self.run_window_late()

        self.assert_waited_and_restored(outcome)
        self.assertEqual(outcome["status"], 1)
        self.assertIsNone(outcome["raised"])
        self.assertIs(self.controller().state, ControllerState.CLOSED)
        self.assertEqual(self.controller().exit_status, 1)
        self.assertEqual(_StandInWindow.created[-1].closed, 1)

    def test_a_failing_close_request_still_ends_and_awaits_the_worker(
        self,
    ) -> None:
        _StandInWindow.hide_failure = RuntimeError(FAILURE_TEXT)

        outcome = self.run_window_late()

        self.assert_waited_and_restored(outcome)
        self.assertEqual(outcome["status"], 1)
        self.assertIsNone(outcome["raised"])
        self.assertIn(
            "The desktop window could not close in order: RuntimeError.",
            self.messages,
        )
        # The session's end then reached the same failing window through
        # the event slot, whose shield kept the failure out of Qt.
        self.assertIn(
            "A desktop window callback failed: RuntimeError.",
            self.messages,
        )
        self.assertTrue(_StandInWindow.created[-1].failed)
        self.assertNotIn(
            "Events could not be delivered while closing: RuntimeError.",
            self.messages,
        )

    def test_a_close_request_that_failed_once_is_still_reported(
        self,
    ) -> None:
        """The session then ends in order and is seen, yet closing failed."""
        _StandInWindow.hide_failure = RuntimeError(FAILURE_TEXT)
        _StandInWindow.hide_failure_once = True

        outcome = self.run_window_late()

        self.assert_waited_and_restored(outcome)
        self.assertEqual(outcome["status"], 1)
        self.assertIsNone(outcome["raised"])
        self.assertIs(self.controller().state, ControllerState.CLOSED)
        self.assertEqual(self.controller().exit_status, 0)
        self.assertFalse(_StandInWindow.created[-1].failed)
        self.assertIn(
            "The desktop window could not close in order: RuntimeError.",
            self.messages,
        )

    def test_a_close_request_failing_outright_still_ends_the_session(
        self,
    ) -> None:
        """The launcher asks the worker to end itself, then waits for it."""
        with patch.object(
            GuiController,
            "request_close",
            side_effect=RuntimeError(FAILURE_TEXT),
        ):
            outcome = self.run_window_late()

        self.assert_waited_and_restored(outcome)
        self.assertEqual(outcome["status"], 1)
        self.assertIsNone(outcome["raised"])
        self.assertIn(
            "The desktop window could not close in order: RuntimeError.",
            self.messages,
        )

    def test_failing_event_delivery_still_awaits_the_worker(self) -> None:
        self.application.events_failure = RuntimeError(FAILURE_TEXT)

        outcome = self.run_window_late()

        self.assert_waited_and_restored(outcome)
        self.assertEqual(outcome["status"], 1)
        self.assertEqual(self.application.event_rounds, 1)
        self.assertIn(
            "Events could not be delivered while closing: RuntimeError.",
            self.messages,
        )

    def test_a_failing_close_request_and_delivery_still_await_the_worker(
        self,
    ) -> None:
        _StandInWindow.hide_failure = ValueError(FAILURE_TEXT)
        self.application.events_failure = OSError(FAILURE_TEXT)

        outcome = self.run_window_late()

        self.assert_waited_and_restored(outcome)
        self.assertEqual(outcome["status"], 1)

    def test_a_failure_beyond_exception_still_waits_and_restores_ctrl_c(
        self,
    ) -> None:
        """Even what no handler catches cannot skip the wait or the restore."""
        _StandInWindow.hide_failure = SystemExit(FAILURE_TEXT)

        outcome = self.run_window_late()

        self.assert_waited_and_restored(outcome)
        self.assertEqual(outcome["raised"], "SystemExit")

    def test_an_interrupt_while_delivering_events_still_waits_and_restores(
        self,
    ) -> None:
        self.application.events_failure = KeyboardInterrupt(FAILURE_TEXT)

        outcome = self.run_window_late()

        self.assert_waited_and_restored(outcome)
        self.assertEqual(outcome["raised"], "KeyboardInterrupt")
        self.assertEqual(self.application.event_rounds, 1)


@unittest.skipIf(UNAVAILABLE, UNAVAILABLE)
class PrepareApplicationTests(unittest.TestCase):
    """Qt's application object comes first, and a fatal start is fixed text.

    What needs a fresh process runs in a child on the offscreen platform,
    which imports the window module and creates nothing else. No test
    forces a platform that cannot start, because on Windows that shows Qt's
    own modal error dialog on the desktop.
    """

    def _child(
        self,
        platform: str,
        script: str,
        *,
        debug_plugins: bool = False,
    ) -> subprocess.CompletedProcess:
        """Run a script in a child Python on the named Qt platform.

        ``debug_plugins`` makes Qt log as it unloads its plugins at exit,
        which crashes a process that still has a Python message handler.
        """
        environment = dict(os.environ, QT_QPA_PLATFORM=platform)
        if debug_plugins:
            environment["QT_DEBUG_PLUGINS"] = "1"
        return subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path(window_module.__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=6 * WAIT_SECONDS,
            check=False,
        )

    def test_an_existing_application_is_returned_untouched(self) -> None:
        existing = application()

        with patch.object(window_module, "qInstallMessageHandler") as install:
            self.assertIs(prepare_application("Maria: fixed", 1), existing)

        install.assert_not_called()

    def test_a_fatal_qt_message_becomes_the_fixed_message_and_status(
        self,
    ) -> None:
        """The startup handler, called directly with each kind of message.

        A real fatal start is never forced here: on Windows, Qt answers a
        platform that cannot start, in a process without a console, with a
        modal error dialog on the desktop before anything else happens.
        """
        handler = window_module._startup_messages(
            "Maria: fixed start message",
            7,
        )
        stderr = StringIO()

        with (
            patch.object(os, "_exit", side_effect=SystemExit) as exit_now,
            redirect_stderr(stderr),
        ):
            handler(QtMsgType.QtDebugMsg, None, "Qt's own debug text")
            handler(QtMsgType.QtWarningMsg, None, "a Qt warning naming C:/x")
            exit_now.assert_not_called()
            with self.assertRaises(SystemExit):
                handler(QtMsgType.QtFatalMsg, None, "Qt's own fatal text")

        exit_now.assert_called_once_with(7)
        self.assertEqual(stderr.getvalue(), "Maria: fixed start message\n")

    def test_the_handler_installed_before_is_put_back_exactly(self) -> None:
        """Whether creating the application succeeds or raises.

        The real ``qInstallMessageHandler`` is used; only the application's
        construction is a stand-in, so no Qt platform is started here.
        """
        for failure in (None, RuntimeError(FAILURE_TEXT)):
            with self.subTest(construction="raises" if failure else "succeeds"):

                def existing(kind: QtMsgType, context: object, text: str) -> None:
                    """A handler someone installed before Maria started."""

                _StandInConstruction.failure = failure
                with (
                    patch.object(
                        window_module,
                        "QApplication",
                        _StandInConstruction,
                    ),
                    patch.object(window_module, "_APPLICATION", []),
                ):
                    outer = qInstallMessageHandler(existing)
                    try:
                        if failure is None:
                            created = prepare_application("Maria: fixed", 7)
                            self.assertIsInstance(created, _StandInConstruction)
                        else:
                            with self.assertRaises(RuntimeError):
                                prepare_application("Maria: fixed", 7)
                    finally:
                        # Whatever the run had installed goes back as well.
                        restored = qInstallMessageHandler(outer)
                        _StandInConstruction.failure = None

                self.assertIs(restored, existing)

    def test_a_fatal_message_exits_even_without_a_usable_stderr(self) -> None:
        """A missing or failing stream never stops the configured exit."""
        cases = {
            "no standard error": (None, None),
            "a write that fails": (_FailingStream("write"), []),
            "a flush that fails": (
                _FailingStream("flush"),
                ["Maria: fixed start message\n"],
            ),
        }
        for case, (stream, written) in cases.items():
            with self.subTest(case=case):
                handler = window_module._startup_messages(
                    "Maria: fixed start message",
                    7,
                )

                with (
                    patch.object(os, "_exit", side_effect=SystemExit) as exit_now,
                    patch.object(sys, "stderr", stream),
                    self.assertRaises(SystemExit),
                ):
                    handler(QtMsgType.QtFatalMsg, None, "Qt's own fatal text")

                exit_now.assert_called_once_with(7)
                if stream is not None:
                    self.assertEqual(stream.written, written)

    def test_qts_own_message_handling_returns_once_the_start_succeeds(
        self,
    ) -> None:
        completed = self._child(
            "offscreen",
            "import sys\n"
            "from PyQt6.QtCore import qInstallMessageHandler\n"
            "from gui.window import prepare_application\n"
            "application = prepare_application('Maria: fixed', 7)\n"
            "previous = qInstallMessageHandler(None)\n"
            "sys.stdout.write('restored' if previous is None else 'kept')\n",
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "restored")

    def test_a_prepared_process_still_exits_cleanly_while_qt_logs(
        self,
    ) -> None:
        """No startup handler is left for Qt to call during shutdown.

        A Python message handler still installed when the interpreter shuts
        down crashes the process as Qt logs its plugins away, so a clean exit
        with Qt logging at full volume proves the handler went with startup.
        """
        completed = self._child(
            "offscreen",
            "import sys\n"
            "from gui.window import prepare_application\n"
            "application = prepare_application('Maria: fixed', 7)\n"
            "sys.stdout.write('prepared')\n",
            debug_plugins=True,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "prepared")


if __name__ == "__main__":
    unittest.main()
