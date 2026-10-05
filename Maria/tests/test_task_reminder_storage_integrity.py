"""Focused tests for what SQL, Maria's API, and her reads each guarantee.

The two V1-22 tables check only a few shapes in SQL: non-empty text by its
bytes, the status set, the date and instant shapes, and the digest length.
Every other rule is Maria's own, enforced before a write and again after a
read. The tests keep the three apart: what SQL refuses, what SQL accepts by
design and the memory layer refuses before any SQL runs, and what a row
inserted from outside Maria is when it is read back: refused privately, as a
whole, with a fixed message, and never repaired. Each runs on a new and on a
migrated database, in UTF-8 and in UTF-16, all temporary.
"""

from __future__ import annotations

import logging
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from core import memory as memory_module
from core.context import (
    ContextConsentOutcome,
    reminder_digest,
    summary_digest,
    task_digest,
)
from core.memory import Memory
from core.task_records import (
    AS_OF_DATE_MESSAGE,
    AS_OF_INSTANT_MESSAGE,
    CONTEXT_DIGEST_MESSAGE,
    DUE_DATE_MESSAGE,
    REMIND_TIME_MESSAGE,
    REMINDER_ID_MESSAGE,
    TASK_ID_MESSAGE,
    TASK_LIST_LIMIT_MESSAGE,
    TASK_STATUS_MESSAGE,
    TASK_TEXT_MESSAGE,
    RecordUpdate,
    StatusChange,
)
from tests.test_conversation_summaries import (
    _database_encoding,
    _start_baseline_database,
    _start_empty_database,
)
from tests.zone_fixtures import (
    CENTRAL_RULES,
    FIXED_PLUS_THREE_RULES,
    SYNTHETIC_KEY,
    SyntheticZones,
)
from tools.reminders import RemindersTool
from tools.tasks import TasksTool


STAMP = "2026-10-04T05:59:35Z"
NUL = "\x00"
TIME = f"2027-11-02T18:30:00+01:00[{SYNTHETIC_KEY}]"
INSTANT = "2027-11-02T17:30:00Z"
UNEXPECTED = "a stored value is not one Maria can show"
ROW_SENTINEL = "private-row-sentinel-6d02c1"

# Text SQL accepts by design and Maria's rules refuse.
SQL_ACCEPTED_TEXT = {
    "a NUL": f"buy{NUL}milk",
    "a control character": "buy\nmilk",
    "a format character": f"buy{chr(0x200B)}milk",
    "more than 512 bytes": "a" * 513,
    "leading whitespace": " buy milk",
    "trailing whitespace": "buy milk ",
}


class CastToBlob(str):
    """Valid text that ``insert_task`` and ``insert_reminder`` store as a BLOB.

    The date and instant checks read a BLOB as text, and a BLOB bound from
    Python can be checked in a different encoding from a stored one. Those
    helpers insert this value as ``CAST(? AS BLOB)`` on bound text instead,
    so SQLite itself makes a BLOB holding the text in the database's own
    encoding, which passes the shape check both as inserted and as stored, in
    UTF-8 and in UTF-16. ``raw`` binds it as plain text.
    """


def placeholders(values: dict[str, object]) -> str:
    """Return one placeholder per value, casting each ``CastToBlob`` value."""
    return ", ".join(
        "CAST(? AS BLOB)" if isinstance(value, CastToBlob) else "?"
        for value in values.values()
    )


class _StorageCase(unittest.TestCase):
    """One temporary database, new or migrated, in one text encoding."""

    encoding = "UTF-8"
    migrated = False

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database_path = Path(temporary.name) / "database" / "maria.db"
        if self.migrated:
            _start_baseline_database(self.database_path, self.encoding)
        else:
            _start_empty_database(self.database_path, self.encoding)
        self.logger = Mock(spec=logging.Logger)
        self.memory = Memory(self.database_path, logger=self.logger)
        self.memory.initialize()
        self.assertEqual(_database_encoding(self.database_path), self.encoding)
        self.assertEqual(self.memory.schema_version(), 7)
        self.zones = SyntheticZones(self, {SYNTHETIC_KEY: CENTRAL_RULES})

    def raw(self, statement: str, parameters: tuple[object, ...] = ()) -> int:
        """Run one statement on a second, raw connection; return the row id."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            cursor = connection.execute(statement, parameters)
            connection.commit()
            return cursor.lastrowid

    def insert_task(self, **changes: object) -> int:
        """Insert one task row from outside Maria, valid unless changed."""
        values: dict[str, object] = {
            "text": "buy milk",
            "due_date": "2026-10-09",
            "status": "open",
            "created_at": STAMP,
            "updated_at": STAMP,
            "completed_at": None,
            "context_digest": None,
        }
        values.update(changes)
        columns = ", ".join(values)
        marks = placeholders(values)
        return self.raw(
            f"INSERT INTO tasks ({columns}) VALUES ({marks})",
            tuple(values.values()),
        )

    def insert_reminder(self, **changes: object) -> int:
        """Insert one reminder row from outside Maria, valid unless changed."""
        values: dict[str, object] = {
            "text": "call mom",
            "remind_time": TIME,
            "remind_utc": INSTANT,
            "status": "open",
            "created_at": STAMP,
            "updated_at": STAMP,
            "completed_at": None,
            "context_digest": None,
        }
        values.update(changes)
        columns = ", ".join(values)
        marks = placeholders(values)
        return self.raw(
            f"INSERT INTO reminders ({columns}) VALUES ({marks})",
            tuple(values.values()),
        )

    def rows(self, table: str) -> list[tuple[object, ...]]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()

    def assertCheckRefuses(
        self,
        statement: str,
        parameters: tuple[object, ...],
    ) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            with self.assertRaises(sqlite3.IntegrityError) as caught:
                connection.execute(statement, parameters)
            connection.rollback()
        self.assertRegex(str(caught.exception), "(CHECK|NOT NULL) constraint failed")


class SqlGuaranteeTests(_StorageCase):
    """Prove exactly what SQL refuses, and what it accepts by design."""

    TASK_INSERT = (
        "INSERT INTO tasks (text, due_date, status, created_at, updated_at, "
        "context_digest) VALUES (?, ?, ?, ?, ?, ?)"
    )
    REMINDER_INSERT = (
        "INSERT INTO reminders (text, remind_time, remind_utc, status, "
        "created_at, updated_at, context_digest) VALUES (?, ?, ?, ?, ?, ?, ?)"
    )

    def test_the_checks_refuse_empty_text_bad_shapes_and_bad_digests(self) -> None:
        for label, parameters in (
            ("empty text", ("", None, "open", STAMP, STAMP, None)),
            ("an empty blob", (b"", None, "open", STAMP, STAMP, None)),
            ("a status outside the set", ("x", None, "done", STAMP, STAMP, None)),
            ("an unshaped date", ("x", "2026-1-9", "open", STAMP, STAMP, None)),
            ("a date word", ("x", "tomorrow", "open", STAMP, STAMP, None)),
            ("a short digest", ("x", None, "open", STAMP, STAMP, "a" * 63)),
            ("a missing text", (None, None, "open", STAMP, STAMP, None)),
        ):
            with self.subTest(table="tasks", case=label):
                self.assertCheckRefuses(self.TASK_INSERT, parameters)
        for label, parameters in (
            ("empty text", ("", TIME, INSTANT, "open", STAMP, STAMP, None)),
            (
                "a status outside the set",
                ("x", TIME, INSTANT, "due", STAMP, STAMP, None),
            ),
            (
                "an unshaped instant",
                ("x", TIME, "2027-11-02 17:30:00Z", "open", STAMP, STAMP, None),
            ),
            (
                "an instant with no zone letter",
                ("x", TIME, "2027-11-02T17:30:00", "open", STAMP, STAMP, None),
            ),
            ("a missing time", ("x", None, INSTANT, "open", STAMP, STAMP, None)),
            ("a long digest", ("x", TIME, INSTANT, "open", STAMP, STAMP, "a" * 65)),
        ):
            with self.subTest(table="reminders", case=label):
                self.assertCheckRefuses(self.REMINDER_INSERT, parameters)
        self.assertEqual(self.rows("tasks"), [])
        self.assertEqual(self.rows("reminders"), [])

    def test_sql_accepts_by_design_what_only_maria_refuses(self) -> None:
        for label, text in SQL_ACCEPTED_TEXT.items():
            with self.subTest(case=label):
                self.insert_task(text=text)
                self.insert_reminder(text=text)
        for label, changes in (
            ("an impossible date", {"due_date": "2026-02-30"}),
            ("an uppercase digest", {"context_digest": "A" * 64}),
            ("a completed task with no completion", {"status": "completed"}),
        ):
            with self.subTest(case=label):
                self.insert_task(**changes)
        for label, changes in (
            ("a malformed time", {"remind_time": "tomorrow at nine"}),
            ("an impossible instant", {"remind_utc": "2027-02-30T25:61:61Z"}),
            ("disagreeing values", {"remind_utc": "2027-11-02T16:30:00Z"}),
        ):
            with self.subTest(case=label):
                self.insert_reminder(**changes)
        self.assertEqual(len(self.rows("tasks")), len(SQL_ACCEPTED_TEXT) + 3)
        self.assertEqual(len(self.rows("reminders")), len(SQL_ACCEPTED_TEXT) + 3)

    def test_nul_text_is_stored_and_read_back_by_bytes(self) -> None:
        task_id = self.insert_task(text=f"{NUL}buy milk")
        with closing(sqlite3.connect(self.database_path)) as connection:
            (length,) = connection.execute(
                "SELECT length(CAST(text AS BLOB)) FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        self.assertGreater(length, 0)

    def test_no_check_beyond_the_documented_ones_exists(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            statements = dict(
                connection.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE name IN ('tasks', 'reminders')"
                ).fetchall()
            )
            others = connection.execute(
                "SELECT type, name FROM sqlite_master WHERE tbl_name IN "
                "('tasks', 'reminders') AND type != 'table'"
            ).fetchall()
        self.assertEqual(statements["tasks"].count("CHECK"), 4)
        self.assertEqual(statements["reminders"].count("CHECK"), 4)
        self.assertEqual(others, [])


class ApiValidationTests(_StorageCase):
    """Prove every write path refuses Maria's forbidden values before SQL."""

    def setUp(self) -> None:
        super().setUp()
        self.task_id = self.memory.create_task("buy milk", "2026-10-09")
        self.reminder_id = self.memory.create_reminder("call mom", TIME, INSTANT)
        self.before = (self.rows("tasks"), self.rows("reminders"))

    def assertRefusedBeforeSql(self, call: object, message: str) -> None:
        with patch.object(
            self.memory,
            "_connection",
            side_effect=AssertionError("no SQL may run"),
        ):
            with self.assertRaises(ValueError) as caught:
                call()
        self.assertEqual(str(caught.exception), message)
        self.assertEqual((self.rows("tasks"), self.rows("reminders")), self.before)

    def test_text_rules(self) -> None:
        memory = self.memory
        for label, text in SQL_ACCEPTED_TEXT.items():
            with self.subTest(case=label):
                for call in (
                    lambda: memory.create_task(text),
                    lambda: memory.edit_task(self.task_id, text),
                    lambda: memory.create_reminder(text, TIME, INSTANT),
                    lambda: memory.edit_reminder(self.reminder_id, text),
                ):
                    self.assertRefusedBeforeSql(call, TASK_TEXT_MESSAGE)

    def test_date_and_time_rules(self) -> None:
        memory = self.memory
        for due in ("2026-02-30", "none", "tomorrow", "2026-10-09T00:00:00Z"):
            with self.subTest(due=due):
                self.assertRefusedBeforeSql(
                    lambda: memory.create_task("x", due),
                    DUE_DATE_MESSAGE,
                )
                self.assertRefusedBeforeSql(
                    lambda: memory.reschedule_task(self.task_id, due),
                    DUE_DATE_MESSAGE,
                )
        for remind_time, remind_utc in (
            ("tomorrow at nine", INSTANT),
            (TIME, "2027-11-02T16:30:00Z"),
            (TIME, "2027-02-30T25:61:61Z"),
            (TIME.replace("[", "").replace("]", ""), INSTANT),
            (TIME.replace("+01:00", "-00:00"), "2027-11-02T18:30:00Z"),
        ):
            with self.subTest(remind_time=remind_time, remind_utc=remind_utc):
                self.assertRefusedBeforeSql(
                    lambda: memory.create_reminder("x", remind_time, remind_utc),
                    REMIND_TIME_MESSAGE,
                )
                self.assertRefusedBeforeSql(
                    lambda: memory.reschedule_reminder(
                        self.reminder_id,
                        remind_time,
                        remind_utc,
                    ),
                    REMIND_TIME_MESSAGE,
                )

    def test_query_ids_statuses_limits_and_digests(self) -> None:
        memory = self.memory
        for call, message in (
            (lambda: memory.due_tasks("2026-02-30"), AS_OF_DATE_MESSAGE),
            (lambda: memory.due_tasks(INSTANT), AS_OF_DATE_MESSAGE),
            (lambda: memory.due_reminders("2026-10-04"), AS_OF_INSTANT_MESSAGE),
            (lambda: memory.list_tasks("done"), TASK_STATUS_MESSAGE),
            (lambda: memory.list_reminders("pending"), TASK_STATUS_MESSAGE),
            (lambda: memory.list_tasks("open", 0), TASK_LIST_LIMIT_MESSAGE),
            (lambda: memory.list_tasks("open", 22), TASK_LIST_LIMIT_MESSAGE),
            (lambda: memory.list_reminders("open", True), TASK_LIST_LIMIT_MESSAGE),
            (lambda: memory.get_task(0), TASK_ID_MESSAGE),
            (lambda: memory.complete_task(True), TASK_ID_MESSAGE),
            (lambda: memory.delete_task(-1), TASK_ID_MESSAGE),
            (lambda: memory.get_reminder("1"), REMINDER_ID_MESSAGE),
            (lambda: memory.reopen_reminder(0), REMINDER_ID_MESSAGE),
            (
                lambda: memory.enable_task_context(self.task_id, "A" * 64),
                CONTEXT_DIGEST_MESSAGE,
            ),
            (
                lambda: memory.enable_reminder_context(self.reminder_id, "a" * 63),
                CONTEXT_DIGEST_MESSAGE,
            ),
        ):
            with self.subTest(message=message):
                self.assertRefusedBeforeSql(call, message)


class ExternalRowTests(_StorageCase):
    """Prove a row Maria could not have written is refused privately on read."""

    def assertRefusedOnRead(
        self,
        table: str,
        record_id: int,
        status: str = "open",
    ) -> None:
        before = self.database_path.read_bytes()
        self.logger.reset_mock()
        noun = "task" if table == "tasks" else "reminder"
        reads = (
            (
                f"read {noun}",
                lambda: getattr(self.memory, f"get_{noun}")(record_id),
            ),
            (
                f"list {table}",
                lambda: getattr(self.memory, f"list_{table}")(status),
            ),
        )
        for operation, call in reads:
            with self.subTest(operation=operation):
                with self.assertRaises(RuntimeError) as caught:
                    call()
                self.assertEqual(str(caught.exception), f"Unable to {operation}.")
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
        self.assertEqual(
            self.logger.error.call_args_list[-1].args,
            ("Failed to %s: %s.", reads[-1][0], UNEXPECTED),
        )
        logged = "\n".join(str(call) for call in self.logger.mock_calls)
        self.assertNotIn(ROW_SENTINEL, logged)
        self.assertEqual(self.database_path.read_bytes(), before)

    def assertDueRefused(self, table: str, as_of: str) -> None:
        """Assert the due query refuses alike, in the memory layer and tool."""
        before = self.database_path.read_bytes()
        self.logger.reset_mock()
        with self.assertRaises(RuntimeError) as caught:
            getattr(self.memory, f"due_{table}")(as_of)
        self.assertEqual(str(caught.exception), f"Unable to list due {table}.")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(
            self.logger.error.call_args_list[-1].args,
            ("Failed to %s: %s.", f"list due {table}", UNEXPECTED),
        )
        tool_class = TasksTool if table == "tasks" else RemindersTool
        result = tool_class(memory=self.memory, logger=self.logger).execute(
            "due",
            {"as_of": as_of},
        )
        self.assertEqual(
            (result.success, result.message),
            (False, f"Unable to list {table}."),
        )
        logged = "\n".join(str(call) for call in self.logger.mock_calls)
        self.assertNotIn(ROW_SENTINEL, logged)
        self.assertEqual(self.database_path.read_bytes(), before)

    def test_malformed_task_rows(self) -> None:
        for label, changes in (
            ("a NUL", {"text": f"{ROW_SENTINEL}{NUL}"}),
            ("a control character", {"text": f"{ROW_SENTINEL}\nmore"}),
            ("over-long text", {"text": ROW_SENTINEL + "a" * 512}),
            ("unstripped text", {"text": f" {ROW_SENTINEL}"}),
            ("text stored as a blob", {"text": ROW_SENTINEL.encode()}),
            ("an impossible date", {"text": ROW_SENTINEL, "due_date": "2026-02-30"}),
            (
                "completed without a time",
                {"text": ROW_SENTINEL, "status": "completed"},
            ),
            ("open with a completion", {"text": ROW_SENTINEL, "completed_at": STAMP}),
            ("an unreadable stamp", {"text": ROW_SENTINEL, "created_at": "yesterday"}),
            ("an uppercase digest", {"text": ROW_SENTINEL, "context_digest": "A" * 64}),
        ):
            with self.subTest(case=label):
                self.raw("DELETE FROM tasks")
                task_id = self.insert_task(**changes)
                status = str(changes.get("status", "open"))
                self.assertRefusedOnRead("tasks", task_id, status)
                if status == "open":
                    self.assertDueRefused("tasks", "2026-12-31")

    def test_malformed_reminder_rows(self) -> None:
        for label, changes in (
            ("unstripped text", {"text": f"{ROW_SENTINEL} "}),
            # Individually valid, an hour apart: the stored instant is not
            # the one the saved local time and offset encode.
            (
                "disagreeing values",
                {"text": ROW_SENTINEL, "remind_utc": "2027-11-02T16:30:00Z"},
            ),
            (
                "a malformed time",
                {"text": ROW_SENTINEL, "remind_time": "tomorrow 9:00"},
            ),
            (
                "an impossible instant",
                {"text": ROW_SENTINEL, "remind_utc": "2027-02-30T25:00:00Z"},
            ),
            (
                "a time with seconds",
                {
                    "text": ROW_SENTINEL,
                    "remind_time": TIME.replace("18:30:00", "18:30:15"),
                    "remind_utc": "2027-11-02T17:30:15Z",
                },
            ),
        ):
            with self.subTest(case=label):
                self.raw("DELETE FROM reminders")
                reminder_id = self.insert_reminder(**changes)
                self.assertRefusedOnRead("reminders", reminder_id)
                self.assertDueRefused("reminders", "2030-01-01T00:00:00Z")

    def test_a_malformed_row_makes_a_whole_listing_refuse(self) -> None:
        self.memory.create_task("valid one")
        self.insert_task(text=f" {ROW_SENTINEL}", due_date=None)
        with self.assertRaises(RuntimeError):
            self.memory.list_tasks("open")
        tool = TasksTool(memory=self.memory, logger=self.logger)
        self.assertEqual(
            tool.execute("list", {"status": "open"}).message,
            "Unable to list tasks.",
        )
        self.assertEqual(
            RemindersTool(memory=self.memory, logger=self.logger)
            .execute("read", {"id": self.insert_reminder(text=f"{ROW_SENTINEL} ")})
            .message,
            "Unable to read the reminder.",
        )

    def test_context_reads_skip_rows_maria_could_not_have_written(self) -> None:
        valid = self.memory.create_task("valid", "2026-10-09")
        self.assertIs(
            self.memory.enable_task_context(valid, task_digest("valid", "2026-10-09")),
            ContextConsentOutcome.ENABLED,
        )
        bad_text = f" {ROW_SENTINEL}"
        self.insert_task(
            text=bad_text,
            due_date=None,
            context_digest=task_digest(bad_text, None),
        )
        self.insert_reminder(
            remind_utc="2027-11-02T16:30:00Z",
            context_digest=reminder_digest("call mom", TIME),
        )

        self.assertEqual(
            self.memory.list_context_tasks(16, 2048),
            [{"id": valid, "text": "valid", "due": "2026-10-09"}],
        )
        self.assertEqual(self.memory.list_context_reminders(16, 2048), [])
        self.assertEqual(
            self.memory.context_tasks_by_id([valid, 2], 2048)[0]["id"],
            valid,
        )
        self.assertEqual(self.memory.context_reminders_by_id([1], 2048), [])


class RuleChangeStorageTests(_StorageCase):
    """Prove a later rule change never rewrites, refuses, or reorders."""

    def test_saved_values_order_and_due_selection_survive_a_rule_change(self) -> None:
        later = self.memory.create_reminder(
            "later",
            f"2027-11-02T19:00:00+01:00[{SYNTHETIC_KEY}]",
            "2027-11-02T18:00:00Z",
        )
        earlier = self.memory.create_reminder("earlier", TIME, INSTANT)
        before = self.database_path.read_bytes()

        self.zones.write(SYNTHETIC_KEY, FIXED_PLUS_THREE_RULES)

        self.assertEqual(
            [reminder.reminder_id for reminder in self.memory.list_reminders("open")],
            [earlier, later],
        )
        self.assertEqual(
            [reminder.reminder_id for reminder in self.memory.due_reminders(INSTANT)],
            [earlier],
        )
        saved = self.memory.get_reminder(earlier)
        self.assertEqual((saved.remind_time, saved.remind_utc), (TIME, INSTANT))
        self.assertTrue(
            RemindersTool(memory=self.memory, logger=self.logger)
            .execute("read", {"id": earlier})
            .success
        )
        self.assertEqual(self.database_path.read_bytes(), before)

    def test_consent_survives_a_rule_change(self) -> None:
        reminder_id = self.memory.create_reminder("call mom", TIME, INSTANT)
        self.memory.enable_reminder_context(
            reminder_id,
            reminder_digest("call mom", TIME),
        )

        self.zones.write(SYNTHETIC_KEY, FIXED_PLUS_THREE_RULES)

        self.assertTrue(self.memory.get_reminder(reminder_id).context_enabled)
        self.assertEqual(
            self.memory.list_context_reminders(16, 2048),
            [{"id": reminder_id, "text": "call mom", "when": TIME}],
        )


class AgendaRewriteAtomicityTests(_StorageCase):
    """Prove consent goes off in the same transaction as new text or timing.

    A trigger makes the write of the new value fail after consent has been
    turned off in the same transaction, so the whole change must roll back:
    the consent, the old value, and the update stamp all stay as they were.
    """

    LATER = f"2027-11-03T18:30:00+01:00[{SYNTHETIC_KEY}]"
    LATER_INSTANT = "2027-11-03T17:30:00Z"

    def refuse_updates_of(self, table: str, column: str) -> None:
        self.raw(
            f"CREATE TRIGGER refuse_{column} BEFORE UPDATE OF {column} ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'refused'); END"
        )
        self.addCleanup(self.raw, f"DROP TRIGGER IF EXISTS refuse_{column}")

    def test_a_failed_task_edit_or_reschedule_changes_nothing(self) -> None:
        task_id = self.memory.create_task("buy milk", "2026-10-09")
        self.assertIs(
            self.memory.enable_task_context(
                task_id,
                task_digest("buy milk", "2026-10-09"),
            ),
            ContextConsentOutcome.ENABLED,
        )
        before = self.rows("tasks")
        for column, operation, change in (
            ("text", "edit", lambda: self.memory.edit_task(task_id, "buy oat milk")),
            (
                "due_date",
                "reschedule",
                lambda: self.memory.reschedule_task(task_id, "2026-10-10"),
            ),
        ):
            with self.subTest(column=column):
                self.refuse_updates_of("tasks", column)
                with self.assertRaises(RuntimeError) as caught:
                    change()
                self.assertEqual(str(caught.exception), f"Unable to {operation} task.")
                self.assertEqual(self.rows("tasks"), before)
                self.raw(f"DROP TRIGGER refuse_{column}")

    def test_a_failed_reminder_edit_or_reschedule_changes_nothing(self) -> None:
        reminder_id = self.memory.create_reminder("call mom", TIME, INSTANT)
        self.assertIs(
            self.memory.enable_reminder_context(
                reminder_id,
                reminder_digest("call mom", TIME),
            ),
            ContextConsentOutcome.ENABLED,
        )
        before = self.rows("reminders")
        for column, operation, change in (
            (
                "text",
                "edit",
                lambda: self.memory.edit_reminder(reminder_id, "call dad"),
            ),
            (
                "remind_time",
                "reschedule",
                lambda: self.memory.reschedule_reminder(
                    reminder_id,
                    self.LATER,
                    self.LATER_INSTANT,
                ),
            ),
        ):
            with self.subTest(column=column):
                self.refuse_updates_of("reminders", column)
                with self.assertRaises(RuntimeError) as caught:
                    change()
                self.assertEqual(
                    str(caught.exception),
                    f"Unable to {operation} reminder.",
                )
                self.assertEqual(self.rows("reminders"), before)
                self.raw(f"DROP TRIGGER refuse_{column}")

    def test_a_reminder_edit_and_a_reschedule_each_turn_consent_off(self) -> None:
        reminder_id = self.memory.create_reminder("call mom", TIME, INSTANT)
        for change, text, timing in (
            (
                lambda: self.memory.edit_reminder(reminder_id, "call dad"),
                "call dad",
                TIME,
            ),
            (
                lambda: self.memory.reschedule_reminder(
                    reminder_id,
                    self.LATER,
                    self.LATER_INSTANT,
                ),
                "call dad",
                self.LATER,
            ),
        ):
            with self.subTest(text=text, timing=timing):
                current = self.memory.get_reminder(reminder_id)
                self.memory.enable_reminder_context(
                    reminder_id,
                    reminder_digest(current.text, current.remind_time),
                )
                self.assertTrue(self.memory.get_reminder(reminder_id).context_enabled)

                self.assertEqual(change(), RecordUpdate(True, True))

                (row,) = self.rows("reminders")
                self.assertEqual((row[1], row[2], row[8]), (text, timing, None))
                self.assertFalse(self.memory.get_reminder(reminder_id).context_enabled)


class IdentifierAndCascadeTests(_StorageCase):
    """Prove ids are never reused and no record type touches another."""

    def test_ids_are_never_reused_after_deleting_the_newest(self) -> None:
        for _ in range(3):
            self.memory.create_task("x")
            self.memory.create_reminder("y", TIME, INSTANT)
        self.assertTrue(self.memory.delete_task(3))
        self.assertTrue(self.memory.delete_reminder(3))

        self.assertEqual(self.memory.create_task("x"), 4)
        self.assertEqual(self.memory.create_reminder("y", TIME, INSTANT), 4)
        self.assertFalse(self.memory.delete_task(3))
        self.assertIs(self.memory.complete_task(3), StatusChange.NOT_FOUND)
        self.assertIsNone(self.memory.get_reminder(3))

    def test_no_change_to_one_record_type_reaches_another(self) -> None:
        task_id = self.memory.create_task("shared words", "2026-10-09")
        reminder_id = self.memory.create_reminder("shared words", TIME, INSTANT)
        note_id = self.memory.create_note("shared words", "body")
        memory_id, _ = self.memory.store_memory("shared words")
        everything = {
            table: self.rows(table)
            for table in ("tasks", "reminders", "notes", "memories")
        }

        self.assertTrue(self.memory.delete_task(task_id))
        self.assertEqual(self.rows("reminders"), everything["reminders"])
        self.assertTrue(self.memory.delete_reminder(reminder_id))
        self.assertEqual(self.rows("notes"), everything["notes"])
        self.assertEqual(self.rows("memories"), everything["memories"])

        other_task = self.memory.create_task("kept")
        other_reminder = self.memory.create_reminder("kept", TIME, INSTANT)
        self.assertTrue(self.memory.delete_note(note_id))
        self.assertTrue(self.memory.forget_memory(memory_id))
        self.assertEqual(self.memory.get_task(other_task).text, "kept")
        self.assertEqual(self.memory.get_reminder(other_reminder).text, "kept")


# The longest value a canonical field can hold, in characters: a reminder
# time whose zone key has the full 64. Only the text may be longer.
LONGEST_CANONICAL_VALUE = 91
LONGEST_TIME = f"2027-11-02T18:30:00+01:00[Maria/{'k' * 58}]"
TEXT_BOUND = 2048
LARGE = 100_000
# A valid saved time and instant other than the approved ones.
OTHER_TIME = f"2027-11-03T18:30:00+01:00[{SYNTHETIC_KEY}]"
OTHER_INSTANT = "2027-11-03T17:30:00Z"

# Each value the check receives, by position, keyed by how many it receives,
# with its own bound in characters, written out by hand: a task's text, due
# date, created, updated, and completion stamps, and identity, and a
# reminder's text, saved time, instant, stamps, and identity. Every canonical
# value is ASCII, so characters are its UTF-8 bytes, and a UTF-16 file stores
# twice as many. None means the value must be NULL: an open record has no
# completion stamp. Only a task's due date may otherwise be NULL.
CHECKED_BOUNDS = {
    6: (TEXT_BOUND, 10, 20, 20, None, 64),
    7: (TEXT_BOUND, 91, 20, 20, 20, None, 64),
}
NULLABLE_POSITIONS = {(6, 1)}


def suffixed(value: str) -> str:
    """Return a valid-looking value followed by NUL and a large suffix.

    SQLite's ``length()`` and ``GLOB`` stop at the NUL, so the schema's own
    checks see only the valid-looking prefix and store the whole value.
    """
    return value + NUL + "x" * LARGE


class ContextReadBoundaryTests(_StorageCase):
    """Prove a Tier A read hands its Python check only typed, bounded values.

    A context read registers a check that SQLite calls for each candidate
    row. A row stored from outside Maria can hold what no schema check
    refuses and no canonical form allows: a valid-looking date, instant,
    stamp, or digest followed by NUL and a large suffix, an oversized saved
    time, a BLOB, a malformed stamp, or a completion time on an open record.
    The check may only ever see text within a small stored-byte bound, or
    NULL where the column allows it, and such a row is skipped before the
    limit is counted, so a valid later row is still selected and rechecked.
    """

    def setUp(self) -> None:
        super().setUp()
        self.seen: list[tuple[object, ...]] = []
        real = memory_module._approved_agenda_check

        def instrumented(*arguments: object) -> object:
            check = real(*arguments)

            def recorded(*values: object) -> object:
                self.seen.append(values)
                return check(*values)

            return recorded

        patcher = patch.object(memory_module, "_approved_agenda_check", instrumented)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertOnlyBoundedValuesWereChecked(self) -> None:
        # Positive control: the instrumented check is the one SQLite calls.
        self.assertTrue(self.seen)
        for values in self.seen:
            bounds = CHECKED_BOUNDS[len(values)]
            for position, (value, bound) in enumerate(zip(values, bounds)):
                if bound is None:
                    self.assertIsNone(value, position)
                elif value is None:
                    self.assertIn((len(values), position), NULLABLE_POSITIONS)
                else:
                    self.assertIs(type(value), str, position)
                    self.assertLessEqual(len(value), bound, position)

    def assertOnlyTheLaterRowIsEligible(
        self,
        table: str,
        bad_id: int,
        good_id: int,
        expected: dict[str, object],
    ) -> None:
        listing = getattr(self.memory, f"list_context_{table}")
        recheck = getattr(self.memory, f"context_{table}_by_id")

        # A limit of one: the refused earlier row does not use it up.
        self.assertEqual(listing(1, TEXT_BOUND), [expected])
        self.assertEqual(recheck((bad_id, good_id), TEXT_BOUND), [expected])
        self.assertOnlyBoundedValuesWereChecked()

    def test_malformed_task_values_never_reach_the_check(self) -> None:
        approved = task_digest("buy milk", "2026-10-09")
        later = task_digest("valid later", "2026-10-09")
        for label, changes in (
            ("oversized text", {"text": "x" * LARGE}),
            ("text one byte over its bound", {"text": "a" * (TEXT_BOUND + 1)}),
            ("text, NUL, and a large suffix", {"text": suffixed("buy milk")}),
            ("text as a BLOB", {"text": b"buy milk"}),
            ("a date one byte over its bound", {"due_date": "2026-10-09" + NUL}),
            ("a stamp one byte over its bound", {"created_at": STAMP + NUL}),
            ("an updated stamp one byte over", {"updated_at": STAMP + NUL}),
            ("a digest one byte over its bound", {"context_digest": approved + NUL}),
            ("an updated stamp, NUL, and a suffix", {"updated_at": suffixed(STAMP)}),
            ("an impossible created stamp", {"created_at": "2026-02-30T05:59:35Z"}),
            # The approved identity of other text, and an impossible date with
            # an identity of its own: the format and identity checks still run.
            ("an approved digest of other text", {"text": "buy oat milk"}),
            (
                "an impossible due date with its own digest",
                {
                    "due_date": "2026-02-30",
                    "context_digest": task_digest("buy milk", "2026-02-30"),
                },
            ),
            ("a date, NUL, and a large suffix", {"due_date": suffixed("2026-10-09")}),
            ("a date as a BLOB", {"due_date": CastToBlob("2026-10-09")}),
            (
                "a digest, NUL, and a large suffix",
                {"context_digest": suffixed(approved)},
            ),
            ("a digest as a BLOB", {"context_digest": approved.encode()}),
            ("a stamp, NUL, and a large suffix", {"created_at": suffixed(STAMP)}),
            ("an oversized stamp", {"updated_at": "x" * LARGE}),
            ("a created stamp as a BLOB", {"created_at": STAMP.encode()}),
            ("an updated stamp as a BLOB", {"updated_at": STAMP.encode()}),
            ("a malformed created stamp", {"created_at": "yesterday"}),
            ("a malformed updated stamp", {"updated_at": "2026-10-04 05:59:35"}),
            ("an open task with a completion time", {"completed_at": STAMP}),
            (
                "a completion, NUL, and a large suffix",
                {"completed_at": suffixed(STAMP)},
            ),
            ("a completion time as a BLOB", {"completed_at": STAMP.encode()}),
        ):
            with self.subTest(case=label):
                self.raw("DELETE FROM tasks")
                self.seen.clear()
                bad = self.insert_task(**{"context_digest": approved, **changes})
                good = self.insert_task(text="valid later", context_digest=later)

                self.assertOnlyTheLaterRowIsEligible(
                    "tasks",
                    bad,
                    good,
                    {"id": good, "text": "valid later", "due": "2026-10-09"},
                )

    def test_malformed_reminder_values_never_reach_the_check(self) -> None:
        approved = reminder_digest("call mom", TIME)
        later = reminder_digest("valid later", TIME)
        for label, changes in (
            ("oversized text", {"text": "x" * LARGE}),
            ("text as a BLOB", {"text": b"call mom"}),
            ("a saved time one byte over", {"remind_time": LONGEST_TIME + "x"}),
            ("an instant one byte over its bound", {"remind_utc": INSTANT + NUL}),
            ("a digest one byte over its bound", {"context_digest": approved + NUL}),
            ("a created stamp as a BLOB", {"created_at": STAMP.encode()}),
            ("a malformed created stamp", {"created_at": "yesterday"}),
            ("an updated stamp, NUL, and a suffix", {"updated_at": suffixed(STAMP)}),
            ("a completion, NUL, and a suffix", {"completed_at": suffixed(STAMP)}),
            # The approved identity of another valid time: the identity check
            # still runs after the bounded transfer.
            (
                "an approved digest of another time",
                {"remind_time": OTHER_TIME, "remind_utc": OTHER_INSTANT},
            ),
            ("an oversized saved time", {"remind_time": "x" * (10 * LARGE)}),
            ("a saved time, NUL, and a large suffix", {"remind_time": suffixed(TIME)}),
            ("a saved time as a BLOB", {"remind_time": TIME.encode()}),
            ("an instant, NUL, and a large suffix", {"remind_utc": suffixed(INSTANT)}),
            ("an instant as a BLOB", {"remind_utc": CastToBlob(INSTANT)}),
            (
                "a digest, NUL, and a large suffix",
                {"context_digest": suffixed(approved)},
            ),
            ("a digest as a BLOB", {"context_digest": approved.encode()}),
            ("a stamp, NUL, and a large suffix", {"created_at": suffixed(STAMP)}),
            ("an updated stamp as a BLOB", {"updated_at": STAMP.encode()}),
            ("a malformed updated stamp", {"updated_at": "tomorrow"}),
            ("an open reminder with a completion time", {"completed_at": STAMP}),
            ("a completion time as a BLOB", {"completed_at": STAMP.encode()}),
        ):
            with self.subTest(case=label):
                self.raw("DELETE FROM reminders")
                self.seen.clear()
                bad = self.insert_reminder(**{"context_digest": approved, **changes})
                good = self.insert_reminder(text="valid later", context_digest=later)

                self.assertOnlyTheLaterRowIsEligible(
                    "reminders",
                    bad,
                    good,
                    {"id": good, "text": "valid later", "when": TIME},
                )

    def test_values_at_their_longest_canonical_length_are_still_selected(
        self,
    ) -> None:
        # A zone key of the full 64 characters gives the longest saved time.
        longest = LONGEST_TIME
        self.assertEqual(len(longest), LONGEST_CANONICAL_VALUE)
        text = "a" * 512
        reminder_id = self.insert_reminder(
            text=text,
            remind_time=longest,
            context_digest=reminder_digest(text, longest),
        )
        task_id = self.insert_task(
            text=text,
            context_digest=task_digest(text, "2026-10-09"),
        )

        self.assertEqual(
            self.memory.list_context_reminders(1, TEXT_BOUND),
            [{"id": reminder_id, "text": text, "when": longest}],
        )
        self.assertEqual(
            self.memory.context_tasks_by_id((task_id,), TEXT_BOUND),
            [{"id": task_id, "text": text, "due": "2026-10-09"}],
        )
        self.assertOnlyBoundedValuesWereChecked()
        self.assertIn(longest, [value for values in self.seen for value in values])

    def test_valid_future_stamps_stay_eligible_and_no_clock_is_read(self) -> None:
        # The record rules check a stamp's form, never its moment, so a
        # context read compares nothing with the current time.
        later = "2099-01-01T00:00:00Z"
        task_id = self.insert_task(
            created_at=later,
            updated_at=later,
            context_digest=task_digest("buy milk", "2026-10-09"),
        )

        with patch.object(
            memory_module,
            "task_timestamp",
            side_effect=AssertionError("a clock was read"),
        ):
            self.assertEqual(
                self.memory.list_context_tasks(1, TEXT_BOUND),
                [{"id": task_id, "text": "buy milk", "due": "2026-10-09"}],
            )
        self.assertOnlyBoundedValuesWereChecked()

    def test_summary_context_reads_still_ignore_their_stamps(self) -> None:
        # The stricter rules cover tasks and reminders only.
        body = "We agreed to meet on Friday."
        summary_id = self.raw(
            """
            INSERT INTO conversation_summaries (
                body, created_at, updated_at, context_digest
            ) VALUES (?, ?, ?, ?)
            """,
            (body, "yesterday", "not a stamp", summary_digest(body)),
        )

        self.assertEqual(
            self.memory.context_summaries_by_id((summary_id,), TEXT_BOUND),
            [{"id": summary_id, "body": body, "oversized": False}],
        )


class UndecodableContextValueTests(_StorageCase):
    """Prove a stored value SQLite cannot decode fails the read, closed.

    A text value whose bytes are not valid UTF-8 passes the type and size
    guard, but cannot be handed to the Python check at all, so the whole
    read fails with its fixed error and nothing is returned, as any other
    record type's context read does. The value never becomes Python text.
    """

    def test_an_undecodable_stamp_fails_the_read_with_its_fixed_error(self) -> None:
        task_id = self.insert_task(
            context_digest=task_digest("buy milk", "2026-10-09"),
        )
        self.raw(
            "UPDATE tasks SET created_at = CAST(X'FF' AS TEXT) WHERE id = ?",
            (task_id,),
        )
        self.logger.reset_mock()

        for read in (
            lambda: self.memory.list_context_tasks(16, TEXT_BOUND),
            lambda: self.memory.context_tasks_by_id((task_id,), TEXT_BOUND),
        ):
            with self.assertRaises(RuntimeError) as caught:
                read()
            self.assertEqual(str(caught.exception), "Unable to read task context.")
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
        self.assertEqual(
            self.logger.error.call_args_list[-1].args,
            ("Failed to %s: %s.", "read task context", "database error"),
        )


class MigratedStorageTests:
    """Mixin marker: the same tests on a database migrated from the baseline."""

    migrated = True


class Utf16Mixin:
    """Mixin marker: the same tests on a UTF-16 database."""

    encoding = "UTF-16le"


class MigratedSqlGuaranteeTests(MigratedStorageTests, SqlGuaranteeTests):
    """SQL guarantees on a migrated UTF-8 database."""


class Utf16SqlGuaranteeTests(Utf16Mixin, SqlGuaranteeTests):
    """SQL guarantees on a new UTF-16 database."""


class MigratedUtf16SqlGuaranteeTests(
    MigratedStorageTests,
    Utf16Mixin,
    SqlGuaranteeTests,
):
    """SQL guarantees on a migrated UTF-16 database."""


class MigratedApiValidationTests(MigratedStorageTests, ApiValidationTests):
    """API validation on a migrated UTF-8 database."""


class Utf16ApiValidationTests(Utf16Mixin, ApiValidationTests):
    """API validation on a new UTF-16 database."""


class MigratedExternalRowTests(MigratedStorageTests, ExternalRowTests):
    """External rows on a migrated UTF-8 database."""


class Utf16ExternalRowTests(Utf16Mixin, ExternalRowTests):
    """External rows on a new UTF-16 database."""


class MigratedUtf16ExternalRowTests(
    MigratedStorageTests,
    Utf16Mixin,
    ExternalRowTests,
):
    """External rows on a migrated UTF-16 database."""


class MigratedContextReadBoundaryTests(
    MigratedStorageTests,
    ContextReadBoundaryTests,
):
    """Bounded context reads on a migrated UTF-8 database."""


class Utf16ContextReadBoundaryTests(Utf16Mixin, ContextReadBoundaryTests):
    """Bounded context reads on a new UTF-16 database."""


class MigratedUtf16ContextReadBoundaryTests(
    MigratedStorageTests,
    Utf16Mixin,
    ContextReadBoundaryTests,
):
    """Bounded context reads on a migrated UTF-16 database."""


if __name__ == "__main__":
    unittest.main()
