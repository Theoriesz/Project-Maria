"""Focused tests for reminder delivery's one claim protocol (V1-23-R1).

They drive ``core.reminder_delivery`` through a fake store, so every claim,
failure, and stop is chosen by the test: the notice text and its limits, the
order of reading, rendering, and claiming, the retained prefix of a batch
that stops early, and the writing of each claim's outcome.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from core.reminder_delivery import (
    CANDIDATE_READ_LIMIT,
    MORE_DUE_LINE,
    NOTICE_TITLE,
    NOTICE_TITLE_SEVERAL,
    ClaimBatch,
    ClaimStop,
    RecordFailures,
    ReminderDeliverySeam,
    ReminderNotice,
    claim_notice,
    instant_text,
    notice_entry,
    record_notice,
)
from core.task_records import (
    DELIVERY_CHANNEL_CLI,
    DELIVERY_CHANNEL_DESKTOP,
    DELIVERY_CHANNEL_GUI,
    ClaimOutcome,
    ClaimResult,
    DeliveryCandidate,
    DeliveryClaim,
    DeliverySetting,
    DeliverySettingState,
    DeliveryStorageError,
    describe_remind_time,
)
from tests.zone_fixtures import SYNTHETIC_KEY

NOW = datetime(2027, 11, 2, 17, 45, tzinfo=timezone.utc)
AS_OF = "2027-11-02T17:45:00Z"
STAMP = "2027-11-01T08:00:00Z"
ENABLED = DeliverySetting(DeliverySettingState.ENABLED, STAMP)


def candidate(
    reminder_id: int,
    *,
    late_minutes: int = 0,
    text: str | None = None,
    revision: int = 1,
) -> DeliveryCandidate:
    """Return a valid candidate saved at +01:00, late by whole minutes."""
    instant = NOW - timedelta(minutes=late_minutes)
    local = instant + timedelta(hours=1)
    return DeliveryCandidate(
        reminder_id,
        text if text is not None else f"reminder number {reminder_id}",
        f"{local:%Y-%m-%dT%H:%M}:00+01:00[{SYNTHETIC_KEY}]",
        f"{instant:%Y-%m-%dT%H:%M}:00Z",
        revision,
    )


def claimed(item: DeliveryCandidate) -> ClaimResult:
    """Return the result of a claim that committed for this candidate."""
    return ClaimResult(
        ClaimOutcome.CLAIMED,
        DeliveryClaim(item.reminder_id, item.schedule_revision),
    )


class FakeStore:
    """A delivery store whose answers the test chooses, recording each call."""

    def __init__(
        self,
        candidates: list[object] | None = None,
        *,
        setting: DeliverySetting = ENABLED,
    ) -> None:
        self.setting = setting
        self.candidates = list(candidates or [])
        self.claims: dict[int, object] = {}
        self.failing_records: set[int] = set()
        self.unexpected_records: set[int] = set()
        self.calls: list[tuple[object, ...]] = []

    def reminder_delivery_setting(self) -> DeliverySetting:
        self.calls.append(("setting",))
        return self.setting

    def reminder_delivery_candidates(self, as_of: str, limit: int) -> list[object]:
        self.calls.append(("candidates", as_of, limit))
        return self.candidates[:limit]

    def claim_reminder_delivery(
        self,
        item: DeliveryCandidate,
        channel: str,
        claimed_at: str,
        as_of: str,
    ) -> object:
        self.calls.append(("claim", item.reminder_id, channel, claimed_at, as_of))
        answer = self.claims.get(item.reminder_id)
        if isinstance(answer, BaseException):
            raise answer
        return claimed(item) if answer is None else answer

    def record_reminder_delivery(self, claim: DeliveryClaim, outcome: str) -> bool:
        self.calls.append(
            ("record", claim.reminder_id, claim.schedule_revision, outcome)
        )
        if claim.reminder_id in self.failing_records:
            raise DeliveryStorageError("Unable to read or write reminder delivery.")
        if claim.reminder_id in self.unexpected_records:
            raise RuntimeError("secret errand")
        return True

    def claimed_ids(self) -> list[object]:
        return [call[1] for call in self.calls if call[0] == "claim"]


class NoticeEntryTests(unittest.TestCase):
    """Pin one reminder's notice lines: label, quoted text, and saved time."""

    def test_an_entry_names_the_id_the_quoted_text_and_the_saved_time(self) -> None:
        entry = notice_entry(candidate(3, text="call mom"), AS_OF)

        self.assertEqual(
            entry,
            'Reminder 3 is due: "call mom"\n'
            "Tuesday 2027-11-02 at 18:45, Maria/Synthetic (UTC+01:00), which is "
            "2027-11-02 17:45 UTC",
        )

    def test_it_is_overdue_only_after_more_than_fifteen_minutes(self) -> None:
        item = candidate(4, late_minutes=15)

        self.assertIn("Reminder 4 is due:", notice_entry(item, AS_OF))
        self.assertIn(
            "Reminder 4 is overdue:",
            notice_entry(item, "2027-11-02T17:45:01Z"),
        )
        self.assertIn(
            "Reminder 5 is overdue:",
            notice_entry(candidate(5, late_minutes=600), AS_OF),
        )

    def test_the_text_is_quoted_as_the_reminders_tool_quotes_it(self) -> None:
        text = 'say "hi" \\ now'

        entry = notice_entry(candidate(6, text=text), AS_OF)

        self.assertEqual(
            entry.splitlines()[0],
            'Reminder 6 is due: "say \\"hi\\" \\\\ now"',
        )
        self.assertEqual(
            entry.splitlines()[1],
            describe_remind_time(
                candidate(6).remind_time,
                candidate(6).remind_utc,
            ),
        )

    def test_anything_but_a_candidate_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            notice_entry(object(), AS_OF)  # type: ignore[arg-type]


class InstantTests(unittest.TestCase):
    """A check needs an aware instant, written in Maria's UTC form."""

    def test_an_aware_instant_is_written_in_utc_to_the_second(self) -> None:
        moment = datetime(
            2027, 11, 2, 18, 45, 30, 999999,
            tzinfo=timezone(timedelta(hours=1)),
        )

        self.assertEqual(instant_text(moment), "2027-11-02T17:45:30Z")

    def test_a_naive_instant_or_another_type_is_refused(self) -> None:
        for moment in (datetime(2027, 11, 2, 17, 45), "2027-11-02T17:45:00Z", None):
            with self.subTest(moment=repr(moment)):
                with self.assertRaises(ValueError):
                    instant_text(moment)  # type: ignore[arg-type]


class ClaimNoticeTests(unittest.TestCase):
    """Pin one check: the setting first, then candidates, rendering, claims."""

    def test_with_delivery_not_on_nothing_but_the_setting_is_read(self) -> None:
        for setting in (
            DeliverySetting(DeliverySettingState.NEVER_SET),
            DeliverySetting(DeliverySettingState.DISABLED, STAMP),
            DeliverySetting(DeliverySettingState.UNREADABLE),
        ):
            with self.subTest(state=setting.state):
                store = FakeStore([candidate(1)], setting=setting)

                batch = claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP)

                self.assertEqual(batch, ClaimBatch())
                self.assertEqual(store.calls, [("setting",)])

    def test_nothing_due_claims_nothing(self) -> None:
        store = FakeStore([])

        batch = claim_notice(store, NOW, DELIVERY_CHANNEL_CLI)

        self.assertEqual(batch, ClaimBatch())
        self.assertEqual(
            store.calls,
            [("setting",), ("candidates", AS_OF, CANDIDATE_READ_LIMIT)],
        )

    def test_one_due_reminder_is_claimed_and_titled_alone(self) -> None:
        store = FakeStore([candidate(1, text="call mom")])

        batch = claim_notice(store, NOW, DELIVERY_CHANNEL_GUI)

        notice = batch.notice
        self.assertIsNotNone(notice)
        self.assertEqual(notice.title, NOTICE_TITLE)
        self.assertEqual(
            notice.text,
            notice_entry(candidate(1, text="call mom"), AS_OF),
        )
        self.assertEqual(notice.claims, (DeliveryClaim(1, 1),))
        self.assertIs(notice.more_due, False)
        self.assertEqual((batch.stopped, batch.failure), (None, None))
        self.assertEqual(
            store.calls,
            [
                ("setting",),
                ("candidates", AS_OF, 6),
                ("claim", 1, DELIVERY_CHANNEL_GUI, AS_OF, AS_OF),
            ],
        )

    def test_at_most_five_are_claimed_oldest_first_and_more_is_said(self) -> None:
        items = [candidate(index, late_minutes=60 - index) for index in range(1, 8)]
        store = FakeStore(items)

        batch = claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP)

        self.assertEqual(store.claimed_ids(), [1, 2, 3, 4, 5])
        self.assertEqual(batch.notice.reminder_ids, (1, 2, 3, 4, 5))
        self.assertIs(batch.notice.more_due, True)
        self.assertEqual(batch.notice.title, NOTICE_TITLE_SEVERAL)
        self.assertEqual(
            batch.notice.text,
            "\n\n".join(
                [*(notice_entry(item, AS_OF) for item in items[:5]), MORE_DUE_LINE]
            ),
        )

    def test_exactly_five_due_says_nothing_more(self) -> None:
        store = FakeStore([candidate(index) for index in range(1, 6)])

        batch = claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP)

        self.assertEqual(batch.notice.reminder_ids, (1, 2, 3, 4, 5))
        self.assertIs(batch.notice.more_due, False)
        self.assertNotIn(MORE_DUE_LINE, batch.notice.text)

    def test_every_entry_is_rendered_before_any_claim(self) -> None:
        store = FakeStore([candidate(1), candidate(2), object()])

        with self.assertRaises(ValueError):
            claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP)

        self.assertEqual(store.claimed_ids(), [])

    def test_a_changed_candidate_is_skipped_and_more_is_said(self) -> None:
        # Skipped as changed, it was due and is left unclaimed (R1, step 10),
        # wherever it stands in the batch.
        for position in range(1, 6):
            with self.subTest(position=position):
                store = FakeStore([candidate(index) for index in range(1, 6)])
                store.claims[position] = ClaimResult(ClaimOutcome.CHANGED)

                batch = claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP)

                self.assertEqual(store.claimed_ids(), [1, 2, 3, 4, 5])
                self.assertEqual(
                    batch.notice.reminder_ids,
                    tuple(index for index in range(1, 6) if index != position),
                )
                self.assertIs(batch.notice.more_due, True)
                self.assertTrue(batch.notice.text.endswith(f"\n\n{MORE_DUE_LINE}"))
                self.assertEqual((batch.stopped, batch.failure), (None, None))

    def test_every_claim_changed_leaves_no_notice(self) -> None:
        store = FakeStore([candidate(1), candidate(2)])
        store.claims[1] = ClaimResult(ClaimOutcome.CHANGED)
        store.claims[2] = ClaimResult(ClaimOutcome.CHANGED)

        self.assertEqual(
            claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP),
            ClaimBatch(),
        )

    def test_an_unknown_channel_or_naive_instant_is_refused_before_reading(
        self,
    ) -> None:
        store = FakeStore([candidate(1)])

        with self.assertRaises(ValueError):
            claim_notice(store, NOW, "email")
        with self.assertRaises(ValueError):
            claim_notice(store, datetime(2027, 11, 2, 17, 45), DELIVERY_CHANNEL_CLI)

        self.assertEqual(store.calls, [])

    def test_the_log_names_ids_and_never_the_text(self) -> None:
        store = FakeStore([candidate(1, text="secret errand"), candidate(2)])
        store.claims[2] = RuntimeError("secret errand")

        with self.assertLogs("maria.reminder_delivery", "INFO") as logs:
            claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP)

        self.assertEqual(
            logs.output,
            [
                "ERROR:maria.reminder_delivery:Stopped claiming reminders after "
                "a RuntimeError.",
                "INFO:maria.reminder_delivery:Claimed 1 reminder(s) for one "
                "desktop notice: 1.",
            ],
        )


class PartialBatchTests(unittest.TestCase):
    """Pin decision D13-A: a stop keeps exactly the claims made before it."""

    STOPS = {
        "busy": (
            DeliveryStorageError("The database is busy."),
            ClaimStop.STORAGE,
            "DeliveryStorageError",
        ),
        "storage": (
            DeliveryStorageError("Unable to read or write reminder delivery."),
            ClaimStop.STORAGE,
            "DeliveryStorageError",
        ),
        "unexpected": (RuntimeError("boom"), ClaimStop.UNEXPECTED, "RuntimeError"),
        "disabled": (ClaimResult(ClaimOutcome.DISABLED), ClaimStop.DISABLED, None),
        "malformed": ("CLAIMED", ClaimStop.UNEXPECTED, None),
    }

    def test_a_stop_at_each_claim_keeps_exactly_the_claims_before_it(self) -> None:
        for label, (answer, stop, failure) in self.STOPS.items():
            for position in range(1, 6):
                with self.subTest(stop=label, position=position):
                    store = FakeStore([candidate(index) for index in range(1, 6)])
                    store.claims[position] = answer

                    # Every stop is logged, by exception type alone.
                    with self.assertLogs("maria.reminder_delivery", "INFO"):
                        batch = claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP)

                    self.assertEqual(
                        store.claimed_ids(),
                        list(range(1, position + 1)),
                    )
                    self.assertEqual((batch.stopped, batch.failure), (stop, failure))
                    if position == 1:
                        self.assertIsNone(batch.notice)
                        continue
                    self.assertEqual(
                        batch.notice.reminder_ids,
                        tuple(range(1, position)),
                    )
                    self.assertIs(batch.notice.more_due, True)
                    self.assertTrue(batch.notice.text.endswith(MORE_DUE_LINE))

    def test_a_base_exception_from_a_claim_is_not_caught(self) -> None:
        store = FakeStore([candidate(1), candidate(2)])
        store.claims[2] = KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            claim_notice(store, NOW, DELIVERY_CHANNEL_CLI)

    def test_a_failing_setting_or_candidate_read_reaches_the_caller(self) -> None:
        store = FakeStore([candidate(1)])

        def failing(*_arguments: object) -> object:
            raise DeliveryStorageError("Unable to read or write reminder delivery.")

        store.reminder_delivery_candidates = failing  # type: ignore[method-assign]
        with self.assertRaises(DeliveryStorageError):
            claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP)
        self.assertEqual(store.claimed_ids(), [])

        store.reminder_delivery_setting = failing  # type: ignore[method-assign]
        with self.assertRaises(DeliveryStorageError):
            claim_notice(store, NOW, DELIVERY_CHANNEL_DESKTOP)


class RecordNoticeTests(unittest.TestCase):
    """Each claim's outcome is its own write, and one failure stops nothing."""

    def notice(self, *identifiers: int) -> ReminderNotice:
        items = [candidate(identifier) for identifier in identifiers]
        return ReminderNotice(
            tuple(notice_entry(item, AS_OF) for item in items),
            False,
            tuple(DeliveryClaim(item.reminder_id, 1) for item in items),
        )

    def test_displayed_and_not_displayed_are_written_for_every_claim(self) -> None:
        for displayed, outcome in ((True, "displayed"), (False, "failed")):
            with self.subTest(displayed=displayed):
                store = FakeStore()

                failures = record_notice(store, self.notice(1, 2, 3), displayed)

                self.assertEqual(failures, RecordFailures())
                self.assertEqual(
                    store.calls,
                    [("record", identifier, 1, outcome) for identifier in (1, 2, 3)],
                )

    def test_a_failing_write_is_counted_and_the_others_still_run(self) -> None:
        store = FakeStore()
        store.failing_records = {2}

        with self.assertLogs("maria.reminder_delivery", "ERROR") as logs:
            failures = record_notice(store, self.notice(1, 2, 3), True)

        self.assertEqual(failures, RecordFailures(storage=1))
        self.assertEqual(
            [call[1] for call in store.calls if call[0] == "record"],
            [1, 2, 3],
        )
        self.assertEqual(
            logs.output,
            [
                "ERROR:maria.reminder_delivery:Could not record the delivery "
                "of reminder 2: DeliveryStorageError.",
            ],
        )

    def test_storage_and_other_write_failures_are_counted_apart(self) -> None:
        store = FakeStore()
        store.failing_records = {1}
        store.unexpected_records = {2, 3}

        with self.assertLogs("maria.reminder_delivery", "ERROR") as logs:
            failures = record_notice(store, self.notice(1, 2, 3, 4), False)

        self.assertEqual(failures, RecordFailures(storage=1, unexpected=2))
        self.assertEqual(
            [call[1] for call in store.calls if call[0] == "record"],
            [1, 2, 3, 4],
        )
        self.assertEqual(
            [line.rsplit(": ", 1)[1] for line in logs.output],
            ["DeliveryStorageError.", "RuntimeError.", "RuntimeError."],
        )
        self.assertNotIn("secret errand", "\n".join(logs.output))

    def test_anything_but_a_notice_and_a_bool_is_refused(self) -> None:
        store = FakeStore()

        with self.assertRaises(ValueError):
            record_notice(store, object(), True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            record_notice(store, self.notice(1), 1)  # type: ignore[arg-type]
        self.assertEqual(store.calls, [])


class ReminderNoticeTests(unittest.TestCase):
    """A notice pairs each entry with one claim and never shows its text."""

    def test_its_repr_shows_neither_text_nor_claims(self) -> None:
        notice = ReminderNotice(("secret errand",), True, (DeliveryClaim(7, 2),))

        self.assertEqual(repr(notice), "ReminderNotice(<redacted>)")
        self.assertEqual(notice.reminder_ids, (7,))
        self.assertEqual(notice.text, f"secret errand\n\n{MORE_DUE_LINE}")

    def test_an_unpaired_or_empty_notice_is_refused(self) -> None:
        for entries, more_due, claims in (
            ((), False, ()),
            (("a", "b"), False, (DeliveryClaim(1, 1),)),
            (("a",), False, ((1, 1),)),
            (("a",), 0, (DeliveryClaim(1, 1),)),
            (["a"], False, (DeliveryClaim(1, 1),)),
        ):
            with self.subTest(entries=entries, claims=claims):
                with self.assertRaises(ValueError):
                    ReminderNotice(entries, more_due, claims)  # type: ignore[arg-type]


class SeamTests(unittest.TestCase):
    """The session's seam reads its clock once per check and claims for one surface."""

    def test_each_check_reads_the_clock_once_and_claims_for_its_channel(self) -> None:
        store = FakeStore([candidate(1)])
        reads: list[datetime] = []

        def clock() -> datetime:
            reads.append(NOW)
            return NOW

        seam = ReminderDeliverySeam(store, DELIVERY_CHANNEL_CLI, clock)

        batch = seam.claim()
        failures = seam.record(batch.notice, True)

        self.assertEqual(len(reads), 1)
        self.assertEqual(seam.channel, DELIVERY_CHANNEL_CLI)
        self.assertEqual(failures, RecordFailures())
        self.assertIn(("claim", 1, DELIVERY_CHANNEL_CLI, AS_OF, AS_OF), store.calls)
        self.assertIn(("record", 1, 1, "displayed"), store.calls)
        self.assertEqual(repr(seam), "ReminderDeliverySeam(channel='cli')")

    def test_an_unknown_channel_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            ReminderDeliverySeam(FakeStore(), "desktop window", lambda: NOW)


if __name__ == "__main__":
    unittest.main()
