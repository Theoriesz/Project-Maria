"""Explicit persistent memory tool for Maria."""

from __future__ import annotations

from collections.abc import Mapping
from logging import Logger
from typing import Any

from core.action_contracts import MAXIMUM_LIST_LIMIT, MINIMUM_LIST_LIMIT
from core.logger import get_logger
from core.memory import Memory
from core.tool_manager import ToolExecutionResult


_DEFAULT_LIST_LIMIT = 20
_MORE_MEMORIES_SUFFIX = "Showing the newest 20 memories. More memories exist."
_MORE_MATCHES_SUFFIX = "Showing the newest 20 matches. More matching memories exist."
_LIST_LIMIT_RANGE_MESSAGE = (
    f"Memories limit must be between {MINIMUM_LIST_LIMIT} and {MAXIMUM_LIST_LIMIT}."
)
_SEARCH_LIMIT_RANGE_MESSAGE = (
    "Memory search limit must be between "
    f"{MINIMUM_LIST_LIMIT} and {MAXIMUM_LIST_LIMIT}."
)


def _split_visible_rows(
    rows: list[dict[str, Any]],
    probe_default: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """Split a limit-plus-one probe result into visible rows and a more-exists flag."""
    if not probe_default:
        return rows, False
    return rows[:_DEFAULT_LIST_LIMIT], len(rows) > _DEFAULT_LIST_LIMIT


class MemoryTool:
    """Store, retrieve, update, and forget explicit memories through the memory layer.

    It also turns one memory's context consent on or off. Those results name
    the memory's id and never its fact, so the interaction row a consent
    change records carries no stored text.
    """

    name = "memory"
    description = (
        "Stores, searches, lists, updates, and forgets explicit memories, "
        "and turns one memory's context consent on or off."
    )
    actions = frozenset(
        {
            "store",
            "search",
            "list",
            "update",
            "forget",
            "enable_context",
            "disable_context",
        }
    )

    def __init__(self, memory: Memory, logger: Logger | None = None) -> None:
        self.memory = memory
        self.logger = logger or get_logger(__name__)

    def execute(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> ToolExecutionResult:
        """Execute an explicit memory action."""
        if action == "store":
            return self._store(arguments)
        if action == "search":
            return self._search(arguments)
        if action == "list":
            return self._list(arguments)
        if action == "update":
            return self._update(arguments)
        if action == "forget":
            return self._forget(arguments)
        if action == "enable_context":
            return self._set_context(arguments, enabled=True)
        if action == "disable_context":
            return self._set_context(arguments, enabled=False)
        return ToolExecutionResult(False, f"Unsupported memory action: {action}")

    def _store(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Store one explicit memory."""
        fact = arguments.get("fact", "")
        if type(fact) is not str:
            return ToolExecutionResult(False, "Memory fact must be text.")

        fact = fact.strip()

        try:
            memory_id, created = self.memory.store_memory(fact)
        except (RuntimeError, ValueError) as exc:
            self.logger.warning("Memory storage failed: %s", exc)
            return ToolExecutionResult(False, f"Memory storage failed: {exc}")

        if created:
            stored_fact = fact
            message = f"Remembered memory {memory_id}: {stored_fact}"
            self.logger.info("Stored memory through memory tool: %s", memory_id)
        else:
            try:
                existing_memory = self.memory.get_memory(memory_id)
            except RuntimeError as exc:
                self.logger.warning("Memory storage failed: %s", exc)
                return ToolExecutionResult(False, f"Memory storage failed: {exc}")

            if existing_memory is None:
                failure = "Stored memory could not be retrieved."
                self.logger.warning(
                    "Memory storage failed for memory %s: %s",
                    memory_id,
                    failure,
                )
                return ToolExecutionResult(False, f"Memory storage failed: {failure}")

            stored_fact = existing_memory["fact"]
            message = f"Already remembered as memory {memory_id}: {stored_fact}"
            self.logger.info(
                "Resolved existing memory through memory tool: %s",
                memory_id,
            )

        return ToolExecutionResult(
            True,
            message,
            {"id": memory_id, "fact": stored_fact, "created": created},
        )

    def _search(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Search explicit memories."""
        query = arguments.get("query", "")
        if type(query) is not str:
            return ToolExecutionResult(False, "Memory search query must be text.")

        query = query.strip()
        limit = arguments.get("limit", _DEFAULT_LIST_LIMIT)
        if type(limit) is not int:
            return ToolExecutionResult(False, "Memory search limit must be a number.")
        if not MINIMUM_LIST_LIMIT <= limit <= MAXIMUM_LIST_LIMIT:
            return ToolExecutionResult(False, _SEARCH_LIMIT_RANGE_MESSAGE)

        probe_default = limit == _DEFAULT_LIST_LIMIT
        query_limit = limit + 1 if probe_default else limit
        try:
            memories = self.memory.search_memories(query, limit=query_limit)
        except (RuntimeError, ValueError) as exc:
            self.logger.warning("Memory search failed: %s", exc)
            return ToolExecutionResult(False, f"Memory search failed: {exc}")

        if not memories:
            return ToolExecutionResult(True, "No matching memories found.", [])

        visible_memories, has_more = _split_visible_rows(memories, probe_default)
        lines = [f"{memory['id']}: {memory['fact']}" for memory in visible_memories]
        message = "\n".join(lines)
        if has_more:
            message = f"{message}\n{_MORE_MATCHES_SUFFIX}"
        return ToolExecutionResult(True, message, visible_memories)

    def _list(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """List explicit memories."""
        limit = arguments.get("limit", _DEFAULT_LIST_LIMIT)
        if type(limit) is not int:
            return ToolExecutionResult(False, "Memories limit must be a number.")
        if not MINIMUM_LIST_LIMIT <= limit <= MAXIMUM_LIST_LIMIT:
            return ToolExecutionResult(False, _LIST_LIMIT_RANGE_MESSAGE)

        probe_default = limit == _DEFAULT_LIST_LIMIT
        query_limit = limit + 1 if probe_default else limit
        try:
            memories = self.memory.list_memories(limit=query_limit)
        except RuntimeError as exc:
            self.logger.warning("Memory listing failed: %s", exc)
            return ToolExecutionResult(False, f"Memory listing failed: {exc}")

        if not memories:
            return ToolExecutionResult(True, "No memories found.", [])

        visible_memories, has_more = _split_visible_rows(memories, probe_default)
        lines = [f"{memory['id']}: {memory['fact']}" for memory in visible_memories]
        message = "\n".join(lines)
        if has_more:
            message = f"{message}\n{_MORE_MEMORIES_SUFFIX}"
        return ToolExecutionResult(True, message, visible_memories)

    def _update(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Replace one memory's fact by id."""
        memory_id = arguments.get("id")
        if (
            not isinstance(memory_id, int)
            or isinstance(memory_id, bool)
            or memory_id < 1
        ):
            return ToolExecutionResult(
                False,
                "Memory id must be a positive whole number.",
            )

        new_fact = arguments.get("fact")
        if not isinstance(new_fact, str) or not new_fact.strip():
            return ToolExecutionResult(False, "New memory fact is required.")

        try:
            updated, conflicting_id, consent_cleared = self.memory.update_memory(
                memory_id,
                new_fact.strip(),
            )
        except (RuntimeError, ValueError) as exc:
            self.logger.warning("Memory update failed: %s", exc)
            return ToolExecutionResult(False, f"Memory update failed: {exc}")

        if updated:
            self.logger.info("Updated memory %s through memory tool.", memory_id)
            message = f"Updated memory {memory_id}."
            if consent_cleared:
                # Consent covered the text the owner approved, which this
                # update replaced, so it is off now and the owner is told.
                message = (
                    f"{message} Context consent for memory {memory_id} was "
                    "turned off because its text changed."
                )
            return ToolExecutionResult(
                True,
                message,
                {"id": memory_id},
            )

        if conflicting_id is not None:
            return ToolExecutionResult(
                False,
                f"Memory {memory_id} conflicts with existing memory "
                f"{conflicting_id}.",
            )

        return ToolExecutionResult(False, f"Memory not found: {memory_id}.")

    def _forget(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Forget one memory by id."""
        memory_id = arguments.get("id")
        if (
            not isinstance(memory_id, int)
            or isinstance(memory_id, bool)
            or memory_id < 1
        ):
            return ToolExecutionResult(
                False,
                "Memory id must be a positive whole number.",
            )

        try:
            forgotten = self.memory.forget_memory(memory_id)
        except (RuntimeError, ValueError) as exc:
            self.logger.warning("Memory forget failed: %s", exc)
            return ToolExecutionResult(False, f"Memory forget failed: {exc}")

        if not forgotten:
            return ToolExecutionResult(False, f"Memory not found: {memory_id}.")

        self.logger.info("Forgot memory %s through memory tool.", memory_id)
        return ToolExecutionResult(
            True,
            f"Forgot memory {memory_id}.",
            {"id": memory_id},
        )

    def _set_context(
        self,
        arguments: Mapping[str, Any],
        *,
        enabled: bool,
    ) -> ToolExecutionResult:
        """Turn one memory's context consent on or off by id."""
        memory_id = arguments.get("id")
        if (
            not isinstance(memory_id, int)
            or isinstance(memory_id, bool)
            or memory_id < 1
        ):
            return ToolExecutionResult(
                False,
                "Memory id must be a positive whole number.",
            )

        try:
            matched = self.memory.set_memory_context(memory_id, enabled)
        except (RuntimeError, ValueError) as exc:
            self.logger.warning("Memory context change failed: %s", exc)
            return ToolExecutionResult(
                False,
                f"Memory context change failed: {exc}",
            )

        if not matched:
            return ToolExecutionResult(False, f"Memory not found: {memory_id}.")

        verb = "Enabled" if enabled else "Disabled"
        self.logger.info(
            "%s context consent for memory %s through memory tool.",
            verb,
            memory_id,
        )
        return ToolExecutionResult(
            True,
            f"{verb} context for memory {memory_id}.",
            {"id": memory_id, "context_enabled": enabled},
        )


def build_tool(memory: Memory, **_: Any) -> MemoryTool:
    """Build an explicit memory tool instance."""
    return MemoryTool(memory=memory)
