"""Focused tests for schema version 6 and its create_conversation_records step.

V1-20 appends one forward step to Maria's migration ladder. It creates the
two tables saved conversations and conversation summaries live in, empty,
and touches no existing table, row, or AUTOINCREMENT high-water mark. Every
database here is built under a ``TemporaryDirectory``, from the frozen
fixtures and from the shapes ``tests.test_audit_migration`` writes out by
hand, reused unchanged; the two new tables are pinned word for word there
as well. Every helper that opens or builds a database refuses Maria's live
database directory before anything is opened, and Maria's live log and
private configuration override are never read.
"""

from __future__ import annotations

import logging
import sqlite3
import unittest
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from core import memory as memory_module
from core.constants import (
    APP_VERSION,
    DATABASE_DIR,
    DATABASE_PATH,
    DATABASE_SCHEMA_VERSION,
)
from core.conversation_records import encode_transcript
from core.failures import Failure, FailureKind, TypedFailureError
from core.logger import configure_logging, get_logger
from core.memory import Memory
from tests.log_assertions import assert_log_omits
from tests.schema_fixtures import (
    BASELINE_INTERACTIONS,
    BASELINE_MEMORIES,
    BASELINE_NOTES,
    read_schema_version,
    table_names,
)
from tests.test_audit_migration import (
    ALL_STARTS,
    APPLIED_STEP_LINES,
    AUDIT_SUMMARIES_VERSION,
    AUDIT_TABLES,
    BASELINE_COUNTS,
    CLASSIFIED_LINES,
    CONVERSATION_RECORDS_VERSION,
    CONVERSATION_TABLES,
    CREATED_LINE,
    CURRENT_SCHEMA,
    CURRENT_SCHEMA_VERSION,
    CURRENT_TABLES,
    DATABASE_BUSY_PATTERN,
    DATABASE_REFUSED_PATTERN,
    EXPECTED_FROZEN_SCHEMAS,
    EXPECTED_LADDER,
    FIXED_AUDIT_TIMESTAMP,
    FROZEN_CONVERSATION_SUMMARIES_TABLE,
    FROZEN_SAVED_CONVERSATIONS_TABLE,
    LIVE_DATABASE_PATTERN,
    LOGGING_CONFIGURED_ENTRY,
    MIGRATED_INTERACTIONS,
    MIGRATED_MEMORIES,
    MIGRATED_NOTES,
    NEVER_COMMITTED,
    NEWER_SCHEMA_VERSION,
    READY_LINE,
    RECORD_TABLES,
    SHORT_BUSY_TIMEOUT_SECONDS,
    TEST_LOGGER,
    UNRECOGNISED_UNVERSIONED_LINE,
    UNVERSIONED,
    V5_SCHEMA,
    V6_SCHEMA,
    V9_SCHEMA,
    _applied,
    _build_schema,
    _build_unversioned_baseline,
    _describe,
    _execute,
    _hold_write_lock,
    _messages,
    _object_names,
    _query,
    _record_rows,
    _refuse_live_database,
    _row_counts,
    _sequences,
    _siblings,
    _Start,
    _TemporaryDatabaseTestCase,
)


STEP_NAME = "create_conversation_records"
APPLIED_LINE = APPLIED_STEP_LINES[STEP_NAME]
# V1-22, V1-23-R1, and V1-24-R1 each append one step after this one, so this
# step is found by name.
TASK_STEP_NAME = "create_tasks_and_reminders"
TASK_STEP_LINE = APPLIED_STEP_LINES[TASK_STEP_NAME]
DELIVERY_STEP_NAME = "add_reminder_delivery_state"
DELIVERY_STEP_LINE = APPLIED_STEP_LINES[DELIVERY_STEP_NAME]
PROJECT_STEP_NAME = "create_project_records"
PROJECT_STEP_LINE = APPLIED_STEP_LINES[PROJECT_STEP_NAME]
LATER_STEPS = [STEP_NAME, TASK_STEP_NAME, DELIVERY_STEP_NAME, PROJECT_STEP_NAME]
FAILED_LINE = (
    "Migration create_conversation_records to schema version 6 failed and was "
    "rolled back; the database remains at version 5."
)
BUSY_LINE = (
    "Migration create_conversation_records to schema version 6 did not run "
    "because the database was busy; nothing was applied."
)
DRIFTED_VERSION_SIX_LINE = (
    "Refused a database stamped at schema version 6 whose structure is not "
    "the frozen schema for that version."
)
TOO_NEW_LINE = (
    "Refused a database stamped at schema version 10; this build expects 9."
)
VERSION_FIVE_TABLES = frozenset((*RECORD_TABLES, *AUDIT_TABLES))

# Three audit summaries, the second deleted, so the version 5 database holds a
# gap and an AUTOINCREMENT high-water mark above its newest row.
SEEDED_AUDIT_SUMMARIES = [
    (1, FIXED_AUDIT_TIMESTAMP, "calculator.calculate", "owner_approved", "none"),
    (
        3,
        FIXED_AUDIT_TIMESTAMP,
        "filesystem.list",
        "owner_approved",
        "named_filesystem_root",
    ),
]
SEEDED_AUDIT_OUTCOMES = [(1, "succeeded")]
APPROVED_NOTE_DIGEST = "0123456789abcdef" * 4

TRANSCRIPT = encode_transcript((("Hello there.", "Hello."),))


def _conversation_step() -> object:
    """Return the memory layer's own conversation-record step, by its name."""
    (step,) = [
        migration
        for migration in memory_module._MIGRATIONS
        if migration.name == STEP_NAME
    ]
    return step


def _ladder_with_step(*statements: str) -> tuple[object, ...]:
    """Return the real ladder with the conversation step's statements replaced."""
    return tuple(
        memory_module._Migration(version=6, name=STEP_NAME, statements=statements)
        if migration.name == STEP_NAME
        else migration
        for migration in memory_module._MIGRATIONS
    )


def _build_stamped_v5(database_path: Path) -> None:
    """Build the pinned version 5 shape with owner rows, stamped 5."""
    _build_schema(database_path, V5_SCHEMA, AUDIT_SUMMARIES_VERSION)


def _build_seeded_v5(database_path: Path) -> None:
    """Build a stamped version 5 database holding audit rows and consent.

    One memory's consent and one note's consent are on, and the audit table
    holds a deleted id, so a step that rewrote a row, reset consent, or
    touched a high-water mark would show.
    """
    _build_stamped_v5(database_path)
    for identifier, action, target in (
        (1, "calculator.calculate", "none"),
        (2, "system.current_time", "none"),
        (3, "filesystem.list", "named_filesystem_root"),
    ):
        _execute(
            database_path,
            """
            INSERT INTO audit_summaries (id, created_at, action, decision, target)
            VALUES (?, ?, ?, 'owner_approved', ?)
            """,
            (identifier, FIXED_AUDIT_TIMESTAMP, action, target),
        )
    _execute(
        database_path,
        "INSERT INTO audit_outcomes (summary_id, outcome) VALUES (1, 'succeeded')",
    )
    _execute(database_path, "DELETE FROM audit_summaries WHERE id = 2")
    _execute(
        database_path,
        "UPDATE memories SET context_enabled = 1 WHERE id = ?",
        (BASELINE_MEMORIES[0][0],),
    )
    _execute(
        database_path,
        "UPDATE notes SET context_digest = ? WHERE id = ?",
        (APPROVED_NOTE_DIGEST, BASELINE_NOTES[0][0]),
    )


def _everything(database_path: Path) -> dict[str, list[tuple[object, ...]]]:
    """Return every row of every table, ``sqlite_sequence`` included."""
    names = [
        str(row[0])
        for row in _query(
            database_path,
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
        )
    ]
    return {
        name: _query(database_path, f"SELECT * FROM {name} ORDER BY rowid")
        for name in names
    }


START_V5 = _Start(
    "stamped version 5",
    _build_stamped_v5,
    None,
    tuple(LATER_STEPS),
    MIGRATED_MEMORIES,
)
EVERY_START = (*ALL_STARTS, START_V5)


def _altered(table: str, old: str, new: str) -> tuple[str, ...]:
    """Return the version 6 schema with one clause of one new table changed."""
    frozen = {
        "saved_conversations": FROZEN_SAVED_CONVERSATIONS_TABLE,
        "conversation_summaries": FROZEN_CONVERSATION_SUMMARIES_TABLE,
    }
    altered = frozen[table].replace(old, new)
    if altered == frozen[table]:
        raise ValueError("The clause to change was not found.")
    return tuple(
        altered if statement == frozen[table] else statement
        for statement in V6_SCHEMA
    )


class ConversationRecordsSchemaPinTests(unittest.TestCase):
    """Pin schema 6, its step, and the history it must not disturb.

    Version 6 is frozen history now: V1-22's task step, V1-23-R1's delivery
    step, and V1-24-R1's project step follow it, and this step is found by its
    name rather than by its place.
    """

    def test_version_six_is_frozen_and_the_app_version_is_unchanged(self) -> None:
        self.assertEqual(CONVERSATION_RECORDS_VERSION, 6)
        self.assertEqual(DATABASE_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION)
        self.assertEqual(memory_module.DATABASE_SCHEMA_VERSION, 9)
        self.assertEqual(APP_VERSION, "0.2.0")

    def test_the_step_is_fifth_and_runs_exactly_its_two_frozen_creates(
        self,
    ) -> None:
        step = _conversation_step()
        names = [migration.name for migration in memory_module._MIGRATIONS]
        self.assertEqual(names.index(STEP_NAME), 4)
        self.assertEqual(names[4:], LATER_STEPS)

        self.assertEqual(
            (step.version, step.name, step.statements),
            (
                6,
                STEP_NAME,
                (FROZEN_SAVED_CONVERSATIONS_TABLE, FROZEN_CONVERSATION_SUMMARIES_TABLE),
            ),
        )
        self.assertEqual(
            dict((entry[1], entry) for entry in EXPECTED_LADDER)[STEP_NAME],
            (step.version, step.name, step.statements),
        )
        self.assertIs(type(step.statements), tuple)
        frozen_six = dict(memory_module._FROZEN_SCHEMAS)[6]
        for statement in step.statements:
            with self.subTest(statement=statement.split("(")[0].strip()):
                self.assertIn(statement, frozen_six)
                self.assertNotIn("IF NOT EXISTS", statement)
        self.assertIsNone(memory_module._validate_migration_ladder())

    def test_every_earlier_step_and_frozen_version_is_unchanged(self) -> None:
        position = [step.name for step in memory_module._MIGRATIONS].index(
            STEP_NAME
        )
        self.assertEqual(
            tuple(
                (step.version, step.name, step.statements)
                for step in memory_module._MIGRATIONS[:position]
            ),
            EXPECTED_LADDER[:position],
        )
        declared = dict(memory_module._FROZEN_SCHEMAS)
        for version in range(1, 6):
            with self.subTest(version=version):
                self.assertEqual(declared[version], EXPECTED_FROZEN_SCHEMAS[version])

    def test_the_frozen_version_six_entry_is_pinned_after_version_seven(self) -> None:
        versions = [version for version, _ in memory_module._FROZEN_SCHEMAS]

        self.assertEqual(versions, [9, 8, 7, 6, 5, 4, 3, 2, 1])
        self.assertEqual(
            dict(memory_module._FROZEN_SCHEMAS)[6],
            EXPECTED_FROZEN_SCHEMAS[6],
        )

    def test_the_live_statements_hold_the_frozen_version_six_schema_whole(
        self,
    ) -> None:
        current = memory_module._reference_schema(
            memory_module._CURRENT_SCHEMA_STATEMENTS
        )
        six = memory_module._reference_schema(memory_module._frozen_schema(6))

        self.assertEqual(six, memory_module._reference_schema(V6_SCHEMA))
        self.assertEqual(current, memory_module._reference_schema(V9_SCHEMA))
        self.assertEqual(
            current,
            memory_module._reference_schema(memory_module._frozen_schema(9)),
        )
        self.assertLessEqual(set(six), set(current))

    def test_version_six_is_version_five_plus_exactly_the_two_new_tables(
        self,
    ) -> None:
        five = {
            shape.name: shape
            for shape in memory_module._reference_schema(V5_SCHEMA)
        }
        six = {
            shape.name: shape
            for shape in memory_module._reference_schema(
                memory_module._frozen_schema(6)
            )
        }

        self.assertEqual(set(six) - set(five), set(CONVERSATION_TABLES))
        self.assertEqual(set(five) - set(six), set())
        for name, shape in five.items():
            with self.subTest(unchanged=name):
                self.assertEqual(six[name], shape)

    def test_the_new_tables_never_reuse_ids_and_refer_to_nothing(self) -> None:
        for statement in (
            FROZEN_SAVED_CONVERSATIONS_TABLE,
            FROZEN_CONVERSATION_SUMMARIES_TABLE,
        ):
            with self.subTest(statement=statement.split("(")[0].strip()):
                self.assertIn("id INTEGER PRIMARY KEY AUTOINCREMENT", statement)
                for absent in ("REFERENCES", "FOREIGN", "TRIGGER", "INDEX", "CASCADE"):
                    self.assertNotIn(absent, statement.upper())

    def test_the_step_turns_frozen_version_five_into_frozen_version_six(self) -> None:
        step = _conversation_step()

        self.assertEqual(
            memory_module._reference_schema(
                memory_module._frozen_schema(5) + step.statements
            ),
            memory_module._reference_schema(memory_module._frozen_schema(6)),
        )
        self.assertEqual(
            memory_module._reference_schema(V5_SCHEMA + step.statements),
            memory_module._reference_schema(V6_SCHEMA),
        )

    def test_neither_new_table_can_exist_unstamped(self) -> None:
        self.assertEqual(memory_module._UNVERSIONED_SHAPE_VERSIONS, (2, 1))


class ConversationRecordsUpgradeTests(_TemporaryDatabaseTestCase):
    """Verify every earlier database climbs through version 6, keeping every row."""

    def _migrate(self, start: _Start) -> tuple[Path, Mock, dict[str, int]]:
        """Build a start, migrate it once, and return what the climb saw."""
        database_path = self.build(start)
        sequences = _sequences(database_path)
        recording = Mock(spec=logging.Logger)

        Memory(database_path, logger=recording).initialize()

        return database_path, recording, sequences

    def test_every_earlier_version_climbs_through_the_step_to_the_current_version(
        self,
    ) -> None:
        for start in EVERY_START:
            with self.subTest(start=start.label):
                database_path, recording, sequences = self._migrate(start)

                self.assert_at_current_version(database_path, start)
                self.assertEqual(_applied(recording), list(start.steps))
                self.assertEqual(_applied(recording)[-4:], LATER_STEPS)
                self.assertEqual(
                    _row_counts(database_path),
                    {**BASELINE_COUNTS, "memories": len(start.memories)},
                )
                # No row was inserted anywhere: every high-water mark is as
                # found, and neither new table has one yet.
                self.assertEqual(_sequences(database_path), sequences)
                for table in CONVERSATION_TABLES:
                    self.assertNotIn(table, _sequences(database_path))
                classified = (
                    []
                    if start.classified_as is None
                    else [CLASSIFIED_LINES[start.classified_as]]
                )
                self.assertEqual(
                    _messages(recording),
                    [
                        *classified,
                        *(APPLIED_STEP_LINES[name] for name in start.steps),
                        READY_LINE,
                    ],
                )
                self.assertEqual(_messages(recording, "warning"), [])
                self.assertEqual(_messages(recording, "error"), [])
                recording.exception.assert_not_called()

    def test_a_version_five_database_keeps_every_row_and_high_water_mark(
        self,
    ) -> None:
        database_path = self.database("seeded")
        _build_seeded_v5(database_path)
        before = _everything(database_path)
        self.assertEqual(
            before["audit_summaries"],
            [tuple(row) for row in SEEDED_AUDIT_SUMMARIES],
        )
        self.assertIn(("audit_summaries", 3), before["sqlite_sequence"])
        recording = Mock(spec=logging.Logger)

        Memory(database_path, logger=recording).initialize()

        after = _everything(database_path)
        self.assertEqual(
            after,
            {
                **before,
                "saved_conversations": [],
                "conversation_summaries": [],
                "tasks": [],
                "reminders": [],
                "reminder_delivery_settings": [],
                "projects": [],
                "project_facts": [],
                "project_activation": [],
            },
        )
        self.assertEqual(_applied(recording), LATER_STEPS)
        self.assertEqual(
            _messages(recording),
            [
                APPLIED_LINE,
                TASK_STEP_LINE,
                DELIVERY_STEP_LINE,
                PROJECT_STEP_LINE,
                READY_LINE,
            ],
        )
        # Consent survives exactly as set, rather than being reset.
        memories = {row[0]: row for row in after["memories"]}
        notes = {row[0]: row for row in after["notes"]}
        self.assertEqual(memories[BASELINE_MEMORIES[0][0]][-1], 1)
        self.assertEqual(notes[BASELINE_NOTES[0][0]][-1], APPROVED_NOTE_DIGEST)

    def test_every_upgraded_database_reopens_without_any_change(self) -> None:
        for start in EVERY_START:
            with self.subTest(start=start.label):
                database_path = self.build(start)
                Memory(database_path, logger=TEST_LOGGER).initialize()
                migrated = database_path.read_bytes()
                recording = Mock(spec=logging.Logger)

                with patch.object(
                    memory_module,
                    "_run_in_one_transaction",
                    side_effect=AssertionError("a current database is not written"),
                ):
                    Memory(database_path, logger=recording).initialize()

                self.assertEqual(database_path.read_bytes(), migrated)
                self.assertEqual(_messages(recording), [READY_LINE])
                self.assertEqual(_messages(recording, "error"), [])

    def test_a_migrated_database_numbers_records_from_one_and_never_reuses_ids(
        self,
    ) -> None:
        database_path = self.build(START_V5)
        memory = Memory(database_path, logger=TEST_LOGGER)
        memory.initialize()
        sequences = _sequences(database_path)

        first_saved = memory.save_conversation(TRANSCRIPT)
        first_summary = memory.create_conversation_summary("We agreed on a plan.")
        self.assertEqual((first_saved, first_summary), (1, 1))
        self.assertTrue(memory.delete_saved_conversation(1))
        self.assertTrue(memory.delete_conversation_summary(1))
        second_saved = memory.save_conversation(TRANSCRIPT)
        second_summary = memory.create_conversation_summary("We agreed on a plan.")

        # AUTOINCREMENT keeps a deleted id from ever being handed out again.
        self.assertEqual((second_saved, second_summary), (2, 2))
        # The only bookkeeping the inserts add is each new table's own mark.
        self.assertEqual(
            _sequences(database_path),
            {**sequences, "saved_conversations": 2, "conversation_summaries": 2},
        )
        self.assertEqual(
            _record_rows(database_path),
            {
                "interactions": list(MIGRATED_INTERACTIONS),
                "notes": list(MIGRATED_NOTES),
                "memories": list(MIGRATED_MEMORIES),
            },
        )


class FreshDatabaseTests(_TemporaryDatabaseTestCase):
    """Verify a database Maria creates itself starts at the current version."""

    def test_a_new_database_is_created_at_the_current_version_with_no_ladder(
        self,
    ) -> None:
        database_path = self.database("fresh")
        recording = Mock(spec=logging.Logger)

        Memory(database_path, logger=recording).initialize()

        self.assertEqual(_messages(recording), [CREATED_LINE, READY_LINE])
        self.assertEqual(_applied(recording), [])
        self.assertEqual(read_schema_version(database_path), CURRENT_SCHEMA_VERSION)
        self.assertEqual(table_names(database_path), CURRENT_TABLES)
        self.assertEqual(
            _describe(database_path),
            memory_module._reference_schema(CURRENT_SCHEMA),
        )
        self.assertEqual(
            _row_counts(database_path),
            dict.fromkeys(BASELINE_COUNTS, 0),
        )

    def test_a_fresh_database_has_the_schema_of_a_migrated_version_five_one(
        self,
    ) -> None:
        fresh_path = self.database("fresh")
        migrated_path = self.build(START_V5)

        Memory(fresh_path, logger=TEST_LOGGER).initialize()
        Memory(migrated_path, logger=TEST_LOGGER).initialize()

        self.assertEqual(_describe(fresh_path), _describe(migrated_path))
        self.assertEqual(_object_names(fresh_path), _object_names(migrated_path))
        self.assertEqual(
            read_schema_version(fresh_path),
            read_schema_version(migrated_path),
        )


class ConversationStepAtomicityTests(_TemporaryDatabaseTestCase):
    """Verify a failed step leaves version 5 whole and resumes exactly once."""

    def _injections(self) -> dict[str, Callable[[], object]]:
        """Return each way the new step is made to fail part-way."""
        real_step = _conversation_step()
        real_write = memory_module._write_schema_version
        broken = _ladder_with_step

        def refuse_the_new_stamp(connection: sqlite3.Connection, version: int) -> None:
            if version == 6:
                raise sqlite3.OperationalError("injected failure stamping version 6")
            real_write(connection, version)

        return {
            "after the saved conversations table": lambda: patch.object(
                memory_module,
                "_MIGRATIONS",
                broken(real_step.statements[0], NEVER_COMMITTED),
            ),
            "after both tables": lambda: patch.object(
                memory_module,
                "_MIGRATIONS",
                broken(*real_step.statements, NEVER_COMMITTED),
            ),
            "at the version marker": lambda: patch.object(
                memory_module,
                "_write_schema_version",
                side_effect=refuse_the_new_stamp,
            ),
        }

    def assert_at_version_five(
        self,
        database_path: Path,
        *,
        before_bytes: bytes,
        before_rows: dict[str, list[tuple[object, ...]]],
        before_siblings: list[str],
    ) -> None:
        """Assert a database is still whole at version 5, byte for byte."""
        self.assertEqual(read_schema_version(database_path), 5)
        self.assertEqual(
            _describe(database_path),
            memory_module._reference_schema(V5_SCHEMA),
        )
        self.assertEqual(table_names(database_path), VERSION_FIVE_TABLES)
        self.assertEqual(_everything(database_path), before_rows)
        self.assertEqual(database_path.read_bytes(), before_bytes)
        self.assertEqual(_siblings(database_path), before_siblings)

    def test_a_failure_inside_the_step_leaves_version_five_whole(self) -> None:
        for label, injection in self._injections().items():
            with self.subTest(injected=label):
                database_path = self.database(label.replace(" ", "-"))
                _build_seeded_v5(database_path)
                before_bytes = database_path.read_bytes()
                before_rows = _everything(database_path)
                before_siblings = _siblings(database_path)
                recording = Mock(spec=logging.Logger)

                with injection():
                    with self.assertRaisesRegex(
                        RuntimeError,
                        DATABASE_REFUSED_PATTERN,
                    ) as raised:
                        Memory(database_path, logger=recording).initialize()

                self.assertIsInstance(raised.exception.__cause__, sqlite3.Error)
                self.assert_at_version_five(
                    database_path,
                    before_bytes=before_bytes,
                    before_rows=before_rows,
                    before_siblings=before_siblings,
                )
                self.assertNotIn("never_committed", _object_names(database_path))
                self.assertEqual(_applied(recording), [])
                self.assertEqual(_messages(recording), [])
                self.assertEqual(_messages(recording, "error"), [FAILED_LINE])
                recording.exception.assert_not_called()

    def test_a_retry_after_a_failed_step_applies_it_exactly_once(self) -> None:
        database_path = self.build(START_V5)
        recording = Mock(spec=logging.Logger)
        retrying = Memory(database_path, logger=recording)

        with self._injections()["after both tables"]():
            with self.assertRaisesRegex(RuntimeError, DATABASE_REFUSED_PATTERN):
                retrying.initialize()
        retrying.initialize()
        retrying.initialize()

        self.assertEqual(_applied(recording), LATER_STEPS)
        self.assertEqual(_messages(recording).count(APPLIED_LINE), 1)
        self.assert_at_current_version(database_path, START_V5)

    def test_a_climb_that_fails_at_the_new_step_stops_whole_at_version_five(
        self,
    ) -> None:
        """Each step commits alone, so the failure undoes only version 6.

        The retry then climbs the rest of the way, through the task step.
        """
        for label, injection in self._injections().items():
            with self.subTest(injected=label):
                database_path = self.database(f"climb-{label.replace(' ', '-')}")
                _build_unversioned_baseline(database_path)
                recording = Mock(spec=logging.Logger)
                retrying = Memory(database_path, logger=recording)

                with injection():
                    with self.assertRaisesRegex(
                        RuntimeError,
                        DATABASE_REFUSED_PATTERN,
                    ):
                        retrying.initialize()

                self.assertEqual(read_schema_version(database_path), 5)
                self.assertEqual(
                    _describe(database_path),
                    memory_module._reference_schema(V5_SCHEMA),
                )
                self.assertEqual(
                    _applied(recording),
                    [
                        "add_memory_context_consent",
                        "add_note_context_consent",
                        "create_audit_summaries",
                    ],
                )
                self.assertEqual(_messages(recording, "error"), [FAILED_LINE])

                retrying.initialize()

                self.assertEqual(_applied(recording)[-1], PROJECT_STEP_NAME)
                self.assertEqual(_applied(recording).count(STEP_NAME), 1)
                self.assertEqual(_applied(recording).count("create_audit_summaries"), 1)
                self.assert_at_current_version(database_path, ALL_STARTS[0])


class ConversationStepContentionTests(_TemporaryDatabaseTestCase):
    """Verify contention during the new step reads as busy and changes nothing."""

    def test_a_write_lock_during_the_step_gives_the_busy_message(self) -> None:
        database_path = self.build(START_V5)
        before_bytes = database_path.read_bytes()
        before_siblings = _siblings(database_path)
        recording = Mock(spec=logging.Logger)
        release = _hold_write_lock(self, database_path)

        with patch.object(
            memory_module,
            "DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS",
            SHORT_BUSY_TIMEOUT_SECONDS,
        ):
            with self.assertRaisesRegex(RuntimeError, DATABASE_BUSY_PATTERN) as raised:
                Memory(database_path, logger=recording).initialize()
        release()

        cause = raised.exception.__cause__
        self.assertIsInstance(cause, sqlite3.OperationalError)
        self.assertEqual(cause.sqlite_errorcode & 0xFF, sqlite3.SQLITE_BUSY)
        self.assertEqual(_messages(recording, "error"), [BUSY_LINE])
        self.assertEqual(_messages(recording), [])
        recording.exception.assert_not_called()
        self.assertEqual(read_schema_version(database_path), 5)
        self.assertEqual(database_path.read_bytes(), before_bytes)
        self.assertEqual(_siblings(database_path), before_siblings)

    def test_the_step_applies_exactly_once_after_the_lock_is_released(self) -> None:
        database_path = self.build(START_V5)
        recording = Mock(spec=logging.Logger)
        retrying = Memory(database_path, logger=recording)
        release = _hold_write_lock(self, database_path)
        with patch.object(
            memory_module,
            "DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS",
            SHORT_BUSY_TIMEOUT_SECONDS,
        ):
            with self.assertRaisesRegex(RuntimeError, DATABASE_BUSY_PATTERN):
                retrying.initialize()
        release()

        retrying.initialize()
        retrying.initialize()

        self.assertEqual(_applied(recording), LATER_STEPS)
        self.assert_at_current_version(database_path, START_V5)

    def test_a_locked_report_during_the_step_is_contention_too(self) -> None:
        database_path = self.build(START_V5)
        before_bytes = database_path.read_bytes()
        locked = sqlite3.OperationalError("database table is locked")
        locked.sqlite_errorcode = sqlite3.SQLITE_LOCKED
        real_runner = memory_module._run_in_one_transaction
        versions: list[int] = []

        def lock_only_the_new_step(connection, statements, version) -> None:
            versions.append(version)
            if version == 6:
                raise locked
            real_runner(connection, statements, version)

        recording = Mock(spec=logging.Logger)
        with patch.object(
            memory_module,
            "_run_in_one_transaction",
            side_effect=lock_only_the_new_step,
        ):
            with self.assertRaisesRegex(RuntimeError, DATABASE_BUSY_PATTERN) as raised:
                Memory(database_path, logger=recording).initialize()

        self.assertIs(raised.exception.__cause__, locked)
        self.assertEqual(versions, [6])
        self.assertEqual(_messages(recording, "error"), [BUSY_LINE])
        self.assertEqual(database_path.read_bytes(), before_bytes)


class VersionSixDriftTests(_TemporaryDatabaseTestCase):
    """Verify a stamped version 6 is checked against its frozen schema first."""

    def _assert_each_refused(self, drifts: dict[str, tuple[str, ...]]) -> None:
        """Build each drifted schema stamped 6 and assert it is refused untouched."""
        exact = memory_module._reference_schema(V6_SCHEMA)
        for label, schema in drifts.items():
            with self.subTest(drift=label):
                self.assertNotEqual(memory_module._reference_schema(schema), exact)
                database_path = self.database(label.replace(" ", "-"))
                _build_schema(database_path, schema, 6)

                recording = self.assert_refused_untouched(database_path, marker=6)

                self.assertEqual(
                    _messages(recording, "error"),
                    [DRIFTED_VERSION_SIX_LINE],
                )

    def test_the_exact_version_six_schema_is_accepted_and_climbs_three_steps(
        self,
    ) -> None:
        """Version 6 is frozen now, so an exact one climbs the task step, the
        delivery step, and the project step only."""
        database_path = self.database("exact")
        _build_schema(database_path, V6_SCHEMA, 6)
        before_rows = _record_rows(database_path)
        recording = Mock(spec=logging.Logger)

        Memory(database_path, logger=recording).initialize()

        self.assertEqual(_applied(recording), LATER_STEPS[1:])
        self.assertEqual(
            _messages(recording),
            [TASK_STEP_LINE, DELIVERY_STEP_LINE, PROJECT_STEP_LINE, READY_LINE],
        )
        self.assertEqual(_messages(recording, "error"), [])
        self.assertEqual(read_schema_version(database_path), CURRENT_SCHEMA_VERSION)
        self.assertEqual(
            _describe(database_path),
            memory_module._reference_schema(CURRENT_SCHEMA),
        )
        self.assertEqual(_record_rows(database_path), before_rows)

        migrated = database_path.read_bytes()
        reopening = Mock(spec=logging.Logger)
        with patch.object(
            memory_module,
            "_run_in_one_transaction",
            side_effect=AssertionError("a current database may not be written"),
        ):
            Memory(database_path, logger=reopening).initialize()

        self.assertEqual(database_path.read_bytes(), migrated)
        self.assertEqual(_messages(reopening), [READY_LINE])

    def test_a_version_six_database_missing_a_new_table_is_refused(self) -> None:
        self._assert_each_refused(
            {
                "missing saved conversations": V5_SCHEMA
                + (FROZEN_CONVERSATION_SUMMARIES_TABLE,),
                "missing conversation summaries": V5_SCHEMA
                + (FROZEN_SAVED_CONVERSATIONS_TABLE,),
                "missing both new tables": V5_SCHEMA,
            }
        )

    def test_an_altered_new_column_is_refused_untouched(self) -> None:
        self._assert_each_refused(
            {
                "nullable transcript": _altered(
                    "saved_conversations",
                    "transcript TEXT NOT NULL CHECK",
                    "transcript TEXT CHECK",
                ),
                "unchecked transcript": _altered(
                    "saved_conversations",
                    "transcript TEXT NOT NULL CHECK (length(transcript) > 0)",
                    "transcript TEXT NOT NULL",
                ),
                "reusable saved ids": _altered(
                    "saved_conversations",
                    "id INTEGER PRIMARY KEY AUTOINCREMENT",
                    "id INTEGER PRIMARY KEY",
                ),
                "an extra request column": _altered(
                    "saved_conversations",
                    "created_at TEXT NOT NULL,",
                    "created_at TEXT NOT NULL,\n        request TEXT,",
                ),
                "unchecked body": _altered(
                    "conversation_summaries",
                    "body TEXT NOT NULL CHECK (length(CAST(body AS BLOB)) > 0)",
                    "body TEXT NOT NULL",
                ),
                # SQLite's length() of TEXT stops at the first NUL, so this
                # check would refuse a body Maria's text rule accepts.
                "a body check that stops at NUL": _altered(
                    "conversation_summaries",
                    "CHECK (length(CAST(body AS BLOB)) > 0)",
                    "CHECK (length(body) > 0)",
                ),
                "reusable summary ids": _altered(
                    "conversation_summaries",
                    "id INTEGER PRIMARY KEY AUTOINCREMENT",
                    "id INTEGER PRIMARY KEY",
                ),
                "unchecked consent": _altered(
                    "conversation_summaries",
                    "context_digest TEXT\n"
                    "            CHECK (context_digest IS NULL OR "
                    "length(context_digest) = 64)",
                    "context_digest TEXT",
                ),
                "consent on by default": _altered(
                    "conversation_summaries",
                    "context_digest TEXT\n",
                    "context_digest TEXT DEFAULT '" + "0" * 64 + "'\n",
                ),
                "a title column": _altered(
                    "conversation_summaries",
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,\n",
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,\n        title TEXT,\n",
                ),
                "a reference to a saved conversation": _altered(
                    "conversation_summaries",
                    "updated_at TEXT NOT NULL,",
                    "updated_at TEXT NOT NULL,\n"
                    "        conversation_id INTEGER "
                    "REFERENCES saved_conversations (id) ON DELETE CASCADE,",
                ),
            }
        )

    def test_an_extra_trigger_index_view_or_table_is_refused_untouched(self) -> None:
        self._assert_each_refused(
            {
                "a trigger copying transcripts": V6_SCHEMA
                + (
                    "CREATE TRIGGER saved_conversations_copy "
                    "AFTER INSERT ON saved_conversations BEGIN "
                    "INSERT INTO notes (title, body, created_at, updated_at) "
                    "VALUES ('copy', NEW.transcript, NEW.created_at, NEW.created_at); "
                    "END",
                ),
                "an extra index": V6_SCHEMA
                + (
                    "CREATE INDEX idx_conversation_summaries_body "
                    "ON conversation_summaries (body)",
                ),
                "an extra view": V6_SCHEMA
                + (
                    "CREATE VIEW summary_view AS "
                    "SELECT id, body FROM conversation_summaries",
                ),
                "an extra table": V6_SCHEMA
                + ("CREATE TABLE summary_drafts (id INTEGER PRIMARY KEY, draft TEXT)",),
            }
        )

    def test_an_unstamped_file_in_the_version_six_shape_is_refused(self) -> None:
        database_path = self.database("unstamped-six")
        _build_schema(database_path, V6_SCHEMA, None)

        recording = self.assert_refused_untouched(database_path, marker=UNVERSIONED)

        self.assertEqual(
            _messages(recording, "error"),
            [UNRECOGNISED_UNVERSIONED_LINE],
        )

    def test_a_database_stamped_ten_is_too_new_and_left_untouched(self) -> None:
        database_path = self.database("newer")
        _build_schema(database_path, V6_SCHEMA, NEWER_SCHEMA_VERSION)
        before = database_path.read_bytes()
        recording = Mock(spec=logging.Logger)

        with self.assertRaises(TypedFailureError) as raised:
            Memory(database_path, logger=recording).initialize()

        self.assertEqual(
            raised.exception.failure,
            Failure(FailureKind.DATABASE_SCHEMA_TOO_NEW),
        )
        self.assertEqual(database_path.read_bytes(), before)
        self.assertEqual(_messages(recording, "error"), [TOO_NEW_LINE])


class ConversationMigrationLoggingTests(unittest.TestCase):
    """Prove the new step writes fixed identifiers to a real log and no more."""

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database_path = self.root / "database" / "maria-test.db"
        self.log_dir = self.root / "logs"
        self.log_file = self.log_dir / "maria.log"
        self.maria_logger = logging.getLogger("maria")
        self.original_handlers = tuple(self.maria_logger.handlers)
        self.original_level = self.maria_logger.level
        self.original_propagate = self.maria_logger.propagate
        self.original_disabled = self.maria_logger.disabled
        for handler in self.original_handlers:
            self.maria_logger.removeHandler(handler)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for handler in list(self.maria_logger.handlers):
            try:
                handler.flush()
            finally:
                self.maria_logger.removeHandler(handler)
                handler.close()
        for handler in self.original_handlers:
            self.maria_logger.addHandler(handler)
        self.maria_logger.setLevel(self.original_level)
        self.maria_logger.propagate = self.original_propagate
        self.maria_logger.disabled = self.original_disabled

    def _entries(self) -> list[tuple[str, ...]]:
        """Return each real log line as its level, logger, and message."""
        for handler in self.maria_logger.handlers:
            handler.flush()
        entries: list[tuple[str, ...]] = []
        for line in self.log_file.read_text(encoding="utf-8").splitlines():
            fields = line.split(" | ", 3)
            entries.append(tuple(fields[1:]) if len(fields) == 4 else (line,))
        return entries

    def _sentinels(self) -> list[str]:
        """Return every stored value, path, and SQL fragment the log must omit."""
        sentinels = [str(self.database_path), str(self.database_path.parent)]
        for _identifier, request, response, _created in BASELINE_INTERACTIONS:
            sentinels.extend((request, response))
        for _identifier, title, body, _created, _updated in BASELINE_NOTES:
            sentinels.extend((title, body))
        for _identifier, fact, normalized, _created in BASELINE_MEMORIES:
            sentinels.extend((fact, normalized))
        sentinels.extend(
            (
                "Traceback",
                "sqlite3.",
                "CREATE TABLE",
                "saved_conversations",
                "conversation_summaries",
                "transcript",
            )
        )
        return sentinels

    def test_the_step_writes_its_fixed_line_to_a_real_log(self) -> None:
        _build_seeded_v5(self.database_path)
        configure_logging(self.log_dir, "DEBUG")

        Memory(self.database_path, logger=get_logger("memory")).initialize()

        self.assertEqual(
            self._entries(),
            [
                LOGGING_CONFIGURED_ENTRY,
                ("INFO", "maria.memory", APPLIED_LINE),
                ("INFO", "maria.memory", TASK_STEP_LINE),
                ("INFO", "maria.memory", DELIVERY_STEP_LINE),
                ("INFO", "maria.memory", PROJECT_STEP_LINE),
                ("INFO", "maria.memory", READY_LINE),
            ],
        )
        assert_log_omits(self, self.log_file, self._sentinels())

    def test_a_failed_step_logs_no_sqlite_text_or_traceback(self) -> None:
        _build_seeded_v5(self.database_path)
        configure_logging(self.log_dir, "DEBUG")
        real_step = _conversation_step()
        broken = _ladder_with_step(*real_step.statements, NEVER_COMMITTED)

        with patch.object(memory_module, "_MIGRATIONS", broken):
            with self.assertRaisesRegex(RuntimeError, DATABASE_REFUSED_PATTERN):
                Memory(self.database_path, logger=get_logger("memory")).initialize()

        self.assertEqual(
            self._entries(),
            [LOGGING_CONFIGURED_ENTRY, ("ERROR", "maria.memory", FAILED_LINE)],
        )
        assert_log_omits(
            self,
            self.log_file,
            (*self._sentinels(), "OperationalError", "never_committed", "INVALID"),
        )

    def test_contention_during_the_step_logs_only_its_fixed_line(self) -> None:
        _build_seeded_v5(self.database_path)
        configure_logging(self.log_dir, "DEBUG")
        release = _hold_write_lock(self, self.database_path)

        with patch.object(
            memory_module,
            "DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS",
            SHORT_BUSY_TIMEOUT_SECONDS,
        ):
            with self.assertRaisesRegex(RuntimeError, DATABASE_BUSY_PATTERN):
                Memory(self.database_path, logger=get_logger("memory")).initialize()
        release()

        self.assertEqual(
            self._entries(),
            [LOGGING_CONFIGURED_ENTRY, ("ERROR", "maria.memory", BUSY_LINE)],
        )
        assert_log_omits(
            self,
            self.log_file,
            (*self._sentinels(), "OperationalError", "database is locked"),
        )


class LiveDatabaseRefusalTests(_TemporaryDatabaseTestCase):
    """Prove every database helper this file adds refuses the live directory."""

    def test_every_new_helper_refuses_the_live_database_directory(self) -> None:
        refused_paths = {
            "live file": DATABASE_PATH,
            "live directory": DATABASE_DIR,
            "sibling": DATABASE_DIR / "records-copy.db",
            "descendant": DATABASE_DIR / "nested" / "copy.db",
            "traversal": DATABASE_DIR / "nested" / ".." / DATABASE_PATH.name,
        }
        helpers = (
            ("stamped version 5", _build_stamped_v5),
            ("seeded version 5", _build_seeded_v5),
            ("every row", _everything),
            ("start: stamped version 5", START_V5.build),
        )
        with patch.object(
            sqlite3,
            "connect",
            side_effect=AssertionError("the live database may never be opened"),
        ):
            for label, helper in helpers:
                for kind, refused in refused_paths.items():
                    with self.subTest(helper=label, path=kind):
                        with self.assertRaisesRegex(ValueError, LIVE_DATABASE_PATTERN):
                            helper(refused)

    def test_the_guard_admits_temporary_databases(self) -> None:
        database_path = self.database("admitted")

        self.assertIsNone(_refuse_live_database(database_path))
        _build_seeded_v5(database_path)
        self.assertEqual(read_schema_version(database_path), 5)
        self.assertEqual(
            _everything(database_path)["audit_outcomes"],
            [tuple(row) for row in SEEDED_AUDIT_OUTCOMES],
        )
        self.assertNotIn(DATABASE_DIR.resolve(), database_path.resolve().parents)


if __name__ == "__main__":
    unittest.main()
