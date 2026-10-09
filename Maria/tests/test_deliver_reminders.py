"""Focused tests for the closed-app reminder pass's entry point (V1-23-R1).

Every seam of ``run_pass`` is replaced: the clock, the tracked configuration
(a temporary file), the Windows notice functions, the memory layer, and the
delivery log. No real box is shown, no task is registered, and the live
database and logs are never touched.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import deliver_reminders
from core.desktop_notice import NotificationState
from core.reminder_delivery import MORE_DUE_LINE, NOTICE_TITLE_SEVERAL, notice_entry
from core.task_records import (
    ClaimOutcome,
    ClaimResult,
    DeliverySetting,
    DeliverySettingState,
    DeliveryStorageError,
)
from deliver_reminders import (
    EXIT_CONFIGURATION_UNREADABLE,
    EXIT_DISPLAY_FAILED,
    EXIT_DONE,
    EXIT_NOTICES_UNAVAILABLE,
    EXIT_STORAGE,
    EXIT_UNEXPECTED,
    run_pass,
)
from tests.test_reminder_delivery import AS_OF, NOW, STAMP, FakeStore, candidate

STORAGE_FAILURE = DeliveryStorageError("Unable to read or write reminder delivery.")


class FakeNotices:
    """The Windows notice functions, answering as the test chooses."""

    def __init__(
        self,
        events: list[str],
        *,
        available: bool = True,
        state: NotificationState = NotificationState.ACCEPTS,
        shown: object = True,
    ) -> None:
        self.events = events
        self._available = available
        self.state = state
        self.shown = shown
        self.boxes: list[tuple[str, str]] = []

    def available(self) -> bool:
        self.events.append("available")
        return self._available

    def notification_state(self) -> NotificationState:
        self.events.append("state")
        return self.state

    def show(self, title: str, text: str) -> bool:
        self.events.append("show")
        self.boxes.append((title, text))
        if isinstance(self.shown, BaseException):
            raise self.shown
        return self.shown  # type: ignore[return-value]


class FakeMemory(FakeStore):
    """The memory layer as a pass uses it: a readiness check and the store."""

    def __init__(self, events: list[str], *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.events = events
        self.readiness: object = None

    def delivery_state(self) -> DeliverySetting:
        self.events.append("delivery_state")
        if isinstance(self.readiness, BaseException):
            raise self.readiness
        return self.setting


class PassCase(unittest.TestCase):
    """Build a pass whose every seam is a fake, over a temporary config."""

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config_path = self.root / "config.toml"
        self.database_path = self.root / "database" / "maria.db"
        self.log_dir = self.root / "logs"
        self.config_path.write_text(
            "[paths]\n"
            f'database = "{self.database_path.as_posix()}"\n'
            f'logs = "{self.log_dir.as_posix()}"\n',
            encoding="utf-8",
        )
        self.events: list[str] = []
        self.notices = FakeNotices(self.events)
        self.memory = FakeMemory(self.events)
        self.opened: list[Path] = []
        self.log_dirs: list[Path] = []
        self.log_failure: BaseException | None = None

    def clock(self) -> datetime:
        self.events.append("clock")
        return NOW

    def open_memory(self, database_path: Path) -> FakeMemory:
        self.events.append("open")
        self.opened.append(database_path)
        return self.memory

    def configure_log(self, log_dir: Path) -> None:
        self.events.append("configure_log")
        self.log_dirs.append(log_dir)
        if self.log_failure is not None:
            raise self.log_failure

    def pass_status(self) -> int:
        """Run one pass through the fakes and return its exit status."""
        return run_pass(
            clock=self.clock,
            config_path=self.config_path,
            notices=self.notices,
            memory_factory=self.open_memory,
            configure_log=self.configure_log,
        )


class EarlyExitTests(PassCase):
    """Pin statuses 1, 2, 3, and 0 before anything is claimed, and their order."""

    def test_an_unreadable_configuration_is_status_one_before_windows(self) -> None:
        for content in (None, "[paths\n", "paths = 3\n", b"\xff\xfe"):
            with self.subTest(content=repr(content)):
                self.events.clear()
                if content is None:
                    self.config_path.unlink(missing_ok=True)
                elif isinstance(content, bytes):
                    self.config_path.write_bytes(content)
                else:
                    self.config_path.write_text(content, encoding="utf-8")

                self.assertEqual(self.pass_status(), EXIT_CONFIGURATION_UNREADABLE)
                self.assertEqual(self.events, ["clock"])

    def test_unavailable_notices_are_status_two_before_the_state(self) -> None:
        self.notices = FakeNotices(self.events, available=False)

        self.assertEqual(self.pass_status(), EXIT_NOTICES_UNAVAILABLE)
        self.assertEqual(self.events, ["clock", "available"])

    def test_a_deferring_state_is_done_and_an_unknown_one_is_two(self) -> None:
        for state, status in (
            (NotificationState.DEFERS, EXIT_DONE),
            (NotificationState.UNKNOWN, EXIT_NOTICES_UNAVAILABLE),
        ):
            with self.subTest(state=state):
                self.events.clear()
                self.notices = FakeNotices(self.events, state=state)

                self.assertEqual(self.pass_status(), status)
                self.assertEqual(self.events, ["clock", "available", "state"])
                self.assertEqual(self.opened, [])

    def test_the_state_is_asked_once_before_the_database_is_opened(self) -> None:
        self.memory.candidates = [candidate(1)]

        self.assertEqual(self.pass_status(), EXIT_DONE)

        self.assertEqual(self.events.count("state"), 1)
        self.assertEqual(self.events.count("clock"), 1)
        self.assertEqual(
            self.events[:6],
            ["clock", "available", "state", "open", "delivery_state", "configure_log"],
        )
        self.assertEqual(self.opened, [self.database_path])
        self.assertEqual(self.log_dirs, [self.log_dir])

    def test_a_database_that_is_not_ready_is_status_three_before_the_log(
        self,
    ) -> None:
        self.memory.readiness = STORAGE_FAILURE

        self.assertEqual(self.pass_status(), EXIT_STORAGE)
        self.assertNotIn("configure_log", self.events)
        self.assertEqual(self.memory.calls, [])

    def test_delivery_not_on_is_done_before_the_log_and_any_claim(self) -> None:
        for setting in (
            DeliverySetting(DeliverySettingState.NEVER_SET),
            DeliverySetting(DeliverySettingState.DISABLED, STAMP),
            DeliverySetting(DeliverySettingState.UNREADABLE),
        ):
            with self.subTest(state=setting.state):
                self.events.clear()
                self.memory.setting = setting
                self.memory.candidates = [candidate(1)]

                self.assertEqual(self.pass_status(), EXIT_DONE)
                self.assertNotIn("configure_log", self.events)
                self.assertEqual(self.memory.claimed_ids(), [])

    def test_a_log_that_cannot_be_configured_is_five_and_claims_nothing(
        self,
    ) -> None:
        self.memory.candidates = [candidate(1)]
        self.log_failure = OSError("read-only")

        self.assertEqual(self.pass_status(), EXIT_UNEXPECTED)
        self.assertEqual(self.memory.claimed_ids(), [])
        self.assertNotIn("show", self.events)


class ClaimShowRecordTests(PassCase):
    """Pin statuses after claiming, and the precedence 5 > 4 > 3 > 0."""

    def setUp(self) -> None:
        super().setUp()
        self.memory.candidates = [candidate(1), candidate(2)]

    def test_nothing_due_is_done_without_a_box(self) -> None:
        self.memory.candidates = []

        self.assertEqual(self.pass_status(), EXIT_DONE)
        self.assertEqual(self.notices.boxes, [])

    def test_a_shown_notice_is_done_and_recorded_displayed(self) -> None:
        self.assertEqual(self.pass_status(), EXIT_DONE)

        self.assertEqual(
            self.notices.boxes,
            [
                (
                    NOTICE_TITLE_SEVERAL,
                    "\n\n".join(
                        notice_entry(candidate(index), AS_OF) for index in (1, 2)
                    ),
                )
            ],
        )
        self.assertEqual(
            [call for call in self.memory.calls if call[0] == "record"],
            [("record", 1, 1, "displayed"), ("record", 2, 1, "displayed")],
        )

    def test_a_box_that_fails_or_raises_is_four_and_recorded_failed(self) -> None:
        for shown in (False, OSError("no desktop")):
            with self.subTest(shown=repr(shown)):
                self.memory.calls.clear()
                self.notices = FakeNotices(self.events, shown=shown)

                with self.assertLogs("maria.delivery", "ERROR"):
                    status = self.pass_status()

                self.assertEqual(status, EXIT_DISPLAY_FAILED)
                self.assertEqual(
                    [call[3] for call in self.memory.calls if call[0] == "record"],
                    ["failed", "failed"],
                )

    def test_a_storage_stop_shows_the_retained_claims_and_is_three(self) -> None:
        self.memory.claims[2] = STORAGE_FAILURE

        with self.assertLogs("maria", "ERROR"):
            status = self.pass_status()

        self.assertEqual(status, EXIT_STORAGE)
        (box,) = self.notices.boxes
        self.assertEqual(
            box[1],
            f"{notice_entry(candidate(1), AS_OF)}\n\n{MORE_DUE_LINE}",
        )

    def test_precedence_runs_five_over_four_over_three_over_zero(self) -> None:
        cases = (
            ("storage stop and box fails", STORAGE_FAILURE, False, set(), 4),
            ("unexpected stop and box fails", RuntimeError(), False, set(), 5),
            ("unexpected stop and record fails", RuntimeError(), True, {1}, 5),
            ("box fails and record fails", None, False, {1}, 4),
            ("record fails", None, True, {1}, 3),
            ("disabled stop", ClaimResult(ClaimOutcome.DISABLED), True, set(), 0),
        )
        for label, claim_two, shown, failing, status in cases:
            with self.subTest(case=label):
                self.memory.calls.clear()
                self.memory.claims = {} if claim_two is None else {2: claim_two}
                self.memory.failing_records = failing
                self.notices = FakeNotices(self.events, shown=shown)

                with self.assertLogs("maria", "INFO"):
                    self.assertEqual(self.pass_status(), status)

    def test_an_outcome_write_failing_unexpectedly_is_five_not_three(self) -> None:
        cases = (
            ("storage failure", {1}, set(), True, EXIT_STORAGE),
            ("unexpected failure", set(), {1}, True, EXIT_UNEXPECTED),
            ("one of each", {1}, {2}, True, EXIT_UNEXPECTED),
            ("box fails, then unexpected", set(), {2}, False, EXIT_UNEXPECTED),
        )
        for label, storage, unexpected, shown, status in cases:
            with self.subTest(case=label):
                self.memory.calls.clear()
                self.memory.failing_records = storage
                self.memory.unexpected_records = unexpected
                self.notices = FakeNotices(self.events, shown=shown)

                with self.assertLogs("maria", "ERROR"):
                    self.assertEqual(self.pass_status(), status)
                self.assertEqual(
                    [call[1] for call in self.memory.calls if call[0] == "record"],
                    [1, 2],
                )

    def test_a_failing_candidate_read_is_three_and_shows_nothing(self) -> None:
        def failing(*_arguments: object) -> object:
            raise STORAGE_FAILURE

        self.memory.reminder_delivery_candidates = (  # type: ignore[method-assign]
            failing
        )

        with self.assertLogs("maria.delivery", "ERROR"):
            self.assertEqual(self.pass_status(), EXIT_STORAGE)
        self.assertEqual(self.notices.boxes, [])

    def test_a_first_claim_that_fails_shows_nothing_and_reports_its_kind(
        self,
    ) -> None:
        cases = (
            ("storage failure", STORAGE_FAILURE, EXIT_STORAGE),
            ("unexpected failure", RuntimeError("secret errand"), EXIT_UNEXPECTED),
        )
        for label, failure, status in cases:
            with self.subTest(case=label):
                self.memory.calls.clear()
                self.memory.claims = {1: failure}

                with self.assertLogs("maria", "ERROR") as logs:
                    self.assertEqual(self.pass_status(), status)

                self.assertEqual(self.notices.boxes, [])
                self.assertEqual(self.memory.claimed_ids(), [1])
                self.assertNotIn("record", [call[0] for call in self.memory.calls])
                self.assertNotIn("secret errand", "\n".join(logs.output))


class EntryBoundaryTests(unittest.TestCase):
    """``main`` turns an Exception into status 5 and catches nothing else."""

    def test_an_exception_is_logged_by_type_and_is_status_five(self) -> None:
        with patch.object(
            deliver_reminders,
            "run_pass",
            side_effect=RuntimeError("secret errand"),
        ):
            with self.assertLogs("maria.delivery", "ERROR") as logs:
                self.assertEqual(deliver_reminders.main(), EXIT_UNEXPECTED)

        self.assertEqual(
            logs.output,
            ["ERROR:maria.delivery:The reminder pass failed: RuntimeError."],
        )

    def test_keyboard_interrupt_and_system_exit_pass_through(self) -> None:
        for error in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(error=type(error).__name__):
                with patch.object(deliver_reminders, "run_pass", side_effect=error):
                    with self.assertRaises(type(error)):
                        deliver_reminders.main()

    def test_the_clock_asks_for_the_current_instant_in_utc(self) -> None:
        asked: list[object] = []

        class StandInDatetime:
            """Answer ``now`` with a fixed instant, so no real clock is read."""

            @staticmethod
            def now(tz: object = None) -> datetime:
                asked.append(tz)
                return NOW

        with patch.object(deliver_reminders, "datetime", StandInDatetime):
            moment = deliver_reminders.utc_now()

        self.assertIs(moment, NOW)
        self.assertEqual(asked, [timezone.utc])


class ExistingDatabaseTests(unittest.TestCase):
    """The pass's memory layer never creates a database."""

    def test_a_missing_database_is_status_three_and_is_not_created(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "database" / "maria.db"
            database_path.parent.mkdir()
            memory = deliver_reminders._existing_memory(database_path)

            with self.assertLogs("maria.memory", "ERROR"):
                with self.assertRaises(DeliveryStorageError):
                    memory.delivery_state()

            self.assertFalse(database_path.exists())
            self.assertEqual(list(database_path.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
