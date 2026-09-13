"""Tool discovery, loading, and execution for Maria."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from logging import Logger
from typing import Any, Protocol

from core.config import MariaConfig
from core.logger import get_logger
from core.memory import Memory


BUILT_IN_TOOL_MODULES = (
    "tools.calculator",
    "tools.filesystem",
    "tools.history",
    "tools.memory",
    "tools.notes",
    "tools.system",
)


@dataclass(frozen=True)
class ToolExecutionResult:
    """Result returned by a tool execution."""

    success: bool
    message: str
    data: Any = None
    recorded_message: str | None = None


class Tool(Protocol):
    """Minimal interface every Maria tool must provide."""

    name: str
    description: str
    actions: frozenset[str]

    def execute(
        self,
        action: str,
        arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        """Execute a supported action."""


class ToolManager:
    """Discover, load, and execute Maria tools."""

    def __init__(
        self,
        config: MariaConfig,
        memory: Memory,
        logger: Logger | None = None,
    ) -> None:
        self.config = config
        self.memory = memory
        self.logger = logger or get_logger(__name__)
        self._tools: dict[str, Tool] = {}

    def discover_tools(self) -> tuple[str, ...]:
        """Return import paths for built-in tools."""
        return BUILT_IN_TOOL_MODULES

    def load_tools(self) -> None:
        """Import and register built-in tools."""
        for module_name in self.discover_tools():
            module = importlib.import_module(module_name)
            tool = module.build_tool(
                config=self.config,
                memory=self.memory,
                logger=get_logger(module_name),
            )
            self._tools[tool.name] = tool
            self.logger.info("Loaded tool: %s", tool.name)

    def execute_tool(
        self,
        tool_name: str,
        action: str,
        arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        """Execute an action on a loaded tool."""
        tool = self._tools.get(tool_name)
        if tool is None:
            self.logger.error("Tool not loaded: %s", tool_name)
            return ToolExecutionResult(False, f"Tool not loaded: {tool_name}")

        if action not in tool.actions:
            self.logger.warning("Unsupported action requested: %s.%s", tool_name, action)
            return ToolExecutionResult(
                False,
                f"Unsupported action for {tool_name}: {action}",
            )

        try:
            self.logger.info("Executing tool action: %s.%s", tool_name, action)
            return tool.execute(action, arguments)
        except Exception as exc:  # noqa: BLE001 - tools must not crash the app.
            self.logger.exception("Tool execution failed: %s.%s", tool_name, action)
            return ToolExecutionResult(False, f"Tool execution failed: {exc}")
