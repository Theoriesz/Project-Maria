"""Tool discovery, loading, and execution for Maria."""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from logging import Logger
from typing import Any, Protocol

from core.action_contracts import (
    BUILT_IN_ACTION_CONTRACTS,
    ActionContractRegistry,
    ContractViolation,
)
from core.config import MariaConfig
from core.failures import Failure, FailureKind
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
        arguments: Mapping[str, Any],
    ) -> ToolExecutionResult:
        """Execute a supported action."""


class ToolManager:
    """Discover, load, and execute Maria tools."""

    def __init__(
        self,
        config: MariaConfig,
        memory: Memory,
        logger: Logger | None = None,
        contracts: ActionContractRegistry | None = None,
    ) -> None:
        self.config = config
        self.memory = memory
        self.logger = logger or get_logger(__name__)
        self.contracts = (
            BUILT_IN_ACTION_CONTRACTS if contracts is None else contracts
        )
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

    def loaded_action_identifiers(self) -> frozenset[str]:
        """Return a read-only snapshot of every loaded tool action.

        The snapshot exists so the declared registry can be checked against
        the tools actually loaded. The registry is never populated from it.
        """
        return frozenset(
            f"{tool_name}.{action}"
            for tool_name, tool in self._tools.items()
            for action in tool.actions
        )

    def validate_action(
        self,
        tool_name: str,
        action: str,
        arguments: Mapping[str, Any],
    ) -> ContractViolation | None:
        """Return the first contract violation for a call, without executing it.

        This is the non-executing half of the dispatch boundary, so a caller
        can settle whether an action and its arguments are supported before
        anything else in the pipeline runs.
        """
        return self.contracts.validate(tool_name, action, arguments)

    def execute_tool(
        self,
        tool_name: str,
        action: str,
        arguments: Mapping[str, Any],
    ) -> ToolExecutionResult:
        """Execute an action on a loaded tool after validating its contract.

        Contract validation runs first and unconditionally, so a direct
        caller cannot reach a tool with arguments the central registry
        rejects. Brain validates a whole plan earlier for ordering reasons;
        this pass is the defensive one that no caller can skip.

        An unexpected exception from a tool is contained here rather than
        crashing Maria, and neither the exception nor its text reaches the
        owner or the log. The result carries the shared typed failure's
        fixed message, and the log records the validated action identity
        and the failure code only. A tool that has a safe, specific
        diagnostic still logs it itself, where it knows what its own
        failure is allowed to say.
        """
        violation = self.validate_action(tool_name, action, arguments)
        if violation is not None:
            self.logger.warning(
                "Rejected action by contract: %s %s.%s field=%s",
                violation.kind.value,
                violation.tool_name,
                violation.action,
                violation.field,
            )
            return ToolExecutionResult(False, violation.message)

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
        except Exception:  # noqa: BLE001 - tools must not crash the app.
            # The exception is deliberately not bound, so no path here can
            # format its text, its args, or its traceback into a response or
            # a log record. Both names logged below already matched a
            # declared contract entry above, so neither carries caller text.
            failure = Failure(FailureKind.TOOL_EXECUTION_FAILED)
            self.logger.error(
                "Tool execution failed: %s.%s code=%s",
                tool_name,
                action,
                failure.code,
            )
            return ToolExecutionResult(False, failure.message)
