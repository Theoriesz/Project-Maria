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

It also holds reminder delivery's vocabulary (V1-23-R1): the three channels
a reminder can be shown through, the two outcomes a display can have, the
scheduling revision that names one claim with the reminder's id, the one-row
delivery setting, and the value objects and fixed messages of each. A claim
is one committed write that reserves one display of one reminder for its
current scheduling revision; every surface commits it before displaying
anything, so a reminder is shown at most once per revision.
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

# The surfaces a reminder can be shown through: the closed-app message box,
# the command line, and the desktop window.
DELIVERY_CHANNEL_DESKTOP = "desktop"
DELIVERY_CHANNEL_CLI = "cli"
DELIVERY_CHANNEL_GUI = "gui"
DELIVERY_CHANNELS = (
    DELIVERY_CHANNEL_DESKTOP,
    DELIVERY_CHANNEL_CLI,
    DELIVERY_CHANNEL_GUI,
)

# What one claimed display came to. A claim with no outcome is unconfirmed:
# Maria cannot say whether it was displayed.
DELIVERY_OUTCOME_DISPLAYED = "displayed"
DELIVERY_OUTCOME_FAILED = "failed"
DELIVERY_OUTCOMES = (DELIVERY_OUTCOME_DISPLAYED, DELIVERY_OUTCOME_FAILED)

# A scheduling revision starts at 1 and only ever rises, by one, when an
# approved reschedule stores a different saved instant. It is an identity,
# not a history: no earlier time, text, or claim is kept with it. The largest
# is the largest INTEGER SQLite can store.
SCHEDULE_REVISION_FIRST = 1
SCHEDULE_REVISION_MAX = 2**63 - 1

# The longest stored channel and outcome, in characters. Both are ASCII, so
# these are also their UTF-8 sizes, and the memory layer bounds a stored
# value by them before it can reach Python.
DELIVERY_CHANNEL_MAX_LENGTH = max(len(channel) for channel in DELIVERY_CHANNELS)
DELIVERY_OUTCOME_MAX_LENGTH = max(len(outcome) for outcome in DELIVERY_OUTCOMES)

# Fixed delivery messages. None interpolates anything supplied at run time.
DELIVERY_RECORD_MESSAGE = (
    "A reminder's delivery state may hold only values Maria can show."
)
DELIVERY_CHANNEL_MESSAGE = (
    "A reminder delivery channel must be desktop, cli, or gui."
)
DELIVERY_OUTCOME_MESSAGE = (
    "A reminder delivery outcome must be displayed or failed."
)
DELIVERY_SETTING_MESSAGE = (
    "The reminder delivery setting may hold only values Maria can show."
)
DELIVERY_STORAGE_MESSAGE = "Unable to read or write reminder delivery."


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


@dataclass(frozen=True)
class ReminderDeliveryState:
    """One reminder's claim for its current scheduling revision.

    ``claimed_at`` is when the claim was committed, a UTC instant to the
    second; ``channel`` is the surface that claimed it; and ``outcome`` is
    ``displayed``, ``failed`` (not displayed), or None while unconfirmed.
    It holds no text, so its ``repr`` shows nothing private.
    """

    claimed_at: str
    channel: str
    outcome: str | None = None

    def __post_init__(self) -> None:
        """Refuse any claim Maria could not have written."""
        if not (
            is_utc_instant(self.claimed_at)
            and is_delivery_channel(self.channel)
            and (self.outcome is None or is_delivery_outcome(self.outcome))
        ):
            raise ValueError(DELIVERY_RECORD_MESSAGE)


@dataclass(frozen=True, repr=False)
class Reminder:
    """One reminder, every field validated.

    ``remind_utc`` is exactly the instant ``remind_time`` encodes.
    ``completed_at`` is set only while the reminder is completed, and
    ``context_enabled`` is True only while the owner's consent covers
    exactly this text and saved time. ``schedule_revision`` is the
    reminder's current scheduling revision, and ``delivery`` its claim for
    that revision, or None when it has none (V1-23-R1).
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
    schedule_revision: int = SCHEDULE_REVISION_FIRST
    delivery: ReminderDeliveryState | None = None

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
            and is_schedule_revision(self.schedule_revision)
            and (
                self.delivery is None
                or type(self.delivery) is ReminderDeliveryState
            )
        ):
            raise ValueError(TASK_RECORD_MESSAGE)

    def __repr__(self) -> str:
        """Name the type and id without showing the text or the time."""
        return f"Reminder(id={self.reminder_id}, <redacted>)"


@dataclass(frozen=True, repr=False)
class DeliveryCandidate:
    """One due reminder with no claim for its revision, as a claim reads it.

    It carries only what a notice shows and what a claim compares: the id,
    the text, the saved time and instant, and the scheduling revision, every
    one validated. Its ``repr`` shows the id alone.
    """

    reminder_id: int
    text: str
    remind_time: str
    remind_utc: str
    schedule_revision: int

    def __post_init__(self) -> None:
        """Refuse any field Maria could not have written."""
        if not (
            is_record_id(self.reminder_id)
            and is_task_text(self.text)
            and timing_agrees(self.remind_time, self.remind_utc)
            and is_schedule_revision(self.schedule_revision)
        ):
            raise ValueError(TASK_RECORD_MESSAGE)

    def __repr__(self) -> str:
        """Name the type and id without showing the text or the time."""
        return f"DeliveryCandidate(id={self.reminder_id}, <redacted>)"


@dataclass(frozen=True)
class DeliveryClaim:
    """The identity of one committed claim: a reminder and its revision.

    A reminder's id is never reused and its revision only rises, so this
    pair names at most one claim, ever. Every outcome is written for exactly
    this pair, never for a timestamp.
    """

    reminder_id: int
    schedule_revision: int

    def __post_init__(self) -> None:
        """Refuse an identity no claim can have."""
        if not (
            is_record_id(self.reminder_id)
            and is_schedule_revision(self.schedule_revision)
        ):
            raise ValueError(DELIVERY_RECORD_MESSAGE)


class ClaimOutcome(str, Enum):
    """What one claim attempt did.

    ``CLAIMED`` committed the claim. ``CHANGED`` wrote nothing because the
    reminder no longer qualified when the claim ran. ``DISABLED`` wrote
    nothing because delivery was not enabled then, which stops the batch.
    """

    CLAIMED = "claimed"
    CHANGED = "changed"
    DISABLED = "disabled"


@dataclass(frozen=True)
class ClaimResult:
    """One claim attempt's outcome, and the claim it committed, if any."""

    outcome: ClaimOutcome
    claim: DeliveryClaim | None = None

    def __post_init__(self) -> None:
        """Require a claim exactly when one was committed."""
        if type(self.outcome) is not ClaimOutcome:
            raise ValueError(DELIVERY_RECORD_MESSAGE)
        if self.outcome is ClaimOutcome.CLAIMED:
            valid = type(self.claim) is DeliveryClaim
        else:
            valid = self.claim is None
        if not valid:
            raise ValueError(DELIVERY_RECORD_MESSAGE)


class DeliverySettingState(str, Enum):
    """What the one-row delivery setting says.

    ``NEVER_SET`` means there is no row, so delivery is off. ``ENABLED`` and
    ``DISABLED`` are a well-formed row. ``UNREADABLE`` is any row Maria could
    not have written, which leaves delivery off.
    """

    NEVER_SET = "never_set"
    ENABLED = "enabled"
    DISABLED = "disabled"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class DeliverySetting:
    """The delivery setting as one read found it.

    ``changed_at`` is the UTC instant the setting last changed, exactly
    while it is ``ENABLED`` or ``DISABLED``, and None otherwise.
    """

    state: DeliverySettingState
    changed_at: str | None = None

    def __post_init__(self) -> None:
        """Refuse a setting value no read can produce."""
        if type(self.state) is not DeliverySettingState:
            raise ValueError(DELIVERY_SETTING_MESSAGE)
        if self.state in (
            DeliverySettingState.ENABLED,
            DeliverySettingState.DISABLED,
        ):
            valid = is_utc_instant(self.changed_at)
        else:
            valid = self.changed_at is None
        if not valid:
            raise ValueError(DELIVERY_SETTING_MESSAGE)

    @property
    def enabled(self) -> bool:
        """Return whether delivery may claim now."""
        return self.state is DeliverySettingState.ENABLED


@dataclass(frozen=True)
class DeliveryCounts:
    """How many reminders' current claims came to each outcome."""

    displayed: int
    unconfirmed: int
    failed: int

    def __post_init__(self) -> None:
        """Refuse a count that is not a whole number of at least zero."""
        if not all(
            type(count) is int and count >= 0
            for count in (self.displayed, self.unconfirmed, self.failed)
        ):
            raise ValueError(DELIVERY_RECORD_MESSAGE)


class DeliveryStorageError(RuntimeError):
    """Reminder delivery state or its setting could not be read or written.

    The memory layer raises it, with a fixed message and no cause, for a
    busy, missing, unreadable, or not-current database, so the delivery
    subsystem can tell a storage failure from any other. Nothing SQLite
    reported and nothing a row held ever joins it.
    """


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


def is_schedule_revision(value: object) -> bool:
    """Return whether a value is an exact int scheduling revision, never a bool."""
    return (
        type(value) is int
        and SCHEDULE_REVISION_FIRST <= value <= SCHEDULE_REVISION_MAX
    )


def is_delivery_channel(value: object) -> bool:
    """Return whether a value is exactly one of the three delivery channels."""
    return type(value) is str and value in DELIVERY_CHANNELS


def is_delivery_outcome(value: object) -> bool:
    """Return whether a value is exactly one of the two delivery outcomes."""
    return type(value) is str and value in DELIVERY_OUTCOMES


def settle_delivery_state(
    claimed_at: object,
    channel: object,
    outcome: object,
) -> ReminderDeliveryState | None:
    """Return a reminder's stored claim columns as its delivery state.

    All three NULL means no claim for the current revision, which is None.
    Otherwise they must be a valid claim stamp, a known channel, and a known
    outcome or NULL; an outcome without a claim, a channel without a stamp,
    or any other mix raises ``ValueError`` with a fixed message.
    """
    if claimed_at is None and channel is None and outcome is None:
        return None
    return ReminderDeliveryState(claimed_at, channel, outcome)


def settle_delivery_setting(
    row_count: object,
    enabled: object,
    changed_at: object,
) -> DeliverySetting:
    """Return what one bounded read of the setting table says.

    No row means the setting was never set. Exactly one row whose enabled
    value is the integer 0 or 1 and whose changed_at is a canonical instant
    is the setting it states. Anything else, from a second row to a value of
    the wrong type, is unreadable, which leaves delivery off.
    """
    if type(row_count) is int and row_count == 0:
        return DeliverySetting(DeliverySettingState.NEVER_SET)
    if (
        type(row_count) is int
        and row_count == 1
        and type(enabled) is int
        and enabled in (0, 1)
        and is_utc_instant(changed_at)
    ):
        return DeliverySetting(
            DeliverySettingState.ENABLED
            if enabled == 1
            else DeliverySettingState.DISABLED,
            changed_at,
        )
    return DeliverySetting(DeliverySettingState.UNREADABLE)


def describe_utc_instant(instant: str) -> str:
    """Return a UTC instant as the owner reads it, to the minute.

    ``2026-10-05T07:00:00Z`` reads ``2026-10-05 07:00 UTC``. Only a valid
    instant may be described.
    """
    if not is_utc_instant(instant):
        raise ValueError(AS_OF_INSTANT_MESSAGE)
    return f"{instant[:10]} {instant[11:16]} UTC"
