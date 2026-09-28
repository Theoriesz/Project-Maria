"""Simple notes tool for Maria."""

from __future__ import annotations

from collections.abc import Mapping
from logging import Logger
from typing import Any

from core.action_contracts import MAXIMUM_LIST_LIMIT, MINIMUM_LIST_LIMIT
from core.context import ContextConsentOutcome, quoted_for_owner
from core.logger import get_logger
from core.memory import Memory
from core.tool_manager import ConfirmationBinding, ToolExecutionResult


_DEFAULT_LIST_LIMIT = 20
_MORE_NOTES_SUFFIX = "Showing the newest 20 notes. More notes exist."
_LIST_LIMIT_RANGE_MESSAGE = (
    f"Notes limit must be between {MINIMUM_LIST_LIMIT} and {MAXIMUM_LIST_LIMIT}."
)


class NotesTool:
    """Create, retrieve, update, and delete notes through the memory layer.

    It also turns one note's context consent on or off. Those results name
    the note's id and never its title or body, so the interaction row a
    consent change records carries no stored text; only the confirmation
    request for an enable shows the owner that text.
    """

    name = "notes"
    description = (
        "Creates, lists, reads, retitles, edits, and deletes notes, and turns "
        "one note's context consent on or off."
    )
    actions = frozenset(
        {
            "create",
            "list",
            "read",
            "retitle",
            "edit",
            "delete",
            "enable_context",
            "disable_context",
        }
    )

    def __init__(self, memory: Memory, logger: Logger | None = None) -> None:
        self.memory = memory
        self.logger = logger or get_logger(__name__)

    def confirmation_binding(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any] | ConfirmationBinding:
        """Pin an enable approval to the exact title and body shown for it.

        Consent covers exact text, so the owner is shown the note's current
        title and body in full, quoted exactly, and the plan they approve
        for ``enable_context`` carries the identity of that same title and
        body as ``text_digest``; both come from one read. At execution the
        memory layer enables consent only while both are still exactly that
        text. Only the identity enters the plan: the shown text is the
        binding's preview, which is never recorded, logged, or sent to a
        model. Nothing is added for any other action, for an id that is not
        valid, for a note that does not exist, or when the note cannot be
        read; an enable carrying no identity can never turn consent on for
        an existing note, so each of those still ends safely.
        """
        if action != "enable_context":
            return {}
        note_id = arguments.get("id")
        if not isinstance(note_id, int) or isinstance(note_id, bool) or note_id < 1:
            return {}
        try:
            snapshot = self.memory.note_snapshot(note_id)
        except RuntimeError:
            self.logger.warning(
                "Could not bind context approval for note %s.",
                note_id,
            )
            return {}
        if snapshot is None:
            return {}
        return ConfirmationBinding(
            {"text_digest": snapshot.text_digest},
            (
                f"This approval covers note {note_id} exactly as it is stored "
                "now, shown quoted:\n"
                f"Title: {quoted_for_owner(snapshot.title)}\n"
                f"Body: {quoted_for_owner(snapshot.body)}"
            ),
        )

    def execute(
        self,
        action: str,
        arguments: Mapping[str, Any],
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
        if action == "enable_context":
            return self._set_context(arguments, enabled=True)
        if action == "disable_context":
            return self._set_context(arguments, enabled=False)
        return ToolExecutionResult(False, f"Unsupported notes action: {action}")

    def _create(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Create a note."""
        if "title" in arguments:
            title = arguments["title"]
            if type(title) is not str:
                return ToolExecutionResult(False, "Note title must be text.")
            if not title.strip():
                return ToolExecutionResult(False, "Note title is required.")
        else:
            title = "Untitled"

        body = arguments.get("body", "")
        if type(body) is not str:
            return ToolExecutionResult(False, "Note body must be text.")

        title = title.strip()
        body = body.strip()
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

    def _list(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """List notes."""
        limit = arguments.get("limit", _DEFAULT_LIST_LIMIT)
        if type(limit) is not int:
            return ToolExecutionResult(False, "Notes limit must be a number.")
        if not MINIMUM_LIST_LIMIT <= limit <= MAXIMUM_LIST_LIMIT:
            return ToolExecutionResult(False, _LIST_LIMIT_RANGE_MESSAGE)

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

    def _read(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Read one note by id or exact title."""
        target = arguments.get("target", "")
        if type(target) is not str:
            return ToolExecutionResult(False, "Note id or title must be text.")

        target = target.strip()
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

    def _retitle(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
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
            result = self.memory.retitle_note_reporting_consent(
                note_id,
                new_title.strip(),
            )
        except (RuntimeError, ValueError) as exc:
            self.logger.warning("Note retitle failed: %s", exc)
            return ToolExecutionResult(False, f"Note retitle failed: {exc}")

        if not result.updated:
            return ToolExecutionResult(False, f"Note not found: {note_id}.")

        self.logger.info("Retitled note %s through notes tool.", note_id)
        message = f"Retitled note {note_id}."
        if result.context_consent_cleared:
            # Consent covered the title and body the owner approved, and this
            # retitle replaced one of them, so it is off now and the owner is
            # told.
            message = (
                f"{message} Context consent for note {note_id} was turned off "
                "because its title changed."
            )
        return ToolExecutionResult(True, message, {"id": note_id})

    def _edit(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
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
            result = self.memory.edit_note_reporting_consent(
                note_id,
                new_body.strip(),
            )
        except (RuntimeError, ValueError) as exc:
            self.logger.warning("Note edit failed: %s", exc)
            return ToolExecutionResult(False, f"Note edit failed: {exc}")

        if not result.updated:
            return ToolExecutionResult(False, f"Note not found: {note_id}.")

        self.logger.info("Edited note %s through notes tool.", note_id)
        message = f"Edited note {note_id}."
        if result.context_consent_cleared:
            message = (
                f"{message} Context consent for note {note_id} was turned off "
                "because its body changed."
            )
        return ToolExecutionResult(True, message, {"id": note_id})

    def _delete(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
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

    def _set_context(
        self,
        arguments: Mapping[str, Any],
        *,
        enabled: bool,
    ) -> ToolExecutionResult:
        """Turn one note's context consent on or off by id.

        Enabling is bound to the text identity pinned when confirmation was
        offered, carried as ``text_digest``. If the note's title or body is no
        longer exactly that text, or no identity was pinned for an existing
        note, nothing changes and the owner is told to ask again; an approval
        is never carried over to text the owner was not asked about.
        Disabling only narrows what can be sent and needs no identity.
        """
        note_id = arguments.get("id")
        if not isinstance(note_id, int) or isinstance(note_id, bool) or note_id < 1:
            return ToolExecutionResult(
                False,
                "Note id must be a positive whole number.",
            )

        try:
            if enabled:
                outcome = self.memory.enable_note_context(
                    note_id,
                    arguments.get("text_digest"),
                )
                matched = outcome is not ContextConsentOutcome.NOT_FOUND
            else:
                outcome = None
                matched = self.memory.disable_note_context(note_id)
        except (RuntimeError, ValueError) as exc:
            self.logger.warning("Note context change failed: %s", exc)
            return ToolExecutionResult(
                False,
                f"Note context change failed: {exc}",
            )

        if not matched:
            return ToolExecutionResult(False, f"Note not found: {note_id}.")

        if outcome is ContextConsentOutcome.TARGET_CHANGED:
            return ToolExecutionResult(
                False,
                f"Context was not enabled for note {note_id} because its title "
                "and body could not be confirmed unchanged since confirmation "
                "was requested. Request confirmation again with: enable note "
                f"context {note_id}",
            )

        verb = "Enabled" if enabled else "Disabled"
        self.logger.info(
            "%s context consent for note %s through notes tool.",
            verb,
            note_id,
        )
        return ToolExecutionResult(
            True,
            f"{verb} context for note {note_id}.",
            {"id": note_id, "context_enabled": enabled},
        )


def build_tool(memory: Memory, **_: Any) -> NotesTool:
    """Build a notes tool instance."""
    return NotesTool(memory=memory)
