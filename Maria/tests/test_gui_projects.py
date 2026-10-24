"""Tests for the desktop window's project selector (V1-24-R1).

The worker, the controller, the session's projects seam, and the launcher's
wiring are driven without Qt, with the doubles of ``tests.gui_fakes`` and,
end to end, a real session over a temporary database. They pin when the
worker sends the selector's choices: once after creating the session and
after each operation that ends with a reply, never after an approval offer,
and never once the window has asked to close, not even the first time. The
controller shows the choices as plain text, with " (active)" on the active
project or one "No projects" item, without changing its state; Activate and
Deactivate are accepted only while the window is ready, and each submits
exactly ``activate project <id>`` or ``deactivate project`` through the same
path as Send, so an activation is still offered for approval.

The window tests run on Qt's offscreen platform and skip explicitly without
PyQt6 or that platform; a skipped or offscreen test is never a visual check.
Nothing here contacts a model or reads Maria's live database, live log, or
private override, and every wait is bounded by ``WAIT_SECONDS``.
"""

from __future__ import annotations

import logging
import queue
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import Mock

import main as main_module
from core.brain import MariaBrain
from core.project_records import ActivationState, ProjectChoices
from core.session import MariaSession, ProjectChoicesSeam
from gui.controller import (
    ACTIVATE_LABEL,
    DEACTIVATE_COMMAND,
    DEACTIVATE_LABEL,
    NO_PROJECTS_ITEM,
    PROJECT_LABEL,
    ControllerState,
    GuiController,
    activate_command,
)
from gui.session_worker import (
    ACTION_CANCELLED_MESSAGE,
    ApprovalOffer,
    ProjectList,
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
    RunningGui,
    capturing_logger,
    reply_text,
    window_module,
)
from tests.test_conversation import _Harness
from tests.test_gui_window import UNAVAILABLE, WindowTestCase, wait_until
from tests.test_permissions import ACTIVATE_PROJECT_NOTICE

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
except ImportError:
    Qt = QTest = None


PRIVATE = "private-gui-projects-detail-5c81e0"
TWO = ((1, "Garden"), (2, "Self review"))
LISTED = ProjectChoices(TWO, None)
SECOND_ACTIVE = ProjectChoices(TWO, 2)


class _WorkerCase(unittest.TestCase):
    """Drive one real worker over a session, collecting what it delivers."""

    def setUp(self) -> None:
        self.events: queue.SimpleQueue[WorkerEvent] = queue.SimpleQueue()
        self.logger, self.messages = capturing_logger("tests.gui.projects")

    def start(self, session: FakeSession, *, closed: bool = False) -> SessionWorker:
        """Start a worker over ``session``, closing it first when asked."""
        worker = SessionWorker(
            lambda: session,  # type: ignore[arg-type, return-value]
            self.events.put,
            logger=self.logger,
        )
        self.addCleanup(self._stop, worker)
        if closed:
            worker.close()
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
        remaining: list[WorkerEvent] = []
        while True:
            try:
                remaining.append(self.events.get_nowait())
            except queue.Empty:
                return remaining


class WorkerProjectListTests(_WorkerCase):
    """The worker sends the choices after the session starts and each reply."""

    def test_the_choices_follow_the_sessions_creation(self) -> None:
        session = FakeSession(choices=(LISTED,))
        worker = self.start(session)

        self.assertEqual(self.next_event(), ProjectList(TWO, None))
        self.assertEqual(self.finish(worker), [SessionEnded(failed=False)])
        self.assertEqual(session.calls, ["project_choices", "end"])

    def test_choices_follow_every_reply_and_never_an_offer(self) -> None:
        session = FakeSession(choices=(LISTED, SECOND_ACTIVE))
        worker = self.start(session)
        self.assertEqual(self.next_event(), ProjectList(TWO, None))

        worker.submit("hello")
        self.assertEqual(
            [self.next_event(), self.next_event()],
            [Reply(reply_text("hello")), ProjectList(TWO, 2)],
        )
        worker.submit(APPROVAL_PREFIX + "activate project 2")
        self.assertIsInstance(self.next_event(), ApprovalOffer)
        worker.cancel()
        self.assertEqual(
            [self.next_event(), self.next_event()],
            [Reply(ACTION_CANCELLED_MESSAGE), ProjectList(TWO, 2)],
        )
        worker.submit(APPROVAL_PREFIX + "activate project 2")
        offer = self.next_event()
        self.assertIsInstance(offer, ApprovalOffer)
        worker.confirm(offer.serial)
        self.assertEqual(
            [self.next_event(), self.next_event()],
            [Reply(CONFIRMED_TEXT), ProjectList(TWO, 2)],
        )

        self.assertEqual(self.finish(worker), [SessionEnded(failed=False)])
        self.assertEqual(
            session.calls,
            [
                "project_choices",
                "submit",
                "project_choices",
                "submit",
                "cancel",
                "project_choices",
                "submit",
                "confirm",
                "project_choices",
                "end",
            ],
        )

    def test_no_choices_are_read_once_close_was_called_not_even_the_first(
        self,
    ) -> None:
        session = FakeSession(choices=(LISTED,))
        worker = self.start(session, closed=True)

        self.assertTrue(worker.join(WAIT_SECONDS))

        self.assertEqual(self.next_event(), SessionEnded(failed=False))
        self.assertEqual(session.calls, ["end"])

    def test_no_choices_follow_a_reply_once_close_was_called(self) -> None:
        gate = threading.Event()
        session = FakeSession(gate=gate, choices=(LISTED,))
        worker = self.start(session)
        self.assertEqual(self.next_event(), ProjectList(TWO, None))
        worker.submit("hello")
        self.assertTrue(session.started_request.wait(WAIT_SECONDS))

        worker.close()
        gate.set()

        self.assertEqual(
            self.finish(worker),
            [Reply(reply_text("hello")), SessionEnded(failed=False)],
        )
        self.assertEqual(session.calls, ["project_choices", "submit", "end"])

    def test_a_session_without_the_seam_sends_no_choices(self) -> None:
        session = FakeSession()
        worker = self.start(session)

        worker.submit("hello")

        self.assertEqual(self.next_event(), Reply(reply_text("hello")))
        self.assertEqual(self.finish(worker), [SessionEnded(failed=False)])
        self.assertEqual(session.calls, ["submit", "end"])

    def test_the_choices_are_read_on_the_worker_thread_alone(self) -> None:
        session = FakeSession(choices=(LISTED,))
        worker = self.start(session)
        self.next_event()
        worker.submit("hello")
        self.next_event()
        self.next_event()
        self.finish(worker)

        threads = {
            thread
            for call, thread in zip(session.calls, session.threads)
            if call == "project_choices"
        }
        self.assertEqual(len(threads), 1)
        self.assertNotIn(threading.get_ident(), threads)


class SessionProjectChoicesTests(unittest.TestCase):
    """The session's one read for the selector contains every Exception."""

    def setUp(self) -> None:
        self.logger, self.messages = capturing_logger("tests.gui.projects.session")
        self.brain = Mock(spec=MariaBrain)

    def session(self, seam: object | None) -> MariaSession:
        """Return a session over a stand-in Brain with ``seam``."""
        return MariaSession(
            self.brain,
            self.logger,
            projects=seam,  # type: ignore[arg-type]
        )

    def test_the_seams_choices_are_returned_and_none_without_a_seam(self) -> None:
        seam = Mock()
        seam.choices.return_value = SECOND_ACTIVE

        self.assertIs(self.session(seam).project_choices(), SECOND_ACTIVE)
        self.assertIsNone(self.session(None).project_choices())
        self.assertEqual(self.brain.method_calls, [])

    def test_a_failing_or_wrong_read_gives_none_and_logs_its_type_alone(
        self,
    ) -> None:
        failing = Mock()
        failing.choices.side_effect = RuntimeError(PRIVATE)
        wrong = Mock()
        wrong.choices.return_value = (TWO, None)

        self.assertIsNone(self.session(failing).project_choices())
        self.assertIsNone(self.session(wrong).project_choices())

        self.assertEqual(
            self.messages,
            [
                "Reading the project choices failed: RuntimeError.",
                "Reading the project choices failed: unexpected.",
            ],
        )
        self.assertNotIn(PRIVATE, "".join(self.messages))
        self.assertEqual(self.brain.method_calls, [])

    def test_a_failure_that_is_not_an_exception_propagates(self) -> None:
        seam = Mock()
        seam.choices.side_effect = KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.session(seam).project_choices()

    def test_the_runtime_seam_reads_only_the_memory_layers_choices(self) -> None:
        brain = Mock()
        brain.memory.project_choices.return_value = LISTED

        seam = main_module.build_runtime_projects(brain)

        self.assertIs(type(seam), ProjectChoicesSeam)
        self.assertIs(seam.choices(), LISTED)
        self.assertEqual(brain.memory.method_calls, [("project_choices", (), {})])
        self.assertEqual(repr(seam), "ProjectChoicesSeam()")


class LauncherWiringTests(unittest.TestCase):
    """The window's sessions get the projects seam only when one is given."""

    def setUp(self) -> None:
        quiet = logging.NullHandler()
        maria = logging.getLogger("maria")
        maria.addHandler(quiet)
        self.addCleanup(maria.removeHandler, quiet)

    def _session_factory(self, **factories: object) -> object:
        """Run the launcher with a stand-in window; return its session factory."""
        brain = Mock(spec=MariaBrain)
        run_window = Mock(return_value=0)
        with (
            window_module(run_window),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            status = main_module.run_gui(Mock(return_value=brain), **factories)
        self.assertEqual(status, 0)
        ((session_factory,), _) = run_window.call_args
        return session_factory

    def test_an_injected_projects_factory_reaches_every_session(self) -> None:
        seam = Mock()
        seam.choices.return_value = SECOND_ACTIVE

        session = self._session_factory(projects_factory=lambda brain: seam)()

        self.assertIs(session.project_choices(), SECOND_ACTIVE)

    def test_an_injected_brain_alone_gives_no_projects_seam(self) -> None:
        session = self._session_factory()()

        self.assertIsNone(session.project_choices())


class ControllerProjectTests(unittest.TestCase):
    """The selector shows plain text and submits exactly two commands."""

    def setUp(self) -> None:
        self.view = RecordingView()
        self.worker = RecordingWorker()
        self.controller = GuiController(self.view, self.worker)
        self.controller.start()

    def submitted(self) -> list[str]:
        """Return every request the controller handed to the worker."""
        return [
            command[1] for command in self.worker.commands if command[0] == "submit"
        ]

    def test_the_fixed_texts_and_commands_are_pinned(self) -> None:
        self.assertEqual(PROJECT_LABEL, "Project")
        self.assertEqual(ACTIVATE_LABEL, "Activate")
        self.assertEqual(DEACTIVATE_LABEL, "Deactivate")
        self.assertEqual(NO_PROJECTS_ITEM, "No projects")
        self.assertEqual(DEACTIVATE_COMMAND, "deactivate project")
        self.assertEqual(activate_command(7), "activate project 7")

    def test_the_choices_show_as_plain_text_with_the_active_one_marked(self) -> None:
        self.controller.receive(ProjectList(TWO, 2))
        self.controller.receive(ProjectList(((3, "<b>Bold</b> & co"),), None))

        self.assertEqual(
            self.view.project_items,
            [("1: Garden", "2: Self review (active)"), ("3: <b>Bold</b> & co",)],
        )
        self.assertIs(self.controller.state, ControllerState.READY)
        self.assertEqual(
            self.view.project_controls,
            {"activating": True, "deactivating": True},
        )

    def test_no_projects_is_one_item_with_nothing_to_activate(self) -> None:
        self.controller.receive(ProjectList((), None))

        self.assertEqual(self.view.project_items, [(NO_PROJECTS_ITEM,)])
        self.assertEqual(
            self.view.project_controls,
            {"activating": False, "deactivating": True},
        )
        self.assertFalse(self.controller.activate(0))
        self.assertEqual(self.submitted(), [])

    def test_choices_change_no_state_offer_or_approval(self) -> None:
        self.controller.send("hello")
        self.controller.receive(ProjectList(TWO, None))
        self.assertIs(self.controller.state, ControllerState.BUSY)
        self.assertEqual(
            self.view.project_controls,
            {"activating": False, "deactivating": False},
        )

        self.controller.receive(ApprovalOffer(1, "Approve this?"))
        self.controller.receive(ProjectList(TWO, 2))

        self.assertIs(self.controller.state, ControllerState.AWAITING_APPROVAL)
        self.assertTrue(self.view.approval_visible)
        self.assertEqual(self.view.shown_offers, [(1, "Approve this?")])
        self.assertEqual(
            self.view.project_controls,
            {"activating": False, "deactivating": False},
        )
        self.assertTrue(self.controller.approve(1))

    def test_activate_and_deactivate_submit_their_commands_only_when_ready(
        self,
    ) -> None:
        self.controller.receive(ProjectList(TWO, None))
        for index in (-1, 2, True, 1.0, None):
            with self.subTest(index=index):
                accepted = self.controller.activate(index)  # type: ignore[arg-type]
                self.assertFalse(accepted)

        self.assertTrue(self.controller.activate(1))
        self.assertIs(self.controller.state, ControllerState.BUSY)
        self.assertEqual(self.view.lines[-1], "You: activate project 2")
        self.assertFalse(self.controller.activate(0))
        self.assertFalse(self.controller.deactivate())
        self.controller.receive(Reply("Done."))
        self.assertTrue(self.controller.deactivate())

        self.assertEqual(self.submitted(), ["activate project 2", "deactivate project"])

    def test_neither_is_accepted_while_closing_or_closed(self) -> None:
        self.controller.receive(ProjectList(TWO, None))
        self.controller.request_close()

        self.assertFalse(self.controller.activate(0))
        self.assertFalse(self.controller.deactivate())
        self.controller.receive(SessionEnded(failed=False))
        self.assertFalse(self.controller.activate(0))
        self.assertFalse(self.controller.deactivate())
        self.assertEqual(self.submitted(), [])
        self.assertEqual(
            self.view.project_controls,
            {"activating": False, "deactivating": False},
        )


class SelectorFlowTests(unittest.TestCase):
    """End to end, without Qt: an activation from the selector is approved."""

    def test_an_activation_is_offered_approved_shown_and_deactivated(self) -> None:
        harness = _Harness(self)
        harness.memory.create_project("Garden", "workspace", "garden")
        gui = RunningGui(
            self,
            lambda: MariaSession(
                harness.brain,
                projects=ProjectChoicesSeam(harness.memory),
            ),
        )
        gui.send("projects")

        self.assertTrue(gui.controller.activate(0))
        gui.pump.settle()
        ((_serial, offer),) = gui.view.shown_offers
        self.assertIn("projects.activate requires explicit confirmation", offer)
        self.assertIn(ACTIVATE_PROJECT_NOTICE, offer)
        self.assertIs(
            harness.memory.project_activation().state,
            ActivationState.NEVER_ACTIVATED,
        )
        gui.approve()
        self.assertTrue(gui.controller.deactivate())
        gui.pump.settle()
        gui.close()

        self.assertEqual(gui.controller.exit_status, 0)
        self.assertIn("You: activate project 1", gui.view.lines)
        self.assertIn("Maria: Project 1 is now the active project.", gui.view.lines)
        self.assertIn("Maria: No project is active now.", gui.view.lines)
        self.assertEqual(gui.view.project_items[0], ("1: Garden",))
        self.assertIn(("1: Garden (active)",), gui.view.project_items)
        self.assertIs(
            harness.memory.project_activation().state,
            ActivationState.INACTIVE,
        )


class WindowProjectTests(WindowTestCase):
    """The real window's selector, on Qt's offscreen platform."""

    def items(self) -> list[str]:
        """Return the selector's items as the window shows them."""
        choice = self.window.project_choice
        return [choice.itemText(index) for index in range(choice.count())]

    def test_the_selector_shows_plain_text_and_its_buttons_take_no_focus(
        self,
    ) -> None:
        window = self.open(
            FakeSession(choices=(ProjectChoices(((1, "<b>Garden</b>"),), 1),))
        )

        self.assertTrue(wait_until(lambda: window.project_choice.count() == 1))
        self.assertEqual(self.items(), ["1: <b>Garden</b> (active)"])
        self.assertFalse(window.project_choice.isEditable())
        self.assertEqual(window.project_label.text(), PROJECT_LABEL)
        self.assertEqual(window.project_label.textFormat(), Qt.TextFormat.PlainText)
        for button, label in (
            (window.activate_button, ACTIVATE_LABEL),
            (window.deactivate_button, DEACTIVATE_LABEL),
        ):
            with self.subTest(button=label):
                self.assertEqual(button.text(), label)
                self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.NoFocus)
                self.assertTrue(button.isEnabled())

    def test_the_buttons_submit_the_chosen_items_command_and_their_own(
        self,
    ) -> None:
        session = FakeSession(choices=(LISTED,))
        window = self.open(session)
        self.assertTrue(wait_until(lambda: window.project_choice.count() == 2))

        window.project_choice.setCurrentIndex(1)
        QTest.mouseClick(window.activate_button, Qt.MouseButton.LeftButton)
        self.assertTrue(self.settled())
        QTest.mouseClick(window.deactivate_button, Qt.MouseButton.LeftButton)
        self.assertTrue(self.settled())

        self.assertEqual(session.requests, ["activate project 2", "deactivate project"])

    def test_no_projects_disables_activate_alone(self) -> None:
        window = self.open(FakeSession(choices=(ProjectChoices(),)))

        self.assertTrue(wait_until(lambda: window.project_choice.count() == 1))
        self.assertEqual(self.items(), [NO_PROJECTS_ITEM])
        self.assertFalse(window.activate_button.isEnabled())
        self.assertTrue(window.deactivate_button.isEnabled())

    def test_a_refresh_keeps_the_chosen_position(self) -> None:
        window = self.open(FakeSession(choices=(LISTED, SECOND_ACTIVE)))
        self.assertTrue(wait_until(lambda: window.project_choice.count() == 2))
        window.project_choice.setCurrentIndex(1)

        self.type_request("hello")
        self.assertTrue(self.settled())
        self.assertTrue(
            wait_until(lambda: self.items() == ["1: Garden", "2: Self review (active)"])
        )

        self.assertEqual(window.project_choice.currentIndex(), 1)


if __name__ == "__main__":
    unittest.main()
