"""The four supported natural-language requests that create a task or reminder.

V1-22 lets the owner ask for a task or reminder in their own words, but only
in four closed English forms, so a model's proposal to create one compiles
only when Maria's own deterministic parse of the owner's request already
contains it. The model never decides whether the owner asked. This leaf
module is that parse: it prepares the cleaned request, splits it into items
at a closed joiner, matches each item against the forms, extracts each body
and timing phrase exactly as the owner wrote them, and refuses a body that
carries a guarded token. It reads no clock and logs nothing; the compiler
resolves each item's own timing phrase afterwards, through
``core.time_resolution``.

The forms, each matched whole:

- F1 ``remind me <timing> to <body>``;
- F2 ``remind me to <body> <timing>``, the timing ending the item;
- F3 ``add a task to <body>``;
- F4 ``add a task to <body> by <date>``, the date ending the item.

The word after ``remind me `` picks F1 or F2: ``to`` means F2. A final
`` by `` followed by a date picks F4. Keywords, timing tokens, and joiners
match ASCII case-insensitively and are separated by exactly one space; a
body keeps the owner's own characters, with only its surrounding whitespace
stripped. A request holding a control, format, surrogate, or separator
character, one that is a question, and one that does not begin with a form
prefix are not creation requests at all. One that begins with a prefix but
does not complete a form, names unsupported or vague timing, has a body
holding a guarded token, or names more than five items is unclear. Both
refusals are fixed; nothing is guessed or repaired.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from core.time_resolution import (
    DateSpec,
    ReminderTiming,
    parse_reminder_phrase,
    parse_task_phrase,
)


# The most items one request may name: a plan holds at most five steps, and
# a test holds this to ``core.planner.PLAN_MAX_STEPS``.
CREATION_REQUEST_MAX_ITEMS = 5

# The longest body, in UTF-8 bytes: the stored text's own limit, which a test
# holds to ``core.task_records.TASK_TEXT_MAX_BYTES``.
CREATION_BODY_MAX_BYTES = 512

# The fixed refusal for text a new task or reminder cannot hold, worded as
# ``core.task_records`` words it, which a test holds this to.
CREATION_TEXT_MESSAGE = (
    "Task and reminder text must be one line of 1 to "
    f"{CREATION_BODY_MAX_BYTES} UTF-8 bytes, with no leading or trailing "
    "whitespace and no control or formatting characters."
)

# The two form prefixes, each followed by exactly one space.
REMINDER_PREFIX = "remind me "
TASK_PREFIX = "add a task to "

# Unicode categories that make a request unsupported outright: control,
# format, surrogate, line separator, and paragraph separator.
UNSAFE_TEXT_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

# Items are split only at `` and `` or ``, and `` immediately followed by a
# form prefix; the comma belongs to the joiner, never to a body.
_JOINER = re.compile(
    r"(?:, and | and )(?=remind me |add a task to )",
    re.ASCII | re.IGNORECASE,
)

# The closed body guards. A body holding any of them makes the whole request
# unclear, so a second request, an intended time, a retraction, a question,
# a quotation, or an example can never travel inside a body. The quotation
# marks are the plain one and the two typographic ones.
GUARDED_CHARACTERS = frozenset({"?", "!", ";", '"', chr(0x201C), chr(0x201D)})
SENTENCE_BREAK = ". "
GUARDED_PREFIXES = ("remind me", "add a task to")
TIMING_WORDS = frozenset(
    {
        "today",
        "tonight",
        "tomorrow",
        "yesterday",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "morning",
        "afternoon",
        "evening",
        "noon",
        "midnight",
        "week",
        "weekend",
        "month",
        "year",
        "next",
        "later",
        "soon",
        "by",
        "due",
        "before",
        "until",
        "deadline",
    }
)
TIMING_UNITS = frozenset(
    {
        "minute",
        "minutes",
        "hour",
        "hours",
        "day",
        "days",
        "week",
        "weeks",
        "month",
        "months",
        "year",
        "years",
    }
)
GUARDED_PHRASES = (
    ("never", "mind"),
    ("nevermind",),
    ("forget", "it"),
    ("forget", "that"),
    ("cancel", "that"),
    ("scratch", "that"),
    ("don't",),
    ("dont",),
    ("do", "not"),
    ("not", "sure"),
    ("maybe",),
    ("perhaps",),
    ("just", "kidding"),
    ("for", "example"),
    ("for", "instance"),
)
_TIMING_TOKEN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"|[0-9]{1,2}:[0-9]{2}"
    r"|[0-9]{1,2}(?::[0-9]{2})?(?:am|pm)"
)
_DIGITS = re.compile(r"[0-9]+")

# A typographic apostrophe counts as the plain one in a guarded phrase.
_APOSTROPHES = str.maketrans({chr(0x2019): "'"})

# ASCII-only lowercasing, which keeps every other character and every index.
_ASCII_LOWER = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)


class RequestRefusal(str, Enum):
    """Why a request cannot create a task or reminder.

    ``NOT_A_CREATION_REQUEST`` is a request that is not one of the supported
    forms at all; ``UNCLEAR`` is one that begins as a form but does not
    complete it, or that a guard or a limit refuses.
    """

    NOT_A_CREATION_REQUEST = "not_a_creation_request"
    UNCLEAR = "unclear"


class CreationKind(str, Enum):
    """What one requested item creates."""

    TASK = "task"
    REMINDER = "reminder"


@dataclass(frozen=True, repr=False)
class CreationItem:
    """One task or reminder the owner's request names, exactly as written.

    ``body`` is the item's text and ``phrase`` its timing phrase, each the
    owner's own characters; an undated task has no phrase. ``due`` is a
    dated task's parsed date and ``timing`` a reminder's parsed timing,
    neither resolved yet. Nothing is shown by ``repr``.
    """

    kind: CreationKind
    body: str
    phrase: str | None = None
    due: DateSpec | None = None
    timing: ReminderTiming | None = None

    def __repr__(self) -> str:
        """Name the type and kind without showing the body or the phrase."""
        return f"CreationItem({self.kind.value}, <redacted>)"


def parse_creation_request(
    request: object,
) -> tuple[CreationItem, ...] | RequestRefusal:
    """Return the one to five items a supported request names, or a refusal.

    ``request`` is the owner's cleaned request. It is prepared in order: a
    control, format, surrogate, or separator character anywhere makes it
    unsupported; one trailing ``.`` is removed; and a request that then ends
    with ``?`` is a question and unsupported. It must then begin with a
    form prefix. It is split into items at the closed joiner, and every item
    must match a form whole, with a non-empty body of at most 512 UTF-8
    bytes that holds no guarded token.
    """
    if type(request) is not str or any(
        unicodedata.category(character) in UNSAFE_TEXT_CATEGORIES
        for character in request
    ):
        return RequestRefusal.NOT_A_CREATION_REQUEST
    prepared = request[:-1] if request.endswith(".") else request
    if prepared.endswith("?"):
        return RequestRefusal.NOT_A_CREATION_REQUEST
    lowered = ascii_lowered(prepared)
    if not lowered.startswith((REMINDER_PREFIX, TASK_PREFIX)):
        if lowered in (REMINDER_PREFIX.rstrip(), TASK_PREFIX.rstrip()):
            return RequestRefusal.UNCLEAR
        return RequestRefusal.NOT_A_CREATION_REQUEST

    segments = _segments(prepared)
    if len(segments) > CREATION_REQUEST_MAX_ITEMS:
        return RequestRefusal.UNCLEAR
    items: list[CreationItem] = []
    for segment in segments:
        item = _item(segment)
        if item is None:
            return RequestRefusal.UNCLEAR
        items.append(item)
    return tuple(items)


def is_creation_text(value: object) -> bool:
    """Return whether a value can be a new task's or reminder's text.

    That is exact text of 1 to ``CREATION_BODY_MAX_BYTES`` UTF-8 bytes that
    equals its own stripped form and holds no control, format, surrogate,
    or separator character: the rule ``core.task_records`` stores text by,
    which a test holds this one to. The deterministic commands check it
    before a confirmation is offered, and every request-form body meets it.
    """
    if (
        type(value) is not str
        or not value
        or len(value) > CREATION_BODY_MAX_BYTES
        or value != value.strip()
        or any(
            unicodedata.category(character) in UNSAFE_TEXT_CATEGORIES
            for character in value
        )
    ):
        return False
    return len(value.encode("utf-8")) <= CREATION_BODY_MAX_BYTES


def canonical_phrase(value: str) -> str:
    """Return a timing phrase as the binding compares it.

    This is the amendment's canonical tokenization: the phrase is split into
    its tokens at spaces, ASCII letters are lowercased, and the tokens are
    joined by single spaces. Digits and every other character stay exactly
    as written, so ``Tomorrow  at 9:00`` and ``tomorrow at 9:00`` agree,
    while ``tomorrow at 09:00`` does not.
    """
    return " ".join(token for token in ascii_lowered(value).split(" ") if token)


def ascii_lowered(value: str) -> str:
    """Return text with only its ASCII letters lowercased."""
    return value.translate(_ASCII_LOWER)


def body_is_guarded(body: str) -> bool:
    """Return whether a body holds any of the closed guards.

    Punctuation, a sentence break, and a form prefix are found anywhere in
    the body. Timing words, timing tokens, and guarded phrases are found
    among its words: the body is casefolded, split on whitespace, and each
    word loses its non-alphanumeric characters at both ends, the V1-16 rule.
    """
    if any(character in GUARDED_CHARACTERS for character in body):
        return True
    if SENTENCE_BREAK in body:
        return True
    lowered = ascii_lowered(body)
    if any(prefix in lowered for prefix in GUARDED_PREFIXES):
        return True
    words = [
        word
        for word in (
            _without_outer_punctuation(token).translate(_APOSTROPHES)
            for token in body.casefold().split()
        )
        if word
    ]
    for index, word in enumerate(words):
        if word in TIMING_WORDS or _TIMING_TOKEN.fullmatch(word):
            return True
        following = words[index + 1 : index + 3]
        if (
            _DIGITS.fullmatch(word)
            and following[:1]
            and following[0] in ("am", "pm")
        ):
            return True
        if (
            word == "in"
            and len(following) == 2
            and _DIGITS.fullmatch(following[0])
            and following[1] in TIMING_UNITS
        ):
            return True
        for phrase in GUARDED_PHRASES:
            if tuple(words[index : index + len(phrase)]) == phrase:
                return True
    return False


def _segments(prepared: str) -> list[str]:
    """Split a prepared request into its items at the closed joiner."""
    segments: list[str] = []
    start = 0
    for joiner in _JOINER.finditer(prepared):
        segments.append(prepared[start : joiner.start()])
        start = joiner.end()
    segments.append(prepared[start:])
    return segments


def _item(segment: str) -> CreationItem | None:
    """Return the item one segment names, or None when no form matches it."""
    lowered = ascii_lowered(segment)
    if lowered.startswith(REMINDER_PREFIX):
        rest = segment[len(REMINDER_PREFIX) :]
        if ascii_lowered(rest.split(" ", 1)[0]) == "to":
            return _reminder_with_suffix(rest[len("to ") :])
        return _reminder_with_prefix(rest)
    if lowered.startswith(TASK_PREFIX):
        return _task(segment[len(TASK_PREFIX) :])
    return None


def _reminder_with_prefix(rest: str) -> CreationItem | None:
    """Match F1, ``<timing> to <body>``, the timing read from the start.

    A timing phrase never contains `` to ``, so the first `` to `` is the
    only place the timing can end.
    """
    boundary = ascii_lowered(rest).find(" to ")
    if boundary < 0:
        return None
    phrase = rest[:boundary]
    timing = parse_reminder_phrase(phrase)
    if timing is None:
        return None
    return _bodied(
        CreationKind.REMINDER,
        rest[boundary + len(" to ") :],
        phrase,
        timing=timing,
    )


def _reminder_with_suffix(rest: str) -> CreationItem | None:
    """Match F2, ``<body> <timing>``, the timing ending the item.

    The shortest suffix after a space that is a whole timing phrase is the
    timing: one ending in a minute or hour unit can only be ``in N <unit>``,
    and one ending in a time is fixed by the token before ``at``, so at most
    one suffix can match.
    """
    position = len(rest)
    while True:
        position = rest.rfind(" ", 0, position)
        if position < 0:
            return None
        phrase = rest[position + 1 :]
        timing = parse_reminder_phrase(phrase)
        if timing is not None:
            return _bodied(
                CreationKind.REMINDER,
                rest[:position],
                phrase,
                timing=timing,
            )


def _task(rest: str) -> CreationItem | None:
    """Match F4 when a final `` by `` and a date end the item, else F3."""
    boundary = ascii_lowered(rest).rfind(" by ")
    if boundary >= 0:
        phrase = rest[boundary + len(" by ") :]
        due = parse_task_phrase(phrase)
        if due is not None:
            return _bodied(CreationKind.TASK, rest[:boundary], phrase, due=due)
    return _bodied(CreationKind.TASK, rest, None)


def _bodied(
    kind: CreationKind,
    body: str,
    phrase: str | None,
    *,
    due: DateSpec | None = None,
    timing: ReminderTiming | None = None,
) -> CreationItem | None:
    """Return the item with its stripped body, or None when the body fails."""
    body = body.strip()
    if not is_creation_text(body) or body_is_guarded(body):
        return None
    return CreationItem(kind, body, phrase, due, timing)


def _without_outer_punctuation(word: str) -> str:
    """Return a word without the non-alphanumeric characters at its ends."""
    start, end = 0, len(word)
    while start < end and not word[start].isalnum():
        start += 1
    while end > start and not word[end - 1].isalnum():
        end -= 1
    return word[start:end]
