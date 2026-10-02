"""Focused tests for V1-16's per-note context consent.

Consent is stored on the note as the identity of exactly the title and body
the owner approved, so these tests cover the memory layer, the notes tool,
the planner commands, the permission floor, the action contract, and the
confirmed session flow, each over a temporary database. Nothing here
contacts a model or reads Maria's live database, live log, or private
override.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from core.action_contracts import BUILT_IN_ACTION_CONTRACTS, FACT_DIGEST_LENGTH
from core.context import ContextConsentOutcome, note_digest, quoted_for_owner
from core.memory import Memory, NoteSnapshot, NoteUpdate
from core.permissions import (
    CONFIRMATION_NOTICES,
    MANDATORY_CONFIRMATION_ACTIONS,
    PermissionDecision,
    PermissionManager,
)
from core.planner import Planner
from core.tool_manager import ConfirmationBinding, ToolExecutionResult
from tests.test_brain_recording_policy import _quiet_logger
from tests.test_context import (
    FAILURE_SENTINEL,
    MALFORMED_TEXT,
    _FailurePrivacyCase,
    everything_sent,
)
from tests.test_conversation import ScriptedProvider, _Harness
from tools.notes import NotesTool


TITLE_SENTINEL = "private-note-title-3e81c0a7d5"
BODY_SENTINEL = "private-note-body-91f4d2b6e0"
REPLACEMENT_SENTINEL = "private-note-replacement-5a0c7e19b3"

# The owner-visible wording, written out so it is pinned rather than agreed
# with.
ENABLE_NOTICE = (
    "Once enabled, this note's entire title and body may be sent to your local "
    "reasoning model on a later conversational turn when a word of four or "
    "more characters from your request appears literally in its title or its "
    "body, within Maria's context limits. Maria does not check the note for "
    "passwords or other secrets, and enabling it does not mean it contains "
    "none."
)
ENABLE_CONFIRMATION = (
    "Confirmation required: notes.enable_context requires explicit "
    f"confirmation. {ENABLE_NOTICE}"
)
DISABLE_CONFIRMATION = (
    "Confirmation required: notes.disable_context requires explicit "
    "confirmation."
)


def _preview(note_id: int, title: str, body: str) -> str:
    """Return the text an enable request shows for plain printable text."""
    return (
        f"This approval covers note {note_id} exactly as it is stored now, "
        "shown quoted:\n"
        f'Title: "{title}"\n'
        f'Body: "{body}"'
    )


def _refused(note_id: int) -> str:
    """Return the fixed refusal for an approval whose text changed."""
    return (
        f"Context was not enabled for note {note_id} because its title and "
        "body could not be confirmed unchanged since confirmation was "
        "requested. Request confirmation again with: enable note context "
        f"{note_id}"
    )


def _logged(records: list[logging.LogRecord]) -> str:
    """Return captured records as a log file would show them.

    A plain formatter includes any traceback a record carries, which the
    captured one-line output alone would leave out.
    """
    formatter = logging.Formatter()
    return "\n".join(formatter.format(record) for record in records)


def _raw_consent(database_path: Path) -> list[tuple[int, str | None]]:
    """Read every note's stored consent straight from a temporary database."""
    with closing(sqlite3.connect(database_path)) as connection:
        return [
            (int(row[0]), row[1])
            for row in connection.execute(
                "SELECT id, context_digest FROM notes ORDER BY id"
            )
        ]


def _enable_note(memory: Memory, note_id: int) -> None:
    """Enable one note for the exact title and body it holds now."""
    outcome = memory.enable_note_context(note_id, memory.note_text_digest(note_id))
    if outcome is not ContextConsentOutcome.ENABLED:
        raise AssertionError("the note could not be enabled")


class _MemoryTestCase(unittest.TestCase):
    """One initialized temporary database per test."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database_path = Path(temporary.name) / "database" / "maria.db"
        self.memory = Memory(
            self.database_path,
            logger=_quiet_logger("tests.note_consent.memory"),
        )
        self.memory.initialize()
        self.other = Memory(
            self.database_path,
            logger=_quiet_logger("tests.note_consent.other"),
        )

    def _raw_update(self, statement: str, parameters: tuple) -> None:
        """Change a note through a second, raw database connection."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(statement, parameters)
            connection.commit()


class NoteIdentityTests(unittest.TestCase):
    """Prove the approved-text identity covers title and body together."""

    def test_the_identity_is_the_sha256_of_both_texts_as_one_json_array(
        self,
    ) -> None:
        expected = hashlib.sha256(
            json.dumps(["Plan", "Straße"], ensure_ascii=False, separators=(",", ":"))
            .encode("utf-8")
        ).hexdigest()

        self.assertEqual(note_digest("Plan", "Straße"), expected)

    def test_title_and_body_can_never_trade_text_for_one_identity(self) -> None:
        identities = {
            note_digest("ab", "c"),
            note_digest("a", "bc"),
            note_digest("c", "ab"),
            note_digest('a","b', "c"),
            note_digest("a", '","bc'),
        }

        self.assertEqual(len(identities), 5)


class NoteConsentStorageTests(_MemoryTestCase):
    """Prove consent is off by default and stored only as an identity."""

    def test_existing_and_new_notes_start_off(self) -> None:
        self.memory.create_note("First", "body one")
        self.memory.create_note("Second", "body two")

        self.assertEqual(_raw_consent(self.database_path), [(1, None), (2, None)])
        self.assertEqual(self.memory.search_context_notes(("body",), 9, 2048), [])

    def test_the_column_admits_only_null_or_a_sixty_four_character_identity(
        self,
    ) -> None:
        self.memory.create_note("Title", "Body")
        with closing(sqlite3.connect(self.database_path)) as connection:
            for value in ("", "x", "a" * 63, "a" * 65):
                with self.subTest(length=len(value)):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            "UPDATE notes SET context_digest = ?",
                            (value,),
                        )
                    connection.rollback()

    def test_the_released_note_reads_are_unchanged(self) -> None:
        note_id = self.memory.create_note("Title", "Body")
        _enable_note(self.memory, note_id)

        self.assertEqual(
            set(self.memory.get_note(note_id)),
            {"id", "title", "body", "created_at", "updated_at"},
        )
        self.assertEqual(
            set(self.memory.list_notes()[0]),
            {"id", "title", "created_at", "updated_at"},
        )


class NoteConsentMemoryTests(_MemoryTestCase):
    """Prove enable binds exact text and every change turns consent off."""

    def setUp(self) -> None:
        super().setUp()
        self.note_id = self.memory.create_note(TITLE_SENTINEL, BODY_SENTINEL)
        self.approved = self.memory.note_text_digest(self.note_id)

    def test_the_identity_names_the_current_title_and_body(self) -> None:
        self.assertEqual(self.approved, note_digest(TITLE_SENTINEL, BODY_SENTINEL))
        self.assertIsNone(self.memory.note_text_digest(999))

    def test_an_unchanged_note_is_enabled_and_its_identity_stored(self) -> None:
        outcome = self.memory.enable_note_context(self.note_id, self.approved)

        self.assertIs(outcome, ContextConsentOutcome.ENABLED)
        self.assertEqual(
            _raw_consent(self.database_path),
            [(self.note_id, self.approved)],
        )

    def test_a_changed_title_or_body_is_refused_and_nothing_changes(self) -> None:
        for method, replacement in (
            ("retitle_note", "Another title"),
            ("edit_note", "Another body"),
        ):
            with self.subTest(method=method):
                note_id = self.memory.create_note(TITLE_SENTINEL, BODY_SENTINEL)
                approved = self.memory.note_text_digest(note_id)
                getattr(self.other, method)(note_id, replacement)

                self.assertIs(
                    self.memory.enable_note_context(note_id, approved),
                    ContextConsentOutcome.TARGET_CHANGED,
                )
                self.assertIsNone(dict(_raw_consent(self.database_path))[note_id])

    def test_a_missing_note_or_an_unbound_approval_never_enables(self) -> None:
        self.assertIs(
            self.memory.enable_note_context(999, self.approved),
            ContextConsentOutcome.NOT_FOUND,
        )
        self.assertIs(
            self.memory.enable_note_context(self.note_id, None),
            ContextConsentOutcome.TARGET_CHANGED,
        )
        self.assertEqual(_raw_consent(self.database_path), [(self.note_id, None)])

    def test_malformed_ids_and_identities_are_refused_without_their_text(
        self,
    ) -> None:
        for arguments in (
            (0, self.approved),
            (True, self.approved),
            ("1", self.approved),
            (self.note_id, "A" * 64),
            (self.note_id, "a" * 63),
            (self.note_id, TITLE_SENTINEL),
        ):
            with self.subTest(arguments=repr(arguments)[:40]):
                with self.assertRaises(ValueError) as caught:
                    self.memory.enable_note_context(*arguments)
                self.assertNotIn(TITLE_SENTINEL, str(caught.exception))
        for bad in (0, -1, True, "1"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    self.memory.disable_note_context(bad)
                with self.assertRaises(ValueError):
                    self.memory.note_text_digest(bad)

    def test_disable_changes_exactly_one_row_and_needs_no_text(self) -> None:
        other_id = self.memory.create_note("Other", "other body")
        _enable_note(self.memory, self.note_id)
        _enable_note(self.memory, other_id)

        self.assertTrue(self.memory.disable_note_context(self.note_id))
        self.assertTrue(self.memory.disable_note_context(self.note_id))
        self.assertFalse(self.memory.disable_note_context(999))
        self.assertEqual(
            _raw_consent(self.database_path),
            [(self.note_id, None), (other_id, note_digest("Other", "other body"))],
        )

    def test_retitle_and_edit_turn_consent_off_and_report_it_only_when_on(
        self,
    ) -> None:
        for method in ("retitle_note_reporting_consent", "edit_note_reporting_consent"):
            with self.subTest(method=method):
                _enable_note(self.memory, self.note_id)

                first = getattr(self.memory, method)(self.note_id, "Changed")
                second = getattr(self.memory, method)(self.note_id, "Changed")
                missing = getattr(self.memory, method)(999, "Changed")

                self.assertEqual(first, NoteUpdate(True, True))
                self.assertEqual(second, NoteUpdate(True, False))
                self.assertEqual(missing, NoteUpdate(False, False))
                self.assertEqual(
                    _raw_consent(self.database_path),
                    [(self.note_id, None)],
                )
                # Restore the approved text for the next method.
                self.memory.retitle_note(self.note_id, TITLE_SENTINEL)
                self.memory.edit_note(self.note_id, BODY_SENTINEL)

    def test_the_released_retitle_and_edit_still_return_plain_bools(self) -> None:
        _enable_note(self.memory, self.note_id)

        self.assertIs(self.memory.retitle_note(self.note_id, "New"), True)
        self.assertIs(self.memory.edit_note(self.note_id, "New body"), True)
        self.assertIs(self.memory.retitle_note(999, "New"), False)
        self.assertIs(self.memory.edit_note(999, "New body"), False)
        self.assertEqual(_raw_consent(self.database_path), [(self.note_id, None)])

    def test_a_failed_edit_leaves_both_the_text_and_the_consent_as_they_were(
        self,
    ) -> None:
        _enable_note(self.memory, self.note_id)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                CREATE TRIGGER refuse_body BEFORE UPDATE OF body ON notes
                BEGIN SELECT RAISE(ABORT, 'refused'); END
                """
            )
            connection.commit()

        with self.assertRaisesRegex(RuntimeError, r"^Unable to edit note\.$"):
            self.memory.edit_note_reporting_consent(self.note_id, "Changed")

        self.assertEqual(
            _raw_consent(self.database_path),
            [(self.note_id, self.approved)],
        )
        self.assertEqual(self.memory.get_note(self.note_id)["body"], BODY_SENTINEL)


class DurableApprovalGuardTests(_MemoryTestCase):
    """Prove text changed elsewhere never inherits an approval.

    Another connection may rewrite a note's title or body directly and leave
    its stored consent in place. The stored identity then no longer matches
    the note's text, so neither a search nor a recheck returns the note, in
    this session or in any later one.
    """

    def setUp(self) -> None:
        super().setUp()
        self.note_id = self.memory.create_note(TITLE_SENTINEL, BODY_SENTINEL)
        _enable_note(self.memory, self.note_id)

    def _eligible(self) -> tuple[list, list]:
        return (
            self.memory.search_context_notes(("private",), 9, 2048),
            self.memory.context_notes_by_id((self.note_id,), 2048),
        )

    def test_the_approved_note_is_eligible_before_any_change(self) -> None:
        searched, rechecked = self._eligible()

        row = {
            "id": self.note_id,
            "title": TITLE_SENTINEL,
            "body": BODY_SENTINEL,
            "oversized": False,
        }
        self.assertEqual(searched, [row])
        self.assertEqual(rechecked, [row])

    def test_a_raw_title_or_body_change_leaving_consent_on_is_never_eligible(
        self,
    ) -> None:
        for column in ("title", "body"):
            with self.subTest(column=column):
                self._raw_update(
                    f"UPDATE notes SET {column} = ? WHERE id = ?",
                    (f"private {REPLACEMENT_SENTINEL}", self.note_id),
                )
                self.assertIsNotNone(dict(_raw_consent(self.database_path))[1])

                self.assertEqual(self._eligible(), ([], []))

                self._raw_update(
                    "UPDATE notes SET title = ?, body = ? WHERE id = ?",
                    (TITLE_SENTINEL, BODY_SENTINEL, self.note_id),
                )

    def test_a_later_fresh_session_never_sends_the_replacement_text(self) -> None:
        provider = ScriptedProvider(lambda request: "reply")
        harness = _Harness(self, provider)
        note_id = harness.memory.create_note(TITLE_SENTINEL, BODY_SENTINEL)
        _enable_note(harness.memory, note_id)
        with closing(sqlite3.connect(harness.database_path)) as connection:
            connection.execute(
                "UPDATE notes SET body = ? WHERE id = ?",
                (f"private {REPLACEMENT_SENTINEL}", note_id),
            )
            connection.commit()

        response = harness.session().submit("anything private here")

        self.assertEqual(provider.requests[0].context, ())
        self.assertNotIn(REPLACEMENT_SENTINEL, everything_sent(provider.requests))
        self.assertNotIn(f"note {note_id}", response.text)

    def test_text_changed_away_and_back_is_the_approved_text_again(self) -> None:
        """The guard compares current text, so it cannot see a round trip."""
        self._raw_update(
            "UPDATE notes SET body = ? WHERE id = ?",
            ("interim", self.note_id),
        )
        self._raw_update(
            "UPDATE notes SET body = ? WHERE id = ?",
            (BODY_SENTINEL, self.note_id),
        )

        self.assertEqual(len(self._eligible()[0]), 1)

    def test_a_non_text_title_or_body_is_never_eligible_or_matched(self) -> None:
        self._raw_update(
            "UPDATE notes SET body = ? WHERE id = ?",
            (b"private blob body", self.note_id),
        )

        self.assertEqual(self._eligible(), ([], []))
        self.assertIsNone(self.memory.note_text_digest(self.note_id))
        self.assertIs(
            self.memory.enable_note_context(
                self.note_id,
                note_digest(TITLE_SENTINEL, BODY_SENTINEL),
            ),
            ContextConsentOutcome.TARGET_CHANGED,
        )


class NoteSnapshotTests(_MemoryTestCase):
    """Prove an approval reads the title, body, and identity together."""

    def test_the_snapshot_is_the_exact_text_and_that_text_s_identity(self) -> None:
        note_id = self.memory.create_note(TITLE_SENTINEL, BODY_SENTINEL)

        snapshot = self.memory.note_snapshot(note_id)

        self.assertIsInstance(snapshot, NoteSnapshot)
        self.assertEqual(
            (snapshot.title, snapshot.body, snapshot.text_digest),
            (TITLE_SENTINEL, BODY_SENTINEL, note_digest(TITLE_SENTINEL, BODY_SENTINEL)),
        )
        self.assertEqual(self.memory.note_text_digest(note_id), snapshot.text_digest)
        self.assertEqual(repr(snapshot), "NoteSnapshot(<redacted>)")

    def test_a_missing_note_or_non_text_has_no_snapshot(self) -> None:
        note_id = self.memory.create_note(TITLE_SENTINEL, BODY_SENTINEL)
        self._raw_update(
            "UPDATE notes SET body = ? WHERE id = ?",
            (b"blob body", note_id),
        )

        self.assertIsNone(self.memory.note_snapshot(note_id))
        self.assertIsNone(self.memory.note_snapshot(999))
        for bad in (0, -1, True, "1"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    self.memory.note_snapshot(bad)


class NoteFailurePrivacyTests(_FailurePrivacyCase):
    """Prove a failing note read or consent change never repeats stored text.

    A malformed TEXT title makes SQLite's decode error quote the title, and a
    trigger's message can hold the sentinel whenever the old or the new title
    or body does. The log, the raised failure, its cause and context, its
    traceback, and anything the owner sees carry only Maria's fixed messages,
    and each failure changes nothing in the database.
    """

    def setUp(self) -> None:
        super().setUp()
        self.tool = NotesTool(self.memory, logger=self.logger)
        self.note_id = self.memory.create_note("placeholder", "body")
        self.raw(
            "UPDATE notes SET title = CAST(? AS TEXT), context_digest = ? "
            "WHERE id = ?",
            (MALFORMED_TEXT, "a" * 64, self.note_id),
        )

    def _approved_note(self, title: str = "Readable title") -> int:
        """Store and enable a note whose text SQLite can read."""
        note_id = self.memory.create_note(title, "Readable body")
        _enable_note(self.memory, note_id)
        return note_id

    def _stored(self) -> list[tuple]:
        return self.raw_rows(
            "SELECT id, CAST(title AS BLOB), CAST(body AS BLOB), context_digest, "
            "updated_at FROM notes ORDER BY id"
        )

    def test_the_synthetic_title_really_puts_its_text_in_sqlite_s_error(self) -> None:
        self.assert_sqlite_quotes_the_sentinel("SELECT title FROM notes")

    def test_note_reads_and_consent_changes_fail_with_fixed_text_only(self) -> None:
        before = self._stored()
        self.assertEqual(before[0][1:4], (MALFORMED_TEXT, b"body", "a" * 64))

        for label, call, message, logged in (
            (
                "snapshot",
                lambda: self.memory.note_snapshot(self.note_id),
                "Unable to read note.",
                "Failed to read note identity.",
            ),
            (
                "identity",
                lambda: self.memory.note_text_digest(self.note_id),
                "Unable to read note.",
                "Failed to read note identity.",
            ),
            (
                "enable",
                lambda: self.memory.enable_note_context(self.note_id, "a" * 64),
                "Unable to change note context consent.",
                "Failed to change note context consent.",
            ),
            (
                "recheck",
                lambda: self.memory.context_notes_by_id((self.note_id,), 2048),
                "Unable to read note context.",
                "Failed to read note context.",
            ),
            (
                "search",
                lambda: self.memory.search_context_notes(("private",), 9, 2048),
                "Unable to read note context.",
                "Failed to read note context.",
            ),
        ):
            with self.subTest(label=label):
                self.assert_fixed_failure(call, message, logged)
                self.assertEqual(self._stored(), before)
        self.assert_displayed(
            self.tool.execute(
                "enable_context",
                {"id": self.note_id, "text_digest": "a" * 64},
            ).message,
            "Note context change failed: Unable to change note context consent.",
        )
        self.assertEqual(self._stored(), before)

    def test_a_failed_disable_keeps_consent_and_repeats_no_text(self) -> None:
        note_id = self._approved_note(f"Readable {FAILURE_SENTINEL}")
        self.refuse_when(
            "refuse_disable",
            "UPDATE OF context_digest ON notes",
            f"instr(OLD.title, '{FAILURE_SENTINEL}') > 0",
        )
        self.assert_sqlite_quotes_the_sentinel(
            f"UPDATE notes SET context_digest = NULL WHERE id = {note_id}"
        )
        before = self._stored()

        self.assert_fixed_failure(
            lambda: self.memory.disable_note_context(note_id),
            "Unable to change note context consent.",
            "Failed to change note context consent.",
        )
        self.assert_displayed(
            self.tool.execute("disable_context", {"id": note_id}).message,
            "Note context change failed: Unable to change note context consent.",
        )
        self.assertEqual(self._stored(), before)

    def test_a_failed_retitle_or_edit_keeps_text_and_consent_privately(
        self,
    ) -> None:
        for column, method, action, message, logged, shown in (
            (
                "title",
                "retitle_note_reporting_consent",
                "retitle",
                "Unable to retitle note.",
                "Failed to retitle note.",
                "Note retitle failed: Unable to retitle note.",
            ),
            (
                "body",
                "edit_note_reporting_consent",
                "edit",
                "Unable to edit note.",
                "Failed to edit note.",
                "Note edit failed: Unable to edit note.",
            ),
        ):
            with self.subTest(column=column):
                note_id = self._approved_note()
                replacement = f"new {column} {FAILURE_SENTINEL}"
                self.refuse_when(
                    f"refuse_{column}",
                    f"UPDATE OF {column} ON notes",
                    f"instr(NEW.{column}, '{FAILURE_SENTINEL}') > 0",
                )
                self.assert_sqlite_quotes_the_sentinel(
                    f"UPDATE notes SET {column} = '{replacement}' "
                    f"WHERE id = {note_id}"
                )
                before = self._stored()

                self.assert_fixed_failure(
                    lambda: getattr(self.memory, method)(note_id, replacement),
                    message,
                    logged,
                )
                self.assert_displayed(
                    self.tool.execute(
                        action,
                        {"id": note_id, column: replacement},
                    ).message,
                    shown,
                )
                self.assertEqual(self._stored(), before)
                self.raw(f"DROP TRIGGER refuse_{column}")

    def test_the_owner_sees_and_the_log_keeps_only_fixed_text(self) -> None:
        provider = ScriptedProvider(lambda request: "reply")
        harness = _Harness(
            self,
            provider,
            logger=logging.getLogger("maria.tests.note_consent.malformed"),
        )
        note_id = harness.memory.create_note("placeholder", "body")
        with closing(sqlite3.connect(harness.database_path)) as connection:
            connection.execute(
                "UPDATE notes SET title = CAST(? AS TEXT), context_digest = ? "
                "WHERE id = ?",
                (MALFORMED_TEXT, "a" * 64, note_id),
            )
            connection.commit()
        session = harness.session()

        with self.assertLogs("maria", level="DEBUG") as captured:
            pending = session.submit(f"enable note context {note_id}")
            confirmed = session.confirm()
            conversational = session.submit("tell me something private")

        self.assertEqual(pending.text, ENABLE_CONFIRMATION)
        self.assertEqual(
            confirmed.text,
            "Note context change failed: Unable to change note context consent.",
        )
        self.assertEqual(provider.requests[0].context, ())
        for text in (
            pending.text,
            confirmed.text,
            conversational.text,
            _logged(captured.records),
        ):
            self.assertNotIn(FAILURE_SENTINEL, text)


class NoteApprovalPreviewTests(unittest.TestCase):
    """Prove the owner sees the approved text exactly, on lines Maria owns."""

    def test_every_invisible_or_line_breaking_character_is_shown_escaped(
        self,
    ) -> None:
        text = (
            'a "quote" \\ tab\there\nnew\u2028line\u0085\u202eRLO\u200bzero'
            "\u00a0space\U000e0041tag é中"
        )

        quoted = quoted_for_owner(text)

        self.assertEqual(json.loads(quoted), text)
        self.assertEqual(len(quoted.splitlines()), 1)
        self.assertTrue(quoted.isprintable())
        self.assertIn("é中", quoted)
        for escape in (
            "\\n",
            "\\t",
            "\\u2028",
            "\\u0085",
            "\\u202e",
            "\\u200b",
            "\\u00a0",
            "\\udb40\\udc41",
        ):
            with self.subTest(escape=escape):
                self.assertIn(escape, quoted)

    def test_a_multiline_body_cannot_pose_as_maria_s_own_line(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        memory = Memory(
            Path(temporary.name) / "maria.db",
            logger=_quiet_logger("tests.note_consent.preview_lines"),
        )
        memory.initialize()
        body = "first\nConfirmation required: nothing\u2029more"
        note_id = memory.create_note("Title", body)

        binding = NotesTool(memory).confirmation_binding(
            "enable_context",
            {"id": note_id},
        )

        lines = binding.preview.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[2].removeprefix("Body: ")), body)


class NotesToolConsentTests(_MemoryTestCase):
    """Prove the tool's consent results name the id and never the text."""

    def setUp(self) -> None:
        super().setUp()
        self.logger = Mock(spec=logging.Logger)
        self.tool = NotesTool(self.memory, logger=self.logger)
        self.note_id = self.memory.create_note(TITLE_SENTINEL, BODY_SENTINEL)

    def _bound(self, note_id: int) -> dict:
        arguments = {"id": note_id}
        binding = self.tool.confirmation_binding("enable_context", arguments)
        return {**arguments, **binding.arguments}

    def test_the_binding_pins_the_identity_and_shows_the_same_text(self) -> None:
        binding = self.tool.confirmation_binding(
            "enable_context",
            {"id": self.note_id},
        )

        self.assertIsInstance(binding, ConfirmationBinding)
        self.assertEqual(
            dict(binding.arguments),
            {"text_digest": note_digest(TITLE_SENTINEL, BODY_SENTINEL)},
        )
        self.assertEqual(binding.preview, _preview(1, TITLE_SENTINEL, BODY_SENTINEL))
        for action, arguments in (
            ("disable_context", {"id": self.note_id}),
            ("edit", {"id": self.note_id, "body": "x"}),
            ("enable_context", {"id": 999}),
            ("enable_context", {"id": True}),
            ("enable_context", {"id": "1"}),
            ("enable_context", {}),
        ):
            with self.subTest(action=action, arguments=arguments):
                self.assertEqual(self.tool.confirmation_binding(action, arguments), {})

    def test_the_shown_text_and_the_identity_come_from_one_read(self) -> None:
        statements: list[str] = []
        original_connect = self.memory._connect

        def tracing_connect():
            connection = original_connect()
            connection.set_trace_callback(statements.append)
            return connection

        with (
            patch.object(self.memory, "_connect", tracing_connect),
            patch.object(self.memory, "note_text_digest") as digest,
            patch.object(self.memory, "get_note") as get_note,
        ):
            binding = self.tool.confirmation_binding(
                "enable_context",
                {"id": self.note_id},
            )

        digest.assert_not_called()
        get_note.assert_not_called()
        reads = [statement for statement in statements if "FROM notes" in statement]
        self.assertEqual(len(reads), 1)
        self.assertIn("SELECT title, body", reads[0])
        self.assertEqual(
            binding.arguments["text_digest"],
            note_digest(TITLE_SENTINEL, BODY_SENTINEL),
        )

    def test_a_failed_binding_adds_nothing_and_logs_only_the_id(self) -> None:
        with patch.object(
            self.memory,
            "note_snapshot",
            side_effect=RuntimeError("Unable to read note."),
        ):
            added = self.tool.confirmation_binding("enable_context", {"id": 1})

        self.assertEqual(added, {})
        self.logger.warning.assert_called_once_with(
            "Could not bind context approval for note %s.",
            1,
        )

    def test_enable_and_disable_answer_with_the_id_only(self) -> None:
        enabled = self.tool.execute("enable_context", self._bound(self.note_id))
        disabled = self.tool.execute("disable_context", {"id": self.note_id})

        self.assertEqual(
            enabled,
            ToolExecutionResult(
                True,
                f"Enabled context for note {self.note_id}.",
                {"id": self.note_id, "context_enabled": True},
            ),
        )
        self.assertEqual(
            disabled,
            ToolExecutionResult(
                True,
                f"Disabled context for note {self.note_id}.",
                {"id": self.note_id, "context_enabled": False},
            ),
        )
        logged = " ".join(str(call) for call in self.logger.method_calls)
        for private in (TITLE_SENTINEL, BODY_SENTINEL):
            self.assertNotIn(private, logged)

    def test_an_unbound_or_stale_approval_is_refused_and_says_how_to_retry(
        self,
    ) -> None:
        unbound = self.tool.execute("enable_context", {"id": self.note_id})
        bound = self._bound(self.note_id)
        self.memory.edit_note(self.note_id, REPLACEMENT_SENTINEL)
        stale = self.tool.execute("enable_context", bound)

        for result in (unbound, stale):
            self.assertEqual(result, ToolExecutionResult(False, _refused(self.note_id)))
        self.assertEqual(_raw_consent(self.database_path), [(self.note_id, None)])

    def test_missing_notes_and_bad_ids_are_refused(self) -> None:
        for action in ("enable_context", "disable_context"):
            with self.subTest(action=action):
                self.assertEqual(
                    self.tool.execute(action, {"id": 999}),
                    ToolExecutionResult(False, "Note not found: 999."),
                )
                for bad in (0, -1, True, "1", None):
                    self.assertEqual(
                        self.tool.execute(action, {"id": bad}),
                        ToolExecutionResult(
                            False,
                            "Note id must be a positive whole number.",
                        ),
                    )

    def test_retitle_and_edit_say_consent_was_turned_off_only_when_it_was_on(
        self,
    ) -> None:
        self.assertEqual(
            self.tool.execute("edit", {"id": self.note_id, "body": "Plain edit"}),
            ToolExecutionResult(True, f"Edited note {self.note_id}.", {"id": 1}),
        )
        self.assertEqual(
            self.tool.execute("retitle", {"id": self.note_id, "title": "Plain"}),
            ToolExecutionResult(True, f"Retitled note {self.note_id}.", {"id": 1}),
        )

        self.tool.execute("enable_context", self._bound(self.note_id))
        edited = self.tool.execute("edit", {"id": self.note_id, "body": "Changed"})
        self.tool.execute("enable_context", self._bound(self.note_id))
        retitled = self.tool.execute(
            "retitle",
            {"id": self.note_id, "title": "Changed"},
        )

        self.assertEqual(
            edited.message,
            "Edited note 1. Context consent for note 1 was turned off because "
            "its body changed.",
        )
        self.assertEqual(
            retitled.message,
            "Retitled note 1. Context consent for note 1 was turned off "
            "because its title changed.",
        )
        self.assertEqual(_raw_consent(self.database_path), [(self.note_id, None)])

    def test_context_change_failures_become_controlled_results(self) -> None:
        for method, action in (
            ("enable_note_context", "enable_context"),
            ("disable_note_context", "disable_context"),
        ):
            with self.subTest(action=action):
                with patch.object(
                    self.memory,
                    method,
                    side_effect=RuntimeError("Unable to change note context consent."),
                ):
                    result = self.tool.execute(action, {"id": self.note_id})
                self.assertEqual(
                    result,
                    ToolExecutionResult(
                        False,
                        "Note context change failed: Unable to change note "
                        "context consent.",
                    ),
                )


class NoteConsentCommandTests(unittest.TestCase):
    """Prove the planner routes the two commands and nothing else."""

    def setUp(self) -> None:
        self.planner = Planner(logger=_quiet_logger("tests.note_consent.planner"))

    def test_both_commands_plan_one_step_by_id(self) -> None:
        for command, action, note_id in (
            ("enable note context 3", "enable_context", 3),
            ("ENABLE Note Context 12", "enable_context", 12),
            ("enable note context   007  ", "enable_context", 7),
            ("disable note context 4", "disable_context", 4),
        ):
            with self.subTest(command=command):
                (step,) = self.planner.create_plan(command).steps
                self.assertEqual(
                    (step.tool_name, step.action, dict(step.arguments)),
                    ("notes", action, {"id": note_id}),
                )

    def test_usage_errors_and_bad_ids_plan_nothing(self) -> None:
        for command, response in (
            ("enable note context", "Use: enable note context <id>"),
            ("disable note context", "Use: disable note context <id>"),
            ("enable note context 0", "Note id must be a positive whole number."),
            ("enable note context -1", "Note id must be a positive whole number."),
            ("enable note context abc", "Note id must be a positive whole number."),
            ("enable note context ٣", "Note id must be a positive whole number."),
            ("disable note context 1 2", "Note id must be a positive whole number."),
        ):
            with self.subTest(command=command):
                plan = self.planner.create_plan(command)
                self.assertEqual(plan.steps, ())
                self.assertEqual(plan.response_if_no_action, response)

    def test_help_lists_both_commands(self) -> None:
        text = self.planner.create_plan("help").response_if_no_action

        self.assertIn("- enable note context <id>\n", text)
        self.assertIn("- disable note context <id>\n", text)


class NoteConsentPolicyTests(unittest.TestCase):
    """Prove the permission floor, the notice, and the contract."""

    def test_both_actions_are_mandatory_and_configuration_cannot_drop_them(
        self,
    ) -> None:
        manager = PermissionManager(
            frozenset(),
            frozenset(),
            logger=_quiet_logger("tests.note_consent.permissions"),
        )

        for action in ("enable_context", "disable_context"):
            with self.subTest(action=action):
                self.assertIn(f"notes.{action}", MANDATORY_CONFIRMATION_ACTIONS)
                self.assertIs(
                    manager.check("notes", action).decision,
                    PermissionDecision.REQUIRES_CONFIRMATION,
                )
        self.assertEqual(CONFIRMATION_NOTICES["notes.enable_context"], ENABLE_NOTICE)
        self.assertNotIn("notes.disable_context", CONFIRMATION_NOTICES)

    def test_the_notice_claims_no_secret_detection(self) -> None:
        self.assertIn("entire title and body", ENABLE_NOTICE)
        self.assertIn("does not check the note for passwords", ENABLE_NOTICE)
        for claim in ("detect", "scan", "safe", "verified"):
            self.assertNotIn(claim, ENABLE_NOTICE)

    def test_the_contract_takes_an_id_and_an_optional_exact_identity(self) -> None:
        enable = BUILT_IN_ACTION_CONTRACTS.contract_for("notes", "enable_context")
        disable = BUILT_IN_ACTION_CONTRACTS.contract_for("notes", "disable_context")

        self.assertEqual(
            [argument.name for argument in enable.arguments],
            ["id", "text_digest"],
        )
        self.assertEqual([argument.name for argument in disable.arguments], ["id"])
        digest = enable.arguments[1]
        self.assertFalse(digest.required)
        self.assertEqual(
            (digest.minimum_length, digest.maximum_length),
            (FACT_DIGEST_LENGTH, FACT_DIGEST_LENGTH),
        )
        for arguments in (
            {"id": 1, "text_digest": "a" * 63},
            {"id": 1, "fact_digest": "a" * 64},
            {"id": 0},
        ):
            with self.subTest(arguments=arguments):
                self.assertIsNotNone(
                    BUILT_IN_ACTION_CONTRACTS.validate(
                        "notes",
                        "enable_context",
                        arguments,
                    )
                )


class NoteConsentSessionTests(unittest.TestCase):
    """Prove the whole confirmed flow through a real session."""

    def setUp(self) -> None:
        self.provider = ScriptedProvider(lambda request: "reply")
        self.harness = _Harness(self, self.provider)
        self.session = self.harness.session()
        self.session.submit(f"create note {TITLE_SENTINEL}: {BODY_SENTINEL}")

    def test_enabling_shows_the_exact_title_and_body_with_the_notice(self) -> None:
        pending = self.session.submit("enable note context 1")

        self.assertTrue(pending.needs_confirmation)
        self.assertEqual(
            pending.text,
            f"{ENABLE_CONFIRMATION}\n{_preview(1, TITLE_SENTINEL, BODY_SENTINEL)}",
        )
        (step,) = self.session.pending_approval.plan.steps
        self.assertEqual(
            dict(step.arguments),
            {"id": 1, "text_digest": note_digest(TITLE_SENTINEL, BODY_SENTINEL)},
        )
        self.assertEqual(_raw_consent(self.harness.database_path), [(1, None)])

        self.assertTrue(self.session.cancel())
        self.assertEqual(_raw_consent(self.harness.database_path), [(1, None)])

        self.session.submit("enable note context 1")
        self.assertEqual(self.session.confirm().text, "Enabled context for note 1.")
        self.assertEqual(
            _raw_consent(self.harness.database_path),
            [(1, note_digest(TITLE_SENTINEL, BODY_SENTINEL))],
        )

    def test_the_shown_text_reaches_no_plan_history_log_model_or_conversation(
        self,
    ) -> None:
        provider = ScriptedProvider(lambda request: "reply")
        harness = _Harness(
            self,
            provider,
            logger=logging.getLogger("maria.tests.note_consent.preview"),
        )
        session = harness.session()
        harness.memory.create_note(TITLE_SENTINEL, BODY_SENTINEL)

        with self.assertLogs("maria", level="DEBUG") as captured:
            shown = session.submit("enable note context 1").text
            held = session.pending_approval
            session.cancel()
            session.submit("hello there friend")
            session.submit("enable note context 1")
            confirmed = session.confirm().text
            session.submit("hello again friend")
        digest = note_digest(TITLE_SENTINEL, BODY_SENTINEL)
        pending = repr(held)

        self.assertIn(f'Title: "{TITLE_SENTINEL}"', shown)
        self.assertIn(f'Body: "{BODY_SENTINEL}"', shown)
        self.assertEqual(confirmed, "Enabled context for note 1.")
        # The approval binds the identity of the shown text, never the text:
        # the digest is pinned in the held plan's one step, while the held
        # approval's own repr names its type and nothing it holds.
        self.assertEqual(
            dict(held.plan.steps[0].arguments),
            {"id": 1, "text_digest": digest},
        )
        self.assertEqual(pending, "PendingApproval(<redacted>)")
        self.assertEqual(provider.calls, 2)
        kept = "".join(
            f"{exchange.user}{exchange.assistant}" for exchange in session.conversation
        )
        places = {
            "pending approval": pending,
            "pending plan": repr(held.plan),
            "interaction history": repr(harness.interactions()),
            "log": _logged(captured.records),
            "model requests": everything_sent(provider.requests),
            "retained conversation": kept,
        }
        for place, text in places.items():
            for private in (TITLE_SENTINEL, BODY_SENTINEL):
                with self.subTest(place=place, private=private):
                    self.assertNotIn(private, text)
        # The held plan's rendering is live: it shows the digest it binds, so
        # the absence of the title and body from it above is not vacuous.
        self.assertIn(digest, places["pending plan"])
        self.assertNotIn(digest, places["pending approval"])
        self.assertNotIn(digest, places["log"])
        self.assertNotIn(digest, places["interaction history"])

    def test_disabling_also_needs_confirmation(self) -> None:
        _enable_note(self.harness.memory, 1)

        pending = self.session.submit("disable note context 1")

        self.assertEqual(pending.text, DISABLE_CONFIRMATION)
        self.assertEqual(self.session.confirm().text, "Disabled context for note 1.")
        self.assertEqual(_raw_consent(self.harness.database_path), [(1, None)])

    def test_a_title_or_body_changed_elsewhere_before_approval_is_refused(
        self,
    ) -> None:
        other = Memory(
            self.harness.database_path,
            logger=_quiet_logger("tests.note_consent.session"),
        )
        for method in ("retitle_note", "edit_note"):
            with self.subTest(method=method):
                self.session.submit("enable note context 1")
                getattr(other, method)(1, REPLACEMENT_SENTINEL)

                self.assertEqual(self.session.confirm().text, _refused(1))
                self.assertEqual(_raw_consent(self.harness.database_path), [(1, None)])

    def test_consent_rows_record_the_id_and_never_the_text_or_identity(self) -> None:
        self.session.submit("enable note context 1")
        digest = self.session.pending_approval.plan.steps[0].arguments["text_digest"]
        self.session.confirm()
        self.session.submit("disable note context 1")
        self.session.confirm()

        rows = [
            row
            for row in self.harness.interactions()
            if "note context" in row[0]
        ]
        self.assertEqual(
            rows,
            [
                ("enable note context 1", "Enabled context for note 1."),
                ("disable note context 1", "Disabled context for note 1."),
            ],
        )
        for row in self.harness.interactions()[1:]:
            for private in (TITLE_SENTINEL, BODY_SENTINEL, digest):
                self.assertNotIn(private, "".join(row))

    def test_the_consent_commands_never_reach_the_model(self) -> None:
        for command in (
            "enable note context 1",
            "disable note context 1",
            "enable note context",
            "enable note context x",
        ):
            self.session.submit(command)
            self.session.cancel()

        self.assertEqual(self.provider.calls, 0)


if __name__ == "__main__":
    unittest.main()
