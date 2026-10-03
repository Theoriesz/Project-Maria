"""Focused tests for V1-20's conversation-record vocabulary.

``core/conversation_records.py`` is the one place saved conversations and
conversation summaries take their limits, timestamp form, canonical
transcript, value objects, and fixed messages from. Every limit and message
is written out here by hand, so a declaration that drifted could not pass by
agreeing with itself. Every invisible, separator, or unencodable character
is built with ``chr()``, and every backslash a JSON escape needs is
``chr(92)``, so none is typed into this file.
"""

from __future__ import annotations

import dataclasses
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core import conversation_records as records
from core.action_contracts import MAXIMUM_SUMMARY_LENGTH, MAXIMUM_TRANSCRIPT_LENGTH
from core.context import CONTEXT_MAX_BYTES
from core.conversation import CONVERSATION_MAX_BYTES, CONVERSATION_MAX_EXCHANGES
from core.conversation_records import (
    ConversationSummary,
    SavedConversation,
    decode_transcript,
    encode_transcript,
    is_record_id,
    is_record_timestamp,
    is_summary_text,
    record_timestamp,
)


BACKSLASH = chr(92)
QUOTE = chr(34)
LINE_SEPARATOR = chr(0x2028)
NEXT_LINE = chr(0x85)
NO_BREAK_SPACE = chr(0xA0)
LONE_SURROGATE = chr(0xD800)
EURO = chr(0x20AC)  # three UTF-8 bytes
E_ACUTE = chr(0xE9)  # two UTF-8 bytes

# Written out by hand, so a drifted message fails here.
SAVED_CONVERSATION_ID_MESSAGE = (
    "Saved conversation id must be a positive whole number."
)
CONVERSATION_SUMMARY_ID_MESSAGE = (
    "Conversation summary id must be a positive whole number."
)
TRANSCRIPT_MESSAGE = (
    "A saved conversation must hold 1 to 8 exchanges of non-empty text, at "
    "most 8192 UTF-8 bytes in all, in Maria's canonical form."
)
SUMMARY_TEXT_MESSAGE = (
    "A conversation summary must be non-empty text of at most 2048 UTF-8 "
    "bytes, with no leading or trailing whitespace."
)
RECORD_MESSAGE = (
    "A saved conversation or conversation summary may hold only values Maria "
    "can show."
)
RECORD_LIST_LIMIT_MESSAGE = (
    "A conversation record listing limit must be a whole number from 1 to 21."
)

SENTINEL = "private-record-sentinel-4c7e19a2b6"
TIMESTAMP = "2026-10-03T04:05:06Z"


class _Text(str):
    """A str subclass, which must never pass as exact text."""


class _Number(int):
    """An int subclass, which must never pass as an exact id."""


class _NoParse:
    """Make any attempt to parse JSON fail the test."""

    def __enter__(self) -> None:
        self._patch = patch.object(
            records.json,
            "loads",
            side_effect=AssertionError("this value may not be parsed"),
        )
        self._patch.start()

    def __exit__(self, *exc_info: object) -> None:
        self._patch.stop()


class LimitTests(unittest.TestCase):
    """Pin every limit and its agreement with the modules it serves."""

    def test_every_limit_and_form_is_pinned(self) -> None:
        self.assertEqual(records.SAVED_CONVERSATION_MAX_EXCHANGES, 8)
        self.assertEqual(records.SAVED_CONVERSATION_MAX_BYTES, 8192)
        self.assertEqual(records.SAVED_CONVERSATION_MAX_CHARACTERS, 65536)
        self.assertEqual(records.CONVERSATION_SUMMARY_MAX_BYTES, 2048)
        self.assertEqual(records.RECORD_LIST_LIMIT, 20)
        self.assertEqual(records.RECORD_QUERY_LIMIT, 21)
        self.assertEqual(records.RECORD_TIMESTAMP_FORMAT, "%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(records.RECORD_MAXIMUM_ID, 9223372036854775807)

    def test_a_saved_conversation_holds_exactly_what_a_session_retains(self) -> None:
        # Every conversation a session can retain can be saved whole.
        self.assertEqual(
            records.SAVED_CONVERSATION_MAX_EXCHANGES,
            CONVERSATION_MAX_EXCHANGES,
        )
        self.assertEqual(records.SAVED_CONVERSATION_MAX_BYTES, CONVERSATION_MAX_BYTES)

    def test_the_contracts_and_context_bounds_agree_with_these(self) -> None:
        self.assertEqual(records.CONVERSATION_SUMMARY_MAX_BYTES, CONTEXT_MAX_BYTES)
        self.assertEqual(MAXIMUM_SUMMARY_LENGTH, records.CONVERSATION_SUMMARY_MAX_BYTES)
        self.assertEqual(
            MAXIMUM_TRANSCRIPT_LENGTH,
            records.SAVED_CONVERSATION_MAX_CHARACTERS,
        )

    def test_the_most_escaped_largest_transcript_fits_the_character_ceiling(
        self,
    ) -> None:
        # Sixteen texts of 512 control characters: 8192 bytes, each byte
        # written as a six-character escape.
        text = chr(1) * 512
        exchanges = tuple((text, text) for _ in range(8))

        encoded = encode_transcript(exchanges)

        self.assertLessEqual(len(encoded), records.SAVED_CONVERSATION_MAX_CHARACTERS)
        self.assertEqual(decode_transcript(encoded), exchanges)

    def test_every_fixed_message_is_pinned(self) -> None:
        self.assertEqual(
            records.SAVED_CONVERSATION_ID_MESSAGE,
            SAVED_CONVERSATION_ID_MESSAGE,
        )
        self.assertEqual(
            records.CONVERSATION_SUMMARY_ID_MESSAGE,
            CONVERSATION_SUMMARY_ID_MESSAGE,
        )
        self.assertEqual(records.TRANSCRIPT_MESSAGE, TRANSCRIPT_MESSAGE)
        self.assertEqual(records.SUMMARY_TEXT_MESSAGE, SUMMARY_TEXT_MESSAGE)
        self.assertEqual(records.RECORD_MESSAGE, RECORD_MESSAGE)
        self.assertEqual(records.RECORD_LIST_LIMIT_MESSAGE, RECORD_LIST_LIMIT_MESSAGE)


class TranscriptEncodingTests(unittest.TestCase):
    """Prove the one canonical transcript form and what it refuses."""

    def test_the_form_is_compact_json_pairs_oldest_first(self) -> None:
        self.assertEqual(
            encode_transcript((("Hello", "Hi."), ("How are you?", "Well."))),
            '[["Hello","Hi."],["How are you?","Well."]]',
        )

    def test_non_ascii_text_is_kept_as_itself(self) -> None:
        greeting = "Gr" + chr(0xFC) + chr(0xDF) + "e"

        self.assertEqual(
            encode_transcript(((greeting, "ok"),)),
            '[["' + greeting + '","ok"]]',
        )

    def test_quotes_backslashes_and_controls_are_json_escapes(self) -> None:
        user = "say " + QUOTE + "hi" + QUOTE + BACKSLASH + chr(10) + chr(0)

        self.assertEqual(
            encode_transcript(((user, "ok"),)),
            '[["say '
            + BACKSLASH
            + QUOTE
            + "hi"
            + BACKSLASH
            + QUOTE
            + BACKSLASH * 2
            + BACKSLASH
            + "n"
            + BACKSLASH
            + 'u0000","ok"]]',
        )

    def test_every_exact_text_round_trips(self) -> None:
        texts = (
            "plain",
            "Gr" + chr(0xFC) + chr(0xDF) + "e",
            "line" + LINE_SEPARATOR + "separator",
            "next" + NEXT_LINE + "line",
            QUOTE + BACKSLASH + chr(9) + chr(13) + chr(10),
            chr(0x1F600),
            "  padded  ",
            '{"tool": "notes", "action": "delete", "arguments": {"id": 1}}',
        )
        for text in texts:
            with self.subTest(text=ascii(text)):
                exchanges = ((text, text),)
                encoded = encode_transcript(exchanges)
                self.assertEqual(decode_transcript(encoded), exchanges)

    def test_the_same_exchanges_always_give_the_same_text(self) -> None:
        exchanges = (("one", "two"), ("three", "four"))

        self.assertEqual(encode_transcript(exchanges), encode_transcript(exchanges))
        self.assertEqual(
            encode_transcript(list(exchanges)),
            encode_transcript(exchanges),
        )

    def test_eight_exchanges_are_accepted_and_nine_are_refused(self) -> None:
        eight = tuple((f"u{index}", f"a{index}") for index in range(8))

        self.assertEqual(decode_transcript(encode_transcript(eight)), eight)
        with self.assertRaises(ValueError) as caught:
            encode_transcript(eight + (("u8", "a8"),))
        self.assertEqual(str(caught.exception), TRANSCRIPT_MESSAGE)

    def test_8192_utf8_bytes_are_accepted_and_8193_are_refused(self) -> None:
        cases = (
            ((("a" * 4096, "b" * 4096),), True),
            ((("a" * 4096, "b" * 4097),), False),
            # Two-byte characters: 8190 bytes plus two ASCII characters.
            (((E_ACUTE * 4095, "ab"),), True),
            (((E_ACUTE * 4095, "abc"),), False),
            # The total counts every exchange.
            (tuple(("a" * 512, "b" * 512) for _ in range(8)), True),
            (
                tuple(("a" * 512, "b" * 512) for _ in range(7))
                + (("a" * 512, "b" * 513),),
                False,
            ),
        )
        for exchanges, accepted in cases:
            with self.subTest(accepted=accepted, count=len(exchanges)):
                if accepted:
                    self.assertEqual(
                        decode_transcript(encode_transcript(exchanges)),
                        exchanges,
                    )
                else:
                    with self.assertRaises(ValueError):
                        encode_transcript(exchanges)

    def test_refused_exchanges_raise_the_fixed_message_and_repeat_nothing(
        self,
    ) -> None:
        cases = {
            "no exchanges": (),
            "an empty list": [],
            "text": SENTINEL,
            "bytes": SENTINEL.encode("ascii"),
            "None": None,
            "a mapping": {SENTINEL: "b"},
            "a one-element pair": ((SENTINEL,),),
            "a three-element pair": ((SENTINEL, "b", "c"),),
            "a list pair": ([SENTINEL, "b"],),
            "an empty user text": (("", SENTINEL),),
            "an empty reply": ((SENTINEL, ""),),
            "a number": ((1, SENTINEL),),
            "a str subclass": ((_Text(SENTINEL), "b"),),
            "a lone surrogate": ((SENTINEL, "a" + LONE_SURROGATE),),
        }
        for label, exchanges in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as caught:
                    encode_transcript(exchanges)
                self.assertEqual(str(caught.exception), TRANSCRIPT_MESSAGE)
                self.assertNotIn(SENTINEL, str(caught.exception))


class TranscriptDecodingTests(unittest.TestCase):
    """Prove only Maria's exact canonical text decodes."""

    def test_only_the_exact_canonical_text_is_a_transcript(self) -> None:
        canonical = '[["Hello","Hi."]]'
        variants = {
            "a space after the comma": '[["Hello", "Hi."]]',
            "a space inside the array": '[ ["Hello","Hi."] ]',
            "a trailing newline": canonical + chr(10),
            "a leading space": " " + canonical,
            "an escaped slash": '[["a' + BACKSLASH + '/b","Hi."]]',
            "an escaped ASCII letter": '[["' + BACKSLASH + 'u0048ello","Hi."]]',
            "escaped non-ASCII": '[["Gr' + BACKSLASH + 'u00fc","Hi."]]',
            "a byte-order mark": chr(0xFEFF) + canonical,
        }
        self.assertEqual(decode_transcript(canonical), (("Hello", "Hi."),))
        for label, text in variants.items():
            with self.subTest(variant=label):
                self.assertIsNone(decode_transcript(text))

    def test_json_that_is_not_a_transcript_is_refused(self) -> None:
        for text in (
            "[]",
            "[[]]",
            '{"a":"b"}',
            '"text"',
            "null",
            "NaN",
            '[["a"]]',
            '[["a","b","c"]]',
            '[["a",1]]',
            '[["",""]]',
            '[["a","b"]',
            '[["a","b"]]x',
            '[{"user":"a","assistant":"b"}]',
            "[" + ",".join('["a","b"]' for _ in range(9)) + "]",
        ):
            with self.subTest(text=text[:40]):
                self.assertIsNone(decode_transcript(text))

    def test_non_text_and_over_long_values_are_refused_without_parsing(
        self,
    ) -> None:
        canonical = '[["Hello","Hi."]]'
        over_long = "[" + " " * records.SAVED_CONVERSATION_MAX_CHARACTERS + "]"
        with _NoParse():
            for value in (
                None,
                canonical.encode("utf-8"),
                1,
                _Text(canonical),
                "",
                over_long,
            ):
                with self.subTest(value=type(value).__name__):
                    self.assertIsNone(decode_transcript(value))

    def test_deep_nesting_is_refused_rather_than_raised(self) -> None:
        nested = "[" * 30000 + "]" * 30000

        self.assertLessEqual(len(nested), records.SAVED_CONVERSATION_MAX_CHARACTERS)
        self.assertIsNone(decode_transcript(nested))

    def test_an_escaped_lone_surrogate_is_refused(self) -> None:
        text = '[["a' + BACKSLASH + 'ud800","b"]]'

        self.assertEqual(json.loads(text)[0][0], "a" + LONE_SURROGATE)
        self.assertIsNone(decode_transcript(text))


class SummaryTextTests(unittest.TestCase):
    """Prove what text can be stored as a conversation summary."""

    def test_2048_utf8_bytes_are_accepted_and_2049_are_refused(self) -> None:
        cases = (
            ("a" * 2048, True),
            ("a" * 2049, False),
            (EURO * 682 + "ab", True),
            (EURO * 682 + "abc", False),
            (EURO * 683, False),
        )
        for text, accepted in cases:
            with self.subTest(size=len(text.encode("utf-8"))):
                self.assertIs(is_summary_text(text), accepted)

    def test_outer_whitespace_is_refused_and_inner_whitespace_is_kept(
        self,
    ) -> None:
        self.assertTrue(is_summary_text("a b" + chr(10) + "c" + chr(9) + "d"))
        for text in (
            " a",
            "a ",
            chr(10) + "a",
            "a" + chr(13),
            "a" + LINE_SEPARATOR,
            NO_BREAK_SPACE + "a",
            "a" + NEXT_LINE,
        ):
            with self.subTest(text=ascii(text)):
                self.assertFalse(is_summary_text(text))

    def test_empty_non_text_and_unencodable_values_are_refused(self) -> None:
        for value in (
            "",
            None,
            1,
            b"a",
            _Text("a"),
            "a" + LONE_SURROGATE + "b",
        ):
            with self.subTest(value=ascii(value)):
                self.assertFalse(is_summary_text(value))


class RecordIdTests(unittest.TestCase):
    """Prove only exact positive ints are record ids."""

    def test_exact_positive_ints_are_ids(self) -> None:
        # The upper bound is the memory layer's, which answers an id above
        # RECORD_MAXIMUM_ID as absent without opening the database.
        for value in (1, 7, records.RECORD_MAXIMUM_ID, records.RECORD_MAXIMUM_ID + 1):
            with self.subTest(value=value):
                self.assertTrue(is_record_id(value))

    def test_everything_else_is_refused(self) -> None:
        for value in (0, -1, True, False, 1.0, "1", None, _Number(1)):
            with self.subTest(value=ascii(value)):
                self.assertFalse(is_record_id(value))


class TimestampTests(unittest.TestCase):
    """Prove Maria's one record timestamp form."""

    def test_the_timestamp_is_the_current_utc_second(self) -> None:
        moment = datetime(2026, 10, 3, 4, 5, 6, 789, tzinfo=timezone.utc)

        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment.astimezone(tz)

        with patch.object(records, "datetime", _Clock):
            self.assertEqual(record_timestamp(), TIMESTAMP)

    def test_a_fresh_timestamp_is_valid_and_now(self) -> None:
        before = datetime.now(timezone.utc).replace(microsecond=0)

        stamp = record_timestamp()

        self.assertTrue(is_record_timestamp(stamp))
        parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        self.assertLessEqual(before - timedelta(seconds=1), parsed)
        self.assertLessEqual(parsed, datetime.now(timezone.utc))

    def test_only_real_utc_seconds_in_the_one_form_are_timestamps(self) -> None:
        for value in (TIMESTAMP, "2024-02-29T23:59:59Z", "0001-01-01T00:00:00Z"):
            with self.subTest(value=value):
                self.assertTrue(is_record_timestamp(value))
        fullwidth_year = "".join(chr(0xFF10 + int(digit)) for digit in "2026")
        for value in (
            "2023-02-29T00:00:00Z",
            "2026-13-01T00:00:00Z",
            "2026-10-01T24:00:00Z",
            "2026-10-01T09:60:00Z",
            "2026-10-01 09:46:17Z",
            "2026-10-01T09:46:17z",
            "2026-10-01T09:46:17",
            "2026-10-01T09:46:17.5Z",
            "2026-10-01T09:46:17+00:00",
            "2026-1-01T09:46:17Z",
            " " + TIMESTAMP,
            TIMESTAMP + chr(10),
            fullwidth_year + TIMESTAMP[4:],
            "",
            None,
            20261003,
            _Text(TIMESTAMP),
        ):
            with self.subTest(value=ascii(value)):
                self.assertFalse(is_record_timestamp(value))


class ValueObjectTests(unittest.TestCase):
    """Prove the two value objects hold only valid fields and hide their text."""

    def test_a_saved_conversation_holds_its_validated_fields(self) -> None:
        conversation = SavedConversation(3, TIMESTAMP, ((SENTINEL, "reply"),))

        self.assertEqual(conversation.conversation_id, 3)
        self.assertEqual(conversation.created_at, TIMESTAMP)
        self.assertEqual(conversation.exchanges, ((SENTINEL, "reply"),))
        self.assertEqual(repr(conversation), "SavedConversation(id=3, <redacted>)")
        self.assertNotIn(SENTINEL, str(conversation))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            conversation.exchanges = ()

    def test_a_saved_conversation_refuses_any_field_maria_could_not_write(
        self,
    ) -> None:
        cases = {
            "id zero": (0, TIMESTAMP, (("a", "b"),)),
            "a bool id": (True, TIMESTAMP, (("a", "b"),)),
            "a bad timestamp": (1, "2026-10-03", (("a", "b"),)),
            "a list of exchanges": (1, TIMESTAMP, [("a", "b")]),
            "a list pair": (1, TIMESTAMP, (["a", "b"],)),
            "no exchanges": (1, TIMESTAMP, ()),
            "nine exchanges": (1, TIMESTAMP, tuple(("a", "b") for _ in range(9))),
            "an empty text": (1, TIMESTAMP, (("a", ""),)),
        }
        for label, fields in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as caught:
                    SavedConversation(*fields)
                self.assertEqual(str(caught.exception), RECORD_MESSAGE)

    def test_a_summary_holds_its_validated_fields(self) -> None:
        summary = ConversationSummary(4, SENTINEL, TIMESTAMP, TIMESTAMP, False)

        self.assertEqual(summary.summary_id, 4)
        self.assertEqual(summary.body, SENTINEL)
        self.assertIs(summary.context_enabled, False)
        self.assertEqual(repr(summary), "ConversationSummary(id=4, <redacted>)")
        self.assertNotIn(SENTINEL, str(summary))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            summary.body = "changed"

    def test_a_summary_refuses_any_field_maria_could_not_write(self) -> None:
        cases = {
            "id zero": (0, "body", TIMESTAMP, TIMESTAMP, False),
            "outer whitespace": (1, " body", TIMESTAMP, TIMESTAMP, False),
            "an empty body": (1, "", TIMESTAMP, TIMESTAMP, False),
            "an oversized body": (1, "a" * 2049, TIMESTAMP, TIMESTAMP, False),
            "a bad creation time": (1, "body", "now", TIMESTAMP, False),
            "a bad update time": (1, "body", TIMESTAMP, None, False),
            "an int consent": (1, "body", TIMESTAMP, TIMESTAMP, 1),
        }
        for label, fields in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as caught:
                    ConversationSummary(*fields)
                self.assertEqual(str(caught.exception), RECORD_MESSAGE)


if __name__ == "__main__":
    unittest.main()
