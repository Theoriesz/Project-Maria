"""Simple notes tool for Maria."""

from __future__ import annotations

from logging import Logger
from typing import Any

from core.logger import get_logger
from core.memory import Memory
from core.tool_manager import ToolExecutionResult


_DEFAULT_LIST_LIMIT = 20
_MORE_NOTES_SUFFIX = "Showing the newest 20 notes. More notes exist."


class NotesTool:
    """Create, retrieve, update, and delete notes through the memory layer."""

    name = "notes"
    description = "Creates, lists, reads, retitles, edits, and deletes notes."
    actions = frozenset(
        {
            "create",
            "list",
            "read",
            "retitle",
            "edit",
            "delete",
        }
    )

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
        if action == "retitle":
            return self._retitle(arguments)
        if action == "edit":
            return self._edit(arguments)
        if action == "delete":
            return self._delete(arguments)
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
            limit = int(arguments.get("limit", _DEFAULT_LIST_LIMIT))
        except (TypeError, ValueError):
            return ToolExecutionResult(False, "Notes limit must be a number.")

        probe_default = limit == _DEFAULT_LIST_LIMIT
        query_limit = limit + 1 if probe_default else limit
        try:
            notes = self.memory.list_notes(limit=query_limit)
        except RuntimeError as exc:
            self.logger.warning("Note listing failed: %s", exc)
            return ToolExecutionResult(False, f"Note listing failed: {exc}")

        if not notes:
            return ToolExecutionResult(True, "No notes found.", [])

        has_more = probe_default and len(notes) > _DEFAULT_LIST_LIMIT
        visible_notes = notes[:_DEFAULT_LIST_LIMIT] if probe_default else notes

        lines = [f"{note['id']}: {note['title']}" for note in visible_notes]
        message = "\n".join(lines)
        if has_more:
            message = f"{message}\n{_MORE_NOTES_SUFFIX}"
        return ToolExecutionResult(True, message, visible_notes)

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

    def _retitle(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        """Retitle one note by id."""
        note_id = arguments.get("id")
        if (
            not isinstance(note_id, int)
            or isinstance(note_id, bool)
            or note_id < 1
        ):
            return ToolExecutionResult(
                False,
                "Note id must be a positive whole number.",
            )

        new_title = arguments.get("title")
        if not isinstance(new_title, str) or not new_title.strip():
            return ToolExecutionResult(False, "New note title is required.")

        try:
            retitled = self.memory.retitle_note(note_id, new_title.strip())
        except (RuntimeError, ValueError) as exc:
            self.logger.warning("Note retitle failed: %s", exc)
            return ToolExecutionResult(False, f"Note retitle failed: {exc}")

        if not retitled:
            return ToolExecutionResult(False, f"Note not found: {note_id}.")

        self.logger.info("Retitled note %s through notes tool.", note_id)
        return ToolExecutionResult(
            True,
            f"Retitled note {note_id}.",
            {"id": note_id},
        )

    def _edit(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        """Edit one note's body by id."""
        note_id = arguments.get("id")
        if (
            not isinstance(note_id, int)
            or isinstance(note_id, bool)
            or note_id < 1
        ):
            return ToolExecutionResult(
                False,
                "Note id must be a positive whole number.",
            )

        new_body = arguments.get("body")
        if not isinstance(new_body, str) or not new_body.strip():
            return ToolExecutionResult(False, "New note body is required.")

        try:
            edited = self.memory.edit_note(note_id, new_body.strip())
        except (RuntimeError, ValueError) as exc:
            self.logger.warning("Note edit failed: %s", exc)
            return ToolExecutionResult(False, f"Note edit failed: {exc}")

        if not edited:
            return ToolExecutionResult(False, f"Note not found: {note_id}.")

        self.logger.info("Edited note %s through notes tool.", note_id)
        return ToolExecutionResult(
            True,
            f"Edited note {note_id}.",
            {"id": note_id},
        )

    def _delete(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        """Delete one note by id."""
        note_id = arguments.get("id")
        if (
            not isinstance(note_id, int)
            or isinstance(note_id, bool)
            or note_id < 1
        ):
            return ToolExecutionResult(
                False,
                "Note id must be a positive whole number.",
            )

        try:
            deleted = self.memory.delete_note(note_id)
        except RuntimeError as exc:
            self.logger.warning("Note deletion failed: %s", exc)
            return ToolExecutionResult(False, f"Note deletion failed: {exc}")

        if not deleted:
            return ToolExecutionResult(False, f"Note not found: {note_id}.")

        self.logger.info("Deleted note %s through notes tool.", note_id)
        return ToolExecutionResult(
            True,
            f"Deleted note {note_id}.",
            {"id": note_id},
        )


def build_tool(memory: Memory, **_: Any) -> NotesTool:
    """Build a notes tool instance."""
    return NotesTool(memory=memory)
