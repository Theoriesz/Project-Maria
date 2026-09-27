"""Request planning for Maria.

The planner is intentionally conservative. It maps explicit user
commands to a small set of supported tool actions and returns no plan when the
request is outside Maria's supported command set.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
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


@dataclass(frozen=True)
class Plan:
    """An executable plan created from user input.

    The recording policy defaults to RECORD, which is how every interaction
    has always been recorded. TRANSIENT is a positive choice a plan's
    producer makes explicitly, and it keeps the whole interaction row out of
    history: the request as well as the response.
    """

    original_request: str
    steps: tuple[PlanStep, ...]
    response_if_no_action: str | None = None
    recording: RecordingPolicy = RecordingPolicy.RECORD

    def __post_init__(self) -> None:
        """Refuse any recording policy that is not exactly a RecordingPolicy.

        The check is on the exact type, so neither a plain string nor an
        object claiming the class through ``__class__`` passes. A string
        would compare and hash equal to the member it spells while failing
        every identity check made against that member, so it is refused
        rather than coerced.
        """
        if type(self.recording) is not RecordingPolicy:
            raise TypeError("Plan recording policy must be a RecordingPolicy.")

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

        The recording policy joins the payload only when it is not the
        default. Every default plan therefore keeps exactly the bytes, and
        the fingerprint, it had before the policy existed, while an otherwise
        identical transient plan fingerprints differently.
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
        return json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")


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


def _parse_positive_ascii_id(value: str) -> int | None:
    """Return a positive decimal id, or None if invalid.

    Accepts ASCII digits only, rejecting Unicode digit lookalikes that
    str.isdigit() alone would allow through.
    """
    if not value.isascii() or not value.isdigit():
        return None
    parsed = int(value)
    return parsed if parsed >= 1 else None


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
        "- delete interaction <id>"
    )
