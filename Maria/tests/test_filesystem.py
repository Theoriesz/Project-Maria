"""Focused regression tests for Maria's safe filesystem tool."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import tools.filesystem as filesystem_module
from core.config import MariaConfig
from core.constants import DEFAULT_TEXT_READ_LIMIT_BYTES
from core.tool_manager import ToolExecutionResult
from tools.filesystem import (
    PATH_OUTSIDE_ROOT_MESSAGE,
    PATH_REQUIRED_MESSAGE,
    UNCONTAINED_DISPLAY,
    UNSPECIFIED_FAILURE_REASON,
    FilesystemTool,
    build_tool,
)


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
        result = self.tool.execute("read", {"path": "missing.txt"})

        self.assertEqual(
            result,
            ToolExecutionResult(False, "Not a file: missing.txt"),
        )

    def test_reading_a_directory_as_a_file_is_rejected(self) -> None:
        (self.root / "documents").mkdir()

        result = self.tool.execute("read", {"path": "documents"})

        self.assertEqual(
            result,
            ToolExecutionResult(False, "Not a file: documents"),
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
        (self.root / "plain.txt").write_text("plain", encoding="utf-8")

        cases = (
            ("missing-directory", "missing-directory"),
            ("plain.txt", "plain.txt"),
        )
        for value, displayed in cases:
            with self.subTest(path=value):
                result = self.tool.execute("list", {"path": value})
                self.assertEqual(
                    result,
                    ToolExecutionResult(False, f"Not a directory: {displayed}"),
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
        result = self.tool.execute(
            "rename",
            {"source": "missing.txt", "destination": "new.txt"},
        )

        self.assertEqual(
            result,
            ToolExecutionResult(False, "Source does not exist: missing.txt"),
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
                "Destination already exists: destination.txt",
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
                "Destination already exists: destination.txt",
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
            ToolExecutionResult(
                False,
                f"File read failed: {UNSPECIFIED_FAILURE_REASON}",
            ),
        )
        self.assertNotIn("read blocked", result.message)

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
            ToolExecutionResult(
                False,
                f"Move failed: {UNSPECIFIED_FAILURE_REASON}",
            ),
        )
        self.assertNotIn("move blocked", result.message)
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

        self.assertEqual(
            result.message,
            f"Move failed: {os.strerror(errno.EXDEV)}",
        )
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


class FilesystemDisplayPathTests(unittest.TestCase):
    """Exercise safe-root-relative display paths in user-facing messages."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name)
        self.root = self.temporary_path / "safe root"
        self.root.mkdir()
        self.logger = Mock(spec=logging.Logger)
        self.tool = FilesystemTool(self.root, logger=self.logger)

    def test_safe_root_itself_displays_as_a_single_dot(self) -> None:
        result = self.tool.execute("read", {"path": "."})

        self.assertEqual(result, ToolExecutionResult(False, "Not a file: ."))

    def test_nested_display_paths_use_forward_slashes(self) -> None:
        (self.root / "a b" / "c d").mkdir(parents=True)

        result = self.tool.execute("list", {"path": "a b/c d/missing"})

        self.assertEqual(
            result,
            ToolExecutionResult(False, "Not a directory: a b/c d/missing"),
        )
        self.assertNotIn("\\", result.message)

    def test_absolute_input_inside_root_is_displayed_relatively(self) -> None:
        (self.root / "nested").mkdir()
        absolute = (self.root / "nested" / "missing.txt").resolve()

        result = self.tool.execute("read", {"path": str(absolute)})

        self.assertEqual(
            result,
            ToolExecutionResult(False, "Not a file: nested/missing.txt"),
        )
        self.assertNotIn(str(self.tool.root_path), result.message)

    def test_redundant_dot_and_parent_components_are_resolved_away(self) -> None:
        (self.root / "nested").mkdir()

        result = self.tool.execute("read", {"path": "nested/../missing.txt"})

        self.assertEqual(
            result,
            ToolExecutionResult(False, "Not a file: missing.txt"),
        )

    def test_spaces_and_unicode_survive_display_conversion(self) -> None:
        result = self.tool.execute(
            "read",
            {"path": "Ünïcode dir/naïve Müller.txt"},
        )

        self.assertEqual(
            result,
            ToolExecutionResult(False, "Not a file: Ünïcode dir/naïve Müller.txt"),
        )

    def test_quote_characters_remain_literal_in_display_paths(self) -> None:
        result = self.tool.execute("read", {"path": "'quoted note.txt'"})

        self.assertEqual(
            result,
            ToolExecutionResult(False, "Not a file: 'quoted note.txt'"),
        )

    def test_nested_rename_success_displays_both_relative_paths(self) -> None:
        source = self.root / "nest" / "old name.txt"
        destination = self.root / "other" / "new name.txt"
        source.parent.mkdir()
        destination.parent.mkdir()
        source.write_text("preserved", encoding="utf-8")

        result = self.tool.execute(
            "rename",
            {"source": "nest/old name.txt", "destination": "other/new name.txt"},
        )

        self.assertEqual(
            result,
            ToolExecutionResult(
                True,
                "Renamed nest/old name.txt to other/new name.txt.",
                {
                    "source": str(source.resolve()),
                    "destination": str(destination.resolve()),
                },
            ),
        )
        self.assertEqual(destination.read_text(encoding="utf-8"), "preserved")

    def test_nested_move_success_displays_both_relative_paths(self) -> None:
        source = self.root / "inbox" / "Customer Order.txt"
        destination = self.root / "archive" / "processed" / "Customer Order.txt"
        source.parent.mkdir()
        source.write_text("order", encoding="utf-8")

        result = self.tool.execute(
            "move",
            {
                "source": "inbox/Customer Order.txt",
                "destination": "archive/processed/Customer Order.txt",
            },
        )

        self.assertEqual(
            result,
            ToolExecutionResult(
                True,
                "Moved inbox/Customer Order.txt to "
                "archive/processed/Customer Order.txt.",
                {
                    "source": str(source.resolve()),
                    "destination": str(destination.resolve()),
                },
            ),
        )
        self.assertNotIn(str(self.tool.root_path), result.message)
        self.assertEqual(destination.read_text(encoding="utf-8"), "order")

    def test_successful_data_fields_remain_canonical_absolute_paths(self) -> None:
        path = self.root / "nested" / "inside.txt"
        path.parent.mkdir()
        path.write_text("inside", encoding="utf-8")

        result = self.tool.execute("read", {"path": "nested/inside.txt"})

        self.assertEqual(result.data, {"path": str(path.resolve())})
        self.assertTrue(Path(result.data["path"]).is_absolute())

    def test_outside_root_rejection_stays_generic_and_hides_the_path(self) -> None:
        outside = self.temporary_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")

        for value in (str(outside.resolve()), "../outside.txt"):
            with self.subTest(path=value):
                result = self.tool.execute("read", {"path": value})
                self.assertEqual(
                    result,
                    ToolExecutionResult(
                        False,
                        f"File read failed: {PATH_OUTSIDE_ROOT_MESSAGE}",
                    ),
                )
                self.assertNotIn(str(outside.resolve()), result.message)
                self.assertNotIn(str(self.tool.root_path), result.message)

    def test_display_helper_fallback_never_exposes_the_safe_root(self) -> None:
        escaped = (self.temporary_path / "elsewhere.txt").resolve()

        displayed = self.tool._display_path(escaped)

        self.assertEqual(displayed, UNCONTAINED_DISPLAY)
        self.assertNotIn(str(self.tool.root_path), displayed)
        self.assertNotIn(str(escaped), displayed)

    def test_empty_path_keeps_its_controlled_value_error_message(self) -> None:
        result = self.tool.execute("read", {"path": "   "})

        self.assertEqual(
            result,
            ToolExecutionResult(False, f"File read failed: {PATH_REQUIRED_MESSAGE}"),
        )


class FilesystemFailureSanitizationTests(unittest.TestCase):
    """Prove failure reasons never carry exception-supplied host paths."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name)
        self.root = self.temporary_path / "safe root"
        self.root.mkdir()
        self.logger = Mock(spec=logging.Logger)
        self.tool = FilesystemTool(self.root, logger=self.logger)
        self.source = self.root / "source.txt"
        self.source.write_text("source", encoding="utf-8")
        self.directory = self.root / "listing"
        self.directory.mkdir()

    def _poisoned_error(self) -> OSError:
        """Build an OSError whose every text field carries an absolute path."""
        return OSError(
            errno.EACCES,
            f"strerror leaking {self.tool.root_path}",
            str(self.source.resolve()),
            None,
            str((self.root / "destination.txt").resolve()),
        )

    def _assert_sanitized(self, message: str, prefix: str) -> None:
        """Assert a message keeps only the errno-derived reason."""
        error = self._poisoned_error()
        self.assertEqual(message, f"{prefix}: {os.strerror(errno.EACCES)}")
        self.assertNotIn(str(self.tool.root_path), message)
        self.assertNotIn(error.strerror, message)
        self.assertNotIn(error.filename, message)
        self.assertNotIn(error.filename2, message)

    def test_every_action_reports_only_the_errno_derived_reason(self) -> None:
        cases = (
            (
                "read",
                {"path": "source.txt"},
                "File read failed",
                lambda exc: patch.object(Path, "read_text", side_effect=exc),
            ),
            (
                "list",
                {"path": "listing"},
                "Directory listing failed",
                lambda exc: patch.object(Path, "iterdir", side_effect=exc),
            ),
            (
                "rename",
                {"source": "source.txt", "destination": "renamed.txt"},
                "Rename failed",
                lambda exc: patch.object(Path, "rename", side_effect=exc),
            ),
            (
                "move",
                {"source": "source.txt", "destination": "moved.txt"},
                "Move failed",
                lambda exc: patch.object(
                    filesystem_module.shutil,
                    "move",
                    side_effect=exc,
                ),
            ),
        )

        for action, arguments, prefix, patcher in cases:
            with self.subTest(action=action):
                with patcher(self._poisoned_error()):
                    result = self.tool.execute(action, arguments)

                self.assertFalse(result.success)
                self._assert_sanitized(result.message, prefix)

    def test_oserror_without_a_usable_errno_uses_the_generic_fallback(self) -> None:
        blocked = OSError(f"no errno but leaking {self.tool.root_path}")
        self.assertIsNone(blocked.errno)

        with patch.object(Path, "read_text", side_effect=blocked):
            result = self.tool.execute("read", {"path": "source.txt"})

        self.assertEqual(
            result,
            ToolExecutionResult(
                False,
                f"File read failed: {UNSPECIFIED_FAILURE_REASON}",
            ),
        )
        self.assertNotIn(str(self.tool.root_path), result.message)

    def test_shutil_error_with_absolute_paths_uses_the_generic_fallback(self) -> None:
        destination = self.root / "nested" / "destination.txt"
        failure = shutil.Error(
            f"Cannot move a directory '{self.source.resolve()}' "
            f"into itself '{destination.resolve()}'."
        )
        self.assertIsInstance(failure, OSError)
        self.assertIsNone(failure.errno)

        with patch.object(filesystem_module.shutil, "move", side_effect=failure):
            result = self.tool.execute(
                "move",
                {"source": "source.txt", "destination": "nested/destination.txt"},
            )

        self.assertEqual(
            result,
            ToolExecutionResult(
                False,
                f"Move failed: {UNSPECIFIED_FAILURE_REASON}",
            ),
        )
        self.assertNotIn(str(self.tool.root_path), result.message)

    def test_unexpected_value_error_uses_the_generic_fallback(self) -> None:
        leaking = ValueError(f"unexpected failure at {self.source.resolve()}")

        with patch.object(Path, "read_text", side_effect=leaking):
            result = self.tool.execute("read", {"path": "source.txt"})

        self.assertEqual(
            result,
            ToolExecutionResult(
                False,
                f"File read failed: {UNSPECIFIED_FAILURE_REASON}",
            ),
        )
        self.assertNotIn(str(self.tool.root_path), result.message)

    def test_controlled_value_error_messages_are_preserved(self) -> None:
        cases = (
            ({"path": ""}, PATH_REQUIRED_MESSAGE),
            ({"path": "../escape.txt"}, PATH_OUTSIDE_ROOT_MESSAGE),
        )

        for arguments, expected in cases:
            with self.subTest(path=arguments["path"]):
                result = self.tool.execute("read", arguments)
                self.assertEqual(
                    result,
                    ToolExecutionResult(False, f"File read failed: {expected}"),
                )

    def test_safe_path_raises_exactly_the_controlled_messages(self) -> None:
        with self.assertRaises(ValueError) as required:
            self.tool._safe_path("   ")
        with self.assertRaises(ValueError) as outside:
            self.tool._safe_path("../escape.txt")

        self.assertEqual(str(required.exception), PATH_REQUIRED_MESSAGE)
        self.assertEqual(str(outside.exception), PATH_OUTSIDE_ROOT_MESSAGE)

    def test_temporary_log_keeps_diagnostics_the_response_no_longer_shows(self) -> None:
        log_directory = self.temporary_path / "logs"
        log_directory.mkdir()
        log_file = log_directory / "maria-test.log"
        file_logger = logging.getLogger("maria-filesystem-sanitization-test")
        file_logger.setLevel(logging.INFO)
        file_logger.propagate = False
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
        file_logger.addHandler(handler)
        self.addCleanup(file_logger.removeHandler, handler)
        self.addCleanup(handler.close)
        tool = FilesystemTool(self.root, logger=file_logger)
        error = self._poisoned_error()

        with patch.object(filesystem_module.shutil, "move", side_effect=error):
            result = tool.execute(
                "move",
                {"source": "source.txt", "destination": "destination.txt"},
            )

        handler.flush()
        logged = log_file.read_text(encoding="utf-8")

        self.assertEqual(
            result.message,
            f"Move failed: {os.strerror(errno.EACCES)}",
        )
        self.assertNotIn(str(tool.root_path), result.message)
        self.assertIn(str(tool.root_path), logged)
        # OSError renders filenames through repr(), so the log holds the
        # escaped form of each absolute path.
        self.assertIn(repr(error.filename), logged)
        self.assertIn(repr(error.filename2), logged)


if __name__ == "__main__":
    unittest.main()
