"""Reminder delivery's one claim protocol, shared by every surface (V1-23-R1).

Every surface that shows the owner a due reminder goes through the two
functions here: the closed-app pass, the command line, and the desktop
window. ``claim_notice`` first reads the delivery setting and, only while
delivery is on, reads the due reminders that have no claim for their current
scheduling revision, renders their notice lines, and claims them one at a
time, each claim its own committed write before anything is displayed.
``record_notice`` then writes each claim's outcome once the surface has
displayed the notice, or failed to. Between the two the surface displays the
notice: the pass in a Windows message box, the command line by printing it,
and the desktop window by adding it to its conversation and acknowledging
that it did.

A claim is retained when its call returned ``CLAIMED``. If a later claim
fails in an ordinary way, the retained claims before it still make up the
notice, and the failure is reported. A claim whose commit succeeded but whose
call then raised is not retained: it stays unconfirmed and is never displayed
or retried. Nothing is ever retried, so a reminder is shown at most once per
scheduling revision.

This module runs no SQL, calls no Windows function, starts no thread, and
reads no clock of its own: ``claim_notice`` receives the store, the instant,
and the channel, and the session's seam calls the clock it is given once per
check. It imports nothing from reasoning, planning, the compiler, Tool
Manager, tools, configuration, the session, the GUI, or the entry points.
Reminder text is displayed, never logged: a log line here names only ids,
counts, channels, and fixed phrases, or an exception's type.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from core.constants import REMINDER_NOTICE_LIMIT, REMINDER_OVERDUE_AFTER_SECONDS
from core.context import quoted_for_owner
from core.logger import get_logger
from core.task_records import (
    DELIVERY_CHANNEL_MESSAGE,
    DELIVERY_OUTCOME_DISPLAYED,
    DELIVERY_OUTCOME_FAILED,
    DELIVERY_RECORD_MESSAGE,
    TASK_TIMESTAMP_FORMAT,
    ClaimOutcome,
    ClaimResult,
    DeliveryCandidate,
    DeliveryClaim,
    DeliverySetting,
    DeliveryStorageError,
    describe_remind_time,
    is_delivery_channel,
)


# The message box's title: one reminder, or several.
NOTICE_TITLE = "Maria reminder"
NOTICE_TITLE_SEVERAL = "Maria reminders"

# The line a notice ends with when a due reminder was left unclaimed.
MORE_DUE_LINE = "More reminders are due. Type due reminders in Maria to see them."

# How many due candidates a check reads: one notice's worth, and the one more
# that shows whether more are due.
CANDIDATE_READ_LIMIT = REMINDER_NOTICE_LIMIT + 1

# Why a check needs an aware instant. Fixed text.
NOTICE_INSTANT_MESSAGE = "A reminder check needs an aware UTC instant."

_LOGGER = get_logger(__name__)


class DeliveryStore(Protocol):
    """The storage a check claims through: the memory layer's delivery API."""

    def reminder_delivery_setting(self) -> DeliverySetting:
        """Return the delivery setting."""

    def reminder_delivery_candidates(
        self,
        as_of: str,
        limit: int,
    ) -> list[DeliveryCandidate]:
        """Return due, unclaimed reminders, oldest first."""

    def claim_reminder_delivery(
        self,
        candidate: DeliveryCandidate,
        channel: str,
        claimed_at: str,
        as_of: str,
    ) -> ClaimResult:
        """Commit one claim, or say why it was not claimed."""

    def record_reminder_delivery(self, claim: DeliveryClaim, outcome: str) -> bool:
        """Write one claim's outcome."""


class ClaimStop(str, Enum):
    """Why a check stopped claiming before its last candidate.

    ``DISABLED``: a claim found delivery off, so nothing more was claimed.
    ``STORAGE``: a claim raised a storage failure. ``UNEXPECTED``: a claim
    raised any other ``Exception``. Every claim retained before the stop is
    still displayed.
    """

    DISABLED = "disabled"
    STORAGE = "storage"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True, repr=False, eq=False)
class ReminderNotice:
    """One notice for exactly the claims one check retained.

    ``entries`` are the rendered lines of each claimed reminder, oldest
    first, and ``more_due`` says a due reminder was left unclaimed. The
    claims' identities travel with the notice so its outcomes can be
    written for exactly them; a surface shows only ``title`` and ``text``.
    Its ``repr`` shows neither the text nor the claims.
    """

    entries: tuple[str, ...]
    more_due: bool
    claims: tuple[DeliveryClaim, ...]

    def __post_init__(self) -> None:
        """Refuse a notice that does not pair each entry with one claim."""
        if not (
            type(self.entries) is tuple
            and type(self.claims) is tuple
            and type(self.more_due) is bool
            and self.entries
            and len(self.entries) == len(self.claims)
            and all(type(entry) is str for entry in self.entries)
            and all(type(claim) is DeliveryClaim for claim in self.claims)
        ):
            raise ValueError(DELIVERY_RECORD_MESSAGE)

    @property
    def title(self) -> str:
        """Return the title a message box shows."""
        return NOTICE_TITLE if len(self.entries) == 1 else NOTICE_TITLE_SEVERAL

    @property
    def text(self) -> str:
        """Return the notice as the owner reads it: its entries, and more.

        Joining the entries is part of each surface's display step.
        """
        blocks = list(self.entries)
        if self.more_due:
            blocks.append(MORE_DUE_LINE)
        return "\n\n".join(blocks)

    @property
    def reminder_ids(self) -> tuple[int, ...]:
        """Return the ids of the claimed reminders, in notice order."""
        return tuple(claim.reminder_id for claim in self.claims)

    def __repr__(self) -> str:
        """Name the type without showing the text or the claims."""
        return "ReminderNotice(<redacted>)"


@dataclass(frozen=True)
class RecordFailures:
    """How many of one notice's outcome writes failed, by kind.

    ``storage`` counts writes that raised ``DeliveryStorageError``, and
    ``unexpected`` those that raised any other ``Exception``, so the
    closed-app pass can report each with its own exit status. Every failed
    write leaves its own claim unconfirmed.
    """

    storage: int = 0
    unexpected: int = 0


@dataclass(frozen=True)
class ClaimBatch:
    """What one check claimed: its notice, if any, and why it stopped early.

    ``notice`` holds exactly the retained claims, or is None when none was
    retained. ``stopped`` is None when the check ran to its last candidate,
    and ``failure`` names the exception type that stopped it, when one did.
    """

    notice: ReminderNotice | None = None
    stopped: ClaimStop | None = None
    failure: str | None = None


def instant_text(moment: datetime) -> str:
    """Return an aware instant in Maria's UTC form, truncated to the second."""
    if type(moment) is not datetime or moment.utcoffset() is None:
        raise ValueError(NOTICE_INSTANT_MESSAGE)
    return moment.astimezone(timezone.utc).strftime(TASK_TIMESTAMP_FORMAT)


def notice_entry(candidate: DeliveryCandidate, as_of: str) -> str:
    """Render one due reminder's notice lines, from its saved values.

    The first line says whether it is due or overdue, by more than the
    overdue window after its saved instant, and quotes its text as the
    reminders tool does; the second describes its saved time as the tool
    does. Nothing is resolved again from the zone or its current rules.
    """
    if type(candidate) is not DeliveryCandidate:
        raise ValueError(DELIVERY_RECORD_MESSAGE)
    late = _parsed(as_of) - _parsed(candidate.remind_utc)
    label = (
        "overdue"
        if late.total_seconds() > REMINDER_OVERDUE_AFTER_SECONDS
        else "due"
    )
    return (
        f"Reminder {candidate.reminder_id} is {label}: "
        f"{quoted_for_owner(candidate.text)}\n"
        f"{describe_remind_time(candidate.remind_time, candidate.remind_utc)}"
    )


def claim_notice(
    store: DeliveryStore,
    now: datetime,
    channel: str,
) -> ClaimBatch:
    """Claim the due reminders one check may show, and package one notice.

    The setting is read first; with delivery not on, nothing else is read
    and nothing is claimed. Otherwise at most six due candidates are read,
    the first five are rendered before any claim, and those five are claimed
    one at a time, oldest first. A candidate that no longer qualifies is
    skipped; like a sixth candidate, it is a due reminder left unclaimed, so
    a notice then says more are due. A claim that finds delivery off stops
    the batch, and so does one that raises: every claim whose call returned
    ``CLAIMED`` is retained, and the stop is logged by exception type alone.
    With nothing retained there is no notice. The setting read, the
    candidate read, rendering, and the packaging of the retained claims into
    the notice catch nothing, so their failures reach the caller; a
    packaging failure leaves the retained claims unconfirmed.
    """
    if not is_delivery_channel(channel):
        raise ValueError(DELIVERY_CHANNEL_MESSAGE)
    as_of = instant_text(now)
    if not store.reminder_delivery_setting().enabled:
        return ClaimBatch()
    candidates = store.reminder_delivery_candidates(as_of, CANDIDATE_READ_LIMIT)
    if not candidates:
        return ClaimBatch()
    batch = candidates[:REMINDER_NOTICE_LIMIT]
    entries = [notice_entry(candidate, as_of) for candidate in batch]
    retained_entries: list[str] = []
    retained_claims: list[DeliveryClaim] = []
    stopped: ClaimStop | None = None
    failure: str | None = None
    unclaimed_due = len(candidates) > len(batch)
    for candidate, entry in zip(batch, entries):
        try:
            result = store.claim_reminder_delivery(candidate, channel, as_of, as_of)
        except DeliveryStorageError as error:
            stopped, failure = ClaimStop.STORAGE, type(error).__name__
        except Exception as error:  # noqa: BLE001 - the batch's boundary.
            stopped, failure = ClaimStop.UNEXPECTED, type(error).__name__
        else:
            if type(result) is ClaimResult and result.outcome is ClaimOutcome.CLAIMED:
                retained_entries.append(entry)
                retained_claims.append(result.claim)
                continue
            if type(result) is ClaimResult and result.outcome is ClaimOutcome.CHANGED:
                # It was due when read and is left unclaimed, so the notice
                # says more are due; the next check reads it afresh.
                unclaimed_due = True
                continue
            stopped = (
                ClaimStop.DISABLED
                if type(result) is ClaimResult
                else ClaimStop.UNEXPECTED
            )
        unclaimed_due = True
        break
    if stopped is ClaimStop.DISABLED:
        _LOGGER.info("Stopped claiming reminders: delivery is not on.")
    elif stopped is not None:
        _LOGGER.error(
            "Stopped claiming reminders after a %s.",
            failure or "malformed claim result",
        )
    if not retained_claims:
        return ClaimBatch(None, stopped, failure)
    _LOGGER.info(
        "Claimed %s reminder(s) for one %s notice: %s.",
        len(retained_claims),
        channel,
        ", ".join(str(claim.reminder_id) for claim in retained_claims),
    )
    notice = ReminderNotice(
        tuple(retained_entries),
        unclaimed_due,
        tuple(retained_claims),
    )
    return ClaimBatch(notice, stopped, failure)


def record_notice(
    store: DeliveryStore,
    notice: ReminderNotice,
    displayed: bool,
) -> RecordFailures:
    """Write each claim's outcome in its own write; say which writes failed.

    ``displayed`` True records every claim of the notice as displayed, and
    False as not displayed. A write that raises leaves its own claim
    unconfirmed, is logged by exception type, and does not stop the others;
    a storage failure and any other ``Exception`` are counted apart.
    """
    if type(notice) is not ReminderNotice or type(displayed) is not bool:
        raise ValueError(DELIVERY_RECORD_MESSAGE)
    outcome = DELIVERY_OUTCOME_DISPLAYED if displayed else DELIVERY_OUTCOME_FAILED
    storage = unexpected = 0
    for claim in notice.claims:
        try:
            store.record_reminder_delivery(claim, outcome)
        except Exception as error:  # noqa: BLE001 - one claim's write only.
            if isinstance(error, DeliveryStorageError):
                storage += 1
            else:
                unexpected += 1
            _LOGGER.error(
                "Could not record the delivery of reminder %s: %s.",
                claim.reminder_id,
                type(error).__name__,
            )
    return RecordFailures(storage, unexpected)


class ReminderDeliverySeam:
    """The session's delivery seam: one store, one channel, and a clock.

    An interface's session holds one, built by the composition point, and
    calls it only through ``MariaSession.claim_due_reminders`` and
    ``record_reminder_notice``. Each check reads the clock once, through the
    injected clock, which must return an aware instant.
    """

    def __init__(
        self,
        store: DeliveryStore,
        channel: str,
        clock: Callable[[], datetime],
    ) -> None:
        if not is_delivery_channel(channel):
            raise ValueError(DELIVERY_CHANNEL_MESSAGE)
        self._store = store
        self._channel = channel
        self._clock = clock

    @property
    def channel(self) -> str:
        """Return the surface this seam claims for."""
        return self._channel

    def claim(self) -> ClaimBatch:
        """Run one check now, through ``claim_notice``."""
        return claim_notice(self._store, self._clock(), self._channel)

    def record(self, notice: ReminderNotice, displayed: bool) -> RecordFailures:
        """Record one notice's outcome, through ``record_notice``."""
        return record_notice(self._store, notice, displayed)

    def __repr__(self) -> str:
        """Name the type and channel only."""
        return f"ReminderDeliverySeam(channel={self._channel!r})"


def _parsed(instant: str) -> datetime:
    """Return one canonical UTC instant as an aware datetime."""
    return datetime.strptime(instant, TASK_TIMESTAMP_FORMAT).replace(
        tzinfo=timezone.utc
    )
