"""Request planning for Maria.

The planner is intentionally conservative. It maps explicit user
commands to a small set of supported tool actions and returns no plan when the
request is outside Maria's supported command set.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, fields as dataclass_fields
from enum import Enum
from itertools import islice
from logging import Logger
from types import MappingProxyType
from typing import Any

from core.logger import get_logger


SUPPORTED_ARGUMENT_VALUE_TYPES = (str, int)


class PlanArguments(Mapping[str, Any]):
    """An immutable, hashable, read-only mapping of plan-step arguments.

    Keys are strings and values are strings or integers, which is exactly
    the domain the deterministic planner emits. Both supported value types
    are themselves immutable, so a validated snapshot is deeply immutable
    without any recursive copying: there is no nested container to reach
    through. Any other key or value type is rejected at construction
    instead of being coerced, so an unsupported argument fails loudly
    rather than becoming an unhashable or mutable member of a plan. The
    input itself must be a Mapping: no other container, and no missing
    value, is quietly treated as empty arguments. Pass an empty mapping to
    build an argumentless step.

    The validated snapshot is stored as a read-only MappingProxyType and
    the dictionary behind it is never retained anywhere else, so there is
    no reachable mutable storage: content cannot be changed through this
    object at all, and the hash computed once at construction can never
    go stale.
    """

    __slots__ = ("_values", "_hash")

    def __init__(self, values: Mapping[str, Any]) -> None:
        if not isinstance(values, Mapping):
            raise TypeError("Plan arguments must be a mapping.")
        snapshot: dict[str, Any] = {}
        for key, value in values.items():
            if type(key) is not str:
                raise TypeError(
                    "Plan argument keys must be str, got "
                    f"{type(key).__name__}."
                )
            if type(value) not in SUPPORTED_ARGUMENT_VALUE_TYPES:
                raise TypeError(
                    f"Unsupported plan argument value type for {key!r}: "
                    f"{type(value).__name__}."
                )
            snapshot[key] = value
        object.__setattr__(self, "_values", MappingProxyType(snapshot))
        object.__setattr__(self, "_hash", hash(frozenset(snapshot.items())))

    def __getitem__(self, key: str) -> Any:
        """Return the value stored for a key."""
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate over argument names."""
        return iter(self._values)

    def __len__(self) -> int:
        """Return how many arguments the step carries."""
        return len(self._values)

    def __hash__(self) -> int:
        """Return a structural hash that ignores key insertion order."""
        return self._hash

    def __repr__(self) -> str:
        """Return a representation matching the underlying mapping."""
        return f"{type(self).__name__}({dict(self._values)!r})"

    def __setattr__(self, name: str, value: Any) -> None:
        """Refuse attribute assignment, as a frozen dataclass does."""
        raise AttributeError(f"{type(self).__name__} is immutable.")

    def __delattr__(self, name: str) -> None:
        """Refuse attribute deletion, as a frozen dataclass does."""
        raise AttributeError(f"{type(self).__name__} is immutable.")


@dataclass(frozen=True)
class PlanStep:
    """One executable step in a Maria plan.

    The declared argument type is the mapping a caller may supply. On
    construction it is normalized into an immutable PlanArguments snapshot,
    so later mutation of the supplied mapping cannot alter the step, and no
    accessor hands back the caller's original object.
    """

    tool_name: str
    action: str
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        """Replace the supplied arguments with an exact immutable snapshot.

        The snapshot is always rebuilt, never reused because the supplied
        object already looks like a PlanArguments. A subclass can satisfy
        that check while backing its Mapping behavior with mutable state,
        so reusing it would let a caller change a constructed step.
        """
        object.__setattr__(self, "arguments", PlanArguments(self.arguments))


class RecordingPolicy(str, Enum):
    """Whether Brain records a plan's interaction row."""

    RECORD = "record"
    TRANSIENT = "transient"


class PlanOrigin(str, Enum):
    """Who produced a plan's steps.

    DETERMINISTIC is every plan Maria's own code builds, the planner's
    included. MODEL is set only by ``core/plan_compiler.py``, on a plan
    compiled from one reasoning-model proposal, and it is what makes Brain
    require the owner's approval whatever PermissionManager decides.
    """

    DETERMINISTIC = "deterministic"
    MODEL = "model"


# The most steps any plan may hold, whatever produced it. A plan is one flat,
# ordered sequence of steps: a step holds only a tool name, an action name,
# and text or whole-number arguments, so no step can hold a plan or another
# step, and nothing a step returns can add one. That is the whole of a plan's
# depth.
PLAN_MAX_STEPS = 5

PLAN_STEP_LIMIT_MESSAGE = f"A plan may hold at most {PLAN_MAX_STEPS} steps."

MODEL_PLAN_SHAPE_MESSAGE = (
    "A model-originated plan must be transient, with 1 to "
    f"{PLAN_MAX_STEPS} steps and no canned response."
)


@dataclass(frozen=True)
class Plan:
    """An executable plan created from user input.

    The recording policy defaults to RECORD, which is how every interaction
    has always been recorded. TRANSIENT is a positive choice a plan's
    producer makes explicitly, and it keeps the whole interaction row out of
    history: the request as well as the response.

    No plan holds more than ``PLAN_MAX_STEPS`` steps. The origin defaults to
    DETERMINISTIC. A MODEL plan always has exact text as its request, 1 to
    ``PLAN_MAX_STEPS`` steps in an exact tuple, no canned response, and the
    TRANSIENT policy, and no other shape can be constructed with that origin.

    Every field is set on the plan itself when it is built. The boundaries
    that route, confirm, or run a plan read its fields from the plan itself,
    through ``own_origin`` and ``plan_snapshot``, so a field deleted later is
    missing there rather than read as its class default.
    """

    original_request: str
    steps: tuple[PlanStep, ...]
    response_if_no_action: str | None = None
    recording: RecordingPolicy = RecordingPolicy.RECORD
    origin: PlanOrigin = PlanOrigin.DETERMINISTIC

    def __post_init__(self) -> None:
        """Refuse a wrong type, a bad MODEL shape, or too many steps.

        Both type checks are on the exact type, so neither a plain string
        nor an object claiming the class through ``__class__`` passes. A
        string would compare and hash equal to the member it spells while
        failing every identity check made against that member, so it is
        refused rather than coerced. A model-originated plan of any shape but
        its own is refused as well, so ``dataclasses.replace`` cannot produce
        one either. Any other plan is refused when reading its steps finds
        more than ``PLAN_MAX_STEPS``: they are read, at most one past the
        bound, rather than counted by the length their container reports, so
        a container that understates its length is refused too. The
        container itself is kept as it is, and steps that cannot be read,
        such as one-shot steps that reading would consume, are not refused
        here; the steps are otherwise exactly as free as they always were.
        Construction is not the only check: the boundaries that confirm or
        run a plan read its steps again.
        """
        if type(self.recording) is not RecordingPolicy:
            raise TypeError("Plan recording policy must be a RecordingPolicy.")
        if type(self.origin) is not PlanOrigin:
            raise TypeError("Plan origin must be a PlanOrigin.")
        if self.origin is PlanOrigin.MODEL:
            if not is_model_plan(self):
                raise ValueError(MODEL_PLAN_SHAPE_MESSAGE)
        else:
            read = _read_steps(self.steps)
            if read is not None and len(read) > PLAN_MAX_STEPS:
                raise ValueError(PLAN_STEP_LIMIT_MESSAGE)

    @property
    def fingerprint(self) -> str:
        """Return a stable lowercase SHA-256 digest of this plan.

        The digest covers the plan's complete logical content and is
        deterministic across processes, so a plan shown to the owner can
        later be matched against the plan about to run. Only the digest is
        exposed: the content it covers can include note bodies, memory
        facts, and file paths, so the serialized form stays private and is
        never logged.
        """
        return hashlib.sha256(self._canonical_content()).hexdigest()

    def _canonical_content(self) -> bytes:
        """Serialize the plan's logical content canonically as UTF-8 bytes.

        JSON keeps the supported argument types distinguishable, because a
        string value never serializes like an integer one. Mapping keys are
        sorted and separators are fixed so that structurally equal plans
        produce identical bytes whatever order their arguments were built
        in, while step order is preserved because steps stay a list.
        Escaping stays ASCII-only so that the payload always encodes.

        The recording policy and the origin each join the payload only when
        they are not the default. Every default plan therefore keeps exactly
        the bytes, and the fingerprint, it had before either existed, while
        an otherwise identical transient or model-originated plan
        fingerprints differently.
        """
        payload: dict[str, Any] = {
            "original_request": self.original_request,
            "response_if_no_action": self.response_if_no_action,
            "steps": [
                {
                    "tool_name": step.tool_name,
                    "action": step.action,
                    "arguments": dict(step.arguments),
                }
                for step in self.steps
            ],
        }
        if self.recording is not RecordingPolicy.RECORD:
            payload["recording"] = self.recording.value
        if self.origin is not PlanOrigin.DETERMINISTIC:
            payload["origin"] = self.origin.value
        return json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")


# Every field a plan holds itself, each set on it when it is built, read from
# the dataclass so a field added later is required and copied too.
_PLAN_FIELDS = tuple(field.name for field in dataclass_fields(Plan))


def is_model_plan(plan: object) -> bool:
    """Return whether a value is exactly a well-formed model-originated plan.

    That is an exact ``Plan`` whose origin is MODEL and whose policy is
    TRANSIENT, with exact text as its request, no canned response, and an
    exact tuple of 1 to ``PLAN_MAX_STEPS`` exact ``PlanStep`` values, each
    named by exact text and holding its immutable ``PlanArguments``. Every
    field is read again rather than trusted from construction, because a
    frozen dataclass can still be altered through ``object.__setattr__`` and
    ``object.__delattr__``, so the boundaries that act on a model plan ask
    this again before they do. The plan's fields are read from the plan
    itself, as ``own_origin`` reads them, so a plan missing any of its own
    fields, or holding a step missing one, is no model plan, and the
    question is answered rather than raised.
    """
    if type(plan) is not Plan:
        return False
    fields = _own_fields(plan)
    if fields is None:
        return False
    steps = fields["steps"]
    try:
        return (
            fields["origin"] is PlanOrigin.MODEL
            and fields["recording"] is RecordingPolicy.TRANSIENT
            and fields["response_if_no_action"] is None
            and type(fields["original_request"]) is str
            and type(steps) is tuple
            and 1 <= len(steps) <= PLAN_MAX_STEPS
            and all(
                type(step) is PlanStep
                and type(step.tool_name) is str
                and type(step.action) is str
                and type(step.arguments) is PlanArguments
                for step in steps
            )
        )
    except AttributeError:
        # A field deleted from one of its steps, none of which has a default.
        return False


def own_origin(plan: object) -> PlanOrigin | None:
    """Return the origin a plan holds itself, or None unless it is whole.

    The origin is read from the plan's own fields, never through attribute
    lookup: a field deleted from a built plan would otherwise read as its
    class default, which for the origin is DETERMINISTIC. None means the
    value is not a ``Plan``, does not hold its fields in an exact
    dictionary, lacks any of its own fields, or holds a policy or an origin
    that is not exactly one of their members. Brain refuses a plan without
    one before it chooses a path, and only a plan holding exactly
    DETERMINISTIC itself takes the deterministic path. An origin reassigned
    to DETERMINISTIC is a real member, so nothing left in the plan can show
    that it was changed; the fingerprint captured when approval was offered
    is what detects a change made after that.
    """
    fields = _own_fields(plan)
    return None if fields is None else fields["origin"]


def plan_snapshot(plan: object) -> Plan | None:
    """Return a plan exactly as one bounded read finds it, or None.

    Every field is read from the plan itself, so a deleted field is missing
    rather than its class default, and no copy can restore it. A model plan
    must already be exactly well formed, and is its own snapshot; it is
    never repaired by copying. A deterministic plan's steps are read once,
    by the same rule as ``bounded_steps``: an exact ``Plan`` already holding
    them in an exact tuple is its own snapshot, and otherwise the snapshot
    is a new ``Plan`` holding the read as a tuple and every other field
    exactly as the plan holds it, each passed explicitly. A snapshot's
    sequence of steps is a tuple, which no later read can change; the step
    objects in it are the plan's own, not copies. None means the value is
    not whole, as ``own_origin`` describes, a model plan is not well
    formed, or a deterministic plan's steps cannot be read within
    ``PLAN_MAX_STEPS``.
    """
    fields = _own_fields(plan)
    if fields is None:
        return None
    if fields["origin"] is PlanOrigin.MODEL:
        return plan if is_model_plan(plan) else None
    steps = _bounded_read(fields["steps"])
    if steps is None:
        return None
    if type(plan) is Plan and type(fields["steps"]) is tuple:
        return plan
    return Plan(**{**fields, "steps": steps})


def _own_fields(plan: object) -> dict[str, Any] | None:
    """Return every field a plan holds itself, or None unless it is whole.

    The fields come from the plan's own instance dictionary, so none can
    fall back to a class default. That dictionary must be exactly a
    ``dict``: one of a subclass could show other values here than the
    attribute reads every other reader of the plan makes. The policy and
    the origin must be exactly their enum members.
    """
    if not issubclass(type(plan), Plan):
        return None
    held = vars(plan)
    if type(held) is not dict:
        return None
    if any(name not in held for name in _PLAN_FIELDS):
        return None
    if (
        type(held["recording"]) is not RecordingPolicy
        or type(held["origin"]) is not PlanOrigin
    ):
        return None
    return {name: held[name] for name in _PLAN_FIELDS}


def bounded_steps(plan: object) -> tuple[PlanStep, ...] | None:
    """Return a plan's steps as one bounded read, or None past the bound.

    The steps are read afresh, by iterating them and stopping one step past
    ``PLAN_MAX_STEPS``, so no length a container reports is trusted and
    nothing endless is read to its end. They come back in order, exactly as
    read and otherwise unchecked; a model plan's stricter shape is
    ``is_model_plan``'s. None means they number more than
    ``PLAN_MAX_STEPS`` or cannot be read: a value without ``steps``,
    anything that cannot be iterated or fails while it is read, and one-shot
    steps, which are never read because reading them would consume the very
    steps they hold. ``plan_snapshot`` reads a plan's steps by this rule.
    The bound counts the steps requested, not time: a container's own code
    can still take as long as it likes to hand out any one of them.
    """
    try:
        steps = plan.steps
    except Exception:  # noqa: BLE001 - no readable steps, never in bounds.
        return None
    return _bounded_read(steps)


def is_within_step_bound(plan: object) -> bool:
    """Return whether a plan's steps can be read and are within the limit.

    This is ``bounded_steps`` answered as yes or no, so the steps are read
    rather than counted by the length they report, and read again every
    time rather than trusted from construction, because a frozen dataclass
    can still be altered through ``object.__setattr__``. It asks about the
    steps alone; whether a plan may be confirmed or run is
    ``plan_snapshot``'s question, and a model plan's stricter shape is
    ``is_model_plan``'s.
    """
    return bounded_steps(plan) is not None


def _bounded_read(steps: object) -> tuple[Any, ...] | None:
    """Return steps read within ``PLAN_MAX_STEPS``, or None past it or unread.

    This is the one rule both ``bounded_steps`` and ``plan_snapshot`` apply.
    """
    read = _read_steps(steps)
    if read is None or len(read) > PLAN_MAX_STEPS:
        return None
    return read


def _read_steps(steps: object) -> tuple[Any, ...] | None:
    """Read at most one step past the bound, or return None if unreadable.

    A fresh iterator is taken and at most ``PLAN_MAX_STEPS + 1`` steps are
    read from it, so a misreported length is never consulted and an endless
    iterable is never exhausted. One-shot steps are not read at all, since
    reading them would consume them: an iterator, which is its own iterator,
    and anything that hands out the same iterator every time it is asked.
    """
    try:
        iterator = iter(steps)
        if iterator is steps or iterator is iter(steps):
            return None
        return tuple(islice(iterator, PLAN_MAX_STEPS + 1))
    except Exception:  # noqa: BLE001 - unreadable steps are never in bounds.
        return None


class Planner:
    """Transform explicit user requests into executable tool plans."""

    def __init__(self, logger: Logger | None = None) -> None:
        self.logger = logger or get_logger(__name__)

    def create_plan(self, user_input: str) -> Plan:
        """Create a plan for a supported request.

        The request text is the planner's only input. No stored record, no
        interaction history, and no memory context is consulted, so the same
        text always produces the same plan.
        """
        text = user_input.strip()
        lowered = text.lower()
        command_parts = text.split(maxsplit=1)
        command = command_parts[0].lower() if command_parts else ""
        self.logger.info("Creating plan for request.")

        if lowered in {"help", "commands"}:
            return self._no_action(text, _help_text())

        if lowered.startswith(("calculate ", "calc ")):
            expression = _remove_first_word(text)
            return self._single_step(
                text,
                "calculator",
                "calculate",
                {"expression": expression},
            )

        if command == "remember":
            if len(command_parts) == 1:
                return self._no_action(text, "Use: remember <fact>")
            fact = command_parts[1].strip()
            return self._single_step(text, "memory", "store", {"fact": fact})

        if command == "recall":
            if len(command_parts) == 1:
                return self._no_action(text, "Use: recall <query>")
            query = command_parts[1].strip()
            return self._single_step(text, "memory", "search", {"query": query})

        if lowered == "memories":
            return self._single_step(text, "memory", "list", {})

        if lowered == "update memory":
            return self._no_action(text, "Use: update memory <id>: <new fact>")

        if lowered.startswith("update memory "):
            parsed_mutation = _parse_id_and_colon_operand(
                text[len("update memory ") :].strip()
            )
            if parsed_mutation is None or not parsed_mutation[1]:
                return self._no_action(
                    text,
                    "Use: update memory <id>: <new fact>",
                )
            id_text, new_fact = parsed_mutation
            memory_id = _parse_positive_ascii_id(id_text)
            if memory_id is None:
                return self._no_action(
                    text,
                    "Memory id must be a positive whole number.",
                )
            return self._single_step(
                text,
                "memory",
                "update",
                {"id": memory_id, "fact": new_fact},
            )

        if lowered == "forget memory":
            return self._no_action(text, "Use: forget memory <id>")

        if lowered.startswith("forget memory "):
            operand = text[len("forget memory ") :].strip()
            memory_id = _parse_positive_ascii_id(operand)
            if memory_id is None:
                return self._no_action(
                    text,
                    "Memory id must be a positive whole number.",
                )
            return self._single_step(text, "memory", "forget", {"id": memory_id})

        if lowered == "enable memory context":
            return self._no_action(text, "Use: enable memory context <id>")

        if lowered.startswith("enable memory context "):
            operand = text[len("enable memory context ") :].strip()
            memory_id = _parse_positive_ascii_id(operand)
            if memory_id is None:
                return self._no_action(
                    text,
                    "Memory id must be a positive whole number.",
                )
            return self._single_step(
                text,
                "memory",
                "enable_context",
                {"id": memory_id},
            )

        if lowered == "disable memory context":
            return self._no_action(text, "Use: disable memory context <id>")

        if lowered.startswith("disable memory context "):
            operand = text[len("disable memory context ") :].strip()
            memory_id = _parse_positive_ascii_id(operand)
            if memory_id is None:
                return self._no_action(
                    text,
                    "Memory id must be a positive whole number.",
                )
            return self._single_step(
                text,
                "memory",
                "disable_context",
                {"id": memory_id},
            )

        if lowered.startswith(("list ", "ls ")):
            path = _remove_first_word(text)
            return self._single_step(text, "filesystem", "list", {"path": path})

        if lowered.startswith("read file "):
            path = text[len("read file ") :].strip()
            return self._single_step(text, "filesystem", "read", {"path": path})

        if lowered.startswith("rename "):
            parsed = _parse_source_destination(text[len("rename ") :])
            if parsed is None:
                return self._no_action(text, "Use: rename <source> to <destination>")
            source, destination = parsed
            return self._single_step(
                text,
                "filesystem",
                "rename",
                {"source": source, "destination": destination},
            )

        if lowered.startswith("move "):
            parsed = _parse_source_destination(text[len("move ") :])
            if parsed is None:
                return self._no_action(text, "Use: move <source> to <destination>")
            source, destination = parsed
            return self._single_step(
                text,
                "filesystem",
                "move",
                {"source": source, "destination": destination},
            )

        if lowered.startswith(("create note ", "note ")):
            note_text = _remove_note_prefix(text)
            parsed_note = _parse_note(note_text)
            if parsed_note is None:
                return self._no_action(text, "Use: create note <title>: <body>")
            title, body = parsed_note
            return self._single_step(
                text,
                "notes",
                "create",
                {"title": title, "body": body},
            )

        if lowered in {"notes", "list notes"}:
            return self._single_step(text, "notes", "list", {})

        if lowered.startswith("read note "):
            target = text[len("read note ") :].strip()
            return self._single_step(text, "notes", "read", {"target": target})

        if lowered == "retitle note":
            return self._no_action(text, "Use: retitle note <id>: <new title>")

        if lowered.startswith("retitle note "):
            parsed_mutation = _parse_id_and_colon_operand(
                text[len("retitle note ") :].strip()
            )
            if parsed_mutation is None or not parsed_mutation[1]:
                return self._no_action(
                    text,
                    "Use: retitle note <id>: <new title>",
                )
            id_text, new_title = parsed_mutation
            note_id = _parse_positive_ascii_id(id_text)
            if note_id is None:
                return self._no_action(
                    text,
                    "Note id must be a positive whole number.",
                )
            return self._single_step(
                text,
                "notes",
                "retitle",
                {"id": note_id, "title": new_title},
            )

        if lowered == "edit note":
            return self._no_action(text, "Use: edit note <id>: <new body>")

        if lowered.startswith("edit note "):
            parsed_mutation = _parse_id_and_colon_operand(
                text[len("edit note ") :].strip()
            )
            if parsed_mutation is None or not parsed_mutation[1]:
                return self._no_action(text, "Use: edit note <id>: <new body>")
            id_text, new_body = parsed_mutation
            note_id = _parse_positive_ascii_id(id_text)
            if note_id is None:
                return self._no_action(
                    text,
                    "Note id must be a positive whole number.",
                )
            return self._single_step(
                text,
                "notes",
                "edit",
                {"id": note_id, "body": new_body},
            )

        if lowered == "delete note":
            return self._no_action(text, "Use: delete note <id>")

        if lowered.startswith("delete note "):
            operand = text[len("delete note ") :].strip()
            note_id = _parse_positive_ascii_id(operand)
            if note_id is None:
                return self._no_action(
                    text,
                    "Note id must be a positive whole number.",
                )
            return self._single_step(text, "notes", "delete", {"id": note_id})

        if lowered == "enable note context":
            return self._no_action(text, "Use: enable note context <id>")

        if lowered.startswith("enable note context "):
            operand = text[len("enable note context ") :].strip()
            note_id = _parse_positive_ascii_id(operand)
            if note_id is None:
                return self._no_action(
                    text,
                    "Note id must be a positive whole number.",
                )
            return self._single_step(
                text,
                "notes",
                "enable_context",
                {"id": note_id},
            )

        if lowered == "disable note context":
            return self._no_action(text, "Use: disable note context <id>")

        if lowered.startswith("disable note context "):
            operand = text[len("disable note context ") :].strip()
            note_id = _parse_positive_ascii_id(operand)
            if note_id is None:
                return self._no_action(
                    text,
                    "Note id must be a positive whole number.",
                )
            return self._single_step(
                text,
                "notes",
                "disable_context",
                {"id": note_id},
            )

        if lowered == "history":
            return self._single_step(text, "history", "list", {})

        if lowered == "search history":
            return self._no_action(text, "Use: search history <query>")

        if lowered.startswith("search history "):
            query = text[len("search history ") :].strip()
            if not query:
                return self._no_action(text, "Use: search history <query>")
            return self._single_step(text, "history", "search", {"query": query})

        if lowered == "read interaction":
            return self._no_action(text, "Use: read interaction <id>")

        if lowered.startswith("read interaction "):
            operand = text[len("read interaction ") :].strip()
            if not operand:
                return self._no_action(text, "Use: read interaction <id>")
            interaction_id = _parse_positive_ascii_id(operand)
            if interaction_id is None:
                return self._no_action(
                    text,
                    "Interaction id must be a positive whole number.",
                )
            return self._single_step(text, "history", "read", {"id": interaction_id})

        if lowered == "delete interaction":
            return self._no_action(text, "Use: delete interaction <id>")

        if lowered.startswith("delete interaction "):
            operand = text[len("delete interaction ") :].strip()
            if not operand:
                return self._no_action(text, "Use: delete interaction <id>")
            interaction_id = _parse_positive_ascii_id(operand)
            if interaction_id is None:
                return self._no_action(
                    text,
                    "Interaction id must be a positive whole number.",
                )
            return self._single_step(text, "history", "delete", {"id": interaction_id})

        if lowered == "audit":
            return self._single_step(text, "audit", "list", {})

        if lowered == "read audit":
            return self._no_action(text, "Use: read audit <id>")

        if lowered.startswith("read audit "):
            operand = text[len("read audit ") :].strip()
            if not operand:
                return self._no_action(text, "Use: read audit <id>")
            summary_id = _parse_positive_ascii_id(operand)
            if summary_id is None:
                return self._no_action(
                    text,
                    "Audit summary id must be a positive whole number.",
                )
            return self._single_step(text, "audit", "read", {"id": summary_id})

        if lowered == "delete audit":
            return self._no_action(text, "Use: delete audit <id>")

        if lowered.startswith("delete audit "):
            operand = text[len("delete audit ") :].strip()
            if not operand:
                return self._no_action(text, "Use: delete audit <id>")
            summary_id = _parse_positive_ascii_id(operand)
            if summary_id is None:
                return self._no_action(
                    text,
                    "Audit summary id must be a positive whole number.",
                )
            return self._single_step(text, "audit", "delete", {"id": summary_id})

        if lowered in {"system", "system info", "status", "sys", "sys info"}:
            return self._single_step(text, "system", "overview", {})

        if lowered in {"time", "current time", "time now"}:
            return self._single_step(text, "system", "current_time", {})

        return self._no_action(
            text,
            "I do not have an executable plan for that yet. "
            "Type 'help' to see supported commands.",
        )

    @staticmethod
    def _single_step(
        original_request: str,
        tool_name: str,
        action: str,
        arguments: Mapping[str, Any],
    ) -> Plan:
        """Create a one-step plan."""
        return Plan(
            original_request=original_request,
            steps=(PlanStep(tool_name, action, arguments),),
        )

    @staticmethod
    def _no_action(original_request: str, response: str) -> Plan:
        """Create a plan that intentionally performs no tool action."""
        return Plan(
            original_request=original_request,
            steps=(),
            response_if_no_action=response,
        )


def _remove_first_word(text: str) -> str:
    """Return text after the first whitespace-delimited word."""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def _parse_source_destination(value: str) -> tuple[str, str] | None:
    """Parse '<source> to <destination>' command text."""
    marker = " to "
    if marker not in value:
        return None
    source, destination = value.split(marker, maxsplit=1)
    source = source.strip()
    destination = destination.strip()
    if not source or not destination:
        return None
    return source, destination


def _remove_note_prefix(text: str) -> str:
    """Remove a note creation command prefix."""
    lowered = text.lower()
    if lowered.startswith("create note "):
        return text[len("create note ") :].strip()
    return text[len("note ") :].strip()


def _parse_note(note_text: str) -> tuple[str, str] | None:
    """Parse note text, or return None when the required colon is missing."""
    if ":" not in note_text:
        return None
    title, body = note_text.split(":", maxsplit=1)
    return title.strip() or "Untitled", body.strip()


def _parse_id_and_colon_operand(value: str) -> tuple[str, str] | None:
    """Parse '<id>: <operand>' text, or return None without a colon.

    Only the first colon separates the id from the operand, so later
    colons stay literal inside a new title or body, matching the note
    creation grammar. Outer whitespace is stripped from both parts;
    casing and internal spacing are preserved.
    """
    if ":" not in value:
        return None
    id_text, operand = value.split(":", maxsplit=1)
    return id_text.strip(), operand.strip()


# The largest id any stored record can have: every id is a SQLite integer,
# and SQLite's largest integer is 2**63 - 1.
_MAXIMUM_ID = 2**63 - 1
_MAXIMUM_ID_DIGITS = len(str(_MAXIMUM_ID))


def _parse_positive_ascii_id(value: str) -> int | None:
    """Return a positive decimal id, or None if invalid.

    Accepts ASCII digits only, rejecting Unicode digit lookalikes that
    str.isdigit() alone would allow through. Leading zeroes are stripped
    however many there are, and more than 19 significant digits are refused
    before any conversion. At most 19 digits are then converted, and the
    result is compared with 9223372036854775807, the largest id any record
    can have. An operand of any length is therefore answered, and Python's
    limit on converting long digit strings is never reached.
    """
    if not value.isascii() or not value.isdigit():
        return None
    digits = value.lstrip("0")
    if not digits or len(digits) > _MAXIMUM_ID_DIGITS:
        return None
    parsed = int(digits)
    return parsed if parsed <= _MAXIMUM_ID else None


def _help_text() -> str:
    """Return the supported command list."""
    return (
        "Supported commands:\n"
        "- calculate <expression>\n"
        "- list <path>\n"
        "- read file <path>\n"
        "- rename <source> to <destination>\n"
        "- move <source> to <destination>\n"
        "- create note <title>: <body>\n"
        "- notes\n"
        "- read note <id or exact title>\n"
        "- retitle note <id>: <new title>\n"
        "- edit note <id>: <new body>\n"
        "- delete note <id>\n"
        "- enable note context <id>\n"
        "- disable note context <id>\n"
        "- remember <fact>\n"
        "- recall <query>\n"
        "- memories\n"
        "- update memory <id>: <new fact>\n"
        "- forget memory <id>\n"
        "- enable memory context <id>\n"
        "- disable memory context <id>\n"
        "- system\n"
        "- time\n"
        "- history\n"
        "- search history <query>\n"
        "- read interaction <id>\n"
        "- delete interaction <id>\n"
        "- audit\n"
        "- read audit <id>\n"
        "- delete audit <id>"
    )
