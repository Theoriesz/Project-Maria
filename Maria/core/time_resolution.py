"""Timing for tasks and reminders: the closed grammar and its resolution.

V1-22 lets the owner give a task a calendar date and a reminder a moment, in
a small closed vocabulary, and this leaf module is the one place that
vocabulary is read and resolved. It holds the timing tokens the deterministic
commands and the supported natural-language request forms share, resolves a
parsed phrase against one supplied reference instant and the owner's
configured time zone, classifies a local time that a daylight-saving change
skips or repeats, writes the canonical values a plan carries, loads a zone
through standard-library ``zoneinfo``, and checks a saved reminder against its
zone's current rules.

It reads no clock: the reference instant is Brain's, captured once per
request, so the same phrase, instant, and zone always resolve the same way.
It has no SQL, logs nothing, and imports only the standard library. Time zone
data comes from configured time zone files on zoneinfo's search path or, when
none provides a key, from the optional ``tzdata`` package, which zoneinfo
loads itself; nothing here imports it, and nothing falls back to UTC, a fixed
offset, or the operating system's zone when neither has the key.

A calendar word names a date in the configured zone: ``today``, ``tomorrow``,
a weekday (the first such date strictly after today), ``YYYY-MM-DD``, or
``in N days``, which is calendar arithmetic. A reminder's date also needs a
wall time. A nonexistent wall time is refused, never shifted, and a repeated
one is refused unless an offset chooses one of its occurrences. Elapsed
minutes and hours are counted on UTC instants from the reference truncated to
the minute, so the local clock they land on carries its true offset.
"""

from __future__ import annotations

import re
import zoneinfo
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum


# The inclusive horizon for new timing: a resolved local date may be at most
# this many years after local today, on the same month and day, with
# February 29 mapping to February 28 when the target year has no such day.
TIMING_HORIZON_YEARS = 10

# The largest count each relative phrase accepts. A count is ASCII digits with
# no leading zero, from 1.
MAXIMUM_DAYS = 366
MAXIMUM_MINUTES = 1440
MAXIMUM_HOURS = 168

# The longest time zone key Maria accepts. Every IANA key is far shorter.
ZONE_KEY_MAX_LENGTH = 64

# The weekday names, in ``date.weekday()`` order, Monday first. They are the
# only weekday spellings the grammar knows, and the English names Maria shows.
WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

# An IANA key: slash-separated parts, each starting with a letter and holding
# only ASCII letters, digits, underscores, and plus and minus signs, such as
# ``Area/City`` or ``Etc/GMT+5``. zoneinfo refuses absolute paths and parent
# references itself; this is stricter still.
_ZONE_KEY_PATTERN = re.compile(
    r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z][A-Za-z0-9_+-]*)*"
)

# The shared timing tokens. Every pattern is matched whole, ASCII
# case-insensitively, and digits are ASCII digits only.
_FLAGS = re.ASCII | re.IGNORECASE
_WEEKDAY = "|".join(WEEKDAY_NAMES)
_COUNT = r"[1-9][0-9]{0,4}"
_ISO_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_CLOCK = r"(?P<clock>[0-9]{1,2}:[0-9]{2})"
_OFFSET = r"(?P<offset>[+-][0-9]{2}:[0-9]{2})"
_ELAPSED = rf"in (?P<count>{_COUNT}) (?P<unit>minutes?|hours?)"

# The request forms' timing phrase: ``today at T``, ``tomorrow at T``,
# ``on WEEKDAY at T``, ``on YYYY-MM-DD at T``, ``in N day(s) at T``, or an
# elapsed ``in N minute(s)`` or ``in N hour(s)``, where T is a clock time
# with an optional offset.
_PHRASE_DATE = (
    rf"(?P<today>today)|(?P<tomorrow>tomorrow)|on (?P<weekday>{_WEEKDAY})"
    rf"|on (?P<date>{_ISO_DATE})|in (?P<days>{_COUNT}) days?"
)
_REMINDER_PHRASE = re.compile(
    rf"(?:{_PHRASE_DATE}) at {_CLOCK}(?: {_OFFSET})?|{_ELAPSED}",
    _FLAGS,
)

# A request form's task date: ``today``, ``tomorrow``, a weekday, or
# ``YYYY-MM-DD``.
_TASK_PHRASE = re.compile(
    rf"(?P<today>today)|(?P<tomorrow>tomorrow)|(?P<weekday>{_WEEKDAY})"
    rf"|(?P<date>{_ISO_DATE})",
    _FLAGS,
)

# The deterministic commands' date adds ``in N day(s)``, and their reminder
# timing is a date and a clock time, with an optional offset, or an elapsed
# span, with no ``on`` or ``at``.
_COMMAND_DATE = (
    rf"(?P<today>today)|(?P<tomorrow>tomorrow)|(?P<weekday>{_WEEKDAY})"
    rf"|(?P<date>{_ISO_DATE})|in (?P<days>{_COUNT}) days?"
)
_TASK_COMMAND = re.compile(_COMMAND_DATE, _FLAGS)
_REMINDER_COMMAND = re.compile(
    rf"(?:{_COMMAND_DATE}) {_CLOCK}(?: {_OFFSET})?|{_ELAPSED}",
    _FLAGS,
)

# The canonical offset: a sign, two-digit hours, a colon, two-digit minutes.
_OFFSET_TEXT = re.compile(r"([+-])([0-9]{2}):([0-9]{2})")


class TimingFailure(str, Enum):
    """Why one parsed timing phrase could not become a value to store."""

    PAST = "past"
    TOO_FAR = "too_far"
    NONEXISTENT = "nonexistent"
    AMBIGUOUS = "ambiguous"
    ZONE_UNAVAILABLE = "zone_unavailable"

    @property
    def message(self) -> str:
        """Return the fixed owner-visible sentence for this failure."""
        return _FAILURE_MESSAGES[self]


# Every message is fixed text built from nothing but Maria's own constants,
# so none can repeat the owner's phrase, a zone, or an offset.
_FAILURE_MESSAGES = {
    TimingFailure.PAST: "That date or time has already passed.",
    TimingFailure.TOO_FAR: (
        f"That date is more than {TIMING_HORIZON_YEARS} years away."
    ),
    TimingFailure.NONEXISTENT: (
        "That local time does not exist on that date because of a "
        "daylight-saving change."
    ),
    TimingFailure.AMBIGUOUS: (
        "That local time does not name exactly one moment: it occurs twice "
        "on that date because of a daylight-saving change, or the offset "
        "given does not match it. Give the time with its offset, such as "
        "02:30 +01:00."
    ),
    TimingFailure.ZONE_UNAVAILABLE: (
        "Maria cannot resolve dates and times because no time zone is "
        "configured, or its time zone data cannot be loaded."
    ),
}


class DateKind(str, Enum):
    """Which calendar phrase a date names."""

    TODAY = "today"
    TOMORROW = "tomorrow"
    WEEKDAY = "weekday"
    DATE = "date"
    DAYS_LATER = "days_later"


class RuleCheck(str, Enum):
    """How a saved reminder's values compare with its zone's current rules.

    ``AGREES`` means the saved offset is one at which the saved local time
    exists under the current rules, so a correctly chosen repeated time
    agrees too. ``CHANGED`` means it is not, because the zone's rules changed
    after the reminder was saved. ``UNAVAILABLE`` means the saved zone could
    not be loaded, so nothing could be checked.
    """

    AGREES = "agrees"
    CHANGED = "changed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, repr=False)
class DateSpec:
    """One calendar date phrase, parsed but not yet resolved.

    ``weekday`` is set for a weekday, 0 for Monday; ``on`` for an exact
    date; and ``days`` for ``in N days``. Its ``repr`` shows the kind only.
    """

    kind: DateKind
    weekday: int | None = None
    on: date | None = None
    days: int | None = None

    def __repr__(self) -> str:
        """Name the type and kind without showing the date."""
        return f"DateSpec({self.kind.value}, <redacted>)"


@dataclass(frozen=True, repr=False)
class ReminderTiming:
    """One reminder timing phrase, parsed but not yet resolved.

    It is either a calendar ``day`` with a wall-clock ``clock`` and an
    optional ``offset`` choosing one occurrence of a repeated local time, or
    an ``elapsed`` span counted from the reference instant. Nothing is shown
    by ``repr``.
    """

    day: DateSpec | None = None
    clock: time | None = None
    offset: timedelta | None = None
    elapsed: timedelta | None = None

    def __repr__(self) -> str:
        """Name the type without showing the date, time, offset, or span."""
        return "ReminderTiming(<redacted>)"


@dataclass(frozen=True, repr=False)
class TimingContext:
    """One request's reference instant and the owner's configured zone key.

    ``reference`` is the aware UTC moment Brain read once, before planning,
    and every timing decision of that request uses it. Its time zone must be
    ``timezone.utc`` itself: a zone that only happens to have no offset at
    that moment could add an elapsed span as wall-clock time across its own
    daylight-saving change. ``zone_key`` is the configured IANA key, or None
    when none is configured. Neither is shown by ``repr``.
    """

    reference: datetime
    zone_key: str | None

    def __post_init__(self) -> None:
        """Refuse anything but an exact UTC instant and a key or None."""
        if (
            type(self.reference) is not datetime
            or self.reference.tzinfo is not timezone.utc
            or (self.zone_key is not None and type(self.zone_key) is not str)
        ):
            raise ValueError(
                "A timing context needs an aware UTC instant and a zone key or "
                "None."
            )

    def __repr__(self) -> str:
        """Name the type without showing the instant or the zone."""
        return "TimingContext(<redacted>)"


@dataclass(frozen=True, repr=False)
class ResolvedTime:
    """One reminder moment resolved in a zone, with its canonical forms.

    ``local`` is the naive wall time, to the minute, ``offset`` its offset
    from UTC, and ``key`` the zone it was resolved in.
    """

    local: datetime
    offset: timedelta
    key: str

    @property
    def canonical(self) -> str:
        """Return the RFC 9557 local time, offset, and zone Maria stores."""
        return canonical_remind_time(self.local, self.offset, self.key)

    @property
    def instant(self) -> datetime:
        """Return the aware UTC instant this local time and offset encode."""
        return (self.local - self.offset).replace(tzinfo=timezone.utc)

    @property
    def instant_text(self) -> str:
        """Return the instant in Maria's UTC form, ``YYYY-MM-DDTHH:MM:SSZ``."""
        return utc_instant_text(self.instant)

    def __repr__(self) -> str:
        """Name the type without showing the moment or the zone."""
        return "ResolvedTime(<redacted>)"


def is_zone_key(value: object) -> bool:
    """Return whether a value has the syntax of an IANA time zone key."""
    return (
        type(value) is str
        and 0 < len(value) <= ZONE_KEY_MAX_LENGTH
        and _ZONE_KEY_PATTERN.fullmatch(value) is not None
    )


def load_zone(key: object) -> zoneinfo.ZoneInfo | None:
    """Return the zone a key names, or None when it is unset or cannot load.

    Any failure to load, a key with no data or data zoneinfo cannot read,
    is the same one outcome, so nothing about it travels further.
    """
    if not is_zone_key(key):
        return None
    try:
        return zoneinfo.ZoneInfo(key)
    except Exception:  # noqa: BLE001 - every load failure is one outcome.
        return None


def parse_reminder_phrase(text: object) -> ReminderTiming | None:
    """Parse a request form's reminder timing phrase, or return None."""
    return _reminder_timing(_REMINDER_PHRASE, text)


def parse_task_phrase(text: object) -> DateSpec | None:
    """Parse a request form's task date, or return None."""
    return _date_spec(_TASK_PHRASE, text)


def parse_reminder_command(text: object) -> ReminderTiming | None:
    """Parse a deterministic command's reminder timing, or return None."""
    return _reminder_timing(_REMINDER_COMMAND, text)


def parse_task_command(text: object) -> DateSpec | None:
    """Parse a deterministic command's task date, or return None."""
    return _date_spec(_TASK_COMMAND, text)


def local_today(timing: TimingContext) -> date | TimingFailure:
    """Return the reference instant's date in the configured zone."""
    zone = load_zone(timing.zone_key)
    if zone is None:
        return TimingFailure.ZONE_UNAVAILABLE
    return timing.reference.astimezone(zone).date()


def resolve_task_date(spec: DateSpec, timing: TimingContext) -> date | TimingFailure:
    """Resolve a task's date phrase at one reference instant and zone.

    The date must be on or after local today and within the horizon.
    """
    today = local_today(timing)
    if isinstance(today, TimingFailure):
        return today
    target = _resolved_date(spec, today)
    if target is None or target > horizon(today):
        return TimingFailure.TOO_FAR
    if target < today:
        return TimingFailure.PAST
    return target


def resolve_reminder_time(
    spec: ReminderTiming,
    timing: TimingContext,
) -> ResolvedTime | TimingFailure:
    """Resolve a reminder's timing phrase at one reference instant and zone.

    A calendar date with a wall time is checked for a skipped or repeated
    local time first, then must be strictly after the reference instant and
    within the horizon. An elapsed span is added to the reference truncated
    to the minute, on UTC instants, and shown with the true offset where it
    lands. Every outcome is either a value to store or one fixed failure.
    """
    zone = load_zone(timing.zone_key)
    if zone is None:
        return TimingFailure.ZONE_UNAVAILABLE
    key = timing.zone_key
    reference = timing.reference
    try:
        today = reference.astimezone(zone).date()
        limit = horizon(today)
        if spec.elapsed is not None:
            start = reference.replace(second=0, microsecond=0)
            landed = (start + spec.elapsed).astimezone(zone)
            offset = landed.utcoffset()
            local = landed.replace(tzinfo=None, fold=0)
            if local.date() > limit:
                return TimingFailure.TOO_FAR
        else:
            target = _resolved_date(spec.day, today)
            if target is None or target > limit:
                return TimingFailure.TOO_FAR
            if target < today:
                return TimingFailure.PAST
            local = datetime.combine(target, spec.clock)
            offsets = valid_offsets(local, zone)
            if not offsets:
                return TimingFailure.NONEXISTENT
            if spec.offset is None:
                if len(offsets) != 1:
                    return TimingFailure.AMBIGUOUS
                offset = offsets[0]
            elif spec.offset in offsets:
                offset = spec.offset
            else:
                return TimingFailure.AMBIGUOUS
    except (OverflowError, ValueError):
        return TimingFailure.TOO_FAR
    if not is_canonical_offset(offset):
        return TimingFailure.ZONE_UNAVAILABLE
    resolved = ResolvedTime(local, offset, key)
    if resolved.instant <= reference:
        return TimingFailure.PAST
    return resolved


def valid_offsets(local: datetime, zone: object) -> tuple[timedelta, ...]:
    """Return the offsets at which a naive local time exists in a zone.

    A local time a daylight-saving change skips has none, an ordinary one
    has one, and one it repeats has two, the earlier occurrence's first.
    Each fold's offset is kept only when converting the instant it implies
    back to the zone gives exactly the same wall time (PEP 495).
    """
    found: list[timedelta] = []
    for fold in (0, 1):
        offset = local.replace(tzinfo=zone, fold=fold).utcoffset()
        if offset is None:
            continue
        instant = (local - offset).replace(tzinfo=timezone.utc)
        back = instant.astimezone(zone).replace(tzinfo=None, fold=0)
        if back == local and offset not in found:
            found.append(offset)
    return tuple(found)


def check_rules(local: datetime, offset: timedelta, key: str) -> RuleCheck:
    """Compare a saved reminder's local time and offset with current rules.

    The saved zone is loaded by its own key, never the configured zone. The
    values agree when the saved offset is one of those at which the saved
    local time exists under the current rules; a skipped local time, or a
    different offset, means the rules changed. The check never changes or
    refuses anything: it only decides whether a warning is shown.
    """
    zone = load_zone(key)
    if zone is None:
        return RuleCheck.UNAVAILABLE
    try:
        offsets = valid_offsets(local, zone)
    except (OverflowError, ValueError):
        return RuleCheck.UNAVAILABLE
    return RuleCheck.AGREES if offset in offsets else RuleCheck.CHANGED


def horizon(today: date) -> date:
    """Return the last date new timing may name, ten years after today.

    It is the same month and day ten years later, February 28 when that
    year has no February 29, and the last representable date beyond that.
    """
    year = today.year + TIMING_HORIZON_YEARS
    if year > date.max.year:
        return date.max
    try:
        return today.replace(year=year)
    except ValueError:
        return today.replace(year=year, day=28)


def is_canonical_offset(offset: object) -> bool:
    """Return whether an offset is whole minutes from -23:59 to +23:59."""
    return (
        type(offset) is timedelta
        and abs(offset) < timedelta(days=1)
        and offset % timedelta(minutes=1) == timedelta(0)
    )


def offset_text(offset: timedelta) -> str:
    """Return an offset as ``+HH:MM`` or ``-HH:MM``; zero is ``+00:00``."""
    minutes = int(offset.total_seconds()) // 60
    sign = "-" if minutes < 0 else "+"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def canonical_remind_time(local: datetime, offset: timedelta, key: str) -> str:
    """Return ``YYYY-MM-DDTHH:MM:SS+HH:MM[Key]`` for one local time."""
    return f"{local:%Y-%m-%dT%H:%M:%S}{offset_text(offset)}[{key}]"


def utc_instant_text(instant: datetime) -> str:
    """Return an aware instant in Maria's UTC form, whole seconds, with Z."""
    utc = instant.astimezone(timezone.utc)
    return f"{utc:%Y-%m-%dT%H:%M:%S}Z"


def iso_date_text(day: date) -> str:
    """Return a date as ``YYYY-MM-DD``."""
    return f"{day:%Y-%m-%d}"


def reference_instant_text(timing: TimingContext) -> str:
    """Return the reference instant in Maria's UTC form, to the second."""
    return utc_instant_text(timing.reference)


def _reminder_timing(pattern: re.Pattern[str], text: object) -> ReminderTiming | None:
    """Return a reminder timing phrase matched whole by ``pattern``, or None."""
    if type(text) is not str:
        return None
    match = pattern.fullmatch(text)
    if match is None:
        return None
    if match.group("count") is not None:
        count = int(match.group("count"))
        if match.group("unit").lower().startswith("minute"):
            if count > MAXIMUM_MINUTES:
                return None
            return ReminderTiming(elapsed=timedelta(minutes=count))
        if count > MAXIMUM_HOURS:
            return None
        return ReminderTiming(elapsed=timedelta(hours=count))
    day = _spec_from(match)
    clock = _clock(match.group("clock"))
    offset = _offset(match.group("offset"))
    if day is None or clock is None or offset is False:
        return None
    return ReminderTiming(day=day, clock=clock, offset=offset)


def _date_spec(pattern: re.Pattern[str], text: object) -> DateSpec | None:
    """Return a date phrase matched whole by ``pattern``, or None."""
    if type(text) is not str:
        return None
    match = pattern.fullmatch(text)
    if match is None:
        return None
    return _spec_from(match)


def _spec_from(match: re.Match[str]) -> DateSpec | None:
    """Return the date a match names, or None when it is out of range."""
    groups = match.groupdict()
    if groups.get("today") is not None:
        return DateSpec(DateKind.TODAY)
    if groups.get("tomorrow") is not None:
        return DateSpec(DateKind.TOMORROW)
    if groups.get("weekday") is not None:
        return DateSpec(
            DateKind.WEEKDAY,
            weekday=WEEKDAY_NAMES.index(groups["weekday"].lower()),
        )
    if groups.get("date") is not None:
        try:
            on = date.fromisoformat(groups["date"])
        except ValueError:
            return None
        return DateSpec(DateKind.DATE, on=on)
    if groups.get("days") is not None:
        days = int(groups["days"])
        if days > MAXIMUM_DAYS:
            return None
        return DateSpec(DateKind.DAYS_LATER, days=days)
    return None


def _clock(text: str | None) -> time | None:
    """Return ``H:MM`` or ``HH:MM`` from 00:00 to 23:59, or None."""
    if text is None:
        return None
    hours, minutes = text.split(":")
    if int(hours) > 23 or int(minutes) > 59:
        return None
    return time(int(hours), int(minutes))


def _offset(text: str | None) -> timedelta | None | bool:
    """Return an offset's value, None when absent, or False when invalid.

    It must be ``+HH:MM`` or ``-HH:MM`` with hours to 23 and minutes to 59;
    zero is written ``+00:00`` only.
    """
    if text is None:
        return None
    match = _OFFSET_TEXT.fullmatch(text)
    if match is None:
        return False
    sign, hours, minutes = match.group(1), int(match.group(2)), int(match.group(3))
    if hours > 23 or minutes > 59 or (sign == "-" and hours == minutes == 0):
        return False
    value = timedelta(hours=hours, minutes=minutes)
    return -value if sign == "-" else value


def _resolved_date(spec: DateSpec, today: date) -> date | None:
    """Return the date a phrase names relative to today, or None past range."""
    try:
        if spec.kind is DateKind.TODAY:
            return today
        if spec.kind is DateKind.TOMORROW:
            return today + timedelta(days=1)
        if spec.kind is DateKind.WEEKDAY:
            ahead = (spec.weekday - today.weekday() - 1) % 7 + 1
            return today + timedelta(days=ahead)
        if spec.kind is DateKind.DATE:
            return spec.on
        return today + timedelta(days=spec.days)
    except OverflowError:
        return None
