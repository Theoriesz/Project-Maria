"""Declarative per-action argument contracts for Maria's built-in tools.

This module is the positive allowlist of every tool action Maria supports and
of the arguments each one accepts. It is declared by hand rather than derived
from the tools that happen to be loaded, because deriving it would inherit
ToolManager's membership-only view and PermissionManager's default-allow
property, and neither of those is argument validation.

The registry decides nothing about permission and performs no tool work. It
answers one question: is this exact tool action supported, and are these exact
arguments the ones it declares? Validation rejects; it never coerces, parses,
truncates, clamps, inserts a default, or repairs. A path argument declares the
named root class it belongs to, which is classification metadata only: no path
is resolved and the filesystem is never touched here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any


# The exact value types PlanArguments admits, declared here rather than
# imported so the registry stays independent of the planner.
SUPPORTED_VALUE_TYPES = (str, int)

# The inclusive window the memory layer already treats as a listing limit's
# public boundary. Declaring it means an out-of-range limit is refused here
# instead of being silently clamped further down.
MINIMUM_LIST_LIMIT = 1
MAXIMUM_LIST_LIMIT = 100

# The calculator's established expression ceiling, measured exactly as
# CalculatorTool measures it: the tool strips the expression before handing it
# to evaluate_expression, which rejects a stripped length above this value.
MAXIMUM_EXPRESSION_LENGTH = 200

# The length of a hexadecimal SHA-256, the fact identity an enable approval
# carries.
FACT_DIGEST_LENGTH = 64


# The longest metadata name Maria will repeat back. Every declared tool,
# action, and field name is far shorter, so anything longer is a caller's
# payload rather than a name.
MAXIMUM_LABEL_LENGTH = 64

UNKNOWN_TOOL_LABEL = "unknown_tool"
UNKNOWN_ACTION_LABEL = "unknown_action"
UNSUPPORTED_FIELD_LABEL = "unsupported_field"
ARGUMENTS_LABEL = "arguments"


def _is_mapping(value: Any) -> bool:
    """Return whether a value really is a Mapping.

    The check goes through ``type(value)`` rather than ``isinstance``, so an
    object whose instance-level ``__class__`` claims to be a ``dict`` cannot
    talk its way past the gate. Genuine mappings still qualify, including
    ``dict``, ``MappingProxyType``, ``PlanArguments``, and any real
    ``Mapping`` subclass or registered implementation.
    """
    return issubclass(type(value), Mapping)


def _is_safe_label(value: Any) -> bool:
    """Return whether a name is safe to repeat back to the owner.

    A name qualifies only when it is an exact str that reads as a plain
    ASCII identifier: non-empty, short, and free of whitespace, control
    characters, path separators, and anything else that could carry a
    caller's content into a response or a log line. The value is inspected,
    never converted with ``str()`` or ``repr()``, so a side-effecting
    ``__str__`` is never reached.
    """
    return (
        type(value) is str
        and bool(value)
        and len(value) <= MAXIMUM_LABEL_LENGTH
        and value.isascii()
        and value.isidentifier()
    )


def _safe_label(value: Any, fallback: str) -> str:
    """Return a safe name unchanged, or the fixed fallback in its place.

    An unsafe value is replaced outright rather than escaped or truncated,
    so no fragment of the original survives into a message or a log record.
    """
    return value if _is_safe_label(value) else fallback


def _safe_component(value: Any, fallback: str, *, named: bool) -> str | None:
    """Return a safe component, keeping an unnamed one absent.

    A failure kind whose message names the component always gets a usable
    label, so a missing one reads as the fixed fallback rather than as the
    word ``None``. A kind whose message does not name it leaves it absent.
    """
    if value is None and not named:
        return None
    return _safe_label(value, fallback)


class RootClass(str, Enum):
    """Named root class a path argument is understood to belong to."""

    NAMED_FILESYSTEM_ROOT = "named_filesystem_root"


class ContractFailure(str, Enum):
    """Why one action or one argument failed contract validation."""

    UNKNOWN_TOOL = "unknown_tool"
    UNKNOWN_ACTION = "unknown_action"
    MISSING_REQUIRED_ARGUMENT = "missing_required_argument"
    EXTRA_ARGUMENT = "extra_argument"
    WRONG_TYPE = "wrong_type"
    OUT_OF_RANGE = "out_of_range"


_FAILURE_TEMPLATES: Mapping[ContractFailure, str] = MappingProxyType(
    {
        ContractFailure.UNKNOWN_TOOL: (
            "Unsupported action: {tool_name} is not a supported tool."
        ),
        ContractFailure.UNKNOWN_ACTION: (
            "Unsupported action: {tool_name}.{action} is not a supported action."
        ),
        ContractFailure.MISSING_REQUIRED_ARGUMENT: (
            "Missing required argument for {tool_name}.{action}: {field}."
        ),
        ContractFailure.EXTRA_ARGUMENT: (
            "Unsupported argument for {tool_name}.{action}: {field}."
        ),
        ContractFailure.WRONG_TYPE: (
            "Invalid argument type for {tool_name}.{action}: {field}."
        ),
        ContractFailure.OUT_OF_RANGE: (
            "Argument out of supported range for {tool_name}.{action}: {field}."
        ),
    }
)


# Which failure kinds interpolate an action and a field into their message.
# A kind that names a component must always receive a usable label.
_KINDS_NAMING_ACTION = frozenset(
    {
        ContractFailure.UNKNOWN_ACTION,
        ContractFailure.MISSING_REQUIRED_ARGUMENT,
        ContractFailure.EXTRA_ARGUMENT,
        ContractFailure.WRONG_TYPE,
        ContractFailure.OUT_OF_RANGE,
    }
)
_KINDS_NAMING_FIELD = frozenset(
    {
        ContractFailure.MISSING_REQUIRED_ARGUMENT,
        ContractFailure.EXTRA_ARGUMENT,
        ContractFailure.WRONG_TYPE,
        ContractFailure.OUT_OF_RANGE,
    }
)


@dataclass(frozen=True)
class ContractViolation:
    """One deterministic contract failure, carrying no submitted value.

    Only the failure kind and the names involved are retained. The offending
    value, its repr, the arguments mapping, and the plan that carried it are
    deliberately absent, so a violation can be displayed and logged without
    leaking note bodies, memory facts, expressions, or host paths.
    """

    kind: ContractFailure
    tool_name: str
    action: str | None = None
    field: str | None = None

    def __post_init__(self) -> None:
        """Settle the kind, then replace any unsafe name before storing it.

        The kind is checked by exact type before anything hashes, compares,
        or formats it, so a plain string that merely equals a member's value
        cannot impersonate one. Name substitution then happens here rather
        than at each call site, so no construction path can retain a
        caller's raw tool, action, or field name, and the object a caller
        inspects never holds one either.
        """
        if type(self.kind) is not ContractFailure:
            raise ValueError(
                "A contract violation kind must be a ContractFailure member."
            )

        object.__setattr__(
            self,
            "tool_name",
            _safe_label(self.tool_name, UNKNOWN_TOOL_LABEL),
        )
        object.__setattr__(
            self,
            "action",
            _safe_component(
                self.action,
                UNKNOWN_ACTION_LABEL,
                named=self.kind in _KINDS_NAMING_ACTION,
            ),
        )
        object.__setattr__(
            self,
            "field",
            _safe_component(
                self.field,
                UNSUPPORTED_FIELD_LABEL,
                named=self.kind in _KINDS_NAMING_FIELD,
            ),
        )

    @property
    def message(self) -> str:
        """Return the fixed owner-visible refusal for this failure."""
        return _FAILURE_TEMPLATES[self.kind].format(
            tool_name=self.tool_name,
            action=self.action,
            field=self.field,
        )


@dataclass(frozen=True)
class ArgumentContract:
    """One argument field's declared type, bounds, and root class.

    ``minimum`` and ``maximum`` are the numeric bounds of an int field, and
    ``allow_blank``, ``minimum_length``, and ``maximum_length`` are the text
    bounds of a str field. A ``None`` bound declares explicitly that the
    established domain has no such bound rather than leaving the question
    open, and ``allow_blank`` is mandatory on a str field so a blank policy
    can never be omitted by accident. Metadata that cannot apply to the
    declared type is rejected at construction rather than ignored or
    normalized away.
    """

    name: str
    value_type: type
    required: bool
    minimum: int | None = None
    maximum: int | None = None
    minimum_length: int | None = None
    maximum_length: int | None = None
    allow_blank: bool | None = None
    root_class: RootClass | None = None

    def __post_init__(self) -> None:
        """Reject a declaration the validator could not honour."""
        self._validate_identity()
        self._validate_bound_types()
        if self.value_type is str:
            self._validate_text_declaration()
        else:
            self._validate_numeric_declaration()

    def _validate_identity(self) -> None:
        """Reject an unusable name, requiredness, type, or root class."""
        if not _is_safe_label(self.name):
            raise ValueError(
                "An argument name must be a plain ASCII identifier no longer "
                f"than {MAXIMUM_LABEL_LENGTH} characters."
            )
        if type(self.required) is not bool:
            raise ValueError(
                f"required must be an exact bool: {self.name!r}."
            )
        if not any(
            self.value_type is supported for supported in SUPPORTED_VALUE_TYPES
        ):
            raise ValueError(
                f"Unsupported declared argument type for {self.name!r}."
            )
        if self.root_class is not None and type(self.root_class) is not RootClass:
            raise ValueError(
                f"root_class must be a RootClass member: {self.name!r}."
            )

    def _validate_bound_types(self) -> None:
        """Reject a bound that is not an exact int, rejecting bool too."""
        for label, bound in (
            ("minimum", self.minimum),
            ("maximum", self.maximum),
            ("minimum_length", self.minimum_length),
            ("maximum_length", self.maximum_length),
        ):
            if bound is not None and type(bound) is not int:
                raise ValueError(
                    f"{label} must be an exact int or None: {self.name!r}."
                )

    def _validate_text_declaration(self) -> None:
        """Reject text metadata a str field could not be measured against."""
        if self.minimum is not None or self.maximum is not None:
            raise ValueError(
                "Numeric bounds are only meaningful for an int argument: "
                f"{self.name!r}."
            )
        if self.allow_blank is None:
            raise ValueError(
                "A str argument must declare its blank policy explicitly: "
                f"{self.name!r}."
            )
        if type(self.allow_blank) is not bool:
            raise ValueError(
                f"allow_blank must be an exact bool: {self.name!r}."
            )
        for label, length in (
            ("minimum_length", self.minimum_length),
            ("maximum_length", self.maximum_length),
        ):
            if length is not None and length < 0:
                raise ValueError(
                    f"A negative {label} is not a usable bound: {self.name!r}."
                )
        if (
            self.minimum_length is not None
            and self.maximum_length is not None
            and self.minimum_length > self.maximum_length
        ):
            raise ValueError(
                f"minimum_length may not exceed maximum_length: {self.name!r}."
            )

    def _validate_numeric_declaration(self) -> None:
        """Reject text or path metadata that cannot apply to an int field."""
        if self.minimum_length is not None or self.maximum_length is not None:
            raise ValueError(
                "Length bounds are only meaningful for a str argument: "
                f"{self.name!r}."
            )
        if self.allow_blank is not None:
            raise ValueError(
                "A blank policy is only meaningful for a str argument: "
                f"{self.name!r}."
            )
        if self.root_class is not None:
            raise ValueError(
                f"Only a str argument may declare a path root class: {self.name!r}."
            )
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError(
                f"minimum may not exceed maximum: {self.name!r}."
            )

    @property
    def is_path(self) -> bool:
        """Return whether this field is classified as a path argument."""
        return self.root_class is not None

    def failure_for(self, value: Any) -> ContractFailure | None:
        """Return why a present value fails, or None when it satisfies this field.

        The exact type is settled before any text operation, so bool never
        satisfies an int field and no object is accepted, or converted, merely
        because it could become text: ``str(value)`` is never called, so a
        side-effecting ``__str__`` is never reached. A str value is then
        measured on its stripped form, which is how every tool establishes
        blankness and exactly how CalculatorTool measures its expression
        ceiling. That stripped text is used for the comparison only and is
        never written back, so the caller keeps the value it supplied.
        """
        if type(value) is not self.value_type:
            return ContractFailure.WRONG_TYPE

        if self.value_type is str:
            measured = value.strip()
            if not self.allow_blank and not measured:
                return ContractFailure.OUT_OF_RANGE
            if (
                self.minimum_length is not None
                and len(measured) < self.minimum_length
            ):
                return ContractFailure.OUT_OF_RANGE
            if (
                self.maximum_length is not None
                and len(measured) > self.maximum_length
            ):
                return ContractFailure.OUT_OF_RANGE
            return None

        if self.minimum is not None and value < self.minimum:
            return ContractFailure.OUT_OF_RANGE
        if self.maximum is not None and value > self.maximum:
            return ContractFailure.OUT_OF_RANGE
        return None


@dataclass(frozen=True)
class ActionContract:
    """Every argument one exact tool action declares.

    Field order is the declared order and validation reports failures in it,
    so the first reported defect of a malformed call never depends on mapping
    iteration order or on hash randomization.
    """

    tool_name: str
    action: str
    arguments: tuple[ArgumentContract, ...] = ()

    def __post_init__(self) -> None:
        """Snapshot the declarations and reject an unusable one.

        The tool and action names are settled first, before anything reads
        ``identifier``, so a name that could not be looked up safely or
        displayed safely never reaches a registry. The supplied declarations
        are then copied into a tuple that nothing else holds, so a caller
        keeping a list it passed in cannot change this contract, or the
        registry holding it, after construction.
        """
        for label, name in (("tool name", self.tool_name), ("action", self.action)):
            if not _is_safe_label(name):
                raise ValueError(
                    f"A declared {label} must be a plain ASCII identifier no "
                    f"longer than {MAXIMUM_LABEL_LENGTH} characters."
                )

        try:
            snapshot = tuple(self.arguments)
        except TypeError as exc:
            raise ValueError(
                f"Arguments for {self.identifier} must be iterable."
            ) from exc

        seen: set[str] = set()
        for argument in snapshot:
            if type(argument) is not ArgumentContract:
                raise ValueError(
                    f"Arguments for {self.identifier} must each be an exact "
                    "ArgumentContract."
                )
            if argument.name in seen:
                raise ValueError(
                    f"Duplicate argument declared for {self.identifier}: "
                    f"{argument.name}."
                )
            seen.add(argument.name)
        object.__setattr__(self, "arguments", snapshot)

    @property
    def identifier(self) -> str:
        """Return the canonical tool.action identifier."""
        return f"{self.tool_name}.{self.action}"

    @property
    def field_names(self) -> frozenset[str]:
        """Return every declared field name."""
        return frozenset(argument.name for argument in self.arguments)

    def validate(self, arguments: Mapping[str, Any]) -> ContractViolation | None:
        """Return the first deterministic violation, or None when valid.

        The order is fixed: the container itself, then missing required
        fields in declared order, then key safety, then extra fields in
        sorted order, then each present field in declared order with its
        type checked before its bounds. Nothing is added to, removed from,
        or rewritten in the supplied mapping.

        A container that is not really a Mapping cannot be inspected for
        fields at all, so it is refused immediately. The keys are then
        enumerated once and split into the exact-str names and a note that
        something else was present. Every later question is asked of that
        name set rather than of the caller's mapping, so a hostile key is
        never hashed against a declared name, never compared, and never
        converted: a key whose ``__eq__`` raises or whose equality collides
        with a required field cannot influence the outcome or escape as an
        exception. Enumerating the keys and reading a declared value are
        each guarded, so a mapping that fails part-way still produces a
        controlled refusal. ``MemoryError`` is re-raised rather than
        answered, because exhausting memory says nothing about the
        arguments and must not be reported as though it did; the other
        ``BaseException`` failures propagate for the same reason without
        needing a handler.
        """
        if not _is_mapping(arguments):
            return self._violation(ContractFailure.WRONG_TYPE, ARGUMENTS_LABEL)

        try:
            submitted = tuple(arguments)
        except MemoryError:
            raise
        except Exception:  # noqa: BLE001 - a hostile mapping must not escape.
            return self._violation(ContractFailure.WRONG_TYPE, ARGUMENTS_LABEL)

        names: set[str] = set()
        holds_unusable_key = False
        for key in submitted:
            if type(key) is str:
                names.add(key)
            else:
                holds_unusable_key = True

        for argument in self.arguments:
            if argument.required and argument.name not in names:
                return self._violation(
                    ContractFailure.MISSING_REQUIRED_ARGUMENT,
                    argument.name,
                )

        if holds_unusable_key:
            return self._violation(
                ContractFailure.WRONG_TYPE,
                ARGUMENTS_LABEL,
            )

        declared = self.field_names
        for name in sorted(names):
            if name not in declared:
                return self._violation(ContractFailure.EXTRA_ARGUMENT, name)

        for argument in self.arguments:
            if argument.name not in names:
                continue
            try:
                value = arguments[argument.name]
            except MemoryError:
                raise
            except Exception:  # noqa: BLE001 - a hostile mapping must not escape.
                return self._violation(
                    ContractFailure.WRONG_TYPE,
                    ARGUMENTS_LABEL,
                )
            failure = argument.failure_for(value)
            if failure is not None:
                return self._violation(failure, argument.name)

        return None

    def _violation(self, kind: ContractFailure, field: str) -> ContractViolation:
        """Build a violation naming this action and one of its fields."""
        return ContractViolation(kind, self.tool_name, self.action, field)


class ActionContractRegistry:
    """An immutable, explicitly declared table of action contracts.

    The table is fixed at construction: a duplicate tool action is rejected
    rather than silently replacing the earlier declaration, which a plain
    dictionary literal would do. The built table is stored behind a read-only
    mapping and the dictionary behind it is never retained anywhere else, so
    no contract can be added, replaced, or removed after construction.
    """

    __slots__ = ("_contracts", "_tool_names")

    def __init__(self, contracts: Iterable[ActionContract]) -> None:
        declared: dict[tuple[str, str], ActionContract] = {}
        for contract in contracts:
            if type(contract) is not ActionContract:
                raise ValueError(
                    "Every registry declaration must be an exact ActionContract."
                )
            key = (contract.tool_name, contract.action)
            if key in declared:
                raise ValueError(
                    f"Duplicate action contract declared: {contract.identifier}."
                )
            declared[key] = contract
        object.__setattr__(self, "_contracts", MappingProxyType(declared))
        object.__setattr__(
            self,
            "_tool_names",
            frozenset(tool_name for tool_name, _ in declared),
        )

    def __setattr__(self, name: str, value: Any) -> None:
        """Refuse attribute assignment, as a frozen dataclass does."""
        raise AttributeError(f"{type(self).__name__} is immutable.")

    def __delattr__(self, name: str) -> None:
        """Refuse attribute deletion, as a frozen dataclass does."""
        raise AttributeError(f"{type(self).__name__} is immutable.")

    @property
    def tool_names(self) -> frozenset[str]:
        """Return every declared tool name."""
        return self._tool_names

    @property
    def action_identifiers(self) -> frozenset[str]:
        """Return every declared tool.action identifier."""
        return frozenset(
            contract.identifier for contract in self._contracts.values()
        )

    @property
    def contracts(self) -> tuple[ActionContract, ...]:
        """Return every declared contract in declaration order."""
        return tuple(self._contracts.values())

    def contract_for(self, tool_name: str, action: str) -> ActionContract | None:
        """Return the contract for one tool action, or None when undeclared.

        An identifier that is not an exact str can never name a declaration,
        so it answers None rather than being hashed or compared. That keeps
        an unhashable object from raising here and keeps a str subclass or a
        hash-and-equality impersonation from resolving to a real contract.
        """
        if type(tool_name) is not str or type(action) is not str:
            return None
        return self._contracts.get((tool_name, action))

    def validate(
        self,
        tool_name: str,
        action: str,
        arguments: Mapping[str, Any],
    ) -> ContractViolation | None:
        """Return the first violation for one call, or None when it is valid.

        The identifiers are settled by exact type before anything else, so
        no caller-defined ``__hash__``, ``__eq__``, ``__str__``, or
        ``__repr__`` runs: an unhashable identifier answers with a controlled
        refusal instead of raising, and neither a str subclass nor an object
        whose hash and equality impersonate a registered name can match a
        declaration. A malformed tool name is answered without the action
        being inspected at all.

        An undeclared tool is reported before an undeclared action, so a
        caller naming nothing Maria supports is told the tool is unknown
        rather than being told about an action on a tool that does not exist.
        """
        if type(tool_name) is not str:
            return ContractViolation(ContractFailure.UNKNOWN_TOOL, tool_name)

        if tool_name not in self._tool_names:
            return ContractViolation(ContractFailure.UNKNOWN_TOOL, tool_name)

        if type(action) is not str:
            return ContractViolation(
                ContractFailure.UNKNOWN_ACTION,
                tool_name,
                action,
            )

        contract = self._contracts.get((tool_name, action))
        if contract is None:
            return ContractViolation(
                ContractFailure.UNKNOWN_ACTION,
                tool_name,
                action,
            )
        return contract.validate(arguments)


def _identifier(required: bool = True) -> ArgumentContract:
    """Declare the positive row identifier shared by nine lifecycle actions.

    Every tool that takes an ``id`` rejects a bool, a non-int, and anything
    below 1, and none imposes a ceiling.
    """
    return ArgumentContract(
        "id",
        int,
        required=required,
        minimum=1,
        maximum=None,
    )


def _listing_limit() -> ArgumentContract:
    """Declare the optional listing limit shared by three listing actions.

    The window is the memory layer's established public boundary. A value
    outside it is malformed and refused here rather than clamped further down.
    """
    return ArgumentContract(
        "limit",
        int,
        required=False,
        minimum=MINIMUM_LIST_LIMIT,
        maximum=MAXIMUM_LIST_LIMIT,
    )


def _text(
    name: str,
    *,
    required: bool,
    allow_blank: bool,
    minimum_length: int | None,
    maximum_length: int | None,
) -> ArgumentContract:
    """Declare one text field, stating its whole bound policy explicitly.

    Every parameter is keyword-only and has no default, so a declaration
    cannot inherit a blank or length policy it did not state.
    """
    return ArgumentContract(
        name,
        str,
        required=required,
        allow_blank=allow_blank,
        minimum_length=minimum_length,
        maximum_length=maximum_length,
    )


def _named_root_path(name: str, required: bool = True) -> ArgumentContract:
    """Declare a path argument governed by Maria's named filesystem roots.

    FilesystemTool raises ``Path is required.`` for text that is empty after
    stripping, so a blank path is malformed for every path field, and no path
    field has an established length ceiling. The root class is classification
    metadata and stays that: nothing here creates a ``Path``, resolves one,
    selects a root, reads the filesystem, consults configuration, or decides
    permission. Which named root a path lands in, and whether it is reachable
    at all, are settled later by ``FilesystemRoots`` and ``FilesystemTool``.
    """
    return ArgumentContract(
        name,
        str,
        required=required,
        allow_blank=False,
        minimum_length=None,
        maximum_length=None,
        root_class=RootClass.NAMED_FILESYSTEM_ROOT,
    )


BUILT_IN_ACTION_CONTRACTS = ActionContractRegistry(
    (
        ActionContract(
            "calculator",
            "calculate",
            (
                _text(
                    "expression",
                    required=True,
                    allow_blank=False,
                    minimum_length=None,
                    maximum_length=MAXIMUM_EXPRESSION_LENGTH,
                ),
            ),
        ),
        ActionContract("filesystem", "read", (_named_root_path("path"),)),
        ActionContract(
            "filesystem",
            "list",
            (_named_root_path("path", required=False),),
        ),
        ActionContract(
            "filesystem",
            "rename",
            (_named_root_path("source"), _named_root_path("destination")),
        ),
        ActionContract(
            "filesystem",
            "move",
            (_named_root_path("source"), _named_root_path("destination")),
        ),
        ActionContract("history", "list"),
        ActionContract(
            "history",
            "search",
            (
                _text(
                    "query",
                    required=True,
                    allow_blank=False,
                    minimum_length=None,
                    maximum_length=None,
                ),
            ),
        ),
        ActionContract("history", "read", (_identifier(),)),
        ActionContract("history", "delete", (_identifier(),)),
        ActionContract(
            "memory",
            "store",
            (
                _text(
                    "fact",
                    required=True,
                    allow_blank=False,
                    minimum_length=None,
                    maximum_length=None,
                ),
            ),
        ),
        ActionContract(
            "memory",
            "search",
            (
                _text(
                    "query",
                    required=True,
                    allow_blank=False,
                    minimum_length=None,
                    maximum_length=None,
                ),
                _listing_limit(),
            ),
        ),
        ActionContract("memory", "list", (_listing_limit(),)),
        ActionContract(
            "memory",
            "update",
            (
                _identifier(),
                _text(
                    "fact",
                    required=True,
                    allow_blank=False,
                    minimum_length=None,
                    maximum_length=None,
                ),
            ),
        ),
        ActionContract("memory", "forget", (_identifier(),)),
        ActionContract(
            "memory",
            "enable_context",
            (
                _identifier(),
                # Absent from the planner's plan; pinned when confirmation is
                # offered, so the approved plan names the exact fact text.
                _text(
                    "fact_digest",
                    required=False,
                    allow_blank=False,
                    minimum_length=FACT_DIGEST_LENGTH,
                    maximum_length=FACT_DIGEST_LENGTH,
                ),
            ),
        ),
        ActionContract("memory", "disable_context", (_identifier(),)),
        ActionContract(
            "notes",
            "create",
            (
                _text(
                    "title",
                    required=False,
                    allow_blank=False,
                    minimum_length=None,
                    maximum_length=None,
                ),
                _text(
                    "body",
                    required=True,
                    allow_blank=False,
                    minimum_length=None,
                    maximum_length=None,
                ),
            ),
        ),
        ActionContract("notes", "list", (_listing_limit(),)),
        ActionContract(
            "notes",
            "read",
            (
                _text(
                    "target",
                    required=True,
                    allow_blank=False,
                    minimum_length=None,
                    maximum_length=None,
                ),
            ),
        ),
        ActionContract(
            "notes",
            "retitle",
            (
                _identifier(),
                _text(
                    "title",
                    required=True,
                    allow_blank=False,
                    minimum_length=None,
                    maximum_length=None,
                ),
            ),
        ),
        ActionContract(
            "notes",
            "edit",
            (
                _identifier(),
                _text(
                    "body",
                    required=True,
                    allow_blank=False,
                    minimum_length=None,
                    maximum_length=None,
                ),
            ),
        ),
        ActionContract("notes", "delete", (_identifier(),)),
        ActionContract("system", "overview"),
        ActionContract("system", "current_time"),
    )
)
