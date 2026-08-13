"""Focused regression tests for Maria's safe filesystem tool."""

from __future__ import annotations

import errno
import logging
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import tools.filesystem as filesystem_module
from core.config import MariaConfig
from core.constants import DEFAULT_TEXT_READ_LIMIT_BYTES
from core.tool_manager import ToolExecutionResult
from tools.filesystem import FilesystemTool, build_tool


class FilesystemToolTests(unittest.TestCase):
    """Exercise filesystem behavior only inside a temporary safe root."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name)
        self.root = self.temporary_path / "safe root"
        self.root.mkdir()
        self.logger = Mock(spec=logging.Logger)
        self.tool = FilesystemTool(self.root, logger=self.logger)

    def test_tool_identity_supported_actions_and_factory_contract(self) -> None:
        config = MariaConfig(
            app_name="Maria",
            version="0.1.0",
            environment="test",
            database_path=self.temporary_path / "db" / "maria.db",
            log_dir=self.temporary_path / "logs",
            prompt_path=self.temporary_path / "prompt.txt",
            safe_filesystem_root=self.root,
            log_level="INFO",
            confirmation_required=frozenset(),
            denied_actions=frozenset(),
        )

        factory_tool = build_tool(
            config=config,
            memory=object(),
            logger=self.logger,
        )

        self.assertEqual(self.tool.name, "filesystem")
        self.assertEqual(
            self.tool.actions,
            frozenset({"read", "list", "rename", "move"}),
        )
        self.assertIsInstance(factory_tool, FilesystemTool)
        self.assertEqual(factory_tool.root_path, self.root.resolve())

    def test_read_normal_utf8_file(self) -> None:
        path = self.root / "greeting.txt"
        path.write_text("Hello, María!", encoding="utf-8")

        result = self.tool.execute("read", {"path": "greeting.txt"})

        self.assertEqual(
            result,
            ToolExecutionResult(
                True,
                "Hello, María!",
                {"path": str(path.resolve())},
            ),
        )

    def test_read_preserves_multiline_content(self) -> None:
        content = "first line\nsecond line\nthird line\n"
        (self.root / "multiline.txt").write_text(content, encoding="utf-8")

        result = self.tool.execute("read", {"path": "multiline.txt"})

        self.assertTrue(result.success)
        self.assertEqual(result.message, content)

    def test_invalid_utf8_uses_replacement_decoding(self) -> None:
        (self.root / "invalid.txt").write_bytes(b"before\xffafter")

        result = self.tool.execute("read", {"path": "invalid.txt"})

        self.assertTrue(result.success)
        self.assertEqual(result.message, "before\ufffdafter")

    def test_missing_file_is_a_controlled_failure(self) -> None:
        missing = (self.root / "missing.txt").resolve()

        result = self.tool.execute("read", {"path": "missing.txt"})

        self.assertEqual(
            result,
            ToolExecutionResult(False, f"Not a file: {missing}"),
        )

    def test_reading_a_directory_as_a_file_is_rejected(self) -> None:
        directory = self.root / "documents"
        directory.mkdir()

        result = self.tool.execute("read", {"path": "documents"})

        self.assertEqual(
            result,
            ToolExecutionResult(False, f"Not a file: {directory.resolve()}"),
        )

    def test_maximum_file_read_limit_is_enforced_at_its_boundary(self) -> None:
        at_limit = self.root / "at-limit.txt"
        over_limit = self.root / "over-limit.txt"
        at_limit.write_bytes(b"a" * DEFAULT_TEXT_READ_LIMIT_BYTES)
        over_limit.write_bytes(b"b" * (DEFAULT_TEXT_READ_LIMIT_BYTES + 1))

        accepted = self.tool.execute("read", {"path": at_limit.name})
        rejected = self.tool.execute("read", {"path": over_limit.name})

        self.assertTrue(accepted.success)
        self.assertEqual(len(accepted.message), DEFAULT_TEXT_READ_LIMIT_BYTES)
        self.assertEqual(
            rejected,
            ToolExecutionResult(False, "File is too large for v0.1 reading."),
        )

    def test_list_is_case_insensitively_sorted_with_existing_type_labels(self) -> None:
        (self.root / "zeta.txt").write_text("z", encoding="utf-8")
        (self.root / "Alpha").mkdir()
        (self.root / "beta.TXT").write_text("b", encoding="utf-8")

        result = self.tool.execute("list", {"path": "."})

        self.assertTrue(result.success)
        self.assertEqual(
            result.message,
            "directory: Alpha\nfile: beta.TXT\nfile: zeta.txt",
        )
        self.assertEqual(
            result.data,
            [
                {"name": "Alpha", "type": "directory"},
                {"name": "beta.TXT", "type": "file"},
                {"name": "zeta.txt", "type": "file"},
            ],
        )

    def test_empty_directory_uses_existing_success_message(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()

        result = self.tool.execute("list", {"path": "empty"})

        self.assertEqual(
            result,
            ToolExecutionResult(True, "Directory is empty.", []),
        )

    def test_missing_directory_and_file_as_directory_are_rejected(self) -> None:
        file_path = self.root / "plain.txt"
        file_path.write_text("plain", encoding="utf-8")
        missing = (self.root / "missing-directory").resolve()

        cases = (
            ("missing-directory", missing),
            ("plain.txt", file_path.resolve()),
        )
        for value, resolved in cases:
            with self.subTest(path=value):
                result = self.tool.execute("list", {"path": value})
                self.assertEqual(
                    result,
                    ToolExecutionResult(False, f"Not a directory: {resolved}"),
                )

    def test_relative_and_absolute_paths_inside_root_are_accepted(self) -> None:
        path = self.root / "nested" / "inside.txt"
        path.parent.mkdir()
        path.write_text("inside", encoding="utf-8")

        relative = self.tool.execute("read", {"path": "nested/inside.txt"})
        absolute = self.tool.execute("read", {"path": str(path.resolve())})

        self.assertTrue(relative.success)
        self.assertTrue(absolute.success)
        self.assertEqual(relative.message, "inside")
        self.assertEqual(absolute.message, "inside")
        self.assertEqual(relative.data, absolute.data)

    def test_absolute_outside_path_and_parent_traversal_are_rejected(self) -> None:
        outside = self.temporary_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        expected = ToolExecutionResult(
            False,
            "File read failed: Path is outside Maria's configured safe root.",
        )

        absolute = self.tool.execute("read", {"path": str(outside.resolve())})
        traversal = self.tool.execute("read", {"path": "../outside.txt"})

        self.assertEqual(absolute, expected)
        self.assertEqual(traversal, expected)

    def test_expanded_home_path_outside_root_is_rejected(self) -> None:
        fake_home = self.temporary_path / "fake-home"
        fake_home.mkdir()
        environment = {
            "HOME": str(fake_home),
            "USERPROFILE": str(fake_home),
        }

        with patch.dict(os.environ, environment):
            result = self.tool.execute("read", {"path": "~/private.txt"})

        self.assertEqual(
            result,
            ToolExecutionResult(
                False,
                "File read failed: Path is outside Maria's configured safe root.",
            ),
        )

    def test_paths_containing_spaces_work(self) -> None:
        path = self.root / "folder with spaces" / "file with spaces.txt"
        path.parent.mkdir()
        path.write_text("spaced path", encoding="utf-8")

        result = self.tool.execute(
            "read",
            {"path": "folder with spaces/file with spaces.txt"},
        )

        self.assertTrue(result.success)
        self.assertEqual(result.message, "spaced path")

    def test_quote_characters_are_literal_and_not_shell_syntax(self) -> None:
        quoted_name = "'quoted note.txt'"
        (self.root / quoted_name).write_text("literal quotes", encoding="utf-8")
        (self.root / "plain.txt").write_text("plain", encoding="utf-8")

        literal = self.tool.execute("read", {"path": quoted_name})
        shell_style = self.tool.execute("read", {"path": '"plain.txt"'})

        self.assertTrue(literal.success)
        self.assertEqual(literal.message, "literal quotes")
        self.assertFalse(shell_style.success)
        self.assertNotEqual(shell_style.message, "plain")

    def test_symlink_escape_is_rejected_when_symlinks_are_available(self) -> None:
        outside = self.temporary_path / "outside-secret.txt"
        outside.write_text("outside secret", encoding="utf-8")
        link = self.root / "escape-link.txt"
        try:
            link.symlink_to(outside)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"Symbolic links unavailable: {exc}")

        result = self.tool.execute("read", {"path": link.name})

        self.assertEqual(
            result,
            ToolExecutionResult(
                False,
                "File read failed: Path is outside Maria's configured safe root.",
            ),
        )

    def test_rename_file_succeeds(self) -> None:
        source = self.root / "old name.txt"
        destination = self.root / "new name.txt"
        source.write_text("preserved", encoding="utf-8")

        result = self.tool.execute(
            "rename",
            {"source": source.name, "destination": destination.name},
        )

        self.assertEqual(
            result,
            ToolExecutionResult(
                True,
                "Renamed old name.txt to new name.txt.",
                {
                    "source": str(source.resolve()),
                    "destination": str(destination.resolve()),
                },
            ),
        )
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_text(encoding="utf-8"), "preserved")

    def test_rename_directory_succeeds(self) -> None:
        source = self.root / "old-directory"
        destination = self.root / "new-directory"
        source.mkdir()
        (source / "child.txt").write_text("child", encoding="utf-8")

        result = self.tool.execute(
            "rename",
            {"source": source.name, "destination": destination.name},
        )

        self.assertTrue(result.success)
        self.assertFalse(source.exists())
        self.assertEqual(
            (destination / "child.txt").read_text(encoding="utf-8"),
            "child",
        )

    def test_rename_rejects_missing_source(self) -> None:
        source = (self.root / "missing.txt").resolve()

        result = self.tool.execute(
            "rename",
            {"source": "missing.txt", "destination": "new.txt"},
        )

        self.assertEqual(
            result,
            ToolExecutionResult(False, f"Source does not exist: {source}"),
        )

    def test_rename_rejects_existing_destination_and_preserves_it(self) -> None:
        source = self.root / "source.txt"
        destination = self.root / "destination.txt"
        source.write_text("source content", encoding="utf-8")
        destination.write_text("destination content", encoding="utf-8")

        result = self.tool.execute(
            "rename",
            {"source": source.name, "destination": destination.name},
        )

        self.assertEqual(
            result,
            ToolExecutionResult(
                False,
                f"Destination already exists: {destination.resolve()}",
            ),
        )
        self.assertEqual(source.read_text(encoding="utf-8"), "source content")
        self.assertEqual(
            destination.read_text(encoding="utf-8"),
            "destination content",
        )

    def test_rename_requires_an_existing_destination_parent(self) -> None:
        source = self.root / "source.txt"
        destination = self.root / "missing-parent" / "renamed.txt"
        source.write_text("source", encoding="utf-8")

        result = self.tool.execute(
            "rename",
            {"source": source.name, "destination": "missing-parent/renamed.txt"},
        )

        self.assertFalse(result.success)
        self.assertTrue(result.message.startswith("Rename failed: "))
        self.assertTrue(source.exists())
        self.assertFalse(destination.exists())

    def test_move_file_succeeds_and_creates_missing_destination_parents(self) -> None:
        source = self.root / "source.txt"
        destination = self.root / "new" / "deep" / "destination.txt"
        source.write_text("moved", encoding="utf-8")

        result = self.tool.execute(
            "move",
            {"source": source.name, "destination": "new/deep/destination.txt"},
        )

        self.assertTrue(result.success)
        self.assertEqual(
            result.data,
            {
                "source": str(source.resolve()),
                "destination": str(destination.resolve()),
            },
        )
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_text(encoding="utf-8"), "moved")

    def test_move_directory_succeeds(self) -> None:
        source = self.root / "source-directory"
        destination = self.root / "archive" / "destination-directory"
        source.mkdir()
        (source / "child.txt").write_text("child", encoding="utf-8")

        result = self.tool.execute(
            "move",
            {
                "source": source.name,
                "destination": "archive/destination-directory",
            },
        )

        self.assertTrue(result.success)
        self.assertFalse(source.exists())
        self.assertEqual(
            (destination / "child.txt").read_text(encoding="utf-8"),
            "child",
        )

    def test_move_rejects_existing_destination_and_preserves_it(self) -> None:
        source = self.root / "source.txt"
        destination = self.root / "destination.txt"
        source.write_text("source content", encoding="utf-8")
        destination.write_text("destination content", encoding="utf-8")

        result = self.tool.execute(
            "move",
            {"source": source.name, "destination": destination.name},
        )

        self.assertEqual(
            result,
            ToolExecutionResult(
                False,
                f"Destination already exists: {destination.resolve()}",
            ),
        )
        self.assertEqual(source.read_text(encoding="utf-8"), "source content")
        self.assertEqual(
            destination.read_text(encoding="utf-8"),
            "destination content",
        )

    def test_bare_relative_move_destination_resolves_from_safe_root(self) -> None:
        source = self.root / "source-parent" / "source.txt"
        root_destination = self.root / "moved.txt"
        source.parent.mkdir()
        source.write_text("root-relative", encoding="utf-8")

        result = self.tool.execute(
            "move",
            {"source": "source-parent/source.txt", "destination": "moved.txt"},
        )

        self.assertTrue(result.success)
        self.assertTrue(root_destination.is_file())
        self.assertFalse((source.parent / "moved.txt").exists())

    def test_unknown_write_and_delete_actions_are_not_exposed(self) -> None:
        self.assertNotIn("write", self.tool.actions)
        self.assertNotIn("delete", self.tool.actions)

        for action in ("copy", "write", "delete"):
            with self.subTest(action=action):
                result = self.tool.execute(action, {"path": "anything.txt"})
                self.assertEqual(
                    result,
                    ToolExecutionResult(
                        False,
                        f"Unsupported filesystem action: {action}",
                    ),
                )

    def test_expected_read_oserror_is_a_controlled_failure(self) -> None:
        path = self.root / "unreadable.txt"
        path.write_text("contents", encoding="utf-8")

        with patch.object(Path, "read_text", side_effect=OSError("read blocked")):
            result = self.tool.execute("read", {"path": path.name})

        self.assertEqual(
            result,
            ToolExecutionResult(False, "File read failed: read blocked"),
        )

    def test_mocked_shutil_move_failure_does_not_report_success(self) -> None:
        source = self.root / "source.txt"
        destination = self.root / "destination.txt"
        source.write_text("source", encoding="utf-8")

        with patch.object(
            filesystem_module.shutil,
            "move",
            side_effect=OSError("move blocked"),
        ):
            result = self.tool.execute(
                "move",
                {"source": source.name, "destination": destination.name},
            )

        self.assertEqual(
            result,
            ToolExecutionResult(False, "Move failed: move blocked"),
        )
        self.assertTrue(source.exists())
        self.assertFalse(destination.exists())

    def test_cross_device_style_move_error_remains_controlled(self) -> None:
        source = self.root / "source.txt"
        destination = self.root / "destination.txt"
        source.write_text("source", encoding="utf-8")
        cross_device_error = OSError(errno.EXDEV, "Invalid cross-device link")

        with patch.object(
            filesystem_module.shutil,
            "move",
            side_effect=cross_device_error,
        ):
            result = self.tool.execute(
                "move",
                {"source": source.name, "destination": destination.name},
            )

        self.assertFalse(result.success)
        self.assertTrue(result.message.startswith("Move failed: "))
        self.assertIn("Invalid cross-device link", result.message)
        self.assertTrue(source.exists())
        self.assertFalse(destination.exists())

    def test_read_logging_contains_path_but_not_file_contents(self) -> None:
        content = "PRIVATE-FILE-CONTENT-437aab9c-complete"
        path = self.root / "private.txt"
        path.write_text(content, encoding="utf-8")
        self.logger.reset_mock()

        result = self.tool.execute("read", {"path": path.name})

        self.assertTrue(result.success)
        self.logger.info.assert_called_once_with("Read file: %s", path.resolve())
        logged_calls = " ".join(str(entry) for entry in self.logger.method_calls)
        self.assertNotIn(content, logged_calls)


if __name__ == "__main__":
    unittest.main()
