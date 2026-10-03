"""Saved-conversation tool for Maria.

The tool stores an explicit save of the current session's conversation and
lists, reads, and deletes saved conversations, through the memory layer. What
it saves is exactly the transcript Maria herself supplied from the session's
retained exchanges; no request, planner, or model ever supplies it.

A saved conversation is an exact, independent copy: later changes to the
memories, notes, or summaries it may quote do not change it, and it never
becomes model context. The replies to a save and a deletion name only ids and
counts, so the interaction row either records carries no saved text; a
listing and a reading record only a fixed label and an id, never what they
showed. Shown text is escaped, so stored model text cannot hide a character
or pose as Maria's own line. Every failure is a fixed message.
"""

from __future__ import annotations

from collections.abc import Mapping
from logging import Logger
from typing import Any

from core.context import quoted_for_owner
from core.conversation_records import (
    RECORD_LIST_LIMIT,
    RECORD_QUERY_LIMIT,
    SAVED_CONVERSATION_ID_MESSAGE,
    SavedConversation,
    decode_transcript,
    is_record_id,
)
from core.logger import get_logger
from core.memory import Memory
from core.tool_manager import ToolExecutionResult


NO_SAVED_CONVERSATIONS_MESSAGE = "No saved conversations found."
MORE_SAVED_CONVERSATIONS_MESSAGE = (
    f"Showing the newest {RECORD_LIST_LIMIT} saved conversations. More saved "
    "conversations exist."
)
SAVED_CONVERSATIONS_LISTED_RECORD = "Listed saved conversations."
SAVE_FAILED_MESSAGE = "Unable to save the conversation."
LIST_FAILED_MESSAGE = "Unable to list saved conversations."
READ_FAILED_MESSAGE = "Unable to read the saved conversation."
DELETE_FAILED_MESSAGE = "Unable to delete the saved conversation."
UNSUPPORTED_ACTION_MESSAGE = "Unsupported saved-conversation action."


def saved_message(conversation_id: int, exchange_count: int) -> str:
    """Return the reply to a save: only the id and the count vary."""
    return (
        f"Saved this session's {exchange_count} exchanges as saved "
        f"conversation {conversation_id}. It is an exact copy: later changes "
        "to memories or notes do not change it."
    )


def not_found_message(conversation_id: int) -> str:
    """Return the fixed reply for an id no saved conversation has."""
    return f"Saved conversation not found: {conversation_id}."


def listing_line(conversation: SavedConversation) -> str:
    """Render one saved conversation as a content-free listing line."""
    return (
        f"{conversation.conversation_id}: saved at {conversation.created_at}, "
        f"exchanges: {len(conversation.exchanges)}"
    )


def conversation_block(conversation: SavedConversation) -> str:
    """Render one saved conversation in full, every text escaped.

    Each turn is labelled and quoted on its own line, so a saved text cannot
    start a new line, hide a character, or pose as another turn.
    """
    lines = [
        f"Saved conversation {conversation.conversation_id}",
        f"Saved at: {conversation.created_at}",
        f"Exchanges: {len(conversation.exchanges)}",
    ]
    for position, (user, assistant) in enumerate(conversation.exchanges, start=1):
        lines.append(f"{position}. You: {quoted_for_owner(user)}")
        lines.append(f"   Maria: {quoted_for_owner(assistant)}")
    return "\n".join(lines)


class ConversationsTool:
    """Save, list, read, and delete saved conversations."""

    name = "conversations"
    description = (
        "Saves the current session's conversation, and lists, reads, and "
        "deletes saved conversations."
    )
    actions = frozenset({"save", "list", "read", "delete"})

    def __init__(self, memory: Memory, logger: Logger | None = None) -> None:
        self.memory = memory
        self.logger = logger or get_logger(__name__)

    def execute(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> ToolExecutionResult:
        """Execute a saved-conversation action."""
        if action == "save":
            return self._save(arguments)
        if action == "list":
            return self._list()
        if action == "read":
            return self._read(arguments)
        if action == "delete":
            return self._delete(arguments)
        return ToolExecutionResult(False, UNSUPPORTED_ACTION_MESSAGE)

    def _save(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Store the transcript Maria supplied; reply with its id and count."""
        transcript = arguments.get("transcript")
        exchanges = decode_transcript(transcript)
        if exchanges is None:
            self.logger.warning("Conversation save refused: not a transcript.")
            return ToolExecutionResult(False, SAVE_FAILED_MESSAGE)
        try:
            conversation_id = self.memory.save_conversation(transcript)
        except (RuntimeError, ValueError):
            self.logger.warning("Conversation save failed.")
            return ToolExecutionResult(False, SAVE_FAILED_MESSAGE)
        self.logger.info(
            "Saved conversation %s through conversations tool.",
            conversation_id,
        )
        return ToolExecutionResult(
            True,
            saved_message(conversation_id, len(exchanges)),
            {"id": conversation_id, "exchanges": len(exchanges)},
        )

    def _list(self) -> ToolExecutionResult:
        """List the newest saved conversations, saying when more exist.

        One row more than is shown is read, so more are recognised without
        counting them. History records only that a listing happened.
        """
        try:
            conversations = self.memory.list_saved_conversations(
                RECORD_QUERY_LIMIT
            )
        except (RuntimeError, ValueError):
            self.logger.warning("Saved conversation listing failed.")
            return ToolExecutionResult(False, LIST_FAILED_MESSAGE)
        if type(conversations) is not list or any(
            type(conversation) is not SavedConversation
            for conversation in conversations
        ):
            self.logger.warning("Saved conversation listing failed.")
            return ToolExecutionResult(False, LIST_FAILED_MESSAGE)

        if not conversations:
            return ToolExecutionResult(
                True,
                NO_SAVED_CONVERSATIONS_MESSAGE,
                [],
                recorded_message=SAVED_CONVERSATIONS_LISTED_RECORD,
            )
        visible = conversations[:RECORD_LIST_LIMIT]
        lines = [listing_line(conversation) for conversation in visible]
        if len(conversations) > RECORD_LIST_LIMIT:
            lines.append(MORE_SAVED_CONVERSATIONS_MESSAGE)
        self.logger.info("Listed saved conversations through conversations tool.")
        return ToolExecutionResult(
            True,
            "\n".join(lines),
            [conversation.conversation_id for conversation in visible],
            recorded_message=SAVED_CONVERSATIONS_LISTED_RECORD,
        )

    def _read(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Show one saved conversation; history records only its id."""
        conversation_id = arguments.get("id")
        if not is_record_id(conversation_id):
            return ToolExecutionResult(False, SAVED_CONVERSATION_ID_MESSAGE)
        try:
            conversation = self.memory.get_saved_conversation(conversation_id)
        except (RuntimeError, ValueError):
            self.logger.warning("Saved conversation read failed.")
            return ToolExecutionResult(False, READ_FAILED_MESSAGE)
        if conversation is None:
            return ToolExecutionResult(False, not_found_message(conversation_id))
        if type(conversation) is not SavedConversation:
            self.logger.warning("Saved conversation read failed.")
            return ToolExecutionResult(False, READ_FAILED_MESSAGE)

        self.logger.info(
            "Displayed saved conversation %s through conversations tool.",
            conversation_id,
        )
        return ToolExecutionResult(
            True,
            conversation_block(conversation),
            {"id": conversation_id},
            recorded_message=f"Displayed saved conversation {conversation_id}.",
        )

    def _delete(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Delete one saved conversation by id."""
        conversation_id = arguments.get("id")
        if not is_record_id(conversation_id):
            return ToolExecutionResult(False, SAVED_CONVERSATION_ID_MESSAGE)
        try:
            deleted = self.memory.delete_saved_conversation(conversation_id)
        except (RuntimeError, ValueError):
            self.logger.warning("Saved conversation deletion failed.")
            return ToolExecutionResult(False, DELETE_FAILED_MESSAGE)
        if deleted is not True:
            return ToolExecutionResult(False, not_found_message(conversation_id))
        self.logger.info(
            "Deleted saved conversation %s through conversations tool.",
            conversation_id,
        )
        return ToolExecutionResult(
            True,
            f"Deleted saved conversation {conversation_id}.",
            {"id": conversation_id},
        )


def build_tool(memory: Memory, **_: Any) -> ConversationsTool:
    """Build a saved-conversation tool instance."""
    return ConversationsTool(memory=memory)
