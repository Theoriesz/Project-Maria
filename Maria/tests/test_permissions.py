"""Focused tests for Maria's mandatory permission policy."""

from __future__ import annotations

import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from core.config import load_config
from core.permissions import (
    CONFIRMATION_NOTICES,
    MANDATORY_CONFIRMATION_ACTIONS,
    MANDATORY_DENIED_ACTIONS,
    PermissionDecision,
    PermissionManager,
    PermissionResult,
)


# Maria's named filesystem roots are a required section, so every fixture that
# loads a real configuration file has to declare them. The permission fixtures
# below are about the permission baseline only, so they append this shipped
# declaration verbatim rather than restating any root policy of their own.
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

# The one confirmation that carries more than the standard sentence, written
# out so the owner-facing wording is pinned rather than agreed with.
ENABLE_CONTEXT_NOTICE = (
    "Once enabled, this memory may be sent to your local reasoning model on "
    "any conversational turn, within Maria's memory context limits. Maria "
    "does not choose memories by relevance."
)
# V1-16's note consent notice, pinned the same way.
ENABLE_NOTE_CONTEXT_NOTICE = (
    "Once enabled, this note's entire title and body may be sent to your local "
    "reasoning model on a later conversational turn when a word of four or "
    "more characters from your request appears literally in its title or its "
    "body, within Maria's context limits. Maria does not check the note for "
    "passwords or other secrets, and enabling it does not mean it contains "
    "none."
)
# V1-18's audit summary deletion notice, pinned the same way.
DELETE_AUDIT_NOTICE = (
    "Deleting an audit summary removes it from Maria's database. It does not "
    "undo the action, and Maria's operational log may still show that a "
    "model-proposed action ran."
)
# V1-20's conversation summary notices, pinned the same way.
STORE_SUMMARY_NOTICE = (
    "Storing this summary keeps text your local reasoning model wrote, which "
    "can repeat anything said in this session, including text from memories "
    "or notes it was given. Its context consent starts off, and Maria does "
    "not check it for passwords or other secrets."
)
ENABLE_SUMMARY_CONTEXT_NOTICE = (
    "Once enabled, this summary's entire text may be sent to your local "
    "reasoning model on a later conversational turn when a word of four or "
    "more characters from your request appears literally in it, within "
    "Maria's context limits. Maria does not check the summary for passwords "
    "or other secrets, and enabling it does not mean it contains none."
)
# V1-22's reminder and task notices, pinned the same way.
CREATE_REMINDER_NOTICE = (
    "Maria will not notify you when this reminder is due, and it does not "
    "repeat: type due reminders to see the reminders whose time has come."
)
ENABLE_TASK_CONTEXT_NOTICE = (
    "Once enabled, this task's text and due date may be sent to your local "
    "reasoning model on a conversational turn while it is open, after your "
    "enabled memories and within Maria's memory context limits, so it is not "
    "always sent. Maria does not check the task for passwords or other "
    "secrets, and enabling it does not mean it contains none."
)
ENABLE_REMINDER_CONTEXT_NOTICE = (
    "Once enabled, this reminder's text and saved time may be sent to your "
    "local reasoning model on a conversational turn while it is open, after "
    "your enabled memories and within Maria's memory context limits, so it is "
    "not always sent. Maria does not check the reminder for passwords or "
    "other secrets, and enabling it does not mean it contains none."
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

    def test_empty_construction_requires_confirmation_for_history_delete(self) -> None:
        manager = self._manager()

        self.assertEqual(
            manager.check("history", "delete"),
            PermissionResult(
                PermissionDecision.REQUIRES_CONFIRMATION,
                "history.delete requires explicit confirmation.",
            ),
        )

    def test_configured_denial_wins_over_mandatory_history_delete_confirmation(
        self,
    ) -> None:
        manager = self._manager(denied_actions={"history.delete"})

        self.assertIn("history.delete", manager.confirmation_required)
        self.assertEqual(
            manager.check("history", "delete").decision,
            PermissionDecision.DENIED,
        )

    def test_empty_construction_requires_confirmation_for_note_lifecycle(
        self,
    ) -> None:
        manager = self._manager()

        for action in ("retitle", "edit", "delete"):
            with self.subTest(action=action):
                self.assertEqual(
                    manager.check("notes", action),
                    PermissionResult(
                        PermissionDecision.REQUIRES_CONFIRMATION,
                        f"notes.{action} requires explicit confirmation.",
                    ),
                )

    def test_note_creation_and_reading_actions_default_to_allowed(self) -> None:
        manager = self._manager()

        for action in ("create", "list", "read"):
            with self.subTest(action=action):
                self.assertEqual(
                    manager.check("notes", action),
                    PermissionResult(
                        PermissionDecision.ALLOWED,
                        f"notes.{action} is allowed.",
                    ),
                )

    def test_empty_construction_requires_confirmation_for_memory_lifecycle(
        self,
    ) -> None:
        manager = self._manager()

        for action in ("update", "forget"):
            with self.subTest(action=action):
                self.assertEqual(
                    manager.check("memory", action),
                    PermissionResult(
                        PermissionDecision.REQUIRES_CONFIRMATION,
                        f"memory.{action} requires explicit confirmation.",
                    ),
                )

    def test_memory_storage_and_retrieval_actions_default_to_allowed(self) -> None:
        manager = self._manager()

        for action in ("store", "search", "list"):
            with self.subTest(action=action):
                self.assertEqual(
                    manager.check("memory", action),
                    PermissionResult(
                        PermissionDecision.ALLOWED,
                        f"memory.{action} is allowed.",
                    ),
                )

    def test_configured_denial_wins_over_mandatory_memory_lifecycle_confirmation(
        self,
    ) -> None:
        for action in ("update", "forget"):
            with self.subTest(action=action):
                manager = self._manager(denied_actions={f"memory.{action}"})

                self.assertIn(f"memory.{action}", manager.confirmation_required)
                self.assertEqual(
                    manager.check("memory", action).decision,
                    PermissionDecision.DENIED,
                )

    def test_configured_denial_wins_over_mandatory_note_lifecycle_confirmation(
        self,
    ) -> None:
        for action in ("retitle", "edit", "delete"):
            with self.subTest(action=action):
                manager = self._manager(denied_actions={f"notes.{action}"})

                self.assertIn(f"notes.{action}", manager.confirmation_required)
                self.assertEqual(
                    manager.check("notes", action).decision,
                    PermissionDecision.DENIED,
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

    def test_history_actions_default_to_allowed(self) -> None:
        manager = self._manager()

        for action in ("list", "search", "read"):
            with self.subTest(action=action):
                self.assertEqual(
                    manager.check("history", action),
                    PermissionResult(
                        PermissionDecision.ALLOWED,
                        f"history.{action} is allowed.",
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
            frozenset(
                {
                    "audit.delete",
                    "filesystem.rename",
                    "filesystem.move",
                    "history.delete",
                    "notes.retitle",
                    "notes.edit",
                    "notes.delete",
                    "notes.enable_context",
                    "notes.disable_context",
                    "memory.update",
                    "memory.forget",
                    "memory.enable_context",
                    "memory.disable_context",
                    # V1-20: storing a drafted summary is the owner's
                    # full-text review, and every summary lifecycle action
                    # and the raw-save deletion are confirmed.
                    "conversation_summaries.create",
                    "conversation_summaries.edit",
                    "conversation_summaries.delete",
                    "conversation_summaries.enable_context",
                    "conversation_summaries.disable_context",
                    "conversations.delete",
                    # V1-22: both creates show their exact text and timing
                    # before storage, and every change to an existing task or
                    # reminder but complete and reopen is confirmed.
                    "tasks.create",
                    "tasks.edit",
                    "tasks.reschedule",
                    "tasks.delete",
                    "tasks.enable_context",
                    "tasks.disable_context",
                    "reminders.create",
                    "reminders.edit",
                    "reminders.reschedule",
                    "reminders.delete",
                    "reminders.enable_context",
                    "reminders.disable_context",
                }
            ),
        )
        self.assertEqual(len(MANDATORY_CONFIRMATION_ACTIONS), 31)
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

    def test_mandatory_policy_sets_stay_immutable_across_construction(self) -> None:
        original_confirmation = set(MANDATORY_CONFIRMATION_ACTIONS)
        original_denied = set(MANDATORY_DENIED_ACTIONS)

        self._manager(
            confirmation_required={"calendar.create"},
            denied_actions={"calendar.remove"},
        )

        for name, mandatory_actions, expected in (
            (
                "confirmation",
                MANDATORY_CONFIRMATION_ACTIONS,
                original_confirmation,
            ),
            ("denied", MANDATORY_DENIED_ACTIONS, original_denied),
        ):
            with self.subTest(name=name):
                self.assertIsInstance(mandatory_actions, frozenset)
                self.assertEqual(set(mandatory_actions), expected)
                self.assertFalse(hasattr(mandatory_actions, "add"))
                self.assertFalse(hasattr(mandatory_actions, "discard"))

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
                "deny = [\"memory.store\", \"calendar.create\"]\n"
                + VALID_FILESYSTEM_SECTION,
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

    def test_configuration_cannot_remove_mandatory_confirmation_actions(self) -> None:
        cases = (
            ("missing_permissions_section", "[app]\nname = \"Maria\"\n"),
            ("empty_permissions_section", "[permissions]\n"),
            (
                "empty_permission_arrays",
                "[permissions]\n"
                "require_confirmation = []\n"
                "deny = []\n",
            ),
            (
                "minimal_unrelated_permissions",
                "[permissions]\n"
                "require_confirmation = [\"calendar.create\"]\n"
                "deny = [\"memory.store\"]\n",
            ),
        )

        for description, config_text in cases:
            with self.subTest(description=description):
                with TemporaryDirectory() as temporary_directory:
                    config_path = Path(temporary_directory) / "config.toml"
                    config_path.write_text(
                        config_text + VALID_FILESYSTEM_SECTION,
                        encoding="utf-8",
                    )
                    config = load_config(config_path)

                manager = PermissionManager(
                    config.confirmation_required,
                    config.denied_actions,
                    logger=Mock(spec=logging.Logger),
                )

                self.assertTrue(
                    MANDATORY_CONFIRMATION_ACTIONS.issubset(
                        config.confirmation_required
                    )
                )
                self.assertTrue(
                    MANDATORY_CONFIRMATION_ACTIONS.issubset(
                        manager.confirmation_required
                    )
                )
                for action_id in sorted(MANDATORY_CONFIRMATION_ACTIONS):
                    tool_name, action = action_id.split(".")
                    reason = f"{action_id} requires explicit confirmation."
                    if action_id == "audit.delete":
                        reason = f"{reason} {DELETE_AUDIT_NOTICE}"
                    if action_id == "memory.enable_context":
                        reason = f"{reason} {ENABLE_CONTEXT_NOTICE}"
                    if action_id == "notes.enable_context":
                        reason = f"{reason} {ENABLE_NOTE_CONTEXT_NOTICE}"
                    if action_id == "conversation_summaries.create":
                        reason = f"{reason} {STORE_SUMMARY_NOTICE}"
                    if action_id == "conversation_summaries.enable_context":
                        reason = f"{reason} {ENABLE_SUMMARY_CONTEXT_NOTICE}"
                    if action_id == "reminders.create":
                        reason = f"{reason} {CREATE_REMINDER_NOTICE}"
                    if action_id == "tasks.enable_context":
                        reason = f"{reason} {ENABLE_TASK_CONTEXT_NOTICE}"
                    if action_id == "reminders.enable_context":
                        reason = f"{reason} {ENABLE_REMINDER_CONTEXT_NOTICE}"
                    with self.subTest(action_id=action_id):
                        self.assertEqual(
                            manager.check(tool_name, action),
                            PermissionResult(
                                PermissionDecision.REQUIRES_CONFIRMATION,
                                reason,
                            ),
                        )

    def test_an_approval_notice_is_the_fixed_text_of_a_confirmed_action(
        self,
    ) -> None:
        manager = self._manager()
        expected = {
            "audit.delete": DELETE_AUDIT_NOTICE,
            "memory.enable_context": ENABLE_CONTEXT_NOTICE,
            "notes.enable_context": ENABLE_NOTE_CONTEXT_NOTICE,
            "conversation_summaries.create": STORE_SUMMARY_NOTICE,
            "conversation_summaries.enable_context": ENABLE_SUMMARY_CONTEXT_NOTICE,
            "reminders.create": CREATE_REMINDER_NOTICE,
            "tasks.enable_context": ENABLE_TASK_CONTEXT_NOTICE,
            "reminders.enable_context": ENABLE_REMINDER_CONTEXT_NOTICE,
        }

        self.assertEqual(set(expected), set(CONFIRMATION_NOTICES))
        for action_id, notice in expected.items():
            with self.subTest(action=action_id):
                tool_name, action = action_id.split(".")
                self.assertEqual(manager.confirmation_notice(tool_name, action), notice)
                self.assertEqual(
                    manager.check(tool_name, action).reason,
                    f"{action_id} requires explicit confirmation. {notice}",
                )
        for tool_name, action in (
            ("tasks", "create"),
            ("filesystem", "rename"),
            ("calculator", "calculate"),
            ("tasks", "complete"),
            ("filesystem", "delete"),
            ("calendar", "create"),
        ):
            with self.subTest(action=f"{tool_name}.{action}"):
                self.assertIsNone(manager.confirmation_notice(tool_name, action))
        # Asking for a notice logs nothing; only ``check`` logs a decision.
        manager.logger.reset_mock()
        manager.confirmation_notice("reminders", "create")
        self.assertEqual(manager.logger.mock_calls, [])

    def test_a_denied_or_unconfirmed_action_carries_no_approval_notice(
        self,
    ) -> None:
        denied = self._manager(denied_actions={"reminders.create"})
        self.assertIs(
            denied.check("reminders", "create").decision,
            PermissionDecision.DENIED,
        )
        self.assertIsNone(denied.confirmation_notice("reminders", "create"))

        # A configured confirmation of an action without a notice adds none.
        extended = self._manager(confirmation_required={"calculator.calculate"})
        self.assertIsNone(extended.confirmation_notice("calculator", "calculate"))

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
