"""Focused tests for V1-22's reminder tool, ``tools/reminders.py``.

Every test runs the tool over a temporary database and synthetic time zones,
so no expectation depends on the interpreter's own zone data. A reminder is
always shown from its saved values; the zone's current rules only decide
whether a fixed warning or note is added, and never change, refuse, or move
anything.
"""

from __future__ import annotations

import ast
import inspect
import logging
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from core.context import reminder_digest
from core.memory import Memory
from core.task_records import (
    AS_OF_INSTANT_MESSAGE,
    REMIND_TIME_MESSAGE,
    REMINDER_ID_MESSAGE,
    RULES_CHANGED_WARNING,
    RULES_UNCHECKED_NOTE,
    TASK_STATUS_MESSAGE,
    TASK_TEXT_MESSAGE,
)
from core.tool_manager import ConfirmationBinding, ToolExecutionResult
from tools import reminders as reminders_module
from tools.reminders import (
    ADD_FAILED_MESSAGE,
    LIST_FAILED_MESSAGE,
    MORE_REMINDERS_MESSAGE,
    REPEAT_MESSAGE,
    RemindersTool,
    build_tool,
)
from tests.zone_fixtures import (
    CENTRAL_RULES,
    FIXED_PLUS_THREE_RULES,
    SYNTHETIC_KEY,
    SyntheticZones,
)


TEXT_SENTINEL = "private-reminder-text-0b7d42"
MISSING_KEY = "Maria/Missing"
STAMPS = tuple(f"2026-10-04T05:{minute:02d}:00Z" for minute in range(60))

# Tuesday 2027-11-02 18:30 at +01:00 under the central rules.
EVENING = f"2027-11-02T18:30:00+01:00[{SYNTHETIC_KEY}]"
EVENING_UTC = "2027-11-02T17:30:00Z"
EVENING_SHOWN = (
    f"Tuesday 2027-11-02 at 18:30, {SYNTHETIC_KEY} (UTC+01:00), which is "
    "2027-11-02 17:30 UTC"
)
# The repeated hour of Sunday 2027-10-31: 02:30 happens at +02:00, then at
# +01:00, and each occurrence is a correctly chosen, valid time.
FIRST_FOLD = f"2027-10-31T02:30:00+02:00[{SYNTHETIC_KEY}]"
SECOND_FOLD = f"2027-10-31T02:30:00+01:00[{SYNTHETIC_KEY}]"
# A time in a zone no data source provides.
UNCHECKED = f"2027-11-03T08:00:00+05:00[{MISSING_KEY}]"
# Tuesday 2027-11-02 20:00 in a second zone, a steady +03:00: half an hour
# before the evening reminder's instant.
STEADY_KEY = "Maria/Steady"
STEADY = f"2027-11-02T20:00:00+03:00[{STEADY_KEY}]"
STEADY_UTC = "2027-11-02T17:00:00Z"


class _ReminderToolTestCase(unittest.TestCase):
    """A reminder tool over a temporary database and a synthetic zone."""

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database_path = Path(temporary.name) / "db" / "maria.db"
        self.logger = Mock(spec=logging.Logger)
        self.memory = Memory(self.database_path, logger=self.logger)
        self.memory.initialize()
        self.tool = RemindersTool(memory=self.memory, logger=self.logger)
        self.zones = SyntheticZones(self, {SYNTHETIC_KEY: CENTRAL_RULES})
        stamps = iter(STAMPS)
        clock = patch("core.memory.task_timestamp", side_effect=lambda: next(stamps))
        clock.start()
        self.addCleanup(clock.stop)

    def add(self, when: str = EVENING, text: str = "call mom") -> int:
        result = self.tool.execute(
            "create",
            {"text": text, "when": when, "repeat": "never"},
        )
        self.assertTrue(result.success, result.message)
        return result.data["id"]

    def raw_rows(self) -> list[tuple[object, ...]]:
        connection = sqlite3.connect(self.database_path)
        try:
            return connection.execute(
                "SELECT *, CAST(remind_time AS BLOB), CAST(remind_utc AS BLOB) "
                "FROM reminders ORDER BY id"
            ).fetchall()
        finally:
            connection.close()

    def logged(self) -> str:
        return "\n".join(str(call) for call in self.logger.mock_calls)


class ReminderCreateTests(_ReminderToolTestCase):
    """Prove a reminder stores exactly the approved text and time."""

    def test_identity_actions_and_factory(self) -> None:
        self.assertEqual(self.tool.name, "reminders")
        self.assertEqual(
            self.tool.actions,
            frozenset(
                {
                    "create",
                    "list",
                    "due",
                    "read",
                    "complete",
                    "reopen",
                    "edit",
                    "reschedule",
                    "delete",
                    "enable_context",
                    "disable_context",
                    "delivery_status",
                    "enable_delivery",
                    "disable_delivery",
                }
            ),
        )
        self.assertIn("never delivers or notifies", self.tool.description)
        self.assertIsInstance(build_tool(memory=self.memory), RemindersTool)

    def test_a_reminder_is_stored_with_the_instant_its_time_encodes(self) -> None:
        result = self.tool.execute(
            "create",
            {"text": "call mom", "when": EVENING, "repeat": "never"},
        )

        self.assertEqual(
            result,
            ToolExecutionResult(
                True,
                f'Added reminder 1: "call mom", {EVENING_SHOWN}. It does not '
                "repeat, Maria shows it at most once and only while reminder "
                "delivery is on, and its context consent is off.",
                {"id": 1},
                recorded_message="Added reminder 1.",
            ),
        )
        self.assertEqual(
            self.raw_rows()[0][:9],
            (
                1,
                "call mom",
                EVENING,
                EVENING_UTC,
                "open",
                STAMPS[0],
                STAMPS[0],
                None,
                None,
            ),
        )

    def test_an_approved_time_that_has_passed_is_stored_unchanged(self) -> None:
        past = f"2001-01-01T09:00:00+01:00[{SYNTHETIC_KEY}]"
        reminder_id = self.add(past)
        self.assertEqual(self.raw_rows()[0][2:4], (past, "2001-01-01T08:00:00Z"))
        due = self.tool.execute("due", {"as_of": "2026-10-04T00:00:00Z"})
        self.assertEqual(due.data, [reminder_id])

    def test_invalid_values_are_refused_before_storage(self) -> None:
        for arguments, message in (
            ({"text": " x", "when": EVENING, "repeat": "never"}, TASK_TEXT_MESSAGE),
            ({"text": "x", "when": EVENING, "repeat": "daily"}, REPEAT_MESSAGE),
            ({"text": "x", "when": EVENING}, REPEAT_MESSAGE),
            (
                {"text": "x", "when": "tomorrow at 9:00", "repeat": "never"},
                REMIND_TIME_MESSAGE,
            ),
            (
                {"text": "x", "when": "2027-11-02T18:30:00+01:00", "repeat": "never"},
                REMIND_TIME_MESSAGE,
            ),
            (
                {
                    "text": "x",
                    "when": EVENING.replace(":00+", ":30+"),
                    "repeat": "never",
                },
                REMIND_TIME_MESSAGE,
            ),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    self.tool.execute("create", arguments),
                    ToolExecutionResult(False, message),
                )
        self.assertEqual(self.raw_rows(), [])
        self.assertEqual(
            REPEAT_MESSAGE,
            "A reminder does not repeat; its repeat must be never.",
        )

    def test_a_storage_failure_is_a_fixed_message(self) -> None:
        with patch.object(
            self.memory,
            "create_reminder",
            side_effect=RuntimeError(TEXT_SENTINEL),
        ):
            result = self.tool.execute(
                "create",
                {"text": TEXT_SENTINEL, "when": EVENING, "repeat": "never"},
            )
        self.assertEqual(result, ToolExecutionResult(False, ADD_FAILED_MESSAGE))
        self.assertNotIn(TEXT_SENTINEL, self.logged())

    def test_storing_needs_no_time_zone_data(self) -> None:
        reminder_id = self.add(UNCHECKED)
        self.assertEqual(
            self.raw_rows()[0][2:4],
            (UNCHECKED, "2027-11-03T03:00:00Z"),
        )
        self.assertTrue(self.tool.execute("read", {"id": reminder_id}).success)


class ReminderPreviewTests(_ReminderToolTestCase):
    """Prove each approval shows exactly what it covers, before storage."""

    def test_an_add_shows_the_exact_text_resolved_time_and_no_repeat(self) -> None:
        self.assertEqual(
            self.tool.confirmation_binding(
                "create",
                {"text": "call mom", "when": EVENING, "repeat": "never"},
            ),
            ConfirmationBinding(
                {},
                "This approval adds a reminder, shown quoted:\n"
                'Text: "call mom"\n'
                f"Time: {EVENING_SHOWN}\n"
                "Repeats: never",
            ),
        )
        self.assertEqual(self.raw_rows(), [])

    def test_changes_show_the_saved_values_beside_the_new_ones(self) -> None:
        reminder_id = self.add()
        later = f"2027-11-03T07:15:00+01:00[{SYNTHETIC_KEY}]"

        self.assertEqual(
            self.tool.confirmation_binding(
                "reschedule",
                {"id": reminder_id, "when": later, "repeat": "never"},
            ).preview,
            "This approval reschedules reminder 1, shown quoted:\n"
            'Text: "call mom"\n'
            f"Now: {EVENING_SHOWN}\n"
            f"New: Wednesday 2027-11-03 at 07:15, {SYNTHETIC_KEY} (UTC+01:00), "
            "which is 2027-11-03 06:15 UTC\n"
            "Repeats: never",
        )
        self.assertEqual(
            self.tool.confirmation_binding(
                "edit",
                {"id": reminder_id, "text": "call dad"},
            ).preview,
            "This approval replaces the text of reminder 1, shown quoted:\n"
            f"Time: {EVENING_SHOWN}\n"
            'Now: "call mom"\n'
            'New: "call dad"',
        )
        self.assertEqual(
            self.tool.confirmation_binding("delete", {"id": reminder_id}).preview,
            "This approval deletes reminder 1, shown quoted:\n"
            'Text: "call mom"\n'
            f"Time: {EVENING_SHOWN}",
        )
        self.assertEqual(
            self.tool.confirmation_binding("enable_context", {"id": reminder_id}),
            ConfirmationBinding(
                {"text_digest": reminder_digest("call mom", EVENING)},
                "This approval covers reminder 1 exactly as it is stored now, "
                "shown quoted:\n"
                'Text: "call mom"\n'
                f"Time: {EVENING_SHOWN}",
            ),
        )

    def test_the_shown_text_and_the_identity_come_from_one_read(self) -> None:
        reminder_id = self.add(EVENING, TEXT_SENTINEL)
        statements: list[str] = []
        original_connect = self.memory._connect

        def tracing_connect() -> sqlite3.Connection:
            connection = original_connect()
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(self.memory, "_connect", tracing_connect):
            binding = self.tool.confirmation_binding(
                "enable_context",
                {"id": reminder_id},
            )

        reads = [
            statement for statement in statements if "FROM reminders" in statement
        ]
        self.assertEqual(len(reads), 1)
        self.assertTrue(reads[0].lstrip().startswith("SELECT"))
        self.assertEqual(
            binding.arguments,
            {"text_digest": reminder_digest(TEXT_SENTINEL, EVENING)},
        )
        self.assertIn(f'Text: "{TEXT_SENTINEL}"', binding.preview)
        self.assertIn(f"Time: {EVENING_SHOWN}", binding.preview)

    def test_nothing_is_shown_or_pinned_when_nothing_can_be_shown(self) -> None:
        self.add()
        for action, arguments in (
            ("create", {"text": "x", "when": "tomorrow", "repeat": "never"}),
            ("create", {"text": " x", "when": EVENING, "repeat": "never"}),
            ("reschedule", {"id": 1, "when": "in 2 hours", "repeat": "never"}),
            ("edit", {"id": 1, "text": ""}),
            ("delete", {"id": 7}),
            ("enable_context", {"id": 0}),
            ("read", {"id": 1}),
            ("complete", {"id": 1}),
            ("due", {"as_of": EVENING_UTC}),
        ):
            with self.subTest(action=action):
                self.assertEqual(self.tool.confirmation_binding(action, arguments), {})


class ReminderListingTests(_ReminderToolTestCase):
    """Prove listings and the due query use only the saved instant."""

    def test_empty_listings_and_invalid_queries(self) -> None:
        self.assertEqual(
            self.tool.execute("list", {"status": "open"}),
            ToolExecutionResult(
                True,
                "No open reminders.",
                [],
                recorded_message="Listed open reminders.",
            ),
        )
        self.assertEqual(
            self.tool.execute("list", {"status": "completed"}).message,
            "No completed reminders.",
        )
        self.assertEqual(
            self.tool.execute("due", {"as_of": EVENING_UTC}),
            ToolExecutionResult(
                True,
                "No reminders are due.",
                [],
                recorded_message="Listed due reminders.",
            ),
        )
        self.assertEqual(
            self.tool.execute("list", {"status": "all"}),
            ToolExecutionResult(False, TASK_STATUS_MESSAGE),
        )
        for as_of in ("2027-11-02", "2027-11-02T17:30:00", EVENING, None):
            with self.subTest(as_of=as_of):
                self.assertEqual(
                    self.tool.execute("due", {"as_of": as_of}),
                    ToolExecutionResult(False, AS_OF_INSTANT_MESSAGE),
                )

    def test_open_reminders_come_by_saved_instant_whatever_their_zone(self) -> None:
        self.add(EVENING, "evening")
        self.add(f"2027-11-02T19:00:00+03:00[{MISSING_KEY}]", "earlier elsewhere")
        self.add(FIRST_FOLD, "first fold")
        self.add(SECOND_FOLD, "second fold")

        result = self.tool.execute("list", {"status": "open"})

        self.assertEqual(result.data, [3, 4, 2, 1])
        self.assertEqual(result.recorded_message, "Listed open reminders.")

    def test_the_due_query_includes_exactly_the_reached_instants(self) -> None:
        self.add(EVENING, "evening")
        self.add(f"2027-11-02T18:31:00+01:00[{SYNTHETIC_KEY}]", "a minute later")
        done = self.add(FIRST_FOLD, "done")
        self.tool.execute("complete", {"id": done})

        self.assertEqual(
            self.tool.execute("due", {"as_of": "2027-11-02T17:29:59Z"}).data,
            [],
        )
        result = self.tool.execute("due", {"as_of": EVENING_UTC})
        self.assertEqual(
            result,
            ToolExecutionResult(
                True,
                f'1: "evening", {EVENING_SHOWN} (time reached)',
                [1],
                recorded_message="Listed due reminders.",
            ),
        )

    def test_twenty_are_shown_and_more_is_said_when_more_exist(self) -> None:
        for minute in range(21):
            self.add(f"2027-11-02T18:{minute:02d}:00+01:00[{SYNTHETIC_KEY}]", "x")

        result = self.tool.execute("list", {"status": "open"})

        lines = result.message.split("\n")
        self.assertEqual(len(lines), 21)
        self.assertEqual(
            lines[-1],
            "Showing the first 20 reminders. More reminders exist.",
        )
        self.assertEqual(lines[-1], MORE_REMINDERS_MESSAGE)
        self.assertEqual(result.data, list(range(1, 21)))

    def test_due_reminders_come_by_saved_instant_whatever_their_zone(self) -> None:
        self.add(EVENING, "evening")
        self.add(f"2027-11-02T19:00:00+03:00[{MISSING_KEY}]", "earlier elsewhere")
        self.add(FIRST_FOLD, "first fold")
        self.add(SECOND_FOLD, "second fold")
        self.add(f"2027-11-02T18:31:00+01:00[{SYNTHETIC_KEY}]", "not yet")

        result = self.tool.execute("due", {"as_of": EVENING_UTC})

        self.assertEqual(result.data, [3, 4, 2, 1])
        self.assertEqual(result.recorded_message, "Listed due reminders.")

    def test_twenty_due_are_shown_and_more_is_said_when_more_exist(self) -> None:
        # Added latest first, so the earliest twenty instants are the newest.
        for minute in range(21):
            self.add(f"2027-11-02T18:{20 - minute:02d}:00+01:00[{SYNTHETIC_KEY}]", "x")

        result = self.tool.execute("due", {"as_of": EVENING_UTC})

        lines = result.message.split("\n")
        self.assertEqual(len(lines), 21)
        self.assertEqual(lines[-1], MORE_REMINDERS_MESSAGE)
        self.assertEqual(result.data, list(range(21, 1, -1)))

    def test_completed_reminders_show_their_completion(self) -> None:
        reminder_id = self.add()
        self.tool.execute("complete", {"id": reminder_id})

        self.assertEqual(
            self.tool.execute("list", {"status": "completed"}).message,
            f'1: "call mom", {EVENING_SHOWN}, completed at {STAMPS[1]}',
        )

    def test_a_listing_failure_is_a_fixed_message(self) -> None:
        with patch.object(self.memory, "list_reminders", side_effect=RuntimeError("x")):
            self.assertEqual(
                self.tool.execute("list", {"status": "open"}),
                ToolExecutionResult(False, LIST_FAILED_MESSAGE),
            )


class ReminderReadAndLifecycleTests(_ReminderToolTestCase):
    """Prove reading, completing, reopening, editing, and rescheduling."""

    def test_a_reminder_is_shown_in_full(self) -> None:
        reminder_id = self.add()

        self.assertEqual(
            self.tool.execute("read", {"id": reminder_id}),
            ToolExecutionResult(
                True,
                "Reminder 1\n"
                'Text: "call mom"\n'
                f"Time: {EVENING_SHOWN}\n"
                "Repeats: never\n"
                "Status: open\n"
                f"Created at: {STAMPS[0]}\n"
                f"Updated at: {STAMPS[0]}\n"
                "Context: off",
                {"id": 1},
                recorded_message="Displayed reminder 1.",
            ),
        )
        self.assertEqual(
            self.tool.execute("read", {"id": 2}),
            ToolExecutionResult(False, "Reminder not found: 2."),
        )
        self.assertEqual(
            self.tool.execute("read", {"id": "1"}),
            ToolExecutionResult(False, REMINDER_ID_MESSAGE),
        )

    def test_complete_and_reopen_change_the_status_only(self) -> None:
        reminder_id = self.add()
        before = self.raw_rows()[0]

        self.assertEqual(
            self.tool.execute("complete", {"id": reminder_id}),
            ToolExecutionResult(
                True,
                "Completed reminder 1.",
                {"id": 1, "status": "completed"},
            ),
        )
        self.assertEqual(self.raw_rows()[0][7], STAMPS[1])
        self.assertEqual(
            self.tool.execute("reopen", {"id": reminder_id}).message,
            "Reopened reminder 1.",
        )
        self.assertEqual(self.raw_rows()[0], before)
        # Completing again stamps a new time; the first is never restored.
        self.tool.execute("complete", {"id": reminder_id})
        self.assertEqual(self.raw_rows()[0][7], STAMPS[2])
        completed = self.raw_rows()
        self.assertEqual(
            self.tool.execute("complete", {"id": reminder_id}).message,
            "Reminder 1 is already completed, so nothing changed.",
        )
        self.assertEqual(self.raw_rows(), completed)

    def test_reopening_an_open_or_a_missing_reminder_changes_nothing(self) -> None:
        reminder_id = self.add()
        before = self.raw_rows()

        self.assertEqual(
            self.tool.execute("reopen", {"id": reminder_id}),
            ToolExecutionResult(
                False,
                "Reminder 1 is already open, so nothing changed.",
            ),
        )
        for action in ("complete", "reopen"):
            with self.subTest(action=action):
                self.assertEqual(
                    self.tool.execute(action, {"id": 9}),
                    ToolExecutionResult(False, "Reminder not found: 9."),
                )
                self.assertEqual(
                    self.tool.execute(action, {"id": 0}),
                    ToolExecutionResult(False, REMINDER_ID_MESSAGE),
                )
        self.assertEqual(self.raw_rows(), before)

    def test_edit_and_reschedule_replace_one_value_and_stamp_the_update(self) -> None:
        reminder_id = self.add()
        later = f"2027-11-03T07:15:00+01:00[{SYNTHETIC_KEY}]"

        self.assertEqual(
            self.tool.execute("edit", {"id": reminder_id, "text": "call dad"}),
            ToolExecutionResult(
                True,
                'Edited reminder 1: "call dad".',
                {"id": 1},
                recorded_message="Edited reminder 1.",
            ),
        )
        self.assertEqual(
            self.tool.execute(
                "reschedule",
                {"id": reminder_id, "when": later, "repeat": "never"},
            ),
            ToolExecutionResult(
                True,
                f"Rescheduled reminder 1: Wednesday 2027-11-03 at 07:15, "
                f"{SYNTHETIC_KEY} (UTC+01:00), which is 2027-11-03 06:15 UTC.",
                {"id": 1},
                recorded_message="Rescheduled reminder 1.",
            ),
        )
        self.assertEqual(
            self.raw_rows()[0][1:7],
            ("call dad", later, "2027-11-03T06:15:00Z", "open", STAMPS[0], STAMPS[2]),
        )
        self.assertEqual(
            self.tool.execute(
                "reschedule",
                {"id": reminder_id, "when": later, "repeat": "weekly"},
            ),
            ToolExecutionResult(False, REPEAT_MESSAGE),
        )
        self.assertEqual(
            self.tool.execute(
                "reschedule",
                {"id": 9, "when": later, "repeat": "never"},
            ),
            ToolExecutionResult(False, "Reminder not found: 9."),
        )

    def test_consent_follows_the_exact_text_and_saved_time(self) -> None:
        reminder_id = self.add()
        binding = self.tool.confirmation_binding("enable_context", {"id": reminder_id})

        self.assertEqual(
            self.tool.execute(
                "enable_context",
                {"id": reminder_id, **binding.arguments},
            ),
            ToolExecutionResult(
                True,
                "Enabled context for reminder 1.",
                {"id": 1, "context_enabled": True},
            ),
        )
        result = self.tool.execute(
            "reschedule",
            {
                "id": reminder_id,
                "when": f"2027-11-03T07:15:00+01:00[{SYNTHETIC_KEY}]",
                "repeat": "never",
            },
        )
        self.assertTrue(
            result.message.endswith(
                "Context consent for reminder 1 was turned off because its time "
                "changed."
            )
        )
        self.assertEqual(
            self.tool.execute(
                "enable_context",
                {"id": reminder_id, **binding.arguments},
            ),
            ToolExecutionResult(
                False,
                "Context was not enabled for reminder 1 because its text and time "
                "could not be confirmed unchanged since confirmation was "
                "requested. Request confirmation again with: enable reminder "
                "context 1",
            ),
        )
        self.assertEqual(
            self.tool.execute("disable_context", {"id": reminder_id}),
            ToolExecutionResult(
                True,
                "Disabled context for reminder 1.",
                {"id": 1, "context_enabled": False},
            ),
        )

    def test_delete_removes_one_reminder_and_ids_are_never_reused(self) -> None:
        self.add()
        self.add()
        self.assertEqual(
            self.tool.execute("delete", {"id": 2}),
            ToolExecutionResult(True, "Deleted reminder 2.", {"id": 2}),
        )
        self.assertEqual(
            self.tool.execute("delete", {"id": 2}),
            ToolExecutionResult(False, "Reminder not found: 2."),
        )
        self.assertEqual(self.add(), 3)


class RuleChangeTests(_ReminderToolTestCase):
    """Prove a later rule change is shown, never applied, refused, or saved."""

    def test_a_correctly_chosen_repeated_time_shows_no_warning(self) -> None:
        # The owner-requested regression: an unchanged, correctly selected
        # daylight-saving fold agrees with the rules it was resolved under.
        first = self.add(FIRST_FOLD, "first")
        second = self.add(SECOND_FOLD, "second")

        listing = self.tool.execute("list", {"status": "open"}).message
        self.assertNotIn("[rules", listing)
        self.assertEqual(listing.count("\n"), 1)
        for reminder_id in (first, second):
            with self.subTest(reminder_id=reminder_id):
                shown = self.tool.execute("read", {"id": reminder_id}).message
                self.assertNotIn(RULES_CHANGED_WARNING, shown)
                self.assertNotIn(RULES_UNCHECKED_NOTE, shown)
                preview = self.tool.confirmation_binding(
                    "delete",
                    {"id": reminder_id},
                ).preview
                self.assertNotIn(RULES_CHANGED_WARNING, preview)

    def test_changed_rules_add_the_warning_and_change_nothing(self) -> None:
        reminder_id = self.add(EVENING, "evening")
        before = self.raw_rows()

        # The zone now has a fixed +03:00, so the saved local time no longer
        # exists at its saved offset of +01:00.
        self.zones.write(SYNTHETIC_KEY, FIXED_PLUS_THREE_RULES)

        listing = self.tool.execute("list", {"status": "open"})
        self.assertEqual(
            listing.message,
            f'1: "evening", {EVENING_SHOWN} [rules changed]\n'
            f"[rules changed]: {RULES_CHANGED_WARNING}",
        )
        shown = self.tool.execute("read", {"id": reminder_id}).message
        self.assertIn(f"Time: {EVENING_SHOWN}", shown)
        self.assertTrue(shown.endswith(RULES_CHANGED_WARNING))
        for action, arguments in (
            ("edit", {"id": reminder_id, "text": "evening call"}),
            ("delete", {"id": reminder_id}),
            ("enable_context", {"id": reminder_id}),
            (
                "reschedule",
                {
                    "id": reminder_id,
                    "when": f"2027-11-04T18:30:00+03:00[{SYNTHETIC_KEY}]",
                    "repeat": "never",
                },
            ),
        ):
            with self.subTest(action=action):
                preview = self.tool.confirmation_binding(action, arguments).preview
                self.assertIn(RULES_CHANGED_WARNING, preview)
        due = self.tool.execute("due", {"as_of": EVENING_UTC})
        self.assertEqual(due.data, [reminder_id])
        self.assertIn("[rules changed]", due.message)
        self.assertEqual(
            self.tool.execute("due", {"as_of": "2027-11-02T17:29:59Z"}).data,
            [],
        )
        self.assertEqual(self.raw_rows(), before)

    def test_after_one_zone_changes_the_saved_instants_still_decide(self) -> None:
        # The steady reminder's saved instant comes first. Under the changed
        # rules the evening reminder's local time would be 15:30 UTC, before
        # it, so an order or a due selection recomputed from the current
        # rules would differ from the saved one, and so would ordering by id.
        self.zones.write(STEADY_KEY, FIXED_PLUS_THREE_RULES)
        changing = self.add(EVENING, "changing")
        steady = self.add(STEADY, "steady")
        self.zones.write(SYNTHETIC_KEY, FIXED_PLUS_THREE_RULES)
        before = self.raw_rows()

        listing = self.tool.execute("list", {"status": "open"})
        self.assertEqual(listing.data, [steady, changing])
        self.assertEqual(listing.message.count("[rules changed]"), 2)
        self.assertEqual(
            self.tool.execute("due", {"as_of": STEADY_UTC}).data,
            [steady],
        )
        self.assertEqual(
            self.tool.execute("due", {"as_of": EVENING_UTC}).data,
            [steady, changing],
        )
        self.assertEqual(self.raw_rows(), before)

        # Every approval shows the saved time beside the warning, and every
        # operation still works on the changed reminder without moving it.
        binding = self.tool.confirmation_binding("enable_context", {"id": changing})
        for action, arguments in (
            ("edit", {"id": changing, "text": "changing, later"}),
            ("delete", {"id": changing}),
            ("enable_context", {"id": changing}),
        ):
            with self.subTest(preview=action):
                preview = self.tool.confirmation_binding(action, arguments).preview
                self.assertIn(EVENING_SHOWN, preview)
                self.assertIn(RULES_CHANGED_WARNING, preview)
        for action, arguments in (
            ("complete", {"id": changing}),
            ("reopen", {"id": changing}),
            ("enable_context", {"id": changing, **binding.arguments}),
            ("edit", {"id": changing, "text": "changing, later"}),
        ):
            with self.subTest(action=action):
                self.assertTrue(self.tool.execute(action, arguments).success)
        saved = {row[0]: row[2:4] for row in self.raw_rows()}
        self.assertEqual(saved[changing], (EVENING, EVENING_UTC))
        self.assertTrue(self.tool.execute("delete", {"id": changing}).success)
        self.assertEqual([row[0] for row in self.raw_rows()], [steady])

    def test_an_unloadable_zone_adds_the_note_and_every_operation_works(self) -> None:
        reminder_id = self.add(UNCHECKED, "elsewhere")

        listing = self.tool.execute("list", {"status": "open"}).message
        self.assertEqual(
            listing,
            '1: "elsewhere", Wednesday 2027-11-03 at 08:00, Maria/Missing '
            "(UTC+05:00), which is 2027-11-03 03:00 UTC [rules unchecked]\n"
            f"[rules unchecked]: {RULES_UNCHECKED_NOTE}",
        )
        self.assertTrue(
            self.tool.execute("read", {"id": reminder_id}).message.endswith(
                RULES_UNCHECKED_NOTE
            )
        )
        binding = self.tool.confirmation_binding("enable_context", {"id": reminder_id})
        for action, arguments in (
            ("enable_context", {"id": reminder_id, **binding.arguments}),
            ("disable_context", {"id": reminder_id}),
            ("complete", {"id": reminder_id}),
            ("reopen", {"id": reminder_id}),
            ("edit", {"id": reminder_id, "text": "elsewhere, later"}),
            ("due", {"as_of": "2027-11-03T03:00:00Z"}),
            ("delete", {"id": reminder_id}),
        ):
            with self.subTest(action=action):
                self.assertTrue(self.tool.execute(action, arguments).success)

    def test_an_unreadable_zone_file_is_unchecked_never_an_error(self) -> None:
        reminder_id = self.add()
        self.zones.corrupt(SYNTHETIC_KEY)

        self.assertIn(
            "[rules unchecked]",
            self.tool.execute("list", {"status": "open"}).message,
        )
        self.assertTrue(self.tool.execute("read", {"id": reminder_id}).success)


class ReminderPrivacyTests(_ReminderToolTestCase):
    """Prove no text, time, zone, or digest reaches a recorded line or the log."""

    def test_recorded_lines_and_logs_carry_ids_and_fixed_phrases_only(self) -> None:
        reminder_id = self.add(EVENING, TEXT_SENTINEL)
        binding = self.tool.confirmation_binding("enable_context", {"id": reminder_id})
        results = [
            self.tool.execute("list", {"status": "open"}),
            self.tool.execute("due", {"as_of": EVENING_UTC}),
            self.tool.execute("read", {"id": reminder_id}),
            self.tool.execute(
                "enable_context",
                {"id": reminder_id, **binding.arguments},
            ),
            self.tool.execute("edit", {"id": reminder_id, "text": f"{TEXT_SENTINEL}!"}),
            self.tool.execute(
                "reschedule",
                {"id": reminder_id, "when": FIRST_FOLD, "repeat": "never"},
            ),
            self.tool.execute("complete", {"id": reminder_id}),
            self.tool.execute("delete", {"id": reminder_id}),
        ]
        for result in results:
            with self.subTest(recorded=result.recorded_message):
                self.assertTrue(result.success)
                recorded = result.recorded_message or ""
                for secret in (TEXT_SENTINEL, "2027", SYNTHETIC_KEY, "+01:00"):
                    self.assertNotIn(secret, recorded)
        logged = self.logged()
        for secret in (
            TEXT_SENTINEL,
            "2027-11-02",
            "18:30",
            SYNTHETIC_KEY,
            "+01:00",
            EVENING_UTC,
            binding.arguments["text_digest"],
        ):
            self.assertNotIn(secret, logged)

    def test_the_tool_reads_no_clock_and_starts_nothing(self) -> None:
        tree = ast.parse(inspect.getsource(reminders_module))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {"now", "utcnow", "today", "monotonic"})
        self.assertFalse(
            imported
            & {
                "datetime",
                "time",
                "zoneinfo",
                "threading",
                "sched",
                "asyncio",
                "signal",
                "sqlite3",
            }
        )


if __name__ == "__main__":
    unittest.main()
