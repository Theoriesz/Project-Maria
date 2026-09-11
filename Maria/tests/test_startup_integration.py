"""Integration tests for Maria's clean, temporary startup wiring."""

from __future__ import annotations

import logging
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core.brain import MariaBrain
from core.config import MariaConfig
from core.permissions import PermissionDecision


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
        self.config = MariaConfig(
            app_name="Maria",
            version="0.1.0",
            environment="test",
            database_path=self.database_path,
            log_dir=self.log_dir,
            prompt_path=self.temporary_root / "runtime" / "prompts" / "system.txt",
            safe_filesystem_root=self.temporary_root / "safe-filesystem-root",
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

        load_config.assert_called_once_with()
        self._temporary_handlers = tuple(self._maria_logger.handlers)

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
                    f"Unsupported action for {tool_name}: startup_probe",
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
            with self.subTest(action=action):
                self.assertEqual(
                    self.brain.permissions.check("history", action).decision,
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

        unconfirmed = self.brain.process(f"delete interaction {target_id}")

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

        confirmed = self.brain.process(
            f"delete interaction {target_id}",
            confirmed=True,
        )

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

        self.brain.process(f"delete interaction {target_id}")

        with patch.object(
            self.brain.memory,
            "record_interaction",
            side_effect=RuntimeError("temporary interaction write failure"),
        ):
            confirmed = self.brain.process(
                f"delete interaction {target_id}",
                confirmed=True,
            )

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

    def test_all_runtime_paths_and_file_handlers_are_temporary(self) -> None:
        self.assertTrue(self.config.database_path.is_relative_to(self.temporary_root))
        self.assertTrue(self.config.log_dir.is_relative_to(self.temporary_root))
        self.assertTrue(
            self.config.safe_filesystem_root.is_relative_to(self.temporary_root)
        )
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
