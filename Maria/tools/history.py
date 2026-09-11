"""Interaction-history browsing tool for Maria."""

from __future__ import annotations

from logging import Logger
from typing import Any

from core.logger import get_logger
from core.memory import Memory
from core.tool_manager import ToolExecutionResult


_DEFAULT_LIST_LIMIT = 20
_MORE_INTERACTIONS_SUFFIX = "Showing the newest 20 interactions. More interactions exist."
_MORE_MATCHES_SUFFIX = "Showing the newest 20 matches. More matching interactions exist."


def _display_snippet(user_input: Any) -> str:
    """Return a single-line preview with non-printable characters neutralized."""
    return "".join(
        character if character.isprintable() else " " for character in str(user_input)
    )


def _row_line(row: dict[str, Any]) -> str:
    """Render one interaction as a single display-safe list/search row."""
    return f"{row['id']}: {_display_snippet(row['user_input'])}"


def _row_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Return the redacted list/search data shape for one interaction row."""
    return {
        "id": row["id"],
        "user_input": row["user_input"],
        "created_at": row["created_at"],
    }


class HistoryTool:
    """List, search, and read interaction-history rows through the memory layer."""

    name = "history"
    description = "Lists, searches, and reads interaction-history rows."
    actions = frozenset({"list", "search", "read"})

    def __init__(self, memory: Memory, logger: Logger | None = None) -> None:
        self.memory = memory
        self.logger = logger or get_logger(__name__)

    def execute(self, action: str, arguments: dict[str, Any]) -> ToolExecutionResult:
        """Execute a history action."""
        if action == "list":
            return self._list(arguments)
        if action == "search":
            return self._search(arguments)
        if action == "read":
            return self._read(arguments)
        return ToolExecutionResult(False, f"Unsupported history action: {action}")

    def _list(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        """List interactions newest first."""
        del arguments
        try:
            interactions = self.memory.get_recent_interactions(
                limit=_DEFAULT_LIST_LIMIT + 1
            )
        except RuntimeError as exc:
            self.logger.warning("History listing failed: %s", exc)
            return ToolExecutionResult(False, f"History listing failed: {exc}")

        if not interactions:
            return ToolExecutionResult(True, "No interactions found.", [])

        has_more = len(interactions) > _DEFAULT_LIST_LIMIT
        visible = interactions[:_DEFAULT_LIST_LIMIT]
        message = "\n".join(_row_line(row) for row in visible)
        if has_more:
            message = f"{message}\n{_MORE_INTERACTIONS_SUFFIX}"
        return ToolExecutionResult(True, message, [_row_summary(row) for row in visible])

    def _search(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        """Search interactions by literal, case-insensitive substring."""
        query = str(arguments.get("query", "")).strip()
        if not query:
            return ToolExecutionResult(False, "Search query is required.")

        try:
            interactions = self.memory.search_interactions(
                query, limit=_DEFAULT_LIST_LIMIT + 1
            )
        except (RuntimeError, ValueError) as exc:
            self.logger.warning("History search failed: %s", exc)
            return ToolExecutionResult(False, f"History search failed: {exc}")

        if not interactions:
            return ToolExecutionResult(True, "No matching interactions found.", [])

        has_more = len(interactions) > _DEFAULT_LIST_LIMIT
        visible = interactions[:_DEFAULT_LIST_LIMIT]
        message = "\n".join(_row_line(row) for row in visible)
        if has_more:
            message = f"{message}\n{_MORE_MATCHES_SUFFIX}"
        return ToolExecutionResult(True, message, [_row_summary(row) for row in visible])

    def _read(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        """Read one interaction by id."""
        interaction_id = arguments.get("id")
        if (
            not isinstance(interaction_id, int)
            or isinstance(interaction_id, bool)
            or interaction_id < 1
        ):
            return ToolExecutionResult(
                False, "Interaction id must be a positive whole number."
            )

        try:
            interaction = self.memory.get_interaction(interaction_id)
        except RuntimeError as exc:
            self.logger.warning("History read failed: %s", exc)
            return ToolExecutionResult(False, f"History read failed: {exc}")

        if interaction is None:
            return ToolExecutionResult(False, f"Interaction not found: {interaction_id}.")

        message = (
            f"Interaction {interaction['id']}\n"
            f"Created at: {interaction['created_at']}\n"
            f"Request:\n"
            f"{interaction['user_input']}\n"
            f"Response:\n"
            f"{interaction['response']}"
        )
        self.logger.info("Displayed interaction %s through history tool.", interaction_id)
        return ToolExecutionResult(
            True,
            message,
            interaction,
            recorded_message=f"Displayed interaction {interaction_id}.",
        )


def build_tool(memory: Memory, **_: Any) -> HistoryTool:
    """Build a history tool instance."""
    return HistoryTool(memory=memory)
