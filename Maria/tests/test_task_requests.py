"""Focused tests for V1-22's request forms, ``core/task_requests.py``.

The parse reads no clock and resolves nothing. It decides only whether a
cleaned request is one to five items in the four closed forms, and extracts
each item's body and timing phrase exactly as the owner wrote them; every
refusal is one of two fixed kinds. Resolution, the binding to a proposal,
and the duplicate rule belong to the compiler and are tested with it.
"""

from __future__ import annotations

import ast
import inspect
import unittest
from datetime import date, time, timedelta

from core import task_requests
from core.task_requests import (
    CREATION_BODY_MAX_BYTES,
    CREATION_REQUEST_MAX_ITEMS,
    GUARDED_PHRASES,
    TIMING_UNITS,
    TIMING_WORDS,
    CreationItem,
    CreationKind,
    RequestRefusal,
    body_is_guarded,
    canonical_phrase,
    is_creation_text,
    parse_creation_request,
)
from core.time_resolution import DateKind, DateSpec, ReminderTiming


NOT_A_REQUEST = RequestRefusal.NOT_A_CREATION_REQUEST
UNCLEAR = RequestRefusal.UNCLEAR
TASK = CreationKind.TASK
REMINDER = CreationKind.REMINDER

# Characters built from code points, so no tool or editor can change them.
RIGHT_SINGLE_QUOTE = chr(0x2019)
LEFT_DOUBLE_QUOTE = chr(0x201C)
RIGHT_DOUBLE_QUOTE = chr(0x201D)
LINE_SEPARATOR = chr(0x2028)
PARAGRAPH_SEPARATOR = chr(0x2029)
ZERO_WIDTH_SPACE = chr(0x200B)
LONE_SURROGATE = chr(0xD800)
ARABIC_INDIC_ONE = chr(0x0661)
FULLWIDTH_ONE = chr(0xFF11)
DOTLESS_I = chr(0x0131)
KELVIN_SIGN = chr(0x212A)
E_ACUTE = chr(0x00E9)
TWO_BYTE = chr(0x00E9)
FOUR_BYTE = chr(0x1F600)


def _items(test: unittest.TestCase, request: str) -> tuple[CreationItem, ...]:
    result = parse_creation_request(request)
    test.assertIsInstance(result, tuple, msg=f"refused: {request!r}")
    return result


def _shapes(items: tuple[CreationItem, ...]) -> list[tuple[object, ...]]:
    return [(item.kind, item.body, item.phrase) for item in items]


class FormTests(unittest.TestCase):
    """Prove each of the four forms extracts its body and phrase exactly."""

    def test_f1_reads_its_timing_before_the_body(self) -> None:
        cases = {
            "remind me tomorrow at 9:00 to call mom": ("call mom", "tomorrow at 9:00"),
            "remind me today at 23:59 to lock the door": (
                "lock the door",
                "today at 23:59",
            ),
            "remind me on friday at 18:30 to water the plants": (
                "water the plants",
                "on friday at 18:30",
            ),
            "remind me on 2027-03-01 at 07:05 to renew the passport": (
                "renew the passport",
                "on 2027-03-01 at 07:05",
            ),
            "remind me in 3 days at 08:00 to call the bank": (
                "call the bank",
                "in 3 days at 08:00",
            ),
            "remind me in 1 day at 08:00 to call the bank": (
                "call the bank",
                "in 1 day at 08:00",
            ),
            "remind me in 20 minutes to check the oven": (
                "check the oven",
                "in 20 minutes",
            ),
            "remind me in 2 hours to stretch": ("stretch", "in 2 hours"),
            "remind me tomorrow at 02:30 +01:00 to check the clocks": (
                "check the clocks",
                "tomorrow at 02:30 +01:00",
            ),
        }
        for request, (body, phrase) in cases.items():
            with self.subTest(request=request):
                self.assertEqual(
                    _shapes(_items(self, request)),
                    [(REMINDER, body, phrase)],
                )

    def test_f1_parses_its_phrase_into_unresolved_timing(self) -> None:
        (item,) = _items(self, "remind me tomorrow at 9:00 to call mom")
        self.assertEqual(
            item.timing,
            ReminderTiming(day=DateSpec(DateKind.TOMORROW), clock=time(9, 0)),
        )
        self.assertIsNone(item.due)

        (item,) = _items(self, "remind me in 20 minutes to check the oven")
        self.assertEqual(item.timing, ReminderTiming(elapsed=timedelta(minutes=20)))

        (item,) = _items(self, "remind me on sunday at 02:30 -03:00 to check")
        self.assertEqual(
            item.timing,
            ReminderTiming(
                day=DateSpec(DateKind.WEEKDAY, weekday=6),
                clock=time(2, 30),
                offset=-timedelta(hours=3),
            ),
        )

    def test_f1_ends_its_timing_at_the_first_to(self) -> None:
        (item,) = _items(self, "remind me tomorrow at 9:00 to talk to the landlord")
        self.assertEqual(item.body, "talk to the landlord")
        self.assertEqual(item.phrase, "tomorrow at 9:00")

    def test_f2_reads_its_timing_from_the_end(self) -> None:
        cases = {
            "remind me to call mom tomorrow at 9:00": ("call mom", "tomorrow at 9:00"),
            "remind me to stretch in 20 minutes": ("stretch", "in 20 minutes"),
            "remind me to rest in 1 hour": ("rest", "in 1 hour"),
            "remind me to check the clocks on sunday at 02:30 +01:00": (
                "check the clocks",
                "on sunday at 02:30 +01:00",
            ),
            "remind me to talk to the landlord on 2027-05-04 at 17:45": (
                "talk to the landlord",
                "on 2027-05-04 at 17:45",
            ),
            "remind me to call the bank in 2 days at 08:00": (
                "call the bank",
                "in 2 days at 08:00",
            ),
        }
        for request, (body, phrase) in cases.items():
            with self.subTest(request=request):
                self.assertEqual(
                    _shapes(_items(self, request)),
                    [(REMINDER, body, phrase)],
                )

    def test_f3_is_an_undated_task(self) -> None:
        (item,) = _items(self, "add a task to buy milk")
        self.assertEqual((item.kind, item.body, item.phrase), (TASK, "buy milk", None))
        self.assertIsNone(item.due)
        self.assertIsNone(item.timing)

    def test_f4_reads_a_final_by_and_a_date(self) -> None:
        cases = {
            "add a task to file taxes by 2027-04-15": (
                "file taxes",
                "2027-04-15",
                DateSpec(DateKind.DATE, on=date(2027, 4, 15)),
            ),
            "add a task to clean the gutters by friday": (
                "clean the gutters",
                "friday",
                DateSpec(DateKind.WEEKDAY, weekday=4),
            ),
            "add a task to pay rent by tomorrow": (
                "pay rent",
                "tomorrow",
                DateSpec(DateKind.TOMORROW),
            ),
            "add a task to call back by today": (
                "call back",
                "today",
                DateSpec(DateKind.TODAY),
            ),
        }
        for request, (body, phrase, due) in cases.items():
            with self.subTest(request=request):
                (item,) = _items(self, request)
                self.assertEqual(
                    (item.kind, item.body, item.phrase, item.due),
                    (TASK, body, phrase, due),
                )
                self.assertIsNone(item.timing)

    def test_the_word_after_remind_me_picks_the_form(self) -> None:
        (item,) = _items(self, "remind me To call mom tomorrow at 9:00")
        self.assertEqual((item.body, item.phrase), ("call mom", "tomorrow at 9:00"))
        # With ``to`` first, only F2 is tried, so an F1-shaped rest is unclear.
        self.assertIs(
            parse_creation_request("remind me to tomorrow at 9:00 to call mom"),
            UNCLEAR,
        )

    def test_keywords_match_ascii_case_insensitively(self) -> None:
        (item,) = _items(self, "Remind Me Tomorrow At 9:00 To Call Mom")
        self.assertEqual((item.body, item.phrase), ("Call Mom", "Tomorrow At 9:00"))
        (item,) = _items(self, "ADD A TASK TO Buy Milk BY Friday")
        self.assertEqual((item.body, item.phrase), ("Buy Milk", "Friday"))

    def test_only_ascii_letters_fold_in_keywords(self) -> None:
        for request in (
            f"rem{DOTLESS_I}nd me tomorrow at 9:00 to call mom",
            f"add a tas{KELVIN_SIGN} to buy milk",
        ):
            with self.subTest(request=request):
                self.assertIs(parse_creation_request(request), NOT_A_REQUEST)

    def test_the_body_keeps_the_owners_characters(self) -> None:
        body = f"caf{E_ACUTE} r{E_ACUTE}union  prep"
        (item,) = _items(self, f"add a task to {body}")
        self.assertEqual(item.body, body)
        (item,) = _items(self, f"remind me tomorrow at 9:00 to   {body}  ")
        self.assertEqual(item.body, body)

    def test_keywords_are_separated_by_exactly_one_space(self) -> None:
        for request in (
            "remind me  tomorrow at 9:00 to call mom",
            "remind me tomorrow  at 9:00 to call mom",
            "remind me tomorrow at  9:00 to call mom",
            "remind me to call mom tomorrow at  9:00",
            "remind me in  20 minutes to stretch",
            "add a task to file taxes by  friday",
        ):
            with self.subTest(request=request):
                self.assertIs(parse_creation_request(request), UNCLEAR)


class PreparationTests(unittest.TestCase):
    """Prove the preparation rules applied before any form is matched."""

    def test_one_trailing_full_stop_is_removed(self) -> None:
        (item,) = _items(self, "add a task to buy milk.")
        self.assertEqual(item.body, "buy milk")
        (item,) = _items(self, "remind me tomorrow at 9:00 to call mom.")
        self.assertEqual(item.body, "call mom")
        (item,) = _items(self, "add a task to buy milk..")
        self.assertEqual(item.body, "buy milk.")

    def test_a_question_is_not_a_request(self) -> None:
        for request in (
            "remind me tomorrow at 09:00 to call mom?",
            "remind me tomorrow at 09:00 to call mom?.",
            "add a task to buy milk?",
        ):
            with self.subTest(request=request):
                self.assertIs(parse_creation_request(request), NOT_A_REQUEST)

    def test_an_unsafe_character_anywhere_is_not_a_request(self) -> None:
        for character in (
            "\x00",
            "\t",
            "\x7f",
            ZERO_WIDTH_SPACE,
            LINE_SEPARATOR,
            PARAGRAPH_SEPARATOR,
            LONE_SURROGATE,
        ):
            for request in (
                f"add a task to buy{character}milk",
                f"remind me tomorrow at 9:00 to call mom{character}",
                f"{character}add a task to buy milk",
            ):
                with self.subTest(character=hex(ord(character)), request=request):
                    self.assertIs(parse_creation_request(request), NOT_A_REQUEST)

    def test_only_text_can_be_a_request(self) -> None:
        for value in (None, b"add a task to buy milk", 5, ["add a task to x"]):
            with self.subTest(value=value):
                self.assertIs(parse_creation_request(value), NOT_A_REQUEST)

    def test_a_bare_prefix_is_unclear(self) -> None:
        for request in (
            "remind me",
            "Remind Me",
            "remind me.",
            "add a task to",
            "add a task to ",
            "remind me ",
            "remind me to",
        ):
            with self.subTest(request=request):
                self.assertIs(parse_creation_request(request), UNCLEAR)


class NotACreationRequestTests(unittest.TestCase):
    """Prove text that does not begin with a form is never a request."""

    def test_questions_negations_quotes_examples_and_mentions(self) -> None:
        for request in (
            "",
            "What does task mean?",
            "Don't remind me about that.",
            '"remind me tomorrow at 09:00 to call mom"',
            "For example: add a task to buy milk",
            "She told me to add a task to buy milk",
            "Please remind me tomorrow at 09:00 to call mom",
            "Could you remind me tomorrow at 09:00 to call mom",
            "My task list is long",
            "I keep meaning to add a task to fix the gate",
            "remind  me tomorrow at 09:00 to call mom",
            "add a task  to buy milk",
            "add task buy milk",
            "reminder: call mom tomorrow at 09:00",
            "remindme tomorrow at 09:00 to call mom",
            " remind me tomorrow at 09:00 to call mom",
        ):
            with self.subTest(request=request):
                self.assertIs(parse_creation_request(request), NOT_A_REQUEST)


class UnclearRequestTests(unittest.TestCase):
    """Prove a request that begins as a form but does not complete it is unclear."""

    def assertUnclear(self, *requests: str) -> None:
        for request in requests:
            with self.subTest(request=request):
                self.assertIs(parse_creation_request(request), UNCLEAR)

    def test_retractions_hedges_questions_and_quotations(self) -> None:
        self.assertUnclear(
            "remind me tomorrow at 09:00 to call mom. Actually, don't.",
            "add a task to buy milk? Not sure",
            "remind me tomorrow at 09:00 to call mom?!",
            'add a task to buy milk" is just an example',
            "add a task to buy milk, just kidding",
            "add a task to buy milk, never mind",
            "add a task to buy milk, or maybe not",
            f"add a task to buy milk, don{RIGHT_SINGLE_QUOTE}t",
            f"add a task to {LEFT_DOUBLE_QUOTE}buy milk{RIGHT_DOUBLE_QUOTE}",
        )

    def test_missing_vague_or_misplaced_timing(self) -> None:
        self.assertUnclear(
            "remind me tomorrow",
            "remind me to call mom",
            "remind me tomorrow morning to call mom",
            "remind me to call mom at 09:00",
            "add a task to buy milk tomorrow",
            "add a task to buy milk by next week",
            "remind me tonight to call mom",
            "remind me later to call mom",
        )

    def test_unsupported_timing_spellings_and_ranges(self) -> None:
        self.assertUnclear(
            "remind me next week at 09:00 to call mom",
            "remind me tomorrow at 9am to call mom",
            "remind me tomorrow at 9 to call mom",
            "remind me tomorrow at 24:00 to call mom",
            "remind me tomorrow at 9:60 to call mom",
            "remind me in 0 minutes to stretch",
            "remind me in 01 minutes to stretch",
            "remind me in 1441 minutes to stretch",
            "remind me in 169 hours to stretch",
            "remind me in 367 days at 09:00 to stretch",
            "remind me in 2 weeks at 09:00 to stretch",
            "remind me on 2027-02-30 at 09:00 to stretch",
            "remind me on 27-03-01 at 09:00 to stretch",
            "remind me friday at 09:00 to stretch",
            "remind me on tomorrow at 09:00 to stretch",
            f"remind me in {ARABIC_INDIC_ONE} minutes to stretch",
            f"remind me in {FULLWIDTH_ONE} hours to stretch",
        )

    def test_other_zones_abbreviations_and_malformed_offsets(self) -> None:
        self.assertUnclear(
            "remind me tomorrow at 09:00 CET to call mom",
            "remind me tomorrow at 09:00 Europe/Berlin to call mom",
            "remind me tomorrow at 09:00 UTC to call mom",
            "remind me tomorrow at 09:00 +1:00 to call mom",
            "remind me tomorrow at 09:00 +0100 to call mom",
            "remind me tomorrow at 09:00 -00:00 to call mom",
            "remind me tomorrow at 09:00 +24:00 to call mom",
            "remind me tomorrow at 09:00 +01:00:30 to call mom",
            "remind me in 20 minutes +01:00 to stretch",
        )

    def test_joined_second_requests_are_unclear(self) -> None:
        self.assertUnclear(
            "remind me tomorrow at 09:00 to call mom. Remind me tomorrow at 10:00 "
            "to call dad",
            "add a task to buy milk; add a task to buy bread",
            "remind me tomorrow at 09:00 to call mom then remind me tomorrow at "
            "10:00 to call dad",
            "add a task to buy milk, add a task to buy bread",
            "add a task to buy milk and then add a task to buy bread",
            "add a task to call Bob at 15:00 by friday",
        )

    def test_a_body_over_the_byte_limit_is_unclear(self) -> None:
        self.assertUnclear(
            "add a task to " + "a" * (CREATION_BODY_MAX_BYTES + 1),
            "add a task to " + TWO_BYTE * (CREATION_BODY_MAX_BYTES // 2 + 1),
            "remind me in 5 minutes to " + FOUR_BYTE * 129,
        )
        (item,) = _items(self, "add a task to " + "a" * CREATION_BODY_MAX_BYTES)
        self.assertEqual(len(item.body), CREATION_BODY_MAX_BYTES)
        (item,) = _items(
            self, "add a task to " + TWO_BYTE * (CREATION_BODY_MAX_BYTES // 2)
        )
        self.assertEqual(len(item.body.encode("utf-8")), CREATION_BODY_MAX_BYTES)

    def test_more_than_five_items_is_unclear(self) -> None:
        bodies = (
            "buy milk",
            "buy bread",
            "buy eggs",
            "buy rice",
            "buy tea",
            "buy salt",
        )
        five = " and ".join(f"add a task to {body}" for body in bodies[:5])
        self.assertEqual(
            [item.body for item in _items(self, five)],
            list(bodies[:5]),
        )
        self.assertEqual(CREATION_REQUEST_MAX_ITEMS, 5)
        six = " and ".join(f"add a task to {body}" for body in bodies)
        self.assertIs(parse_creation_request(six), UNCLEAR)

    def test_one_failing_item_makes_the_whole_request_unclear(self) -> None:
        self.assertUnclear(
            "add a task to buy milk and remind me tomorrow to call mom",
            "remind me tomorrow at 9:00 to call mom and add a task to buy milk "
            "tomorrow",
        )


class SegmentationTests(unittest.TestCase):
    """Prove items are split only at the closed joiner before a form prefix."""

    def test_and_and_comma_and_split_before_a_prefix(self) -> None:
        self.assertEqual(
            _shapes(
                _items(
                    self,
                    "add a task to buy milk and remind me tomorrow at 9:00 to call mom",
                )
            ),
            [(TASK, "buy milk", None), (REMINDER, "call mom", "tomorrow at 9:00")],
        )
        self.assertEqual(
            _shapes(
                _items(self, "add a task to buy milk, and add a task to buy bread")
            ),
            [(TASK, "buy milk", None), (TASK, "buy bread", None)],
        )

    def test_the_joiner_matches_ascii_case_insensitively(self) -> None:
        self.assertEqual(
            [
                item.body
                for item in _items(
                    self, "add a task to buy milk AND Remind Me in 5 minutes to stir"
                )
            ],
            ["buy milk", "stir"],
        )

    def test_an_and_without_a_following_prefix_stays_in_the_body(self) -> None:
        (item,) = _items(self, "add a task to buy bread and milk")
        self.assertEqual(item.body, "buy bread and milk")
        (item,) = _items(self, "remind me in 5 minutes to stir, and taste")
        self.assertEqual(item.body, "stir, and taste")

    def test_f2_and_f4_items_end_at_the_joiner(self) -> None:
        self.assertEqual(
            _shapes(
                _items(
                    self,
                    "remind me to call mom tomorrow at 9:00 and add a task to file "
                    "taxes by 2027-04-15, and remind me to stretch in 20 minutes",
                )
            ),
            [
                (REMINDER, "call mom", "tomorrow at 9:00"),
                (TASK, "file taxes", "2027-04-15"),
                (REMINDER, "stretch", "in 20 minutes"),
            ],
        )

    def test_the_same_item_named_twice_still_parses(self) -> None:
        # The duplicate rule needs resolved timing, so the compiler applies it.
        self.assertEqual(
            len(_items(self, "add a task to buy milk and add a task to buy milk")),
            2,
        )


class GuardTests(unittest.TestCase):
    """Prove the closed body guards, word by word."""

    def test_guarded_punctuation_anywhere(self) -> None:
        for body in (
            "buy milk?",
            "buy milk!",
            "buy; milk",
            'buy "milk"',
            f"buy {LEFT_DOUBLE_QUOTE}milk",
            f"buy milk{RIGHT_DOUBLE_QUOTE}",
            "buy milk. Then bread",
        ):
            with self.subTest(body=body):
                self.assertTrue(body_is_guarded(body))
        for body in ("buy milk.", "read v1.2 notes", "buy milk, bread", "buy 2-3 eggs"):
            with self.subTest(body=body):
                self.assertFalse(body_is_guarded(body))

    def test_a_form_prefix_anywhere(self) -> None:
        for body in ("ask Bob to REMIND ME", "say add a task to", "xremind mex"):
            with self.subTest(body=body):
                self.assertTrue(body_is_guarded(body))

    def test_every_timing_word_as_a_whole_word(self) -> None:
        for word in sorted(TIMING_WORDS):
            for body in (
                f"check {word}",
                f"check ({word.upper()}),",
                f"{word.title()}: check",
            ):
                with self.subTest(body=body):
                    self.assertTrue(body_is_guarded(body))
        for body in ("standby mode", "file the duet", "weekly review", "a sooner plan"):
            with self.subTest(body=body):
                self.assertFalse(body_is_guarded(body))

    def test_timing_tokens(self) -> None:
        for body in (
            "pay 2027-04-15",
            "call at 9:00",
            "call at 09:30",
            "call 9am",
            "call 9:30PM",
            "call 9 am",
            "call 11 PM sharp",
            "check in 3 days",
        ):
            with self.subTest(body=body):
                self.assertTrue(body_is_guarded(body))
        for unit in sorted(TIMING_UNITS):
            with self.subTest(unit=unit):
                self.assertTrue(body_is_guarded(f"check in 12 {unit}"))
        for body in ("room 101", "buy 3 apples", "pack in 3 boxes", "in a box"):
            with self.subTest(body=body):
                self.assertFalse(body_is_guarded(body))

    def test_retraction_hedge_and_example_phrases(self) -> None:
        for phrase in GUARDED_PHRASES:
            body = "call " + " ".join(phrase) + " mom"
            with self.subTest(body=body):
                self.assertTrue(body_is_guarded(body))
            with self.subTest(body=body.upper()):
                self.assertTrue(body_is_guarded(body.upper()))
        self.assertTrue(body_is_guarded(f"call mom, don{RIGHT_SINGLE_QUOTE}t"))
        for body in ("donate books", "do nothing", "forget-me-not seeds", "kidding"):
            with self.subTest(body=body):
                self.assertFalse(body_is_guarded(body))


class CreationTextTests(unittest.TestCase):
    """Prove the creation-text rule every body and deterministic text meets."""

    def test_accepted_text(self) -> None:
        for value in (
            "a",
            "buy  milk",
            "a" * CREATION_BODY_MAX_BYTES,
            TWO_BYTE * (CREATION_BODY_MAX_BYTES // 2),
            FOUR_BYTE * (CREATION_BODY_MAX_BYTES // 4),
        ):
            with self.subTest(length=len(value)):
                self.assertTrue(is_creation_text(value))

    def test_refused_text(self) -> None:
        for value in (
            "",
            " a",
            "a ",
            "a" * (CREATION_BODY_MAX_BYTES + 1),
            TWO_BYTE * (CREATION_BODY_MAX_BYTES // 2 + 1),
            FOUR_BYTE * (CREATION_BODY_MAX_BYTES // 4 + 1),
            "a\nb",
            f"a{LINE_SEPARATOR}b",
            f"a{ZERO_WIDTH_SPACE}b",
            f"a{LONE_SURROGATE}b",
            None,
            b"a",
            1,
        ):
            with self.subTest(value=repr(value)[:40]):
                self.assertFalse(is_creation_text(value))


class CanonicalPhraseTests(unittest.TestCase):
    """Prove the binding's canonical tokenization of a timing phrase.

    ASCII letters are lowercased and the tokens are joined by single spaces;
    digits and every other character stay exactly as written.
    """

    def test_canonical_phrase(self) -> None:
        self.assertEqual(canonical_phrase("Tomorrow At 9:00"), "tomorrow at 9:00")
        self.assertEqual(canonical_phrase("ON FRIDAY AT 18:30"), "on friday at 18:30")
        for spaced in (
            "tomorrow  at 9:00",
            " tomorrow at 9:00",
            "tomorrow at 9:00 ",
            "  Tomorrow   AT  9:00  ",
        ):
            with self.subTest(spaced=spaced):
                self.assertEqual(canonical_phrase(spaced), "tomorrow at 9:00")
        for different in ("tomorrow at 09:00", "tomorrowat 9:00", "tomorrow at 9: 00"):
            with self.subTest(different=different):
                self.assertNotEqual(
                    canonical_phrase(different),
                    canonical_phrase("tomorrow at 9:00"),
                )
        # Only the space separates tokens; a tab stays as written.
        self.assertEqual(
            canonical_phrase("tomorrow" + chr(9) + "at 9:00"),
            "tomorrow" + chr(9) + "at 9:00",
        )
        self.assertEqual(canonical_phrase(f"{KELVIN_SIGN}"), KELVIN_SIGN)


class CreationItemTests(unittest.TestCase):
    """Prove an item never shows its body or its phrase."""

    def test_repr_is_redacted(self) -> None:
        (item,) = _items(self, "remind me tomorrow at 9:00 to call mom")
        self.assertEqual(repr(item), "CreationItem(reminder, <redacted>)")
        self.assertNotIn("mom", repr(item) + str(item))
        self.assertNotIn("9:00", repr(item) + str(item))


class ModuleBoundaryTests(unittest.TestCase):
    """Prove the parse imports only its allowed modules and reads no clock."""

    ALLOWED = {
        "__future__",
        "re",
        "unicodedata",
        "dataclasses",
        "enum",
        "core.time_resolution",
    }

    def _tree(self) -> ast.Module:
        return ast.parse(inspect.getsource(task_requests))

    def test_it_imports_only_its_allowed_modules(self) -> None:
        imported = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertLessEqual(imported, self.ALLOWED)

    def test_it_never_reads_a_clock_or_logs(self) -> None:
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Attribute):
                with self.subTest(attribute=node.attr):
                    self.assertNotIn(
                        node.attr,
                        {"now", "utcnow", "today", "time", "monotonic", "getLogger"},
                    )


if __name__ == "__main__":
    unittest.main()
