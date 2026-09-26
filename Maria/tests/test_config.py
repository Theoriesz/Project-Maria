"""Focused tests for Maria's permission configuration loading."""

from __future__ import annotations

import inspect
import json
import os
import stat
import tomllib
import traceback
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import core.config as config_module
from core.config import (
    LOCAL_OVERRIDE_ENTRY_MESSAGE,
    LOCAL_OVERRIDE_SECTIONS_MESSAGE,
    LOCAL_OVERRIDE_TOML_MESSAGE,
    LOCAL_OVERRIDE_TOO_LARGE_MESSAGE,
    LOCAL_OVERRIDE_UNREADABLE_MESSAGE,
    MAXIMUM_LOCAL_OVERRIDE_BYTES,
    REASONING_ENABLED_REQUIRED_MESSAGE,
    REASONING_MISSING_FIELD_MESSAGE,
    REASONING_SECTION_TABLE_MESSAGE,
    REASONING_TRACKED_ENABLED_MESSAGE,
    REASONING_UNKNOWN_KEY_MESSAGE,
)
from core.config import (
    FILESYSTEM_DEFAULT_ROOT_TEXT_MESSAGE,
    FILESYSTEM_DISABLED_DEFAULT_MESSAGE,
    FILESYSTEM_DUPLICATE_NAME_MESSAGE,
    FILESYSTEM_DUPLICATE_PATH_MESSAGE,
    FILESYSTEM_OVERLAP_MESSAGE,
    FILESYSTEM_PROJECT_MARIA_ENABLED_MESSAGE,
    FILESYSTEM_PROJECT_MARIA_PATH_MESSAGE,
    FILESYSTEM_PROJECT_MARIA_POLICY_MESSAGE,
    FILESYSTEM_ROOT_ENABLED_MESSAGE,
    FILESYSTEM_ROOT_INVENTORY_MESSAGE,
    FILESYSTEM_ROOT_IS_A_FILE_MESSAGE,
    FILESYSTEM_ROOT_KEYS_MESSAGE,
    FILESYSTEM_ROOT_MISSING_FIELD_MESSAGE,
    FILESYSTEM_ROOT_POLICY_MESSAGE,
    FILESYSTEM_ROOT_TEXT_MESSAGE,
    FILESYSTEM_ROOTS_ARRAY_MESSAGE,
    FILESYSTEM_SECTION_REQUIRED_MESSAGE,
    FILESYSTEM_SECTION_TABLE_MESSAGE,
    FILESYSTEM_UNKNOWN_DEFAULT_MESSAGE,
    FILESYSTEM_UNKNOWN_KEY_MESSAGE,
    FILESYSTEM_WORKSPACE_DEFAULT_MESSAGE,
    FILESYSTEM_WORKSPACE_DISABLED_MESSAGE,
    FILESYSTEM_WORKSPACE_OVERLAP_MESSAGE,
    FILESYSTEM_WORKSPACE_PATH_MESSAGE,
    FILESYSTEM_WORKSPACE_POLICY_MESSAGE,
    MariaConfig,
    _reject_unapproved_production_roots,
    load_config,
)
from core.constants import (
    APP_VERSION,
    CONFIG_PATH,
    PROJECT_ROOT,
    REPOSITORY_ROOT,
)
from core.filesystem_roots import (
    FilesystemProtections,
    FilesystemRoot,
    RootPolicy,
)
from core.permissions import (
    MANDATORY_CONFIRMATION_ACTIONS,
    MANDATORY_DENIED_ACTIONS,
    PermissionDecision,
    PermissionManager,
)
from core.reasoning import (
    DISABLED_REASONING,
    REASONING_DEADLINE_MESSAGE,
    REASONING_DISABLED_FIELDS_MESSAGE,
    REASONING_ENABLED_MESSAGE,
    REASONING_HOST_MESSAGE,
    REASONING_MAX_INPUT_MESSAGE,
    REASONING_MAX_OUTPUT_MESSAGE,
    REASONING_MAXIMUM_INPUT_BYTES,
    REASONING_MAXIMUM_OUTPUT_BYTES,
    REASONING_MODEL_MESSAGE,
    REASONING_TRANSPORT_MESSAGE,
    ReasoningSettings,
    ReasoningTransport,
)


# The exact production filesystem declaration, appended to every fixture that
# is testing something other than the filesystem section itself. It is written
# once here so a permission fixture never has to restate Maria's root policy.
VALID_FILESYSTEM_SECTION = """
[filesystem]
default_root = "workspace"

[[filesystem.roots]]
name = "workspace"
path = "~/MariaWorkspace"
policy = "organize"
enabled = true

[[filesystem.roots]]
name = "project_maria"
path = ".."
policy = "read_only"
enabled = false
"""


class ConfigTests(unittest.TestCase):
    """Verify permission validation without touching runtime state."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.config_path = Path(self.temporary_directory.name) / "config.toml"
        self.home = Path(self.temporary_directory.name) / "home"
        self.home.mkdir()
        # Both variables are patched so home expansion lands in this temporary
        # directory on every platform, and the owner's real ~/MariaWorkspace is
        # never named, created, or read by a test.
        home_environment = patch.dict(
            os.environ,
            {"HOME": str(self.home), "USERPROFILE": str(self.home)},
        )
        home_environment.start()
        self.addCleanup(home_environment.stop)
        self.expected_workspace = Path("~/MariaWorkspace").expanduser().resolve()

    def test_shipped_configuration_loads_with_existing_values(self) -> None:
        config = load_config(CONFIG_PATH)

        self.assertEqual(config.app_name, "Maria")
        self.assertEqual(config.version, "0.2.0")
        self.assertEqual(config.environment, "development")
        self.assertEqual(
            config.database_path,
            (PROJECT_ROOT / "database" / "maria.db").resolve(),
        )
        self.assertEqual(config.log_dir, (PROJECT_ROOT / "logs").resolve())
        self.assertEqual(
            config.prompt_path,
            (PROJECT_ROOT / "prompts" / "system_prompt.txt").resolve(),
        )
        self.assertEqual(config.filesystem_roots.default_name, "workspace")
        self.assertEqual(
            tuple(root.name for root in config.filesystem_roots.roots),
            ("workspace", "project_maria"),
        )
        self.assertEqual(config.log_level, "INFO")
        self.assertTrue(
            MANDATORY_CONFIRMATION_ACTIONS <= config.confirmation_required
        )
        self.assertTrue(MANDATORY_DENIED_ACTIONS <= config.denied_actions)

    def test_shipped_configuration_version_matches_runtime_version(self) -> None:
        config = load_config(CONFIG_PATH)

        self.assertEqual(
            config.version,
            APP_VERSION,
            "Shipped configuration version must match APP_VERSION.",
        )

    def test_missing_permissions_section_uses_mandatory_baseline(self) -> None:
        config = self._load("[app]\nname = \"Temporary Maria\"\n")

        self.assertEqual(
            config.confirmation_required,
            MANDATORY_CONFIRMATION_ACTIONS,
        )
        self.assertEqual(config.denied_actions, MANDATORY_DENIED_ACTIONS)

    def test_missing_confirmation_key_uses_mandatory_baseline(self) -> None:
        config = self._load(
            "[permissions]\n"
            "deny = [\"notes.remove\"]\n"
        )

        self.assertEqual(
            config.confirmation_required,
            MANDATORY_CONFIRMATION_ACTIONS,
        )
        self.assertEqual(
            config.denied_actions,
            MANDATORY_DENIED_ACTIONS | {"notes.remove"},
        )

    def test_missing_deny_key_uses_mandatory_baseline(self) -> None:
        config = self._load(
            "[permissions]\n"
            "require_confirmation = [\"notes.create\"]\n"
        )

        self.assertEqual(
            config.confirmation_required,
            MANDATORY_CONFIRMATION_ACTIONS | {"notes.create"},
        )
        self.assertEqual(config.denied_actions, MANDATORY_DENIED_ACTIONS)

    def test_empty_permission_arrays_cannot_remove_mandatory_actions(self) -> None:
        config = self._load(
            "[permissions]\n"
            "require_confirmation = []\n"
            "deny = []\n"
        )

        self.assertEqual(
            config.confirmation_required,
            MANDATORY_CONFIRMATION_ACTIONS,
        )
        self.assertEqual(config.denied_actions, MANDATORY_DENIED_ACTIONS)

    def test_additional_permission_actions_are_preserved(self) -> None:
        config = self._load(
            "[permissions]\n"
            "require_confirmation = [\"notes.create\"]\n"
            "deny = [\"memory.store\"]\n"
        )

        self.assertEqual(
            config.confirmation_required,
            MANDATORY_CONFIRMATION_ACTIONS | {"notes.create"},
        )
        self.assertEqual(
            config.denied_actions,
            MANDATORY_DENIED_ACTIONS | {"memory.store"},
        )

    def test_valid_identifier_examples_and_future_actions_are_accepted(self) -> None:
        configured_actions = {
            "filesystem.rename",
            "filesystem.move",
            "memory.store",
            "notes.create",
            "tool_name.action_2",
            "calendar.create",
        }
        entries = ", ".join(json.dumps(action) for action in configured_actions)

        config = self._load(
            "[permissions]\n"
            f"require_confirmation = [{entries}]\n"
        )

        self.assertTrue(configured_actions <= config.confirmation_required)

    def test_cross_list_overlap_is_preserved_and_denied_wins(self) -> None:
        config = self._load(
            "[permissions]\n"
            "require_confirmation = [\"calendar.create\"]\n"
            "deny = [\"calendar.create\"]\n"
        )
        manager = PermissionManager(
            config.confirmation_required,
            config.denied_actions,
            logger=Mock(),
        )

        self.assertIn("calendar.create", config.confirmation_required)
        self.assertIn("calendar.create", config.denied_actions)
        self.assertEqual(
            manager.check("calendar", "create").decision,
            PermissionDecision.DENIED,
        )

    def test_loaded_permission_collections_are_frozensets(self) -> None:
        config = self._load(
            "[permissions]\n"
            "require_confirmation = [\"notes.create\"]\n"
            "deny = [\"memory.store\"]\n"
        )

        self.assertIsInstance(config.confirmation_required, frozenset)
        self.assertIsInstance(config.denied_actions, frozenset)

    def test_non_table_permissions_values_are_rejected(self) -> None:
        cases = {
            "string": '"filesystem.rename"',
            "integer": "1",
            "boolean": "true",
            "array": '["filesystem.rename"]',
        }

        for value_type, value in cases.items():
            with self.subTest(value_type=value_type):
                with self.assertRaises(ValueError) as raised:
                    self._load(f"permissions = {value}\n")
                self.assertIn("[permissions]", str(raised.exception))
                self.assertIn("TOML table", str(raised.exception))

    def test_permission_keys_with_wrong_container_types_are_rejected(self) -> None:
        cases = {
            "string": '"filesystem.rename"',
            "integer": "1",
            "boolean": "true",
            "float": "1.5",
            "table": '{ action = "filesystem.rename" }',
        }

        for key in ("require_confirmation", "deny"):
            for value_type, value in cases.items():
                with self.subTest(key=key, value_type=value_type):
                    with self.assertRaises(ValueError) as raised:
                        self._load(
                            "[permissions]\n"
                            f"{key} = {value}\n"
                        )
                    message = str(raised.exception)
                    self.assertIn(f"permissions.{key}", message)
                    self.assertIn("TOML array", message)

    def test_non_string_permission_entries_are_rejected(self) -> None:
        cases = {
            "integer": "123",
            "boolean": "true",
            "float": "1.5",
            "nested_array": '["memory.store"]',
            "inline_table": '{ action = "memory.store" }',
        }

        for key in ("require_confirmation", "deny"):
            for value_type, value in cases.items():
                with self.subTest(key=key, value_type=value_type):
                    with self.assertRaises(ValueError) as raised:
                        self._load(
                            "[permissions]\n"
                            f"{key} = [{value}]\n"
                        )
                    message = str(raised.exception)
                    self.assertIn(f"permissions.{key}", message)
                    self.assertIn("strings", message)

    def test_invalid_permission_identifiers_are_rejected(self) -> None:
        cases = {
            "empty": "",
            "whitespace_only": "   ",
            "leading_whitespace": " filesystem.rename",
            "trailing_whitespace": "filesystem.rename ",
            "internal_whitespace": "filesystem rename",
            "missing_period": "filesystem",
            "empty_tool": ".rename",
            "empty_action": "filesystem.",
            "multiple_periods": "filesystem.rename.extra",
            "uppercase_tool": "Filesystem.rename",
            "uppercase_action": "filesystem.Rename",
            "hyphenated_segment": "filesystem-ops.rename",
            "digit_started_tool": "1filesystem.rename",
            "digit_started_action": "filesystem.2rename",
            "underscore_started_tool": "_filesystem.rename",
            "underscore_started_action": "filesystem._rename",
            "non_ascii_letter": "fílesystem.rename",
        }

        for key in ("require_confirmation", "deny"):
            for description, identifier in cases.items():
                with self.subTest(key=key, description=description):
                    with self.assertRaises(ValueError) as raised:
                        self._load(
                            "[permissions]\n"
                            f"{key} = [{json.dumps(identifier)}]\n"
                        )
                    message = str(raised.exception)
                    self.assertIn(f"permissions.{key}", message)
                    self.assertIn("lowercase tool.action", message)
                    if description == "non_ascii_letter":
                        self.assertIn("ASCII", message)

    def test_duplicate_entries_are_rejected_with_key_specific_errors(self) -> None:
        for key in ("require_confirmation", "deny"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as raised:
                    self._load(
                        "[permissions]\n"
                        f"{key} = [\"memory.store\", \"memory.store\"]\n"
                    )
                message = str(raised.exception)
                self.assertIn(f"permissions.{key}", message)
                self.assertIn("duplicate", message.lower())

    def test_relative_paths_still_resolve_from_project_root(self) -> None:
        config = self._load(
            "[paths]\n"
            "database = \"state/test.db\"\n"
            "logs = \"state/logs\"\n"
            "system_prompt = \"state/prompt.txt\"\n"
        )

        self.assertEqual(
            config.database_path,
            (PROJECT_ROOT / "state" / "test.db").resolve(),
        )
        self.assertEqual(
            config.log_dir,
            (PROJECT_ROOT / "state" / "logs").resolve(),
        )
        self.assertEqual(
            config.prompt_path,
            (PROJECT_ROOT / "state" / "prompt.txt").resolve(),
        )

    def test_absolute_paths_remain_accepted(self) -> None:
        absolute_path = Path(self.temporary_directory.name) / "absolute.db"

        config = self._load(
            "[paths]\n"
            f"database = {json.dumps(absolute_path.as_posix())}\n"
        )

        self.assertEqual(config.database_path, absolute_path.resolve())

    def test_existing_app_and_logging_defaults_remain_unchanged(self) -> None:
        config = self._load("")

        self.assertEqual(config.app_name, "Maria")
        self.assertEqual(config.version, APP_VERSION)
        self.assertEqual(config.environment, "development")
        self.assertEqual(config.log_level, "INFO")

    def test_existing_logging_level_loading_remains_unchanged(self) -> None:
        cases = {
            "string": ("\"DEBUG\"", "DEBUG"),
            "integer": ("10", "10"),
        }

        for value_type, (value, expected) in cases.items():
            with self.subTest(value_type=value_type):
                config = self._load(
                    "[logging]\n"
                    f"level = {value}\n"
                )
                self.assertEqual(config.log_level, expected)

    def test_unknown_configuration_keys_remain_ignored(self) -> None:
        config = self._load(
            "unknown_top_level = \"ignored\"\n"
            "[app]\n"
            "name = \"Configured Maria\"\n"
            "unknown_app_key = \"ignored\"\n"
            "[paths]\n"
            "unknown_path_key = \"ignored\"\n"
            "[logging]\n"
            "unknown_logging_key = \"ignored\"\n"
            "[permissions]\n"
            "unknown_permission_key = \"ignored\"\n"
        )

        self.assertEqual(config.app_name, "Configured Maria")
        self.assertEqual(
            config.database_path,
            (PROJECT_ROOT / "database" / "maria.db").resolve(),
        )
        self.assertEqual(config.log_level, "INFO")
        self.assertEqual(
            config.confirmation_required,
            MANDATORY_CONFIRMATION_ACTIONS,
        )
        self.assertEqual(config.denied_actions, MANDATORY_DENIED_ACTIONS)

    def _load(self, content: str) -> MariaConfig:
        """Write and load one temporary TOML configuration.

        The valid filesystem declaration is appended rather than prepended, so
        a fixture's own bare keys stay in the table they were written for.
        """
        return self._load_raw(content + VALID_FILESYSTEM_SECTION)

    def _load_raw(self, content: str) -> MariaConfig:
        """Write and load one temporary TOML configuration exactly as given."""
        self.config_path.write_text(content, encoding="utf-8")
        return load_config(self.config_path)


WORKSPACE_TABLE = """[[filesystem.roots]]
name = "workspace"
path = "~/MariaWorkspace"
policy = "organize"
enabled = true
"""

PROJECT_MARIA_TABLE = """[[filesystem.roots]]
name = "project_maria"
path = ".."
policy = "read_only"
enabled = false
"""


class FilesystemConfigurationTests(unittest.TestCase):
    """Verify Maria's named filesystem roots load strictly or not at all."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.config_path = self.temporary_path / "config.toml"
        self.home = self.temporary_path / "home"
        self.home.mkdir()
        home_environment = patch.dict(
            os.environ,
            {"HOME": str(self.home), "USERPROFILE": str(self.home)},
        )
        home_environment.start()
        self.addCleanup(home_environment.stop)
        self.expected_workspace = Path("~/MariaWorkspace").expanduser().resolve()

    def test_the_shipped_declaration_is_exactly_two_semantic_roots(self) -> None:
        roots = load_config(CONFIG_PATH).filesystem_roots

        workspace = roots.by_name("workspace")
        project_maria = roots.by_name("project_maria")

        self.assertEqual(roots.default_name, "workspace")
        self.assertEqual(roots.default_root, workspace)
        self.assertEqual(workspace.path, self.expected_workspace)
        self.assertEqual(workspace.policy, RootPolicy.ORGANIZE)
        self.assertTrue(workspace.enabled)
        self.assertEqual(project_maria.path, REPOSITORY_ROOT)
        self.assertEqual(project_maria.policy, RootPolicy.READ_ONLY)
        self.assertFalse(project_maria.enabled)
        self.assertEqual(roots.enabled_roots, (workspace,))

    def test_the_active_runtime_locations_are_settled_as_protections(self) -> None:
        config = load_config(CONFIG_PATH)

        self.assertEqual(
            config.filesystem_protections,
            FilesystemProtections(
                database_path=config.database_path,
                log_dir=config.log_dir,
                prompt_path=config.prompt_path,
            ),
        )
        self.assertEqual(
            config.filesystem_protections.database_path,
            config.database_path,
        )
        self.assertEqual(config.filesystem_protections.log_dir, config.log_dir)
        self.assertEqual(
            config.filesystem_protections.prompt_path,
            config.prompt_path,
        )

    def test_protections_follow_a_relocated_runtime_configuration(self) -> None:
        relocated = self.temporary_path / "relocated"
        content = (
            "[paths]\n"
            f"database = {json.dumps((relocated / 'db' / 'maria.db').as_posix())}\n"
            f"logs = {json.dumps((relocated / 'logs').as_posix())}\n"
            f"system_prompt = {json.dumps((relocated / 'prompt.txt').as_posix())}\n"
        )

        config = self._load_raw(content + self._section())
        protections = config.filesystem_protections

        self.assertEqual(protections.database_path, relocated / "db" / "maria.db")
        self.assertEqual(protections.log_dir, relocated / "logs")
        self.assertEqual(protections.prompt_path, relocated / "prompt.txt")
        self.assertTrue(protections.protects(relocated / "logs" / "maria.log.3"))
        self.assertTrue(
            protections.protects((relocated / "db" / "maria.db-wal"))
        )
        self.assertFalse(protections.protects(relocated / "notes.txt"))

    def test_loading_protections_creates_nothing(self) -> None:
        relocated = self.temporary_path / "never-created"
        content = (
            "[paths]\n"
            f"database = {json.dumps((relocated / 'db' / 'maria.db').as_posix())}\n"
            f"logs = {json.dumps((relocated / 'logs').as_posix())}\n"
            f"system_prompt = {json.dumps((relocated / 'prompt.txt').as_posix())}\n"
        )

        self._load_raw(content + self._section())

        self.assertFalse(relocated.exists())

    def test_the_shipped_workspace_path_names_no_user_or_machine(self) -> None:
        with CONFIG_PATH.open("rb") as handle:
            raw = tomllib.load(handle)
        declared = {
            root["name"]: root["path"] for root in raw["filesystem"]["roots"]
        }

        self.assertEqual(declared["workspace"], "~/MariaWorkspace")
        self.assertEqual(declared["project_maria"], "..")

        workspace = load_config(CONFIG_PATH).filesystem_roots.by_name("workspace")
        self.assertTrue(workspace.path.is_relative_to(self.home))
        self.assertEqual(workspace.path.name, "MariaWorkspace")

    def test_loading_creates_nothing_on_the_filesystem(self) -> None:
        before = sorted(entry.name for entry in self.home.iterdir())

        load_config(CONFIG_PATH)

        self.assertEqual(
            sorted(entry.name for entry in self.home.iterdir()),
            before,
        )
        self.assertFalse(self.expected_workspace.exists())

    def test_a_missing_or_non_table_filesystem_section_is_refused(self) -> None:
        with self.assertRaises(ValueError) as missing:
            self._load_raw('[app]\nname = "Maria"\n')
        self.assertEqual(
            str(missing.exception),
            FILESYSTEM_SECTION_REQUIRED_MESSAGE,
        )

        for label, value in {
            "string": '"workspace"',
            "integer": "1",
            "boolean": "true",
            "array": '["workspace"]',
        }.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as caught:
                    self._load_raw(f"filesystem = {value}\n")
                self.assertEqual(
                    str(caught.exception),
                    FILESYSTEM_SECTION_TABLE_MESSAGE,
                )

    def test_an_unknown_key_inside_the_filesystem_section_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._load_raw(self._section(extra='unknown_key = "ignored"\n'))

        self.assertEqual(str(caught.exception), FILESYSTEM_UNKNOWN_KEY_MESSAGE)

    def test_roots_must_be_a_non_empty_array_of_tables(self) -> None:
        cases = {
            "missing": '[filesystem]\ndefault_root = "workspace"\n',
            "string": '[filesystem]\ndefault_root = "workspace"\nroots = "x"\n',
            "empty": '[filesystem]\ndefault_root = "workspace"\nroots = []\n',
            "table": (
                "[filesystem]\n"
                'default_root = "workspace"\n'
                "[filesystem.roots]\n"
                'name = "workspace"\n'
            ),
            "string_entries": (
                "[filesystem]\n"
                'default_root = "workspace"\n'
                'roots = ["workspace"]\n'
            ),
        }

        for label, content in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as caught:
                    self._load_raw(content)
                self.assertEqual(
                    str(caught.exception),
                    FILESYSTEM_ROOTS_ARRAY_MESSAGE,
                )

    def test_a_root_table_must_declare_exactly_the_four_known_fields(self) -> None:
        with self.assertRaises(ValueError) as unknown:
            self._load_raw(
                self._section(
                    workspace=WORKSPACE_TABLE + 'unknown_field = "x"\n',
                )
            )
        self.assertEqual(str(unknown.exception), FILESYSTEM_ROOT_KEYS_MESSAGE)

        for field in ("name", "path", "policy", "enabled"):
            with self.subTest(missing=field):
                reduced = "".join(
                    line
                    for line in WORKSPACE_TABLE.splitlines(keepends=True)
                    if not line.startswith(f"{field} =")
                )
                with self.assertRaises(ValueError) as caught:
                    self._load_raw(self._section(workspace=reduced))
                self.assertEqual(
                    str(caught.exception),
                    FILESYSTEM_ROOT_MISSING_FIELD_MESSAGE,
                )

    def test_root_field_types_are_settled_exactly(self) -> None:
        text_cases = {
            "name": "name = 1\n",
            "path": "path = true\n",
            "policy": "policy = 2\n",
        }
        for field, replacement in text_cases.items():
            with self.subTest(field=field):
                with self.assertRaises(ValueError) as caught:
                    self._load_raw(
                        self._section(workspace=self._replace(field, replacement))
                    )
                self.assertEqual(
                    str(caught.exception),
                    FILESYSTEM_ROOT_TEXT_MESSAGE,
                )

        for label, replacement in {
            "string": 'enabled = "true"\n',
            "integer": "enabled = 1\n",
        }.items():
            with self.subTest(enabled=label):
                with self.assertRaises(ValueError) as caught:
                    self._load_raw(
                        self._section(
                            workspace=self._replace("enabled", replacement)
                        )
                    )
                self.assertEqual(
                    str(caught.exception),
                    FILESYSTEM_ROOT_ENABLED_MESSAGE,
                )

    def test_an_unknown_policy_is_refused(self) -> None:
        for policy in ("write", "organise", "ORGANIZE", ""):
            with self.subTest(policy=policy):
                with self.assertRaises(ValueError) as caught:
                    self._load_raw(
                        self._section(
                            workspace=self._replace(
                                "policy",
                                f"policy = {json.dumps(policy)}\n",
                            )
                        )
                    )
                self.assertEqual(
                    str(caught.exception),
                    FILESYSTEM_ROOT_POLICY_MESSAGE,
                )

    def test_a_duplicate_root_name_is_refused(self) -> None:
        elsewhere = self.temporary_path / "elsewhere"
        duplicate = WORKSPACE_TABLE.replace(
            'path = "~/MariaWorkspace"',
            f"path = {json.dumps(elsewhere.as_posix())}",
        )

        with self.assertRaises(ValueError) as caught:
            self._load_raw(
                self._section(project_maria=PROJECT_MARIA_TABLE + "\n" + duplicate)
            )

        self.assertEqual(
            str(caught.exception),
            FILESYSTEM_DUPLICATE_NAME_MESSAGE,
        )

    def test_a_duplicate_canonical_path_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._load_raw(
                self._section(
                    workspace=self._replace("path", 'path = ".."\n'),
                )
            )

        self.assertEqual(
            str(caught.exception),
            FILESYSTEM_DUPLICATE_PATH_MESSAGE,
        )

    def test_a_root_containing_another_root_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._load_raw(
                self._section(
                    project_maria=PROJECT_MARIA_TABLE.replace(
                        'path = ".."',
                        'path = "."',
                    ),
                    workspace=self._replace("path", 'path = ".."\n'),
                )
            )

        self.assertEqual(str(caught.exception), FILESYSTEM_OVERLAP_MESSAGE)

    def test_a_workspace_overlapping_the_repository_is_refused(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HOME": str(REPOSITORY_ROOT),
                "USERPROFILE": str(REPOSITORY_ROOT),
            },
        ):
            with self.assertRaises(ValueError) as caught:
                self._load_raw(self._section())

        self.assertEqual(str(caught.exception), FILESYSTEM_OVERLAP_MESSAGE)
        self.assertFalse((REPOSITORY_ROOT / "MariaWorkspace").exists())

    def test_the_workspace_overlap_guard_stands_on_its_own(self) -> None:
        workspace = FilesystemRoot(
            name="workspace",
            path=REPOSITORY_ROOT / "MariaWorkspace",
            policy=RootPolicy.ORGANIZE,
            enabled=True,
        )
        project_maria = FilesystemRoot(
            name="project_maria",
            path=REPOSITORY_ROOT,
            policy=RootPolicy.READ_ONLY,
            enabled=False,
        )
        configured = (
            ("~/MariaWorkspace", workspace),
            ("..", project_maria),
        )

        with self.assertRaises(ValueError) as overlap:
            _reject_unapproved_production_roots(configured, "workspace")
        with self.assertRaises(ValueError) as default:
            _reject_unapproved_production_roots(configured, "project_maria")

        self.assertEqual(
            str(overlap.exception),
            FILESYSTEM_WORKSPACE_OVERLAP_MESSAGE,
        )
        self.assertEqual(
            str(default.exception),
            FILESYSTEM_WORKSPACE_DEFAULT_MESSAGE,
        )

    def test_a_missing_unknown_or_disabled_default_root_is_refused(self) -> None:
        cases = (
            ("missing", "", FILESYSTEM_DEFAULT_ROOT_TEXT_MESSAGE),
            ("non_text", "default_root = 1\n", FILESYSTEM_DEFAULT_ROOT_TEXT_MESSAGE),
            (
                "unknown",
                'default_root = "elsewhere"\n',
                FILESYSTEM_UNKNOWN_DEFAULT_MESSAGE,
            ),
            (
                "disabled",
                'default_root = "project_maria"\n',
                FILESYSTEM_DISABLED_DEFAULT_MESSAGE,
            ),
        )

        for label, default, expected in cases:
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as caught:
                    self._load_raw(self._section(default=default))
                self.assertEqual(str(caught.exception), expected)

    def test_the_production_root_inventory_is_exactly_two_names(self) -> None:
        elsewhere = self.temporary_path / "elsewhere"
        third = (
            "[[filesystem.roots]]\n"
            'name = "second_workspace"\n'
            f"path = {json.dumps(elsewhere.as_posix())}\n"
            'policy = "organize"\n'
            "enabled = true\n"
        )

        with self.assertRaises(ValueError) as extra:
            self._load_raw(
                self._section(project_maria=PROJECT_MARIA_TABLE + "\n" + third)
            )
        with self.assertRaises(ValueError) as missing:
            self._load_raw(self._section(project_maria=""))
        with self.assertRaises(ValueError) as renamed:
            self._load_raw(
                self._section(
                    project_maria=PROJECT_MARIA_TABLE.replace(
                        'name = "project_maria"',
                        'name = "second_workspace"',
                    )
                )
            )

        for caught in (extra, missing, renamed):
            self.assertEqual(
                str(caught.exception),
                FILESYSTEM_ROOT_INVENTORY_MESSAGE,
            )

    def test_the_workspace_semantics_are_enforced_not_assumed(self) -> None:
        cases = (
            (
                "path",
                self._replace("path", 'path = "~/Somewhere"\n'),
                FILESYSTEM_WORKSPACE_PATH_MESSAGE,
            ),
            (
                "policy",
                self._replace("policy", 'policy = "read_only"\n'),
                FILESYSTEM_WORKSPACE_POLICY_MESSAGE,
            ),
        )

        for label, workspace, expected in cases:
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as caught:
                    self._load_raw(self._section(workspace=workspace))
                self.assertEqual(str(caught.exception), expected)

    def test_a_disabled_workspace_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._load_raw(
                self._section(
                    workspace=self._replace("enabled", "enabled = false\n"),
                    default='default_root = "project_maria"\n',
                )
            )

        self.assertEqual(
            str(caught.exception),
            FILESYSTEM_DISABLED_DEFAULT_MESSAGE,
        )

    def test_the_project_maria_semantics_are_enforced_not_assumed(self) -> None:
        cases = (
            (
                "path",
                PROJECT_MARIA_TABLE.replace('path = ".."', 'path = "."'),
                FILESYSTEM_PROJECT_MARIA_PATH_MESSAGE,
            ),
            (
                "policy",
                PROJECT_MARIA_TABLE.replace(
                    'policy = "read_only"',
                    'policy = "organize"',
                ),
                FILESYSTEM_PROJECT_MARIA_POLICY_MESSAGE,
            ),
            (
                "enabled",
                PROJECT_MARIA_TABLE.replace(
                    "enabled = false",
                    "enabled = true",
                ),
                FILESYSTEM_PROJECT_MARIA_ENABLED_MESSAGE,
            ),
        )

        for label, project_maria, expected in cases:
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as caught:
                    self._load_raw(self._section(project_maria=project_maria))
                self.assertEqual(str(caught.exception), expected)

    def test_a_root_path_that_is_an_ordinary_file_is_refused(self) -> None:
        occupied = self.home / "MariaWorkspace"
        occupied.write_text("not a directory", encoding="utf-8")

        with self.assertRaises(ValueError) as caught:
            self._load_raw(self._section())

        self.assertEqual(
            str(caught.exception),
            FILESYSTEM_ROOT_IS_A_FILE_MESSAGE,
        )
        self.assertTrue(occupied.is_file())

    def test_no_filesystem_failure_message_interpolates_a_value(self) -> None:
        messages = (
            FILESYSTEM_SECTION_REQUIRED_MESSAGE,
            FILESYSTEM_SECTION_TABLE_MESSAGE,
            FILESYSTEM_UNKNOWN_KEY_MESSAGE,
            FILESYSTEM_ROOTS_ARRAY_MESSAGE,
            FILESYSTEM_ROOT_KEYS_MESSAGE,
            FILESYSTEM_ROOT_MISSING_FIELD_MESSAGE,
            FILESYSTEM_ROOT_TEXT_MESSAGE,
            FILESYSTEM_ROOT_ENABLED_MESSAGE,
            FILESYSTEM_ROOT_POLICY_MESSAGE,
            FILESYSTEM_DUPLICATE_NAME_MESSAGE,
            FILESYSTEM_DUPLICATE_PATH_MESSAGE,
            FILESYSTEM_OVERLAP_MESSAGE,
            FILESYSTEM_DEFAULT_ROOT_TEXT_MESSAGE,
            FILESYSTEM_UNKNOWN_DEFAULT_MESSAGE,
            FILESYSTEM_DISABLED_DEFAULT_MESSAGE,
            FILESYSTEM_ROOT_IS_A_FILE_MESSAGE,
            FILESYSTEM_ROOT_INVENTORY_MESSAGE,
            FILESYSTEM_WORKSPACE_PATH_MESSAGE,
            FILESYSTEM_WORKSPACE_POLICY_MESSAGE,
            FILESYSTEM_WORKSPACE_DISABLED_MESSAGE,
            FILESYSTEM_WORKSPACE_DEFAULT_MESSAGE,
            FILESYSTEM_WORKSPACE_OVERLAP_MESSAGE,
            FILESYSTEM_PROJECT_MARIA_PATH_MESSAGE,
            FILESYSTEM_PROJECT_MARIA_POLICY_MESSAGE,
            FILESYSTEM_PROJECT_MARIA_ENABLED_MESSAGE,
        )

        for message in messages:
            with self.subTest(message=message):
                self.assertNotIn("{", message)
                self.assertNotIn("%", message)
                self.assertNotIn(str(self.home), message)

    @staticmethod
    def _replace(field: str, replacement: str) -> str:
        """Return the workspace table with one field line replaced."""
        return "".join(
            replacement if line.startswith(f"{field} =") else line
            for line in WORKSPACE_TABLE.splitlines(keepends=True)
        )

    @staticmethod
    def _section(
        default: str = 'default_root = "workspace"\n',
        workspace: str = WORKSPACE_TABLE,
        project_maria: str = PROJECT_MARIA_TABLE,
        extra: str = "",
    ) -> str:
        """Assemble one filesystem section from its declared parts."""
        return "[filesystem]\n" + default + extra + "\n" + workspace + "\n" + (
            project_maria
        )

    def _load_raw(self, content: str) -> MariaConfig:
        """Write and load one temporary TOML configuration exactly as given."""
        self.config_path.write_text(content, encoding="utf-8")
        return load_config(self.config_path)


# A complete, valid enabled override block, field by field. Every value is
# synthetic and names no real machine.
ENABLED_REASONING_FIELDS = (
    ("transport", '"private_lan"'),
    ("host", '"10.20.30.40"'),
    ("model", '"synthetic-model:1b"'),
    ("request_deadline_seconds", "30"),
    ("max_input_bytes", "16384"),
    ("max_output_bytes", "8192"),
)

# Distinctive, valid values that must never be shown, logged, or echoed.
OVERRIDE_HOST_SENTINEL = "fd4c:2b19:83a7::5e"
OVERRIDE_MODEL_SENTINEL = "sentinel-model-4f8a2c:7b"
OVERRIDE_TEXT_SENTINEL = "private-override-text-58c1f0e3a2"

# Keys the tracked configuration must never carry anywhere.
MACHINE_SPECIFIC_KEYS = frozenset(
    {
        "transport",
        "host",
        "port",
        "endpoint",
        "url",
        "model",
        "model_path",
        "request_deadline_seconds",
        "max_input_bytes",
        "max_output_bytes",
        "credential",
        "credentials",
        "token",
        "api_key",
        "password",
        "secret",
        "header",
        "headers",
    }
)


def reasoning_block(
    enabled: str = "true",
    omit: frozenset[str] = frozenset(),
    **values: str,
) -> str:
    """Render one [reasoning] table, replacing or omitting named fields."""
    lines = ["[reasoning]", f"enabled = {enabled}"]
    for key, default in ENABLED_REASONING_FIELDS:
        if key not in omit:
            lines.append(f"{key} = {values.get(key, default)}")
    return "\n".join(lines) + "\n"


def all_keys(value: object) -> set[str]:
    """Return every key at every depth of a parsed TOML document."""
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(key)
            keys |= all_keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= all_keys(item)
    return keys


class ReasoningTemplateTests(unittest.TestCase):
    """Verify the tracked reasoning template is disabled and stays that way."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.config_path = self.temporary_path / "config.toml"
        self.home = self.temporary_path / "home"
        self.home.mkdir()
        home_environment = patch.dict(
            os.environ,
            {"HOME": str(self.home), "USERPROFILE": str(self.home)},
        )
        home_environment.start()
        self.addCleanup(home_environment.stop)

    def test_the_shipped_template_loads_disabled_with_no_active_field(
        self,
    ) -> None:
        reasoning = load_config(CONFIG_PATH).reasoning

        self.assertEqual(reasoning, DISABLED_REASONING)
        self.assertFalse(reasoning.enabled)
        for name in (
            "transport",
            "host",
            "model",
            "request_deadline_seconds",
            "max_input_bytes",
            "max_output_bytes",
        ):
            with self.subTest(field=name):
                self.assertIsNone(getattr(reasoning, name))

    def test_the_shipped_template_is_exactly_enabled_false(self) -> None:
        with CONFIG_PATH.open("rb") as config_file:
            raw_config = tomllib.load(config_file)

        self.assertEqual(raw_config["reasoning"], {"enabled": False})

    def test_the_shipped_file_names_no_host_endpoint_model_or_credential(
        self,
    ) -> None:
        with CONFIG_PATH.open("rb") as config_file:
            raw_config = tomllib.load(config_file)

        self.assertEqual(all_keys(raw_config) & MACHINE_SPECIFIC_KEYS, set())

    def test_a_configuration_without_a_reasoning_section_is_disabled(
        self,
    ) -> None:
        self.assertEqual(self._load("").reasoning, DISABLED_REASONING)

    def test_the_tracked_file_may_not_enable_reasoning(self) -> None:
        for block in (reasoning_block(), "[reasoning]\nenabled = true\n"):
            with self.subTest(block=block.count("\n")):
                self._assert_refused(block, REASONING_TRACKED_ENABLED_MESSAGE)

    def test_the_tracked_file_may_not_name_a_provider_field_when_disabled(
        self,
    ) -> None:
        for key, value in ENABLED_REASONING_FIELDS:
            with self.subTest(field=key):
                self._assert_refused(
                    f"[reasoning]\nenabled = false\n{key} = {value}\n",
                    REASONING_DISABLED_FIELDS_MESSAGE,
                )

    def test_the_tracked_template_must_declare_enabled(self) -> None:
        self._assert_refused("[reasoning]\n", REASONING_ENABLED_REQUIRED_MESSAGE)

    def test_a_non_table_tracked_reasoning_value_is_refused(self) -> None:
        for value in ("false", '"off"', "[]"):
            with self.subTest(value=value):
                self._assert_refused(
                    f"reasoning = {value}\n",
                    REASONING_SECTION_TABLE_MESSAGE,
                )

    def test_an_unknown_key_in_the_tracked_template_is_refused(self) -> None:
        for key in ("retries", "api_key", "endpoint", "model_path"):
            with self.subTest(key=key):
                self._assert_refused(
                    f'[reasoning]\nenabled = false\n{key} = "x"\n',
                    REASONING_UNKNOWN_KEY_MESSAGE,
                )

    def test_unknown_keys_elsewhere_stay_ignored_beside_the_template(
        self,
    ) -> None:
        config = self._load(
            'unknown_top_level = "ignored"\n'
            "[app]\n"
            'unknown_app_key = "ignored"\n'
            "[logging]\n"
            'unknown_logging_key = "ignored"\n'
            "[permissions]\n"
            'unknown_permission_key = "ignored"\n'
            "[reasoning]\n"
            "enabled = false\n"
        )

        self.assertEqual(config.reasoning, DISABLED_REASONING)
        self.assertEqual(config.log_level, "INFO")

    def _load(self, content: str) -> MariaConfig:
        """Write and load one tracked configuration, with no override."""
        self.config_path.write_text(
            content + VALID_FILESYSTEM_SECTION,
            encoding="utf-8",
        )
        return load_config(self.config_path)

    def _assert_refused(self, content: str, message: str) -> None:
        """Assert one tracked configuration is refused with one message."""
        with self.assertRaises(ValueError) as caught:
            self._load(content)
        self.assertEqual(str(caught.exception), message)


class LocalOverrideTests(unittest.TestCase):
    """Verify the owner-approved private override, using synthetic files only.

    Every override here is written into a temporary directory. No test reads,
    creates, or names the owner's real ``config/config.local.toml``.
    """

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.config_path = self.temporary_path / "config.toml"
        self.override_path = self.temporary_path / "config.local.toml"
        self.home = self.temporary_path / "home"
        self.home.mkdir()
        home_environment = patch.dict(
            os.environ,
            {"HOME": str(self.home), "USERPROFILE": str(self.home)},
        )
        home_environment.start()
        self.addCleanup(home_environment.stop)
        self.config_path.write_text(
            "[reasoning]\nenabled = false\n" + VALID_FILESYSTEM_SECTION,
            encoding="utf-8",
        )

    # The mechanism.

    def test_ordinary_callers_read_no_override(self) -> None:
        signature = inspect.signature(load_config)
        self.assertIsNone(signature.parameters["local_override_path"].default)
        self._write_override(reasoning_block())

        with patch.object(
            config_module,
            "_local_override_reasoning",
            wraps=config_module._local_override_reasoning,
        ) as reader:
            config = load_config(self.config_path)

        reader.assert_not_called()
        self.assertEqual(config.reasoning, DISABLED_REASONING)

    def test_an_absent_override_leaves_the_tracked_configuration(self) -> None:
        tracked = load_config(self.config_path)

        config = self._load()

        self.assertEqual(config, tracked)
        self.assertEqual(config.reasoning, DISABLED_REASONING)

    def test_loading_never_creates_the_override(self) -> None:
        self._load()

        with self.assertRaises(FileNotFoundError):
            os.lstat(self.override_path)

    def test_a_complete_enabled_override_replaces_the_tracked_block(
        self,
    ) -> None:
        self._write_override(reasoning_block())

        reasoning = self._load().reasoning

        self.assertEqual(
            reasoning,
            ReasoningSettings(
                enabled=True,
                transport=ReasoningTransport.PRIVATE_LAN,
                host="10.20.30.40",
                model="synthetic-model:1b",
                request_deadline_seconds=30,
                max_input_bytes=16384,
                max_output_bytes=8192,
            ),
        )

    def test_the_override_changes_nothing_but_reasoning(self) -> None:
        tracked = load_config(self.config_path)
        self._write_override(reasoning_block(transport='"loopback"', host='"::1"'))

        config = self._load()

        self.assertTrue(config.reasoning.enabled)
        self.assertEqual(replace(config, reasoning=DISABLED_REASONING), tracked)

    def test_a_disabled_override_is_accepted(self) -> None:
        self._write_override("[reasoning]\nenabled = false\n")

        self.assertEqual(self._load().reasoning, DISABLED_REASONING)

    def test_a_disabled_override_may_not_name_a_provider_field(self) -> None:
        self._assert_refused(
            '[reasoning]\nenabled = false\nhost = "10.20.30.40"\n',
            REASONING_DISABLED_FIELDS_MESSAGE,
        )

    def test_an_override_block_must_declare_enabled(self) -> None:
        block = reasoning_block().replace("enabled = true\n", "")

        self._assert_refused(block, REASONING_ENABLED_REQUIRED_MESSAGE)

    # One missing-field test for each of the six active fields.

    def test_an_enabled_override_missing_transport_fails_closed(self) -> None:
        self._assert_missing("transport")

    def test_an_enabled_override_missing_host_fails_closed(self) -> None:
        self._assert_missing("host")

    def test_an_enabled_override_missing_model_fails_closed(self) -> None:
        self._assert_missing("model")

    def test_an_enabled_override_missing_request_deadline_fails_closed(
        self,
    ) -> None:
        self._assert_missing("request_deadline_seconds")

    def test_an_enabled_override_missing_max_input_bytes_fails_closed(
        self,
    ) -> None:
        self._assert_missing("max_input_bytes")

    def test_an_enabled_override_missing_max_output_bytes_fails_closed(
        self,
    ) -> None:
        self._assert_missing("max_output_bytes")

    def test_a_partial_enabled_override_never_borrows_missing_fields(
        self,
    ) -> None:
        self._assert_refused(
            reasoning_block(
                omit=frozenset(
                    {
                        "model",
                        "request_deadline_seconds",
                        "max_input_bytes",
                        "max_output_bytes",
                    }
                )
            ),
            REASONING_MISSING_FIELD_MESSAGE,
        )
        self._assert_refused(
            "[reasoning]\nenabled = true\n",
            REASONING_MISSING_FIELD_MESSAGE,
        )

    # Wrong types, one test per field.

    def test_a_non_boolean_enabled_is_refused(self) -> None:
        for value in ('"true"', "1", "0", '"yes"'):
            with self.subTest(value=value):
                self._assert_refused(
                    reasoning_block(enabled=value),
                    REASONING_ENABLED_MESSAGE,
                )

    def test_a_non_string_transport_is_refused(self) -> None:
        self._assert_values_refused(
            "transport",
            ("1", "true", '["private_lan"]', '{ class = "private_lan" }'),
            REASONING_TRANSPORT_MESSAGE,
        )

    def test_a_non_string_host_is_refused(self) -> None:
        self._assert_values_refused(
            "host",
            ("10", "true", '["10.20.30.40"]', "1979-05-27"),
            REASONING_HOST_MESSAGE,
        )

    def test_a_non_string_model_is_refused(self) -> None:
        self._assert_values_refused(
            "model",
            ("1", "false", '["synthetic-model:1b"]'),
            REASONING_MODEL_MESSAGE,
        )

    def test_a_non_integer_request_deadline_is_refused(self) -> None:
        self._assert_values_refused(
            "request_deadline_seconds",
            ("true", "false", "30.0", '"30"', "inf", "1979-05-27"),
            REASONING_DEADLINE_MESSAGE,
        )

    def test_a_non_integer_max_input_bytes_is_refused(self) -> None:
        self._assert_values_refused(
            "max_input_bytes",
            ("true", "16384.0", '"16384"', "nan", "[16384]"),
            REASONING_MAX_INPUT_MESSAGE,
        )

    def test_a_non_integer_max_output_bytes_is_refused(self) -> None:
        self._assert_values_refused(
            "max_output_bytes",
            ("true", "8192.0", '"8192"', "-inf", "[8192]"),
            REASONING_MAX_OUTPUT_MESSAGE,
        )

    # Ranges, one test per bounded field.

    def test_the_request_deadline_range_is_enforced(self) -> None:
        self._assert_range("request_deadline_seconds", 300, REASONING_DEADLINE_MESSAGE)

    def test_the_max_input_bytes_range_is_enforced(self) -> None:
        self._assert_range(
            "max_input_bytes",
            REASONING_MAXIMUM_INPUT_BYTES,
            REASONING_MAX_INPUT_MESSAGE,
        )

    def test_the_max_output_bytes_range_is_enforced(self) -> None:
        self._assert_range(
            "max_output_bytes",
            REASONING_MAXIMUM_OUTPUT_BYTES,
            REASONING_MAX_OUTPUT_MESSAGE,
        )

    def test_a_host_outside_its_transport_class_is_refused(self) -> None:
        cases = (
            ('"loopback"', '"10.20.30.40"'),
            ('"private_lan"', '"127.0.0.1"'),
            ('"private_lan"', '"169.254.1.1"'),
            ('"private_lan"', '"8.8.8.8"'),
            ('"private_lan"', '"localhost"'),
            ('"private_lan"', '"inference.lan"'),
            ('"private_lan"', '"10.20.30.40:8080"'),
            ('"private_lan"', '"http://10.20.30.40"'),
            ('"private_lan"', '"FD00::1"'),
            ('"private_lan"', '""'),
        )
        for transport, host in cases:
            with self.subTest(transport=transport, host=host):
                self._assert_refused(
                    reasoning_block(transport=transport, host=host),
                    REASONING_HOST_MESSAGE,
                )

    def test_a_model_identifier_outside_its_bounds_is_refused(self) -> None:
        self._write_override(reasoning_block(model=f'"{"m" * 128}"'))
        self.assertEqual(self._load().reasoning.model, "m" * 128)

        self._assert_values_refused(
            "model",
            (
                '""',
                f'"{"m" * 129}"',
                '"model name"',
                '"/models/local.gguf"',
                "'C:\\models\\local.gguf'",
                '"~/model"',
            ),
            REASONING_MODEL_MESSAGE,
        )

    def test_a_windows_drive_path_model_identifier_is_refused(self) -> None:
        self._assert_values_refused(
            "model",
            (
                '"C:/models/local.gguf"',
                '"c:/models/local.gguf"',
                "'Z:/m'",
                '"C:models/local.gguf"',
            ),
            REASONING_MODEL_MESSAGE,
        )

    def test_opaque_model_labels_with_slashes_and_tags_are_accepted(
        self,
    ) -> None:
        for model in ("publisher/model:tag", "llama3.1:8b"):
            with self.subTest(model=model):
                self._write_override(reasoning_block(model=f'"{model}"'))
                self.assertEqual(self._load().reasoning.model, model)

    # Enumeration.

    def test_an_unknown_transport_class_is_refused(self) -> None:
        self._assert_values_refused(
            "transport",
            ('"http"', '"LOOPBACK"', '"Loopback"', '"private-lan"', '"cloud"', '""'),
            REASONING_TRANSPORT_MESSAGE,
        )

    # Unknown keys inside the reasoning table.

    def test_an_unknown_reasoning_key_is_refused(self) -> None:
        self._assert_refused(
            reasoning_block() + "retries = 3\n",
            REASONING_UNKNOWN_KEY_MESSAGE,
        )

    def test_credential_endpoint_port_and_model_path_keys_are_refused(
        self,
    ) -> None:
        for key in (
            "api_key",
            "token",
            "password",
            "credential",
            "header",
            "headers",
            "endpoint",
            "url",
            "port",
            "model_path",
            "fallback",
        ):
            with self.subTest(key=key):
                self._assert_refused(
                    reasoning_block() + f'{key} = "x"\n',
                    REASONING_UNKNOWN_KEY_MESSAGE,
                )

    def test_a_nested_table_inside_reasoning_is_refused(self) -> None:
        self._assert_refused(
            reasoning_block() + "[reasoning.extra]\nvalue = 1\n",
            REASONING_UNKNOWN_KEY_MESSAGE,
        )

    def test_a_non_table_override_reasoning_value_is_refused(self) -> None:
        self._assert_refused("reasoning = true\n", REASONING_SECTION_TABLE_MESSAGE)

    # Nothing but [reasoning].

    def test_an_override_may_hold_no_other_section_or_key(self) -> None:
        extras = (
            "[app]\nname = \"Other\"\n",
            "[paths]\ndatabase = \"elsewhere.db\"\n",
            "[filesystem]\ndefault_root = \"workspace\"\n",
            "[logging]\nlevel = \"DEBUG\"\n",
            "[permissions]\ndeny = []\n",
            "[unknown]\nvalue = 1\n",
        )
        for extra in extras:
            with self.subTest(section=extra.split("]")[0]):
                self._assert_refused(
                    reasoning_block() + extra,
                    LOCAL_OVERRIDE_SECTIONS_MESSAGE,
                )
        self._assert_refused(
            'note = "top level"\n' + reasoning_block(),
            LOCAL_OVERRIDE_SECTIONS_MESSAGE,
        )

    def test_an_override_without_a_reasoning_section_is_refused(self) -> None:
        for content in ("", "# nothing here\n", "[permissions]\ndeny = []\n"):
            with self.subTest(content=content[:12]):
                self._assert_refused(content, LOCAL_OVERRIDE_SECTIONS_MESSAGE)

    # The private file itself.

    def test_invalid_toml_is_refused_without_cause_context_or_text(
        self,
    ) -> None:
        refused = self._assert_refused(
            "[reasoning]\n"
            f'host = "{OVERRIDE_TEXT_SENTINEL}"\n'
            "model = = broken\n",
            LOCAL_OVERRIDE_TOML_MESSAGE,
        )

        self._assert_carries_nothing(refused, OVERRIDE_TEXT_SENTINEL)

    def test_a_non_utf8_override_is_refused(self) -> None:
        refused = self._assert_refused(
            b"[reasoning]\nenabled = false\nmodel = \"\xff\xfe\"\n",
            LOCAL_OVERRIDE_TOML_MESSAGE,
        )

        self._assert_carries_nothing(refused, OVERRIDE_TEXT_SENTINEL)

    def test_an_oversized_override_is_refused(self) -> None:
        padding = "#" * (MAXIMUM_LOCAL_OVERRIDE_BYTES + 1) + "\n"

        self._assert_refused(
            padding + reasoning_block(),
            LOCAL_OVERRIDE_TOO_LARGE_MESSAGE,
        )

    def test_a_directory_override_is_refused(self) -> None:
        self.override_path.mkdir()

        self._assert_load_refused(LOCAL_OVERRIDE_ENTRY_MESSAGE)

    def test_real_symbolic_links_are_refused_including_a_dangling_one(
        self,
    ) -> None:
        target = self.temporary_path / "target.toml"
        target.write_text(reasoning_block(), encoding="utf-8")
        try:
            os.symlink(target, self.override_path)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic links are unavailable on this system.")

        self._assert_load_refused(LOCAL_OVERRIDE_ENTRY_MESSAGE)

        target.unlink()
        # Path.exists() follows the link, so it alone would call a dangling
        # link absent; the loader must still see it as present and refuse.
        self.assertFalse(self.override_path.exists())
        self._assert_load_refused(LOCAL_OVERRIDE_ENTRY_MESSAGE)

    def test_a_link_entry_is_refused_even_when_nothing_is_behind_it(
        self,
    ) -> None:
        # The override path does not exist, as for a dangling link; the entry
        # itself reports a link. This runs on every platform, including
        # those where creating a real link needs extra privilege.
        self.assertFalse(self.override_path.exists())
        with self._entry_reporting(stat.S_IFLNK), patch(
            "core.config.os.open",
            wraps=os.open,
        ) as opened:
            self._assert_load_refused(LOCAL_OVERRIDE_ENTRY_MESSAGE)

        self._assert_override_never_opened(opened)

    def test_special_entries_are_refused_before_anything_is_opened(
        self,
    ) -> None:
        self._write_override(reasoning_block())
        for mode in (stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK):
            with self.subTest(mode=oct(mode)):
                with self._entry_reporting(mode), patch(
                    "core.config.os.open",
                    wraps=os.open,
                ) as opened:
                    self._assert_load_refused(LOCAL_OVERRIDE_ENTRY_MESSAGE)

                self._assert_override_never_opened(opened)

    @unittest.skipUnless(
        hasattr(os, "mkfifo"),
        "Named pipes cannot be created on this platform.",
    )
    def test_a_real_named_pipe_is_refused_without_blocking(self) -> None:
        os.mkfifo(self.override_path)

        self._assert_load_refused(LOCAL_OVERRIDE_ENTRY_MESSAGE)

    def test_an_entry_swapped_after_inspection_is_refused(self) -> None:
        self._write_override(reasoning_block())
        real_fstat = os.fstat

        def another_file(descriptor: int) -> os.stat_result:
            values = list(real_fstat(descriptor))
            values[1] += 1
            return os.stat_result(values)

        def a_named_pipe(descriptor: int) -> os.stat_result:
            values = list(real_fstat(descriptor))
            values[0] = stat.S_IFIFO | 0o600
            return os.stat_result(values)

        for swapped in (another_file, a_named_pipe):
            with self.subTest(swapped=swapped.__name__):
                with patch("core.config.os.fstat", side_effect=swapped):
                    self._assert_load_refused(LOCAL_OVERRIDE_ENTRY_MESSAGE)

    def test_an_unreadable_override_is_refused_without_the_os_error(
        self,
    ) -> None:
        self._write_override(reasoning_block())
        error = PermissionError(13, "Access is denied", OVERRIDE_TEXT_SENTINEL)
        real_lstat = os.lstat

        def denied_lstat(path: object, *args: object, **kwargs: object):
            if os.fspath(path) == os.fspath(self.override_path):
                raise error
            return real_lstat(path, *args, **kwargs)

        with patch("core.config.os.lstat", side_effect=denied_lstat):
            refused = self._assert_load_refused(LOCAL_OVERRIDE_UNREADABLE_MESSAGE)
        self._assert_carries_nothing(refused, OVERRIDE_TEXT_SENTINEL)

        with patch("core.config.os.open", side_effect=error):
            refused = self._assert_load_refused(LOCAL_OVERRIDE_UNREADABLE_MESSAGE)
        self._assert_carries_nothing(refused, OVERRIDE_TEXT_SENTINEL)

    # Privacy of what was loaded and what was refused.

    def test_no_refusal_interpolates_a_configured_value_or_the_path(
        self,
    ) -> None:
        hidden = self.temporary_path / OVERRIDE_TEXT_SENTINEL
        hidden.mkdir()
        self.override_path = hidden / "config.local.toml"
        self.config_path.replace(hidden / "config.toml")
        self.config_path = hidden / "config.toml"

        cases = (
            reasoning_block(host=f'"{OVERRIDE_TEXT_SENTINEL}"'),
            reasoning_block(model=f'"{OVERRIDE_TEXT_SENTINEL} x"'),
            reasoning_block(transport=f'"{OVERRIDE_TEXT_SENTINEL}"'),
            reasoning_block() + f'{OVERRIDE_TEXT_SENTINEL} = "x"\n',
            reasoning_block() + f"[{OVERRIDE_TEXT_SENTINEL}]\n",
            f'[reasoning]\nenabled = "{OVERRIDE_TEXT_SENTINEL}"\n',
            f"{OVERRIDE_TEXT_SENTINEL} = = broken\n",
        )
        for content in cases:
            with self.subTest(content=content[-30:]):
                self._write_override(content)
                with self.assertRaises(ValueError) as caught:
                    self._load()
                self._assert_carries_nothing(
                    caught.exception,
                    OVERRIDE_TEXT_SENTINEL,
                )

        self.override_path.unlink()
        self.override_path.mkdir()
        with self.assertRaises(ValueError) as caught:
            self._load()
        self._assert_carries_nothing(caught.exception, OVERRIDE_TEXT_SENTINEL)

    def test_the_loaded_configuration_never_shows_the_host_or_model(
        self,
    ) -> None:
        self._write_override(
            reasoning_block(
                host=f'"{OVERRIDE_HOST_SENTINEL}"',
                model=f'"{OVERRIDE_MODEL_SENTINEL}"',
            )
        )

        config = self._load()

        self.assertEqual(config.reasoning.host, OVERRIDE_HOST_SENTINEL)
        self.assertEqual(config.reasoning.model, OVERRIDE_MODEL_SENTINEL)
        for value in (config, config.reasoning):
            with self.subTest(value=type(value).__name__):
                text = " ".join(
                    (repr(value), str(value), f"{value}", "%r" % (value,))
                )
                self.assertNotIn(OVERRIDE_HOST_SENTINEL, text)
                self.assertNotIn(OVERRIDE_MODEL_SENTINEL, text)

    def test_loading_an_override_logs_nothing(self) -> None:
        self._write_override(
            reasoning_block(
                host=f'"{OVERRIDE_HOST_SENTINEL}"',
                model=f'"{OVERRIDE_MODEL_SENTINEL}"',
            )
        )
        with self.assertNoLogs("maria", level="DEBUG"):
            self._load()

        self._write_override(reasoning_block(model='"bad model"'))
        with self.assertNoLogs("maria", level="DEBUG"):
            with self.assertRaises(ValueError):
                self._load()

    def test_the_local_override_is_protected_by_an_exact_ignore_rule(
        self,
    ) -> None:
        rules = [
            line.strip()
            for line in (PROJECT_ROOT / ".gitignore").read_text(
                encoding="utf-8"
            ).splitlines()
        ]

        self.assertIn("config/config.local.toml", rules)
        self.assertNotIn("!config/config.local.toml", rules)

    # Helpers.

    def _write_override(self, content: str | bytes) -> None:
        """Write one synthetic override into the temporary directory."""
        if isinstance(content, bytes):
            self.override_path.write_bytes(content)
        else:
            self.override_path.write_text(content, encoding="utf-8")

    def _load(self) -> MariaConfig:
        """Load the temporary tracked file with the temporary override."""
        return load_config(
            self.config_path,
            local_override_path=self.override_path,
        )

    def _assert_load_refused(self, message: str) -> ValueError:
        """Assert loading is refused with one fixed message, and return it."""
        with self.assertRaises(ValueError) as caught:
            self._load()
        self.assertEqual(str(caught.exception), message)
        return caught.exception

    def _assert_refused(self, content: str | bytes, message: str) -> ValueError:
        """Write one override, then assert it is refused with one message."""
        self._write_override(content)
        return self._assert_load_refused(message)

    def _assert_missing(self, field: str) -> None:
        """Assert an enabled override missing one field fails closed."""
        self._assert_refused(
            reasoning_block(omit=frozenset({field})),
            REASONING_MISSING_FIELD_MESSAGE,
        )

    def _assert_values_refused(
        self,
        field: str,
        values: tuple[str, ...],
        message: str,
    ) -> None:
        """Assert each TOML value for one field is refused with its message."""
        for value in values:
            with self.subTest(field=field, value=value):
                self._assert_refused(reasoning_block(**{field: value}), message)

    def _assert_range(self, field: str, maximum: int, message: str) -> None:
        """Assert one integer field accepts its bounds and refuses beyond."""
        for accepted in (1, maximum):
            with self.subTest(field=field, value=accepted):
                self._write_override(reasoning_block(**{field: str(accepted)}))
                self.assertEqual(getattr(self._load().reasoning, field), accepted)
        self._assert_values_refused(
            field,
            ("0", "-1", str(maximum + 1)),
            message,
        )

    def _entry_reporting(self, mode: int):
        """Make the override's directory entry report one file type."""
        real_lstat = os.lstat

        def reporting(path: object, *args: object, **kwargs: object):
            if os.fspath(path) == os.fspath(self.override_path):
                return os.stat_result((mode | 0o600, 1, 1, 1, 0, 0, 0, 0, 0, 0))
            return real_lstat(path, *args, **kwargs)

        return patch("core.config.os.lstat", side_effect=reporting)

    def _assert_override_never_opened(self, opened: Mock) -> None:
        """Assert no open call ever named the override."""
        for call in opened.call_args_list:
            self.assertNotEqual(
                os.fspath(call.args[0]),
                os.fspath(self.override_path),
            )

    def _assert_carries_nothing(
        self,
        refused: BaseException,
        sentinel: str,
    ) -> None:
        """Assert a refusal has no cause or context and never names a value."""
        self.assertIsNone(refused.__cause__)
        self.assertIsNone(refused.__context__)
        rendered = " ".join(
            (
                str(refused),
                repr(refused),
                repr(refused.args),
                "".join(traceback.format_exception(refused)),
            )
        )
        self.assertNotIn(sentinel, rendered)
        self.assertNotIn(OVERRIDE_HOST_SENTINEL, rendered)
        self.assertNotIn(OVERRIDE_MODEL_SENTINEL, rendered)


if __name__ == "__main__":
    unittest.main()
