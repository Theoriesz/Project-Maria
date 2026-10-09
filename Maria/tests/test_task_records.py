"""Focused tests for V1-22's storage vocabulary, ``core/task_records.py``.

Every limit, canonical form, value object, and fixed message the task and
reminder layers share is pinned here, together with the agreements other
modules keep with it: the request forms' text rule, the contracts' closed
text formats, the planner's literal values, the compiler's limits, and the
timing leaf's canonical output, which must always be a value this module
accepts. Reminder delivery's vocabulary (V1-23-R1) is pinned here as well.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import unittest
from datetime import datetime, timedelta, timezone

from core import action_contracts, planner, plan_compiler, task_records, task_requests
from core.action_contracts import TextFormat, matches_text_format
from core.context import CONTEXT_TIMING_MAX_CHARACTERS, reminder_digest, task_digest
from core.task_records import (
    CONTEXT_DIGEST_LENGTH,
    DELIVERY_CHANNEL_MAX_LENGTH,
    DELIVERY_CHANNELS,
    DELIVERY_OUTCOME_MAX_LENGTH,
    DELIVERY_OUTCOMES,
    DELIVERY_RECORD_MESSAGE,
    DELIVERY_SETTING_MESSAGE,
    DUE_DATE_LENGTH,
    NO_DUE_DATE,
    RECORD_STATUSES,
    REMIND_TIME_MAX_LENGTH,
    REMIND_TIME_MESSAGE,
    REPEAT_NEVER,
    RULES_CHANGED_WARNING,
    RULES_UNCHECKED_NOTE,
    SCHEDULE_REVISION_FIRST,
    SCHEDULE_REVISION_MAX,
    STATUS_COMPLETED,
    STATUS_OPEN,
    TASK_LIST_LIMIT,
    TASK_MAXIMUM_ID,
    TASK_QUERY_LIMIT,
    TASK_RECORD_MESSAGE,
    TASK_TEXT_MAX_BYTES,
    TASK_TEXT_MESSAGE,
    DUE_DATE_MESSAGE,
    UTC_INSTANT_LENGTH,
    ClaimOutcome,
    ClaimResult,
    DeliveryCandidate,
    DeliveryClaim,
    DeliveryCounts,
    DeliverySetting,
    DeliverySettingState,
    RecordUpdate,
    Reminder,
    ReminderDeliveryState,
    StatusChange,
    Task,
    ZonedTime,
    describe_due_date,
    describe_remind_time,
    describe_utc_instant,
    is_context_digest,
    is_delivery_channel,
    is_delivery_outcome,
    is_due_date,
    is_record_id,
    is_remind_time,
    is_schedule_revision,
    is_status,
    is_task_text,
    is_task_timestamp,
    is_utc_instant,
    parse_remind_time,
    settle_delivery_setting,
    settle_delivery_state,
    task_timestamp,
    timing_agrees,
)
from core.time_resolution import (
    ZONE_KEY_MAX_LENGTH,
    TimingContext,
    iso_date_text,
    is_zone_key,
    parse_reminder_phrase,
    parse_task_phrase,
    reference_instant_text,
    resolve_reminder_time,
    resolve_task_date,
    utc_instant_text,
)
from tests.zone_fixtures import (
    CENTRAL_RULES,
    HALF_HOUR_RULES,
    SYNTHETIC_KEY,
    SyntheticZones,
)


TWO_BYTE = chr(0x00E9)
FOUR_BYTE = chr(0x1F600)
LINE_SEPARATOR = chr(0x2028)
ZERO_WIDTH_SPACE = chr(0x200B)
LONE_SURROGATE = chr(0xD800)
FULLWIDTH_TWO = chr(0xFF12)

STAMP = "2026-10-04T05:59:35Z"
LATER_STAMP = "2026-10-04T06:00:00Z"
BERLIN_TIME = "2026-10-05T09:00:00+02:00[Europe/Berlin]"
BERLIN_INSTANT = "2026-10-05T07:00:00Z"

# Values both sides of an agreement are asked about: valid ones and near
# misses of every kind.
TEXT_CORPUS = (
    "a",
    "buy milk",
    "buy  milk",
    "a" * TASK_TEXT_MAX_BYTES,
    "a" * (TASK_TEXT_MAX_BYTES + 1),
    TWO_BYTE * (TASK_TEXT_MAX_BYTES // 2),
    TWO_BYTE * (TASK_TEXT_MAX_BYTES // 2 + 1),
    FOUR_BYTE * (TASK_TEXT_MAX_BYTES // 4),
    FOUR_BYTE * (TASK_TEXT_MAX_BYTES // 4 + 1),
    "",
    " a",
    "a ",
    "a\tb",
    "a\nb",
    "a\x00b",
    f"a{LINE_SEPARATOR}b",
    f"a{ZERO_WIDTH_SPACE}b",
    f"a{LONE_SURROGATE}b",
    None,
    b"a",
    7,
)
DATE_CORPUS = (
    "2026-10-09",
    "2028-02-29",
    "2026-02-29",
    "2026-13-01",
    "2026-00-10",
    "2026-1-9",
    "2026-10-09 ",
    "2026/10/09",
    f"2026-10-0{FULLWIDTH_TWO}",
    "none",
    "",
)
INSTANT_CORPUS = (
    STAMP,
    "2026-12-31T23:59:59Z",
    "2026-10-04T24:00:00Z",
    "2026-10-04T05:60:00Z",
    "2026-10-04T05:59:35",
    "2026-10-04T05:59:35z",
    "2026-10-04 05:59:35Z",
    "2026-10-04T05:59:35+00:00",
    "2026-02-30T00:00:00Z",
    "",
)
REMIND_TIME_CORPUS = (
    BERLIN_TIME,
    "2027-10-31T02:30:00+01:00[Maria/Synthetic]",
    "2027-01-01T00:00:00+00:00[Etc/UTC]",
    "2027-01-01T09:00:00-03:30[America/St_Johns]",
    "2027-01-01T09:00:00+05:45[Asia/Kathmandu]",
    "2027-01-01T09:00:00+23:59[Etc/Example]",
    "2027-01-01T09:00:00-12:00[Etc/GMT+12]",
    "2027-01-01T09:00:00-05:00[America/Port-au-Prince]",
    "2027-01-01T09:00:00+01:00[" + "A" * 64 + "]",
    "2027-01-01T09:00:00+01:00[" + "A" * 65 + "]",
    "2027-01-01T09:00:30+01:00[Etc/Example]",
    "2027-01-01T09:00:00-00:00[Etc/Example]",
    "2027-01-01T09:00:00+24:00[Etc/Example]",
    "2027-01-01T09:00:00+01:60[Etc/Example]",
    "2027-01-01T24:00:00+01:00[Etc/Example]",
    "2027-01-01T09:60:00+01:00[Etc/Example]",
    "2027-02-30T09:00:00+01:00[Etc/Example]",
    "2027-01-01T09:00:00+01:00",
    "2027-01-01T09:00:00+01:00[]",
    "2027-01-01T09:00:00+01:00[1Zone]",
    "2027-01-01T09:00:00+01:00[../etc/passwd]",
    "2027-01-01T09:00:00+01:00[/etc/zone]",
    "2027-01-01T09:00:00+01:00[Area/]",
    "2027-01-01T09:00:00+01:00[Area City]",
    "2027-01-01t09:00:00+01:00[Etc/Example]",
    "2027-01-01T09:00:00+0100[Etc/Example]",
    "2027-01-01T09:00:00Z[Etc/Example]",
    "2027-01-01T09:00:00+01:00[Etc/Example] ",
    "2027-01-01T9:00:00+01:00[Etc/Example]",
    "",
)
ZONE_KEY_CORPUS = (
    "Europe/Berlin",
    "America/Argentina/Buenos_Aires",
    "Etc/GMT+5",
    "Etc/GMT-14",
    "America/Port-au-Prince",
    "UTC",
    "Maria/Synthetic",
    "A" * ZONE_KEY_MAX_LENGTH,
    "A" * (ZONE_KEY_MAX_LENGTH + 1),
    "",
    "1Zone",
    "Europe/",
    "/Europe",
    "Europe//Berlin",
    "../Berlin",
    "Europe/../Berlin",
    "Europe/Berlin ",
    "Europe\\Berlin",
    "Europe/_Berlin",
)


def _task(**changes: object) -> Task:
    fields: dict[str, object] = {
        "task_id": 1,
        "text": "buy milk",
        "due_date": "2026-10-09",
        "status": STATUS_OPEN,
        "created_at": STAMP,
        "updated_at": STAMP,
        "completed_at": None,
        "context_enabled": False,
    }
    fields.update(changes)
    return Task(**fields)


def _reminder(**changes: object) -> Reminder:
    fields: dict[str, object] = {
        "reminder_id": 1,
        "text": "call mom",
        "remind_time": BERLIN_TIME,
        "remind_utc": BERLIN_INSTANT,
        "status": STATUS_OPEN,
        "created_at": STAMP,
        "updated_at": STAMP,
        "completed_at": None,
        "context_enabled": False,
    }
    fields.update(changes)
    return Reminder(**fields)


class ConstantTests(unittest.TestCase):
    """Pin the shared limits, values, and owner-visible sentences."""

    def test_limits_and_closed_values(self) -> None:
        self.assertEqual(TASK_TEXT_MAX_BYTES, 512)
        self.assertEqual(RECORD_STATUSES, ("open", "completed"))
        self.assertEqual((STATUS_OPEN, STATUS_COMPLETED), RECORD_STATUSES)
        self.assertEqual(NO_DUE_DATE, "none")
        self.assertEqual(REPEAT_NEVER, "never")
        self.assertEqual((TASK_LIST_LIMIT, TASK_QUERY_LIMIT), (20, 21))
        self.assertEqual(TASK_MAXIMUM_ID, 9223372036854775807)

    def test_owner_visible_sentences(self) -> None:
        self.assertEqual(
            TASK_TEXT_MESSAGE,
            "Task and reminder text must be one line of 1 to 512 UTF-8 bytes, "
            "with no leading or trailing whitespace and no control or "
            "formatting characters.",
        )
        self.assertEqual(
            RULES_CHANGED_WARNING,
            "Time zone rules for this reminder's zone have changed since it was "
            "set. It stays at its saved instant; reschedule it to use the "
            "current rules.",
        )
        self.assertEqual(
            RULES_UNCHECKED_NOTE,
            "Maria cannot check this time against current time zone rules.",
        )

    def test_every_fixed_message_is_fixed_text(self) -> None:
        for name in dir(task_records):
            value = getattr(task_records, name)
            if name.endswith(("_MESSAGE", "_WARNING", "_NOTE")):
                with self.subTest(name=name):
                    self.assertIsInstance(value, str)
                    self.assertNotIn("{", value)
                    self.assertNotIn("%", value)


class TextTests(unittest.TestCase):
    """Prove the stored-text rule: one visible line of 1 to 512 bytes."""

    def test_accepted_and_refused_text(self) -> None:
        accepted = {
            "a",
            "buy milk",
            "buy  milk",
            "a" * TASK_TEXT_MAX_BYTES,
            TWO_BYTE * (TASK_TEXT_MAX_BYTES // 2),
            FOUR_BYTE * (TASK_TEXT_MAX_BYTES // 4),
        }
        for value in TEXT_CORPUS:
            with self.subTest(value=repr(value)[:40]):
                self.assertEqual(is_task_text(value), value in accepted)


class IdentifierAndStatusTests(unittest.TestCase):
    """Prove ids are exact positive ints and statuses one of two."""

    def test_record_ids(self) -> None:
        for value in (1, 2, TASK_MAXIMUM_ID):
            with self.subTest(value=value):
                self.assertTrue(is_record_id(value))
        for value in (0, -1, True, False, 1.0, "1", None):
            with self.subTest(value=value):
                self.assertFalse(is_record_id(value))

    def test_statuses(self) -> None:
        self.assertTrue(is_status("open"))
        self.assertTrue(is_status("completed"))
        for value in ("Open", "done", "", None, 1):
            with self.subTest(value=value):
                self.assertFalse(is_status(value))


class DateAndInstantTests(unittest.TestCase):
    """Prove calendar dates and UTC instants are exact and real."""

    def test_due_dates(self) -> None:
        for value in DATE_CORPUS:
            with self.subTest(value=value):
                self.assertEqual(
                    is_due_date(value),
                    value in {"2026-10-09", "2028-02-29"},
                )
        self.assertFalse(is_due_date(None))

    def test_utc_instants(self) -> None:
        for value in INSTANT_CORPUS:
            with self.subTest(value=value):
                self.assertEqual(
                    is_utc_instant(value),
                    value in {STAMP, "2026-12-31T23:59:59Z"},
                )
        self.assertFalse(is_utc_instant(None))
        self.assertEqual(is_task_timestamp(STAMP), True)

    def test_a_task_timestamp_is_the_current_utc_second(self) -> None:
        before = datetime.now(timezone.utc).replace(microsecond=0)
        stamp = task_timestamp()
        after = datetime.now(timezone.utc)
        self.assertTrue(is_task_timestamp(stamp))
        moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        self.assertLessEqual(before, moment)
        self.assertLessEqual(moment, after)


class RemindTimeTests(unittest.TestCase):
    """Prove the canonical reminder time and its agreement with the instant."""

    def test_canonical_times_parse_into_their_parts(self) -> None:
        parts = parse_remind_time("2027-10-31T02:30:00+01:00[Maria/Synthetic]")
        self.assertIsInstance(parts, ZonedTime)
        self.assertEqual(parts.local, datetime(2027, 10, 31, 2, 30))
        self.assertEqual(parts.offset, timedelta(hours=1))
        self.assertEqual(parts.key, "Maria/Synthetic")
        self.assertEqual(parts.instant_text, "2027-10-31T01:30:00Z")
        self.assertEqual(repr(parts), "ZonedTime(<redacted>)")

        parts = parse_remind_time("2027-01-01T09:00:00-03:30[America/St_Johns]")
        self.assertEqual(parts.offset, -timedelta(hours=3, minutes=30))
        self.assertEqual(parts.instant_text, "2027-01-01T12:30:00Z")

    def test_only_canonical_times_are_accepted(self) -> None:
        accepted = set(REMIND_TIME_CORPUS[:9])
        for value in REMIND_TIME_CORPUS:
            with self.subTest(value=value):
                self.assertEqual(is_remind_time(value), value in accepted)
        self.assertFalse(is_remind_time(None))

    def test_an_instant_outside_the_representable_range_is_refused(self) -> None:
        for value in (
            "0001-01-01T00:00:00+01:00[Etc/Example]",
            "9999-12-31T23:59:00-01:00[Etc/Example]",
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_remind_time(value))

    def test_the_saved_instant_must_be_exactly_the_encoded_one(self) -> None:
        self.assertTrue(timing_agrees(BERLIN_TIME, BERLIN_INSTANT))
        # Individually valid, but an hour apart: the audit's example.
        self.assertFalse(timing_agrees(BERLIN_TIME, "2026-10-05T08:00:00Z"))
        self.assertFalse(timing_agrees(BERLIN_TIME, "2026-10-05T07:00:01Z"))
        self.assertFalse(timing_agrees(BERLIN_TIME, "2026-10-05T07:00:00"))
        self.assertFalse(timing_agrees("2026-10-05T09:00:00", BERLIN_INSTANT))
        self.assertFalse(timing_agrees(None, None))

    def test_agreement_takes_no_time_zone_data(self) -> None:
        self.assertTrue(
            timing_agrees(
                "2026-10-05T09:00:00+02:00[Maria/NoSuchZone]",
                BERLIN_INSTANT,
            )
        )


class DescriptionTests(unittest.TestCase):
    """Prove the owner-visible descriptions show exactly the saved values."""

    def test_due_dates_are_described_with_their_weekday(self) -> None:
        self.assertEqual(describe_due_date("2026-10-09"), "due Friday 2026-10-09")
        self.assertEqual(describe_due_date(None), "no due date")
        with self.assertRaises(ValueError) as raised:
            describe_due_date("2026-02-30")
        self.assertEqual(str(raised.exception), DUE_DATE_MESSAGE)

    def test_reminder_times_show_local_time_zone_offset_and_instant(self) -> None:
        self.assertEqual(
            describe_remind_time(BERLIN_TIME, BERLIN_INSTANT),
            "Monday 2026-10-05 at 09:00, Europe/Berlin (UTC+02:00), which is "
            "2026-10-05 07:00 UTC",
        )
        self.assertEqual(
            describe_remind_time(
                "2027-01-01T09:00:00-03:30[America/St_Johns]",
                "2027-01-01T12:30:00Z",
            ),
            "Friday 2027-01-01 at 09:00, America/St_Johns (UTC-03:30), which is "
            "2027-01-01 12:30 UTC",
        )
        self.assertEqual(
            describe_remind_time(
                "2027-01-01T00:00:00+00:00[Etc/UTC]",
                "2027-01-01T00:00:00Z",
            ),
            "Friday 2027-01-01 at 00:00, Etc/UTC (UTC+00:00), which is "
            "2027-01-01 00:00 UTC",
        )

    def test_a_description_needs_agreeing_values_and_no_zone_data(self) -> None:
        self.assertIn(
            "Maria/NoSuchZone",
            describe_remind_time(
                "2026-10-05T09:00:00+02:00[Maria/NoSuchZone]",
                BERLIN_INSTANT,
            ),
        )
        with self.assertRaises(ValueError) as raised:
            describe_remind_time(BERLIN_TIME, "2026-10-05T08:00:00Z")
        self.assertEqual(str(raised.exception), REMIND_TIME_MESSAGE)


class ValueObjectTests(unittest.TestCase):
    """Prove a value object holds only what Maria could have written."""

    def test_a_valid_task_and_reminder(self) -> None:
        task = _task()
        self.assertEqual(task.due_date, "2026-10-09")
        self.assertIsNone(_task(due_date=None).due_date)
        completed = _task(status=STATUS_COMPLETED, completed_at=LATER_STAMP)
        self.assertEqual(completed.completed_at, LATER_STAMP)
        reminder = _reminder(context_enabled=True)
        self.assertTrue(reminder.context_enabled)

    def test_invalid_tasks_are_refused(self) -> None:
        for changes in (
            {"task_id": True},
            {"task_id": 0},
            {"text": " buy milk"},
            {"text": "a" * (TASK_TEXT_MAX_BYTES + 1)},
            {"due_date": "2026-02-30"},
            {"due_date": "none"},
            {"status": "done"},
            {"created_at": "2026-10-04 05:59:35"},
            {"updated_at": None},
            {"completed_at": LATER_STAMP},
            {"status": STATUS_COMPLETED},
            {"status": STATUS_COMPLETED, "completed_at": "yesterday"},
            {"context_enabled": 1},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError) as raised:
                    _task(**changes)
                self.assertEqual(str(raised.exception), TASK_RECORD_MESSAGE)

    def test_invalid_reminders_are_refused(self) -> None:
        for changes in (
            {"reminder_id": -3},
            {"text": ""},
            {"remind_utc": "2026-10-05T08:00:00Z"},
            {"remind_time": "2026-10-05T09:00:00+02:00"},
            {"status": STATUS_COMPLETED},
            {"completed_at": STAMP},
            {"context_enabled": None},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError) as raised:
                    _reminder(**changes)
                self.assertEqual(str(raised.exception), TASK_RECORD_MESSAGE)

    def test_reprs_name_only_the_type_and_id(self) -> None:
        self.assertEqual(repr(_task()), "Task(id=1, <redacted>)")
        self.assertEqual(repr(_reminder()), "Reminder(id=1, <redacted>)")
        for shown in (repr(_task()), str(_task()), repr(_reminder())):
            for secret in ("milk", "mom", "2026", "Berlin"):
                self.assertNotIn(secret, shown)

    def test_outcome_values(self) -> None:
        self.assertEqual(
            [change.value for change in StatusChange],
            ["changed", "unchanged", "not_found"],
        )
        self.assertEqual(RecordUpdate(True), RecordUpdate(True, False))

    def test_context_digests(self) -> None:
        self.assertTrue(is_context_digest("a" * 64))
        for value in ("A" * 64, "a" * 63, "g" * 64, None, 64):
            with self.subTest(value=value):
                self.assertFalse(is_context_digest(value))


class DigestTests(unittest.TestCase):
    """Pin the consent identities: compact JSON of text and timing."""

    def test_task_and_reminder_identities(self) -> None:
        def expected(values: list[str]) -> str:
            text = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
            return hashlib.sha256(text.encode("utf-8")).hexdigest()

        text = f"caf{TWO_BYTE} \"run\""
        self.assertEqual(
            task_digest(text, "2026-10-09"),
            expected([text, "2026-10-09"]),
        )
        self.assertEqual(task_digest(text, None), expected([text, ""]))
        self.assertEqual(
            reminder_digest(text, BERLIN_TIME),
            expected([text, BERLIN_TIME]),
        )
        self.assertNotEqual(task_digest(text, None), task_digest(text, "2026-10-09"))
        # The saved zone is part of the identity, so the same wall time in
        # another zone is a changed identity.
        self.assertNotEqual(
            reminder_digest(text, BERLIN_TIME),
            reminder_digest(text, "2026-10-05T09:00:00+02:00[Europe/Paris]"),
        )


class AgreementTests(unittest.TestCase):
    """Prove every module that repeats a rule repeats exactly this one."""

    def test_the_request_forms_use_the_stored_text_rule(self) -> None:
        for value in TEXT_CORPUS:
            with self.subTest(value=repr(value)[:40]):
                self.assertEqual(
                    task_requests.is_creation_text(value),
                    is_task_text(value),
                )
        self.assertEqual(task_requests.CREATION_TEXT_MESSAGE, TASK_TEXT_MESSAGE)
        self.assertEqual(task_requests.CREATION_BODY_MAX_BYTES, TASK_TEXT_MAX_BYTES)

    def test_limits_and_categories_agree(self) -> None:
        self.assertEqual(action_contracts.MAXIMUM_TASK_TEXT_LENGTH, TASK_TEXT_MAX_BYTES)
        self.assertEqual(plan_compiler.PROPOSAL_TEXT_MAX_BYTES, TASK_TEXT_MAX_BYTES)
        self.assertEqual(
            task_requests.CREATION_REQUEST_MAX_ITEMS,
            planner.PLAN_MAX_STEPS,
        )
        self.assertEqual(
            task_records.UNSAFE_TEXT_CATEGORIES,
            task_requests.UNSAFE_TEXT_CATEGORIES,
        )
        self.assertEqual(
            task_records.UNSAFE_TEXT_CATEGORIES,
            plan_compiler.UNSAFE_TEXT_CATEGORIES,
        )

    def test_closed_values_agree(self) -> None:
        self.assertEqual(action_contracts.TASK_STATUSES, RECORD_STATUSES)
        self.assertEqual(action_contracts.REPEAT_VALUES, (REPEAT_NEVER,))
        self.assertEqual(action_contracts.NO_DUE_DATE, NO_DUE_DATE)
        self.assertEqual(
            (
                planner._STATUS_OPEN,
                planner._STATUS_COMPLETED,
                planner._NO_DUE_DATE,
                planner._REPEAT_NEVER,
            ),
            (STATUS_OPEN, STATUS_COMPLETED, NO_DUE_DATE, REPEAT_NEVER),
        )

    def test_contract_formats_accept_exactly_the_stored_shapes(self) -> None:
        for value in DATE_CORPUS:
            with self.subTest(format="iso_date", value=value):
                self.assertEqual(
                    matches_text_format(TextFormat.ISO_DATE, value),
                    is_due_date(value),
                )
            with self.subTest(format="iso_date_or_none", value=value):
                self.assertEqual(
                    matches_text_format(TextFormat.ISO_DATE_OR_NONE, value),
                    value == NO_DUE_DATE or is_due_date(value),
                )
        for value in INSTANT_CORPUS:
            with self.subTest(format="utc_instant", value=value):
                self.assertEqual(
                    matches_text_format(TextFormat.UTC_INSTANT, value),
                    is_utc_instant(value),
                )
        for value in REMIND_TIME_CORPUS:
            with self.subTest(format="zoned_local_time", value=value):
                self.assertEqual(
                    matches_text_format(TextFormat.ZONED_LOCAL_TIME, value),
                    is_remind_time(value),
                )

    def test_the_contract_checks_shape_and_the_record_also_its_instant(self) -> None:
        # The only difference, by design: a well-formed time whose instant
        # cannot be represented passes the contract's shape check and is
        # refused by the record layer, so it is never stored either way.
        value = "0001-01-01T00:00:00+01:00[Etc/Example]"
        self.assertTrue(matches_text_format(TextFormat.ZONED_LOCAL_TIME, value))
        self.assertFalse(is_remind_time(value))

    def test_zone_key_syntax_agrees(self) -> None:
        self.assertEqual(task_records._ZONE_KEY_MAX_LENGTH, ZONE_KEY_MAX_LENGTH)
        self.assertEqual(
            action_contracts._MAXIMUM_ZONE_KEY_LENGTH,
            ZONE_KEY_MAX_LENGTH,
        )
        for key in ZONE_KEY_CORPUS:
            value = f"2027-01-01T09:00:00+01:00[{key}]"
            with self.subTest(key=key):
                self.assertEqual(is_remind_time(value), is_zone_key(key))
                self.assertEqual(
                    matches_text_format(TextFormat.ZONED_LOCAL_TIME, value),
                    is_zone_key(key),
                )

    def test_the_longest_canonical_time_fits_the_context_bound(self) -> None:
        longest = "9999-12-30T23:59:00+23:59[" + "A" * ZONE_KEY_MAX_LENGTH + "]"
        self.assertTrue(is_remind_time(longest))
        self.assertLessEqual(len(longest), CONTEXT_TIMING_MAX_CHARACTERS)

    def test_each_canonical_form_fits_its_stored_bound_exactly(self) -> None:
        # The memory layer bounds every stored value by these before it can
        # reach Python, so each must admit the longest valid value and no
        # valid value may be longer. Each is ASCII, so characters are bytes.
        longest_time = (
            "9999-12-30T23:59:00+23:59[" + "A" * ZONE_KEY_MAX_LENGTH + "]"
        )
        too_long_key = (
            "9999-12-30T23:59:00+23:59[" + "A" * (ZONE_KEY_MAX_LENGTH + 1) + "]"
        )
        stamp = task_timestamp()
        digest = task_digest("x", None)
        for value, valid, bound in (
            (longest_time, is_remind_time(longest_time), REMIND_TIME_MAX_LENGTH),
            ("9999-12-31", is_due_date("9999-12-31"), DUE_DATE_LENGTH),
            (stamp, is_task_timestamp(stamp), UTC_INSTANT_LENGTH),
            (digest, is_context_digest(digest), CONTEXT_DIGEST_LENGTH),
        ):
            with self.subTest(value=value):
                self.assertTrue(valid)
                self.assertTrue(value.isascii())
                self.assertEqual(len(value), bound)
        self.assertFalse(is_remind_time(too_long_key))
        self.assertEqual(REMIND_TIME_MAX_LENGTH, 91)
        self.assertLessEqual(REMIND_TIME_MAX_LENGTH, CONTEXT_TIMING_MAX_CHARACTERS)


class CanonicalOutputTests(unittest.TestCase):
    """Prove every value the timing leaf writes is one this module stores."""

    def setUp(self) -> None:
        SyntheticZones(
            self,
            {SYNTHETIC_KEY: CENTRAL_RULES, "Maria/HalfHour": HALF_HOUR_RULES},
        )

    def test_resolved_reminder_times_are_canonical_and_agree(self) -> None:
        references = (
            datetime(2027, 10, 30, 8, 3, 27, tzinfo=timezone.utc),
            datetime(2027, 3, 27, 9, 0, tzinfo=timezone.utc),
            datetime(2027, 12, 31, 22, 59, 59, 900000, tzinfo=timezone.utc),
        )
        phrases = (
            "tomorrow at 9:00",
            "tomorrow at 02:30 +01:00",
            "tomorrow at 02:30 +02:00",
            "on sunday at 23:59",
            "in 3 days at 00:00",
            "in 20 minutes",
            "in 168 hours",
            "in 1440 minutes",
        )
        checked = 0
        for key in (SYNTHETIC_KEY, "Maria/HalfHour"):
            for reference in references:
                timing = TimingContext(reference, key)
                for phrase in phrases:
                    resolved = resolve_reminder_time(
                        parse_reminder_phrase(phrase),
                        timing,
                    )
                    if not hasattr(resolved, "canonical"):
                        continue
                    checked += 1
                    with self.subTest(key=key, reference=reference, phrase=phrase):
                        canonical = resolved.canonical
                        self.assertTrue(is_remind_time(canonical))
                        self.assertTrue(
                            matches_text_format(TextFormat.ZONED_LOCAL_TIME, canonical)
                        )
                        self.assertTrue(is_utc_instant(resolved.instant_text))
                        self.assertTrue(timing_agrees(canonical, resolved.instant_text))
                        self.assertEqual(
                            parse_remind_time(canonical).instant_text,
                            resolved.instant_text,
                        )
                    self.assertTrue(is_utc_instant(reference_instant_text(timing)))
        self.assertGreater(checked, 30)

    def test_resolved_task_dates_are_stored_dates(self) -> None:
        timing = TimingContext(
            datetime(2027, 10, 30, 22, 30, tzinfo=timezone.utc),
            SYNTHETIC_KEY,
        )
        for phrase in ("today", "tomorrow", "friday", "2027-12-31"):
            with self.subTest(phrase=phrase):
                resolved = resolve_task_date(parse_task_phrase(phrase), timing)
                text = iso_date_text(resolved)
                self.assertTrue(is_due_date(text))
                self.assertTrue(matches_text_format(TextFormat.ISO_DATE, text))
        self.assertEqual(
            utc_instant_text(
                datetime(2027, 1, 1, 9, 0, 59, 999999, tzinfo=timezone.utc)
            ),
            "2027-01-01T09:00:59Z",
        )


class DeliveryVocabularyTests(unittest.TestCase):
    """Pin reminder delivery's vocabulary: its values, rules, and reprs."""

    def test_channels_outcomes_and_revision_bounds(self) -> None:
        self.assertEqual(DELIVERY_CHANNELS, ("desktop", "cli", "gui"))
        self.assertEqual(DELIVERY_OUTCOMES, ("displayed", "failed"))
        self.assertEqual(
            (DELIVERY_CHANNEL_MAX_LENGTH, DELIVERY_OUTCOME_MAX_LENGTH),
            (7, 9),
        )
        self.assertEqual(
            (SCHEDULE_REVISION_FIRST, SCHEDULE_REVISION_MAX),
            (1, 2**63 - 1),
        )

    def test_only_exact_known_values_pass_the_checks(self) -> None:
        for channel in DELIVERY_CHANNELS:
            self.assertTrue(is_delivery_channel(channel))
        for outcome in DELIVERY_OUTCOMES:
            self.assertTrue(is_delivery_outcome(outcome))
        for revision in (1, 2, 2**63 - 1):
            self.assertTrue(is_schedule_revision(revision))
        for value in ("Desktop", "cli ", "window", "", None, b"cli", 1):
            with self.subTest(channel=value):
                self.assertFalse(is_delivery_channel(value))
        for value in ("Displayed", "unconfirmed", "", None, b"failed", 0):
            with self.subTest(outcome=value):
                self.assertFalse(is_delivery_outcome(value))
        for value in (0, -1, 2**63, True, 1.0, "1", None):
            with self.subTest(revision=value):
                self.assertFalse(is_schedule_revision(value))

    def test_a_delivery_state_holds_only_a_claim_maria_could_write(self) -> None:
        self.assertIsNone(ReminderDeliveryState(STAMP, "cli").outcome)
        self.assertEqual(
            ReminderDeliveryState(STAMP, "gui", "failed").outcome,
            "failed",
        )
        for fields in (
            ("2026-10-04 05:59:35", "cli", None),
            (None, "cli", "displayed"),
            (STAMP, "window", None),
            (STAMP, None, None),
            (STAMP, "cli", "unconfirmed"),
        ):
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError) as raised:
                    ReminderDeliveryState(*fields)
                self.assertEqual(str(raised.exception), DELIVERY_RECORD_MESSAGE)

    def test_stored_claim_columns_settle_to_a_state_or_none(self) -> None:
        self.assertIsNone(settle_delivery_state(None, None, None))
        self.assertEqual(
            settle_delivery_state(STAMP, "desktop", "displayed"),
            ReminderDeliveryState(STAMP, "desktop", "displayed"),
        )
        for columns in (
            (None, None, "displayed"),
            (None, "cli", None),
            (STAMP, None, None),
            (STAMP, "cli", 1),
        ):
            with self.subTest(columns=columns):
                with self.assertRaises(ValueError) as raised:
                    settle_delivery_state(*columns)
                self.assertEqual(str(raised.exception), DELIVERY_RECORD_MESSAGE)

    def test_a_reminder_carries_its_revision_and_at_most_its_claim(self) -> None:
        reminder = _reminder()
        self.assertEqual((reminder.schedule_revision, reminder.delivery), (1, None))
        claimed = _reminder(
            schedule_revision=3,
            delivery=ReminderDeliveryState(STAMP, "cli"),
        )
        self.assertEqual(claimed.delivery, ReminderDeliveryState(STAMP, "cli"))
        self.assertEqual(repr(claimed), "Reminder(id=1, <redacted>)")
        for changes in (
            {"schedule_revision": 0},
            {"schedule_revision": True},
            {"schedule_revision": 2**63},
            {"delivery": (STAMP, "cli", None)},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError) as raised:
                    _reminder(**changes)
                self.assertEqual(str(raised.exception), TASK_RECORD_MESSAGE)

    def test_a_candidate_is_validated_and_shows_only_its_id(self) -> None:
        candidate = DeliveryCandidate(4, "call mom", BERLIN_TIME, BERLIN_INSTANT, 2)
        self.assertEqual(repr(candidate), "DeliveryCandidate(id=4, <redacted>)")
        for shown in (repr(candidate), str(candidate)):
            for secret in ("mom", "2026", "Berlin"):
                self.assertNotIn(secret, shown)
        for fields in (
            (0, "call mom", BERLIN_TIME, BERLIN_INSTANT, 1),
            (4, " call mom", BERLIN_TIME, BERLIN_INSTANT, 1),
            (4, "call mom", BERLIN_TIME, "2026-10-05T08:00:00Z", 1),
            (4, "call mom", "2026-10-05T09:00:00+02:00", BERLIN_INSTANT, 1),
            (4, "call mom", BERLIN_TIME, BERLIN_INSTANT, 0),
            (4, "call mom", BERLIN_TIME, BERLIN_INSTANT, True),
        ):
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError) as raised:
                    DeliveryCandidate(*fields)
                self.assertEqual(str(raised.exception), TASK_RECORD_MESSAGE)

    def test_a_claim_result_holds_a_claim_exactly_when_claimed(self) -> None:
        claim = DeliveryClaim(4, 2)
        self.assertIs(ClaimResult(ClaimOutcome.CLAIMED, claim).claim, claim)
        self.assertIsNone(ClaimResult(ClaimOutcome.CHANGED).claim)
        self.assertIsNone(ClaimResult(ClaimOutcome.DISABLED).claim)
        self.assertEqual(
            [outcome.value for outcome in ClaimOutcome],
            ["claimed", "changed", "disabled"],
        )
        for outcome, given in (
            (ClaimOutcome.CLAIMED, None),
            (ClaimOutcome.CLAIMED, (4, 2)),
            (ClaimOutcome.CHANGED, claim),
            (ClaimOutcome.DISABLED, claim),
            ("claimed", claim),
        ):
            with self.subTest(outcome=outcome, claim=given):
                with self.assertRaises(ValueError) as raised:
                    ClaimResult(outcome, given)  # type: ignore[arg-type]
                self.assertEqual(str(raised.exception), DELIVERY_RECORD_MESSAGE)
        for identity in ((0, 1), (True, 1), (4, 0), (4, 2**63)):
            with self.subTest(identity=identity):
                with self.assertRaises(ValueError) as raised:
                    DeliveryClaim(*identity)
                self.assertEqual(str(raised.exception), DELIVERY_RECORD_MESSAGE)

    def test_a_setting_has_a_stamp_exactly_while_it_was_set(self) -> None:
        for state in (DeliverySettingState.ENABLED, DeliverySettingState.DISABLED):
            with self.subTest(state=state):
                self.assertEqual(
                    DeliverySetting(state, STAMP).enabled,
                    state is DeliverySettingState.ENABLED,
                )
                with self.assertRaises(ValueError) as raised:
                    DeliverySetting(state)
                self.assertEqual(str(raised.exception), DELIVERY_SETTING_MESSAGE)
        for state in (DeliverySettingState.NEVER_SET, DeliverySettingState.UNREADABLE):
            with self.subTest(state=state):
                self.assertIs(DeliverySetting(state).enabled, False)
                with self.assertRaises(ValueError):
                    DeliverySetting(state, STAMP)
        with self.assertRaises(ValueError):
            DeliverySetting("enabled", STAMP)  # type: ignore[arg-type]

    def test_a_setting_read_is_off_unless_one_well_formed_row(self) -> None:
        self.assertEqual(
            settle_delivery_setting(0, None, None),
            DeliverySetting(DeliverySettingState.NEVER_SET),
        )
        self.assertEqual(
            settle_delivery_setting(1, 1, STAMP),
            DeliverySetting(DeliverySettingState.ENABLED, STAMP),
        )
        self.assertEqual(
            settle_delivery_setting(1, 0, STAMP),
            DeliverySetting(DeliverySettingState.DISABLED, STAMP),
        )
        for row in (
            (2, 1, STAMP),
            (False, None, None),
            (None, None, None),
            (1, 2, STAMP),
            (1, True, STAMP),
            (1, "1", STAMP),
            (1, 1.0, STAMP),
            (1, 1, None),
            (1, 1, "2026-10-04 05:59:35"),
        ):
            with self.subTest(row=row):
                self.assertEqual(
                    settle_delivery_setting(*row),
                    DeliverySetting(DeliverySettingState.UNREADABLE),
                )

    def test_counts_are_whole_numbers_of_at_least_zero(self) -> None:
        self.assertEqual(DeliveryCounts(1, 0, 2), DeliveryCounts(1, 0, 2))
        for counts in ((-1, 0, 0), (0, True, 0), (0, 0, 1.0)):
            with self.subTest(counts=counts):
                with self.assertRaises(ValueError) as raised:
                    DeliveryCounts(*counts)
                self.assertEqual(str(raised.exception), DELIVERY_RECORD_MESSAGE)

    def test_a_utc_instant_is_described_to_the_minute(self) -> None:
        self.assertEqual(describe_utc_instant(BERLIN_INSTANT), "2026-10-05 07:00 UTC")
        for value in ("2026-10-05 07:00", "2026-10-05T07:00:00+00:00", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    describe_utc_instant(value)  # type: ignore[arg-type]


class ModuleBoundaryTests(unittest.TestCase):
    """Prove the vocabulary imports only the standard library and logs nothing."""

    ALLOWED = {"__future__", "re", "unicodedata", "dataclasses", "datetime", "enum"}

    def test_it_imports_only_its_allowed_standard_modules(self) -> None:
        imported = set()
        for node in ast.walk(ast.parse(inspect.getsource(task_records))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertLessEqual(imported, self.ALLOWED)


if __name__ == "__main__":
    unittest.main()
