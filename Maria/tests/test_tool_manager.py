"""Focused regression tests for Maria's Tool Manager."""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, call, patch

import core.tool_manager as tool_manager_module
from core.action_contracts import (
    ActionContract,
    ActionContractRegistry,
    ArgumentContract,
)
from core.config import MariaConfig
from core.memory import Memory
from core.tool_manager import (
    BUILT_IN_TOOL_MODULES,
    ToolExecutionResult,
    ToolManager,
)


EXPECTED_MODULES = (
    "tools.calculator",
    "tools.filesystem",
    "tools.history",
    "tools.memory",
    "tools.notes",
    "tools.system",
)
EXPECTED_TOOL_NAMES = frozenset(
    {"calculator", "filesystem", "history", "memory", "notes", "system"}
)


def _stub_registry(
    *arguments: ArgumentContract,
    tool_name: str = "sample",
    action: str = "run",
) -> ActionContractRegistry:
    """Declare one stub tool action so dispatch tests reach the stub."""
    return ActionContractRegistry((ActionContract(tool_name, action, arguments),))


class ToolManagerTests(unittest.TestCase):
    """Verify tool discovery, loading, and controlled dispatch."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.config = _temporary_config(root)
        self.memory = Memory(
            self.config.database_path,
            logger=Mock(spec=logging.Logger),
        )
        self.manager_logger = Mock(spec=logging.Logger)

    def test_built_in_modules_are_exactly_the_shipped_set(self) -> None:
        self.assertEqual(BUILT_IN_TOOL_MODULES, EXPECTED_MODULES)

    def test_load_imports_all_modules_and_passes_factory_keywords(self) -> None:
        manager = ToolManager(
            self.config,
            self.memory,
            self.manager_logger,
            contracts=ActionContractRegistry(
                ActionContract(module_name.removeprefix("tools."), "run")
                for module_name in EXPECTED_MODULES
            ),
        )
        modules: dict[str, SimpleNamespace] = {}
        factories: dict[str, Mock] = {}
        child_loggers = {
            module_name: Mock(spec=logging.Logger)
            for module_name in EXPECTED_MODULES
        }

        for module_name in EXPECTED_MODULES:
            tool_name = module_name.removeprefix("tools.")
            tool = _StubTool(tool_name)
            factory = Mock(return_value=tool)
            factories[module_name] = factory
            modules[module_name] = SimpleNamespace(build_tool=factory)

        with (
            patch.object(
                tool_manager_module.importlib,
                "import_module",
                side_effect=lambda name: modules[name],
            ) as import_module,
            patch.object(
                tool_manager_module,
                "get_logger",
                side_effect=lambda name: child_loggers[name],
            ) as get_logger,
        ):
            manager.load_tools()

        self.assertEqual(
            import_module.call_args_list,
            [call(module_name) for module_name in EXPECTED_MODULES],
        )
        self.assertEqual(
            get_logger.call_args_list,
            [call(module_name) for module_name in EXPECTED_MODULES],
        )
        for module_name in EXPECTED_MODULES:
            factories[module_name].assert_called_once_with(
                config=self.config,
                memory=self.memory,
                logger=child_loggers[module_name],
            )
            tool_name = module_name.removeprefix("tools.")
            result = manager.execute_tool(tool_name, "run", {})
            self.assertEqual(result, ToolExecutionResult(True, f"ran {tool_name}"))

    def test_real_temporary_load_exposes_all_public_tool_names(self) -> None:
        self.memory.initialize()
        manager = ToolManager(self.config, self.memory, self.manager_logger)

        manager.load_tools()

        for tool_name in EXPECTED_TOOL_NAMES:
            with self.subTest(tool_name=tool_name):
                result = manager.execute_tool(tool_name, "not_an_action", {})
                self.assertEqual(
                    result,
                    ToolExecutionResult(
                        False,
                        f"Unsupported action: {tool_name}.not_an_action "
                        "is not a supported action.",
                    ),
                )
        self.assertEqual(
            manager.execute_tool("not_shipped", "not_an_action", {}),
            ToolExecutionResult(
                False,
                "Unsupported action: not_shipped is not a supported tool.",
            ),
        )

    def test_unknown_tool_is_refused_by_the_action_contract(self) -> None:
        manager = ToolManager(self.config, self.memory, self.manager_logger)

        result = manager.execute_tool("missing", "run", {"value": 1})

        self.assertEqual(
            result,
            ToolExecutionResult(
                False,
                "Unsupported action: missing is not a supported tool.",
            ),
        )
        self.manager_logger.warning.assert_called_once_with(
            "Rejected action by contract: %s %s.%s field=%s",
            "unknown_tool",
            "missing",
            None,
            None,
        )

    def test_declared_but_unloaded_tool_still_reports_it_is_not_loaded(self) -> None:
        manager = ToolManager(self.config, self.memory, self.manager_logger)

        result = manager.execute_tool(
            "calculator",
            "calculate",
            {"expression": "1 + 1"},
        )

        self.assertEqual(
            result,
            ToolExecutionResult(False, "Tool not loaded: calculator"),
        )
        self.manager_logger.error.assert_called_once_with(
            "Tool not loaded: %s",
            "calculator",
        )

    def test_unknown_action_is_refused_by_the_action_contract(self) -> None:
        tool = _StubTool("sample")
        manager = self._load_single_tool(tool)

        result = manager.execute_tool("sample", "missing", {"value": 1})

        self.assertEqual(
            result,
            ToolExecutionResult(
                False,
                "Unsupported action: sample.missing is not a supported action.",
            ),
        )
        self.manager_logger.warning.assert_called_once_with(
            "Rejected action by contract: %s %s.%s field=%s",
            "unknown_action",
            "sample",
            "missing",
            None,
        )

    def test_declared_action_absent_from_the_loaded_tool_is_still_refused(
        self,
    ) -> None:
        tool = _StubTool("sample")
        manager = self._load_single_tool(
            tool,
            contracts=_stub_registry(action="other"),
        )

        result = manager.execute_tool("sample", "other", {})

        self.assertEqual(
            result,
            ToolExecutionResult(False, "Unsupported action for sample: other"),
        )
        self.manager_logger.warning.assert_called_once_with(
            "Unsupported action requested: %s.%s",
            "sample",
            "other",
        )

    def test_arguments_are_forwarded_unchanged(self) -> None:
        expected = ToolExecutionResult(True, "completed", {"answer": 42})
        tool = _StubTool("sample", result=expected)
        manager = self._load_single_tool(
            tool,
            contracts=_stub_registry(
                ArgumentContract("text", str, required=True, allow_blank=True, minimum_length=None, maximum_length=None),
                ArgumentContract("count", int, required=True),
            ),
        )
        arguments: dict[str, Any] = {"text": "value", "count": 3}

        actual = manager.execute_tool("sample", "run", arguments)

        self.assertIs(actual, expected)
        self.assertIs(tool.received_arguments, arguments)

    def test_ordinary_tool_exception_is_contained_and_logged(self) -> None:
        tool = _StubTool(
            "sample",
            exception=RuntimeError("distinct failure detail"),
        )
        manager = self._load_single_tool(tool)

        result = manager.execute_tool("sample", "run", {})

        self.assertEqual(
            result,
            ToolExecutionResult(
                False,
                "Tool execution failed: distinct failure detail",
            ),
        )
        self.manager_logger.exception.assert_called_once_with(
            "Tool execution failed: %s.%s",
            "sample",
            "run",
        )

    def test_missing_module_import_propagates_during_load(self) -> None:
        manager = ToolManager(self.config, self.memory, self.manager_logger)
        with (
            patch.object(manager, "discover_tools", return_value=("tools.missing",)),
            patch.object(
                tool_manager_module.importlib,
                "import_module",
                side_effect=ModuleNotFoundError("missing module"),
            ),
        ):
            with self.assertRaisesRegex(ModuleNotFoundError, "missing module"):
                manager.load_tools()

    def test_missing_build_tool_propagates_during_load(self) -> None:
        manager = ToolManager(self.config, self.memory, self.manager_logger)
        with (
            patch.object(manager, "discover_tools", return_value=("tools.incomplete",)),
            patch.object(
                tool_manager_module.importlib,
                "import_module",
                return_value=SimpleNamespace(),
            ),
        ):
            with self.assertRaises(AttributeError):
                manager.load_tools()

    def test_factory_failure_propagates_during_load(self) -> None:
        manager = ToolManager(self.config, self.memory, self.manager_logger)
        module = SimpleNamespace(
            build_tool=Mock(side_effect=RuntimeError("factory failed"))
        )
        with (
            patch.object(manager, "discover_tools", return_value=("tools.broken",)),
            patch.object(
                tool_manager_module.importlib,
                "import_module",
                return_value=module,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "factory failed"):
                manager.load_tools()

    def test_repeated_real_loading_keeps_exact_public_tool_names(self) -> None:
        self.memory.initialize()
        manager = ToolManager(self.config, self.memory, self.manager_logger)

        manager.load_tools()
        manager.load_tools()

        for tool_name in EXPECTED_TOOL_NAMES:
            with self.subTest(tool_name=tool_name):
                result = manager.execute_tool(tool_name, "not_an_action", {})
                self.assertEqual(
                    result,
                    ToolExecutionResult(
                        False,
                        f"Unsupported action: {tool_name}.not_an_action "
                        "is not a supported action.",
                    ),
                )
        self.assertEqual(
            manager.execute_tool("not_shipped", "not_an_action", {}),
            ToolExecutionResult(
                False,
                "Unsupported action: not_shipped is not a supported tool.",
            ),
        )

    def test_successful_dispatch_logging_omits_sensitive_arguments(self) -> None:
        tool = _StubTool("sample")
        manager = self._load_single_tool(
            tool,
            contracts=_stub_registry(
                ArgumentContract(
                    "secret",
                    str,
                    required=True,
                    allow_blank=True,
                    minimum_length=None,
                    maximum_length=None,
                )
            ),
        )
        sensitive_value = "sensitive-value-9d5fc1f6f4"

        result = manager.execute_tool(
            "sample",
            "run",
            {"secret": sensitive_value},
        )

        self.assertTrue(result.success)
        logged_calls = " ".join(str(entry) for entry in self.manager_logger.method_calls)
        self.assertNotIn(sensitive_value, logged_calls)

    def _load_single_tool(
        self,
        tool: _StubTool,
        contracts: ActionContractRegistry | None = None,
    ) -> ToolManager:
        """Load one stub through the public loading flow."""
        manager = ToolManager(
            self.config,
            self.memory,
            self.manager_logger,
            contracts=_stub_registry() if contracts is None else contracts,
        )
        module = SimpleNamespace(build_tool=Mock(return_value=tool))
        with (
            patch.object(manager, "discover_tools", return_value=("tools.stub",)),
            patch.object(
                tool_manager_module.importlib,
                "import_module",
                return_value=module,
            ),
            patch.object(
                tool_manager_module,
                "get_logger",
                return_value=Mock(spec=logging.Logger),
            ),
        ):
            manager.load_tools()
        self.manager_logger.reset_mock()
        return manager


class _StubTool:
    """Small deterministic tool used to exercise manager dispatch."""

    description = "Test-only tool."
    actions = frozenset({"run"})

    def __init__(
        self,
        name: str,
        result: ToolExecutionResult | None = None,
        exception: Exception | None = None,
    ) -> None:
        self.name = name
        self.result = result or ToolExecutionResult(True, f"ran {name}")
        self.exception = exception
        self.received_arguments: dict[str, Any] | None = None

    def execute(
        self,
        action: str,
        arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        del action
        self.received_arguments = arguments
        if self.exception is not None:
            raise self.exception
        return self.result


def _temporary_config(root: Path) -> MariaConfig:
    """Return a complete config whose paths remain under a temporary root."""
    return MariaConfig(
        app_name="Maria",
        version="0.1.0",
        environment="test",
        database_path=root / "database" / "maria.db",
        log_dir=root / "logs",
        prompt_path=root / "system_prompt.txt",
        safe_filesystem_root=root,
        log_level="INFO",
        confirmation_required=frozenset({"filesystem.rename", "filesystem.move"}),
        denied_actions=frozenset({"filesystem.delete", "filesystem.write"}),
    )


if __name__ == "__main__":
    unittest.main()
