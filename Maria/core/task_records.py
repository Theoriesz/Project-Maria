"""Tasks and reminders: their shared storage vocabulary.

A task is one line of the owner's text with an optional calendar due date. A
reminder is one line of text with one moment: ``remind_time``, the canonical
RFC 9557 local time, offset, and IANA zone the owner approved, and
``remind_utc``, the instant they encode, which alone orders reminders and
selects due ones. Both are open or completed, carry UTC stamps, and hold
their own context consent. The memory layer stores them, the two tools show
them, and this module is the one place all of them take their limits,
statuses, canonical forms, value objects, owner-visible descriptions, and
fixed messages from.

Validation is strict and runs in both directions: the memory layer checks
every value before writing it and again after reading it, and a value it
cannot use is refused with a fixed message that repeats nothing. A
reminder's stored timing must agree with itself on every write and read:
``remind_utc`` is exactly ``remind_time``'s local date and time minus its
offset, which takes offset arithmetic and no time zone data. Whether a saved
reminder still agrees with its zone's current rules is a separate,
best-effort question for ``core.time_resolution``; a rule change never makes
a record invalid.

This module stores nothing, logs nothing, and reads no time zone data. Its
one clock read is ``task_timestamp``, the stamp a record write takes, which
never decides any timing. No value object here shows its text in a ``repr``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum


# The longest task or reminder text, in UTF-8 bytes.
TASK_TEXT_MAX_BYTES = 512

# The two statuses a task or reminder can have.
STATUS_OPEN = "open"
STATUS_COMPLETED = "completed"
RECORD_STATUSES = (STATUS_OPEN, STATUS_COMPLETED)

# The value a task reschedule carries to clear a due date, and the only
# repeat a reminder can have.
NO_DUE_DATE = "none"
REPEAT_NEVER = "never"

# How many records a listing or due query shows, and the one more it reads
# to learn whether more exist without counting them.
TASK_LIST_LIMIT = 20
TASK_QUERY_LIMIT = TASK_LIST_LIMIT + 1

# Maria's one timestamp form for these records: UTC to the second with a
# literal Z, always exactly this long.
TASK_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# The largest value SQLite can store as an INTEGER, and so the largest id a
# row can have. A larger id names no record.
TASK_MAXIMUM_ID = 2**63 - 1

# Unicode categories a stored text may not contain at all: control, format,
# surrogate, line separator, and paragraph separator, the model compiler's
# own unsafe set. Text without them is always one visible line.
UNSAFE_TEXT_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# A canonical reminder time: a local date and minute with zero seconds, a
# ``+HH:MM`` or ``-HH:MM`` offset, and an IANA key in brackets. The key's
# syntax is ``core.time_resolution``'s, which a test holds this one to.
_REMIND_TIME_PATTERN = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}):([0-9]{2}):00"
    r"([+-])([0-9]{2}):([0-9]{2})"
    r"\[([A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z][A-Za-z0-9_+-]*)*)\]"
)
_ZONE_KEY_MAX_LENGTH = 64

_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")

# The longest value each canonical form can have, in characters. Every
# canonical value is ASCII, so this is also its UTF-8 size, and the memory
# layer bounds a stored value by it before the value can reach Python. A
# reminder time is a local date and second, an offset, and a bracketed key.
DUE_DATE_LENGTH = 10
UTC_INSTANT_LENGTH = 20
REMIND_TIME_MAX_LENGTH = 19 + 6 + 2 + _ZONE_KEY_MAX_LENGTH
CONTEXT_DIGEST_LENGTH = 64

# The English weekday names Maria shows, Monday first.
_WEEKDAY_TITLES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# Fixed messages. None interpolates anything supplied at run time.
TASK_ID_MESSAGE = "Task id must be a positive whole number."
REMINDER_ID_MESSAGE = "Reminder id must be a positive whole number."
TASK_TEXT_MESSAGE = (
    "Task and reminder text must be one line of 1 to "
    f"{TASK_TEXT_MAX_BYTES} UTF-8 bytes, with no leading or trailing "
    "whitespace and no control or formatting characters."
)
DUE_DATE_MESSAGE = "A task's due date must be a calendar date, YYYY-MM-DD."
REMIND_TIME_MESSAGE = (
    "A reminder's time must be a canonical local time, offset, and time zone, "
    "saved with exactly the instant they encode."
)
TASK_RECORD_MESSAGE = "A task or reminder may hold only values Maria can show."
TASK_STATUS_MESSAGE = "A task or reminder status must be open or completed."
TASK_LIST_LIMIT_MESSAGE = (
    "A task or reminder listing limit must be a whole number from 1 to "
    f"{TASK_QUERY_LIMIT}."
)
AS_OF_DATE_MESSAGE = "A due-task query needs a calendar date, YYYY-MM-DD."
AS_OF_INSTANT_MESSAGE = (
    "A due-reminder query needs a UTC instant, YYYY-MM-DDTHH:MM:SSZ."
)
CONTEXT_DIGEST_MESSAGE = "A task or reminder identity must be a SHA-256 digest."
RULES_CHANGED_WARNING = (
    "Time zone rules for this reminder's zone have changed since it was set. "
    "It stays at its saved instant; reschedule it to use the current rules."
)
RULES_UNCHECKED_NOTE = (
    "Maria cannot check this time against current time zone rules."
)


class StatusChange(str, Enum):
    """What one complete or reopen did.

    ``CHANGED`` means the status changed. ``UNCHANGED`` means the record
    already had the requested status, so nothing changed. ``NOT_FOUND``
    means no record has the id.
    """

    CHANGED = "changed"
    UNCHANGED = "unchanged"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class RecordUpdate:
    """What one task or reminder edit or reschedule did.

    ``updated`` is whether a row matched and received the new text or
    timing, and ``context_consent_cleared`` says the record's context
    consent had been on and was turned off.
    """

    updated: bool
    context_consent_cleared: bool = False


@dataclass(frozen=True, repr=False)
class ZonedTime:
    """One canonical reminder time, read into its parts.

    ``local`` is the naive wall time, ``offset`` its offset from UTC, and
    ``key`` the IANA zone it was resolved in.
    """

    local: datetime
    offset: timedelta
    key: str

    @property
    def instant_text(self) -> str:
        """Return the UTC instant these parts encode, in Maria's form."""
        instant = (self.local - self.offset).replace(tzinfo=timezone.utc)
        return f"{instant:%Y-%m-%dT%H:%M:%S}Z"

    def __repr__(self) -> str:
        """Name the type without showing the time or the zone."""
        return "ZonedTime(<redacted>)"


@dataclass(frozen=True, repr=False)
class Task:
    """One task, every field validated.

    ``due_date`` is None for an undated task. ``completed_at`` is set only
    while the task is completed. ``context_enabled`` is True only while the
    owner's consent covers exactly this text and due date.
    """

    task_id: int
    text: str
    due_date: str | None
    status: str
    created_at: str
    updated_at: str
    completed_at: str | None
    context_enabled: bool

    def __post_init__(self) -> None:
        """Refuse any field Maria could not have written."""
        if not (
            is_record_id(self.task_id)
            and is_task_text(self.text)
            and (self.due_date is None or is_due_date(self.due_date))
            and is_lifecycle(
                self.status,
                self.created_at,
                self.updated_at,
                self.completed_at,
            )
            and type(self.context_enabled) is bool
        ):
            raise ValueError(TASK_RECORD_MESSAGE)

    def __repr__(self) -> str:
        """Name the type and id without showing the text or the date."""
        return f"Task(id={self.task_id}, <redacted>)"


@dataclass(frozen=True, repr=False)
class Reminder:
    """One reminder, every field validated.

    ``remind_utc`` is exactly the instant ``remind_time`` encodes.
    ``completed_at`` is set only while the reminder is completed, and
    ``context_enabled`` is True only while the owner's consent covers
    exactly this text and saved time.
    """

    reminder_id: int
    text: str
    remind_time: str
    remind_utc: str
    status: str
    created_at: str
    updated_at: str
    completed_at: str | None
    context_enabled: bool

    def __post_init__(self) -> None:
        """Refuse any field Maria could not have written."""
        if not (
            is_record_id(self.reminder_id)
            and is_task_text(self.text)
            and timing_agrees(self.remind_time, self.remind_utc)
            and is_lifecycle(
                self.status,
                self.created_at,
                self.updated_at,
                self.completed_at,
            )
            and type(self.context_enabled) is bool
        ):
            raise ValueError(TASK_RECORD_MESSAGE)

    def __repr__(self) -> str:
        """Name the type and id without showing the text or the time."""
        return f"Reminder(id={self.reminder_id}, <redacted>)"


def is_task_text(value: object) -> bool:
    """Return whether a value can be stored as a task's or reminder's text.

    That is exact, encodable text of 1 to ``TASK_TEXT_MAX_BYTES`` UTF-8
    bytes that equals its own stripped form and holds no character of an
    unsafe category, so it is always one visible line.
    """
    if (
        type(value) is not str
        or not value
        or len(value) > TASK_TEXT_MAX_BYTES
        or value != value.strip()
    ):
        return False
    if any(
        unicodedata.category(character) in UNSAFE_TEXT_CATEGORIES
        for character in value
    ):
        return False
    try:
        return len(value.encode("utf-8")) <= TASK_TEXT_MAX_BYTES
    except UnicodeEncodeError:
        return False


def is_record_id(value: object) -> bool:
    """Return whether a value is an exact positive int, never a bool."""
    return type(value) is int and value >= 1


def is_status(value: object) -> bool:
    """Return whether a value is exactly one of the two statuses."""
    return type(value) is str and value in RECORD_STATUSES


def is_due_date(value: object) -> bool:
    """Return whether a value is exactly ``YYYY-MM-DD`` naming a real date."""
    if type(value) is not str or _DATE_PATTERN.fullmatch(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def is_utc_instant(value: object) -> bool:
    """Return whether a value is exactly ``YYYY-MM-DDTHH:MM:SSZ``, a real second."""
    if type(value) is not str or _TIMESTAMP_PATTERN.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, TASK_TIMESTAMP_FORMAT)
    except ValueError:
        return False
    return True


def is_task_timestamp(value: object) -> bool:
    """Return whether a value is exactly one valid record timestamp."""
    return is_utc_instant(value)


def task_timestamp() -> str:
    """Return the current UTC time in Maria's task timestamp form."""
    return datetime.now(timezone.utc).strftime(TASK_TIMESTAMP_FORMAT)


def parse_remind_time(value: object) -> ZonedTime | None:
    """Return a canonical reminder time's parts, or None if it is not one.

    It must be exactly the form ``core.time_resolution`` writes: a real
    local date and minute with zero seconds, an offset of whole minutes from
    -23:59 to +23:59 written ``+00:00`` when zero, and a key of at most 64
    characters in IANA key syntax.
    """
    if type(value) is not str:
        return None
    match = _REMIND_TIME_PATTERN.fullmatch(value)
    if match is None:
        return None
    day, hours, minutes, sign, offset_hours, offset_minutes, key = match.groups()
    if (
        int(hours) > 23
        or int(minutes) > 59
        or int(offset_hours) > 23
        or int(offset_minutes) > 59
        or (sign == "-" and offset_hours == offset_minutes == "00")
        or len(key) > _ZONE_KEY_MAX_LENGTH
    ):
        return None
    try:
        on = date.fromisoformat(day)
    except ValueError:
        return None
    magnitude = timedelta(hours=int(offset_hours), minutes=int(offset_minutes))
    offset = -magnitude if sign == "-" else magnitude
    local = datetime(on.year, on.month, on.day, int(hours), int(minutes))
    try:
        local - offset
    except OverflowError:
        return None
    return ZonedTime(local, offset, key)


def is_remind_time(value: object) -> bool:
    """Return whether a value is exactly a canonical reminder time."""
    return parse_remind_time(value) is not None


def timing_agrees(remind_time: object, remind_utc: object) -> bool:
    """Return whether a saved reminder time and instant agree exactly.

    ``remind_utc`` must be a valid UTC instant equal to ``remind_time``'s
    local date and time minus its offset. No time zone data is consulted.
    """
    parts = parse_remind_time(remind_time)
    return (
        parts is not None
        and is_utc_instant(remind_utc)
        and parts.instant_text == remind_utc
    )


def is_context_digest(value: object) -> bool:
    """Return whether a value is a lowercase hexadecimal SHA-256."""
    return type(value) is str and _DIGEST_PATTERN.fullmatch(value) is not None


def describe_due_date(due_date: str | None) -> str:
    """Return a due date as the owner reads it, or that there is none.

    ``2026-10-09`` reads ``due Friday 2026-10-09``; an undated task reads
    ``no due date``. Only a valid date or None may be described.
    """
    if due_date is None:
        return "no due date"
    if not is_due_date(due_date):
        raise ValueError(DUE_DATE_MESSAGE)
    day = date.fromisoformat(due_date)
    return f"due {_WEEKDAY_TITLES[day.weekday()]} {due_date}"


def describe_remind_time(remind_time: str, remind_utc: str) -> str:
    """Return a saved reminder time as the owner reads it, from saved values.

    ``2026-10-05T09:00:00+02:00[Europe/Berlin]`` reads ``Monday 2026-10-05 at
    09:00, Europe/Berlin (UTC+02:00), which is 2026-10-05 07:00 UTC``. Only
    the saved values are shown; no time zone data is consulted.
    """
    parts = parse_remind_time(remind_time)
    if parts is None or not timing_agrees(remind_time, remind_utc):
        raise ValueError(REMIND_TIME_MESSAGE)
    local = parts.local
    offset_minutes = int(parts.offset.total_seconds()) // 60
    sign = "-" if offset_minutes < 0 else "+"
    offset_minutes = abs(offset_minutes)
    offset = f"{sign}{offset_minutes // 60:02d}:{offset_minutes % 60:02d}"
    return (
        f"{_WEEKDAY_TITLES[local.weekday()]} {local:%Y-%m-%d} at "
        f"{local:%H:%M}, {parts.key} (UTC{offset}), which is "
        f"{remind_utc[:10]} {remind_utc[11:16]} UTC"
    )


def is_lifecycle(
    status: object,
    created_at: object,
    updated_at: object,
    completed_at: object,
) -> bool:
    """Return whether a status and its stamps agree.

    Both stamps must be valid, and ``completed_at`` must be a valid stamp
    exactly while the status is completed and None while it is open.
    """
    if not (
        is_status(status)
        and is_task_timestamp(created_at)
        and is_task_timestamp(updated_at)
    ):
        return False
    if status == STATUS_OPEN:
        return completed_at is None
    return is_task_timestamp(completed_at)
