"""The gate that turns one untrusted model proposal into a Maria plan.

A reasoning model's output is untrusted, so this module is deny-by-default. A
proposal names one action, or, inside a ``steps`` envelope, two to
``PLAN_MAX_STEPS`` actions run in order. It is compiled only when every
proposed tool action is in the closed allowlist declared here, its action
contract separately classifies it as model-eligible, and every argument
satisfies that contract and the text and path checks below. Compilation is
all or nothing: anything else is refused with one fixed
``ProposalRejection``, and no part of a refused proposal becomes a plan.
Nothing here repairs a proposal, and nothing here decides permission:
PermissionManager allows any action absent from its mandatory sets, so it
cannot be the positive gate, and a compiled plan still has to pass it later,
unchanged.

A proposal to create a task or reminder, from V1-22, compiles only from the
owner's own words. Its raw arguments are checked first; the owner's cleaned
request must then be one to five items in the four supported forms that
``core/task_requests.py`` parses; the proposal must hold exactly one creation
step per item, in order, copying each item's body and timing phrase; and
each item's own phrase is resolved at the request's one reference instant,
through ``core/time_resolution.py``. Maria then builds every step from her
own extraction, so the model's values are compared and never used: this is
the one declared, deterministic exception to compiling arguments exactly as
supplied, and it replaces only the timing value, with Maria's resolution of
the owner's own phrase. A proposal with no creation step compiles exactly as
it always has.

The compiler executes nothing. It calls no tool, Tool Manager, permission
check, memory layer, conversation, network, shell, or SQL, and it performs no
filesystem operation. A model-supplied path is judged by its text alone, under
both the Windows and the POSIX reading of that text. That lexical preflight
cannot see a symbolic link, so it establishes only that the text names
something beneath the default named root; resolved containment and the
permanent exclusions stay with ``FilesystemRoots`` and ``FilesystemTool``
when a plan eventually runs.

Brain is the one production module that reaches this one. It hands a
proposal attempt to ``compile_proposal``, and it asks ``recheck_model_plan``
about the compiled plan again when it offers that plan for the owner's
approval and once more just before the approved plan runs. A compiled plan
is model-originated, so Brain never runs it without that approval.

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
from core.planner import (
    PLAN_MAX_STEPS,
    Plan,
    PlanArguments,
    PlanOrigin,
    PlanStep,
    RecordingPolicy,
    is_model_plan,
)
from core.reasoning import ReasoningResponse
from core.task_requests import (
    CreationItem,
    CreationKind,
    RequestRefusal,
    canonical_phrase,
    parse_creation_request,
)
from core.time_resolution import (
    ResolvedTime,
    TimingContext,
    TimingFailure,
    iso_date_text,
    resolve_reminder_time,
    resolve_task_date,
)


# The closed positive allowlist. It is declared here by hand rather than read
# from the registry's eligibility classification, so a proposal is compiled
# only when two independent declarations agree, and a change to either one
# alone opens nothing.
MODEL_ACTION_ALLOWLIST = frozenset(
    {
        "calculator.calculate",
        "filesystem.list",
        "system.current_time",
        "tasks.create",
        "reminders.create",
    }
)

# The two creation actions and the kind of requested item each one binds to.
# A proposal holding either one compiles only through the creation gates.
CREATION_ACTIONS = MappingProxyType(
    {
        "tasks.create": CreationKind.TASK,
        "reminders.create": CreationKind.REMINDER,
    }
)

# The closed proposal-input table for the creation actions: every argument
# each one accepts, whether it is required, and its raw check, in the order
# the checks run. A ``text`` is 1 to ``PROPOSAL_TEXT_MAX_BYTES`` UTF-8 bytes
# and not blank; a timing phrase is 1 to ``PROPOSAL_PHRASE_MAX_LENGTH``
# characters of printable ASCII; and ``repeat`` is exactly ``never``. These
# run before anything else reads a value, so no model value is tokenized,
# compared, or resolved before it has been checked.
PROPOSAL_TEXT_MAX_BYTES = 512
PROPOSAL_PHRASE_MAX_LENGTH = 64
_TEXT_INPUT = "text"
_PHRASE_INPUT = "phrase"
_NEVER_INPUT = "never"
_PROPOSAL_INPUTS = MappingProxyType(
    {
        "tasks.create": (
            ("text", True, _TEXT_INPUT),
            ("due", False, _PHRASE_INPUT),
        ),
        "reminders.create": (
            ("text", True, _TEXT_INPUT),
            ("when", True, _PHRASE_INPUT),
            ("repeat", True, _NEVER_INPUT),
        ),
    }
)
_REPEAT_NEVER = "never"

# The largest proposal considered at all, in UTF-8 bytes, measured before any
# parsing. The cap covers the whole reply, however many actions it proposes:
# a proposal for allowlisted actions is far smaller, and one whose arguments
# make it larger is refused, never cut down.
MODEL_PROPOSAL_MAX_BYTES = 4096

# The deepest container nesting considered, checked before parsing. A proposal
# for one action needs two levels, the proposal object and its arguments
# object, and a steps envelope four: the envelope, its list, each action, and
# each action's arguments. The rest is room for a wrongly shaped proposal to
# be refused by its shape, while the parser never recurses into pathological
# nesting.
MODEL_PROPOSAL_MAX_NESTING = 8

PROPOSAL_FIELDS = frozenset({"tool", "action", "arguments"})

# A proposal for several actions is one object holding exactly this field: a
# list of at least this many, and at most PLAN_MAX_STEPS, action objects, each
# shaped exactly as a proposal for one action. One action is always proposed
# in its own shape, so no plan can be spelled two ways.
STEPS_ENVELOPE_FIELDS = frozenset({"steps"})
MODEL_PROPOSAL_MIN_STEPS = 2

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
    MALFORMED_PLAN = "malformed_plan"
    UNKNOWN_TOOL = "unknown_tool"
    UNKNOWN_ACTION = "unknown_action"
    NOT_MODEL_ELIGIBLE = "not_model_eligible"
    MISSING_ARGUMENT = "missing_argument"
    EXTRA_ARGUMENT = "extra_argument"
    WRONG_TYPE = "wrong_type"
    OUT_OF_RANGE = "out_of_range"
    UNSAFE_TEXT = "unsafe_text"
    UNSAFE_PATH = "unsafe_path"
    NOT_A_CREATION_REQUEST = "not_a_creation_request"
    CREATION_REQUEST_UNCLEAR = "creation_request_unclear"
    CREATION_MISMATCH = "creation_mismatch"
    TIME_PAST = "time_past"
    TIME_TOO_FAR = "time_too_far"
    TIME_NONEXISTENT = "time_nonexistent"
    TIME_AMBIGUOUS = "time_ambiguous"
    TIME_ZONE_UNAVAILABLE = "time_zone_unavailable"

    @property
    def code(self) -> str:
        """Return the stable identifier a log line may record."""
        return self.value

    @property
    def message(self) -> str:
        """Return the fixed owner-visible refusal for this kind."""
        return _REJECTION_MESSAGES[self]


# Every message is fixed when this module loads, built from nothing but
# Maria's own constants, so a refusal can never repeat a tool, action, or
# argument name the model supplied, or a value.
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
        ProposalRejection.MALFORMED_PLAN: (
            "The proposed plan did not have exactly a steps list of "
            f"{MODEL_PROPOSAL_MIN_STEPS} to {PLAN_MAX_STEPS} actions, "
            "each with exactly a tool, an action, and an arguments object."
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
        ProposalRejection.NOT_A_CREATION_REQUEST: (
            "Maria adds a task or reminder from conversation only when your "
            "whole request is a supported form, such as 'remind me tomorrow "
            "at 09:00 to call mom' or 'add a task to pay rent by friday'. "
            "Nothing was added."
        ),
        ProposalRejection.CREATION_REQUEST_UNCLEAR: (
            "That request names a task or reminder, but its time or wording "
            "is missing, vague, or not a supported form. Please say it again, "
            "for example 'remind me tomorrow at 09:00 to call mom'. Nothing "
            "was added."
        ),
        ProposalRejection.CREATION_MISMATCH: (
            "The proposed tasks or reminders did not match your request: each "
            "item must appear once, in order, with its text and time copied "
            "exactly. Nothing was added."
        ),
        ProposalRejection.TIME_PAST: (
            f"{TimingFailure.PAST.message} Nothing was added."
        ),
        ProposalRejection.TIME_TOO_FAR: (
            f"{TimingFailure.TOO_FAR.message} Nothing was added."
        ),
        ProposalRejection.TIME_NONEXISTENT: (
            f"{TimingFailure.NONEXISTENT.message} Nothing was added."
        ),
        ProposalRejection.TIME_AMBIGUOUS: (
            f"{TimingFailure.AMBIGUOUS.message} Nothing was added."
        ),
        ProposalRejection.TIME_ZONE_UNAVAILABLE: (
            f"{TimingFailure.ZONE_UNAVAILABLE.message} Nothing was added."
        ),
    }
)

_REQUEST_REJECTIONS = MappingProxyType(
    {
        RequestRefusal.NOT_A_CREATION_REQUEST: (
            ProposalRejection.NOT_A_CREATION_REQUEST
        ),
        RequestRefusal.UNCLEAR: ProposalRejection.CREATION_REQUEST_UNCLEAR,
    }
)

_TIMING_REJECTIONS = MappingProxyType(
    {
        TimingFailure.PAST: ProposalRejection.TIME_PAST,
        TimingFailure.TOO_FAR: ProposalRejection.TIME_TOO_FAR,
        TimingFailure.NONEXISTENT: ProposalRejection.TIME_NONEXISTENT,
        TimingFailure.AMBIGUOUS: ProposalRejection.TIME_AMBIGUOUS,
        TimingFailure.ZONE_UNAVAILABLE: ProposalRejection.TIME_ZONE_UNAVAILABLE,
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
COMPILER_PLAN_MESSAGE = (
    "Only a well-formed model-originated plan can be checked again."
)
COMPILER_TIMING_MESSAGE = (
    "A model proposal's timing must be a TimingContext or None."
)


class _StrictJsonError(ValueError):
    """Raised inside the parser for JSON Maria refuses but Python accepts."""


def compile_proposal(
    request: str,
    response: ReasoningResponse,
    contracts: ActionContractRegistry = BUILT_IN_ACTION_CONTRACTS,
    logger: Logger | None = None,
    *,
    timing: TimingContext | None = None,
) -> Plan | ProposalRejection:
    """Compile one model proposal into a plan of 1 to 5 steps, or refuse it.

    ``request`` is the owner's cleaned request and becomes the plan's
    ``original_request``; no model text ever does. ``response`` is the
    model's reply, read again here rather than trusted from construction.
    ``contracts`` is the registry the eventual dispatch would use.
    ``timing`` is the request's one reference instant and configured zone,
    which only a creation step's timing ever uses; without it, any timing
    is refused as unavailable.

    The gates run in a fixed order and the first failure decides the
    rejection: the byte cap, strict parsing, the proposal's shape, which is
    either one action or a ``steps`` envelope of 2 to ``PLAN_MAX_STEPS``
    actions, and then, for each action in order, the allowlist together
    with the contract's eligibility, the argument contract, and the text and
    path checks on each present argument in declared order. Only a proposal
    whose every action passes all of them becomes a plan: the existing
    immutable, fingerprinted ``Plan`` holding one step per proposed action,
    in the proposed order, each named by Maria's own declared tool and
    action, with the arguments exactly as supplied, recorded as
    ``TRANSIENT``, and of origin ``MODEL``. Nothing is repaired, reordered,
    defaulted, or substituted, and a reference-like value such as ``$1`` is
    only literal text.

    A proposal holding a creation step goes through the creation gates
    instead, in this order: allowlist and eligibility for every step; the
    raw checks of every creation step's arguments; the parse of the owner's
    request and the positional binding of steps to its items; the
    resolution of each item's own timing phrase at the shared instant and
    the duplicate rule; and the step checks above on the canonical steps
    Maria builds from her own extraction. No model value enters that plan.

    A caller passing anything but cleaned request text, an exact
    ``ReasoningResponse``, an exact registry, or a timing context or None
    receives ``ValueError`` with a fixed message. The log records only
    whether a proposal compiled and, when it did not, the rejection code.
    """
    if type(contracts) is not ActionContractRegistry:
        raise ValueError(COMPILER_CONTRACTS_MESSAGE)
    if type(request) is not str or not request or request != request.strip():
        raise ValueError(COMPILER_REQUEST_MESSAGE)
    if type(response) is not ReasoningResponse:
        raise ValueError(COMPILER_RESPONSE_MESSAGE)
    if timing is not None and type(timing) is not TimingContext:
        raise ValueError(COMPILER_TIMING_MESSAGE)

    log = logger or get_logger(__name__)
    outcome = _compile(request, getattr(response, "text", None), contracts, timing)
    if type(outcome) is ProposalRejection:
        log.warning("Refused a model proposal: code=%s", outcome.code)
    else:
        log.info("Compiled a model proposal.")
    return outcome


def recheck_model_plan(
    plan: Plan,
    contracts: ActionContractRegistry,
    logger: Logger | None = None,
) -> ProposalRejection | None:
    """Check an already compiled plan again, exactly as it stands.

    Brain asks this when it offers a compiled plan for approval and again
    just before the approved plan runs, because an approval authorises a
    plan rather than freezing the registry that was current when the plan
    was shown. No proposal is parsed again, nothing is rebuilt, repaired,
    defaulted, or substituted, and no provider is asked anything: every step
    of the plan is checked, in order, as it is, against the closed allowlist,
    the model eligibility ``contracts`` declares now, the argument contract,
    and the text and path checks, in that order, and the first failure is
    returned, or None when every step still passes every gate.

    A plan holding a creation step is first checked against the owner's
    request again, with no clock: the request is parsed once more, and the
    plan must still hold exactly one creation step per requested item, in
    order, each carrying that item's body and, for a task, a due date
    exactly when the item names one, with no two items alike. Each step's
    canonical timing is then checked by its contract's format. Nothing is
    resolved again.

    A value that is not a well-formed model-originated plan, including one
    holding more steps than ``PLAN_MAX_STEPS``, or a registry that is not
    exactly an ``ActionContractRegistry``, raises ``ValueError`` with a
    fixed message. A refusal logs its code and nothing else.
    """
    if type(contracts) is not ActionContractRegistry:
        raise ValueError(COMPILER_CONTRACTS_MESSAGE)
    if not is_model_plan(plan):
        raise ValueError(COMPILER_PLAN_MESSAGE)

    outcome = _recheck(plan, contracts)
    if outcome is not None:
        (logger or get_logger(__name__)).warning(
            "Refused a compiled model plan: code=%s",
            outcome.code,
        )
    return outcome


def _recheck(
    plan: Plan,
    contracts: ActionContractRegistry,
) -> ProposalRejection | None:
    """Return the first gate a compiled plan no longer passes, or None."""
    steps = plan.steps
    if any(
        f"{step.tool_name}.{step.action}" in CREATION_ACTIONS for step in steps
    ):
        for step in steps:
            contract = _eligible_contract(step.tool_name, step.action, contracts)
            if type(contract) is ProposalRejection:
                return contract
        items = parse_creation_request(plan.original_request)
        if type(items) is RequestRefusal:
            return _REQUEST_REJECTIONS[items]
        if len(items) != len(steps):
            return ProposalRejection.CREATION_MISMATCH
        values: list[object] = []
        for step, item in zip(steps, items):
            kind = CREATION_ACTIONS.get(f"{step.tool_name}.{step.action}")
            arguments = step.arguments
            if kind is not item.kind or arguments.get("text") != item.body:
                return ProposalRejection.CREATION_MISMATCH
            if item.kind is CreationKind.TASK:
                if ("due" in arguments) != (item.phrase is not None):
                    return ProposalRejection.CREATION_MISMATCH
                values.append(arguments.get("due"))
            else:
                values.append(arguments.get("when"))
        if _has_duplicates(items, values):
            return ProposalRejection.CREATION_REQUEST_UNCLEAR

    for step in steps:
        outcome = _checked_step(
            step.tool_name,
            step.action,
            step.arguments,
            contracts,
        )
        if type(outcome) is ProposalRejection:
            return outcome
    return None


def _compile(
    request: str,
    text: object,
    contracts: ActionContractRegistry,
    timing: TimingContext | None = None,
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
    if _is_steps_envelope(proposal):
        actions = proposal["steps"]
        if not _is_step_list(actions):
            return ProposalRejection.MALFORMED_PLAN
    elif _is_proposal(proposal):
        actions = [proposal]
    else:
        return ProposalRejection.MALFORMED_PROPOSAL

    if any(
        f"{action['tool']}.{action['action']}" in CREATION_ACTIONS
        for action in actions
    ):
        return _compile_creation(request, actions, contracts, timing)

    # Every action passes every gate before any step is built, so a refusal
    # anywhere leaves nothing behind.
    checked: list[tuple[ActionContract, dict[str, object]]] = []
    for action in actions:
        arguments = action["arguments"]
        contract = _checked_step(
            action["tool"],
            action["action"],
            arguments,
            contracts,
        )
        if type(contract) is ProposalRejection:
            return contract
        checked.append((contract, arguments))

    return Plan(
        original_request=request,
        steps=tuple(
            PlanStep(contract.tool_name, contract.action, arguments)
            for contract, arguments in checked
        ),
        recording=RecordingPolicy.TRANSIENT,
        origin=PlanOrigin.MODEL,
    )


def _checked_step(
    tool_name: str,
    action: str,
    arguments: dict[str, object] | PlanArguments,
    contracts: ActionContractRegistry,
) -> ActionContract | ProposalRejection:
    """Return the contract of a step every gate admits, or the first refusal.

    The gates are the allowlist together with the contract's eligibility,
    the argument contract, and then the text and path checks on each present
    argument in declared order. ``compile_proposal`` and
    ``recheck_model_plan`` both apply exactly these, so a plan is checked
    again by the same gates that compiled it.
    """
    contract = _eligible_contract(tool_name, action, contracts)
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

    return contract


def _compile_creation(
    request: str,
    actions: list[dict[str, object]],
    contracts: ActionContractRegistry,
    timing: TimingContext | None,
) -> Plan | ProposalRejection:
    """Compile a proposal holding a creation step, from the owner's own words.

    The gates run in order, each over every step before the next begins,
    and the first failure decides the refusal: allowlist and eligibility;
    the raw checks of each creation step's arguments; the parse of the
    owner's request, then the positional binding; the resolution of each
    item's own timing phrase, then the duplicate rule; and finally the
    ordinary step checks on the canonical steps Maria builds. Every step of
    the plan comes from Maria's extraction of the request; the model's
    values are only compared with it.
    """
    eligible: list[tuple[ActionContract, dict[str, object]]] = []
    for action in actions:
        contract = _eligible_contract(action["tool"], action["action"], contracts)
        if type(contract) is ProposalRejection:
            return contract
        eligible.append((contract, action["arguments"]))

    for contract, arguments in eligible:
        inputs = _PROPOSAL_INPUTS.get(contract.identifier)
        if inputs is None:
            continue
        rejection = _raw_rejection(inputs, arguments)
        if rejection is not None:
            return rejection

    items = parse_creation_request(request)
    if type(items) is RequestRefusal:
        return _REQUEST_REJECTIONS[items]
    rejection = _binding_rejection(items, eligible)
    if rejection is not None:
        return rejection

    values: list[str | None] = []
    for item in items:
        value = _resolved_value(item, timing)
        if type(value) is ProposalRejection:
            return value
        values.append(value)
    if _has_duplicates(items, values):
        return ProposalRejection.CREATION_REQUEST_UNCLEAR

    steps: list[PlanStep] = []
    for (contract, _), item, value in zip(eligible, items, values):
        arguments = _canonical_arguments(item, value)
        checked = _checked_step(
            contract.tool_name,
            contract.action,
            arguments,
            contracts,
        )
        if type(checked) is ProposalRejection:
            return checked
        steps.append(PlanStep(checked.tool_name, checked.action, arguments))

    return Plan(
        original_request=request,
        steps=tuple(steps),
        recording=RecordingPolicy.TRANSIENT,
        origin=PlanOrigin.MODEL,
    )


def _raw_rejection(
    inputs: tuple[tuple[str, bool, str], ...],
    arguments: dict[str, object],
) -> ProposalRejection | None:
    """Return the first raw check one creation step's arguments fail, or None.

    A missing required argument is found first, in the table's order, then
    an argument the table does not name, then each argument's own checks in
    the table's order: type, size, unsafe characters, and, for a timing
    phrase, printable ASCII. The unsafe check runs before the ASCII check,
    so both refusals are reachable.
    """
    for name, required, _ in inputs:
        if required and name not in arguments:
            return ProposalRejection.MISSING_ARGUMENT
    declared = {name for name, _, _ in inputs}
    if any(name not in declared for name in arguments):
        return ProposalRejection.EXTRA_ARGUMENT
    for name, _, check in inputs:
        if name not in arguments:
            continue
        value = arguments[name]
        if type(value) is not str:
            return ProposalRejection.WRONG_TYPE
        if check == _NEVER_INPUT:
            if value != _REPEAT_NEVER:
                return ProposalRejection.OUT_OF_RANGE
            continue
        if check == _TEXT_INPUT:
            size = len(value.encode("utf-8", "surrogatepass"))
            if not 0 < size <= PROPOSAL_TEXT_MAX_BYTES or not value.strip():
                return ProposalRejection.OUT_OF_RANGE
            if _has_unsafe_character(value):
                return ProposalRejection.UNSAFE_TEXT
            continue
        if not 0 < len(value) <= PROPOSAL_PHRASE_MAX_LENGTH or (
            len(value.encode("utf-8", "surrogatepass"))
            > PROPOSAL_PHRASE_MAX_LENGTH
        ):
            return ProposalRejection.OUT_OF_RANGE
        if _has_unsafe_character(value):
            return ProposalRejection.UNSAFE_TEXT
        if not all(" " <= character <= "~" for character in value):
            return ProposalRejection.OUT_OF_RANGE
    return None


def _binding_rejection(
    items: tuple[CreationItem, ...],
    eligible: list[tuple[ActionContract, dict[str, object]]],
) -> ProposalRejection | None:
    """Refuse a proposal that is not exactly the requested items, in order.

    There must be one creation step per item, of the item's kind, whose
    ``text`` equals the item's body exactly and whose timing phrase equals
    the item's own after canonical tokenization, ``canonical_phrase``: ASCII
    letters lowercased and tokens joined by single spaces, digits as
    written. A task carries ``due`` exactly when its item names a date.
    Anything else, an extra, missing, reordered, or non-creation step
    included, is a mismatch.
    """
    if len(items) != len(eligible):
        return ProposalRejection.CREATION_MISMATCH
    for (contract, arguments), item in zip(eligible, items):
        if CREATION_ACTIONS.get(contract.identifier) is not item.kind:
            return ProposalRejection.CREATION_MISMATCH
        if arguments["text"] != item.body:
            return ProposalRejection.CREATION_MISMATCH
        phrase = arguments.get("due" if item.kind is CreationKind.TASK else "when")
        if (phrase is None) != (item.phrase is None):
            return ProposalRejection.CREATION_MISMATCH
        if phrase is not None and (
            canonical_phrase(phrase) != canonical_phrase(item.phrase)
        ):
            return ProposalRejection.CREATION_MISMATCH
    return None


def _resolved_value(
    item: CreationItem,
    timing: TimingContext | None,
) -> str | None | ProposalRejection:
    """Return one item's canonical timing, None when it has none, or a refusal.

    The item's own parsed phrase is resolved at the shared reference
    instant; an undated task needs no timing at all, and so no zone.
    """
    if item.kind is CreationKind.TASK and item.due is None:
        return None
    if timing is None:
        return ProposalRejection.TIME_ZONE_UNAVAILABLE
    if item.kind is CreationKind.TASK:
        resolved = resolve_task_date(item.due, timing)
        if type(resolved) is TimingFailure:
            return _TIMING_REJECTIONS[resolved]
        return iso_date_text(resolved)
    resolved = resolve_reminder_time(item.timing, timing)
    if type(resolved) is not ResolvedTime:
        return _TIMING_REJECTIONS[resolved]
    return resolved.canonical


def _has_duplicates(
    items: tuple[CreationItem, ...],
    values: list[object],
) -> bool:
    """Return whether two items share a kind, a body, and a resolved timing.

    Two undated tasks with the same body are duplicates too.
    """
    keys = [(item.kind, item.body, value) for item, value in zip(items, values)]
    return len(set(keys)) != len(keys)


def _canonical_arguments(item: CreationItem, value: str | None) -> dict[str, str]:
    """Return the arguments Maria builds for one item from her extraction."""
    if item.kind is CreationKind.TASK:
        if value is None:
            return {"text": item.body}
        return {"text": item.body, "due": value}
    return {"text": item.body, "when": value, "repeat": _REPEAT_NEVER}


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


def _is_steps_envelope(value: object) -> bool:
    """Return whether a parsed document is an object of exactly ``steps``.

    Such an object is a proposal for several actions however its list turns
    out, so a malformed list is refused as a malformed plan rather than as a
    malformed single action.
    """
    return type(value) is dict and set(value) == STEPS_ENVELOPE_FIELDS


def _is_step_list(value: object) -> bool:
    """Return whether an envelope's ``steps`` is a well-formed action list.

    That is an exact list of ``MODEL_PROPOSAL_MIN_STEPS`` to
    ``PLAN_MAX_STEPS`` items, each exactly the shape of a proposal for one
    action. A nested list, a nested envelope, or any other field is
    refused, so no step can hold another step.
    """
    return (
        type(value) is list
        and MODEL_PROPOSAL_MIN_STEPS <= len(value) <= PLAN_MAX_STEPS
        and all(_is_proposal(item) for item in value)
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
