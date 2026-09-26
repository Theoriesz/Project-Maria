"""Focused tests for Maria's named filesystem roots and permanent exclusions.

Nothing here reads, enumerates, or creates the owner's real workspace,
database, logs, or assistant working state. Exclusion behaviour is proven
either by pure path classification, which touches no filesystem at all, or
against a temporary replica repository built inside a ``TemporaryDirectory``.
"""

from __future__ import annotations

import copy
import os
import pickle
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from typing import Any, Iterator
from unittest.mock import patch

import core.filesystem_roots as filesystem_roots_module
from core.constants import (
    DATABASE_DIR,
    DATABASE_PATH,
    DEFAULT_LOG_FILE,
    GIT_DIR,
    LOCAL_CONFIG_PATH,
    LOG_DIR,
    PROJECT_ROOT,
    REPOSITORY_ROOT,
)
from core.filesystem_roots import (
    DATABASE_SIDECAR_SUFFIXES,
    MAXIMUM_ROOT_NAME_LENGTH,
    PROTECTION_PATH_CANONICAL_MESSAGE,
    PROTECTION_PATH_TYPE_MESSAGE,
    PROTECTION_SNAPSHOT_MESSAGE,
    PROTECTIONS_VALUE_MESSAGE,
    RESOLVED_PATH_MESSAGE,
    RESOLVED_ROOT_MESSAGE,
    ROOT_CREATION_MESSAGE,
    ROOT_ENABLED_MESSAGE,
    ROOT_EXCLUDED_MESSAGE,
    ROOT_NAME_MESSAGE,
    ROOT_NOT_A_DIRECTORY_MESSAGE,
    ROOT_PATH_CANONICAL_MESSAGE,
    ROOT_PATH_TYPE_MESSAGE,
    ROOT_POLICY_MESSAGE,
    ROOTS_DISABLED_DEFAULT_MESSAGE,
    ROOTS_DUPLICATE_NAME_MESSAGE,
    ROOTS_EMPTY_MESSAGE,
    ROOTS_MEMBER_MESSAGE,
    ROOTS_OVERLAP_MESSAGE,
    ROOTS_UNKNOWN_DEFAULT_MESSAGE,
    ROOTS_VALUE_MESSAGE,
    FilesystemProtections,
    FilesystemRoot,
    FilesystemRoots,
    ResolvedPath,
    RootPolicy,
    _rebuild_filesystem_protections,
    is_universally_excluded,
    prepare_roots,
)


def _root(
    path: Path,
    name: str = "workspace",
    policy: RootPolicy = RootPolicy.ORGANIZE,
    enabled: bool = True,
) -> FilesystemRoot:
    """Return one root value without restating every keyword each time."""
    return FilesystemRoot(name=name, path=path, policy=policy, enabled=enabled)


def _protections(base: Path) -> FilesystemProtections:
    """Return active runtime locations beneath a temporary directory.

    They sit under a ``runtime`` sibling no root fixture covers, so a test
    about root preparation is unaffected by active-runtime protection unless
    it deliberately arranges a collision.
    """
    return FilesystemProtections(
        database_path=base / "runtime" / "database" / "maria.db",
        log_dir=base / "runtime" / "logs",
        prompt_path=base / "runtime" / "prompts" / "system_prompt.txt",
    )


def link_directory(link: Path, target: Path) -> None:
    """Create a directory link, using a junction on Windows.

    A junction needs no elevated privilege, so this kind of regression runs on
    an ordinary Windows host instead of becoming a routine skip. A failure is
    raised with the exact return code rather than skipped, so an environment
    that really cannot create one is visible rather than silently untested.
    """
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
def replica_repository(root: Path) -> Iterator[Path]:
    """Point every repository-scoped rule at a temporary replica checkout.

    Both the barrier constant and the two derived exclusion tuples are
    redirected, because they are settled at import time from the real project
    layout. Redirecting them is what lets a test build real directories,
    links, and junctions for the excluded locations without going anywhere
    near the owner's own repository.
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
    ):
        yield project


class RootPolicyTests(unittest.TestCase):
    """Verify the closed set of root policies."""

    def test_exactly_two_policies_with_stable_values(self) -> None:
        self.assertEqual(
            {policy.name: policy.value for policy in RootPolicy},
            {"ORGANIZE": "organize", "READ_ONLY": "read_only"},
        )


class FilesystemRootImmutabilityTests(unittest.TestCase):
    """Prove a settled root cannot be changed, extended, or subclassed."""

    def setUp(self) -> None:
        self.root = _root(Path("/maria-workspace").resolve())

    def test_a_root_carries_no_instance_dictionary(self) -> None:
        self.assertFalse(hasattr(self.root, "__dict__"))
        with self.assertRaises(AttributeError):
            self.root.__dict__  # noqa: B018 - the access itself is the assertion.

    def test_ordinary_assignment_and_deletion_are_refused(self) -> None:
        for field in ("name", "path", "policy", "enabled"):
            with self.subTest(field=field):
                with self.assertRaises(AttributeError):
                    setattr(self.root, field, "replaced")
                with self.assertRaises(AttributeError):
                    delattr(self.root, field)

    def test_object_setattr_cannot_replace_a_settled_field(self) -> None:
        for field in ("name", "path", "policy", "enabled"):
            with self.subTest(field=field):
                with self.assertRaises(AttributeError):
                    object.__setattr__(self.root, field, "replaced")

        self.assertEqual(self.root.name, "workspace")
        self.assertEqual(self.root.policy, RootPolicy.ORGANIZE)
        self.assertTrue(self.root.enabled)

    def test_object_setattr_cannot_add_an_undeclared_payload(self) -> None:
        for field in ("payload", "secret", "_private"):
            with self.subTest(field=field):
                with self.assertRaises(AttributeError):
                    object.__setattr__(self.root, field, "smuggled")

        self.assertEqual(tuple(self.root), tuple(self.root))
        self.assertEqual(len(self.root), 4)

    def test_subclassing_is_refused_outright(self) -> None:
        with self.assertRaises(TypeError) as caught:
            type("PayloadRoot", (FilesystemRoot,), {"payload": "x"})

        self.assertIn("may not be subclassed", str(caught.exception))

    def test_make_cannot_bypass_validation(self) -> None:
        with self.assertRaises(ValueError) as caught:
            FilesystemRoot._make(
                ("workspace", "/not-a-path", RootPolicy.ORGANIZE, True)
            )

        self.assertEqual(str(caught.exception), ROOT_PATH_TYPE_MESSAGE)

    def test_replace_cannot_bypass_validation(self) -> None:
        cases = {
            "policy": ({"policy": "organize"}, ROOT_POLICY_MESSAGE),
            "enabled": ({"enabled": 1}, ROOT_ENABLED_MESSAGE),
            "name": ({"name": "not an identifier"}, ROOT_NAME_MESSAGE),
            "path": ({"path": "relative"}, ROOT_PATH_TYPE_MESSAGE),
        }

        for field, (replacement, expected) in cases.items():
            with self.subTest(field=field):
                with self.assertRaises(ValueError) as caught:
                    self.root._replace(**replacement)
                self.assertEqual(str(caught.exception), expected)

    def test_replace_still_produces_a_validated_value(self) -> None:
        replaced = self.root._replace(policy=RootPolicy.READ_ONLY)

        self.assertEqual(replaced.policy, RootPolicy.READ_ONLY)
        self.assertEqual(replaced.path, self.root.path)
        self.assertEqual(self.root.policy, RootPolicy.ORGANIZE)

    def test_equality_and_hashing_are_structural(self) -> None:
        twin = _root(Path("/maria-workspace").resolve())
        different = _root(Path("/maria-workspace").resolve(), name="second_root")

        self.assertEqual(self.root, twin)
        self.assertEqual(hash(self.root), hash(twin))
        self.assertNotEqual(self.root, different)

    def test_a_rejected_mutation_cannot_change_set_or_mapping_membership(
        self,
    ) -> None:
        holder = {self.root: "workspace"}
        members = {self.root}

        with self.assertRaises(AttributeError):
            object.__setattr__(self.root, "name", "moved")

        self.assertIn(self.root, holder)
        self.assertIn(self.root, members)
        self.assertEqual(holder[self.root], "workspace")

    def test_copy_deepcopy_and_pickle_preserve_exact_values(self) -> None:
        for label, duplicate in (
            ("copy", copy.copy(self.root)),
            ("deepcopy", copy.deepcopy(self.root)),
            ("pickle", pickle.loads(pickle.dumps(self.root))),
        ):
            with self.subTest(operation=label):
                self.assertEqual(duplicate, self.root)
                self.assertEqual(duplicate.name, self.root.name)
                self.assertEqual(duplicate.path, self.root.path)
                self.assertEqual(duplicate.policy, self.root.policy)
                self.assertEqual(duplicate.enabled, self.root.enabled)
                self.assertFalse(hasattr(duplicate, "__dict__"))


class FilesystemRootValidationTests(unittest.TestCase):
    """Prove the canonical-path invariant is established in the constructor."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()

    def test_a_non_canonical_absolute_path_is_settled_on_construction(self) -> None:
        nested = self.temporary_path / "outer" / "inner"
        nested.mkdir(parents=True)
        noncanonical = self.temporary_path / "outer" / "inner" / ".." / "inner"

        root = _root(noncanonical)

        self.assertEqual(root.path, nested.resolve())
        self.assertNotIn("..", root.path.parts)

    def test_a_relative_path_is_refused_rather_than_completed(self) -> None:
        with self.assertRaises(ValueError) as caught:
            _root(Path("workspace"))

        self.assertEqual(str(caught.exception), ROOT_PATH_TYPE_MESSAGE)

    def test_only_the_exact_concrete_path_type_is_accepted(self) -> None:
        foreign = PureWindowsPath if os.name != "nt" else PurePosixPath
        cases = {
            "text": str(self.temporary_path),
            "pure_path": PurePosixPath("/workspace"),
            "foreign_flavour": foreign("/workspace"),
            "none": None,
            "integer": 1,
        }

        for label, value in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as caught:
                    _root(value)
                self.assertEqual(str(caught.exception), ROOT_PATH_TYPE_MESSAGE)

    def test_a_path_subclass_cannot_impersonate_the_concrete_type(self) -> None:
        class DerivedPath(type(Path())):
            """A path subclass that could otherwise override resolution."""

        with self.assertRaises(ValueError) as caught:
            _root(DerivedPath(self.temporary_path))

        self.assertEqual(str(caught.exception), ROOT_PATH_TYPE_MESSAGE)

    def test_a_failing_resolution_becomes_one_deterministic_value_error(self) -> None:
        with patch.object(Path, "resolve", side_effect=OSError(13, "blocked")):
            with self.assertRaises(ValueError) as caught:
                _root(self.temporary_path)

        self.assertEqual(str(caught.exception), ROOT_PATH_CANONICAL_MESSAGE)
        self.assertNotIn(str(self.temporary_path), str(caught.exception))
        self.assertNotIn("blocked", str(caught.exception))

    def test_policy_and_enabled_are_settled_by_exact_type(self) -> None:
        with self.assertRaises(ValueError) as policy:
            _root(self.temporary_path, policy="organize")
        with self.assertRaises(ValueError) as enabled:
            _root(self.temporary_path, enabled=1)

        self.assertEqual(str(policy.exception), ROOT_POLICY_MESSAGE)
        self.assertEqual(str(enabled.exception), ROOT_ENABLED_MESSAGE)

    def test_safe_synthetic_names_are_accepted_for_isolated_tests(self) -> None:
        for name in (
            "workspace",
            "project_maria",
            "test_read_only",
            "second_workspace",
        ):
            with self.subTest(name=name):
                self.assertEqual(_root(self.temporary_path, name=name).name, name)

    def test_an_unusable_root_name_is_refused(self) -> None:
        cases = {
            "empty": "",
            "whitespace": "   ",
            "separator": "work/space",
            "dotted": "work.space",
            "hyphenated": "work-space",
            "non_ascii": "wörkspace",
            "newline": "workspace\n",
            "too_long": "w" * (MAXIMUM_ROOT_NAME_LENGTH + 1),
            "integer": 1,
            "none": None,
        }

        for label, name in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as caught:
                    _root(self.temporary_path, name=name)
                self.assertEqual(str(caught.exception), ROOT_NAME_MESSAGE)

    def test_the_maximum_name_length_boundary_is_inclusive(self) -> None:
        at_limit = "w" * MAXIMUM_ROOT_NAME_LENGTH

        self.assertEqual(_root(self.temporary_path, name=at_limit).name, at_limit)


class FilesystemRootsImmutabilityTests(unittest.TestCase):
    """Prove the collection is a snapshot that cannot drift after settling."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.workspace = _root(self.temporary_path / "workspace")
        self.reference = _root(
            self.temporary_path / "reference",
            name="test_read_only",
            policy=RootPolicy.READ_ONLY,
            enabled=False,
        )
        self.roots = FilesystemRoots(
            (self.workspace, self.reference),
            "workspace",
        )

    def test_roots_are_retained_as_a_plain_tuple(self) -> None:
        self.assertIs(type(self.roots.roots), tuple)
        self.assertNotIsInstance(self.roots.roots, (dict, list))

    def test_a_constructor_iterable_is_snapshotted(self) -> None:
        supplied = [self.workspace]
        roots = FilesystemRoots(supplied, "workspace")

        supplied.append(self.reference)

        self.assertEqual(len(roots.roots), 1)
        self.assertEqual(roots.roots, (self.workspace,))

    def test_a_generator_is_consumed_into_a_stable_snapshot(self) -> None:
        roots = FilesystemRoots((root for root in (self.workspace,)), "workspace")

        self.assertEqual(roots.roots, (self.workspace,))

    def test_the_collection_carries_no_instance_dictionary(self) -> None:
        self.assertFalse(hasattr(self.roots, "__dict__"))

    def test_assignment_deletion_and_object_setattr_are_refused(self) -> None:
        for field in ("roots", "default_name"):
            with self.subTest(field=field):
                with self.assertRaises(AttributeError):
                    setattr(self.roots, field, ())
                with self.assertRaises(AttributeError):
                    delattr(self.roots, field)
                with self.assertRaises(AttributeError):
                    object.__setattr__(self.roots, field, ())
        with self.assertRaises(AttributeError):
            object.__setattr__(self.roots, "payload", "smuggled")

        self.assertEqual(self.roots.default_name, "workspace")
        self.assertEqual(len(self.roots.roots), 2)

    def test_subclassing_is_refused_outright(self) -> None:
        with self.assertRaises(TypeError) as caught:
            type("PayloadRoots", (FilesystemRoots,), {})

        self.assertIn("may not be subclassed", str(caught.exception))

    def test_make_and_replace_cannot_bypass_validation(self) -> None:
        with self.assertRaises(ValueError) as made:
            FilesystemRoots._make(((self.workspace,), "missing_root"))
        with self.assertRaises(ValueError) as replaced:
            self.roots._replace(default_name="missing_root")

        self.assertEqual(str(made.exception), ROOTS_UNKNOWN_DEFAULT_MESSAGE)
        self.assertEqual(str(replaced.exception), ROOTS_UNKNOWN_DEFAULT_MESSAGE)

    def test_equality_hashing_and_membership_are_structural(self) -> None:
        twin = FilesystemRoots((self.workspace, self.reference), "workspace")
        holder = {self.roots: "declared"}

        with self.assertRaises(AttributeError):
            object.__setattr__(self.roots, "default_name", "moved")

        self.assertEqual(self.roots, twin)
        self.assertEqual(hash(self.roots), hash(twin))
        self.assertIn(self.roots, holder)
        self.assertIn(twin, holder)

    def test_copy_deepcopy_and_pickle_preserve_exact_values(self) -> None:
        for label, duplicate in (
            ("copy", copy.copy(self.roots)),
            ("deepcopy", copy.deepcopy(self.roots)),
            ("pickle", pickle.loads(pickle.dumps(self.roots))),
        ):
            with self.subTest(operation=label):
                self.assertEqual(duplicate, self.roots)
                self.assertEqual(duplicate.roots, self.roots.roots)
                self.assertEqual(duplicate.default_name, "workspace")
                self.assertFalse(hasattr(duplicate, "__dict__"))


class FilesystemRootsInvariantTests(unittest.TestCase):
    """Prove the collection refuses a declaration resolution could not use."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.workspace = _root(self.temporary_path / "workspace")

    def test_an_empty_declaration_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            FilesystemRoots((), "workspace")

        self.assertEqual(str(caught.exception), ROOTS_EMPTY_MESSAGE)

    def test_a_member_that_is_not_a_root_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            FilesystemRoots(("workspace",), "workspace")

        self.assertEqual(str(caught.exception), ROOTS_MEMBER_MESSAGE)

    def test_a_duplicate_root_name_is_refused(self) -> None:
        twin = _root(self.temporary_path / "second")

        with self.assertRaises(ValueError) as caught:
            FilesystemRoots((self.workspace, twin), "workspace")

        self.assertEqual(str(caught.exception), ROOTS_DUPLICATE_NAME_MESSAGE)

    def test_a_shared_or_nested_canonical_path_is_refused_in_both_directions(
        self,
    ) -> None:
        cases = {
            "identical": self.temporary_path / "workspace",
            "inside": self.temporary_path / "workspace" / "nested",
            "containing": self.temporary_path,
        }

        for label, path in cases.items():
            with self.subTest(case=label):
                other = _root(path, name="second_workspace")
                with self.assertRaises(ValueError) as caught:
                    FilesystemRoots((self.workspace, other), "workspace")
                self.assertEqual(str(caught.exception), ROOTS_OVERLAP_MESSAGE)

    def test_an_unknown_or_disabled_default_is_refused(self) -> None:
        disabled = _root(
            self.temporary_path / "reference",
            name="test_read_only",
            enabled=False,
        )

        with self.assertRaises(ValueError) as unknown:
            FilesystemRoots((self.workspace,), "missing_root")
        with self.assertRaises(ValueError) as off:
            FilesystemRoots((self.workspace, disabled), "test_read_only")

        self.assertEqual(str(unknown.exception), ROOTS_UNKNOWN_DEFAULT_MESSAGE)
        self.assertEqual(str(off.exception), ROOTS_DISABLED_DEFAULT_MESSAGE)

    def test_containing_ignores_a_disabled_root(self) -> None:
        disabled = _root(
            self.temporary_path / "reference",
            name="test_read_only",
            policy=RootPolicy.READ_ONLY,
            enabled=False,
        )
        roots = FilesystemRoots((self.workspace, disabled), "workspace")

        self.assertIsNone(roots.containing(disabled.path / "notes.txt"))
        self.assertEqual(
            roots.containing(self.workspace.path / "notes.txt"),
            self.workspace,
        )
        self.assertEqual(roots.enabled_roots, (self.workspace,))

    def test_relative_text_resolves_against_the_default_root(self) -> None:
        roots = FilesystemRoots((self.workspace,), "workspace")

        resolved = roots.resolve_path("notes/today.txt")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.root, self.workspace)
        self.assertEqual(
            resolved.target,
            self.workspace.path / "notes" / "today.txt",
        )
        self.assertEqual(resolved.candidate, resolved.target)

    def test_a_path_outside_every_enabled_root_reports_nothing(self) -> None:
        roots = FilesystemRoots((self.workspace,), "workspace")

        self.assertIsNone(roots.resolve_path(str(self.temporary_path)))
        self.assertIsNone(roots.resolve_path("../escape.txt"))

    def test_home_expansion_uses_the_patched_home_only(self) -> None:
        home = self.temporary_path / "home"
        workspace = _root(home / "MariaWorkspace")
        roots = FilesystemRoots((workspace,), "workspace")

        with patch.dict(
            os.environ,
            {"HOME": str(home), "USERPROFILE": str(home)},
        ):
            resolved = roots.resolve_path("~/MariaWorkspace/notes.txt")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.target, workspace.path / "notes.txt")


class UniversalExclusionClassificationTests(unittest.TestCase):
    """Classify real project paths without touching a single one of them."""

    def test_the_database_directory_and_its_sidecars_are_excluded(self) -> None:
        for path in (
            DATABASE_DIR,
            DATABASE_PATH,
            DATABASE_PATH.with_name("maria.db-wal"),
            DATABASE_PATH.with_name("maria.db-shm"),
            DATABASE_PATH.with_name("maria.db-journal"),
            DATABASE_DIR / "nested" / "copy.db",
        ):
            with self.subTest(path=path.name):
                self.assertTrue(is_universally_excluded(path))

    def test_the_log_directory_and_its_rotations_are_excluded(self) -> None:
        for path in (
            LOG_DIR,
            DEFAULT_LOG_FILE,
            DEFAULT_LOG_FILE.with_name("maria.log.1"),
            DEFAULT_LOG_FILE.with_name("maria.log.2026-09-20"),
            LOG_DIR / "archive" / "maria.log",
        ):
            with self.subTest(path=path.name):
                self.assertTrue(is_universally_excluded(path))

    def test_git_metadata_is_excluded_as_a_directory_and_as_a_pointer(self) -> None:
        for path in (GIT_DIR, GIT_DIR / "config", GIT_DIR / "objects" / "ab"):
            with self.subTest(path=path.name):
                self.assertTrue(is_universally_excluded(path))

    def test_the_reserved_local_configuration_is_excluded(self) -> None:
        # The owner's private override may legitimately exist, so the
        # exclusion is proven without asking the filesystem anything: the
        # classification consults no metadata, so its answer cannot depend
        # on whether the file is there.
        self.assertEqual(
            LOCAL_CONFIG_PATH,
            PROJECT_ROOT / "config" / "config.local.toml",
        )
        with patch("os.stat", side_effect=AssertionError("stat consulted")), patch(
            "os.lstat",
            side_effect=AssertionError("lstat consulted"),
        ):
            self.assertTrue(is_universally_excluded(LOCAL_CONFIG_PATH))

    def test_credential_components_are_excluded_at_any_depth(self) -> None:
        for relative in (
            ".env",
            ".env.local",
            ".env.production",
            "Maria/.env",
            "Maria/tools/.env.staging",
            ".env/inside.txt",
        ):
            with self.subTest(path=relative):
                self.assertTrue(
                    is_universally_excluded(REPOSITORY_ROOT / relative)
                )

    def test_the_template_exception_is_an_exact_leaf_only(self) -> None:
        allowed = REPOSITORY_ROOT / ".env.example"
        nested_allowed = REPOSITORY_ROOT / "Maria" / ".env.example"

        self.assertFalse(is_universally_excluded(allowed))
        self.assertFalse(is_universally_excluded(nested_allowed))

        for relative in (
            ".env.example/secret.txt",
            ".env.example/nested/secret.txt",
            ".ENV.EXAMPLE",
            ".env.example.bak",
            ".env.examples",
        ):
            with self.subTest(path=relative):
                self.assertTrue(
                    is_universally_excluded(REPOSITORY_ROOT / relative)
                )

    def test_cache_and_environment_directories_are_excluded_at_any_depth(
        self,
    ) -> None:
        for component in (
            "__pycache__",
            ".venv",
            "venv",
            "env",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "maria_cli_test",
        ):
            for relative in (
                Path(component),
                Path("Maria") / component,
                Path("Maria") / "tools" / component / "deep" / "file.txt",
            ):
                with self.subTest(component=component, path=str(relative)):
                    self.assertTrue(
                        is_universally_excluded(REPOSITORY_ROOT / relative)
                    )

    def test_assistant_working_state_is_excluded_at_any_depth(self) -> None:
        for relative in (
            ".claude",
            ".claude/settings.json",
            "Maria/.claude",
            "Maria/core/.claude/notes/private.md",
        ):
            with self.subTest(path=relative):
                self.assertTrue(
                    is_universally_excluded(REPOSITORY_ROOT / relative)
                )

    def test_temporary_and_backup_artifacts_are_excluded_at_any_depth(self) -> None:
        for relative in (
            "notes.tmp",
            "notes.bak",
            "Maria/core/brain.py.bak",
            "Maria/scratch.tmp/inside.txt",
            "Maria/SCRATCH.TMP",
        ):
            with self.subTest(path=relative):
                self.assertTrue(
                    is_universally_excluded(REPOSITORY_ROOT / relative)
                )

    def test_ordinary_repository_content_is_not_universally_excluded(self) -> None:
        for path in (
            REPOSITORY_ROOT,
            PROJECT_ROOT / "core" / "brain.py",
            PROJECT_ROOT / "docs" / "architecture.md",
            PROJECT_ROOT / "prompts" / "system_prompt.txt",
            PROJECT_ROOT / "config" / "config.toml",
        ):
            with self.subTest(path=path.name):
                self.assertFalse(is_universally_excluded(path))

    def test_the_component_rules_stop_at_the_repository_boundary(self) -> None:
        outside = Path.home().resolve() / "MariaWorkspace"

        for relative in (
            "env",
            "venv",
            ".venv/lib",
            ".claude/settings.json",
            "__pycache__/notes.txt",
            "notes.tmp",
            "archive.bak",
            ".env",
            "maria_cli_test",
        ):
            with self.subTest(path=relative):
                self.assertFalse(is_universally_excluded(outside / relative))


class UniversalExclusionReplicaTests(unittest.TestCase):
    """Prove exclusion follows the canonical path, not the submitted spelling."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()

    def test_a_traversal_spelling_cannot_walk_past_an_exclusion(self) -> None:
        with replica_repository(self.temporary_path) as project:
            database = project / "database"
            database.mkdir(parents=True)
            spelled = (project / "docs" / ".." / "database" / "maria.db").resolve()

            self.assertTrue(is_universally_excluded(spelled))

    def test_a_link_into_an_excluded_subtree_is_still_excluded(self) -> None:
        with replica_repository(self.temporary_path) as project:
            logs = project / "logs"
            logs.mkdir(parents=True)
            (logs / "maria.log").write_text("entry", encoding="utf-8")
            link = project / "log-link"
            link_directory(link, logs)

            self.assertTrue(
                is_universally_excluded((link / "maria.log").resolve())
            )

    def test_a_broad_root_declaration_cannot_override_an_exclusion(self) -> None:
        with replica_repository(self.temporary_path) as project:
            database = project / "database"
            database.mkdir(parents=True)
            broad = _root(self.temporary_path, name="second_workspace")
            roots = FilesystemRoots((broad,), "second_workspace")

            resolved = roots.resolve_path(str(database / "maria.db"))

            self.assertIsNotNone(resolved)
            self.assertTrue(is_universally_excluded(resolved.target))


class RootPreparationTests(unittest.TestCase):
    """Prove preparation creates exactly what it is allowed to create."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()

    def test_a_missing_organizing_root_is_created_with_parents(self) -> None:
        workspace = self.temporary_path / "nested" / "MariaWorkspace"
        roots = FilesystemRoots((_root(workspace),), "workspace")
        self.assertFalse(workspace.exists())

        prepare_roots(roots, _protections(self.temporary_path))

        self.assertTrue(workspace.is_dir())

    def test_preparing_an_existing_organizing_root_is_idempotent(self) -> None:
        workspace = self.temporary_path / "MariaWorkspace"
        workspace.mkdir()
        (workspace / "keep.txt").write_text("kept", encoding="utf-8")
        roots = FilesystemRoots((_root(workspace),), "workspace")

        prepare_roots(roots, _protections(self.temporary_path))
        prepare_roots(roots, _protections(self.temporary_path))

        self.assertEqual(
            (workspace / "keep.txt").read_text(encoding="utf-8"),
            "kept",
        )

    def test_an_enabled_read_only_root_is_never_created(self) -> None:
        workspace = self.temporary_path / "MariaWorkspace"
        reference = self.temporary_path / "reference"
        roots = FilesystemRoots(
            (
                _root(workspace),
                _root(
                    reference,
                    name="test_read_only",
                    policy=RootPolicy.READ_ONLY,
                ),
            ),
            "workspace",
        )

        with self.assertRaises(ValueError) as caught:
            prepare_roots(roots, _protections(self.temporary_path))

        self.assertEqual(str(caught.exception), ROOT_NOT_A_DIRECTORY_MESSAGE)
        self.assertFalse(reference.exists())

    def test_an_existing_read_only_root_is_accepted_unchanged(self) -> None:
        workspace = self.temporary_path / "MariaWorkspace"
        reference = self.temporary_path / "reference"
        reference.mkdir()
        roots = FilesystemRoots(
            (
                _root(workspace),
                _root(
                    reference,
                    name="test_read_only",
                    policy=RootPolicy.READ_ONLY,
                ),
            ),
            "workspace",
        )

        prepare_roots(roots, _protections(self.temporary_path))

        self.assertTrue(workspace.is_dir())
        self.assertEqual(list(reference.iterdir()), [])

    def test_a_disabled_root_is_neither_created_nor_required(self) -> None:
        workspace = self.temporary_path / "MariaWorkspace"
        inert = self.temporary_path / "inert"
        roots = FilesystemRoots(
            (
                _root(workspace),
                _root(
                    inert,
                    name="project_maria",
                    policy=RootPolicy.READ_ONLY,
                    enabled=False,
                ),
            ),
            "workspace",
        )

        prepare_roots(roots, _protections(self.temporary_path))

        self.assertTrue(workspace.is_dir())
        self.assertFalse(inert.exists())

    def test_a_disabled_organizing_root_is_not_created_either(self) -> None:
        workspace = self.temporary_path / "MariaWorkspace"
        inert = self.temporary_path / "inert"
        roots = FilesystemRoots(
            (
                _root(workspace),
                _root(inert, name="second_workspace", enabled=False),
            ),
            "workspace",
        )

        prepare_roots(roots, _protections(self.temporary_path))

        self.assertFalse(inert.exists())

    def test_a_root_path_that_is_a_file_fails_closed(self) -> None:
        occupied = self.temporary_path / "MariaWorkspace"
        occupied.write_text("not a directory", encoding="utf-8")
        roots = FilesystemRoots((_root(occupied),), "workspace")

        with self.assertRaises(ValueError) as caught:
            prepare_roots(roots, _protections(self.temporary_path))

        self.assertEqual(str(caught.exception), ROOT_NOT_A_DIRECTORY_MESSAGE)
        self.assertTrue(occupied.is_file())

    def test_a_creation_failure_becomes_one_fixed_refusal(self) -> None:
        workspace = self.temporary_path / "MariaWorkspace"
        roots = FilesystemRoots((_root(workspace),), "workspace")

        with patch.object(Path, "mkdir", side_effect=OSError(13, "blocked")):
            with self.assertRaises(ValueError) as caught:
                prepare_roots(roots, _protections(self.temporary_path))

        self.assertEqual(str(caught.exception), ROOT_CREATION_MESSAGE)
        self.assertNotIn(str(workspace), str(caught.exception))
        self.assertNotIn("blocked", str(caught.exception))

    def test_a_universally_excluded_root_fails_closed_before_creation(self) -> None:
        never_created = DATABASE_DIR / "v1_08_never_created"
        roots = FilesystemRoots((_root(never_created),), "workspace")

        with self.assertRaises(ValueError) as caught:
            prepare_roots(roots, _protections(self.temporary_path))

        self.assertEqual(str(caught.exception), ROOT_EXCLUDED_MESSAGE)
        self.assertFalse(never_created.exists())

    def test_a_disabled_excluded_root_is_refused_as_well(self) -> None:
        workspace = self.temporary_path / "MariaWorkspace"
        roots = FilesystemRoots(
            (
                _root(workspace),
                _root(
                    LOG_DIR / "v1_08_never_created",
                    name="second_workspace",
                    enabled=False,
                ),
            ),
            "workspace",
        )

        with self.assertRaises(ValueError) as caught:
            prepare_roots(roots, _protections(self.temporary_path))

        self.assertEqual(str(caught.exception), ROOT_EXCLUDED_MESSAGE)
        self.assertFalse((LOG_DIR / "v1_08_never_created").exists())

    def test_preparation_requires_a_settled_roots_value(self) -> None:
        for value in (None, (), [_root(self.temporary_path)], "workspace"):
            with self.subTest(case=type(value).__name__):
                with self.assertRaises(ValueError) as caught:
                    prepare_roots(value, _protections(self.temporary_path))
                self.assertEqual(str(caught.exception), ROOTS_VALUE_MESSAGE)

    def test_preparation_requires_a_settled_protections_value(self) -> None:
        workspace = self.temporary_path / "MariaWorkspace"
        roots = FilesystemRoots((_root(workspace),), "workspace")

        for value in (None, (), "protections", _root(self.temporary_path)):
            with self.subTest(case=type(value).__name__):
                with self.assertRaises(ValueError) as caught:
                    prepare_roots(roots, value)
                self.assertEqual(str(caught.exception), PROTECTIONS_VALUE_MESSAGE)
        self.assertFalse(workspace.exists())

    def test_home_relative_preparation_stays_inside_a_temporary_home(self) -> None:
        home = self.temporary_path / "home"
        home.mkdir()

        with patch.dict(
            os.environ,
            {"HOME": str(home), "USERPROFILE": str(home)},
        ):
            expanded = Path("~/MariaWorkspace").expanduser()
            roots = FilesystemRoots((_root(expanded),), "workspace")
            prepare_roots(roots, _protections(self.temporary_path))

        self.assertTrue((home / "MariaWorkspace").is_dir())
        self.assertTrue(
            roots.default_root.path.is_relative_to(self.temporary_path)
        )


class ResolvedPathTests(unittest.TestCase):
    """Prove the settled two-identity result cannot be edited after the fact."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.root = _root(self.temporary_path / "workspace")
        self.resolved = ResolvedPath(
            self.root,
            self.temporary_path / "workspace" / "link",
            self.temporary_path / "workspace" / "target",
        )

    def test_both_identities_are_carried_separately(self) -> None:
        self.assertEqual(self.resolved.root, self.root)
        self.assertEqual(
            self.resolved.candidate,
            self.temporary_path / "workspace" / "link",
        )
        self.assertEqual(
            self.resolved.target,
            self.temporary_path / "workspace" / "target",
        )
        self.assertNotEqual(self.resolved.candidate, self.resolved.target)

    def test_the_value_is_sealed(self) -> None:
        self.assertFalse(hasattr(self.resolved, "__dict__"))
        for field in ("root", "candidate", "target"):
            with self.subTest(field=field):
                with self.assertRaises(AttributeError):
                    setattr(self.resolved, field, "replaced")
                with self.assertRaises(AttributeError):
                    object.__setattr__(self.resolved, field, "replaced")
        with self.assertRaises(AttributeError):
            object.__setattr__(self.resolved, "payload", "smuggled")
        with self.assertRaises(TypeError):
            type("DerivedResolved", (ResolvedPath,), {})

    def test_construction_and_replacement_are_validated(self) -> None:
        with self.assertRaises(ValueError) as root:
            ResolvedPath("workspace", self.temporary_path, self.temporary_path)
        with self.assertRaises(ValueError) as candidate:
            ResolvedPath(self.root, "relative", self.temporary_path)
        with self.assertRaises(ValueError) as made:
            ResolvedPath._make((self.root, Path("relative"), self.temporary_path))
        with self.assertRaises(ValueError) as replaced:
            self.resolved._replace(target="text")

        self.assertEqual(str(root.exception), RESOLVED_ROOT_MESSAGE)
        self.assertEqual(str(candidate.exception), RESOLVED_PATH_MESSAGE)
        self.assertEqual(str(made.exception), RESOLVED_PATH_MESSAGE)
        self.assertEqual(str(replaced.exception), RESOLVED_PATH_MESSAGE)

    def test_a_lexical_candidate_is_reported_beside_the_followed_target(
        self,
    ) -> None:
        workspace = self.temporary_path / "workspace"
        (workspace / "nested").mkdir(parents=True)
        roots = FilesystemRoots((_root(workspace),), "workspace")

        resolved = roots.resolve_path("nested/../notes.txt")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.candidate, workspace / "notes.txt")
        self.assertEqual(resolved.target, workspace / "notes.txt")


class FilesystemProtectionsImmutabilityTests(unittest.TestCase):
    """Prove the active protection policy cannot drift after it is settled."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.protections = _protections(self.temporary_path)

    def test_it_carries_no_instance_dictionary(self) -> None:
        self.assertFalse(hasattr(self.protections, "__dict__"))
        with self.assertRaises(AttributeError):
            self.protections.__dict__  # noqa: B018 - the access is the assertion.

    def test_ordinary_assignment_and_deletion_are_refused(self) -> None:
        for field in (
            "database_path",
            "log_dir",
            "prompt_path",
            "database_sidecars",
        ):
            with self.subTest(field=field):
                with self.assertRaises(AttributeError):
                    setattr(self.protections, field, self.temporary_path)
                with self.assertRaises(AttributeError):
                    delattr(self.protections, field)

    def test_object_setattr_cannot_replace_or_add_a_field(self) -> None:
        for field in (
            "database_path",
            "log_dir",
            "prompt_path",
            "database_sidecars",
        ):
            with self.subTest(field=field):
                with self.assertRaises(AttributeError):
                    object.__setattr__(self.protections, field, self.temporary_path)
        for field in ("payload", "secret", "_private"):
            with self.subTest(field=field):
                with self.assertRaises(AttributeError):
                    object.__setattr__(self.protections, field, "smuggled")

        # Three declared inputs plus the derived sidecar identities. The
        # guard above is unchanged: object.__setattr__ added nothing.
        self.assertEqual(len(self.protections), 4)
        self.assertEqual(self.protections._fields[3], "database_sidecars")
        self.assertEqual(
            self.protections.log_dir,
            (self.temporary_path / "runtime" / "logs").resolve(),
        )

    def test_subclassing_is_refused_outright(self) -> None:
        with self.assertRaises(TypeError) as caught:
            type("PayloadProtections", (FilesystemProtections,), {})

        self.assertIn("may not be subclassed", str(caught.exception))

    def test_make_snapshots_its_iterable_and_validates(self) -> None:
        supplied = [
            self.temporary_path / "db" / "maria.db",
            self.temporary_path / "logs",
            self.temporary_path / "prompt.txt",
        ]

        settled = FilesystemProtections._make(supplied)
        supplied[1] = self.temporary_path / "moved"

        self.assertEqual(settled.log_dir, (self.temporary_path / "logs").resolve())

        with self.assertRaises(ValueError) as caught:
            FilesystemProtections._make(
                ("relative", self.temporary_path, self.temporary_path)
            )
        self.assertEqual(str(caught.exception), PROTECTION_PATH_TYPE_MESSAGE)

    def test_replace_cannot_bypass_validation(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.protections._replace(prompt_path="elsewhere")

        self.assertEqual(str(caught.exception), PROTECTION_PATH_TYPE_MESSAGE)

    def test_replace_still_produces_a_validated_value(self) -> None:
        moved = self.protections._replace(log_dir=self.temporary_path / "other")

        self.assertEqual(moved.log_dir, (self.temporary_path / "other").resolve())
        self.assertEqual(moved.database_path, self.protections.database_path)
        self.assertEqual(
            self.protections.log_dir,
            (self.temporary_path / "runtime" / "logs").resolve(),
        )

    def test_equality_hashing_and_membership_are_structural(self) -> None:
        twin = _protections(self.temporary_path)
        holder = {self.protections: "active"}
        members = {self.protections}

        with self.assertRaises(AttributeError):
            object.__setattr__(self.protections, "log_dir", self.temporary_path)

        self.assertEqual(self.protections, twin)
        self.assertEqual(hash(self.protections), hash(twin))
        self.assertIn(self.protections, holder)
        self.assertIn(twin, members)
        self.assertEqual(holder[twin], "active")

    def test_copy_deepcopy_and_pickle_preserve_exact_values(self) -> None:
        for label, duplicate in (
            ("copy", copy.copy(self.protections)),
            ("deepcopy", copy.deepcopy(self.protections)),
            ("pickle", pickle.loads(pickle.dumps(self.protections))),
        ):
            with self.subTest(operation=label):
                self.assertEqual(duplicate, self.protections)
                self.assertEqual(
                    duplicate.database_path,
                    self.protections.database_path,
                )
                self.assertEqual(duplicate.log_dir, self.protections.log_dir)
                self.assertEqual(
                    duplicate.prompt_path,
                    self.protections.prompt_path,
                )
                self.assertFalse(hasattr(duplicate, "__dict__"))

    def test_the_canonical_invariant_is_established_in_the_constructor(self) -> None:
        nested = self.temporary_path / "outer" / "inner"
        nested.mkdir(parents=True)

        settled = FilesystemProtections(
            database_path=self.temporary_path / "outer" / "inner" / ".." / "db.sqlite",
            log_dir=nested,
            prompt_path=nested / "prompt.txt",
        )

        self.assertEqual(
            settled.database_path,
            (self.temporary_path / "outer" / "db.sqlite").resolve(),
        )
        self.assertNotIn("..", settled.database_path.parts)

        for label, values in {
            "text": (str(nested), nested, nested),
            "relative": (Path("relative"), nested, nested),
            "none": (None, nested, nested),
        }.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as caught:
                    FilesystemProtections(*values)
                self.assertEqual(
                    str(caught.exception),
                    PROTECTION_PATH_TYPE_MESSAGE,
                )

    def test_a_failing_resolution_becomes_one_deterministic_value_error(self) -> None:
        with patch.object(Path, "resolve", side_effect=OSError(13, "blocked")):
            with self.assertRaises(ValueError) as caught:
                FilesystemProtections(
                    database_path=self.temporary_path / "db",
                    log_dir=self.temporary_path / "logs",
                    prompt_path=self.temporary_path / "prompt.txt",
                )

        self.assertEqual(str(caught.exception), PROTECTION_PATH_CANONICAL_MESSAGE)
        self.assertNotIn(str(self.temporary_path), str(caught.exception))
        self.assertNotIn("blocked", str(caught.exception))


class FilesystemProtectionsPolicyTests(unittest.TestCase):
    """Prove the active runtime locations are protected wherever configured."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_path / "MariaWorkspace"
        self.database = self.workspace / "state" / "maria.db"
        self.log_dir = self.workspace / "state" / "logs"
        self.prompt = self.workspace / "state" / "system_prompt.txt"
        self.protections = FilesystemProtections(
            database_path=self.database,
            log_dir=self.log_dir,
            prompt_path=self.prompt,
        )

    def test_the_active_database_and_its_sidecars_are_protected(self) -> None:
        self.assertTrue(self.protections.protects(self.database))
        for suffix in DATABASE_SIDECAR_SUFFIXES:
            sidecar = self.database.with_name(f"maria.db{suffix}")
            with self.subTest(suffix=suffix):
                self.assertTrue(self.protections.protects(sidecar))
        self.assertEqual(
            self.protections.database_sidecars,
            tuple(
                self.database.with_name(f"maria.db{suffix}")
                for suffix in DATABASE_SIDECAR_SUFFIXES
            ),
        )

    def test_an_unrelated_neighbour_of_the_database_stays_usable(self) -> None:
        for name in ("maria.db.notes", "notes.txt", "maria.dbx", "other.db"):
            with self.subTest(name=name):
                self.assertFalse(
                    self.protections.protects(self.database.with_name(name))
                )

    def test_the_active_log_directory_is_protected_as_a_whole_subtree(self) -> None:
        for name in (
            "maria.log",
            "maria.log.1",
            "maria.log.2",
            "maria.log.3",
            "anything-else.txt",
        ):
            with self.subTest(name=name):
                self.assertTrue(self.protections.protects(self.log_dir / name))
        self.assertTrue(self.protections.protects(self.log_dir))
        self.assertTrue(
            self.protections.protects(self.log_dir / "nested" / "deep.log")
        )

    def test_the_active_prompt_is_protected_as_an_exact_file(self) -> None:
        self.assertTrue(self.protections.protects(self.prompt))
        self.assertFalse(
            self.protections.protects(self.prompt.with_name("other_prompt.txt"))
        )

    def test_ordinary_workspace_content_stays_unprotected(self) -> None:
        for relative in ("notes.txt", "inbox/order.txt", "state/notes.txt"):
            with self.subTest(path=relative):
                self.assertFalse(self.protections.protects(self.workspace / relative))

    def test_excludes_covers_both_halves_and_every_identity(self) -> None:
        fixed = DATABASE_PATH
        active = self.database
        ordinary = self.workspace / "notes.txt"

        self.assertTrue(self.protections.excludes(fixed))
        self.assertTrue(self.protections.excludes(active))
        self.assertTrue(self.protections.excludes(ordinary, active))
        self.assertTrue(self.protections.excludes(active, ordinary))
        self.assertFalse(self.protections.excludes(ordinary))
        self.assertFalse(self.protections.excludes(ordinary, ordinary))

    def test_the_default_layout_exclusions_are_still_carried(self) -> None:
        for path in (DATABASE_PATH, DEFAULT_LOG_FILE, LOCAL_CONFIG_PATH, GIT_DIR):
            with self.subTest(path=path.name):
                self.assertTrue(self.protections.excludes(path))


class ProtectedNameLinkDirectionTests(unittest.TestCase):
    """Prove neither link direction defeats a protected identity."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_path / "MariaWorkspace"
        self.workspace.mkdir()
        self.protections = _protections(self.temporary_path)

    def _escaped(self, name: str) -> Path:
        """Return a real workspace directory a protected name may point at."""
        target = self.workspace / name
        target.mkdir()
        (target / "private.txt").write_text("private", encoding="utf-8")
        return target

    def test_a_protected_repository_name_linking_out_stays_excluded(self) -> None:
        cases = {
            "logs": ("logs", "maria.log"),
            "database": ("database", "maria.db"),
        }

        for label, (component, leaf) in cases.items():
            with self.subTest(case=label):
                with TemporaryDirectory() as replica_directory:
                    replica = Path(replica_directory).resolve()
                    with replica_repository(replica) as project:
                        project.mkdir(parents=True)
                        escaped = self._escaped(f"escaped-{label}")
                        link_directory(project / component, escaped)
                        candidate = project / component / leaf
                        target = candidate.resolve()

                        self.assertTrue(target.is_relative_to(self.workspace))
                        self.assertFalse(self.protections.protects(target))
                        self.assertTrue(
                            self.protections.excludes(candidate, target)
                        )

    def test_the_local_configuration_name_linking_out_stays_excluded(self) -> None:
        with TemporaryDirectory() as replica_directory:
            replica = Path(replica_directory).resolve()
            with replica_repository(replica) as project:
                (project / "config").mkdir(parents=True)
                elsewhere = self.workspace / "private.toml"
                elsewhere.write_text("secret = true", encoding="utf-8")
                candidate = project / "config" / "config.local.toml"
                try:
                    candidate.symlink_to(elsewhere)
                except (NotImplementedError, OSError):
                    # A file symlink needs a privilege this host may withhold.
                    # The candidate rule is spelling-based, so the link is not
                    # required for the identity it protects to be the one asked
                    # about; resolve falls back to the candidate itself.
                    pass
                target = candidate.resolve()

                self.assertTrue(self.protections.excludes(candidate, target))

    def test_the_git_name_linking_out_stays_excluded(self) -> None:
        with TemporaryDirectory() as replica_directory:
            replica = Path(replica_directory).resolve()
            with replica_repository(replica) as project:
                project.mkdir(parents=True)
                escaped = self._escaped("escaped-git")
                link_directory(replica / ".git", escaped)
                candidate = replica / ".git" / "config"
                target = candidate.resolve()

                self.assertTrue(target.is_relative_to(self.workspace))
                self.assertTrue(self.protections.excludes(candidate, target))

    def test_a_repository_scoped_assistant_name_linking_out_stays_excluded(
        self,
    ) -> None:
        with TemporaryDirectory() as replica_directory:
            replica = Path(replica_directory).resolve()
            with replica_repository(replica) as project:
                project.mkdir(parents=True)
                escaped = self._escaped("escaped-claude")
                link_directory(replica / ".claude", escaped)
                candidate = replica / ".claude" / "settings.json"
                target = candidate.resolve()

                self.assertTrue(target.is_relative_to(self.workspace))
                self.assertTrue(self.protections.excludes(candidate, target))

    def test_a_workspace_link_into_each_protected_target_stays_excluded(self) -> None:
        active_log_dir = self.temporary_path / "runtime" / "logs"
        active_log_dir.mkdir(parents=True)
        (active_log_dir / "maria.log").write_text("log", encoding="utf-8")
        link_directory(self.workspace / "log-link", active_log_dir)

        candidate = self.workspace / "log-link" / "maria.log"
        target = candidate.resolve()

        self.assertFalse(self.protections.protects(candidate))
        self.assertTrue(self.protections.protects(target))
        self.assertTrue(self.protections.excludes(candidate, target))

    def test_an_ordinary_workspace_name_beside_a_link_stays_reachable(self) -> None:
        ordinary = self.workspace / "notes.txt"
        ordinary.write_text("notes", encoding="utf-8")

        self.assertFalse(self.protections.excludes(ordinary, ordinary))


class FilesystemProtectionsMutationTests(unittest.TestCase):
    """Prove relocating a path asks a wider question than reading it."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_path / "MariaWorkspace"
        self.state = self.workspace / "state"
        self.state.mkdir(parents=True)
        self.database = self.state / "maria.db"
        self.prompt = self.state / "system_prompt.txt"
        self.log_dir = self.workspace / "telemetry" / "logs"
        self.log_dir.mkdir(parents=True)
        self.protections = FilesystemProtections(
            database_path=self.database,
            log_dir=self.log_dir,
            prompt_path=self.prompt,
        )

    def test_an_ancestor_of_a_protected_file_is_not_directly_protected(self) -> None:
        for ancestor in (self.state, self.workspace, self.temporary_path):
            with self.subTest(ancestor=ancestor.name):
                self.assertFalse(self.protections.protects(ancestor))
                self.assertFalse(self.protections.excludes(ancestor, ancestor))

    def test_an_ancestor_of_a_protected_file_intersects_a_mutation(self) -> None:
        for ancestor in (self.state, self.workspace, self.temporary_path):
            with self.subTest(ancestor=ancestor.name):
                self.assertTrue(self.protections.intersects_mutation(ancestor))

    def test_an_ancestor_of_the_active_log_directory_intersects_a_mutation(
        self,
    ) -> None:
        for ancestor in (self.log_dir.parent, self.workspace):
            with self.subTest(ancestor=ancestor.name):
                self.assertFalse(self.protections.protects(ancestor))
                self.assertTrue(self.protections.intersects_mutation(ancestor))

    def test_an_ancestor_of_a_database_sidecar_intersects_a_mutation(self) -> None:
        sidecars_only = FilesystemProtections(
            database_path=self.temporary_path / "elsewhere" / "maria.db",
            log_dir=self.temporary_path / "elsewhere-logs",
            prompt_path=self.temporary_path / "elsewhere-prompt.txt",
        )
        sidecar_parent = self.temporary_path / "elsewhere"

        self.assertFalse(sidecars_only.protects(sidecar_parent))
        self.assertTrue(sidecars_only.intersects_mutation(sidecar_parent))
        for sidecar in sidecars_only.database_sidecars:
            with self.subTest(sidecar=sidecar.name):
                self.assertTrue(sidecars_only.intersects_mutation(sidecar))

    def test_a_protected_path_itself_intersects_a_mutation(self) -> None:
        for protected in (self.database, self.prompt, self.log_dir):
            with self.subTest(protected=protected.name):
                self.assertTrue(self.protections.intersects_mutation(protected))

    def test_a_fixed_layout_exclusion_intersects_a_mutation(self) -> None:
        for path in (DATABASE_PATH, DEFAULT_LOG_FILE, LOCAL_CONFIG_PATH, GIT_DIR):
            with self.subTest(path=path.name):
                self.assertTrue(self.protections.intersects_mutation(path))

    def test_an_unrelated_path_does_not_intersect_a_mutation(self) -> None:
        for relative in ("notes.txt", "inbox", "inbox/order.txt", "state.backup"):
            with self.subTest(path=relative):
                self.assertFalse(
                    self.protections.intersects_mutation(self.workspace / relative)
                )

    def test_a_nonexistent_ancestor_still_intersects_a_mutation(self) -> None:
        absent = FilesystemProtections(
            database_path=self.temporary_path / "future" / "db" / "maria.db",
            log_dir=self.temporary_path / "future" / "logs",
            prompt_path=self.temporary_path / "future" / "prompts" / "system.txt",
        )
        would_be_ancestor = self.temporary_path / "future" / "prompts"

        self.assertFalse(would_be_ancestor.exists())
        self.assertTrue(absent.intersects_mutation(would_be_ancestor))

    def test_each_supplied_identity_is_checked_independently(self) -> None:
        ordinary = self.workspace / "notes.txt"

        self.assertTrue(self.protections.intersects_mutation(self.state, ordinary))
        self.assertTrue(self.protections.intersects_mutation(ordinary, self.state))
        self.assertFalse(self.protections.intersects_mutation(ordinary, ordinary))
        self.assertFalse(self.protections.intersects_mutation())


class DatabaseSidecarAliasTests(unittest.TestCase):
    """Prove a sidecar is protected by spelling and by canonical target."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_path / "MariaWorkspace"
        self.workspace.mkdir()
        self.runtime = self.temporary_path / "runtime"
        self.runtime.mkdir()
        self.database = self.runtime / "maria.db"
        self.database.write_text("DB", encoding="utf-8")

    def _protections(self) -> FilesystemProtections:
        """Return protections settled against the current filesystem."""
        return FilesystemProtections(
            database_path=self.database,
            log_dir=self.temporary_path / "runtime-logs",
            prompt_path=self.runtime / "system_prompt.txt",
        )

    def test_lexical_sidecars_are_protected_when_nothing_is_linked(self) -> None:
        protections = self._protections()

        self.assertEqual(
            protections.database_sidecars,
            tuple(
                self.database.with_name(f"maria.db{suffix}")
                for suffix in DATABASE_SIDECAR_SUFFIXES
            ),
        )
        for sidecar in protections.database_sidecars:
            with self.subTest(sidecar=sidecar.name):
                self.assertTrue(protections.protects(sidecar))

    def test_a_linked_sidecar_protects_its_canonical_target_too(self) -> None:
        alias = self.workspace / "wal-alias"
        alias.mkdir()
        lexical = self.database.with_name("maria.db-wal")
        link_directory(lexical, alias)

        protections = self._protections()
        canonical = lexical.resolve()

        self.assertEqual(canonical, alias)
        self.assertTrue(protections.protects(lexical))
        self.assertTrue(protections.protects(canonical))
        self.assertTrue(protections.excludes(canonical, canonical))
        self.assertIn(alias, protections.database_sidecars)

    def test_a_linked_file_sidecar_protects_its_target_deterministically(self) -> None:
        """Cover a file-shaped sidecar link without a privilege-dependent skip.

        A file symbolic link needs a privilege Windows may withhold, so the
        resolution step alone is substituted here. Everything else is the real
        settling path, and the assertion is the same one the junction case
        makes: the canonical target joins the protected identities.
        """
        target = self.workspace / "relocated-wal.bin"
        target.write_text("WAL-PRIVATE", encoding="utf-8")
        lexical = self.database.with_name("maria.db-wal")
        original_resolve = Path.resolve

        def aliasing_resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
            if self == lexical:
                return target
            return original_resolve(self, *args, **kwargs)

        with patch.object(Path, "resolve", aliasing_resolve):
            protections = self._protections()

        self.assertIn(target, protections.database_sidecars)
        self.assertTrue(protections.protects(target))
        self.assertTrue(protections.protects(lexical))

    def test_an_unrelated_neighbour_of_the_database_stays_usable(self) -> None:
        protections = self._protections()

        for name in ("maria.db.notes", "notes.txt", "maria.dbx", "other.db"):
            with self.subTest(name=name):
                neighbour = self.database.with_name(name)
                self.assertFalse(protections.protects(neighbour))
                self.assertFalse(protections.excludes(neighbour, neighbour))

    def test_the_database_parent_directory_is_not_directly_excluded(self) -> None:
        protections = self._protections()

        self.assertFalse(protections.protects(self.runtime))
        self.assertFalse(protections.excludes(self.runtime, self.runtime))
        self.assertTrue(protections.intersects_mutation(self.runtime))

    def test_an_unreadable_sidecar_keeps_its_lexical_protection(self) -> None:
        lexical = self.database.with_name("maria.db-shm")
        original_resolve = Path.resolve

        def failing_resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
            if self == lexical:
                raise OSError(13, "blocked")
            return original_resolve(self, *args, **kwargs)

        with patch.object(Path, "resolve", failing_resolve):
            protections = self._protections()

        self.assertIn(lexical, protections.database_sidecars)
        self.assertTrue(protections.protects(lexical))

    def test_the_derived_field_cannot_be_forged(self) -> None:
        protections = self._protections()
        forged = self.workspace / "forged.txt"

        replaced = protections._replace(database_sidecars=(forged,))
        made = FilesystemProtections._make(
            (
                self.database,
                self.temporary_path / "runtime-logs",
                self.runtime / "system_prompt.txt",
                (forged,),
            )
        )

        self.assertEqual(replaced.database_sidecars, protections.database_sidecars)
        self.assertEqual(made.database_sidecars, protections.database_sidecars)
        self.assertNotIn(forged, replaced.database_sidecars)
        self.assertEqual(pickle.loads(pickle.dumps(protections)), protections)


class ProtectedDescendantTests(unittest.TestCase):
    """Prove a wrong-shaped protected identity cannot leak through a child."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_path / "MariaWorkspace"
        self.runtime = self.workspace / "runtime"
        self.runtime.mkdir(parents=True)
        self.database = self.runtime / "maria.db"
        self.prompt = self.runtime / "system_prompt.txt"
        self.log_dir = self.temporary_path / "runtime-logs"
        self.protections = FilesystemProtections(
            database_path=self.database,
            log_dir=self.log_dir,
            prompt_path=self.prompt,
        )

    def test_a_descendant_of_every_protected_identity_is_protected(self) -> None:
        identities = {
            "database": self.database,
            "prompt": self.prompt,
            "log_dir": self.log_dir,
            **{
                f"sidecar{index}": sidecar
                for index, sidecar in enumerate(
                    self.protections.database_sidecars
                )
            },
        }

        for label, identity in identities.items():
            for relative in ("child.txt", "nested/deep/child.txt"):
                with self.subTest(identity=label, path=relative):
                    descendant = identity / relative
                    self.assertTrue(self.protections.protects(descendant))
                    self.assertTrue(
                        self.protections.excludes(descendant, descendant)
                    )

    def test_each_protected_identity_itself_remains_protected(self) -> None:
        for identity in (
            self.database,
            self.prompt,
            self.log_dir,
            *self.protections.database_sidecars,
        ):
            with self.subTest(identity=identity.name):
                self.assertTrue(self.protections.protects(identity))

    def test_an_ancestor_is_still_not_directly_protected(self) -> None:
        for ancestor in (self.runtime, self.workspace, self.temporary_path):
            with self.subTest(ancestor=ancestor.name):
                self.assertFalse(self.protections.protects(ancestor))
                self.assertFalse(self.protections.excludes(ancestor, ancestor))
                self.assertTrue(self.protections.intersects_mutation(ancestor))

    def test_a_sibling_sharing_a_name_prefix_stays_unprotected(self) -> None:
        for name in (
            "maria.db.notes",
            "maria.dbx",
            "maria.db-walrus",
            "system_prompt.txt.bak",
            "other.db",
        ):
            with self.subTest(name=name):
                neighbour = self.runtime / name
                self.assertFalse(self.protections.protects(neighbour))
                self.assertFalse(
                    self.protections.excludes(neighbour, neighbour)
                )

    def test_a_linked_sidecar_protects_the_target_and_its_children(self) -> None:
        alias = self.workspace / "wal-alias"
        alias.mkdir()
        (alias / "leaked.txt").write_text("WAL-PRIVATE", encoding="utf-8")
        link_directory(self.database.with_name("maria.db-wal"), alias)

        protections = FilesystemProtections(
            database_path=self.database,
            log_dir=self.log_dir,
            prompt_path=self.prompt,
        )

        self.assertIn(alias, protections.database_sidecars)
        self.assertTrue(protections.protects(alias))
        self.assertTrue(protections.protects(alias / "leaked.txt"))
        self.assertTrue(protections.protects(alias / "nested" / "deep.txt"))
        self.assertFalse(protections.protects(self.workspace))


class FilesystemProtectionsSnapshotTests(unittest.TestCase):
    """Prove copying an already-settled value never re-reads the filesystem."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.runtime = self.temporary_path / "runtime"
        self.runtime.mkdir()
        self.database = self.runtime / "maria.db"
        self.database.write_text("DB", encoding="utf-8")
        self.sidecar = self.database.with_name("maria.db-wal")
        self.first = self.temporary_path / "wal-target-one"
        self.second = self.temporary_path / "wal-target-two"
        self.first.mkdir()
        self.second.mkdir()

        link_directory(self.sidecar, self.first)
        self.settled = self._build()
        self.payload = pickle.dumps(self.settled)

        # Repoint the sidecar after the value was settled and serialised.
        # The link itself is removed, never its target: a POSIX directory
        # symbolic link is unlinked, while a Windows junction is a directory
        # entry and is removed with rmdir. Both operations detach only the
        # link, so wal-target-one keeps the payload the snapshot was settled
        # against.
        if self.sidecar.is_symlink():
            self.sidecar.unlink()
        else:
            self.sidecar.rmdir()
        link_directory(self.sidecar, self.second)

    def _build(self) -> FilesystemProtections:
        """Construct a protection value against the current filesystem."""
        return FilesystemProtections(
            database_path=self.database,
            log_dir=self.temporary_path / "logs",
            prompt_path=self.runtime / "system_prompt.txt",
        )

    def test_a_fresh_construction_sees_the_new_target(self) -> None:
        rebuilt = self._build()

        self.assertIn(self.second, rebuilt.database_sidecars)
        self.assertNotIn(self.first, rebuilt.database_sidecars)
        self.assertIn(self.first, self.settled.database_sidecars)
        self.assertNotEqual(rebuilt, self.settled)

    def test_every_duplicate_preserves_the_settled_snapshot(self) -> None:
        duplicates = {
            "copy": copy.copy(self.settled),
            "deepcopy": copy.deepcopy(self.settled),
            "pickle": pickle.loads(self.payload),
        }

        for label, duplicate in duplicates.items():
            with self.subTest(operation=label):
                self.assertEqual(duplicate, self.settled)
                self.assertIn(self.first, duplicate.database_sidecars)
                self.assertNotIn(self.second, duplicate.database_sidecars)
                self.assertEqual(
                    duplicate.database_sidecars,
                    self.settled.database_sidecars,
                )
                self.assertIs(type(duplicate), FilesystemProtections)
                self.assertFalse(hasattr(duplicate, "__dict__"))

    def test_every_duplicate_hashes_and_matches_the_original(self) -> None:
        duplicates = (
            copy.copy(self.settled),
            copy.deepcopy(self.settled),
            pickle.loads(self.payload),
        )
        holder = {self.settled: "settled"}
        members = {self.settled}

        for duplicate in duplicates:
            with self.subTest(duplicate=id(duplicate)):
                self.assertEqual(hash(duplicate), hash(self.settled))
                self.assertIn(duplicate, holder)
                self.assertIn(duplicate, members)
                self.assertEqual(holder[duplicate], "settled")

        # Each duplicate addresses the same entry rather than adding one, so
        # writing through any of them is writing through all of them.
        for duplicate in duplicates:
            holder[duplicate] = "replaced"
            members.add(duplicate)

        self.assertEqual(len(holder), 1)
        self.assertEqual(len(members), 1)
        self.assertEqual(holder[self.settled], "replaced")

    def test_no_duplication_route_resolves_a_path(self) -> None:
        payload = self.payload

        with patch.object(
            Path,
            "resolve",
            side_effect=AssertionError("duplication resolved a path"),
        ):
            duplicates = (
                copy.copy(self.settled),
                copy.deepcopy(self.settled),
                pickle.loads(payload),
            )

        for duplicate in duplicates:
            with self.subTest(duplicate=id(duplicate)):
                self.assertEqual(duplicate, self.settled)

    def test_a_rebuilt_snapshot_of_the_wrong_shape_is_refused(self) -> None:
        cases = {
            "short": (self.database, self.temporary_path),
            "not_a_tuple": [
                self.database,
                self.temporary_path,
                self.database,
                (),
            ],
            "sidecars_not_a_tuple": (
                self.database,
                self.temporary_path,
                self.database,
                [self.first],
            ),
            "relative_field": (
                Path("relative"),
                self.temporary_path,
                self.database,
                (),
            ),
            "text_sidecar": (
                self.database,
                self.temporary_path,
                self.database,
                (str(self.first),),
            ),
        }

        for label, snapshot in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as caught:
                    _rebuild_filesystem_protections(snapshot)
                self.assertEqual(
                    str(caught.exception),
                    PROTECTION_SNAPSHOT_MESSAGE,
                )
                self.assertNotIn(str(self.temporary_path), str(caught.exception))

    def test_replace_still_cannot_forge_the_derived_field(self) -> None:
        forged = self.temporary_path / "forged"

        replaced = self.settled._replace(database_sidecars=(forged,))

        self.assertNotIn(forged, replaced.database_sidecars)
        self.assertIn(self.second, replaced.database_sidecars)

    def test_replace_recomputes_the_derived_field_for_a_new_database(
        self,
    ) -> None:
        moved = self.temporary_path / "moved" / "maria.db"

        replaced = self.settled._replace(database_path=moved)

        self.assertEqual(replaced.database_path, moved)
        self.assertEqual(
            replaced.database_sidecars,
            tuple(
                moved.with_name(f"maria.db{suffix}")
                for suffix in DATABASE_SIDECAR_SUFFIXES
            ),
        )
        self.assertNotIn(self.first, replaced.database_sidecars)

    def test_replace_of_another_field_still_validates(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.settled._replace(log_dir="elsewhere")

        self.assertEqual(str(caught.exception), PROTECTION_PATH_TYPE_MESSAGE)


if __name__ == "__main__":
    unittest.main()
