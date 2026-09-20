"""Focused regression tests for Maria's safe filesystem tool."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterator
from unittest.mock import Mock, patch

import core.filesystem_roots as filesystem_roots_module
import tools.filesystem as filesystem_module
from core.config import MariaConfig
from core.constants import (
    DEFAULT_TEXT_READ_LIMIT_BYTES,
    PROJECT_ROOT,
    REPOSITORY_ROOT,
)
from core.filesystem_roots import (
    FilesystemProtections,
    FilesystemRoot,
    FilesystemRoots,
    RootPolicy,
)
from core.tool_manager import ToolExecutionResult
from tools.filesystem import (
    CROSS_ROOT_MESSAGE,
    PATH_EXCLUDED_MESSAGE,
    PATH_OUTSIDE_ROOT_MESSAGE,
    PATH_READ_ONLY_ROOT_MESSAGE,
    PATH_REQUIRED_MESSAGE,
    UNCONTAINED_DISPLAY,
    UNSPECIFIED_FAILURE_REASON,
    FilesystemTool,
    build_tool,
)


def organizing_roots(
    path: Path,
    name: str = "workspace",
) -> FilesystemRoots:
    """Return one enabled organizing root at a temporary directory."""
    return FilesystemRoots(
        (
            FilesystemRoot(
                name=name,
                path=path,
                policy=RootPolicy.ORGANIZE,
                enabled=True,
            ),
        ),
        name,
    )


def unreachable_protections(path: Path) -> FilesystemProtections:
    """Return active runtime locations no test path can collide with.

    The three locations sit in a sibling directory that no fixture places a
    root over, so a test exercising something other than active-runtime
    protection is unaffected by it.
    """
    runtime = path.parent / "maria-runtime"
    return FilesystemProtections(
        database_path=runtime / "database" / "maria.db",
        log_dir=runtime / "logs",
        prompt_path=runtime / "prompts" / "system_prompt.txt",
    )


def link_directory(
    link: Path,
    target: Path,
    prefer_symlink: bool = False,
) -> None:
    """Create a directory link, using a junction on Windows.

    A junction needs no elevated privilege, so a link regression runs on an
    ordinary Windows host rather than becoming a routine skip. When a real
    symbolic link is wanted the attempt is made first and a junction is used
    only if the host refuses the privilege; both are reparse points and
    ``Path.resolve`` follows either the same way. A failure with no fallback
    is raised with its exact return code instead of being skipped.
    """
    if prefer_symlink:
        try:
            link.symlink_to(target, target_is_directory=True)
            return
        except (NotImplementedError, OSError):
            if os.name != "nt":
                raise
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise OSError(
                f"mklink /J returned {completed.returncode}: "
                f"{completed.stdout.strip()} {completed.stderr.strip()}"
            )
        return
    link.symlink_to(target, target_is_directory=True)


@contextmanager
def repository_component_scope(root: Path) -> Iterator[None]:
    """Treat one temporary directory as the repository for component rules.

    Only the root model's view moves. The general filesystem barrier keeps
    pointing at the real repository, so a test can exercise a repository-scoped
    component rule such as ``.claude`` on its own, without the barrier
    refusing the whole directory first.
    """
    with patch.object(filesystem_roots_module, "REPOSITORY_ROOT", root):
        yield


@contextmanager
def replica_repository(root: Path) -> Iterator[None]:
    """Treat one temporary directory as Maria's repository entirely.

    Both the root model's view and the general filesystem barrier move, along
    with the two exclusion tables settled from the real project layout at
    import time. That lets a test build real directories, links, and junctions
    for protected repository identities without going anywhere near the
    owner's own repository.
    """
    project = root / "Maria"
    with patch.multiple(
        filesystem_roots_module,
        REPOSITORY_ROOT=root,
        UNIVERSAL_EXCLUDED_SUBTREES=(
            project / "database",
            project / "logs",
            root / ".git",
        ),
        UNIVERSAL_EXCLUDED_FILES=(project / "config" / "config.local.toml",),
    ), patch.object(filesystem_module, "REPOSITORY_ROOT", root):
        yield


class FilesystemToolTests(unittest.TestCase):
    """Exercise filesystem behavior only inside a temporary safe root."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name)
        self.root = self.temporary_path / "safe root"
        self.root.mkdir()
        self.root_path = self.root.resolve()
        self.roots = organizing_roots(self.root)
        self.logger = Mock(spec=logging.Logger)
        self.protections = unreachable_protections(self.root)
        self.tool = FilesystemTool(
            self.roots,
            self.protections,
            logger=self.logger,
        )

    def test_tool_identity_supported_actions_and_factory_contract(self) -> None:
        config = MariaConfig(
            app_name="Maria",
            version="0.1.0",
            environment="test",
            database_path=self.temporary_path / "db" / "maria.db",
            log_dir=self.temporary_path / "logs",
            prompt_path=self.temporary_path / "prompt.txt",
            filesystem_roots=self.roots,
            filesystem_protections=self.protections,
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
        self.assertEqual(factory_tool.roots, self.roots)
        self.assertEqual(factory_tool.protections, self.protections)
        self.assertEqual(factory_tool.roots.default_root.path, self.root_path)

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
            ToolExecutionResult(False, "File exceeds Maria's text-read limit."),
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
        self.root_path = self.root.resolve()
        self.roots = organizing_roots(self.root)
        self.logger = Mock(spec=logging.Logger)
        self.protections = unreachable_protections(self.root)
        self.tool = FilesystemTool(
            self.roots,
            self.protections,
            logger=self.logger,
        )

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
        self.assertNotIn(str(self.root_path), result.message)

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
        self.assertNotIn(str(self.root_path), result.message)
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
                self.assertNotIn(str(self.root_path), result.message)

    def test_display_helper_fallback_never_exposes_the_safe_root(self) -> None:
        escaped = (self.temporary_path / "elsewhere.txt").resolve()

        displayed = self.tool._display_path(escaped, self.roots.default_root)

        self.assertEqual(displayed, UNCONTAINED_DISPLAY)
        self.assertNotIn(str(self.root_path), displayed)
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
        self.root_path = self.root.resolve()
        self.roots = organizing_roots(self.root)
        self.logger = Mock(spec=logging.Logger)
        self.protections = unreachable_protections(self.root)
        self.tool = FilesystemTool(
            self.roots,
            self.protections,
            logger=self.logger,
        )
        self.source = self.root / "source.txt"
        self.source.write_text("source", encoding="utf-8")
        self.directory = self.root / "listing"
        self.directory.mkdir()

    def _poisoned_error(self) -> OSError:
        """Build an OSError whose every text field carries an absolute path."""
        return OSError(
            errno.EACCES,
            f"strerror leaking {self.root_path}",
            str(self.source.resolve()),
            None,
            str((self.root / "destination.txt").resolve()),
        )

    def _assert_sanitized(self, message: str, prefix: str) -> None:
        """Assert a message keeps only the errno-derived reason."""
        error = self._poisoned_error()
        self.assertEqual(message, f"{prefix}: {os.strerror(errno.EACCES)}")
        self.assertNotIn(str(self.root_path), message)
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
        blocked = OSError(f"no errno but leaking {self.root_path}")
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
        self.assertNotIn(str(self.root_path), result.message)

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
        self.assertNotIn(str(self.root_path), result.message)

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
        self.assertNotIn(str(self.root_path), result.message)

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
        tool = FilesystemTool(
            self.roots,
            self.protections,
            logger=file_logger,
        )
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
        self.assertNotIn(str(self.root_path), result.message)
        self.assertIn(str(self.root_path), logged)
        # OSError renders filenames through repr(), so the log holds the
        # escaped form of each absolute path.
        self.assertIn(repr(error.filename), logged)
        self.assertIn(repr(error.filename2), logged)


class FilesystemRootPolicyTests(unittest.TestCase):
    """Exercise named-root selection, per-root policy, and cross-root refusal."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_path / "workspace"
        self.second = self.temporary_path / "second"
        self.reference = self.temporary_path / "reference"
        self.inert = self.temporary_path / "inert"
        for directory in (self.workspace, self.second, self.reference, self.inert):
            directory.mkdir()
        self.logger = Mock(spec=logging.Logger)
        self.roots = FilesystemRoots(
            (
                FilesystemRoot(
                    name="workspace",
                    path=self.workspace,
                    policy=RootPolicy.ORGANIZE,
                    enabled=True,
                ),
                FilesystemRoot(
                    name="second_workspace",
                    path=self.second,
                    policy=RootPolicy.ORGANIZE,
                    enabled=True,
                ),
                FilesystemRoot(
                    name="test_read_only",
                    path=self.reference,
                    policy=RootPolicy.READ_ONLY,
                    enabled=True,
                ),
                FilesystemRoot(
                    name="project_maria",
                    path=self.inert,
                    policy=RootPolicy.READ_ONLY,
                    enabled=False,
                ),
            ),
            "workspace",
        )
        self.protections = unreachable_protections(self.temporary_path)
        self.tool = FilesystemTool(
            self.roots,
            self.protections,
            logger=self.logger,
        )

    def test_a_relative_path_resolves_against_the_default_workspace(self) -> None:
        (self.workspace / "notes.txt").write_text("workspace", encoding="utf-8")
        (self.second / "notes.txt").write_text("second", encoding="utf-8")

        result = self.tool.execute("read", {"path": "notes.txt"})

        self.assertTrue(result.success)
        self.assertEqual(result.message, "workspace")
        self.assertEqual(
            result.data,
            {"path": str(self.workspace / "notes.txt")},
        )

    def test_an_absolute_path_in_another_enabled_root_is_accepted(self) -> None:
        target = self.second / "report.txt"
        target.write_text("second root", encoding="utf-8")

        result = self.tool.execute("read", {"path": str(target)})

        self.assertTrue(result.success)
        self.assertEqual(result.message, "second root")

    def test_an_absolute_path_outside_every_root_is_refused(self) -> None:
        outside = self.temporary_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")

        result = self.tool.execute("read", {"path": str(outside)})

        self.assertEqual(
            result,
            ToolExecutionResult(
                False,
                f"File read failed: {PATH_OUTSIDE_ROOT_MESSAGE}",
            ),
        )

    def test_a_disabled_root_reads_as_outside_maria_s_roots(self) -> None:
        target = self.inert / "private.txt"
        target.write_text("inert", encoding="utf-8")

        read = self.tool.execute("read", {"path": str(target)})
        listed = self.tool.execute("list", {"path": str(self.inert)})

        self.assertEqual(
            read,
            ToolExecutionResult(
                False,
                f"File read failed: {PATH_OUTSIDE_ROOT_MESSAGE}",
            ),
        )
        self.assertEqual(
            listed,
            ToolExecutionResult(
                False,
                f"Directory listing failed: {PATH_OUTSIDE_ROOT_MESSAGE}",
            ),
        )
        for result in (read, listed):
            self.assertNotIn("project_maria", result.message)
            self.assertNotIn(str(self.inert), result.message)

    def test_reading_and_listing_work_in_an_organizing_root(self) -> None:
        (self.workspace / "inbox").mkdir()
        (self.workspace / "inbox" / "order.txt").write_text("o", encoding="utf-8")

        read = self.tool.execute("read", {"path": "inbox/order.txt"})
        listed = self.tool.execute("list", {"path": "inbox"})

        self.assertTrue(read.success)
        self.assertEqual(listed.message, "file: order.txt")

    def test_reading_and_listing_work_in_a_read_only_root(self) -> None:
        target = self.reference / "manual.txt"
        target.write_text("read only", encoding="utf-8")

        read = self.tool.execute("read", {"path": str(target)})
        listed = self.tool.execute("list", {"path": str(self.reference)})

        self.assertTrue(read.success)
        self.assertEqual(read.message, "read only")
        self.assertEqual(listed.message, "file: manual.txt")

    def test_rename_and_move_are_refused_inside_a_read_only_root(self) -> None:
        source = self.reference / "manual.txt"
        source.write_text("read only", encoding="utf-8")
        destination = self.reference / "renamed.txt"

        for action in ("rename", "move"):
            with self.subTest(action=action):
                result = self.tool.execute(
                    action,
                    {"source": str(source), "destination": str(destination)},
                )
                self.assertEqual(
                    result.message,
                    f"{action.capitalize()} failed: {PATH_READ_ONLY_ROOT_MESSAGE}",
                )
                self.assertTrue(source.is_file())
                self.assertFalse(destination.exists())

    def test_a_read_only_destination_is_refused_for_an_organizing_source(
        self,
    ) -> None:
        source = self.workspace / "note.txt"
        source.write_text("organize", encoding="utf-8")
        destination = self.reference / "note.txt"

        result = self.tool.execute(
            "move",
            {"source": "note.txt", "destination": str(destination)},
        )

        self.assertEqual(
            result.message,
            f"Move failed: {PATH_READ_ONLY_ROOT_MESSAGE}",
        )
        self.assertTrue(source.is_file())
        self.assertFalse(destination.exists())

    def test_a_cross_root_rename_or_move_is_refused_between_organizing_roots(
        self,
    ) -> None:
        source = self.workspace / "note.txt"
        source.write_text("organize", encoding="utf-8")
        destination = self.second / "note.txt"

        for action in ("rename", "move"):
            with self.subTest(action=action):
                result = self.tool.execute(
                    action,
                    {"source": "note.txt", "destination": str(destination)},
                )
                self.assertEqual(
                    result.message,
                    f"{action.capitalize()} failed: {CROSS_ROOT_MESSAGE}",
                )
                self.assertTrue(source.is_file())
                self.assertFalse(destination.exists())
                self.assertNotIn("workspace", result.message)
                self.assertNotIn("second_workspace", result.message)

    def test_a_move_still_creates_a_missing_destination_parent_inside_one_root(
        self,
    ) -> None:
        source = self.workspace / "note.txt"
        source.write_text("organize", encoding="utf-8")

        result = self.tool.execute(
            "move",
            {"source": "note.txt", "destination": "archive/2026/note.txt"},
        )

        self.assertTrue(result.success)
        self.assertEqual(
            (self.workspace / "archive" / "2026" / "note.txt").read_text(
                encoding="utf-8"
            ),
            "organize",
        )

    def test_workspace_names_the_repository_would_exclude_remain_usable(
        self,
    ) -> None:
        for directory in ("env", "venv", ".venv", ".claude", "__pycache__"):
            (self.workspace / directory).mkdir()
            (self.workspace / directory / "file.txt").write_text(
                directory,
                encoding="utf-8",
            )
        for leaf in ("notes.tmp", "archive.bak", ".env"):
            (self.workspace / leaf).write_text(leaf, encoding="utf-8")

        listed = self.tool.execute("list", {"path": "."})

        self.assertTrue(listed.success)
        listed_names = {entry["name"] for entry in listed.data}
        self.assertEqual(
            listed_names,
            {
                "env",
                "venv",
                ".venv",
                ".claude",
                "__pycache__",
                "notes.tmp",
                "archive.bak",
                ".env",
            },
        )

        for path in (".claude/file.txt", "env/file.txt", "notes.tmp", ".env"):
            with self.subTest(path=path):
                read = self.tool.execute("read", {"path": path})
                self.assertTrue(read.success)

    @unittest.skipUnless(os.name == "nt", "Windows path casing only.")
    def test_an_alternate_windows_case_is_canonicalised(self) -> None:
        (self.workspace / "Report.txt").write_text("cased", encoding="utf-8")

        read = self.tool.execute("read", {"path": "REPORT.TXT"})
        missing = self.tool.execute("read", {"path": "NESTED/MISSING.TXT"})

        self.assertTrue(read.success)
        self.assertEqual(read.message, "cased")
        self.assertEqual(
            read.data,
            {"path": str(self.workspace / "Report.txt")},
        )
        self.assertFalse(missing.success)
        self.assertTrue(missing.message.startswith("Not a file: "))


class FilesystemRepositoryBarrierTests(unittest.TestCase):
    """Prove the generic commands can never reach Maria's own repository."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.logger = Mock(spec=logging.Logger)

    def _repository_tool(self, root: Path) -> FilesystemTool:
        """Return a tool whose synthetic root really does contain the repo."""
        roots = FilesystemRoots(
            (
                FilesystemRoot(
                    name="second_workspace",
                    path=root,
                    policy=RootPolicy.ORGANIZE,
                    enabled=True,
                ),
            ),
            "second_workspace",
        )
        return FilesystemTool(
            roots,
            unreachable_protections(self.temporary_path),
            logger=self.logger,
        )

    def test_every_repository_location_is_refused_through_a_broad_root(
        self,
    ) -> None:
        tool = self._repository_tool(REPOSITORY_ROOT)
        cases = {
            "repository_root": str(REPOSITORY_ROOT),
            "source": str(PROJECT_ROOT / "core" / "brain.py"),
            "documentation": str(PROJECT_ROOT / "docs" / "architecture.md"),
            "prompt": str(PROJECT_ROOT / "prompts" / "system_prompt.txt"),
            "configuration": str(PROJECT_ROOT / "config" / "config.toml"),
            "untracked": str(PROJECT_ROOT / "an-untracked-scratch-file.txt"),
            "relative": "Maria/core/brain.py",
        }

        for label, path in cases.items():
            with self.subTest(case=label):
                read = tool.execute("read", {"path": path})
                listed = tool.execute("list", {"path": path})
                self.assertEqual(
                    read.message,
                    f"File read failed: {PATH_EXCLUDED_MESSAGE}",
                )
                self.assertEqual(
                    listed.message,
                    f"Directory listing failed: {PATH_EXCLUDED_MESSAGE}",
                )

    def test_a_root_declared_above_the_repository_cannot_override_the_barrier(
        self,
    ) -> None:
        tool = self._repository_tool(REPOSITORY_ROOT.parent)

        result = tool.execute(
            "read",
            {"path": str(PROJECT_ROOT / "core" / "brain.py")},
        )

        self.assertEqual(
            result.message,
            f"File read failed: {PATH_EXCLUDED_MESSAGE}",
        )

    def test_rename_and_move_are_refused_on_either_repository_operand(self) -> None:
        workspace = self.temporary_path / "workspace"
        workspace.mkdir()
        replica = self.temporary_path / "repository"
        replica.mkdir()
        source = workspace / "note.txt"
        source.write_text("note", encoding="utf-8")
        roots = FilesystemRoots(
            (
                FilesystemRoot(
                    name="workspace",
                    path=self.temporary_path,
                    policy=RootPolicy.ORGANIZE,
                    enabled=True,
                ),
            ),
            "workspace",
        )
        tool = FilesystemTool(
            roots,
            unreachable_protections(self.temporary_path),
            logger=self.logger,
        )

        with patch.object(filesystem_module, "REPOSITORY_ROOT", replica):
            destination = tool.execute(
                "move",
                {
                    "source": "workspace/note.txt",
                    "destination": "repository/note.txt",
                },
            )
            (replica / "tracked.txt").write_text("tracked", encoding="utf-8")
            origin = tool.execute(
                "rename",
                {
                    "source": "repository/tracked.txt",
                    "destination": "workspace/tracked.txt",
                },
            )

        self.assertEqual(
            destination.message,
            f"Move failed: {PATH_EXCLUDED_MESSAGE}",
        )
        self.assertEqual(
            origin.message,
            f"Rename failed: {PATH_EXCLUDED_MESSAGE}",
        )
        self.assertTrue(source.is_file())
        self.assertFalse((replica / "note.txt").exists())
        self.assertTrue((replica / "tracked.txt").is_file())

    def test_listing_an_allowed_parent_omits_the_repository_child(self) -> None:
        replica = self.temporary_path / "repository"
        replica.mkdir()
        (replica / "tracked.txt").write_text("tracked", encoding="utf-8")
        (self.temporary_path / "ordinary.txt").write_text("keep", encoding="utf-8")
        (self.temporary_path / "sibling").mkdir()
        roots = FilesystemRoots(
            (
                FilesystemRoot(
                    name="workspace",
                    path=self.temporary_path,
                    policy=RootPolicy.ORGANIZE,
                    enabled=True,
                ),
            ),
            "workspace",
        )
        tool = FilesystemTool(
            roots,
            unreachable_protections(self.temporary_path),
            logger=self.logger,
        )

        with patch.object(filesystem_module, "REPOSITORY_ROOT", replica):
            result = tool.execute("list", {"path": "."})

        self.assertTrue(result.success)
        self.assertEqual(
            result.data,
            [
                {"name": "ordinary.txt", "type": "file"},
                {"name": "sibling", "type": "directory"},
            ],
        )
        self.assertNotIn("repository", result.message)

    def test_a_barrier_refusal_reveals_no_location_root_or_exception_text(
        self,
    ) -> None:
        tool = self._repository_tool(REPOSITORY_ROOT)
        submitted = str(PROJECT_ROOT / "core" / "brain.py")

        result = tool.execute("read", {"path": submitted})

        self.assertEqual(
            result.message,
            f"File read failed: {PATH_EXCLUDED_MESSAGE}",
        )
        for forbidden in (
            submitted,
            str(REPOSITORY_ROOT),
            str(Path.home().resolve()),
            "second_workspace",
            "brain.py",
            ".claude",
            "0x",
            "object at",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, result.message)


class FilesystemChildListingTests(unittest.TestCase):
    """Prove each directory child is settled before it is ever described."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.root = self.temporary_path / "workspace"
        self.root.mkdir()
        self.outside = self.temporary_path / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("secret", encoding="utf-8")
        self.roots = organizing_roots(self.root)
        self.logger = Mock(spec=logging.Logger)
        self.protections = unreachable_protections(self.root)
        self.tool = FilesystemTool(
            self.roots,
            self.protections,
            logger=self.logger,
        )

    def test_a_junction_pointing_out_of_the_root_is_omitted(self) -> None:
        (self.root / "ordinary.txt").write_text("keep", encoding="utf-8")
        link_directory(self.root / "escape", self.outside)

        result = self.tool.execute("list", {"path": "."})

        self.assertTrue(result.success)
        self.assertEqual(result.data, [{"name": "ordinary.txt", "type": "file"}])
        self.assertNotIn("escape", result.message)
        self.assertNotIn("secret", result.message)

    def test_a_symbolic_link_pointing_out_of_the_root_is_omitted(self) -> None:
        (self.root / "ordinary.txt").write_text("keep", encoding="utf-8")
        link_directory(self.root / "escape-link", self.outside, prefer_symlink=True)

        result = self.tool.execute("list", {"path": "."})

        self.assertTrue(result.success)
        self.assertEqual(result.data, [{"name": "ordinary.txt", "type": "file"}])

    def test_a_junction_into_an_excluded_location_is_omitted(self) -> None:
        excluded = self.root / "database"
        excluded.mkdir()
        (excluded / "maria.db").write_text("db", encoding="utf-8")
        (self.root / "ordinary.txt").write_text("keep", encoding="utf-8")
        link_directory(self.root / "db-link", excluded)

        with patch.object(
            filesystem_roots_module,
            "UNIVERSAL_EXCLUDED_SUBTREES",
            (excluded,),
        ):
            result = self.tool.execute("list", {"path": "."})

        self.assertTrue(result.success)
        self.assertEqual(result.data, [{"name": "ordinary.txt", "type": "file"}])
        self.assertNotIn("db-link", result.message)
        self.assertNotIn("database", result.message)

    def test_an_assistant_state_child_is_omitted_inside_the_repository(self) -> None:
        (self.root / ".claude").mkdir()
        (self.root / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        (self.root / "ordinary.txt").write_text("keep", encoding="utf-8")

        with repository_component_scope(self.root):
            result = self.tool.execute("list", {"path": "."})

        self.assertTrue(result.success)
        self.assertEqual(result.data, [{"name": "ordinary.txt", "type": "file"}])
        self.assertNotIn(".claude", result.message)

    def test_a_filtered_empty_listing_keeps_the_existing_success_message(
        self,
    ) -> None:
        link_directory(self.root / "escape", self.outside)

        result = self.tool.execute("list", {"path": "."})

        self.assertEqual(
            result,
            ToolExecutionResult(True, "Directory is empty.", []),
        )

    def test_an_omitted_child_is_never_classified(self) -> None:
        (self.root / "ordinary.txt").write_text("keep", encoding="utf-8")
        link_directory(self.root / "escape", self.outside)
        original_is_dir = Path.is_dir
        forbidden = self.outside

        def guarded_is_dir(self: Path, *args: Any, **kwargs: Any) -> bool:
            if self == forbidden:
                raise AssertionError("an omitted child was classified")
            return original_is_dir(self, *args, **kwargs)

        with patch.object(Path, "is_dir", guarded_is_dir):
            result = self.tool.execute("list", {"path": "."})

        self.assertTrue(result.success)
        self.assertEqual(result.data, [{"name": "ordinary.txt", "type": "file"}])

    def test_an_unexpected_child_resolution_error_fails_the_whole_listing(
        self,
    ) -> None:
        (self.root / "ordinary.txt").write_text("keep", encoding="utf-8")
        (self.root / "broken.txt").write_text("broken", encoding="utf-8")
        original_resolve = Path.resolve

        def failing_resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
            if self.name == "broken.txt":
                raise OSError(errno.EACCES, f"blocked {self}")
            return original_resolve(self, *args, **kwargs)

        with patch.object(Path, "resolve", failing_resolve):
            result = self.tool.execute("list", {"path": "."})

        self.assertEqual(
            result,
            ToolExecutionResult(
                False,
                f"Directory listing failed: {os.strerror(errno.EACCES)}",
            ),
        )
        self.assertNotIn("broken.txt", result.message)
        self.assertNotIn(str(self.root), result.message)

    def test_an_unexpected_metadata_error_fails_the_whole_listing(self) -> None:
        (self.root / "ordinary.txt").write_text("keep", encoding="utf-8")
        original_is_dir = Path.is_dir
        target = (self.root / "ordinary.txt").resolve()

        def failing_is_dir(self: Path, *args: Any, **kwargs: Any) -> bool:
            if self == target:
                raise OSError(errno.EACCES, f"blocked {self}")
            return original_is_dir(self, *args, **kwargs)

        with patch.object(Path, "is_dir", failing_is_dir):
            result = self.tool.execute("list", {"path": "."})

        self.assertEqual(
            result,
            ToolExecutionResult(
                False,
                f"Directory listing failed: {os.strerror(errno.EACCES)}",
            ),
        )
        self.assertNotIn("ordinary.txt", result.message)

    def test_ordinary_siblings_survive_every_omission(self) -> None:
        for name in ("alpha.txt", "beta.txt"):
            (self.root / name).write_text(name, encoding="utf-8")
        (self.root / "Gamma").mkdir()
        link_directory(self.root / "escape", self.outside)

        result = self.tool.execute("list", {"path": "."})

        self.assertEqual(
            result.message,
            "file: alpha.txt\nfile: beta.txt\ndirectory: Gamma",
        )


class FilesystemFixedMessageTests(unittest.TestCase):
    """Lock the exact wording of every fixed filesystem message."""

    def test_the_three_new_messages_are_byte_exact(self) -> None:
        self.assertEqual(
            PATH_EXCLUDED_MESSAGE,
            "Path is permanently excluded from Maria's filesystem access.",
        )
        self.assertEqual(
            PATH_READ_ONLY_ROOT_MESSAGE,
            "This filesystem root is read-only.",
        )
        self.assertEqual(
            CROSS_ROOT_MESSAGE,
            "Source and destination must be inside the same filesystem root.",
        )

    def test_the_existing_outside_root_message_is_unchanged(self) -> None:
        self.assertEqual(
            PATH_OUTSIDE_ROOT_MESSAGE,
            "Path is outside Maria's configured safe root.",
        )

    def test_no_fixed_message_interpolates_anything(self) -> None:
        for message in (
            PATH_REQUIRED_MESSAGE,
            PATH_OUTSIDE_ROOT_MESSAGE,
            PATH_EXCLUDED_MESSAGE,
            PATH_READ_ONLY_ROOT_MESSAGE,
            CROSS_ROOT_MESSAGE,
        ):
            with self.subTest(message=message):
                self.assertNotIn("{", message)
                self.assertNotIn("%", message)

    def test_the_four_action_prefixes_are_unchanged(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve() / "workspace"
            root.mkdir()
            tool = FilesystemTool(
                organizing_roots(root),
                unreachable_protections(root),
                logger=Mock(spec=logging.Logger),
            )
            cases = {
                "File read failed: ": ("read", {"path": "../escape.txt"}),
                "Directory listing failed: ": ("list", {"path": "../escape"}),
                "Rename failed: ": (
                    "rename",
                    {"source": "../a.txt", "destination": "b.txt"},
                ),
                "Move failed: ": (
                    "move",
                    {"source": "../a.txt", "destination": "b.txt"},
                ),
            }

            for prefix, (action, arguments) in cases.items():
                with self.subTest(action=action):
                    result = tool.execute(action, arguments)
                    self.assertTrue(result.message.startswith(prefix))
                    self.assertEqual(
                        result.message,
                        f"{prefix}{PATH_OUTSIDE_ROOT_MESSAGE}",
                    )


class FilesystemActiveRuntimeProtectionTests(unittest.TestCase):
    """Prove the live database, log, and prompt stay unreachable in a root."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_path / "MariaWorkspace"
        self.state = self.workspace / "state"
        self.state.mkdir(parents=True)

        # Every active runtime location is deliberately configured inside the
        # enabled workspace, which is exactly the configuration the fixed
        # source-layout exclusions alone would hand straight back out.
        self.database = self.state / "maria.db"
        self.log_dir = self.state / "logs"
        self.prompt = self.state / "system_prompt.txt"
        self.log_dir.mkdir()
        self.database.write_text("SQLITE-PRIVATE", encoding="utf-8")
        self.prompt.write_text("PROMPT-PRIVATE", encoding="utf-8")
        for name in ("maria.log", "maria.log.1", "maria.log.2", "maria.log.3"):
            (self.log_dir / name).write_text(f"LOG-PRIVATE-{name}", encoding="utf-8")

        self.protections = FilesystemProtections(
            database_path=self.database,
            log_dir=self.log_dir,
            prompt_path=self.prompt,
        )
        self.roots = organizing_roots(self.workspace)
        self.logger = Mock(spec=logging.Logger)
        self.tool = FilesystemTool(self.roots, self.protections, logger=self.logger)
        (self.workspace / "notes.txt").write_text("ordinary", encoding="utf-8")

    def _assert_refused(self, action: str, arguments: dict[str, str]) -> None:
        """Assert one action reports exactly the fixed exclusion refusal."""
        prefixes = {
            "read": "File read failed",
            "list": "Directory listing failed",
            "rename": "Rename failed",
            "move": "Move failed",
        }
        result = self.tool.execute(action, arguments)
        self.assertEqual(
            result.message,
            f"{prefixes[action]}: {PATH_EXCLUDED_MESSAGE}",
        )
        self.assertIsNone(result.data)

    def test_the_active_database_is_refused_by_every_action(self) -> None:
        cases = (
            ("read", {"path": "state/maria.db"}),
            ("list", {"path": "state/maria.db"}),
            ("rename", {"source": "state/maria.db", "destination": "copy.db"}),
            ("rename", {"source": "notes.txt", "destination": "state/maria.db"}),
            ("move", {"source": "state/maria.db", "destination": "copy.db"}),
            ("move", {"source": "notes.txt", "destination": "state/maria.db"}),
        )

        for action, arguments in cases:
            with self.subTest(action=action, arguments=sorted(arguments)):
                self._assert_refused(action, arguments)

        self.assertEqual(
            self.database.read_text(encoding="utf-8"),
            "SQLITE-PRIVATE",
        )
        self.assertTrue((self.workspace / "notes.txt").is_file())

    def test_the_active_database_sidecars_are_refused(self) -> None:
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = self.database.with_name(f"maria.db{suffix}")
            sidecar.write_text(f"SIDECAR{suffix}", encoding="utf-8")
            with self.subTest(suffix=suffix):
                self._assert_refused("read", {"path": f"state/maria.db{suffix}"})
                self.assertTrue(sidecar.is_file())

    def test_the_active_log_directory_and_rotations_are_refused(self) -> None:
        self._assert_refused("list", {"path": "state/logs"})
        for name in ("maria.log", "maria.log.1", "maria.log.2", "maria.log.3"):
            with self.subTest(name=name):
                self._assert_refused("read", {"path": f"state/logs/{name}"})
        self._assert_refused(
            "move",
            {"source": "state/logs/maria.log", "destination": "copied.log"},
        )

    def test_the_active_system_prompt_is_refused(self) -> None:
        self._assert_refused("read", {"path": "state/system_prompt.txt"})
        self._assert_refused(
            "rename",
            {
                "source": "state/system_prompt.txt",
                "destination": "state/renamed.txt",
            },
        )
        self.assertEqual(self.prompt.read_text(encoding="utf-8"), "PROMPT-PRIVATE")

    def test_an_unrelated_neighbour_of_a_protected_file_stays_usable(self) -> None:
        (self.state / "maria.db.notes").write_text("notes", encoding="utf-8")
        (self.state / "other_prompt.txt").write_text("other", encoding="utf-8")

        for name in ("maria.db.notes", "other_prompt.txt"):
            with self.subTest(name=name):
                result = self.tool.execute("read", {"path": f"state/{name}"})
                self.assertTrue(result.success)

    def test_listing_a_parent_omits_every_active_runtime_child(self) -> None:
        (self.state / "keep.txt").write_text("keep", encoding="utf-8")
        (self.state / "archive").mkdir()

        result = self.tool.execute("list", {"path": "state"})

        self.assertTrue(result.success)
        self.assertEqual(
            result.data,
            [
                {"name": "archive", "type": "directory"},
                {"name": "keep.txt", "type": "file"},
            ],
        )
        for hidden in ("maria.db", "logs", "system_prompt.txt"):
            with self.subTest(hidden=hidden):
                self.assertNotIn(hidden, result.message)

    def test_a_fully_protected_directory_keeps_the_empty_message(self) -> None:
        protected_only = self.workspace / "only-runtime"
        protected_only.mkdir()
        protections = FilesystemProtections(
            database_path=protected_only / "maria.db",
            log_dir=protected_only / "logs",
            prompt_path=protected_only / "system_prompt.txt",
        )
        (protected_only / "maria.db").write_text("db", encoding="utf-8")
        (protected_only / "logs").mkdir()
        (protected_only / "system_prompt.txt").write_text("p", encoding="utf-8")
        tool = FilesystemTool(self.roots, protections, logger=self.logger)

        result = tool.execute("list", {"path": "only-runtime"})

        self.assertEqual(
            result,
            ToolExecutionResult(True, "Directory is empty.", []),
        )

    def test_a_broad_synthetic_root_cannot_override_active_protection(self) -> None:
        broad = FilesystemRoots(
            (
                FilesystemRoot(
                    name="second_workspace",
                    path=self.temporary_path,
                    policy=RootPolicy.ORGANIZE,
                    enabled=True,
                ),
            ),
            "second_workspace",
        )
        tool = FilesystemTool(broad, self.protections, logger=self.logger)

        for path in (self.database, self.prompt, self.log_dir / "maria.log"):
            with self.subTest(path=path.name):
                result = tool.execute("read", {"path": str(path)})
                self.assertEqual(
                    result.message,
                    f"File read failed: {PATH_EXCLUDED_MESSAGE}",
                )

    def test_a_refusal_leaks_neither_the_path_nor_the_content(self) -> None:
        self.logger.reset_mock()

        result = self.tool.execute("read", {"path": "state/logs/maria.log"})

        logged = " ".join(str(entry) for entry in self.logger.method_calls)
        for forbidden in (
            "maria.log",
            "maria.db",
            "system_prompt",
            "LOG-PRIVATE",
            "MariaWorkspace",
            str(self.workspace),
            "workspace",
            "0x",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, result.message)
                self.assertNotIn(forbidden, logged)

    def test_an_omitted_runtime_child_is_never_classified(self) -> None:
        original_is_dir = Path.is_dir
        forbidden = {self.database, self.log_dir, self.prompt}

        def guarded_is_dir(self: Path, *args: Any, **kwargs: Any) -> bool:
            if self in forbidden:
                raise AssertionError("a protected child was classified")
            return original_is_dir(self, *args, **kwargs)

        original_stat = Path.stat

        def guarded_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self in forbidden:
                raise AssertionError("a protected child was stat'ed")
            return original_stat(self, *args, **kwargs)

        (self.state / "keep.txt").write_text("keep", encoding="utf-8")

        with patch.object(Path, "is_dir", guarded_is_dir):
            with patch.object(Path, "stat", guarded_stat):
                result = self.tool.execute("list", {"path": "state"})

        self.assertTrue(result.success)
        self.assertEqual(result.data, [{"name": "keep.txt", "type": "file"}])


class FilesystemProtectedNameLinkTests(unittest.TestCase):
    """Prove a protected repository name cannot be reached by linking out."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_path / "MariaWorkspace"
        self.workspace.mkdir()
        self.escaped = self.workspace / "escaped"
        self.escaped.mkdir()
        (self.escaped / "private.txt").write_text("ESCAPED-PRIVATE", encoding="utf-8")
        self.replica = self.temporary_path / "repository"
        self.project = self.replica / "Maria"
        self.project.mkdir(parents=True)
        self.protections = unreachable_protections(self.temporary_path)
        self.logger = Mock(spec=logging.Logger)
        # One broad root deliberately spans the workspace and the replica, so
        # nothing here is refused merely for being outside a root.
        self.roots = FilesystemRoots(
            (
                FilesystemRoot(
                    name="second_workspace",
                    path=self.temporary_path,
                    policy=RootPolicy.ORGANIZE,
                    enabled=True,
                ),
            ),
            "second_workspace",
        )
        self.tool = FilesystemTool(self.roots, self.protections, logger=self.logger)

    def _assert_link_out_is_refused(self, link: Path, leaf: str) -> None:
        """Assert a protected name pointing out of the repository is refused."""
        link_directory(link, self.escaped)
        submitted = str(link / leaf)

        with replica_repository(self.replica):
            read = self.tool.execute("read", {"path": submitted})
            listed = self.tool.execute("list", {"path": str(link)})

        self.assertEqual(
            read.message,
            f"File read failed: {PATH_EXCLUDED_MESSAGE}",
        )
        self.assertEqual(
            listed.message,
            f"Directory listing failed: {PATH_EXCLUDED_MESSAGE}",
        )
        logged = " ".join(str(entry) for entry in self.logger.method_calls)
        for forbidden in ("escaped", "ESCAPED-PRIVATE", "private.txt"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, read.message)
                self.assertNotIn(forbidden, listed.message)
                self.assertNotIn(forbidden, logged)

    def test_the_default_log_directory_name_linking_out_is_refused(self) -> None:
        self._assert_link_out_is_refused(self.project / "logs", "private.txt")

    def test_the_default_database_directory_name_linking_out_is_refused(self) -> None:
        self._assert_link_out_is_refused(self.project / "database", "private.txt")

    def test_the_repository_git_name_linking_out_is_refused(self) -> None:
        self._assert_link_out_is_refused(self.replica / ".git", "private.txt")

    def test_the_repository_assistant_state_name_linking_out_is_refused(self) -> None:
        self._assert_link_out_is_refused(self.replica / ".claude", "private.txt")

    def test_the_local_configuration_name_linking_out_is_refused(self) -> None:
        (self.project / "config").mkdir(parents=True)
        elsewhere = self.workspace / "private.toml"
        elsewhere.write_text("secret = true", encoding="utf-8")
        local_config = self.project / "config" / "config.local.toml"
        try:
            local_config.symlink_to(elsewhere)
        except (NotImplementedError, OSError):
            local_config.write_text("secret = true", encoding="utf-8")

        with replica_repository(self.replica):
            result = self.tool.execute("read", {"path": str(local_config)})

        self.assertEqual(
            result.message,
            f"File read failed: {PATH_EXCLUDED_MESSAGE}",
        )
        self.assertNotIn("private.toml", result.message)

    def test_a_workspace_link_into_the_repository_is_refused(self) -> None:
        tracked = self.project / "core"
        tracked.mkdir()
        (tracked / "brain.py").write_text("SOURCE", encoding="utf-8")
        link_directory(self.workspace / "source-link", tracked)

        with replica_repository(self.replica):
            result = self.tool.execute(
                "read",
                {"path": "MariaWorkspace/source-link/brain.py"},
            )

        self.assertEqual(
            result.message,
            f"File read failed: {PATH_EXCLUDED_MESSAGE}",
        )
        self.assertNotIn("SOURCE", result.message)

    def test_a_linked_protected_child_is_omitted_from_its_parent_listing(
        self,
    ) -> None:
        (self.project / "notes.txt").write_text("ordinary", encoding="utf-8")
        link_directory(self.project / "logs", self.escaped)

        with replica_repository(self.replica), patch.object(
            filesystem_module,
            "REPOSITORY_ROOT",
            self.temporary_path / "no-repository-here",
        ):
            # The barrier is moved aside deliberately, so this proves the
            # universal half omits the linked protected name on its own.
            result = self.tool.execute("list", {"path": str(self.project)})

        self.assertTrue(result.success)
        self.assertEqual(result.data, [{"name": "notes.txt", "type": "file"}])
        self.assertNotIn("logs", result.message)

    def test_ordinary_workspace_names_beside_the_links_stay_reachable(self) -> None:
        (self.workspace / "notes.tmp").write_text("kept", encoding="utf-8")
        (self.workspace / ".claude").mkdir()
        (self.workspace / ".claude" / "settings.json").write_text(
            "{}",
            encoding="utf-8",
        )

        with replica_repository(self.replica):
            tmp = self.tool.execute(
                "read",
                {"path": "MariaWorkspace/notes.tmp"},
            )
            assistant = self.tool.execute(
                "read",
                {"path": "MariaWorkspace/.claude/settings.json"},
            )

        self.assertTrue(tmp.success)
        self.assertEqual(tmp.message, "kept")
        self.assertTrue(assistant.success)
        self.assertEqual(assistant.message, "{}")


class FilesystemProtectedMutationTests(unittest.TestCase):
    """Prove rename and move cannot carry a protected location out from under."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_path / "MariaWorkspace"

        # The database and the prompt live beneath one ordinary-looking
        # directory while the active log directory is somewhere else entirely.
        # Relocating that directory is a pure path operation on every
        # platform, so this regression does not depend on an open log handle
        # or on Windows refusing to move a file that is in use.
        self.state = self.workspace / "state"
        self.state.mkdir(parents=True)
        self.database = self.state / "maria.db"
        self.prompt = self.state / "system_prompt.txt"
        self.database.write_text("DB-PRIVATE", encoding="utf-8")
        self.prompt.write_text("PROMPT-PRIVATE", encoding="utf-8")

        self.telemetry = self.workspace / "telemetry"
        self.log_dir = self.telemetry / "logs"
        self.log_dir.mkdir(parents=True)
        (self.log_dir / "maria.log").write_text("LOG-PRIVATE", encoding="utf-8")

        self.ordinary = self.workspace / "ordinary"
        self.ordinary.mkdir()
        (self.ordinary / "note.txt").write_text("ordinary", encoding="utf-8")

        self.protections = FilesystemProtections(
            database_path=self.database,
            log_dir=self.log_dir,
            prompt_path=self.prompt,
        )
        self.roots = organizing_roots(self.workspace)
        self.logger = Mock(spec=logging.Logger)
        self.tool = FilesystemTool(self.roots, self.protections, logger=self.logger)

    def _assert_mutation_refused(self, source: str, destination: str) -> None:
        """Assert both mutating actions refuse one operand pair unchanged."""
        for action, prefix in (("rename", "Rename"), ("move", "Move")):
            with self.subTest(action=action):
                result = self.tool.execute(
                    action,
                    {"source": source, "destination": destination},
                )
                self.assertEqual(
                    result.message,
                    f"{prefix} failed: {PATH_EXCLUDED_MESSAGE}",
                )
                self.assertIsNone(result.data)

    def _assert_nothing_moved(self) -> None:
        """Assert every protected file is still where it was, unchanged."""
        self.assertEqual(self.database.read_text(encoding="utf-8"), "DB-PRIVATE")
        self.assertEqual(self.prompt.read_text(encoding="utf-8"), "PROMPT-PRIVATE")
        self.assertEqual(
            (self.log_dir / "maria.log").read_text(encoding="utf-8"),
            "LOG-PRIVATE",
        )
        self.assertFalse((self.workspace / "archive").exists())
        self.assertFalse((self.workspace / "moved").exists())

    def test_a_directory_containing_the_active_database_cannot_be_relocated(
        self,
    ) -> None:
        self._assert_mutation_refused("state", "archive")
        self._assert_nothing_moved()

    def test_a_directory_containing_the_active_prompt_cannot_be_relocated(
        self,
    ) -> None:
        prompt_only = self.workspace / "prompts"
        prompt_only.mkdir()
        (prompt_only / "system_prompt.txt").write_text("P", encoding="utf-8")
        tool = FilesystemTool(
            self.roots,
            FilesystemProtections(
                database_path=self.temporary_path / "elsewhere" / "maria.db",
                log_dir=self.temporary_path / "elsewhere-logs",
                prompt_path=prompt_only / "system_prompt.txt",
            ),
            logger=self.logger,
        )

        for action, prefix in (("rename", "Rename"), ("move", "Move")):
            with self.subTest(action=action):
                result = tool.execute(
                    action,
                    {"source": "prompts", "destination": "archive"},
                )
                self.assertEqual(
                    result.message,
                    f"{prefix} failed: {PATH_EXCLUDED_MESSAGE}",
                )
        self.assertTrue((prompt_only / "system_prompt.txt").is_file())

    def test_an_ancestor_of_the_active_log_directory_cannot_be_relocated(
        self,
    ) -> None:
        self._assert_mutation_refused("telemetry", "archive")
        self._assert_nothing_moved()

    def test_the_workspace_root_itself_cannot_be_relocated(self) -> None:
        broad = FilesystemRoots(
            (
                FilesystemRoot(
                    name="second_workspace",
                    path=self.temporary_path,
                    policy=RootPolicy.ORGANIZE,
                    enabled=True,
                ),
            ),
            "second_workspace",
        )
        tool = FilesystemTool(broad, self.protections, logger=self.logger)

        result = tool.execute(
            "rename",
            {"source": "MariaWorkspace", "destination": "renamed-workspace"},
        )

        self.assertEqual(
            result.message,
            f"Rename failed: {PATH_EXCLUDED_MESSAGE}",
        )
        self._assert_nothing_moved()

    def test_a_directory_containing_only_a_sidecar_cannot_be_relocated(self) -> None:
        sidecar_home = self.workspace / "sidecars"
        sidecar_home.mkdir()
        tool = FilesystemTool(
            self.roots,
            FilesystemProtections(
                database_path=sidecar_home / "maria.db",
                log_dir=self.temporary_path / "elsewhere-logs",
                prompt_path=self.temporary_path / "elsewhere-prompt.txt",
            ),
            logger=self.logger,
        )

        result = tool.execute(
            "move",
            {"source": "sidecars", "destination": "archive"},
        )

        self.assertEqual(result.message, f"Move failed: {PATH_EXCLUDED_MESSAGE}")
        self.assertTrue(sidecar_home.is_dir())

    def test_a_source_reached_through_a_link_is_refused_by_its_target(self) -> None:
        link_directory(self.workspace / "state-link", self.state)

        self._assert_mutation_refused("state-link", "archive")
        self._assert_nothing_moved()

    def test_a_destination_reached_through_a_link_is_refused_by_its_target(
        self,
    ) -> None:
        link_directory(self.workspace / "state-link", self.state)

        self._assert_mutation_refused("ordinary", "state-link")
        self.assertTrue((self.ordinary / "note.txt").is_file())
        self._assert_nothing_moved()

    def test_a_source_naming_a_protected_identity_is_refused_by_its_candidate(
        self,
    ) -> None:
        # The candidate spelling alone carries the refusal here: the fixed
        # layout name is what is protected, whatever it resolves to.
        replica = self.temporary_path / "repository"
        project = replica / "Maria"
        project.mkdir(parents=True)
        escaped = self.workspace / "escaped-logs"
        escaped.mkdir()
        link_directory(project / "logs", escaped)
        broad = FilesystemRoots(
            (
                FilesystemRoot(
                    name="second_workspace",
                    path=self.temporary_path,
                    policy=RootPolicy.ORGANIZE,
                    enabled=True,
                ),
            ),
            "second_workspace",
        )
        tool = FilesystemTool(broad, self.protections, logger=self.logger)

        with replica_repository(replica):
            result = tool.execute(
                "rename",
                {
                    "source": str(project / "logs"),
                    "destination": "MariaWorkspace/archive",
                },
            )

        self.assertEqual(
            result.message,
            f"Rename failed: {PATH_EXCLUDED_MESSAGE}",
        )
        self.assertTrue(escaped.is_dir())

    def test_a_nonexistent_destination_covering_the_prompt_is_refused(self) -> None:
        future = self.workspace / "future"
        tool = FilesystemTool(
            self.roots,
            FilesystemProtections(
                database_path=self.temporary_path / "elsewhere" / "maria.db",
                log_dir=self.temporary_path / "elsewhere-logs",
                prompt_path=future / "prompts" / "system_prompt.txt",
            ),
            logger=self.logger,
        )

        self.assertFalse(future.exists())
        for action, prefix in (("rename", "Rename"), ("move", "Move")):
            with self.subTest(action=action):
                result = tool.execute(
                    action,
                    {"source": "ordinary", "destination": "future/prompts"},
                )
                self.assertEqual(
                    result.message,
                    f"{prefix} failed: {PATH_EXCLUDED_MESSAGE}",
                )
        self.assertFalse(future.exists())
        self.assertTrue((self.ordinary / "note.txt").is_file())

    def test_protected_files_remain_excluded_after_a_refusal(self) -> None:
        self._assert_mutation_refused("state", "archive")

        for path in ("state/maria.db", "state/system_prompt.txt"):
            with self.subTest(path=path):
                result = self.tool.execute("read", {"path": path})
                self.assertEqual(
                    result.message,
                    f"File read failed: {PATH_EXCLUDED_MESSAGE}",
                )
        self.assertTrue(self.protections.excludes(self.database, self.database))
        self.assertTrue(self.protections.excludes(self.prompt, self.prompt))

    def test_an_ordinary_sibling_directory_can_still_be_relocated(self) -> None:
        renamed = self.tool.execute(
            "rename",
            {"source": "ordinary", "destination": "renamed"},
        )
        self.assertTrue(renamed.success)
        self.assertEqual(
            (self.workspace / "renamed" / "note.txt").read_text(encoding="utf-8"),
            "ordinary",
        )

        moved = self.tool.execute(
            "move",
            {"source": "renamed", "destination": "archive/renamed"},
        )
        self.assertTrue(moved.success)
        self.assertEqual(
            (self.workspace / "archive" / "renamed" / "note.txt").read_text(
                encoding="utf-8"
            ),
            "ordinary",
        )

    def test_an_ordinary_file_beside_a_protected_one_can_still_be_relocated(
        self,
    ) -> None:
        neighbour = self.state / "maria.db.notes"
        neighbour.write_text("notes", encoding="utf-8")

        result = self.tool.execute(
            "rename",
            {
                "source": "state/maria.db.notes",
                "destination": "state/renamed.notes",
            },
        )

        self.assertTrue(result.success)
        self.assertTrue((self.state / "renamed.notes").is_file())
        self._assert_nothing_moved()

    def test_listing_the_protected_parent_still_succeeds_and_filters(self) -> None:
        (self.state / "keep.txt").write_text("keep", encoding="utf-8")

        listed = self.tool.execute("list", {"path": "state"})
        workspace_listing = self.tool.execute("list", {"path": "."})

        self.assertTrue(listed.success)
        self.assertEqual(listed.data, [{"name": "keep.txt", "type": "file"}])
        self.assertTrue(workspace_listing.success)
        self.assertEqual(
            [entry["name"] for entry in workspace_listing.data],
            ["ordinary", "state", "telemetry"],
        )

    def test_a_directory_whose_children_are_all_protected_stays_empty(self) -> None:
        listed = self.tool.execute("list", {"path": "telemetry"})

        self.assertEqual(
            listed,
            ToolExecutionResult(True, "Directory is empty.", []),
        )

    def test_a_mutation_refusal_leaks_nothing(self) -> None:
        self.logger.reset_mock()

        result = self.tool.execute(
            "move",
            {"source": "state", "destination": "archive"},
        )

        logged = " ".join(str(entry) for entry in self.logger.method_calls)
        for forbidden in (
            "maria.db",
            "system_prompt",
            "DB-PRIVATE",
            "PROMPT-PRIVATE",
            "MariaWorkspace",
            str(self.workspace),
            str(REPOSITORY_ROOT),
            "workspace",
            "state",
            "archive",
            "0x",
            "object at",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, result.message)
                self.assertNotIn(forbidden, logged)

    def test_a_broad_root_cannot_relocate_an_ancestor_of_the_repository(self) -> None:
        broad = FilesystemRoots(
            (
                FilesystemRoot(
                    name="second_workspace",
                    path=REPOSITORY_ROOT.parent,
                    policy=RootPolicy.ORGANIZE,
                    enabled=True,
                ),
            ),
            "second_workspace",
        )
        tool = FilesystemTool(broad, self.protections, logger=self.logger)

        for action, prefix in (("rename", "Rename"), ("move", "Move")):
            with self.subTest(action=action):
                result = tool.execute(
                    action,
                    {
                        "source": str(REPOSITORY_ROOT.parent),
                        "destination": str(REPOSITORY_ROOT.parent / "relocated"),
                    },
                )
                self.assertEqual(
                    result.message,
                    f"{prefix} failed: {PATH_EXCLUDED_MESSAGE}",
                )
        self.assertTrue(REPOSITORY_ROOT.is_dir())
        self.assertFalse((REPOSITORY_ROOT.parent / "relocated").exists())


class FilesystemSidecarAliasTests(unittest.TestCase):
    """Prove a linked sidecar is refused by spelling and by canonical target."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_path / "MariaWorkspace"
        self.workspace.mkdir()
        self.runtime = self.workspace / "runtime"
        self.runtime.mkdir()
        self.database = self.runtime / "maria.db"
        self.database.write_text("DB", encoding="utf-8")
        self.alias = self.workspace / "wal-alias"
        self.alias.mkdir()
        (self.alias / "leaked.txt").write_text("WAL-PRIVATE", encoding="utf-8")
        link_directory(self.database.with_name("maria.db-wal"), self.alias)

        self.protections = FilesystemProtections(
            database_path=self.database,
            log_dir=self.temporary_path / "runtime-logs",
            prompt_path=self.runtime / "system_prompt.txt",
        )
        self.roots = organizing_roots(self.workspace)
        self.logger = Mock(spec=logging.Logger)
        self.tool = FilesystemTool(self.roots, self.protections, logger=self.logger)

    def test_the_lexical_sidecar_spelling_is_refused(self) -> None:
        result = self.tool.execute("list", {"path": "runtime/maria.db-wal"})

        self.assertEqual(
            result.message,
            f"Directory listing failed: {PATH_EXCLUDED_MESSAGE}",
        )

    def test_the_canonical_sidecar_target_is_refused_by_an_alternate_path(
        self,
    ) -> None:
        result = self.tool.execute("list", {"path": "wal-alias"})

        self.assertEqual(
            result.message,
            f"Directory listing failed: {PATH_EXCLUDED_MESSAGE}",
        )
        self.assertNotIn("wal-alias", result.message)
        self.assertNotIn("WAL-PRIVATE", result.message)

    def test_the_alias_is_omitted_from_its_parent_listing(self) -> None:
        (self.workspace / "notes.txt").write_text("ordinary", encoding="utf-8")

        result = self.tool.execute("list", {"path": "."})

        self.assertTrue(result.success)
        self.assertEqual(
            [entry["name"] for entry in result.data],
            ["notes.txt", "runtime"],
        )

    def test_the_alias_cannot_be_relocated(self) -> None:
        for action, prefix in (("rename", "Rename"), ("move", "Move")):
            with self.subTest(action=action):
                result = self.tool.execute(
                    action,
                    {"source": "wal-alias", "destination": "archive"},
                )
                self.assertEqual(
                    result.message,
                    f"{prefix} failed: {PATH_EXCLUDED_MESSAGE}",
                )
        self.assertTrue((self.alias / "leaked.txt").is_file())

    def test_an_unrelated_database_neighbour_remains_usable(self) -> None:
        neighbour = self.database.with_name("maria.db.notes")
        neighbour.write_text("notes", encoding="utf-8")

        read = self.tool.execute("read", {"path": "runtime/maria.db.notes"})
        renamed = self.tool.execute(
            "rename",
            {
                "source": "runtime/maria.db.notes",
                "destination": "runtime/renamed.notes",
            },
        )

        self.assertTrue(read.success)
        self.assertEqual(read.message, "notes")
        self.assertTrue(renamed.success)

    def test_the_database_parent_stays_listable(self) -> None:
        (self.runtime / "keep.txt").write_text("keep", encoding="utf-8")

        result = self.tool.execute("list", {"path": "runtime"})

        self.assertTrue(result.success)
        self.assertEqual(result.data, [{"name": "keep.txt", "type": "file"}])


class FilesystemWrongShapeProtectionTests(unittest.TestCase):
    """Prove a protected identity of the wrong shape leaks nothing."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_path / "MariaWorkspace"
        self.runtime = self.workspace / "runtime"
        self.runtime.mkdir(parents=True)
        self.logger = Mock(spec=logging.Logger)
        self.roots = organizing_roots(self.workspace)

    def _tool(self, protections: FilesystemProtections) -> FilesystemTool:
        """Return a tool over the workspace with one protection policy."""
        return FilesystemTool(self.roots, protections, logger=self.logger)

    def test_a_database_occupied_by_a_directory_hides_its_descendants(self) -> None:
        database = self.runtime / "maria.db"
        database.mkdir()
        (database / "leaked.txt").write_text("DB-PRIVATE", encoding="utf-8")
        (database / "nested").mkdir()
        (database / "nested" / "deep.txt").write_text("DEEP", encoding="utf-8")
        tool = self._tool(
            FilesystemProtections(
                database_path=database,
                log_dir=self.temporary_path / "logs",
                prompt_path=self.runtime / "system_prompt.txt",
            )
        )

        cases = (
            ("read", {"path": "runtime/maria.db/leaked.txt"}, "File read failed"),
            (
                "read",
                {"path": "runtime/maria.db/nested/deep.txt"},
                "File read failed",
            ),
            ("list", {"path": "runtime/maria.db"}, "Directory listing failed"),
            (
                "list",
                {"path": "runtime/maria.db/nested"},
                "Directory listing failed",
            ),
        )
        for action, arguments, prefix in cases:
            with self.subTest(action=action, path=arguments["path"]):
                result = tool.execute(action, arguments)
                self.assertEqual(
                    result.message,
                    f"{prefix}: {PATH_EXCLUDED_MESSAGE}",
                )

    def test_a_prompt_occupied_by_a_directory_hides_its_descendants(self) -> None:
        prompt = self.runtime / "system_prompt.txt"
        prompt.mkdir()
        (prompt / "leaked.txt").write_text("PROMPT-PRIVATE", encoding="utf-8")
        tool = self._tool(
            FilesystemProtections(
                database_path=self.runtime / "maria.db",
                log_dir=self.temporary_path / "logs",
                prompt_path=prompt,
            )
        )

        read = tool.execute("read", {"path": "runtime/system_prompt.txt/leaked.txt"})
        listed = tool.execute("list", {"path": "runtime/system_prompt.txt"})

        self.assertEqual(
            read.message,
            f"File read failed: {PATH_EXCLUDED_MESSAGE}",
        )
        self.assertEqual(
            listed.message,
            f"Directory listing failed: {PATH_EXCLUDED_MESSAGE}",
        )
        self.assertNotIn("PROMPT-PRIVATE", read.message)

    def test_the_wrong_shaped_parent_directory_remains_listable(self) -> None:
        database = self.runtime / "maria.db"
        database.mkdir()
        (database / "leaked.txt").write_text("DB-PRIVATE", encoding="utf-8")
        (self.runtime / "keep.txt").write_text("keep", encoding="utf-8")
        (self.runtime / "maria.db.notes").write_text("notes", encoding="utf-8")
        tool = self._tool(
            FilesystemProtections(
                database_path=database,
                log_dir=self.temporary_path / "logs",
                prompt_path=self.runtime / "system_prompt.txt",
            )
        )

        listed = tool.execute("list", {"path": "runtime"})
        neighbour = tool.execute("read", {"path": "runtime/maria.db.notes"})

        self.assertTrue(listed.success)
        self.assertEqual(
            [entry["name"] for entry in listed.data],
            ["keep.txt", "maria.db.notes"],
        )
        self.assertTrue(neighbour.success)
        self.assertEqual(neighbour.message, "notes")


class FilesystemSidecarDescendantTests(unittest.TestCase):
    """Prove nothing behind a linked sidecar is reachable by any route."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_path / "MariaWorkspace"
        self.workspace.mkdir()
        self.runtime = self.workspace / "runtime"
        self.runtime.mkdir()
        self.database = self.runtime / "maria.db"
        self.database.write_text("DB", encoding="utf-8")
        self.alias = self.workspace / "wal-alias"
        self.alias.mkdir()
        (self.alias / "leaked.txt").write_text("WAL-PRIVATE", encoding="utf-8")
        (self.alias / "nested").mkdir()
        (self.alias / "nested" / "deeper.txt").write_text("DEEP", encoding="utf-8")
        link_directory(self.database.with_name("maria.db-wal"), self.alias)

        self.protections = FilesystemProtections(
            database_path=self.database,
            log_dir=self.temporary_path / "runtime-logs",
            prompt_path=self.runtime / "system_prompt.txt",
        )
        self.roots = organizing_roots(self.workspace)
        self.logger = Mock(spec=logging.Logger)
        self.tool = FilesystemTool(self.roots, self.protections, logger=self.logger)

    def test_the_exact_alias_remains_refused(self) -> None:
        for path in ("wal-alias", "runtime/maria.db-wal"):
            with self.subTest(path=path):
                result = self.tool.execute("list", {"path": path})
                self.assertEqual(
                    result.message,
                    f"Directory listing failed: {PATH_EXCLUDED_MESSAGE}",
                )

    def test_a_descendant_is_refused_by_the_target_spelling(self) -> None:
        result = self.tool.execute("read", {"path": "wal-alias/leaked.txt"})

        self.assertEqual(
            result.message,
            f"File read failed: {PATH_EXCLUDED_MESSAGE}",
        )
        self.assertNotIn("WAL-PRIVATE", result.message)

    def test_a_descendant_is_refused_by_the_sidecar_spelling(self) -> None:
        result = self.tool.execute(
            "read",
            {"path": "runtime/maria.db-wal/leaked.txt"},
        )

        self.assertEqual(
            result.message,
            f"File read failed: {PATH_EXCLUDED_MESSAGE}",
        )
        self.assertNotIn("WAL-PRIVATE", result.message)

    def test_a_nested_listing_below_the_alias_is_refused(self) -> None:
        for path in ("wal-alias/nested", "runtime/maria.db-wal/nested"):
            with self.subTest(path=path):
                result = self.tool.execute("list", {"path": path})
                self.assertEqual(
                    result.message,
                    f"Directory listing failed: {PATH_EXCLUDED_MESSAGE}",
                )

    def test_a_descendant_cannot_be_relocated(self) -> None:
        for action, prefix in (("rename", "Rename"), ("move", "Move")):
            with self.subTest(action=action):
                result = self.tool.execute(
                    action,
                    {"source": "wal-alias/leaked.txt", "destination": "taken.txt"},
                )
                self.assertEqual(
                    result.message,
                    f"{prefix} failed: {PATH_EXCLUDED_MESSAGE}",
                )
        self.assertEqual(
            (self.alias / "leaked.txt").read_text(encoding="utf-8"),
            "WAL-PRIVATE",
        )
        self.assertFalse((self.workspace / "taken.txt").exists())

    def test_the_alias_remains_omitted_from_its_parent_listing(self) -> None:
        (self.workspace / "notes.txt").write_text("ordinary", encoding="utf-8")

        result = self.tool.execute("list", {"path": "."})

        self.assertTrue(result.success)
        self.assertEqual(
            [entry["name"] for entry in result.data],
            ["notes.txt", "runtime"],
        )
        self.assertNotIn("wal-alias", result.message)

    def test_no_descendant_refusal_leaks_anything(self) -> None:
        self.logger.reset_mock()

        result = self.tool.execute(
            "read",
            {"path": "runtime/maria.db-wal/nested/deeper.txt"},
        )

        logged = " ".join(str(entry) for entry in self.logger.method_calls)
        for forbidden in (
            "wal-alias",
            "maria.db-wal",
            "leaked.txt",
            "deeper.txt",
            "nested",
            "WAL-PRIVATE",
            "DEEP",
            str(self.workspace),
            str(self.alias),
            "workspace",
            "0x",
            "object at",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, result.message)
                self.assertNotIn(forbidden, logged)

    def test_ordinary_parents_and_neighbours_remain_usable(self) -> None:
        (self.runtime / "keep.txt").write_text("keep", encoding="utf-8")
        neighbour = self.database.with_name("maria.db.notes")
        neighbour.write_text("notes", encoding="utf-8")

        runtime_listing = self.tool.execute("list", {"path": "runtime"})
        neighbour_read = self.tool.execute("read", {"path": "runtime/maria.db.notes"})

        self.assertTrue(runtime_listing.success)
        self.assertEqual(
            [entry["name"] for entry in runtime_listing.data],
            ["keep.txt", "maria.db.notes"],
        )
        self.assertTrue(neighbour_read.success)
        self.assertEqual(neighbour_read.message, "notes")

    def test_unrelated_workspace_names_remain_usable(self) -> None:
        (self.workspace / "notes.tmp").write_text("kept", encoding="utf-8")
        (self.workspace / ".claude").mkdir()
        (self.workspace / ".claude" / "settings.json").write_text(
            "{}",
            encoding="utf-8",
        )

        tmp = self.tool.execute("read", {"path": "notes.tmp"})
        assistant = self.tool.execute("read", {"path": ".claude/settings.json"})

        self.assertTrue(tmp.success)
        self.assertEqual(tmp.message, "kept")
        self.assertTrue(assistant.success)
        self.assertEqual(assistant.message, "{}")


if __name__ == "__main__":
    unittest.main()
