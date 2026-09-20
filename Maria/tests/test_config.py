"""Focused tests for Maria's permission configuration loading."""

from __future__ import annotations

import json
import os
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

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


if __name__ == "__main__":
    unittest.main()
