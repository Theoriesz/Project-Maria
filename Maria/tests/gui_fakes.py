"""Stand-ins shared by the desktop GUI tests.

Nothing here imports Qt, contacts a model, or opens a database.
``FakeSession`` answers like ``MariaSession`` from a script and records the
thread that made every call; ``RecordingView`` and ``RecordingWorker`` record
what the controller asks of the window and of the worker; ``EventPump`` plays
the GUI thread for a controller driven by a real ``SessionWorker``; and
``capturing_logger`` keeps every message a logger is handed.
"""

from __future__ import annotations

import io
import logging
import queue
import sys
import threading
import types
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from core.brain import BrainResponse
from core.session import MariaSession, PendingApproval
from gui.controller import ControllerState, GuiController
from gui.session_worker import SessionWorker, WorkerEvent


# How long any wait in these tests may take before it counts as a hang.
WAIT_SECONDS = 10.0

# A request starting with this needs approval in a FakeSession.
APPROVAL_PREFIX = "approve "
CONFIRMED_TEXT = "Done as approved."


def reply_text(request: str) -> str:
    """Return a FakeSession's reply to an ordinary request."""
    return f"Reply to {request}"


def approval_text(request: str) -> str:
    """Return a FakeSession's approval text for a request that needs one."""
    return f"Confirmation required for {request}\nTool: fake\nAction: act"


class FakeSession:
    """A session double: scripted answers, recorded calls and threads.

    A request starting with ``APPROVAL_PREFIX`` leaves a pending approval, as
    a confirmation-required request does. ``gate``, when given, holds every
    request until it is set; ``failure``, when given, is raised by every
    request; and ``end_failure``, when given, is raised by ``end()`` once it
    has been recorded.

    The reminder-delivery seam (V1-23-R1) is off unless
    ``delivers_reminders`` is True. Each ``claim_due_reminders`` call then
    returns the next of ``notices``, or None once they run out, and raises
    ``claim_failure`` instead when one is given; ``record_reminder_notice``
    keeps every recording in ``recorded``, raising ``record_failure`` after
    recording the call when one is given.

    The projects seam (V1-24-R1) answers None, recording nothing, unless
    ``choices`` is given: each ``project_choices`` call then records itself
    and returns the next of ``choices``, the last one repeating, or raises
    ``choices_failure`` instead when one is given.
    """

    def __init__(
        self,
        *,
        gate: threading.Event | None = None,
        failure: BaseException | None = None,
        end_failure: BaseException | None = None,
        delivers_reminders: bool = False,
        notices: tuple[object, ...] = (),
        claim_failure: BaseException | None = None,
        record_failure: BaseException | None = None,
        choices: tuple[object, ...] | None = None,
        choices_failure: BaseException | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.threads: list[int] = []
        self.requests: list[str] = []
        self.pending_approval: PendingApproval | None = None
        self.started_request = threading.Event()
        self._gate = gate
        self._failure = failure
        self._end_failure = end_failure
        self.delivers_reminders = delivers_reminders
        self._notices = list(notices)
        self._claim_failure = claim_failure
        self._record_failure = record_failure
        self.recorded: list[tuple[object, bool]] = []
        self.recorded_event = threading.Event()
        self._choices = None if choices is None else list(choices)
        self._choices_failure = choices_failure

    def submit(self, user_input: str) -> BrainResponse:
        """Answer one request, leaving an approval pending if it needs one."""
        self._note("submit")
        self.requests.append(user_input)
        self.pending_approval = None
        self.started_request.set()
        if self._gate is not None and not self._gate.wait(WAIT_SECONDS):
            raise AssertionError("The test never released its gate.")
        if self._failure is not None:
            raise self._failure
        if user_input.startswith(APPROVAL_PREFIX):
            self.pending_approval = PendingApproval(None, "f" * 64)
            return BrainResponse(
                approval_text(user_input),
                needs_confirmation=True,
            )
        return BrainResponse(reply_text(user_input))

    def confirm(self) -> BrainResponse:
        """Consume the pending approval and report it done."""
        self._note("confirm")
        self.pending_approval = None
        return BrainResponse(CONFIRMED_TEXT)

    def cancel(self) -> bool:
        """Discard the pending approval, reporting whether there was one."""
        self._note("cancel")
        had_pending = self.pending_approval is not None
        self.pending_approval = None
        return had_pending

    def end(self) -> None:
        """End the session, discarding any pending approval."""
        self._note("end")
        self.pending_approval = None
        if self._end_failure is not None:
            raise self._end_failure

    def claim_due_reminders(self) -> object | None:
        """Return the next scripted notice, or None once there are none."""
        self._note("claim_due_reminders")
        if self._claim_failure is not None:
            raise self._claim_failure
        return self._notices.pop(0) if self._notices else None

    def record_reminder_notice(self, notice: object, displayed: bool) -> None:
        """Keep one recording of a notice's outcome."""
        self._note("record_reminder_notice")
        self.recorded.append((notice, displayed))
        self.recorded_event.set()
        if self._record_failure is not None:
            raise self._record_failure

    def project_choices(self) -> object | None:
        """Return the next scripted choices, or None without a seam."""
        if self._choices is None and self._choices_failure is None:
            return None
        self._note("project_choices")
        if self._choices_failure is not None:
            raise self._choices_failure
        if not self._choices:
            return None
        if len(self._choices) > 1:
            return self._choices.pop(0)
        return self._choices[0]

    def _note(self, call: str) -> None:
        """Record one call and the thread that made it."""
        self.calls.append(call)
        self.threads.append(threading.get_ident())


class RecordingView:
    """Record everything the controller asks of its window."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.statuses: list[str] = []
        self.shown_offers: list[tuple[int, str]] = []
        self.approval_visible = False
        self.controls: dict[str, bool] = {}
        self.cleared = 0
        self.closed = 0
        self.project_items: list[tuple[str, ...]] = []
        self.project_controls: dict[str, bool] = {}

    def append(self, line: str) -> None:
        """Record one conversation line."""
        self.lines.append(line)

    def set_status(self, text: str) -> None:
        """Record the status line."""
        self.statuses.append(text)

    def show_approval(self, serial: int, text: str) -> None:
        """Record an offer shown for approval."""
        self.shown_offers.append((serial, text))
        self.approval_visible = True

    def hide_approval(self) -> None:
        """Record that the approval area is hidden."""
        self.approval_visible = False

    def set_controls(
        self,
        *,
        sending: bool,
        answering: bool,
        typing: bool,
    ) -> None:
        """Record which controls are enabled."""
        self.controls = {
            "sending": sending,
            "answering": answering,
            "typing": typing,
        }

    def clear_input(self) -> None:
        """Record that the input was emptied."""
        self.cleared += 1

    def close_window(self) -> None:
        """Record that the window was closed."""
        self.closed += 1

    def show_projects(self, items: tuple[str, ...]) -> None:
        """Record the project selector's items."""
        self.project_items.append(items)

    def set_project_controls(self, *, activating: bool, deactivating: bool) -> None:
        """Record which project controls are enabled."""
        self.project_controls = {
            "activating": activating,
            "deactivating": deactivating,
        }

    @property
    def status(self) -> str:
        """Return the status line now showing."""
        return self.statuses[-1]

    @property
    def maria_lines(self) -> list[str]:
        """Return every conversation line after the opening banner."""
        return [
            line for line in self.lines[2:] if not line.startswith("You: ")
        ]


class RecordingWorker:
    """Record what the controller asks of the worker, running nothing."""

    def __init__(self) -> None:
        self.commands: list[tuple[object, ...]] = []

    def start(self) -> None:
        """Record a start."""
        self.commands.append(("start",))

    def submit(self, request: str) -> None:
        """Record a request."""
        self.commands.append(("submit", request))

    def confirm(self, serial: int) -> None:
        """Record a confirmation."""
        self.commands.append(("confirm", serial))

    def cancel(self) -> None:
        """Record a dismissal."""
        self.commands.append(("cancel",))

    def acknowledge_reminder_notice(self, serial: int) -> None:
        """Record that the window showed a reminder notice."""
        self.commands.append(("acknowledge_reminder_notice", serial))

    def close(self) -> None:
        """Record a close."""
        self.commands.append(("close",))


class EventPump:
    """Play the GUI thread for a controller driven by a real worker."""

    def __init__(self) -> None:
        self._events: queue.SimpleQueue[WorkerEvent] = queue.SimpleQueue()
        self.controller: GuiController | None = None
        self.delivered: list[WorkerEvent] = []

    def deliver(self, event: WorkerEvent) -> None:
        """The worker's delivery callback: queue one event for this thread."""
        self._events.put(event)

    def settle(self) -> None:
        """Hand events to the controller until it waits for the owner."""
        assert self.controller is not None
        while self.controller.state in (
            ControllerState.BUSY,
            ControllerState.CLOSING,
        ):
            event = self._events.get(timeout=WAIT_SECONDS)
            self.delivered.append(event)
            self.controller.receive(event)


class RunningGui:
    """A controller, a real worker, and a recording view, for one test."""

    def __init__(
        self,
        test: unittest.TestCase,
        session_factory: Callable[[], MariaSession | FakeSession],
        logger: logging.Logger | None = None,
    ) -> None:
        self.view = RecordingView()
        self.pump = EventPump()
        self.worker = SessionWorker(
            session_factory,
            self.pump.deliver,
            logger=logger,
        )
        self.controller = GuiController(self.view, self.worker)
        self.pump.controller = self.controller
        test.addCleanup(self.shut_down, test)
        self.controller.start()

    def send(self, text: str) -> None:
        """Type one request and wait for its outcome."""
        if not self.controller.send(text):
            raise AssertionError("The window refused the request.")
        self.pump.settle()

    def approve(self) -> None:
        """Approve the offer showing and wait for the result."""
        serial, _ = self.view.shown_offers[-1]
        if not self.controller.approve(serial):
            raise AssertionError("The window refused the approval.")
        self.pump.settle()

    def cancel(self) -> None:
        """Dismiss the offer showing and wait for the reply."""
        serial, _ = self.view.shown_offers[-1]
        if not self.controller.cancel(serial):
            raise AssertionError("The window refused the dismissal.")
        self.pump.settle()

    def close(self) -> None:
        """Close the window and wait for the session to end."""
        self.controller.request_close()
        self.pump.settle()

    def shut_down(self, test: unittest.TestCase) -> None:
        """End anything a test left running, and prove the worker stopped.

        The worker is told to end even when closing through the controller
        failed, so a failed test never leaves its thread running.
        """
        try:
            if self.controller.state is not ControllerState.CLOSED:
                self.close()
        finally:
            self.worker.close()
            test.assertTrue(self.worker.join(2 * WAIT_SECONDS))


def capturing_logger(name: str) -> tuple[logging.Logger, list[str]]:
    """Return an isolated logger and the list of every message it keeps."""
    messages: list[str] = []

    class _Keep(logging.Handler):
        """Keep each message."""

        def emit(self, record: logging.LogRecord) -> None:
            """Keep one message."""
            messages.append(record.getMessage())

    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.addHandler(_Keep())
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, messages


# The module that alone imports Qt, and the names that make up Qt.
QT_WINDOW_MODULE = "gui.window"


def _is_qt_or_window(name: str) -> bool:
    """Return whether a module name is PyQt6, part of it, or the window."""
    return (
        name == "PyQt6"
        or name.startswith("PyQt6.")
        or name == QT_WINDOW_MODULE
    )


@contextmanager
def without_qt() -> Iterator[None]:
    """Make PyQt6 unimportable, as on a Python build without it.

    Any PyQt6 module and the window module already imported are set aside
    and put back afterwards exactly as they were, so the rest of the run is
    unaffected; nothing PyQt6 provides is called meanwhile.
    """
    hidden = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if _is_qt_or_window(name)
    }
    sys.modules["PyQt6"] = None  # type: ignore[assignment]
    try:
        yield
    finally:
        for name in [name for name in sys.modules if _is_qt_or_window(name)]:
            del sys.modules[name]
        sys.modules.update(hidden)


@contextmanager
def window_module(
    run_window: Callable[..., int],
    prepare_application: Callable[..., object] | None = None,
) -> Iterator[None]:
    """Stand in for the window module, so the launcher runs without Qt.

    ``prepare_application`` defaults to one that prepares nothing.
    """
    missing = object()
    previous = sys.modules.get(QT_WINDOW_MODULE, missing)
    stand_in = types.ModuleType(QT_WINDOW_MODULE)
    stand_in.run_window = run_window  # type: ignore[attr-defined]
    stand_in.prepare_application = (  # type: ignore[attr-defined]
        prepare_application or (lambda message, status: None)
    )
    sys.modules[QT_WINDOW_MODULE] = stand_in
    try:
        yield
    finally:
        if previous is missing:
            sys.modules.pop(QT_WINDOW_MODULE, None)
        else:
            sys.modules[QT_WINDOW_MODULE] = previous  # type: ignore[assignment]


@contextmanager
def thread_failures_observed() -> Iterator[tuple[list[str], io.StringIO]]:
    """Record what reaches Python's thread exception hook, and stderr.

    Nothing is suppressed: the original hook still runs and writes, as it
    would, into the captured standard error. A leak therefore shows up both
    as a recorded hook call, by exception type, and as text.
    """
    calls: list[str] = []
    original_hook = threading.excepthook
    original_stderr = sys.stderr
    captured = io.StringIO()

    def recording_hook(arguments: threading.ExceptHookArgs) -> None:
        """Record one escaped exception, then let the original hook run."""
        calls.append(arguments.exc_type.__name__)
        original_hook(arguments)

    threading.excepthook = recording_hook
    sys.stderr = captured
    try:
        yield calls, captured
    finally:
        sys.stderr = original_stderr
        threading.excepthook = original_hook
