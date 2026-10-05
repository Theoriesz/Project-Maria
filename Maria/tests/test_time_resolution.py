"""Focused tests for V1-22's timing leaf, ``core/time_resolution.py``.

The module reads no clock: every resolution here is given its reference
instant, so each expectation is exact. Daylight-saving and rule-change cases
use synthetic zones built in the test by ``tests.zone_fixtures``, which
restores zoneinfo's search path afterwards; the few cases that need real IANA
data skip, with a reason, when the interpreter has none.
"""

from __future__ import annotations

import ast
import inspect
import io
import unittest
import zoneinfo
from datetime import date, datetime, time, timedelta, timezone

from core import time_resolution
from core.constants import PROJECT_ROOT
from core.time_resolution import (
    MAXIMUM_DAYS,
    MAXIMUM_HOURS,
    MAXIMUM_MINUTES,
    DateKind,
    DateSpec,
    ReminderTiming,
    ResolvedTime,
    RuleCheck,
    TimingContext,
    TimingFailure,
    canonical_remind_time,
    check_rules,
    horizon,
    is_canonical_offset,
    is_zone_key,
    load_zone,
    local_today,
    offset_text,
    parse_reminder_command,
    parse_reminder_phrase,
    parse_task_command,
    parse_task_phrase,
    reference_instant_text,
    resolve_reminder_time,
    resolve_task_date,
    utc_instant_text,
    valid_offsets,
)
from tests.test_reasoning import PRODUCTION_SOURCES
from tests.zone_fixtures import (
    CENTRAL_RULES,
    FIXED_PLUS_THREE_RULES,
    HALF_HOUR_RULES,
    SECONDS_OFFSET_RULES,
    SYNTHETIC_KEY,
    SyntheticZones,
    real_zone,
    tzif_bytes,
)


FULLWIDTH_ONE = chr(0xFF11)
ARABIC_INDIC_ONE = chr(0x0661)

# Saturday 2027-10-30, 10:03:27 at +02:00 in the central rules: the night
# before the local hour from 02:00 repeats.
BEFORE_FALL_BACK = datetime(2027, 10, 30, 8, 3, 27, tzinfo=timezone.utc)
# Saturday 2027-03-27, 10:00 at +01:00: the night before the local hour from
# 02:00 is skipped.
BEFORE_SPRING_FORWARD = datetime(2027, 3, 27, 9, 0, tzinfo=timezone.utc)

PLUS_ONE = timedelta(hours=1)
PLUS_TWO = timedelta(hours=2)
PLUS_THREE = timedelta(hours=3)


def _timing(reference: datetime, key: str | None = SYNTHETIC_KEY) -> TimingContext:
    return TimingContext(reference, key)


def _phrase(text: str) -> ReminderTiming:
    spec = parse_reminder_phrase(text)
    assert spec is not None, text
    return spec


def _resolve(text: str, reference: datetime = BEFORE_FALL_BACK) -> object:
    return resolve_reminder_time(_phrase(text), _timing(reference))


class GrammarTests(unittest.TestCase):
    """Prove the closed timing grammar, its ranges, and its spellings."""

    def test_request_form_phrases_parse_to_their_meaning(self) -> None:
        cases = (
            ("today at 9:00", DateKind.TODAY, time(9, 0), None),
            ("tomorrow at 09:00", DateKind.TOMORROW, time(9, 0), None),
            ("on monday at 23:59", DateKind.WEEKDAY, time(23, 59), None),
            ("on 2027-10-31 at 02:30 +01:00", DateKind.DATE, time(2, 30), PLUS_ONE),
            ("in 1 day at 00:00", DateKind.DAYS_LATER, time(0, 0), None),
            (
                "in 3 days at 7:05 -05:00",
                DateKind.DAYS_LATER,
                time(7, 5),
                -timedelta(hours=5),
            ),
        )
        for text, kind, clock, offset in cases:
            with self.subTest(text=text):
                spec = parse_reminder_phrase(text)
                self.assertIsNotNone(spec)
                self.assertIs(spec.day.kind, kind)
                self.assertEqual(spec.clock, clock)
                self.assertEqual(spec.offset, offset)
                self.assertIsNone(spec.elapsed)

    def test_elapsed_phrases_parse_to_spans(self) -> None:
        cases = (
            ("in 1 minute", timedelta(minutes=1)),
            ("in 20 minutes", timedelta(minutes=20)),
            (f"in {MAXIMUM_MINUTES} minutes", timedelta(minutes=1440)),
            ("in 1 hour", timedelta(hours=1)),
            (f"in {MAXIMUM_HOURS} hours", timedelta(hours=168)),
        )
        for text, span in cases:
            with self.subTest(text=text):
                spec = parse_reminder_phrase(text)
                self.assertEqual(spec, ReminderTiming(elapsed=span))
                self.assertEqual(parse_reminder_command(text), spec)

    def test_keywords_match_ascii_case_insensitively(self) -> None:
        self.assertEqual(
            parse_reminder_phrase("TOMORROW At 9:00"),
            parse_reminder_phrase("tomorrow at 9:00"),
        )
        self.assertEqual(
            parse_reminder_phrase("On FRIDAY at 9:00").day,
            DateSpec(DateKind.WEEKDAY, weekday=4),
        )
        self.assertEqual(
            parse_task_phrase("Friday"),
            DateSpec(DateKind.WEEKDAY, weekday=4),
        )

    def test_unsupported_phrases_are_refused(self) -> None:
        refused = (
            "tomorrow",
            "tomorrow at",
            "tomorrow at 9",
            "tomorrow at 9am",
            "tomorrow at 24:00",
            "tomorrow at 9:60",
            "tomorrow at 9:5",
            "tomorrow at 109:00",
            "tomorrow  at 9:00",
            " tomorrow at 9:00",
            "tomorrow at 9:00 ",
            "monday at 9:00",
            "on mon at 9:00",
            "next monday at 9:00",
            "tomorrow morning",
            "tomorrow at noon",
            "on 2027-02-30 at 9:00",
            "on 2027-13-01 at 9:00",
            "on 27-10-31 at 9:00",
            "in 0 days at 9:00",
            "in 05 minutes",
            f"in {MAXIMUM_DAYS + 1} days at 9:00",
            f"in {MAXIMUM_MINUTES + 1} minutes",
            f"in {MAXIMUM_HOURS + 1} hours",
            "in 100000 minutes",
            "in 2 weeks",
            "in 2 days",
            "in half an hour",
            "tomorrow at 9:00 CET",
            "tomorrow at 9:00 UTC",
            "tomorrow at 9:00 +0100",
            "tomorrow at 9:00 +24:00",
            "tomorrow at 9:00 +01:60",
            "tomorrow at 9:00 -00:00",
            "tomorrow at 9:00 Europe/Berlin",
            f"in {FULLWIDTH_ONE} hour",
            f"tomorrow at {ARABIC_INDIC_ONE}:00",
            "",
        )
        for text in refused:
            with self.subTest(text=text):
                self.assertIsNone(parse_reminder_phrase(text))

    def test_non_text_is_never_parsed(self) -> None:
        for value in (None, 7, b"tomorrow at 9:00", ["tomorrow at 9:00"]):
            with self.subTest(value=value):
                self.assertIsNone(parse_reminder_phrase(value))
                self.assertIsNone(parse_task_phrase(value))
                self.assertIsNone(parse_reminder_command(value))
                self.assertIsNone(parse_task_command(value))

    def test_range_bounds_are_inclusive(self) -> None:
        self.assertIsNotNone(parse_reminder_phrase(f"in {MAXIMUM_DAYS} days at 9:00"))
        self.assertIsNotNone(parse_task_command(f"in {MAXIMUM_DAYS} days"))
        self.assertIsNone(parse_task_command(f"in {MAXIMUM_DAYS + 1} days"))
        self.assertEqual(
            (MAXIMUM_DAYS, MAXIMUM_MINUTES, MAXIMUM_HOURS),
            (366, 1440, 168),
        )

    def test_task_phrases_are_dates_only(self) -> None:
        self.assertEqual(parse_task_phrase("today"), DateSpec(DateKind.TODAY))
        self.assertEqual(parse_task_phrase("tomorrow"), DateSpec(DateKind.TOMORROW))
        self.assertEqual(
            parse_task_phrase("2027-11-05"),
            DateSpec(DateKind.DATE, on=date(2027, 11, 5)),
        )
        for text in ("in 3 days", "on friday", "friday at 9:00", "next friday", "eod"):
            with self.subTest(text=text):
                self.assertIsNone(parse_task_phrase(text))

    def test_the_command_grammar_has_no_on_or_at(self) -> None:
        spec = parse_reminder_command("monday 09:00 +01:00")
        self.assertEqual(spec.day, DateSpec(DateKind.WEEKDAY, weekday=0))
        self.assertEqual((spec.clock, spec.offset), (time(9, 0), PLUS_ONE))
        self.assertEqual(
            parse_reminder_command("in 2 days 8:30").day,
            DateSpec(DateKind.DAYS_LATER, days=2),
        )
        self.assertEqual(
            parse_task_command("in 1 day"),
            DateSpec(DateKind.DAYS_LATER, days=1),
        )
        for text in ("on monday at 09:00", "tomorrow at 09:00", "tomorrow"):
            with self.subTest(text=text):
                self.assertIsNone(parse_reminder_command(text))


class CalendarResolutionTests(unittest.TestCase):
    """Prove calendar words, wall times, and elapsed spans resolve exactly."""

    def setUp(self) -> None:
        SyntheticZones(self, {SYNTHETIC_KEY: CENTRAL_RULES})

    def test_local_today_is_the_reference_in_the_zone(self) -> None:
        late = datetime(2027, 7, 14, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(local_today(_timing(late)), date(2027, 7, 15))

    def test_calendar_words_name_dates_after_local_today(self) -> None:
        timing = _timing(BEFORE_FALL_BACK)  # local Saturday 2027-10-30
        cases = (
            ("today", date(2027, 10, 30)),
            ("tomorrow", date(2027, 10, 31)),
            ("sunday", date(2027, 10, 31)),
            ("friday", date(2027, 11, 5)),
            ("saturday", date(2027, 11, 6)),
            ("2027-12-24", date(2027, 12, 24)),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    resolve_task_date(parse_task_phrase(text), timing),
                    expected,
                )
        self.assertEqual(
            resolve_task_date(parse_task_command("in 10 days"), timing),
            date(2027, 11, 9),
        )

    def test_the_same_weekday_is_seven_days_later(self) -> None:
        monday = datetime(2027, 11, 1, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(
            resolve_task_date(parse_task_phrase("monday"), _timing(monday)),
            date(2027, 11, 8),
        )

    def test_a_wall_time_is_kept_across_a_daylight_saving_change(self) -> None:
        resolved = _resolve("in 1 day at 09:00")
        self.assertEqual(
            resolved.canonical,
            "2027-10-31T09:00:00+01:00[Maria/Synthetic]",
        )
        self.assertEqual(resolved.instant_text, "2027-10-31T08:00:00Z")

    def test_elapsed_hours_are_exact_across_a_change(self) -> None:
        resolved = _resolve("in 24 hours")
        # 10:03 at +02:00 plus 24 elapsed hours is 09:03 at +01:00.
        self.assertEqual(
            resolved.canonical,
            "2027-10-31T09:03:00+01:00[Maria/Synthetic]",
        )
        self.assertEqual(
            resolved.instant - BEFORE_FALL_BACK.replace(second=0),
            timedelta(hours=24),
        )

    def test_elapsed_minutes_count_from_the_reference_truncated(self) -> None:
        resolved = _resolve("in 20 minutes")
        self.assertEqual(
            resolved.canonical,
            "2027-10-30T10:23:00+02:00[Maria/Synthetic]",
        )
        self.assertEqual(resolved.instant_text, "2027-10-30T08:23:00Z")
        waited = resolved.instant - BEFORE_FALL_BACK
        self.assertEqual(waited, timedelta(minutes=19, seconds=33))

    def test_an_elapsed_span_is_always_after_the_reference(self) -> None:
        for second in (0, 1, 59):
            reference = BEFORE_FALL_BACK.replace(second=second, microsecond=999999)
            with self.subTest(second=second):
                resolved = resolve_reminder_time(
                    _phrase("in 1 minute"),
                    _timing(reference),
                )
                self.assertGreater(resolved.instant, reference)

    def test_a_duration_ending_in_a_repeated_hour_carries_its_true_offset(self) -> None:
        first_occurrence = datetime(2027, 10, 31, 0, 10, tzinfo=timezone.utc)
        resolved = resolve_reminder_time(
            _phrase("in 1 hour"),
            _timing(first_occurrence),
        )
        self.assertEqual(
            resolved.canonical,
            "2027-10-31T02:10:00+01:00[Maria/Synthetic]",
        )
        self.assertEqual(resolved.instant_text, "2027-10-31T01:10:00Z")

    def test_a_reminder_must_be_strictly_after_the_reference(self) -> None:
        self.assertIs(_resolve("today at 09:00"), TimingFailure.PAST)
        self.assertIs(_resolve("today at 10:03"), TimingFailure.PAST)
        self.assertEqual(
            _resolve("today at 10:04").canonical,
            "2027-10-30T10:04:00+02:00[Maria/Synthetic]",
        )
        self.assertIs(_resolve("on 2027-10-29 at 23:00"), TimingFailure.PAST)
        # A reference exactly on the minute: that minute is not after it.
        on_the_minute = _timing(BEFORE_FALL_BACK.replace(second=0))
        self.assertIs(
            resolve_reminder_time(_phrase("today at 10:03"), on_the_minute),
            TimingFailure.PAST,
        )
        self.assertEqual(
            resolve_reminder_time(_phrase("in 1 minute"), on_the_minute).canonical,
            "2027-10-30T10:04:00+02:00[Maria/Synthetic]",
        )

    def test_a_task_date_may_be_today_but_not_earlier(self) -> None:
        timing = _timing(BEFORE_FALL_BACK)
        self.assertEqual(
            resolve_task_date(parse_task_phrase("today"), timing),
            date(2027, 10, 30),
        )
        self.assertIs(
            resolve_task_date(parse_task_phrase("2027-10-29"), timing),
            TimingFailure.PAST,
        )


class HorizonTests(unittest.TestCase):
    """Prove the inclusive ten-year horizon and its February 29 rule."""

    def setUp(self) -> None:
        SyntheticZones(self, {SYNTHETIC_KEY: CENTRAL_RULES})

    def test_the_horizon_is_the_same_day_ten_years_on(self) -> None:
        self.assertEqual(horizon(date(2026, 10, 4)), date(2036, 10, 4))
        self.assertEqual(horizon(date(2028, 2, 29)), date(2038, 2, 28))
        self.assertEqual(horizon(date(2032, 2, 29)), date(2042, 2, 28))
        self.assertEqual(horizon(date(2030, 2, 28)), date(2040, 2, 28))
        self.assertEqual(horizon(date(9995, 1, 1)), date.max)

    def test_the_boundary_is_inclusive_and_one_day_beyond_is_refused(self) -> None:
        timing = _timing(datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(
            resolve_task_date(parse_task_phrase("2036-10-04"), timing),
            date(2036, 10, 4),
        )
        self.assertIs(
            resolve_task_date(parse_task_phrase("2036-10-05"), timing),
            TimingFailure.TOO_FAR,
        )
        self.assertIsInstance(
            resolve_reminder_time(_phrase("on 2036-10-04 at 23:59"), timing),
            ResolvedTime,
        )
        self.assertIs(
            resolve_reminder_time(_phrase("on 2036-10-05 at 00:00"), timing),
            TimingFailure.TOO_FAR,
        )

    def test_a_leap_day_reference_maps_the_horizon_to_february_28(self) -> None:
        timing = _timing(datetime(2028, 2, 29, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(
            resolve_task_date(parse_task_phrase("2038-02-28"), timing),
            date(2038, 2, 28),
        )
        self.assertIs(
            resolve_task_date(parse_task_phrase("2038-03-01"), timing),
            TimingFailure.TOO_FAR,
        )


class DaylightSavingTests(unittest.TestCase):
    """Prove a skipped local time is refused and a repeated one needs an offset."""

    def setUp(self) -> None:
        SyntheticZones(
            self,
            {SYNTHETIC_KEY: CENTRAL_RULES, "Maria/Half": HALF_HOUR_RULES},
        )

    def test_a_nonexistent_local_time_is_refused_never_shifted(self) -> None:
        for text in (
            "tomorrow at 02:30",
            "tomorrow at 02:00",
            "tomorrow at 02:59",
            "tomorrow at 02:30 +01:00",
            "tomorrow at 02:30 +02:00",
        ):
            with self.subTest(text=text):
                self.assertIs(
                    _resolve(text, BEFORE_SPRING_FORWARD),
                    TimingFailure.NONEXISTENT,
                )
        self.assertEqual(
            _resolve("tomorrow at 03:00", BEFORE_SPRING_FORWARD).canonical,
            "2027-03-28T03:00:00+02:00[Maria/Synthetic]",
        )

    def test_a_repeated_local_time_needs_one_of_its_offsets(self) -> None:
        self.assertIs(_resolve("tomorrow at 02:30"), TimingFailure.AMBIGUOUS)
        first = _resolve("tomorrow at 02:30 +02:00")
        second = _resolve("tomorrow at 02:30 +01:00")
        self.assertEqual(first.canonical, "2027-10-31T02:30:00+02:00[Maria/Synthetic]")
        self.assertEqual(first.instant_text, "2027-10-31T00:30:00Z")
        self.assertEqual(second.canonical, "2027-10-31T02:30:00+01:00[Maria/Synthetic]")
        self.assertEqual(second.instant_text, "2027-10-31T01:30:00Z")
        self.assertIs(_resolve("tomorrow at 02:30 +03:00"), TimingFailure.AMBIGUOUS)

    def test_an_offset_on_an_ordinary_time_must_be_its_own(self) -> None:
        self.assertEqual(
            _resolve("tomorrow at 09:00 +01:00").canonical,
            "2027-10-31T09:00:00+01:00[Maria/Synthetic]",
        )
        self.assertIs(_resolve("tomorrow at 09:00 +02:00"), TimingFailure.AMBIGUOUS)

    def test_valid_offsets_classify_gaps_folds_and_ordinary_times(self) -> None:
        zone = load_zone(SYNTHETIC_KEY)
        self.assertEqual(valid_offsets(datetime(2027, 3, 28, 2, 30), zone), ())
        self.assertEqual(
            valid_offsets(datetime(2027, 10, 31, 2, 30), zone),
            (PLUS_TWO, PLUS_ONE),
        )
        self.assertEqual(valid_offsets(datetime(2027, 7, 1, 12, 0), zone), (PLUS_TWO,))
        self.assertEqual(valid_offsets(datetime(2027, 1, 1, 12, 0), zone), (PLUS_ONE,))

    def test_a_thirty_minute_change_has_a_thirty_minute_gap(self) -> None:
        # Saturday 2027-10-02 at 10:00 local, +10:30.
        reference = datetime(2027, 10, 1, 23, 30, tzinfo=timezone.utc)
        timing = _timing(reference, "Maria/Half")
        self.assertIs(
            resolve_reminder_time(_phrase("tomorrow at 02:15"), timing),
            TimingFailure.NONEXISTENT,
        )
        self.assertEqual(
            resolve_reminder_time(_phrase("tomorrow at 02:30"), timing).canonical,
            "2027-10-03T02:30:00+11:00[Maria/Half]",
        )


class ZoneAvailabilityTests(unittest.TestCase):
    """Prove timing that needs a zone refuses without one, never falling back."""

    def setUp(self) -> None:
        self.zones = SyntheticZones(
            self,
            {SYNTHETIC_KEY: CENTRAL_RULES, "Maria/Seconds": SECONDS_OFFSET_RULES},
        )

    def test_no_configured_zone_refuses_new_timing(self) -> None:
        timing = _timing(BEFORE_FALL_BACK, None)
        self.assertIs(local_today(timing), TimingFailure.ZONE_UNAVAILABLE)
        self.assertIs(
            resolve_task_date(parse_task_phrase("tomorrow"), timing),
            TimingFailure.ZONE_UNAVAILABLE,
        )
        self.assertIs(
            resolve_reminder_time(_phrase("in 20 minutes"), timing),
            TimingFailure.ZONE_UNAVAILABLE,
        )

    def test_a_key_with_no_data_or_unreadable_data_refuses(self) -> None:
        self.zones.corrupt("Maria/Broken")
        for key in ("Maria/Missing", "Maria/Broken", "UTC/../Etc", "/etc/localtime"):
            with self.subTest(key=key):
                self.assertIsNone(load_zone(key))
                self.assertIs(
                    resolve_reminder_time(
                        _phrase("tomorrow at 09:00"),
                        _timing(BEFORE_FALL_BACK, key),
                    ),
                    TimingFailure.ZONE_UNAVAILABLE,
                )

    def test_an_offset_with_seconds_cannot_be_stored(self) -> None:
        self.assertIs(
            resolve_reminder_time(
                _phrase("tomorrow at 09:00"),
                _timing(BEFORE_FALL_BACK, "Maria/Seconds"),
            ),
            TimingFailure.ZONE_UNAVAILABLE,
        )

    def test_zone_keys_have_iana_syntax(self) -> None:
        for key in (
            "UTC",
            "Etc/GMT+5",
            "America/Port-au-Prince",
            "America/Argentina/Buenos_Aires",
            "EST5EDT",
            "Maria/Synthetic",
        ):
            with self.subTest(key=key):
                self.assertIs(is_zone_key(key), True)
        for key in (
            "",
            "/Europe/Berlin",
            "Europe/",
            "Europe//Berlin",
            "Europe/../Berlin",
            "Europe/Berlin ",
            "1Europe/Berlin",
            "Europe/Ber lin",
            "A" * 65,
            None,
            5,
        ):
            with self.subTest(key=key):
                self.assertIs(is_zone_key(key), False)


class RuleCheckTests(unittest.TestCase):
    """Prove the rule-change check compares saved values with current rules."""

    def setUp(self) -> None:
        self.zones = SyntheticZones(self, {SYNTHETIC_KEY: CENTRAL_RULES})

    def test_an_unchanged_ordinary_time_agrees(self) -> None:
        self.assertIs(
            check_rules(datetime(2027, 7, 1, 9, 0), PLUS_TWO, SYNTHETIC_KEY),
            RuleCheck.AGREES,
        )

    def test_an_unchanged_correctly_selected_repeated_time_agrees(self) -> None:
        # The regression the owner asked for: either occurrence of a repeated
        # local time, chosen with its own offset, is no rule change.
        for offset in (PLUS_TWO, PLUS_ONE):
            with self.subTest(offset=offset):
                self.assertIs(
                    check_rules(datetime(2027, 10, 31, 2, 30), offset, SYNTHETIC_KEY),
                    RuleCheck.AGREES,
                )

    def test_changed_rules_are_recognised(self) -> None:
        local = datetime(2027, 7, 1, 9, 0)
        self.zones.write(SYNTHETIC_KEY, FIXED_PLUS_THREE_RULES)
        self.assertIs(check_rules(local, PLUS_TWO, SYNTHETIC_KEY), RuleCheck.CHANGED)
        self.assertIs(check_rules(local, PLUS_THREE, SYNTHETIC_KEY), RuleCheck.AGREES)

    def test_a_time_that_no_longer_exists_is_a_change(self) -> None:
        self.assertIs(
            check_rules(datetime(2027, 3, 28, 2, 30), PLUS_ONE, SYNTHETIC_KEY),
            RuleCheck.CHANGED,
        )

    def test_an_unloadable_zone_is_unchecked_never_an_error(self) -> None:
        self.zones.remove(SYNTHETIC_KEY)
        self.assertIs(
            check_rules(datetime(2027, 7, 1, 9, 0), PLUS_TWO, SYNTHETIC_KEY),
            RuleCheck.UNAVAILABLE,
        )


class RealZoneTests(unittest.TestCase):
    """The same rules against real IANA data, skipped when there is none."""

    def test_a_real_repeated_hour_needs_its_offset_and_agrees_unchanged(self) -> None:
        real_zone(self, "Europe/Berlin")
        timing = _timing(BEFORE_FALL_BACK, "Europe/Berlin")
        self.assertIs(
            resolve_reminder_time(_phrase("tomorrow at 02:30"), timing),
            TimingFailure.AMBIGUOUS,
        )
        resolved = resolve_reminder_time(_phrase("tomorrow at 02:30 +01:00"), timing)
        self.assertEqual(
            resolved.canonical,
            "2027-10-31T02:30:00+01:00[Europe/Berlin]",
        )
        for offset in (PLUS_ONE, PLUS_TWO):
            with self.subTest(offset=offset):
                self.assertIs(
                    check_rules(datetime(2027, 10, 31, 2, 30), offset, "Europe/Berlin"),
                    RuleCheck.AGREES,
                )

    def test_a_real_skipped_hour_is_refused(self) -> None:
        real_zone(self, "America/New_York")
        reference = datetime(2027, 3, 13, 17, 0, tzinfo=timezone.utc)
        self.assertIs(
            resolve_reminder_time(
                _phrase("tomorrow at 02:30"),
                _timing(reference, "America/New_York"),
            ),
            TimingFailure.NONEXISTENT,
        )

    def test_a_real_thirty_minute_change_is_classified(self) -> None:
        zone = real_zone(self, "Australia/Lord_Howe")
        self.assertEqual(valid_offsets(datetime(2026, 10, 4, 2, 15), zone), ())
        self.assertEqual(
            valid_offsets(datetime(2027, 4, 4, 1, 45), zone),
            (timedelta(hours=11), timedelta(hours=10, minutes=30)),
        )


class CanonicalFormTests(unittest.TestCase):
    """Prove the canonical texts Maria writes, and the timing context."""

    def test_offsets_and_instants_are_written_canonically(self) -> None:
        self.assertEqual(offset_text(timedelta(0)), "+00:00")
        self.assertEqual(offset_text(timedelta(hours=-5, minutes=-30)), "-05:30")
        self.assertEqual(offset_text(timedelta(hours=13, minutes=45)), "+13:45")
        self.assertEqual(
            canonical_remind_time(
                datetime(2027, 1, 2, 3, 4),
                -timedelta(hours=5),
                "America/New_York",
            ),
            "2027-01-02T03:04:00-05:00[America/New_York]",
        )
        moment = datetime(2027, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
        self.assertEqual(utc_instant_text(moment), "2027-01-02T03:04:05Z")
        self.assertEqual(
            reference_instant_text(_timing(moment)),
            "2027-01-02T03:04:05Z",
        )

    def test_only_whole_minute_offsets_within_a_day_are_canonical(self) -> None:
        for offset, valid in (
            (timedelta(0), True),
            (timedelta(hours=23, minutes=59), True),
            (-timedelta(hours=23, minutes=59), True),
            (timedelta(hours=24), False),
            (timedelta(hours=1, seconds=30), False),
            (None, False),
        ):
            with self.subTest(offset=offset):
                self.assertIs(is_canonical_offset(offset), valid)

    def test_a_timing_context_needs_an_aware_utc_instant(self) -> None:
        naive = datetime(2027, 1, 1, 12, 0)
        offset = datetime(2027, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=-3)))
        # Zones that only happen to have no offset are refused too: in this
        # one, a week from Thursday 2027-03-25 at 12:00 crosses its change to
        # +01:00, so wall-clock arithmetic in it would lose an hour.
        no_offset_yet = zoneinfo.ZoneInfo.from_file(
            io.BytesIO(tzif_bytes("GMT0BST,M3.5.0/1,M10.5.0"))
        )
        winter = datetime(2027, 3, 25, 12, 0, tzinfo=no_offset_yet)
        self.assertEqual(winter.utcoffset(), timedelta(0))
        named_zero = datetime(2027, 1, 1, 12, 0, tzinfo=timezone(timedelta(0), "GMT"))
        for reference, key in (
            (naive, None),
            (offset, None),
            (winter, SYNTHETIC_KEY),
            (named_zero, SYNTHETIC_KEY),
            ("2027", None),
            (BEFORE_FALL_BACK, 5),
        ):
            with self.subTest(reference=reference, key=key):
                with self.assertRaises(ValueError):
                    TimingContext(reference, key)
        # The same moment converted to UTC is accepted.
        converted = TimingContext(winter.astimezone(timezone.utc), SYNTHETIC_KEY)
        self.assertEqual(converted.reference, winter)
        context = TimingContext(BEFORE_FALL_BACK, "Secret/Zone")
        self.assertEqual(repr(context), "TimingContext(<redacted>)")
        self.assertNotIn("Secret", repr(context))

    def test_parsed_phrases_show_nothing_of_their_timing(self) -> None:
        dated = parse_task_phrase("2027-11-05")
        timed = parse_reminder_phrase("on 2027-11-05 at 09:30 +01:00")
        elapsed = parse_reminder_phrase("in 20 minutes")

        self.assertEqual(repr(dated), "DateSpec(date, <redacted>)")
        self.assertEqual(repr(timed), "ReminderTiming(<redacted>)")
        self.assertEqual(repr(elapsed), "ReminderTiming(<redacted>)")
        self.assertEqual(repr(timed.day), "DateSpec(date, <redacted>)")
        for shown in (repr(dated), str(timed), str(elapsed), repr(timed.day)):
            for value in ("2027", "11", "09", "30", "20", "minutes"):
                with self.subTest(shown=shown, value=value):
                    self.assertNotIn(value, shown)

    def test_failures_carry_fixed_messages(self) -> None:
        self.assertEqual(
            TimingFailure.PAST.message,
            "That date or time has already passed.",
        )
        self.assertEqual(
            TimingFailure.TOO_FAR.message,
            "That date is more than 10 years away.",
        )
        self.assertEqual(
            TimingFailure.ZONE_UNAVAILABLE.message,
            "Maria cannot resolve dates and times because no time zone is "
            "configured, or its time zone data cannot be loaded.",
        )
        for failure in TimingFailure:
            with self.subTest(failure=failure):
                self.assertNotIn("{", failure.message)


class ModuleBoundaryTests(unittest.TestCase):
    """Prove the leaf imports only the standard library and reads no clock."""

    ALLOWED = {"__future__", "re", "zoneinfo", "dataclasses", "datetime", "enum"}
    FORBIDDEN = {
        "threading",
        "sched",
        "asyncio",
        "signal",
        "time",
        "_thread",
        "subprocess",
    }

    def _tree(self) -> ast.Module:
        return ast.parse(inspect.getsource(time_resolution))

    def test_it_imports_only_its_allowed_standard_modules(self) -> None:
        imported = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertLessEqual(imported, self.ALLOWED)
        self.assertFalse(imported & self.FORBIDDEN)
        self.assertNotIn("tzdata", imported)

    def test_it_never_reads_a_clock(self) -> None:
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Attribute):
                with self.subTest(attribute=node.attr):
                    self.assertNotIn(
                        node.attr,
                        {"now", "utcnow", "today", "time", "monotonic"},
                    )

    def test_it_is_the_only_production_module_that_loads_zone_data(self) -> None:
        # Only this leaf imports zoneinfo, nothing imports tzdata, and no
        # production module names either one to import it some other way.
        importers: dict[str, set[str]] = {"zoneinfo": set(), "tzdata": set()}
        for path in PRODUCTION_SOURCES:
            shown = path.relative_to(PROJECT_ROOT).as_posix()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                elif isinstance(node, ast.Constant) and node.value in importers:
                    names = [node.value]
                else:
                    continue
                for name in names:
                    root = name.split(".")[0]
                    if root in importers:
                        importers[root].add(shown)

        self.assertEqual(
            importers,
            {"zoneinfo": {"core/time_resolution.py"}, "tzdata": set()},
        )


if __name__ == "__main__":
    unittest.main()
