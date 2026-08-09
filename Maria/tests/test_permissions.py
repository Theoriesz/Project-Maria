"""Focused tests for Maria's mandatory permission policy."""

from __future__ import annotations

import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from core.config import load_config
from core.permissions import (
    MANDATORY_CONFIRMATION_ACTIONS,
    MANDATORY_DENIED_ACTIONS,
    PermissionDecision,
    PermissionManager,
    PermissionResult,
)


class PermissionManagerTests(unittest.TestCase):
    """Verify mandatory policy, configured extensions, and logging."""

    def test_empty_construction_requires_confirmation_for_rename_and_move(self) -> None:
        manager = self._manager()

        for action in ("rename", "move"):
            with self.subTest(action=action):
                self.assertEqual(
                    manager.check("filesystem", action),
                    PermissionResult(
                        PermissionDecision.REQUIRES_CONFIRMATION,
                        f"filesystem.{action} requires explicit confirmation.",
                    ),
                )

    def test_empty_construction_denies_delete_and_write(self) -> None:
        manager = self._manager()

        for action in ("delete", "write"):
            with self.subTest(action=action):
                self.assertEqual(
                    manager.check("filesystem", action),
                    PermissionResult(
                        PermissionDecision.DENIED,
                        f"filesystem.{action} is denied by configuration.",
                    ),
                )

    def test_ordinary_unconfigured_action_remains_allowed(self) -> None:
        result = self._manager().check("notes", "list")

        self.assertEqual(
            result,
            PermissionResult(
                PermissionDecision.ALLOWED,
                "notes.list is allowed.",
            ),
        )

    def test_configured_additional_action_can_require_confirmation(self) -> None:
        manager = self._manager(confirmation_required={"notes.create"})

        self.assertEqual(
            manager.check("notes", "create"),
            PermissionResult(
                PermissionDecision.REQUIRES_CONFIRMATION,
                "notes.create requires explicit confirmation.",
            ),
        )

    def test_configured_additional_action_can_be_denied(self) -> None:
        manager = self._manager(denied_actions={"memory.store"})

        self.assertEqual(
            manager.check("memory", "store"),
            PermissionResult(
                PermissionDecision.DENIED,
                "memory.store is denied by configuration.",
            ),
        )

    def test_denied_wins_for_configured_cross_set_overlap(self) -> None:
        manager = self._manager(
            confirmation_required={"calendar.create"},
            denied_actions={"calendar.create"},
        )

        self.assertIn("calendar.create", manager.confirmation_required)
        self.assertIn("calendar.create", manager.denied_actions)
        self.assertEqual(
            manager.check("calendar", "create").decision,
            PermissionDecision.DENIED,
        )

    def test_mandatory_denial_wins_when_also_configured_for_confirmation(self) -> None:
        manager = self._manager(
            confirmation_required={"filesystem.delete", "filesystem.write"},
        )

        for action in ("delete", "write"):
            with self.subTest(action=action):
                self.assertIn(
                    f"filesystem.{action}",
                    manager.confirmation_required,
                )
                self.assertEqual(
                    manager.check("filesystem", action).decision,
                    PermissionDecision.DENIED,
                )

    def test_configured_denial_wins_over_mandatory_confirmation(self) -> None:
        manager = self._manager(denied_actions={"filesystem.rename"})

        self.assertIn("filesystem.rename", manager.confirmation_required)
        self.assertEqual(
            manager.check("filesystem", "rename").decision,
            PermissionDecision.DENIED,
        )

    def test_confirmation_reason_is_generic_for_any_configured_action(self) -> None:
        result = self._manager(
            confirmation_required={"calendar.create"}
        ).check("calendar", "create")

        self.assertEqual(
            result.reason,
            "calendar.create requires explicit confirmation.",
        )
        self.assertNotIn("file", result.reason.lower())

    def test_denied_and_allowed_reasons_remain_controlled(self) -> None:
        manager = self._manager(denied_actions={"notes.remove"})

        self.assertEqual(
            manager.check("notes", "remove").reason,
            "notes.remove is denied by configuration.",
        )
        self.assertEqual(
            manager.check("notes", "list").reason,
            "notes.list is allowed.",
        )

    def test_matching_remains_exact_and_case_sensitive(self) -> None:
        manager = self._manager(
            confirmation_required={"notes.create"},
            denied_actions={"memory.store"},
        )
        cases = (
            ("Filesystem", "rename"),
            ("filesystem", "Rename"),
            ("filesystem", "rename "),
            ("Notes", "create"),
            ("notes", "Create"),
            ("Memory", "store"),
            ("memory", "Store"),
            ("memory", "store "),
        )

        for tool_name, action in cases:
            with self.subTest(tool_name=tool_name, action=action):
                self.assertEqual(
                    manager.check(tool_name, action).decision,
                    PermissionDecision.ALLOWED,
                )

    def test_constructor_inputs_are_not_mutated(self) -> None:
        confirmation_required = {"notes.create"}
        denied_actions = {"memory.store"}
        original_confirmation = confirmation_required.copy()
        original_denied = denied_actions.copy()

        manager = PermissionManager(
            confirmation_required,
            denied_actions,
            logger=Mock(spec=logging.Logger),
        )

        self.assertEqual(confirmation_required, original_confirmation)
        self.assertEqual(denied_actions, original_denied)
        self.assertIsNot(manager.confirmation_required, confirmation_required)
        self.assertIsNot(manager.denied_actions, denied_actions)

    def test_effective_sets_and_policy_constants_are_frozensets(self) -> None:
        manager = self._manager(
            confirmation_required={"notes.create"},
            denied_actions={"memory.store"},
        )

        self.assertIsInstance(MANDATORY_CONFIRMATION_ACTIONS, frozenset)
        self.assertIsInstance(MANDATORY_DENIED_ACTIONS, frozenset)
        self.assertEqual(
            MANDATORY_CONFIRMATION_ACTIONS,
            frozenset({"filesystem.rename", "filesystem.move"}),
        )
        self.assertEqual(
            MANDATORY_DENIED_ACTIONS,
            frozenset({"filesystem.delete", "filesystem.write"}),
        )
        self.assertIsInstance(manager.confirmation_required, frozenset)
        self.assertIsInstance(manager.denied_actions, frozenset)
        self.assertEqual(
            manager.confirmation_required,
            MANDATORY_CONFIRMATION_ACTIONS | {"notes.create"},
        )
        self.assertEqual(
            manager.denied_actions,
            MANDATORY_DENIED_ACTIONS | {"memory.store"},
        )

    def test_decisions_use_expected_log_levels(self) -> None:
        cases = (
            (
                "allowed",
                frozenset(),
                frozenset(),
                "notes",
                "list",
                PermissionDecision.ALLOWED,
                "info",
            ),
            (
                "confirmation",
                frozenset({"notes.create"}),
                frozenset(),
                "notes",
                "create",
                PermissionDecision.REQUIRES_CONFIRMATION,
                "info",
            ),
            (
                "denied",
                frozenset(),
                frozenset({"memory.store"}),
                "memory",
                "store",
                PermissionDecision.DENIED,
                "warning",
            ),
        )

        for (
            description,
            confirmation_required,
            denied_actions,
            tool_name,
            action,
            expected_decision,
            log_method,
        ) in cases:
            with self.subTest(description=description):
                logger = Mock(spec=logging.Logger)
                manager = PermissionManager(
                    confirmation_required,
                    denied_actions,
                    logger=logger,
                )

                result = manager.check(tool_name, action)

                self.assertEqual(result.decision, expected_decision)
                getattr(logger, log_method).assert_called_once()
                if log_method == "info":
                    logger.warning.assert_not_called()
                else:
                    logger.info.assert_not_called()

    def test_loaded_config_can_be_passed_directly_to_manager(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.toml"
            config_path.write_text(
                "[permissions]\n"
                "require_confirmation = [\"calendar.create\"]\n"
                "deny = [\"memory.store\", \"calendar.create\"]\n",
                encoding="utf-8",
            )
            config = load_config(config_path)

        manager = PermissionManager(
            config.confirmation_required,
            config.denied_actions,
            logger=Mock(spec=logging.Logger),
        )

        expected_decisions = {
            ("filesystem", "rename"): PermissionDecision.REQUIRES_CONFIRMATION,
            ("filesystem", "move"): PermissionDecision.REQUIRES_CONFIRMATION,
            ("filesystem", "delete"): PermissionDecision.DENIED,
            ("filesystem", "write"): PermissionDecision.DENIED,
            ("memory", "store"): PermissionDecision.DENIED,
            ("calendar", "create"): PermissionDecision.DENIED,
            ("notes", "list"): PermissionDecision.ALLOWED,
        }

        for (tool_name, action), expected in expected_decisions.items():
            with self.subTest(tool_name=tool_name, action=action):
                self.assertEqual(
                    manager.check(tool_name, action).decision,
                    expected,
                )

    @staticmethod
    def _manager(
        confirmation_required: set[str] | frozenset[str] | None = None,
        denied_actions: set[str] | frozenset[str] | None = None,
    ) -> PermissionManager:
        """Return a manager with an isolated mock logger."""
        return PermissionManager(
            frozenset() if confirmation_required is None else confirmation_required,
            frozenset() if denied_actions is None else denied_actions,
            logger=Mock(spec=logging.Logger),
        )


if __name__ == "__main__":
    unittest.main()
