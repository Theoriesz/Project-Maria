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
from core.filesystem_roots import (
    FilesystemProtections,
    FilesystemRoot,
    FilesystemRoots,
    RootPolicy,
)
from core.failures import FailureKind
from core.logger import configure_logging
from core.memory import Memory
from core.tool_manager import (
    BUILT_IN_TOOL_MODULES,
    ToolExecutionResult,
    ToolManager,
)
from tests.log_assertions import assert_log_omits


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

# The one owner-visible sentence an unexpected tool exception produces,
# written out here so the test asserts the contract rather than echoing
# whatever the implementation currently returns.
TOOL_EXECUTION_FAILED_MESSAGE = "Tool execution failed."

# Distinctive values that must never leave the boundary that saw them.
EXCEPTION_SENTINEL = "private-exception-detail-7f3b9c2d1e"
ARGUMENT_SENTINEL = "private-argument-content-5a8e4b06c7"


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
            ToolExecutionResult(False, TOOL_EXECUTION_FAILED_MESSAGE),
        )
        self.assertNotIn("distinct failure detail", result.message)
        self.manager_logger.error.assert_called_once_with(
            "Tool execution failed: %s.%s code=%s",
            "sample",
            "run",
            FailureKind.TOOL_EXECUTION_FAILED.value,
        )
        # exception() would attach the traceback, and with it the very
        # text the response no longer carries.
        self.manager_logger.exception.assert_not_called()
        logged_calls = " ".join(
            str(entry) for entry in self.manager_logger.method_calls
        )
        self.assertNotIn("distinct failure detail", logged_calls)

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


class ToolManagerLogPrivacyTests(unittest.TestCase):
    """Prove an unexpected tool exception reaches neither response nor log.

    This exercises the production logging path rather than a mock: the real
    ``configure_logging()`` installs the real handler, the manager takes the
    real child logger it builds for itself, and the assertions read the file
    that handler actually wrote. Everything stays under a temporary
    directory, so Maria's live log is never opened.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.config = _temporary_config(self.root)
        self.memory = Memory(
            self.config.database_path,
            logger=Mock(spec=logging.Logger),
        )
        self.log_dir = self.root / "logs"
        self.log_file = self.log_dir / "maria.log"

        self.maria_logger = logging.getLogger("maria")
        self.original_handlers = tuple(self.maria_logger.handlers)
        self.original_level = self.maria_logger.level
        self.original_propagate = self.maria_logger.propagate
        for handler in self.original_handlers:
            self.maria_logger.removeHandler(handler)
        self.addCleanup(self._restore_maria_logger)

    def _restore_maria_logger(self) -> None:
        """Close what this test configured and put the previous state back."""
        for handler in list(self.maria_logger.handlers):
            try:
                handler.flush()
            finally:
                self.maria_logger.removeHandler(handler)
                handler.close()
        for handler in self.original_handlers:
            self.maria_logger.addHandler(handler)
        self.maria_logger.setLevel(self.original_level)
        self.maria_logger.propagate = self.original_propagate

    def _load_stub_through_the_real_logging_path(self, tool: _StubTool) -> ToolManager:
        """Load one stub, letting the manager build its own real loggers."""
        manager = ToolManager(
            self.config,
            self.memory,
            contracts=_stub_registry(
                ArgumentContract(
                    "text",
                    str,
                    required=True,
                    allow_blank=True,
                    minimum_length=None,
                    maximum_length=None,
                )
            ),
        )
        module = SimpleNamespace(build_tool=Mock(return_value=tool))
        with (
            patch.object(manager, "discover_tools", return_value=("tools.stub",)),
            patch.object(
                tool_manager_module.importlib,
                "import_module",
                return_value=module,
            ),
        ):
            manager.load_tools()
        return manager

    def _flush(self) -> None:
        """Flush the configured handler so the file holds every record."""
        for handler in self.maria_logger.handlers:
            handler.flush()

    def test_unexpected_tool_exception_reaches_neither_response_nor_log(self) -> None:
        configure_logging(self.log_dir)
        tool = _StubTool("sample", exception=RuntimeError(EXCEPTION_SENTINEL))
        manager = self._load_stub_through_the_real_logging_path(tool)
        self._flush()
        before = self.log_file.read_text(encoding="utf-8")

        # Returning rather than raising is itself the proof that the stub's
        # exception was contained and never escaped the manager.
        result = manager.execute_tool("sample", "run", {"text": ARGUMENT_SENTINEL})

        self._flush()
        added = self.log_file.read_text(encoding="utf-8")[len(before):]

        self.assertFalse(result.success)
        self.assertEqual(result.message, TOOL_EXECUTION_FAILED_MESSAGE)
        self.assertIsNone(result.recorded_message)
        self.assertIsNone(result.data)
        self.assertNotIn(EXCEPTION_SENTINEL, result.message)
        self.assertNotIn(ARGUMENT_SENTINEL, result.message)
        self.assertEqual(tool.call_count, 1)

        assert_log_omits(
            self,
            self.log_file,
            (
                EXCEPTION_SENTINEL,
                ARGUMENT_SENTINEL,
                "RuntimeError",
                "Traceback",
                "_StubTool",
                "object at 0x",
            ),
        )

        # Enumerating every record the call produced proves the omission
        # positively: there is no third line in which a request, a plan, an
        # argument, or an object repr could have been recorded.
        self.assertEqual(
            [line.split(" | ", 1)[1] for line in added.splitlines()],
            [
                "INFO | maria.tool_manager | Executing tool action: sample.run",
                "ERROR | maria.tool_manager | Tool execution failed: sample.run "
                f"code={FailureKind.TOOL_EXECUTION_FAILED.value}",
            ],
        )

    def test_a_successful_action_still_logs_only_its_safe_identity(self) -> None:
        configure_logging(self.log_dir)
        tool = _StubTool("sample")
        manager = self._load_stub_through_the_real_logging_path(tool)
        self._flush()
        before = self.log_file.read_text(encoding="utf-8")

        result = manager.execute_tool("sample", "run", {"text": ARGUMENT_SENTINEL})

        self._flush()
        added = self.log_file.read_text(encoding="utf-8")[len(before):]

        self.assertTrue(result.success)
        self.assertEqual(result.message, "ran sample")
        assert_log_omits(self, self.log_file, (ARGUMENT_SENTINEL,))
        self.assertEqual(
            [line.split(" | ", 1)[1] for line in added.splitlines()],
            ["INFO | maria.tool_manager | Executing tool action: sample.run"],
        )


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
        self.call_count = 0

    def execute(
        self,
        action: str,
        arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        del action
        self.call_count += 1
        self.received_arguments = arguments
        if self.exception is not None:
            raise self.exception
        return self.result


def _temporary_filesystem_protections(root: Path) -> FilesystemProtections:
    """Return active runtime locations beneath a temporary directory."""
    return FilesystemProtections(
        database_path=root / "database" / "maria.db",
        log_dir=root / "logs",
        prompt_path=root / "system_prompt.txt",
    )


def _temporary_filesystem_roots(root: Path) -> FilesystemRoots:
    """Return one enabled organizing root beneath a temporary directory."""
    return FilesystemRoots(
        (
            FilesystemRoot(
                name="workspace",
                path=root,
                policy=RootPolicy.ORGANIZE,
                enabled=True,
            ),
        ),
        "workspace",
    )


def _temporary_config(root: Path) -> MariaConfig:
    """Return a complete config whose paths remain under a temporary root."""
    return MariaConfig(
        app_name="Maria",
        version="0.1.0",
        environment="test",
        database_path=root / "database" / "maria.db",
        log_dir=root / "logs",
        prompt_path=root / "system_prompt.txt",
        filesystem_roots=_temporary_filesystem_roots(root),
        filesystem_protections=_temporary_filesystem_protections(root),
        log_level="INFO",
        confirmation_required=frozenset({"filesystem.rename", "filesystem.move"}),
        denied_actions=frozenset({"filesystem.delete", "filesystem.write"}),
    )


if __name__ == "__main__":
    unittest.main()
