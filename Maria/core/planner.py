"""Request planning for Maria v0.1.

The v0.1 planner is intentionally conservative. It maps explicit user
commands to a small set of supported tool actions and returns no plan when the
request is outside the foundation scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from logging import Logger
from typing import Any

from core.logger import get_logger


@dataclass(frozen=True)
class PlanStep:
    """One executable step in a Maria plan."""

    tool_name: str
    action: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Plan:
    """An executable plan created from user input."""

    original_request: str
    steps: tuple[PlanStep, ...]
    response_if_no_action: str | None = None


class Planner:
    """Transform explicit user requests into executable tool plans."""

    def __init__(self, logger: Logger | None = None) -> None:
        self.logger = logger or get_logger(__name__)

    def create_plan(
        self,
        user_input: str,
        context: dict[str, Any] | None = None,
    ) -> Plan:
        """Create a plan for a supported v0.1 request."""
        del context

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
            title, body = _parse_note(note_text)
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

        if lowered in {"system", "system info", "status"}:
            return self._single_step(text, "system", "overview", {})

        if lowered in {"time", "current time"}:
            return self._single_step(text, "system", "current_time", {})

        return self._no_action(
            text,
            "I do not have an executable v0.1 plan for that yet. "
            "Type 'help' to see supported commands.",
        )

    @staticmethod
    def _single_step(
        original_request: str,
        tool_name: str,
        action: str,
        arguments: dict[str, Any],
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


def _parse_note(note_text: str) -> tuple[str, str]:
    """Parse note text into title and body."""
    if ":" in note_text:
        title, body = note_text.split(":", maxsplit=1)
        return title.strip() or "Untitled", body.strip()
    return "Untitled", note_text.strip()


def _help_text() -> str:
    """Return the supported v0.1 command list."""
    return (
        "Supported v0.1 commands:\n"
        "- calculate <expression>\n"
        "- list <path>\n"
        "- read file <path>\n"
        "- rename <source> to <destination>\n"
        "- move <source> to <destination>\n"
        "- create note <title>: <body>\n"
        "- notes\n"
        "- read note <id or exact title>\n"
        "- remember <fact>\n"
        "- recall <query>\n"
        "- memories\n"
        "- system\n"
        "- time"
    )
