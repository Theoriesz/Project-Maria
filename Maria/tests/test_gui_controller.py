"""Tests for the desktop GUI's Qt-free controller (V1-21).

The controller is driven first with recording stand-ins for the window and
the worker, so every decision it makes is visible: which requests it takes,
which answers it accepts, what it shows, and how it closes. It is then driven
through a real worker and a real ``MariaSession`` over a stand-in Brain, to
prove that the approval it confirms is the exact one the session offered.

Nothing here imports Qt, creates a window, contacts a model, or opens a
database. Invisible characters are built with ``chr()``.
"""

from __future__ import annotations

import unittest
from unittest.mock import Mock

import main as main_module
from core.brain import BrainResponse, MariaBrain
from core.constants import APP_NAME, APP_VERSION
from core.planner import Plan, PlanStep
from core.session import (
    CONFIRMATION_REFUSED_MESSAGE,
    MariaSession,
    PendingApproval,
)
from gui.controller import (
    EXIT_COMMANDS,
    ONE_LINE_MESSAGE,
    STATUS_AWAITING_APPROVAL,
    STATUS_BUSY,
    STATUS_CLOSED,
    STATUS_CLOSING,
    STATUS_CLOSING_BUSY,
    STATUS_READY,
    WELCOME_LINES,
    ControllerState,
    GuiController,
)
from gui.session_worker import ApprovalOffer, Reply, SessionEnded
from tests.gui_fakes import (
    FakeSession,
    RecordingView,
    RecordingWorker,
    RunningGui,
)
from tests.test_brain_recording_policy import _quiet_logger


RENAME_PLAN = Plan(
    original_request="rename old.txt to new.txt",
    steps=(
        PlanStep(
            "filesystem",
            "rename",
            {"source": "old.txt", "destination": "new.txt"},
        ),
    ),
)
CONFIRMATION_TEXT = (
    "Confirmation required: filesystem.rename requires explicit confirmation."
)


class ControllerTestCase(unittest.TestCase):
    """A controller over a recording window and a recording worker."""

    def setUp(self) -> None:
        self.view = RecordingView()
        self.worker = RecordingWorker()
        self.controller = GuiController(self.view, self.worker)
        self.controller.start()

    def commands(self) -> list[tuple[object, ...]]:
        """Return what the controller asked of the worker after starting."""
        return self.worker.commands[1:]

    def offer(self, serial: int = 1, text: str = CONFIRMATION_TEXT) -> None:
        """Send a request and answer it with an offer of approval."""
        self.assertTrue(self.controller.send(f"request for offer {serial}"))
        self.controller.receive(ApprovalOffer(serial, text))


class StartTests(ControllerTestCase):
    """The window opens with the command line's banner and nothing else."""

    def test_the_window_opens_with_the_command_lines_banner(self) -> None:
        self.assertEqual(
            self.view.lines,
            [
                f"{APP_NAME} v{APP_VERSION}",
                "Type 'help' for supported commands. Type 'exit' to quit.",
            ],
        )
        self.assertEqual(list(WELCOME_LINES), self.view.lines)
        self.assertEqual(self.worker.commands, [("start",)])
        self.assertEqual(self.view.status, STATUS_READY)
        self.assertEqual(
            self.view.controls,
            {"sending": True, "answering": False, "typing": True},
        )
        self.assertIs(self.controller.state, ControllerState.READY)

    def test_starting_reads_no_record_and_restores_no_history(self) -> None:
        # The only thing starting asks of the worker is to start: no request
        # is sent, so nothing stored is listed, read, or replayed.
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.view.shown_offers, [])


class SendTests(ControllerTestCase):
    """Requests follow the command line's rules, one at a time."""

    def test_a_request_is_stripped_echoed_and_sent_once(self) -> None:
        self.assertTrue(self.controller.send("  calculate 2 + 3 \t"))

        self.assertEqual(self.commands(), [("submit", "calculate 2 + 3")])
        self.assertEqual(self.view.lines[-1], "You: calculate 2 + 3")
        self.assertEqual(self.view.cleared, 1)
        self.assertIs(self.controller.state, ControllerState.BUSY)
        self.assertEqual(self.view.status, STATUS_BUSY)
        self.assertEqual(
            self.view.controls,
            {"sending": False, "answering": False, "typing": True},
        )

    def test_blank_text_is_ignored_and_kept_in_the_input(self) -> None:
        for text in ("", " ", " \t ", chr(10), chr(13) + chr(10)):
            with self.subTest(text=ascii(text)):
                self.assertFalse(self.controller.send(text))

        self.assertEqual(self.commands(), [])
        self.assertEqual(self.view.cleared, 0)
        self.assertEqual(len(self.view.lines), 2)

    def test_a_second_request_while_busy_is_refused(self) -> None:
        self.assertTrue(self.controller.send("first"))

        self.assertFalse(self.controller.send("second"))
        self.assertFalse(self.controller.send("first"))

        self.assertEqual(self.commands(), [("submit", "first")])
        self.assertEqual(self.view.cleared, 1)

    def test_a_request_is_refused_while_an_approval_is_showing(self) -> None:
        self.offer()

        self.assertFalse(self.controller.send("another request"))

        self.assertEqual(len(self.commands()), 1)
        self.assertIs(self.controller.state, ControllerState.AWAITING_APPROVAL)

    def test_text_holding_a_line_break_is_refused_as_the_cli_would(
        self,
    ) -> None:
        """The command line reads one line, so it can never send a break."""
        for separator in (chr(10), chr(13), chr(13) + chr(10)):
            with self.subTest(separator=ascii(separator)):
                text = f"edit note 1: first{separator}second"

                self.assertFalse(self.controller.send(text))
                self.assertEqual(self.view.lines[-1], f"Maria: {ONE_LINE_MESSAGE}")

        self.assertEqual(self.commands(), [])
        self.assertEqual(self.view.cleared, 0)
        self.assertIs(self.controller.state, ControllerState.READY)

    def test_a_break_only_at_either_end_is_stripped_like_any_whitespace(
        self,
    ) -> None:
        self.assertTrue(self.controller.send(chr(10) + "help" + chr(13) + chr(10)))

        self.assertEqual(self.commands(), [("submit", "help")])

    def test_characters_the_cli_accepts_within_a_line_are_kept(self) -> None:
        text = "remember a" + chr(0x2028) + "b" + chr(0x85) + "c" + chr(9) + "d"

        self.assertTrue(self.controller.send(text))

        self.assertEqual(self.commands(), [("submit", text)])

    def test_exit_words_close_the_window_without_reaching_maria(self) -> None:
        for word in ("exit", " EXIT ", "Quit", "q", " Q "):
            with self.subTest(word=word):
                view = RecordingView()
                worker = RecordingWorker()
                controller = GuiController(view, worker)
                controller.start()

                self.assertTrue(controller.send(word))

                self.assertEqual(worker.commands, [("start",), ("close",)])
                self.assertIs(controller.state, ControllerState.CLOSING)
                self.assertEqual(view.cleared, 1)
                self.assertNotIn(f"You: {word.strip()}", view.lines)

    def test_the_exit_words_are_the_command_lines_own(self) -> None:
        self.assertEqual(EXIT_COMMANDS, frozenset(main_module.EXIT_COMMANDS))


class ReplyTests(ControllerTestCase):
    """Replies are shown as Maria's lines and free the window."""

    def test_a_reply_is_shown_and_the_window_is_ready_again(self) -> None:
        self.controller.send("calculate 2 + 3")

        self.controller.receive(Reply("2 + 3 = 5"))

        self.assertEqual(self.view.lines[-1], "Maria: 2 + 3 = 5")
        self.assertIs(self.controller.state, ControllerState.READY)
        self.assertEqual(self.view.status, STATUS_READY)
        self.assertTrue(self.view.controls["sending"])

    def test_a_reply_is_shown_exactly_as_text(self) -> None:
        text = "<b>bold</b> <a href='https://example.invalid'>link</a>\nline two"
        self.controller.send("show me")

        self.controller.receive(Reply(text))

        self.assertEqual(self.view.lines[-1], f"Maria: {text}")


class ApprovalTests(ControllerTestCase):
    """An offer is shown whole and can be answered once, and only by itself."""

    def test_an_offer_shows_the_complete_core_text_with_its_answers(
        self,
    ) -> None:
        text = CONFIRMATION_TEXT + "\nTool: filesystem\nAction: rename"

        self.offer(4, text)

        self.assertEqual(self.view.lines[-1], f"Maria: {text}")
        self.assertEqual(self.view.shown_offers, [(4, text)])
        self.assertTrue(self.view.approval_visible)
        self.assertIs(self.controller.state, ControllerState.AWAITING_APPROVAL)
        self.assertEqual(self.view.status, STATUS_AWAITING_APPROVAL)
        self.assertEqual(
            self.view.controls,
            {"sending": False, "answering": True, "typing": True},
        )

    def test_approving_confirms_exactly_the_offer_shown_once(self) -> None:
        self.offer(2)

        self.assertTrue(self.controller.approve(2))
        self.assertFalse(self.controller.approve(2))
        self.assertFalse(self.controller.cancel(2))

        self.assertEqual(self.commands()[1:], [("confirm", 2)])
        self.assertFalse(self.view.approval_visible)
        self.assertIs(self.controller.state, ControllerState.BUSY)

    def test_cancelling_dismisses_the_offer_shown_once(self) -> None:
        self.offer(5)

        self.assertTrue(self.controller.cancel(5))
        self.assertFalse(self.controller.cancel(5))
        self.assertFalse(self.controller.approve(5))

        self.assertEqual(self.commands()[1:], [("cancel",)])
        self.assertFalse(self.view.approval_visible)

    def test_an_answer_naming_another_offer_is_refused(self) -> None:
        self.offer(7)

        for serial in (6, 8, 0, -7):
            with self.subTest(serial=serial):
                self.assertFalse(self.controller.approve(serial))
                self.assertFalse(self.controller.cancel(serial))

        self.assertEqual(len(self.commands()), 1)
        self.assertTrue(self.view.approval_visible)

    def test_an_answer_with_no_offer_showing_is_refused(self) -> None:
        self.assertFalse(self.controller.approve(1))
        self.assertFalse(self.controller.cancel(1))
        self.controller.send("busy now")
        self.assertFalse(self.controller.approve(1))
        self.assertFalse(self.controller.cancel(1))

        self.assertEqual(self.commands(), [("submit", "busy now")])

    def test_an_answer_from_an_earlier_offer_cannot_answer_a_later_one(
        self,
    ) -> None:
        self.offer(1)
        self.controller.cancel(1)
        self.controller.receive(Reply("Action cancelled."))
        self.offer(2)

        self.assertFalse(self.controller.approve(1))
        self.assertTrue(self.controller.approve(2))

        self.assertEqual(
            [command for command in self.commands() if command[0] == "confirm"],
            [("confirm", 2)],
        )

    def test_the_result_of_an_answer_frees_the_window(self) -> None:
        self.offer(1)
        self.controller.approve(1)

        self.controller.receive(Reply("Renamed old.txt to new.txt"))

        self.assertEqual(self.view.lines[-1], "Maria: Renamed old.txt to new.txt")
        self.assertIs(self.controller.state, ControllerState.READY)


class ClosingTests(ControllerTestCase):
    """The window and its session end together, and closing approves nothing."""

    def test_closing_while_ready_ends_the_session_then_closes(self) -> None:
        self.assertFalse(self.controller.request_close())
        self.assertFalse(self.controller.request_close())

        self.assertEqual(self.commands(), [("close",)])
        self.assertEqual(self.view.status, STATUS_CLOSING)
        self.assertEqual(
            self.view.controls,
            {"sending": False, "answering": False, "typing": False},
        )
        self.assertEqual(self.view.closed, 0)

        self.controller.receive(SessionEnded())

        self.assertEqual(self.view.closed, 1)
        self.assertIs(self.controller.state, ControllerState.CLOSED)
        self.assertEqual(self.view.status, STATUS_CLOSED)
        self.assertTrue(self.controller.request_close())
        self.assertEqual(self.controller.exit_status, 0)

    def test_closing_while_busy_waits_and_never_shows_an_arriving_offer(
        self,
    ) -> None:
        self.controller.send("rename old.txt to new.txt")

        self.assertFalse(self.controller.request_close())

        self.assertEqual(self.view.status, STATUS_CLOSING_BUSY)
        self.assertFalse(self.controller.send("another"))
        self.controller.receive(ApprovalOffer(1, CONFIRMATION_TEXT))
        self.assertFalse(self.controller.approve(1))
        self.assertFalse(self.controller.cancel(1))
        self.assertEqual(self.view.shown_offers, [])
        self.assertNotIn(f"Maria: {CONFIRMATION_TEXT}", self.view.lines)
        self.assertEqual(self.view.closed, 0)

        self.controller.receive(SessionEnded())

        self.assertEqual(
            self.commands(),
            [("submit", "rename old.txt to new.txt"), ("close",)],
        )
        self.assertEqual(self.view.closed, 1)
        self.assertEqual(self.controller.exit_status, 0)

    def test_a_reply_arriving_while_closing_is_still_shown(self) -> None:
        self.controller.send("calculate 2 + 3")
        self.controller.request_close()

        self.controller.receive(Reply("2 + 3 = 5"))

        self.assertEqual(self.view.lines[-1], "Maria: 2 + 3 = 5")
        self.assertIs(self.controller.state, ControllerState.CLOSING)

    def test_closing_with_an_offer_showing_answers_nothing(self) -> None:
        self.offer(3)

        self.controller.request_close()

        self.assertFalse(self.view.approval_visible)
        self.assertFalse(self.controller.approve(3))
        self.assertEqual(self.commands()[1:], [("close",)])

    def test_a_window_failing_while_closing_has_already_asked_for_the_end(
        self,
    ) -> None:
        """The worker is told before the window is touched."""

        def fail() -> None:
            raise RuntimeError("private-view-detail")

        self.view.hide_approval = fail

        with self.assertRaises(RuntimeError):
            self.controller.request_close()

        self.assertEqual(self.commands(), [("close",)])

    def test_a_session_that_ends_unasked_closes_the_window_as_a_failure(
        self,
    ) -> None:
        for state in ("ready", "busy", "awaiting"):
            with self.subTest(state=state):
                view = RecordingView()
                controller = GuiController(view, RecordingWorker())
                controller.start()
                if state == "busy":
                    controller.send("request")
                elif state == "awaiting":
                    controller.send("request")
                    controller.receive(ApprovalOffer(1, CONFIRMATION_TEXT))

                controller.receive(SessionEnded(failed=False))

                self.assertEqual(view.closed, 1)
                self.assertFalse(view.approval_visible)
                self.assertEqual(controller.exit_status, 1)
                self.assertFalse(controller.approve(1))

    def test_the_exit_status_is_a_failure_until_the_session_has_ended(
        self,
    ) -> None:
        """Nothing has ended in order before the window sees the end."""
        self.assertEqual(self.controller.exit_status, 1)
        self.controller.request_close()
        self.assertEqual(self.controller.exit_status, 1)

        self.controller.receive(SessionEnded())

        self.assertEqual(self.controller.exit_status, 0)

    def test_a_failure_while_closing_still_closes_with_a_failure(self) -> None:
        self.controller.send("request")
        self.controller.request_close()

        self.controller.receive(SessionEnded(failed=True))

        self.assertEqual(self.view.closed, 1)
        self.assertEqual(self.controller.exit_status, 1)

    def test_nothing_is_shown_once_the_session_has_ended(self) -> None:
        self.controller.request_close()
        self.controller.receive(SessionEnded())
        shown = list(self.view.lines)

        self.controller.receive(Reply("late"))
        self.controller.receive(ApprovalOffer(9, "late offer"))
        self.controller.receive(SessionEnded(failed=True))

        self.assertEqual(self.view.lines, shown)
        self.assertEqual(self.view.closed, 1)
        self.assertEqual(self.controller.exit_status, 0)
        self.assertFalse(self.controller.send("after the end"))


class FailureClosingTests(unittest.TestCase):
    """A window that failed closes as a failure and accepts nothing more."""

    def opened(self, state: str) -> tuple[RecordingView, RecordingWorker]:
        """Start a controller and bring it to one of its open states."""
        view = RecordingView()
        worker = RecordingWorker()
        self.controller = GuiController(view, worker)
        self.controller.start()
        if state in ("busy", "awaiting"):
            self.controller.send("rename old.txt to new.txt")
        if state == "awaiting":
            self.controller.receive(ApprovalOffer(1, CONFIRMATION_TEXT))
        return view, worker

    def test_a_failed_window_accepts_no_later_action_in_any_state(
        self,
    ) -> None:
        for state in ("ready", "busy", "awaiting"):
            with self.subTest(state=state):
                view, worker = self.opened(state)
                before = list(worker.commands)

                self.controller.close_as_failed()

                self.assertIs(self.controller.state, ControllerState.CLOSING)
                self.assertEqual(worker.commands, [*before, ("close",)])
                self.assertFalse(self.controller.send("another request"))
                self.assertFalse(self.controller.approve(1))
                self.assertFalse(self.controller.cancel(1))
                self.assertEqual(worker.commands, [*before, ("close",)])
                self.assertFalse(view.approval_visible)
                self.assertEqual(
                    view.controls,
                    {"sending": False, "answering": False, "typing": False},
                )
                self.assertEqual(
                    view.status,
                    STATUS_CLOSING_BUSY if state == "busy" else STATUS_CLOSING,
                )

    def test_a_failed_window_ends_its_session_once_and_closes_as_failed(
        self,
    ) -> None:
        view, worker = self.opened("awaiting")

        self.controller.close_as_failed()
        self.controller.close_as_failed()
        self.assertFalse(self.controller.request_close())
        self.controller.receive(SessionEnded(failed=False))

        # The orderly end itself keeps the failure recorded before it.
        self.assertEqual(self.controller.exit_status, 1)
        self.controller.close_as_failed()
        self.assertEqual(worker.commands.count(("close",)), 1)
        self.assertEqual(view.closed, 1)
        self.assertIs(self.controller.state, ControllerState.CLOSED)
        self.assertEqual(self.controller.exit_status, 1)

    def test_the_failure_is_settled_before_a_broken_window_is_touched(
        self,
    ) -> None:
        """The end is asked for even when showing anything fails."""
        view, worker = self.opened("awaiting")

        def broken(*arguments: object, **keywords: object) -> None:
            raise RuntimeError("private-view-detail")

        view.hide_approval = broken
        view.set_status = broken
        view.set_controls = broken

        with self.assertRaises(RuntimeError):
            self.controller.close_as_failed()

        self.assertIs(self.controller.state, ControllerState.CLOSING)
        self.assertEqual(worker.commands[-1], ("close",))
        self.assertFalse(self.controller.approve(1))
        self.assertFalse(self.controller.cancel(1))
        self.assertFalse(self.controller.send("another request"))
        self.assertEqual(worker.commands[-1], ("close",))
        # Repaired, the window then sees an orderly end, which still
        # closes as the failure recorded before anything was shown.
        for member in ("hide_approval", "set_status", "set_controls"):
            delattr(view, member)
        self.controller.receive(SessionEnded(failed=False))
        self.assertEqual(view.closed, 1)
        self.assertEqual(self.controller.exit_status, 1)

    def test_the_window_is_asked_to_close_even_when_clearing_it_fails(
        self,
    ) -> None:
        for member in ("hide_approval", "set_status", "set_controls"):
            with self.subTest(member=member):
                view, worker = self.opened("awaiting")
                self.assertFalse(self.controller.request_close())

                def broken(*arguments: object, **keywords: object) -> None:
                    raise RuntimeError("private-view-detail")

                setattr(view, member, broken)

                with self.assertRaises(RuntimeError):
                    self.controller.receive(SessionEnded())

                self.assertEqual(view.closed, 1)
                self.assertIs(self.controller.state, ControllerState.CLOSED)
                # The window's shield then reports the failure; nothing of
                # the session's end is asked for again.
                self.controller.close_as_failed()
                self.assertEqual(self.controller.exit_status, 1)
                self.assertEqual(worker.commands.count(("close",)), 1)
                self.assertEqual(view.closed, 1)


class RealSessionTests(unittest.TestCase):
    """A real worker and a real session over a stand-in Brain."""

    def setUp(self) -> None:
        self.brain = Mock(spec=MariaBrain)
        self.brain.process.return_value = BrainResponse(
            CONFIRMATION_TEXT,
            needs_confirmation=True,
            plan=RENAME_PLAN,
        )
        self.brain.execute_confirmed_plan.return_value = BrainResponse(
            "Renamed old.txt to new.txt"
        )
        self.sessions: list[MariaSession] = []
        logger = _quiet_logger("tests.gui.controller")

        def session_factory() -> MariaSession:
            session = MariaSession(self.brain, logger=logger)
            self.sessions.append(session)
            return session

        self.gui = RunningGui(self, session_factory, logger=logger)

    def test_approving_confirms_the_plan_the_session_offered(self) -> None:
        self.gui.send("rename old.txt to new.txt")
        (session,) = self.sessions
        offered = session.pending_approval

        self.assertIsInstance(offered, PendingApproval)
        self.gui.approve()

        self.brain.execute_confirmed_plan.assert_called_once()
        (confirmed,), _ = self.brain.execute_confirmed_plan.call_args
        self.assertEqual(confirmed.fingerprint, offered.fingerprint)
        self.assertEqual(confirmed.steps, RENAME_PLAN.steps)
        self.assertEqual(
            self.gui.view.maria_lines,
            [f"Maria: {CONFIRMATION_TEXT}", "Maria: Renamed old.txt to new.txt"],
        )
        self.assertIsNone(session.pending_approval)

    def test_a_pending_approval_replaced_after_the_offer_is_refused(
        self,
    ) -> None:
        """The GUI confirms only the very object the session offered."""
        self.gui.send("rename old.txt to new.txt")
        (session,) = self.sessions
        offered = session.pending_approval
        # A lookalike: the same plan and the same fingerprint, but not the
        # object that was offered. The command line would confirm it.
        session._pending = PendingApproval(offered.plan, offered.fingerprint)

        self.gui.approve()

        self.brain.execute_confirmed_plan.assert_not_called()
        self.assertEqual(
            self.gui.view.maria_lines[-1],
            f"Maria: {CONFIRMATION_REFUSED_MESSAGE}",
        )
        self.assertIsNone(session.pending_approval)

    def test_cancelling_dismisses_the_pending_approval_only(self) -> None:
        self.gui.send("rename old.txt to new.txt")
        (session,) = self.sessions

        self.gui.cancel()

        self.brain.execute_confirmed_plan.assert_not_called()
        self.assertIsNone(session.pending_approval)
        self.assertEqual(self.gui.view.maria_lines[-1], "Maria: Action cancelled.")
        self.brain.process.assert_called_once_with("rename old.txt to new.txt")

    def test_closing_ends_the_session_and_discards_its_conversation(
        self,
    ) -> None:
        self.gui.send("rename old.txt to new.txt")
        (session,) = self.sessions

        self.gui.close()

        self.assertIsNone(session.pending_approval)
        self.assertEqual(session.conversation, ())
        self.brain.execute_confirmed_plan.assert_not_called()
        self.assertEqual(self.gui.view.closed, 1)
        self.assertEqual(self.gui.controller.exit_status, 0)


class CleanupFailureTests(unittest.TestCase):
    """A session that cannot end cleanly closes the window as a failure."""

    def test_a_failing_cleanup_is_a_failed_end_never_an_orderly_close(
        self,
    ) -> None:
        for kind in (RuntimeError, SystemExit, KeyboardInterrupt):
            with self.subTest(kind=kind.__name__):
                session = FakeSession(end_failure=kind("private-end-detail"))
                gui = RunningGui(
                    self,
                    lambda session=session: session,
                    logger=_quiet_logger("tests.gui.controller.cleanup"),
                )
                gui.send("hello")

                gui.close()

                self.assertEqual(session.calls, ["submit", "end"])
                self.assertEqual(gui.view.closed, 1)
                self.assertIs(gui.controller.state, ControllerState.CLOSED)
                self.assertEqual(gui.controller.exit_status, 1)


if __name__ == "__main__":
    unittest.main()
