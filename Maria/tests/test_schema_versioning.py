"""Focused tests for Maria's database schema versioning and migrations.

Every database here is built under a ``TemporaryDirectory``. The historical
shapes come from ``tests.schema_fixtures``, which rebuilds them from its own
frozen text rather than by calling the code under test, so a migration test
starts from the database owners actually have instead of from whatever the
current schema happens to create.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import logging
import re
import sqlite3
import tempfile
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import tests.schema_fixtures as schema_fixtures
from core import memory as memory_module
from core.constants import APP_VERSION, DATABASE_SCHEMA_VERSION
from core.failures import Failure, FailureKind, TypedFailureError
from core.logger import configure_logging, get_logger
from core.memory import Memory
from tests.log_assertions import assert_log_omits
from tests.schema_fixtures import (
    BASELINE_INTERACTIONS,
    BASELINE_MEMORIES,
    BASELINE_NOTES,
    PRE_MEMORIES_SCHEMA,
    V0_2_0_BASELINE_SCHEMA,
    create_pre_memories_baseline,
    create_schema,
    create_v0_2_0_baseline,
    read_schema_version,
    stamp_schema_version,
    table_names,
)


TEST_LOGGER = logging.getLogger("tests.schema_versioning")
TEST_LOGGER.addHandler(logging.NullHandler())
TEST_LOGGER.propagate = False

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = PROJECT_ROOT / "tests"

# The version the baseline stamp must use, written out rather than imported,
# so the number itself is pinned by the test instead of agreed with.
EXPECTED_SCHEMA_VERSION = 2
UNVERSIONED = 0
PRE_MEMORIES_VERSION = 1

BASELINE_TABLES = frozenset({"interactions", "notes", "memories"})

# The two constraints the v0.2.0 ``memories`` table is required to carry,
# written out in their normalized form rather than read back from the module,
# so the test pins what must be verified instead of agreeing with whatever the
# classifier currently extracts.
REQUIRED_MEMORY_CHECKS = ("length(fact)>0", "length(normalized_fact)>0")

# The same v0.2.0 schema spelled as differently as SQL allows while stating
# exactly the same thing: uppercase keywords, no spacing, every identifier
# quoted in a different style, redundant parentheses, a comment, and the
# checks moved from their columns to the table. None of that changes a single
# rule, so all of it has to be accepted.
FORMATTING_EQUIVALENT_BASELINE = (
    'CREATE TABLE "interactions" ("id" INTEGER PRIMARY KEY AUTOINCREMENT,'
    '"user_input" TEXT NOT NULL,"response" TEXT NOT NULL,'
    '"created_at" TEXT NOT NULL)',
    'CREATE TABLE [notes] ([id] INTEGER PRIMARY KEY AUTOINCREMENT,'
    '[title] TEXT NOT NULL,[body] TEXT NOT NULL,[created_at] TEXT NOT NULL,'
    '[updated_at] TEXT NOT NULL)',
    """
    CREATE TABLE `memories` (   -- the published baseline, spelled differently
        `id`   INTEGER   PRIMARY KEY   AUTOINCREMENT ,
        `fact` TEXT NOT NULL ,
        `normalized_fact` TEXT NOT NULL UNIQUE ,
        `created_at` TEXT NOT NULL ,
        CHECK ((  LENGTH ( `fact` )  >  0  )) ,
        CHECK (
            ( length ( [normalized_fact] ) > 0 )
        )
    )
    """,
    "CREATE INDEX idx_interactions_created_at ON interactions(created_at)",
    "CREATE INDEX idx_notes_title ON notes(title)",
)

# A distinctive value a SQLite failure carries but nothing may repeat.
SQLITE_DETAIL_SENTINEL = "private-sqlite-detail-6c1af03d92"

# A statement that only succeeds once ``memories`` exists, so a ladder that
# ran its steps out of order could not complete.
DEPENDS_ON_MEMORIES = (
    "CREATE INDEX idx_probe_memories_created_at ON memories (created_at)"
)

# SQL that must not appear anywhere outside the memory boundary.
SQL_STATEMENT = re.compile(
    r"\b(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM"
    r"|CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|VIEW|TRIGGER)|ALTER\s+TABLE"
    r"|DROP\s+(?:TABLE|INDEX)|PRAGMA\s+\w+|BEGIN\s+IMMEDIATE|user_version"
    r"|sqlite_master)\b",
    re.IGNORECASE | re.DOTALL,
)
SQLITE_IMPORT = re.compile(r"(?m)^\s*(?:import|from)\s+sqlite3\b")


class SchemaVersionConstantTests(unittest.TestCase):
    """Verify the schema version is its own number, not the app version."""

    def test_schema_version_is_the_exact_expected_whole_number(self) -> None:
        self.assertEqual(DATABASE_SCHEMA_VERSION, EXPECTED_SCHEMA_VERSION)
        self.assertIs(type(DATABASE_SCHEMA_VERSION), int)

    def test_schema_version_is_independent_of_the_application_version(self) -> None:
        self.assertEqual(APP_VERSION, "0.2.0")
        self.assertNotIsInstance(DATABASE_SCHEMA_VERSION, str)
        self.assertNotEqual(str(DATABASE_SCHEMA_VERSION), APP_VERSION)

        # The value must be a literal rather than anything computed from a
        # release string, so a frozen application version cannot pin the
        # schema and a schema change cannot move the application version.
        source = inspect.getsource(
            __import__("core.constants", fromlist=["constants"])
        )
        assignment = re.search(
            r"(?m)^DATABASE_SCHEMA_VERSION\s*=\s*(.+)$",
            source,
        )
        self.assertIsNotNone(assignment)
        self.assertRegex(assignment.group(1).strip(), r"^\d+$")
        self.assertNotIn("APP_VERSION", assignment.group(1))

    def test_the_declared_migrations_reach_exactly_the_expected_version(self) -> None:
        versions = [migration.version for migration in memory_module._MIGRATIONS]

        self.assertEqual(versions, sorted(set(versions)))
        self.assertEqual(versions[-1], DATABASE_SCHEMA_VERSION)
        self.assertIsNone(memory_module._validate_migration_ladder())


class _TemporaryDatabaseTestCase(unittest.TestCase):
    """Base case supplying one temporary database path per test."""

    def setUp(self) -> None:
        self._temporary_directory = TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.root = Path(self._temporary_directory.name)
        self.database_path = self.root / "database" / "maria-test.db"
        self.memory = Memory(self.database_path, logger=TEST_LOGGER)

    def describe(self, database_path: Path | None = None) -> tuple:
        """Return the structural description of a temporary database."""
        target = self.database_path if database_path is None else database_path
        connection = sqlite3.connect(target)
        connection.row_factory = sqlite3.Row
        try:
            return memory_module._describe_schema(connection)
        finally:
            connection.close()


class NewDatabaseTests(_TemporaryDatabaseTestCase):
    """Verify a database Maria creates itself starts stamped and current."""

    def test_a_new_database_is_created_directly_at_the_current_schema(self) -> None:
        self.memory.initialize()

        self.assertEqual(read_schema_version(self.database_path), EXPECTED_SCHEMA_VERSION)
        self.assertEqual(table_names(self.database_path), BASELINE_TABLES)
        self.assertEqual(self.memory.schema_version(), EXPECTED_SCHEMA_VERSION)

    def test_an_empty_file_is_a_new_database_rather_than_a_legacy_one(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path.touch()
        self.assertEqual(self.database_path.stat().st_size, 0)

        recording_logger = Mock(spec=logging.Logger)
        Memory(self.database_path, logger=recording_logger).initialize()

        self.assertEqual(read_schema_version(self.database_path), EXPECTED_SCHEMA_VERSION)
        self.assertEqual(table_names(self.database_path), BASELINE_TABLES)
        # An empty file is created at the current schema in one step. It must
        # not be classified as a legacy database and walked up the ladder.
        logged = [call.args[0] for call in recording_logger.info.call_args_list]
        self.assertTrue(
            any("Created a new database" in message for message in logged)
        )
        self.assertEqual(
            [message for message in logged if "Applied migration" in message],
            [],
        )
        self.assertEqual(
            [message for message in logged if "Classified an unversioned" in message],
            [],
        )

    def test_reinitializing_a_current_database_changes_nothing(self) -> None:
        self.memory.initialize()
        memory_id, _ = self.memory.store_memory("A fact that must survive")
        before = self.database_path.read_bytes()

        self.memory.initialize()
        self.memory.initialize()

        self.assertEqual(self.database_path.read_bytes(), before)
        self.assertEqual(read_schema_version(self.database_path), EXPECTED_SCHEMA_VERSION)
        self.assertEqual(
            [row["id"] for row in self.memory.list_memories()],
            [memory_id],
        )

    def test_a_current_database_runs_no_migration_at_all(self) -> None:
        self.memory.initialize()
        recording_logger = Mock(spec=logging.Logger)
        restarted = Memory(self.database_path, logger=recording_logger)

        restarted.initialize()

        applied = [
            call.args[0]
            for call in recording_logger.info.call_args_list
            if "Applied migration" in call.args[0]
        ]
        self.assertEqual(applied, [])


class BaselineClassificationTests(_TemporaryDatabaseTestCase):
    """Verify each unversioned shape is classified explicitly and once."""

    def test_the_fixture_rebuilds_the_exact_frozen_v0_2_0_baseline(self) -> None:
        create_v0_2_0_baseline(self.database_path)

        self.assertEqual(read_schema_version(self.database_path), UNVERSIONED)
        self.assertEqual(table_names(self.database_path), BASELINE_TABLES)
        self.assertEqual(
            self.describe(),
            memory_module._reference_schema(
                memory_module._HISTORICAL_UNVERSIONED_SCHEMAS[0][1]
            ),
        )

    def test_a_valid_unversioned_baseline_is_stamped_without_migrating(self) -> None:
        create_v0_2_0_baseline(self.database_path)
        before = self.describe()

        self.memory.initialize()

        self.assertEqual(read_schema_version(self.database_path), EXPECTED_SCHEMA_VERSION)
        self.assertEqual(self.describe(), before)

    def test_baseline_rows_survive_initialization_unchanged(self) -> None:
        create_v0_2_0_baseline(self.database_path)

        self.memory.initialize()

        for identifier, request, response, created_at in BASELINE_INTERACTIONS:
            self.assertEqual(
                self.memory.get_interaction(identifier),
                {
                    "id": identifier,
                    "user_input": request,
                    "response": response,
                    "created_at": created_at,
                },
            )
        for identifier, title, body, created_at, updated_at in BASELINE_NOTES:
            self.assertEqual(
                self.memory.get_note(identifier),
                {
                    "id": identifier,
                    "title": title,
                    "body": body,
                    "created_at": created_at,
                    "updated_at": updated_at,
                },
            )
        for identifier, fact, _normalized, created_at in BASELINE_MEMORIES:
            self.assertEqual(
                self.memory.get_memory(identifier),
                {"id": identifier, "fact": fact, "created_at": created_at},
            )

    def test_normalized_memory_uniqueness_still_applies_after_stamping(self) -> None:
        create_v0_2_0_baseline(self.database_path)

        self.memory.initialize()
        existing_id, existing_fact, _normalized, _created = BASELINE_MEMORIES[0]
        memory_id, created = self.memory.store_memory(existing_fact.upper())

        self.assertFalse(created)
        self.assertEqual(memory_id, existing_id)
        self.assertEqual(self.memory.get_memory(existing_id)["fact"], existing_fact)

    def test_note_updated_timestamps_survive_and_still_order_listings(self) -> None:
        create_v0_2_0_baseline(self.database_path)

        self.memory.initialize()
        listed = self.memory.list_notes()

        expected = sorted(
            BASELINE_NOTES,
            key=lambda note: (note[4], note[0]),
            reverse=True,
        )
        self.assertEqual(
            [(row["id"], row["updated_at"]) for row in listed],
            [(note[0], note[4]) for note in expected],
        )

    def test_a_populated_legacy_database_is_migrated_rather_than_recreated(
        self,
    ) -> None:
        create_pre_memories_baseline(self.database_path)

        self.memory.initialize()

        self.assertEqual(read_schema_version(self.database_path), EXPECTED_SCHEMA_VERSION)
        self.assertEqual(table_names(self.database_path), BASELINE_TABLES)
        for identifier, request, response, created_at in BASELINE_INTERACTIONS:
            self.assertEqual(
                self.memory.get_interaction(identifier),
                {
                    "id": identifier,
                    "user_input": request,
                    "response": response,
                    "created_at": created_at,
                },
            )
        self.assertEqual(self.memory.list_memories(), [])

    def test_a_migrated_database_matches_a_freshly_created_one(self) -> None:
        create_pre_memories_baseline(self.database_path)
        fresh_path = self.root / "fresh" / "maria-test.db"

        self.memory.initialize()
        Memory(fresh_path, logger=TEST_LOGGER).initialize()

        self.assertEqual(self.describe(), self.describe(fresh_path))
        self.assertEqual(
            read_schema_version(self.database_path),
            read_schema_version(fresh_path),
        )


class DriftRefusalTests(_TemporaryDatabaseTestCase):
    """Verify a missing marker is never taken as proof of the baseline."""

    def _assert_refused_untouched(self) -> None:
        """Assert initialization refuses and leaves the file exactly as found."""
        before = self.database_path.read_bytes()

        with self.assertRaisesRegex(
            RuntimeError,
            r"^Unable to initialize Maria database\.$",
        ):
            self.memory.initialize()

        self.assertEqual(self.database_path.read_bytes(), before)
        self.assertEqual(read_schema_version(self.database_path), UNVERSIONED)

    def test_a_partially_created_database_is_refused(self) -> None:
        create_schema(self.database_path, V0_2_0_BASELINE_SCHEMA[:2])

        self._assert_refused_untouched()

    def test_a_missing_required_table_is_refused(self) -> None:
        without_notes = tuple(
            statement
            for statement in V0_2_0_BASELINE_SCHEMA
            if "notes" not in statement
        )
        create_schema(self.database_path, without_notes)

        self._assert_refused_untouched()

    def test_a_drifted_column_definition_is_refused(self) -> None:
        drifted = tuple(
            statement.replace("body TEXT NOT NULL", "body TEXT")
            for statement in V0_2_0_BASELINE_SCHEMA
        )
        create_schema(self.database_path, drifted)

        self._assert_refused_untouched()

    def test_an_additional_column_is_refused(self) -> None:
        widened = tuple(
            statement.replace(
                "created_at TEXT NOT NULL\n    )",
                "created_at TEXT NOT NULL,\n        extra TEXT\n    )",
            )
            for statement in V0_2_0_BASELINE_SCHEMA
        )
        self.assertNotEqual(widened, V0_2_0_BASELINE_SCHEMA)
        create_schema(self.database_path, widened)

        self._assert_refused_untouched()

    def test_a_missing_uniqueness_constraint_is_refused(self) -> None:
        without_unique = tuple(
            statement.replace(
                "normalized_fact TEXT NOT NULL UNIQUE",
                "normalized_fact TEXT NOT NULL",
            )
            for statement in V0_2_0_BASELINE_SCHEMA
        )
        create_schema(self.database_path, without_unique)

        self._assert_refused_untouched()

    def test_a_missing_fact_check_is_refused(self) -> None:
        create_schema(self.database_path, _without_check(r"length\(fact\)"))

        self._assert_refused_untouched()

    def test_a_missing_normalized_fact_check_is_refused(self) -> None:
        create_schema(
            self.database_path,
            _without_check(r"length\(normalized_fact\)"),
        )

        self._assert_refused_untouched()

    def test_a_weakened_fact_check_is_refused(self) -> None:
        weakened = tuple(
            statement.replace(
                "CHECK (length(fact) > 0)",
                "CHECK (length(fact) >= 0)",
            )
            for statement in V0_2_0_BASELINE_SCHEMA
        )
        self.assertNotEqual(weakened, V0_2_0_BASELINE_SCHEMA)
        create_schema(self.database_path, weakened)

        self._assert_refused_untouched()

    def test_a_changed_normalized_fact_check_is_refused(self) -> None:
        changed = tuple(
            statement.replace(
                "CHECK (length(normalized_fact) > 0)",
                "CHECK (length(normalized_fact) > -1)",
            )
            for statement in V0_2_0_BASELINE_SCHEMA
        )
        self.assertNotEqual(changed, V0_2_0_BASELINE_SCHEMA)
        create_schema(self.database_path, changed)

        self._assert_refused_untouched()

    def test_a_check_on_the_wrong_column_is_refused(self) -> None:
        misdirected = tuple(
            statement.replace(
                "CHECK (length(normalized_fact) > 0)",
                "CHECK (length(fact) > 0)",
            )
            for statement in V0_2_0_BASELINE_SCHEMA
        )
        self.assertNotEqual(misdirected, V0_2_0_BASELINE_SCHEMA)
        create_schema(self.database_path, misdirected)

        self._assert_refused_untouched()

    def test_a_check_that_is_only_commented_out_is_refused(self) -> None:
        """A commented rule is not a rule, however much it looks like one."""
        commented = tuple(
            statement.replace(
                "fact TEXT NOT NULL CHECK (length(fact) > 0),",
                "fact TEXT NOT NULL, -- CHECK (length(fact) > 0)\n",
            )
            for statement in V0_2_0_BASELINE_SCHEMA
        )
        self.assertNotEqual(commented, V0_2_0_BASELINE_SCHEMA)
        create_schema(self.database_path, commented)

        self._assert_refused_untouched()

    def test_an_extra_check_is_refused(self) -> None:
        widened = tuple(
            statement.replace(
                "created_at TEXT NOT NULL\n    )",
                "created_at TEXT NOT NULL,\n"
                "        CHECK (length(created_at) > 0)\n    )",
            )
            for statement in V0_2_0_BASELINE_SCHEMA
        )
        self.assertNotEqual(widened, V0_2_0_BASELINE_SCHEMA)
        create_schema(self.database_path, widened)

        self._assert_refused_untouched()

    def test_a_missing_secondary_index_is_refused(self) -> None:
        without_index = tuple(
            statement
            for statement in V0_2_0_BASELINE_SCHEMA
            if "idx_notes_title" not in statement
        )
        create_schema(self.database_path, without_index)

        self._assert_refused_untouched()

    def test_a_wrong_primary_key_shape_is_refused(self) -> None:
        reshaped = tuple(
            statement.replace(
                "id INTEGER PRIMARY KEY AUTOINCREMENT,\n        title TEXT NOT NULL",
                "id INTEGER NOT NULL,\n        title TEXT NOT NULL",
            ).replace(
                "updated_at TEXT NOT NULL\n    )",
                "updated_at TEXT NOT NULL,\n        PRIMARY KEY (id, title)\n    )",
            )
            for statement in V0_2_0_BASELINE_SCHEMA
        )
        self.assertNotEqual(reshaped, V0_2_0_BASELINE_SCHEMA)
        create_schema(self.database_path, reshaped)

        self._assert_refused_untouched()

    def test_a_foreign_database_sharing_table_names_is_refused(self) -> None:
        create_schema(
            self.database_path,
            (
                "CREATE TABLE interactions (id INTEGER PRIMARY KEY, payload BLOB)",
                "CREATE TABLE notes (id INTEGER PRIMARY KEY, payload BLOB)",
                "CREATE TABLE memories (id INTEGER PRIMARY KEY, payload BLOB)",
            ),
        )

        self._assert_refused_untouched()

    def test_an_unrelated_populated_database_is_refused(self) -> None:
        create_schema(
            self.database_path,
            ("CREATE TABLE somebody_elses_data (id INTEGER PRIMARY KEY)",),
        )

        self._assert_refused_untouched()

    def test_a_corrupt_file_is_refused_without_a_schema_diagnosis(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path.write_bytes(b"this is not a SQLite database" * 64)
        before = self.database_path.read_bytes()

        with self.assertRaises(RuntimeError) as raised:
            self.memory.initialize()

        self.assertNotIsInstance(raised.exception, TypedFailureError)
        self.assertEqual(
            str(raised.exception),
            "Unable to initialize Maria database.",
        )
        self.assertIsInstance(raised.exception.__cause__, sqlite3.DatabaseError)
        self.assertEqual(self.database_path.read_bytes(), before)


class NewerSchemaRefusalTests(_TemporaryDatabaseTestCase):
    """Verify a database from a newer build fails closed and stays untouched."""

    def setUp(self) -> None:
        super().setUp()
        create_v0_2_0_baseline(self.database_path)
        stamp_schema_version(self.database_path, DATABASE_SCHEMA_VERSION + 1)

    def test_a_newer_schema_raises_the_typed_failure(self) -> None:
        with self.assertRaises(TypedFailureError) as raised:
            self.memory.initialize()

        self.assertEqual(
            raised.exception.failure,
            Failure(FailureKind.DATABASE_SCHEMA_TOO_NEW),
        )
        self.assertEqual(
            str(raised.exception),
            "This database was written by a newer version of Maria and "
            "cannot be opened by this one.",
        )

    def test_the_refusal_leaves_the_file_byte_for_byte_unchanged(self) -> None:
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        siblings_before = sorted(
            path.name for path in self.database_path.parent.iterdir()
        )

        with self.assertRaises(TypedFailureError):
            self.memory.initialize()

        self.assertEqual(
            hashlib.sha256(self.database_path.read_bytes()).hexdigest(),
            before,
        )
        self.assertEqual(
            sorted(path.name for path in self.database_path.parent.iterdir()),
            siblings_before,
        )
        self.assertEqual(
            read_schema_version(self.database_path),
            DATABASE_SCHEMA_VERSION + 1,
        )

    def test_the_refusal_runs_no_migration_and_creates_no_schema(self) -> None:
        recording_logger = Mock(spec=logging.Logger)
        refusing = Memory(self.database_path, logger=recording_logger)

        with patch.object(
            memory_module,
            "_run_in_one_transaction",
            side_effect=AssertionError("nothing may be written"),
        ):
            with self.assertRaises(TypedFailureError):
                refusing.initialize()

        self.assertEqual(
            [
                call.args[0]
                for call in recording_logger.info.call_args_list
                if "Applied migration" in call.args[0]
            ],
            [],
        )

    def test_the_refusal_carries_no_path_row_content_or_sqlite_text(self) -> None:
        with self.assertRaises(TypedFailureError) as raised:
            self.memory.initialize()

        rendered = f"{raised.exception!r} {raised.exception} "
        rendered += raised.exception.failure.code
        self.assertNotIn(str(self.database_path), rendered)
        self.assertNotIn("sqlite", rendered.lower())
        for _identifier, _request, response, _created_at in BASELINE_INTERACTIONS:
            self.assertNotIn(response, rendered)
        self.assertEqual(raised.exception.args, (raised.exception.failure,))

    def test_a_negative_marker_is_refused_as_unrecognised_not_as_newer(self) -> None:
        stamp_schema_version(self.database_path, -1)
        before = self.database_path.read_bytes()

        with self.assertRaises(RuntimeError) as raised:
            self.memory.initialize()

        self.assertNotIsInstance(raised.exception, TypedFailureError)
        self.assertEqual(self.database_path.read_bytes(), before)


class MigrationRunnerTests(_TemporaryDatabaseTestCase):
    """Verify migrations are ordered, atomic, idempotent, and resumable."""

    def setUp(self) -> None:
        super().setUp()
        create_pre_memories_baseline(self.database_path)

    @staticmethod
    def _probe_ladder(*, include_third: bool = True) -> tuple:
        """Return a three-step ladder whose later steps need the earlier one."""
        steps = [
            memory_module._Migration(
                version=2,
                name="create_memories",
                statements=(memory_module._CREATE_MEMORIES_TABLE,),
            ),
            memory_module._Migration(
                version=3,
                name="index_memories_created_at",
                statements=(DEPENDS_ON_MEMORIES,),
            ),
            memory_module._Migration(
                version=4,
                name="create_probe_table",
                statements=("CREATE TABLE probe (id INTEGER PRIMARY KEY)",),
            ),
        ]
        if not include_third:
            del steps[1]
        return tuple(steps)

    def test_migrations_run_in_ascending_order_to_the_expected_version(self) -> None:
        recording_logger = Mock(spec=logging.Logger)
        migrating = Memory(self.database_path, logger=recording_logger)

        with (
            patch.object(memory_module, "DATABASE_SCHEMA_VERSION", 4),
            patch.object(memory_module, "_MIGRATIONS", self._probe_ladder()),
        ):
            migrating.initialize()

        self.assertEqual(read_schema_version(self.database_path), 4)
        self.assertIn("probe", table_names(self.database_path))
        self.assertEqual(
            [
                call.args[1]
                for call in recording_logger.info.call_args_list
                if "Applied migration" in call.args[0]
            ],
            ["create_memories", "index_memories_created_at", "create_probe_table"],
        )

    def test_a_missing_migration_step_is_refused_before_anything_is_written(
        self,
    ) -> None:
        before = self.database_path.read_bytes()

        with (
            patch.object(memory_module, "DATABASE_SCHEMA_VERSION", 4),
            patch.object(
                memory_module,
                "_MIGRATIONS",
                self._probe_ladder(include_third=False),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to initialize Maria database\.$",
            ):
                self.memory.initialize()

        self.assertEqual(self.database_path.read_bytes(), before)
        self.assertEqual(read_schema_version(self.database_path), UNVERSIONED)

    def test_an_unknown_migration_step_is_refused(self) -> None:
        stray = memory_module._Migration(
            version=DATABASE_SCHEMA_VERSION + 1,
            name="a_step_this_build_cannot_reach",
            statements=("CREATE TABLE stray (id INTEGER PRIMARY KEY)",),
        )

        with patch.object(
            memory_module,
            "_MIGRATIONS",
            memory_module._MIGRATIONS + (stray,),
        ):
            with self.assertRaises(RuntimeError):
                self.memory.initialize()

        self.assertEqual(read_schema_version(self.database_path), UNVERSIONED)

    def test_an_out_of_order_ladder_is_refused(self) -> None:
        with (
            patch.object(memory_module, "DATABASE_SCHEMA_VERSION", 4),
            patch.object(
                memory_module,
                "_MIGRATIONS",
                tuple(reversed(self._probe_ladder())),
            ),
        ):
            with self.assertRaises(RuntimeError):
                self.memory.initialize()

        self.assertEqual(read_schema_version(self.database_path), UNVERSIONED)

    def test_a_failure_mid_migration_rolls_back_schema_and_marker_together(
        self,
    ) -> None:
        broken = (
            memory_module._Migration(
                version=2,
                name="create_memories",
                statements=(
                    memory_module._CREATE_MEMORIES_TABLE,
                    "CREATE TABLE never_committed (INVALID",
                ),
            ),
        )

        with patch.object(memory_module, "_MIGRATIONS", broken):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to initialize Maria database\.$",
            ) as raised:
                self.memory.initialize()

        self.assertIsInstance(raised.exception.__cause__, sqlite3.Error)
        # The baseline stamp committed before the migration was attempted, so
        # the database sits at the previous version with no part of the failed
        # step present.
        self.assertEqual(read_schema_version(self.database_path), PRE_MEMORIES_VERSION)
        self.assertEqual(
            table_names(self.database_path),
            frozenset({"interactions", "notes"}),
        )

    def test_a_retry_after_a_failure_applies_the_step_exactly_once(self) -> None:
        broken = (
            memory_module._Migration(
                version=2,
                name="create_memories",
                statements=(
                    memory_module._CREATE_MEMORIES_TABLE,
                    "CREATE TABLE never_committed (INVALID",
                ),
            ),
        )
        recording_logger = Mock(spec=logging.Logger)
        retrying = Memory(self.database_path, logger=recording_logger)

        with patch.object(memory_module, "_MIGRATIONS", broken):
            with self.assertRaises(RuntimeError):
                retrying.initialize()

        retrying.initialize()
        retrying.initialize()

        self.assertEqual(read_schema_version(self.database_path), EXPECTED_SCHEMA_VERSION)
        self.assertEqual(table_names(self.database_path), BASELINE_TABLES)
        self.assertEqual(
            [
                call.args[1]
                for call in recording_logger.info.call_args_list
                if "Applied migration" in call.args[0]
            ],
            ["create_memories"],
        )
        for identifier, request, response, created_at in BASELINE_INTERACTIONS:
            self.assertEqual(
                retrying.get_interaction(identifier),
                {
                    "id": identifier,
                    "user_input": request,
                    "response": response,
                    "created_at": created_at,
                },
            )

    def test_contention_during_a_migration_is_reported_as_contention(self) -> None:
        """A busy database is retryable, so it must not read as a failure.

        Reporting contention as a failed migration would tell the owner their
        database was unusable when another process merely held it for a
        moment, and would hide the one message that says to try again.
        """
        busy = sqlite3.OperationalError("database is locked")
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        recording_logger = Mock(spec=logging.Logger)
        contended = Memory(self.database_path, logger=recording_logger)
        real_runner = memory_module._run_in_one_transaction
        calls: list[int] = []

        def fail_only_the_migration(connection, statements, version):
            calls.append(version)
            if statements:
                raise busy
            return real_runner(connection, statements, version)

        with patch.object(
            memory_module,
            "_run_in_one_transaction",
            side_effect=fail_only_the_migration,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Database is busy\. Try again shortly\.$",
            ) as raised:
                contended.initialize()

        self.assertIs(raised.exception.__cause__, busy)
        self.assertEqual(calls, [PRE_MEMORIES_VERSION, EXPECTED_SCHEMA_VERSION])
        self.assertEqual(read_schema_version(self.database_path), PRE_MEMORIES_VERSION)
        self.assertEqual(
            table_names(self.database_path),
            frozenset({"interactions", "notes"}),
        )
        recording_logger.exception.assert_not_called()

    def test_a_rejected_schema_version_value_never_reaches_a_statement(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            for rejected in (True, 2.0, "2", -1, None):
                with self.subTest(rejected=rejected):
                    with self.assertRaises(ValueError):
                        memory_module._write_schema_version(connection, rejected)
        finally:
            connection.close()

        self.assertEqual(read_schema_version(self.database_path), UNVERSIONED)


class MigrationLoggingTests(unittest.TestCase):
    """Prove a real migration writes identifiers to the real log and no more.

    The production ``configure_logging()`` installs the real handler and the
    assertions read the file that handler wrote, all beneath a temporary
    directory, so Maria's live log is never opened.
    """

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.root = Path(self._temporary_directory.name)
        self.database_path = self.root / "database" / "maria-test.db"
        self.log_dir = self.root / "logs"
        self.log_file = self.log_dir / "maria.log"

        self.maria_logger = logging.getLogger("maria")
        self.original_handlers = tuple(self.maria_logger.handlers)
        self.original_level = self.maria_logger.level
        self.original_propagate = self.maria_logger.propagate
        for handler in self.original_handlers:
            self.maria_logger.removeHandler(handler)
        self.addCleanup(self._restore_maria_logger)

    def _restore_maria_logger(self) -> None:
        """Close what this test configured and put the previous state back."""
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

    def _flush(self) -> None:
        """Flush every handler so the file reflects what was logged."""
        for handler in self.maria_logger.handlers:
            handler.flush()

    def test_a_real_migration_logs_only_step_and_version_identifiers(self) -> None:
        create_pre_memories_baseline(self.database_path)
        configure_logging(self.log_dir, "DEBUG")

        Memory(self.database_path, logger=get_logger("memory")).initialize()
        self._flush()

        logged = self.log_file.read_text(encoding="utf-8")
        self.assertIn("create_memories", logged)
        self.assertIn("schema version is now 2", logged)

        sentinels = [str(self.database_path), str(self.database_path.parent)]
        for _identifier, request, response, _created_at in BASELINE_INTERACTIONS:
            sentinels.extend((request, response))
        for _identifier, title, body, _created, _updated in BASELINE_NOTES:
            sentinels.extend((title, body))
        for _identifier, fact, normalized, _created_at in BASELINE_MEMORIES:
            sentinels.extend((fact, normalized))
        sentinels.extend(("Traceback", "sqlite3.", "SELECT ", "INSERT INTO"))
        assert_log_omits(self, self.log_file, sentinels)

    def test_a_refused_migration_logs_no_sqlite_text_or_traceback(self) -> None:
        create_pre_memories_baseline(self.database_path)
        configure_logging(self.log_dir, "DEBUG")
        broken = (
            memory_module._Migration(
                version=2,
                name="create_memories",
                statements=(
                    memory_module._CREATE_MEMORIES_TABLE,
                    "CREATE TABLE never_committed (INVALID",
                ),
            ),
        )

        with patch.object(memory_module, "_MIGRATIONS", broken):
            with self.assertRaises(RuntimeError):
                Memory(self.database_path, logger=get_logger("memory")).initialize()
        self._flush()

        logged = self.log_file.read_text(encoding="utf-8")
        self.assertIn("create_memories", logged)
        self.assertIn("rolled back", logged)
        assert_log_omits(
            self,
            self.log_file,
            (
                str(self.database_path),
                "Traceback",
                "OperationalError",
                "never_committed",
                "incomplete input",
            ),
        )

    def test_a_newer_schema_refusal_logs_versions_and_nothing_else(self) -> None:
        create_v0_2_0_baseline(self.database_path)
        stamp_schema_version(self.database_path, DATABASE_SCHEMA_VERSION + 5)
        configure_logging(self.log_dir, "DEBUG")

        with self.assertRaises(TypedFailureError):
            Memory(self.database_path, logger=get_logger("memory")).initialize()
        self._flush()

        logged = self.log_file.read_text(encoding="utf-8")
        self.assertIn(str(DATABASE_SCHEMA_VERSION + 5), logged)
        sentinels = [str(self.database_path), "Traceback"]
        for _identifier, fact, normalized, _created_at in BASELINE_MEMORIES:
            sentinels.extend((fact, normalized))
        assert_log_omits(self, self.log_file, sentinels)


class SqlOwnershipGuardTests(unittest.TestCase):
    """Verify the memory boundary is still the only place SQL lives."""

    @staticmethod
    def _production_modules() -> tuple[Path, ...]:
        """Return every production module except the memory boundary itself."""
        modules = (
            sorted(( PROJECT_ROOT / "core").glob("*.py"))
            + sorted((PROJECT_ROOT / "tools").glob("*.py"))
            + [PROJECT_ROOT / "main.py"]
        )
        return tuple(
            module
            for module in modules
            if module != PROJECT_ROOT / "core" / "memory.py"
        )

    def test_the_scan_actually_sees_the_production_modules(self) -> None:
        scanned = {module.name for module in self._production_modules()}

        self.assertIn("brain.py", scanned)
        self.assertIn("main.py", scanned)
        self.assertIn("notes.py", scanned)
        self.assertNotIn("memory.py", {
            module.name
            for module in self._production_modules()
            if module.parent.name == "core"
        })

    def test_no_production_module_outside_memory_imports_sqlite3(self) -> None:
        for module in self._production_modules():
            with self.subTest(module=module.name):
                source = module.read_text(encoding="utf-8")
                self.assertIsNone(SQLITE_IMPORT.search(source))

    def test_no_production_module_outside_memory_contains_sql(self) -> None:
        for module in self._production_modules():
            with self.subTest(module=module.name):
                source = module.read_text(encoding="utf-8")
                found = [match.group(0) for match in SQL_STATEMENT.finditer(source)]
                self.assertEqual(found, [])

    def test_the_memory_boundary_is_where_the_schema_marker_is_read(self) -> None:
        source = (PROJECT_ROOT / "core" / "memory.py").read_text(encoding="utf-8")

        self.assertIn("PRAGMA user_version", source)
        self.assertTrue(hasattr(Memory, "schema_version"))


class LiveRuntimeGuardTests(unittest.TestCase):
    """Verify no test can reach Maria's live database."""

    @staticmethod
    def _test_modules() -> tuple[Path, ...]:
        """Return every module in the test package."""
        return tuple(sorted(TESTS_DIR.glob("*.py")))

    def test_the_scan_actually_sees_the_test_modules(self) -> None:
        scanned = {module.name for module in self._test_modules()}

        self.assertIn("test_memory.py", scanned)
        self.assertIn("test_schema_versioning.py", scanned)
        self.assertIn("schema_fixtures.py", scanned)

    def test_no_test_opens_a_database_at_the_live_constants(self) -> None:
        opener = re.compile(
            r"(?:sqlite3\.connect|Memory)\(\s*([^)]*?)[,)]",
            re.DOTALL,
        )
        for module in self._test_modules():
            source = module.read_text(encoding="utf-8")
            for match in opener.finditer(source):
                with self.subTest(module=module.name, opened=match.group(1)[:40]):
                    self.assertNotRegex(
                        match.group(1),
                        r"\bDATABASE_PATH\b|\bDATABASE_DIR\b",
                    )

    def test_the_fixture_refuses_maria_live_database_directory(self) -> None:
        from core.constants import DATABASE_DIR, DATABASE_PATH

        for refused in (
            DATABASE_PATH,
            DATABASE_DIR,
            DATABASE_DIR / "nested" / "copy.db",
            DATABASE_DIR / ".." / "database" / "maria.db",
        ):
            with self.subTest(refused=refused.name):
                with self.assertRaisesRegex(
                    ValueError,
                    r"never Maria's live database directory\.$",
                ):
                    create_v0_2_0_baseline(refused)
                with self.assertRaises(ValueError):
                    read_schema_version(refused)
                with self.assertRaises(ValueError):
                    stamp_schema_version(refused, 1)
                with self.assertRaises(ValueError):
                    table_names(refused)


class FixtureIndependenceTests(unittest.TestCase):
    """Verify the frozen fixture never asks the live code what it should be."""

    def test_the_fixture_imports_no_part_of_the_memory_layer(self) -> None:
        source = inspect.getsource(schema_fixtures)

        self.assertNotRegex(source, r"(?m)^\s*(?:import|from)\s+core\.memory\b")
        self.assertNotRegex(source, r"\bMemory\(")
        self.assertNotRegex(source, r"\binitialize\(")
        self.assertNotRegex(source, r"\bDATABASE_SCHEMA_VERSION\b")

    def test_the_fixture_declares_both_historical_shapes_in_full(self) -> None:
        self.assertEqual(len(V0_2_0_BASELINE_SCHEMA), 5)
        self.assertEqual(len(PRE_MEMORIES_SCHEMA), 4)
        self.assertTrue(
            any("memories" in statement for statement in V0_2_0_BASELINE_SCHEMA)
        )
        self.assertFalse(
            any(
                "CREATE TABLE memories" in statement
                for statement in PRE_MEMORIES_SCHEMA
            )
        )

    def test_the_fixture_and_the_frozen_production_history_agree(self) -> None:
        declared = dict(memory_module._HISTORICAL_UNVERSIONED_SCHEMAS)

        self.assertEqual(
            memory_module._reference_schema(V0_2_0_BASELINE_SCHEMA),
            memory_module._reference_schema(declared[EXPECTED_SCHEMA_VERSION]),
        )
        self.assertEqual(
            memory_module._reference_schema(PRE_MEMORIES_SCHEMA),
            memory_module._reference_schema(declared[PRE_MEMORIES_VERSION]),
        )

    def test_the_frozen_baseline_still_describes_the_current_schema(self) -> None:
        # True only while DATABASE_SCHEMA_VERSION is the baseline version. A
        # later stage that advances the schema replaces this with its own
        # migration test rather than editing the frozen fixture.
        self.assertEqual(DATABASE_SCHEMA_VERSION, EXPECTED_SCHEMA_VERSION)
        self.assertEqual(
            memory_module._reference_schema(V0_2_0_BASELINE_SCHEMA),
            memory_module._reference_schema(memory_module._CURRENT_SCHEMA_STATEMENTS),
        )


class CheckConstraintTests(_TemporaryDatabaseTestCase):
    """Verify the required CHECK constraints are part of being the baseline.

    No pragma reports a CHECK, so a validator built on pragmas alone would
    stamp a ``memories`` table that accepts the empty facts the real baseline
    refuses. These prove the constraints are verified, and verified on what
    they say rather than on how they were typed.
    """

    @staticmethod
    def _memories_shape(description: tuple) -> tuple:
        """Return the ``memories`` entry of a schema description."""
        return next(table for table in description if table[0] == "memories")

    def test_the_frozen_baseline_declares_exactly_the_required_checks(self) -> None:
        reference = memory_module._reference_schema(V0_2_0_BASELINE_SCHEMA)

        self.assertEqual(
            self._memories_shape(reference)[5],
            REQUIRED_MEMORY_CHECKS,
        )
        for table in reference:
            if table[0] != "memories":
                with self.subTest(table=table[0]):
                    self.assertEqual(table[5], ())

    def test_the_genuine_frozen_fixture_is_accepted(self) -> None:
        create_v0_2_0_baseline(self.database_path)

        self.memory.initialize()

        self.assertEqual(read_schema_version(self.database_path), EXPECTED_SCHEMA_VERSION)
        self.assertEqual(
            self._memories_shape(self.describe())[5],
            REQUIRED_MEMORY_CHECKS,
        )

    def test_formatting_equivalent_checks_are_accepted(self) -> None:
        """The same rules typed differently are the same rules."""
        create_schema(
            self.database_path,
            FORMATTING_EQUIVALENT_BASELINE,
            with_rows=True,
            with_memories=True,
        )
        self.assertNotEqual(
            FORMATTING_EQUIVALENT_BASELINE,
            V0_2_0_BASELINE_SCHEMA,
        )

        self.memory.initialize()

        self.assertEqual(read_schema_version(self.database_path), EXPECTED_SCHEMA_VERSION)
        self.assertEqual(
            self._memories_shape(self.describe())[5],
            REQUIRED_MEMORY_CHECKS,
        )
        for identifier, fact, _normalized, created_at in BASELINE_MEMORIES:
            self.assertEqual(
                self.memory.get_memory(identifier),
                {"id": identifier, "fact": fact, "created_at": created_at},
            )

    def test_the_stamped_baseline_still_refuses_an_empty_fact(self) -> None:
        """The point of verifying the checks: they have to still be enforced."""
        create_schema(
            self.database_path,
            FORMATTING_EQUIVALENT_BASELINE,
            with_rows=True,
            with_memories=True,
        )
        self.memory.initialize()

        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO memories (fact, normalized_fact, created_at)
                    VALUES (?, ?, ?)
                    """,
                    ("", "", "2026-02-01T00:00:00+00:00"),
                )
        finally:
            connection.close()

    def test_equivalent_spellings_normalize_to_one_expression(self) -> None:
        equivalent = (
            "CHECK (length(fact) > 0)",
            "check(length(fact)>0)",
            "CHECK ( LENGTH ( fact ) > 0 )",
            'CHECK ((length("fact") > 0))',
            "CHECK (\n    length([fact])\n    >\n    0\n)",
            "CHECK (length(`fact`) > 0)",
        )

        for spelling in equivalent:
            with self.subTest(spelling=spelling):
                tokens = memory_module._tokenize_definition(
                    f"CREATE TABLE t (fact TEXT {spelling})"
                )
                self.assertEqual(
                    memory_module._check_constraints(tokens),
                    ("length(fact)>0",),
                )

    def test_different_conditions_stay_different(self) -> None:
        different = (
            "CHECK (length(fact) >= 0)",
            "CHECK (length(fact) > 1)",
            "CHECK (length(other) > 0)",
            "CHECK (length(fact) <> 0)",
        )

        for spelling in different:
            with self.subTest(spelling=spelling):
                tokens = memory_module._tokenize_definition(
                    f"CREATE TABLE t (fact TEXT, other TEXT, {spelling})"
                )
                self.assertNotEqual(
                    memory_module._check_constraints(tokens),
                    ("length(fact)>0",),
                )

    def test_a_check_inside_a_literal_or_comment_is_not_a_check(self) -> None:
        disguises = (
            "CREATE TABLE t (fact TEXT DEFAULT 'CHECK (length(fact) > 0)')",
            "CREATE TABLE t (fact TEXT) -- CHECK (length(fact) > 0)",
            "CREATE TABLE t (/* CHECK (length(fact) > 0) */ fact TEXT)",
            "CREATE TABLE t (checksum TEXT)",
        )

        for disguise in disguises:
            with self.subTest(disguise=disguise[:48]):
                tokens = memory_module._tokenize_definition(disguise)
                self.assertEqual(memory_module._check_constraints(tokens), ())

    def test_the_classifier_never_compares_whole_stored_statements(self) -> None:
        """Equality on stored SQL is what the token form exists to avoid."""
        source = inspect.getsource(memory_module._describe_table)

        self.assertNotIn("sql ==", source)
        self.assertNotIn(".strip()", source)
        self.assertIn("_tokenize_definition", source)


class ForeignDatabaseTests(_TemporaryDatabaseTestCase):
    """Verify emptiness is decided on every object, not on Maria's names.

    A file that belongs to another application must never be treated as a
    blank one to build Maria's schema inside, and the only way to be sure of
    that is to count what is actually in it rather than to look for names
    Maria would recognise.
    """

    def _assert_untouched_and_refused(self, expected_objects: set[str]) -> None:
        """Assert a refusal that wrote no table, no marker, and no byte."""
        before = self.database_path.read_bytes()

        with self.assertRaisesRegex(
            RuntimeError,
            r"^Unable to initialize Maria database\.$",
        ):
            self.memory.initialize()

        self.assertEqual(self.database_path.read_bytes(), before)
        self.assertEqual(read_schema_version(self.database_path), UNVERSIONED)
        self.assertEqual(table_names(self.database_path) & BASELINE_TABLES, frozenset())
        self.assertEqual(set(_object_names(self.database_path)), expected_objects)

    def test_a_file_with_no_user_defined_object_becomes_a_new_database(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(self.database_path).close()
        self.assertEqual(_object_names(self.database_path), ())

        self.memory.initialize()

        self.assertEqual(read_schema_version(self.database_path), EXPECTED_SCHEMA_VERSION)
        self.assertEqual(table_names(self.database_path), BASELINE_TABLES)

    def test_a_file_holding_only_sqlite_internals_is_still_empty(self) -> None:
        """``sqlite_sequence`` outlives the table that created it."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "CREATE TABLE gone (id INTEGER PRIMARY KEY AUTOINCREMENT)"
            )
            connection.execute("INSERT INTO gone DEFAULT VALUES")
            connection.execute("DROP TABLE gone")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(_object_names(self.database_path), ())
        self.assertIn("sqlite_sequence", _all_object_names(self.database_path))

        self.memory.initialize()

        self.assertEqual(read_schema_version(self.database_path), EXPECTED_SCHEMA_VERSION)
        self.assertEqual(table_names(self.database_path), BASELINE_TABLES)

    def test_one_unrelated_table_makes_the_file_somebody_elses(self) -> None:
        create_schema(
            self.database_path,
            ("CREATE TABLE payroll (id INTEGER PRIMARY KEY, amount TEXT)",),
        )

        self._assert_untouched_and_refused({"payroll"})

    def test_several_unrelated_tables_are_refused(self) -> None:
        create_schema(
            self.database_path,
            (
                "CREATE TABLE payroll (id INTEGER PRIMARY KEY, amount TEXT)",
                "CREATE TABLE invoices (id INTEGER PRIMARY KEY, total TEXT)",
                "CREATE INDEX idx_invoices_total ON invoices (total)",
            ),
        )

        self._assert_untouched_and_refused(
            {"payroll", "invoices", "idx_invoices_total"}
        )

    def test_a_file_holding_only_a_view_is_not_empty(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("CREATE TABLE seed (id INTEGER)")
            connection.execute("CREATE VIEW seen AS SELECT id FROM seed")
            connection.execute("DROP TABLE seed")
            connection.commit()
        finally:
            connection.close()

        self._assert_untouched_and_refused({"seen"})

    def test_a_foreign_table_beside_the_baseline_is_refused(self) -> None:
        create_schema(
            self.database_path,
            V0_2_0_BASELINE_SCHEMA
            + ("CREATE TABLE payroll (id INTEGER PRIMARY KEY, amount TEXT)",),
            with_rows=True,
            with_memories=True,
        )
        before = self.database_path.read_bytes()

        with self.assertRaisesRegex(
            RuntimeError,
            r"^Unable to initialize Maria database\.$",
        ):
            self.memory.initialize()

        self.assertEqual(self.database_path.read_bytes(), before)
        self.assertEqual(read_schema_version(self.database_path), UNVERSIONED)


class InitializationLogPrivacyTests(unittest.TestCase):
    """Prove a failed initialization tells the log nothing about the failure.

    The real ``configure_logging()`` installs the real handler and these
    assertions read the file it wrote, all beneath a temporary directory.
    Maria's live log is never opened.
    """

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.root = Path(self._temporary_directory.name)
        self.database_path = self.root / "database" / "maria-test.db"
        self.log_dir = self.root / "logs"
        self.log_file = self.log_dir / "maria.log"

        self.maria_logger = logging.getLogger("maria")
        self.original_handlers = tuple(self.maria_logger.handlers)
        self.original_level = self.maria_logger.level
        self.original_propagate = self.maria_logger.propagate
        for handler in self.original_handlers:
            self.maria_logger.removeHandler(handler)
        self.addCleanup(self._restore_maria_logger)

    def _restore_maria_logger(self) -> None:
        """Close what this test configured and put the previous state back."""
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

    def _flush(self) -> None:
        """Flush every handler so the file reflects what was logged."""
        for handler in self.maria_logger.handlers:
            handler.flush()

    def _forbidden_values(self) -> tuple[str, ...]:
        """Return every value a failed initialization must not repeat."""
        return (
            SQLITE_DETAIL_SENTINEL,
            str(self.database_path),
            str(self.database_path.parent),
            "Traceback",
            "DatabaseError",
            "OperationalError",
            "sqlite3",
            "PRAGMA",
            "user_version",
        )

    def test_a_sqlite_failure_reaches_neither_the_response_nor_the_log(
        self,
    ) -> None:
        create_v0_2_0_baseline(self.database_path)
        configure_logging(self.log_dir, "DEBUG")
        failure = sqlite3.DatabaseError(SQLITE_DETAIL_SENTINEL)
        memory = Memory(self.database_path, logger=get_logger("memory"))

        with patch.object(
            memory_module,
            "_read_schema_version",
            side_effect=failure,
        ):
            with self.assertRaises(RuntimeError) as raised:
                memory.initialize()
        self._flush()

        self.assertEqual(
            str(raised.exception),
            "Unable to initialize Maria database.",
        )
        self.assertIs(raised.exception.__cause__, failure)
        self.assertNotIn(SQLITE_DETAIL_SENTINEL, str(raised.exception))
        self.assertNotIn(str(self.database_path), str(raised.exception))

        logged = self.log_file.read_text(encoding="utf-8")
        self.assertIn("Database initialization stopped: database_unreadable.", logged)
        assert_log_omits(self, self.log_file, self._forbidden_values())

    def test_a_failure_while_opening_the_connection_is_equally_silent(
        self,
    ) -> None:
        configure_logging(self.log_dir, "DEBUG")
        failure = sqlite3.OperationalError(SQLITE_DETAIL_SENTINEL)
        memory = Memory(self.database_path, logger=get_logger("memory"))

        with patch.object(memory, "_connect", side_effect=failure):
            with self.assertRaises(RuntimeError) as raised:
                memory.initialize()
        self._flush()

        self.assertEqual(
            str(raised.exception),
            "Unable to initialize Maria database.",
        )
        self.assertIs(raised.exception.__cause__, failure)
        assert_log_omits(self, self.log_file, self._forbidden_values())

    def test_a_real_corrupt_file_is_refused_without_describing_itself(
        self,
    ) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path.write_bytes(
            f"not a database {SQLITE_DETAIL_SENTINEL} ".encode("utf-8") * 64
        )
        before = self.database_path.read_bytes()
        configure_logging(self.log_dir, "DEBUG")

        with self.assertRaises(RuntimeError) as raised:
            Memory(self.database_path, logger=get_logger("memory")).initialize()
        self._flush()

        self.assertNotIsInstance(raised.exception, TypedFailureError)
        self.assertEqual(
            str(raised.exception),
            "Unable to initialize Maria database.",
        )
        self.assertEqual(self.database_path.read_bytes(), before)
        assert_log_omits(
            self,
            self.log_file,
            self._forbidden_values() + ("file is not a database",),
        )

    def test_contention_is_recorded_as_a_code_rather_than_an_exception(
        self,
    ) -> None:
        create_v0_2_0_baseline(self.database_path)
        configure_logging(self.log_dir, "DEBUG")
        busy = sqlite3.OperationalError(SQLITE_DETAIL_SENTINEL)
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        memory = Memory(self.database_path, logger=get_logger("memory"))

        with patch.object(
            memory_module,
            "_read_schema_version",
            side_effect=busy,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Database is busy\. Try again shortly\.$",
            ):
                memory.initialize()
        self._flush()

        logged = self.log_file.read_text(encoding="utf-8")
        self.assertIn("Database initialization stopped: database_busy.", logged)
        assert_log_omits(self, self.log_file, self._forbidden_values())

    def test_reading_the_schema_version_is_equally_silent(self) -> None:
        create_v0_2_0_baseline(self.database_path)
        configure_logging(self.log_dir, "DEBUG")
        failure = sqlite3.DatabaseError(SQLITE_DETAIL_SENTINEL)
        memory = Memory(self.database_path, logger=get_logger("memory"))

        with patch.object(memory, "_connect", side_effect=failure):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Unable to read database schema version\.$",
            ):
                memory.schema_version()
        self._flush()

        assert_log_omits(self, self.log_file, self._forbidden_values())

    def test_the_initialization_boundary_hands_the_logger_nothing_private(
        self,
    ) -> None:
        """A source guard, because a reintroduced leak would itself be silent.

        Each logging call is read as a call rather than as text, so the check
        is on what is actually handed to the logger. ``initialize`` legitimately
        uses the database path to create its parent directory; what it may not
        do is pass that path, or anything derived from the failure, to a log
        record.
        """
        inspected = 0

        for name in (
            "initialize",
            "schema_version",
            "_settle_schema_version",
            "_classify_unversioned_database",
            "_apply_pending_migrations",
        ):
            source = textwrap.dedent(inspect.getsource(getattr(Memory, name)))
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if not isinstance(function, ast.Attribute):
                    continue
                if not isinstance(function.value, ast.Attribute):
                    continue
                if function.value.attr != "logger":
                    continue

                inspected += 1
                rendered = " ".join(
                    [ast.unparse(argument) for argument in node.args]
                    + [
                        f"{keyword.arg}={ast.unparse(keyword.value)}"
                        for keyword in node.keywords
                    ]
                )
                with self.subTest(method=name, call=function.attr):
                    self.assertNotEqual(function.attr, "exception")
                    self.assertNotIn("exc_info", rendered)
                    self.assertNotIn("database_path", rendered)
                    self.assertNotRegex(rendered, r"\bexc\b")
                    self.assertNotRegex(rendered, r"\bstr\(|\brepr\(")

        # The walk has to have found something, or it would pass by reaching
        # nothing at all.
        self.assertGreaterEqual(inspected, 6)


def _object_names(database_path: Path) -> tuple[str, ...]:
    """Return a database's user-defined object names, in name order."""
    connection = sqlite3.connect(database_path)
    try:
        return tuple(
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite~_%' ESCAPE '~'
                ORDER BY name
                """
            )
        )
    finally:
        connection.close()


def _all_object_names(database_path: Path) -> tuple[str, ...]:
    """Return every object name a database holds, SQLite's own included."""
    connection = sqlite3.connect(database_path)
    try:
        return tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master ORDER BY name"
            )
        )
    finally:
        connection.close()


def _without_check(column_pattern: str) -> tuple[str, ...]:
    """Return the baseline schema with one CHECK clause removed."""
    removed = tuple(
        re.sub(
            r"\s*CHECK \(" + column_pattern + r" > 0\)",
            "",
            statement,
        )
        for statement in V0_2_0_BASELINE_SCHEMA
    )
    if removed == V0_2_0_BASELINE_SCHEMA:
        raise ValueError("The CHECK clause to remove was not found.")
    return removed


if __name__ == "__main__":
    unittest.main()
