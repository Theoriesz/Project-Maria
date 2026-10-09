"""Focused tests for the three reminder-delivery commands (V1-23-R1).

They pin the planner's exact routing and the reminders tool's content-free
replies over a temporary database, from never set through on and off, and
the delivery line ``read reminder`` adds for a current claim. End to end,
through a real session, Brain, planner, permissions, tools, and memory, each
change is offered for confirmation with its own fixed notice, a model can
never select any of the three, and a due-reminder notice is never recorded,
kept in the conversation, or sent to a model. The contract and permission
inventories are pinned with the other actions'.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from core.action_contracts import BUILT_IN_ACTION_CONTRACTS
from core.memory import Memory
from core.permissions import CONFIRMATION_NOTICES
from core.plan_compiler import ProposalRejection
from core.planner import Planner
from core.reminder_delivery import ReminderDeliverySeam
from core.session import MariaSession
from core.task_records import (
    DELIVERY_CHANNEL_CLI,
    DELIVERY_CHANNEL_DESKTOP,
    DELIVERY_CHANNEL_GUI,
    DeliveryClaim,
    DeliveryCounts,
    DeliverySetting,
    DeliverySettingState,
    DeliveryStorageError,
)
from core.time_resolution import TimingFailure
from gui.session_worker import DueReminderNotice, SessionEnded, SessionWorker
from tests.gui_fakes import WAIT_SECONDS
from tests.test_context import everything_sent
from tests.test_task_reminder_model import (
    REFERENCE,
    ZONE,
    _ModelPathCase,
    _step,
    proposal,
)
from tests.zone_fixtures import CENTRAL_RULES, SYNTHETIC_KEY, SyntheticZones
from tools.reminders import (
    DELIVERY_CHANGE_FAILED_MESSAGE,
    DELIVERY_DISABLED_MESSAGE,
    DELIVERY_ENABLED_MESSAGE,
    DELIVERY_STATUS_FAILED_MESSAGE,
    RemindersTool,
)

COMMANDS = {
    "reminder delivery": "delivery_status",
    "enable reminder delivery": "enable_delivery",
    "disable reminder delivery": "disable_delivery",
}
COUNTS = (
    "For your reminders' current saved times: 0 displayed, 0 unconfirmed, 0 "
    "not displayed. Maria cannot see whether the Windows task is installed."
)


class DeliveryCommandRoutingTests(unittest.TestCase):
    """Each command plans exactly one argument-free reminders step."""

    def setUp(self) -> None:
        self.planner = Planner(logger=Mock(spec=logging.Logger))

    def steps(self, command: str) -> list[tuple[str, str, dict[str, object]]]:
        """Return the planned steps as plain tuples."""
        return [
            (step.tool_name, step.action, dict(step.arguments))
            for step in self.planner.create_plan(command).steps
        ]

    def test_each_command_routes_to_its_action_in_any_letter_case(self) -> None:
        for command, action in COMMANDS.items():
            for typed in (command, command.upper(), f"  {command.title()}  "):
                with self.subTest(typed=typed):
                    self.assertEqual(
                        self.steps(typed),
                        [("reminders", action, {})],
                    )
                    self.assertIsNone(
                        BUILT_IN_ACTION_CONTRACTS.validate("reminders", action, {})
                    )

    def test_a_command_with_anything_more_plans_no_delivery_step(self) -> None:
        for typed in (
            "enable reminder delivery now",
            "disable reminder delivery 1",
            "reminder delivery status",
            "enable reminders delivery",
        ):
            with self.subTest(typed=typed):
                actions = {action for _tool, action, _arguments in self.steps(typed)}
                self.assertFalse(actions & set(COMMANDS.values()))

    def test_no_argument_is_accepted(self) -> None:
        for action in COMMANDS.values():
            with self.subTest(action=action):
                self.assertIsNotNone(
                    BUILT_IN_ACTION_CONTRACTS.validate(
                        "reminders",
                        action,
                        {"id": 1},
                    )
                )


class DeliveryCommandReplyTests(unittest.TestCase):
    """The tool's three replies are content-free and record no summary."""

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        logger = Mock(spec=logging.Logger)
        self.memory = Memory(Path(temporary.name) / "db" / "maria.db", logger=logger)
        self.memory.initialize()
        self.tool = RemindersTool(memory=self.memory, logger=logger)
        stamps = iter(
            ("2027-11-01T08:00:00Z", "2027-11-01T08:30:00Z", "2027-11-01T09:00:00Z")
        )
        clock = patch("core.memory.task_timestamp", side_effect=lambda: next(stamps))
        clock.start()
        self.addCleanup(clock.stop)

    def run_action(self, action: str) -> tuple[str, object]:
        """Run one delivery action and return its message and data."""
        result = self.tool.execute(action, {})
        self.assertIs(result.success, True)
        self.assertIsNone(result.recorded_message)
        return result.message, result.data

    def test_delivery_is_off_until_turned_on_and_says_since_when(self) -> None:
        self.assertEqual(
            self.run_action("delivery_status"),
            (
                f"Reminder delivery is off. {COUNTS}",
                {"delivery": DeliverySettingState.NEVER_SET.value},
            ),
        )

        self.assertEqual(
            self.run_action("enable_delivery"),
            (
                DELIVERY_ENABLED_MESSAGE,
                {"delivery": DeliverySettingState.ENABLED.value},
            ),
        )
        # Turning it on again changes nothing, so its time stays the first.
        self.run_action("enable_delivery")
        self.assertEqual(
            self.run_action("delivery_status")[0],
            f"Reminder delivery is on, since 2027-11-01 08:00 UTC. {COUNTS}",
        )

        self.assertEqual(
            self.run_action("disable_delivery"),
            (
                DELIVERY_DISABLED_MESSAGE,
                {"delivery": DeliverySettingState.DISABLED.value},
            ),
        )
        self.assertEqual(
            self.run_action("delivery_status")[0],
            f"Reminder delivery is off, since 2027-11-01 09:00 UTC. {COUNTS}",
        )

    def test_the_replies_are_the_approved_wording(self) -> None:
        self.assertEqual(
            DELIVERY_ENABLED_MESSAGE,
            "Reminder delivery is on. Maria cannot see whether the Windows task "
            "is installed; type reminder delivery to see the status.",
        )
        self.assertEqual(
            DELIVERY_DISABLED_MESSAGE,
            "Reminder delivery is off. Maria starts no new reminder notices; one "
            "already started may still appear.",
        )

    def test_an_unreadable_setting_reads_off_with_its_fixed_sentence(self) -> None:
        with closing(sqlite3.connect(self.memory.database_path)) as connection:
            connection.execute(
                "INSERT INTO reminder_delivery_settings (id, enabled, changed_at) "
                "VALUES (1, 1, CAST(? AS BLOB))",
                ("2027-11-01T08:00:00Z",),
            )
            connection.commit()

        self.assertEqual(
            self.run_action("delivery_status"),
            (
                "Maria could not read the reminder delivery setting, so "
                f"delivery is off. {COUNTS}",
                {"delivery": DeliverySettingState.UNREADABLE.value},
            ),
        )


# A reminder saved for Tuesday 2027-11-02 18:30 at +01:00, and the instant a
# check claims it at, fifteen minutes later.
EVENING = f"2027-11-02T18:30:00+01:00[{SYNTHETIC_KEY}]"
EVENING_UTC = "2027-11-02T17:30:00Z"
CLAIMED_AT = "2027-11-02T17:45:00Z"
LATER = f"2027-11-03T18:30:00+01:00[{SYNTHETIC_KEY}]"
LATER_UTC = "2027-11-03T17:30:00Z"


class DeliveryLineTests(unittest.TestCase):
    """``read reminder`` adds one delivery line for a current claim (A21)."""

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        logger = Mock(spec=logging.Logger)
        self.memory = Memory(Path(temporary.name) / "db" / "maria.db", logger=logger)
        self.memory.initialize()
        self.tool = RemindersTool(memory=self.memory, logger=logger)
        self.zones = SyntheticZones(self, {SYNTHETIC_KEY: CENTRAL_RULES})
        self.memory.set_reminder_delivery(True)

    def claimed(self, channel: str, outcome: str | None) -> int:
        """Store a due reminder, claim it through ``channel``, record ``outcome``."""
        reminder_id = self.memory.create_reminder("call mom", EVENING, EVENING_UTC)
        (item,) = self.memory.reminder_delivery_candidates(CLAIMED_AT, 6)
        claim = self.memory.claim_reminder_delivery(
            item,
            channel,
            CLAIMED_AT,
            CLAIMED_AT,
        ).claim
        if outcome is not None:
            self.memory.record_reminder_delivery(claim, outcome)
        return reminder_id

    def read(self, reminder_id: int) -> str:
        """Return what ``read reminder`` shows, checking its recorded summary."""
        result = self.tool.execute("read", {"id": reminder_id})
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.recorded_message, f"Displayed reminder {reminder_id}.")
        return result.message

    def test_no_line_is_added_before_a_claim(self) -> None:
        reminder_id = self.memory.create_reminder("call mom", EVENING, EVENING_UTC)

        shown = self.read(reminder_id)

        self.assertTrue(shown.endswith("Context: off"))
        self.assertNotIn("Delivery:", shown)

    def test_each_channel_and_outcome_has_its_exact_line(self) -> None:
        started = "Delivery: started at 2027-11-02 17:45 UTC"
        for channel, phrase in (
            (DELIVERY_CHANNEL_DESKTOP, "as a desktop notice"),
            (DELIVERY_CHANNEL_CLI, "in the command line"),
            (DELIVERY_CHANNEL_GUI, "in the desktop window"),
        ):
            for outcome, line in (
                (
                    "displayed",
                    f"{started} {phrase}, and displayed. Maria cannot tell "
                    "whether you read it.",
                ),
                (
                    "failed",
                    f"{started} {phrase}, but it could not be displayed. Maria "
                    "will not try again unless you move it to a different time.",
                ),
                (
                    None,
                    f"{started} {phrase}. Maria cannot confirm that it was "
                    "displayed, and will not show it again unless you move it "
                    "to a different time.",
                ),
            ):
                with self.subTest(channel=channel, outcome=outcome):
                    reminder_id = self.claimed(channel, outcome)

                    shown = self.read(reminder_id)

                    self.assertEqual(shown.splitlines()[-1], line)
                    self.assertEqual(shown.count("Delivery:"), 1)
                    self.memory.delete_reminder(reminder_id)

    def test_a_new_time_drops_the_line_and_listings_never_show_it(self) -> None:
        reminder_id = self.claimed(DELIVERY_CHANNEL_DESKTOP, "displayed")
        listing = self.tool.execute("list", {"status": "open"}).message
        due = self.tool.execute("due", {"as_of": CLAIMED_AT}).message

        self.memory.reschedule_reminder(reminder_id, LATER, LATER_UTC)

        self.assertNotIn("Delivery:", listing)
        self.assertNotIn("Delivery:", due)
        self.assertNotIn("Delivery:", self.read(reminder_id))

    def test_the_status_counts_the_current_claims(self) -> None:
        self.claimed(DELIVERY_CHANNEL_DESKTOP, "displayed")
        self.claimed(DELIVERY_CHANNEL_CLI, None)
        self.claimed(DELIVERY_CHANNEL_GUI, "failed")

        result = self.tool.execute("delivery_status", {})

        self.assertTrue(result.success)
        self.assertIn(
            "For your reminders' current saved times: 1 displayed, 1 "
            "unconfirmed, 1 not displayed.",
            result.message,
        )
        self.assertEqual(
            self.memory.reminder_delivery_counts(),
            DeliveryCounts(1, 1, 1),
        )


class DeliveryFailureReplyTests(unittest.TestCase):
    """A failing store gives each action its fixed, content-free failure."""

    def setUp(self) -> None:
        self.memory = Mock(spec=Memory)
        self.logger = Mock(spec=logging.Logger)
        self.tool = RemindersTool(memory=self.memory, logger=self.logger)

    def test_a_storage_failure_is_a_fixed_reply_and_a_fixed_log_line(self) -> None:
        failure = DeliveryStorageError("Unable to read or write reminder delivery.")
        self.memory.reminder_delivery_setting.side_effect = failure
        self.memory.set_reminder_delivery.side_effect = failure

        status = self.tool.execute("delivery_status", {})
        enable = self.tool.execute("enable_delivery", {})
        disable = self.tool.execute("disable_delivery", {})

        self.assertEqual(
            (status.success, status.message),
            (False, DELIVERY_STATUS_FAILED_MESSAGE),
        )
        for result in (enable, disable):
            self.assertEqual(
                (result.success, result.message),
                (False, DELIVERY_CHANGE_FAILED_MESSAGE),
            )
        self.assertEqual(
            [call.args for call in self.logger.warning.call_args_list],
            [
                ("Reminder delivery status failed.",),
                ("Reminder delivery change failed.",),
                ("Reminder delivery change failed.",),
            ],
        )
        self.assertEqual(
            (DELIVERY_STATUS_FAILED_MESSAGE, DELIVERY_CHANGE_FAILED_MESSAGE),
            (
                "Unable to read reminder delivery.",
                "Unable to change reminder delivery.",
            ),
        )

    def test_a_change_the_store_did_not_make_is_a_failure(self) -> None:
        self.memory.set_reminder_delivery.return_value = DeliverySetting(
            DeliverySettingState.DISABLED,
            "2027-11-01T08:00:00Z",
        )

        result = self.tool.execute("enable_delivery", {})

        self.assertEqual(
            (result.success, result.message),
            (False, DELIVERY_CHANGE_FAILED_MESSAGE),
        )


class DeliveryThroughSessionTests(_ModelPathCase):
    """The three commands end to end, through a real session (A9, A25)."""

    def setUp(self) -> None:
        super().setUp()
        # Every record write is stamped from here: 08:10, 08:20, then a
        # minute apart.
        stamps = iter(
            (
                "2027-10-30T08:10:00Z",
                *(f"2027-10-30T08:{minute}:00Z" for minute in range(20, 60)),
            )
        )
        clock = patch("core.memory.task_timestamp", side_effect=lambda: next(stamps))
        clock.start()
        self.addCleanup(clock.stop)

    def setting(self) -> DeliverySettingState:
        """Return what the delivery setting says now."""
        return self.harness.memory.reminder_delivery_setting().state

    def test_each_change_is_confirmed_with_its_own_notice(self) -> None:
        enable = CONFIRMATION_NOTICES["reminders.enable_delivery"]
        disable = CONFIRMATION_NOTICES["reminders.disable_delivery"]

        offered = self.offer("enable reminder delivery")
        self.assertIn(enable, offered.text)
        self.assertNotIn(disable, offered.text)
        self.assertIs(self.session.cancel(), True)
        self.assertIs(self.setting(), DeliverySettingState.NEVER_SET)

        self.offer("enable reminder delivery")
        self.assertEqual(self.session.confirm().text, DELIVERY_ENABLED_MESSAGE)
        self.assertIs(self.setting(), DeliverySettingState.ENABLED)

        offered = self.offer("disable reminder delivery")
        self.assertIn(disable, offered.text)
        self.assertNotIn(enable, offered.text)
        self.assertIs(self.session.cancel(), True)
        self.assertIs(self.setting(), DeliverySettingState.ENABLED)

        self.offer("disable reminder delivery")
        self.assertEqual(self.session.confirm().text, DELIVERY_DISABLED_MESSAGE)
        self.assertIs(self.setting(), DeliverySettingState.DISABLED)

        status = self.ask("reminder delivery")
        self.assertFalse(status.needs_confirmation)
        self.assertTrue(
            status.text.startswith(
                "Reminder delivery is off, since 2027-10-30 08:20 UTC."
            )
        )
        self.assertEqual(self.provider.calls, 0)

    def test_each_command_records_its_ordinary_content_free_reply(self) -> None:
        self.offer("enable reminder delivery")
        self.session.confirm()
        status = self.ask("reminder delivery").text

        self.assertEqual(
            self.harness.interactions()[-2:],
            [
                ("enable reminder delivery", DELIVERY_ENABLED_MESSAGE),
                ("reminder delivery", status),
            ],
        )

    def test_a_model_can_never_select_a_delivery_action(self) -> None:
        for action in ("delivery_status", "enable_delivery", "disable_delivery"):
            with self.subTest(action=action):
                self.assertRefused(
                    "please make sure my reminders pop up",
                    proposal(_step("reminders", action, {})),
                    ProposalRejection.NOT_MODEL_ELIGIBLE,
                )
                self.assertIs(self.setting(), DeliverySettingState.NEVER_SET)

    def test_a_fresh_request_for_a_shown_time_that_passed_is_refused(self) -> None:
        memory = self.harness.memory
        memory.set_reminder_delivery(True)
        reminder_id = memory.create_reminder(
            "stir",
            f"2027-10-30T09:00:00+02:00{ZONE}",
            "2027-10-30T07:00:00Z",
        )
        seam = ReminderDeliverySeam(memory, DELIVERY_CHANNEL_CLI, lambda: REFERENCE)
        seam.record(seam.claim().notice, True)
        memory.reschedule_reminder(
            reminder_id,
            f"2027-10-31T10:00:00+01:00{ZONE}",
            "2027-10-31T09:00:00Z",
        )

        # 09:00 today has passed at the reference, 10:03 local.
        response = self.ask(f"reschedule reminder {reminder_id}: today 09:00")

        self.assertEqual(
            response.text,
            f"{TimingFailure.PAST.message} Nothing was saved.",
        )
        self.assertFalse(response.needs_confirmation)
        reminder = memory.get_reminder(reminder_id)
        self.assertEqual(
            (reminder.schedule_revision, reminder.delivery, reminder.remind_utc),
            (2, None, "2027-10-31T09:00:00Z"),
        )
        self.assertIsNone(seam.claim().notice)


class NoticeNeverRecordedTests(_ModelPathCase):
    """A notice is never recorded, kept, or sent to a model (A24)."""

    TEXT = "private-notice-text-5f17a2"

    def setUp(self) -> None:
        super().setUp()
        memory = self.harness.memory
        memory.set_reminder_delivery(True)
        memory.create_reminder(
            self.TEXT,
            f"2027-10-30T09:00:00+02:00{ZONE}",
            "2027-10-30T07:00:00Z",
        )

    def delivering_session(self, channel: str) -> MariaSession:
        """Return a real session over the harness Brain, with a seam."""
        return MariaSession(
            self.harness.brain,
            logger=self.harness.logger,
            delivery=ReminderDeliverySeam(
                self.harness.memory,
                channel,
                lambda: REFERENCE,
            ),
        )

    def assertNothingReachedBrain(self) -> None:
        self.harness.create_plan.assert_not_called()
        self.harness.validate_action.assert_not_called()
        self.harness.check.assert_not_called()
        self.harness.execute_tool.assert_not_called()
        self.assertEqual(self.provider.calls, 0)

    def test_a_command_line_notice_leaves_no_record_or_trace(self) -> None:
        session = self.delivering_session(DELIVERY_CHANNEL_CLI)
        before = self.harness.row_counts()

        notice = session.claim_due_reminders()
        session.record_reminder_notice(notice, displayed=True)

        self.assertIn(self.TEXT, notice.text)
        self.assertEqual(self.harness.row_counts(), before)
        self.assertEqual(session.conversation, ())
        self.assertNothingReachedBrain()

        # A conversational turn afterwards carries nothing of the notice.
        session.submit("how is the weather where you are")
        self.assertEqual(self.provider.calls, 1)
        self.assertNotIn(self.TEXT, everything_sent(self.provider.requests))
        self.assertNotIn(self.TEXT, str(self.harness.interactions()))
        kept = [(turn.user, turn.assistant) for turn in session.conversation]
        self.assertNotIn(self.TEXT, str(kept))

    def test_a_window_notice_leaves_no_record_or_trace(self) -> None:
        session = self.delivering_session(DELIVERY_CHANNEL_GUI)
        before = self.harness.row_counts()
        received: queue.SimpleQueue[object] = queue.SimpleQueue()
        worker = SessionWorker(lambda: session, received.put)
        # Ended even when an assertion fails first: the worker is not a daemon.
        self.addCleanup(lambda: (worker.close(), worker.join(WAIT_SECONDS)))
        worker.start()

        notice = received.get(timeout=WAIT_SECONDS)
        worker.acknowledge_reminder_notice(notice.serial)
        worker.close()
        self.assertTrue(worker.join(WAIT_SECONDS))

        self.assertIsInstance(notice, DueReminderNotice)
        self.assertIn(self.TEXT, notice.text)
        self.assertEqual(received.get(timeout=WAIT_SECONDS), SessionEnded(failed=False))
        self.assertEqual(self.harness.row_counts(), before)
        self.assertEqual(
            self.harness.memory.get_reminder(1).delivery.outcome,
            "displayed",
        )
        self.assertNothingReachedBrain()
        self.assertEqual(
            self.harness.memory.reminder_delivery_counts(),
            DeliveryCounts(1, 0, 0),
        )
        self.assertEqual(
            notice_claims(self.harness.memory),
            [DeliveryClaim(1, 1)],
        )


def notice_claims(memory: Memory) -> list[DeliveryClaim]:
    """Return the claims of every reminder that has one, by id."""
    return [
        DeliveryClaim(reminder.reminder_id, reminder.schedule_revision)
        for reminder in memory.list_reminders("open")
        if reminder.delivery is not None
    ]


if __name__ == "__main__":
    unittest.main()
