"""Saved conversations and conversation summaries: their shared vocabulary.

A saved conversation is an exact, immutable copy of the exchanges one session
retained when the owner asked to keep them. A conversation summary is text
Maria's local reasoning model drafted from those exchanges, stored only after
the owner approved exactly that text, and editable by the owner afterwards.
The memory layer stores both, the conversation module produces what a save or
a draft hands to a tool, and the two tools show them; this module is the one
place all of them take their limits, timestamp form, transcript form, value
objects, and fixed messages from.

A saved conversation is one canonical text, a compact JSON array of
``[user, assistant]`` pairs, so it is stored, read, and deleted as one row.
Its text is the exchanges' text and nothing else: no notice, context item,
provenance, or command result can be part of it. A summary is one text with
no title, at most ``CONVERSATION_SUMMARY_MAX_BYTES`` UTF-8 bytes, stored
exactly as the owner was shown it, so it never begins or ends with
whitespace.

Validation is strict and runs in both directions: the memory layer checks
every value before writing it and again after reading it, and a value it
cannot use is refused with a fixed message that repeats nothing. This module
reads nothing, stores nothing, and logs nothing, and no value object here
shows its text in a ``repr``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone


# The most exchanges, and the most UTF-8 bytes of their text, one saved
# conversation holds: exactly what a session may retain, so every retained
# conversation can be saved whole. A test holds these to the conversation
# module's own limits.
SAVED_CONVERSATION_MAX_EXCHANGES = 8
SAVED_CONVERSATION_MAX_BYTES = 8 * 1024

# The longest canonical transcript, in characters. JSON escapes a control
# character in six characters, so the text of 8 KiB of exchanges never
# encodes to more than about 48 KiB; this ceiling only bounds what is ever
# parsed, and the exact checks are the ones above.
SAVED_CONVERSATION_MAX_CHARACTERS = 64 * 1024

# The longest conversation summary, in UTF-8 bytes. It equals the largest
# text a context read may hand over, so a stored summary is never too large
# to be considered as context, though that never guarantees it is selected.
CONVERSATION_SUMMARY_MAX_BYTES = 2 * 1024

# How many records a listing shows, and the one more it reads to learn
# whether more exist without counting them.
RECORD_LIST_LIMIT = 20
RECORD_QUERY_LIMIT = RECORD_LIST_LIMIT + 1

# Maria's one timestamp form for these records: UTC to the second with a
# literal Z, always exactly this long.
RECORD_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_RECORD_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)

# The largest value SQLite can store as an INTEGER, and so the largest id a
# row can have. A larger id names no record.
RECORD_MAXIMUM_ID = 2**63 - 1

# Fixed messages. None interpolates anything supplied at run time.
SAVED_CONVERSATION_ID_MESSAGE = (
    "Saved conversation id must be a positive whole number."
)
CONVERSATION_SUMMARY_ID_MESSAGE = (
    "Conversation summary id must be a positive whole number."
)
TRANSCRIPT_MESSAGE = (
    "A saved conversation must hold 1 to "
    f"{SAVED_CONVERSATION_MAX_EXCHANGES} exchanges of non-empty text, at most "
    f"{SAVED_CONVERSATION_MAX_BYTES} UTF-8 bytes in all, in Maria's canonical "
    "form."
)
SUMMARY_TEXT_MESSAGE = (
    "A conversation summary must be non-empty text of at most "
    f"{CONVERSATION_SUMMARY_MAX_BYTES} UTF-8 bytes, with no leading or "
    "trailing whitespace."
)
RECORD_MESSAGE = (
    "A saved conversation or conversation summary may hold only values Maria "
    "can show."
)
RECORD_LIST_LIMIT_MESSAGE = (
    "A conversation record listing limit must be a whole number from 1 to "
    f"{RECORD_QUERY_LIMIT}."
)


@dataclass(frozen=True, repr=False)
class SavedConversation:
    """One saved conversation, every field validated.

    ``exchanges`` holds the saved ``(user, assistant)`` pairs, oldest first,
    exactly as the session retained them.
    """

    conversation_id: int
    created_at: str
    exchanges: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        """Refuse any field Maria could not have written."""
        if not (
            is_record_id(self.conversation_id)
            and is_record_timestamp(self.created_at)
            and _is_exchange_tuple(self.exchanges)
        ):
            raise ValueError(RECORD_MESSAGE)

    def __repr__(self) -> str:
        """Name the type and id without showing any exchange."""
        return f"SavedConversation(id={self.conversation_id}, <redacted>)"


@dataclass(frozen=True, repr=False)
class ConversationSummary:
    """One conversation summary, every field validated.

    ``context_enabled`` is True only while the owner's consent covers exactly
    this body, so a summary changed outside Maria reads as not enabled.
    """

    summary_id: int
    body: str
    created_at: str
    updated_at: str
    context_enabled: bool

    def __post_init__(self) -> None:
        """Refuse any field Maria could not have written."""
        if not (
            is_record_id(self.summary_id)
            and is_summary_text(self.body)
            and is_record_timestamp(self.created_at)
            and is_record_timestamp(self.updated_at)
            and type(self.context_enabled) is bool
        ):
            raise ValueError(RECORD_MESSAGE)

    def __repr__(self) -> str:
        """Name the type and id without showing the body."""
        return f"ConversationSummary(id={self.summary_id}, <redacted>)"


def encode_transcript(exchanges: Sequence[tuple[str, str]]) -> str:
    """Return the canonical transcript of retained exchanges.

    ``exchanges`` are ``(user, assistant)`` pairs, oldest first: 1 to
    ``SAVED_CONVERSATION_MAX_EXCHANGES`` of them, each text exact, non-empty,
    and encodable, at most ``SAVED_CONVERSATION_MAX_BYTES`` UTF-8 bytes in
    all. The result is one compact JSON array of two-element arrays, with
    non-ASCII text kept as itself, so the same exchanges always give the
    same text. Anything else raises ``ValueError`` with the fixed message.
    """
    pairs = _exchange_pairs(exchanges)
    if pairs is None:
        raise ValueError(TRANSCRIPT_MESSAGE)
    return _canonical(pairs)


def decode_transcript(text: object) -> tuple[tuple[str, str], ...] | None:
    """Return a canonical transcript's exchanges, or None if it is not one.

    The text must be exactly what ``encode_transcript`` writes for valid
    exchanges: a stored text in any other form, including the same
    exchanges spelled with different escapes or whitespace, is not a
    transcript. Text longer than ``SAVED_CONVERSATION_MAX_CHARACTERS`` is
    refused before it is parsed.
    """
    if type(text) is not str or not 0 < len(text) <= SAVED_CONVERSATION_MAX_CHARACTERS:
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if type(parsed) is not list:
        return None
    pairs = _exchange_pairs(
        tuple(
            tuple(item) if type(item) is list else item
            for item in parsed
        )
    )
    if pairs is None or _canonical(pairs) != text:
        return None
    return pairs


def is_summary_text(text: object) -> bool:
    """Return whether a value can be stored as a conversation summary.

    That is exact, encodable, non-empty text of at most
    ``CONVERSATION_SUMMARY_MAX_BYTES`` UTF-8 bytes that equals its own
    stripped form, so what is stored is always exactly what was shown.
    """
    return (
        type(text) is str
        and bool(text)
        and text == text.strip()
        and _utf8_size(text, CONVERSATION_SUMMARY_MAX_BYTES) is not None
    )


def is_record_id(value: object) -> bool:
    """Return whether a value is an exact positive int, never a bool."""
    return type(value) is int and value >= 1


def record_timestamp() -> str:
    """Return the current UTC time in Maria's record timestamp form."""
    return datetime.now(timezone.utc).strftime(RECORD_TIMESTAMP_FORMAT)


def is_record_timestamp(value: object) -> bool:
    """Return whether a value is exactly one valid record timestamp.

    It must be exact text in the one form ``record_timestamp`` writes, with
    every field at its fixed width, naming a real calendar second.
    """
    if type(value) is not str or not _RECORD_TIMESTAMP_PATTERN.fullmatch(value):
        return False
    try:
        datetime.strptime(value, RECORD_TIMESTAMP_FORMAT)
    except ValueError:
        return False
    return True


def _exchange_pairs(exchanges: object) -> tuple[tuple[str, str], ...] | None:
    """Return valid exchanges as a tuple of pairs, or None.

    The exchanges must be a sequence, other than text, of 1 to
    ``SAVED_CONVERSATION_MAX_EXCHANGES`` two-element pairs of exact,
    non-empty, encodable text, at most ``SAVED_CONVERSATION_MAX_BYTES``
    UTF-8 bytes in all.
    """
    if not isinstance(exchanges, Sequence) or isinstance(exchanges, (str, bytes)):
        return None
    if not 1 <= len(exchanges) <= SAVED_CONVERSATION_MAX_EXCHANGES:
        return None
    pairs: list[tuple[str, str]] = []
    total = 0
    for exchange in exchanges:
        if type(exchange) is not tuple or len(exchange) != 2:
            return None
        for text in exchange:
            if type(text) is not str or not text:
                return None
            size = _utf8_size(text, SAVED_CONVERSATION_MAX_BYTES - total)
            if size is None:
                return None
            total += size
        pairs.append((exchange[0], exchange[1]))
    return tuple(pairs)


def _is_exchange_tuple(value: object) -> bool:
    """Return whether a value is already an exact tuple of valid pairs."""
    return type(value) is tuple and _exchange_pairs(value) == value


def _canonical(pairs: tuple[tuple[str, str], ...]) -> str:
    """Return the one canonical JSON text of valid exchange pairs."""
    return json.dumps(
        [[user, assistant] for user, assistant in pairs],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _utf8_size(text: str, maximum_bytes: int) -> int | None:
    """Return text's UTF-8 size when it is encodable and within the bound.

    Text with more characters than the bound is over it and is never
    encoded, so text of any length is never copied to be measured. None
    means the text cannot be encoded or is over the bound.
    """
    if maximum_bytes < 0 or len(text) > maximum_bytes:
        return None
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    return size if size <= maximum_bytes else None
