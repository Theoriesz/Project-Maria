"""Maria's desktop window, drawn with Qt Widgets.

This is the only production module that imports PyQt6, under the owner's
amendment V1-21-R1. The window is presentation only: ``GuiController``
decides what it shows and which of the owner's actions it accepts, and the
one ``SessionWorker`` owns the session. Widgets are touched only on the GUI
thread; the worker's events reach the window through a queued Qt signal.

Everything shown is plain text. The conversation and approval displays are
read-only, cannot be selected or copied from, have no context menu, and never
interpret text as HTML, open a link, or load a referenced file; the status
line is plain text as well. Maria writes nothing to the clipboard. The input
is one line that keeps every typed or pasted character for the controller to
judge, with no length cap that could silently cut a long request short.
Approve and Cancel never take keyboard focus, so a keystroke meant for the
input can never answer an approval. The window title never changes.

The project selector (V1-24-R1) lists plain text items the controller
chooses. Its Activate and Deactivate buttons never take keyboard focus, and
choosing an item, scrolling the list, or typing activates nothing: only a
click on Activate or Deactivate submits a command, through the controller,
exactly as Send does.

``prepare_application`` creates Qt's application object before anything else
starts, because that is where Qt checks whether it can show a window at all,
and a failure there is fatal inside Qt rather than an exception. While it is
created, Qt's own messages are withheld, since they can name host paths, and
a fatal one becomes the caller's fixed message and exit status instead, even
without a usable standard error. The message handler installed before is put
back exactly afterwards.

``run_window`` shows the window until it closes. The window closes only once
the session has ended, and the launcher waits for the worker before it
returns, keeping the window painted and its last events delivered however
Qt's loop ended. Closing waits for the current operation and has no general
GUI-wide deadline. Provider deadlines apply where configured, but a stalled
operation can delay closing indefinitely, since a running operation is never
interrupted. If asking the window to close, or delivering events, raises an
``Exception``, the launcher logs its type, still asks the worker to end,
waits for it, restores Ctrl+C, and returns 1; anything else raised there
propagates only after the same request, wait, and restore. While the window
is open, Ctrl+C in the launching terminal is ignored: an interrupt raised
inside a Qt callback would end the process abruptly, whereas closing the
window ends the session in order.

Qt ends the whole process when a callback it runs raises, which would skip
the session's orderly end. Every callback the window hands to Qt therefore
makes its controller call through a shield. A failure is logged by its
exception type alone and marks the window as failed, and the controller then
closes it as a failure: no later Send, Approve, or Cancel is accepted, queued
ones included, and the worker is asked to end the session before the window
is touched again. The session's end is still delivered, the window is still
asked to close even if clearing it fails, and the launcher reports a failed
window with status 1.
"""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Callable
from logging import Logger

from PyQt6.QtCore import (
    Qt,
    QtMsgType,
    pyqtSignal,
    pyqtSlot,
    qInstallMessageHandler,
)
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.constants import APP_NAME
from core.logger import get_logger
from core.session import MariaSession
from gui.controller import (
    ACTIVATE_LABEL,
    DEACTIVATE_LABEL,
    PROJECT_LABEL,
    GuiController,
)
from gui.session_worker import SessionWorker, WorkerEvent


# Qt's largest text length. The input's default cap of 32,767 characters
# would silently cut a longer request short, which the command line never
# does.
INPUT_MAXIMUM_LENGTH = 2**31 - 1

APPROVAL_TITLE = "Approval required"
INPUT_PLACEHOLDER = "Type a request or command, then press Enter"

# How often the launcher lets Qt deliver events while it waits for the
# session to end after Qt's loop has stopped.
CLOSING_POLL_SECONDS = 0.05

# Qt allows one application object per process, and it must outlive every
# window, so the one Maria creates is kept here.
_APPLICATION: list[QApplication] = []


class MariaWindow(QMainWindow):
    """A read-only conversation, an approval area, one input, and a status."""

    # Carries one worker event to the GUI thread. Emitted on the worker
    # thread, it is delivered through a queued connection only.
    event_delivered = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self._controller: GuiController | None = None
        self._offer_serial: int | None = None
        # Set once a callback Qt runs has failed. The controller is then told
        # as well, and records the failure the launcher reports.
        self.failed = False
        self.setWindowTitle(APP_NAME)

        self.conversation = _read_only_display()
        self.approval_text = _read_only_display()
        self.approve_button = _answer_button("Approve")
        self.cancel_button = _answer_button("Cancel")
        self.request_input = QLineEdit()
        self.request_input.setMaxLength(INPUT_MAXIMUM_LENGTH)
        self.request_input.setPlaceholderText(INPUT_PLACEHOLDER)
        self.send_button = QPushButton("Send")
        self.send_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.send_button.setAutoDefault(False)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.project_label = QLabel(PROJECT_LABEL)
        self.project_label.setTextFormat(Qt.TextFormat.PlainText)
        self.project_choice = QComboBox()
        self.project_choice.setEditable(False)
        self.activate_button = _answer_button(ACTIVATE_LABEL)
        self.deactivate_button = _answer_button(DEACTIVATE_LABEL)

        self.approval = QGroupBox(APPROVAL_TITLE)
        answers = QHBoxLayout()
        answers.addStretch(1)
        answers.addWidget(self.approve_button)
        answers.addWidget(self.cancel_button)
        approval_layout = QVBoxLayout(self.approval)
        approval_layout.addWidget(self.approval_text)
        approval_layout.addLayout(answers)
        self.approval.hide()

        entry = QHBoxLayout()
        entry.addWidget(self.request_input, 1)
        entry.addWidget(self.send_button)

        projects = QHBoxLayout()
        projects.addWidget(self.project_label)
        projects.addWidget(self.project_choice, 1)
        projects.addWidget(self.activate_button)
        projects.addWidget(self.deactivate_button)

        layout = QVBoxLayout()
        layout.addLayout(projects)
        layout.addWidget(self.conversation, 3)
        layout.addWidget(self.approval, 2)
        layout.addLayout(entry)
        layout.addWidget(self.status)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)
        self.resize(760, 600)

        self.request_input.returnPressed.connect(self._send_request)
        self.send_button.clicked.connect(self._send_request)
        self.approve_button.clicked.connect(self._approve_offer)
        self.cancel_button.clicked.connect(self._cancel_offer)
        self.activate_button.clicked.connect(self._activate_project)
        self.deactivate_button.clicked.connect(self._deactivate_project)
        self.event_delivered.connect(
            self._receive,
            Qt.ConnectionType.QueuedConnection,
        )
        self.request_input.setFocus()

    def bind(self, controller: GuiController) -> None:
        """Attach the controller that decides what this window shows."""
        self._controller = controller

    def append(self, line: str) -> None:
        """Add one line to the conversation, as plain text, and follow it."""
        self.conversation.appendPlainText(line)
        scroll_bar = self.conversation.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())

    def set_status(self, text: str) -> None:
        """Show the short status line."""
        self.status.setText(text)

    def show_approval(self, serial: int, text: str) -> None:
        """Show the complete approval text from its start, with its answers."""
        self._offer_serial = serial
        self.approval_text.setPlainText(text)
        self.approval_text.verticalScrollBar().setValue(0)
        self.approval.show()

    def hide_approval(self) -> None:
        """Clear and hide the approval area, forgetting its offer."""
        self._offer_serial = None
        self.approval_text.clear()
        self.approval.hide()

    def set_controls(
        self,
        *,
        sending: bool,
        answering: bool,
        typing: bool,
    ) -> None:
        """Enable Send, Approve and Cancel, and the input."""
        self.send_button.setEnabled(sending)
        self.approve_button.setEnabled(answering)
        self.cancel_button.setEnabled(answering)
        self.request_input.setEnabled(typing)

    def clear_input(self) -> None:
        """Empty the input once its text has been taken."""
        self.request_input.clear()

    def show_projects(self, items: tuple[str, ...]) -> None:
        """Show the selector's plain text items, keeping the chosen position."""
        chosen = self.project_choice.currentIndex()
        self.project_choice.clear()
        self.project_choice.addItems(list(items))
        if 0 <= chosen < len(items):
            self.project_choice.setCurrentIndex(chosen)

    def set_project_controls(self, *, activating: bool, deactivating: bool) -> None:
        """Enable Activate and Deactivate."""
        self.activate_button.setEnabled(activating)
        self.deactivate_button.setEnabled(deactivating)

    def close_window(self) -> None:
        """Close the window once the session has ended."""
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Close only once the session has ended; until then, start closing.

        A close that fails to start leaves the window open, as closing does,
        and a later close is answered again.
        """
        event.ignore()
        if self._controller is None or self._shielded(
            self._controller.request_close
        ):
            event.accept()

    @pyqtSlot(object)
    def _receive(self, event: WorkerEvent) -> None:
        """Hand one worker event to the controller, on the GUI thread."""
        if self._controller is not None:
            self._shielded(self._controller.receive, event)

    def _send_request(self) -> None:
        """Offer the input's text to the controller as one request."""
        if self._controller is not None:
            self._shielded(self._controller.send, self.request_input.text())

    def _approve_offer(self) -> None:
        """Approve the offer this window is showing."""
        if self._controller is not None and self._offer_serial is not None:
            self._shielded(self._controller.approve, self._offer_serial)

    def _cancel_offer(self) -> None:
        """Dismiss the offer this window is showing."""
        if self._controller is not None and self._offer_serial is not None:
            self._shielded(self._controller.cancel, self._offer_serial)

    def _activate_project(self) -> None:
        """Ask to activate the project the selector shows as chosen."""
        if self._controller is not None:
            self._shielded(
                self._controller.activate,
                self.project_choice.currentIndex(),
            )

    def _deactivate_project(self) -> None:
        """Ask to leave no project active."""
        if self._controller is not None:
            self._shielded(self._controller.deactivate)

    def _shielded(
        self,
        call: Callable[..., object],
        *arguments: object,
    ) -> object:
        """Make one controller call for a callback Qt runs, never raising.

        Qt ends the whole process when a callback raises, which would skip
        the session's orderly end. A failure is instead logged by its
        exception type alone, marks the window as failed, and closes it as a
        failure, and ``None`` stands in for the call's result.
        """
        try:
            return call(*arguments)
        except Exception as error:  # noqa: BLE001 - Qt must not see it.
            self.failed = True
            logger = get_logger(__name__)
            logger.error(
                "A desktop window callback failed: %s.",
                type(error).__name__,
            )
            self._close_after_failure(logger)
            return None

    def _close_after_failure(self, logger: Logger) -> None:
        """Put the controller on its failure path, containing any raise.

        The controller refuses every later action and asks the worker to end
        the session before it touches this window again, so a window still
        too broken to show that it is closing changes nothing but the log.
        """
        if self._controller is None:
            return
        try:
            self._controller.close_as_failed()
        except Exception as error:  # noqa: BLE001 - logged; still closing.
            logger.error(
                "The failed desktop window could not show it is closing: %s.",
                type(error).__name__,
            )


def prepare_application(fatal_message: str, fatal_status: int) -> QApplication:
    """Create Qt's application object, before anything else starts.

    Creating it is where Qt first checks that it can show a window here at
    all, and a failure there is fatal inside Qt: it ends the process without
    raising anything Python could catch. While the object is created, Qt's
    own messages are withheld, and a fatal one is replaced by
    ``fatal_message`` on standard error and an immediate exit with
    ``fatal_status``. That is safe because nothing has been configured,
    opened, or started yet. As soon as the object exists, or its creation
    raises, exactly the message handler installed before is put back. An
    application object that already exists is returned as it is.
    """
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        return existing

    previous = qInstallMessageHandler(
        _startup_messages(fatal_message, fatal_status)
    )
    try:
        application = QApplication([APP_NAME])
    finally:
        qInstallMessageHandler(previous)
    _APPLICATION.append(application)
    return application


def _startup_messages(
    fatal_message: str,
    fatal_status: int,
) -> Callable[[QtMsgType, object, str], None]:
    """Return the handler that stands in for Qt's messages during startup.

    Every message is withheld. A fatal one writes ``fatal_message`` to
    standard error when it can, and always ends the process at once with
    ``fatal_status``: a missing standard error, or one that fails to write
    or flush, never stops the exit, and neither Qt's own text nor the
    stream's failure is shown. On Windows, a process without a console first
    sees Qt's own error dialog for a platform that cannot start; Qt shows it
    before this is called.
    """

    def withhold(kind: QtMsgType, context: object, message: str) -> None:
        if kind != QtMsgType.QtFatalMsg:
            return
        try:
            if sys.stderr is not None:
                sys.stderr.write(f"{fatal_message}\n")
                sys.stderr.flush()
        finally:
            os._exit(fatal_status)

    return withhold


def run_window(session_factory: Callable[[], MariaSession]) -> int:
    """Show Maria's window until it closes, and return the exit status.

    ``session_factory`` is called once, on the worker thread, which then owns
    that session until it ends. Whatever ends Qt's event loop, the session is
    ended in order and the worker is waited for, never stopped, before this
    returns; while it waits, Qt still delivers events, so the window keeps
    showing that it is closing and its last events are seen. Closing has no
    general GUI-wide deadline, so a stalled operation can delay it
    indefinitely. Closing is resilient: if asking the window to close, or
    delivering events, raises an ``Exception``, the worker is still asked to
    end and waited for, and the prior Ctrl+C handler is still restored;
    anything else raised there propagates after the same steps. The status
    is 0 only once the controller has seen the session end in order and
    nothing failed, and 1 otherwise.
    """
    logger = get_logger(__name__)
    application = QApplication.instance()
    if not isinstance(application, QApplication):
        application = QApplication([APP_NAME])
        _APPLICATION.append(application)
    window = MariaWindow()
    worker = SessionWorker(session_factory, window.event_delivered.emit)
    controller = GuiController(window, worker)
    window.bind(controller)

    # A handler not installed from Python reads as None, and the default
    # handler is then the nearest one that can be put back.
    previous_handler = signal.getsignal(signal.SIGINT) or signal.SIG_DFL
    closed_in_order = False
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        controller.start()
        window.show()
        logger.info("Desktop window opened.")
        application.exec()
    finally:
        try:
            closed_in_order = _close_in_order(
                application,
                controller,
                worker,
                logger,
            )
        finally:
            signal.signal(signal.SIGINT, previous_handler)
    logger.info("Desktop window closed.")
    return controller.exit_status if closed_in_order else 1


def _close_in_order(
    application: QApplication,
    controller: GuiController,
    worker: SessionWorker,
    logger: Logger,
) -> bool:
    """End the window's session in order and wait for the worker to stop.

    Closing is asked of the controller first, and then of the worker itself,
    so a window that fails to close can never leave the session without its
    end; asking the worker twice is harmless, since it stops at the first.
    The worker is then waited for, never stopped, whatever was raised before.
    Returns whether everything went in order. An ``Exception`` is logged by
    its type alone; anything else propagates once the worker has stopped.
    """
    in_order = True
    try:
        controller.request_close()
    except Exception as error:  # noqa: BLE001 - logged; closing goes on.
        in_order = False
        logger.error(
            "The desktop window could not close in order: %s.",
            type(error).__name__,
        )
    finally:
        worker.close()
        in_order = _wait_for_worker(application, worker, logger) and in_order
    return in_order


def _wait_for_worker(
    application: QApplication,
    worker: SessionWorker,
    logger: Logger,
) -> bool:
    """Wait for the worker to stop, letting Qt deliver events meanwhile.

    Events are delivered until delivering them fails; the wait then goes on
    without them, and nothing raised while delivering them can end the wait
    early. Returns whether delivering events never failed.
    """
    delivering = True
    try:
        while not worker.join(CLOSING_POLL_SECONDS):
            if delivering:
                delivering = _deliver_events(application, logger)
        if delivering:
            delivering = _deliver_events(application, logger)
    finally:
        worker.join()
    return delivering


def _deliver_events(application: QApplication, logger: Logger) -> bool:
    """Let Qt deliver pending events; return False, logged, if that fails."""
    try:
        application.processEvents()
    except Exception as error:  # noqa: BLE001 - logged; waiting goes on.
        logger.error(
            "Events could not be delivered while closing: %s.",
            type(error).__name__,
        )
        return False
    return True


def _read_only_display() -> QPlainTextEdit:
    """Return a plain-text display nothing can select, copy, or focus."""
    display = QPlainTextEdit()
    display.setReadOnly(True)
    display.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    display.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
    display.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    display.setUndoRedoEnabled(False)
    return display


def _answer_button(label: str) -> QPushButton:
    """Return a button only a deliberate click can press, never focused.

    Approve, Cancel, Activate, and Deactivate are all made this way.
    """
    button = QPushButton(label)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.setAutoDefault(False)
    button.setDefault(False)
    return button
