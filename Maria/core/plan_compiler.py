"""The gate that turns one untrusted model proposal into a Maria plan.

A reasoning model's output is untrusted, so this module is deny-by-default. A
proposal is compiled only when its tool action is in the closed allowlist
declared here, its action contract separately classifies it as
model-eligible, and every argument satisfies that contract and the text and
path checks below. Anything else is refused with one fixed
``ProposalRejection``. Nothing here repairs a proposal, and nothing here
decides permission: PermissionManager allows any action absent from its
mandatory sets, so it cannot be the positive gate, and a compiled plan still
has to pass it later, unchanged.

The compiler executes nothing. It calls no tool, Tool Manager, permission
check, memory layer, conversation, network, shell, or SQL, and it performs no
filesystem operation. A model-supplied path is judged by its text alone, under
both the Windows and the POSIX reading of that text. That lexical preflight
cannot see a symbolic link, so it establishes only that the text names
something beneath the default named root; resolved containment and the
permanent exclusions stay with ``FilesystemRoots`` and ``FilesystemTool``
when a plan eventually runs.

Nothing reaches this module yet. Brain does not import it, so no model output
can currently become an action.

Privacy follows ``core/failures.py``. A rejection is an enum member and
nothing else, so it cannot carry the proposal, a tool, action, or argument
name, a value, or a parser error. No exception raised while reading a
proposal escapes: each is settled into a rejection inside this module. The
proposal, a compiled plan, and its fingerprint are never handed to the
logger; a refusal logs its code.
"""

from __future__ import annotations

import json
import unicodedata
from enum import Enum
from logging import Logger
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType

from core.action_contracts import (
    BUILT_IN_ACTION_CONTRACTS,
    ActionContract,
    ActionContractRegistry,
    ContractFailure,
    ModelEligibility,
)
from core.logger import get_logger
from core.planner import Plan, PlanStep, RecordingPolicy
from core.reasoning import ReasoningResponse


# The closed positive allowlist. It is declared here by hand rather than read
# from the registry's eligibility classification, so a proposal is compiled
# only when two independent declarations agree, and a change to either one
# alone opens nothing.
MODEL_ACTION_ALLOWLIST = frozenset(
    {
        "calculator.calculate",
        "filesystem.list",
        "system.current_time",
    }
)

# The largest proposal considered at all, in UTF-8 bytes, measured before any
# parsing. One single-step proposal for an allowlisted action is far smaller.
MODEL_PROPOSAL_MAX_BYTES = 4096

# The deepest container nesting considered, checked before parsing. A proposal
# needs two levels, the proposal object and its arguments object; the rest is
# room for a wrongly shaped proposal to be refused by its shape, while the
# parser never recurses into pathological nesting.
MODEL_PROPOSAL_MAX_NESTING = 8

PROPOSAL_FIELDS = frozenset({"tool", "action", "arguments"})

# Unicode categories an argument string may not contain at all: control,
# format, surrogate, line separator, and paragraph separator. Any of them can
# hide or reorder what the owner is later shown, so the whole value is
# refused; nothing is ever stripped out of it.
UNSAFE_TEXT_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

# Device names Windows reserves in any directory, with or without an
# extension, matching the standard library's own list. They are refused under
# both readings of a path, because a name is only safe if it is safe on
# either platform.
_RESERVED_DEVICE_DIGITS = "123456789" + chr(0xB9) + chr(0xB2) + chr(0xB3)
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{digit}" for digit in _RESERVED_DEVICE_DIGITS}
    | {f"LPT{digit}" for digit in _RESERVED_DEVICE_DIGITS}
)

_NUL = chr(0)
_PATH_FLAVOURS = (PurePosixPath, PureWindowsPath)

# What parsing returns for text that is not one strict JSON document. JSON's
# own ``null`` parses to None, so None cannot be the one that means failure.
_NOT_ONE_DOCUMENT = object()


class ProposalRejection(str, Enum):
    """Why one model proposal was refused, and nothing else."""

    OVERSIZED = "oversized"
    MALFORMED_OUTPUT = "malformed_output"
    MALFORMED_PROPOSAL = "malformed_proposal"
    UNKNOWN_TOOL = "unknown_tool"
    UNKNOWN_ACTION = "unknown_action"
    NOT_MODEL_ELIGIBLE = "not_model_eligible"
    MISSING_ARGUMENT = "missing_argument"
    EXTRA_ARGUMENT = "extra_argument"
    WRONG_TYPE = "wrong_type"
    OUT_OF_RANGE = "out_of_range"
    UNSAFE_TEXT = "unsafe_text"
    UNSAFE_PATH = "unsafe_path"

    @property
    def code(self) -> str:
        """Return the stable identifier a log line may record."""
        return self.value

    @property
    def message(self) -> str:
        """Return the fixed owner-visible refusal for this kind."""
        return _REJECTION_MESSAGES[self]


# Every message is fixed and interpolates nothing, so a refusal can never
# repeat a tool, action, or argument name the model supplied, or a value.
_REJECTION_MESSAGES = MappingProxyType(
    {
        ProposalRejection.OVERSIZED: (
            "The proposed action was too large to consider."
        ),
        ProposalRejection.MALFORMED_OUTPUT: (
            "The proposed action was not a single well-formed JSON object."
        ),
        ProposalRejection.MALFORMED_PROPOSAL: (
            "The proposed action did not have exactly a tool, an action, and "
            "an arguments object."
        ),
        ProposalRejection.UNKNOWN_TOOL: (
            "The proposed action named a tool Maria does not have."
        ),
        ProposalRejection.UNKNOWN_ACTION: (
            "The proposed action named an action Maria does not support."
        ),
        ProposalRejection.NOT_MODEL_ELIGIBLE: (
            "Maria does not accept that action from the reasoning model."
        ),
        ProposalRejection.MISSING_ARGUMENT: (
            "The proposed action was missing a required argument."
        ),
        ProposalRejection.EXTRA_ARGUMENT: (
            "The proposed action had an argument Maria does not accept."
        ),
        ProposalRejection.WRONG_TYPE: (
            "The proposed action had an argument of the wrong type."
        ),
        ProposalRejection.OUT_OF_RANGE: (
            "The proposed action had an argument outside its supported range."
        ),
        ProposalRejection.UNSAFE_TEXT: (
            "The proposed action had an argument containing control or "
            "formatting characters."
        ),
        ProposalRejection.UNSAFE_PATH: (
            "The proposed action had a path that is not a plain relative path "
            "inside Maria's default filesystem root."
        ),
    }
)

_CONTRACT_REJECTIONS = MappingProxyType(
    {
        ContractFailure.UNKNOWN_TOOL: ProposalRejection.UNKNOWN_TOOL,
        ContractFailure.UNKNOWN_ACTION: ProposalRejection.UNKNOWN_ACTION,
        ContractFailure.MISSING_REQUIRED_ARGUMENT: (
            ProposalRejection.MISSING_ARGUMENT
        ),
        ContractFailure.EXTRA_ARGUMENT: ProposalRejection.EXTRA_ARGUMENT,
        ContractFailure.WRONG_TYPE: ProposalRejection.WRONG_TYPE,
        ContractFailure.OUT_OF_RANGE: ProposalRejection.OUT_OF_RANGE,
    }
)

# Fixed messages for a caller that breaks this module's own contract. Each is
# raised outside any exception handler and repeats nothing it was handed.
COMPILER_REQUEST_MESSAGE = (
    "A compiled plan's request must be the owner's cleaned, non-empty request "
    "text."
)
COMPILER_RESPONSE_MESSAGE = (
    "A model proposal must be exactly a ReasoningResponse."
)
COMPILER_CONTRACTS_MESSAGE = (
    "Model proposals must be checked against an ActionContractRegistry."
)


class _StrictJsonError(ValueError):
    """Raised inside the parser for JSON Maria refuses but Python accepts."""


def compile_proposal(
    request: str,
    response: ReasoningResponse,
    contracts: ActionContractRegistry = BUILT_IN_ACTION_CONTRACTS,
    logger: Logger | None = None,
) -> Plan | ProposalRejection:
    """Compile one model proposal into a single-step plan, or refuse it.

    ``request`` is the owner's cleaned request and becomes the plan's
    ``original_request``; no model text ever does. ``response`` is the
    model's reply, read again here rather than trusted from construction.
    ``contracts`` is the registry the eventual dispatch would use.

    The gates run in a fixed order and the first failure decides the
    rejection: the byte cap, strict parsing, the proposal's shape, the
    allowlist together with the contract's eligibility, the argument
    contract, then the text and path checks on each present argument in
    declared order. Only a proposal passing all of them becomes a plan: the
    existing immutable, fingerprinted ``Plan`` holding exactly one step,
    named by Maria's own declared tool and action, with the arguments exactly
    as supplied, and recorded as ``TRANSIENT``.

    A caller passing anything but cleaned request text, an exact
    ``ReasoningResponse``, or an exact registry receives ``ValueError`` with
    a fixed message. The log records only whether a proposal compiled and,
    when it did not, the rejection code.
    """
    if type(contracts) is not ActionContractRegistry:
        raise ValueError(COMPILER_CONTRACTS_MESSAGE)
    if type(request) is not str or not request or request != request.strip():
        raise ValueError(COMPILER_REQUEST_MESSAGE)
    if type(response) is not ReasoningResponse:
        raise ValueError(COMPILER_RESPONSE_MESSAGE)

    log = logger or get_logger(__name__)
    outcome = _compile(request, getattr(response, "text", None), contracts)
    if type(outcome) is ProposalRejection:
        log.warning("Refused a model proposal: code=%s", outcome.code)
    else:
        log.info("Compiled a model proposal.")
    return outcome


def _compile(
    request: str,
    text: object,
    contracts: ActionContractRegistry,
) -> Plan | ProposalRejection:
    """Apply every gate in order and build the plan only after all pass."""
    size = _utf8_size(text)
    if size is None:
        return ProposalRejection.MALFORMED_OUTPUT
    if size > MODEL_PROPOSAL_MAX_BYTES:
        return ProposalRejection.OVERSIZED

    proposal = _parse(text)
    if proposal is _NOT_ONE_DOCUMENT:
        return ProposalRejection.MALFORMED_OUTPUT
    if not _is_proposal(proposal):
        return ProposalRejection.MALFORMED_PROPOSAL

    arguments = proposal["arguments"]
    contract = _eligible_contract(
        proposal["tool"],
        proposal["action"],
        contracts,
    )
    if type(contract) is ProposalRejection:
        return contract

    violation = contract.validate(arguments)
    if violation is not None:
        return _CONTRACT_REJECTIONS[violation.kind]

    for argument in contract.arguments:
        if argument.name not in arguments:
            continue
        value = arguments[argument.name]
        if type(value) is str and _has_unsafe_character(value):
            return ProposalRejection.UNSAFE_TEXT
        if argument.is_path and not _is_plain_relative_path(value):
            return ProposalRejection.UNSAFE_PATH

    return Plan(
        original_request=request,
        steps=(PlanStep(contract.tool_name, contract.action, arguments),),
        recording=RecordingPolicy.TRANSIENT,
    )


def _utf8_size(text: object) -> int | None:
    """Measure text against the byte cap without copying oversized text.

    Returns None for anything but exact, encodable text. Every encodable
    character takes at least one UTF-8 byte, so text with more characters
    than the cap is over it and is never encoded: the result is then only
    some value above the cap, which is all the caller compares.
    """
    if type(text) is not str:
        return None
    if len(text) > MODEL_PROPOSAL_MAX_BYTES:
        return MODEL_PROPOSAL_MAX_BYTES + 1
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _parse(text: str) -> object:
    """Parse one strict JSON document, or return ``_NOT_ONE_DOCUMENT``.

    Strict means what JSON itself says and nothing Python adds: a repeated
    key and the ``NaN`` and ``Infinity`` constants are refused, as is text
    nesting deeper than ``MODEL_PROPOSAL_MAX_NESTING``, which is settled
    before the parser runs. Surrounding prose, a code fence, a second
    document, and truncated text are all simply not one JSON document. The
    parser's own error is dropped without being bound, so none of its text
    or position survives.
    """
    if not _is_nesting_within(text, MODEL_PROPOSAL_MAX_NESTING):
        return _NOT_ONE_DOCUMENT
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_refuse_constant,
        )
    except ValueError:
        return _NOT_ONE_DOCUMENT


def _is_nesting_within(text: str, limit: int) -> bool:
    """Return whether JSON text never opens more than ``limit`` containers.

    Brackets inside strings are skipped exactly as the JSON grammar reads
    strings, so the count matches the nesting the parser would build. A
    closing bracket with nothing open is refused too; such text can never be
    one JSON document.
    """
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > limit:
                return False
        elif character in "]}":
            depth -= 1
            if depth < 0:
                return False
    return True


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one JSON object, refusing it when any key repeats."""
    built = dict(pairs)
    if len(built) != len(pairs):
        raise _StrictJsonError
    return built


def _refuse_constant(name: str) -> object:
    """Refuse ``NaN``, ``Infinity``, and ``-Infinity``, which JSON lacks."""
    raise _StrictJsonError


def _is_proposal(value: object) -> bool:
    """Return whether a parsed document has exactly the proposal shape.

    That is one object holding exactly ``tool`` and ``action`` as strings
    and ``arguments`` as an object, even when no argument is needed.
    """
    return (
        type(value) is dict
        and set(value) == PROPOSAL_FIELDS
        and type(value["tool"]) is str
        and type(value["action"]) is str
        and type(value["arguments"]) is dict
    )


def _eligible_contract(
    tool_name: str,
    action: str,
    contracts: ActionContractRegistry,
) -> ActionContract | ProposalRejection:
    """Return the contract of an open action, or why the action is closed.

    The allowlist and the contract's classification must both admit the
    action. Every declared name contains no dot, so the identifier built here
    can only match an allowlisted one when the tool and action are exactly
    that entry's. A refused action is then only classified, for the
    rejection's kind: registered but closed, an unknown action of a known
    tool, or an unknown tool.
    """
    allowlisted = f"{tool_name}.{action}" in MODEL_ACTION_ALLOWLIST
    contract = contracts.contract_for(tool_name, action)
    if (
        allowlisted
        and contract is not None
        and contract.model_eligibility is ModelEligibility.MODEL_ELIGIBLE
    ):
        return contract
    if contract is not None:
        return ProposalRejection.NOT_MODEL_ELIGIBLE
    if tool_name in contracts.tool_names:
        return ProposalRejection.UNKNOWN_ACTION
    return ProposalRejection.UNKNOWN_TOOL


def _has_unsafe_character(text: str) -> bool:
    """Return whether text holds any character of an unsafe category."""
    return any(
        unicodedata.category(character) in UNSAFE_TEXT_CATEGORIES
        for character in text
    )


def _is_plain_relative_path(text: str) -> bool:
    """Return whether path text is safe to place beneath the default root.

    The text is read, never resolved: nothing here touches the filesystem,
    expands a home directory, or follows a link. It must hold no NUL and no
    colon, which rules out drives and alternate data streams. Under both the
    POSIX and the Windows reading it must then have no root, drive, or UNC
    share, name at least one component, not begin with ``~``, and contain no
    component made only of dots and spaces, which covers parent traversal
    and the spellings Windows would treat as it. No component may be a
    reserved device name. Passing says only what the text names; whether the
    resolved location is inside a root and reachable is decided when a plan
    runs.
    """
    if _NUL in text or ":" in text:
        return False
    for flavour in _PATH_FLAVOURS:
        path = flavour(text)
        if path.anchor or not path.parts:
            return False
        if path.parts[0].startswith("~"):
            return False
        for part in path.parts:
            if not part.strip(". ") or _is_reserved_device_name(part):
                return False
    return True


def _is_reserved_device_name(part: str) -> bool:
    """Return whether one path component names a Windows device.

    Windows ignores an extension and trailing spaces when it recognises a
    device, so ``nul.txt`` and ``CON `` are devices as surely as ``NUL``.
    """
    stem = part.partition(".")[0].rstrip(" ")
    return stem.upper() in WINDOWS_RESERVED_NAMES
