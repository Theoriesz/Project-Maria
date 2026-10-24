"""Tests for project storage in the memory layer (V1-24-R1).

Every test runs on a temporary database. The record rules, the activation
setting's four states and its serial, and the deletion rules run on a new
UTF-8 database; the SQL shape checks, the bounded reads, and the fact
context reads run on new and migrated databases in UTF-8, UTF-16le, and
UTF-16be, where each holds at its exact boundary and refuses no valid value.
A row written from outside Maria is refused privately, never repaired, and a
fact whose text or project no longer matches its approval digest is never
returned or counted.
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
from core.memory import Memory
from core.project_records import (
    ACTIVATION_SERIAL_MAX,
    ActivationChange,
    ActivationState,
    DeactivationChange,
    FactEdit,
    Project,
    ProjectChoices,
    ProjectContextRead,
    ProjectDeletion,
    ProjectFact,
    fact_digest,
)
from tests.test_conversation_summaries import (
    _database_encoding,
    _start_baseline_database,
    _start_empty_database,
)


STAMP = "2026-10-09T21:24:13Z"
STORAGE = "Unable to read or write projects."
BUSY = "Database is busy. Try again shortly."


class _ProjectCase(unittest.TestCase):
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
        self.assertEqual(self.memory.schema_version(), 9)

    def raw(self, statement: str, parameters: tuple[object, ...] = ()) -> int:
        """Run one statement on a second, raw connection; return the row id."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            cursor = connection.execute(statement, parameters)
            connection.commit()
            return cursor.lastrowid

    def rows(self, statement: str) -> list[tuple[object, ...]]:
        """Read rows on a second, raw connection."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            return [tuple(row) for row in connection.execute(statement)]

    def activation_rows(self) -> list[tuple[object, ...]]:
        """Return the activation table's rows, without their stamps."""
        return self.rows(
            "SELECT id, project_id, serial FROM project_activation ORDER BY id"
        )

    def insert_project(
        self,
        name: object,
        root: object = "workspace",
        folder: object = "",
    ) -> int:
        """Insert one project row from outside Maria."""
        return self.raw(
            "INSERT INTO projects (name, root, folder, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, root, folder, STAMP, STAMP),
        )

    def insert_fact(
        self,
        project_id: int,
        text: object,
        digest: object | None = None,
    ) -> int:
        """Insert one fact row from outside Maria."""
        if digest is None:
            digest = fact_digest(project_id, text)
        return self.raw(
            "INSERT INTO project_facts "
            "(project_id, text, approved_digest, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, text, digest, STAMP, STAMP),
        )


class ProjectRecordTests(_ProjectCase):
    """Projects: create, list, read, rename, and delete by their rules."""

    def test_a_project_is_stored_listed_and_read_whole(self) -> None:
        project_id = self.memory.create_project("Garden", "workspace", "plots/north")

        listing = self.memory.list_projects()
        reading = self.memory.get_project(project_id)

        self.assertEqual(project_id, 1)
        self.assertEqual([project.project_id for project in listing.projects], [1])
        self.assertIs(listing.activation.state, ActivationState.NEVER_ACTIVATED)
        self.assertIs(type(reading.project), Project)
        self.assertEqual(reading.project.location, "workspace/plots/north")
        self.assertEqual(reading.fact_count, 0)
        self.assertIsNone(self.memory.get_project(2).project)

    def test_a_location_equal_inside_or_around_another_is_refused(self) -> None:
        self.memory.create_project("A", "workspace", "plots/north")

        for root, folder in (
            ("workspace", "plots/north"),
            ("workspace", "PLOTS/North"),
            ("workspace", "plots/north/east"),
            ("workspace", "plots"),
            ("workspace", ""),
        ):
            with self.subTest(folder=folder):
                self.assertIsNone(self.memory.create_project("B", root, folder))
        self.assertEqual(
            self.memory.create_project("C", "workspace", "plots/northern"),
            2,
        )
        self.assertEqual(self.memory.create_project("Maria", "project_maria", ""), 3)
        self.assertIsNone(self.memory.create_project("Again", "project_maria", ""))
        self.assertTrue(
            any(
                call.args == ("Refused a project whose location overlaps another.",)
                for call in self.logger.info.call_args_list
            )
        )

    def test_a_rename_changes_the_name_only_and_ids_are_never_reused(self) -> None:
        first = self.memory.create_project("Old", "workspace", "a")

        self.assertTrue(self.memory.retitle_project(first, "New"))
        self.assertFalse(self.memory.retitle_project(99, "New"))
        renamed = self.memory.get_project(first).project
        self.assertEqual((renamed.name, renamed.location), ("New", "workspace/a"))
        self.assertGreaterEqual(renamed.updated_at, renamed.created_at)
        self.assertIs(self.memory.delete_project(first), ProjectDeletion.DELETED)
        self.assertEqual(self.memory.create_project("Next", "workspace", "a"), 2)

    def test_an_active_project_or_one_with_facts_is_never_deleted(self) -> None:
        project_id = self.memory.create_project("A", "workspace", "a")
        fact_id = self.memory.add_project_fact(project_id, "A fact.")

        self.assertIs(self.memory.delete_project(project_id), ProjectDeletion.HAS_FACTS)
        self.memory.delete_project_fact(fact_id)
        self.memory.activate_project(project_id)
        self.assertIs(self.memory.delete_project(project_id), ProjectDeletion.ACTIVE)
        self.memory.deactivate_project()
        self.assertIs(self.memory.delete_project(project_id), ProjectDeletion.DELETED)
        self.assertIs(self.memory.delete_project(project_id), ProjectDeletion.NOT_FOUND)

    def test_values_maria_could_not_write_are_refused_before_any_sql(self) -> None:
        for arguments in (
            ("A: B", "workspace", "a"),
            ("", "workspace", "a"),
            ("a" * 65, "workspace", "a"),
            ("A", "project_maria", "Maria"),
            ("A", "workspace", "../a"),
            ("A", "other", ""),
            ("A", "workspace", "é" * 257),
        ):
            with self.subTest(arguments=arguments[0][:8]):
                with self.assertRaises(ValueError):
                    self.memory.create_project(*arguments)
        self.assertEqual(self.rows("SELECT count(*) FROM projects"), [(0,)])

    def test_listing_reads_one_more_than_it_shows(self) -> None:
        for index in range(52):
            self.memory.create_project(f"P{index}", "workspace", f"p{index}")

        self.assertEqual(len(self.memory.list_projects().projects), 51)
        choices = self.memory.project_choices()
        self.assertIs(type(choices), ProjectChoices)
        self.assertEqual(len(choices.projects), 50)
        self.assertEqual(choices.projects[0], (1, "P0"))
        self.assertIsNone(choices.active_id)
        self.memory.activate_project(3)
        self.assertEqual(self.memory.project_choices().active_id, 3)


class FactTests(_ProjectCase):
    """Facts: added with their digest, edited only as shown, deleted by id."""

    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.memory.create_project("Garden", "workspace", "g")

    def test_a_fact_is_stored_with_its_approval_digest(self) -> None:
        fact_id = self.memory.add_project_fact(self.project_id, "Water at dawn.")

        fact = self.memory.get_project_fact(fact_id)
        self.assertIs(type(fact), ProjectFact)
        self.assertEqual(
            fact.approved_digest,
            fact_digest(self.project_id, "Water at dawn."),
        )
        self.assertEqual(
            self.rows("SELECT approved_digest FROM project_facts"),
            [(fact_digest(self.project_id, "Water at dawn."),)],
        )
        self.assertIsNone(self.memory.add_project_fact(99, "No project."))
        self.assertEqual(self.memory.get_project(self.project_id).fact_count, 1)

    def test_an_edit_applies_only_while_the_fact_is_as_it_was_shown(self) -> None:
        fact_id = self.memory.add_project_fact(self.project_id, "Old text.")
        shown = self.memory.get_project_fact(fact_id).approved_digest

        self.assertIs(
            self.memory.edit_project_fact(fact_id, "New text.", None),
            FactEdit.CHANGED,
        )
        self.assertIs(
            self.memory.edit_project_fact(fact_id, "New text.", "0" * 64),
            FactEdit.CHANGED,
        )
        self.assertIs(
            self.memory.edit_project_fact(fact_id, "New text.", shown),
            FactEdit.EDITED,
        )
        self.assertIs(
            self.memory.edit_project_fact(fact_id, "Newer text.", shown),
            FactEdit.CHANGED,
        )
        self.assertEqual(self.memory.get_project_fact(fact_id).text, "New text.")
        self.assertIs(
            self.memory.edit_project_fact(99, "x", shown),
            FactEdit.NOT_FOUND,
        )

    def test_a_fact_is_deleted_by_id_and_listed_by_project(self) -> None:
        first = self.memory.add_project_fact(self.project_id, "One.")
        second = self.memory.add_project_fact(self.project_id, "Two.")
        other = self.memory.create_project("Other", "workspace", "o")
        self.memory.add_project_fact(other, "Elsewhere.")

        listing = self.memory.list_project_facts(self.project_id)
        self.assertEqual([fact.fact_id for fact in listing.facts], [first, second])
        self.assertTrue(self.memory.delete_project_fact(first))
        self.assertFalse(self.memory.delete_project_fact(first))
        self.assertIsNone(self.memory.list_project_facts(99).project)

    def test_an_injected_write_failure_changes_neither_text_nor_digest(
        self,
    ) -> None:
        fact_id = self.memory.add_project_fact(self.project_id, "Kept text.")
        shown = self.memory.get_project_fact(fact_id).approved_digest
        before = self.rows("SELECT id, text, approved_digest FROM project_facts")
        for event in ("UPDATE", "INSERT"):
            self.raw(
                f"CREATE TRIGGER refuse_fact_{event.lower()} BEFORE {event} ON "
                "project_facts BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )

        for label, call in (
            ("edit", lambda: self.memory.edit_project_fact(fact_id, "New.", shown)),
            ("add", lambda: self.memory.add_project_fact(self.project_id, "Add.")),
        ):
            with self.subTest(call=label):
                with self.assertRaises(RuntimeError) as raised:
                    call()
                self.assertEqual(str(raised.exception), STORAGE)
        self.assertEqual(
            self.rows("SELECT id, text, approved_digest FROM project_facts"),
            before,
        )

    def test_fact_text_maria_could_not_write_is_refused_before_any_sql(self) -> None:
        for text in ("", " x", "x\ny", "x" * 513, "a\u2028b"):
            with self.subTest(text=ascii(text)[:12]):
                with self.assertRaises(ValueError):
                    self.memory.add_project_fact(self.project_id, text)


class ActivationTests(_ProjectCase):
    """The activation setting: four states, one serial, two owner commands."""

    def setUp(self) -> None:
        super().setUp()
        self.first = self.memory.create_project("First", "workspace", "f")
        self.second = self.memory.create_project("Second", "workspace", "s")

    def test_a_new_database_was_never_activated_and_deactivating_writes_nothing(
        self,
    ) -> None:
        self.assertIs(
            self.memory.project_activation().state,
            ActivationState.NEVER_ACTIVATED,
        )
        self.assertEqual(self.memory.project_activation().token, (None, 0))
        self.assertIs(self.memory.deactivate_project(), DeactivationChange.NONE_ACTIVE)
        self.assertEqual(self.activation_rows(), [])

    def test_each_change_raises_the_serial_by_exactly_one(self) -> None:
        self.assertIs(
            self.memory.activate_project(self.first).change,
            ActivationChange.ACTIVATED,
        )
        self.assertEqual(self.activation_rows(), [(1, self.first, 1)])
        self.assertIs(
            self.memory.activate_project(self.first).change,
            ActivationChange.ALREADY_ACTIVE,
        )
        self.assertEqual(self.activation_rows(), [(1, self.first, 1)])
        switched = self.memory.activate_project(self.second)
        self.assertEqual(switched.replaced_project_id, self.first)
        self.assertEqual(self.activation_rows(), [(1, self.second, 2)])
        self.assertIs(self.memory.deactivate_project(), DeactivationChange.DEACTIVATED)
        self.assertEqual(self.activation_rows(), [(1, None, 3)])
        self.assertIs(self.memory.deactivate_project(), DeactivationChange.NONE_ACTIVE)
        self.assertEqual(self.activation_rows(), [(1, None, 3)])
        self.assertIs(
            self.memory.project_activation().state,
            ActivationState.INACTIVE,
        )
        self.assertIs(
            self.memory.activate_project(99).change,
            ActivationChange.NOT_FOUND,
        )
        self.assertEqual(self.activation_rows(), [(1, None, 3)])

    def test_a_setting_naming_no_project_is_unreadable_and_replaced_only_by_owners(
        self,
    ) -> None:
        self.raw(
            "INSERT INTO project_activation (id, project_id, serial, changed_at) "
            "VALUES (1, 99, 5, ?)",
            (STAMP,),
        )

        snapshot = self.memory.project_activation()
        self.assertIs(snapshot.state, ActivationState.UNREADABLE)
        self.assertIsNone(snapshot.token)
        self.assertIsNone(snapshot.active_project_id)
        self.assertIs(
            self.memory.list_projects().activation.state,
            ActivationState.UNREADABLE,
        )
        self.assertEqual(self.activation_rows(), [(1, 99, 5)])
        self.memory.activate_project(self.first)
        self.assertEqual(self.activation_rows(), [(1, self.first, 6)])

    def test_a_deactivation_replaces_an_unreadable_setting_with_an_inactive_one(
        self,
    ) -> None:
        self.raw(
            "INSERT INTO project_activation (id, project_id, serial, changed_at) "
            "VALUES (1, NULL, 7, '2026-13-45T25:61:61Z')"
        )

        self.assertIs(
            self.memory.project_activation().state,
            ActivationState.UNREADABLE,
        )
        self.assertIs(self.memory.deactivate_project(), DeactivationChange.DEACTIVATED)
        self.assertEqual(self.activation_rows(), [(1, None, 8)])
        self.assertIs(self.memory.project_activation().state, ActivationState.INACTIVE)

    def test_a_project_whose_own_row_is_refused_makes_the_setting_unreadable(
        self,
    ) -> None:
        broken = self.insert_project("bad: name")
        self.raw(
            "INSERT INTO project_activation (id, project_id, serial, changed_at) "
            "VALUES (1, ?, 2, ?)",
            (broken, STAMP),
        )

        self.assertIs(
            self.memory.project_activation().state,
            ActivationState.UNREADABLE,
        )

    def test_at_the_largest_serial_activation_is_refused_and_deactivation_works(
        self,
    ) -> None:
        self.memory.activate_project(self.first)
        self.raw("UPDATE project_activation SET serial = ?", (ACTIVATION_SERIAL_MAX,))

        refused = self.memory.activate_project(self.second)
        self.assertIs(refused.change, ActivationChange.AT_LIMIT)
        self.assertEqual(
            self.activation_rows(),
            [(1, self.first, ACTIVATION_SERIAL_MAX)],
        )
        self.assertIs(self.memory.deactivate_project(), DeactivationChange.DEACTIVATED)
        self.assertEqual(self.activation_rows(), [(1, None, ACTIVATION_SERIAL_MAX)])
        self.assertIs(
            self.memory.activate_project(self.first).change,
            ActivationChange.AT_LIMIT,
        )
        self.assertEqual(self.activation_rows(), [(1, None, ACTIVATION_SERIAL_MAX)])
        # An unreadable setting at the largest serial: deactivation keeps it,
        # and activation is still refused.
        self.raw("UPDATE project_activation SET changed_at = '2026-02-30T00:00:00Z'")
        self.assertIs(
            self.memory.activate_project(self.first).change,
            ActivationChange.AT_LIMIT,
        )
        self.assertIs(self.memory.deactivate_project(), DeactivationChange.DEACTIVATED)
        self.assertEqual(self.activation_rows(), [(1, None, ACTIVATION_SERIAL_MAX)])

    def test_each_unreadable_kind_is_reported_and_replaced_by_its_rule(
        self,
    ) -> None:
        stamp = f"'{STAMP}'"
        # Each kind is written with SQLite's checks switched off for that one
        # connection, as a write from outside Maria could be; the serial the
        # owner's command then writes follows the stated rule. A setting naming
        # a refused project row is tested on its own above, since that row
        # also refuses every listing.
        for label, rows, serial in (
            ("two rows", ((1, "NULL", "3", stamp), (2, "NULL", "4", stamp)), 1),
            ("a zero serial", ((1, "NULL", "0", stamp),), 1),
            ("a negative serial", ((1, "NULL", "-3", stamp),), 1),
            ("a real serial", ((1, "NULL", "2.5", stamp),), 1),
            ("a text serial", ((1, "NULL", "'five'", stamp),), 1),
            ("a zero project", ((1, "0", "7", stamp),), 8),
            ("a negative project", ((1, "-1", "7", stamp),), 8),
            ("a text project", ((1, "'x'", "7", stamp),), 8),
            ("no such project", ((1, "999", "7", stamp),), 8),
            ("a malformed stamp", ((1, "NULL", "7", "'yesterday'"),), 8),
            ("an oversized stamp", ((1, "NULL", "7", f"'{STAMP * 2}'"),), 8),
            ("a stamp with NUL", ((1, "NULL", "7", f"'{STAMP}' || char(0)"),), 8),
        ):
            with self.subTest(kind=label):
                with closing(sqlite3.connect(self.database_path)) as connection:
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute("DELETE FROM project_activation")
                    for row in rows:
                        connection.execute(
                            "INSERT INTO project_activation VALUES "
                            f"({', '.join(str(value) for value in row)})"
                        )
                    connection.commit()

                snapshot = self.memory.project_activation()
                self.assertIs(snapshot.state, ActivationState.UNREADABLE)
                self.assertIsNone(snapshot.token)
                self.assertIs(
                    self.memory.list_projects().activation.state,
                    ActivationState.UNREADABLE,
                )
                context = self.memory.project_context_snapshot(17, 2048)
                self.assertEqual((context.project, context.facts), (None, ()))

                self.assertIs(
                    self.memory.activate_project(self.first).change,
                    ActivationChange.ACTIVATED,
                )
                self.assertEqual(self.activation_rows(), [(1, self.first, serial)])

    def test_the_sql_alone_never_passes_the_largest_serial(self) -> None:
        self.memory.activate_project(self.first)
        self.raw("UPDATE project_activation SET serial = ?", (ACTIVATION_SERIAL_MAX,))

        # The guarded UPDATE Maria runs matches no row at the largest serial,
        # even if her own check before it were skipped.
        with closing(sqlite3.connect(self.database_path)) as connection:
            cursor = connection.execute(
                "UPDATE project_activation "
                "SET project_id = ?, serial = serial + 1, changed_at = ? "
                "WHERE id = 1 AND serial = ? AND serial < ?",
                (self.second, STAMP, ACTIVATION_SERIAL_MAX, ACTIVATION_SERIAL_MAX),
            )
            connection.commit()
        self.assertEqual(cursor.rowcount, 0)
        # Without that condition the sum becomes a real number, which the
        # column's check refuses: the serial never wraps, resets, or turns real.
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw("UPDATE project_activation SET serial = serial + 1")
        self.assertEqual(
            self.rows("SELECT serial, typeof(serial) FROM project_activation"),
            [(ACTIVATION_SERIAL_MAX, "integer")],
        )

    def test_the_sql_refuses_a_serial_or_row_maria_never_writes(self) -> None:
        # Text that reads as a whole number takes the column's integer
        # affinity and is stored as one; any other text stays text.
        for statement, parameters in (
            ("INSERT INTO project_activation VALUES (2, NULL, 1, ?)", (STAMP,)),
            ("INSERT INTO project_activation VALUES (1, NULL, 0, ?)", (STAMP,)),
            ("INSERT INTO project_activation VALUES (1, NULL, 'five', ?)", (STAMP,)),
            ("INSERT INTO project_activation VALUES (1, 0, 1, ?)", (STAMP,)),
            ("INSERT INTO project_activation VALUES (1, NULL, 1, 'yesterday')", ()),
            ("INSERT INTO project_activation VALUES (1, NULL, 1.5, ?)", (STAMP,)),
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.raw(statement, parameters)
        self.assertEqual(self.activation_rows(), [])

    def test_a_busy_database_gives_the_shared_busy_text_and_a_failure_the_fixed(
        self,
    ) -> None:
        holder = sqlite3.connect(self.database_path, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")
        with patch.object(memory_module, "DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS", 0):
            for call in (
                lambda: self.memory.project_activation(),
                lambda: self.memory.activate_project(self.first),
                lambda: self.memory.list_projects(),
            ):
                with self.subTest(call=call):
                    with self.assertRaises(RuntimeError) as raised:
                        call()
                    self.assertEqual(str(raised.exception), BUSY)
        holder.execute("ROLLBACK")
        with patch.object(
            memory_module.sqlite3,
            "connect",
            side_effect=sqlite3.DatabaseError("x"),
        ):
            with self.assertRaises(RuntimeError) as raised:
                self.memory.project_activation()
        self.assertEqual(str(raised.exception), STORAGE)
        self.assertIsNone(raised.exception.__cause__)


class SnapshotTests(_ProjectCase):
    """One read transaction gives a request its activation, project, and facts."""

    def setUp(self) -> None:
        super().setUp()
        self.first = self.memory.create_project("First", "workspace", "f")
        self.second = self.memory.create_project("Second", "workspace", "s")
        self.first_facts = [
            self.memory.add_project_fact(self.first, f"First fact {index}.")
            for index in range(3)
        ]
        self.second_fact = self.memory.add_project_fact(self.second, "Second fact.")

    def test_no_active_project_means_no_project_and_no_fact(self) -> None:
        snapshot = self.memory.project_context_snapshot(
            17,
            2048,
            (self.first_facts[0],),
        )

        self.assertIs(type(snapshot), ProjectContextRead)
        self.assertIsNone(snapshot.project)
        self.assertEqual((snapshot.facts, snapshot.supplied_facts), ((), ()))

    def test_only_the_active_projects_eligible_facts_are_read(self) -> None:
        self.memory.activate_project(self.first)

        snapshot = self.memory.project_context_snapshot(
            17,
            2048,
            (self.first_facts[1], self.second_fact),
        )

        self.assertEqual(snapshot.project.project_id, self.first)
        self.assertEqual(snapshot.activation.token, (self.first, 1))
        self.assertEqual(
            [row["id"] for row in snapshot.facts],
            self.first_facts,
        )
        self.assertEqual(
            snapshot.facts[0],
            {
                "id": self.first_facts[0],
                "project_id": self.first,
                "text": "First fact 0.",
            },
        )
        self.assertEqual(
            [row["id"] for row in snapshot.supplied_facts],
            [self.first_facts[1]],
        )
        no_facts = self.memory.active_project()
        self.assertEqual(
            (no_facts.facts, no_facts.project.project_id),
            ((), self.first),
        )

    def test_the_limit_and_the_text_bound_hold(self) -> None:
        self.memory.activate_project(self.first)

        self.assertEqual(len(self.memory.project_context_snapshot(2, 2048).facts), 2)
        self.assertEqual(self.memory.project_context_snapshot(17, 12).facts, ())
        self.assertEqual(len(self.memory.project_context_snapshot(17, 13).facts), 3)
        with self.assertRaises(ValueError):
            self.memory.project_context_snapshot(101, 2048)


class FactIntegrityTests(_ProjectCase):
    """A fact the approval digest does not cover is never read for context."""

    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.memory.create_project("Garden", "workspace", "g")
        self.memory.activate_project(self.project_id)

    def eligible(self, limit: int = 17) -> list[int]:
        """Return the ids of the active project's eligible facts, in order."""
        return [
            row["id"]
            for row in self.memory.project_context_snapshot(limit, 2048).facts
        ]

    def test_a_digest_that_is_not_lowercase_hexadecimal_text_is_refused(
        self,
    ) -> None:
        self.insert_fact(self.project_id, "Not hexadecimal.", "g" * 64)
        digest = fact_digest(self.project_id, "Blob digest.")
        # Bound as bytes, the digest is a 64-byte blob in every encoding, so
        # the length check passes and only the read's type guard refuses it.
        self.raw(
            "INSERT INTO project_facts "
            "(project_id, text, approved_digest, created_at, updated_at) "
            "VALUES (?, 'Blob digest.', ?, ?, ?)",
            (self.project_id, digest.encode("ascii"), STAMP, STAMP),
        )

        self.assertEqual(self.eligible(), [])
        with self.assertRaises(RuntimeError) as raised:
            self.memory.list_project_facts(self.project_id)
        self.assertEqual(str(raised.exception), STORAGE)

    def test_a_text_or_project_changed_without_its_digest_is_refused(self) -> None:
        fact_id = self.memory.add_project_fact(self.project_id, "Original.")
        self.raw("UPDATE project_facts SET text = 'Changed.' WHERE id = ?", (fact_id,))
        self.assertEqual(self.eligible(), [])
        self.raw("UPDATE project_facts SET text = 'Original.' WHERE id = ?", (fact_id,))
        self.assertEqual(self.eligible(), [fact_id])
        other = self.memory.create_project("Other", "workspace", "o")
        self.raw(
            "UPDATE project_facts SET project_id = ? WHERE id = ?",
            (other, fact_id),
        )
        self.memory.activate_project(other)
        self.assertEqual(self.eligible(), [])

    def test_a_recomputed_digest_is_accepted(self) -> None:
        # The digest is an integrity check, not authentication: anyone who can
        # rewrite the database can recompute it, and Maria then accepts it.
        fact_id = self.insert_fact(self.project_id, "Written outside Maria.")

        self.assertEqual(self.eligible(), [fact_id])

    def test_refused_rows_are_never_returned_or_counted_toward_the_limit(
        self,
    ) -> None:
        for text, digest in (
            ("Wrong digest.", "f" * 64),
            (
                "Uppercase digest.",
                fact_digest(self.project_id, "Uppercase digest.").upper(),
            ),
            ("Has\x00NUL", None),
            ("x" * 513, None),
            (" leading", None),
        ):
            with self.subTest(text=ascii(text)[:16]):
                self.insert_fact(self.project_id, text, digest)
        self.raw(
            "INSERT INTO project_facts "
            "(project_id, text, approved_digest, created_at, updated_at) "
            "VALUES (?, CAST('blob text' AS BLOB), ?, ?, ?)",
            (self.project_id, fact_digest(self.project_id, "blob text"), STAMP, STAMP),
        )
        self.raw(
            "INSERT INTO project_facts "
            "(project_id, text, approved_digest, created_at, updated_at) "
            "VALUES (?, 'Backwards stamps.', ?, ?, ?)",
            (
                self.project_id,
                fact_digest(self.project_id, "Backwards stamps."),
                "2026-10-09T21:24:13Z",
                "2026-10-08T00:00:00Z",
            ),
        )
        good = self.memory.add_project_fact(self.project_id, "The one good fact.")

        self.assertEqual(self.eligible(limit=1), [good])
        with self.assertRaises(RuntimeError) as raised:
            self.memory.list_project_facts(self.project_id)
        self.assertEqual(str(raised.exception), STORAGE)


class SqlBoundaryTests(_ProjectCase):
    """The SQL checks count characters, so no valid value is refused."""

    def test_folder_and_root_checks_hold_at_their_character_bounds(self) -> None:
        self.insert_project("Folder", "workspace", "a" * 512)
        self.insert_project("Wide", "workspace", "é" * 512)
        self.insert_project("Root", "r" * 32, "")
        for name, root, folder in (
            ("Over", "workspace", "a" * 513),
            ("Root over", "r" * 33, ""),
            ("Empty root", "", ""),
            ("", "workspace", "x"),
        ):
            with self.subTest(name=name):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_project(name, root, folder)

    def test_a_valid_name_and_location_round_trip_in_this_encoding(self) -> None:
        name = "é" * 32
        folder = "/".join(["é" * 100, "é" * 100, "é" * 55])

        project_id = self.memory.create_project(name, "workspace", folder)

        project = self.memory.get_project(project_id).project
        self.assertEqual((project.name, project.folder), (name, folder))

    def test_rows_the_sql_accepts_and_maria_refuses_fail_the_read_privately(
        self,
    ) -> None:
        for name, root, folder in (
            ("a" * 65, "workspace", ""),
            ("has: colon", "workspace", ""),
            ("Wide folder", "workspace", "é" * 257),
            ("Bad root", "elsewhere", ""),
            ("Maria with folder", "project_maria", "Maria"),
        ):
            with self.subTest(name=name[:12]):
                project_id = self.insert_project(name, root, folder)
                with self.assertRaises(RuntimeError) as raised:
                    self.memory.list_projects()
                self.assertEqual(str(raised.exception), STORAGE)
                self.assertIsNone(raised.exception.__cause__)
                self.raw("DELETE FROM projects WHERE id = ?", (project_id,))

    def test_a_fact_digest_must_be_sixty_four_characters(self) -> None:
        project_id = self.memory.create_project("Garden", "workspace", "g")
        for digest in ("a" * 63, "a" * 65, ""):
            with self.subTest(length=len(digest)):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_fact(project_id, "Text.", digest)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_fact(project_id, "", fact_digest(project_id, ""))


class MigratedProjectTests:
    """Mixin marker: the same tests on a database migrated from the baseline."""

    migrated = True


class Utf16LeMixin:
    """Mixin marker: the same tests on a UTF-16le database."""

    encoding = "UTF-16le"


class Utf16BeMixin:
    """Mixin marker: the same tests on a UTF-16be database."""

    encoding = "UTF-16be"


class MigratedSqlBoundaryTests(MigratedProjectTests, SqlBoundaryTests):
    """SQL boundaries on a migrated UTF-8 database."""


class Utf16LeSqlBoundaryTests(Utf16LeMixin, SqlBoundaryTests):
    """SQL boundaries on a new UTF-16le database."""


class Utf16BeSqlBoundaryTests(Utf16BeMixin, SqlBoundaryTests):
    """SQL boundaries on a new UTF-16be database."""


class MigratedUtf16LeSqlBoundaryTests(
    MigratedProjectTests,
    Utf16LeMixin,
    SqlBoundaryTests,
):
    """SQL boundaries on a migrated UTF-16le database."""


class MigratedUtf16BeSqlBoundaryTests(
    MigratedProjectTests,
    Utf16BeMixin,
    SqlBoundaryTests,
):
    """SQL boundaries on a migrated UTF-16be database."""


class MigratedFactIntegrityTests(MigratedProjectTests, FactIntegrityTests):
    """Fact integrity on a migrated UTF-8 database."""


class Utf16LeFactIntegrityTests(Utf16LeMixin, FactIntegrityTests):
    """Fact integrity on a new UTF-16le database."""


class Utf16BeFactIntegrityTests(Utf16BeMixin, FactIntegrityTests):
    """Fact integrity on a new UTF-16be database."""


class Utf16LeActivationTests(Utf16LeMixin, ActivationTests):
    """The activation setting on a new UTF-16le database."""


class Utf16BeSnapshotTests(Utf16BeMixin, SnapshotTests):
    """The snapshot on a new UTF-16be database."""


class MigratedActivationTests(MigratedProjectTests, ActivationTests):
    """A migrated database was never activated, and behaves the same."""


if __name__ == "__main__":
    unittest.main()
