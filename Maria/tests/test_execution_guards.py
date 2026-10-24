"""The repository-wide execution guard and the stage's ownership guards.

V1-24-R1 adds no way to execute anything. Statically, over every production
module found rather than listed, no module imports a process or
code-execution module, names a process function of ``os``, or uses
``eval``, ``exec``, ``compile``, ``__import__``, or ``breakpoint``. The one
dynamic import is registered-tool dispatch, ToolManager's loader importing
each listed built-in tool module, which is explicitly exempt and left
unaffected. In a run of every project command and of model requests with
and without file parts, every process and code-execution function is made
to fail, and none is reached.

The same scans prove the stage's ownership rules: only
``core/project_file_access.py`` names the nine Windows functions project
file reading uses; only ``tools/projects.py`` changes the activation
setting, through the memory layer's two methods, whose SQL is the only SQL
that writes it; and the SQL boundary's own scan covers every new module.
Nothing here starts a process, contacts a model, or reads Maria's live
database, live log, or private override.
"""

from __future__ import annotations

import ast
import builtins
import logging
import os
import subprocess
import sys
import unittest
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from unittest.mock import Mock, patch

import core.project_files as project_files
from core.brain import MariaBrain
from core.project_files import ProjectFileReader
from core.project_records import ActivationState
from tests.project_fakes import (
    WORKSPACE_PATH,
    FakeAccess,
    FakeVolume,
    StubProtections,
    StubRoots,
)
from tests.test_conversation import ScriptedProvider, _Harness
from tests.test_memory import (
    MARIA_ROOT,
    _package_of,
    _parse,
    database_driver_imports,
    executable_sql,
    production_modules,
)
from tests.test_project_context import REPLY, _harness


# The production modules this stage adds, V1-24-R1's and V1-25-R1's.
NEW_PRODUCTION_MODULES = (
    "core/file_organization.py",
    "core/organize_access.py",
    "core/project_file_access.py",
    "core/project_files.py",
    "core/project_maria_manifest.py",
    "core/project_records.py",
    "tools/projects.py",
)

# Modules whose import alone offers a way to run another program or code.
EXECUTION_MODULES = frozenset(
    {
        "asyncio",
        "builtins",
        "code",
        "codeop",
        "concurrent",
        "marshal",
        "multiprocessing",
        "pexpect",
        "pickle",
        "pty",
        "runpy",
        "subprocess",
        "webbrowser",
    }
)

# The functions of ``os`` that start or replace a process.
OS_PROCESS_FUNCTIONS = frozenset(
    {
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "fork",
        "forkpty",
        "popen",
        "posix_spawn",
        "posix_spawnp",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "startfile",
        "system",
    }
)

# Builtins that run, compile, or import code handed to them as data.
CODE_BUILTINS = frozenset({"__import__", "breakpoint", "compile", "eval", "exec"})

# The one exempt dynamic import: ToolManager's loader of registered tools.
TOOL_LOADER = "core/tool_manager.py"

# The nine Windows functions project file reading calls (D13, W).
WINDOWS_FUNCTIONS = frozenset(
    {
        "CloseHandle",
        "CreateFileW",
        "FindClose",
        "FindFirstFileExW",
        "FindNextFileW",
        "GetFileInformationByHandle",
        "GetFileInformationByHandleEx",
        "GetFinalPathNameByHandleW",
        "RtlSetThreadPlaceholderCompatibilityMode",
    }
)

# V1-25-R1: the seven Windows functions file organization calls (four of them
# are bound separately by the project reader too), and the two the closed-app
# notice calls. Each ctypes module owns exactly the functions its row names.
ORGANIZE_FUNCTIONS = frozenset(
    {
        "CloseHandle",
        "CreateFileW",
        "GetFileInformationByHandle",
        "GetFinalPathNameByHandleW",
        "GetVolumeInformationByHandleW",
        "NtCreateFile",
        "NtSetInformationFile",
    }
)
NOTICE_FUNCTIONS = frozenset({"MessageBoxW", "SHQueryUserNotificationState"})
OWNED_WINDOWS_FUNCTIONS = {
    "core/desktop_notice.py": NOTICE_FUNCTIONS,
    "core/organize_access.py": ORGANIZE_FUNCTIONS,
    "core/project_file_access.py": WINDOWS_FUNCTIONS,
}

ACTIVATION_METHODS = frozenset({"activate_project", "deactivate_project"})

# Every project command and two model requests, one naming a file.
SCRIPT = (
    "create project Garden: workspace/garden",
    "projects",
    "read project 1",
    "retitle project 1: Garden plans",
    "add project fact 1: Beds are raised.",
    "project facts 1",
    "edit project fact 1: Beds are raised and mulched.",
    "activate project 1",
    "project files",
    "read project file plan.md",
    "read project file plan.md lines 1-1",
    "Review @plan.md please",
    "What should I check next?",
    "deactivate project",
    "delete project fact 1",
    "delete project 1",
)


def _relative(module: object) -> str:
    """Return a production module's path relative to the Maria folder."""
    return module.relative_to(MARIA_ROOT).as_posix()


def _is_forbidden_import(name: str, relative: str) -> bool:
    """Return whether importing ``name`` would give a module a way to execute."""
    root = name.split(".")[0]
    return root in EXECUTION_MODULES or (
        root == "importlib" and relative != TOOL_LOADER
    )


def _execution_findings(tree: ast.Module, relative: str) -> list[str]:
    """Return every way to execute something that ``tree`` imports or names."""
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            findings.extend(
                f"import {alias.name}"
                for alias in node.names
                if _is_forbidden_import(alias.name, relative)
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            if _is_forbidden_import(module, relative):
                findings.append(f"from {module} import")
            if module == "os":
                findings.extend(
                    f"from os import {alias.name}"
                    for alias in node.names
                    if alias.name in OS_PROCESS_FUNCTIONS
                )
        elif isinstance(node, ast.Attribute):
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and node.attr in OS_PROCESS_FUNCTIONS
            ):
                findings.append(f"os.{node.attr}")
        elif isinstance(node, ast.Name) and node.id in CODE_BUILTINS:
            findings.append(node.id)
    return findings


def _named_windows_functions(
    tree: ast.Module,
    functions: frozenset[str] = WINDOWS_FUNCTIONS,
) -> set[str]:
    """Return which of the given Windows functions a module names in code."""
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in functions:
            named.add(node.attr)
        elif isinstance(node, ast.Name) and node.id in functions:
            named.add(node.id)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in functions
        ):
            named.add(node.value)
    return named


def _writes_activation(text: str) -> bool:
    """Return whether SQL text inserts, updates, or deletes activation rows."""
    folded = " ".join(text.split()).upper()
    return any(
        statement in folded
        for statement in (
            "INSERT INTO PROJECT_ACTIVATION",
            "UPDATE PROJECT_ACTIVATION",
            "DELETE FROM PROJECT_ACTIVATION",
        )
    )


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    """Return every function and method of a module by name."""
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }


def _callers(functions: dict[str, ast.FunctionDef], name: str) -> set[str]:
    """Return the functions whose bodies call ``name``, as a name or a method."""
    found: set[str] = set()
    for caller, node in functions.items():
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and (
                (isinstance(inner.func, ast.Name) and inner.func.id == name)
                or (isinstance(inner.func, ast.Attribute) and inner.func.attr == name)
            ):
                found.add(caller)
    return found


def _is_production(module_name: str) -> bool:
    """Return whether a module name is one of Maria's production modules."""
    return module_name in ("main", "deliver_reminders") or module_name.startswith(
        ("core.", "tools.", "gui.")
    )


@contextmanager
def _execution_refused() -> Iterator[list[str]]:
    """Make every process and code-execution function fail, recording calls.

    A process function fails whoever calls it. A code builtin fails when a
    production module calls it directly; the standard library's own use,
    such as building a class, still works, so only Maria's code is judged.
    """
    attempted: list[str] = []

    def refusing(
        label: str,
        original: Callable[..., object] | None = None,
    ) -> Callable[..., object]:
        def call(*arguments: object, **keywords: object) -> object:
            if original is not None:
                caller = sys._getframe(1).f_globals.get("__name__", "")
                if not _is_production(caller):
                    return original(*arguments, **keywords)
            attempted.append(label)
            raise AssertionError(f"Maria never calls {label}.")

        return call

    with ExitStack() as stack:
        for name in ("Popen", "run", "call", "check_call", "check_output"):
            stack.enter_context(
                patch.object(subprocess, name, refusing(f"subprocess.{name}"))
            )
        for name in sorted(OS_PROCESS_FUNCTIONS):
            if hasattr(os, name):
                stack.enter_context(patch.object(os, name, refusing(f"os.{name}")))
        for name in ("eval", "exec", "compile"):
            stack.enter_context(
                patch.object(builtins, name, refusing(name, getattr(builtins, name)))
            )
        yield attempted


class StaticExecutionGuardTests(unittest.TestCase):
    """No production module can start a process or run code it was handed."""

    def test_the_scan_covers_every_production_module_and_this_stages(self) -> None:
        found = {_relative(module) for module in production_modules()}

        for relative in NEW_PRODUCTION_MODULES:
            with self.subTest(module=relative):
                self.assertIn(relative, found)
        self.assertIn("main.py", found)
        self.assertIn("deliver_reminders.py", found)
        self.assertIn("gui/window.py", found)

    def test_no_production_module_can_start_a_process_or_run_code(self) -> None:
        for module in production_modules():
            relative = _relative(module)
            with self.subTest(module=relative):
                self.assertEqual(_execution_findings(_parse(module), relative), [])

    def test_the_scan_finds_everything_it_forbids(self) -> None:
        # A guard that found nothing in source built to fail would prove nothing.
        sample = ast.parse(
            "import subprocess\n"
            "import importlib.util\n"
            "from os import system\n"
            "from multiprocessing import Process\n"
            "os.startfile('x')\n"
            "eval('1')\n"
            "exec('x = 1')\n"
            "compile('1', 'f', 'eval')\n"
            "__import__('os')\n"
            "re.compile('x')\n"
            "app.exec()\n"
        )

        self.assertEqual(
            _execution_findings(sample, "core/sample.py"),
            [
                "import subprocess",
                "import importlib.util",
                "from os import system",
                "from multiprocessing import",
                "os.startfile",
                "eval",
                "exec",
                "compile",
                "__import__",
            ],
        )

    def test_registered_tool_dispatch_is_the_one_exempt_dynamic_import(self) -> None:
        tree = _parse(MARIA_ROOT / "core" / "tool_manager.py")
        imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
        ]

        (call,) = imports
        self.assertEqual(ast.unparse(call), "importlib.import_module(module_name)")
        (loop,) = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.For)
            and any(inner is call for inner in ast.walk(node))
        ]
        self.assertEqual(ast.unparse(loop.iter), "self.discover_tools()")
        self.assertEqual(
            _execution_findings(tree, TOOL_LOADER),
            [],
        )
        self.assertEqual(
            _execution_findings(tree, "core/elsewhere.py"),
            ["import importlib"],
        )


class OwnershipGuardTests(unittest.TestCase):
    """The stage's Windows calls, activation writes, and SQL stay where they belong."""

    def test_each_ctypes_module_names_exactly_the_windows_functions_it_owns(
        self,
    ) -> None:
        everything = frozenset().union(*OWNED_WINDOWS_FUNCTIONS.values())
        naming = {
            _relative(module): names
            for module in production_modules()
            if (names := _named_windows_functions(_parse(module), everything))
        }

        self.assertEqual(
            naming,
            {
                module: set(functions)
                for module, functions in OWNED_WINDOWS_FUNCTIONS.items()
            },
        )
        self.assertEqual(len(ORGANIZE_FUNCTIONS), 7)
        self.assertEqual(len(WINDOWS_FUNCTIONS), 9)

    def test_the_organizer_logic_module_names_no_windows_function(self) -> None:
        everything = frozenset().union(*OWNED_WINDOWS_FUNCTIONS.values())

        self.assertEqual(
            _named_windows_functions(
                _parse(MARIA_ROOT / "core" / "file_organization.py"), everything
            ),
            set(),
        )

    def test_only_the_projects_tool_calls_the_two_activation_methods(self) -> None:
        calling = {
            _relative(module): names
            for module in production_modules()
            if (
                names := {
                    node.attr
                    for node in ast.walk(_parse(module))
                    if isinstance(node, ast.Attribute)
                    and node.attr in ACTIVATION_METHODS
                }
            )
        }

        self.assertEqual(calling, {"tools/projects.py": set(ACTIVATION_METHODS)})

    def test_only_those_two_methods_reach_the_sql_that_writes_the_setting(
        self,
    ) -> None:
        functions = _functions(_parse(MARIA_ROOT / "core" / "memory.py"))
        writers = {
            name
            for name, node in functions.items()
            if any(
                isinstance(inner, ast.Constant)
                and isinstance(inner.value, str)
                and _writes_activation(inner.value)
                for inner in ast.walk(node)
            )
        }

        self.assertEqual(writers, {"_write_activation", "_activate", "_deactivate"})
        self.assertEqual(
            {name: _callers(functions, name) for name in sorted(writers)},
            {
                "_activate": {"activate_project"},
                "_deactivate": {"deactivate_project"},
                "_write_activation": {"_activate", "_deactivate"},
            },
        )

    def test_the_sql_boundary_scan_covers_every_new_module(self) -> None:
        modules = set(production_modules())
        for relative in NEW_PRODUCTION_MODULES:
            module = MARIA_ROOT / relative
            with self.subTest(module=relative):
                self.assertIn(module, modules)
                tree = _parse(module)
                self.assertEqual(
                    database_driver_imports(tree, _package_of(module)),
                    [],
                )
                self.assertEqual(executable_sql(tree), [])


class ExecutionRunTests(unittest.TestCase):
    """Every project command runs with each way to execute made to fail."""

    def setUp(self) -> None:
        latch = patch.object(project_files, "_LATCH", project_files._RestoreLatch())
        latch.start()
        self.addCleanup(latch.stop)
        volume = FakeVolume(WORKSPACE_PATH)
        volume.add_file("garden/plan.md", b"first line\nsecond line\n")
        self.access = FakeAccess(volume)

    def _world(self) -> tuple[ScriptedProvider, _Harness]:
        """Return a harness whose tool and Brain read the fake volume."""

        def reader(*_arguments: object, **_keywords: object) -> ProjectFileReader:
            return ProjectFileReader(
                StubRoots(),
                StubProtections(),
                Mock(spec=logging.Logger),
                access_loader=lambda: self.access,
            )

        with patch("tools.projects.ProjectFileReader", reader):
            provider, harness = _harness(self)
        harness.brain = MariaBrain(
            config=harness.config,
            planner=harness.planner,
            memory=harness.memory,
            tool_manager=harness.tool_manager,
            permissions=harness.permissions,
            logger=harness.logger,
            reasoning=provider,
            project_files=reader(),
        )
        return provider, harness

    def test_every_project_command_runs_with_execution_refused(self) -> None:
        provider, harness = self._world()
        session = harness.session()
        callers: list[str] = []

        def recording(name: str) -> Callable[..., object]:
            original = getattr(harness.memory, name)

            def call(*arguments: object) -> object:
                callers.append(sys._getframe(1).f_globals.get("__name__", ""))
                return original(*arguments)

            return call

        shown: list[str] = []
        with (
            patch.object(
                harness.memory,
                "activate_project",
                recording("activate_project"),
            ),
            patch.object(
                harness.memory,
                "deactivate_project",
                recording("deactivate_project"),
            ),
            _execution_refused() as attempted,
        ):
            for request in SCRIPT:
                response = session.submit(request)
                if response.needs_confirmation:
                    response = session.confirm()
                shown.append(response.text)

        self.assertEqual(attempted, [])
        self.assertEqual(callers, ["tools.projects", "tools.projects"])
        self.assertTrue(shown[0].startswith("Created project 1: "))
        self.assertEqual(shown[7], "Project 1 is now the active project.")
        self.assertTrue(shown[11].startswith(REPLY))
        self.assertEqual(shown[-1], "Deleted project 1.")
        self.assertEqual(provider.calls, 2)
        self.assertEqual(len(provider.requests[0].files), 1)
        self.assertEqual(provider.requests[1].files, ())
        listing = harness.memory.list_projects()
        self.assertEqual(listing.projects, ())
        self.assertIs(listing.activation.state, ActivationState.INACTIVE)
        self.assertEqual(self.access.leaked(), (set(), set(), set()))

    def test_the_refusal_is_real_for_a_production_caller(self) -> None:
        # The run above proves something only if a production module that
        # called one of these functions would be caught.
        namespace = {"__name__": "core.sample"}
        exec(  # noqa: S102 - this test's own probe, never Maria's code.
            "def probe(run):\n    return run('1 + 1')\n",
            namespace,
        )
        with _execution_refused() as attempted:
            with self.assertRaises(AssertionError):
                namespace["probe"](eval)
            with self.assertRaises(AssertionError):
                subprocess.Popen(["never-run"])
            self.assertEqual(eval("1 + 1"), 2)

        self.assertEqual(attempted, ["eval", "subprocess.Popen"])


if __name__ == "__main__":
    unittest.main()
