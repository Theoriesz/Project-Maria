"""Focused tests for the memory layer's reminder-delivery API (V1-23-R1).

They drive the real memory layer over temporary databases, new and migrated,
in UTF-8 and UTF-16: the one-row delivery setting, schema 8's scheduling
revision and claim columns, the candidate read, each claim's own write
transaction, and each outcome's write. A test reaches between a candidate
read and its claim through a store that runs a hook before or after a chosen
claim, so every race here is deterministic. Every check's instant is passed
in, the setting's stamps come from a replaced record clock, and nothing
sleeps. Rows Maria could not have written are stored from outside, through a
second connection, and must be refused privately, never claimed, and never
counted. The closed-app pass runs here over a real temporary database with
fake Windows functions; no real box is shown and no task is registered.
"""

from __future__ import annotations

import logging
import sqlite3
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import deliver_reminders
from core import memory as memory_module
from core import reminder_delivery as reminder_delivery_module
from core.brain import MariaBrain
from core.context import reminder_digest
from core.desktop_notice import NotificationState
from core.memory import Memory
from core.reminder_delivery import (
    MORE_DUE_LINE,
    ClaimBatch,
    ClaimStop,
    RecordFailures,
    ReminderDeliverySeam,
    claim_notice,
    notice_entry,
    record_notice,
)
from core.session import MariaSession
from core.task_records import (
    DELIVERY_CHANNEL_CLI,
    DELIVERY_CHANNEL_DESKTOP,
    DELIVERY_CHANNEL_GUI,
    DELIVERY_SETTING_MESSAGE,
    DELIVERY_STORAGE_MESSAGE,
    SCHEDULE_REVISION_MAX,
    TASK_TEXT_MAX_BYTES,
    ClaimOutcome,
    ClaimResult,
    DeliveryCandidate,
    DeliveryClaim,
    DeliveryCounts,
    DeliverySetting,
    DeliverySettingState,
    DeliveryStorageError,
    ReminderDeliveryState,
)
from deliver_reminders import (
    EXIT_DISPLAY_FAILED,
    EXIT_DONE,
    EXIT_STORAGE,
    EXIT_UNEXPECTED,
    run_pass,
)
from tests.schema_fixtures import read_schema_version
from tests.test_audit_migration import (
    DATABASE_REFUSED_PATTERN,
    FROZEN_REMINDER_DELIVERY_SETTINGS_TABLE,
    FROZEN_REMINDERS_TABLE_WITH_DELIVERY_STATE,
    NEVER_COMMITTED,
    V7_SCHEMA,
    V8_SCHEMA,
    _applied,
    _build_schema,
    _describe,
    _execute,
    _hold_write_lock,
    _messages,
    _query,
    _TemporaryDatabaseTestCase,
)
from tests.test_task_reminder_storage_integrity import (
    INSTANT,
    LARGE,
    OTHER_INSTANT,
    OTHER_TIME,
    ROW_SENTINEL,
    STAMP,
    TIME,
    CastToBlob,
    MigratedStorageTests,
    Utf16Mixin,
    _StorageCase,
    placeholders,
    suffixed,
)
from tests.zone_fixtures import SYNTHETIC_KEY
from tools.reminders import DELIVERY_UNREADABLE_SENTENCE, RemindersTool


# A check's instant: fifteen minutes after INSTANT, so a reminder saved at
# INSTANT is due at it, and not yet overdue.
NOW = datetime(2027, 11, 2, 17, 45, tzinfo=timezone.utc)
AS_OF = "2027-11-02T17:45:00Z"

# The setting's stamps, each written by a replaced record clock.
ON_STAMP = "2027-11-01T08:00:00Z"
OFF_STAMP = "2027-11-01T09:00:00Z"
LATER_STAMP = "2027-11-01T10:00:00Z"

# INSTANT written with another offset, and an earlier instant also due at NOW.
SAME_INSTANT_TIME = f"2027-11-02T19:30:00+02:00[{SYNTHETIC_KEY}]"
EARLIER_TIME = f"2027-11-02T18:00:00+01:00[{SYNTHETIC_KEY}]"
EARLIER_INSTANT = "2027-11-02T17:00:00Z"

# A stamp whose shape every CHECK admits and no calendar has.
IMPOSSIBLE_STAMP = "2027-02-30T25:61:00Z"

# What a trigger stored from outside raises, which must never be repeated.
TRIGGER_SENTINEL = "private-trigger-sentinel-4b81e0"

# The memory layer's fixed log line for a database that is not current.
NOT_CURRENT_LINE = (
    "Failed to %s: %s.",
    "check reminder delivery",
    "the database is not at the current schema",
)
DELIVERY_STEP_NAME = "add_reminder_delivery_state"
DRIFTED_VERSION_EIGHT_LINE = (
    "Refused a database stamped at schema version 8 whose structure is not "
    "the frozen schema for that version."
)


def due_times(count: int) -> list[tuple[str, str]]:
    """Return saved times and instants a minute apart, oldest first, all due."""
    times: list[tuple[str, str]] = []
    for index in range(count):
        instant = NOW - timedelta(minutes=30 - index)
        local = instant + timedelta(hours=1)
        times.append(
            (
                f"{local:%Y-%m-%dT%H:%M}:00+01:00[{SYNTHETIC_KEY}]",
                f"{instant:%Y-%m-%dT%H:%M}:00Z",
            )
        )
    return times


class _Abandoned(BaseException):
    """Stands in for the process dying: nothing in Maria catches it."""


class HookedStore:
    """The real memory layer, with hooks around chosen claims.

    ``before`` maps a claim's position in its batch, from 1, to a hook run
    just before that claim reads anything, so a test can change the database
    between the candidate read and the claim. ``after`` maps a position to a
    hook run once that claim has returned. Each hook runs once.
    """

    def __init__(
        self,
        memory: Memory,
        *,
        before: dict[int, Callable[[], object]] | None = None,
        after: dict[int, Callable[[], object]] | None = None,
    ) -> None:
        self._memory = memory
        self._before = dict(before or {})
        self._after = dict(after or {})
        self._position = 0

    def reminder_delivery_setting(self) -> DeliverySetting:
        return self._memory.reminder_delivery_setting()

    def reminder_delivery_candidates(
        self,
        as_of: str,
        limit: int,
    ) -> list[DeliveryCandidate]:
        return self._memory.reminder_delivery_candidates(as_of, limit)

    def claim_reminder_delivery(
        self,
        candidate: DeliveryCandidate,
        channel: str,
        claimed_at: str,
        as_of: str,
    ) -> ClaimResult:
        self._position += 1
        hook = self._before.pop(self._position, None)
        if hook is not None:
            hook()
        result = self._memory.claim_reminder_delivery(
            candidate,
            channel,
            claimed_at,
            as_of,
        )
        hook = self._after.pop(self._position, None)
        if hook is not None:
            hook()
        return result

    def record_reminder_delivery(self, claim: DeliveryClaim, outcome: str) -> bool:
        return self._memory.record_reminder_delivery(claim, outcome)


def _abandon() -> None:
    """End a pass as a dying process would, after its last commit."""
    raise _Abandoned()


class _DeliveryCase(_StorageCase):
    """One temporary database, with Maria's delivery API and raw access."""

    def set_delivery(self, enabled: bool, stamp: str = ON_STAMP) -> DeliverySetting:
        """Change the setting through the API, stamped by a replaced clock."""
        with patch.object(memory_module, "task_timestamp", return_value=stamp):
            return self.memory.set_reminder_delivery(enabled)

    def add(
        self,
        text: str = "call mom",
        time: str = TIME,
        instant: str = INSTANT,
    ) -> int:
        """Store one reminder through the API and return its id."""
        return self.memory.create_reminder(text, time, instant)

    def add_due(self, count: int) -> list[int]:
        """Store ``count`` due reminders, oldest first, and return their ids."""
        return [
            self.add(f"reminder number {number}", time, instant)
            for number, (time, instant) in enumerate(due_times(count), 1)
        ]

    def candidates(self, as_of: str = AS_OF) -> list[DeliveryCandidate]:
        """Read the due candidates one check would read."""
        return self.memory.reminder_delivery_candidates(as_of, 6)

    def claim(
        self,
        item: DeliveryCandidate,
        *,
        channel: str = DELIVERY_CHANNEL_DESKTOP,
        as_of: str = AS_OF,
    ) -> ClaimResult:
        """Claim one candidate through the API, at ``as_of``."""
        return self.memory.claim_reminder_delivery(item, channel, as_of, as_of)

    def fresh_memory(self) -> Memory:
        """Return a second memory layer over the same file, as a restart has."""
        return Memory(self.database_path, logger=Mock(spec=logging.Logger))

    def query(
        self,
        statement: str,
        parameters: tuple[object, ...] = (),
    ) -> list[tuple[object, ...]]:
        """Run one read on a second, raw connection."""
        connection = sqlite3.connect(self.database_path)
        try:
            return connection.execute(statement, parameters).fetchall()
        finally:
            connection.close()

    def state(self, reminder_id: int) -> tuple[object, ...] | None:
        """Return one reminder's revision and claim columns, or None."""
        rows = self.query(
            """
            SELECT schedule_revision, delivery_claimed_at, delivery_channel,
                delivery_outcome
            FROM reminders WHERE id = ?
            """,
            (reminder_id,),
        )
        return rows[0] if rows else None

    def setting_rows(self) -> list[tuple[object, ...]]:
        """Return the setting table's rows as stored."""
        return self.query(
            "SELECT id, enabled, changed_at FROM reminder_delivery_settings"
        )

    def insert_setting(self, enabled: object, changed_at: object) -> None:
        """Store the one setting row from outside Maria."""
        values = {"id": 1, "enabled": enabled, "changed_at": changed_at}
        self.raw(
            "INSERT INTO reminder_delivery_settings (id, enabled, changed_at) "
            f"VALUES ({placeholders(values)})",
            tuple(values.values()),
        )

    def clear_claims(self) -> None:
        """Remove every claim from outside Maria, for the next case."""
        self.raw(
            "UPDATE reminders SET delivery_claimed_at = NULL, "
            "delivery_channel = NULL, delivery_outcome = NULL"
        )

    def assertLoggedNothingPrivate(self, *private: str) -> None:
        """Assert no memory log call holds any of ``private``."""
        logged = "\n".join(str(call) for call in self.logger.mock_calls)
        for value in private:
            self.assertNotIn(value, logged)


class DeliverySettingTests(_DeliveryCase):
    """Pin the one-row setting: never set, on, off, and only on a change."""

    def test_a_new_database_has_never_set_delivery_and_holds_no_row(self) -> None:
        never_set = DeliverySetting(DeliverySettingState.NEVER_SET)

        self.assertEqual(self.memory.reminder_delivery_setting(), never_set)
        self.assertIs(self.memory.reminder_delivery_setting().enabled, False)
        self.assertEqual(self.memory.delivery_state(), never_set)
        self.assertEqual(self.setting_rows(), [])

    def test_each_change_writes_the_one_row_only_when_it_differs(self) -> None:
        on = self.set_delivery(True, ON_STAMP)
        again = self.set_delivery(True, LATER_STAMP)

        self.assertEqual(on, DeliverySetting(DeliverySettingState.ENABLED, ON_STAMP))
        self.assertEqual(again, on)
        self.assertEqual(self.setting_rows(), [(1, 1, ON_STAMP)])

        off = self.set_delivery(False, OFF_STAMP)
        still_off = self.set_delivery(False, LATER_STAMP)

        self.assertEqual(
            off,
            DeliverySetting(DeliverySettingState.DISABLED, OFF_STAMP),
        )
        self.assertEqual(still_off, off)
        self.assertEqual(self.setting_rows(), [(1, 0, OFF_STAMP)])
        self.assertEqual(self.memory.reminder_delivery_setting(), off)
        self.assertEqual(self.memory.delivery_state(), off)

    def test_only_a_bool_is_accepted_and_a_refusal_writes_nothing(self) -> None:
        before = self.database_path.read_bytes()

        for value in (1, 0, "on", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    self.memory.set_reminder_delivery(value)  # type: ignore[arg-type]
                self.assertEqual(str(caught.exception), DELIVERY_SETTING_MESSAGE)

        self.assertEqual(self.database_path.read_bytes(), before)
        self.assertEqual(self.setting_rows(), [])

    def test_a_change_logs_only_whether_delivery_is_on(self) -> None:
        self.set_delivery(True)
        self.set_delivery(False, OFF_STAMP)

        self.assertEqual(
            [call.args for call in self.logger.info.call_args_list[-2:]],
            [
                ("Reminder delivery is %s.", "on"),
                ("Reminder delivery is %s.", "off"),
            ],
        )


class SettingIntegrityTests(_DeliveryCase):
    """A setting row Maria could not have written leaves delivery off (A17)."""

    INSERT = (
        "INSERT INTO reminder_delivery_settings (id, enabled, changed_at) "
        "VALUES (?, ?, ?)"
    )

    def test_the_checks_refuse_a_setting_row_maria_could_never_write(self) -> None:
        for parameters in (
            (1, 2, ON_STAMP),
            (1, -1, ON_STAMP),
            (1, None, ON_STAMP),
            (1, "yes", ON_STAMP),
            (1, b"\x01", ON_STAMP),
            (1, 1, None),
            (1, 1, "yesterday"),
            (1, 1, "2027-11-01 08:00:00Z"),
            (0, 1, ON_STAMP),
            (2, 1, ON_STAMP),
        ):
            with self.subTest(parameters=repr(parameters)):
                self.assertCheckRefuses(self.INSERT, parameters)
        self.assertEqual(self.setting_rows(), [])

    def test_a_second_setting_row_is_refused_beside_the_first(self) -> None:
        self.set_delivery(True)

        self.assertCheckRefuses(self.INSERT, (2, 0, OFF_STAMP))

        self.assertEqual(self.setting_rows(), [(1, 1, ON_STAMP)])

    def test_a_row_the_checks_admit_but_maria_refuses_leaves_delivery_off(
        self,
    ) -> None:
        unreadable = DeliverySetting(DeliverySettingState.UNREADABLE)
        tool = RemindersTool(memory=self.memory, logger=self.logger)
        for label, changed_at in (
            ("a BLOB stamp", CastToBlob(ON_STAMP)),
            ("a NUL-suffixed stamp", suffixed(ON_STAMP)),
            ("an impossible stamp", IMPOSSIBLE_STAMP),
        ):
            with self.subTest(case=label):
                self.raw("DELETE FROM reminder_delivery_settings")
                self.raw("DELETE FROM reminders")
                self.insert_setting(1, changed_at)
                reminder_id = self.add()

                self.assertEqual(self.memory.reminder_delivery_setting(), unreadable)
                self.assertEqual(self.memory.delivery_state(), unreadable)
                self.assertEqual(
                    claim_notice(self.memory, NOW, DELIVERY_CHANNEL_DESKTOP),
                    ClaimBatch(),
                )
                # A claim reads the setting again under its write lock, and
                # fails closed there too.
                (item,) = self.candidates()
                self.assertEqual(self.claim(item), ClaimResult(ClaimOutcome.DISABLED))
                self.assertEqual(self.state(reminder_id), (1, None, None, None))
                status = tool.execute("delivery_status", {})
                self.assertTrue(status.success)
                self.assertTrue(status.message.startswith(DELIVERY_UNREADABLE_SENTENCE))

                # Turning delivery on replaces the unreadable row.
                self.assertEqual(
                    self.set_delivery(True, LATER_STAMP),
                    DeliverySetting(DeliverySettingState.ENABLED, LATER_STAMP),
                )
                self.assertEqual(self.setting_rows(), [(1, 1, LATER_STAMP)])

    def test_turning_an_unreadable_setting_off_writes_a_readable_row(self) -> None:
        self.insert_setting(1, CastToBlob(ON_STAMP))

        self.assertEqual(
            self.set_delivery(False, OFF_STAMP),
            DeliverySetting(DeliverySettingState.DISABLED, OFF_STAMP),
        )
        self.assertEqual(self.setting_rows(), [(1, 0, OFF_STAMP)])

    def test_a_deleted_setting_row_reads_never_set_and_stops_claims(self) -> None:
        self.set_delivery(True)
        reminder_id = self.add()
        (item,) = self.candidates()

        self.raw("DELETE FROM reminder_delivery_settings")

        self.assertEqual(
            self.memory.reminder_delivery_setting(),
            DeliverySetting(DeliverySettingState.NEVER_SET),
        )
        self.assertEqual(self.claim(item), ClaimResult(ClaimOutcome.DISABLED))
        self.assertEqual(self.state(reminder_id), (1, None, None, None))


class DeliveryStateIntegrityTests(_DeliveryCase):
    """Delivery state Maria could not have written: refused, unclaimed, uncounted."""

    # Values each CHECK admits on its own, which no claim Maria writes holds:
    # (claim stamp, channel, outcome).
    MALFORMED = {
        "an outcome without a claim": (None, None, "displayed"),
        "a channel without a stamp": (None, DELIVERY_CHANNEL_CLI, None),
        "a stamp without a channel": (AS_OF, None, None),
        "a stamp and an outcome without a channel": (AS_OF, None, "failed"),
        "a BLOB claim stamp": (CastToBlob(AS_OF), DELIVERY_CHANNEL_CLI, None),
        "a NUL-suffixed claim stamp": (suffixed(AS_OF), DELIVERY_CHANNEL_GUI, None),
        "an impossible claim stamp": (
            IMPOSSIBLE_STAMP,
            DELIVERY_CHANNEL_DESKTOP,
            "displayed",
        ),
    }

    def test_the_checks_refuse_delivery_state_maria_could_never_write(self) -> None:
        reminder_id = self.add()
        for column, value, as_blob in (
            ("schedule_revision", 0, False),
            ("schedule_revision", -1, False),
            ("schedule_revision", 1.5, False),
            ("schedule_revision", "two", False),
            ("schedule_revision", None, False),
            ("schedule_revision", "1", True),
            ("delivery_claimed_at", "yesterday", False),
            ("delivery_claimed_at", "2027-11-02 17:45:00Z", False),
            ("delivery_channel", "email", False),
            ("delivery_channel", "email", True),
            ("delivery_channel", "CLI", False),
            ("delivery_outcome", "shown", False),
            ("delivery_outcome", "shown", True),
            ("delivery_outcome", "DISPLAYED", False),
        ):
            with self.subTest(column=column, value=value, blob=as_blob):
                mark = "CAST(? AS BLOB)" if as_blob else "?"
                self.assertCheckRefuses(
                    f"UPDATE reminders SET {column} = {mark} WHERE id = ?",
                    (value, reminder_id),
                )
        self.assertEqual(self.state(reminder_id), (1, None, None, None))

    def test_the_largest_revision_cannot_wrap(self) -> None:
        reminder_id = self.add()
        self.raw(
            "UPDATE reminders SET schedule_revision = ? WHERE id = ?",
            (SCHEDULE_REVISION_MAX, reminder_id),
        )

        self.assertCheckRefuses(
            "UPDATE reminders SET schedule_revision = schedule_revision + 1 "
            "WHERE id = ?",
            (reminder_id,),
        )
        self.assertEqual(
            self.memory.get_reminder(reminder_id).schedule_revision,
            SCHEDULE_REVISION_MAX,
        )

    def test_delivery_state_the_checks_admit_is_refused_privately(self) -> None:
        self.set_delivery(True)
        for label, (claimed_at, channel, outcome) in self.MALFORMED.items():
            with self.subTest(case=label):
                self.raw("DELETE FROM reminders")
                shown = self.add("shown", EARLIER_TIME, EARLIER_INSTANT)
                (item,) = self.candidates()
                claim = self.claim(item).claim
                self.memory.record_reminder_delivery(claim, "displayed")
                due = self.add("still due")
                bad = self.insert_reminder(
                    text=ROW_SENTINEL,
                    delivery_claimed_at=claimed_at,
                    delivery_channel=channel,
                    delivery_outcome=outcome,
                )
                stored = self.query("SELECT * FROM reminders WHERE id = ?", (bad,))
                self.logger.reset_mock()

                for operation, call in (
                    ("read reminder", lambda: self.memory.get_reminder(bad)),
                    ("list reminders", lambda: self.memory.list_reminders("open")),
                    ("list due reminders", lambda: self.memory.due_reminders(AS_OF)),
                ):
                    with self.assertRaises(RuntimeError) as caught:
                        call()
                    self.assertEqual(str(caught.exception), f"Unable to {operation}.")
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertIsNone(caught.exception.__context__)

                # Excluded from every delivery read; the valid rows stand.
                self.assertEqual([c.reminder_id for c in self.candidates()], [due])
                self.assertEqual(
                    self.memory.reminder_delivery_counts(),
                    DeliveryCounts(1, 0, 0),
                )
                batch = claim_notice(self.memory, NOW, DELIVERY_CHANNEL_CLI)
                self.assertEqual(batch.notice.reminder_ids, (due,))
                self.assertEqual(self.state(shown)[3], "displayed")
                self.assertEqual(
                    self.query("SELECT * FROM reminders WHERE id = ?", (bad,)),
                    stored,
                )
                self.assertLoggedNothingPrivate(ROW_SENTINEL)


class RevisionLifecycleTests(_DeliveryCase):
    """Only a reschedule to a different instant starts a revision (A18, A41)."""

    def delivered(self) -> int:
        """Store a due reminder, claim it, and record it displayed."""
        self.set_delivery(True)
        reminder_id = self.add()
        (item,) = self.candidates()
        self.assertTrue(
            self.memory.record_reminder_delivery(self.claim(item).claim, "displayed")
        )
        return reminder_id

    def test_a_new_reminder_has_revision_one_and_no_claim(self) -> None:
        reminder_id = self.add()

        reminder = self.memory.get_reminder(reminder_id)

        self.assertEqual(self.state(reminder_id), (1, None, None, None))
        self.assertEqual((reminder.schedule_revision, reminder.delivery), (1, None))

    def test_every_read_shows_the_claim_of_the_current_revision(self) -> None:
        reminder_id = self.delivered()
        expected = ReminderDeliveryState(AS_OF, DELIVERY_CHANNEL_DESKTOP, "displayed")

        self.assertEqual(self.memory.get_reminder(reminder_id).delivery, expected)
        self.assertEqual(self.memory.list_reminders("open")[0].delivery, expected)
        self.assertEqual(self.memory.due_reminders(AS_OF)[0].delivery, expected)

    def test_everything_but_a_new_instant_keeps_the_revision_and_claim(
        self,
    ) -> None:
        reminder_id = self.delivered()
        kept = (1, AS_OF, DELIVERY_CHANNEL_DESKTOP, "displayed")

        def consent_on() -> object:
            reminder = self.memory.get_reminder(reminder_id)
            return self.memory.enable_reminder_context(
                reminder_id,
                reminder_digest(reminder.text, reminder.remind_time),
            )

        for label, change in (
            (
                "a reschedule to the same instant",
                lambda: self.memory.reschedule_reminder(reminder_id, TIME, INSTANT),
            ),
            (
                "the same instant written with another offset",
                lambda: self.memory.reschedule_reminder(
                    reminder_id,
                    SAME_INSTANT_TIME,
                    INSTANT,
                ),
            ),
            ("an edit", lambda: self.memory.edit_reminder(reminder_id, "call dad")),
            ("completion", lambda: self.memory.complete_reminder(reminder_id)),
            ("reopening", lambda: self.memory.reopen_reminder(reminder_id)),
            ("consent on", consent_on),
            ("consent off", lambda: self.memory.disable_reminder_context(reminder_id)),
            ("delivery off", lambda: self.set_delivery(False, OFF_STAMP)),
            ("delivery on", lambda: self.set_delivery(True, LATER_STAMP)),
        ):
            with self.subTest(change=label):
                change()
                self.assertEqual(self.state(reminder_id), kept)

        reminder = self.memory.get_reminder(reminder_id)
        self.assertEqual(
            (reminder.text, reminder.remind_time, reminder.remind_utc),
            ("call dad", SAME_INSTANT_TIME, INSTANT),
        )

    def test_a_new_instant_starts_a_new_revision_and_clears_the_claim(self) -> None:
        reminder_id = self.delivered()

        self.memory.reschedule_reminder(reminder_id, OTHER_TIME, OTHER_INSTANT)
        self.assertEqual(self.state(reminder_id), (2, None, None, None))

        # Moved back to the time it was shown at, it is in a third revision.
        self.memory.reschedule_reminder(reminder_id, TIME, INSTANT)
        self.assertEqual(self.state(reminder_id), (3, None, None, None))
        self.assertIsNone(self.memory.get_reminder(reminder_id).delivery)

    def test_a_revision_at_its_largest_refuses_a_new_instant_whole(self) -> None:
        reminder_id = self.delivered()
        digest = reminder_digest("call mom", TIME)
        self.memory.enable_reminder_context(reminder_id, digest)
        self.raw(
            "UPDATE reminders SET schedule_revision = ? WHERE id = ?",
            (SCHEDULE_REVISION_MAX, reminder_id),
        )
        before = self.rows("reminders")

        with self.assertRaises(RuntimeError) as caught:
            self.memory.reschedule_reminder(reminder_id, OTHER_TIME, OTHER_INSTANT)

        self.assertEqual(str(caught.exception), "Unable to reschedule reminder.")
        self.assertIsNone(caught.exception.__cause__)
        # The consent the same transaction would have turned off stays on.
        self.assertEqual(self.rows("reminders"), before)

    def test_deleting_a_reminder_deletes_its_delivery_state(self) -> None:
        reminder_id = self.delivered()
        other = self.add("other", OTHER_TIME, OTHER_INSTANT)

        self.assertTrue(self.memory.delete_reminder(reminder_id))

        self.assertIsNone(self.state(reminder_id))
        self.assertFalse(
            self.memory.record_reminder_delivery(
                DeliveryClaim(reminder_id, 1),
                "failed",
            )
        )
        self.assertEqual(self.state(other), (1, None, None, None))
        self.assertEqual(
            self.memory.reminder_delivery_counts(),
            DeliveryCounts(0, 0, 0),
        )
        self.assertEqual(self.setting_rows(), [(1, 1, ON_STAMP)])


class ClaimTimeTests(_DeliveryCase):
    """Each change between the candidate read and its claim is judged (A29)."""

    def judge(
        self,
        change: Callable[[int], object],
        *,
        as_of: str = AS_OF,
    ) -> tuple[int, ClaimResult]:
        """Read one due candidate, make one change, then claim it.

        Whatever the claim decides other than claiming writes nothing.
        """
        self.raw("DELETE FROM reminders")
        self.raw("DELETE FROM reminder_delivery_settings")
        self.set_delivery(True)
        reminder_id = self.add()
        (item,) = self.candidates()
        change(reminder_id)
        before = self.rows("reminders")
        result = self.claim(item, as_of=as_of)
        if result.outcome is not ClaimOutcome.CLAIMED:
            self.assertEqual(self.rows("reminders"), before)
        return reminder_id, result

    def outside(
        self,
        column: str,
        value: object,
        *,
        as_blob: bool = False,
    ) -> Callable[[int], object]:
        """Return a change that sets one reminder column from outside Maria."""
        mark = "CAST(? AS BLOB)" if as_blob else "?"
        return lambda reminder_id: self.raw(
            f"UPDATE reminders SET {column} = {mark} WHERE id = ?",
            (value, reminder_id),
        )

    def claimed_elsewhere(self, _reminder_id: int) -> None:
        """Claim the same candidate first, through a second memory layer."""
        other = self.fresh_memory()
        (theirs,) = other.reminder_delivery_candidates(AS_OF, 6)
        other.claim_reminder_delivery(theirs, DELIVERY_CHANNEL_CLI, AS_OF, AS_OF)

    def test_a_row_that_no_longer_qualifies_is_skipped(self) -> None:
        memory = self.memory
        for label, change in (
            ("the text edited", lambda rid: memory.edit_reminder(rid, "call dad")),
            ("completed", lambda rid: memory.complete_reminder(rid)),
            (
                "rescheduled",
                lambda rid: memory.reschedule_reminder(
                    rid,
                    EARLIER_TIME,
                    EARLIER_INSTANT,
                ),
            ),
            (
                "moved away and back",
                lambda rid: (
                    memory.reschedule_reminder(rid, OTHER_TIME, OTHER_INSTANT),
                    memory.reschedule_reminder(rid, TIME, INSTANT),
                ),
            ),
            ("deleted", lambda rid: memory.delete_reminder(rid)),
            ("created_at as a BLOB", self.outside("created_at", STAMP, as_blob=True)),
            ("created_at with NUL", self.outside("created_at", suffixed(STAMP))),
            (
                "created_at not canonical",
                self.outside("created_at", "2026-10-04 05:59:35"),
            ),
            ("updated_at as a BLOB", self.outside("updated_at", STAMP, as_blob=True)),
            ("updated_at with NUL", self.outside("updated_at", suffixed(STAMP))),
            ("updated_at not canonical", self.outside("updated_at", "yesterday")),
            ("a completion time while open", self.outside("completed_at", STAMP)),
            ("an uppercase digest", self.outside("context_digest", "A" * 64)),
            ("a disagreeing instant", self.outside("remind_utc", EARLIER_INSTANT)),
            ("a claim stamp alone", self.outside("delivery_claimed_at", AS_OF)),
            ("a channel alone", self.outside("delivery_channel", DELIVERY_CHANNEL_GUI)),
            ("an outcome alone", self.outside("delivery_outcome", "failed")),
            ("claimed elsewhere first", self.claimed_elsewhere),
        ):
            with self.subTest(change=label):
                _reminder_id, result = self.judge(change)
                self.assertEqual(result, ClaimResult(ClaimOutcome.CHANGED))

    def test_a_saved_time_after_the_claims_instant_is_skipped(self) -> None:
        reminder_id, result = self.judge(
            lambda _rid: None,
            as_of="2027-11-02T17:29:59Z",
        )

        self.assertEqual(result, ClaimResult(ClaimOutcome.CHANGED))
        self.assertEqual(self.state(reminder_id), (1, None, None, None))

    def test_a_change_undone_or_beside_the_point_still_claims(self) -> None:
        memory = self.memory
        digest = reminder_digest("call mom", TIME)
        for label, change in (
            (
                "edited and edited back",
                lambda rid: (
                    memory.edit_reminder(rid, "call dad"),
                    memory.edit_reminder(rid, "call mom"),
                ),
            ),
            (
                "completed and reopened",
                lambda rid: (
                    memory.complete_reminder(rid),
                    memory.reopen_reminder(rid),
                ),
            ),
            ("consent on", lambda rid: memory.enable_reminder_context(rid, digest)),
            (
                "consent on and off",
                lambda rid: (
                    memory.enable_reminder_context(rid, digest),
                    memory.disable_reminder_context(rid),
                ),
            ),
            (
                "a later valid updated_at",
                self.outside("updated_at", "2099-01-01T00:00:00Z"),
            ),
            (
                "delivery off and on again",
                lambda _rid: (
                    self.set_delivery(False, OFF_STAMP),
                    self.set_delivery(True, LATER_STAMP),
                ),
            ),
        ):
            with self.subTest(change=label):
                reminder_id, result = self.judge(change)
                self.assertEqual(
                    result,
                    ClaimResult(ClaimOutcome.CLAIMED, DeliveryClaim(reminder_id, 1)),
                )
                self.assertEqual(
                    self.state(reminder_id),
                    (1, AS_OF, DELIVERY_CHANNEL_DESKTOP, None),
                )

    def test_delivery_not_on_when_claiming_stops_and_writes_nothing(self) -> None:
        for label, change in (
            ("turned off", lambda _rid: self.set_delivery(False, OFF_STAMP)),
            (
                "the setting deleted",
                lambda _rid: self.raw("DELETE FROM reminder_delivery_settings"),
            ),
            (
                "a BLOB setting stamp",
                lambda _rid: self.raw(
                    "UPDATE reminder_delivery_settings "
                    "SET changed_at = CAST(? AS BLOB)",
                    (ON_STAMP,),
                ),
            ),
            (
                "a NUL-suffixed setting stamp",
                lambda _rid: self.raw(
                    "UPDATE reminder_delivery_settings SET changed_at = ?",
                    (suffixed(ON_STAMP),),
                ),
            ),
            (
                "an impossible setting stamp",
                lambda _rid: self.raw(
                    "UPDATE reminder_delivery_settings SET changed_at = ?",
                    (IMPOSSIBLE_STAMP,),
                ),
            ),
        ):
            with self.subTest(change=label):
                reminder_id, result = self.judge(change)
                self.assertEqual(result, ClaimResult(ClaimOutcome.DISABLED))
                self.assertEqual(self.state(reminder_id), (1, None, None, None))

    def test_an_oversized_text_is_skipped_and_never_reaches_python(self) -> None:
        self.set_delivery(True)
        oversized = self.add()
        valid = self.add("water the plants", EARLIER_TIME, EARLIER_INSTANT)
        items = self.candidates()
        self.raw(
            "UPDATE reminders SET text = ? WHERE id = ?",
            (ROW_SENTINEL + "a" * LARGE, oversized),
        )
        seen: list[tuple[object, ...]] = []
        real_check = memory_module._is_deliverable_reminder

        def recorded(*values: object) -> int:
            seen.append(values)
            return real_check(*values)

        with patch.object(memory_module, "_is_deliverable_reminder", recorded):
            results = [self.claim(item) for item in items]
            later = self.candidates()

        self.assertEqual(
            [item.reminder_id for item in items],
            [valid, oversized],
        )
        self.assertEqual(
            results,
            [
                ClaimResult(ClaimOutcome.CLAIMED, DeliveryClaim(valid, 1)),
                ClaimResult(ClaimOutcome.CHANGED),
            ],
        )
        self.assertEqual(later, [])
        # Positive control: the registered check ran, for the valid row only.
        self.assertTrue(seen)
        for values in seen:
            self.assertIs(type(values[0]), str)
            self.assertNotIn(ROW_SENTINEL, values[0])
            self.assertLessEqual(len(values[0].encode("utf-8")), TASK_TEXT_MAX_BYTES)
        self.assertEqual(self.state(oversized), (1, None, None, None))


class StaleOutcomeTests(_DeliveryCase):
    """An outcome is written for its own claim only (A30)."""

    def test_an_old_claims_outcome_never_lands_on_a_newer_claim(self) -> None:
        self.set_delivery(True)
        reminder_id = self.add()
        # The injected clock gives both claims the same second.
        old = claim_notice(self.memory, NOW, DELIVERY_CHANNEL_DESKTOP).notice
        self.memory.reschedule_reminder(reminder_id, EARLIER_TIME, EARLIER_INSTANT)
        new = claim_notice(self.memory, NOW, DELIVERY_CHANNEL_CLI).notice

        self.assertEqual(old.claims, (DeliveryClaim(reminder_id, 1),))
        self.assertEqual(new.claims, (DeliveryClaim(reminder_id, 2),))
        self.assertEqual(
            self.state(reminder_id),
            (2, AS_OF, DELIVERY_CHANNEL_CLI, None),
        )

        # The old outcome finds no claim of its own and is dropped, not failed.
        self.assertEqual(record_notice(self.memory, old, False), RecordFailures())
        self.assertEqual(
            self.state(reminder_id),
            (2, AS_OF, DELIVERY_CHANNEL_CLI, None),
        )
        self.assertEqual(record_notice(self.memory, new, True), RecordFailures())
        self.assertEqual(record_notice(self.memory, old, False), RecordFailures())
        self.assertEqual(
            self.state(reminder_id),
            (2, AS_OF, DELIVERY_CHANNEL_CLI, "displayed"),
        )
        self.assertIn(
            (
                "Dropped an outcome for reminder %s: its claim no longer exists.",
                reminder_id,
            ),
            [call.args for call in self.logger.info.call_args_list],
        )

    def test_a_claims_outcome_is_written_once(self) -> None:
        self.set_delivery(True)
        reminder_id = self.add()
        (item,) = self.candidates()
        claim = self.claim(item).claim

        self.assertTrue(self.memory.record_reminder_delivery(claim, "displayed"))
        self.assertFalse(self.memory.record_reminder_delivery(claim, "failed"))

        self.assertEqual(
            self.state(reminder_id),
            (1, AS_OF, DELIVERY_CHANNEL_DESKTOP, "displayed"),
        )

    def test_an_identity_or_outcome_maria_cannot_have_is_refused_first(self) -> None:
        self.set_delivery(True)
        self.add()
        (item,) = self.candidates()
        claim = self.claim(item).claim
        before = self.rows("reminders")

        for call in (
            lambda: self.memory.record_reminder_delivery((1, 1), "displayed"),
            lambda: self.memory.record_reminder_delivery(claim, "read"),
            lambda: self.memory.record_reminder_delivery(claim, None),
            lambda: self.memory.claim_reminder_delivery(item, "email", AS_OF, AS_OF),
            lambda: self.memory.claim_reminder_delivery(item, "cli", "now", AS_OF),
            lambda: self.memory.claim_reminder_delivery(object(), "cli", AS_OF, AS_OF),
            lambda: self.memory.reminder_delivery_candidates("today", 6),
            lambda: self.memory.reminder_delivery_candidates(AS_OF, 7),
            lambda: self.memory.reminder_delivery_candidates(AS_OF, 0),
        ):
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()
        self.assertEqual(self.rows("reminders"), before)


class CandidateSelectionTests(_DeliveryCase):
    """Only open reminders due at the check's instant are read (D7-A)."""

    # A reminder saved exactly at the check's instant, and one a minute later.
    AT_INSTANT_TIME = f"2027-11-02T18:45:00+01:00[{SYNTHETIC_KEY}]"
    MINUTE_LATER_TIME = f"2027-11-02T18:46:00+01:00[{SYNTHETIC_KEY}]"
    MINUTE_LATER = "2027-11-02T17:46:00Z"

    def test_only_open_reminders_saved_at_or_before_the_instant_are_read(
        self,
    ) -> None:
        self.set_delivery(True)
        due = self.add()
        exactly = self.add("at the instant", self.AT_INSTANT_TIME, AS_OF)
        self.add("a minute later", self.MINUTE_LATER_TIME, self.MINUTE_LATER)
        completed = self.add("completed", EARLIER_TIME, EARLIER_INSTANT)
        self.memory.complete_reminder(completed)

        self.assertEqual(
            [item.reminder_id for item in self.candidates()],
            [due, exactly],
        )

    def test_a_reminder_not_yet_due_never_says_more_are_due(self) -> None:
        self.set_delivery(True)
        ids = self.add_due(5)
        later = self.add("a minute later", self.MINUTE_LATER_TIME, self.MINUTE_LATER)

        batch = claim_notice(self.memory, NOW, DELIVERY_CHANNEL_DESKTOP)

        self.assertEqual(batch.notice.reminder_ids, tuple(ids))
        self.assertIs(batch.notice.more_due, False)
        self.assertEqual(self.state(later), (1, None, None, None))


class CandidateWindowTests(_DeliveryCase):
    """The six-candidate window holds only claimable rows, and says what it left.

    Six rows stored from outside with ids Maria never assigns, -5 through 0,
    are otherwise valid open reminders, due before the valid one: if the
    positive-id gate came after the read's limit, they would fill the window
    and starve every later reminder. SQLite stores such ids as written, and
    every CHECK still applies to them.
    """

    MALFORMED_IDS = tuple(range(-5, 1))

    def store_malformed(self) -> list[tuple[object, ...]]:
        """Store the six rows through a second connection; return them as stored."""
        for reminder_id in self.MALFORMED_IDS:
            self.insert_reminder(
                id=reminder_id,
                text=f"{ROW_SENTINEL} {reminder_id}",
                remind_time=EARLIER_TIME,
                remind_utc=EARLIER_INSTANT,
            )
        return self.malformed_rows()

    def malformed_rows(self) -> list[tuple[object, ...]]:
        """Return every stored row whose id is zero or below, as stored."""
        return self.query("SELECT * FROM reminders WHERE id <= 0 ORDER BY id")

    def test_nonpositive_ids_never_take_a_place_in_the_window(self) -> None:
        self.set_delivery(True)
        before = self.store_malformed()
        valid = self.add()

        self.assertEqual([row[0] for row in before], list(self.MALFORMED_IDS))
        self.assertGreaterEqual(valid, 1)
        self.assertEqual([item.reminder_id for item in self.candidates()], [valid])

        batch = claim_notice(self.memory, NOW, DELIVERY_CHANNEL_DESKTOP)

        self.assertEqual(batch.notice.reminder_ids, (valid,))
        self.assertIs(batch.notice.more_due, False)
        self.assertEqual(
            record_notice(self.memory, batch.notice, displayed=True),
            RecordFailures(),
        )
        self.assertEqual(
            self.state(valid),
            (1, AS_OF, DELIVERY_CHANNEL_DESKTOP, "displayed"),
        )
        self.assertEqual(
            self.memory.reminder_delivery_counts(),
            DeliveryCounts(1, 0, 0),
        )
        self.assertEqual(
            claim_notice(self.memory, NOW, DELIVERY_CHANNEL_CLI),
            ClaimBatch(),
        )
        self.assertEqual(self.malformed_rows(), before)
        self.assertLoggedNothingPrivate(ROW_SENTINEL)

    def test_the_read_and_every_claim_share_the_positive_id_gate(self) -> None:
        self.set_delivery(True)
        self.store_malformed()
        valid = self.add()
        statements: list[str] = []
        memory = self.fresh_memory()
        connect = memory._connect

        def traced() -> sqlite3.Connection:
            connection = connect()
            connection.set_trace_callback(statements.append)
            return connection

        memory._connect = traced  # type: ignore[method-assign]

        batch = claim_notice(memory, NOW, DELIVERY_CHANNEL_DESKTOP)

        self.assertEqual(batch.notice.reminder_ids, (valid,))
        gated = [
            " ".join(statement.split())
            for statement in statements
            if "maria_deliverable_reminder(" in statement
        ]
        self.assertEqual(len(gated), 2)
        read, claim = gated
        self.assertIn("LIMIT 6", read)
        self.assertIn(f"WHERE id = {valid} AND", claim)
        for statement in gated:
            self.assertIn("CASE WHEN id >= 1 AND", statement)

    def test_a_candidate_edited_before_its_claim_is_left_and_more_is_said(
        self,
    ) -> None:
        self.set_delivery(True)
        first, second = self.add_due(2)
        store = HookedStore(
            self.memory,
            before={2: lambda: self.fresh_memory().edit_reminder(second, "call dad")},
        )

        batch = claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP)

        self.assertEqual(batch.notice.reminder_ids, (first,))
        self.assertIs(batch.notice.more_due, True)
        self.assertTrue(batch.notice.text.endswith(f"\n\n{MORE_DUE_LINE}"))
        self.assertEqual(self.state(second), (1, None, None, None))
        self.assertEqual(
            record_notice(self.memory, batch.notice, displayed=True),
            RecordFailures(),
        )

        later = claim_notice(
            self.memory,
            NOW + timedelta(minutes=1),
            DELIVERY_CHANNEL_DESKTOP,
        )

        self.assertEqual(later.notice.reminder_ids, (second,))
        self.assertIn('"call dad"', later.notice.text)
        self.assertIs(later.notice.more_due, False)
        self.assertEqual(
            self.state(second),
            (1, "2027-11-02T17:46:00Z", DELIVERY_CHANNEL_DESKTOP, None),
        )


class AtMostOnceTests(_DeliveryCase):
    """No reminder is shown twice in one revision, by any surface (A5, A14)."""

    def setUp(self) -> None:
        super().setUp()
        self.set_delivery(True)

    def test_a_claim_is_never_made_again_after_a_restart(self) -> None:
        reminder_id = self.add()
        # Claimed and never recorded, as when a process dies before display.
        first = claim_notice(self.memory, NOW, DELIVERY_CHANNEL_DESKTOP)
        self.assertEqual(first.notice.reminder_ids, (reminder_id,))

        for channel in (
            DELIVERY_CHANNEL_DESKTOP,
            DELIVERY_CHANNEL_CLI,
            DELIVERY_CHANNEL_GUI,
        ):
            with self.subTest(channel=channel):
                later = NOW + timedelta(days=1)
                self.assertEqual(
                    claim_notice(self.fresh_memory(), later, channel),
                    ClaimBatch(),
                )
        self.assertEqual(
            self.state(reminder_id),
            (1, AS_OF, DELIVERY_CHANNEL_DESKTOP, None),
        )

    def test_a_claim_between_anothers_read_and_claim_is_never_doubled(self) -> None:
        reminder_id = self.add()
        other = self.fresh_memory()
        (mine,) = self.candidates()
        (theirs,) = other.reminder_delivery_candidates(AS_OF, 6)

        self.assertEqual(
            self.claim(mine),
            ClaimResult(ClaimOutcome.CLAIMED, DeliveryClaim(reminder_id, 1)),
        )
        self.assertEqual(
            other.claim_reminder_delivery(theirs, DELIVERY_CHANNEL_CLI, AS_OF, AS_OF),
            ClaimResult(ClaimOutcome.CHANGED),
        )
        self.assertEqual(
            self.state(reminder_id),
            (1, AS_OF, DELIVERY_CHANNEL_DESKTOP, None),
        )

    def test_two_surfaces_racing_each_show_only_their_own_claims(self) -> None:
        ids = self.add_due(3)
        other = self.fresh_memory()
        theirs: list[ClaimBatch] = []
        store = HookedStore(
            self.memory,
            before={
                2: lambda: theirs.append(
                    claim_notice(other, NOW, DELIVERY_CHANNEL_CLI)
                )
            },
        )

        mine = claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP)

        (their_batch,) = theirs
        self.assertEqual(mine.notice.reminder_ids, (ids[0],))
        self.assertEqual(their_batch.notice.reminder_ids, (ids[1], ids[2]))
        self.assertEqual(
            record_notice(self.memory, mine.notice, True),
            RecordFailures(),
        )
        self.assertEqual(
            record_notice(other, their_batch.notice, True),
            RecordFailures(),
        )
        self.assertEqual(
            [self.state(reminder_id)[2:] for reminder_id in ids],
            [
                (DELIVERY_CHANNEL_DESKTOP, "displayed"),
                (DELIVERY_CHANNEL_CLI, "displayed"),
                (DELIVERY_CHANNEL_CLI, "displayed"),
            ],
        )

    def test_a_late_approval_restoring_a_shown_time_shows_it_once_more(self) -> None:
        reminder_id = self.add()
        record_notice(
            self.memory,
            claim_notice(self.memory, NOW, DELIVERY_CHANNEL_DESKTOP).notice,
            True,
        )
        # A reschedule approved after its time passed is stored as approved,
        # so a late approval can restore a time already shown: revision 3.
        self.memory.reschedule_reminder(reminder_id, OTHER_TIME, OTHER_INSTANT)
        self.memory.reschedule_reminder(reminder_id, TIME, INSTANT)

        again = claim_notice(
            self.fresh_memory(),
            NOW + timedelta(minutes=5),
            DELIVERY_CHANNEL_CLI,
        )

        self.assertEqual(again.notice.claims, (DeliveryClaim(reminder_id, 3),))
        self.assertEqual(
            claim_notice(
                self.fresh_memory(),
                NOW + timedelta(minutes=6),
                DELIVERY_CHANNEL_GUI,
            ),
            ClaimBatch(),
        )

    def test_a_same_instant_reschedule_after_display_shows_nothing_again(
        self,
    ) -> None:
        reminder_id = self.add()
        record_notice(
            self.memory,
            claim_notice(self.memory, NOW, DELIVERY_CHANNEL_DESKTOP).notice,
            True,
        )

        self.memory.reschedule_reminder(reminder_id, SAME_INSTANT_TIME, INSTANT)

        self.assertEqual(
            claim_notice(self.memory, NOW + timedelta(minutes=1), DELIVERY_CHANNEL_CLI),
            ClaimBatch(),
        )
        self.assertEqual(
            self.state(reminder_id),
            (1, AS_OF, DELIVERY_CHANNEL_DESKTOP, "displayed"),
        )

    def test_five_at_a_time_oldest_first_and_the_rest_follow(self) -> None:
        ids = self.add_due(7)

        first = claim_notice(self.memory, NOW, DELIVERY_CHANNEL_DESKTOP)
        second = claim_notice(self.memory, NOW, DELIVERY_CHANNEL_DESKTOP)

        self.assertEqual(first.notice.reminder_ids, tuple(ids[:5]))
        self.assertIs(first.notice.more_due, True)
        self.assertEqual(second.notice.reminder_ids, tuple(ids[5:]))
        self.assertIs(second.notice.more_due, False)
        self.assertEqual(
            claim_notice(self.memory, NOW, DELIVERY_CHANNEL_CLI),
            ClaimBatch(),
        )


class PartialBatchStorageTests(_DeliveryCase):
    """A batch that stops keeps exactly the claims before it (A31, D13-A)."""

    def setUp(self) -> None:
        super().setUp()
        self.set_delivery(True)
        self.ids = self.add_due(5)

    def assertStoppedAt(self, position: int, batch: ClaimBatch) -> None:
        """Assert claims before ``position`` were retained and none after it."""
        retained = tuple(self.ids[: position - 1])
        if retained:
            self.assertEqual(batch.notice.reminder_ids, retained)
            self.assertIs(batch.notice.more_due, True)
        else:
            self.assertIsNone(batch.notice)
        for reminder_id in retained:
            self.assertEqual(
                self.state(reminder_id),
                (1, AS_OF, DELIVERY_CHANNEL_DESKTOP, None),
            )

    def test_a_busy_claim_at_each_position_is_rolled_back(self) -> None:
        for position in range(1, 6):
            with self.subTest(position=position):
                self.clear_claims()
                releases: list[Callable[[], None]] = []
                store = HookedStore(
                    self.memory,
                    before={
                        position: lambda: releases.append(
                            _hold_write_lock(self, self.database_path)
                        )
                    },
                )
                with patch.object(
                    memory_module,
                    "DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS",
                    0,
                ):
                    with self.assertLogs("maria.reminder_delivery", "ERROR"):
                        batch = claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP)
                releases[0]()

                self.assertEqual(
                    (batch.stopped, batch.failure),
                    (ClaimStop.STORAGE, "DeliveryStorageError"),
                )
                self.assertStoppedAt(position, batch)
                for reminder_id in self.ids[position - 1 :]:
                    self.assertEqual(self.state(reminder_id), (1, None, None, None))

    def test_a_failing_claim_write_at_each_position_is_rolled_back(self) -> None:
        for position in range(1, 6):
            with self.subTest(position=position):
                self.clear_claims()
                self.raw(
                    "CREATE TRIGGER refuse_one_claim BEFORE UPDATE OF "
                    "delivery_claimed_at ON reminders "
                    f"WHEN NEW.id = {self.ids[position - 1]:d} BEGIN "
                    f"SELECT RAISE(ABORT, '{TRIGGER_SENTINEL}'); END"
                )
                self.logger.reset_mock()
                with self.assertLogs("maria.reminder_delivery", "ERROR") as logs:
                    batch = claim_notice(self.memory, NOW, DELIVERY_CHANNEL_DESKTOP)
                self.raw("DROP TRIGGER refuse_one_claim")

                self.assertEqual(
                    (batch.stopped, batch.failure),
                    (ClaimStop.STORAGE, "DeliveryStorageError"),
                )
                self.assertStoppedAt(position, batch)
                for reminder_id in self.ids[position - 1 :]:
                    self.assertEqual(self.state(reminder_id), (1, None, None, None))
                self.assertNotIn(TRIGGER_SENTINEL, "\n".join(logs.output))
                self.assertLoggedNothingPrivate(TRIGGER_SENTINEL)
                self.logger.error.assert_called_with(
                    "Failed to %s: %s.",
                    "claim a reminder for delivery",
                    "database error",
                )

    def test_a_claim_that_raises_after_its_commit_is_never_shown_or_retried(
        self,
    ) -> None:
        for position in range(1, 6):
            with self.subTest(position=position):
                self.clear_claims()
                target = self.ids[position - 1]
                logger = Mock(spec=logging.Logger)

                def info(message: str, *values: object, target: int = target) -> None:
                    if message.startswith("Claimed reminder") and values[0] == target:
                        raise RuntimeError(TRIGGER_SENTINEL)

                logger.info.side_effect = info
                raising = Memory(self.database_path, logger=logger)
                with self.assertLogs("maria.reminder_delivery", "ERROR") as logs:
                    batch = claim_notice(raising, NOW, DELIVERY_CHANNEL_DESKTOP)

                self.assertEqual(
                    (batch.stopped, batch.failure),
                    (ClaimStop.UNEXPECTED, "RuntimeError"),
                )
                self.assertStoppedAt(position, batch)
                # Committed, so never claimed again; never displayed.
                unconfirmed = (1, AS_OF, DELIVERY_CHANNEL_DESKTOP, None)
                self.assertEqual(self.state(target), unconfirmed)
                if batch.notice is not None:
                    self.assertEqual(
                        record_notice(self.memory, batch.notice, True),
                        RecordFailures(),
                    )
                later = claim_notice(
                    self.fresh_memory(),
                    NOW + timedelta(minutes=1),
                    DELIVERY_CHANNEL_CLI,
                )
                rest = tuple(self.ids[position:])
                if rest:
                    self.assertEqual(later.notice.reminder_ids, rest)
                else:
                    self.assertEqual(later, ClaimBatch())
                self.assertEqual(self.state(target), unconfirmed)
                self.assertNotIn(TRIGGER_SENTINEL, "\n".join(logs.output))

    def test_a_pass_that_dies_after_each_claim_leaves_the_rest_to_the_next(
        self,
    ) -> None:
        for position in range(1, 6):
            with self.subTest(position=position):
                self.clear_claims()
                store = HookedStore(self.memory, after={position: _abandon})

                with self.assertRaises(_Abandoned):
                    claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP)
                later = claim_notice(
                    self.fresh_memory(),
                    NOW + timedelta(minutes=1),
                    DELIVERY_CHANNEL_DESKTOP,
                )

                for reminder_id in self.ids[:position]:
                    self.assertEqual(
                        self.state(reminder_id),
                        (1, AS_OF, DELIVERY_CHANNEL_DESKTOP, None),
                    )
                rest = tuple(self.ids[position:])
                if rest:
                    self.assertEqual(later.notice.reminder_ids, rest)
                else:
                    self.assertEqual(later, ClaimBatch())

    def test_a_failing_outcome_write_leaves_only_its_own_claim_unconfirmed(
        self,
    ) -> None:
        batch = claim_notice(self.memory, NOW, DELIVERY_CHANNEL_DESKTOP)
        self.raw(
            "CREATE TRIGGER refuse_one_outcome BEFORE UPDATE OF delivery_outcome "
            f"ON reminders WHEN NEW.id = {self.ids[2]:d} BEGIN "
            f"SELECT RAISE(ABORT, '{TRIGGER_SENTINEL}'); END"
        )

        with self.assertLogs("maria.reminder_delivery", "ERROR") as logs:
            failures = record_notice(self.memory, batch.notice, True)

        self.assertEqual(failures, RecordFailures(storage=1))
        self.assertEqual(
            [self.state(reminder_id)[3] for reminder_id in self.ids],
            ["displayed", "displayed", None, "displayed", "displayed"],
        )
        self.assertEqual(
            self.memory.reminder_delivery_counts(),
            DeliveryCounts(4, 1, 0),
        )
        self.assertNotIn(TRIGGER_SENTINEL, "\n".join(logs.output))
        self.assertLoggedNothingPrivate(TRIGGER_SENTINEL)


class DisableBoundaryTests(_DeliveryCase):
    """A disable stops every later claim and cancels none made before (A34)."""

    def setUp(self) -> None:
        super().setUp()
        self.set_delivery(True)
        self.ids = self.add_due(3)

    def disable(self) -> None:
        self.set_delivery(False, OFF_STAMP)

    def assertUnclaimed(self, *ids: int) -> None:
        for reminder_id in ids:
            self.assertEqual(self.state(reminder_id), (1, None, None, None))

    def test_a_disable_before_the_setting_read_claims_nothing(self) -> None:
        self.disable()

        self.assertEqual(
            claim_notice(self.memory, NOW, DELIVERY_CHANNEL_DESKTOP),
            ClaimBatch(),
        )
        self.assertUnclaimed(*self.ids)

    def test_a_disable_before_the_first_claim_stops_the_batch_empty(self) -> None:
        store = HookedStore(self.memory, before={1: self.disable})

        with self.assertLogs("maria.reminder_delivery", "INFO"):
            batch = claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP)

        self.assertEqual(batch, ClaimBatch(None, ClaimStop.DISABLED, None))
        self.assertUnclaimed(*self.ids)

    def test_a_disable_between_claims_keeps_and_records_the_earlier_ones(
        self,
    ) -> None:
        store = HookedStore(self.memory, before={2: self.disable})

        with self.assertLogs("maria.reminder_delivery", "INFO"):
            batch = claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP)
        failures = record_notice(self.memory, batch.notice, True)

        self.assertEqual((batch.stopped, batch.failure), (ClaimStop.DISABLED, None))
        self.assertEqual(batch.notice.reminder_ids, (self.ids[0],))
        self.assertIs(batch.notice.more_due, True)
        self.assertEqual(failures, RecordFailures())
        self.assertEqual(
            self.state(self.ids[0]),
            (1, AS_OF, DELIVERY_CHANNEL_DESKTOP, "displayed"),
        )
        self.assertUnclaimed(*self.ids[1:])

    def test_a_disable_after_the_claims_still_records_their_outcomes(self) -> None:
        batch = claim_notice(self.memory, NOW, DELIVERY_CHANNEL_DESKTOP)
        self.disable()

        self.assertEqual(
            record_notice(self.memory, batch.notice, True),
            RecordFailures(),
        )

        self.assertEqual(
            [self.state(reminder_id)[3] for reminder_id in self.ids],
            ["displayed"] * 3,
        )

    def test_a_disable_while_a_window_notice_awaits_acknowledgment(self) -> None:
        session = MariaSession(
            Mock(spec=MariaBrain),
            Mock(spec=logging.Logger),
            delivery=ReminderDeliverySeam(
                self.memory,
                DELIVERY_CHANNEL_GUI,
                lambda: NOW,
            ),
        )
        notice = session.claim_due_reminders()
        self.disable()
        late = self.add("due after the disable", EARLIER_TIME, EARLIER_INSTANT)

        session.record_reminder_notice(notice, displayed=True)

        self.assertEqual(
            [self.state(reminder_id)[2:] for reminder_id in self.ids],
            [(DELIVERY_CHANNEL_GUI, "displayed")] * 3,
        )
        self.assertIsNone(session.claim_due_reminders())
        self.assertUnclaimed(late)

    def test_a_disable_and_an_enable_between_read_and_claim_let_it_proceed(
        self,
    ) -> None:
        store = HookedStore(
            self.memory,
            before={1: lambda: (self.disable(), self.set_delivery(True, LATER_STAMP))},
        )

        batch = claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP)

        self.assertEqual(batch.notice.reminder_ids, tuple(self.ids))
        self.assertIsNone(batch.stopped)


class DeliveryCountTests(_DeliveryCase):
    """Counts cover only current claims, by what became of them."""

    def test_counts_follow_each_claims_outcome_and_revision(self) -> None:
        self.set_delivery(True)
        first, second, third, fourth = self.add_due(4)
        claim_notice(self.memory, NOW, DELIVERY_CHANNEL_DESKTOP)
        self.memory.record_reminder_delivery(DeliveryClaim(first, 1), "displayed")
        self.memory.record_reminder_delivery(DeliveryClaim(second, 1), "failed")
        self.memory.record_reminder_delivery(DeliveryClaim(fourth, 1), "displayed")
        counts = self.memory.reminder_delivery_counts

        self.assertEqual(counts(), DeliveryCounts(2, 1, 1))

        self.memory.reschedule_reminder(fourth, OTHER_TIME, OTHER_INSTANT)
        self.assertEqual(counts(), DeliveryCounts(1, 1, 1))

        self.memory.delete_reminder(first)
        self.assertEqual(counts(), DeliveryCounts(0, 1, 1))
        self.assertEqual(self.state(third), (1, AS_OF, DELIVERY_CHANNEL_DESKTOP, None))


class ClosedAppPassStorageTests(_DeliveryCase):
    """A whole pass over a real temporary database, with fake Windows calls."""

    def setUp(self) -> None:
        super().setUp()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.config_path = root / "config.toml"
        self.config_path.write_text(
            "[paths]\n"
            f'database = "{self.database_path.as_posix()}"\n'
            f'logs = "{(root / "logs").as_posix()}"\n',
            encoding="utf-8",
        )
        self.boxes: list[tuple[str, str]] = []

    def notices(
        self,
        result: object = True,
        on_show: Callable[[], object] | None = None,
        state: NotificationState = NotificationState.ACCEPTS,
    ) -> object:
        """Return fake Windows notice functions, accepting unless told not to."""
        boxes = self.boxes

        class _Notices:
            def available(self) -> bool:
                return True

            def notification_state(self) -> NotificationState:
                return state

            def show(self, title: str, text: str) -> bool:
                boxes.append((title, text))
                if on_show is not None:
                    on_show()
                if isinstance(result, BaseException):
                    raise result
                return result  # type: ignore[return-value]

        return _Notices()

    def pass_status(self, notices: object | None = None, now: datetime = NOW) -> int:
        """Run one pass whose every seam but the database is a fake."""
        return run_pass(
            clock=lambda: now,
            config_path=self.config_path,
            notices=notices or self.notices(),  # type: ignore[arg-type]
            memory_factory=lambda path: Memory(
                path,
                logger=Mock(spec=logging.Logger),
                existing_only=True,
            ),
            configure_log=lambda _log_dir: None,
        )

    def test_a_pass_shows_due_reminders_once_and_records_them(self) -> None:
        self.set_delivery(True)
        ids = self.add_due(2)

        self.assertEqual(self.pass_status(), EXIT_DONE)
        self.assertEqual(self.pass_status(now=NOW + timedelta(minutes=1)), EXIT_DONE)

        (box,) = self.boxes
        self.assertEqual(box[0], "Maria reminders")
        self.assertEqual(
            [self.state(reminder_id)[2:] for reminder_id in ids],
            [(DELIVERY_CHANNEL_DESKTOP, "displayed")] * 2,
        )

    def test_delivery_never_enabled_claims_and_writes_nothing(self) -> None:
        ids = self.add_due(2)
        before = self.database_path.read_bytes()

        self.assertEqual(self.pass_status(), EXIT_DONE)

        self.assertEqual(self.boxes, [])
        self.assertEqual(self.database_path.read_bytes(), before)
        for reminder_id in ids:
            self.assertEqual(self.state(reminder_id), (1, None, None, None))

    def test_a_disable_while_the_box_is_open_still_records_it(self) -> None:
        self.set_delivery(True)
        (reminder_id,) = self.add_due(1)
        notices = self.notices(on_show=lambda: self.set_delivery(False, OFF_STAMP))

        self.assertEqual(self.pass_status(notices), EXIT_DONE)
        self.add("due after the disable", EARLIER_TIME, EARLIER_INSTANT)
        self.assertEqual(self.pass_status(now=NOW + timedelta(minutes=1)), EXIT_DONE)

        self.assertEqual(len(self.boxes), 1)
        self.assertEqual(
            self.state(reminder_id),
            (1, AS_OF, DELIVERY_CHANNEL_DESKTOP, "displayed"),
        )

    def test_a_box_that_fails_records_not_displayed_and_never_retries(self) -> None:
        self.set_delivery(True)
        (reminder_id,) = self.add_due(1)

        with self.assertLogs("maria.delivery", "ERROR"):
            status = self.pass_status(self.notices(result=OSError("no desktop")))

        self.assertEqual(status, EXIT_DISPLAY_FAILED)
        self.assertEqual(self.pass_status(now=NOW + timedelta(minutes=1)), EXIT_DONE)
        self.assertEqual(len(self.boxes), 1)
        self.assertEqual(
            self.state(reminder_id),
            (1, AS_OF, DELIVERY_CHANNEL_DESKTOP, "failed"),
        )

    def test_a_packaging_failure_exits_five_with_the_claims_unconfirmed(self) -> None:
        self.set_delivery(True)
        ids = self.add_due(2)
        boundary_pass = partial(
            run_pass,
            clock=lambda: NOW,
            config_path=self.config_path,
            notices=self.notices(),
            memory_factory=lambda path: Memory(
                path,
                logger=Mock(spec=logging.Logger),
                existing_only=True,
            ),
            configure_log=lambda _log_dir: None,
        )

        with (
            patch.object(deliver_reminders, "run_pass", boundary_pass),
            patch.object(
                reminder_delivery_module,
                "ReminderNotice",
                side_effect=ValueError("packaging"),
            ),
            self.assertLogs("maria.delivery", "ERROR") as logs,
        ):
            status = deliver_reminders.main()

        self.assertEqual(status, EXIT_UNEXPECTED)
        self.assertEqual(
            logs.output,
            ["ERROR:maria.delivery:The reminder pass failed: ValueError."],
        )
        self.assertEqual(self.boxes, [])
        for reminder_id in ids:
            self.assertEqual(
                self.state(reminder_id),
                (1, AS_OF, DELIVERY_CHANNEL_DESKTOP, None),
            )
        self.assertEqual(self.pass_status(now=NOW + timedelta(minutes=1)), EXIT_DONE)
        self.assertEqual(self.boxes, [])

    def test_a_reminder_missed_by_deferral_disabling_or_sleep_is_overdue(
        self,
    ) -> None:
        self.set_delivery(True)
        reminder_id = self.add()
        deferring = self.notices(state=NotificationState.DEFERS)

        # Windows defers notifications a minute after its time.
        self.assertEqual(
            self.pass_status(deferring, now=NOW - timedelta(minutes=14)),
            EXIT_DONE,
        )
        # Delivery is off ten minutes later.
        self.set_delivery(False, OFF_STAMP)
        self.assertEqual(
            self.pass_status(now=NOW - timedelta(minutes=5)),
            EXIT_DONE,
        )
        self.assertEqual(self.boxes, [])
        self.assertEqual(self.state(reminder_id), (1, None, None, None))
        # Turned on again, the computer then sleeps until 18:30.
        self.set_delivery(True, LATER_STAMP)
        woken = NOW + timedelta(minutes=45)

        self.assertEqual(self.pass_status(now=woken), EXIT_DONE)

        ((_title, text),) = self.boxes
        self.assertTrue(text.startswith(f"Reminder {reminder_id} is overdue: "))
        self.assertEqual(
            self.state(reminder_id),
            (1, "2027-11-02T18:30:00Z", DELIVERY_CHANNEL_DESKTOP, "displayed"),
        )

    def test_the_box_shows_the_notice_text_of_the_claimed_candidates(self) -> None:
        self.set_delivery(True)
        (reminder_id,) = self.add_due(1)
        (item,) = self.candidates()

        self.assertEqual(self.pass_status(), EXIT_DONE)

        self.assertEqual(self.boxes, [("Maria reminder", notice_entry(item, AS_OF))])
        self.assertEqual(self.state(reminder_id)[3], "displayed")


class DeliveryReadinessTests(_TemporaryDatabaseTestCase):
    """The pass's readiness check accepts only the current schema, read-only."""

    def test_a_current_database_is_ready_and_reads_its_setting(self) -> None:
        database_path = self.database("current")
        Memory(database_path, logger=Mock(spec=logging.Logger)).initialize()

        ready = Memory(
            database_path,
            logger=Mock(spec=logging.Logger),
            existing_only=True,
        ).delivery_state()

        self.assertEqual(ready, DeliverySetting(DeliverySettingState.NEVER_SET))

    def test_a_database_not_at_the_current_schema_is_refused_untouched(self) -> None:
        for label, schema, marker in (
            ("version seven", V7_SCHEMA, 7),
            (
                "version eight without its setting table",
                tuple(
                    statement
                    for statement in V8_SCHEMA
                    if statement != FROZEN_REMINDER_DELIVERY_SETTINGS_TABLE
                ),
                8,
            ),
            ("stamped nine", V8_SCHEMA, 9),
        ):
            with self.subTest(case=label):
                database_path = self.database(label.replace(" ", "-"))
                _build_schema(database_path, schema, marker)
                before = database_path.read_bytes()
                logger = Mock(spec=logging.Logger)

                pass_memory = Memory(database_path, logger=logger, existing_only=True)
                with self.assertRaises(DeliveryStorageError) as caught:
                    pass_memory.delivery_state()

                self.assertEqual(str(caught.exception), DELIVERY_STORAGE_MESSAGE)
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
                self.assertEqual(database_path.read_bytes(), before)
                self.assertEqual(read_schema_version(database_path), marker)
                logger.error.assert_called_once_with(*NOT_CURRENT_LINE)

    def test_a_pass_over_an_older_database_exits_three_and_migrates_nothing(
        self,
    ) -> None:
        database_path = self.database("older")
        _build_schema(database_path, V7_SCHEMA, 7)
        config_path = self.root / "config.toml"
        config_path.write_text(
            "[paths]\n"
            f'database = "{database_path.as_posix()}"\n'
            f'logs = "{(self.root / "logs").as_posix()}"\n',
            encoding="utf-8",
        )
        before = database_path.read_bytes()
        notices = Mock()
        notices.available.return_value = True
        notices.notification_state.return_value = NotificationState.ACCEPTS
        configure_log = Mock()

        status = run_pass(
            clock=lambda: NOW,
            config_path=config_path,
            notices=notices,
            memory_factory=lambda path: Memory(
                path,
                logger=Mock(spec=logging.Logger),
                existing_only=True,
            ),
            configure_log=configure_log,
        )

        self.assertEqual(status, EXIT_STORAGE)
        self.assertEqual(database_path.read_bytes(), before)
        configure_log.assert_not_called()
        notices.show.assert_not_called()

    def test_a_database_locked_by_a_writer_is_busy_and_unchanged(self) -> None:
        database_path = self.database("locked")
        Memory(database_path, logger=Mock(spec=logging.Logger)).initialize()
        before = database_path.read_bytes()
        holder = sqlite3.connect(database_path, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")

        with patch.object(memory_module, "DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS", 0):
            with self.assertRaises(DeliveryStorageError) as caught:
                Memory(
                    database_path,
                    logger=Mock(spec=logging.Logger),
                    existing_only=True,
                ).delivery_state()
        holder.execute("ROLLBACK")

        self.assertEqual(str(caught.exception), "Database is busy. Try again shortly.")
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(database_path.read_bytes(), before)
        self.assertEqual(read_schema_version(database_path), 8)


class DeliveryMigrationTests(_TemporaryDatabaseTestCase):
    """Schema 8's one step keeps every reminder, unclaimed at revision 1 (A16)."""

    def build_seeded_v7(self, label: str) -> Path:
        """Build a stamped version 7 database holding reminders and a gap."""
        database_path = self.database(label)
        _build_schema(database_path, V7_SCHEMA, 7)
        for values in (
            ("call mom", TIME, INSTANT, "open", STAMP, STAMP, None, None),
            ("deleted later", TIME, INSTANT, "open", STAMP, STAMP, None, None),
            (
                "water the plants",
                OTHER_TIME,
                OTHER_INSTANT,
                "completed",
                STAMP,
                STAMP,
                STAMP,
                None,
            ),
            (
                "with consent",
                TIME,
                INSTANT,
                "open",
                STAMP,
                STAMP,
                None,
                reminder_digest("with consent", TIME),
            ),
        ):
            _execute(
                database_path,
                "INSERT INTO reminders (text, remind_time, remind_utc, status, "
                "created_at, updated_at, completed_at, context_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        _execute(database_path, "DELETE FROM reminders WHERE id = 2")
        return database_path

    def reminders(self, database_path: Path) -> list[tuple[object, ...]]:
        return _query(database_path, "SELECT * FROM reminders ORDER BY id")

    def test_existing_reminders_read_revision_one_and_no_claim(self) -> None:
        database_path = self.build_seeded_v7("seeded")
        before = self.reminders(database_path)
        sequence = _query(database_path, "SELECT * FROM sqlite_sequence ORDER BY name")
        recording = Mock(spec=logging.Logger)
        memory = Memory(database_path, logger=recording)

        memory.initialize()
        memory.initialize()

        self.assertEqual(read_schema_version(database_path), 8)
        self.assertEqual(_applied(recording), [DELIVERY_STEP_NAME])
        self.assertEqual(
            self.reminders(database_path),
            [row + (1, None, None, None) for row in before],
        )
        self.assertEqual(
            _query(database_path, "SELECT * FROM sqlite_sequence ORDER BY name"),
            sequence,
        )
        self.assertEqual(
            _query(database_path, "SELECT * FROM reminder_delivery_settings"),
            [],
        )
        for row in before:
            reminder = memory.get_reminder(row[0])
            self.assertEqual((reminder.schedule_revision, reminder.delivery), (1, None))
        self.assertEqual(
            memory.reminder_delivery_setting(),
            DeliverySetting(DeliverySettingState.NEVER_SET),
        )

    def test_a_failure_inside_the_step_leaves_version_seven_whole(self) -> None:
        (step,) = [
            migration
            for migration in memory_module._MIGRATIONS
            if migration.name == DELIVERY_STEP_NAME
        ]
        for label, statements in (
            ("after the first column", (step.statements[0], NEVER_COMMITTED)),
            ("after every statement", (*step.statements, NEVER_COMMITTED)),
        ):
            with self.subTest(injected=label):
                database_path = self.build_seeded_v7(label.replace(" ", "-"))
                before = self.reminders(database_path)
                ladder = tuple(
                    memory_module._Migration(
                        version=8,
                        name=DELIVERY_STEP_NAME,
                        statements=statements,
                    )
                    if migration.name == DELIVERY_STEP_NAME
                    else migration
                    for migration in memory_module._MIGRATIONS
                )
                recording = Mock(spec=logging.Logger)

                with patch.object(memory_module, "_MIGRATIONS", ladder):
                    with self.assertRaisesRegex(RuntimeError, DATABASE_REFUSED_PATTERN):
                        Memory(database_path, logger=recording).initialize()

                self.assertEqual(read_schema_version(database_path), 7)
                self.assertEqual(
                    _describe(database_path),
                    memory_module._reference_schema(V7_SCHEMA),
                )
                self.assertEqual(self.reminders(database_path), before)

                Memory(database_path, logger=recording).initialize()
                self.assertEqual(read_schema_version(database_path), 8)
                self.assertEqual(
                    self.reminders(database_path),
                    [row + (1, None, None, None) for row in before],
                )

    def test_a_version_eight_database_that_drifted_is_refused_untouched(
        self,
    ) -> None:
        def altered(old: str, new: str, table: str) -> tuple[str, ...]:
            changed = table.replace(old, new)
            self.assertNotEqual(changed, table)
            return tuple(
                changed if statement == table else statement
                for statement in V8_SCHEMA
            )

        reminders = FROZEN_REMINDERS_TABLE_WITH_DELIVERY_STATE
        settings = FROZEN_REMINDER_DELIVERY_SETTINGS_TABLE
        for label, schema in (
            (
                "no setting table",
                tuple(statement for statement in V8_SCHEMA if statement != settings),
            ),
            (
                "a fourth channel",
                altered("'cli', 'gui')", "'cli', 'gui', 'email')", reminders),
            ),
            (
                "an unchecked revision",
                altered(
                    "\n            CHECK (typeof(schedule_revision) = 'integer' "
                    "AND schedule_revision >= 1)",
                    "",
                    reminders,
                ),
            ),
            (
                "a delivered marker",
                altered(
                    "completed_at TEXT,",
                    "completed_at TEXT,\n        delivered_at TEXT,",
                    reminders,
                ),
            ),
            (
                "a second setting row allowed",
                altered("PRIMARY KEY CHECK (id = 1)", "PRIMARY KEY", settings),
            ),
        ):
            with self.subTest(drift=label):
                database_path = self.database(label.replace(" ", "-"))
                _build_schema(database_path, schema, 8)

                recording = self.assert_refused_untouched(database_path, marker=8)

                self.assertEqual(
                    _messages(recording, "error"),
                    [DRIFTED_VERSION_EIGHT_LINE],
                )


# The integrity, claim-time, and candidate-window tests again, on a migrated
# database and on UTF-16 databases, where each stored-byte bound doubles.
class MigratedSettingIntegrityTests(MigratedStorageTests, SettingIntegrityTests):
    """Setting integrity on a migrated UTF-8 database."""


class Utf16SettingIntegrityTests(Utf16Mixin, SettingIntegrityTests):
    """Setting integrity on a new UTF-16 database."""


class MigratedUtf16SettingIntegrityTests(
    MigratedStorageTests,
    Utf16Mixin,
    SettingIntegrityTests,
):
    """Setting integrity on a migrated UTF-16 database."""


class MigratedDeliveryStateIntegrityTests(
    MigratedStorageTests,
    DeliveryStateIntegrityTests,
):
    """Delivery-state integrity on a migrated UTF-8 database."""


class Utf16DeliveryStateIntegrityTests(Utf16Mixin, DeliveryStateIntegrityTests):
    """Delivery-state integrity on a new UTF-16 database."""


class MigratedUtf16DeliveryStateIntegrityTests(
    MigratedStorageTests,
    Utf16Mixin,
    DeliveryStateIntegrityTests,
):
    """Delivery-state integrity on a migrated UTF-16 database."""


class MigratedClaimTimeTests(MigratedStorageTests, ClaimTimeTests):
    """Claim-time judgment on a migrated UTF-8 database."""


class Utf16ClaimTimeTests(Utf16Mixin, ClaimTimeTests):
    """Claim-time judgment on a new UTF-16 database."""


class MigratedUtf16ClaimTimeTests(MigratedStorageTests, Utf16Mixin, ClaimTimeTests):
    """Claim-time judgment on a migrated UTF-16 database."""


class MigratedCandidateWindowTests(MigratedStorageTests, CandidateWindowTests):
    """The candidate window on a migrated UTF-8 database."""


class Utf16CandidateWindowTests(Utf16Mixin, CandidateWindowTests):
    """The candidate window on a new UTF-16 database."""


class MigratedUtf16CandidateWindowTests(
    MigratedStorageTests,
    Utf16Mixin,
    CandidateWindowTests,
):
    """The candidate window on a migrated UTF-16 database."""


if __name__ == "__main__":
    unittest.main()
