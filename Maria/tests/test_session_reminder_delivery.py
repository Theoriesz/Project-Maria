"""Focused tests for in-session reminder delivery (V1-23-R1).

They cover the session's delivery seam, the command line's check before
each prompt, and how ``main.py`` gives each interface's session its seam,
with a fake store behind the real seam and a spec-limited Brain double, so
no database, tool, provider, window, or real notice is involved.
"""

from __future__ import annotations

import logging
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import Mock, patch

import main as main_module
from core.brain import BrainResponse
from core.planner import Plan, PlanStep
from core.reminder_delivery import ReminderDeliverySeam, notice_entry
from core.session import MariaSession
from core.task_records import (
    DELIVERY_CHANNEL_CLI,
    DELIVERY_CHANNEL_GUI,
    DeliveryCandidate,
    DeliveryStorageError,
)
from tests.gui_fakes import window_module
from tests.test_reminder_delivery import AS_OF, NOW, FakeStore, candidate

CALL_MOM = candidate(1, text="call mom")
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


class ClaimingStore(FakeStore):
    """A fake store that, as the real one does, offers a claimed reminder no more."""

    def reminder_delivery_candidates(
        self,
        as_of: str,
        limit: int,
    ) -> list[DeliveryCandidate]:
        claimed = set(self.claimed_ids())
        self.calls.append(("candidates", as_of, limit))
        return [
            item for item in self.candidates if item.reminder_id not in claimed
        ][:limit]


def _seam(store: FakeStore) -> ReminderDeliverySeam:
    """Return the real command-line seam over a fake store and a fixed clock."""
    return ReminderDeliverySeam(store, DELIVERY_CHANNEL_CLI, lambda: NOW)


class SessionSeamTests(unittest.TestCase):
    """The session claims and records through its seam, and nothing else."""

    def setUp(self) -> None:
        self.brain = Mock(spec=main_module.MariaBrain)
        self.logger = Mock(spec=logging.Logger)
        self.store = FakeStore([CALL_MOM])

    def session(self, store: FakeStore | None = None) -> MariaSession:
        """Return a session over the Brain double, with a seam when given a store."""
        return MariaSession(
            self.brain,
            self.logger,
            delivery=None if store is None else _seam(store),
        )

    def test_a_session_without_a_seam_claims_and_records_nothing(self) -> None:
        notice = self.session(self.store).claim_due_reminders()
        session = self.session()

        self.assertIs(session.delivers_reminders, False)
        self.assertIsNone(session.claim_due_reminders())
        self.assertIsNone(session.record_reminder_notice(notice, displayed=True))

        self.assertIsNotNone(notice)
        self.assertNotIn("record", [call[0] for call in self.store.calls])
        self.assertEqual(self.brain.method_calls, [])
        self.logger.error.assert_not_called()

    def test_a_check_and_a_record_leave_a_pending_approval_confirmable(
        self,
    ) -> None:
        self.brain.process.return_value = BrainResponse(
            "Confirmation required: filesystem.rename requires explicit confirmation.",
            needs_confirmation=True,
            plan=RENAME_PLAN,
        )
        completed = BrainResponse("Renamed old.txt to new.txt")
        self.brain.execute_confirmed_plan.return_value = completed
        session = self.session(self.store)
        session.submit("rename old.txt to new.txt")
        pending = session.pending_approval
        conversation = session.conversation
        supplied = session.supplied_context

        notice = session.claim_due_reminders()
        session.record_reminder_notice(notice, displayed=True)

        self.assertEqual(notice.text, notice_entry(CALL_MOM, AS_OF))
        self.assertIn(("record", 1, 1, "displayed"), self.store.calls)
        self.assertIsNotNone(pending)
        self.assertIs(session.pending_approval, pending)
        self.assertEqual(session.conversation, conversation)
        self.assertEqual(session.supplied_context, supplied)
        self.assertIs(session.confirm(), completed)
        (approved,), _ = self.brain.execute_confirmed_plan.call_args
        self.assertEqual(approved.fingerprint, pending.fingerprint)
        self.logger.error.assert_not_called()
        self.logger.warning.assert_not_called()

    def test_a_claim_and_its_record_touch_nothing_else_in_the_session(self) -> None:
        session = self.session(self.store)

        notice = session.claim_due_reminders()
        session.record_reminder_notice(notice, displayed=True)

        self.assertIs(session.delivers_reminders, True)
        self.assertEqual(notice.text, notice_entry(CALL_MOM, AS_OF))
        self.assertIn(("record", 1, 1, "displayed"), self.store.calls)
        self.assertEqual(self.brain.method_calls, [])
        self.assertEqual(session.conversation, ())
        self.assertIsNone(session.pending_approval)
        self.logger.error.assert_not_called()

    def test_an_exception_is_logged_by_type_and_returns_nothing(self) -> None:
        session = self.session(self.store)

        def failing(*_arguments: object) -> object:
            raise RuntimeError("call mom")

        self.store.reminder_delivery_setting = failing  # type: ignore[method-assign]

        self.assertIsNone(session.claim_due_reminders())
        self.logger.error.assert_called_once_with(
            "The reminder check failed: %s.",
            "RuntimeError",
        )

    def test_a_failing_record_is_contained_and_logged_by_type(self) -> None:
        session = self.session(self.store)
        notice = session.claim_due_reminders()

        def failing(*_arguments: object) -> object:
            raise RuntimeError("call mom")

        # One claim's failed write is contained by the subsystem itself.
        self.store.record_reminder_delivery = failing  # type: ignore[method-assign]
        with self.assertLogs("maria.reminder_delivery", "ERROR") as logs:
            session.record_reminder_notice(notice, displayed=True)

        self.assertEqual(
            logs.output,
            [
                "ERROR:maria.reminder_delivery:Could not record the delivery "
                "of reminder 1: RuntimeError.",
            ],
        )
        self.logger.error.assert_not_called()

        # Anything else the seam raises stops at the session's boundary.
        session.record_reminder_notice(
            object(),  # type: ignore[arg-type]
            displayed=True,
        )

        self.logger.error.assert_called_once_with(
            "Recording a reminder notice failed: %s.",
            "ValueError",
        )

    def test_a_keyboard_interrupt_is_never_contained(self) -> None:
        self.store.claims[1] = KeyboardInterrupt()
        session = self.session(self.store)

        with self.assertRaises(KeyboardInterrupt):
            session.claim_due_reminders()


class CommandLineCheckTests(unittest.TestCase):
    """The command line checks before each prompt, inside its own handling."""

    def run_cli(self, store: FakeStore, inputs: list[str]) -> tuple[int, str]:
        """Run the command line over a Brain double and this store's seam."""
        brain = Mock(spec=main_module.MariaBrain)
        stdout = StringIO()
        with (
            patch("builtins.input", side_effect=inputs),
            redirect_stdout(stdout),
            redirect_stderr(StringIO()),
        ):
            status = main_module.run_cli(
                brain_factory=lambda: brain,
                delivery_factory=lambda _brain: _seam(store),
            )
        brain.process.assert_not_called()
        return status, stdout.getvalue()

    def test_a_due_reminder_prints_before_the_prompt_and_is_displayed(self) -> None:
        store = ClaimingStore([CALL_MOM])

        status, output = self.run_cli(store, ["exit"])

        self.assertEqual(status, 0)
        self.assertIn(f"Maria: {notice_entry(CALL_MOM, AS_OF)}\n", output)
        self.assertIn(("record", 1, 1, "displayed"), store.calls)

    def test_the_check_runs_before_every_prompt_and_shows_a_reminder_once(
        self,
    ) -> None:
        store = ClaimingStore([CALL_MOM])

        _status, output = self.run_cli(store, ["", "", "exit"])

        self.assertEqual(store.calls.count(("setting",)), 3)
        self.assertEqual(store.claimed_ids(), [1])
        self.assertEqual(output.count("Reminder 1 is due"), 1)

    def test_an_interrupt_during_the_check_ends_maria_as_at_the_prompt(
        self,
    ) -> None:
        store = ClaimingStore([CALL_MOM])
        store.claims[1] = KeyboardInterrupt()

        with patch.object(MariaSession, "end", autospec=True) as end:
            status, output = self.run_cli(store, ["never read"])

        self.assertEqual(status, 0)
        self.assertTrue(output.endswith("\nMaria: Goodbye.\n"))
        self.assertNotIn("call mom", output)
        end.assert_called_once()

    def test_a_notice_that_cannot_print_is_recorded_not_displayed(self) -> None:
        store = FakeStore([CALL_MOM])
        session = MariaSession(
            Mock(spec=main_module.MariaBrain),
            delivery=_seam(store),
        )

        with patch("builtins.print", side_effect=OSError("closed console")):
            with self.assertLogs("maria", "INFO"):
                main_module.show_due_reminders(session)

        self.assertIn(("record", 1, 1, "failed"), store.calls)

    def test_a_failing_check_still_reaches_the_prompt(self) -> None:
        store = ClaimingStore([CALL_MOM])

        def busy(*_arguments: object) -> object:
            raise DeliveryStorageError("Database is busy. Try again shortly.")

        store.reminder_delivery_setting = busy  # type: ignore[method-assign]

        status, output = self.run_cli(store, ["exit"])

        self.assertEqual(status, 0)
        self.assertTrue(output.endswith("Maria: Goodbye.\n"))
        self.assertNotIn("call mom", output)
        self.assertEqual(store.claimed_ids(), [])

    def test_a_notice_that_cannot_print_is_recorded_and_the_prompt_follows(
        self,
    ) -> None:
        store = ClaimingStore([CALL_MOM])
        real_print = print

        def failing_print(*values: object, **options: object) -> None:
            if values and str(values[0]).startswith("Maria: Reminder"):
                raise OSError("The console is gone.")
            real_print(*values, **options)  # type: ignore[arg-type]

        with patch("builtins.print", side_effect=failing_print):
            status, output = self.run_cli(store, ["", "exit"])

        self.assertEqual(status, 0)
        self.assertTrue(output.endswith("Maria: Goodbye.\n"))
        self.assertNotIn("call mom", output)
        self.assertIn(("record", 1, 1, "failed"), store.calls)

    def test_an_interrupt_while_a_notice_prints_ends_maria_as_at_the_prompt(
        self,
    ) -> None:
        store = ClaimingStore([CALL_MOM])
        real_print = print

        def interrupted_print(*values: object, **options: object) -> None:
            if values and str(values[0]).startswith("Maria: Reminder"):
                raise KeyboardInterrupt()
            real_print(*values, **options)  # type: ignore[arg-type]

        with (
            patch("builtins.print", side_effect=interrupted_print),
            patch.object(MariaSession, "end", autospec=True) as end,
        ):
            status, output = self.run_cli(store, ["never read"])

        self.assertEqual(status, 0)
        self.assertTrue(output.endswith("\nMaria: Goodbye.\n"))
        # Interrupted before it could be recorded: it stays unconfirmed.
        self.assertEqual([call for call in store.calls if call[0] == "record"], [])
        end.assert_called_once()

    def test_a_system_exit_during_the_check_propagates_and_ends_the_session(
        self,
    ) -> None:
        store = ClaimingStore([CALL_MOM])
        store.claims[1] = SystemExit(9)

        with patch.object(MariaSession, "end", autospec=True) as end:
            with self.assertRaises(SystemExit) as raised:
                self.run_cli(store, ["never read"])

        self.assertEqual(raised.exception.code, 9)
        end.assert_called_once()

    def test_an_injected_brain_without_a_delivery_factory_gets_no_seam(
        self,
    ) -> None:
        brain = Mock(spec=main_module.MariaBrain)

        with (
            patch("builtins.input", side_effect=["exit"]),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(main_module.run_cli(brain_factory=lambda: brain), 0)

        self.assertEqual(brain.method_calls, [])


class InterfaceSeamWiringTests(unittest.TestCase):
    """``main.py`` gives each interface's session its seam, on its channel."""

    def setUp(self) -> None:
        # A failing window is logged; with no log configured here, keep the
        # line off the captured standard error.
        quiet = logging.NullHandler()
        maria = logging.getLogger("maria")
        maria.addHandler(quiet)
        self.addCleanup(maria.removeHandler, quiet)

    def runtime_brain(self, store: FakeStore) -> Mock:
        """Return a Brain double, built as the runtime one, over this store."""
        brain = Mock(spec=main_module.MariaBrain)
        brain.memory = store
        return brain

    def window_session(
        self,
        brain_factory: Callable[[], Mock] | None = None,
        delivery_factory: main_module.DeliveryFactory | None = None,
    ) -> MariaSession:
        """Start the window launcher with a stand-in window; return a session."""
        window = Mock(return_value=0)
        with (
            window_module(window),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            status = main_module.run_gui(brain_factory, delivery_factory)
        self.assertEqual(status, 0)
        (session_factory,), _ = window.call_args
        return session_factory()

    def test_the_runtime_command_line_claims_over_brain_memory_as_cli(
        self,
    ) -> None:
        store = ClaimingStore([CALL_MOM])
        brain = self.runtime_brain(store)
        stdout = StringIO()

        with (
            patch.object(main_module.MariaBrain, "from_defaults", return_value=brain),
            patch.object(main_module, "utc_now", return_value=NOW),
            patch("builtins.input", side_effect=["exit"]),
            redirect_stdout(stdout),
            redirect_stderr(StringIO()),
        ):
            status = main_module.run_cli()

        self.assertEqual(status, 0)
        self.assertIn(f"Maria: {notice_entry(CALL_MOM, AS_OF)}\n", stdout.getvalue())
        self.assertEqual(
            [call for call in store.calls if call[0] == "claim"],
            [("claim", 1, DELIVERY_CHANNEL_CLI, AS_OF, AS_OF)],
        )
        self.assertIn(("record", 1, 1, "displayed"), store.calls)
        brain.process.assert_not_called()

    def test_the_runtime_window_session_claims_over_brain_memory_as_gui(
        self,
    ) -> None:
        store = ClaimingStore([CALL_MOM])
        brain = self.runtime_brain(store)

        with (
            patch.object(main_module.MariaBrain, "from_defaults", return_value=brain),
            patch.object(main_module, "utc_now", return_value=NOW),
        ):
            session = self.window_session()
            notice = session.claim_due_reminders()

        self.assertIs(session.brain, brain)
        self.assertIs(session.delivers_reminders, True)
        self.assertEqual(notice.text, notice_entry(CALL_MOM, AS_OF))
        self.assertEqual(
            [call for call in store.calls if call[0] == "claim"],
            [("claim", 1, DELIVERY_CHANNEL_GUI, AS_OF, AS_OF)],
        )
        brain.process.assert_not_called()

    def test_an_injected_delivery_factory_serves_the_window_too(self) -> None:
        store = ClaimingStore([CALL_MOM])
        brain = Mock(spec=main_module.MariaBrain)
        started: list[object] = []

        def delivery_factory(given: object) -> ReminderDeliverySeam:
            started.append(given)
            return ReminderDeliverySeam(store, DELIVERY_CHANNEL_GUI, lambda: NOW)

        session = self.window_session(lambda: brain, delivery_factory)

        self.assertEqual(started, [brain])
        self.assertIs(session.delivers_reminders, True)
        self.assertIsNotNone(session.claim_due_reminders())
        self.assertEqual(store.claimed_ids(), [1])
        self.assertEqual(brain.method_calls, [])

    def test_an_injected_brain_without_a_delivery_factory_gets_no_window_seam(
        self,
    ) -> None:
        brain = Mock(spec=main_module.MariaBrain)

        session = self.window_session(lambda: brain)

        self.assertIs(session.delivers_reminders, False)
        self.assertIsNone(session.claim_due_reminders())
        self.assertEqual(brain.method_calls, [])


if __name__ == "__main__":
    unittest.main()
