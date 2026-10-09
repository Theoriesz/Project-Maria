"""Import, ownership, and logging boundaries of the desktop GUI (V1-21).

The import guards parse source and never import or run it. They prove that
the GUI package reaches Maria only through the session API, that Qt lives in
exactly one module and the worker's thread in exactly one other, that the
GUI uses no Qt thread, timer, clipboard, rich text, link, process, or
settings store, that only the worker's thread boundary catches failures of
every kind and no production module touches Python's exception hooks, that
it confirms only through the session's own methods, and that ``main.py``
loads it only for ``--gui``. Separate tests import the
controller and worker with PyQt6 unimportable, and run both interfaces in one
process over temporary logs, to prove neither loses the other's logging.

Nothing here contacts a model or reads Maria's live database, live log, or
private override.
"""

from __future__ import annotations

import ast
import importlib
import logging
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import main as main_module
from core.brain import MariaBrain
from core.reasoning import DISABLED_REASONING
from core.session import MariaSession
from tests.gui_fakes import RunningGui, without_qt
from tests.log_assertions import assert_log_omits
from tests.test_conversation import _test_config
from tests.test_ollama_adapter import (
    NETWORK_MODULES,
    THREADING_MODULES,
    module_name,
    production_modules,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUI_ROOT = PROJECT_ROOT / "gui"

# Every module of the GUI package, and which of them may do what.
GUI_MODULES = frozenset(
    {"gui", "gui.controller", "gui.session_worker", "gui.window"}
)
QT_MODULE = "gui.window"
WORKER_MODULE = "gui.session_worker"

# The only Maria modules the GUI package may import itself: the session API,
# the shared constants and logger, and its own modules.
PERMITTED_MARIA_IMPORTS = frozenset(
    {
        "core.constants",
        "core.logger",
        "core.session",
        "gui.controller",
        "gui.session_worker",
    }
)

# The only Qt modules the window may import: no network, web, or other
# add-on module.
PERMITTED_QT_IMPORTS = frozenset(
    {"PyQt6.QtCore", "PyQt6.QtGui", "PyQt6.QtWidgets"}
)

# GUI toolkits and Qt bindings, by top-level name.
GUI_TOOLKITS = frozenset(
    {
        "PyQt6",
        "PyQt5",
        "PySide6",
        "PySide2",
        "tkinter",
        "_tkinter",
        "wx",
        "kivy",
        "gi",
        "sip",
        "shiboken6",
    }
)

# Qt and standard names that would give the GUI a thread, a timer, the
# clipboard, rich text, a link, a process, a tray, or a settings store.
FORBIDDEN_GUI_NAMES = frozenset(
    {
        "QThread",
        "QThreadPool",
        "QRunnable",
        "QTimer",
        "QtConcurrent",
        "QClipboard",
        "clipboard",
        "QDesktopServices",
        "QUrl",
        "QProcess",
        "QSystemTrayIcon",
        "QSettings",
        "QTextBrowser",
        "QTextEdit",
        "setHtml",
        "appendHtml",
        "insertHtml",
        "setOpenExternalLinks",
        "RichText",
        "AutoText",
        "MarkdownText",
        "openUrl",
    }
)

# The session members the worker may use, and nothing private: the five of
# V1-21, and exactly the three of the reminder-delivery seam (V1-23-R1).
SESSION_MEMBERS = frozenset(
    {
        "submit",
        "pending_approval",
        "confirm",
        "cancel",
        "end",
        "delivers_reminders",
        "claim_due_reminders",
        "record_reminder_notice",
    }
)


def gui_sources() -> dict[str, Path]:
    """Return every module of the GUI package, by dotted name."""
    return {
        module_name(path): path
        for path in sorted(GUI_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def own_imports(path: Path) -> set[str]:
    """Return every module a source imports itself, at any depth."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.add("." * node.level + (node.module or ""))
            else:
                names.add(node.module or "")
    return names


def names_used(path: Path) -> set[str]:
    """Return every bare name, attribute, and imported name in a source."""
    used: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.alias):
            used.add(node.name.split(".")[-1])
    return used


def _maria(name: str) -> bool:
    """Return whether a module name belongs to Maria."""
    return name.split(".")[0] in {"core", "tools", "gui", "main"}


def catches_everything(tree: ast.AST) -> bool:
    """Return whether any handler in a tree catches ``BaseException``.

    A bare ``except:`` counts, and so does any spelling that names
    ``BaseException``: alone, in a tuple, or as an attribute.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            return True
        for caught in ast.walk(node.type):
            if isinstance(caught, ast.Name) and caught.id == "BaseException":
                return True
            if (
                isinstance(caught, ast.Attribute)
                and caught.attr == "BaseException"
            ):
                return True
    return False


# Python's own exception hooks, which no production module may touch.
EXCEPTION_HOOKS = frozenset(
    {"excepthook", "__excepthook__", "unraisablehook", "__unraisablehook__"}
)


def gui_imports_by_scope(tree: ast.Module) -> dict[str, set[str]]:
    """Return every GUI or Qt import in a module, by where it is written.

    The scope is the name of the top-level function an import sits in, at
    any depth, or ``<module>`` for anything outside every function.
    """
    found: dict[str, set[str]] = {}

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if scope == "<module>" and isinstance(
                child,
                (ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                inner = child.name
            if isinstance(child, ast.Import):
                names = [alias.name for alias in child.names]
            elif isinstance(child, ast.ImportFrom):
                names = [child.module or ""]
            else:
                names = []
            for name in names:
                if name.split(".")[0] in {"gui", "PyQt6"}:
                    found.setdefault(inner, set()).add(name)
            visit(child, inner)

    visit(tree, "<module>")
    return found


class GuiImportBoundaryTests(unittest.TestCase):
    """Prove the GUI package reaches Maria only through the session API."""

    def test_the_gui_package_holds_exactly_its_four_modules(self) -> None:
        self.assertEqual(set(gui_sources()), set(GUI_MODULES))

    def test_gui_modules_import_only_the_session_api_and_shared_basics(
        self,
    ) -> None:
        for name, path in gui_sources().items():
            with self.subTest(module=name):
                imported = own_imports(path)
                maria = {module for module in imported if _maria(module)}

                self.assertLessEqual(maria, PERMITTED_MARIA_IMPORTS)
                self.assertFalse(
                    any(module.startswith(".") for module in imported)
                )
                for forbidden in (
                    "core.memory",
                    "core.tool_manager",
                    "core.permissions",
                    "core.plan_compiler",
                    "core.planner",
                    "core.brain",
                    "sqlite3",
                    "main",
                ):
                    self.assertNotIn(forbidden, imported)
                self.assertFalse(
                    any(module.split(".")[0] == "tools" for module in imported)
                )
                self.assertNotIn("import_module", names_used(path))
                self.assertNotIn("__import__", names_used(path))

        # Positive control: the worker does import the session API.
        self.assertIn(
            "core.session",
            own_imports(gui_sources()[WORKER_MODULE]),
        )

    def test_only_the_window_imports_qt_and_only_three_qt_modules(
        self,
    ) -> None:
        owners = {}
        for path in production_modules():
            toolkits = {
                module
                for module in own_imports(path)
                if module.split(".")[0] in GUI_TOOLKITS
            }
            if toolkits:
                owners[module_name(path)] = toolkits

        self.assertEqual(set(owners), {QT_MODULE})
        self.assertLessEqual(owners[QT_MODULE], PERMITTED_QT_IMPORTS)
        self.assertIn("PyQt6.QtWidgets", owners[QT_MODULE])

    def test_only_the_worker_imports_a_thread_and_nothing_a_network(
        self,
    ) -> None:
        for name, path in gui_sources().items():
            with self.subTest(module=name):
                tops = {module.split(".")[0] for module in own_imports(path)}

                self.assertEqual(tops & NETWORK_MODULES, set())
                if name == WORKER_MODULE:
                    self.assertEqual(tops & THREADING_MODULES, {"threading"})
                    self.assertIn("queue", tops)
                else:
                    self.assertEqual(tops & THREADING_MODULES, set())
                    self.assertNotIn("queue", tops)
                if name != QT_MODULE:
                    self.assertNotIn("signal", tops)

    def test_only_the_worker_catches_every_failure_and_no_hook_changes(
        self,
    ) -> None:
        """Catching ``BaseException`` stays at the worker thread's boundary.

        No production module replaces or reads Python's exception hooks, so
        containing a failure never changes how the process reports others.
        """
        catchers = {
            name
            for name, path in gui_sources().items()
            if catches_everything(ast.parse(path.read_text(encoding="utf-8")))
        }

        self.assertEqual(catchers, {WORKER_MODULE})
        for path in production_modules():
            with self.subTest(module=module_name(path)):
                self.assertEqual(names_used(path) & EXCEPTION_HOOKS, set())

        # Positive controls: every spelling of catching everything is seen,
        # an ordinary handler is not, and a hook replaced, as a test does.
        for source in (
            "try:\n    pass\nexcept:\n    pass\n",
            "try:\n    pass\nexcept builtins.BaseException:\n    pass\n",
            "try:\n    pass\nexcept (OSError, BaseException):\n    pass\n",
        ):
            self.assertTrue(catches_everything(ast.parse(source)))
        self.assertFalse(
            catches_everything(
                ast.parse("try:\n    pass\nexcept Exception:\n    pass\n")
            )
        )
        self.assertIn(
            "excepthook",
            names_used(PROJECT_ROOT / "tests" / "gui_fakes.py"),
        )

    def test_the_gui_uses_no_qt_thread_timer_clipboard_rich_text_or_link(
        self,
    ) -> None:
        for name, path in gui_sources().items():
            with self.subTest(module=name):
                self.assertEqual(names_used(path) & FORBIDDEN_GUI_NAMES, set())

        # Positive controls: the scan sees the window's plain-text choices.
        window = names_used(gui_sources()[QT_MODULE])
        self.assertIn("PlainText", window)
        self.assertIn("NoTextInteraction", window)
        self.assertIn("QueuedConnection", window)

    def test_the_gui_never_configures_logging_or_touches_sql(self) -> None:
        for name, path in gui_sources().items():
            with self.subTest(module=name):
                used = names_used(path)
                for forbidden in (
                    "configure_logging",
                    "basicConfig",
                    "addHandler",
                    "removeHandler",
                    "sqlite3",
                    "execute",
                    "executescript",
                    "Memory",
                    "ToolManager",
                    "PermissionManager",
                    "Plan",
                    "PlanStep",
                    "plan_snapshot",
                ):
                    self.assertNotIn(forbidden, used)

    def test_the_worker_confirms_only_through_the_sessions_own_methods(
        self,
    ) -> None:
        tree = ast.parse(
            gui_sources()[WORKER_MODULE].read_text(encoding="utf-8")
        )
        members = set()
        confirm_calls = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "session"
            ):
                members.add(node.attr)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "confirm"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "session"
            ):
                confirm_calls.append(node)

        self.assertEqual(members, SESSION_MEMBERS)
        self.assertEqual(len(confirm_calls), 1)
        self.assertEqual(confirm_calls[0].args, [])
        self.assertEqual(confirm_calls[0].keywords, [])
        for name, path in gui_sources().items():
            with self.subTest(module=name):
                self.assertNotIn("_pending", names_used(path))

    def test_main_imports_the_gui_only_inside_run_gui(self) -> None:
        """Every import statement in ``main.py``, at any depth, is placed.

        An import at module level, under a ``try`` or ``if`` included, or in
        any function but ``run_gui``, nested functions included, is found,
        whether it is written ``import`` or ``from ... import``.
        """
        tree = ast.parse((PROJECT_ROOT / "main.py").read_text("utf-8"))
        found = gui_imports_by_scope(tree)

        self.assertEqual(found, {"run_gui": {QT_MODULE}})

        # Positive controls: the scan places each spelling where it is.
        sneaky = ast.parse(
            "try:\n"
            "    import gui.window\n"
            "except ImportError:\n"
            "    pass\n"
            "def run_cli():\n"
            "    def later():\n"
            "        from PyQt6 import QtWidgets\n"
            "    if True:\n"
            "        import gui.controller as controller\n"
        )
        self.assertEqual(
            gui_imports_by_scope(sneaky),
            {
                "<module>": {"gui.window"},
                "run_cli": {"PyQt6", "gui.controller"},
            },
        )

    def test_the_controller_and_worker_import_without_qt(self) -> None:
        names = ("gui.session_worker", "gui.controller")
        originals = {name: sys.modules.get(name) for name in names}
        try:
            with without_qt():
                for name in names:
                    sys.modules.pop(name, None)
                fresh = [importlib.import_module(name) for name in names]

                self.assertTrue(all(module is not None for module in fresh))
                self.assertIsNone(sys.modules["PyQt6"])
                self.assertNotIn(QT_MODULE, sys.modules)
        finally:
            # Put back the modules every other test uses, in the import
            # table and as attributes of the package alike.
            for name, module in originals.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
                    setattr(sys.modules["gui"], name.split(".")[-1], module)


class SequentialInterfaceLoggingTests(unittest.TestCase):
    """Run the interfaces in one process over real, temporary log files."""

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

        self.maria_logger = logging.getLogger("maria")
        self.original_handlers = tuple(self.maria_logger.handlers)
        self.original_level = self.maria_logger.level
        self.original_propagate = self.maria_logger.propagate
        self.original_disabled = self.maria_logger.disabled
        for handler in self.original_handlers:
            self.maria_logger.removeHandler(handler)
        # Registered after the directory, so it runs first and every file
        # handler is closed before the directory is removed.
        self.addCleanup(self._restore_maria_logger)

    def _restore_maria_logger(self) -> None:
        """Close every handler the test created and restore the original."""
        for handler in list(self.maria_logger.handlers):
            self.maria_logger.removeHandler(handler)
            handler.close()
        for handler in self.original_handlers:
            self.maria_logger.addHandler(handler)
        self.maria_logger.setLevel(self.original_level)
        self.maria_logger.propagate = self.original_propagate
        self.maria_logger.disabled = self.original_disabled

    def _brain(self, name: str) -> MariaBrain:
        """Start Brain the way startup does, over one temporary location."""
        config = _test_config(self.root / name, DISABLED_REASONING)
        with patch("core.brain.load_config", return_value=config):
            return MariaBrain.from_defaults()

    def _run_cli(self, name: str, request: str) -> None:
        """Run one request through the CLI over the named location."""
        stdout = StringIO()
        with (
            patch("builtins.input", side_effect=[request, "exit"]),
            redirect_stdout(stdout),
            redirect_stderr(StringIO()),
        ):
            status = main_module.run_cli(lambda: self._brain(name))
        self.assertEqual(status, 0)
        self.assertIn("Maria: ", stdout.getvalue())

    def _log(self, name: str) -> str:
        """Read one temporary log after flushing every handler."""
        for handler in self.maria_logger.handlers:
            handler.flush()
        return (self.root / name / "logs" / "maria.log").read_text("utf-8")

    def test_neither_interface_loses_the_others_logging(self) -> None:
        self._run_cli("first", "calculate 1 + 1")
        first_handler = self.maria_logger.handlers[0]

        brain = self._brain("second")
        gui = RunningGui(self, lambda: MariaSession(brain))
        gui.send("calculate 2 + 2")
        gui.close()
        self.assertIsNone(first_handler.stream)
        second_handler = self.maria_logger.handlers[0]

        self._run_cli("first", "calculate 3 + 3")

        first = self._log("first")
        second = self._log("second")
        self.assertEqual(first.count("Received user request."), 2)
        self.assertEqual(first.count("Session ended."), 2)
        self.assertNotIn("GUI session started.", first)
        self.assertEqual(second.count("Received user request."), 1)
        self.assertEqual(second.count("GUI session started."), 1)
        self.assertEqual(second.count("Session ended."), 1)
        self.assertIsNone(second_handler.stream)
        managed = [
            handler
            for handler in self.maria_logger.handlers
            if getattr(handler, "_maria_managed", False)
        ]
        self.assertEqual(len(managed), 1)
        self.assertEqual(len(self.maria_logger.handlers), 1)
        self.assertEqual(
            Path(managed[0].baseFilename).resolve(),
            self.root / "first" / "logs" / "maria.log",
        )

    def test_the_gui_path_writes_no_request_reply_or_record_text_to_the_log(
        self,
    ) -> None:
        sentinels = (
            "private-gui-memory-3d81c0f2a7",
            "private-gui-note-title-a64e1b9c50",
            "private-gui-note-body-7f20d3e8b1",
            "private-gui-edit-5b9e0a4c62",
            "private-gui-unknown-e1c7f9b304",
        )
        memory, title, body, edit, unknown = sentinels
        brain = self._brain("private")
        gui = RunningGui(self, lambda: MariaSession(brain))

        gui.send(f"remember {memory}")
        gui.send(f"create note {title}: {body}")
        gui.send(f"edit note 1: {edit}")
        gui.approve()
        gui.send(f"read note 1 {unknown}")
        gui.send(unknown)
        gui.close()

        # Positive controls: the requests really ran and reached the log.
        shown = "\n".join(gui.view.lines)
        self.assertIn(edit, shown)
        log = self._log("private")
        self.assertIn("GUI session started.", log)
        self.assertGreaterEqual(log.count("Received user request."), 5)
        assert_log_omits(
            self,
            self.root / "private" / "logs" / "maria.log",
            sentinels,
        )


if __name__ == "__main__":
    unittest.main()
