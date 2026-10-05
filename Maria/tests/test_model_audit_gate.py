"""Focused tests for V1-18's audit gate around model-proposed execution.

A plan Maria's local reasoning model proposed runs only after the owner
approves it, and only once its content-free audit summary is committed. These
tests drive that gate end to end, through ``MariaSession`` and ``MariaBrain``
with the real planner, contracts, permissions, tools, and memory layer behind
them and a scripted stand-in for the model. They prove that every way the
summary can fail to be recorded refuses the dispatch and spends the approval;
that an interruption leaves a summary whose outcome is honestly unknown rather
than a rerun; that a failed outcome write never hides the tool's result; that
nothing the owner, the model, a stored record, a tool, or an exception
supplied reaches an audit row or the operational log; and that a proposal
turn's context notices, its clearing, and the session's fingerprints behave
exactly as V1-18 specifies.

Every database, log, and workspace is temporary. No test contacts a model or
opens a network connection, and Maria's live database, live log, and private
override are never read.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
import unittest
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call, patch

import core.memory as memory_module
from core.audit import AuditDecision, AuditOutcome, AuditTarget
from core.brain import BrainResponse, MariaBrain
from core.config import MariaConfig
from core.constants import APP_VERSION
from core.context import ContextConsentOutcome, ContextFingerprint
from core.filesystem_roots import (
    FilesystemProtections,
    FilesystemRoot,
    FilesystemRoots,
    RootPolicy,
)
from core.logger import configure_logging, get_logger
from core.memory import Memory
from core.permissions import PermissionManager
from core.planner import Planner
from core.reasoning import (
    DISABLED_REASONING,
    ReasoningRequest,
    ReasoningResponse,
    ReasoningSettings,
    ReasoningTransport,
)
from core.session import MariaSession
from core.tool_manager import ToolManager
from tests.log_assertions import assert_log_omits
from tools.calculator import CalculatorTool


# Owner-visible wording, written out so it is pinned rather than agreed with.
APPROVAL_HEADER = (
    "Maria's local reasoning model proposed this action. It runs only if you "
    "approve it."
)
AUDIT_SUMMARY_UNRECORDED = (
    "Maria did not run the approved action because its audit summary could "
    "not be recorded."
)
CONFIRMATION_REFUSED = (
    "No pending action matches that confirmation. Please make the request "
    "again."
)
GENERIC_FALLBACK = (
    "I do not have an executable plan for that yet. "
    "Type 'help' to see supported commands."
)
NOT_MODEL_ELIGIBLE = "Maria does not accept that action from the reasoning model."
UNSAFE_PATH = (
    "The proposed action had a path that is not a plain relative path inside "
    "Maria's default filesystem root."
)
REASONING_UNAVAILABLE = "The local reasoning model is unavailable."
TOOL_EXECUTION_FAILED = "Tool execution failed."
CALCULATION_FAILED = "Calculation failed: Only basic arithmetic is supported."
NO_CONTEXT_NOTICE = "No memory context supplied."
CONTEXT_UNCERTAINTY = (
    "Maria cannot verify whether these memories are still current or whether "
    "they conflict with one another."
)
# V1-20 names conversation summaries too, since they can be supplied as well,
# and V1-22 tasks and reminders, which also stop being current when they are
# no longer open.
CLEARED_NOTICE = (
    "This session's earlier conversation was cleared because a memory, note, "
    "summary, task, or reminder supplied to it is no longer enabled with the "
    "same text, or is no longer open, so the copy supplied earlier was "
    "invalidated."
)
UNREADABLE_NOTICE = (
    "Memory context could not be read, so none was supplied and this "
    "session's earlier conversation was cleared."
)
OUTCOME_UNKNOWN = "Outcome not recorded; execution is unknown."
AUDIT_DELETE_CONFIRMATION = (
    "Confirmation required: audit.delete requires explicit confirmation. "
    "Deleting an audit summary removes it from Maria's database. It does not "
    "undo the action, and Maria's operational log may still show that a "
    "model-proposed action ran."
)

# Distinctive values a privacy check can search for. Each is put somewhere
# private by a test, and each is first shown to have been in play.
PROMPT_SENTINEL = "private-gate-prompt-5e8a1f37c2"
PATH_SENTINEL = "private-gate-path-0b6f2d94e1"
EXPRESSION_SENTINEL = "private_gate_expression_6d4b2a80f3"
MEMORY_SENTINEL = "private-gate-memory-2c7d40b9a5"
LISTING_SENTINEL = "private-gate-listing-93c1e5a7d0"
EXCEPTION_SENTINEL = "private-gate-exception-a41f9b6e28"
# A root name must be a short plain identifier, so this one is shaped as one.
ROOT_NAME_SENTINEL = "gate_root_sentinel_4e7a9c"

# Every audit summary in these tests is stamped with this time, so the audit
# tool's listing and reading can be compared byte for byte.
CREATED_AT = "2031-02-03T04:05:06Z"

# The moment the system tool reports wherever a test reads the clock, and the
# exact report it gives for that moment.
CLOCK = datetime(2031, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
CLOCK_REPORT = (
    "Date: 2031-02-03\n"
    "Time: 04:05:06\n"
    "UTC offset: +00:00\n"
    "ISO-8601: 2031-02-03T04:05:06+00:00"
)

# Short enough that SQLite gives up on a held lock almost at once, so no test
# waits noticeably.
SHORT_BUSY_TIMEOUT_SECONDS = 0.05

# A request the deterministic planner answers with its generic fallback, so
# with reasoning enabled it reaches the model.
REQUEST = "Please work out two plus two for me."

TEAL_FACT = "My favourite colour is teal."
GREEN_FACT = "My favourite colour is green."
EDITOR_FACT = "My editor is VS Code."
MEMORY_FACT = f"My locker code is {MEMORY_SENTINEL}."
LISTING_FILE = f"{LISTING_SENTINEL}.txt"
MISSING_PATH = f"{PATH_SENTINEL}/absent"
SENTINEL_EXPRESSION = f"{EXPRESSION_SENTINEL} + 1"

RECORD_TABLES = (
    "interactions",
    "notes",
    "memories",
    "audit_summaries",
    "audit_outcomes",
    "saved_conversations",
    "conversation_summaries",
    "tasks",
    "reminders",
)
ZERO_ROWS = {table: 0 for table in RECORD_TABLES}


def _proposal(tool: str, action: str, arguments: dict[str, object]) -> str:
    """Return one proposal exactly as a cooperative model would write it."""
    return json.dumps({"tool": tool, "action": action, "arguments": arguments})


CALCULATE = _proposal("calculator", "calculate", {"expression": "2 + 2"})
CURRENT_TIME = _proposal("system", "current_time", {})
LIST_DEFAULT_ROOT = _proposal("filesystem", "list", {})
LIST_MISSING = _proposal("filesystem", "list", {"path": "missing"})
# A registered action a model may never propose, so the compiler refuses it
# before any permission, approval, or audit summary is considered.
CLOSED_PROPOSAL = _proposal("notes", "delete", {"id": 1})


def _approval(tool: str, action: str, arguments: str, *notices: str) -> str:
    """Return the approval prompt the owner sees, notices below the plan."""
    rendering = "\n".join(
        (
            APPROVAL_HEADER,
            f"Tool: {tool}",
            f"Action: {action}",
            f"Arguments: {arguments}",
        )
    )
    if not notices:
        return rendering
    return rendering + "\n\n" + "\n".join(notices)


CALCULATION_APPROVAL = _approval(
    "calculator",
    "calculate",
    '{"expression": "2 + 2"}',
    NO_CONTEXT_NOTICE,
)


def _supplied(names: str) -> str:
    """Return the notice for memory context a request carried."""
    return f"Memory context supplied: {names}. {CONTEXT_UNCERTAINTY}"


def _recorded(result: str, summary_id: int) -> str:
    """Return a tool's result followed by the summary that records it."""
    return f"{result}\n\nRecorded as audit summary {summary_id}."


def _outcome_unrecorded(summary_id: int) -> str:
    """Return the notice for a summary whose outcome was not recorded."""
    return (
        f"Recorded as audit summary {summary_id}, but its outcome could not be "
        "recorded, so that summary shows the outcome as unknown."
    )


def _summary_row(summary_id: int, action: str, target: str) -> tuple:
    """Return one stored audit summary row exactly as SQLite holds it."""
    return (summary_id, CREATED_AT, action, "owner_approved", target)


def _block(summary_id: int, action: str, target: str, outcome: str) -> str:
    """Return what reading one audit summary shows the owner."""
    return "\n".join(
        (
            f"Audit summary {summary_id}",
            f"Created at: {CREATED_AT}",
            f"Action: {action}",
            f"Target: {target}",
            "Decision: approved by the owner",
            outcome,
        )
    )


def _line(summary_id: int, action: str, target: str, outcome: str) -> str:
    """Return one audit listing line."""
    return (
        f"{summary_id}: {CREATED_AT} {action} (target: {target}). "
        f"Approved by the owner. {outcome}"
    )


def _record_ids(fingerprints: Iterable[ContextFingerprint]) -> set[tuple[str, int]]:
    """Return which records a set of fingerprints names, without digests."""
    return {
        (fingerprint.source.value, fingerprint.record_id)
        for fingerprint in fingerprints
    }


def _enabled_settings() -> ReasoningSettings:
    """Return valid enabled settings; the stub model never uses the host."""
    return ReasoningSettings(
        enabled=True,
        transport=ReasoningTransport.LOOPBACK,
        host="127.0.0.1",
        model="stub-model:1b",
        request_deadline_seconds=5,
        max_input_bytes=4096,
        max_output_bytes=4096,
    )


def _config(
    root: Path,
    settings: ReasoningSettings,
    *,
    confirmation_required: frozenset[str],
    denied_actions: frozenset[str],
) -> MariaConfig:
    """Return a configuration whose every path lies under a temporary root.

    The one named filesystem root carries a sentinel name, so a privacy check
    can prove that name is never stored or logged.
    """
    database_path = root / "database" / "maria.db"
    return MariaConfig(
        app_name="Maria",
        version=APP_VERSION,
        environment="test",
        database_path=database_path,
        log_dir=root / "logs",
        prompt_path=root / "prompts" / "system.txt",
        filesystem_roots=FilesystemRoots(
            (
                FilesystemRoot(
                    name=ROOT_NAME_SENTINEL,
                    path=root / "workspace",
                    policy=RootPolicy.ORGANIZE,
                    enabled=True,
                ),
            ),
            ROOT_NAME_SENTINEL,
        ),
        filesystem_protections=FilesystemProtections(
            database_path=database_path,
            log_dir=root / "logs",
            prompt_path=root / "prompts" / "system.txt",
        ),
        log_level="DEBUG",
        confirmation_required=confirmation_required,
        denied_actions=denied_actions,
        reasoning=settings,
    )


class _Model:
    """A stub local model that answers from a script and counts its requests.

    Request n is answered with reply n, and the last reply repeats once the
    script runs out, so a test proves how often the model was asked by
    counting rather than by exhausting the script. A reply that is an
    exception is raised, as a failing provider's would be.
    """

    def __init__(self, *replies: str | BaseException) -> None:
        if not replies:
            raise ValueError("A scripted model needs at least one reply.")
        self._replies = replies
        self.requests: list[ReasoningRequest] = []

    @property
    def calls(self) -> int:
        """Return how many requests this model has received."""
        return len(self.requests)

    def respond(self, request: ReasoningRequest) -> ReasoningResponse:
        """Record the request, then answer it from the script."""
        self.requests.append(request)
        reply = self._replies[min(self.calls, len(self._replies)) - 1]
        if isinstance(reply, BaseException):
            raise reply
        return ReasoningResponse(reply)


class _ToolRuns:
    """Count every real run of one tool class's ``execute``, or fail it.

    The class's own method is wrapped for one test, so a run is counted only
    when Tool Manager actually reached the tool. ``error``, when set, is
    raised in place of the run, as a tool that crashed or was interrupted
    part way through would raise it.
    """

    def __init__(self, test: unittest.TestCase, tool_class: type) -> None:
        self.actions: list[str] = []
        self.error: BaseException | None = None
        original = tool_class.execute
        runs = self

        def execute(
            tool: object,
            action: str,
            arguments: Mapping[str, Any],
        ) -> object:
            runs.actions.append(action)
            if runs.error is not None:
                raise runs.error
            return original(tool, action, arguments)

        active = patch.object(tool_class, "execute", execute)
        active.start()
        test.addCleanup(active.stop)


class _InterruptedCommit:
    """A real connection whose commit is interrupted, as Ctrl+C would be.

    Every statement runs on the real connection and is noted; only the
    commit raises ``KeyboardInterrupt`` instead of committing, so closing
    the connection afterwards discards the uncommitted write.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.statements: list[str] = []
        self.commits = 0

    def execute(self, statement: str, *parameters: object) -> sqlite3.Cursor:
        """Note one statement, then run it on the real connection."""
        self.statements.append(" ".join(statement.split()))
        return self._connection.execute(statement, *parameters)

    def commit(self) -> None:
        """Interrupt the commit before anything is committed."""
        self.commits += 1
        raise KeyboardInterrupt

    def __getattr__(self, name: str) -> Any:
        """Reach everything else on the real connection."""
        return getattr(self._connection, name)


def _spy(test: unittest.TestCase, owner: object, name: str) -> Mock:
    """Wrap one method so it still runs but its calls are counted."""
    active = patch.object(owner, name, wraps=getattr(owner, name))
    spy = active.start()
    test.addCleanup(active.stop)
    return spy


@contextmanager
def _raising(spy: Mock, error: BaseException) -> Iterator[None]:
    """Make one spied method raise, then let it run for real again."""
    spy.side_effect = error
    try:
        yield
    finally:
        spy.side_effect = None


class _Harness:
    """Real planner, contracts, permissions, tools, memory, Brain, and session.

    Everything lives under one temporary directory, and the default named
    root carries a sentinel name. Planning, contract validation, confirmation
    preparation, permission checks, dispatch, and both audit writes are
    wrapped by spies that still call the real components, so a test can prove
    how often each was reached, or make one of them fail. The calculator
    tool's own runs are counted too, so a dispatch that reached the tool can
    be told from one that did not. Every audit summary is stamped with
    ``CREATED_AT``.
    """

    def __init__(
        self,
        test: unittest.TestCase,
        provider: _Model,
        *,
        settings: ReasoningSettings | None = None,
        confirmation_required: frozenset[str] = frozenset(),
        denied_actions: frozenset[str] = frozenset(),
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.temporary_path = Path(temporary.name)
        self.root = self.temporary_path.resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.config = _config(
            self.root,
            _enabled_settings() if settings is None else settings,
            confirmation_required=confirmation_required,
            denied_actions=denied_actions,
        )
        self.database_path = self.config.database_path

        stamp = patch.object(
            memory_module,
            "audit_timestamp",
            return_value=CREATED_AT,
        )
        stamp.start()
        test.addCleanup(stamp.stop)

        # Under Maria's logger namespace, so a test that configures Maria's
        # file logging captures every component's lines.
        self.logger = get_logger("tests.model_audit_gate")
        self.memory = Memory(self.database_path, logger=self.logger)
        self.memory.initialize()
        self.tool_manager = ToolManager(
            config=self.config,
            memory=self.memory,
            logger=self.logger,
        )
        self.tool_manager.load_tools()
        self.permissions = PermissionManager(
            confirmation_required=confirmation_required,
            denied_actions=denied_actions,
            logger=self.logger,
        )
        self.planner = Planner(logger=self.logger)

        self.real_execute_tool = self.tool_manager.execute_tool
        self.create_plan = _spy(test, self.planner, "create_plan")
        self.validate_action = _spy(test, self.tool_manager, "validate_action")
        self.prepare_confirmation = _spy(
            test,
            self.tool_manager,
            "prepare_confirmation",
        )
        self.execute_tool = _spy(test, self.tool_manager, "execute_tool")
        self.check = _spy(test, self.permissions, "check")
        self.record_audit_summary = _spy(test, self.memory, "record_audit_summary")
        self.record_audit_outcome = _spy(test, self.memory, "record_audit_outcome")
        self.calculator_runs = _ToolRuns(test, CalculatorTool)

        self.provider = provider
        self.brain = MariaBrain(
            config=self.config,
            planner=self.planner,
            memory=self.memory,
            tool_manager=self.tool_manager,
            permissions=self.permissions,
            logger=self.logger,
            reasoning=provider,
        )
        self.session = MariaSession(self.brain, logger=self.logger)

    def enable(self, fact: str) -> int:
        """Store one memory and enable it for context, for its exact text."""
        memory_id, _ = self.memory.store_memory(fact)
        outcome = self.memory.enable_memory_context(
            memory_id,
            self.memory.memory_fact_digest(memory_id),
        )
        if outcome is not ContextConsentOutcome.ENABLED:
            raise AssertionError("The memory could not be enabled for context.")
        return memory_id

    def row_counts(self) -> dict[str, int]:
        """Count the rows of every record table in the temporary database."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            return {
                table: connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in RECORD_TABLES
            }

    def audit_rows(self) -> tuple[list[tuple], list[tuple]]:
        """Return every stored audit summary and outcome, column for column."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            summaries = connection.execute(
                "SELECT * FROM audit_summaries ORDER BY id"
            ).fetchall()
            outcomes = connection.execute(
                "SELECT * FROM audit_outcomes ORDER BY summary_id"
            ).fetchall()
        return summaries, outcomes

    def interactions(self) -> list[tuple[str, str]]:
        """Return every stored request and response, oldest first."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                "SELECT user_input, response FROM interactions ORDER BY id"
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def raw(self, statement: str, parameters: tuple = ()) -> None:
        """Run and commit one statement through a second, direct connection."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(statement, parameters)
            connection.commit()


class _GateTestCase(unittest.TestCase):
    """Silence Maria's log, or write it only to a temporary file.

    The ``maria`` logger is saved, emptied, and given a null handler for
    every test, and restored afterwards, so nothing reaches the console or a
    real log. A test that inspects the log calls ``start_log`` before it
    builds anything, which configures Maria's own file logging into this
    test's temporary directory exactly as startup would.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.log_dir = Path(temporary.name) / "logs"
        self.log_path = self.log_dir / "maria.log"

        self.maria_logger = logging.getLogger("maria")
        saved = (
            tuple(self.maria_logger.handlers),
            self.maria_logger.level,
            self.maria_logger.propagate,
            self.maria_logger.disabled,
        )
        for handler in saved[0]:
            self.maria_logger.removeHandler(handler)
        self.maria_logger.addHandler(logging.NullHandler())
        self.maria_logger.propagate = False
        self.maria_logger.disabled = False
        # Registered after the directory, so the handler is closed before
        # the directory holding its file is removed.
        self.addCleanup(self._restore_maria_logger, *saved)

    def _restore_maria_logger(
        self,
        handlers: tuple[logging.Handler, ...],
        level: int,
        propagate: bool,
        disabled: bool,
    ) -> None:
        """Close every handler this test added and restore the logger."""
        for handler in list(self.maria_logger.handlers):
            try:
                handler.flush()
            finally:
                self.maria_logger.removeHandler(handler)
                handler.close()
        for handler in handlers:
            self.maria_logger.addHandler(handler)
        self.maria_logger.setLevel(level)
        self.maria_logger.propagate = propagate
        self.maria_logger.disabled = disabled

    def start_log(self) -> None:
        """Write Maria's log to this test's temporary directory."""
        configure_logging(self.log_dir, "DEBUG")

    def logged(self) -> str:
        """Return everything the temporary log holds so far."""
        for handler in self.maria_logger.handlers:
            handler.flush()
        return self.log_path.read_text(encoding="utf-8")

    def offer(
        self,
        harness: _Harness,
        request: str = REQUEST,
        expected: str = CALCULATION_APPROVAL,
    ) -> BrainResponse:
        """Submit a request the model answers with a plan awaiting approval."""
        offered = harness.session.submit(request)
        self.assertEqual(offered.text, expected)
        self.assertTrue(offered.needs_confirmation)
        self.assertIsNotNone(harness.session.pending_approval)
        return offered


# --- A summary that cannot be recorded --------------------------------------


class _IntSubclass(int):
    """An int subtype, which must never pass as a summary id."""


class AuditSummaryFailureTests(_GateTestCase):
    """Prove an unrecorded summary refuses the dispatch and spends the approval.

    However the summary fails to be recorded, the owner is shown one fixed
    refusal, no tool is reached, no outcome is attempted, nothing is stored,
    and the approval is gone: confirming again is refused, never retried.
    """

    def setUp(self) -> None:
        super().setUp()
        self.start_log()

    def _offered(self) -> _Harness:
        """Return a harness whose session holds one plan awaiting approval."""
        harness = _Harness(self, _Model(CALCULATE))
        self.offer(harness)
        return harness

    def _assert_refused_and_spent(
        self,
        harness: _Harness,
        response: BrainResponse,
    ) -> None:
        """Assert one confirmation was refused before dispatch, for good."""
        session = harness.session
        self.assertEqual(response.text, AUDIT_SUMMARY_UNRECORDED)
        self.assertFalse(response.needs_confirmation)
        self.assertIsNone(response.conversation)
        harness.record_audit_summary.assert_called_once_with(
            "calculator.calculate",
            AuditDecision.OWNER_APPROVED,
            AuditTarget.NONE,
        )
        harness.execute_tool.assert_not_called()
        harness.validate_action.assert_not_called()
        harness.prepare_confirmation.assert_not_called()
        harness.record_audit_outcome.assert_not_called()
        self.assertEqual(harness.calculator_runs.actions, [])
        self.assertEqual(harness.audit_rows(), ([], []))
        self.assertIsNone(session.pending_approval)

        # The approval was spent before the summary was attempted, so a
        # second confirmation finds nothing to run and is not retried.
        self.assertEqual(session.confirm().text, CONFIRMATION_REFUSED)
        harness.record_audit_summary.assert_called_once()
        harness.execute_tool.assert_not_called()
        self.assertEqual(harness.calculator_runs.actions, [])
        self.assertEqual(harness.check.call_count, 2)
        self.assertEqual(harness.create_plan.call_count, 1)
        self.assertEqual(harness.provider.calls, 1)
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_a_summary_insert_that_adds_no_row_refuses_the_dispatch(
        self,
    ) -> None:
        harness = self._offered()
        # Added after initialization, so the schema check never sees it: the
        # insert runs as a statement but stores nothing.
        harness.raw(
            "CREATE TRIGGER audit_summary_ignored BEFORE INSERT ON "
            "audit_summaries BEGIN SELECT RAISE(IGNORE); END"
        )

        response = harness.session.confirm()

        self._assert_refused_and_spent(harness, response)
        logged = self.logged()
        self.assertIn("Failed to record audit summary: no row was inserted.", logged)
        self.assertIn(
            "Refused a model-proposed action: its audit summary was not recorded.",
            logged,
        )

    def test_a_locked_database_refuses_the_dispatch(self) -> None:
        harness = self._offered()

        with closing(
            sqlite3.connect(harness.database_path, isolation_level=None)
        ) as holder:
            holder.execute("BEGIN EXCLUSIVE")
            with patch.object(
                memory_module,
                "DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS",
                SHORT_BUSY_TIMEOUT_SECONDS,
            ):
                response = harness.session.confirm()
            holder.execute("ROLLBACK")

        self._assert_refused_and_spent(harness, response)
        logged = self.logged()
        self.assertIn("Failed to record audit summary: database busy.", logged)
        self.assertIn(
            "Refused a model-proposed action: its audit summary was not recorded.",
            logged,
        )

    def test_a_summary_write_that_raises_refuses_without_repeating_it(
        self,
    ) -> None:
        for error_type in (
            RuntimeError,
            ValueError,
            TypeError,
            OSError,
            sqlite3.DatabaseError,
        ):
            with self.subTest(error=error_type.__name__):
                harness = self._offered()
                harness.record_audit_summary.side_effect = error_type(
                    EXCEPTION_SENTINEL
                )

                response = harness.session.confirm()

                self._assert_refused_and_spent(harness, response)
                self.assertNotIn(EXCEPTION_SENTINEL, response.text)

        self.assertIn(
            "Refused a model-proposed action: its audit summary was not recorded.",
            self.logged(),
        )
        assert_log_omits(self, self.log_path, (EXCEPTION_SENTINEL,))

    def test_a_summary_id_that_is_not_an_exact_positive_int_refuses_the_dispatch(
        self,
    ) -> None:
        returned = {
            "True": True,
            "False": False,
            "zero": 0,
            "negative": -1,
            "text": "1",
            "float": 1.0,
            "None": None,
            "int subtype": _IntSubclass(1),
        }
        for label, value in returned.items():
            with self.subTest(returned=label):
                harness = self._offered()
                harness.record_audit_summary.return_value = value

                response = harness.session.confirm()

                self._assert_refused_and_spent(harness, response)

        self.assertIn(
            "Refused a model-proposed action: its audit summary id was not valid.",
            self.logged(),
        )

    def test_a_generated_timestamp_outside_the_vocabulary_refuses_the_dispatch(
        self,
    ) -> None:
        for value in ("2031-02-03 04:05:06", "2031-02-30T04:05:06Z", None):
            with self.subTest(timestamp=repr(value)):
                harness = self._offered()

                with patch.object(
                    memory_module,
                    "audit_timestamp",
                    return_value=value,
                ):
                    response = harness.session.confirm()

                self._assert_refused_and_spent(harness, response)

        self.assertIn(
            "Refused a model-proposed action: its audit summary was not recorded.",
            self.logged(),
        )


# --- Interruptions ----------------------------------------------------------


class InterruptionTests(_GateTestCase):
    """Prove an interrupted dispatch never reruns and never claims an outcome.

    ``KeyboardInterrupt`` is contained nowhere on this path, so the owner can
    always stop Maria. Stopped before the summary commits, nothing is stored
    and nothing runs; stopped after it, whether before the tool, inside it,
    or while its outcome commits, the summary stays without an outcome,
    which the audit tool shows as unknown. In no case can the spent approval
    run the action again.
    """

    def test_an_interrupt_before_the_summary_commits_stores_and_runs_nothing(
        self,
    ) -> None:
        model = _Model(CALCULATE)
        harness = _Harness(self, model)
        session = harness.session
        self.offer(harness)

        connect = harness.memory._connect
        opened: list[_InterruptedCommit] = []

        def interrupted() -> _InterruptedCommit:
            connection = _InterruptedCommit(connect())
            opened.append(connection)
            return connection

        with patch.object(harness.memory, "_connect", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                session.confirm()

        # One connection was opened, for the summary insert alone, and the
        # interrupt struck its commit after the insert had run.
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].commits, 1)
        self.assertEqual(len(opened[0].statements), 1)
        self.assertTrue(
            opened[0].statements[0].startswith("INSERT INTO audit_summaries")
        )
        harness.record_audit_summary.assert_called_once()
        self.assertEqual(harness.audit_rows(), ([], []))
        harness.execute_tool.assert_not_called()
        harness.record_audit_outcome.assert_not_called()
        self.assertEqual(harness.calculator_runs.actions, [])

        self.assertIsNone(session.pending_approval)
        self.assertEqual(session.confirm().text, CONFIRMATION_REFUSED)
        harness.execute_tool.assert_not_called()
        self.assertEqual(harness.calculator_runs.actions, [])
        self.assertEqual(harness.row_counts(), ZERO_ROWS)
        self.assertEqual(model.calls, 1)

    def test_an_interrupt_after_the_summary_commits_leaves_its_outcome_unknown(
        self,
    ) -> None:
        model = _Model(CALCULATE)
        harness = _Harness(self, model)
        session = harness.session
        self.offer(harness)
        # Dispatch itself is interrupted, before the tool can be reached.
        harness.execute_tool.side_effect = KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            session.confirm()

        # The summary was committed first, and nothing after it happened.
        summary = _summary_row(1, "calculator.calculate", "none")
        self.assertEqual(harness.audit_rows(), ([summary], []))
        harness.execute_tool.assert_called_once()
        harness.validate_action.assert_not_called()
        harness.record_audit_outcome.assert_not_called()
        self.assertEqual(harness.calculator_runs.actions, [])
        self.assertIsNone(session.pending_approval)

        harness.execute_tool.side_effect = None
        self.assertEqual(session.confirm().text, CONFIRMATION_REFUSED)
        shown = session.submit("read audit 1")
        self.assertEqual(
            shown.text,
            _block(1, "calculator.calculate", "none", OUTCOME_UNKNOWN),
        )

        # Nothing ran again: the only later dispatch is the owner's own read.
        self.assertEqual(
            [made.args[:2] for made in harness.execute_tool.call_args_list],
            [("calculator", "calculate"), ("audit", "read")],
        )
        self.assertEqual(harness.calculator_runs.actions, [])
        self.assertEqual(harness.audit_rows(), ([summary], []))
        self.assertEqual(
            harness.interactions(),
            [("read audit 1", "Displayed audit summary 1.")],
        )
        self.assertEqual(model.calls, 1)

    def test_an_interrupt_inside_the_tool_leaves_its_summary_without_an_outcome(
        self,
    ) -> None:
        model = _Model(CALCULATE)
        harness = _Harness(self, model)
        session = harness.session
        self.offer(harness)
        harness.calculator_runs.error = KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            session.confirm()

        summary = _summary_row(1, "calculator.calculate", "none")
        self.assertEqual(harness.calculator_runs.actions, ["calculate"])
        harness.execute_tool.assert_called_once()
        harness.record_audit_outcome.assert_not_called()
        self.assertEqual(harness.audit_rows(), ([summary], []))
        self.assertIsNone(session.pending_approval)

        harness.calculator_runs.error = None
        self.assertEqual(session.confirm().text, CONFIRMATION_REFUSED)
        listed = session.submit("audit")
        self.assertEqual(
            listed.text,
            _line(1, "calculator.calculate", "none", OUTCOME_UNKNOWN),
        )
        self.assertEqual(harness.calculator_runs.actions, ["calculate"])
        self.assertEqual(harness.audit_rows(), ([summary], []))
        self.assertEqual(model.calls, 1)

    def test_an_interrupt_while_the_outcome_commits_leaves_it_unknown(
        self,
    ) -> None:
        model = _Model(CALCULATE)
        harness = _Harness(self, model)
        session = harness.session
        self.offer(harness)
        execute = harness.real_execute_tool
        connect = harness.memory._connect
        opened: list[_InterruptedCommit] = []

        def interrupted() -> _InterruptedCommit:
            connection = _InterruptedCommit(connect())
            opened.append(connection)
            return connection

        # Only connections opened after the tool has run are interrupted, so
        # the summary commits normally and the outcome's commit is what stops.
        interrupt = patch.object(
            harness.memory,
            "_connect",
            side_effect=interrupted,
        )

        def run_then_interrupt_the_outcome(
            tool_name: str,
            action: str,
            arguments: Mapping[str, Any],
        ) -> object:
            result = execute(tool_name, action, arguments)
            interrupt.start()
            return result

        harness.execute_tool.side_effect = run_then_interrupt_the_outcome
        try:
            with self.assertRaises(KeyboardInterrupt):
                session.confirm()
        finally:
            interrupt.stop()
            harness.execute_tool.side_effect = None

        # The tool ran once and its outcome insert ran, but the interrupted
        # commit stored nothing, so the summary's outcome stays unknown.
        self.assertEqual(harness.calculator_runs.actions, ["calculate"])
        harness.execute_tool.assert_called_once()
        harness.record_audit_outcome.assert_called_once_with(
            1,
            AuditOutcome.SUCCEEDED,
        )
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].commits, 1)
        self.assertEqual(len(opened[0].statements), 1)
        self.assertTrue(
            opened[0].statements[0].startswith("INSERT INTO audit_outcomes")
        )
        summary = _summary_row(1, "calculator.calculate", "none")
        self.assertEqual(harness.audit_rows(), ([summary], []))
        self.assertIsNone(session.pending_approval)

        # Nothing reruns, and the owner's audit view says what is known.
        self.assertEqual(session.confirm().text, CONFIRMATION_REFUSED)
        self.assertEqual(
            session.submit("read audit 1").text,
            _block(1, "calculator.calculate", "none", OUTCOME_UNKNOWN),
        )
        self.assertEqual(harness.calculator_runs.actions, ["calculate"])
        harness.record_audit_outcome.assert_called_once()
        self.assertEqual(harness.audit_rows(), ([summary], []))
        self.assertEqual(model.calls, 1)


# --- Outcomes ----------------------------------------------------------------


class OutcomeTests(_GateTestCase):
    """Prove the tool's result survives whatever happens to its outcome.

    A recorded outcome follows the result with the summary's id. An outcome
    that cannot be recorded is never retried and never hides the result: the
    result is shown, followed by the fixed notice that the summary's outcome
    is unknown, and no orphan outcome is left behind.
    """

    def setUp(self) -> None:
        super().setUp()
        self.start_log()

    def test_a_recorded_outcome_follows_the_result_it_records(self) -> None:
        model = _Model(CALCULATE)
        harness = _Harness(self, model)
        session = harness.session
        self.offer(harness)
        plan = session.pending_approval.plan

        confirmed = session.confirm()

        self.assertEqual(confirmed.text, _recorded("2 + 2 = 4", 1))
        self.assertFalse(confirmed.needs_confirmation)
        self.assertIsNone(confirmed.conversation)
        # The approved arguments object itself was dispatched, once, and the
        # only contract validation was dispatch's own defensive pass.
        harness.execute_tool.assert_called_once()
        dispatched = harness.execute_tool.call_args.args
        self.assertEqual(dispatched[:2], ("calculator", "calculate"))
        self.assertIs(dispatched[2], plan.steps[0].arguments)
        harness.validate_action.assert_called_once_with(
            "calculator",
            "calculate",
            plan.steps[0].arguments,
        )
        # Permission was decided when the plan was offered and again when
        # the approved plan ran; it was never prepared or rebound.
        self.assertEqual(
            harness.check.call_args_list,
            [call("calculator", "calculate"), call("calculator", "calculate")],
        )
        harness.prepare_confirmation.assert_not_called()
        self.assertEqual(harness.create_plan.call_count, 1)
        self.assertEqual(harness.calculator_runs.actions, ["calculate"])
        harness.record_audit_summary.assert_called_once_with(
            "calculator.calculate",
            AuditDecision.OWNER_APPROVED,
            AuditTarget.NONE,
        )
        harness.record_audit_outcome.assert_called_once_with(
            1,
            AuditOutcome.SUCCEEDED,
        )
        self.assertEqual(
            harness.audit_rows(),
            (
                [_summary_row(1, "calculator.calculate", "none")],
                [(1, "succeeded")],
            ),
        )
        # A model-originated plan is transient: nothing became history.
        self.assertEqual(harness.interactions(), [])
        self.assertEqual(
            session.submit("read audit 1").text,
            _block(1, "calculator.calculate", "none", "Outcome: succeeded"),
        )
        self.assertEqual(model.calls, 1)

    def test_a_dispatch_answer_that_is_not_a_tool_result_counts_as_failed(
        self,
    ) -> None:
        class _LooksLikeAResult:
            """An object shaped like a successful result, but not one."""

            success = True
            message = "Looks like a successful result."

        for label, answer in (
            ("plain text", "2 + 2 = 4"),
            ("nothing", None),
            ("a look-alike", _LooksLikeAResult()),
        ):
            with self.subTest(answer=label):
                harness = _Harness(self, _Model(CALCULATE))
                self.offer(harness)
                harness.execute_tool.return_value = answer

                confirmed = harness.session.confirm()

                # Neither shown nor trusted: the shared fixed failure stands
                # in for it, and the outcome says failed.
                self.assertEqual(
                    confirmed.text,
                    _recorded(TOOL_EXECUTION_FAILED, 1),
                )
                harness.execute_tool.assert_called_once()
                harness.record_audit_outcome.assert_called_once_with(
                    1,
                    AuditOutcome.FAILED,
                )
                self.assertEqual(
                    harness.audit_rows(),
                    (
                        [_summary_row(1, "calculator.calculate", "none")],
                        [(1, "failed")],
                    ),
                )

    def test_an_outcome_write_that_raises_keeps_the_result_and_says_unknown(
        self,
    ) -> None:
        model = _Model(CALCULATE)
        harness = _Harness(self, model)
        session = harness.session
        self.offer(harness)

        with _raising(harness.record_audit_outcome, RuntimeError(EXCEPTION_SENTINEL)):
            confirmed = session.confirm()

        self.assertEqual(
            confirmed.text,
            f"2 + 2 = 4\n\n{_outcome_unrecorded(1)}",
        )
        self.assertNotIn(EXCEPTION_SENTINEL, confirmed.text)
        harness.record_audit_outcome.assert_called_once_with(
            1,
            AuditOutcome.SUCCEEDED,
        )
        self.assertEqual(harness.calculator_runs.actions, ["calculate"])
        self.assertEqual(
            harness.audit_rows(),
            ([_summary_row(1, "calculator.calculate", "none")], []),
        )

        # Nothing is retried: the approval is spent, and the summary says the
        # outcome is unknown.
        self.assertEqual(session.confirm().text, CONFIRMATION_REFUSED)
        self.assertEqual(
            session.submit("read audit 1").text,
            _block(1, "calculator.calculate", "none", OUTCOME_UNKNOWN),
        )
        self.assertEqual(harness.calculator_runs.actions, ["calculate"])
        harness.record_audit_outcome.assert_called_once()
        self.assertIn(
            "The outcome of audit summary 1 could not be recorded.",
            self.logged(),
        )
        assert_log_omits(self, self.log_path, (EXCEPTION_SENTINEL,))
        self.assertEqual(model.calls, 1)

    def test_an_outcome_whose_summary_vanished_leaves_no_orphan(self) -> None:
        model = _Model(CALCULATE)
        harness = _Harness(self, model)
        session = harness.session
        self.offer(harness)
        execute = harness.real_execute_tool
        present_during_dispatch: list[int] = []

        def run_then_delete_summary(
            tool_name: str,
            action: str,
            arguments: Mapping[str, Any],
        ) -> object:
            result = execute(tool_name, action, arguments)
            present_during_dispatch.append(
                harness.row_counts()["audit_summaries"]
            )
            harness.raw("DELETE FROM audit_summaries WHERE id = ?", (1,))
            return result

        harness.execute_tool.side_effect = run_then_delete_summary

        confirmed = session.confirm()

        self.assertEqual(present_during_dispatch, [1])
        self.assertEqual(
            confirmed.text,
            f"2 + 2 = 4\n\n{_outcome_unrecorded(1)}",
        )
        harness.record_audit_outcome.assert_called_once_with(
            1,
            AuditOutcome.SUCCEEDED,
        )
        self.assertEqual(harness.calculator_runs.actions, ["calculate"])
        # The outcome insert found no summary, so it stored no orphan.
        self.assertEqual(harness.audit_rows(), ([], []))
        logged = self.logged()
        self.assertIn("Failed to record audit outcome: no row was inserted.", logged)
        self.assertIn("The outcome of audit summary 1 could not be recorded.", logged)

        harness.execute_tool.side_effect = None
        self.assertEqual(
            session.submit("read audit 1").text,
            "Audit summary not found: 1.",
        )
        self.assertEqual(model.calls, 1)

    def test_a_database_locked_after_dispatch_keeps_the_result_and_says_unknown(
        self,
    ) -> None:
        model = _Model(CALCULATE)
        harness = _Harness(self, model)
        session = harness.session
        self.offer(harness)
        execute = harness.real_execute_tool
        holders: list[sqlite3.Connection] = []

        def run_then_lock(
            tool_name: str,
            action: str,
            arguments: Mapping[str, Any],
        ) -> object:
            result = execute(tool_name, action, arguments)
            holder = sqlite3.connect(harness.database_path, isolation_level=None)
            holders.append(holder)
            holder.execute("BEGIN EXCLUSIVE")
            return result

        harness.execute_tool.side_effect = run_then_lock
        try:
            with patch.object(
                memory_module,
                "DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS",
                SHORT_BUSY_TIMEOUT_SECONDS,
            ):
                confirmed = session.confirm()
        finally:
            harness.execute_tool.side_effect = None
            for holder in holders:
                holder.close()

        self.assertEqual(len(holders), 1)
        self.assertEqual(
            confirmed.text,
            f"2 + 2 = 4\n\n{_outcome_unrecorded(1)}",
        )
        # The busy outcome write was attempted once and never retried.
        harness.record_audit_outcome.assert_called_once_with(
            1,
            AuditOutcome.SUCCEEDED,
        )
        self.assertEqual(harness.calculator_runs.actions, ["calculate"])
        summary = _summary_row(1, "calculator.calculate", "none")
        self.assertEqual(harness.audit_rows(), ([summary], []))
        self.assertIsNone(session.pending_approval)
        logged = self.logged()
        self.assertIn("Failed to record audit outcome: database busy.", logged)
        self.assertIn("The outcome of audit summary 1 could not be recorded.", logged)

        # With the lock gone, nothing reruns and the summary says the outcome
        # is unknown.
        self.assertEqual(session.confirm().text, CONFIRMATION_REFUSED)
        self.assertEqual(
            session.submit("read audit 1").text,
            _block(1, "calculator.calculate", "none", OUTCOME_UNKNOWN),
        )
        self.assertEqual(harness.calculator_runs.actions, ["calculate"])
        harness.record_audit_outcome.assert_called_once()
        self.assertEqual(harness.audit_rows(), ([summary], []))
        self.assertEqual(model.calls, 1)

    def test_a_failed_listing_records_a_failed_outcome(self) -> None:
        model = _Model(LIST_MISSING)
        harness = _Harness(self, model)
        session = harness.session
        self.offer(
            harness,
            "Please show me what is in the missing folder.",
            _approval("filesystem", "list", '{"path": "missing"}', NO_CONTEXT_NOTICE),
        )

        confirmed = session.confirm()

        self.assertEqual(
            confirmed.text,
            _recorded("Not a directory: missing", 1),
        )
        harness.record_audit_summary.assert_called_once_with(
            "filesystem.list",
            AuditDecision.OWNER_APPROVED,
            AuditTarget.NAMED_FILESYSTEM_ROOT,
        )
        harness.record_audit_outcome.assert_called_once_with(
            1,
            AuditOutcome.FAILED,
        )
        self.assertEqual(
            harness.audit_rows(),
            (
                [_summary_row(1, "filesystem.list", "named_filesystem_root")],
                [(1, "failed")],
            ),
        )
        self.assertEqual(
            session.submit("read audit 1").text,
            _block(1, "filesystem.list", "named filesystem root", "Outcome: failed"),
        )
        self.assertEqual(model.calls, 1)

    def test_listing_without_a_path_is_still_audited_as_a_named_root_action(
        self,
    ) -> None:
        model = _Model(LIST_DEFAULT_ROOT)
        harness = _Harness(self, model)
        session = harness.session
        self.offer(
            harness,
            "Please show me what is in my folder.",
            _approval("filesystem", "list", "{}", NO_CONTEXT_NOTICE),
        )

        confirmed = session.confirm()

        self.assertEqual(confirmed.text, _recorded("Directory is empty.", 1))
        # The target class comes from the contract, never from the argument
        # values, so a listing with no path still acts in a named root.
        harness.record_audit_summary.assert_called_once_with(
            "filesystem.list",
            AuditDecision.OWNER_APPROVED,
            AuditTarget.NAMED_FILESYSTEM_ROOT,
        )
        self.assertEqual(
            harness.audit_rows(),
            (
                [_summary_row(1, "filesystem.list", "named_filesystem_root")],
                [(1, "succeeded")],
            ),
        )


# --- Privacy -----------------------------------------------------------------


class AuditPrivacyTests(_GateTestCase):
    """Prove no private value reaches an audit row or the operational log.

    One session drives every audited path while distinctive sentinels are in
    play: the owner's request, a proposal's path and expression, an enabled
    memory supplied as context, a file name a listing shows, an exception
    raised on each failure path, the named root's own name, and the
    temporary host path. Each is first shown to have been in play, so its
    absence afterwards proves something.
    """

    def test_no_private_value_reaches_an_audit_row_or_the_log(self) -> None:
        self.start_log()
        model = _Model(
            _proposal("filesystem", "list", {"path": PATH_SENTINEL}),
            _proposal("calculator", "calculate", {"expression": SENTINEL_EXPRESSION}),
            CURRENT_TIME,
            CALCULATE,
            CALCULATE,
            _proposal("filesystem", "list", {"path": MISSING_PATH}),
            _proposal("filesystem", "list", {"path": f"../{PATH_SENTINEL}"}),
        )
        harness = _Harness(self, model)
        folder = harness.workspace / PATH_SENTINEL
        folder.mkdir()
        (folder / LISTING_FILE).write_text(
            "Listed by name, never read.\n",
            encoding="utf-8",
        )
        harness.enable(MEMORY_FACT)
        session = harness.session
        request = f"Please help me with {PROMPT_SENTINEL} today."
        supplied = _supplied("memory 1")

        # A listing that succeeds and shows a file named with a sentinel.
        self.offer(
            harness,
            request,
            _approval("filesystem", "list", f'{{"path": "{PATH_SENTINEL}"}}', supplied),
        )
        self.assertEqual(session.confirm().text, _recorded(f"file: {LISTING_FILE}", 1))

        # A calculation that fails on its sentinel expression.
        self.offer(
            harness,
            request,
            _approval(
                "calculator",
                "calculate",
                f'{{"expression": "{SENTINEL_EXPRESSION}"}}',
                supplied,
            ),
        )
        self.assertEqual(session.confirm().text, _recorded(CALCULATION_FAILED, 2))

        # A clock reading whose outcome write raises the exception sentinel.
        self.offer(
            harness,
            request,
            _approval("system", "current_time", "{}", supplied),
        )
        with patch("tools.system._current_moment", return_value=CLOCK):
            with _raising(
                harness.record_audit_outcome,
                RuntimeError(EXCEPTION_SENTINEL),
            ):
                clock = session.confirm().text
        self.assertEqual(clock, f"{CLOCK_REPORT}\n\n{_outcome_unrecorded(3)}")

        # A calculation whose tool raises the exception sentinel.
        calculation = _approval(
            "calculator",
            "calculate",
            '{"expression": "2 + 2"}',
            supplied,
        )
        self.offer(harness, request, calculation)
        harness.calculator_runs.error = RuntimeError(EXCEPTION_SENTINEL)
        self.assertEqual(session.confirm().text, _recorded(TOOL_EXECUTION_FAILED, 4))
        harness.calculator_runs.error = None

        # A calculation whose summary write raises the exception sentinel.
        self.offer(harness, request, calculation)
        with _raising(harness.record_audit_summary, RuntimeError(EXCEPTION_SENTINEL)):
            self.assertEqual(session.confirm().text, AUDIT_SUMMARY_UNRECORDED)

        # A listing of a directory that does not exist.
        self.offer(
            harness,
            request,
            _approval("filesystem", "list", f'{{"path": "{MISSING_PATH}"}}', supplied),
        )
        self.assertEqual(
            session.confirm().text,
            _recorded(f"Not a directory: {MISSING_PATH}", 5),
        )

        # A path the compiler refuses never reaches the gate at all.
        refused = session.submit(request)
        self.assertEqual(refused.text, f"{UNSAFE_PATH}\n\n{supplied}")
        self.assertIsNone(session.pending_approval)

        # What the owner's own audit view shows.
        named_root = "named filesystem root"
        self.assertEqual(
            session.submit("audit").text,
            "\n".join(
                (
                    _line(5, "filesystem.list", named_root, "Outcome: failed."),
                    _line(4, "calculator.calculate", "none", "Outcome: failed."),
                    _line(3, "system.current_time", "none", OUTCOME_UNKNOWN),
                    _line(2, "calculator.calculate", "none", "Outcome: failed."),
                    _line(1, "filesystem.list", named_root, "Outcome: succeeded."),
                )
            ),
        )

        # Positive controls: every sentinel really was in play.
        self.assertEqual(model.calls, 7)
        for sent in model.requests:
            self.assertIn(PROMPT_SENTINEL, sent.prompt)
            self.assertIn(
                MEMORY_SENTINEL,
                " ".join(item.text for item in sent.context),
            )
        self.assertEqual(
            harness.config.filesystem_roots.default_root.name,
            ROOT_NAME_SENTINEL,
        )
        self.assertEqual(
            harness.config.filesystem_roots.default_root.path,
            harness.workspace,
        )
        self.assertEqual(harness.calculator_runs.actions, ["calculate", "calculate"])
        self.assertEqual(harness.record_audit_summary.call_count, 6)
        self.assertEqual(harness.record_audit_outcome.call_count, 5)

        summaries, outcomes = harness.audit_rows()
        self.assertEqual(
            summaries,
            [
                _summary_row(1, "filesystem.list", "named_filesystem_root"),
                _summary_row(2, "calculator.calculate", "none"),
                _summary_row(3, "system.current_time", "none"),
                _summary_row(4, "calculator.calculate", "none"),
                _summary_row(5, "filesystem.list", "named_filesystem_root"),
            ],
        )
        self.assertEqual(
            outcomes,
            [(1, "succeeded"), (2, "failed"), (4, "failed"), (5, "failed")],
        )
        self.assertLessEqual(
            {summary[4] for summary in summaries},
            {"named_filesystem_root", "none"},
        )
        self.assertEqual(
            harness.row_counts(),
            {
                "interactions": 1,
                "notes": 0,
                "memories": 1,
                "audit_summaries": 5,
                "audit_outcomes": 4,
                "saved_conversations": 0,
                "conversation_summaries": 0,
                "tasks": 0,
                "reminders": 0,
            },
        )
        self.assertEqual(
            harness.interactions(),
            [("audit", "Listed audit summaries.")],
        )

        private = {
            "prompt": PROMPT_SENTINEL,
            "path": PATH_SENTINEL,
            "expression": EXPRESSION_SENTINEL,
            "memory": MEMORY_SENTINEL,
            "listing": LISTING_SENTINEL,
            "exception": EXCEPTION_SENTINEL,
            "root name": ROOT_NAME_SENTINEL,
            "host path": str(harness.root),
            "host path, forward slashes": harness.root.as_posix(),
            "temporary path": str(harness.temporary_path),
            "temporary path, forward slashes": harness.temporary_path.as_posix(),
            "temporary directory name": harness.root.name,
            "workspace path": str(harness.workspace),
        }
        # Each stored value is searched as SQLite returned it, so a host path
        # is matched with its own separators rather than a repr's escapes.
        stored = [str(value) for row in (*summaries, *outcomes) for value in row]
        self.assertEqual(len(stored), 5 * 5 + 4 * 2)
        for label, value in private.items():
            with self.subTest(stored=label):
                self.assertEqual([text for text in stored if value in text], [])

        logged = self.logged()
        for line in (
            "Owner approval required for model-proposed action: filesystem.list",
            "Dispatching approved model-proposed action filesystem.list under "
            "audit summary 1.",
            "Recorded audit summary 1.",
            "Recorded the outcome of audit summary 1.",
            "Listed a directory.",
            "The outcome of audit summary 3 could not be recorded.",
            "Tool execution failed: calculator.calculate code=tool_execution_failed",
            "Refused a model-proposed action: its audit summary was not recorded.",
            "Refused a model proposal: code=unsafe_path",
            "Memory context selected: 1 item(s).",
        ):
            with self.subTest(logged=line):
                self.assertIn(line, logged)
        assert_log_omits(self, self.log_path, tuple(private.values()))


# --- Context notices ----------------------------------------------------------


class ContextNoticeTests(_GateTestCase):
    """Prove a turn's context notices are shown once, with that turn alone.

    A refusal carries them below its fixed message, an approval prompt below
    the rendered plan, and the approved plan's result never repeats them.
    """

    def test_a_refused_proposal_shows_the_context_notice_exactly_once(
        self,
    ) -> None:
        model = _Model(CLOSED_PROPOSAL)
        harness = _Harness(self, model)
        harness.enable(TEAL_FACT)
        notice = _supplied("memory 1")

        refused = harness.session.submit(REQUEST)

        self.assertEqual(refused.text, f"{NOT_MODEL_ELIGIBLE}\n\n{notice}")
        self.assertEqual(refused.text.count(notice), 1)
        self.assertFalse(refused.needs_confirmation)
        self.assertIsNone(harness.session.pending_approval)
        # The compiler refused it before permission or the gate was asked,
        # and a refusal is recorded nowhere: the one row is the memory.
        harness.check.assert_not_called()
        harness.execute_tool.assert_not_called()
        harness.record_audit_summary.assert_not_called()
        self.assertEqual(harness.row_counts(), {**ZERO_ROWS, "memories": 1})
        self.assertEqual(model.calls, 1)

    def test_the_approval_shows_the_notice_once_and_the_result_omits_it(
        self,
    ) -> None:
        model = _Model(CALCULATE)
        harness = _Harness(self, model)
        harness.enable(TEAL_FACT)
        notice = _supplied("memory 1")
        session = harness.session

        offered = self.offer(
            harness,
            expected=_approval(
                "calculator",
                "calculate",
                '{"expression": "2 + 2"}',
                notice,
            ),
        )

        self.assertEqual(offered.text.count(notice), 1)
        self.assertTrue(offered.text.startswith(APPROVAL_HEADER))
        self.assertTrue(offered.text.endswith(f"\n\n{notice}"))
        harness.check.assert_called_once_with("calculator", "calculate")
        harness.prepare_confirmation.assert_not_called()
        harness.execute_tool.assert_not_called()
        harness.record_audit_summary.assert_not_called()

        confirmed = session.confirm()

        self.assertEqual(confirmed.text, _recorded("2 + 2 = 4", 1))
        self.assertNotIn("Memory context", confirmed.text)
        self.assertEqual(
            harness.row_counts(),
            {
                **ZERO_ROWS,
                "memories": 1,
                "audit_summaries": 1,
                "audit_outcomes": 1,
            },
        )
        self.assertEqual(model.calls, 1)


# --- Clearing and fingerprints ----------------------------------------------


class ContextClearingTests(_GateTestCase):
    """Prove a proposal turn keeps the clearing it made and commits nothing.

    When a record supplied earlier changed, or context could not be read,
    the session's conversation and fingerprints are cleared and the owner is
    told once, whether the turn ends in a refusal or an approval prompt. A
    proposal turn that cleared nothing leaves both exactly as they were: the
    fingerprints of what it supplied are not kept, because the proposal is
    not kept either.
    """

    def _converse_then_change_the_record(self, model: _Model) -> _Harness:
        """Hold one exchange supplied with memory 1, then change memory 1."""
        harness = _Harness(self, model)
        harness.enable(TEAL_FACT)
        first = harness.session.submit("Tell me something cheerful.")
        self.assertEqual(first.text, f"Noted.\n\n{_supplied('memory 1')}")
        self.assertEqual(len(harness.session.conversation), 1)
        self.assertEqual(
            _record_ids(harness.session.supplied_context),
            {("memory", 1)},
        )
        # Updating the fact also turns its context consent off, so the copy
        # this conversation was given is no longer current.
        self.assertEqual(harness.memory.update_memory(1, GREEN_FACT), (True, None))
        return harness

    def test_a_refused_proposal_keeps_the_clearing_a_changed_record_caused(
        self,
    ) -> None:
        model = _Model("Noted.", CLOSED_PROPOSAL)
        harness = self._converse_then_change_the_record(model)
        session = harness.session

        refused = session.submit(REQUEST)

        self.assertEqual(
            refused.text,
            f"{NOT_MODEL_ELIGIBLE}\n\n{CLEARED_NOTICE}\n{NO_CONTEXT_NOTICE}",
        )
        self.assertEqual(refused.text.count(CLEARED_NOTICE), 1)
        self.assertEqual(session.conversation, ())
        self.assertEqual(session.supplied_context, frozenset())
        self.assertIsNone(session.pending_approval)
        # The earlier exchange was cleared before the request was sent.
        self.assertEqual(model.requests[1].history, ())
        self.assertEqual(model.requests[1].context, ())
        self.assertEqual(model.calls, 2)
        self.assertEqual(harness.row_counts(), {**ZERO_ROWS, "memories": 1})

    def test_an_approval_prompt_keeps_the_clearing_a_changed_record_caused(
        self,
    ) -> None:
        model = _Model("Noted.", CALCULATE)
        harness = self._converse_then_change_the_record(model)
        session = harness.session

        offered = self.offer(
            harness,
            expected=_approval(
                "calculator",
                "calculate",
                '{"expression": "2 + 2"}',
                CLEARED_NOTICE,
                NO_CONTEXT_NOTICE,
            ),
        )

        self.assertEqual(offered.text.count(CLEARED_NOTICE), 1)
        self.assertEqual(session.conversation, ())
        self.assertEqual(session.supplied_context, frozenset())
        self.assertEqual(model.requests[1].history, ())

        confirmed = session.confirm()

        self.assertEqual(confirmed.text, _recorded("2 + 2 = 4", 1))
        self.assertNotIn(CLEARED_NOTICE, confirmed.text)
        self.assertEqual(session.conversation, ())
        self.assertEqual(session.supplied_context, frozenset())
        self.assertEqual(
            harness.row_counts(),
            {
                **ZERO_ROWS,
                "memories": 1,
                "audit_summaries": 1,
                "audit_outcomes": 1,
            },
        )
        self.assertEqual(model.calls, 2)

    def test_a_refused_proposal_keeps_the_clearing_unreadable_context_caused(
        self,
    ) -> None:
        model = _Model("Noted.", CLOSED_PROPOSAL)
        harness = _Harness(self, model)
        session = harness.session
        session.submit("Tell me something cheerful.")
        self.assertEqual(len(session.conversation), 1)

        with patch.object(
            harness.memory,
            "list_context_memories",
            side_effect=RuntimeError("Unable to read memory context."),
        ):
            refused = session.submit(REQUEST)

        self.assertEqual(
            refused.text,
            f"{NOT_MODEL_ELIGIBLE}\n\n{UNREADABLE_NOTICE}\n{NO_CONTEXT_NOTICE}",
        )
        self.assertEqual(refused.text.count(UNREADABLE_NOTICE), 1)
        self.assertEqual(session.conversation, ())
        self.assertEqual(session.supplied_context, frozenset())
        self.assertEqual(model.requests[1].history, ())
        self.assertEqual(harness.row_counts(), ZERO_ROWS)
        self.assertEqual(model.calls, 2)

    def test_a_proposal_turn_commits_no_new_fingerprints(self) -> None:
        model = _Model("Noted.", CLOSED_PROPOSAL, CALCULATE, "Noted again.")
        harness = _Harness(self, model)
        session = harness.session
        harness.enable(TEAL_FACT)
        session.submit("Tell me something cheerful.")
        conversation = session.conversation
        supplied = session.supplied_context
        self.assertEqual(len(conversation), 1)
        self.assertEqual(_record_ids(supplied), {("memory", 1)})
        harness.enable(EDITOR_FACT)
        both = _supplied("memory 1, memory 2")

        refused = session.submit(REQUEST)

        self.assertEqual(refused.text, f"{NOT_MODEL_ELIGIBLE}\n\n{both}")
        self.assertEqual(session.conversation, conversation)
        self.assertEqual(session.supplied_context, supplied)

        self.offer(
            harness,
            expected=_approval(
                "calculator",
                "calculate",
                '{"expression": "2 + 2"}',
                both,
            ),
        )
        self.assertEqual(session.conversation, conversation)
        self.assertEqual(session.supplied_context, supplied)

        self.assertEqual(session.confirm().text, _recorded("2 + 2 = 4", 1))
        self.assertEqual(session.conversation, conversation)
        self.assertEqual(session.supplied_context, supplied)

        # Positive control: a conversational turn does keep what it supplied.
        after = session.submit("Anything else?")
        self.assertEqual(after.text, f"Noted again.\n\n{both}")
        self.assertEqual(len(session.conversation), 2)
        self.assertEqual(
            _record_ids(session.supplied_context),
            {("memory", 1), ("memory", 2)},
        )
        # Neither proposal ever became history the model was sent.
        self.assertEqual(model.calls, 4)
        for sent in model.requests[1:]:
            self.assertEqual(sent.history, conversation)


# --- Provider calls ------------------------------------------------------------


class ProviderCallTests(_GateTestCase):
    """Prove every model-backed request asks the model exactly once.

    A conversational reply, a refused proposal, a proposal offered and run,
    one offered and cancelled, one that ran and failed, and a provider that
    failed each cost exactly one request. Confirming, cancelling, and a
    deterministic command cost none.
    """

    def test_every_model_path_asks_the_model_exactly_once_per_request(
        self,
    ) -> None:
        model = _Model(
            "A plain reply.",
            CLOSED_PROPOSAL,
            CALCULATE,
            CALCULATE,
            LIST_MISSING,
            RuntimeError(EXCEPTION_SENTINEL),
        )
        harness = _Harness(self, model)
        session = harness.session

        reply = session.submit("Tell me something cheerful.")
        self.assertEqual(reply.text, f"A plain reply.\n\n{NO_CONTEXT_NOTICE}")
        self.assertEqual(model.calls, 1)

        refused = session.submit("Please tidy everything up.")
        self.assertEqual(refused.text, f"{NOT_MODEL_ELIGIBLE}\n\n{NO_CONTEXT_NOTICE}")
        self.assertEqual(model.calls, 2)

        self.offer(harness)
        self.assertEqual(model.calls, 3)
        self.assertEqual(session.confirm().text, _recorded("2 + 2 = 4", 1))
        self.assertEqual(model.calls, 3)

        self.offer(harness)
        self.assertEqual(model.calls, 4)
        self.assertTrue(session.cancel())
        self.assertEqual(session.confirm().text, CONFIRMATION_REFUSED)
        self.assertEqual(model.calls, 4)

        self.offer(
            harness,
            "Please show me what is in the missing folder.",
            _approval("filesystem", "list", '{"path": "missing"}', NO_CONTEXT_NOTICE),
        )
        self.assertEqual(model.calls, 5)
        self.assertEqual(
            session.confirm().text,
            _recorded("Not a directory: missing", 2),
        )
        self.assertEqual(model.calls, 5)

        failed = session.submit("Are you still there?")
        self.assertEqual(failed.text, f"{REASONING_UNAVAILABLE}\n\n{NO_CONTEXT_NOTICE}")
        self.assertEqual(model.calls, 6)

        self.assertEqual(session.submit("calculate 2 + 2").text, "2 + 2 = 4")
        self.assertEqual(model.calls, 6)

        # Every request carried Maria's proposal instructions, and none
        # carried a proposal back as history.
        self.assertTrue(
            all(sent.proposal_instructions is True for sent in model.requests)
        )
        first_exchange = model.requests[1].history
        self.assertEqual(len(first_exchange), 1)
        for sent in model.requests[1:]:
            self.assertEqual(sent.history, first_exchange)
        self.assertEqual(harness.calculator_runs.actions, ["calculate", "calculate"])
        self.assertEqual(
            harness.audit_rows(),
            (
                [
                    _summary_row(1, "calculator.calculate", "none"),
                    _summary_row(2, "filesystem.list", "named_filesystem_root"),
                ],
                [(1, "succeeded"), (2, "failed")],
            ),
        )


# --- Permission policy ---------------------------------------------------------


class PermissionPolicyTests(_GateTestCase):
    """Prove configured policy decides only whether a model plan is offered.

    A denied action is refused before any approval is offered, with the
    turn's notices once. An action configured to need confirmation still
    needs exactly the one approval every model plan needs, while the same
    action requested deterministically keeps its own confirmation and is
    never audited.
    """

    def test_a_denied_model_action_is_refused_before_approval_or_audit(
        self,
    ) -> None:
        model = _Model(CALCULATE)
        harness = _Harness(
            self,
            model,
            denied_actions=frozenset({"calculator.calculate"}),
        )
        harness.enable(TEAL_FACT)
        notice = _supplied("memory 1")
        session = harness.session
        denial = "Permission denied: calculator.calculate is denied by configuration."

        refused = session.submit(REQUEST)

        self.assertEqual(refused.text, f"{denial}\n\n{notice}")
        self.assertEqual(refused.text.count(notice), 1)
        self.assertFalse(refused.needs_confirmation)
        self.assertIsNone(session.pending_approval)
        harness.check.assert_called_once_with("calculator", "calculate")
        harness.prepare_confirmation.assert_not_called()
        harness.validate_action.assert_not_called()
        harness.execute_tool.assert_not_called()
        harness.record_audit_summary.assert_not_called()
        self.assertEqual(session.confirm().text, CONFIRMATION_REFUSED)
        harness.execute_tool.assert_not_called()
        self.assertEqual(harness.calculator_runs.actions, [])
        self.assertEqual(model.calls, 1)

        # The same action requested deterministically is denied as before
        # and recorded as before, without the model or the gate.
        self.assertEqual(session.submit("calculate 2 + 2").text, denial)
        self.assertEqual(model.calls, 1)
        self.assertEqual(harness.interactions(), [("calculate 2 + 2", denial)])
        self.assertEqual(harness.audit_rows(), ([], []))

    def test_a_confirmation_required_model_action_needs_exactly_one_approval(
        self,
    ) -> None:
        model = _Model(CALCULATE)
        harness = _Harness(
            self,
            model,
            confirmation_required=frozenset({"calculator.calculate"}),
        )
        session = harness.session

        self.offer(harness)

        harness.check.assert_called_once_with("calculator", "calculate")
        harness.prepare_confirmation.assert_not_called()
        harness.execute_tool.assert_not_called()

        self.assertEqual(session.confirm().text, _recorded("2 + 2 = 4", 1))
        self.assertEqual(harness.check.call_count, 2)
        self.assertEqual(harness.calculator_runs.actions, ["calculate"])
        summary = _summary_row(1, "calculator.calculate", "none")
        self.assertEqual(harness.audit_rows(), ([summary], [(1, "succeeded")]))

        # Requested deterministically, the same action keeps its own
        # confirmation and writes no audit summary.
        pending = session.submit("calculate 2 + 2")
        self.assertEqual(
            pending.text,
            "Confirmation required: calculator.calculate requires explicit "
            "confirmation.",
        )
        self.assertTrue(pending.needs_confirmation)
        harness.prepare_confirmation.assert_called_once()
        self.assertEqual(session.confirm().text, "2 + 2 = 4")
        self.assertEqual(harness.calculator_runs.actions, ["calculate", "calculate"])
        self.assertEqual(harness.audit_rows(), ([summary], [(1, "succeeded")]))
        harness.record_audit_summary.assert_called_once()
        self.assertEqual(harness.interactions(), [("calculate 2 + 2", "2 + 2 = 4")])
        self.assertEqual(model.calls, 1)


# --- Deterministic requests ----------------------------------------------------


class DeterministicPathTests(_GateTestCase):
    """Prove deterministic requests never reach the model or the audit gate."""

    def _assert_untouched_gate(self, harness: _Harness) -> None:
        """Assert no model was asked and no audit write was attempted."""
        self.assertEqual(harness.provider.calls, 0)
        harness.record_audit_summary.assert_not_called()
        harness.record_audit_outcome.assert_not_called()
        self.assertEqual(harness.audit_rows(), ([], []))

    def test_with_reasoning_disabled_the_fallback_is_answered_and_recorded(
        self,
    ) -> None:
        model = _Model(CALCULATE)
        harness = _Harness(self, model, settings=DISABLED_REASONING)
        session = harness.session

        # Proposal-shaped text the owner types is just an unsupported request.
        for request in (REQUEST, CALCULATE):
            with self.subTest(request=request):
                response = session.submit(request)

                self.assertEqual(response.text, GENERIC_FALLBACK)
                self.assertFalse(response.needs_confirmation)
                self.assertIsNone(session.pending_approval)

        self.assertEqual(
            harness.interactions(),
            [(REQUEST, GENERIC_FALLBACK), (CALCULATE, GENERIC_FALLBACK)],
        )
        harness.check.assert_not_called()
        harness.execute_tool.assert_not_called()
        self._assert_untouched_gate(harness)

    def test_with_reasoning_disabled_a_calculation_runs_without_approval(
        self,
    ) -> None:
        model = _Model(CALCULATE)
        harness = _Harness(self, model, settings=DISABLED_REASONING)
        session = harness.session

        response = session.submit("calculate 2 + 2")

        self.assertEqual(response.text, "2 + 2 = 4")
        self.assertFalse(response.needs_confirmation)
        self.assertIsNone(session.pending_approval)
        self.assertEqual(harness.calculator_runs.actions, ["calculate"])
        harness.prepare_confirmation.assert_not_called()
        self.assertEqual(harness.interactions(), [("calculate 2 + 2", "2 + 2 = 4")])
        self._assert_untouched_gate(harness)

    def test_with_reasoning_enabled_deterministic_commands_skip_the_model(
        self,
    ) -> None:
        model = _Model(CALCULATE)
        harness = _Harness(self, model)
        session = harness.session

        for command, expected in (
            ("calculate 2 + 2", "2 + 2 = 4"),
            ("list .", "Directory is empty."),
            ("audit", "No audit summaries found."),
            ("read audit 1", "Audit summary not found: 1."),
            ("read audit x", "Audit summary id must be a positive whole number."),
            ("read audit", "Use: read audit <id>"),
            ("delete audit", "Use: delete audit <id>"),
            ("delete audit 1", AUDIT_DELETE_CONFIRMATION),
        ):
            with self.subTest(command=command):
                self.assertEqual(session.submit(command).text, expected)
        self.assertEqual(session.confirm().text, "Audit summary not found: 1.")

        for command in ("help", "time", "history", "notes", "memories"):
            with self.subTest(command=command):
                response = session.submit(command)

                self.assertFalse(response.needs_confirmation)
                self.assertNotIn(APPROVAL_HEADER, response.text)

        self.assertEqual(harness.calculator_runs.actions, ["calculate"])
        self._assert_untouched_gate(harness)


if __name__ == "__main__":
    unittest.main()
