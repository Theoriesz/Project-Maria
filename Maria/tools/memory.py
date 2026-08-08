"""Explicit persistent memory tool for Maria."""

from __future__ import annotations

from logging import Logger
from typing import Any

from core.logger import get_logger
from core.memory import Memory
from core.tool_manager import ToolExecutionResult


class MemoryTool:
    """Store and retrieve explicit memories through the memory layer."""

    name = "memory"
    description = "Stores, searches, and lists explicit memories."
    actions = frozenset({"store", "search", "list"})

    def __init__(self, memory: Memory, logger: Logger | None = None) -> None:
        self.memory = memory
        self.logger = logger or get_logger(__name__)

    def execute(
        self,
        action: str,
        arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        """Execute an explicit memory action."""
        if action == "store":
            return self._store(arguments)
        if action == "search":
            return self._search(arguments)
        if action == "list":
            return self._list(arguments)
        return ToolExecutionResult(False, f"Unsupported memory action: {action}")

    def _store(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        """Store one explicit memory."""
        fact = str(arguments.get("fact", "")).strip()

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

    def _search(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        """Search explicit memories."""
        query = str(arguments.get("query", "")).strip()
        try:
            limit = int(arguments.get("limit", 20))
        except (TypeError, ValueError):
            return ToolExecutionResult(False, "Memory search limit must be a number.")

        try:
            memories = self.memory.search_memories(query, limit=limit)
        except (RuntimeError, ValueError) as exc:
            self.logger.warning("Memory search failed: %s", exc)
            return ToolExecutionResult(False, f"Memory search failed: {exc}")

        if not memories:
            return ToolExecutionResult(True, "No matching memories found.", [])

        lines = [f"{memory['id']}: {memory['fact']}" for memory in memories]
        return ToolExecutionResult(True, "\n".join(lines), memories)

    def _list(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        """List explicit memories."""
        try:
            limit = int(arguments.get("limit", 20))
        except (TypeError, ValueError):
            return ToolExecutionResult(False, "Memories limit must be a number.")

        try:
            memories = self.memory.list_memories(limit=limit)
        except RuntimeError as exc:
            self.logger.warning("Memory listing failed: %s", exc)
            return ToolExecutionResult(False, f"Memory listing failed: {exc}")

        if not memories:
            return ToolExecutionResult(True, "No memories found.", [])

        lines = [f"{memory['id']}: {memory['fact']}" for memory in memories]
        return ToolExecutionResult(True, "\n".join(lines), memories)


def build_tool(memory: Memory, **_: Any) -> MemoryTool:
    """Build an explicit memory tool instance."""
    return MemoryTool(memory=memory)
