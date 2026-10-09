"""Guards on what the reminder-delivery subsystem can reach and do (V1-23-R1).

The static guards read source only: the four subsystem modules' own imports,
the Maria modules the closed-app pass reaches at any depth (standard-library
modules are leaves, as in ``tests.test_ollama_adapter``), and the names the
subsystem's source uses. The behavioral guards run real cycles over a
temporary world, a closed-app pass and the command-line and window checks,
with socket creation, network connections, thread starts, and new processes
patched to fail and Brain, Tool Manager, and the provider spied on; they
trace every SQL statement the delivery code runs, record every file the pass
opens, and read both logs for the reminder's text. Nothing here shows a real
box, registers a task, sleeps, or touches Maria's live database, logs, or
private override.

Python's audit hooks cannot be removed, so the one this module adds is
installed on first use and records only while a file-access guard is active.
"""

from __future__ import annotations

import ast
import logging
import os
import queue
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import unittest
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import core.logger as logger_module
import main as main_module
from core.brain import MariaBrain
from core.constants import DELIVERY_LOG_FILE_NAME, PROJECT_ROOT
from core.desktop_notice import NotificationState
from core.logger import configure_delivery_logging, configure_logging, get_logger
from core.memory import Memory
from core.ollama_adapter import OllamaReasoningProvider
from core.reminder_delivery import ReminderDeliverySeam
from core.session import MariaSession
from core.task_records import DELIVERY_CHANNEL_CLI, DELIVERY_CHANNEL_GUI
from core.tool_manager import ToolManager
from deliver_reminders import EXIT_DONE, run_pass
from gui.session_worker import DueReminderNotice, SessionEnded, SessionWorker
from tests.gui_fakes import WAIT_SECONDS
from tests.test_ollama_adapter import (
    FORBIDDEN_MODULES,
    importable_top_level_roots,
    imports_of,
    module_name,
    module_path,
    production_modules,
    reachable_modules,
)
from tests.zone_fixtures import SYNTHETIC_KEY


# --- Static guards -----------------------------------------------------------

# The subsystem: the closed-app entry point and its three core modules.
SUBSYSTEM = (
    "deliver_reminders",
    "core.runtime_paths",
    "core.reminder_delivery",
    "core.desktop_notice",
)

# Each subsystem module's own imports of Maria modules.
DIRECT_MARIA_IMPORTS = {
    "deliver_reminders": frozenset(
        {
            "core",
            "core.constants",
            "core.desktop_notice",
            "core.logger",
            "core.memory",
            "core.reminder_delivery",
            "core.runtime_paths",
            "core.task_records",
        }
    ),
    "core.runtime_paths": frozenset({"core.constants"}),
    "core.reminder_delivery": frozenset(
        {"core.constants", "core.context", "core.logger", "core.task_records"}
    ),
    "core.desktop_notice": frozenset(),
}

# Each subsystem module's own standard-library imports, by top-level name.
STANDARD_IMPORTS = {
    "deliver_reminders": frozenset(
        {"__future__", "collections", "datetime", "pathlib", "typing"}
    ),
    "core.runtime_paths": frozenset(
        {"__future__", "dataclasses", "pathlib", "tomllib", "typing"}
    ),
    "core.reminder_delivery": frozenset(
        {"__future__", "collections", "dataclasses", "datetime", "enum", "typing"}
    ),
    "core.desktop_notice": frozenset({"__future__", "ctypes", "enum", "sys"}),
}

# Every Maria module the closed-app pass reaches, at any depth.
PASS_REACH = frozenset(
    {
        "deliver_reminders",
        "core",
        "core.action_contracts",
        "core.audit",
        "core.constants",
        "core.context",
        "core.conversation_records",
        "core.desktop_notice",
        "core.failures",
        "core.logger",
        "core.memory",
        "core.reminder_delivery",
        "core.runtime_paths",
        "core.task_records",
    }
)

# Maria modules the subsystem never reaches: configuration, reasoning and its
# adapter, permissions, planning and compilation, Tool Manager, the
# conversation, the session, and the composition point; and no tool or GUI
# module at all.
NEVER_REACHED = frozenset(
    {
        "main",
        "core.brain",
        "core.config",
        "core.conversation",
        "core.filesystem_roots",
        "core.ollama_adapter",
        "core.permissions",
        "core.plan_compiler",
        "core.planner",
        "core.reasoning",
        "core.session",
        "core.task_requests",
        "core.time_resolution",
        "core.tool_manager",
    }
)
NEVER_REACHED_PACKAGES = ("tools", "gui")

# Clock, timer, and concurrency modules the subsystem never imports (A10).
CLOCK_AND_CONCURRENCY = frozenset(
    {"time", "sched", "asyncio", "threading", "_thread", "signal"}
)

# Names of planning, tool, permission, and provider machinery the subsystem's
# source never uses, whether called or not.
FORBIDDEN_SYMBOLS = frozenset(
    {
        "MariaBrain",
        "Planner",
        "Plan",
        "PlanStep",
        "compile_proposal",
        "ToolManager",
        "execute_tool",
        "validate_action",
        "load_tools",
        "build_tool",
        "PermissionManager",
        "ReasoningProvider",
        "OllamaReasoningProvider",
        "request_reasoning",
        "MariaSession",
        "LOCAL_CONFIG_PATH",
        "load_config",
    }
)

# The only calls the delivery code makes on the store, the pass's memory
# layer, and the Windows notice functions.
STORE_CALLS = frozenset(
    {
        "reminder_delivery_setting",
        "reminder_delivery_candidates",
        "claim_reminder_delivery",
        "record_reminder_delivery",
    }
)
PASS_MEMORY_CALLS = frozenset({"delivery_state"})
NOTICE_CALLS = frozenset({"available", "notification_state", "show"})

# The modules whose tests are V1-23-R1's own, none of which may sleep.
DELIVERY_TEST_MODULES = (
    "test_deliver_reminders.py",
    "test_desktop_notice.py",
    "test_gui_reminder_delivery.py",
    "test_reminder_delivery.py",
    "test_reminder_delivery_commands.py",
    "test_reminder_delivery_guards.py",
    "test_reminder_delivery_storage.py",
    "test_reminder_delivery_task.py",
    "test_runtime_paths.py",
    "test_session_reminder_delivery.py",
)
TESTS_DIR = PROJECT_ROOT / "tests"


def _source_tree(name: str) -> ast.Module:
    """Parse one Maria module by its dotted name, without importing it."""
    path = module_path(name)
    assert path is not None, name
    return ast.parse(path.read_text(encoding="utf-8"))


def _direct_maria_imports(name: str) -> frozenset[str]:
    """Return the Maria modules one module imports itself."""
    names, _unresolved = imports_of(module_path(name))  # type: ignore[arg-type]
    return frozenset(entry for entry in names if module_path(entry) is not None)


def _standard_imports(name: str) -> frozenset[str]:
    """Return the top-level names one module imports from outside Maria."""
    names, _unresolved = imports_of(module_path(name))  # type: ignore[arg-type]
    maria_roots = {Path(entry).stem for entry in importable_top_level_roots()}
    return frozenset(
        top
        for top in (entry.split(".")[0] for entry in names)
        if top not in maria_roots
    )


def _called_attributes(tree: ast.AST) -> set[str]:
    """Return every attribute name a source calls, on any receiver."""
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _names_used(tree: ast.AST) -> set[str]:
    """Return every bare name and attribute name a source uses."""
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.alias):
            used.add(node.asname or node.name.split(".")[-1])
    return used


def _attribute_calls(tree: ast.AST, receiver: str) -> set[str]:
    """Return the methods a source calls on ``receiver`` or ``self._receiver``."""
    calls = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        target = node.func.value
        if (isinstance(target, ast.Name) and target.id == receiver) or (
            isinstance(target, ast.Attribute)
            and target.attr == f"_{receiver}"
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        ):
            calls.add(node.func.attr)
    return calls


class SubsystemImportGuardTests(unittest.TestCase):
    """The subsystem's own imports and its reach (A2, A10, A27)."""

    def test_each_module_imports_exactly_its_pinned_maria_modules(self) -> None:
        for name in SUBSYSTEM:
            with self.subTest(module=name):
                self.assertEqual(
                    _direct_maria_imports(name),
                    DIRECT_MARIA_IMPORTS[name],
                )

    def test_each_module_imports_only_its_pinned_standard_modules(self) -> None:
        for name in SUBSYSTEM:
            with self.subTest(module=name):
                imported = _standard_imports(name)
                self.assertEqual(imported, STANDARD_IMPORTS[name])
                self.assertFalse(imported & CLOCK_AND_CONCURRENCY)
                self.assertFalse(imported & FORBIDDEN_MODULES)

    def test_the_pass_reaches_exactly_its_pinned_modules(self) -> None:
        reached, unfollowed = reachable_modules("deliver_reminders")

        self.assertEqual(set(reached), PASS_REACH)
        self.assertEqual(unfollowed, set())

    def test_no_module_the_subsystem_reaches_is_forbidden_or_forbids(self) -> None:
        for start in SUBSYSTEM:
            reached, unfollowed = reachable_modules(start)
            with self.subTest(module=start):
                self.assertEqual(unfollowed, set())
                self.assertFalse(set(reached) & NEVER_REACHED)
                self.assertFalse(
                    [
                        name
                        for name in reached
                        if name.split(".")[0] in NEVER_REACHED_PACKAGES
                    ]
                )
                for name, path in reached.items():
                    names, _ = imports_of(path)
                    tops = {entry.split(".")[0] for entry in names}
                    self.assertFalse(tops & FORBIDDEN_MODULES, name)

    def test_the_claim_protocol_reaches_no_store_or_windows_code(self) -> None:
        reached, _ = reachable_modules("core.reminder_delivery")

        self.assertNotIn("core.memory", reached)
        self.assertNotIn("core.desktop_notice", reached)
        self.assertNotIn("sqlite3", _standard_imports("core.reminder_delivery"))

    def test_only_the_notice_module_and_the_system_tool_import_ctypes(self) -> None:
        importers = set()
        for path in production_modules():
            names, _ = imports_of(path)
            if any(entry.split(".")[0] == "ctypes" for entry in names):
                importers.add(module_name(path))

        self.assertEqual(importers, {"core.desktop_notice", "tools.system"})

    def test_the_subsystem_names_no_planning_tool_or_provider_symbol(self) -> None:
        for name in SUBSYSTEM:
            with self.subTest(module=name):
                tree = _source_tree(name)
                self.assertFalse(_names_used(tree) & FORBIDDEN_SYMBOLS)
                source = module_path(name).read_text(encoding="utf-8")
                self.assertNotIn("config.local", source)

    def test_the_store_memory_and_notices_are_used_only_narrowly(self) -> None:
        protocol = _source_tree("core.reminder_delivery")
        entry = _source_tree("deliver_reminders")

        self.assertLessEqual(_attribute_calls(protocol, "store"), STORE_CALLS)
        self.assertEqual(_attribute_calls(protocol, "store"), STORE_CALLS)
        self.assertEqual(_attribute_calls(entry, "memory"), PASS_MEMORY_CALLS)
        self.assertEqual(_attribute_calls(entry, "notices"), NOTICE_CALLS)

    def test_the_claim_protocol_reads_no_clock_of_its_own(self) -> None:
        clocks = {"now", "utcnow", "today", "time", "monotonic", "perf_counter"}
        protocol = _source_tree("core.reminder_delivery")
        entry = _source_tree("deliver_reminders")

        self.assertFalse(_called_attributes(protocol) & clocks)
        # The pass reads the clock in its one seam only.
        (clock_seam,) = [
            node
            for node in ast.walk(entry)
            if isinstance(node, ast.FunctionDef)
            and _called_attributes(node) & clocks
        ]
        self.assertEqual(clock_seam.name, "utc_now")

    def test_the_delivery_tests_never_sleep(self) -> None:
        for file_name in DELIVERY_TEST_MODULES:
            with self.subTest(module=file_name):
                tree = ast.parse((TESTS_DIR / file_name).read_text(encoding="utf-8"))
                self.assertNotIn("sleep", _names_used(tree))
        # The window's notice tests live beside V1-21's own, which pump Qt
        # with a short sleep; the notice tests themselves never wait on it.
        window_tests = ast.parse(
            (TESTS_DIR / "test_gui_window.py").read_text(encoding="utf-8")
        )
        (notice_class,) = [
            node
            for node in window_tests.body
            if isinstance(node, ast.ClassDef)
            and node.name == "ReminderNoticeWindowTests"
        ]
        self.assertFalse(_names_used(notice_class) & {"sleep", "wait_until"})


# --- Behavioral guards -------------------------------------------------------

NOW = datetime(2027, 11, 2, 17, 45, tzinfo=timezone.utc)
TEXT_SENTINEL = "private-reminder-text-c0ffee41"
TRIGGER_SENTINEL = "private-trigger-text-77d2b9"
TIME = f"2027-11-02T18:30:00+01:00[{SYNTHETIC_KEY}]"
INSTANT = "2027-11-02T17:30:00Z"
EARLIER_TIME = f"2027-11-02T18:00:00+01:00[{SYNTHETIC_KEY}]"
EARLIER_INSTANT = "2027-11-02T17:00:00Z"

# Python's audit events a file-access guard keeps.
_WATCHED_EVENTS = frozenset(
    {"open", "sqlite3.connect", "os.mkdir", "os.remove", "os.rename", "os.replace"}
)
_recording: list[tuple[str, tuple[object, ...]]] | None = None
_hook_installed = False


def _audit(event: str, arguments: tuple[object, ...]) -> None:
    """Keep one watched event while a guard records; otherwise do nothing."""
    recording = _recording
    if recording is not None and event in _WATCHED_EVENTS:
        recording.append((event, arguments))


@contextmanager
def recorded_file_access() -> Iterator[list[tuple[str, tuple[object, ...]]]]:
    """Record the file events Python audits while the block runs."""
    global _recording, _hook_installed
    if not _hook_installed:
        sys.addaudithook(_audit)
        _hook_installed = True
    events: list[tuple[str, tuple[object, ...]]] = []
    _recording = events
    try:
        yield events
    finally:
        _recording = None


def _path_of(value: object) -> Path | None:
    """Return an audited path argument as a resolved path, or None."""
    if isinstance(value, (str, bytes, os.PathLike)):
        return Path(os.fsdecode(value)).resolve()
    return None


class _FakeNotices:
    """Windows notice functions that accept notifications and record boxes."""

    def __init__(self, result: object = True) -> None:
        self.result = result
        self.boxes: list[tuple[str, str]] = []

    def available(self) -> bool:
        return True

    def notification_state(self) -> NotificationState:
        return NotificationState.ACCEPTS

    def show(self, title: str, text: str) -> bool:
        self.boxes.append((title, text))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result  # type: ignore[return-value]


def _traced(memory: Memory, statements: list[str]) -> Memory:
    """Trace every statement ``memory`` runs on its own connections."""
    original_connect = memory._connect

    def tracing_connect() -> sqlite3.Connection:
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    memory._connect = tracing_connect  # type: ignore[method-assign]
    return memory


class _World(unittest.TestCase):
    """A temporary Maria world: configuration, database, and log directory.

    Maria's logger is emptied for each test and put back exactly afterwards,
    so a pass's or a session's own log configuration never outlives it.
    """

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.database_path = self.root / "database" / "maria.db"
        self.log_dir = self.root / "logs"
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(
            "[paths]\n"
            f'database = "{self.database_path.as_posix()}"\n'
            f'logs = "{self.log_dir.as_posix()}"\n',
            encoding="utf-8",
        )
        self.memory = Memory(self.database_path, logger=Mock(spec=logging.Logger))
        self.memory.initialize()
        self.memory.set_reminder_delivery(True)
        self.reminder_id = self.memory.create_reminder(TEXT_SENTINEL, TIME, INSTANT)
        self._empty_maria_logger()

    def _empty_maria_logger(self) -> None:
        maria = logging.getLogger("maria")
        handlers = tuple(maria.handlers)
        state = (maria.level, maria.propagate, maria.disabled)
        for handler in handlers:
            maria.removeHandler(handler)

        def restore() -> None:
            for handler in tuple(maria.handlers):
                maria.removeHandler(handler)
                handler.close()
            for handler in handlers:
                maria.addHandler(handler)
            maria.level, maria.propagate, maria.disabled = state

        self.addCleanup(restore)

    def close_log_handlers(self) -> None:
        """Flush and close every handler on Maria's logger, keeping it empty."""
        maria = logging.getLogger("maria")
        for handler in tuple(maria.handlers):
            maria.removeHandler(handler)
            handler.close()

    def delivery_state(self, reminder_id: int) -> tuple[object, ...]:
        """Return one reminder's revision and claim columns."""
        connection = sqlite3.connect(self.database_path)
        try:
            return connection.execute(
                "SELECT schedule_revision, delivery_claimed_at, delivery_channel, "
                "delivery_outcome FROM reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone()
        finally:
            connection.close()

    def add_due(self, text: str, time: str, instant: str) -> int:
        """Store one more due reminder."""
        return self.memory.create_reminder(text, time, instant)

    def full_pass(
        self,
        notices: _FakeNotices,
        **seams: object,
    ) -> int:
        """Run one pass with only the clock and the notices replaced."""
        return run_pass(
            clock=lambda: NOW,
            config_path=self.config_path,
            notices=notices,  # type: ignore[arg-type]
            **seams,  # type: ignore[arg-type]
        )


class BehavioralGuardTests(_World):
    """Full cycles call nothing, open nothing, and start nothing more (A3, A11)."""

    @contextmanager
    def everything_else_refused(self) -> Iterator[dict[str, Mock]]:
        """Spy on Brain, Tool Manager, and the provider; refuse sockets and more."""
        refused = AssertionError("delivery code may not do this")
        spies: dict[str, Mock] = {}
        with ExitStack() as stack:
            for label, owner, name in (
                ("process", MariaBrain, "process"),
                ("execute_confirmed_plan", MariaBrain, "execute_confirmed_plan"),
                ("execute_tool", ToolManager, "execute_tool"),
                ("validate_action", ToolManager, "validate_action"),
                ("respond", OllamaReasoningProvider, "respond"),
                ("socket", socket, "socket"),
                ("create_connection", socket, "create_connection"),
                ("thread_start", threading.Thread, "start"),
                ("popen", subprocess, "Popen"),
            ):
                spies[label] = stack.enter_context(
                    patch.object(owner, name, side_effect=refused)
                )
            if hasattr(os, "startfile"):
                spies["startfile"] = stack.enter_context(
                    patch.object(os, "startfile", side_effect=refused)
                )
            yield spies

    def assertNothingCalled(self, spies: dict[str, Mock]) -> None:
        for label, spy in spies.items():
            with self.subTest(spy=label):
                spy.assert_not_called()

    def test_a_full_pass_calls_nothing_and_leaves_no_thread_or_process(self) -> None:
        notices = _FakeNotices()
        threads = {thread.ident for thread in threading.enumerate()}

        with self.everything_else_refused() as spies:
            status = self.full_pass(notices)

        self.assertEqual(status, EXIT_DONE)
        ((_title, text),) = notices.boxes
        self.assertIn(TEXT_SENTINEL, text)
        self.assertEqual(self.delivery_state(self.reminder_id)[3], "displayed")
        self.assertNothingCalled(spies)
        self.assertEqual({thread.ident for thread in threading.enumerate()}, threads)

    def test_the_in_session_checks_call_nothing_and_start_nothing(self) -> None:
        second = self.add_due("second", EARLIER_TIME, EARLIER_INSTANT)
        brain = Mock(spec=MariaBrain)
        stdout = StringIO()

        with self.everything_else_refused() as spies:
            # The command line's own check and display, before its prompt.
            with patch("builtins.input", side_effect=["exit"]), redirect_stdout(stdout):
                cli_status = main_module.run_cli(
                    brain_factory=lambda: brain,
                    delivery_factory=lambda _brain: ReminderDeliverySeam(
                        self.memory,
                        DELIVERY_CHANNEL_CLI,
                        lambda: NOW,
                    ),
                )
            # The window's check and its acknowledged record, on this thread.
            self.add_due("third", EARLIER_TIME, EARLIER_INSTANT)
            session = MariaSession(
                brain,
                Mock(spec=logging.Logger),
                delivery=ReminderDeliverySeam(
                    self.memory,
                    DELIVERY_CHANNEL_GUI,
                    lambda: NOW + timedelta(minutes=1),
                ),
            )
            notice = session.claim_due_reminders()
            session.record_reminder_notice(notice, displayed=True)

        self.assertEqual(cli_status, 0)
        self.assertIn(TEXT_SENTINEL, stdout.getvalue())
        self.assertEqual(self.delivery_state(second)[2:], ("cli", "displayed"))
        self.assertEqual(notice.reminder_ids, (second + 1,))
        self.assertEqual(brain.method_calls, [])
        self.assertNothingCalled(spies)


# What a delivery statement may touch, and what it may return or write.
DELIVERY_TABLES = frozenset({"reminders", "reminder_delivery_settings"})
CANDIDATE_COLUMNS = "id, text, remind_time, remind_utc, schedule_revision"
DELIVERY_COLUMNS = frozenset(
    {"delivery_claimed_at", "delivery_channel", "delivery_outcome"}
)
CATALOGUE_FUNCTIONS = (
    "pragma_table_list",
    "pragma_table_xinfo",
    "pragma_index_list",
    "pragma_index_xinfo",
    "pragma_foreign_key_list",
)


def _classify(statement: str) -> str:
    """Return a traced statement's kind, or why it is not admitted.

    A catalogue read returns metadata only; transaction control moves no
    row; and row access may touch only the reminders and the setting, read
    from reminders only what a notice shows and a claim compares, and write
    only the claim columns. Anything else is named for the failure.
    """
    text = " ".join(statement.split())
    upper = text.upper()
    if re.fullmatch(r"(BEGIN( IMMEDIATE| DEFERRED)?|COMMIT|ROLLBACK)", upper):
        return "transaction"
    if re.fullmatch(r"PRAGMA (USER_VERSION|ENCODING)", upper):
        return "catalogue"
    # SQLite traces the statement each catalogue pragma function runs inside
    # the query that calls it, marked as nested with a leading "--".
    nested = re.fullmatch(r"-- PRAGMA (\w+)='[^']*'", text)
    if nested is not None:
        function = f"pragma_{nested.group(1)}"
        return "catalogue" if function in CATALOGUE_FUNCTIONS else f"other: {text}"
    if upper.startswith("SELECT") and (
        "FROM SQLITE_MASTER" in upper
        or any(f"FROM {name.upper()}(" in upper for name in CATALOGUE_FUNCTIONS)
    ):
        # Exactly one source, the catalogue itself: no join, no second
        # source after a comma, and no subquery. Quoted names and literals,
        # such as the foreign-key list's "from" column, are not keywords.
        bare = re.sub(r"\"[^\"]*\"|'[^']*'", "''", text)
        sources = re.findall(r"\b(?:FROM|JOIN)\b", bare, re.I)
        functions = "|".join(CATALOGUE_FUNCTIONS)
        source = rf"(?:sqlite_master|(?:{functions})\('[^']*'\))"
        alone = re.search(rf"\bFROM {source}(?: (?:WHERE|ORDER)\b|$)", text, re.I)
        if len(sources) == 1 and alone is not None:
            return "catalogue"
        return f"other: {text}"
    tables = set(
        re.findall(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([A-Za-z_]\w*)", text, re.I)
    )
    if not tables or not tables <= DELIVERY_TABLES:
        return f"other: {text}"
    if upper.startswith("UPDATE"):
        match = re.match(r"UPDATE reminders SET (.*?) WHERE ", text)
        if match is None:
            return f"other: {text}"
        columns = {part.split("=")[0].strip() for part in match.group(1).split(",")}
        return "delivery write" if columns <= DELIVERY_COLUMNS else f"other: {text}"
    if upper.startswith("SELECT"):
        match = re.match(r"SELECT (.*?) FROM reminders\b", text)
        if "reminders" in tables and (
            match is None or match.group(1) != CANDIDATE_COLUMNS
        ):
            return f"other: {text}"
        return "delivery read"
    return f"other: {text}"


class SqlTraceGuardTests(_World):
    """Every statement of every channel is admitted by kind (A4)."""

    def test_every_statement_is_a_catalogue_read_control_or_delivery_access(
        self,
    ) -> None:
        traces: dict[str, list[str]] = {"desktop": [], "cli": [], "gui": []}

        def traced_pass_memory(path: Path) -> Memory:
            memory = Memory(path, logger=Mock(spec=logging.Logger), existing_only=True)
            return _traced(memory, traces["desktop"])

        status = self.full_pass(
            _FakeNotices(),
            memory_factory=traced_pass_memory,
            configure_log=lambda _log_dir: None,
        )
        self.assertEqual(status, EXIT_DONE)
        for channel, minutes in (("cli", 1), ("gui", 2)):
            self.add_due(f"{channel} reminder", EARLIER_TIME, EARLIER_INSTANT)
            memory = _traced(
                Memory(self.database_path, logger=Mock(spec=logging.Logger)),
                traces[channel],
            )
            session = MariaSession(
                Mock(spec=MariaBrain),
                Mock(spec=logging.Logger),
                delivery=ReminderDeliverySeam(
                    memory,
                    channel,
                    lambda minutes=minutes: NOW + timedelta(minutes=minutes),
                ),
            )
            session.record_reminder_notice(session.claim_due_reminders(), True)

        for channel, statements in traces.items():
            with self.subTest(channel=channel):
                kinds = [_classify(statement) for statement in statements]
                self.assertEqual(
                    [kind for kind in kinds if kind.startswith("other")],
                    [],
                )
                # Positive control: the cycle read, claimed, and recorded.
                self.assertIn("delivery read", kinds)
                self.assertEqual(kinds.count("delivery write"), 2)
                self.assertNotIn(TEXT_SENTINEL, "\n".join(statements))
        self.assertIn("catalogue", [_classify(s) for s in traces["desktop"]])

    def test_the_classifier_refuses_what_delivery_may_not_run(self) -> None:
        for statement in (
            "SELECT text FROM notes",
            "SELECT id, text, created_at FROM reminders WHERE id = 1",
            "UPDATE reminders SET text = 'x' WHERE id = 1",
            "DELETE FROM reminders WHERE id = 1",
            "SELECT * FROM interactions",
            "INSERT INTO memories (fact) VALUES ('x')",
            "SELECT name FROM sqlite_master JOIN notes",
            "select name from sqlite_master join notes",
            "SELECT name FROM sqlite_master, notes",
            "SELECT name FROM sqlite_master WHERE name IN (SELECT text FROM notes)",
            "SELECT name FROM pragma_table_list('notes'), notes",
            'SELECT "from" FROM sqlite_master JOIN notes',
            "-- PRAGMA table_info='notes'",
            "-- SELECT text FROM notes",
            "PRAGMA journal_mode",
        ):
            with self.subTest(statement=statement):
                self.assertTrue(_classify(statement).startswith("other"))


class FileAccessGuardTests(_World):
    """A pass opens its configuration, its database, and its log only (A27)."""

    def test_a_pass_opens_only_its_configuration_database_and_log(self) -> None:
        notices = _FakeNotices()

        with recorded_file_access() as events:
            status = self.full_pass(notices)
        self.close_log_handlers()

        self.assertEqual(status, EXIT_DONE)
        delivery_log = (self.log_dir / DELIVERY_LOG_FILE_NAME).resolve()
        opened: dict[Path, set[str]] = {}
        for event, arguments in events:
            if event != "open":
                continue
            path = _path_of(arguments[0])
            if path is None or path.suffix in {".py", ".pyc"}:
                continue
            opened.setdefault(path, set()).add(str(arguments[1]))
        self.assertEqual(set(opened), {self.config_path, delivery_log})
        # The audited mode is the raw file's: read-only, or append-only.
        modes = {
            path: {mode.replace("b", "") for mode in found}
            for path, found in opened.items()
        }
        self.assertEqual(modes, {self.config_path: {"r"}, delivery_log: {"a"}})
        # Besides the database, only the structure check's private in-memory
        # reference schema, which holds no data, is ever opened.
        connected = [
            str(arguments[0])
            for event, arguments in events
            if event == "sqlite3.connect" and arguments[0] != ":memory:"
        ]
        self.assertTrue(connected)
        for database in connected:
            self.assertTrue(
                database.startswith(self.database_path.as_uri()),
                database,
            )
            self.assertTrue(database.endswith("?mode=rw"), database)
        made = {
            _path_of(arguments[0]) for event, arguments in events if event == "os.mkdir"
        }
        self.assertLessEqual(made, {self.log_dir})
        removed_or_moved = {"os.remove", "os.rename", "os.replace"}
        self.assertEqual(
            [event for event, _ in events if event in removed_or_moved],
            [],
        )
        everything = "\n".join(repr(arguments) for _, arguments in events)
        self.assertNotIn("config.local.toml", everything)
        self.assertNotIn("maria.log", everything)


class LogSentinelTests(_World):
    """Reminder text is displayed, never logged, on every channel (A13)."""

    def test_neither_log_ever_holds_reminder_text(self) -> None:
        self.memory.create_reminder(f"second {TEXT_SENTINEL}", TIME, INSTANT)
        # The desktop box fails, so the pass logs errors as well as claims.
        with redirect_stderr(StringIO()):
            self.full_pass(_FakeNotices(result=OSError(TEXT_SENTINEL)))
        self.close_log_handlers()

        configure_logging(self.log_dir)
        self.add_due(f"third {TEXT_SENTINEL}", EARLIER_TIME, EARLIER_INSTANT)
        session_memory = Memory(self.database_path)
        connection = sqlite3.connect(self.database_path)
        connection.execute(
            "CREATE TRIGGER refuse_outcomes BEFORE UPDATE OF delivery_outcome "
            f"ON reminders BEGIN SELECT RAISE(ABORT, '{TRIGGER_SENTINEL}'); END"
        )
        connection.commit()
        connection.close()
        cli = MariaSession(
            Mock(spec=MariaBrain),
            delivery=ReminderDeliverySeam(
                session_memory,
                DELIVERY_CHANNEL_CLI,
                lambda: NOW,
            ),
        )
        with redirect_stdout(StringIO()) as printed:
            main_module.show_due_reminders(cli)
        self.assertIn(TEXT_SENTINEL, printed.getvalue())

        self.add_due(f"fourth {TEXT_SENTINEL}", EARLIER_TIME, EARLIER_INSTANT)
        gui = MariaSession(
            Mock(spec=MariaBrain),
            delivery=ReminderDeliverySeam(
                session_memory,
                DELIVERY_CHANNEL_GUI,
                lambda: NOW + timedelta(minutes=1),
            ),
        )
        events: queue.SimpleQueue[object] = queue.SimpleQueue()
        worker = SessionWorker(lambda: gui, events.put)
        # Ended even when an assertion fails first: the worker is not a daemon.
        self.addCleanup(lambda: (worker.close(), worker.join(WAIT_SECONDS)))
        worker.start()
        notice = events.get(timeout=WAIT_SECONDS)
        self.assertIsInstance(notice, DueReminderNotice)
        worker.acknowledge_reminder_notice(notice.serial)
        worker.close()
        self.assertTrue(worker.join(WAIT_SECONDS))
        self.assertEqual(events.get(timeout=WAIT_SECONDS), SessionEnded(failed=False))
        self.close_log_handlers()

        delivery_log = (self.log_dir / DELIVERY_LOG_FILE_NAME).read_text(
            encoding="utf-8"
        )
        maria_log = (self.log_dir / "maria.log").read_text(encoding="utf-8")
        for name, text in (("delivery.log", delivery_log), ("maria.log", maria_log)):
            with self.subTest(log=name):
                self.assertNotIn(TEXT_SENTINEL, text)
                self.assertNotIn(TRIGGER_SENTINEL, text)
        # Positive controls: each log holds its channel's own lines.
        self.assertIn("for one desktop notice", delivery_log)
        self.assertIn("The reminder box failed: OSError.", delivery_log)
        self.assertIn("for one cli notice", maria_log)
        self.assertIn("for one gui notice", maria_log)
        self.assertIn("Could not record the delivery of reminder", maria_log)


class IdlePassLogTests(_World):
    """An enabled pass with nothing due writes no line, so no log file."""

    def test_an_enabled_pass_with_nothing_due_creates_no_delivery_log(self) -> None:
        self.assertTrue(self.memory.delete_reminder(self.reminder_id))
        notices = _FakeNotices()

        status = self.full_pass(notices)

        # The pass did configure its log: a handler waits on the file.
        waiting = [
            Path(getattr(handler, "baseFilename", ""))
            for handler in logging.getLogger("maria").handlers
        ]
        self.assertEqual(waiting, [self.log_dir / DELIVERY_LOG_FILE_NAME])
        self.close_log_handlers()
        self.assertEqual(status, EXIT_DONE)
        self.assertEqual(notices.boxes, [])
        self.assertTrue(self.log_dir.is_dir())
        self.assertEqual(list(self.log_dir.iterdir()), [])


class DeliveryLogFailureTests(_World):
    """No log failure stops delivery, before or after the claims (A32, A37)."""

    def pass_through(self, configure_log: Callable[[Path], object]) -> int:
        """Run a full pass whose log is set up by ``configure_log``."""
        notices = _FakeNotices()
        with redirect_stderr(StringIO()):
            status = self.full_pass(notices, configure_log=configure_log)
        self.close_log_handlers()
        self.assertEqual(len(notices.boxes), 1)
        return status

    def test_a_log_file_that_cannot_be_opened_stops_nothing(self) -> None:
        (self.log_dir / DELIVERY_LOG_FILE_NAME).mkdir(parents=True)

        status = self.pass_through(configure_delivery_logging)

        self.assertEqual(status, EXIT_DONE)
        self.assertEqual(self.delivery_state(self.reminder_id)[3], "displayed")

    def test_a_log_directory_gone_after_configuration_stops_nothing(self) -> None:
        def configure_then_lose(log_dir: Path) -> object:
            logger = configure_delivery_logging(log_dir)
            log_dir.rmdir()
            return logger

        status = self.pass_through(configure_then_lose)

        self.assertEqual(status, EXIT_DONE)
        self.assertFalse(self.log_dir.exists())
        self.assertEqual(self.delivery_state(self.reminder_id)[3], "displayed")

    def test_a_refused_rotation_during_a_pass_stops_nothing(self) -> None:
        self.log_dir.mkdir()
        delivery_log = self.log_dir / DELIVERY_LOG_FILE_NAME
        delivery_log.write_text("an earlier pass's line\n", encoding="utf-8")
        real_rename = os.rename

        def refuse_the_active_file(source: object, destination: object) -> None:
            if Path(os.fsdecode(source)) == delivery_log:
                raise PermissionError(13, "The file is open in another pass.")
            real_rename(source, destination)

        with (
            patch.object(logger_module, "LOG_MAX_BYTES", 32),
            patch.object(os, "rename", refuse_the_active_file),
        ):
            status = self.pass_through(configure_delivery_logging)

        self.assertEqual(status, EXIT_DONE)
        self.assertEqual(self.delivery_state(self.reminder_id)[3], "displayed")
        self.assertEqual(
            delivery_log.read_text(encoding="utf-8"),
            "an earlier pass's line\n",
        )

    def test_two_writers_and_a_refused_rotation_lose_lines_but_never_raise(
        self,
    ) -> None:
        self.log_dir.mkdir()
        base = self.log_dir / DELIVERY_LOG_FILE_NAME
        for suffix, text in (
            ("", "old active\n"),
            (".1", "old one\n"),
            (".2", "old two\n"),
            (".3", "old three\n"),
        ):
            Path(f"{base}{suffix}").write_text(text, encoding="utf-8")
        with patch.object(logger_module, "LOG_MAX_BYTES", 32):
            configure_delivery_logging(self.log_dir)
        # A second pass's handler on the same file, holding it open.
        other = RotatingFileHandler(
            base,
            maxBytes=10**6,
            backupCount=3,
            encoding="utf-8",
            delay=True,
        )
        other.setFormatter(logging.Formatter("%(message)s"))
        self.addCleanup(other.close)
        real_rename = os.rename

        def refuse_the_active_file(source: object, destination: object) -> None:
            if Path(os.fsdecode(source)) == base:
                raise PermissionError(13, "The file is open in another pass.")
            real_rename(source, destination)

        pass_logger = get_logger("delivery")
        with patch.object(os, "rename", refuse_the_active_file), redirect_stderr(
            StringIO()
        ) as errors:
            other.emit(
                logging.LogRecord("other", logging.INFO, "", 0, "other line", (), None)
            )
            pass_logger.info("first line of this pass")
            pass_logger.info("second line of this pass")
        other.close()
        self.close_log_handlers()

        # Each refused rollover first shifted the backups and deleted the
        # oldest, then lost its own line: two of the three backups are gone.
        self.assertEqual(base.read_text(encoding="utf-8"), "old active\nother line\n")
        self.assertFalse(Path(f"{base}.1").exists())
        self.assertFalse(Path(f"{base}.2").exists())
        self.assertEqual(Path(f"{base}.3").read_text(encoding="utf-8"), "old one\n")
        self.assertIn("PermissionError", errors.getvalue())

    def test_two_passes_at_once_each_show_only_their_own_claims(self) -> None:
        later = self.add_due("later", EARLIER_TIME, EARLIER_INSTANT)
        earliest = self.reminder_id
        inner = _FakeNotices()
        outer = _FakeNotices()

        class OverlappingMemory(Memory):
            """A pass's memory layer that lets another pass run mid-batch."""

            started = False

            def claim_reminder_delivery(self, *arguments: object) -> object:
                if not OverlappingMemory.started:
                    OverlappingMemory.started = True
                    run_inner()
                return super().claim_reminder_delivery(*arguments)

        def run_inner() -> None:
            self.assertEqual(
                self.full_pass(inner, configure_log=lambda _log_dir: None),
                EXIT_DONE,
            )

        status = self.full_pass(
            outer,
            memory_factory=lambda path: OverlappingMemory(
                path,
                logger=Mock(spec=logging.Logger),
                existing_only=True,
            ),
            configure_log=lambda _log_dir: None,
        )

        self.assertEqual(status, EXIT_DONE)
        # The inner pass claimed both first; the outer pass found them
        # changed and showed nothing.
        self.assertEqual(len(inner.boxes), 1)
        self.assertEqual(outer.boxes, [])
        for reminder_id in (later, earliest):
            self.assertEqual(
                self.delivery_state(reminder_id)[2:],
                ("desktop", "displayed"),
            )


if __name__ == "__main__":
    unittest.main()
