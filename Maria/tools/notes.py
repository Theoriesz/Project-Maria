"""Simple notes tool for Maria."""

from __future__ import annotations

from logging import Logger
from typing import Any

from core.logger import get_logger
from core.memory import Memory
from core.tool_manager import ToolExecutionResult


class NotesTool:
    """Create and retrieve notes through the memory layer."""

    name = "notes"
    description = "Creates, lists, and reads notes."
    actions = frozenset({"create", "list", "read"})

    def __init__(self, memory: Memory, logger: Logger | None = None) -> None:
        self.memory = memory
        self.logger = logger or get_logger(__name__)

    def execute(
        self,
        action: str,
        arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        """Execute a notes action."""
        if action == "create":
            return self._create(arguments)
        if action == "list":
            return self._list(arguments)
        if action == "read":
            return self._read(arguments)
        return ToolExecutionResult(False, f"Unsupported notes action: {action}")

    def _create(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        """Create a note."""
        title = str(arguments.get("title", "Untitled")).strip() or "Untitled"
        body = str(arguments.get("body", "")).strip()
        if not body:
            return ToolExecutionResult(False, "Note body is required.")

        try:
            note_id = self.memory.create_note(title, body)
        except (RuntimeError, ValueError) as exc:
            self.logger.warning("Note creation failed: %s", exc)
            return ToolExecutionResult(False, f"Note creation failed: {exc}")

        self.logger.info("Created note through notes tool: %s", note_id)
        return ToolExecutionResult(
            True,
            f"Created note {note_id}: {title}",
            {"id": note_id, "title": title},
        )

    def _list(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        """List notes."""
        try:
            limit = int(arguments.get("limit", 20))
        except (TypeError, ValueError):
            return ToolExecutionResult(False, "Notes limit must be a number.")

        try:
            notes = self.memory.list_notes(limit=limit)
        except RuntimeError as exc:
            self.logger.warning("Note listing failed: %s", exc)
            return ToolExecutionResult(False, f"Note listing failed: {exc}")

        if not notes:
            return ToolExecutionResult(True, "No notes found.", [])

        lines = [f"{note['id']}: {note['title']}" for note in notes]
        return ToolExecutionResult(True, "\n".join(lines), notes)

    def _read(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        """Read one note by id or exact title."""
        target = str(arguments.get("target", "")).strip()
        if not target:
            return ToolExecutionResult(False, "Note id or title is required.")

        try:
            if target.isdigit():
                note = self.memory.get_note(note_id=int(target))
            else:
                note = self.memory.get_note(title=target)
        except (RuntimeError, ValueError) as exc:
            self.logger.warning("Note read failed: %s", exc)
            return ToolExecutionResult(False, f"Note read failed: {exc}")

        if note is None:
            return ToolExecutionResult(False, f"Note not found: {target}")

        message = f"{note['title']}\n{note['body']}"
        return ToolExecutionResult(True, message, note)


def build_tool(memory: Memory, **_: Any) -> NotesTool:
    """Build a notes tool instance."""
    return NotesTool(memory=memory)
