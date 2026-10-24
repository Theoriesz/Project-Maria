"""Tests that keep Project Maria's approved path list true (V1-24-R1).

Three tests, with no Git at run time or in the tests: the members are
unique, sorted, ASCII paths within the grammar that no fixed exclusion rule
covers and that name no database, log, Git, private-configuration, cache,
credential, or temporary path; every member exists as a regular file; and
every file in the places the list covers is either a member or on the
explicit not-approved list, so a new file there is reported until it is
classified. The last two describe the isolated test copy,
which holds every explicitly approved application path, new unstaged files
included, and no private, runtime, or Git file. Listing directories that hold
the owner's private files is never done: outside such a copy the third test
skips. A further test proves the module is data only.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

import core.project_maria_manifest as manifest_module
from core.constants import REPOSITORY_ROOT
from core.filesystem_roots import is_universally_excluded
from core.project_maria_manifest import APPROVED_PATHS, NOT_APPROVED_PATHS
from core.project_records import is_project_path


# The places whose every file must be classified, relative to the repository.
CLASSIFIED_PLACES = (
    "",
    "Maria",
    "Maria/config",
    "Maria/core",
    "Maria/docs",
    "Maria/gui",
    "Maria/prompts",
    "Maria/tests",
    "Maria/tools",
)

# Every file this stage adds, each classified in this pull request.
STAGE_FILES = (
    "Maria/core/project_file_access.py",
    "Maria/core/project_files.py",
    "Maria/core/project_maria_manifest.py",
    "Maria/core/project_records.py",
    "Maria/tests/project_fakes.py",
    "Maria/tests/test_execution_guards.py",
    "Maria/tests/test_gui_projects.py",
    "Maria/tests/test_project_context.py",
    "Maria/tests/test_project_file_access.py",
    "Maria/tests/test_project_file_context.py",
    "Maria/tests/test_project_files.py",
    "Maria/tests/test_project_maria.py",
    "Maria/tests/test_project_maria_manifest.py",
    "Maria/tests/test_project_migration.py",
    "Maria/tests/test_project_records.py",
    "Maria/tests/test_project_storage.py",
    "Maria/tests/test_projects_tool.py",
    "Maria/tools/projects.py",
)

# Path fragments no member may hold, compared ignoring case.
FORBIDDEN_FRAGMENTS = (
    "database/",
    ".db",
    "-wal",
    "-shm",
    "-journal",
    "logs/",
    ".log",
    ".git",
    "config.local",
    "__pycache__",
    ".pyc",
    ".env",
    ".tmp",
    ".bak",
    "credential",
    "secret",
    ".claude",
)


class MemberRuleTests(unittest.TestCase):
    """Test 1: the members' own rules, checked without the filesystem."""

    def test_members_are_unique_sorted_ascii_and_within_the_grammar(self) -> None:
        self.assertIs(type(APPROVED_PATHS), tuple)
        self.assertEqual(list(APPROVED_PATHS), sorted(APPROVED_PATHS))
        self.assertEqual(len(APPROVED_PATHS), len(set(APPROVED_PATHS)))
        for member in APPROVED_PATHS:
            with self.subTest(member=member):
                self.assertIs(type(member), str)
                self.assertTrue(member.isascii())
                self.assertTrue(is_project_path(member))
                self.assertFalse(is_universally_excluded(REPOSITORY_ROOT / member))
                folded = member.casefold()
                for fragment in FORBIDDEN_FRAGMENTS:
                    self.assertNotIn(fragment, folded)

    def test_the_count_is_stated_against_this_tree(self) -> None:
        # 139 candidates at base commit 175c4ce, and the 18 files V1-24-R1
        # adds, every one proposed as a member.
        self.assertEqual(len(APPROVED_PATHS), 157)
        self.assertEqual(len(NOT_APPROVED_PATHS), 3)

    def test_every_file_this_stage_adds_is_classified(self) -> None:
        for path in STAGE_FILES:
            with self.subTest(path=path):
                self.assertIn(path, APPROVED_PATHS)
        self.assertFalse(set(APPROVED_PATHS) & set(NOT_APPROVED_PATHS))
        for path in NOT_APPROVED_PATHS:
            with self.subTest(path=path):
                self.assertTrue(path.isascii())


class MemberFileTests(unittest.TestCase):
    """Test 2: every member exists as a regular file."""

    def test_every_member_is_a_regular_file(self) -> None:
        for member in APPROVED_PATHS:
            with self.subTest(member=member):
                path = REPOSITORY_ROOT / member
                self.assertTrue(path.is_file())
                self.assertFalse(path.is_symlink())


def _found_files() -> list[str]:
    """Return every file directly in a covered place, repository-relative."""
    found = []
    for place in CLASSIFIED_PLACES:
        directory = REPOSITORY_ROOT / place if place else REPOSITORY_ROOT
        for entry in sorted(directory.iterdir()):
            if entry.is_file():
                found.append(entry.relative_to(REPOSITORY_ROOT).as_posix())
    return found


def _unclassified() -> list[str]:
    """Return the files in covered places that neither list names."""
    classified = set(APPROVED_PATHS) | set(NOT_APPROVED_PATHS)
    return sorted(set(_found_files()) - classified)


class ClassificationTests(unittest.TestCase):
    """Test 3: no file appears in a covered place without a decision."""

    def setUp(self) -> None:
        if (REPOSITORY_ROOT / ".git").exists():
            self.skipTest(
                "Runs only in an isolated test copy, which holds no Git, "
                "private, or runtime file."
            )

    def test_every_file_in_a_covered_place_is_classified(self) -> None:
        self.assertGreater(len(_found_files()), 100)
        self.assertEqual(_unclassified(), [])

    def test_a_new_file_in_a_covered_place_is_found_until_classified(self) -> None:
        # Written only in the isolated copy: the check above skips anywhere
        # Git data exists, and so does this one.
        probe = REPOSITORY_ROOT / "Maria" / "core" / "unclassified_probe.md"
        self.assertFalse(probe.exists())
        probe.write_bytes(b"probe\n")
        try:
            self.assertEqual(_unclassified(), ["Maria/core/unclassified_probe.md"])
        finally:
            probe.unlink()
        self.assertEqual(_unclassified(), [])


class DataOnlyTests(unittest.TestCase):
    """The module holds two tuples of path strings and nothing else."""

    def test_the_module_is_a_docstring_an_import_and_two_literal_tuples(self) -> None:
        tree = ast.parse(Path(manifest_module.__file__).read_text(encoding="utf-8"))

        docstring, future, approved, not_approved, *rest = tree.body
        self.assertEqual(rest, [])
        self.assertIsInstance(docstring, ast.Expr)
        self.assertIsInstance(docstring.value, ast.Constant)
        self.assertIsInstance(future, ast.ImportFrom)
        self.assertEqual(future.module, "__future__")
        for node, name in (
            (approved, "APPROVED_PATHS"),
            (not_approved, "NOT_APPROVED_PATHS"),
        ):
            with self.subTest(name=name):
                self.assertIsInstance(node, ast.Assign)
                self.assertEqual([target.id for target in node.targets], [name])
                self.assertIsInstance(node.value, ast.Tuple)
                for element in node.value.elts:
                    self.assertIsInstance(element, ast.Constant)
                    self.assertIs(type(element.value), str)


if __name__ == "__main__":
    unittest.main()
