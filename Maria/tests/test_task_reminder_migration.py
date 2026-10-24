"""Focused tests for schema version 7 and its create_tasks_and_reminders step.

V1-22 appends one forward step to Maria's migration ladder. It creates the
two tables tasks and reminders live in, empty, and touches no existing table,
row, or AUTOINCREMENT high-water mark. Every database here is built under a
``TemporaryDirectory``, from the frozen fixtures and from the shapes
``tests.test_audit_migration`` writes out by hand, reused unchanged; the two
new tables are pinned word for word there as well. Every helper that opens or
builds a database refuses Maria's live database directory before anything is
opened, and Maria's live log and private configuration override are never
read.
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
    FROZEN_REMINDERS_TABLE,
    FROZEN_TASKS_TABLE,
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
    TASK_TABLES,
    TASKS_AND_REMINDERS_VERSION,
    TEST_LOGGER,
    UNRECOGNISED_UNVERSIONED_LINE,
    UNVERSIONED,
    V6_SCHEMA,
    V7_SCHEMA,
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
from tests.test_conversation_records_migration import START_V5


STEP_NAME = "create_tasks_and_reminders"
APPLIED_LINE = APPLIED_STEP_LINES[STEP_NAME]
# V1-23-R1 appends one more step after this one, and V1-24-R1 one more after
# that, so every climb through version 7 now ends with both, and version 9 is
# current.
DELIVERY_STEP_NAME = "add_reminder_delivery_state"
DELIVERY_APPLIED_LINE = APPLIED_STEP_LINES[DELIVERY_STEP_NAME]
PROJECT_STEP_NAME = "create_project_records"
PROJECT_APPLIED_LINE = APPLIED_STEP_LINES[PROJECT_STEP_NAME]
LATER_STEPS = [STEP_NAME, DELIVERY_STEP_NAME, PROJECT_STEP_NAME]
FAILED_LINE = (
    "Migration create_tasks_and_reminders to schema version 7 failed and was "
    "rolled back; the database remains at version 6."
)
BUSY_LINE = (
    "Migration create_tasks_and_reminders to schema version 7 did not run "
    "because the database was busy; nothing was applied."
)
DRIFTED_VERSION_SEVEN_LINE = (
    "Refused a database stamped at schema version 7 whose structure is not "
    "the frozen schema for that version."
)
TOO_NEW_LINE = (
    "Refused a database stamped at schema version 10; this build expects 9."
)
VERSION_SIX_TABLES = frozenset((*RECORD_TABLES, *AUDIT_TABLES, *CONVERSATION_TABLES))

FIXED_STAMP = "2026-10-02T08:15:00Z"
APPROVED_DIGEST = "0123456789abcdef" * 4
TRANSCRIPT = encode_transcript((("Hello there.", "Hello."),))
REMINDER_TIME = "2027-11-02T18:30:00+01:00[Europe/Berlin]"
REMINDER_INSTANT = "2027-11-02T17:30:00Z"


def _task_step() -> object:
    """Return the memory layer's own task step, by its name."""
    (step,) = [
        migration
        for migration in memory_module._MIGRATIONS
        if migration.name == STEP_NAME
    ]
    return step


def _ladder_with_step(*statements: str) -> tuple[object, ...]:
    """Return the real ladder with the task step's statements replaced."""
    return tuple(
        memory_module._Migration(version=7, name=STEP_NAME, statements=statements)
        if migration.name == STEP_NAME
        else migration
        for migration in memory_module._MIGRATIONS
    )


def _build_stamped_v6(database_path: Path) -> None:
    """Build the pinned version 6 shape with owner rows, stamped 6."""
    _build_schema(database_path, V6_SCHEMA, CONVERSATION_RECORDS_VERSION)


def _build_seeded_v6(database_path: Path) -> None:
    """Build a stamped version 6 database holding V1-20 rows and consent.

    Two saved conversations and two summaries are stored and one of each
    deleted, so each table holds a gap and a high-water mark above its
    newest row, and one summary's consent is on. A step that rewrote a row,
    reset consent, or touched a high-water mark would show.
    """
    _build_stamped_v6(database_path)
    for _ in range(2):
        _execute(
            database_path,
            "INSERT INTO saved_conversations (created_at, transcript) VALUES (?, ?)",
            (FIXED_STAMP, TRANSCRIPT),
        )
        _execute(
            database_path,
            """
            INSERT INTO conversation_summaries
                (body, created_at, updated_at, context_digest)
            VALUES ('We agreed on a plan.', ?, ?, ?)
            """,
            (FIXED_STAMP, FIXED_STAMP, APPROVED_DIGEST),
        )
    _execute(database_path, "DELETE FROM saved_conversations WHERE id = 2")
    _execute(database_path, "DELETE FROM conversation_summaries WHERE id = 1")


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


START_V6 = _Start(
    "stamped version 6",
    _build_stamped_v6,
    None,
    tuple(LATER_STEPS),
    MIGRATED_MEMORIES,
)
EVERY_START = (*ALL_STARTS, START_V5, START_V6)


def _altered(table: str, old: str, new: str) -> tuple[str, ...]:
    """Return the version 7 schema with one clause of one new table changed."""
    frozen = {"tasks": FROZEN_TASKS_TABLE, "reminders": FROZEN_REMINDERS_TABLE}
    altered = frozen[table].replace(old, new)
    if altered == frozen[table]:
        raise ValueError("The clause to change was not found.")
    return tuple(
        altered if statement == frozen[table] else statement
        for statement in V7_SCHEMA
    )


class TaskSchemaPinTests(unittest.TestCase):
    """Pin schema 7, its one step, and the history it must not disturb."""

    def test_version_seven_is_followed_by_eight_and_the_app_version_is_unchanged(
        self,
    ) -> None:
        self.assertEqual(TASKS_AND_REMINDERS_VERSION, 7)
        self.assertEqual(DATABASE_SCHEMA_VERSION, 9)
        self.assertEqual(memory_module.DATABASE_SCHEMA_VERSION, 9)
        self.assertEqual(CURRENT_SCHEMA_VERSION, 9)
        self.assertEqual(APP_VERSION, "0.2.0")
        self.assertEqual(NEWER_SCHEMA_VERSION, 10)

    def test_the_step_is_sixth_and_runs_its_two_frozen_creates(
        self,
    ) -> None:
        step = _task_step()
        names = [migration.name for migration in memory_module._MIGRATIONS]

        self.assertEqual(names.index(STEP_NAME), 5)
        # V1-23-R1's step follows it, and V1-24-R1's is now the last.
        self.assertEqual(names.index(DELIVERY_STEP_NAME), 6)
        self.assertEqual(names[-1], PROJECT_STEP_NAME)
        self.assertEqual(names.index(PROJECT_STEP_NAME), 7)
        self.assertEqual(
            (step.version, step.name, step.statements),
            (7, STEP_NAME, (FROZEN_TASKS_TABLE, FROZEN_REMINDERS_TABLE)),
        )
        self.assertEqual(
            EXPECTED_LADDER[5],
            (step.version, step.name, step.statements),
        )
        frozen_seven = dict(memory_module._FROZEN_SCHEMAS)[7]
        for statement in step.statements:
            with self.subTest(statement=statement.split("(")[0].strip()):
                self.assertIn(statement, frozen_seven)
                self.assertNotIn("IF NOT EXISTS", statement)
        self.assertIsNone(memory_module._validate_migration_ladder())

    def test_every_earlier_step_and_frozen_version_is_unchanged(self) -> None:
        self.assertEqual(
            tuple(
                (step.version, step.name, step.statements)
                for step in memory_module._MIGRATIONS[:5]
            ),
            EXPECTED_LADDER[:5],
        )
        declared = dict(memory_module._FROZEN_SCHEMAS)
        for version in range(1, 10):
            with self.subTest(version=version):
                self.assertEqual(declared[version], EXPECTED_FROZEN_SCHEMAS[version])
        self.assertEqual(
            [version for version, _ in memory_module._FROZEN_SCHEMAS],
            [9, 8, 7, 6, 5, 4, 3, 2, 1],
        )

    def test_the_live_statements_hold_the_frozen_version_nine_schema(self) -> None:
        current = memory_module._reference_schema(
            memory_module._CURRENT_SCHEMA_STATEMENTS
        )

        self.assertEqual(current, memory_module._reference_schema(V9_SCHEMA))
        self.assertEqual(
            current,
            memory_module._reference_schema(memory_module._frozen_schema(9)),
        )
        for name in ("_CREATE_TASKS_TABLE", "_CREATE_REMINDERS_TABLE"):
            with self.subTest(statement=name):
                live = getattr(memory_module, name)
                self.assertIn(live, memory_module._CURRENT_SCHEMA_STATEMENTS)
                self.assertIn("IF NOT EXISTS", live)

    def test_version_seven_is_version_six_plus_exactly_the_two_new_tables(
        self,
    ) -> None:
        six = {
            shape.name: shape
            for shape in memory_module._reference_schema(
                memory_module._frozen_schema(6)
            )
        }
        seven = {
            shape.name: shape
            for shape in memory_module._reference_schema(
                memory_module._frozen_schema(7)
            )
        }

        self.assertEqual(set(seven) - set(six), set(TASK_TABLES))
        self.assertEqual(set(six) - set(seven), set())
        for name, shape in six.items():
            with self.subTest(unchanged=name):
                self.assertEqual(seven[name], shape)
        self.assertEqual(
            memory_module._reference_schema(V6_SCHEMA + _task_step().statements),
            memory_module._reference_schema(V7_SCHEMA),
        )

    def test_the_new_tables_never_reuse_ids_refer_to_nothing_and_never_repeat(
        self,
    ) -> None:
        for statement in (FROZEN_TASKS_TABLE, FROZEN_REMINDERS_TABLE):
            with self.subTest(statement=statement.split("(")[0].strip()):
                self.assertIn("id INTEGER PRIMARY KEY AUTOINCREMENT", statement)
                for absent in (
                    "REFERENCES",
                    "FOREIGN",
                    "TRIGGER",
                    "INDEX",
                    "CASCADE",
                    "REPEAT",
                    "RECURR",
                    "DELIVER",
                    "NOTIF",
                ):
                    self.assertNotIn(absent, statement.upper())

    def test_sql_checks_only_the_documented_shapes(self) -> None:
        self.assertEqual(FROZEN_TASKS_TABLE.count("CHECK"), 4)
        self.assertEqual(FROZEN_REMINDERS_TABLE.count("CHECK"), 4)
        for statement in (FROZEN_TASKS_TABLE, FROZEN_REMINDERS_TABLE):
            with self.subTest(statement=statement.split("(")[0].strip()):
                self.assertIn("CHECK (length(CAST(text AS BLOB)) > 0)", statement)
                self.assertIn("CHECK (status IN ('open', 'completed'))", statement)
                self.assertIn(
                    "CHECK (context_digest IS NULL OR length(context_digest) = 64)",
                    statement,
                )
        self.assertIn("remind_time TEXT NOT NULL,", FROZEN_REMINDERS_TABLE)
        self.assertIn("due_date GLOB", FROZEN_TASKS_TABLE)
        self.assertIn("remind_utc GLOB", FROZEN_REMINDERS_TABLE)

    def test_neither_new_table_can_exist_unstamped(self) -> None:
        self.assertEqual(memory_module._UNVERSIONED_SHAPE_VERSIONS, (2, 1))


class TaskStepUpgradeTests(_TemporaryDatabaseTestCase):
    """Verify every earlier database climbs through version 7, keeping every row."""

    def test_every_earlier_version_climbs_through_the_step_to_nine(
        self,
    ) -> None:
        for start in EVERY_START:
            with self.subTest(start=start.label):
                database_path = self.build(start)
                sequences = _sequences(database_path)
                recording = Mock(spec=logging.Logger)

                Memory(database_path, logger=recording).initialize()

                self.assert_at_current_version(database_path, start)
                self.assertEqual(_applied(recording), list(start.steps))
                self.assertEqual(_applied(recording)[-3:], LATER_STEPS)
                self.assertEqual(
                    _row_counts(database_path),
                    {**BASELINE_COUNTS, "memories": len(start.memories)},
                )
                self.assertEqual(_sequences(database_path), sequences)
                for table in TASK_TABLES:
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

    def test_a_version_six_database_keeps_every_row_and_high_water_mark(
        self,
    ) -> None:
        database_path = self.database("seeded")
        _build_seeded_v6(database_path)
        before = _everything(database_path)
        self.assertIn(("saved_conversations", 2), before["sqlite_sequence"])
        self.assertIn(("conversation_summaries", 2), before["sqlite_sequence"])
        recording = Mock(spec=logging.Logger)

        Memory(database_path, logger=recording).initialize()

        self.assertEqual(
            _everything(database_path),
            {
                **before,
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
            [APPLIED_LINE, DELIVERY_APPLIED_LINE, PROJECT_APPLIED_LINE, READY_LINE],
        )

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

    def test_migrated_records_number_from_one_and_never_reuse_ids(self) -> None:
        database_path = self.build(START_V6)
        memory = Memory(database_path, logger=TEST_LOGGER)
        memory.initialize()
        sequences = _sequences(database_path)

        self.assertEqual(memory.create_task("buy milk"), 1)
        self.assertEqual(
            memory.create_reminder("call mom", REMINDER_TIME, REMINDER_INSTANT),
            1,
        )
        self.assertTrue(memory.delete_task(1))
        self.assertTrue(memory.delete_reminder(1))
        self.assertEqual(memory.create_task("buy milk"), 2)
        self.assertEqual(
            memory.create_reminder("call mom", REMINDER_TIME, REMINDER_INSTANT),
            2,
        )
        # Each new table's own mark is the only bookkeeping the inserts add,
        # and no other record type was touched.
        self.assertEqual(
            _sequences(database_path),
            {**sequences, "tasks": 2, "reminders": 2},
        )
        self.assertEqual(
            _record_rows(database_path),
            {
                "interactions": list(MIGRATED_INTERACTIONS),
                "notes": list(MIGRATED_NOTES),
                "memories": list(MIGRATED_MEMORIES),
            },
        )


class FreshTaskDatabaseTests(_TemporaryDatabaseTestCase):
    """Verify a database Maria creates itself starts at the current version."""

    def test_a_new_database_is_created_at_nine_with_no_ladder(self) -> None:
        database_path = self.database("fresh")
        recording = Mock(spec=logging.Logger)

        Memory(database_path, logger=recording).initialize()

        self.assertEqual(_messages(recording), [CREATED_LINE, READY_LINE])
        self.assertEqual(_applied(recording), [])
        self.assertEqual(read_schema_version(database_path), 9)
        self.assertEqual(table_names(database_path), CURRENT_TABLES)
        self.assertLessEqual(set(TASK_TABLES), set(CURRENT_TABLES))
        self.assertEqual(
            _describe(database_path),
            memory_module._reference_schema(CURRENT_SCHEMA),
        )

    def test_a_fresh_database_equals_a_migrated_version_six_one(self) -> None:
        fresh_path = self.database("fresh")
        migrated_path = self.build(START_V6)

        Memory(fresh_path, logger=TEST_LOGGER).initialize()
        Memory(migrated_path, logger=TEST_LOGGER).initialize()

        self.assertEqual(_describe(fresh_path), _describe(migrated_path))
        self.assertEqual(_object_names(fresh_path), _object_names(migrated_path))


class TaskStepAtomicityTests(_TemporaryDatabaseTestCase):
    """Verify a failed step leaves version 6 whole and resumes exactly once."""

    def _injections(self) -> dict[str, Callable[[], object]]:
        """Return each way the new step is made to fail part-way."""
        real_step = _task_step()
        real_write = memory_module._write_schema_version

        def refuse_the_new_stamp(connection: sqlite3.Connection, version: int) -> None:
            if version == 7:
                raise sqlite3.OperationalError("injected failure stamping version 7")
            real_write(connection, version)

        return {
            "after the tasks table": lambda: patch.object(
                memory_module,
                "_MIGRATIONS",
                _ladder_with_step(real_step.statements[0], NEVER_COMMITTED),
            ),
            "after both tables": lambda: patch.object(
                memory_module,
                "_MIGRATIONS",
                _ladder_with_step(*real_step.statements, NEVER_COMMITTED),
            ),
            "at the version marker": lambda: patch.object(
                memory_module,
                "_write_schema_version",
                side_effect=refuse_the_new_stamp,
            ),
        }

    def test_a_failure_inside_the_step_leaves_version_six_whole(self) -> None:
        for label, injection in self._injections().items():
            with self.subTest(injected=label):
                database_path = self.database(label.replace(" ", "-"))
                _build_seeded_v6(database_path)
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
                self.assertEqual(read_schema_version(database_path), 6)
                self.assertEqual(
                    _describe(database_path),
                    memory_module._reference_schema(V6_SCHEMA),
                )
                self.assertEqual(table_names(database_path), VERSION_SIX_TABLES)
                self.assertEqual(_everything(database_path), before_rows)
                self.assertEqual(database_path.read_bytes(), before_bytes)
                self.assertEqual(_siblings(database_path), before_siblings)
                self.assertNotIn("never_committed", _object_names(database_path))
                self.assertEqual(_applied(recording), [])
                self.assertEqual(_messages(recording), [])
                self.assertEqual(_messages(recording, "error"), [FAILED_LINE])
                recording.exception.assert_not_called()

    def test_a_retry_after_a_failed_step_applies_it_exactly_once(self) -> None:
        database_path = self.build(START_V6)
        recording = Mock(spec=logging.Logger)
        retrying = Memory(database_path, logger=recording)

        with self._injections()["after both tables"]():
            with self.assertRaisesRegex(RuntimeError, DATABASE_REFUSED_PATTERN):
                retrying.initialize()
        retrying.initialize()
        retrying.initialize()

        self.assertEqual(_applied(recording), LATER_STEPS)
        self.assertEqual(_messages(recording).count(APPLIED_LINE), 1)
        self.assert_at_current_version(database_path, START_V6)

    def test_a_climb_that_fails_at_the_new_step_stops_whole_at_version_six(
        self,
    ) -> None:
        """Each step commits alone, so the failure undoes only version 7."""
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

                self.assertEqual(read_schema_version(database_path), 6)
                self.assertEqual(
                    _applied(recording),
                    [
                        "add_memory_context_consent",
                        "add_note_context_consent",
                        "create_audit_summaries",
                        "create_conversation_records",
                    ],
                )
                self.assertEqual(_messages(recording, "error"), [FAILED_LINE])

                retrying.initialize()

                self.assertEqual(_applied(recording)[-3:], LATER_STEPS)
                self.assertEqual(_applied(recording).count(STEP_NAME), 1)
                self.assert_at_current_version(database_path, ALL_STARTS[0])


class TaskStepContentionTests(_TemporaryDatabaseTestCase):
    """Verify contention during the new step reads as busy and changes nothing."""

    def test_a_write_lock_during_the_step_gives_the_busy_message(self) -> None:
        database_path = self.build(START_V6)
        before_bytes = database_path.read_bytes()
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
        self.assertEqual(read_schema_version(database_path), 6)
        self.assertEqual(database_path.read_bytes(), before_bytes)

    def test_the_step_applies_exactly_once_after_the_lock_is_released(self) -> None:
        database_path = self.build(START_V6)
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
        self.assert_at_current_version(database_path, START_V6)

    def test_a_locked_report_during_the_step_is_contention_too(self) -> None:
        database_path = self.build(START_V6)
        before_bytes = database_path.read_bytes()
        locked = sqlite3.OperationalError("database table is locked")
        locked.sqlite_errorcode = sqlite3.SQLITE_LOCKED
        real_runner = memory_module._run_in_one_transaction
        versions: list[int] = []

        def lock_only_the_new_step(connection, statements, version) -> None:
            versions.append(version)
            if version == 7:
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
        self.assertEqual(versions, [7])
        self.assertEqual(_messages(recording, "error"), [BUSY_LINE])
        self.assertEqual(database_path.read_bytes(), before_bytes)


class VersionSevenDriftTests(_TemporaryDatabaseTestCase):
    """Verify a stamped version 7 is checked against its frozen schema first."""

    def _assert_each_refused(self, drifts: dict[str, tuple[str, ...]]) -> None:
        """Build each drifted schema stamped 7 and assert it is refused untouched."""
        exact = memory_module._reference_schema(V7_SCHEMA)
        for label, schema in drifts.items():
            with self.subTest(drift=label):
                self.assertNotEqual(memory_module._reference_schema(schema), exact)
                database_path = self.database(label.replace(" ", "-"))
                _build_schema(database_path, schema, 7)

                recording = self.assert_refused_untouched(database_path, marker=7)

                self.assertEqual(
                    _messages(recording, "error"),
                    [DRIFTED_VERSION_SEVEN_LINE],
                )

    def test_the_exact_version_seven_schema_is_accepted_and_climbs_two_steps(
        self,
    ) -> None:
        """Version 7 is no longer current: it is accepted, then migrated by
        V1-23-R1's step to version 8 and V1-24-R1's to version 9, and by
        nothing else."""
        database_path = self.database("exact")
        _build_schema(database_path, V7_SCHEMA, 7)
        recording = Mock(spec=logging.Logger)

        Memory(database_path, logger=recording).initialize()

        self.assertEqual(read_schema_version(database_path), 9)
        self.assertEqual(
            _applied(recording),
            [DELIVERY_STEP_NAME, PROJECT_STEP_NAME],
        )
        self.assertEqual(
            _messages(recording),
            [DELIVERY_APPLIED_LINE, PROJECT_APPLIED_LINE, READY_LINE],
        )
        self.assertEqual(_messages(recording, "error"), [])

    def test_a_version_seven_database_missing_a_new_table_is_refused(self) -> None:
        self._assert_each_refused(
            {
                "missing tasks": V6_SCHEMA + (FROZEN_REMINDERS_TABLE,),
                "missing reminders": V6_SCHEMA + (FROZEN_TASKS_TABLE,),
                "missing both new tables": V6_SCHEMA,
            }
        )

    def test_an_altered_new_column_is_refused_untouched(self) -> None:
        self._assert_each_refused(
            {
                "unchecked task text": _altered(
                    "tasks",
                    "text TEXT NOT NULL CHECK (length(CAST(text AS BLOB)) > 0)",
                    "text TEXT NOT NULL",
                ),
                # SQLite's length() of TEXT stops at the first NUL.
                "a task text check that stops at NUL": _altered(
                    "tasks",
                    "CHECK (length(CAST(text AS BLOB)) > 0)",
                    "CHECK (length(text) > 0)",
                ),
                "an unshaped due date": _altered(
                    "tasks",
                    "OR due_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'",
                    "OR 1",
                ),
                "reusable task ids": _altered(
                    "tasks",
                    "id INTEGER PRIMARY KEY AUTOINCREMENT",
                    "id INTEGER PRIMARY KEY",
                ),
                "a third task status": _altered(
                    "tasks",
                    "status IN ('open', 'completed')",
                    "status IN ('open', 'completed', 'delivered')",
                ),
                "a recurrence column": _altered(
                    "reminders",
                    "remind_time TEXT NOT NULL,",
                    "remind_time TEXT NOT NULL,\n        repeat TEXT,",
                ),
                "a nullable reminder time": _altered(
                    "reminders",
                    "remind_time TEXT NOT NULL,",
                    "remind_time TEXT,",
                ),
                "an unshaped instant": _altered(
                    "reminders",
                    "remind_utc TEXT NOT NULL CHECK (",
                    "remind_utc TEXT NOT NULL CHECK (1 OR ",
                ),
                "a delivered marker": _altered(
                    "reminders",
                    "completed_at TEXT,",
                    "completed_at TEXT,\n        delivered_at TEXT,",
                ),
                "consent on by default": _altered(
                    "reminders",
                    "context_digest TEXT\n",
                    "context_digest TEXT DEFAULT '" + "0" * 64 + "'\n",
                ),
                "a link to a task": _altered(
                    "reminders",
                    "updated_at TEXT NOT NULL,",
                    "updated_at TEXT NOT NULL,\n"
                    "        task_id INTEGER REFERENCES tasks (id) ON DELETE CASCADE,",
                ),
            }
        )

    def test_an_extra_trigger_index_view_or_table_is_refused_untouched(self) -> None:
        self._assert_each_refused(
            {
                "a trigger completing reminders": V7_SCHEMA
                + (
                    "CREATE TRIGGER tasks_complete AFTER UPDATE ON tasks BEGIN "
                    "UPDATE reminders SET status = NEW.status; END",
                ),
                "an index on the instant": V7_SCHEMA
                + ("CREATE INDEX idx_reminders_utc ON reminders (remind_utc)",),
                "a view": V7_SCHEMA
                + ("CREATE VIEW due_view AS SELECT id FROM reminders",),
                "an extra table": V7_SCHEMA
                + ("CREATE TABLE deliveries (id INTEGER PRIMARY KEY, sent TEXT)",),
            }
        )

    def test_an_unstamped_file_in_the_version_seven_shape_is_refused(self) -> None:
        database_path = self.database("unstamped-seven")
        _build_schema(database_path, V7_SCHEMA, None)

        recording = self.assert_refused_untouched(database_path, marker=UNVERSIONED)

        self.assertEqual(
            _messages(recording, "error"),
            [UNRECOGNISED_UNVERSIONED_LINE],
        )

    def test_a_database_stamped_ten_is_too_new_and_left_untouched(self) -> None:
        database_path = self.database("newer")
        _build_schema(database_path, V7_SCHEMA, NEWER_SCHEMA_VERSION)
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


class TaskMigrationLoggingTests(unittest.TestCase):
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
                "due_date",
                "remind_time",
                "remind_utc",
                "GLOB",
                "We agreed on a plan.",
            )
        )
        return sentinels

    def test_the_step_writes_its_fixed_line_to_a_real_log(self) -> None:
        _build_seeded_v6(self.database_path)
        configure_logging(self.log_dir, "DEBUG")

        Memory(self.database_path, logger=get_logger("memory")).initialize()

        self.assertEqual(
            self._entries(),
            [
                LOGGING_CONFIGURED_ENTRY,
                ("INFO", "maria.memory", APPLIED_LINE),
                ("INFO", "maria.memory", DELIVERY_APPLIED_LINE),
                ("INFO", "maria.memory", PROJECT_APPLIED_LINE),
                ("INFO", "maria.memory", READY_LINE),
            ],
        )
        assert_log_omits(self, self.log_file, self._sentinels())

    def test_a_failed_step_logs_no_sqlite_text_or_traceback(self) -> None:
        _build_seeded_v6(self.database_path)
        configure_logging(self.log_dir, "DEBUG")
        broken = _ladder_with_step(*_task_step().statements, NEVER_COMMITTED)

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
        _build_seeded_v6(self.database_path)
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


class TaskMigrationLiveDatabaseRefusalTests(_TemporaryDatabaseTestCase):
    """Prove every database helper this file adds refuses the live directory."""

    def test_every_new_helper_refuses_the_live_database_directory(self) -> None:
        refused_paths = {
            "live file": DATABASE_PATH,
            "live directory": DATABASE_DIR,
            "sibling": DATABASE_DIR / "tasks-copy.db",
            "descendant": DATABASE_DIR / "nested" / "copy.db",
            "traversal": DATABASE_DIR / "nested" / ".." / DATABASE_PATH.name,
        }
        helpers = (
            ("stamped version 6", _build_stamped_v6),
            ("seeded version 6", _build_seeded_v6),
            ("every row", _everything),
            ("start: stamped version 6", START_V6.build),
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
        _build_seeded_v6(database_path)
        self.assertEqual(read_schema_version(database_path), 6)
        self.assertNotIn(DATABASE_DIR.resolve(), database_path.resolve().parents)


if __name__ == "__main__":
    unittest.main()
