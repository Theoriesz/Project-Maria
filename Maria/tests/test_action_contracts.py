"""Focused tests for Maria's declarative per-action argument contracts."""

from __future__ import annotations

import logging
import unittest
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import Any
from unittest.mock import Mock, patch

from core.action_contracts import (
    BUILT_IN_ACTION_CONTRACTS,
    FACT_DIGEST_LENGTH,
    MAXIMUM_EXPRESSION_LENGTH,
    MAXIMUM_LINE_NUMBER,
    MAXIMUM_LIST_LIMIT,
    MAXIMUM_PROJECT_FACT_LENGTH,
    MAXIMUM_PROJECT_LOCATION_LENGTH,
    MAXIMUM_PROJECT_NAME_LENGTH,
    MAXIMUM_PROJECT_PATH_LENGTH,
    MAXIMUM_SUMMARY_LENGTH,
    MAXIMUM_TASK_TEXT_LENGTH,
    MAXIMUM_TRANSCRIPT_LENGTH,
    MINIMUM_LIST_LIMIT,
    REPEAT_VALUES,
    TASK_STATUSES,
    ActionContract,
    ActionContractRegistry,
    ArgumentContract,
    ContractFailure,
    ContractViolation,
    ModelEligibility,
    RootClass,
    TextFormat,
)
from core.config import MariaConfig
from core.filesystem_roots import (
    FilesystemProtections,
    FilesystemRoot,
    FilesystemRoots,
    RootPolicy,
)
from core.memory import Memory
from core.planner import PlanArguments, Planner
from core.time_resolution import TimingContext
from core.tool_manager import ToolManager
from tests.zone_fixtures import CENTRAL_RULES, SYNTHETIC_KEY, SyntheticZones


EXPECTED_ACTION_IDENTIFIERS = frozenset(
    {
        "audit.list",
        "audit.read",
        "audit.delete",
        "calculator.calculate",
        "conversation_summaries.create",
        "conversation_summaries.list",
        "conversation_summaries.read",
        "conversation_summaries.edit",
        "conversation_summaries.delete",
        "conversation_summaries.enable_context",
        "conversation_summaries.disable_context",
        "conversations.save",
        "conversations.list",
        "conversations.read",
        "conversations.delete",
        "filesystem.read",
        "filesystem.list",
        "filesystem.rename",
        "filesystem.move",
        "history.list",
        "history.search",
        "history.read",
        "history.delete",
        "memory.store",
        "memory.search",
        "memory.list",
        "memory.update",
        "memory.forget",
        "memory.enable_context",
        "memory.disable_context",
        "notes.create",
        "notes.list",
        "notes.read",
        "notes.retitle",
        "notes.edit",
        "notes.delete",
        "notes.enable_context",
        "notes.disable_context",
        "projects.list",
        "projects.create",
        "projects.read",
        "projects.retitle",
        "projects.delete",
        "projects.activate",
        "projects.deactivate",
        "projects.list_facts",
        "projects.add_fact",
        "projects.edit_fact",
        "projects.delete_fact",
        "projects.list_files",
        "projects.read_file",
        "reminders.create",
        "reminders.list",
        "reminders.due",
        "reminders.read",
        "reminders.complete",
        "reminders.reopen",
        "reminders.edit",
        "reminders.reschedule",
        "reminders.delete",
        "reminders.enable_context",
        "reminders.disable_context",
        "reminders.delivery_status",
        "reminders.enable_delivery",
        "reminders.disable_delivery",
        "system.overview",
        "system.current_time",
        "tasks.create",
        "tasks.list",
        "tasks.due",
        "tasks.read",
        "tasks.complete",
        "tasks.reopen",
        "tasks.edit",
        "tasks.reschedule",
        "tasks.delete",
        "tasks.enable_context",
        "tasks.disable_context",
    }
)
EXPECTED_PATH_FIELDS = frozenset(
    {
        ("filesystem", "read", "path"),
        ("filesystem", "list", "path"),
        ("filesystem", "rename", "source"),
        ("filesystem", "rename", "destination"),
        ("filesystem", "move", "source"),
        ("filesystem", "move", "destination"),
    }
)
UNREGISTERED_ACTIONS = ("write", "delete")

# The approved V1-17-R1 classification, written out independently of both
# the registry and the compiler's allowlist. V1-18-R1 keeps it: every audit
# action is closed to the model. V1-22-R1 opens the two creation actions,
# which compile only from the owner's own supported request.
EXPECTED_MODEL_ELIGIBLE = frozenset(
    {
        "calculator.calculate",
        "filesystem.list",
        "system.current_time",
        "tasks.create",
        "reminders.create",
    }
)
EXPECTED_NOT_MODEL_ELIGIBLE = EXPECTED_ACTION_IDENTIFIERS - EXPECTED_MODEL_ELIGIBLE

# The classification every synthetic contract below states. None of those
# tests is about eligibility, so each declares the closed one explicitly.
NOT_ELIGIBLE = ModelEligibility.NOT_MODEL_ELIGIBLE


def _temporary_filesystem_protections(root: Path) -> FilesystemProtections:
    """Return active runtime locations beneath a temporary directory."""
    return FilesystemProtections(
        database_path=root / "database" / "maria.db",
        log_dir=root / "logs",
        prompt_path=root / "system_prompt.txt",
    )


def _temporary_filesystem_roots(root: Path) -> FilesystemRoots:
    """Return one enabled organizing root beneath a temporary directory."""
    return FilesystemRoots(
        (
            FilesystemRoot(
                name="workspace",
                path=root,
                policy=RootPolicy.ORGANIZE,
                enabled=True,
            ),
        ),
        "workspace",
    )


def _temporary_config(root: Path) -> MariaConfig:
    """Return a complete config whose paths remain under a temporary root."""
    return MariaConfig(
        app_name="Maria",
        version="0.2.0",
        environment="test",
        database_path=root / "database" / "maria.db",
        log_dir=root / "logs",
        prompt_path=root / "system_prompt.txt",
        filesystem_roots=_temporary_filesystem_roots(root),
        filesystem_protections=_temporary_filesystem_protections(root),
        log_level="INFO",
        confirmation_required=frozenset(),
        denied_actions=frozenset(),
    )


class _CollidingKey:
    """A key that collides with a declared field and refuses comparison."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __hash__(self) -> int:
        """Collide with the calculator's declared expression field."""
        self.calls.append("hash")
        return hash("expression")

    def __eq__(self, other: object) -> bool:
        """Refuse comparison loudly if validation ever attempts it."""
        self.calls.append("eq")
        raise RuntimeError("a submitted key must never be compared")

    def __str__(self) -> str:
        """Record that this key was converted to text."""
        self.calls.append("str")
        return "expression"

    def __repr__(self) -> str:
        """Record that this key was represented."""
        self.calls.append("repr")
        return "expression"


class _HostileNameKey(str):
    """A str subclass key whose equality refuses to cooperate."""

    def __eq__(self, other: object) -> bool:
        """Refuse comparison loudly if validation ever attempts it."""
        raise RuntimeError("a submitted key must never be compared")

    def __hash__(self) -> int:
        """Hash as the plain text this key subclasses."""
        return hash(str(self))


class _MappingSpoof:
    """A non-Mapping whose instance-level __class__ claims to be a dict."""

    @property
    def __class__(self) -> type:
        """Claim to be a dict without being one."""
        return dict


class _FailingIterationMapping(Mapping):
    """A real Mapping whose iteration fails."""

    def __iter__(self) -> Any:
        """Fail while the keys are being enumerated."""
        raise RuntimeError("iteration is unavailable")

    def __len__(self) -> int:
        """Report an empty mapping."""
        return 0

    def __getitem__(self, key: str) -> Any:
        """Never resolve a key."""
        raise KeyError(key)


class _FailingLookupMapping(Mapping):
    """A real Mapping that names a field it then refuses to return."""

    def __iter__(self) -> Any:
        """Offer exactly the calculator's declared field."""
        return iter(("expression",))

    def __len__(self) -> int:
        """Report the single offered key."""
        return 1

    def __getitem__(self, key: str) -> Any:
        """Fail while the declared value is being read."""
        raise RuntimeError("lookup is unavailable")


class _RaisingIterationMapping(Mapping):
    """A real Mapping whose iteration raises a chosen exception."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def __iter__(self) -> Any:
        """Raise the configured failure while enumerating the keys."""
        raise self.error

    def __len__(self) -> int:
        """Report an empty mapping."""
        return 0

    def __getitem__(self, key: str) -> Any:
        """Never resolve a key."""
        raise KeyError(key)


class _RaisingLookupMapping(Mapping):
    """A real Mapping whose declared-value retrieval raises."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def __iter__(self) -> Any:
        """Offer exactly the calculator's declared field."""
        return iter(("expression",))

    def __len__(self) -> int:
        """Report the single offered key."""
        return 1

    def __getitem__(self, key: str) -> Any:
        """Raise the configured failure while reading the value."""
        raise self.error


class _PlainMapping(Mapping):
    """An ordinary Mapping subclass over a private dictionary."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def __iter__(self) -> Any:
        """Iterate the underlying names."""
        return iter(self._values)

    def __len__(self) -> int:
        """Return how many names are carried."""
        return len(self._values)

    def __getitem__(self, key: str) -> Any:
        """Return one stored value."""
        return self._values[key]


class _TypeImpersonator:
    """A declared type whose equality claims to be a supported one."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __eq__(self, other: object) -> bool:
        """Record that equality was consulted, then claim a match."""
        self.calls.append("eq")
        return True

    def __hash__(self) -> int:
        """Hash as str so a tuple membership test would find it."""
        return hash(str)


class _RootClassSpoof:
    """An object whose instance-level __class__ claims to be a RootClass."""

    @property
    def __class__(self) -> type:
        """Claim to be a RootClass member without being one."""
        return RootClass


class _EligibilitySpoof:
    """An object whose instance-level __class__ claims to be a classification."""

    @property
    def __class__(self) -> type:
        """Claim to be a ModelEligibility member without being one."""
        return ModelEligibility


class _ArgumentContractSpoof:
    """An object whose instance-level __class__ claims to be a declaration."""

    @property
    def __class__(self) -> type:
        """Claim to be an ArgumentContract without being one."""
        return ArgumentContract


class _ActionContractSpoof:
    """An object whose instance-level __class__ claims to be a contract."""

    @property
    def __class__(self) -> type:
        """Claim to be an ActionContract without being one."""
        return ActionContract


class _KindTripwire:
    """A violation kind whose every user-defined hook records that it ran."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __hash__(self) -> int:
        """Record that this kind was hashed."""
        self.calls.append("hash")
        return hash("unknown_tool")

    def __eq__(self, other: object) -> bool:
        """Record that this kind was compared."""
        self.calls.append("eq")
        return True

    def __str__(self) -> str:
        """Record that this kind was converted to text."""
        self.calls.append("str")
        return "unknown_tool"

    def __repr__(self) -> str:
        """Record that this kind was represented."""
        self.calls.append("repr")
        return "unknown_tool"


class _NameSubclass(str):
    """A str subclass, which is never an exact str identifier."""


class _Impersonator:
    """An identifier whose hash and equality mimic a registered name."""

    def __init__(self, target: str) -> None:
        self.target = target

    def __hash__(self) -> int:
        """Return the hash of the name this object imitates."""
        return hash(self.target)

    def __eq__(self, other: object) -> bool:
        """Compare equal to the name this object imitates."""
        return other == self.target


class _IdentifierTripwire:
    """An identifier whose every user-defined hook records that it ran."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __hash__(self) -> int:
        """Record that this object was hashed."""
        self.calls.append("hash")
        return 0

    def __eq__(self, other: object) -> bool:
        """Record that this object was compared."""
        self.calls.append("eq")
        return True

    def __str__(self) -> str:
        """Record that this object was converted to text."""
        self.calls.append("str")
        return "system"

    def __repr__(self) -> str:
        """Record that this object was represented."""
        self.calls.append("repr")
        return "system"


class RegistryCompletenessTests(unittest.TestCase):
    """Verify the declared registry matches the shipped tool inventory."""

    def test_registry_declares_exactly_the_built_in_action_set(self) -> None:
        self.assertEqual(
            BUILT_IN_ACTION_CONTRACTS.action_identifiers,
            EXPECTED_ACTION_IDENTIFIERS,
        )
        # V1-20 added four saved-conversation and seven conversation-summary
        # actions to V1-18's twenty-nine, V1-22 eleven task and eleven
        # reminder actions, V1-23-R1 three delivery actions, and V1-24-R1
        # thirteen project actions.
        self.assertEqual(len(EXPECTED_ACTION_IDENTIFIERS), 78)

    def test_registry_and_loaded_tool_inventory_agree_in_both_directions(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            config = _temporary_config(root)
            manager = ToolManager(
                config,
                Memory(config.database_path, logger=Mock(spec=logging.Logger)),
                Mock(spec=logging.Logger),
            )
            manager.load_tools()
            loaded = manager.loaded_action_identifiers()

        declared = BUILT_IN_ACTION_CONTRACTS.action_identifiers
        self.assertEqual(loaded - declared, frozenset())
        self.assertEqual(declared - loaded, frozenset())
        self.assertEqual(loaded, declared)

    def test_loaded_inventory_snapshot_is_a_read_only_frozenset(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            config = _temporary_config(root)
            manager = ToolManager(
                config,
                Memory(config.database_path, logger=Mock(spec=logging.Logger)),
                Mock(spec=logging.Logger),
            )
            manager.load_tools()

            self.assertIsInstance(manager.loaded_action_identifiers(), frozenset)

    def test_every_action_is_declared_exactly_once(self) -> None:
        identifiers = [
            contract.identifier for contract in BUILT_IN_ACTION_CONTRACTS.contracts
        ]

        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertEqual(len(identifiers), 78)

    def test_no_contract_exists_for_a_mandatorily_denied_filesystem_action(
        self,
    ) -> None:
        for action in UNREGISTERED_ACTIONS:
            with self.subTest(action=action):
                self.assertIsNone(
                    BUILT_IN_ACTION_CONTRACTS.contract_for("filesystem", action)
                )
                self.assertNotIn(
                    f"filesystem.{action}",
                    BUILT_IN_ACTION_CONTRACTS.action_identifiers,
                )

    def test_every_field_declares_its_type_bounds_and_root_class(self) -> None:
        for contract in BUILT_IN_ACTION_CONTRACTS.contracts:
            for argument in contract.arguments:
                with self.subTest(field=f"{contract.identifier}.{argument.name}"):
                    self.assertIn(argument.value_type, (str, int))
                    self.assertIsInstance(argument.required, bool)
                    if argument.value_type is str:
                        self.assertIsNone(argument.minimum)
                        self.assertIsNone(argument.maximum)
                    else:
                        self.assertIsNone(argument.root_class)
                        self.assertIsNone(argument.minimum_length)
                        self.assertIsNone(argument.maximum_length)
                        self.assertIsNone(argument.allow_blank)

    def test_every_text_field_declares_blank_and_length_policy(self) -> None:
        text_fields = [
            (contract, argument)
            for contract in BUILT_IN_ACTION_CONTRACTS.contracts
            for argument in contract.arguments
            if argument.value_type is str
        ]

        # V1-20 added five: a summary's text and pinned identity on create,
        # the new text on edit, the pinned identity on enable_context, and a
        # save's transcript. V1-22 adds sixteen: seven for tasks (text and
        # due on create, status, as_of, the new text, the new due date, and
        # the pinned identity) and nine for reminders (text, when, and repeat
        # on create, status, as_of, the new text, when and repeat on
        # reschedule, and the pinned identity). V1-24-R1 adds eight: a
        # project's name and location on create, the new name, a fact's text
        # on add and on edit, an edit's pinned digest, a listing's folder, and
        # a read's path.
        self.assertEqual(len(text_fields), 47)
        for contract, argument in text_fields:
            with self.subTest(field=f"{contract.identifier}.{argument.name}"):
                self.assertIsInstance(argument.allow_blank, bool)
                for bound in (argument.minimum_length, argument.maximum_length):
                    self.assertTrue(bound is None or isinstance(bound, int))
                    if bound is not None:
                        self.assertGreaterEqual(bound, 0)

    def test_no_text_field_currently_accepts_blank_content(self) -> None:
        accepting_blank = {
            f"{contract.identifier}.{argument.name}"
            for contract in BUILT_IN_ACTION_CONTRACTS.contracts
            for argument in contract.arguments
            if argument.value_type is str and argument.allow_blank
        }

        self.assertEqual(accepting_blank, set())

    def test_only_the_expression_and_the_fact_identity_declare_text_bounds(
        self,
    ) -> None:
        bounded = {
            f"{contract.identifier}.{argument.name}": argument.maximum_length
            for contract in BUILT_IN_ACTION_CONTRACTS.contracts
            for argument in contract.arguments
            if argument.value_type is str and argument.maximum_length is not None
        }
        minimums = {
            f"{contract.identifier}.{argument.name}"
            for contract in BUILT_IN_ACTION_CONTRACTS.contracts
            for argument in contract.arguments
            if argument.value_type is str and argument.minimum_length is not None
        }

        self.assertEqual(
            bounded,
            {
                "calculator.calculate.expression": MAXIMUM_EXPRESSION_LENGTH,
                "memory.enable_context.fact_digest": FACT_DIGEST_LENGTH,
                "notes.enable_context.text_digest": FACT_DIGEST_LENGTH,
                # V1-20: session text Maria supplies is always bounded, the
                # owner's new summary text has the same ceiling, and each
                # pinned summary identity is exactly a SHA-256.
                "conversation_summaries.create.text": MAXIMUM_SUMMARY_LENGTH,
                "conversation_summaries.create.text_digest": FACT_DIGEST_LENGTH,
                "conversation_summaries.edit.text": MAXIMUM_SUMMARY_LENGTH,
                "conversation_summaries.enable_context.text_digest": (
                    FACT_DIGEST_LENGTH
                ),
                "conversations.save.transcript": MAXIMUM_TRANSCRIPT_LENGTH,
                # V1-22: task and reminder text has its own ceiling, and each
                # pinned identity is exactly a SHA-256. Formatted values are
                # bounded by their format instead.
                "tasks.create.text": MAXIMUM_TASK_TEXT_LENGTH,
                "tasks.edit.text": MAXIMUM_TASK_TEXT_LENGTH,
                "tasks.enable_context.text_digest": FACT_DIGEST_LENGTH,
                "reminders.create.text": MAXIMUM_TASK_TEXT_LENGTH,
                "reminders.edit.text": MAXIMUM_TASK_TEXT_LENGTH,
                "reminders.enable_context.text_digest": FACT_DIGEST_LENGTH,
                # V1-24-R1: every project text has a coarse ceiling of its
                # own, and a fact edit's pinned digest is exactly a SHA-256.
                "projects.create.name": MAXIMUM_PROJECT_NAME_LENGTH,
                "projects.create.location": MAXIMUM_PROJECT_LOCATION_LENGTH,
                "projects.retitle.name": MAXIMUM_PROJECT_NAME_LENGTH,
                "projects.add_fact.text": MAXIMUM_PROJECT_FACT_LENGTH,
                "projects.edit_fact.text": MAXIMUM_PROJECT_FACT_LENGTH,
                "projects.edit_fact.text_digest": FACT_DIGEST_LENGTH,
                "projects.list_files.folder": MAXIMUM_PROJECT_PATH_LENGTH,
                "projects.read_file.path": MAXIMUM_PROJECT_PATH_LENGTH,
            },
        )
        self.assertEqual(MAXIMUM_EXPRESSION_LENGTH, 200)
        self.assertEqual(FACT_DIGEST_LENGTH, 64)
        self.assertEqual(MAXIMUM_SUMMARY_LENGTH, 2048)
        self.assertEqual(MAXIMUM_TRANSCRIPT_LENGTH, 65536)
        self.assertEqual(MAXIMUM_TASK_TEXT_LENGTH, 512)
        self.assertEqual(MAXIMUM_PROJECT_NAME_LENGTH, 64)
        self.assertEqual(MAXIMUM_PROJECT_FACT_LENGTH, 512)
        self.assertEqual(MAXIMUM_PROJECT_PATH_LENGTH, 512)
        self.assertEqual(MAXIMUM_PROJECT_LOCATION_LENGTH, 522)
        self.assertEqual(
            minimums,
            {
                "memory.enable_context.fact_digest",
                "notes.enable_context.text_digest",
                "conversation_summaries.create.text_digest",
                "conversation_summaries.enable_context.text_digest",
                "tasks.enable_context.text_digest",
                "reminders.enable_context.text_digest",
                "projects.edit_fact.text_digest",
            },
        )
        digest = BUILT_IN_ACTION_CONTRACTS.contract_for(
            "memory",
            "enable_context",
        ).arguments[1]
        self.assertEqual(digest.minimum_length, FACT_DIGEST_LENGTH)
        self.assertFalse(digest.required)
        self.assertFalse(digest.allow_blank)

    def test_every_filesystem_path_field_declares_the_safe_root_class(self) -> None:
        declared_paths = {
            (contract.tool_name, contract.action, argument.name)
            for contract in BUILT_IN_ACTION_CONTRACTS.contracts
            for argument in contract.arguments
            if argument.is_path
        }

        self.assertEqual(declared_paths, EXPECTED_PATH_FIELDS)
        for tool_name, action, field in EXPECTED_PATH_FIELDS:
            with self.subTest(field=f"{tool_name}.{action}.{field}"):
                contract = BUILT_IN_ACTION_CONTRACTS.contract_for(tool_name, action)
                argument = next(
                    item for item in contract.arguments if item.name == field
                )
                self.assertEqual(
                    argument.root_class,
                    RootClass.NAMED_FILESYSTEM_ROOT,
                )

    def test_no_non_path_field_carries_a_path_root_class(self) -> None:
        for contract in BUILT_IN_ACTION_CONTRACTS.contracts:
            for argument in contract.arguments:
                key = (contract.tool_name, contract.action, argument.name)
                with self.subTest(field=f"{contract.identifier}.{argument.name}"):
                    if key not in EXPECTED_PATH_FIELDS:
                        self.assertIsNone(argument.root_class)

    def test_declared_identifier_and_limit_bounds_are_the_expected_ones(self) -> None:
        identifier_actions = (
            ("audit", "read"),
            ("audit", "delete"),
            ("history", "read"),
            ("history", "delete"),
            ("memory", "update"),
            ("memory", "forget"),
            ("memory", "enable_context"),
            ("memory", "disable_context"),
            ("notes", "retitle"),
            ("notes", "edit"),
            ("notes", "delete"),
            ("notes", "enable_context"),
            ("notes", "disable_context"),
            ("conversations", "read"),
            ("conversations", "delete"),
            ("conversation_summaries", "read"),
            ("conversation_summaries", "edit"),
            ("conversation_summaries", "delete"),
            ("conversation_summaries", "enable_context"),
            ("conversation_summaries", "disable_context"),
        )
        for tool_name, action in identifier_actions:
            with self.subTest(action=f"{tool_name}.{action}"):
                argument = BUILT_IN_ACTION_CONTRACTS.contract_for(
                    tool_name, action
                ).arguments[0]
                self.assertEqual(argument.name, "id")
                self.assertEqual(argument.value_type, int)
                self.assertTrue(argument.required)
                self.assertEqual(argument.minimum, 1)
                self.assertIsNone(argument.maximum)

        for tool_name, action in (
            ("memory", "search"),
            ("memory", "list"),
            ("notes", "list"),
        ):
            with self.subTest(action=f"{tool_name}.{action}"):
                contract = BUILT_IN_ACTION_CONTRACTS.contract_for(tool_name, action)
                argument = next(
                    item for item in contract.arguments if item.name == "limit"
                )
                self.assertEqual(argument.value_type, int)
                self.assertFalse(argument.required)
                self.assertEqual(argument.minimum, MINIMUM_LIST_LIMIT)
                self.assertEqual(argument.maximum, MAXIMUM_LIST_LIMIT)

    def test_optional_fields_are_exactly_the_expected_ones(self) -> None:
        optional = {
            (contract.tool_name, contract.action, argument.name)
            for contract in BUILT_IN_ACTION_CONTRACTS.contracts
            for argument in contract.arguments
            if not argument.required
        }

        self.assertEqual(
            optional,
            {
                ("filesystem", "list", "path"),
                ("memory", "search", "limit"),
                ("memory", "list", "limit"),
                ("notes", "list", "limit"),
                ("notes", "create", "title"),
                # Absent from the planner's plan and pinned when confirmation
                # is offered, so an approval names the exact fact text.
                ("memory", "enable_context", "fact_digest"),
                # V1-16: likewise pinned for a note, naming its exact title
                # and body together.
                ("notes", "enable_context", "text_digest"),
                # V1-20: likewise pinned for a summary, beside the full
                # preview of a draft or of the stored text.
                ("conversation_summaries", "create", "text_digest"),
                ("conversation_summaries", "enable_context", "text_digest"),
                # V1-22: a task may have no due date, and a task's or
                # reminder's enable is pinned like a note's.
                ("tasks", "create", "due"),
                ("tasks", "enable_context", "text_digest"),
                ("reminders", "enable_context", "text_digest"),
                # V1-24-R1: a fact edit's digest is pinned like a note's; a
                # listing may name no folder, and a read no line range.
                ("projects", "edit_fact", "text_digest"),
                ("projects", "list_files", "folder"),
                ("projects", "read_file", "start"),
                ("projects", "read_file", "end"),
            },
        )

    def test_the_project_line_numbers_are_whole_numbers_to_six_digits(
        self,
    ) -> None:
        contract = BUILT_IN_ACTION_CONTRACTS.contract_for("projects", "read_file")

        for argument in contract.arguments[1:]:
            with self.subTest(field=argument.name):
                self.assertIs(argument.value_type, int)
                self.assertFalse(argument.required)
                self.assertEqual(argument.minimum, 1)
                self.assertEqual(argument.maximum, MAXIMUM_LINE_NUMBER)
        self.assertEqual(MAXIMUM_LINE_NUMBER, 999_999)


class ModelEligibilityClassificationTests(unittest.TestCase):
    """Verify every action states whether a reasoning model may propose it."""

    def test_the_classification_has_exactly_two_members(self) -> None:
        self.assertEqual(
            {member.value for member in ModelEligibility},
            {"model_eligible", "not_model_eligible"},
        )

    def test_every_registered_action_is_classified_exactly_as_approved(
        self,
    ) -> None:
        classified = {
            contract.identifier: contract.model_eligibility
            for contract in BUILT_IN_ACTION_CONTRACTS.contracts
        }
        eligible = {
            identifier
            for identifier, decision in classified.items()
            if decision is ModelEligibility.MODEL_ELIGIBLE
        }
        closed = {
            identifier
            for identifier, decision in classified.items()
            if decision is ModelEligibility.NOT_MODEL_ELIGIBLE
        }

        # Both pinned sets together must name every registered action, so a
        # new action fails here until someone decides which side it is on.
        self.assertEqual(
            EXPECTED_MODEL_ELIGIBLE | EXPECTED_NOT_MODEL_ELIGIBLE,
            BUILT_IN_ACTION_CONTRACTS.action_identifiers,
        )
        self.assertEqual(eligible, EXPECTED_MODEL_ELIGIBLE)
        self.assertEqual(closed, EXPECTED_NOT_MODEL_ELIGIBLE)
        self.assertEqual((len(eligible), len(closed)), (5, 73))
        for identifier, decision in classified.items():
            with self.subTest(action=identifier):
                self.assertIs(type(decision), ModelEligibility)

    def test_a_contract_without_a_classification_cannot_be_declared(
        self,
    ) -> None:
        for declaration in (
            lambda: ActionContract("sample", "run"),
            lambda: ActionContract("sample", "run", ()),
        ):
            with self.subTest():
                with self.assertRaises(TypeError) as caught:
                    declaration()

                self.assertIn("model_eligibility", str(caught.exception))

    def test_the_classification_is_keyword_only(self) -> None:
        with self.assertRaises(TypeError):
            ActionContract("sample", "run", (), NOT_ELIGIBLE)

    def test_a_classification_must_be_an_exact_member(self) -> None:
        # A plain string equal to a member's value, a spoofed class, and a
        # member of another enumeration are each refused by exact type.
        for value in (
            "model_eligible",
            "not_model_eligible",
            "MODEL_ELIGIBLE",
            None,
            True,
            1,
            _EligibilitySpoof(),
            RootClass.NAMED_FILESYSTEM_ROOT,
        ):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ValueError) as caught:
                    ActionContract("sample", "run", model_eligibility=value)

                self.assertEqual(
                    str(caught.exception),
                    "model_eligibility for sample.run must be a "
                    "ModelEligibility member.",
                )

    def test_names_are_settled_before_the_classification(self) -> None:
        with self.assertRaises(ValueError) as caught:
            ActionContract("bad/tool", "run", model_eligibility="eligible")

        self.assertIn("tool name", str(caught.exception))
        self.assertNotIn("bad/tool", str(caught.exception))

    def test_both_members_are_accepted_and_kept_exactly(self) -> None:
        for decision in ModelEligibility:
            with self.subTest(decision=decision.value):
                contract = ActionContract(
                    "sample", "run", model_eligibility=decision
                )

                self.assertIs(contract.model_eligibility, decision)

    def test_the_classification_cannot_be_reassigned(self) -> None:
        contract = BUILT_IN_ACTION_CONTRACTS.contract_for("memory", "forget")

        with self.assertRaises(FrozenInstanceError):
            contract.model_eligibility = ModelEligibility.MODEL_ELIGIBLE

        self.assertIs(
            contract.model_eligibility,
            ModelEligibility.NOT_MODEL_ELIGIBLE,
        )


class RegistryConstructionTests(unittest.TestCase):
    """Verify duplicate rejection and immutability after construction."""

    def test_duplicate_action_declaration_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            ActionContractRegistry(
                (
                    ActionContract("sample", "run", model_eligibility=NOT_ELIGIBLE),
                    ActionContract(
                        "sample",
                        "run",
                        (
                            ArgumentContract(
                                "later",
                                str,
                                required=True,
                                allow_blank=True,
                                minimum_length=None,
                                maximum_length=None,
                            ),
                        ),
                        model_eligibility=NOT_ELIGIBLE,
                    ),
                )
            )

        self.assertIn("sample.run", str(caught.exception))

    def test_rejection_is_keyed_on_the_exact_tool_and_action_pair(self) -> None:
        registry = ActionContractRegistry(
            (
                ActionContract(
                    "sample",
                    "run",
                    (
                        ArgumentContract(
                            "first",
                            str,
                            required=True,
                            allow_blank=True,
                            minimum_length=None,
                            maximum_length=None,
                        ),
                    ),
                    model_eligibility=NOT_ELIGIBLE,
                ),
                ActionContract(
                    "sample",
                    "walk",
                    (
                        ArgumentContract(
                            "second",
                            str,
                            required=True,
                            allow_blank=True,
                            minimum_length=None,
                            maximum_length=None,
                        ),
                    ),
                    model_eligibility=NOT_ELIGIBLE,
                ),
            )
        )

        self.assertEqual(
            registry.contract_for("sample", "run").field_names,
            {"first"},
        )
        self.assertEqual(
            registry.contract_for("sample", "walk").field_names,
            {"second"},
        )

    def test_duplicate_field_inside_one_contract_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            ActionContract(
                "sample",
                "run",
                (
                    ArgumentContract(
                        "value",
                        str,
                        required=True,
                        allow_blank=True,
                        minimum_length=None,
                        maximum_length=None,
                    ),
                    ArgumentContract("value", int, required=False),
                ),
                model_eligibility=NOT_ELIGIBLE,
            )

        self.assertIn("value", str(caught.exception))

    def test_registry_refuses_attribute_assignment_and_deletion(self) -> None:
        registry = ActionContractRegistry(
            (ActionContract("sample", "run", model_eligibility=NOT_ELIGIBLE),)
        )

        with self.assertRaises(AttributeError):
            registry.extra = "value"
        with self.assertRaises(AttributeError):
            del registry._contracts

    def test_shipped_registry_cannot_be_extended_after_construction(self) -> None:
        with self.assertRaises(AttributeError):
            BUILT_IN_ACTION_CONTRACTS.contracts = ()

        with self.assertRaises(TypeError):
            BUILT_IN_ACTION_CONTRACTS._contracts[("sample", "run")] = ActionContract(
                "sample", "run", model_eligibility=NOT_ELIGIBLE
            )

        self.assertEqual(len(BUILT_IN_ACTION_CONTRACTS.action_identifiers), 78)

    def test_unsupported_declared_type_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ArgumentContract("value", float, required=True)

    def test_numeric_bounds_on_a_text_field_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ArgumentContract(
                "value",
                str,
                required=True,
                allow_blank=True,
                minimum=1,
            )

    def test_root_class_on_a_numeric_field_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ArgumentContract(
                "value",
                int,
                required=True,
                root_class=RootClass.NAMED_FILESYSTEM_ROOT,
            )

    def test_explicitly_unbounded_text_metadata_is_accepted(self) -> None:
        argument = ArgumentContract(
            "value",
            str,
            required=True,
            allow_blank=True,
            minimum_length=None,
            maximum_length=None,
        )

        self.assertTrue(argument.allow_blank)
        self.assertIsNone(argument.minimum_length)
        self.assertIsNone(argument.maximum_length)
        self.assertIsNone(argument.failure_for(""))

    def test_text_field_without_a_blank_policy_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            ArgumentContract("value", str, required=True)

        self.assertIn("blank policy", str(caught.exception))

    def test_negative_string_length_bounds_are_rejected(self) -> None:
        for bounds in ({"minimum_length": -1}, {"maximum_length": -1}):
            with self.subTest(bounds=tuple(bounds)):
                with self.assertRaises(ValueError) as caught:
                    ArgumentContract(
                        "value",
                        str,
                        required=True,
                        allow_blank=False,
                        **bounds,
                    )

                self.assertIn("negative", str(caught.exception))

    def test_reversed_string_length_bounds_are_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            ArgumentContract(
                "value",
                str,
                required=True,
                allow_blank=False,
                minimum_length=10,
                maximum_length=9,
            )

        self.assertIn("may not exceed", str(caught.exception))

    def test_equal_string_length_bounds_are_accepted(self) -> None:
        argument = ArgumentContract(
            "value",
            str,
            required=True,
            allow_blank=False,
            minimum_length=4,
            maximum_length=4,
        )

        self.assertIsNone(argument.failure_for("abcd"))
        self.assertEqual(
            argument.failure_for("abc"),
            ContractFailure.OUT_OF_RANGE,
        )

    def test_string_length_metadata_on_an_int_field_is_rejected(self) -> None:
        for bounds in ({"minimum_length": 1}, {"maximum_length": 5}):
            with self.subTest(bounds=tuple(bounds)):
                with self.assertRaises(ValueError) as caught:
                    ArgumentContract("value", int, required=True, **bounds)

                self.assertIn("str argument", str(caught.exception))

    def test_blank_policy_on_an_int_field_is_rejected(self) -> None:
        for policy in (True, False):
            with self.subTest(allow_blank=policy):
                with self.assertRaises(ValueError) as caught:
                    ArgumentContract(
                        "value",
                        int,
                        required=True,
                        allow_blank=policy,
                    )

                self.assertIn("blank policy", str(caught.exception))


class DeclarationInvariantTests(unittest.TestCase):
    """Verify incoherent declaration metadata is rejected, never normalized."""

    def _text_argument(self, **overrides: Any) -> ArgumentContract:
        """Build one text declaration with the given fields overridden."""
        declaration: dict[str, Any] = {
            "name": "value",
            "value_type": str,
            "required": True,
            "allow_blank": False,
            "minimum_length": None,
            "maximum_length": None,
        }
        declaration.update(overrides)
        return ArgumentContract(**declaration)

    def test_retained_argument_list_cannot_alter_a_built_contract(self) -> None:
        declarations = [self._text_argument(name="first")]
        contract = ActionContract(
            "sample", "run", declarations, model_eligibility=NOT_ELIGIBLE
        )

        declarations.append(self._text_argument(name="second"))
        declarations[0] = self._text_argument(name="replaced")

        self.assertIsInstance(contract.arguments, tuple)
        self.assertEqual(contract.field_names, {"first"})
        self.assertEqual(len(contract.arguments), 1)
        self.assertEqual(contract.arguments[0].name, "first")

    def test_retained_list_cannot_alter_a_registry_after_construction(self) -> None:
        declarations = [self._text_argument(name="first")]
        registry = ActionContractRegistry(
            (
                ActionContract(
                    "sample", "run", declarations, model_eligibility=NOT_ELIGIBLE
                ),
            )
        )

        declarations.clear()

        self.assertEqual(
            registry.contract_for("sample", "run").field_names,
            {"first"},
        )

    def test_a_non_argument_contract_element_is_rejected(self) -> None:
        for element in ("first", 1, None, ("first",)):
            with self.subTest(element=type(element).__name__):
                with self.assertRaises(ValueError) as caught:
                    ActionContract(
                        "sample", "run", [element], model_eligibility=NOT_ELIGIBLE
                    )

                self.assertIn("ArgumentContract", str(caught.exception))

    def test_a_non_action_contract_registry_element_is_rejected(self) -> None:
        for element in ("sample", 1, None, self._text_argument()):
            with self.subTest(element=type(element).__name__):
                with self.assertRaises(ValueError) as caught:
                    ActionContractRegistry((element,))

                self.assertIn("ActionContract", str(caught.exception))

    def test_a_non_iterable_arguments_declaration_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            ActionContract("sample", "run", 7, model_eligibility=NOT_ELIGIBLE)

        self.assertIn("iterable", str(caught.exception))

    def test_a_non_bool_required_flag_is_rejected(self) -> None:
        for value in ("yes", 1, 0, None):
            with self.subTest(required=repr(value)):
                with self.assertRaises(ValueError) as caught:
                    self._text_argument(required=value)

                self.assertIn("required", str(caught.exception))

    def test_a_non_bool_blank_policy_is_rejected(self) -> None:
        for value in ("no", 0, 1):
            with self.subTest(allow_blank=repr(value)):
                with self.assertRaises(ValueError) as caught:
                    self._text_argument(allow_blank=value)

                self.assertIn("allow_blank", str(caught.exception))

    def test_non_int_numeric_bounds_are_rejected(self) -> None:
        cases = (
            ("minimum", True),
            ("minimum", "1"),
            ("maximum", "100"),
            ("maximum", 1.5),
        )
        for label, value in cases:
            with self.subTest(bound=label, value=repr(value)):
                with self.assertRaises(ValueError) as caught:
                    ArgumentContract(
                        "value",
                        int,
                        required=True,
                        **{label: value},
                    )

                self.assertIn(label, str(caught.exception))

    def test_a_reversed_numeric_range_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            ArgumentContract("value", int, required=True, minimum=10, maximum=1)

        self.assertIn("may not exceed", str(caught.exception))

    def test_non_int_length_bounds_are_rejected(self) -> None:
        cases = (
            ("minimum_length", True),
            ("maximum_length", "200"),
            ("maximum_length", 2.5),
        )
        for label, value in cases:
            with self.subTest(bound=label, value=repr(value)):
                with self.assertRaises(ValueError) as caught:
                    self._text_argument(**{label: value})

                self.assertIn(label, str(caught.exception))

    def test_a_root_class_that_is_not_a_member_is_rejected(self) -> None:
        for value in ("named_filesystem_root", 1, object()):
            with self.subTest(root_class=type(value).__name__):
                with self.assertRaises(ValueError) as caught:
                    self._text_argument(root_class=value)

                self.assertIn("root_class", str(caught.exception))

    def test_an_unusable_field_name_is_rejected_without_being_repeated(
        self,
    ) -> None:
        unsafe = "C:\\Users\\ExC\\secret.txt"
        for value in (unsafe, "", "two words", 7, None, "z" * 500):
            with self.subTest(name=type(value).__name__):
                with self.assertRaises(ValueError) as caught:
                    self._text_argument(name=value)

                message = str(caught.exception)
                self.assertIn("ASCII identifier", message)
                self.assertNotIn(unsafe, message)


class DeclarationIdentifierTests(unittest.TestCase):
    """Verify a contract cannot be declared with an unusable tool or action."""

    UNUSABLE = (
        ("list", []),
        ("dict", {}),
        ("set", {1}),
        ("none", None),
        ("int", 1),
        ("bool", True),
        ("path", "bad/path"),
        ("windows path", "C:\\Users\\ExC\\secret.txt"),
        ("whitespace", "two words"),
        ("control", "bad\x07name"),
        ("newline", "bad\nname"),
        ("empty", ""),
        ("oversized", "z" * 500),
        ("subclass", _NameSubclass("sample")),
    )

    def test_an_unusable_tool_name_is_rejected(self) -> None:
        for label, value in self.UNUSABLE:
            with self.subTest(tool_name=label):
                with self.assertRaises(ValueError) as caught:
                    ActionContract(value, "run", model_eligibility=NOT_ELIGIBLE)

                self.assertIn("tool name", str(caught.exception))

    def test_an_unusable_action_is_rejected(self) -> None:
        for label, value in self.UNUSABLE:
            with self.subTest(action=label):
                with self.assertRaises(ValueError) as caught:
                    ActionContract("sample", value, model_eligibility=NOT_ELIGIBLE)

                self.assertIn("action", str(caught.exception))

    def test_the_rejection_message_never_repeats_the_unsafe_name(self) -> None:
        unsafe_names = (
            "C:\\Users\\ExC\\secret.txt",
            "/etc/passwd",
            "bad\nname",
            "two words",
            "z" * 500,
        )
        for unsafe in unsafe_names:
            with self.subTest(name=unsafe[:16]):
                for declaration in (
                    lambda: ActionContract(
                        unsafe, "run", model_eligibility=NOT_ELIGIBLE
                    ),
                    lambda: ActionContract(
                        "sample", unsafe, model_eligibility=NOT_ELIGIBLE
                    ),
                ):
                    with self.assertRaises(ValueError) as caught:
                        declaration()

                    self.assertNotIn(unsafe, str(caught.exception))

    def test_a_str_subclass_identifier_is_rejected(self) -> None:
        for tool_name, action in (
            (_NameSubclass("sample"), "run"),
            ("sample", _NameSubclass("run")),
        ):
            with self.subTest(subclassed="tool" if action == "run" else "action"):
                with self.assertRaises(ValueError):
                    ActionContract(
                        tool_name, action, model_eligibility=NOT_ELIGIBLE
                    )

    def test_ordinary_identifier_declarations_remain_valid(self) -> None:
        for tool_name, action in (
            ("calculator", "calculate"),
            ("filesystem", "read"),
            ("system", "current_time"),
            ("sample", "run"),
            ("sample", "startup_probe"),
        ):
            with self.subTest(action=f"{tool_name}.{action}"):
                contract = ActionContract(
                    tool_name, action, model_eligibility=NOT_ELIGIBLE
                )

                self.assertEqual(contract.identifier, f"{tool_name}.{action}")

    def test_a_failed_declaration_never_reaches_registry_insertion(self) -> None:
        accepted = ActionContract("sample", "run", model_eligibility=NOT_ELIGIBLE)

        with self.assertRaises(ValueError):
            ActionContractRegistry(
                (
                    accepted,
                    ActionContract(
                        "bad/tool", "walk", model_eligibility=NOT_ELIGIBLE
                    ),
                )
            )

        registry = ActionContractRegistry((accepted,))
        self.assertEqual(registry.action_identifiers, {"sample.run"})

    def test_contract_for_answers_none_for_a_malformed_identifier(self) -> None:
        for tool_name, action in (
            ([], "overview"),
            ({}, "overview"),
            ({1}, "overview"),
            (None, "overview"),
            (7, "overview"),
            ("system", []),
            ("system", {}),
            ("system", None),
            ("system", 7),
            (_NameSubclass("system"), _NameSubclass("overview")),
        ):
            pair = f"{type(tool_name).__name__}/{type(action).__name__}"
            with self.subTest(pair=pair):
                self.assertIsNone(
                    BUILT_IN_ACTION_CONTRACTS.contract_for(tool_name, action)
                )

    def test_contract_for_answers_none_for_an_impersonating_identifier(
        self,
    ) -> None:
        self.assertIsNone(
            BUILT_IN_ACTION_CONTRACTS.contract_for(
                _Impersonator("system"),
                _Impersonator("overview"),
            )
        )
        self.assertIsNotNone(
            BUILT_IN_ACTION_CONTRACTS.contract_for("system", "overview")
        )


class MalformedIdentifierTests(unittest.TestCase):
    """Verify call-time identifiers are settled by exact type first."""

    MALFORMED = (
        ("list", []),
        ("dict", {}),
        ("set", {1}),
        ("none", None),
        ("int", 7),
        ("bool", True),
        ("float", 1.5),
    )

    def test_a_malformed_tool_name_is_a_controlled_unknown_tool(self) -> None:
        for label, value in self.MALFORMED:
            with self.subTest(tool_name=label):
                violation = BUILT_IN_ACTION_CONTRACTS.validate(value, "run", {})

                self.assertEqual(violation.kind, ContractFailure.UNKNOWN_TOOL)
                self.assertEqual(violation.tool_name, "unknown_tool")
                self.assertEqual(
                    violation.message,
                    "Unsupported action: unknown_tool is not a supported tool.",
                )

    def test_a_malformed_action_on_a_known_tool_is_unknown_action(self) -> None:
        for label, value in self.MALFORMED:
            with self.subTest(action=label):
                violation = BUILT_IN_ACTION_CONTRACTS.validate(
                    "filesystem", value, {}
                )

                self.assertEqual(violation.kind, ContractFailure.UNKNOWN_ACTION)
                self.assertEqual(violation.action, "unknown_action")
                self.assertEqual(
                    violation.message,
                    "Unsupported action: filesystem.unknown_action is not a "
                    "supported action.",
                )

    def test_unknown_tool_precedence_survives_a_malformed_action(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate("scheduler", [], {})

        self.assertEqual(violation.kind, ContractFailure.UNKNOWN_TOOL)
        self.assertEqual(
            violation.message,
            "Unsupported action: scheduler is not a supported tool.",
        )

    def test_a_str_subclass_cannot_match_a_registered_declaration(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            _NameSubclass("system"),
            _NameSubclass("overview"),
            {},
        )

        self.assertEqual(violation.kind, ContractFailure.UNKNOWN_TOOL)
        self.assertEqual(
            violation.message,
            "Unsupported action: unknown_tool is not a supported tool.",
        )

    def test_a_str_subclass_action_cannot_match_a_registered_action(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "system",
            _NameSubclass("overview"),
            {},
        )

        self.assertEqual(violation.kind, ContractFailure.UNKNOWN_ACTION)
        self.assertEqual(violation.action, "unknown_action")

    def test_an_impersonating_identifier_never_resolves_to_a_contract(
        self,
    ) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            _Impersonator("system"),
            _Impersonator("overview"),
            {},
        )

        self.assertIsNotNone(violation)
        self.assertEqual(violation.kind, ContractFailure.UNKNOWN_TOOL)
        self.assertEqual(violation.tool_name, "unknown_tool")

    def test_an_impersonating_action_never_resolves_to_a_contract(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "system",
            _Impersonator("overview"),
            {},
        )

        self.assertIsNotNone(violation)
        self.assertEqual(violation.kind, ContractFailure.UNKNOWN_ACTION)

    def test_no_user_defined_identifier_method_is_invoked(self) -> None:
        tool_probe = _IdentifierTripwire()
        action_probe = _IdentifierTripwire()

        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            tool_probe, action_probe, {}
        )

        self.assertEqual(violation.kind, ContractFailure.UNKNOWN_TOOL)
        self.assertEqual(tool_probe.calls, [])
        self.assertEqual(action_probe.calls, [])

    def test_no_user_defined_action_method_is_invoked_for_a_known_tool(
        self,
    ) -> None:
        action_probe = _IdentifierTripwire()

        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "system", action_probe, {}
        )

        self.assertEqual(violation.kind, ContractFailure.UNKNOWN_ACTION)
        self.assertEqual(action_probe.calls, [])

    def test_contract_for_invokes_no_user_defined_method(self) -> None:
        probe = _IdentifierTripwire()

        self.assertIsNone(BUILT_IN_ACTION_CONTRACTS.contract_for(probe, probe))
        self.assertEqual(probe.calls, [])

    def test_the_registry_still_resolves_ordinary_identifiers(self) -> None:
        self.assertIsNone(
            BUILT_IN_ACTION_CONTRACTS.validate("system", "overview", {})
        )
        self.assertEqual(
            BUILT_IN_ACTION_CONTRACTS.validate("scheduler", "run", {}).message,
            "Unsupported action: scheduler is not a supported tool.",
        )
        self.assertEqual(
            BUILT_IN_ACTION_CONTRACTS.validate(
                "filesystem", "write", {"path": "x"}
            ).message,
            "Unsupported action: filesystem.write is not a supported action.",
        )


class SafeMetadataLabelTests(unittest.TestCase):
    """Verify unsafe names are replaced before a violation retains them."""

    UNSAFE = (
        "C:\\Users\\ExC\\secret.txt",
        "/etc/passwd",
        "with\nnewline",
        "with\x07control",
        "two words",
        "",
        "z" * 500,
        "unicode\u00e9",
    )

    def test_ordinary_identifiers_are_kept_exactly(self) -> None:
        for name in (
            "scheduler",
            "filesystem",
            "write",
            "surplus",
            "expression",
            "current_time",
            "not_an_action",
            "startup_probe",
        ):
            with self.subTest(name=name):
                violation = ContractViolation(
                    ContractFailure.UNKNOWN_ACTION,
                    name,
                    name,
                    name,
                )

                self.assertEqual(violation.tool_name, name)
                self.assertEqual(violation.action, name)
                self.assertEqual(violation.field, name)

    def test_unsafe_names_are_replaced_by_fixed_labels(self) -> None:
        for name in self.UNSAFE:
            with self.subTest(name=name[:20]):
                violation = ContractViolation(
                    ContractFailure.EXTRA_ARGUMENT,
                    name,
                    name,
                    name,
                )

                self.assertEqual(violation.tool_name, "unknown_tool")
                self.assertEqual(violation.action, "unknown_action")
                self.assertEqual(violation.field, "unsupported_field")
                if name:
                    self.assertNotIn(name, violation.message)

    def test_non_string_names_are_replaced_without_conversion(self) -> None:
        probe = _ConvertingProbe()

        violation = ContractViolation(
            ContractFailure.EXTRA_ARGUMENT,
            probe,
            probe,
            probe,
        )

        self.assertEqual(violation.tool_name, "unknown_tool")
        self.assertEqual(violation.action, "unknown_action")
        self.assertEqual(violation.field, "unsupported_field")
        self.assertFalse(probe.converted)

    def test_absent_action_and_field_stay_absent(self) -> None:
        violation = ContractViolation(ContractFailure.UNKNOWN_TOOL, "scheduler")

        self.assertIsNone(violation.action)
        self.assertIsNone(violation.field)
        self.assertEqual(
            violation.message,
            "Unsupported action: scheduler is not a supported tool.",
        )


class MalformedContainerTests(unittest.TestCase):
    """Verify a container that is not a well-keyed Mapping fails closed."""

    def test_a_non_mapping_container_is_refused_immediately(self) -> None:
        for container in (None, [], (), "expression", 7, {"a"}):
            with self.subTest(container=type(container).__name__):
                violation = BUILT_IN_ACTION_CONTRACTS.validate(
                    "system", "overview", container
                )

                self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)
                self.assertEqual(violation.field, "arguments")
                self.assertEqual(
                    violation.message,
                    "Invalid argument type for system.overview: arguments.",
                )

    def test_a_non_str_key_is_refused_before_sorting(self) -> None:
        for container in ({1: "x"}, {"a": "x", 1: "y"}, {(1, 2): "x"}):
            with self.subTest(container=tuple(container)):
                violation = BUILT_IN_ACTION_CONTRACTS.validate(
                    "system", "overview", container
                )

                self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)
                self.assertEqual(violation.field, "arguments")

    def test_a_missing_required_field_outranks_key_safety(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "history", "read", {1: "x"}
        )

        self.assertEqual(
            violation.kind,
            ContractFailure.MISSING_REQUIRED_ARGUMENT,
        )
        self.assertEqual(violation.field, "id")


class HostileArgumentKeyTests(unittest.TestCase):
    """Verify a submitted key is never hashed, compared, or converted."""

    def test_a_colliding_key_cannot_raise_through_a_required_check(self) -> None:
        probe = _CollidingKey()
        arguments = {probe: "x"}
        probe.calls.clear()

        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "calculator", "calculate", arguments
        )

        self.assertEqual(
            violation.kind,
            ContractFailure.MISSING_REQUIRED_ARGUMENT,
        )
        self.assertEqual(violation.field, "expression")
        self.assertEqual(
            violation.message,
            "Missing required argument for calculator.calculate: expression.",
        )
        self.assertEqual(probe.calls, [])

    def test_a_colliding_key_on_a_fieldless_action_is_a_container_failure(
        self,
    ) -> None:
        probe = _CollidingKey()
        arguments = {probe: "x"}
        probe.calls.clear()

        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "system", "overview", arguments
        )

        self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)
        self.assertEqual(violation.field, "arguments")
        self.assertEqual(probe.calls, [])

    def test_a_hostile_str_subclass_key_is_refused_without_comparison(
        self,
    ) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "system",
            "overview",
            {_HostileNameKey("overview"): 1},
        )

        self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)
        self.assertEqual(violation.field, "arguments")

    def test_a_class_spoofing_container_is_refused(self) -> None:
        spoof = _MappingSpoof()

        self.assertIsInstance(spoof, Mapping)
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "system", "overview", spoof
        )

        self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)
        self.assertEqual(violation.field, "arguments")
        self.assertEqual(
            violation.message,
            "Invalid argument type for system.overview: arguments.",
        )

    def test_a_mapping_whose_iteration_fails_is_refused(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "system", "overview", _FailingIterationMapping()
        )

        self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)
        self.assertEqual(violation.field, "arguments")

    def test_a_mapping_whose_lookup_fails_is_refused(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "calculator", "calculate", _FailingLookupMapping()
        )

        self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)
        self.assertEqual(violation.field, "arguments")

    def test_missing_required_still_outranks_a_malformed_key(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "history", "read", {1: "x"}
        )

        self.assertEqual(
            violation.kind,
            ContractFailure.MISSING_REQUIRED_ARGUMENT,
        )
        self.assertEqual(violation.field, "id")

    def test_every_ordinary_mapping_kind_still_validates(self) -> None:
        containers = (
            ("dict", {"expression": "1 + 1"}),
            ("mapping proxy", MappingProxyType({"expression": "1 + 1"})),
            ("plan arguments", PlanArguments({"expression": "1 + 1"})),
            ("mapping subclass", _PlainMapping({"expression": "1 + 1"})),
        )
        for label, container in containers:
            with self.subTest(container=label):
                self.assertIsNone(
                    BUILT_IN_ACTION_CONTRACTS.validate(
                        "calculator", "calculate", container
                    )
                )

    def test_ordinary_mappings_still_report_ordinary_defects(self) -> None:
        containers = (
            ("dict", {"expression": 7}),
            ("mapping proxy", MappingProxyType({"expression": 7})),
            ("mapping subclass", _PlainMapping({"expression": 7})),
        )
        for label, container in containers:
            with self.subTest(container=label):
                violation = BUILT_IN_ACTION_CONTRACTS.validate(
                    "calculator", "calculate", container
                )

                self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)
                self.assertEqual(violation.field, "expression")


class ResourceFailurePropagationTests(unittest.TestCase):
    """Verify a resource failure is never answered as an argument defect.

    Running out of memory, or being interrupted, says nothing about the
    arguments a caller supplied, so neither may be reported as though it
    did. Ordinary failures from a hostile mapping stay controlled.
    """

    def test_memory_error_propagates_from_key_enumeration(self) -> None:
        container = _RaisingIterationMapping(MemoryError("exhausted"))

        with self.assertRaises(MemoryError):
            BUILT_IN_ACTION_CONTRACTS.validate(
                "calculator", "calculate", container
            )

    def test_memory_error_propagates_from_value_retrieval(self) -> None:
        container = _RaisingLookupMapping(MemoryError("exhausted"))

        with self.assertRaises(MemoryError):
            BUILT_IN_ACTION_CONTRACTS.validate(
                "calculator", "calculate", container
            )

    def test_interrupting_failures_propagate_from_both_operations(self) -> None:
        for label, error in (
            ("keyboard interrupt", KeyboardInterrupt()),
            ("system exit", SystemExit(1)),
            ("generator exit", GeneratorExit()),
        ):
            for operation, factory in (
                ("enumeration", _RaisingIterationMapping),
                ("retrieval", _RaisingLookupMapping),
            ):
                with self.subTest(error=label, operation=operation):
                    with self.assertRaises(type(error)):
                        BUILT_IN_ACTION_CONTRACTS.validate(
                            "calculator",
                            "calculate",
                            factory(error),
                        )

    def test_ordinary_failures_remain_controlled_in_both_operations(self) -> None:
        for label, error in (
            ("runtime error", RuntimeError("boom")),
            ("value error", ValueError("bad")),
            ("type error", TypeError("wrong")),
        ):
            for operation, factory in (
                ("enumeration", _RaisingIterationMapping),
                ("retrieval", _RaisingLookupMapping),
            ):
                with self.subTest(error=label, operation=operation):
                    violation = BUILT_IN_ACTION_CONTRACTS.validate(
                        "calculator",
                        "calculate",
                        factory(error),
                    )

                    self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)
                    self.assertEqual(violation.field, "arguments")
                    self.assertEqual(
                        violation.message,
                        "Invalid argument type for calculator.calculate: "
                        "arguments.",
                    )


class ExactDeclarationTypeTests(unittest.TestCase):
    """Verify declarations are admitted by exact type, never by equality."""

    def _text_argument(self, **overrides: Any) -> ArgumentContract:
        """Build one text declaration with the given fields overridden."""
        declaration: dict[str, Any] = {
            "name": "value",
            "value_type": str,
            "required": True,
            "allow_blank": False,
            "minimum_length": None,
            "maximum_length": None,
        }
        declaration.update(overrides)
        return ArgumentContract(**declaration)

    def test_a_value_type_impersonating_str_is_rejected_without_comparison(
        self,
    ) -> None:
        probe = _TypeImpersonator()

        with self.assertRaises(ValueError) as caught:
            ArgumentContract(
                "value",
                probe,
                required=True,
                allow_blank=False,
                minimum_length=None,
                maximum_length=None,
            )

        self.assertIn("Unsupported declared argument type", str(caught.exception))
        self.assertEqual(probe.calls, [])

    def test_a_class_spoofing_root_class_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._text_argument(root_class=_RootClassSpoof())

        self.assertIn("root_class", str(caught.exception))

    def test_an_argument_contract_subclass_is_rejected(self) -> None:
        class _SubclassedArgument(ArgumentContract):
            """A declaration subclass that would override validation."""

            def failure_for(self, value: Any) -> None:
                """Approve every value, which a declaration must never do."""
                return None

        subclassed = _SubclassedArgument(
            "value",
            str,
            required=True,
            allow_blank=False,
            minimum_length=None,
            maximum_length=None,
        )

        with self.assertRaises(ValueError) as caught:
            ActionContract(
                "sample", "run", (subclassed,), model_eligibility=NOT_ELIGIBLE
            )

        self.assertIn("exact ArgumentContract", str(caught.exception))

    def test_a_class_spoofing_argument_contract_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            ActionContract(
                "sample",
                "run",
                (_ArgumentContractSpoof(),),
                model_eligibility=NOT_ELIGIBLE,
            )

        self.assertIn("exact ArgumentContract", str(caught.exception))

    def test_an_action_contract_subclass_is_rejected(self) -> None:
        class _SubclassedContract(ActionContract):
            """A contract subclass that would bypass argument validation."""

            def validate(self, arguments: Any) -> None:
                """Approve every call, which a contract must never do."""
                return None

        with self.assertRaises(ValueError) as caught:
            ActionContractRegistry(
                (
                    _SubclassedContract(
                        "sample", "run", model_eligibility=NOT_ELIGIBLE
                    ),
                )
            )

        self.assertIn("exact ActionContract", str(caught.exception))

    def test_a_class_spoofing_action_contract_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            ActionContractRegistry((_ActionContractSpoof(),))

        self.assertIn("exact ActionContract", str(caught.exception))

    def test_ordinary_exact_declarations_remain_accepted(self) -> None:
        contract = ActionContract(
            "sample",
            "run",
            (
                self._text_argument(),
                ArgumentContract("count", int, required=False, minimum=1),
            ),
            model_eligibility=NOT_ELIGIBLE,
        )
        registry = ActionContractRegistry((contract,))

        self.assertEqual(registry.action_identifiers, {"sample.run"})
        self.assertIsNone(
            registry.validate("sample", "run", {"value": "x", "count": 2})
        )


class ViolationKindTests(unittest.TestCase):
    """Verify a violation kind is settled by exact type before use."""

    def test_a_plain_string_equal_to_a_member_value_is_rejected(self) -> None:
        for value in ("unknown_tool", "wrong_type", "out_of_range"):
            with self.subTest(kind=value):
                with self.assertRaises(ValueError) as caught:
                    ContractViolation(value, "scheduler")

                self.assertIn("ContractFailure member", str(caught.exception))

    def test_an_unhashable_kind_is_rejected_without_hashing(self) -> None:
        class _HashRaiser:
            def __hash__(self) -> int:
                raise RuntimeError("a kind must never be hashed")

        with self.assertRaises(ValueError) as caught:
            ContractViolation(_HashRaiser(), "scheduler")

        self.assertIn("ContractFailure member", str(caught.exception))

    def test_no_hostile_kind_method_is_invoked(self) -> None:
        probe = _KindTripwire()

        with self.assertRaises(ValueError):
            ContractViolation(probe, "scheduler")

        self.assertEqual(probe.calls, [])

    def test_every_real_member_is_still_accepted(self) -> None:
        for kind in ContractFailure:
            with self.subTest(kind=kind.value):
                violation = ContractViolation(kind, "notes", "edit", "body")

                self.assertIs(violation.kind, kind)
                self.assertTrue(violation.message)


class ValidationCategoryTests(unittest.TestCase):
    """Verify each typed failure kind and its exact owner-visible text."""

    def test_unknown_tool_is_reported_before_any_action_lookup(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate("scheduler", "run", {})

        self.assertEqual(violation.kind, ContractFailure.UNKNOWN_TOOL)
        self.assertEqual(violation.tool_name, "scheduler")
        self.assertIsNone(violation.action)
        self.assertIsNone(violation.field)
        self.assertEqual(
            violation.message,
            "Unsupported action: scheduler is not a supported tool.",
        )

    def test_unknown_action_for_a_known_tool_is_reported(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "filesystem",
            "write",
            {"path": "notes.txt"},
        )

        self.assertEqual(violation.kind, ContractFailure.UNKNOWN_ACTION)
        self.assertEqual(violation.tool_name, "filesystem")
        self.assertEqual(violation.action, "write")
        self.assertIsNone(violation.field)
        self.assertEqual(
            violation.message,
            "Unsupported action: filesystem.write is not a supported action.",
        )

    def test_missing_required_argument_is_reported(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate("history", "read", {})

        self.assertEqual(violation.kind, ContractFailure.MISSING_REQUIRED_ARGUMENT)
        self.assertEqual(violation.field, "id")
        self.assertEqual(
            violation.message,
            "Missing required argument for history.read: id.",
        )

    def test_extra_argument_is_reported(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "system",
            "current_time",
            {"timezone": "UTC"},
        )

        self.assertEqual(violation.kind, ContractFailure.EXTRA_ARGUMENT)
        self.assertEqual(violation.field, "timezone")
        self.assertEqual(
            violation.message,
            "Unsupported argument for system.current_time: timezone.",
        )

    def test_wrong_type_is_reported(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "calculator",
            "calculate",
            {"expression": 42},
        )

        self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)
        self.assertEqual(violation.field, "expression")
        self.assertEqual(
            violation.message,
            "Invalid argument type for calculator.calculate: expression.",
        )

    def test_out_of_range_value_is_reported(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate("notes", "delete", {"id": 0})

        self.assertEqual(violation.kind, ContractFailure.OUT_OF_RANGE)
        self.assertEqual(violation.field, "id")
        self.assertEqual(
            violation.message,
            "Argument out of supported range for notes.delete: id.",
        )

    def test_every_failure_kind_has_a_distinct_fixed_template(self) -> None:
        messages = {
            ContractViolation(kind, "notes", "edit", "body").message
            for kind in ContractFailure
        }

        self.assertEqual(len(messages), len(ContractFailure))

    def test_no_failure_message_exposes_a_value_repr_or_path(self) -> None:
        secret = "sentinel-contract-value-4b7e21"
        cases = (
            ("filesystem", "write", {"path": secret}),
            ("scheduler", "run", {"path": secret}),
            ("notes", "edit", {"id": 4, "body": secret, "extra": secret}),
            ("notes", "edit", {"id": 0, "body": secret}),
            ("notes", "edit", {"id": secret, "body": secret}),
            ("notes", "edit", {"body": secret}),
        )
        for tool_name, action, arguments in cases:
            with self.subTest(action=f"{tool_name}.{action}"):
                violation = BUILT_IN_ACTION_CONTRACTS.validate(
                    tool_name, action, arguments
                )
                message = violation.message

                self.assertNotIn(secret, message)
                self.assertNotIn("object at 0x", message)
                self.assertNotIn("\\", message)
                self.assertNotIn("/", message)
                self.assertNotIn("'", message)


class FailureOrderTests(unittest.TestCase):
    """Verify the documented deterministic order of reported failures."""

    def test_unknown_tool_outranks_unknown_action(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate("scheduler", "write", {})

        self.assertEqual(violation.kind, ContractFailure.UNKNOWN_TOOL)

    def test_unknown_action_outranks_argument_defects(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "filesystem",
            "write",
            {"unsupported": 1},
        )

        self.assertEqual(violation.kind, ContractFailure.UNKNOWN_ACTION)

    def test_missing_required_outranks_extra_and_type_defects(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "notes",
            "edit",
            {"id": "not-an-int", "surplus": 1},
        )

        self.assertEqual(violation.kind, ContractFailure.MISSING_REQUIRED_ARGUMENT)
        self.assertEqual(violation.field, "body")

    def test_missing_required_fields_are_reported_in_declared_order(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate("notes", "retitle", {})

        self.assertEqual(violation.field, "id")

    def test_extra_argument_outranks_type_and_range_defects(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "notes",
            "edit",
            {"id": 0, "body": 7, "surplus": 1},
        )

        self.assertEqual(violation.kind, ContractFailure.EXTRA_ARGUMENT)
        self.assertEqual(violation.field, "surplus")

    def test_extra_arguments_are_reported_in_sorted_order(self) -> None:
        for arguments in (
            {"id": 4, "body": "text", "zulu": 1, "alpha": 2},
            {"alpha": 2, "zulu": 1, "body": "text", "id": 4},
        ):
            with self.subTest(order=tuple(arguments)):
                violation = BUILT_IN_ACTION_CONTRACTS.validate(
                    "notes", "edit", arguments
                )

                self.assertEqual(violation.field, "alpha")

    def test_present_fields_are_checked_in_declared_order(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "notes",
            "edit",
            {"id": "bad", "body": 7},
        )

        self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)
        self.assertEqual(violation.field, "id")

    def test_type_is_checked_before_bounds_for_one_field(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "notes",
            "delete",
            {"id": "0"},
        )

        self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)


class TypeAndCoercionTests(unittest.TestCase):
    """Verify exact type semantics and the absence of any coercion."""

    def test_bool_is_not_accepted_for_an_int_field(self) -> None:
        for value in (True, False):
            with self.subTest(value=value):
                violation = BUILT_IN_ACTION_CONTRACTS.validate(
                    "notes", "delete", {"id": value}
                )

                self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)

    def test_numeric_string_is_not_accepted_for_an_int_field(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate("notes", "delete", {"id": "7"})

        self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)

    def test_int_is_not_accepted_for_a_text_field(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "memory", "store", {"fact": 7}
        )

        self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)

    def test_object_with_side_effecting_str_is_rejected_without_conversion(
        self,
    ) -> None:
        probe = _ConvertingProbe()

        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "memory",
            "store",
            {"fact": probe},
        )

        self.assertEqual(violation.kind, ContractFailure.WRONG_TYPE)
        self.assertFalse(probe.converted)

    def test_extra_fields_are_rejected_rather_than_ignored(self) -> None:
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "history",
            "list",
            {"limit": 5},
        )

        self.assertEqual(violation.kind, ContractFailure.EXTRA_ARGUMENT)

    def test_absent_optional_field_is_accepted_without_being_inserted(self) -> None:
        arguments: dict[str, Any] = {}

        violation = BUILT_IN_ACTION_CONTRACTS.validate("notes", "list", arguments)

        self.assertIsNone(violation)
        self.assertEqual(arguments, {})

    def test_valid_arguments_are_left_exactly_as_supplied(self) -> None:
        fact = "a fact worth keeping"
        arguments = {"id": 3, "fact": fact}

        violation = BUILT_IN_ACTION_CONTRACTS.validate("memory", "update", arguments)

        self.assertIsNone(violation)
        self.assertEqual(arguments, {"id": 3, "fact": fact})
        self.assertIs(arguments["fact"], fact)

    def test_casing_and_whitespace_are_never_normalized(self) -> None:
        arguments = {"query": "  MiXeD Case  "}

        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "history", "search", arguments
        )

        self.assertIsNone(violation)
        self.assertEqual(arguments["query"], "  MiXeD Case  ")

    def test_every_planner_emitted_shape_satisfies_its_contract(self) -> None:
        emitted = (
            ("audit", "list", {}),
            ("audit", "read", {"id": 1}),
            ("audit", "delete", {"id": 1}),
            ("calculator", "calculate", {"expression": "2 + 2"}),
            ("filesystem", "list", {"path": "docs"}),
            ("filesystem", "read", {"path": "notes.txt"}),
            ("filesystem", "rename", {"source": "a", "destination": "b"}),
            ("filesystem", "move", {"source": "a", "destination": "b"}),
            ("history", "list", {}),
            ("history", "search", {"query": "calculate"}),
            ("history", "read", {"id": 1}),
            ("history", "delete", {"id": 1}),
            ("memory", "store", {"fact": "a fact"}),
            ("memory", "search", {"query": "a fact"}),
            ("memory", "list", {}),
            ("memory", "update", {"id": 1, "fact": "new"}),
            ("memory", "forget", {"id": 1}),
            ("notes", "create", {"title": "Title", "body": "Body"}),
            ("notes", "list", {}),
            ("notes", "read", {"target": "Title"}),
            ("notes", "retitle", {"id": 1, "title": "New"}),
            ("notes", "edit", {"id": 1, "body": "New"}),
            ("notes", "delete", {"id": 1}),
            ("system", "overview", {}),
            ("system", "current_time", {}),
        )

        self.assertEqual(len(emitted), 25)
        for tool_name, action, arguments in emitted:
            with self.subTest(action=f"{tool_name}.{action}"):
                self.assertIsNone(
                    BUILT_IN_ACTION_CONTRACTS.validate(tool_name, action, arguments)
                )


class BoundaryTests(unittest.TestCase):
    """Verify every declared numeric boundary directly."""

    def test_identifier_minimum_boundary(self) -> None:
        cases = ((0, ContractFailure.OUT_OF_RANGE), (1, None), (2, None))
        for value, expected in cases:
            with self.subTest(value=value):
                violation = BUILT_IN_ACTION_CONTRACTS.validate(
                    "notes", "delete", {"id": value}
                )
                self.assertEqual(
                    None if violation is None else violation.kind,
                    expected,
                )

    def test_identifier_has_no_declared_maximum(self) -> None:
        self.assertIsNone(
            BUILT_IN_ACTION_CONTRACTS.validate(
                "notes", "delete", {"id": 2**62}
            )
        )

    def test_listing_limit_boundaries(self) -> None:
        cases = (
            (0, ContractFailure.OUT_OF_RANGE),
            (1, None),
            (100, None),
            (101, ContractFailure.OUT_OF_RANGE),
        )
        for tool_name, action in (
            ("memory", "list"),
            ("notes", "list"),
        ):
            for value, expected in cases:
                with self.subTest(action=f"{tool_name}.{action}", value=value):
                    violation = BUILT_IN_ACTION_CONTRACTS.validate(
                        tool_name, action, {"limit": value}
                    )
                    self.assertEqual(
                        None if violation is None else violation.kind,
                        expected,
                    )

    def test_memory_search_limit_boundaries(self) -> None:
        cases = (
            (0, ContractFailure.OUT_OF_RANGE),
            (1, None),
            (100, None),
            (101, ContractFailure.OUT_OF_RANGE),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                violation = BUILT_IN_ACTION_CONTRACTS.validate(
                    "memory",
                    "search",
                    {"query": "term", "limit": value},
                )
                self.assertEqual(
                    None if violation is None else violation.kind,
                    expected,
                )

    def test_calculator_expression_length_boundaries(self) -> None:
        cases = (
            ("1" * (MAXIMUM_EXPRESSION_LENGTH - 1), None),
            ("1" * MAXIMUM_EXPRESSION_LENGTH, None),
            ("1" * (MAXIMUM_EXPRESSION_LENGTH + 1), ContractFailure.OUT_OF_RANGE),
        )
        for value, expected in cases:
            with self.subTest(length=len(value)):
                violation = BUILT_IN_ACTION_CONTRACTS.validate(
                    "calculator", "calculate", {"expression": value}
                )
                self.assertEqual(
                    None if violation is None else violation.kind,
                    expected,
                )

    def test_calculator_expression_length_is_measured_after_stripping(
        self,
    ) -> None:
        padded = "  " + "1" * MAXIMUM_EXPRESSION_LENGTH + "  "

        self.assertEqual(len(padded), MAXIMUM_EXPRESSION_LENGTH + 4)
        self.assertIsNone(
            BUILT_IN_ACTION_CONTRACTS.validate(
                "calculator", "calculate", {"expression": padded}
            )
        )

    def test_blank_text_boundaries_across_every_text_field(self) -> None:
        representative = (
            ("calculator", "calculate", "expression", {}),
            ("history", "search", "query", {}),
            ("memory", "store", "fact", {}),
            ("memory", "update", "fact", {"id": 1}),
            ("notes", "create", "body", {"title": "Title"}),
            ("notes", "create", "title", {"body": "Body"}),
            ("notes", "read", "target", {}),
            ("notes", "retitle", "title", {"id": 1}),
            ("notes", "edit", "body", {"id": 1}),
            ("filesystem", "read", "path", {}),
            ("filesystem", "list", "path", {}),
            ("filesystem", "rename", "source", {"destination": "b.txt"}),
            ("filesystem", "rename", "destination", {"source": "a.txt"}),
            ("filesystem", "move", "source", {"destination": "b.txt"}),
            ("filesystem", "move", "destination", {"source": "a.txt"}),
        )
        for tool_name, action, field, base in representative:
            for blank in ("", "   ", " \t\r\n "):
                with self.subTest(field=f"{tool_name}.{action}.{field}"):
                    violation = BUILT_IN_ACTION_CONTRACTS.validate(
                        tool_name, action, {**base, field: blank}
                    )

                    self.assertIsNotNone(violation)
                    self.assertEqual(violation.kind, ContractFailure.OUT_OF_RANGE)
                    self.assertEqual(violation.field, field)

            with self.subTest(field=f"{tool_name}.{action}.{field}", case="valid"):
                self.assertIsNone(
                    BUILT_IN_ACTION_CONTRACTS.validate(
                        tool_name, action, {**base, field: "notes.txt"}
                    )
                )

    def test_filesystem_path_validation_touches_no_filesystem_api(self) -> None:
        with (
            patch.object(Path, "resolve") as resolve,
            patch.object(Path, "exists") as exists,
            patch.object(Path, "is_file") as is_file,
            patch.object(Path, "is_dir") as is_dir,
        ):
            valid = BUILT_IN_ACTION_CONTRACTS.validate(
                "filesystem", "read", {"path": "docs/notes.txt"}
            )
            blank = BUILT_IN_ACTION_CONTRACTS.validate(
                "filesystem", "read", {"path": "  "}
            )

        self.assertIsNone(valid)
        self.assertEqual(blank.kind, ContractFailure.OUT_OF_RANGE)
        resolve.assert_not_called()
        exists.assert_not_called()
        is_file.assert_not_called()
        is_dir.assert_not_called()

    def test_optional_title_distinguishes_absent_from_provided_blank(self) -> None:
        absent = BUILT_IN_ACTION_CONTRACTS.validate(
            "notes", "create", {"body": "Body"}
        )
        provided = "Provided Title"
        arguments = {"title": provided, "body": "Body"}
        valid = BUILT_IN_ACTION_CONTRACTS.validate("notes", "create", arguments)

        self.assertIsNone(absent)
        self.assertIsNone(valid)
        self.assertIs(arguments["title"], provided)
        for blank in ("", "   "):
            with self.subTest(blank=repr(blank)):
                violation = BUILT_IN_ACTION_CONTRACTS.validate(
                    "notes", "create", {"title": blank, "body": "Body"}
                )

                self.assertEqual(violation.kind, ContractFailure.OUT_OF_RANGE)
                self.assertEqual(violation.field, "title")

    def test_text_validation_returns_the_original_value_unchanged(self) -> None:
        original = "   Mixed Case Fact   "
        arguments = {"fact": original}

        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            "memory", "store", arguments
        )

        self.assertIsNone(violation)
        self.assertIs(arguments["fact"], original)
        self.assertEqual(arguments["fact"], original)


# V1-22: Saturday 2027-10-30, 10:03:27 local at +02:00 in the synthetic zone,
# and one canonical reminder time.
TIMING_REFERENCE = datetime(2027, 10, 30, 8, 3, 27, tzinfo=timezone.utc)
CANONICAL_WHEN = f"2027-10-31T09:00:00+01:00[{SYNTHETIC_KEY}]"

# Every task and reminder command form, with each optional part both ways.
TASK_AND_REMINDER_COMMANDS = (
    "add task buy milk",
    "add task due friday: buy milk",
    "tasks",
    "completed tasks",
    "due tasks",
    "read task 1",
    "complete task 1",
    "reopen task 1",
    "edit task 1: buy oat milk",
    "reschedule task 1: none",
    "reschedule task 1: in 3 days",
    "delete task 1",
    "enable task context 1",
    "disable task context 1",
    "add reminder tomorrow 9:00: call mom",
    "add reminder 2027-10-31 02:30 +01:00: call mom",
    "add reminder in 20 minutes: stir",
    "reminders",
    "completed reminders",
    "due reminders",
    "read reminder 1",
    "complete reminder 1",
    "reopen reminder 1",
    "edit reminder 1: call dad",
    "reschedule reminder 1: tomorrow 18:30",
    "reschedule reminder 1: in 2 hours",
    "delete reminder 1",
    "enable reminder context 1",
    "disable reminder context 1",
    "reminder delivery",
    "enable reminder delivery",
    "disable reminder delivery",
)


class TaskAndReminderContractTests(unittest.TestCase):
    """Pin V1-22's closed formats, and prove every command's step meets them."""

    def test_closed_formats_are_declared_on_exactly_these_fields(self) -> None:
        declared = {
            (contract.tool_name, contract.action, argument.name): (
                argument.text_format,
                argument.allowed_values,
            )
            for contract in BUILT_IN_ACTION_CONTRACTS.contracts
            for argument in contract.arguments
            if argument.text_format is not None or argument.allowed_values is not None
        }
        zoned = (TextFormat.ZONED_LOCAL_TIME, None)
        calendar_date = (TextFormat.ISO_DATE, None)
        statuses = (None, ("open", "completed"))
        never = (None, ("never",))

        self.assertEqual(
            declared,
            {
                ("reminders", "create", "when"): zoned,
                ("reminders", "create", "repeat"): never,
                ("reminders", "list", "status"): statuses,
                ("reminders", "due", "as_of"): (TextFormat.UTC_INSTANT, None),
                ("reminders", "reschedule", "when"): zoned,
                ("reminders", "reschedule", "repeat"): never,
                ("tasks", "create", "due"): calendar_date,
                ("tasks", "list", "status"): statuses,
                ("tasks", "due", "as_of"): calendar_date,
                ("tasks", "reschedule", "due"): (TextFormat.ISO_DATE_OR_NONE, None),
            },
        )
        self.assertEqual(TASK_STATUSES, ("open", "completed"))
        self.assertEqual(REPEAT_VALUES, ("never",))

    def test_malformed_timing_and_closed_values_are_refused(self) -> None:
        reminder = {"text": "call mom", "when": CANONICAL_WHEN, "repeat": "never"}
        cases = (
            (
                "tasks",
                "create",
                {"text": "pay rent", "due": "2026-10-09"},
                "due",
                ("2026-02-30", "friday", "none", "2026-10-9", "2026-10-09 "),
            ),
            (
                "tasks",
                "reschedule",
                {"id": 1, "due": "none"},
                "due",
                ("None", "2026-02-30", "tomorrow"),
            ),
            (
                "tasks",
                "due",
                {"as_of": "2026-10-04"},
                "as_of",
                ("2026-10-04T08:03:27Z", "today"),
            ),
            ("tasks", "list", {"status": "open"}, "status", ("done", "Open", "open ")),
            (
                "reminders",
                "create",
                reminder,
                "when",
                (
                    "tomorrow at 9:00",
                    "2027-10-31T09:00:00+01:00",
                    "2027-10-31T09:00:15+01:00[Maria/Synthetic]",
                    "2027-02-30T09:00:00+01:00[Maria/Synthetic]",
                    "2027-10-31T24:00:00+01:00[Maria/Synthetic]",
                    "2027-10-31T09:00:00-00:00[Maria/Synthetic]",
                    "2027-10-31T09:00:00+01:00[/etc/localtime]",
                ),
            ),
            ("reminders", "create", reminder, "repeat", ("daily", "Never", "never ")),
            (
                "reminders",
                "reschedule",
                {"id": 1, "when": CANONICAL_WHEN, "repeat": "never"},
                "when",
                ("in 2 hours", "2027-10-31T09:00:00Z"),
            ),
            (
                "reminders",
                "due",
                {"as_of": "2026-10-04T08:03:27Z"},
                "as_of",
                ("2026-10-04", "2026-10-04T08:03:27+00:00", "2026-10-04T08:03Z"),
            ),
            ("reminders", "list", {"status": "completed"}, "status", ("closed",)),
        )
        for tool_name, action, valid, field, refused in cases:
            with self.subTest(field=f"{tool_name}.{action}.{field}"):
                self.assertIsNone(
                    BUILT_IN_ACTION_CONTRACTS.validate(tool_name, action, valid)
                )
            for value in refused:
                with self.subTest(field=f"{tool_name}.{action}.{field}", value=value):
                    violation = BUILT_IN_ACTION_CONTRACTS.validate(
                        tool_name,
                        action,
                        {**valid, field: value},
                    )
                    self.assertIsNotNone(violation)
                    self.assertEqual(
                        (violation.kind, violation.field),
                        (ContractFailure.OUT_OF_RANGE, field),
                    )

    def test_blank_task_and_reminder_values_are_refused(self) -> None:
        for tool_name, action, field, base in (
            ("tasks", "create", "text", {}),
            ("tasks", "create", "due", {"text": "pay rent"}),
            ("tasks", "edit", "text", {"id": 1}),
            ("tasks", "reschedule", "due", {"id": 1}),
            ("tasks", "due", "as_of", {}),
            ("tasks", "list", "status", {}),
            (
                "reminders",
                "create",
                "text",
                {"when": CANONICAL_WHEN, "repeat": "never"},
            ),
            ("reminders", "create", "when", {"text": "x", "repeat": "never"}),
            ("reminders", "create", "repeat", {"text": "x", "when": CANONICAL_WHEN}),
            ("reminders", "edit", "text", {"id": 1}),
            ("reminders", "reschedule", "when", {"id": 1, "repeat": "never"}),
            ("reminders", "due", "as_of", {}),
            ("reminders", "list", "status", {}),
        ):
            for blank in ("", "   ", " \t\r\n "):
                with self.subTest(
                    field=f"{tool_name}.{action}.{field}",
                    blank=ascii(blank),
                ):
                    violation = BUILT_IN_ACTION_CONTRACTS.validate(
                        tool_name,
                        action,
                        {**base, field: blank},
                    )
                    self.assertIsNotNone(violation)
                    self.assertEqual(
                        (violation.kind, violation.field),
                        (ContractFailure.OUT_OF_RANGE, field),
                    )

    def test_every_task_and_reminder_command_plans_a_contract_valid_step(
        self,
    ) -> None:
        SyntheticZones(self, {SYNTHETIC_KEY: CENTRAL_RULES})
        timing = TimingContext(TIMING_REFERENCE, SYNTHETIC_KEY)
        planner = Planner(logger=Mock(spec=logging.Logger))
        covered = set()

        for command in TASK_AND_REMINDER_COMMANDS:
            with self.subTest(command=command):
                plan = planner.create_plan(command, timing=timing)
                (step,) = plan.steps
                covered.add(f"{step.tool_name}.{step.action}")
                self.assertIsNone(
                    BUILT_IN_ACTION_CONTRACTS.validate(
                        step.tool_name,
                        step.action,
                        step.arguments,
                    )
                )

        self.assertEqual(
            covered,
            {
                identifier
                for identifier in EXPECTED_ACTION_IDENTIFIERS
                if identifier.startswith(("tasks.", "reminders."))
            },
        )


class _ConvertingProbe:
    """An object whose text conversion is observable but must never happen."""

    def __init__(self) -> None:
        self.converted = False

    def __str__(self) -> str:
        """Record that a caller converted this object to text."""
        self.converted = True
        return "converted"


if __name__ == "__main__":
    unittest.main()
