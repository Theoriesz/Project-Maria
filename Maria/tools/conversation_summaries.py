"""Conversation-summary tool for Maria.

The tool stores a conversation summary the owner approved, and lists, reads,
edits, and deletes conversation summaries and turns one summary's context
consent on or off, through the memory layer. A summary to store is a draft
the owner's local reasoning model wrote from exactly the session's retained
exchanges, which Maria herself supplied; no request, planner, or model ever
supplies it.

Storing is confirmation-required, and the confirmation is the owner's review
of exactly that text: this tool's binding shows the whole draft, escaped and
never cut down, and pins its identity into the plan the owner approves. The
draft is stored only when that identity still matches it, so a draft never
shown can never be stored. A new summary's context consent is off; enabling
it is a separate confirmation bound to the exact stored text, and an edit
turns it off. These are conversation summaries, never the content-free audit
summaries of model-proposed actions.

The replies to a store, an edit, a deletion, and a consent change name only
ids, timestamps, and consent states, and a listing and a reading record only
a fixed label and an id, so no reply Maria records carries summary text. An
edit's request, like a note edit's, is recorded as the owner typed it, new
text included. Every failure is a fixed message.
"""

from __future__ import annotations

from collections.abc import Mapping
from logging import Logger
from typing import Any

from core.context import ContextConsentOutcome, quoted_for_owner, summary_digest
from core.conversation_records import (
    CONVERSATION_SUMMARY_ID_MESSAGE,
    RECORD_LIST_LIMIT,
    RECORD_QUERY_LIMIT,
    SUMMARY_TEXT_MESSAGE,
    ConversationSummary,
    is_record_id,
    is_summary_text,
)
from core.logger import get_logger
from core.memory import Memory
from core.tool_manager import ConfirmationBinding, ToolExecutionResult


DRAFT_PREVIEW_HEADER = (
    "This approval stores exactly this draft as a new conversation summary, "
    "shown quoted:"
)
NOT_SHOWN_MESSAGE = (
    "Nothing was stored because the draft was not shown in full for approval."
)
NO_SUMMARIES_MESSAGE = "No conversation summaries found."
MORE_SUMMARIES_MESSAGE = (
    f"Showing the newest {RECORD_LIST_LIMIT} conversation summaries. More "
    "conversation summaries exist."
)
SUMMARIES_LISTED_RECORD = "Listed conversation summaries."
STORE_FAILED_MESSAGE = "Unable to store the conversation summary."
LIST_FAILED_MESSAGE = "Unable to list conversation summaries."
READ_FAILED_MESSAGE = "Unable to read the conversation summary."
EDIT_FAILED_MESSAGE = "Unable to edit the conversation summary."
DELETE_FAILED_MESSAGE = "Unable to delete the conversation summary."
CONTEXT_FAILED_MESSAGE = "Unable to change the conversation summary's context."
UNSUPPORTED_ACTION_MESSAGE = "Unsupported conversation-summary action."


def stored_message(summary_id: int) -> str:
    """Return the reply to an approved summary that was stored."""
    return (
        f"Saved as conversation summary {summary_id}. Its context consent is "
        "off."
    )


def not_found_message(summary_id: int) -> str:
    """Return the fixed reply for an id no conversation summary has."""
    return f"Conversation summary not found: {summary_id}."


def listing_line(summary: ConversationSummary) -> str:
    """Render one summary as a content-free listing line."""
    state = "on" if summary.context_enabled else "off"
    return f"{summary.summary_id}: created at {summary.created_at}, context {state}"


def summary_block(summary: ConversationSummary) -> str:
    """Render one summary in full, its text escaped on one line."""
    state = "on" if summary.context_enabled else "off"
    return "\n".join(
        (
            f"Conversation summary {summary.summary_id}",
            f"Created at: {summary.created_at}",
            f"Updated at: {summary.updated_at}",
            f"Context: {state}",
            f"Text: {quoted_for_owner(summary.body)}",
        )
    )


class ConversationSummariesTool:
    """Store, list, read, edit, and delete conversation summaries."""

    name = "conversation_summaries"
    description = (
        "Stores an approved conversation summary, lists, reads, edits, and "
        "deletes conversation summaries, and turns one summary's context "
        "consent on or off."
    )
    actions = frozenset(
        {
            "create",
            "list",
            "read",
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
        """Pin an approval to the exact text the owner is shown for it.

        For ``create`` the text is the draft the plan already holds: it is
        shown whole, escaped, and its identity is pinned as ``text_digest``,
        with no read at all. For ``enable_context`` the summary's stored body
        is read once, shown whole, and its identity pinned from that same
        read. Only identities enter the plan; the shown text is the
        binding's preview, which is never recorded, logged, or sent to a
        model. Nothing is added for any other action or for anything that
        cannot be shown, and a ``create`` or ``enable_context`` carrying no
        identity can never store or enable anything.
        """
        if action == "create":
            text = arguments.get("text")
            if not is_summary_text(text):
                return {}
            return ConfirmationBinding(
                {"text_digest": summary_digest(text)},
                f"{DRAFT_PREVIEW_HEADER}\n{quoted_for_owner(text)}",
            )
        if action != "enable_context":
            return {}
        summary_id = arguments.get("id")
        if not is_record_id(summary_id):
            return {}
        try:
            summary = self.memory.get_conversation_summary(summary_id)
        except (RuntimeError, ValueError):
            self.logger.warning(
                "Could not bind context approval for conversation summary %s.",
                summary_id,
            )
            return {}
        if type(summary) is not ConversationSummary:
            return {}
        return ConfirmationBinding(
            {"text_digest": summary_digest(summary.body)},
            (
                f"This approval covers conversation summary {summary_id} "
                "exactly as it is stored now, shown quoted:\n"
                f"{quoted_for_owner(summary.body)}"
            ),
        )

    def execute(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> ToolExecutionResult:
        """Execute a conversation-summary action."""
        if action == "create":
            return self._create(arguments)
        if action == "list":
            return self._list()
        if action == "read":
            return self._read(arguments)
        if action == "edit":
            return self._edit(arguments)
        if action == "delete":
            return self._delete(arguments)
        if action == "enable_context":
            return self._set_context(arguments, enabled=True)
        if action == "disable_context":
            return self._set_context(arguments, enabled=False)
        return ToolExecutionResult(False, UNSUPPORTED_ACTION_MESSAGE)

    def _create(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Store exactly the approved draft, only if it was shown in full."""
        text = arguments.get("text")
        pinned = arguments.get("text_digest")
        if not is_summary_text(text):
            return ToolExecutionResult(False, SUMMARY_TEXT_MESSAGE)
        if type(pinned) is not str or pinned != summary_digest(text):
            self.logger.warning("Refused a conversation summary never shown.")
            return ToolExecutionResult(False, NOT_SHOWN_MESSAGE)
        try:
            summary_id = self.memory.create_conversation_summary(text)
        except (RuntimeError, ValueError):
            self.logger.warning("Conversation summary storage failed.")
            return ToolExecutionResult(False, STORE_FAILED_MESSAGE)
        self.logger.info(
            "Stored conversation summary %s through conversation summaries tool.",
            summary_id,
        )
        return ToolExecutionResult(
            True,
            stored_message(summary_id),
            {"id": summary_id},
        )

    def _list(self) -> ToolExecutionResult:
        """List the newest summaries, saying when more exist."""
        try:
            summaries = self.memory.list_conversation_summaries(RECORD_QUERY_LIMIT)
        except (RuntimeError, ValueError):
            self.logger.warning("Conversation summary listing failed.")
            return ToolExecutionResult(False, LIST_FAILED_MESSAGE)
        if type(summaries) is not list or any(
            type(summary) is not ConversationSummary for summary in summaries
        ):
            self.logger.warning("Conversation summary listing failed.")
            return ToolExecutionResult(False, LIST_FAILED_MESSAGE)

        if not summaries:
            return ToolExecutionResult(
                True,
                NO_SUMMARIES_MESSAGE,
                [],
                recorded_message=SUMMARIES_LISTED_RECORD,
            )
        visible = summaries[:RECORD_LIST_LIMIT]
        lines = [listing_line(summary) for summary in visible]
        if len(summaries) > RECORD_LIST_LIMIT:
            lines.append(MORE_SUMMARIES_MESSAGE)
        self.logger.info(
            "Listed conversation summaries through conversation summaries tool."
        )
        return ToolExecutionResult(
            True,
            "\n".join(lines),
            [summary.summary_id for summary in visible],
            recorded_message=SUMMARIES_LISTED_RECORD,
        )

    def _read(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Show one summary; history records only its id."""
        summary_id = arguments.get("id")
        if not is_record_id(summary_id):
            return ToolExecutionResult(False, CONVERSATION_SUMMARY_ID_MESSAGE)
        try:
            summary = self.memory.get_conversation_summary(summary_id)
        except (RuntimeError, ValueError):
            self.logger.warning("Conversation summary read failed.")
            return ToolExecutionResult(False, READ_FAILED_MESSAGE)
        if summary is None:
            return ToolExecutionResult(False, not_found_message(summary_id))
        if type(summary) is not ConversationSummary:
            self.logger.warning("Conversation summary read failed.")
            return ToolExecutionResult(False, READ_FAILED_MESSAGE)

        self.logger.info(
            "Displayed conversation summary %s through conversation summaries "
            "tool.",
            summary_id,
        )
        return ToolExecutionResult(
            True,
            summary_block(summary),
            {"id": summary_id},
            recorded_message=f"Displayed conversation summary {summary_id}.",
        )

    def _edit(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Replace one summary's text by id, turning its consent off."""
        summary_id = arguments.get("id")
        if not is_record_id(summary_id):
            return ToolExecutionResult(False, CONVERSATION_SUMMARY_ID_MESSAGE)
        text = arguments.get("text")
        if not is_summary_text(text):
            return ToolExecutionResult(False, SUMMARY_TEXT_MESSAGE)
        try:
            result = self.memory.edit_conversation_summary(summary_id, text)
        except (RuntimeError, ValueError):
            self.logger.warning("Conversation summary edit failed.")
            return ToolExecutionResult(False, EDIT_FAILED_MESSAGE)
        if not result.updated:
            return ToolExecutionResult(False, not_found_message(summary_id))

        self.logger.info(
            "Edited conversation summary %s through conversation summaries tool.",
            summary_id,
        )
        message = f"Edited conversation summary {summary_id}."
        if result.context_consent_cleared:
            # Consent covered the text the owner approved, and this edit
            # replaced it, so it is off now and the owner is told.
            message = (
                f"{message} Context consent for conversation summary "
                f"{summary_id} was turned off because its text changed."
            )
        return ToolExecutionResult(True, message, {"id": summary_id})

    def _delete(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Delete one summary by id."""
        summary_id = arguments.get("id")
        if not is_record_id(summary_id):
            return ToolExecutionResult(False, CONVERSATION_SUMMARY_ID_MESSAGE)
        try:
            deleted = self.memory.delete_conversation_summary(summary_id)
        except (RuntimeError, ValueError):
            self.logger.warning("Conversation summary deletion failed.")
            return ToolExecutionResult(False, DELETE_FAILED_MESSAGE)
        if deleted is not True:
            return ToolExecutionResult(False, not_found_message(summary_id))
        self.logger.info(
            "Deleted conversation summary %s through conversation summaries tool.",
            summary_id,
        )
        return ToolExecutionResult(
            True,
            f"Deleted conversation summary {summary_id}.",
            {"id": summary_id},
        )

    def _set_context(
        self,
        arguments: Mapping[str, Any],
        *,
        enabled: bool,
    ) -> ToolExecutionResult:
        """Turn one summary's context consent on or off by id.

        Enabling is bound to the text identity pinned when confirmation was
        offered; if the summary's text is no longer exactly that text, or no
        identity was pinned, nothing changes and the owner is told to ask
        again. Disabling only narrows what can be sent and needs none.
        """
        summary_id = arguments.get("id")
        if not is_record_id(summary_id):
            return ToolExecutionResult(False, CONVERSATION_SUMMARY_ID_MESSAGE)
        try:
            if enabled:
                outcome = self.memory.enable_conversation_summary_context(
                    summary_id,
                    arguments.get("text_digest"),
                )
                matched = outcome is not ContextConsentOutcome.NOT_FOUND
            else:
                outcome = None
                matched = self.memory.disable_conversation_summary_context(
                    summary_id
                )
        except (RuntimeError, ValueError):
            self.logger.warning("Conversation summary context change failed.")
            return ToolExecutionResult(False, CONTEXT_FAILED_MESSAGE)

        if matched is not True:
            return ToolExecutionResult(False, not_found_message(summary_id))
        if outcome is ContextConsentOutcome.TARGET_CHANGED:
            return ToolExecutionResult(
                False,
                f"Context was not enabled for conversation summary {summary_id} "
                "because its text could not be confirmed unchanged since "
                "confirmation was requested. Request confirmation again with: "
                f"enable summary context {summary_id}",
            )

        verb = "Enabled" if enabled else "Disabled"
        self.logger.info(
            "%s context consent for conversation summary %s through "
            "conversation summaries tool.",
            verb,
            summary_id,
        )
        return ToolExecutionResult(
            True,
            f"{verb} context for conversation summary {summary_id}.",
            {"id": summary_id, "context_enabled": enabled},
        )


def build_tool(memory: Memory, **_: Any) -> ConversationSummariesTool:
    """Build a conversation-summary tool instance."""
    return ConversationSummariesTool(memory=memory)
