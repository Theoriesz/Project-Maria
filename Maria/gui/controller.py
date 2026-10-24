"""The Qt-free controller of Maria's desktop window.

The controller decides what the window shows and which of the owner's
actions it accepts. It holds no session and no plan: every request, answer,
and dismissal goes to the window's one ``SessionWorker``, and everything the
window shows comes from the worker's events or from the fixed text below. It
imports no Qt and creates no window, so it is tested without either; the
window calls it only on the GUI thread.

A request is accepted only while the window is idle, and an answer only for
the offer the window is showing, so a second click, a repeated key, or a
callback from an earlier offer is refused rather than queued. Requests follow
the command line's rules: one line, stripped, blank text ignored, and
``exit``, ``quit``, or ``q`` closing the window rather than reaching Maria.
Text holding a line break is refused, because the command line reads one
line per request and so can never send one.

The window and its session end together. Closing asks the worker to end the
session after anything still running, refuses every action meanwhile, never
shows an approval that arrives, and lets the window close only once the
session has ended; a session that ends for any reason closes the window, and
the window is asked to close even when clearing it first fails.

A window that has itself failed closes as a failure. ``close_as_failed()``
records the failure and enters closing before anything else happens, so no
later Send, Approve, or Cancel is accepted, and it asks the worker to end the
session before the window is touched again; an operation already running
finishes normally first.

What the window shows is presentation only. The controller keeps no
transcript, never hands displayed text back to the session, saves nothing,
and reads no stored record.

Under V1-23-R1 the worker may also send due reminders the session claimed.
The controller adds such a notice to the conversation, as plain text, in any
state but closed, changes nothing else, and only then acknowledges it to the
worker, which records it as displayed. A notice whose rendering fails is
never acknowledged, so it stays unconfirmed, and the window's existing
shield closes the window as a failure.

Under V1-24-R1 the worker also sends the project selector's choices. The
controller shows them as plain text items, ``<id>: <name>`` with
`` (active)`` on the active one, or one "No projects" item, without changing
its state. Activate and Deactivate are accepted only while the window is
ready, and each submits exactly ``activate project <id>`` or ``deactivate
project`` through the same path as Send, so an activation is still offered
for the owner's approval like any other.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol

from core.constants import APP_NAME, APP_VERSION
from gui.session_worker import (
    ApprovalOffer,
    DueReminderNotice,
    ProjectList,
    Reply,
    SessionEnded,
    WorkerEvent,
)


# The command line's exit words: they close the window and never reach Maria.
EXIT_COMMANDS = frozenset({"exit", "quit", "q"})

# The lines the window opens with, which are the command line's own banner.
WELCOME_LINES = (
    f"{APP_NAME} v{APP_VERSION}",
    "Type 'help' for supported commands. Type 'exit' to quit.",
)

ONE_LINE_MESSAGE = (
    "Maria takes one line per request, as the command line does. Remove the "
    "line breaks and send it again."
)

# The short status line, one per state.
STATUS_READY = "Ready"
STATUS_BUSY = "Working..."
STATUS_AWAITING_APPROVAL = "Waiting for your approval"
STATUS_CLOSING = "Closing"
STATUS_CLOSING_BUSY = "Closing after the current request finishes"
STATUS_CLOSED = "Closed"

# The project selector's fixed texts (V1-24-R1, D12), and the two commands
# its buttons submit.
PROJECT_LABEL = "Project"
ACTIVATE_LABEL = "Activate"
DEACTIVATE_LABEL = "Deactivate"
NO_PROJECTS_ITEM = "No projects"
ACTIVE_ITEM_SUFFIX = " (active)"
DEACTIVATE_COMMAND = "deactivate project"


def activate_command(project_id: int) -> str:
    """Return the exact command Activate submits for one project."""
    return f"activate project {project_id}"


class WindowView(Protocol):
    """What the controller asks of the window it drives."""

    def append(self, line: str) -> None:
        """Add one line to the conversation, as plain text."""

    def set_status(self, text: str) -> None:
        """Show the short status line."""

    def show_approval(self, serial: int, text: str) -> None:
        """Show the approval text, with Approve and Cancel for this offer."""

    def hide_approval(self) -> None:
        """Clear and hide the approval area."""

    def set_controls(
        self,
        *,
        sending: bool,
        answering: bool,
        typing: bool,
    ) -> None:
        """Enable Send, Approve and Cancel, and the input."""

    def clear_input(self) -> None:
        """Empty the input once its text has been taken."""

    def close_window(self) -> None:
        """Close the window once the session has ended."""

    def show_projects(self, items: tuple[str, ...]) -> None:
        """Show the project selector's items, as plain text, in order."""

    def set_project_controls(self, *, activating: bool, deactivating: bool) -> None:
        """Enable Activate and Deactivate."""


class SessionCommands(Protocol):
    """What the controller asks of the worker that owns the session."""

    def start(self) -> None:
        """Start the worker."""

    def submit(self, request: str) -> None:
        """Hand one request to the session."""

    def confirm(self, serial: int) -> None:
        """Confirm the offer named by ``serial``."""

    def cancel(self) -> None:
        """Dismiss the pending approval."""

    def acknowledge_reminder_notice(self, serial: int) -> None:
        """Say that the window showed the notice named by ``serial``."""

    def close(self) -> None:
        """End the session once earlier operations finish."""


class ControllerState(str, Enum):
    """Where the window is in its one-request-at-a-time cycle."""

    READY = "ready"
    BUSY = "busy"
    AWAITING_APPROVAL = "awaiting_approval"
    CLOSING = "closing"
    CLOSED = "closed"


# The status line of every state but closing, whose line says whether it
# waits for a request still running.
_STATUS = {
    ControllerState.READY: STATUS_READY,
    ControllerState.BUSY: STATUS_BUSY,
    ControllerState.AWAITING_APPROVAL: STATUS_AWAITING_APPROVAL,
    ControllerState.CLOSED: STATUS_CLOSED,
}

# The states in which the owner may type, though not necessarily send.
_TYPING_STATES = frozenset(
    {
        ControllerState.READY,
        ControllerState.BUSY,
        ControllerState.AWAITING_APPROVAL,
    }
)


class GuiController:
    """Decide what Maria's window shows and which owner actions it takes."""

    def __init__(self, view: WindowView, worker: SessionCommands) -> None:
        self._view = view
        self._worker = worker
        self._state = ControllerState.READY
        self._offer_serial: int | None = None
        self._closing_status = STATUS_CLOSING
        self._failed = False
        # The ids of the projects the selector shows, in its order.
        self._project_ids: tuple[int, ...] = ()

    @property
    def state(self) -> ControllerState:
        """Return where the window is in its cycle."""
        return self._state

    @property
    def exit_status(self) -> int:
        """Return 0 once the session has ended in order, and 1 otherwise.

        Until the window has seen the session end, nothing is known to have
        ended in order, so the status is 1 then as well, and a window that
        failed always closes with 1.
        """
        ended_in_order = (
            self._state is ControllerState.CLOSED and not self._failed
        )
        return 0 if ended_in_order else 1

    def start(self) -> None:
        """Show the opening lines and start the worker; call once."""
        for line in WELCOME_LINES:
            self._view.append(line)
        self._worker.start()
        self._refresh()

    def send(self, text: str) -> bool:
        """Take the input's text as one request, if the window can now.

        Returns whether the text was taken: as a request, or as an exit word
        that starts closing. Refused text stays in the input.
        """
        if self._state is not ControllerState.READY:
            return False
        request = text.strip()
        if not request:
            return False
        if "\n" in request or "\r" in request:
            self._view.append(f"{APP_NAME}: {ONE_LINE_MESSAGE}")
            return False
        self._view.clear_input()
        if request.lower() in EXIT_COMMANDS:
            self.request_close()
            return True
        self._submit(request)
        return True

    def activate(self, index: int) -> bool:
        """Submit ``activate project <id>`` for the item at ``index``.

        Accepted only while the window is ready and the item names a project;
        the activation is then offered for approval like any other request.
        """
        if self._state is not ControllerState.READY or not (
            type(index) is int and 0 <= index < len(self._project_ids)
        ):
            return False
        self._submit(activate_command(self._project_ids[index]))
        return True

    def deactivate(self) -> bool:
        """Submit ``deactivate project``, if the window is ready."""
        if self._state is not ControllerState.READY:
            return False
        self._submit(DEACTIVATE_COMMAND)
        return True

    def approve(self, serial: int) -> bool:
        """Confirm the offer the window shows, named by ``serial``."""
        if not self._answering(serial):
            return False
        self._answered()
        self._worker.confirm(serial)
        return True

    def cancel(self, serial: int) -> bool:
        """Dismiss the offer the window shows, named by ``serial``.

        This dismisses a pending approval only. Nothing stops an operation
        that is already running.
        """
        if not self._answering(serial):
            return False
        self._answered()
        self._worker.cancel()
        return True

    def request_close(self) -> bool:
        """Start closing, and return whether the window may close now.

        Unless closing has already started, this asks the worker to end the
        session after anything still running, and every later action is
        refused. The window may close only once the session has ended.
        """
        if self._state is ControllerState.CLOSED:
            return True
        self._start_closing()
        return False

    def close_as_failed(self) -> None:
        """Close as a failure, because the window itself has failed.

        The failure is recorded and closing starts exactly as for
        ``request_close()``: no later action is accepted, and the worker is
        asked to end the session before the window is touched, so a window
        too broken to show any of it still ends in order. Calling this again,
        or once the session has ended, records the failure and nothing else.
        """
        self._failed = True
        if self._state is not ControllerState.CLOSED:
            self._start_closing()

    def receive(self, event: WorkerEvent) -> None:
        """Show one event from the worker; call only on the GUI thread."""
        if self._state is ControllerState.CLOSED:
            return
        if isinstance(event, SessionEnded):
            self._end(event)
            return
        if isinstance(event, DueReminderNotice):
            # Shown first and acknowledged only then, so a notice whose
            # rendering raised is never recorded as displayed. Nothing else
            # changes: not the state, the offer shown, or the approval area.
            self._view.append(f"{APP_NAME}: {event.text}")
            self._worker.acknowledge_reminder_notice(event.serial)
            return
        if isinstance(event, ProjectList):
            # The selector changes; the state, the offer shown, and the
            # approval area do not.
            self._show_projects(event)
            self._refresh()
            return
        if isinstance(event, ApprovalOffer):
            self._offer(event)
        elif isinstance(event, Reply):
            self._view.append(f"{APP_NAME}: {event.text}")
            if self._state is ControllerState.BUSY:
                self._state = ControllerState.READY
        self._refresh()

    def _submit(self, request: str) -> None:
        """Show one accepted request and hand it to the worker."""
        self._view.append(f"You: {request}")
        self._state = ControllerState.BUSY
        self._worker.submit(request)
        self._refresh()

    def _show_projects(self, event: ProjectList) -> None:
        """Show the selector's items as plain text, and keep their ids."""
        self._project_ids = tuple(project_id for project_id, _ in event.projects)
        items = tuple(
            f"{project_id}: {name}"
            f"{ACTIVE_ITEM_SUFFIX if project_id == event.active_id else ''}"
            for project_id, name in event.projects
        )
        self._view.show_projects(items or (NO_PROJECTS_ITEM,))

    def _offer(self, event: ApprovalOffer) -> None:
        """Show an offer the owner can answer, unless the window is closing.

        While closing, an offer is never shown: ending the session discards
        the approval it describes.
        """
        if self._state is not ControllerState.BUSY:
            return
        self._view.append(f"{APP_NAME}: {event.text}")
        self._state = ControllerState.AWAITING_APPROVAL
        self._offer_serial = event.serial
        self._view.show_approval(event.serial, event.text)

    def _start_closing(self) -> None:
        """Refuse every later action, then end the session, once only."""
        if self._state is ControllerState.CLOSING:
            return
        self._closing_status = (
            STATUS_CLOSING_BUSY
            if self._state is ControllerState.BUSY
            else STATUS_CLOSING
        )
        self._state = ControllerState.CLOSING
        self._offer_serial = None
        # The worker is told first, so nothing the window does next can
        # leave the session without its end.
        self._worker.close()
        self._view.hide_approval()
        self._refresh()

    def _end(self, event: SessionEnded) -> None:
        """Close the window now that its session has ended.

        The outcome is settled first. The window is then cleared and asked
        to close, and the close is attempted even when clearing it fails;
        that failure still reaches the caller afterwards.
        """
        closing = self._state is ControllerState.CLOSING
        self._state = ControllerState.CLOSED
        self._offer_serial = None
        self._failed = self._failed or event.failed or not closing
        try:
            self._view.hide_approval()
            self._refresh()
        finally:
            self._view.close_window()

    def _answering(self, serial: int) -> bool:
        """Return whether an answer names the offer the window shows."""
        return (
            self._state is ControllerState.AWAITING_APPROVAL
            and serial == self._offer_serial
        )

    def _answered(self) -> None:
        """Retire the offer shown, so it can be answered only once."""
        self._state = ControllerState.BUSY
        self._offer_serial = None
        self._view.hide_approval()
        self._refresh()

    def _refresh(self) -> None:
        """Show the status line and enable the controls the state allows."""
        state = self._state
        self._view.set_status(
            self._closing_status
            if state is ControllerState.CLOSING
            else _STATUS[state]
        )
        self._view.set_controls(
            sending=state is ControllerState.READY,
            answering=state is ControllerState.AWAITING_APPROVAL,
            typing=state in _TYPING_STATES,
        )
        self._view.set_project_controls(
            activating=state is ControllerState.READY and bool(self._project_ids),
            deactivating=state is ControllerState.READY,
        )
