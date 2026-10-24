"""Focused tests for schema version 9 and its create_project_records step.

V1-24-R1 appends one forward step to Maria's migration ladder. It creates the
three empty tables projects, project facts, and the activation setting live
in, with no foreign key, trigger, index, or cascade, and touches no existing
table, row, or AUTOINCREMENT high-water mark. Every database is built under
a ``TemporaryDirectory`` from the frozen fixtures and the shapes
``tests.test_audit_migration`` writes out by hand, where the three new tables
are pinned word for word. A migrated database was never activated: the
migration writes no activation row, and nothing reports it unreadable.
"""

from __future__ import annotations

import logging
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from core import memory as memory_module
from core.constants import APP_VERSION, DATABASE_SCHEMA_VERSION
from core.desktop_notice import NotificationState
from core.failures import Failure, FailureKind, TypedFailureError
from core.memory import Memory
from core.project_records import ActivationState
from deliver_reminders import EXIT_STORAGE, run_pass
from tests.schema_fixtures import read_schema_version, table_names
from tests.test_audit_migration import (
    APPLIED_STEP_LINES,
    BASELINE_COUNTS,
    CURRENT_SCHEMA,
    CURRENT_TABLES,
    DATABASE_REFUSED_PATTERN,
    EXPECTED_FROZEN_SCHEMAS,
    EXPECTED_LADDER,
    FROZEN_PROJECT_ACTIVATION_TABLE,
    FROZEN_PROJECT_FACTS_TABLE,
    FROZEN_PROJECTS_TABLE,
    NEVER_COMMITTED,
    NEWER_SCHEMA_VERSION,
    PROJECT_RECORDS_VERSION,
    PROJECT_TABLES,
    READY_LINE,
    REMINDER_DELIVERY_VERSION,
    V8_SCHEMA,
    V9_SCHEMA,
    _applied,
    _build_schema,
    _describe,
    _messages,
    _row_counts,
    _sequences,
    _TemporaryDatabaseTestCase,
)
from tests.test_task_reminder_migration import EVERY_START, START_V6


STEP_NAME = "create_project_records"
APPLIED_LINE = APPLIED_STEP_LINES[STEP_NAME]
FAILED_LINE = (
    "Migration create_project_records to schema version 9 failed and was "
    "rolled back; the database remains at version 8."
)
DRIFTED_LINE = (
    "Refused a database stamped at schema version 9 whose structure is not "
    "the frozen schema for that version."
)
TOO_NEW_LINE = (
    "Refused a database stamped at schema version 10; this build expects 9."
)
NOW = datetime(2026, 10, 9, 21, 24, 13, tzinfo=timezone.utc)


def _project_step() -> object:
    """Return the memory layer's own project step, by its name."""
    (step,) = [
        migration
        for migration in memory_module._MIGRATIONS
        if migration.name == STEP_NAME
    ]
    return step


def _ladder_with_step(*statements: str) -> tuple[object, ...]:
    """Return the real ladder with the project step's statements replaced."""
    return tuple(
        memory_module._Migration(
            version=PROJECT_RECORDS_VERSION,
            name=STEP_NAME,
            statements=statements,
        )
        if migration.name == STEP_NAME
        else migration
        for migration in memory_module._MIGRATIONS
    )


class ProjectSchemaPinTests(unittest.TestCase):
    """Pin schema 9, its one step, and the history it must not disturb."""

    def test_version_nine_is_current_and_the_app_version_is_unchanged(self) -> None:
        self.assertEqual(PROJECT_RECORDS_VERSION, 9)
        self.assertEqual(DATABASE_SCHEMA_VERSION, 9)
        self.assertEqual(memory_module.DATABASE_SCHEMA_VERSION, 9)
        self.assertEqual(NEWER_SCHEMA_VERSION, 10)
        self.assertEqual(APP_VERSION, "0.2.0")

    def test_the_step_is_eighth_and_last_and_runs_its_three_frozen_creates(
        self,
    ) -> None:
        step = _project_step()
        names = [migration.name for migration in memory_module._MIGRATIONS]

        self.assertEqual(names.index(STEP_NAME), 7)
        self.assertEqual(names[-1], STEP_NAME)
        self.assertEqual(
            (step.version, step.name, step.statements),
            (
                PROJECT_RECORDS_VERSION,
                STEP_NAME,
                (
                    FROZEN_PROJECTS_TABLE,
                    FROZEN_PROJECT_FACTS_TABLE,
                    FROZEN_PROJECT_ACTIVATION_TABLE,
                ),
            ),
        )
        self.assertEqual(
            EXPECTED_LADDER[-1],
            (step.version, step.name, step.statements),
        )
        for statement in step.statements:
            with self.subTest(statement=statement.split("(")[0].strip()):
                self.assertNotIn("IF NOT EXISTS", statement)
                for word in ("REFERENCES", "TRIGGER", "INDEX", "CASCADE"):
                    self.assertNotIn(word, statement.upper())
        self.assertIsNone(memory_module._validate_migration_ladder())

    def test_every_earlier_step_and_the_frozen_version_eight_are_unchanged(
        self,
    ) -> None:
        self.assertEqual(
            tuple(
                (step.version, step.name, step.statements)
                for step in memory_module._MIGRATIONS[:7]
            ),
            EXPECTED_LADDER[:7],
        )
        declared = dict(memory_module._FROZEN_SCHEMAS)
        for version in range(1, 10):
            with self.subTest(version=version):
                self.assertEqual(declared[version], EXPECTED_FROZEN_SCHEMAS[version])
        self.assertEqual(
            memory_module._reference_schema(V8_SCHEMA),
            memory_module._reference_schema(
                memory_module._frozen_schema(REMINDER_DELIVERY_VERSION)
            ),
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


class ProjectStepUpgradeTests(_TemporaryDatabaseTestCase):
    """Every earlier database climbs to 9, keeping every row, never activated."""

    def test_every_earlier_version_climbs_to_nine_ending_with_the_step(self) -> None:
        for start in (*EVERY_START,):
            with self.subTest(start=start.label):
                database_path = self.build(start)
                sequences = _sequences(database_path)
                recording = Mock(spec=logging.Logger)

                memory = Memory(database_path, logger=recording)
                memory.initialize()

                self.assertEqual(read_schema_version(database_path), 9)
                self.assertEqual(table_names(database_path), CURRENT_TABLES)
                self.assertEqual(_applied(recording)[-1], STEP_NAME)
                self.assertEqual(
                    _row_counts(database_path),
                    {**BASELINE_COUNTS, "memories": len(start.memories)},
                )
                self.assertEqual(_sequences(database_path), sequences)
                for table in PROJECT_TABLES:
                    self.assertNotIn(table, _sequences(database_path))
                self.assertIs(
                    memory.project_activation().state,
                    ActivationState.NEVER_ACTIVATED,
                )
                listing = memory.list_projects()
                self.assertEqual(listing.projects, ())
                self.assertIs(
                    listing.activation.state,
                    ActivationState.NEVER_ACTIVATED,
                )

    def test_a_version_eight_database_climbs_one_step_and_reopens_unchanged(
        self,
    ) -> None:
        database_path = self.database("eight")
        _build_schema(database_path, V8_SCHEMA, 8)
        recording = Mock(spec=logging.Logger)

        Memory(database_path, logger=recording).initialize()
        migrated = database_path.read_bytes()
        reopening = Mock(spec=logging.Logger)
        with patch.object(
            memory_module,
            "_run_in_one_transaction",
            side_effect=AssertionError("a current database is not written"),
        ):
            Memory(database_path, logger=reopening).initialize()

        self.assertEqual(_applied(recording), [STEP_NAME])
        self.assertEqual(_messages(recording), [APPLIED_LINE, READY_LINE])
        self.assertEqual(database_path.read_bytes(), migrated)
        self.assertEqual(_messages(reopening), [READY_LINE])
        self.assertEqual(
            _describe(database_path),
            memory_module._reference_schema(CURRENT_SCHEMA),
        )

    def test_a_new_database_is_created_at_nine_never_activated(self) -> None:
        database_path = self.database("fresh")
        memory = Memory(database_path, logger=Mock(spec=logging.Logger))

        memory.initialize()

        self.assertEqual(read_schema_version(database_path), 9)
        self.assertEqual(memory.project_activation().token, (None, 0))
        self.assertEqual(_row_counts(database_path)["project_activation"], 0)


class ProjectStepAtomicityTests(_TemporaryDatabaseTestCase):
    """A failure inside the step leaves version 8 whole; a retry applies it once."""

    def test_a_failure_inside_the_step_leaves_version_eight_whole(self) -> None:
        for label, statements in (
            ("after the projects table", (FROZEN_PROJECTS_TABLE, NEVER_COMMITTED)),
            (
                "after all three tables",
                (*_project_step().statements, NEVER_COMMITTED),
            ),
        ):
            with self.subTest(injected=label):
                database_path = self.database(label.replace(" ", "-"))
                _build_schema(database_path, V8_SCHEMA, 8)
                before = database_path.read_bytes()
                recording = Mock(spec=logging.Logger)

                with patch.object(
                    memory_module,
                    "_MIGRATIONS",
                    _ladder_with_step(*statements),
                ):
                    with self.assertRaisesRegex(RuntimeError, DATABASE_REFUSED_PATTERN):
                        Memory(database_path, logger=recording).initialize()

                self.assertEqual(read_schema_version(database_path), 8)
                self.assertEqual(
                    _describe(database_path),
                    memory_module._reference_schema(V8_SCHEMA),
                )
                self.assertEqual(database_path.read_bytes(), before)
                self.assertEqual(_messages(recording, "error"), [FAILED_LINE])
                Memory(database_path, logger=recording).initialize()
                self.assertEqual(_applied(recording), [STEP_NAME])
                self.assertEqual(read_schema_version(database_path), 9)

    def test_a_climb_from_version_six_that_fails_at_the_step_keeps_the_rest(
        self,
    ) -> None:
        database_path = self.build(START_V6)
        recording = Mock(spec=logging.Logger)

        with patch.object(
            memory_module,
            "_MIGRATIONS",
            _ladder_with_step(FROZEN_PROJECTS_TABLE, NEVER_COMMITTED),
        ):
            with self.assertRaisesRegex(RuntimeError, DATABASE_REFUSED_PATTERN):
                Memory(database_path, logger=recording).initialize()

        self.assertEqual(read_schema_version(database_path), 8)
        self.assertEqual(
            _applied(recording),
            ["create_tasks_and_reminders", "add_reminder_delivery_state"],
        )


class ProjectDriftTests(_TemporaryDatabaseTestCase):
    """A stamped 9 is checked against its frozen schema; a newer one is refused."""

    def test_a_drifted_version_nine_is_refused_untouched(self) -> None:
        def altered(table: str, old: str, new: str) -> tuple[str, ...]:
            changed = table.replace(old, new)
            self.assertNotEqual(changed, table)
            return tuple(
                changed if statement == table else statement
                for statement in V9_SCHEMA
            )

        for label, schema in (
            (
                "no activation table",
                tuple(
                    statement
                    for statement in V9_SCHEMA
                    if statement != FROZEN_PROJECT_ACTIVATION_TABLE
                ),
            ),
            (
                "a second activation row allowed",
                altered(
                    FROZEN_PROJECT_ACTIVATION_TABLE,
                    "PRIMARY KEY CHECK (id = 1)",
                    "PRIMARY KEY",
                ),
            ),
            (
                "an unchecked folder",
                altered(
                    FROZEN_PROJECTS_TABLE,
                    " CHECK (length(folder) <= 512)",
                    "",
                ),
            ),
            (
                "a longer digest",
                altered(
                    FROZEN_PROJECT_FACTS_TABLE,
                    "length(approved_digest) = 64",
                    "length(approved_digest) = 65",
                ),
            ),
        ):
            with self.subTest(drift=label):
                database_path = self.database(label.replace(" ", "-"))
                _build_schema(database_path, schema, 9)

                recording = self.assert_refused_untouched(database_path, marker=9)

                self.assertEqual(_messages(recording, "error"), [DRIFTED_LINE])

    def test_a_database_stamped_ten_is_too_new_and_left_untouched(self) -> None:
        database_path = self.database("newer")
        _build_schema(database_path, V9_SCHEMA, NEWER_SCHEMA_VERSION)
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


class ClosedAppPassCompatibilityTests(_TemporaryDatabaseTestCase):
    """The closed-app pass refuses a version 8 database and migrates nothing.

    A deliberate compatibility change: it exits 3 until the command line or
    the window has started once and migrated the database to version 9.
    """

    def test_a_pass_over_a_version_eight_database_exits_three_untouched(
        self,
    ) -> None:
        database_path = self.database("eight")
        _build_schema(database_path, V8_SCHEMA, 8)
        config_path = self.root / "config.toml"
        config_path.write_text(
            "[paths]\n"
            f'database = "{database_path.as_posix()}"\n'
            f'logs = "{(self.root / "logs").as_posix()}"\n',
            encoding="utf-8",
        )
        before = database_path.read_bytes()
        notices = Mock()
        notices.available.return_value = True
        notices.notification_state.return_value = NotificationState.ACCEPTS

        status = run_pass(
            clock=lambda: NOW,
            config_path=config_path,
            notices=notices,
            memory_factory=lambda path: Memory(
                path,
                logger=Mock(spec=logging.Logger),
                existing_only=True,
            ),
            configure_log=Mock(),
        )

        self.assertEqual(status, EXIT_STORAGE)
        self.assertEqual(EXIT_STORAGE, 3)
        self.assertEqual(database_path.read_bytes(), before)
        notices.show.assert_not_called()
        Memory(database_path, logger=Mock(spec=logging.Logger)).initialize()
        self.assertEqual(read_schema_version(database_path), 9)


if __name__ == "__main__":
    unittest.main()
