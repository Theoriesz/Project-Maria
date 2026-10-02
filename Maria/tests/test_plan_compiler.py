"""Tests for the model-to-plan compilation gate, from V1-17 and V1-18.

The gate is proven entirely against hostile output: the V1-12 adversarial
fake's eight categories, the compiler-focused proposals beside them, and
hand-written variations. Every refusal is asserted by its exact category, and
every argument refusal is shown on an action the model may propose, beside a
valid counterpart that compiles, so none can pass merely because the
allowlist refused first. A compiled plan is model-originated, and checking it
again applies the same gates to its existing step exactly as it stands.
Compiling and checking again reach no tool, dispatch, permission check,
memory layer, network, or filesystem API; Brain is the only production module
that reaches the gate; and nothing the model supplied survives into a refusal
or a log.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import core.plan_compiler as plan_compiler_module
from core.action_contracts import (
    BUILT_IN_ACTION_CONTRACTS,
    MAXIMUM_EXPRESSION_LENGTH,
    ActionContract,
    ActionContractRegistry,
    ArgumentContract,
    ContractFailure,
    ModelEligibility,
)
from core.constants import PROJECT_ROOT
from core.failures import Failure, FailureKind
from core.logger import configure_logging
from core.memory import Memory
from core.permissions import PermissionDecision, PermissionManager
from core.plan_compiler import (
    COMPILER_CONTRACTS_MESSAGE,
    COMPILER_PLAN_MESSAGE,
    COMPILER_REQUEST_MESSAGE,
    COMPILER_RESPONSE_MESSAGE,
    MODEL_ACTION_ALLOWLIST,
    MODEL_PROPOSAL_MAX_BYTES,
    MODEL_PROPOSAL_MAX_NESTING,
    UNSAFE_TEXT_CATEGORIES,
    ProposalRejection,
    compile_proposal,
    recheck_model_plan,
)
from core.planner import (
    MODEL_PLAN_SHAPE_MESSAGE,
    Plan,
    PlanArguments,
    PlanOrigin,
    PlanStep,
    RecordingPolicy,
    is_model_plan,
)
from core.reasoning import ReasoningRequest, ReasoningResponse, request_reasoning
from core.tool_manager import BUILT_IN_TOOL_MODULES, ToolManager
from tests.log_assertions import assert_log_omits
from tests.reasoning_fakes import (
    FAKE_NAME_SENTINEL,
    FAKE_PAYLOAD_SENTINEL,
    OVER_BYTE_CAP_SOURCE,
    AdversarialReasoningProvider,
    HostileOutput,
    HostileProposal,
    hostile_payload,
    hostile_proposal,
)
from tests.test_reasoning import (
    PRODUCTION_SOURCES,
    enabled_settings,
    forbidden_calls,
    forbidden_imports,
    own_imports,
    peak_allocation,
    rendered,
)
from tools.audit import AuditTool
from tools.calculator import CalculatorTool
from tools.filesystem import FilesystemTool
from tools.history import HistoryTool
from tools.memory import MemoryTool
from tools.notes import NotesTool
from tools.system import SystemTool


REQUEST = "Please help with this."

# A distinctive owner request. It becomes a compiled plan's request, which may
# carry private text, so it must never reach a log either.
REQUEST_SENTINEL = "owner-request-sentinel-3d8b1f0a6e"

BACKSLASH = chr(92)

EXPECTED_ALLOWLIST = frozenset(
    {"calculator.calculate", "filesystem.list", "system.current_time"}
)

# The actions PermissionManager allows with an empty configuration that the
# model may nevertheless never propose. Deleting an audit summary is absent
# only because it always requires confirmation.
PERMITTED_BUT_CLOSED = frozenset(
    {
        "audit.list",
        "audit.read",
        "filesystem.read",
        "history.list",
        "history.search",
        "history.read",
        "memory.store",
        "memory.search",
        "memory.list",
        "notes.create",
        "notes.list",
        "notes.read",
        "system.overview",
    }
)

EXPECTED_REJECTION_CODES = frozenset(
    {
        "oversized",
        "malformed_output",
        "malformed_proposal",
        "unknown_tool",
        "unknown_action",
        "not_model_eligible",
        "missing_argument",
        "extra_argument",
        "wrong_type",
        "out_of_range",
        "unsafe_text",
        "unsafe_path",
    }
)

DIGEST = "0" * 64

# One contract-valid argument mapping for every registered action.
VALID_ARGUMENTS: dict[str, dict[str, Any]] = {
    "audit.list": {},
    "audit.read": {"id": 1},
    "audit.delete": {"id": 1},
    "calculator.calculate": {"expression": "2 + 2"},
    "filesystem.read": {"path": "notes.txt"},
    "filesystem.list": {"path": "docs"},
    "filesystem.rename": {"source": "old.txt", "destination": "new.txt"},
    "filesystem.move": {"source": "old.txt", "destination": "archive/old.txt"},
    "history.list": {},
    "history.search": {"query": "weather"},
    "history.read": {"id": 1},
    "history.delete": {"id": 1},
    "memory.store": {"fact": "The owner prefers tea."},
    "memory.search": {"query": "tea", "limit": 5},
    "memory.list": {"limit": 5},
    "memory.update": {"id": 1, "fact": "The owner prefers coffee."},
    "memory.forget": {"id": 1},
    "memory.enable_context": {"id": 1, "fact_digest": DIGEST},
    "memory.disable_context": {"id": 1},
    "notes.create": {"title": "Plans", "body": "Visit the library."},
    "notes.list": {"limit": 5},
    "notes.read": {"target": "1"},
    "notes.retitle": {"id": 1, "title": "Later plans"},
    "notes.edit": {"id": 1, "body": "Visit the museum."},
    "notes.delete": {"id": 1},
    "notes.enable_context": {"id": 1, "text_digest": DIGEST},
    "notes.disable_context": {"id": 1},
    "system.overview": {},
    "system.current_time": {},
}

# What the compiler must do with each V1-12 category. The out-of-range
# payload targets memory.update, which the model may never propose, so the
# allowlist refuses it before its contract is ever read.
EXPECTED_V1_12_OUTCOMES = {
    HostileOutput.MALFORMED_STRUCTURE: ProposalRejection.MALFORMED_PROPOSAL,
    HostileOutput.UNKNOWN_TOOL: ProposalRejection.UNKNOWN_TOOL,
    HostileOutput.UNKNOWN_ACTION: ProposalRejection.UNKNOWN_ACTION,
    HostileOutput.EXTRA_ARGUMENTS: ProposalRejection.EXTRA_ARGUMENT,
    HostileOutput.OUT_OF_RANGE_VALUES: ProposalRejection.NOT_MODEL_ELIGIBLE,
    HostileOutput.INJECTED_INSTRUCTION: ProposalRejection.MALFORMED_OUTPUT,
    HostileOutput.TRUNCATED_OUTPUT: ProposalRejection.MALFORMED_OUTPUT,
    HostileOutput.OVERSIZED_OUTPUT: ProposalRejection.OVERSIZED,
}

# What the compiler must do with each compiler-focused proposal. None means
# the proposal is valid and must compile.
EXPECTED_PROPOSAL_OUTCOMES = {
    HostileProposal.CONTRACT_LENGTH: ProposalRejection.OUT_OF_RANGE,
    HostileProposal.OVER_BYTE_CAP: ProposalRejection.OVERSIZED,
    HostileProposal.MISSING_ARGUMENT: ProposalRejection.MISSING_ARGUMENT,
    HostileProposal.WRONG_TYPE: ProposalRejection.WRONG_TYPE,
    HostileProposal.EXTRA_NAMED_ARGUMENT: ProposalRejection.EXTRA_ARGUMENT,
    HostileProposal.UNKNOWN_NAMED_TOOL: ProposalRejection.UNKNOWN_TOOL,
    HostileProposal.UNKNOWN_NAMED_ACTION: ProposalRejection.UNKNOWN_ACTION,
    HostileProposal.PATH_OUTSIDE_ROOT: ProposalRejection.UNSAFE_PATH,
    HostileProposal.NOT_MODEL_ELIGIBLE: ProposalRejection.NOT_MODEL_ELIGIBLE,
    HostileProposal.INJECTED_FIELDS: ProposalRejection.MALFORMED_PROPOSAL,
    HostileProposal.INERT_INSTRUCTION_PATH: None,
}

# One character of every refused category, each written as a code point so
# no invisible character sits in this source. Line feed, carriage return,
# tab, and next line are also whitespace that stripping would remove.
UNSAFE_CHARACTERS = (
    ("NUL", chr(0x00)),
    ("BEL", chr(0x07)),
    ("TAB", chr(0x09)),
    ("LF", chr(0x0A)),
    ("CR", chr(0x0D)),
    ("ESC", chr(0x1B)),
    ("DEL", chr(0x7F)),
    ("NEL", chr(0x85)),
    ("CSI", chr(0x9B)),
    ("SOFT HYPHEN", chr(0xAD)),
    ("ZERO WIDTH SPACE", chr(0x200B)),
    ("LEFT-TO-RIGHT MARK", chr(0x200E)),
    ("RIGHT-TO-LEFT OVERRIDE", chr(0x202E)),
    ("LEFT-TO-RIGHT ISOLATE", chr(0x2066)),
    ("BYTE ORDER MARK", chr(0xFEFF)),
    ("HIGH SURROGATE", chr(0xD800)),
    ("LOW SURROGATE", chr(0xDFFF)),
    ("LINE SEPARATOR", chr(0x2028)),
    ("PARAGRAPH SEPARATOR", chr(0x2029)),
)

UNSAFE_PATHS = (
    # Parent traversal and the dot-only spellings Windows treats alike.
    "..",
    "../outside",
    "docs/../../outside",
    "docs/..",
    ".." + BACKSLASH + "outside",
    "docs" + BACKSLASH + ".." + BACKSLASH + "outside",
    "...",
    ". .",
    " .. /outside",
    "docs/ /outside",
    # Nothing named at all.
    ".",
    "./",
    # Rooted, absolute, drive, and UNC forms under either reading.
    "/",
    "/etc/passwd",
    BACKSLASH + "Windows",
    BACKSLASH * 2 + "server" + BACKSLASH + "share" + BACKSLASH + "x",
    "//server/share/x",
    BACKSLASH * 2 + "." + BACKSLASH + "PhysicalDrive0",
    "C:" + BACKSLASH + "Windows",
    "C:/Windows",
    "C:relative",
    "c:",
    BACKSLASH * 2 + "?" + BACKSLASH + "C:" + BACKSLASH + "x",
    # Colons and alternate data streams.
    "notes.txt:secret",
    "docs/file:stream:$DATA",
    # Home expansion.
    "~",
    "~/secrets",
    "~root/.ssh",
    "~" + BACKSLASH + "secrets",
    # Reserved device names, with or without an extension or trailing space.
    "CON",
    "con",
    "nul.txt",
    "docs/NUL",
    "docs" + BACKSLASH + "com3",
    "AUX.log",
    "PRN",
    "COM1",
    "lpt9.txt",
    "COM" + chr(0xB9),
    "CONIN$",
    "CONOUT$",
    "CON ",
)

SAFE_PATHS = (
    "docs",
    "docs/",
    "docs/reports/2026",
    "docs" + BACKSLASH + "reports",
    "a/./b",
    ".hidden",
    "..hidden",
    "notes.v2.txt",
    "a/~b",
    "My Files",
    "Caf" + chr(0xE9),
    chr(0x8CC7) + chr(0x6599),
    "CONSOLE",
    "COM10",
    "file.con",
    "com",
)


def _proposal(tool: Any, action: Any, arguments: Any, **extra: Any) -> str:
    """Render one proposal as JSON text, with any extra top-level fields."""
    return json.dumps(
        {"tool": tool, "action": action, "arguments": arguments, **extra}
    )


def _split(identifier: str) -> tuple[str, str]:
    """Return the tool and action an identifier names."""
    tool_name, action = identifier.split(".")
    return tool_name, action


def _compile(
    text: str,
    *,
    request: str = REQUEST,
    contracts: ActionContractRegistry = BUILT_IN_ACTION_CONTRACTS,
) -> Plan | ProposalRejection:
    """Compile one proposal text with a quiet logger."""
    return compile_proposal(
        request,
        ReasoningResponse(text),
        contracts,
        logger=Mock(spec=logging.Logger),
    )


def _recheck(
    plan: Plan,
    contracts: ActionContractRegistry = BUILT_IN_ACTION_CONTRACTS,
) -> ProposalRejection | None:
    """Check one plan again with a quiet logger."""
    return recheck_model_plan(plan, contracts, logger=Mock(spec=logging.Logger))


def _hand_built(
    tool_name: str,
    action: str,
    arguments: dict[str, Any],
    request: str = REQUEST,
) -> Plan:
    """Return, built by hand, the model plan a correct compilation must equal.

    Nothing stops code from building such a plan around any step, so the
    same helper also supplies steps the compiler itself would have refused.
    """
    return Plan(
        original_request=request,
        steps=(PlanStep(tool_name, action, arguments),),
        recording=RecordingPolicy.TRANSIENT,
        origin=PlanOrigin.MODEL,
    )


def _registry_with(
    identifier: str,
    eligibility: ModelEligibility,
) -> ActionContractRegistry:
    """Return the shipped registry with one action reclassified."""
    return ActionContractRegistry(
        replace(contract, model_eligibility=eligibility)
        if contract.identifier == identifier
        else contract
        for contract in BUILT_IN_ACTION_CONTRACTS.contracts
    )


def _registry_without(*identifiers: str) -> ActionContractRegistry:
    """Return the shipped registry with some actions left undeclared."""
    return ActionContractRegistry(
        contract
        for contract in BUILT_IN_ACTION_CONTRACTS.contracts
        if contract.identifier not in identifiers
    )


def _registry_declaring(
    identifier: str,
    *arguments: ArgumentContract,
) -> ActionContractRegistry:
    """Return the shipped registry with one action's arguments redeclared."""
    return ActionContractRegistry(
        replace(contract, arguments=arguments)
        if contract.identifier == identifier
        else contract
        for contract in BUILT_IN_ACTION_CONTRACTS.contracts
    )


def _padded(proposal: str, characters: int) -> str:
    """Pad a JSON object inside its opening brace to exactly ``characters``."""
    return "{" + " " * (characters - len(proposal)) + proposal[1:]


def _fake_texts() -> list[str]:
    """Return every hostile text the fake can emit for the compiler."""
    texts = [
        hostile_payload(output, MODEL_PROPOSAL_MAX_BYTES)
        if output is HostileOutput.OVERSIZED_OUTPUT
        else hostile_payload(output)
        for output in HostileOutput
    ]
    texts += [hostile_proposal(proposal) for proposal in HostileProposal]
    return texts


# Every callable whose use would mean compiling executed, dispatched, decided
# permission, persisted, reached the network, or touched the filesystem.
_TRIPWIRE_TARGETS = (
    (ToolManager, "execute_tool"),
    (ToolManager, "validate_action"),
    (ToolManager, "prepare_confirmation"),
    (ToolManager, "load_tools"),
    (AuditTool, "execute"),
    (CalculatorTool, "execute"),
    (FilesystemTool, "execute"),
    (HistoryTool, "execute"),
    (MemoryTool, "execute"),
    (NotesTool, "execute"),
    (SystemTool, "execute"),
    (PermissionManager, "check"),
    (Memory, "initialize"),
    (sqlite3, "connect"),
    (socket, "socket"),
    (socket, "create_connection"),
    (subprocess, "Popen"),
    (Path, "resolve"),
    (Path, "exists"),
    (Path, "is_file"),
    (Path, "is_dir"),
    (Path, "iterdir"),
    (Path, "stat"),
    (Path, "open"),
    (Path, "expanduser"),
    (os, "stat"),
    (os, "lstat"),
    (os, "listdir"),
    (os, "scandir"),
    (os.path, "expanduser"),
)


@contextmanager
def _tripwires() -> Iterator[dict[str, Mock]]:
    """Make every forbidden callable fail if reached, and hand back each one.

    A reached tripwire raises, and each mock is returned so a test can also
    assert it was never called, in case something swallowed the failure.
    """
    with ExitStack() as stack:
        mocks = {
            f"{getattr(owner, '__name__', owner)}.{name}": stack.enter_context(
                patch.object(
                    owner,
                    name,
                    side_effect=AssertionError(f"{name} was reached"),
                )
            )
            for owner, name in _TRIPWIRE_TARGETS
        }
        yield mocks


class AllowlistTests(unittest.TestCase):
    """Prove the closed allowlist and the eligibility flags agree, both ways."""

    def test_the_allowlist_is_exactly_the_approved_closed_set(self) -> None:
        self.assertIs(type(MODEL_ACTION_ALLOWLIST), frozenset)
        self.assertEqual(MODEL_ACTION_ALLOWLIST, EXPECTED_ALLOWLIST)

    def test_the_allowlist_is_a_strict_subset_of_registered_actions(self) -> None:
        registered = BUILT_IN_ACTION_CONTRACTS.action_identifiers

        self.assertEqual(MODEL_ACTION_ALLOWLIST - registered, frozenset())
        self.assertLess(MODEL_ACTION_ALLOWLIST, registered)

    def test_the_allowlist_and_the_eligibility_flags_agree_both_ways(
        self,
    ) -> None:
        flagged = {
            contract.identifier
            for contract in BUILT_IN_ACTION_CONTRACTS.contracts
            if contract.model_eligibility is ModelEligibility.MODEL_ELIGIBLE
        }

        self.assertEqual(MODEL_ACTION_ALLOWLIST - flagged, frozenset())
        self.assertEqual(flagged - MODEL_ACTION_ALLOWLIST, set())

    def test_the_allowlist_is_a_literal_declaration(self) -> None:
        tree = ast.parse(Path(plan_compiler_module.__file__).read_text("utf-8"))
        assignments = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and [getattr(target, "id", None) for target in node.targets]
            == ["MODEL_ACTION_ALLOWLIST"]
        ]

        self.assertEqual(len(assignments), 1)
        value = assignments[0].value
        self.assertIsInstance(value, ast.Call)
        self.assertEqual(value.func.id, "frozenset")
        (members,) = value.args
        self.assertIsInstance(members, ast.Set)
        self.assertEqual(
            {element.value for element in members.elts},
            EXPECTED_ALLOWLIST,
        )
        self.assertTrue(
            all(isinstance(element, ast.Constant) for element in members.elts)
        )

    def test_an_eligible_flag_without_an_allowlist_entry_opens_nothing(
        self,
    ) -> None:
        widened = _registry_with("memory.store", ModelEligibility.MODEL_ELIGIBLE)
        text = _proposal("memory", "store", VALID_ARGUMENTS["memory.store"])

        self.assertIs(
            widened.contract_for("memory", "store").model_eligibility,
            ModelEligibility.MODEL_ELIGIBLE,
        )
        self.assertIs(
            _compile(text, contracts=widened),
            ProposalRejection.NOT_MODEL_ELIGIBLE,
        )

    def test_an_allowlisted_action_classified_closed_is_refused(self) -> None:
        text = _proposal("calculator", "calculate", {"expression": "2 + 2"})
        closed = _registry_with(
            "calculator.calculate",
            ModelEligibility.NOT_MODEL_ELIGIBLE,
        )

        self.assertIs(
            _compile(text, contracts=closed),
            ProposalRejection.NOT_MODEL_ELIGIBLE,
        )
        self.assertEqual(
            _compile(text),
            _hand_built("calculator", "calculate", {"expression": "2 + 2"}),
        )

    def test_an_allowlisted_action_the_registry_lacks_is_refused(self) -> None:
        listing = _proposal("filesystem", "list", {"path": "docs"})
        arithmetic = _proposal("calculator", "calculate", {"expression": "1"})

        self.assertIs(
            _compile(listing, contracts=_registry_without("filesystem.list")),
            ProposalRejection.UNKNOWN_ACTION,
        )
        self.assertIs(
            _compile(
                arithmetic,
                contracts=_registry_without("calculator.calculate"),
            ),
            ProposalRejection.UNKNOWN_TOOL,
        )

    def test_the_sample_arguments_cover_every_action_and_are_valid(
        self,
    ) -> None:
        self.assertEqual(
            set(VALID_ARGUMENTS),
            BUILT_IN_ACTION_CONTRACTS.action_identifiers,
        )
        for identifier, arguments in VALID_ARGUMENTS.items():
            with self.subTest(action=identifier):
                self.assertIsNone(
                    BUILT_IN_ACTION_CONTRACTS.validate(
                        *_split(identifier),
                        arguments,
                    )
                )

    def test_every_closed_action_is_refused_despite_valid_arguments(
        self,
    ) -> None:
        for identifier, arguments in VALID_ARGUMENTS.items():
            if identifier in MODEL_ACTION_ALLOWLIST:
                continue
            with self.subTest(action=identifier):
                outcome = _compile(_proposal(*_split(identifier), arguments))

                self.assertIs(outcome, ProposalRejection.NOT_MODEL_ELIGIBLE)

    def test_every_open_action_compiles_from_valid_arguments(self) -> None:
        for identifier in sorted(MODEL_ACTION_ALLOWLIST):
            arguments = VALID_ARGUMENTS[identifier]
            with self.subTest(action=identifier):
                outcome = _compile(_proposal(*_split(identifier), arguments))

                self.assertEqual(
                    outcome,
                    _hand_built(*_split(identifier), arguments),
                )
                self.assertIs(outcome.origin, PlanOrigin.MODEL)


class PermissionOrderTests(unittest.TestCase):
    """Prove the allowlist refuses before any permission decision exists."""

    def test_actions_permission_would_allow_are_refused_without_asking(
        self,
    ) -> None:
        permissions = PermissionManager(
            frozenset(),
            frozenset(),
            logger=Mock(spec=logging.Logger),
        )
        allowed = {
            identifier
            for identifier in VALID_ARGUMENTS
            if permissions.check(*_split(identifier)).decision
            is PermissionDecision.ALLOWED
        }
        self.assertEqual(allowed - MODEL_ACTION_ALLOWLIST, PERMITTED_BUT_CLOSED)

        responses = {
            identifier: ReasoningResponse(
                _proposal(*_split(identifier), VALID_ARGUMENTS[identifier])
            )
            for identifier in PERMITTED_BUT_CLOSED
        }
        logger = Mock(spec=logging.Logger)
        with _tripwires() as mocks:
            outcomes = {
                identifier: compile_proposal(
                    REQUEST,
                    response,
                    logger=logger,
                )
                for identifier, response in responses.items()
            }

        for identifier, outcome in outcomes.items():
            with self.subTest(action=identifier):
                self.assertIs(outcome, ProposalRejection.NOT_MODEL_ELIGIBLE)
        mocks["PermissionManager.check"].assert_not_called()

    def test_the_v1_12_out_of_range_payload_never_reaches_its_contract(
        self,
    ) -> None:
        payload = hostile_payload(HostileOutput.OUT_OF_RANGE_VALUES)
        proposal = json.loads(payload)

        # The contract would refuse it too, so only the order decides which
        # gate answers.
        violation = BUILT_IN_ACTION_CONTRACTS.validate(
            proposal["tool"],
            proposal["action"],
            proposal["arguments"],
        )
        self.assertIs(violation.kind, ContractFailure.OUT_OF_RANGE)

        with patch.object(
            ActionContract,
            "validate",
            side_effect=AssertionError("contract was read"),
        ) as validate:
            outcome = _compile(payload)

        self.assertIs(outcome, ProposalRejection.NOT_MODEL_ELIGIBLE)
        validate.assert_not_called()


class ProposalFormatTests(unittest.TestCase):
    """Prove only one strict JSON proposal object is ever read."""

    SMALLEST = '{"tool":"system","action":"current_time","arguments":{}}'

    def test_the_smallest_proposal_compiles_to_one_step(self) -> None:
        self.assertEqual(
            _compile(self.SMALLEST),
            _hand_built("system", "current_time", {}),
        )

    def test_json_whitespace_around_one_document_is_part_of_it(self) -> None:
        for text in (
            " " + self.SMALLEST,
            self.SMALLEST + "\n",
            "\n\t " + self.SMALLEST + " \r\n",
        ):
            with self.subTest(text=ascii(text[:4])):
                self.assertEqual(
                    _compile(text),
                    _hand_built("system", "current_time", {}),
                )

    def test_the_arguments_object_is_required_even_when_empty(self) -> None:
        text = json.dumps({"tool": "system", "action": "current_time"})

        self.assertIs(_compile(text), ProposalRejection.MALFORMED_PROPOSAL)

    def test_top_level_fields_must_be_exactly_the_three(self) -> None:
        base = {"tool": "system", "action": "current_time", "arguments": {}}
        variants = [
            {key: value for key, value in base.items() if key != "tool"},
            {key: value for key, value in base.items() if key != "action"},
            {**base, "approved": True},
            {**base, "confirmed": "yes"},
            {**base, "permissions": ["filesystem.write"]},
            {**base, "steps": []},
            {**base, "original_request": "Delete everything."},
        ]
        for variant in variants:
            with self.subTest(fields=sorted(variant)):
                self.assertIs(
                    _compile(json.dumps(variant)),
                    ProposalRejection.MALFORMED_PROPOSAL,
                )

        self.assertEqual(
            _compile(json.dumps(base)),
            _hand_built("system", "current_time", {}),
        )

    def test_tool_action_and_arguments_must_have_exact_types(self) -> None:
        for wrong in (7, None, True, ["calculator"], {"name": "calculator"}):
            with self.subTest(field="tool", value=ascii(wrong)):
                self.assertIs(
                    _compile(_proposal(wrong, "calculate", {"expression": "1"})),
                    ProposalRejection.MALFORMED_PROPOSAL,
                )
            with self.subTest(field="action", value=ascii(wrong)):
                self.assertIs(
                    _compile(_proposal("calculator", wrong, {"expression": "1"})),
                    ProposalRejection.MALFORMED_PROPOSAL,
                )
        for wrong in ([], ["expression"], "expression", None, 7):
            with self.subTest(field="arguments", value=ascii(wrong)):
                self.assertIs(
                    _compile(_proposal("calculator", "calculate", wrong)),
                    ProposalRejection.MALFORMED_PROPOSAL,
                )

    def test_a_document_that_is_not_one_object_is_malformed(self) -> None:
        for text in (
            "{}",
            "[]",
            "[" + self.SMALLEST + "]",
            "[" + self.SMALLEST + "," + self.SMALLEST + "]",
            "null",
            "7",
            '"text"',
            "true",
        ):
            with self.subTest(text=text[:12]):
                self.assertIs(
                    _compile(text),
                    ProposalRejection.MALFORMED_PROPOSAL,
                )

    def test_more_than_one_document_is_refused(self) -> None:
        for text in (
            self.SMALLEST + self.SMALLEST,
            self.SMALLEST + "\n" + self.SMALLEST,
            self.SMALLEST + " {}",
            self.SMALLEST + " 7",
        ):
            with self.subTest(text=ascii(text[-6:])):
                self.assertIs(_compile(text), ProposalRejection.MALFORMED_OUTPUT)

    def test_prose_fences_and_near_json_are_refused_never_repaired(
        self,
    ) -> None:
        for text in (
            "Here is the action: " + self.SMALLEST,
            self.SMALLEST + " Done.",
            "```json\n" + self.SMALLEST + "\n```",
            "```\n" + self.SMALLEST + "\n```",
            "`" + self.SMALLEST + "`",
            "json " + self.SMALLEST,
            chr(0xFEFF) + self.SMALLEST,
            "// the action\n" + self.SMALLEST,
            str({"tool": "system", "action": "current_time", "arguments": {}}),
            self.SMALLEST[:-1] + ",}",
            '{tool:"system",action:"current_time",arguments:{}}',
            "",
        ):
            with self.subTest(text=ascii(text[:12])):
                outcome = compile_proposal(
                    REQUEST,
                    # An empty reply cannot be a response, so it arrives as
                    # one changed after construction.
                    _tampered(text) if not text else ReasoningResponse(text),
                    logger=Mock(spec=logging.Logger),
                )

                self.assertIs(outcome, ProposalRejection.MALFORMED_OUTPUT)

    def test_truncated_text_is_refused_at_every_cut(self) -> None:
        text = _proposal("calculator", "calculate", {"expression": "2 + 2"})
        for end in range(1, len(text)):
            with self.subTest(end=end):
                self.assertIs(
                    _compile(text[:end]),
                    ProposalRejection.MALFORMED_OUTPUT,
                )

        self.assertIsInstance(_compile(text), Plan)

    def test_a_repeated_key_is_refused_at_every_level(self) -> None:
        # Python's parser would keep the last value, turning the first text
        # into a valid proposal for a different tool.
        repeated_tool = (
            '{"tool":"filesystem","tool":"system",'
            '"action":"current_time","arguments":{}}'
        )
        repeated_argument = (
            '{"tool":"calculator","action":"calculate",'
            '"arguments":{"expression":"1","expression":"2"}}'
        )
        self.assertEqual(json.loads(repeated_tool)["tool"], "system")

        for text in (repeated_tool, repeated_argument):
            with self.subTest(text=text[:24]):
                self.assertIs(_compile(text), ProposalRejection.MALFORMED_OUTPUT)

    def test_nan_and_infinity_are_refused(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            for text in (
                '{"tool":"calculator","action":"calculate",'
                '"arguments":{"expression":' + constant + "}}",
                '{"tool":' + constant + ',"action":"calculate",'
                '"arguments":{}}',
            ):
                with self.subTest(constant=constant, text=text[:10]):
                    self.assertIs(
                        _compile(text),
                        ProposalRejection.MALFORMED_OUTPUT,
                    )

        quoted = _proposal("calculator", "calculate", {"expression": "NaN"})
        self.assertEqual(
            _compile(quoted),
            _hand_built("calculator", "calculate", {"expression": "NaN"}),
        )

    def test_nesting_beyond_the_limit_is_refused_before_parsing(self) -> None:
        def nested(extra: int) -> str:
            return (
                '{"tool":"calculator","action":"calculate",'
                '"arguments":{"expression":'
                + "[" * extra
                + '"x"'
                + "]" * extra
                + "}}"
            )

        self.assertEqual(MODEL_PROPOSAL_MAX_NESTING, 8)
        with patch.object(
            plan_compiler_module.json,
            "loads",
            wraps=json.loads,
        ) as loads:
            too_deep = _compile(nested(MODEL_PROPOSAL_MAX_NESTING - 1))
            # Within the byte cap, so only the nesting check can refuse it.
            pathological = _compile("[" * 2000 + "]" * 2000)
            loads.assert_not_called()
            at_limit = _compile(nested(MODEL_PROPOSAL_MAX_NESTING - 2))

        self.assertIs(too_deep, ProposalRejection.MALFORMED_OUTPUT)
        self.assertIs(pathological, ProposalRejection.MALFORMED_OUTPUT)
        # At the limit the text is parsed, and the list is simply the wrong
        # type for the expression.
        self.assertIs(at_limit, ProposalRejection.WRONG_TYPE)

    def test_brackets_inside_strings_are_not_nesting(self) -> None:
        expression = '"[{' * 12 + BACKSLASH + '"}]' * 3
        text = _proposal("calculator", "calculate", {"expression": expression})

        self.assertEqual(
            _compile(text),
            _hand_built("calculator", "calculate", {"expression": expression}),
        )

    def test_a_closing_bracket_with_nothing_open_is_malformed(self) -> None:
        for text in ("]" + self.SMALLEST, "}", self.SMALLEST + "]"):
            with self.subTest(text=ascii(text[:3])):
                self.assertIs(_compile(text), ProposalRejection.MALFORMED_OUTPUT)


class ByteCapTests(unittest.TestCase):
    """Prove the byte cap is exact, measured in UTF-8, and applied first."""

    def test_the_cap_is_the_approved_size(self) -> None:
        self.assertEqual(MODEL_PROPOSAL_MAX_BYTES, 4096)

    def test_a_proposal_at_the_cap_compiles_and_one_byte_over_is_refused(
        self,
    ) -> None:
        source = _proposal("calculator", "calculate", {"expression": "6 * 7"})
        expected = _hand_built("calculator", "calculate", {"expression": "6 * 7"})

        self.assertEqual(_compile(source), expected)
        self.assertEqual(
            _compile(_padded(source, MODEL_PROPOSAL_MAX_BYTES)),
            expected,
        )
        self.assertIs(
            _compile(_padded(source, MODEL_PROPOSAL_MAX_BYTES + 1)),
            ProposalRejection.OVERSIZED,
        )

    def test_the_cap_counts_utf8_bytes_not_characters(self) -> None:
        expression = chr(0xE9) * 100
        source = json.dumps(
            {
                "tool": "calculator",
                "action": "calculate",
                "arguments": {"expression": expression},
            },
            ensure_ascii=False,
        )
        # Each of the hundred characters takes two bytes.
        fitting = _padded(source, MODEL_PROPOSAL_MAX_BYTES - 100)
        over = _padded(source, MODEL_PROPOSAL_MAX_BYTES - 99)

        self.assertEqual(len(over.encode("utf-8")), MODEL_PROPOSAL_MAX_BYTES + 1)
        self.assertLess(len(over), MODEL_PROPOSAL_MAX_BYTES)
        self.assertEqual(
            _compile(fitting),
            _hand_built("calculator", "calculate", {"expression": expression}),
        )
        self.assertIs(_compile(over), ProposalRejection.OVERSIZED)

    def test_an_oversized_proposal_is_never_parsed(self) -> None:
        with patch.object(
            plan_compiler_module.json,
            "loads",
            wraps=json.loads,
        ) as loads:
            outcome = _compile(hostile_proposal(HostileProposal.OVER_BYTE_CAP))

        self.assertIs(outcome, ProposalRejection.OVERSIZED)
        loads.assert_not_called()

    def test_an_oversized_reply_is_refused_without_being_copied(self) -> None:
        response = ReasoningResponse("x" * (1024 * 1024))
        logger = Mock(spec=logging.Logger)

        outcome, peak = peak_allocation(
            lambda: compile_proposal(REQUEST, response, logger=logger)
        )

        self.assertIs(outcome, ProposalRejection.OVERSIZED)
        self.assertLess(peak, 64 * 1024)

    def test_a_reply_changed_after_construction_is_read_again(self) -> None:
        huge = "x" * (8 * 1024 * 1024)
        cases = (
            (huge, ProposalRejection.OVERSIZED),
            (7, ProposalRejection.MALFORMED_OUTPUT),
            (None, ProposalRejection.MALFORMED_OUTPUT),
            ("", ProposalRejection.MALFORMED_OUTPUT),
            (chr(0xD800), ProposalRejection.MALFORMED_OUTPUT),
        )
        logger = Mock(spec=logging.Logger)
        for text, expected in cases:
            with self.subTest(text=type(text).__name__):
                response = _tampered(text)

                outcome, peak = peak_allocation(
                    lambda: compile_proposal(REQUEST, response, logger=logger)
                )

                self.assertIs(outcome, expected)
                self.assertLess(peak, 64 * 1024)


class HostileFakeTests(unittest.TestCase):
    """Prove every category the fake emits meets its exact outcome."""

    SETTINGS = enabled_settings(max_output_bytes=2 * MODEL_PROPOSAL_MAX_BYTES)

    def _through_the_boundary(
        self,
        provider: AdversarialReasoningProvider,
    ) -> Plan | ProposalRejection:
        """Ask the fake through the reasoning boundary, then compile."""
        reply = request_reasoning(
            provider,
            ReasoningRequest(REQUEST),
            self.SETTINGS,
            logger=Mock(spec=logging.Logger),
        )
        # The reasoning boundary hands every one of these back as inert text.
        self.assertIs(type(reply), ReasoningResponse)
        return compile_proposal(REQUEST, reply, logger=Mock(spec=logging.Logger))

    def test_every_v1_12_category_meets_its_expected_refusal(self) -> None:
        self.assertEqual(set(EXPECTED_V1_12_OUTCOMES), set(HostileOutput))
        for output, expected in EXPECTED_V1_12_OUTCOMES.items():
            with self.subTest(output=output.value):
                limit = (
                    MODEL_PROPOSAL_MAX_BYTES
                    if output is HostileOutput.OVERSIZED_OUTPUT
                    else None
                )
                provider = AdversarialReasoningProvider.emitting(output, limit)

                self.assertIs(self._through_the_boundary(provider), expected)
                self.assertEqual(provider.calls, 1)

    def test_the_injected_prose_is_still_refused(self) -> None:
        provider = AdversarialReasoningProvider.emitting(
            HostileOutput.INJECTED_INSTRUCTION
        )

        self.assertIs(
            self._through_the_boundary(provider),
            ProposalRejection.MALFORMED_OUTPUT,
        )

    def test_every_compiler_focused_case_meets_its_expected_outcome(
        self,
    ) -> None:
        self.assertEqual(set(EXPECTED_PROPOSAL_OUTCOMES), set(HostileProposal))
        for proposal, expected in EXPECTED_PROPOSAL_OUTCOMES.items():
            with self.subTest(proposal=proposal.value):
                outcome = self._through_the_boundary(
                    AdversarialReasoningProvider.proposing(proposal)
                )

                if expected is None:
                    self.assertIsInstance(outcome, Plan)
                else:
                    self.assertIs(outcome, expected)

    def test_each_case_is_hostile_only_in_its_named_way(self) -> None:
        length = json.loads(hostile_proposal(HostileProposal.CONTRACT_LENGTH))
        self.assertEqual(
            len(length["arguments"]["expression"]),
            MAXIMUM_EXPRESSION_LENGTH + 1,
        )

        padded = hostile_proposal(HostileProposal.OVER_BYTE_CAP)
        self.assertEqual(len(padded.encode("utf-8")), MODEL_PROPOSAL_MAX_BYTES + 1)
        self.assertEqual(json.loads(padded), json.loads(OVER_BYTE_CAP_SOURCE))
        self.assertIsInstance(_compile(OVER_BYTE_CAP_SOURCE), Plan)

        closed = json.loads(hostile_proposal(HostileProposal.NOT_MODEL_ELIGIBLE))
        self.assertIsNone(
            BUILT_IN_ACTION_CONTRACTS.validate(
                closed["tool"],
                closed["action"],
                closed["arguments"],
            )
        )

        injected = json.loads(hostile_proposal(HostileProposal.INJECTED_FIELDS))
        del injected["approved"], injected["instruction"]
        self.assertIsInstance(_compile(json.dumps(injected)), Plan)

        for proposal in HostileProposal:
            with self.subTest(proposal=proposal.value):
                self.assertIn(FAKE_PAYLOAD_SENTINEL, hostile_proposal(proposal))
        for proposal in (
            HostileProposal.MISSING_ARGUMENT,
            HostileProposal.EXTRA_NAMED_ARGUMENT,
            HostileProposal.UNKNOWN_NAMED_TOOL,
            HostileProposal.UNKNOWN_NAMED_ACTION,
        ):
            with self.subTest(named=proposal.value):
                self.assertIn(FAKE_NAME_SENTINEL, hostile_proposal(proposal))

    def test_hostile_proposal_refuses_an_unknown_case(self) -> None:
        with self.assertRaises(ValueError):
            hostile_proposal("contract_length")


class ContractGateTests(unittest.TestCase):
    """Prove argument contracts refuse on actions the model may propose."""

    def assertOpen(self, identifier: str) -> None:
        """Assert the action is allowlisted, so its contract must answer."""
        self.assertIn(identifier, MODEL_ACTION_ALLOWLIST)

    def test_an_expression_past_its_declared_length_is_out_of_range(
        self,
    ) -> None:
        self.assertOpen("calculator.calculate")
        longest = "1" * MAXIMUM_EXPRESSION_LENGTH

        self.assertIs(
            _compile(
                _proposal("calculator", "calculate", {"expression": longest + "1"})
            ),
            ProposalRejection.OUT_OF_RANGE,
        )
        for expression in (longest, "  " + longest + "  "):
            with self.subTest(length=len(expression)):
                self.assertEqual(
                    _compile(
                        _proposal(
                            "calculator",
                            "calculate",
                            {"expression": expression},
                        )
                    ),
                    _hand_built(
                        "calculator",
                        "calculate",
                        {"expression": expression},
                    ),
                )

    def test_blank_text_is_out_of_range(self) -> None:
        for identifier, field in (
            ("calculator.calculate", "expression"),
            ("filesystem.list", "path"),
        ):
            self.assertOpen(identifier)
            for blank in ("", "   "):
                with self.subTest(action=identifier, blank=len(blank)):
                    self.assertIs(
                        _compile(_proposal(*_split(identifier), {field: blank})),
                        ProposalRejection.OUT_OF_RANGE,
                    )

    def test_a_missing_required_argument_is_refused(self) -> None:
        self.assertOpen("calculator.calculate")

        self.assertIs(
            _compile(_proposal("calculator", "calculate", {})),
            ProposalRejection.MISSING_ARGUMENT,
        )
        self.assertIsInstance(
            _compile(_proposal("calculator", "calculate", {"expression": "1"})),
            Plan,
        )

    def test_a_wrong_argument_type_is_refused(self) -> None:
        wrong_values = (7, 10**30, 1.5, True, False, None, ["1"], {"value": "1"})
        for identifier, field, valid in (
            ("calculator.calculate", "expression", "1 + 1"),
            ("filesystem.list", "path", "docs"),
        ):
            self.assertOpen(identifier)
            for wrong in wrong_values:
                with self.subTest(action=identifier, value=ascii(wrong)):
                    self.assertIs(
                        _compile(_proposal(*_split(identifier), {field: wrong})),
                        ProposalRejection.WRONG_TYPE,
                    )
            self.assertIsInstance(
                _compile(_proposal(*_split(identifier), {field: valid})),
                Plan,
            )

        overflowing = (
            '{"tool":"calculator","action":"calculate",'
            '"arguments":{"expression":1e999}}'
        )
        self.assertIs(_compile(overflowing), ProposalRejection.WRONG_TYPE)

    def test_an_extra_argument_is_refused(self) -> None:
        cases = (
            ("calculator.calculate", {"expression": "1", "comment": "hi"}),
            ("system.current_time", {"format": "iso"}),
            ("filesystem.list", {"path": "docs", "recursive": True}),
            ("filesystem.list", {"target": "docs"}),
        )
        for identifier, arguments in cases:
            self.assertOpen(identifier)
            with self.subTest(action=identifier, fields=sorted(arguments)):
                self.assertIs(
                    _compile(_proposal(*_split(identifier), arguments)),
                    ProposalRejection.EXTRA_ARGUMENT,
                )

        self.assertIsInstance(
            _compile(_proposal("system", "current_time", {})),
            Plan,
        )

    def test_an_absent_optional_path_is_accepted_as_absent(self) -> None:
        self.assertOpen("filesystem.list")

        self.assertEqual(
            _compile(_proposal("filesystem", "list", {})),
            _hand_built("filesystem", "list", {}),
        )

    def test_the_first_defect_follows_the_contract_order(self) -> None:
        self.assertIs(
            _compile(_proposal("calculator", "calculate", {"extra": "x"})),
            ProposalRejection.MISSING_ARGUMENT,
        )
        self.assertIs(
            _compile(
                _proposal(
                    "calculator",
                    "calculate",
                    {"expression": 7, "extra": "x"},
                )
            ),
            ProposalRejection.EXTRA_ARGUMENT,
        )

    def test_a_refused_argument_never_reaches_plan_construction(self) -> None:
        # PlanArguments names an unsupported key in its own error, so no
        # refused proposal may ever reach it.
        cases = (
            (
                _proposal(
                    "calculator",
                    "calculate",
                    {"expression": "1", FAKE_NAME_SENTINEL: [1, {"a": 2}]},
                ),
                ProposalRejection.EXTRA_ARGUMENT,
            ),
            (
                _proposal(
                    "calculator",
                    "calculate",
                    {"expression": [FAKE_PAYLOAD_SENTINEL]},
                ),
                ProposalRejection.WRONG_TYPE,
            ),
            (
                _proposal("filesystem", "list", {"path": 1.5}),
                ProposalRejection.WRONG_TYPE,
            ),
        )
        with (
            patch.object(
                plan_compiler_module,
                "PlanStep",
                side_effect=AssertionError("a step was built"),
            ) as step,
            patch.object(
                plan_compiler_module,
                "Plan",
                side_effect=AssertionError("a plan was built"),
            ) as plan,
        ):
            outcomes = [_compile(text) for text, _ in cases]

        self.assertEqual(outcomes, [expected for _, expected in cases])
        step.assert_not_called()
        plan.assert_not_called()


class UnsafeTextTests(unittest.TestCase):
    """Prove unsafe characters refuse a value and are never stripped away."""

    def test_the_refused_categories_are_exactly_the_approved_ones(self) -> None:
        self.assertEqual(
            UNSAFE_TEXT_CATEGORIES,
            frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"}),
        )

    def test_every_unsafe_character_refuses_an_expression_anywhere(
        self,
    ) -> None:
        for name, character in UNSAFE_CHARACTERS:
            for expression in (
                "1 + 1" + character,
                character + "1 + 1",
                "1 +" + character + " 1",
            ):
                position = expression.index(character)
                with self.subTest(character=name, position=position):
                    self.assertIs(
                        _compile(
                            _proposal(
                                "calculator",
                                "calculate",
                                {"expression": expression},
                            )
                        ),
                        ProposalRejection.UNSAFE_TEXT,
                    )

        # Without the character the same value compiles, so nothing was
        # refused for any other reason, and nothing was stripped to accept.
        self.assertIsInstance(
            _compile(_proposal("calculator", "calculate", {"expression": "1 + 1"})),
            Plan,
        )

    def test_every_unsafe_character_refuses_a_path(self) -> None:
        for name, character in UNSAFE_CHARACTERS:
            with self.subTest(character=name):
                self.assertIs(
                    _compile(
                        _proposal("filesystem", "list", {"path": "docs" + character})
                    ),
                    ProposalRejection.UNSAFE_TEXT,
                )

    def test_unsafe_text_is_found_before_an_unsafe_path(self) -> None:
        self.assertIs(
            _compile(
                _proposal("filesystem", "list", {"path": "../x" + chr(0x1B)})
            ),
            ProposalRejection.UNSAFE_TEXT,
        )

    def test_a_raw_control_character_is_not_even_json(self) -> None:
        raw_escape = json.dumps(
            {
                "tool": "calculator",
                "action": "calculate",
                "arguments": {"expression": "1"},
            }
        ).replace('"1"', '"1' + chr(0x1B) + '"')
        raw_override = json.dumps(
            {
                "tool": "calculator",
                "action": "calculate",
                "arguments": {"expression": "1" + chr(0x202E)},
            },
            ensure_ascii=False,
        )

        self.assertIn(chr(0x1B), raw_escape)
        self.assertIs(_compile(raw_escape), ProposalRejection.MALFORMED_OUTPUT)
        self.assertIn(chr(0x202E), raw_override)
        self.assertIs(_compile(raw_override), ProposalRejection.UNSAFE_TEXT)

    def test_ordinary_non_ascii_text_is_accepted_unchanged(self) -> None:
        for identifier, field, value in (
            ("filesystem.list", "path", "Caf" + chr(0xE9) + " notes"),
            ("filesystem.list", "path", chr(0x8CC7) + chr(0x6599)),
            ("calculator.calculate", "expression", "1" + chr(0xA0) + "+ 1"),
        ):
            for ensure_ascii in (True, False):
                text = json.dumps(
                    {
                        "tool": _split(identifier)[0],
                        "action": _split(identifier)[1],
                        "arguments": {field: value},
                    },
                    ensure_ascii=ensure_ascii,
                )
                with self.subTest(value=ascii(value), escaped=ensure_ascii):
                    self.assertEqual(
                        _compile(text),
                        _hand_built(*_split(identifier), {field: value}),
                    )


class PathRuleTests(unittest.TestCase):
    """Prove the lexical path rule, which reads text and touches nothing."""

    def test_every_unsafe_path_is_refused(self) -> None:
        self.assertIn("filesystem.list", MODEL_ACTION_ALLOWLIST)
        for path in UNSAFE_PATHS:
            with self.subTest(path=ascii(path)):
                self.assertIs(
                    _compile(_proposal("filesystem", "list", {"path": path})),
                    ProposalRejection.UNSAFE_PATH,
                )

    def test_a_nul_in_a_path_is_refused_as_unsafe_text(self) -> None:
        self.assertIs(
            _compile(
                _proposal("filesystem", "list", {"path": "docs" + chr(0) + "x"})
            ),
            ProposalRejection.UNSAFE_TEXT,
        )

    def test_every_safe_path_compiles_with_its_text_unchanged(self) -> None:
        for path in SAFE_PATHS:
            with self.subTest(path=ascii(path)):
                self.assertEqual(
                    _compile(_proposal("filesystem", "list", {"path": path})),
                    _hand_built("filesystem", "list", {"path": path}),
                )

    def test_the_rule_touches_no_filesystem_api(self) -> None:
        responses = [
            ReasoningResponse(_proposal("filesystem", "list", {"path": path}))
            for path in (*UNSAFE_PATHS, *SAFE_PATHS)
        ]
        logger = Mock(spec=logging.Logger)
        with _tripwires() as mocks:
            outcomes = [
                compile_proposal(REQUEST, response, logger=logger)
                for response in responses
            ]

        self.assertEqual(len(outcomes), len(UNSAFE_PATHS) + len(SAFE_PATHS))
        for name, mock in mocks.items():
            with self.subTest(tripwire=name):
                mock.assert_not_called()

    def test_the_rule_applies_only_to_declared_path_fields(self) -> None:
        expression = "../../" + "C:" + BACKSLASH + "CON"

        self.assertEqual(
            _compile(
                _proposal("calculator", "calculate", {"expression": expression})
            ),
            _hand_built("calculator", "calculate", {"expression": expression}),
        )

    def test_instruction_like_path_text_is_inert_data(self) -> None:
        text = hostile_proposal(HostileProposal.INERT_INSTRUCTION_PATH)
        path = json.loads(text)["arguments"]["path"]

        outcome = _compile(text)

        self.assertEqual(outcome, _hand_built("filesystem", "list", {"path": path}))
        self.assertEqual(len(outcome.steps), 1)
        self.assertEqual(
            (outcome.steps[0].tool_name, outcome.steps[0].action),
            ("filesystem", "list"),
        )
        self.assertIs(outcome.recording, RecordingPolicy.TRANSIENT)
        self.assertIs(outcome.origin, PlanOrigin.MODEL)
        self.assertEqual(MODEL_ACTION_ALLOWLIST, EXPECTED_ALLOWLIST)


class GateOrderTests(unittest.TestCase):
    """Prove the first failing gate decides the refusal."""

    def test_each_earlier_gate_answers_before_a_later_one(self) -> None:
        cases = (
            # Too large and not even JSON: the cap answers.
            (
                _padded(
                    '{"tool":"memory","action":"store","arguments":',
                    MODEL_PROPOSAL_MAX_BYTES + 1,
                ),
                ProposalRejection.OVERSIZED,
            ),
            # Not strict JSON and naming a closed action: parsing answers.
            (
                '{"tool":"memory","tool":"memory","action":"store",'
                '"arguments":{"fact":"x"}}',
                ProposalRejection.MALFORMED_OUTPUT,
            ),
            # Wrongly shaped and naming a closed action: the shape answers.
            (
                _proposal("memory", "store", {"fact": "x"}, approved=True),
                ProposalRejection.MALFORMED_PROPOSAL,
            ),
            # Closed and out of range: the allowlist answers.
            (
                _proposal("memory", "update", {"id": 0, "fact": "x"}),
                ProposalRejection.NOT_MODEL_ELIGIBLE,
            ),
            # An extra field and an unsafe path: the contract answers.
            (
                _proposal("filesystem", "list", {"path": "../x", "extra": "1"}),
                ProposalRejection.EXTRA_ARGUMENT,
            ),
            # Unsafe text in an unsafe path: the text check answers.
            (
                _proposal("filesystem", "list", {"path": "../x" + chr(0x1B)}),
                ProposalRejection.UNSAFE_TEXT,
            ),
        )
        for text, expected in cases:
            with self.subTest(expected=expected.value):
                self.assertIs(_compile(text), expected)


class NoExecutionTests(unittest.TestCase):
    """Prove compiling, accepted or refused, executes and decides nothing."""

    def test_no_accepted_or_refused_compilation_reaches_anything(self) -> None:
        texts = _fake_texts()
        texts += [
            _proposal(*_split(identifier), arguments)
            for identifier, arguments in VALID_ARGUMENTS.items()
        ]
        texts += [
            _proposal("filesystem", "list", {"path": path})
            for path in UNSAFE_PATHS[:5]
        ]
        responses = [ReasoningResponse(text) for text in texts]
        logger = Mock(spec=logging.Logger)

        with _tripwires() as mocks:
            outcomes = [
                compile_proposal(REQUEST, response, logger=logger)
                for response in responses
            ]

        accepted = [outcome for outcome in outcomes if type(outcome) is Plan]
        refused = [
            outcome
            for outcome in outcomes
            if type(outcome) is ProposalRejection
        ]
        # Positive controls: both outcomes were exercised under the wires.
        self.assertEqual(len(accepted) + len(refused), len(outcomes))
        self.assertGreaterEqual(len(accepted), 4)
        self.assertGreaterEqual(len(refused), 30)
        for name, mock in mocks.items():
            with self.subTest(tripwire=name):
                mock.assert_not_called()

    def test_the_tripwires_are_live(self) -> None:
        """Positive control, so the check above cannot pass by seeing nothing."""
        with _tripwires() as mocks:
            with self.assertRaises(AssertionError):
                PermissionManager(frozenset(), frozenset()).check(
                    "system",
                    "current_time",
                )
            with self.assertRaises(AssertionError):
                Path(".").resolve()

        mocks["PermissionManager.check"].assert_called_once()

    def test_every_built_in_tool_is_wired(self) -> None:
        """Positive control, so no built-in tool can run without a tripwire."""
        wired = {
            owner.__module__
            for owner, name in _TRIPWIRE_TARGETS
            if name == "execute"
        }

        self.assertEqual(wired, set(BUILT_IN_TOOL_MODULES))
        self.assertIn("tools.audit", wired)


class CompiledPlanTests(unittest.TestCase):
    """Prove a compiled plan is the existing immutable, fingerprinted Plan."""

    CASES = (
        ("calculator", "calculate", {"expression": "6 * 7"}),
        ("calculator", "calculate", {"expression": "  2 ** 8  "}),
        ("filesystem", "list", {"path": "Docs" + BACKSLASH + "Reports"}),
        ("filesystem", "list", {}),
        ("system", "current_time", {}),
    )

    def test_every_compiled_plan_equals_its_hand_built_plan(self) -> None:
        for tool_name, action, arguments in self.CASES:
            with self.subTest(action=f"{tool_name}.{action}"):
                compiled = _compile(_proposal(tool_name, action, arguments))
                expected = _hand_built(tool_name, action, arguments)

                self.assertIs(type(compiled), Plan)
                self.assertEqual(compiled, expected)
                self.assertEqual(compiled.fingerprint, expected.fingerprint)
                self.assertEqual(hash(compiled), hash(expected))
                self.assertIs(type(compiled.steps), tuple)
                self.assertEqual(len(compiled.steps), 1)
                self.assertIs(type(compiled.steps[0]), PlanStep)
                self.assertIs(type(compiled.steps[0].arguments), PlanArguments)
                self.assertEqual(dict(compiled.steps[0].arguments), arguments)
                self.assertIsNone(compiled.response_if_no_action)
                self.assertIs(compiled.recording, RecordingPolicy.TRANSIENT)
                self.assertIs(compiled.origin, PlanOrigin.MODEL)
                self.assertTrue(is_model_plan(compiled))
                self.assertEqual(compiled.original_request, REQUEST)

    def test_the_fingerprint_binds_request_arguments_and_transience(
        self,
    ) -> None:
        text = _proposal("calculator", "calculate", {"expression": "6 * 7"})
        first = _compile(text)

        self.assertEqual(first.fingerprint, _compile(text).fingerprint)
        self.assertRegex(first.fingerprint, r"^[0-9a-f]{64}$")
        self.assertNotEqual(
            first.fingerprint,
            _compile(text, request="Please help with that.").fingerprint,
        )
        self.assertNotEqual(
            first.fingerprint,
            _compile(text.replace("6 * 7", "6 * 8")).fingerprint,
        )

        # A model-originated plan cannot be made a recorded one at all.
        with self.assertRaises(ValueError) as caught:
            replace(first, recording=RecordingPolicy.RECORD)
        self.assertEqual(str(caught.exception), MODEL_PLAN_SHAPE_MESSAGE)

        # Its deterministic twins are plans Maria's own code could build, and
        # none shares a fingerprint with it or with the other, so both the
        # origin and the transience are bound, and an approval of one can
        # never be spent on another.
        transient_twin = replace(first, origin=PlanOrigin.DETERMINISTIC)
        recorded_twin = replace(
            transient_twin,
            recording=RecordingPolicy.RECORD,
        )
        self.assertNotEqual(first.fingerprint, transient_twin.fingerprint)
        self.assertNotEqual(first.fingerprint, recorded_twin.fingerprint)
        self.assertNotEqual(
            transient_twin.fingerprint,
            recorded_twin.fingerprint,
        )

    def test_a_compiled_plan_cannot_be_changed(self) -> None:
        plan = _compile(_proposal("filesystem", "list", {"path": "docs"}))
        step = plan.steps[0]

        with self.assertRaises(TypeError):
            step.arguments["path"] = "../outside"
        with self.assertRaises(TypeError):
            del step.arguments["path"]
        with self.assertRaises(AttributeError):
            step.arguments.extra = "x"
        with self.assertRaises(FrozenInstanceError):
            step.tool_name = "memory"
        with self.assertRaises(FrozenInstanceError):
            plan.steps = ()
        with self.assertRaises(FrozenInstanceError):
            plan.recording = RecordingPolicy.RECORD
        with self.assertRaises(FrozenInstanceError):
            plan.origin = PlanOrigin.DETERMINISTIC

        self.assertIs(plan.origin, PlanOrigin.MODEL)
        self.assertEqual(plan, _hand_built("filesystem", "list", {"path": "docs"}))

    def test_the_step_names_the_declared_tool_and_action(self) -> None:
        plan = _compile(_proposal("calculator", "calculate", {"expression": "1"}))
        contract = BUILT_IN_ACTION_CONTRACTS.contract_for("calculator", "calculate")
        step = plan.steps[0]

        self.assertEqual(
            (step.tool_name, step.action),
            (contract.tool_name, contract.action),
        )
        self.assertEqual((step.tool_name, step.action), ("calculator", "calculate"))
        self.assertIs(type(step.tool_name), str)
        self.assertIs(type(step.action), str)

    def test_the_owner_request_is_the_plan_request(self) -> None:
        plan = _compile(
            _proposal("system", "current_time", {}),
            request=REQUEST_SENTINEL,
        )

        self.assertEqual(plan.original_request, REQUEST_SENTINEL)


class ModelPlanRecheckTests(unittest.TestCase):
    """Prove a compiled plan is checked again exactly as it stands."""

    def test_a_valid_compiled_plan_passes_again_unchanged(self) -> None:
        logger = Mock(spec=logging.Logger)
        for tool_name, action, arguments in CompiledPlanTests.CASES:
            with self.subTest(action=f"{tool_name}.{action}"):
                plan = _compile(_proposal(tool_name, action, arguments))
                fingerprint = plan.fingerprint

                self.assertIsNone(
                    recheck_model_plan(
                        plan,
                        BUILT_IN_ACTION_CONTRACTS,
                        logger=logger,
                    )
                )
                self.assertEqual(plan, _hand_built(tool_name, action, arguments))
                self.assertEqual(plan.fingerprint, fingerprint)

        self.assertEqual(logger.mock_calls, [])

    def test_a_plan_its_registry_no_longer_opens_is_refused(self) -> None:
        for identifier in sorted(MODEL_ACTION_ALLOWLIST):
            plan = _compile(
                _proposal(*_split(identifier), VALID_ARGUMENTS[identifier])
            )
            closed = _registry_with(
                identifier,
                ModelEligibility.NOT_MODEL_ELIGIBLE,
            )
            with self.subTest(action=identifier):
                self.assertIsNone(_recheck(plan))
                self.assertIs(
                    _recheck(plan, closed),
                    ProposalRejection.NOT_MODEL_ELIGIBLE,
                )

        listing = _compile(_proposal("filesystem", "list", {"path": "docs"}))
        arithmetic = _compile(
            _proposal("calculator", "calculate", {"expression": "1"})
        )
        self.assertIs(
            _recheck(listing, _registry_without("filesystem.list")),
            ProposalRejection.UNKNOWN_ACTION,
        )
        self.assertIs(
            _recheck(arithmetic, _registry_without("calculator.calculate")),
            ProposalRejection.UNKNOWN_TOOL,
        )

    def test_a_model_plan_for_a_closed_action_is_refused(self) -> None:
        # The compiler never builds these, so each is built by hand, and the
        # allowlist refuses it even where the registry's flag was widened.
        closed = [
            identifier
            for identifier in VALID_ARGUMENTS
            if identifier not in MODEL_ACTION_ALLOWLIST
        ]
        self.assertIn("audit.delete", closed)
        for identifier in closed:
            plan = _hand_built(*_split(identifier), VALID_ARGUMENTS[identifier])
            widened = _registry_with(identifier, ModelEligibility.MODEL_ELIGIBLE)
            with self.subTest(action=identifier):
                self.assertIs(_recheck(plan), ProposalRejection.NOT_MODEL_ELIGIBLE)
                self.assertIs(
                    _recheck(plan, widened),
                    ProposalRejection.NOT_MODEL_ELIGIBLE,
                )

    def test_a_changed_contract_refuses_the_existing_step(self) -> None:
        cases = (
            (
                ("calculator", "calculate", {"expression": "6 * 7"}),
                _registry_declaring(
                    "calculator.calculate",
                    ArgumentContract(
                        "expression",
                        str,
                        required=True,
                        allow_blank=False,
                        maximum_length=3,
                    ),
                ),
                ProposalRejection.OUT_OF_RANGE,
            ),
            (
                ("calculator", "calculate", {"expression": "6 * 7"}),
                _registry_declaring(
                    "calculator.calculate",
                    ArgumentContract("expression", int, required=True),
                ),
                ProposalRejection.WRONG_TYPE,
            ),
            (
                ("filesystem", "list", {"path": "docs"}),
                _registry_declaring("filesystem.list"),
                ProposalRejection.EXTRA_ARGUMENT,
            ),
            (
                ("system", "current_time", {}),
                _registry_declaring(
                    "system.current_time",
                    ArgumentContract("zone", str, required=True, allow_blank=False),
                ),
                ProposalRejection.MISSING_ARGUMENT,
            ),
        )
        for (tool_name, action, arguments), changed, expected in cases:
            plan = _compile(_proposal(tool_name, action, arguments))
            with self.subTest(action=f"{tool_name}.{action}", code=expected.code):
                self.assertIsNone(_recheck(plan))
                self.assertIs(_recheck(plan, changed), expected)

    def test_unsafe_text_in_an_existing_step_is_refused(self) -> None:
        for name, character in UNSAFE_CHARACTERS:
            for identifier, field, value in (
                ("calculator.calculate", "expression", "1 +" + character + " 1"),
                ("filesystem.list", "path", "do" + character + "cs"),
            ):
                with self.subTest(character=name, action=identifier):
                    plan = _hand_built(*_split(identifier), {field: value})

                    self.assertIs(_recheck(plan), ProposalRejection.UNSAFE_TEXT)

        # Without the character each step passes, so nothing was refused for
        # any other reason.
        self.assertIsNone(
            _recheck(_hand_built("calculator", "calculate", {"expression": "1 + 1"}))
        )
        self.assertIsNone(
            _recheck(_hand_built("filesystem", "list", {"path": "docs"}))
        )

    def test_an_unsafe_path_in_an_existing_step_is_refused(self) -> None:
        for path in UNSAFE_PATHS:
            with self.subTest(path=ascii(path)):
                plan = _hand_built("filesystem", "list", {"path": path})

                self.assertIs(_recheck(plan), ProposalRejection.UNSAFE_PATH)
        for path in SAFE_PATHS:
            with self.subTest(safe=ascii(path)):
                plan = _hand_built("filesystem", "list", {"path": path})

                self.assertIsNone(_recheck(plan))

    def test_the_first_failing_gate_decides_a_check_again(self) -> None:
        longest = "1" * MAXIMUM_EXPRESSION_LENGTH
        cases = (
            # Closed and out of range: the allowlist answers.
            (
                ("memory", "update", {"id": 0, "fact": "x"}),
                ProposalRejection.NOT_MODEL_ELIGIBLE,
            ),
            # Closed and holding unsafe text: the allowlist answers.
            (
                ("memory", "store", {"fact": "x" + chr(0x1B)}),
                ProposalRejection.NOT_MODEL_ELIGIBLE,
            ),
            # Too long and holding unsafe text: the contract answers.
            (
                ("calculator", "calculate", {"expression": longest + chr(0x1B)}),
                ProposalRejection.OUT_OF_RANGE,
            ),
            # An extra field and an unsafe path: the contract answers.
            (
                ("filesystem", "list", {"path": "../x", "extra": "1"}),
                ProposalRejection.EXTRA_ARGUMENT,
            ),
            # Unsafe text in an unsafe path: the text check answers.
            (
                ("filesystem", "list", {"path": "../x" + chr(0x1B)}),
                ProposalRejection.UNSAFE_TEXT,
            ),
        )
        # The contract alone would refuse the first step too, so only the
        # order decides which gate answers.
        self.assertIs(
            BUILT_IN_ACTION_CONTRACTS.validate(*cases[0][0]).kind,
            ContractFailure.OUT_OF_RANGE,
        )
        for (tool_name, action, arguments), expected in cases:
            with self.subTest(action=f"{tool_name}.{action}", code=expected.code):
                self.assertIs(
                    _recheck(_hand_built(tool_name, action, arguments)),
                    expected,
                )
                # The same gates compiled it, so compiling refuses it alike.
                self.assertIs(
                    _compile(_proposal(tool_name, action, arguments)),
                    expected,
                )

    def test_a_step_changed_after_compiling_is_judged_as_it_now_stands(
        self,
    ) -> None:
        plan = _compile(_proposal("filesystem", "list", {"path": "docs"}))
        step = plan.steps[0]
        self.assertIsNone(_recheck(plan))

        # A frozen step can still be altered underneath, so nothing about it
        # is trusted from when it was compiled.
        object.__setattr__(step, "arguments", PlanArguments({"path": "../x"}))
        self.assertIs(_recheck(plan), ProposalRejection.UNSAFE_PATH)

        object.__setattr__(step, "arguments", PlanArguments({"path": "docs"}))
        object.__setattr__(step, "action", "read")
        self.assertIs(_recheck(plan), ProposalRejection.NOT_MODEL_ELIGIBLE)

    def test_checking_again_parses_builds_and_reaches_nothing(self) -> None:
        plans = [
            _compile(_proposal(*_split(identifier), VALID_ARGUMENTS[identifier]))
            for identifier in sorted(MODEL_ACTION_ALLOWLIST)
        ]
        plans += [
            _hand_built("filesystem", "list", {"path": path})
            for path in UNSAFE_PATHS[:5]
        ]
        plans += [
            _hand_built(*_split(identifier), arguments)
            for identifier, arguments in VALID_ARGUMENTS.items()
            if identifier not in MODEL_ACTION_ALLOWLIST
        ]
        fingerprints = [plan.fingerprint for plan in plans]
        logger = Mock(spec=logging.Logger)

        with (
            patch.object(
                plan_compiler_module.json,
                "loads",
                side_effect=AssertionError("a proposal was parsed"),
            ) as loads,
            patch.object(
                plan_compiler_module,
                "Plan",
                side_effect=AssertionError("a plan was built"),
            ) as plan_class,
            patch.object(
                plan_compiler_module,
                "PlanStep",
                side_effect=AssertionError("a step was built"),
            ) as step_class,
            _tripwires() as mocks,
        ):
            outcomes = [
                recheck_model_plan(plan, BUILT_IN_ACTION_CONTRACTS, logger=logger)
                for plan in plans
            ]

        # Positive controls: plans passed and plans were refused, under the
        # wires, and each plan was left exactly as it was.
        self.assertEqual(
            outcomes,
            [None] * len(MODEL_ACTION_ALLOWLIST)
            + [ProposalRejection.UNSAFE_PATH] * 5
            + [ProposalRejection.NOT_MODEL_ELIGIBLE]
            * (len(VALID_ARGUMENTS) - len(MODEL_ACTION_ALLOWLIST)),
        )
        self.assertEqual([plan.fingerprint for plan in plans], fingerprints)
        mocks.update({"json.loads": loads, "Plan": plan_class, "PlanStep": step_class})
        for name, mock in mocks.items():
            with self.subTest(tripwire=name):
                mock.assert_not_called()

    def test_a_refusal_logs_only_its_code(self) -> None:
        plan = _hand_built(
            "filesystem",
            "list",
            {"path": "../" + FAKE_PAYLOAD_SENTINEL},
            request=REQUEST_SENTINEL,
        )
        logger = Mock(spec=logging.Logger)

        outcome = recheck_model_plan(plan, BUILT_IN_ACTION_CONTRACTS, logger=logger)

        self.assertIs(outcome, ProposalRejection.UNSAFE_PATH)
        logger.warning.assert_called_once_with(
            "Refused a compiled model plan: code=%s",
            "unsafe_path",
        )
        self.assertEqual(len(logger.mock_calls), 1)


class CallerContractTests(unittest.TestCase):
    """Prove a caller that breaks the compiler's own contract is refused."""

    def _assert_refused(self, call: Any, message: str) -> None:
        """Assert one fixed ValueError, carrying nothing it was handed."""
        logger = Mock(spec=logging.Logger)
        with self.assertRaises(ValueError) as caught:
            call(logger)

        self.assertEqual(str(caught.exception), message)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(logger.mock_calls, [])

    def test_the_request_must_be_cleaned_non_empty_text(self) -> None:
        response = ReasoningResponse(_proposal("system", "current_time", {}))
        for request in (
            None,
            7,
            b"request",
            "",
            " ",
            " " + REQUEST_SENTINEL,
            REQUEST_SENTINEL + "\n",
            _TextSubclass(REQUEST_SENTINEL),
        ):
            with self.subTest(request=type(request).__name__):
                self._assert_refused(
                    lambda logger: compile_proposal(
                        request,
                        response,
                        logger=logger,
                    ),
                    COMPILER_REQUEST_MESSAGE,
                )

    def test_the_proposal_must_be_an_exact_reasoning_response(self) -> None:
        text = _proposal("system", "current_time", {})
        for response in (
            text,
            None,
            Failure(FailureKind.REASONING_UNAVAILABLE),
            SimpleNamespace(text=text),
            _ResponseSubclass(text),
        ):
            with self.subTest(response=type(response).__name__):
                self._assert_refused(
                    lambda logger: compile_proposal(
                        REQUEST,
                        response,
                        logger=logger,
                    ),
                    COMPILER_RESPONSE_MESSAGE,
                )

    def test_the_contracts_must_be_an_exact_registry(self) -> None:
        response = ReasoningResponse(_proposal("system", "current_time", {}))
        for contracts in (
            None,
            {},
            BUILT_IN_ACTION_CONTRACTS.contracts,
            SimpleNamespace(contract_for=lambda *_: None),
        ):
            with self.subTest(contracts=type(contracts).__name__):
                self._assert_refused(
                    lambda logger: compile_proposal(
                        REQUEST,
                        response,
                        contracts,
                        logger=logger,
                    ),
                    COMPILER_CONTRACTS_MESSAGE,
                )

    def test_only_a_well_formed_model_plan_can_be_checked_again(self) -> None:
        self.assertEqual(
            COMPILER_PLAN_MESSAGE,
            "Only a well-formed model-originated plan can be checked again.",
        )
        text = _proposal("system", "current_time", {})
        compiled = _compile(text)
        self.assertIsNone(_recheck(compiled))

        def altered(name: str, value: object) -> Plan:
            """Compile the plan again and change one field underneath."""
            plan = _compile(text)
            object.__setattr__(plan, name, value)
            return plan

        def altered_step(name: str, value: object) -> Plan:
            """Compile the plan again and change one step field underneath."""
            plan = _compile(text)
            object.__setattr__(plan.steps[0], name, value)
            return plan

        # A subclass cannot be built with the model origin, so it is given
        # that origin underneath; only its type then differs.
        subclassed = _PlanSubclass(
            REQUEST,
            compiled.steps,
            recording=RecordingPolicy.TRANSIENT,
        )
        object.__setattr__(subclassed, "origin", PlanOrigin.MODEL)
        self.assertEqual(vars(subclassed), vars(compiled))

        cases = {
            "deterministic twin": replace(compiled, origin=PlanOrigin.DETERMINISTIC),
            "recorded deterministic": Plan(REQUEST, compiled.steps),
            "recorded": altered("recording", RecordingPolicy.RECORD),
            "canned response": altered("response_if_no_action", "Done."),
            "step list": altered("steps", list(compiled.steps)),
            "two steps": altered("steps", compiled.steps * 2),
            "no steps": altered("steps", ()),
            "origin text": altered("origin", "model"),
            "recording text": altered("recording", "transient"),
            "step subclass": altered(
                "steps",
                (_StepSubclass("system", "current_time", {}),),
            ),
            "tool text subclass": altered_step(
                "tool_name",
                _TextSubclass("system"),
            ),
            "action text subclass": altered_step(
                "action",
                _TextSubclass("current_time"),
            ),
            "plain arguments": altered_step("arguments", {}),
            "plan subclass": subclassed,
            "lookalike": SimpleNamespace(**vars(compiled)),
            "step": compiled.steps[0],
            "none": None,
        }
        for case, plan in cases.items():
            with self.subTest(case=case):
                self._assert_refused(
                    lambda logger: recheck_model_plan(
                        plan,
                        BUILT_IN_ACTION_CONTRACTS,
                        logger=logger,
                    ),
                    COMPILER_PLAN_MESSAGE,
                )

    def test_a_check_again_needs_an_exact_registry(self) -> None:
        plan = _compile(_proposal("system", "current_time", {}))
        for contracts in (
            None,
            {},
            BUILT_IN_ACTION_CONTRACTS.contracts,
            SimpleNamespace(contract_for=lambda *_: None),
            _RegistrySubclass(BUILT_IN_ACTION_CONTRACTS.contracts),
        ):
            with self.subTest(contracts=type(contracts).__name__):
                self._assert_refused(
                    lambda logger: recheck_model_plan(
                        plan,
                        contracts,
                        logger=logger,
                    ),
                    COMPILER_CONTRACTS_MESSAGE,
                )


class RejectionPrivacyTests(unittest.TestCase):
    """Prove a refusal is a fixed value that repeats nothing supplied."""

    def test_the_rejection_vocabulary_is_closed_and_fixed(self) -> None:
        self.assertEqual(
            {rejection.code for rejection in ProposalRejection},
            EXPECTED_REJECTION_CODES,
        )
        messages = [rejection.message for rejection in ProposalRejection]
        self.assertEqual(len(set(messages)), len(messages))
        for message in messages:
            with self.subTest(message=message[:24]):
                self.assertIs(type(message), str)
                for placeholder in ("{", "}", "%"):
                    self.assertNotIn(placeholder, message)

    def test_every_contract_failure_has_its_own_rejection(self) -> None:
        expected = {
            ContractFailure.UNKNOWN_TOOL: ProposalRejection.UNKNOWN_TOOL,
            ContractFailure.UNKNOWN_ACTION: ProposalRejection.UNKNOWN_ACTION,
            ContractFailure.MISSING_REQUIRED_ARGUMENT: (
                ProposalRejection.MISSING_ARGUMENT
            ),
            ContractFailure.EXTRA_ARGUMENT: ProposalRejection.EXTRA_ARGUMENT,
            ContractFailure.WRONG_TYPE: ProposalRejection.WRONG_TYPE,
            ContractFailure.OUT_OF_RANGE: ProposalRejection.OUT_OF_RANGE,
        }

        self.assertEqual(dict(plan_compiler_module._CONTRACT_REJECTIONS), expected)

    def test_no_refusal_repeats_a_supplied_name_or_value(self) -> None:
        # Positive control: an ordinary contract refusal would repeat an
        # identifier-shaped name, which is exactly what must not happen here.
        self.assertIn(
            FAKE_NAME_SENTINEL,
            BUILT_IN_ACTION_CONTRACTS.validate(FAKE_NAME_SENTINEL, "run", {}).message,
        )

        texts = _fake_texts()
        texts += [
            _proposal("filesystem", "list", {"path": path + FAKE_PAYLOAD_SENTINEL})
            for path in ("../", "~/", "C:", "CON.")
        ]
        texts.append(
            _proposal(
                "calculator",
                "calculate",
                {"expression": FAKE_PAYLOAD_SENTINEL + chr(0x202E)},
            )
        )
        texts.append(
            _proposal(
                "system",
                FAKE_NAME_SENTINEL,
                {},
                **{FAKE_NAME_SENTINEL: FAKE_PAYLOAD_SENTINEL},
            )
        )
        refused = 0
        for text in texts:
            outcome = _compile(text)
            if type(outcome) is Plan:
                continue
            refused += 1
            with self.subTest(code=outcome.code):
                shown = " ".join(
                    (rendered(outcome), outcome.code, outcome.message)
                )
                self.assertIs(type(outcome), ProposalRejection)
                self.assertNotIn(FAKE_PAYLOAD_SENTINEL, shown)
                self.assertNotIn(FAKE_NAME_SENTINEL, shown)

        self.assertGreaterEqual(refused, len(texts) - 1)

    def test_a_parser_failure_leaves_no_trace(self) -> None:
        logger = Mock(spec=logging.Logger)
        with patch.object(
            plan_compiler_module.json,
            "loads",
            side_effect=ValueError(FAKE_PAYLOAD_SENTINEL),
        ):
            outcome = compile_proposal(
                REQUEST,
                ReasoningResponse(_proposal("system", "current_time", {})),
                logger=logger,
            )

        self.assertIs(outcome, ProposalRejection.MALFORMED_OUTPUT)
        self.assertNotIn(FAKE_PAYLOAD_SENTINEL, rendered(outcome))
        self.assertNotIn(FAKE_PAYLOAD_SENTINEL, repr(logger.mock_calls))


class CompilerLogPrivacyTests(unittest.TestCase):
    """Prove a real log file records codes and nothing the gate was given."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.log_dir = Path(self.temporary_directory.name) / "logs"

        self.maria_logger = logging.getLogger("maria")
        self.original_handlers = tuple(self.maria_logger.handlers)
        self.original_level = self.maria_logger.level
        self.original_propagate = self.maria_logger.propagate
        self.original_disabled = self.maria_logger.disabled
        for handler in self.original_handlers:
            self.maria_logger.removeHandler(handler)
        self.addCleanup(self._restore_maria_logger)

    def _restore_maria_logger(self) -> None:
        """Close the temporary handler and restore the logger as it was."""
        for handler in list(self.maria_logger.handlers):
            try:
                handler.flush()
            finally:
                self.maria_logger.removeHandler(handler)
                handler.close()
        for handler in self.original_handlers:
            self.maria_logger.addHandler(handler)
        self.maria_logger.setLevel(self.original_level)
        self.maria_logger.propagate = self.original_propagate
        self.maria_logger.disabled = self.original_disabled

    def test_codes_are_logged_and_nothing_private_is(self) -> None:
        configure_logging(self.log_dir, level="DEBUG")
        texts = _fake_texts()
        texts += [
            _proposal("filesystem", "list", {"path": "../" + FAKE_PAYLOAD_SENTINEL}),
            _proposal(
                "calculator",
                "calculate",
                {"expression": FAKE_PAYLOAD_SENTINEL + chr(0x2028)},
            ),
            _proposal("calculator", "calculate", {"expression": FAKE_PAYLOAD_SENTINEL}),
        ]

        fingerprints = []
        for text in texts:
            outcome = compile_proposal(REQUEST_SENTINEL, ReasoningResponse(text))
            if type(outcome) is Plan:
                fingerprints.append(outcome.fingerprint)

        log_file = self.log_dir / "maria.log"
        for handler in self.maria_logger.handlers:
            handler.flush()
        log_text = log_file.read_text(encoding="utf-8")

        # Positive controls: the gate did log, so an empty log cannot pass
        # for a private one.
        self.assertGreaterEqual(len(fingerprints), 2)
        self.assertIn("Compiled a model proposal.", log_text)
        for code in (
            "oversized",
            "malformed_output",
            "malformed_proposal",
            "unknown_tool",
            "unknown_action",
            "not_model_eligible",
            "missing_argument",
            "extra_argument",
            "wrong_type",
            "out_of_range",
            "unsafe_text",
            "unsafe_path",
        ):
            with self.subTest(code=code):
                self.assertIn(f"Refused a model proposal: code={code}", log_text)

        assert_log_omits(
            self,
            log_file,
            (
                FAKE_PAYLOAD_SENTINEL,
                FAKE_NAME_SENTINEL,
                REQUEST_SENTINEL,
                *fingerprints,
            ),
        )

    def test_a_check_again_logs_its_code_and_nothing_private(self) -> None:
        configure_logging(self.log_dir, level="DEBUG")
        refused = {
            "unknown_tool": _hand_built(
                FAKE_NAME_SENTINEL,
                "run",
                {},
                request=REQUEST_SENTINEL,
            ),
            "unknown_action": _hand_built(
                "system",
                FAKE_NAME_SENTINEL,
                {},
                request=REQUEST_SENTINEL,
            ),
            "not_model_eligible": _hand_built(
                "memory",
                "store",
                {"fact": FAKE_PAYLOAD_SENTINEL},
                request=REQUEST_SENTINEL,
            ),
            "extra_argument": _hand_built(
                "calculator",
                "calculate",
                {"expression": "1", FAKE_NAME_SENTINEL: FAKE_PAYLOAD_SENTINEL},
                request=REQUEST_SENTINEL,
            ),
            "unsafe_text": _hand_built(
                "calculator",
                "calculate",
                {"expression": FAKE_PAYLOAD_SENTINEL + chr(0x2028)},
                request=REQUEST_SENTINEL,
            ),
            "unsafe_path": _hand_built(
                "filesystem",
                "list",
                {"path": "../" + FAKE_PAYLOAD_SENTINEL},
                request=REQUEST_SENTINEL,
            ),
        }
        passing = _hand_built(
            "calculator",
            "calculate",
            {"expression": FAKE_PAYLOAD_SENTINEL},
            request=REQUEST_SENTINEL,
        )

        outcomes = {
            code: recheck_model_plan(plan, BUILT_IN_ACTION_CONTRACTS)
            for code, plan in refused.items()
        }
        self.assertIsNone(recheck_model_plan(passing, BUILT_IN_ACTION_CONTRACTS))

        log_file = self.log_dir / "maria.log"
        for handler in self.maria_logger.handlers:
            handler.flush()
        log_text = log_file.read_text(encoding="utf-8")

        # Positive controls: every refusal was logged by its code, so an empty
        # log cannot pass for a private one.
        for code, outcome in outcomes.items():
            with self.subTest(code=code):
                self.assertIs(outcome, ProposalRejection(code))
                self.assertIn(f"Refused a compiled model plan: code={code}", log_text)

        assert_log_omits(
            self,
            log_file,
            (
                FAKE_PAYLOAD_SENTINEL,
                FAKE_NAME_SENTINEL,
                REQUEST_SENTINEL,
                passing.fingerprint,
                *(plan.fingerprint for plan in refused.values()),
            ),
        )


class CompilerImportBoundaryTests(unittest.TestCase):
    """Guard what the compiler can reach, and that only Brain reaches it."""

    PERMITTED = frozenset(
        {
            "__future__",
            "enum",
            "json",
            "logging",
            "pathlib",
            "types",
            "unicodedata",
            "core.action_contracts",
            "core.logger",
            "core.planner",
            "core.reasoning",
        }
    )

    # The Maria modules the compiler reaches, directly or through another.
    PERMITTED_REACH = frozenset(
        {
            "core.plan_compiler",
            "core.action_contracts",
            "core.constants",
            "core.context",
            "core.failures",
            "core.logger",
            "core.planner",
            "core.reasoning",
        }
    )

    FORBIDDEN_REACH = frozenset(
        {
            "main",
            "core.brain",
            "core.config",
            "core.conversation",
            "core.filesystem_roots",
            "core.memory",
            "core.ollama_adapter",
            "core.permissions",
            "core.session",
            "core.tool_manager",
        }
    )

    def setUp(self) -> None:
        self.source = Path(plan_compiler_module.__file__).read_text(
            encoding="utf-8"
        )

    def test_the_compiler_imports_only_what_it_needs(self) -> None:
        self.assertLessEqual(_direct_imports(self.source), self.PERMITTED)
        self.assertEqual(forbidden_imports(self.source), set())
        self.assertEqual(forbidden_calls(self.source), set())

    def test_only_pure_paths_are_taken_from_pathlib(self) -> None:
        names = {
            alias.name
            for node in ast.walk(ast.parse(self.source))
            if isinstance(node, ast.ImportFrom) and node.module == "pathlib"
            for alias in node.names
        }

        self.assertEqual(names, {"PurePosixPath", "PureWindowsPath"})

    def test_the_compiler_opens_nothing(self) -> None:
        calls = {
            node.func.id
            for node in ast.walk(ast.parse(self.source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        self.assertNotIn("open", calls)

    def test_the_compiler_reaches_no_dispatch_permission_or_persistence(
        self,
    ) -> None:
        reach = _maria_reach("core.plan_compiler")

        self.assertLessEqual(reach, self.PERMITTED_REACH)
        self.assertEqual(reach & self.FORBIDDEN_REACH, set())
        self.assertFalse(any(name.startswith("tools") for name in reach))
        for module in reach:
            with self.subTest(module=module):
                source = _module_path(module).read_text(encoding="utf-8")
                self.assertEqual(forbidden_imports(source), set())
                self.assertNotIn("sqlite3", _direct_imports(source))
                self.assertNotIn("subprocess", _direct_imports(source))

    def test_only_brain_reaches_the_compiler(self) -> None:
        compiler_path = Path(plan_compiler_module.__file__).resolve()
        scanned = 0
        reaching = set()
        for path in PRODUCTION_SOURCES:
            if path.resolve() == compiler_path:
                continue
            scanned += 1
            if _reaches_the_compiler(path.read_text(encoding="utf-8")):
                reaching.add(path.relative_to(PROJECT_ROOT).as_posix())

        self.assertGreater(scanned, 10)
        self.assertEqual(reaching, {"core/brain.py"})

        # Positive controls: Brain imports the gate itself and names both of
        # its entry points, and the detector sees every spelling of the
        # import but not a prose mention of the compiler's file.
        brain = (PROJECT_ROOT / "core" / "brain.py").read_text("utf-8")
        self.assertIn("core.plan_compiler", _direct_imports(brain))
        self.assertIn("compile_proposal", brain)
        self.assertIn("recheck_model_plan", brain)
        for spelling in (
            "import core.plan_compiler",
            "import core.plan_compiler as gate",
            "from core.plan_compiler import MODEL_ACTION_ALLOWLIST",
            "from core import plan_compiler",
            "from . import plan_compiler",
            "from .plan_compiler import MODEL_ACTION_ALLOWLIST",
            "import core\ncore.plan_compiler.MODEL_ACTION_ALLOWLIST",
            "gate.compile_proposal(request, reply)",
            "gate.recheck_model_plan(plan, contracts)",
        ):
            with self.subTest(spelling=ascii(spelling)):
                self.assertTrue(_reaches_the_compiler(spelling))
        for spelling in (
            '"""See core/plan_compiler.py."""',
            "from core.planner import Plan",
        ):
            with self.subTest(unreached=spelling):
                self.assertFalse(_reaches_the_compiler(spelling))

        for entry in ("session.py", "conversation.py"):
            with self.subTest(entry=entry):
                source = (PROJECT_ROOT / "core" / entry).read_text("utf-8")
                self.assertNotIn("plan_compiler", source)


class _TextSubclass(str):
    """A str subclass, which is never the exact text a request must be."""


class _ResponseSubclass(ReasoningResponse):
    """A response subclass, which is never an exact ReasoningResponse."""


class _PlanSubclass(Plan):
    """A Plan subclass, which is never the exact plan a model plan must be."""


class _StepSubclass(PlanStep):
    """A PlanStep subclass, which is never the exact step a model plan holds."""


class _RegistrySubclass(ActionContractRegistry):
    """A registry subclass, which is never the exact registry a check needs."""


def _tampered(text: object) -> ReasoningResponse:
    """Return a response whose text was replaced after construction."""
    response = ReasoningResponse("placeholder")
    object.__setattr__(response, "text", text)
    return response


def _direct_imports(source: str) -> set[str]:
    """Return every module a source names in an import statement."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def _reaches_the_compiler(source: str) -> bool:
    """Return whether a source imports the compiler or names an entry point.

    Every spelling of the import counts, ``from core import plan_compiler``
    and relative imports included, and so does reaching the module as an
    attribute of its package or naming either entry point anywhere. A prose
    mention of the compiler's file, such as a docstring may make, does not.
    """
    imported = any(
        "plan_compiler" in name.split(".") for name in own_imports(source)
    )
    attribute = any(
        isinstance(node, ast.Attribute) and node.attr == "plan_compiler"
        for node in ast.walk(ast.parse(source))
    )
    return (
        imported
        or attribute
        or "compile_proposal" in source
        or "recheck_model_plan" in source
    )


def _module_path(module: str) -> Path:
    """Return the source file of one Maria module."""
    return PROJECT_ROOT.joinpath(*module.split(".")).with_suffix(".py")


def _maria_reach(start: str) -> set[str]:
    """Return every Maria module a module imports, directly or not."""
    reached = {start}
    pending = [start]
    while pending:
        source = _module_path(pending.pop()).read_text(encoding="utf-8")
        for name in _direct_imports(source):
            if name.split(".")[0] in {"core", "tools", "main"} and name not in reached:
                reached.add(name)
                pending.append(name)
    return reached


if __name__ == "__main__":
    unittest.main()
