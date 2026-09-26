"""Integration tests for Maria's clean, temporary startup wiring."""

from __future__ import annotations

import logging
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import core.brain as brain_module
from core.brain import MariaBrain
from core.config import MariaConfig
from core.constants import DATABASE_SCHEMA_VERSION, LOCAL_CONFIG_PATH
from core.failures import Failure, FailureKind, TypedFailureError
from core.filesystem_roots import (
    FilesystemProtections,
    FilesystemRoot,
    FilesystemRoots,
    RootPolicy,
)
from core.permissions import PermissionDecision
from core.session import MariaSession
from tests.schema_fixtures import (
    create_v0_2_0_baseline,
    read_schema_version,
    stamp_schema_version,
)


class StartupIntegrationTests(unittest.TestCase):
    """Exercise real startup components without touching runtime files."""

    def setUp(self) -> None:
        self._temporary_directory = TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.temporary_root = Path(self._temporary_directory.name).resolve()

        self.database_path = (
            self.temporary_root / "runtime" / "database" / "maria.db"
        )
        self.log_dir = self.temporary_root / "runtime" / "logs"
        self.workspace_root = self.temporary_root / "workspace"
        self.filesystem_roots = FilesystemRoots(
            (
                FilesystemRoot(
                    name="workspace",
                    path=self.workspace_root,
                    policy=RootPolicy.ORGANIZE,
                    enabled=True,
                ),
            ),
            "workspace",
        )
        self.workspace_was_missing = not self.workspace_root.exists()
        self.filesystem_protections = FilesystemProtections(
            database_path=self.database_path,
            log_dir=self.log_dir,
            prompt_path=self.temporary_root / "runtime" / "prompts" / "system.txt",
        )
        self.config = MariaConfig(
            app_name="Maria",
            version="0.1.0",
            environment="test",
            database_path=self.database_path,
            log_dir=self.log_dir,
            prompt_path=self.temporary_root / "runtime" / "prompts" / "system.txt",
            filesystem_roots=self.filesystem_roots,
            filesystem_protections=self.filesystem_protections,
            log_level="DEBUG",
            confirmation_required=frozenset(),
            denied_actions=frozenset(),
        )
        self.database_parent_was_missing = not self.database_path.parent.exists()
        self.log_directory_was_missing = not self.log_dir.exists()

        self._maria_logger = logging.getLogger("maria")
        self._original_handlers = tuple(self._maria_logger.handlers)
        self._original_level = self._maria_logger.level
        self._original_propagate = self._maria_logger.propagate
        self._original_disabled = self._maria_logger.disabled
        for handler in self._original_handlers:
            self._maria_logger.removeHandler(handler)
        self._logging_restored = False
        self.addCleanup(self._restore_maria_logging)

        with patch("core.brain.load_config", return_value=self.config) as load_config:
            self.brain = MariaBrain.from_defaults()

        # Startup names the private override deliberately and nothing else;
        # the patched loader means no test here ever reads the real file.
        load_config.assert_called_once_with(local_override_path=LOCAL_CONFIG_PATH)
        self._temporary_handlers = tuple(self._maria_logger.handlers)
        self.session = MariaSession(self.brain)

    def test_clean_start_creates_database_log_and_expected_schema(self) -> None:
        self.assertTrue(self.database_parent_was_missing)
        self.assertTrue(self.log_directory_was_missing)
        self.assertTrue(self.database_path.parent.is_dir())
        self.assertTrue(self.database_path.is_file())
        self.assertTrue(self.log_dir.is_dir())
        self.assertTrue((self.log_dir / "maria.log").is_file())

        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        finally:
            connection.close()

        table_names = {str(row[0]) for row in rows}
        self.assertTrue(
            {"interactions", "notes", "memories"}.issubset(table_names)
        )

    def test_clean_start_stamps_the_database_with_the_current_schema(self) -> None:
        self.assertEqual(
            read_schema_version(self.database_path),
            DATABASE_SCHEMA_VERSION,
        )
        self.assertEqual(self.brain.memory.schema_version(), DATABASE_SCHEMA_VERSION)

    def test_a_database_from_a_newer_build_stops_startup_with_its_own_message(
        self,
    ) -> None:
        """Prove the refusal survives the whole real startup path.

        This is the boundary the owner meets: configuration is loaded, roots
        are prepared, logging is configured, and the memory layer then refuses
        the file. The exception has to reach ``from_defaults``' caller intact,
        because ``main.py`` decides what to print from its carried failure.
        """
        newer_root = self.temporary_root / "newer"
        database_path = newer_root / "database" / "maria.db"
        create_v0_2_0_baseline(database_path)
        stamp_schema_version(database_path, DATABASE_SCHEMA_VERSION + 1)
        before = database_path.read_bytes()
        newer_config = MariaConfig(
            app_name="Maria",
            version="0.1.0",
            environment="test",
            database_path=database_path,
            log_dir=newer_root / "logs",
            prompt_path=newer_root / "prompts" / "system.txt",
            filesystem_roots=self.filesystem_roots,
            filesystem_protections=FilesystemProtections(
                database_path=database_path,
                log_dir=newer_root / "logs",
                prompt_path=newer_root / "prompts" / "system.txt",
            ),
            log_level="DEBUG",
            confirmation_required=frozenset(),
            denied_actions=frozenset(),
        )

        with patch("core.brain.load_config", return_value=newer_config):
            with self.assertRaises(TypedFailureError) as caught:
                MariaBrain.from_defaults()

        self.assertEqual(
            caught.exception.failure,
            Failure(FailureKind.DATABASE_SCHEMA_TOO_NEW),
        )
        self.assertEqual(
            str(caught.exception),
            "This database was written by a newer version of Maria and "
            "cannot be opened by this one.",
        )
        self.assertEqual(database_path.read_bytes(), before)
        self.assertEqual(
            read_schema_version(database_path),
            DATABASE_SCHEMA_VERSION + 1,
        )
        self.assertEqual(
            sorted(path.name for path in database_path.parent.iterdir()),
            ["maria.db"],
        )

    def test_real_tools_load_and_calculator_executes(self) -> None:
        expected_tool_names = {
            "calculator",
            "filesystem",
            "history",
            "memory",
            "notes",
            "system",
        }
        for tool_name in expected_tool_names:
            with self.subTest(tool_name=tool_name):
                probe = self.brain.tool_manager.execute_tool(
                    tool_name,
                    "startup_probe",
                    {},
                )
                self.assertFalse(probe.success)
                self.assertEqual(
                    probe.message,
                    f"Unsupported action: {tool_name}.startup_probe "
                    "is not a supported action.",
                )

        response = self.brain.process("calculate 6 * 7")

        self.assertEqual(response.text, "6 * 7 = 42")
        self.assertFalse(response.needs_confirmation)

    def test_note_round_trip_uses_only_the_temporary_database(self) -> None:
        created = self.brain.process(
            "create note Release check: Stored in the temporary database"
        )
        retrieved = self.brain.process("read note Release check")

        self.assertEqual(created.text, "Created note 1: Release check")
        self.assertEqual(
            retrieved.text,
            "Release check\nStored in the temporary database",
        )
        self.assertEqual(self.brain.memory.database_path, self.database_path)

    def test_missing_colon_note_command_performs_no_note_action(self) -> None:
        def _row_counts() -> tuple[int, int]:
            connection = sqlite3.connect(self.database_path)
            try:
                notes = connection.execute(
                    "SELECT COUNT(*) FROM notes"
                ).fetchone()[0]
                interactions = connection.execute(
                    "SELECT COUNT(*) FROM interactions"
                ).fetchone()[0]
            finally:
                connection.close()
            return notes, interactions

        notes_before, interactions_before = _row_counts()

        response = self.brain.process("create note Project Plan")

        self.assertEqual(response.text, "Use: create note <title>: <body>")
        self.assertFalse(response.needs_confirmation)

        notes_after, interactions_after = _row_counts()
        self.assertEqual(notes_after, notes_before)
        self.assertEqual(interactions_after, interactions_before + 1)

    def test_missing_colon_note_command_does_not_reach_notes_tool(self) -> None:
        notes_tool = self.brain.tool_manager._tools["notes"]

        with patch.object(
            notes_tool,
            "execute",
            wraps=notes_tool.execute,
        ) as notes_execute:
            response = self.brain.process("create note Project Plan")

        notes_execute.assert_not_called()
        self.assertEqual(response.text, "Use: create note <title>: <body>")

    def test_explicit_untitled_note_creation_still_works(self) -> None:
        created = self.brain.process("create note : Stored without a title")
        retrieved = self.brain.process("read note Untitled")

        self.assertEqual(created.text, "Created note 1: Untitled")
        self.assertEqual(
            retrieved.text,
            "Untitled\nStored without a title",
        )

    def test_explicit_memory_round_trip_uses_only_temporary_database(self) -> None:
        fact = "Startup integration token is TEMPORARY-ONLY"

        stored = self.brain.process(f"remember {fact}")
        recalled = self.brain.process("recall temporary-only")

        self.assertEqual(stored.text, f"Remembered memory 1: {fact}")
        self.assertEqual(recalled.text, f"1: {fact}")
        self.assertEqual(self.brain.memory.database_path, self.database_path)

    def test_read_interaction_records_a_content_free_audit_summary(self) -> None:
        self.brain.process("remember a distinctive marker EXC-HISTORY-TOKEN")
        self.brain.process("recall EXC-HISTORY-TOKEN")

        connection = sqlite3.connect(self.database_path)
        try:
            recall_row_id = connection.execute(
                "SELECT id FROM interactions ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        finally:
            connection.close()

        read_response = self.brain.process(f"read interaction {recall_row_id}")

        self.assertIn("EXC-HISTORY-TOKEN", read_response.text)
        self.assertIn(f"Interaction {recall_row_id}", read_response.text)
        self.assertIn("Request:\nrecall EXC-HISTORY-TOKEN", read_response.text)

        connection = sqlite3.connect(self.database_path)
        try:
            newest_id, newest_response = connection.execute(
                "SELECT id, response FROM interactions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()

        self.assertGreater(newest_id, recall_row_id)
        self.assertEqual(
            newest_response,
            f"Displayed interaction {recall_row_id}.",
        )
        self.assertNotIn("EXC-HISTORY-TOKEN", newest_response)

    def test_mandatory_permission_policy_remains_active(self) -> None:
        rename = self.brain.permissions.check("filesystem", "rename")
        move = self.brain.permissions.check("filesystem", "move")
        delete = self.brain.permissions.check("filesystem", "delete")
        write = self.brain.permissions.check("filesystem", "write")
        history_delete = self.brain.permissions.check("history", "delete")

        self.assertEqual(
            rename.decision,
            PermissionDecision.REQUIRES_CONFIRMATION,
        )
        self.assertEqual(
            move.decision,
            PermissionDecision.REQUIRES_CONFIRMATION,
        )
        self.assertEqual(delete.decision, PermissionDecision.DENIED)
        self.assertEqual(write.decision, PermissionDecision.DENIED)
        self.assertEqual(
            history_delete.decision,
            PermissionDecision.REQUIRES_CONFIRMATION,
        )

        for action in ("list", "search", "read"):
            with self.subTest(action=f"history.{action}"):
                self.assertEqual(
                    self.brain.permissions.check("history", action).decision,
                    PermissionDecision.ALLOWED,
                )

        for action in ("retitle", "edit", "delete"):
            with self.subTest(action=f"notes.{action}"):
                self.assertEqual(
                    self.brain.permissions.check("notes", action).decision,
                    PermissionDecision.REQUIRES_CONFIRMATION,
                )

        for action in ("create", "list", "read"):
            with self.subTest(action=f"notes.{action}"):
                self.assertEqual(
                    self.brain.permissions.check("notes", action).decision,
                    PermissionDecision.ALLOWED,
                )

        for action in ("update", "forget"):
            with self.subTest(action=f"memory.{action}"):
                self.assertEqual(
                    self.brain.permissions.check("memory", action).decision,
                    PermissionDecision.REQUIRES_CONFIRMATION,
                )

        for action in ("store", "search", "list"):
            with self.subTest(action=f"memory.{action}"):
                self.assertEqual(
                    self.brain.permissions.check("memory", action).decision,
                    PermissionDecision.ALLOWED,
                )

    def test_delete_interaction_removes_the_row_and_records_a_content_free_audit_summary(
        self,
    ) -> None:
        self.brain.process("remember a distinctive marker EXC-DELETE-TOKEN")
        self.brain.process("recall EXC-DELETE-TOKEN")

        connection = sqlite3.connect(self.database_path)
        try:
            target_id, other_id = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM interactions ORDER BY id DESC LIMIT 2"
                ).fetchall()
            ]
            row_count_before = connection.execute(
                "SELECT COUNT(*) FROM interactions"
            ).fetchone()[0]
        finally:
            connection.close()

        unconfirmed = self.session.submit(f"delete interaction {target_id}")

        self.assertTrue(unconfirmed.needs_confirmation)
        self.assertEqual(
            unconfirmed.text,
            "Confirmation required: history.delete requires explicit confirmation.",
        )

        connection = sqlite3.connect(self.database_path)
        try:
            still_present = connection.execute(
                "SELECT id FROM interactions WHERE id = ?", (target_id,)
            ).fetchone()
            row_count_after_unconfirmed = connection.execute(
                "SELECT COUNT(*) FROM interactions"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertIsNotNone(still_present)
        self.assertEqual(row_count_after_unconfirmed, row_count_before)

        confirmed = self.session.confirm()

        self.assertEqual(confirmed.text, f"Deleted interaction {target_id}.")
        self.assertFalse(confirmed.needs_confirmation)

        connection = sqlite3.connect(self.database_path)
        try:
            deleted_row = connection.execute(
                "SELECT id FROM interactions WHERE id = ?", (target_id,)
            ).fetchone()
            other_row = connection.execute(
                "SELECT id FROM interactions WHERE id = ?", (other_id,)
            ).fetchone()
            newest_input, newest_response = connection.execute(
                "SELECT user_input, response FROM interactions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            row_count_after_confirmed = connection.execute(
                "SELECT COUNT(*) FROM interactions"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertIsNone(deleted_row)
        self.assertIsNotNone(other_row)
        self.assertEqual(newest_input, f"delete interaction {target_id}")
        self.assertEqual(newest_response, f"Deleted interaction {target_id}.")
        self.assertNotIn("EXC-DELETE-TOKEN", newest_response)
        self.assertEqual(row_count_after_confirmed, row_count_before)

    def test_delete_interaction_recording_failure_does_not_undo_the_deletion(
        self,
    ) -> None:
        self.brain.process("remember a distinctive marker EXC-RECORD-FAILURE-TOKEN")

        connection = sqlite3.connect(self.database_path)
        try:
            target_id = connection.execute(
                "SELECT id FROM interactions ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        finally:
            connection.close()

        self.session.submit(f"delete interaction {target_id}")

        with patch.object(
            self.brain.memory,
            "record_interaction",
            side_effect=RuntimeError("temporary interaction write failure"),
        ):
            confirmed = self.session.confirm()

        self.assertEqual(confirmed.text, f"Deleted interaction {target_id}.")
        self.assertFalse(confirmed.needs_confirmation)

        connection = sqlite3.connect(self.database_path)
        try:
            deleted_row = connection.execute(
                "SELECT id FROM interactions WHERE id = ?", (target_id,)
            ).fetchone()
        finally:
            connection.close()

        self.assertIsNone(deleted_row)

    def test_retitle_note_requires_confirmation_and_then_changes_only_the_title(
        self,
    ) -> None:
        self.brain.process("create note Release Check: Original body text")
        self.brain.process("create note Untouched Note: Untouched body")
        self.brain.process("remember a distinctive marker EXC-RETITLE-TOKEN")

        target_before = self._fetch_note(1)
        other_before = self._fetch_note(2)
        snapshot_before = self._row_id_snapshot()

        notes_tool = self.brain.tool_manager._tools["notes"]
        with patch.object(
            notes_tool,
            "execute",
            wraps=notes_tool.execute,
        ) as notes_execute:
            unconfirmed = self.session.submit("retitle note 1: Renamed Release Check")

        notes_execute.assert_not_called()
        self.assertTrue(unconfirmed.needs_confirmation)
        self.assertEqual(
            unconfirmed.text,
            "Confirmation required: notes.retitle requires explicit confirmation.",
        )
        self.assertEqual(self._fetch_note(1), target_before)
        self.assertEqual(self._row_id_snapshot(), snapshot_before)

        confirmed = self.session.confirm()

        self.assertFalse(confirmed.needs_confirmation)
        self.assertEqual(confirmed.text, "Retitled note 1.")

        target_after = self._fetch_note(1)
        self.assertEqual(target_after["title"], "Renamed Release Check")
        self.assertEqual(target_after["body"], target_before["body"])
        self.assertEqual(target_after["created_at"], target_before["created_at"])
        self.assertNotEqual(target_after["updated_at"], target_before["updated_at"])
        self.assertEqual(self._fetch_note(2), other_before)

        snapshot_after = self._row_id_snapshot()
        self.assertEqual(snapshot_after["notes"], snapshot_before["notes"])
        self.assertEqual(snapshot_after["memories"], snapshot_before["memories"])
        self.assertEqual(
            snapshot_after["interactions"],
            snapshot_before["interactions"] + snapshot_after["interactions"][-1:],
        )

        newest_input, newest_response = self._newest_interaction()
        self.assertEqual(newest_input, "retitle note 1: Renamed Release Check")
        self.assertEqual(newest_response, "Retitled note 1.")
        self.assertIn("Renamed Release Check", newest_input)
        self.assertNotIn("Renamed Release Check", newest_response)

    def test_edit_note_requires_confirmation_and_then_changes_only_the_body(
        self,
    ) -> None:
        self.brain.process("create note Release Check: Original body text")
        self.brain.process("create note Untouched Note: Untouched body")
        self.brain.process("remember a distinctive marker EXC-EDIT-TOKEN")

        target_before = self._fetch_note(1)
        other_before = self._fetch_note(2)
        snapshot_before = self._row_id_snapshot()

        notes_tool = self.brain.tool_manager._tools["notes"]
        with patch.object(
            notes_tool,
            "execute",
            wraps=notes_tool.execute,
        ) as notes_execute:
            unconfirmed = self.session.submit("edit note 1: Replacement body text")

        notes_execute.assert_not_called()
        self.assertTrue(unconfirmed.needs_confirmation)
        self.assertEqual(
            unconfirmed.text,
            "Confirmation required: notes.edit requires explicit confirmation.",
        )
        self.assertEqual(self._fetch_note(1), target_before)
        self.assertEqual(self._row_id_snapshot(), snapshot_before)

        confirmed = self.session.confirm()

        self.assertFalse(confirmed.needs_confirmation)
        self.assertEqual(confirmed.text, "Edited note 1.")

        target_after = self._fetch_note(1)
        self.assertEqual(target_after["body"], "Replacement body text")
        self.assertEqual(target_after["title"], target_before["title"])
        self.assertEqual(target_after["created_at"], target_before["created_at"])
        self.assertNotEqual(target_after["updated_at"], target_before["updated_at"])
        self.assertEqual(self._fetch_note(2), other_before)

        snapshot_after = self._row_id_snapshot()
        self.assertEqual(snapshot_after["notes"], snapshot_before["notes"])
        self.assertEqual(snapshot_after["memories"], snapshot_before["memories"])
        self.assertEqual(
            snapshot_after["interactions"],
            snapshot_before["interactions"] + snapshot_after["interactions"][-1:],
        )

        newest_input, newest_response = self._newest_interaction()
        self.assertEqual(newest_input, "edit note 1: Replacement body text")
        self.assertEqual(newest_response, "Edited note 1.")
        self.assertIn("Replacement body text", newest_input)
        self.assertNotIn("Replacement body text", newest_response)

    def test_delete_note_requires_confirmation_and_then_removes_only_that_note(
        self,
    ) -> None:
        self.brain.process("create note Release Check: Original body text")
        self.brain.process("create note Untouched Note: Untouched body")
        self.brain.process("remember a distinctive marker EXC-NOTE-DELETE-TOKEN")

        target_before = self._fetch_note(1)
        other_before = self._fetch_note(2)
        snapshot_before = self._row_id_snapshot()

        notes_tool = self.brain.tool_manager._tools["notes"]
        with patch.object(
            notes_tool,
            "execute",
            wraps=notes_tool.execute,
        ) as notes_execute:
            unconfirmed = self.session.submit("delete note 1")

        notes_execute.assert_not_called()
        self.assertTrue(unconfirmed.needs_confirmation)
        self.assertEqual(
            unconfirmed.text,
            "Confirmation required: notes.delete requires explicit confirmation.",
        )
        self.assertEqual(self._fetch_note(1), target_before)
        self.assertEqual(self._row_id_snapshot(), snapshot_before)

        confirmed = self.session.confirm()

        self.assertFalse(confirmed.needs_confirmation)
        self.assertEqual(confirmed.text, "Deleted note 1.")

        self.assertIsNone(self._fetch_note(1))
        self.assertEqual(self._fetch_note(2), other_before)

        snapshot_after = self._row_id_snapshot()
        self.assertEqual(snapshot_after["notes"], [2])
        self.assertEqual(snapshot_after["memories"], snapshot_before["memories"])
        self.assertEqual(
            snapshot_after["interactions"],
            snapshot_before["interactions"] + snapshot_after["interactions"][-1:],
        )

        newest_input, newest_response = self._newest_interaction()
        self.assertEqual(newest_input, "delete note 1")
        self.assertEqual(newest_response, "Deleted note 1.")
        self.assertNotIn("Original body text", newest_response)
        self.assertNotIn("Release Check", newest_response)

    def test_note_mutation_recording_failure_does_not_undo_the_mutation(self) -> None:
        self.brain.process("create note Retitle Target: Original body text")
        self.brain.process("create note Edit Target: Original body text")
        self.brain.process("create note Delete Target: Original body text")

        for command in (
            "retitle note 1: Renamed",
            "edit note 2: Replaced",
            "delete note 3",
        ):
            with self.subTest(command=command, confirmed=False):
                self.assertTrue(self.session.submit(command).needs_confirmation)

        with patch.object(
            self.brain.memory,
            "record_interaction",
            side_effect=RuntimeError("temporary interaction write failure"),
        ):
            for command, expected_text in (
                ("retitle note 1: Renamed", "Retitled note 1."),
                ("edit note 2: Replaced", "Edited note 2."),
                ("delete note 3", "Deleted note 3."),
            ):
                with self.subTest(command=command, confirmed=True):
                    pending = self.session.submit(command)
                    self.assertTrue(pending.needs_confirmation)
                    confirmed = self.session.confirm()
                    self.assertFalse(confirmed.needs_confirmation)
                    self.assertEqual(confirmed.text, expected_text)

        self.assertEqual(self._fetch_note(1)["title"], "Renamed")
        self.assertEqual(self._fetch_note(2)["body"], "Replaced")
        self.assertIsNone(self._fetch_note(3))
        self.assertEqual(self._row_id_snapshot()["notes"], [1, 2])

    def test_update_memory_requires_confirmation_and_then_changes_only_the_fact(
        self,
    ) -> None:
        self.brain.process("remember My editor is EXC-UPDATE-TOKEN")
        self.brain.process("remember An untouched EXC-KEEP-TOKEN fact")
        self.brain.process("create note Untouched Note: Untouched body")

        target_before = self._fetch_memory(1)
        other_before = self._fetch_memory(2)
        snapshot_before = self._row_id_snapshot()

        memory_tool = self.brain.tool_manager._tools["memory"]
        with patch.object(
            memory_tool,
            "execute",
            wraps=memory_tool.execute,
        ) as memory_execute:
            unconfirmed = self.session.submit(
                "update memory 1: My editor is EXC-REPLACEMENT-TOKEN"
            )

        memory_execute.assert_not_called()
        self.assertTrue(unconfirmed.needs_confirmation)
        self.assertEqual(
            unconfirmed.text,
            "Confirmation required: memory.update requires explicit confirmation.",
        )
        self.assertEqual(self._fetch_memory(1), target_before)
        self.assertEqual(self._row_id_snapshot(), snapshot_before)

        confirmed = self.session.confirm()

        self.assertFalse(confirmed.needs_confirmation)
        self.assertEqual(confirmed.text, "Updated memory 1.")

        target_after = self._fetch_memory(1)
        self.assertEqual(target_after["fact"], "My editor is EXC-REPLACEMENT-TOKEN")
        self.assertEqual(
            target_after["normalized_fact"],
            "My editor is EXC-REPLACEMENT-TOKEN".casefold(),
        )
        self.assertEqual(target_after["created_at"], target_before["created_at"])
        self.assertEqual(self._fetch_memory(2), other_before)

        snapshot_after = self._row_id_snapshot()
        self.assertEqual(snapshot_after["memories"], snapshot_before["memories"])
        self.assertEqual(snapshot_after["notes"], snapshot_before["notes"])
        self.assertEqual(
            snapshot_after["interactions"],
            snapshot_before["interactions"] + snapshot_after["interactions"][-1:],
        )

        self.assertEqual(
            [row["id"] for row in self.brain.memory.list_memories()], [2, 1]
        )

        newest_input, newest_response = self._newest_interaction()
        self.assertEqual(
            newest_input, "update memory 1: My editor is EXC-REPLACEMENT-TOKEN"
        )
        self.assertEqual(newest_response, "Updated memory 1.")
        self.assertIn("EXC-REPLACEMENT-TOKEN", newest_input)
        self.assertNotIn("EXC-REPLACEMENT-TOKEN", newest_response)

    def test_update_memory_conflict_reports_ids_and_changes_no_row(self) -> None:
        self.brain.process("remember My editor is EXC-CONFLICT-ONE")
        self.brain.process("remember My editor is EXC-CONFLICT-TWO")

        target_before = self._fetch_memory(1)
        other_before = self._fetch_memory(2)

        self.assertTrue(
            self.session.submit(
                "update memory 1: MY EDITOR IS EXC-CONFLICT-TWO"
            ).needs_confirmation
        )
        confirmed = self.session.confirm()

        self.assertFalse(confirmed.needs_confirmation)
        self.assertEqual(
            confirmed.text, "Memory 1 conflicts with existing memory 2."
        )
        self.assertNotIn("EXC-CONFLICT-TWO", confirmed.text)
        self.assertEqual(self._fetch_memory(1), target_before)
        self.assertEqual(self._fetch_memory(2), other_before)

    def test_forget_memory_requires_confirmation_and_then_removes_only_the_target(
        self,
    ) -> None:
        self.brain.process("remember My editor is EXC-FORGET-TOKEN")
        self.brain.process("remember An untouched EXC-KEEP-TOKEN fact")
        self.brain.process("create note Untouched Note: Untouched body")

        target_before = self._fetch_memory(1)
        other_before = self._fetch_memory(2)
        snapshot_before = self._row_id_snapshot()

        memory_tool = self.brain.tool_manager._tools["memory"]
        with patch.object(
            memory_tool,
            "execute",
            wraps=memory_tool.execute,
        ) as memory_execute:
            unconfirmed = self.session.submit("forget memory 1")

        memory_execute.assert_not_called()
        self.assertTrue(unconfirmed.needs_confirmation)
        self.assertEqual(
            unconfirmed.text,
            "Confirmation required: memory.forget requires explicit confirmation.",
        )
        self.assertEqual(self._fetch_memory(1), target_before)
        self.assertEqual(self._row_id_snapshot(), snapshot_before)

        confirmed = self.session.confirm()

        self.assertFalse(confirmed.needs_confirmation)
        self.assertEqual(confirmed.text, "Forgot memory 1.")

        self.assertIsNone(self._fetch_memory(1))
        self.assertEqual(self._fetch_memory(2), other_before)

        snapshot_after = self._row_id_snapshot()
        self.assertEqual(snapshot_after["memories"], [2])
        self.assertEqual(snapshot_after["notes"], snapshot_before["notes"])
        self.assertEqual(
            snapshot_after["interactions"],
            snapshot_before["interactions"] + snapshot_after["interactions"][-1:],
        )

        newest_input, newest_response = self._newest_interaction()
        self.assertEqual(newest_input, "forget memory 1")
        self.assertEqual(newest_response, "Forgot memory 1.")
        self.assertNotIn("EXC-FORGET-TOKEN", newest_response)

    def test_forgotten_memory_content_remains_in_earlier_interaction_rows(
        self,
    ) -> None:
        self.brain.process("remember My editor is EXC-RETAINED-TOKEN")
        self.brain.process("recall EXC-RETAINED-TOKEN")

        self.assertTrue(self.session.submit("forget memory 1").needs_confirmation)
        confirmed = self.session.confirm()

        self.assertEqual(confirmed.text, "Forgot memory 1.")
        self.assertIsNone(self._fetch_memory(1))

        connection = sqlite3.connect(self.database_path)
        try:
            retained = connection.execute(
                "SELECT COUNT(*) FROM interactions "
                "WHERE instr(user_input, ?) > 0 OR instr(response, ?) > 0",
                ("EXC-RETAINED-TOKEN", "EXC-RETAINED-TOKEN"),
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertGreaterEqual(retained, 2)

    def test_memory_mutation_recording_failure_does_not_undo_the_mutation(
        self,
    ) -> None:
        self.brain.process("remember An EXC-UPDATE-TARGET fact")
        self.brain.process("remember An EXC-FORGET-TARGET fact")

        for command in ("update memory 1: A replaced fact", "forget memory 2"):
            with self.subTest(command=command, confirmed=False):
                self.assertTrue(self.session.submit(command).needs_confirmation)

        with patch.object(
            self.brain.memory,
            "record_interaction",
            side_effect=RuntimeError("temporary interaction write failure"),
        ):
            for command, expected_text in (
                ("update memory 1: A replaced fact", "Updated memory 1."),
                ("forget memory 2", "Forgot memory 2."),
            ):
                with self.subTest(command=command, confirmed=True):
                    pending = self.session.submit(command)
                    self.assertTrue(pending.needs_confirmation)
                    confirmed = self.session.confirm()
                    self.assertFalse(confirmed.needs_confirmation)
                    self.assertEqual(confirmed.text, expected_text)

        self.assertEqual(self._fetch_memory(1)["fact"], "A replaced fact")
        self.assertIsNone(self._fetch_memory(2))
        self.assertEqual(self._row_id_snapshot()["memories"], [1])

    def test_all_runtime_paths_and_file_handlers_are_temporary(self) -> None:
        self.assertTrue(self.config.database_path.is_relative_to(self.temporary_root))
        self.assertTrue(self.config.log_dir.is_relative_to(self.temporary_root))
        for root in self.config.filesystem_roots.roots:
            with self.subTest(root=root.name):
                self.assertTrue(root.path.is_relative_to(self.temporary_root))
        self.assertEqual(self.brain.config, self.config)
        self.assertEqual(self.brain.memory.database_path, self.database_path)

        file_handlers = [
            handler
            for handler in self._temporary_handlers
            if isinstance(handler, logging.FileHandler)
        ]
        self.assertEqual(len(file_handlers), 1)
        handler_path = Path(file_handlers[0].baseFilename).resolve()
        self.assertTrue(handler_path.is_relative_to(self.temporary_root))
        self.assertEqual(handler_path, self.log_dir / "maria.log")

    def test_startup_prepares_the_configured_workspace_root(self) -> None:
        self.assertTrue(self.workspace_was_missing)
        self.assertTrue(self.workspace_root.is_dir())
        self.assertTrue(self.workspace_root.is_relative_to(self.temporary_root))
        self.assertEqual(
            self.brain.config.filesystem_roots.default_root.path,
            self.workspace_root,
        )

    def test_the_prepared_workspace_serves_real_filesystem_commands(self) -> None:
        (self.workspace_root / "inbox").mkdir()
        (self.workspace_root / "inbox" / "order.txt").write_text(
            "order",
            encoding="utf-8",
        )

        listed = self.brain.process("list inbox")
        read = self.brain.process("read file inbox/order.txt")

        self.assertEqual(listed.text, "file: order.txt")
        self.assertEqual(read.text, "order")

    def test_a_refused_root_stops_startup_before_the_database_exists(self) -> None:
        occupied = self.temporary_root / "occupied-workspace"
        occupied.write_text("not a directory", encoding="utf-8")
        database_path = self.temporary_root / "refused" / "database" / "maria.db"
        refused = MariaConfig(
            app_name="Maria",
            version="0.1.0",
            environment="test",
            database_path=database_path,
            log_dir=self.temporary_root / "refused" / "logs",
            prompt_path=self.temporary_root / "refused" / "prompts" / "system.txt",
            filesystem_roots=FilesystemRoots(
                (
                    FilesystemRoot(
                        name="workspace",
                        path=occupied,
                        policy=RootPolicy.ORGANIZE,
                        enabled=True,
                    ),
                ),
                "workspace",
            ),
            filesystem_protections=FilesystemProtections(
                database_path=database_path,
                log_dir=self.temporary_root / "refused" / "logs",
                prompt_path=self.temporary_root / "refused" / "prompts" / "system.txt",
            ),
            log_level="DEBUG",
            confirmation_required=frozenset(),
            denied_actions=frozenset(),
        )

        with patch("core.brain.load_config", return_value=refused):
            with self.assertRaises(ValueError) as caught:
                MariaBrain.from_defaults()

        self.assertEqual(
            str(caught.exception),
            "An enabled filesystem root must be a directory.",
        )
        self.assertFalse(database_path.exists())
        self.assertFalse(database_path.parent.exists())
        self.assertTrue(occupied.is_file())

    def test_root_preparation_receives_the_exact_active_protections(self) -> None:
        recorded: list[object] = []
        real_prepare = brain_module.prepare_roots

        def recording_prepare(roots: object, protections: object) -> None:
            recorded.append((roots, protections))
            real_prepare(roots, protections)

        with patch("core.brain.load_config", return_value=self.config):
            with patch.object(brain_module, "prepare_roots", recording_prepare):
                MariaBrain.from_defaults()

        self.assertEqual(len(recorded), 1)
        prepared_roots, prepared_protections = recorded[0]
        self.assertIs(prepared_roots, self.config.filesystem_roots)
        self.assertIs(prepared_protections, self.config.filesystem_protections)

    def test_a_root_naming_an_active_runtime_location_is_refused(self) -> None:
        refused_log_dir = self.temporary_root / "refused-runtime" / "logs"
        database_path = (
            self.temporary_root / "refused-runtime" / "database" / "maria.db"
        )
        refused = MariaConfig(
            app_name="Maria",
            version="0.1.0",
            environment="test",
            database_path=database_path,
            log_dir=refused_log_dir,
            prompt_path=self.temporary_root / "refused-runtime" / "system.txt",
            filesystem_roots=FilesystemRoots(
                (
                    FilesystemRoot(
                        name="workspace",
                        path=refused_log_dir,
                        policy=RootPolicy.ORGANIZE,
                        enabled=True,
                    ),
                ),
                "workspace",
            ),
            filesystem_protections=FilesystemProtections(
                database_path=database_path,
                log_dir=refused_log_dir,
                prompt_path=self.temporary_root / "refused-runtime" / "system.txt",
            ),
            log_level="DEBUG",
            confirmation_required=frozenset(),
            denied_actions=frozenset(),
        )

        with patch("core.brain.load_config", return_value=refused):
            with self.assertRaises(ValueError) as caught:
                MariaBrain.from_defaults()

        self.assertEqual(
            str(caught.exception),
            "A filesystem root may not be a permanently excluded location.",
        )
        # The refusal precedes every side effect: the root was not created,
        # the active log directory configuring logging would have created was
        # not created, and the database was never opened.
        self.assertFalse(refused_log_dir.exists())
        self.assertFalse(refused_log_dir.parent.exists())
        self.assertFalse(database_path.exists())

    def test_a_refused_root_precedes_logging_configuration(self) -> None:
        occupied = self.temporary_root / "occupied-root"
        occupied.write_text("not a directory", encoding="utf-8")
        log_dir = self.temporary_root / "unreached" / "logs"
        refused = MariaConfig(
            app_name="Maria",
            version="0.1.0",
            environment="test",
            database_path=self.temporary_root / "unreached" / "database" / "maria.db",
            log_dir=log_dir,
            prompt_path=self.temporary_root / "unreached" / "system.txt",
            filesystem_roots=FilesystemRoots(
                (
                    FilesystemRoot(
                        name="workspace",
                        path=occupied,
                        policy=RootPolicy.ORGANIZE,
                        enabled=True,
                    ),
                ),
                "workspace",
            ),
            filesystem_protections=FilesystemProtections(
                database_path=self.temporary_root
                / "unreached"
                / "database"
                / "maria.db",
                log_dir=log_dir,
                prompt_path=self.temporary_root / "unreached" / "system.txt",
            ),
            log_level="DEBUG",
            confirmation_required=frozenset(),
            denied_actions=frozenset(),
        )

        with patch("core.brain.load_config", return_value=refused):
            with self.assertRaises(ValueError):
                MariaBrain.from_defaults()

        self.assertFalse(log_dir.exists())
        self.assertTrue(occupied.is_file())

    def test_temporary_logging_handler_closes_before_directory_cleanup(self) -> None:
        file_handlers = [
            handler
            for handler in self._temporary_handlers
            if isinstance(handler, logging.FileHandler)
        ]
        self.assertEqual(len(file_handlers), 1)

        self._restore_maria_logging()

        for handler in file_handlers:
            self.assertNotIn(handler, self._maria_logger.handlers)
            self.assertIsNone(handler.stream)
        self.assertTrue(self.log_dir.is_dir())

    def _fetch_note(self, note_id: int) -> dict[str, str] | None:
        """Return one temporary-database note row, or None when it is gone."""
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT title, body, created_at, updated_at FROM notes WHERE id = ?",
                (note_id,),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else dict(row)

    def _fetch_memory(self, memory_id: int) -> dict[str, str] | None:
        """Return one temporary-database memory row, or None when it is gone."""
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT fact, normalized_fact, created_at FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else dict(row)

    def _row_id_snapshot(self) -> dict[str, list[int]]:
        """Return the ids stored in every persistence table, oldest first."""
        connection = sqlite3.connect(self.database_path)
        try:
            return {
                table: [
                    int(row[0])
                    for row in connection.execute(
                        f"SELECT id FROM {table} ORDER BY id"
                    ).fetchall()
                ]
                for table in ("notes", "memories", "interactions")
            }
        finally:
            connection.close()

    def _newest_interaction(self) -> tuple[str, str]:
        """Return the newest interaction row as (user_input, response)."""
        connection = sqlite3.connect(self.database_path)
        try:
            user_input, response = connection.execute(
                "SELECT user_input, response FROM interactions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        return str(user_input), str(response)

    def _restore_maria_logging(self) -> None:
        """Close temporary handlers and restore the process-global logger."""
        if self._logging_restored:
            return

        for handler in tuple(self._maria_logger.handlers):
            self._maria_logger.removeHandler(handler)
            if handler not in self._original_handlers:
                handler.flush()
                handler.close()

        for handler in self._original_handlers:
            self._maria_logger.addHandler(handler)
        self._maria_logger.setLevel(self._original_level)
        self._maria_logger.propagate = self._original_propagate
        self._maria_logger.disabled = self._original_disabled
        self._logging_restored = True


if __name__ == "__main__":
    unittest.main()
