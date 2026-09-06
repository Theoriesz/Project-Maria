"""Focused tests for Maria's permission configuration loading."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from core.config import MariaConfig, load_config
from core.constants import APP_VERSION, CONFIG_PATH, PROJECT_ROOT
from core.permissions import (
    MANDATORY_CONFIRMATION_ACTIONS,
    MANDATORY_DENIED_ACTIONS,
    PermissionDecision,
    PermissionManager,
)


class ConfigTests(unittest.TestCase):
    """Verify permission validation without touching runtime state."""

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.config_path = Path(self.temporary_directory.name) / "config.toml"

    def test_shipped_configuration_loads_with_existing_values(self) -> None:
        config = load_config(CONFIG_PATH)

        self.assertEqual(config.app_name, "Maria")
        self.assertEqual(config.version, "0.1.1")
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
        self.assertEqual(config.safe_filesystem_root, PROJECT_ROOT.resolve())
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
            "safe_filesystem_root = \"workspace\"\n"
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
        self.assertEqual(
            config.safe_filesystem_root,
            (PROJECT_ROOT / "workspace").resolve(),
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
        """Write and load one temporary TOML configuration."""
        self.config_path.write_text(content, encoding="utf-8")
        return load_config(self.config_path)


if __name__ == "__main__":
    unittest.main()
