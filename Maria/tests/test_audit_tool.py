"""Focused tests for V1-18's deterministic audit surface.

An audit summary is Maria's own content-free record that the owner approved
one model-proposed action. The owner reads and removes summaries only through
three deterministic commands, so these tests cover the planner grammar and
help text, the three closed action contracts, the permission floor on
deletion, the audit tool over a temporary database, and the confirmed session
flow with reasoning disabled and enabled. Summaries are seeded through the
memory layer's own audit methods. Every provider is a stub, every database,
log, and workspace is temporary, and Maria's live database, live log, and
private override are never read.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import tempfile
import unittest
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from unittest.mock import ANY, Mock, call, patch

from core.action_contracts import (
    BUILT_IN_ACTION_CONTRACTS,
    ActionContract,
    ActionContractRegistry,
    ArgumentContract,
    ContractFailure,
    ModelEligibility,
)
from core.audit import (
    AUDITED_ACTIONS,
    AuditDecision,
    AuditOutcome,
    AuditSummary,
    AuditTarget,
)
from core.brain import BrainResponse, MariaBrain
from core.config import load_config
from core.context import ContextSource, render_context_data
from core.conversation import is_generic_fallback
from core.logger import configure_logging, get_logger
from core.memory import Memory
from core.permissions import (
    CONFIRMATION_NOTICES,
    MANDATORY_CONFIRMATION_ACTIONS,
    MANDATORY_DENIED_ACTIONS,
    PermissionDecision,
    PermissionManager,
    PermissionResult,
)
from core.plan_compiler import (
    MODEL_ACTION_ALLOWLIST,
    ProposalRejection,
    compile_proposal,
    recheck_model_plan,
)
from core.planner import Plan, PlanOrigin, Planner, PlanStep, RecordingPolicy
from core.reasoning import (
    DISABLED_REASONING,
    ReasoningExchange,
    ReasoningRequest,
    ReasoningResponse,
    ReasoningSettings,
    system_instructions,
)
from core.session import CONFIRMATION_REFUSED_MESSAGE, MariaSession
from core.tool_manager import BUILT_IN_TOOL_MODULES, ToolExecutionResult, ToolManager
from tests.log_assertions import assert_log_omits
from tests.test_brain_recording_policy import _quiet_logger
from tests.test_conversation import (
    ScriptedProvider,
    _spy,
    _test_config,
    enabled_settings,
)
from tools.audit import AuditTool, build_tool


# Maria's own audit timestamps, pinned while seeding. They are deliberately
# not in id order, so a listing ordered by time rather than by id would show.
FIRST_TIMESTAMP = "2024-02-29T23:59:58Z"
SECOND_TIMESTAMP = "2025-07-14T05:06:07Z"
THIRD_TIMESTAMP = "2023-12-31T00:00:01Z"
FOURTH_TIMESTAMP = "2025-11-30T12:34:56Z"
FIFTH_TIMESTAMP = "2026-01-15T08:09:10Z"

FAILURE_SENTINEL = "private-audit-failure-6d0b2f9e31"
TAMPER_SENTINEL = "private-audit-tamper-c81e4a7f05"
REPLY = "A conversational reply."

# The owner-visible wording, written out so it is pinned rather than agreed
# with.
ID_MESSAGE = "Audit summary id must be a positive whole number."
NO_SUMMARIES_MESSAGE = "No audit summaries found."
LISTED_RECORD = "Listed audit summaries."
MORE_SUMMARIES_LINE = (
    "Showing the newest 20 audit summaries. More audit summaries exist."
)
LIST_FAILED_MESSAGE = "Unable to list audit summaries."
READ_FAILED_MESSAGE = "Unable to read audit summary."
DELETE_FAILED_MESSAGE = "Unable to delete audit summary."
DELETE_NOTICE = (
    "Deleting an audit summary removes it from Maria's database. It does not "
    "undo the action, and Maria's operational log may still show that a "
    "model-proposed action ran."
)
DELETE_CONFIRMATION = (
    "Confirmation required: audit.delete requires explicit confirmation. "
    "Deleting an audit summary removes it from Maria's database. It does not "
    "undo the action, and Maria's operational log may still show that a "
    "model-proposed action ran."
)
NOT_MODEL_ELIGIBLE_MESSAGE = (
    "Maria does not accept that action from the reasoning model."
)
UNSUPPORTED_RESPONSE = (
    "I do not have an executable plan for that yet. "
    "Type 'help' to see supported commands."
)
NO_CONTEXT_SUFFIX = "\n\nNo memory context supplied."

# The largest id any summary can have, because every id is a SQLite integer.
LARGEST_ID = 2**63 - 1
# More zeroes than the 4,300 digits Python converts from text by default.
LONG_ZEROES = "0" * 5000

# The listing lines and read blocks of the summaries most tests seed: one per
# outcome, and both targets.
LINE_ONE = (
    "1: 2024-02-29T23:59:58Z calculator.calculate (target: none). "
    "Approved by the owner. Outcome: succeeded."
)
LINE_TWO = (
    "2: 2025-07-14T05:06:07Z filesystem.list (target: named filesystem root). "
    "Approved by the owner. Outcome: failed."
)
LINE_THREE = (
    "3: 2023-12-31T00:00:01Z system.current_time (target: none). "
    "Approved by the owner. Outcome not recorded; execution is unknown."
)
LINE_FOUR = (
    "4: 2025-11-30T12:34:56Z filesystem.list (target: named filesystem root). "
    "Approved by the owner. Outcome not recorded; execution is unknown."
)
TRIO_LISTING = f"{LINE_THREE}\n{LINE_TWO}\n{LINE_ONE}"

BLOCK_ONE = (
    "Audit summary 1\n"
    "Created at: 2024-02-29T23:59:58Z\n"
    "Action: calculator.calculate\n"
    "Target: none\n"
    "Decision: approved by the owner\n"
    "Outcome: succeeded"
)
BLOCK_TWO = (
    "Audit summary 2\n"
    "Created at: 2025-07-14T05:06:07Z\n"
    "Action: filesystem.list\n"
    "Target: named filesystem root\n"
    "Decision: approved by the owner\n"
    "Outcome: failed"
)
BLOCK_THREE = (
    "Audit summary 3\n"
    "Created at: 2023-12-31T00:00:01Z\n"
    "Action: system.current_time\n"
    "Target: none\n"
    "Decision: approved by the owner\n"
    "Outcome not recorded; execution is unknown."
)
BLOCK_FOUR = (
    "Audit summary 4\n"
    "Created at: 2025-11-30T12:34:56Z\n"
    "Action: filesystem.list\n"
    "Target: named filesystem root\n"
    "Decision: approved by the owner\n"
    "Outcome not recorded; execution is unknown."
)

# Text that only a summary's stored fields, or their rendering, contain. None
# of it may reach interaction history, a model request, or the log.
SUMMARY_FIELD_TEXTS = (
    FIRST_TIMESTAMP,
    SECOND_TIMESTAMP,
    THIRD_TIMESTAMP,
    "calculator.calculate",
    "filesystem.list",
    "system.current_time",
    "owner_approved",
    "named_filesystem_root",
    "named filesystem root",
    "Approved by the owner",
    "approved by the owner",
    "Outcome",
    "execution is unknown",
    "Created at",
)

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
ZERO_COUNTS = {table: 0 for table in RECORD_TABLES}
SEEDED_COUNTS = {**ZERO_COUNTS, "audit_summaries": 3, "audit_outcomes": 2}

# One valid step for each audit action, as a plan or a proposal would name it.
AUDIT_STEPS = (("list", {}), ("read", {"id": 1}), ("delete", {"id": 1}))

# The target each audited action's contract implies, written out by hand.
AUDITED_TARGETS = {
    "calculator.calculate": AuditTarget.NONE,
    "filesystem.list": AuditTarget.NAMED_FILESYSTEM_ROOT,
    "system.current_time": AuditTarget.NONE,
}

# Summary 2's stored values, and values outside the audit vocabulary written
# over them by a raw connection: (table, key column, column, value).
SECOND_STORED = {
    "action": "filesystem.list",
    "created_at": SECOND_TIMESTAMP,
    "decision": "owner_approved",
    "target": "named_filesystem_root",
    "outcome": "failed",
}
TAMPERED_VALUES = (
    ("audit_summaries", "id", "action", TAMPER_SENTINEL),
    ("audit_summaries", "id", "action", "memory.store"),
    ("audit_summaries", "id", "action", "audit.delete"),
    ("audit_summaries", "id", "action", "Filesystem.list"),
    ("audit_summaries", "id", "action", " filesystem.list"),
    ("audit_summaries", "id", "created_at", TAMPER_SENTINEL),
    ("audit_summaries", "id", "created_at", "2026-02-30T00:00:00Z"),
    ("audit_summaries", "id", "created_at", "2025-07-14 05:06:07"),
    ("audit_summaries", "id", "created_at", "2025-07-14T05:06:07+00:00"),
    ("audit_summaries", "id", "created_at", SECOND_TIMESTAMP.encode("ascii")),
    ("audit_summaries", "id", "decision", TAMPER_SENTINEL),
    ("audit_summaries", "id", "decision", "OWNER_APPROVED"),
    ("audit_summaries", "id", "target", TAMPER_SENTINEL),
    ("audit_summaries", "id", "target", "named filesystem root"),
    ("audit_outcomes", "summary_id", "outcome", TAMPER_SENTINEL),
    ("audit_outcomes", "summary_id", "outcome", "Failed"),
)

# A loadable configuration needs Maria's named roots. This is the shipped
# declaration, so the permission arrays are the only thing under test.
FILESYSTEM_SECTION = """
[filesystem]
default_root = "workspace"

[[filesystem.roots]]
name = "workspace"
path = "~/MariaWorkspace"
policy = "organize"
enabled = true

[[filesystem.roots]]
name = "project_maria"
path = ".."
policy = "read_only"
enabled = false
"""


def _seed(
    memory: Memory,
    action: str,
    outcome: AuditOutcome | None = None,
    *,
    created_at: str,
) -> int:
    """Record one summary, and optionally its outcome, at a pinned time."""
    with patch("core.memory.audit_timestamp", return_value=created_at):
        summary_id = memory.record_audit_summary(
            action,
            AuditDecision.OWNER_APPROVED,
            AUDITED_TARGETS[action],
        )
    if outcome is not None:
        memory.record_audit_outcome(summary_id, outcome)
    return summary_id


def _seed_trio(memory: Memory) -> None:
    """Seed summaries 1 to 3: succeeded, failed, and no outcome recorded."""
    _seed(
        memory,
        "calculator.calculate",
        AuditOutcome.SUCCEEDED,
        created_at=FIRST_TIMESTAMP,
    )
    _seed(memory, "filesystem.list", AuditOutcome.FAILED, created_at=SECOND_TIMESTAMP)
    _seed(memory, "system.current_time", created_at=THIRD_TIMESTAMP)


def _seed_four(memory: Memory) -> None:
    """Seed the trio, then summary 4: a directory listing with no outcome."""
    _seed_trio(memory)
    _seed(memory, "filesystem.list", created_at=FOURTH_TIMESTAMP)


def _bulk_timestamp(summary_id: int) -> str:
    """Return the pinned timestamp of one bulk-seeded summary."""
    return f"2025-01-01T00:00:{summary_id:02d}Z"


def _bulk_line(summary_id: int) -> str:
    """Return the listing line of one bulk-seeded summary."""
    return (
        f"{summary_id}: 2025-01-01T00:00:{summary_id:02d}Z system.current_time "
        "(target: none). Approved by the owner. Outcome not recorded; "
        "execution is unknown."
    )


def _audit_rows(database_path: Path) -> list[tuple[object, ...]]:
    """Read every stored summary with its outcome, by id, from a temp database."""
    with closing(sqlite3.connect(database_path)) as connection:
        return connection.execute(
            "SELECT summary.id, summary.created_at, summary.action, "
            "summary.decision, summary.target, result.outcome "
            "FROM audit_summaries AS summary "
            "LEFT JOIN audit_outcomes AS result "
            "ON result.summary_id = summary.id "
            "ORDER BY summary.id"
        ).fetchall()


def _outcome_rows(database_path: Path) -> list[tuple[object, ...]]:
    """Read every stored outcome, by summary id, from a temporary database."""
    with closing(sqlite3.connect(database_path)) as connection:
        return connection.execute(
            "SELECT summary_id, outcome FROM audit_outcomes ORDER BY summary_id"
        ).fetchall()


def _raw_interactions(database_path: Path) -> list[tuple[object, ...]]:
    """Read every interaction column, oldest first, from a temporary database."""
    with closing(sqlite3.connect(database_path)) as connection:
        return connection.execute(
            "SELECT id, user_input, response, created_at FROM interactions "
            "ORDER BY id"
        ).fetchall()


def _raw_execute(database_path: Path, statement: str, parameters: tuple) -> None:
    """Change a temporary database through a second, raw connection."""
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(statement, parameters)
        connection.commit()


def _row_counts(database_path: Path) -> dict[str, int]:
    """Count the rows of all five record tables in a temporary database."""
    with closing(sqlite3.connect(database_path)) as connection:
        return {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in RECORD_TABLES
        }


def _everything_sent(requests: Iterable[ReasoningRequest]) -> str:
    """Return every text the requests carried, Maria's instructions included."""
    parts: list[str] = []
    for request in requests:
        parts.append(request.prompt)
        for exchange in request.history:
            parts.extend((exchange.user, exchange.assistant))
        parts.append(render_context_data(request.context))
        instructions = system_instructions(
            request.context,
            request.proposal_instructions,
        )
        if instructions is not None:
            parts.append(instructions)
    return "\n".join(parts)


def _silence_maria(test: unittest.TestCase) -> None:
    """Keep tool warnings off the console for the duration of one test."""
    namespace = logging.getLogger("maria")
    quiet = logging.NullHandler()
    namespace.addHandler(quiet)
    test.addCleanup(namespace.removeHandler, quiet)


class _AuditHarness:
    """Real planner, contracts, permissions, tools, memory, Brain, and session.

    Everything lives under one temporary directory, and the permission policy
    is whatever configuration the test supplies above Maria's mandatory
    floor. Planning, contract validation, confirmation preparation,
    permission checks, dispatch, and every audit call on Brain's memory layer
    are wrapped by spies that still call the real components, so a test can
    prove which were reached. Summaries are seeded through ``seeder``, a
    second memory layer on the same file, so seeding never counts as a call
    Brain or the audit tool made.
    """

    def __init__(
        self,
        test: unittest.TestCase,
        provider: object | None = None,
        *,
        settings: ReasoningSettings | None = None,
        confirmation_required: frozenset[str] = frozenset(),
        denied_actions: frozenset[str] = frozenset(),
        logger: logging.Logger | None = None,
    ) -> None:
        _silence_maria(test)
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        if settings is None:
            settings = DISABLED_REASONING if provider is None else enabled_settings()
        self.config = dataclasses.replace(
            _test_config(root, settings),
            confirmation_required=frozenset(confirmation_required),
            denied_actions=frozenset(denied_actions),
        )
        self.database_path = self.config.database_path

        self.logger = logger or _quiet_logger("tests.audit_tool.harness")
        self.memory = Memory(self.database_path, logger=self.logger)
        self.memory.initialize()
        self.seeder = Memory(
            self.database_path,
            logger=_quiet_logger("tests.audit_tool.seeder"),
        )
        self.tool_manager = ToolManager(
            config=self.config,
            memory=self.memory,
            logger=self.logger,
        )
        self.tool_manager.load_tools()
        self.permissions = PermissionManager(
            confirmation_required=self.config.confirmation_required,
            denied_actions=self.config.denied_actions,
            logger=self.logger,
        )
        self.planner = Planner(logger=self.logger)

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
        self.list_audit_summaries = _spy(test, self.memory, "list_audit_summaries")
        self.get_audit_summary = _spy(test, self.memory, "get_audit_summary")
        self.delete_audit_summary = _spy(test, self.memory, "delete_audit_summary")

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

    def row_counts(self) -> dict[str, int]:
        """Count the rows of all five record tables."""
        return _row_counts(self.database_path)

    def interactions(self) -> list[tuple[str, str]]:
        """Return every stored request and response, oldest first."""
        return [
            (str(row[1]), str(row[2]))
            for row in _raw_interactions(self.database_path)
        ]

    def audit_rows(self) -> list[tuple[object, ...]]:
        """Return every stored summary with its outcome, by id."""
        return _audit_rows(self.database_path)


# --- Planner ------------------------------------------------------------------


class AuditPlannerTests(unittest.TestCase):
    """Prove the planner routes the three commands, and nothing near them."""

    def setUp(self) -> None:
        self.planner = Planner(logger=_quiet_logger("tests.audit_tool.planner"))

    def test_audit_plans_one_listing_step_in_any_case(self) -> None:
        for command in ("audit", "AUDIT", "Audit", "  aUdIt  "):
            with self.subTest(command=command):
                plan = self.planner.create_plan(command)

                self.assertEqual(
                    plan,
                    Plan(command.strip(), (PlanStep("audit", "list", {}),)),
                )
                self.assertIs(plan.recording, RecordingPolicy.RECORD)
                self.assertIs(plan.origin, PlanOrigin.DETERMINISTIC)

    def test_read_and_delete_audit_plan_one_step_by_id_in_any_case(self) -> None:
        for command, action, summary_id in (
            ("read audit 5", "read", 5),
            ("READ AUDIT 5", "read", 5),
            ("Read Audit     5", "read", 5),
            ("rEaD aUdIt 12  ", "read", 12),
            ("read audit 007", "read", 7),
            ("delete audit 5", "delete", 5),
            ("DELETE AUDIT 5", "delete", 5),
            ("Delete Audit     5", "delete", 5),
            ("  dElEtE AUDIT 30", "delete", 30),
        ):
            with self.subTest(command=command):
                plan = self.planner.create_plan(command)

                self.assertEqual(
                    plan,
                    Plan(
                        command.strip(),
                        (PlanStep("audit", action, {"id": summary_id}),),
                    ),
                )
                self.assertIs(plan.origin, PlanOrigin.DETERMINISTIC)

    def test_bare_read_and_delete_audit_answer_with_their_usage(self) -> None:
        for command, usage in (
            ("read audit", "Use: read audit <id>"),
            ("READ AUDIT", "Use: read audit <id>"),
            ("Read Audit   ", "Use: read audit <id>"),
            ("delete audit", "Use: delete audit <id>"),
            ("DELETE AUDIT", "Use: delete audit <id>"),
            ("  Delete audit  ", "Use: delete audit <id>"),
        ):
            with self.subTest(command=command):
                plan = self.planner.create_plan(command)

                self.assertEqual(plan, Plan(command.strip(), (), usage))
                self.assertFalse(is_generic_fallback(plan))

    def test_an_id_that_is_not_a_positive_ascii_number_is_refused(self) -> None:
        arabic_indic_three = chr(0x0663)
        fullwidth_one = chr(0xFF11)
        for operand in (
            "abc",
            "0",
            "00",
            "-1",
            "+1",
            "1.5",
            "1 2",
            arabic_indic_three,
            fullwidth_one,
        ):
            for verb in ("read", "delete"):
                command = f"{verb} audit {operand}"
                with self.subTest(command=ascii(command)):
                    self.assertEqual(
                        self.planner.create_plan(command),
                        Plan(command, (), ID_MESSAGE),
                    )

    def test_list_audits_is_still_a_filesystem_listing(self) -> None:
        self.assertEqual(
            self.planner.create_plan("list audits"),
            Plan(
                "list audits",
                (PlanStep("filesystem", "list", {"path": "audits"}),),
            ),
        )

    def test_near_misses_are_the_generic_fallback(self) -> None:
        for command in (
            "audit 5",
            "audits",
            "AUDITS",
            "audit list",
            "show audit",
            "read audits 5",
            "delete audits 5",
            "readaudit 5",
        ):
            with self.subTest(command=command):
                plan = self.planner.create_plan(command)

                self.assertTrue(is_generic_fallback(plan))
                self.assertEqual(plan, Plan(command, (), UNSUPPORTED_RESPONSE))

    def test_help_ends_with_the_three_audit_commands(self) -> None:
        text = self.planner.create_plan("help").response_if_no_action

        self.assertTrue(
            text.endswith(
                "\n- delete interaction <id>\n"
                "- audit\n"
                "- read audit <id>\n"
                "- delete audit <id>"
            )
        )
        for line in ("- audit", "- read audit <id>", "- delete audit <id>"):
            with self.subTest(line=line):
                self.assertEqual(text.splitlines().count(line), 1)


# --- Contracts and the model boundary ------------------------------------------


class AuditContractTests(unittest.TestCase):
    """Prove the three audit contracts are exact and closed to the model."""

    def setUp(self) -> None:
        self.logger = _quiet_logger("tests.audit_tool.contracts")

    def test_the_three_contracts_are_exact_and_not_model_eligible(self) -> None:
        identifier = ArgumentContract("id", int, required=True, minimum=1, maximum=None)
        expected = (
            ActionContract(
                "audit",
                "list",
                model_eligibility=ModelEligibility.NOT_MODEL_ELIGIBLE,
            ),
            ActionContract(
                "audit",
                "read",
                (identifier,),
                model_eligibility=ModelEligibility.NOT_MODEL_ELIGIBLE,
            ),
            ActionContract(
                "audit",
                "delete",
                (identifier,),
                model_eligibility=ModelEligibility.NOT_MODEL_ELIGIBLE,
            ),
        )

        for contract in expected:
            with self.subTest(action=contract.action):
                declared = BUILT_IN_ACTION_CONTRACTS.contract_for(
                    "audit",
                    contract.action,
                )
                self.assertEqual(declared, contract)
                self.assertIs(
                    declared.model_eligibility,
                    ModelEligibility.NOT_MODEL_ELIGIBLE,
                )
        audit_identifiers = {"audit.list", "audit.read", "audit.delete"}
        self.assertEqual(
            {
                contract.identifier
                for contract in BUILT_IN_ACTION_CONTRACTS.contracts
                if contract.tool_name == "audit"
            },
            audit_identifiers,
        )
        self.assertEqual(
            {
                contract.identifier
                for contract in BUILT_IN_ACTION_CONTRACTS.contracts[:3]
            },
            audit_identifiers,
        )
        self.assertEqual(
            {f"audit.{action}" for action in AuditTool.actions},
            audit_identifiers,
        )

    def test_the_registry_refuses_malformed_audit_arguments(self) -> None:
        for action, arguments, kind, field in (
            ("list", {"id": 1}, ContractFailure.EXTRA_ARGUMENT, "id"),
            ("read", {}, ContractFailure.MISSING_REQUIRED_ARGUMENT, "id"),
            ("delete", {}, ContractFailure.MISSING_REQUIRED_ARGUMENT, "id"),
            ("read", {"id": True}, ContractFailure.WRONG_TYPE, "id"),
            ("delete", {"id": "1"}, ContractFailure.WRONG_TYPE, "id"),
            ("read", {"id": 0}, ContractFailure.OUT_OF_RANGE, "id"),
            ("delete", {"id": -1}, ContractFailure.OUT_OF_RANGE, "id"),
            ("read", {"id": 1, "limit": 5}, ContractFailure.EXTRA_ARGUMENT, "limit"),
        ):
            with self.subTest(action=action, arguments=arguments):
                violation = BUILT_IN_ACTION_CONTRACTS.validate(
                    "audit",
                    action,
                    arguments,
                )
                self.assertEqual((violation.kind, violation.field), (kind, field))
        for action, arguments in (
            ("list", {}),
            ("read", {"id": 1}),
            ("delete", {"id": 2**40}),
        ):
            with self.subTest(action=action, arguments=arguments):
                self.assertIsNone(
                    BUILT_IN_ACTION_CONTRACTS.validate("audit", action, arguments)
                )

    def test_the_compiler_refuses_every_audit_proposal(self) -> None:
        for proposal in (
            '{"tool":"audit","action":"list","arguments":{}}',
            '{"tool":"audit","action":"read","arguments":{"id":1}}',
            '{"tool":"audit","action":"delete","arguments":{"id":1}}',
            '{"tool":"audit","action":"delete","arguments":{}}',
            '{"tool":"audit","action":"read","arguments":{"id":"one"}}',
        ):
            with self.subTest(proposal=proposal):
                outcome = compile_proposal(
                    "tidy up my audit records",
                    ReasoningResponse(proposal),
                    BUILT_IN_ACTION_CONTRACTS,
                    self.logger,
                )

                self.assertIs(outcome, ProposalRejection.NOT_MODEL_ELIGIBLE)
                self.assertEqual(outcome.message, NOT_MODEL_ELIGIBLE_MESSAGE)

        # The tool is known, so an action it lacks is unknown, not closed.
        self.assertIs(
            compile_proposal(
                "tidy up my audit records",
                ReasoningResponse('{"tool":"audit","action":"purge","arguments":{}}'),
                BUILT_IN_ACTION_CONTRACTS,
                self.logger,
            ),
            ProposalRejection.UNKNOWN_ACTION,
        )

    def test_audit_stays_closed_even_where_a_registry_calls_it_eligible(
        self,
    ) -> None:
        opened = ActionContractRegistry(
            dataclasses.replace(
                contract,
                model_eligibility=ModelEligibility.MODEL_ELIGIBLE,
            )
            if contract.tool_name == "audit"
            else contract
            for contract in BUILT_IN_ACTION_CONTRACTS.contracts
        )

        for action, arguments in AUDIT_STEPS:
            with self.subTest(action=action):
                proposal = json.dumps(
                    {"tool": "audit", "action": action, "arguments": arguments}
                )
                plan = Plan(
                    "tidy up my audit records",
                    (PlanStep("audit", action, arguments),),
                    recording=RecordingPolicy.TRANSIENT,
                    origin=PlanOrigin.MODEL,
                )

                self.assertIs(
                    compile_proposal(
                        "tidy up my audit records",
                        ReasoningResponse(proposal),
                        opened,
                        self.logger,
                    ),
                    ProposalRejection.NOT_MODEL_ELIGIBLE,
                )
                self.assertIs(
                    recheck_model_plan(plan, opened, self.logger),
                    ProposalRejection.NOT_MODEL_ELIGIBLE,
                )

    def test_no_audit_action_is_itself_audited_or_proposable(self) -> None:
        self.assertEqual(AUDITED_ACTIONS, MODEL_ACTION_ALLOWLIST)
        for identifier in ("audit.list", "audit.read", "audit.delete"):
            with self.subTest(identifier=identifier):
                self.assertNotIn(identifier, AUDITED_ACTIONS)
                self.assertNotIn(identifier, MODEL_ACTION_ALLOWLIST)


# --- Permissions --------------------------------------------------------------


class AuditPermissionTests(unittest.TestCase):
    """Prove deletion is a mandatory confirmation and the rest is allowed."""

    def setUp(self) -> None:
        self.logger = _quiet_logger("tests.audit_tool.permissions")
        self.manager = PermissionManager(frozenset(), frozenset(), logger=self.logger)

    def test_audit_delete_is_mandatory_and_its_reason_carries_the_notice(
        self,
    ) -> None:
        self.assertIn("audit.delete", MANDATORY_CONFIRMATION_ACTIONS)
        self.assertNotIn("audit.delete", MANDATORY_DENIED_ACTIONS)
        self.assertEqual(CONFIRMATION_NOTICES["audit.delete"], DELETE_NOTICE)
        self.assertEqual(
            self.manager.check("audit", "delete"),
            PermissionResult(
                PermissionDecision.REQUIRES_CONFIRMATION,
                "audit.delete requires explicit confirmation. " + DELETE_NOTICE,
            ),
        )

    def test_the_notice_promises_no_undo_and_no_log_erasure(self) -> None:
        # Read from production, not from this module's pinned copies, so the
        # promise and the one-line shape are what Maria actually shows.
        notice = CONFIRMATION_NOTICES["audit.delete"]
        reason = self.manager.check("audit", "delete").reason

        self.assertIn("It does not undo the action", notice)
        self.assertIn("operational log may still show", notice)
        self.assertTrue(reason.endswith(" " + notice))
        for text in (notice, reason):
            with self.subTest(text=text):
                self.assertEqual(text.splitlines(), [text])

    def test_listing_and_reading_are_allowed_without_confirmation(self) -> None:
        for action in ("list", "read"):
            identifier = f"audit.{action}"
            with self.subTest(action=action):
                self.assertEqual(
                    self.manager.check("audit", action),
                    PermissionResult(
                        PermissionDecision.ALLOWED,
                        f"{identifier} is allowed.",
                    ),
                )
                self.assertNotIn(identifier, MANDATORY_CONFIRMATION_ACTIONS)
                self.assertNotIn(identifier, MANDATORY_DENIED_ACTIONS)
                self.assertNotIn(identifier, CONFIRMATION_NOTICES)

    def test_configuration_cannot_remove_the_deletion_confirmation(self) -> None:
        for description, permissions in (
            ("missing permissions section", '[app]\nname = "Maria"\n'),
            ("empty permissions section", "[permissions]\n"),
            (
                "empty permission arrays",
                "[permissions]\nrequire_confirmation = []\ndeny = []\n",
            ),
            (
                "unrelated permissions",
                "[permissions]\n"
                'require_confirmation = ["calendar.create"]\n'
                'deny = ["memory.store"]\n',
            ),
        ):
            with self.subTest(description=description):
                with tempfile.TemporaryDirectory() as temporary:
                    config_path = Path(temporary) / "config.toml"
                    config_path.write_text(
                        permissions + FILESYSTEM_SECTION,
                        encoding="utf-8",
                    )
                    config = load_config(config_path)
                manager = PermissionManager(
                    config.confirmation_required,
                    config.denied_actions,
                    logger=self.logger,
                )

                self.assertIn("audit.delete", config.confirmation_required)
                self.assertEqual(
                    manager.check("audit", "delete"),
                    PermissionResult(
                        PermissionDecision.REQUIRES_CONFIRMATION,
                        "audit.delete requires explicit confirmation. "
                        + DELETE_NOTICE,
                    ),
                )

    def test_configuration_may_deny_deletion_or_hold_the_other_actions(
        self,
    ) -> None:
        manager = PermissionManager(
            frozenset({"audit.list", "audit.read"}),
            frozenset({"audit.delete"}),
            logger=self.logger,
        )

        self.assertEqual(
            manager.check("audit", "delete"),
            PermissionResult(
                PermissionDecision.DENIED,
                "audit.delete is denied by configuration.",
            ),
        )
        for action in ("list", "read"):
            with self.subTest(action=action):
                self.assertEqual(
                    manager.check("audit", action),
                    PermissionResult(
                        PermissionDecision.REQUIRES_CONFIRMATION,
                        f"audit.{action} requires explicit confirmation.",
                    ),
                )


# --- The audit tool -----------------------------------------------------------


class _AuditToolTestCase(unittest.TestCase):
    """One initialized temporary database and one audit tool per test.

    The tool and the memory layer each log to their own mock, so a test can
    prove exactly what either was handed.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database_path = Path(temporary.name) / "database" / "maria.db"
        self.memory_logger = Mock(spec=logging.Logger)
        self.memory = Memory(self.database_path, logger=self.memory_logger)
        self.memory.initialize()
        self.logger = Mock(spec=logging.Logger)
        self.tool = AuditTool(self.memory, logger=self.logger)

    def _logged(self) -> str:
        """Return every call the tool and the memory layer logged, as text."""
        return f"{self.logger.mock_calls!r}\n{self.memory_logger.mock_calls!r}"


class AuditToolTests(_AuditToolTestCase):
    """Prove the tool's identity, its factory, and what history records."""

    def test_identity_actions_and_factory(self) -> None:
        self.assertEqual(self.tool.name, "audit")
        self.assertEqual(self.tool.actions, frozenset({"list", "read", "delete"}))
        self.assertEqual(
            self.tool.description,
            "Lists, reads, and deletes audit summaries of approved "
            "model-proposed actions.",
        )
        self.assertIn("tools.audit", BUILT_IN_TOOL_MODULES)

        built = build_tool(
            config=None,
            memory=self.memory,
            logger=Mock(spec=logging.Logger),
        )

        self.assertIsInstance(built, AuditTool)
        self.assertIs(built.memory, self.memory)

    def test_an_unsupported_action_is_refused(self) -> None:
        self.assertEqual(
            self.tool.execute("purge", {}),
            ToolExecutionResult(False, "Unsupported audit action."),
        )

    def test_only_successful_listing_and_reading_replace_what_is_recorded(
        self,
    ) -> None:
        self.assertEqual(self.tool.execute("list", {}).recorded_message, LISTED_RECORD)
        _seed_trio(self.memory)

        self.assertEqual(self.tool.execute("list", {}).recorded_message, LISTED_RECORD)
        for summary_id in (1, 2, 3):
            with self.subTest(summary_id=summary_id):
                self.assertEqual(
                    self.tool.execute("read", {"id": summary_id}).recorded_message,
                    f"Displayed audit summary {summary_id}.",
                )
        for action, arguments in (
            ("read", {"id": 99}),
            ("read", {"id": 0}),
            ("delete", {"id": 99}),
            ("delete", {"id": True}),
            ("delete", {"id": 1}),
            ("purge", {}),
        ):
            with self.subTest(action=action, arguments=arguments):
                self.assertIsNone(
                    self.tool.execute(action, arguments).recorded_message
                )


class AuditToolListTests(_AuditToolTestCase):
    """Prove the listing: newest first, at most twenty, in fixed words."""

    def test_an_empty_listing_is_a_successful_fixed_reply(self) -> None:
        self.assertEqual(
            self.tool.execute("list", {}),
            ToolExecutionResult(
                True,
                NO_SUMMARIES_MESSAGE,
                [],
                recorded_message=LISTED_RECORD,
            ),
        )

    def test_one_summary_is_one_line_of_audit_vocabulary(self) -> None:
        _seed(
            self.memory,
            "calculator.calculate",
            AuditOutcome.SUCCEEDED,
            created_at=FIRST_TIMESTAMP,
        )

        self.assertEqual(
            self.tool.execute("list", {}),
            ToolExecutionResult(
                True,
                LINE_ONE,
                [
                    AuditSummary(
                        1,
                        FIRST_TIMESTAMP,
                        "calculator.calculate",
                        AuditDecision.OWNER_APPROVED,
                        AuditTarget.NONE,
                        AuditOutcome.SUCCEEDED,
                    )
                ],
                recorded_message=LISTED_RECORD,
            ),
        )

    def test_every_outcome_and_target_is_listed_by_id_newest_first(self) -> None:
        _seed_four(self.memory)

        result = self.tool.execute("list", {})

        self.assertTrue(result.success)
        self.assertEqual(
            result.message,
            "\n".join((LINE_FOUR, LINE_THREE, LINE_TWO, LINE_ONE)),
        )
        self.assertEqual([summary.summary_id for summary in result.data], [4, 3, 2, 1])
        self.assertEqual(result.recorded_message, LISTED_RECORD)
        logged = self._logged()
        for text in SUMMARY_FIELD_TEXTS:
            with self.subTest(text=text):
                self.assertNotIn(text, logged)

    def test_at_most_twenty_are_shown_and_more_is_said_only_past_twenty(
        self,
    ) -> None:
        seeded = 0
        with patch.object(
            self.memory,
            "list_audit_summaries",
            wraps=self.memory.list_audit_summaries,
        ) as listed:
            for count in (1, 20, 21, 25):
                while seeded < count:
                    seeded += 1
                    _seed(
                        self.memory,
                        "system.current_time",
                        created_at=_bulk_timestamp(seeded),
                    )
                with self.subTest(count=count):
                    result = self.tool.execute("list", {})

                    newest = list(range(count, max(count - 20, 0), -1))
                    lines = [_bulk_line(summary_id) for summary_id in newest]
                    if count > 20:
                        lines.append(MORE_SUMMARIES_LINE)
                    self.assertTrue(result.success)
                    self.assertEqual(result.message, "\n".join(lines))
                    self.assertEqual(
                        [summary.summary_id for summary in result.data],
                        newest,
                    )
                    self.assertEqual(result.recorded_message, LISTED_RECORD)
                    self.assertEqual(
                        result.message.count(MORE_SUMMARIES_LINE),
                        1 if count > 20 else 0,
                    )

        self.assertEqual(listed.call_args_list, [call(21)] * 4)
        self.assertNotIn("2025-01-01T", self._logged())


class AuditToolReadTests(_AuditToolTestCase):
    """Prove reading one summary shows a fixed block and records its id."""

    def test_each_outcome_and_target_reads_as_an_exact_block(self) -> None:
        _seed_four(self.memory)

        for summary_id, block in (
            (1, BLOCK_ONE),
            (2, BLOCK_TWO),
            (3, BLOCK_THREE),
            (4, BLOCK_FOUR),
        ):
            with self.subTest(summary_id=summary_id):
                result = self.tool.execute("read", {"id": summary_id})

                self.assertTrue(result.success)
                self.assertEqual(result.message, block)
                self.assertEqual(
                    result.recorded_message,
                    f"Displayed audit summary {summary_id}.",
                )
                self.assertIs(type(result.data), AuditSummary)
                self.assertEqual(result.data.summary_id, summary_id)
        logged = self._logged()
        for text in SUMMARY_FIELD_TEXTS:
            with self.subTest(text=text):
                self.assertNotIn(text, logged)

    def test_a_missing_summary_is_not_found(self) -> None:
        _seed_trio(self.memory)

        self.assertEqual(
            self.tool.execute("read", {"id": 99}),
            ToolExecutionResult(False, "Audit summary not found: 99."),
        )


class AuditToolDeleteTests(_AuditToolTestCase):
    """Prove deletion removes one summary with its outcome, by id."""

    def test_delete_removes_one_summary_and_its_outcome_and_frees_no_id(
        self,
    ) -> None:
        _seed_trio(self.memory)

        result = self.tool.execute("delete", {"id": 2})

        self.assertEqual(
            result,
            ToolExecutionResult(True, "Deleted audit summary 2.", {"id": 2}),
        )
        self.assertIsNone(result.recorded_message)
        self.assertEqual(
            _audit_rows(self.database_path),
            [
                (
                    1,
                    FIRST_TIMESTAMP,
                    "calculator.calculate",
                    "owner_approved",
                    "none",
                    "succeeded",
                ),
                (
                    3,
                    THIRD_TIMESTAMP,
                    "system.current_time",
                    "owner_approved",
                    "none",
                    None,
                ),
            ],
        )
        self.assertEqual(_outcome_rows(self.database_path), [(1, "succeeded")])
        for action in ("read", "delete"):
            with self.subTest(action=action):
                self.assertEqual(
                    self.tool.execute(action, {"id": 2}),
                    ToolExecutionResult(False, "Audit summary not found: 2."),
                )

        self.tool.execute("delete", {"id": 3})
        self.assertEqual(
            _seed(self.memory, "system.current_time", created_at=FOURTH_TIMESTAMP),
            4,
        )

    def test_deleting_a_missing_summary_changes_nothing(self) -> None:
        _seed_trio(self.memory)
        # An outcome row whose summary does not exist. Deleting that id must
        # leave it alone: an outcome goes only with a summary that was deleted.
        _raw_execute(
            self.database_path,
            "INSERT INTO audit_outcomes (summary_id, outcome) VALUES (?, ?)",
            (99, "failed"),
        )
        before = _audit_rows(self.database_path)
        outcomes = [(1, "succeeded"), (2, "failed"), (99, "failed")]
        self.assertEqual(_outcome_rows(self.database_path), outcomes)

        self.assertEqual(
            self.tool.execute("delete", {"id": 99}),
            ToolExecutionResult(False, "Audit summary not found: 99."),
        )
        self.assertEqual(_audit_rows(self.database_path), before)
        self.assertEqual(_outcome_rows(self.database_path), outcomes)


class AuditToolFailureTests(_AuditToolTestCase):
    """Prove every refusal and failure is fixed text that repeats nothing."""

    def test_an_id_that_is_not_an_exact_positive_int_is_refused_unread(
        self,
    ) -> None:
        _seed_trio(self.memory)
        before = _audit_rows(self.database_path)

        with (
            patch.object(
                self.memory,
                "get_audit_summary",
                wraps=self.memory.get_audit_summary,
            ) as read,
            patch.object(
                self.memory,
                "delete_audit_summary",
                wraps=self.memory.delete_audit_summary,
            ) as delete,
        ):
            for action in ("read", "delete"):
                for arguments in (
                    {"id": True},
                    {"id": False},
                    {"id": 0},
                    {"id": -1},
                    {"id": "1"},
                    {"id": 1.0},
                    {"id": None},
                    {},
                ):
                    with self.subTest(action=action, arguments=arguments):
                        self.assertEqual(
                            self.tool.execute(action, arguments),
                            ToolExecutionResult(False, ID_MESSAGE),
                        )

        read.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(_audit_rows(self.database_path), before)

    def test_a_memory_failure_becomes_a_fixed_message_without_its_text(
        self,
    ) -> None:
        _seed_trio(self.memory)
        cases = (
            ("list_audit_summaries", "list", {}, LIST_FAILED_MESSAGE),
            ("get_audit_summary", "read", {"id": 1}, READ_FAILED_MESSAGE),
            ("delete_audit_summary", "delete", {"id": 1}, DELETE_FAILED_MESSAGE),
        )

        for method, action, arguments, message in cases:
            for error_type in (RuntimeError, ValueError):
                with self.subTest(action=action, error=error_type.__name__):
                    with patch.object(
                        self.memory,
                        method,
                        side_effect=error_type(FAILURE_SENTINEL),
                    ):
                        result = self.tool.execute(action, arguments)

                    self.assertEqual(result, ToolExecutionResult(False, message))

        self.assertEqual(self.logger.warning.call_count, 2 * len(cases))
        self.assertNotIn(FAILURE_SENTINEL, self._logged())
        self.assertEqual(len(_audit_rows(self.database_path)), 3)

    def test_an_answer_that_is_not_a_summary_is_a_fixed_failure(self) -> None:
        summary = AuditSummary(
            1,
            FIRST_TIMESTAMP,
            "calculator.calculate",
            AuditDecision.OWNER_APPROVED,
            AuditTarget.NONE,
        )

        for returned in (None, (summary,), [summary, {"id": 2}], [{"id": 1}]):
            with self.subTest(method="list", returned=returned):
                with patch.object(
                    self.memory,
                    "list_audit_summaries",
                    return_value=returned,
                ):
                    self.assertEqual(
                        self.tool.execute("list", {}),
                        ToolExecutionResult(False, LIST_FAILED_MESSAGE),
                    )
        for returned in ({"id": 1}, (summary,), "summary"):
            with self.subTest(method="read", returned=returned):
                with patch.object(
                    self.memory,
                    "get_audit_summary",
                    return_value=returned,
                ):
                    self.assertEqual(
                        self.tool.execute("read", {"id": 1}),
                        ToolExecutionResult(False, READ_FAILED_MESSAGE),
                    )

    def test_a_busy_database_gives_the_same_fixed_failures(self) -> None:
        _seed_trio(self.memory)
        holder = sqlite3.connect(self.database_path, timeout=0)
        try:
            holder.execute("BEGIN EXCLUSIVE")
            with patch("core.memory.DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS", 0.01):
                results = [
                    self.tool.execute("list", {}),
                    self.tool.execute("read", {"id": 1}),
                    self.tool.execute("delete", {"id": 1}),
                ]
        finally:
            holder.rollback()
            holder.close()

        self.assertEqual(
            results,
            [
                ToolExecutionResult(False, LIST_FAILED_MESSAGE),
                ToolExecutionResult(False, READ_FAILED_MESSAGE),
                ToolExecutionResult(False, DELETE_FAILED_MESSAGE),
            ],
        )
        self.assertEqual(self.tool.execute("read", {"id": 1}).message, BLOCK_ONE)
        self.assertEqual(len(_audit_rows(self.database_path)), 3)

    def test_a_stored_value_outside_the_vocabulary_fails_without_repeating_it(
        self,
    ) -> None:
        _seed_trio(self.memory)
        before = _audit_rows(self.database_path)

        for table, key, column, value in TAMPERED_VALUES:
            with self.subTest(column=column, value=value):
                statement = f"UPDATE {table} SET {column} = ? WHERE {key} = 2"
                _raw_execute(self.database_path, statement, (value,))
                try:
                    listed = self.tool.execute("list", {})
                    read = self.tool.execute("read", {"id": 2})
                    intact = self.tool.execute("read", {"id": 1})
                finally:
                    _raw_execute(
                        self.database_path,
                        statement,
                        (SECOND_STORED[column],),
                    )

                self.assertEqual(
                    listed,
                    ToolExecutionResult(False, LIST_FAILED_MESSAGE),
                )
                self.assertEqual(read, ToolExecutionResult(False, READ_FAILED_MESSAGE))
                self.assertEqual(intact.message, BLOCK_ONE)

        self.assertEqual(_audit_rows(self.database_path), before)
        # Each refused read logs exactly its one fixed line, and the intact
        # read logs no error at all.
        self.assertEqual(
            [
                logged.args[0] % logged.args[1:]
                for logged in self.memory_logger.error.call_args_list
            ],
            [
                "Failed to list audit summaries: a stored value is outside "
                "the audit vocabulary.",
                "Failed to read audit summary: a stored value is outside the "
                "audit vocabulary.",
            ]
            * len(TAMPERED_VALUES),
        )
        self.assertNotIn(TAMPER_SENTINEL, self._logged())

    def test_a_summary_the_listing_refuses_can_still_be_deleted_by_id(
        self,
    ) -> None:
        _seed_trio(self.memory)
        _raw_execute(
            self.database_path,
            "UPDATE audit_summaries SET action = ? WHERE id = 2",
            (TAMPER_SENTINEL,),
        )

        refused = self.tool.execute("list", {})
        deleted = self.tool.execute("delete", {"id": 2})

        self.assertEqual(refused, ToolExecutionResult(False, LIST_FAILED_MESSAGE))
        self.assertEqual(
            deleted,
            ToolExecutionResult(True, "Deleted audit summary 2.", {"id": 2}),
        )
        self.assertEqual(
            self.tool.execute("list", {}).message,
            f"{LINE_THREE}\n{LINE_ONE}",
        )
        self.assertEqual(_outcome_rows(self.database_path), [(1, "succeeded")])
        self.assertNotIn(TAMPER_SENTINEL, self._logged())


# --- Through a session, reasoning disabled --------------------------------------


class AuditSessionTests(unittest.TestCase):
    """Prove the three commands through a real session with reasoning off."""

    def setUp(self) -> None:
        self.harness = _AuditHarness(self)
        _seed_trio(self.harness.seeder)
        self.session = self.harness.session

    def test_audit_lists_newest_first_and_records_only_a_fixed_line(self) -> None:
        response = self.session.submit("audit")

        self.assertEqual(response.text, TRIO_LISTING)
        self.assertFalse(response.needs_confirmation)
        self.assertIsNone(self.session.pending_approval)
        self.assertEqual(self.harness.interactions(), [("audit", LISTED_RECORD)])
        self.assertEqual(
            self.harness.row_counts(),
            {**SEEDED_COUNTS, "interactions": 1},
        )
        self.harness.create_plan.assert_called_once_with("audit", timing=ANY)
        # Brain validates the plan, then dispatch validates the step again.
        self.assertEqual(
            self.harness.validate_action.call_args_list,
            [call("audit", "list", {})] * 2,
        )
        self.harness.check.assert_called_once_with("audit", "list")
        self.harness.execute_tool.assert_called_once_with("audit", "list", {})
        self.harness.list_audit_summaries.assert_called_once_with(21)
        self.harness.prepare_confirmation.assert_not_called()
        self.harness.record_audit_summary.assert_not_called()
        self.harness.record_audit_outcome.assert_not_called()

    def test_the_command_is_case_insensitive_and_recorded_as_typed(self) -> None:
        for command in ("AUDIT", "  Audit  "):
            with self.subTest(command=command):
                self.assertEqual(self.session.submit(command).text, TRIO_LISTING)

        self.assertEqual(
            self.harness.interactions(),
            [("AUDIT", LISTED_RECORD), ("Audit", LISTED_RECORD)],
        )

    def test_an_empty_listing_is_recorded_the_same_way(self) -> None:
        harness = _AuditHarness(self)

        response = harness.session.submit("audit")

        self.assertEqual(response.text, NO_SUMMARIES_MESSAGE)
        self.assertEqual(harness.interactions(), [("audit", LISTED_RECORD)])
        self.assertEqual(harness.row_counts(), {**ZERO_COUNTS, "interactions": 1})

    def test_read_audit_shows_one_block_and_records_only_its_id(self) -> None:
        before = self.harness.audit_rows()

        for summary_id, block in ((1, BLOCK_ONE), (2, BLOCK_TWO), (3, BLOCK_THREE)):
            with self.subTest(summary_id=summary_id):
                response = self.session.submit(f"read audit {summary_id}")

                self.assertEqual(response.text, block)
                self.assertFalse(response.needs_confirmation)

        self.assertEqual(
            self.harness.interactions(),
            [
                (f"read audit {summary_id}", f"Displayed audit summary {summary_id}.")
                for summary_id in (1, 2, 3)
            ],
        )
        self.assertEqual(
            self.harness.execute_tool.call_args_list,
            [call("audit", "read", {"id": summary_id}) for summary_id in (1, 2, 3)],
        )
        self.harness.prepare_confirmation.assert_not_called()
        self.assertEqual(self.harness.audit_rows(), before)
        self.assertEqual(
            self.harness.row_counts(),
            {**SEEDED_COUNTS, "interactions": 3},
        )

    def test_reading_a_missing_summary_says_so_and_records_that(self) -> None:
        response = self.session.submit("read audit 99")

        self.assertEqual(response.text, "Audit summary not found: 99.")
        self.assertEqual(
            self.harness.interactions(),
            [("read audit 99", "Audit summary not found: 99.")],
        )

    def test_summary_fields_never_reach_interaction_history(self) -> None:
        for command in (
            "audit",
            "read audit 1",
            "read audit 2",
            "read audit 3",
            "READ AUDIT 2",
        ):
            self.session.submit(command)
        self.session.submit("delete audit 1")
        self.session.confirm()

        self.assertEqual(
            self.harness.interactions(),
            [
                ("audit", LISTED_RECORD),
                ("read audit 1", "Displayed audit summary 1."),
                ("read audit 2", "Displayed audit summary 2."),
                ("read audit 3", "Displayed audit summary 3."),
                ("READ AUDIT 2", "Displayed audit summary 2."),
                ("delete audit 1", "Deleted audit summary 1."),
            ],
        )
        stored = repr(_raw_interactions(self.harness.database_path))
        for text in SUMMARY_FIELD_TEXTS:
            with self.subTest(text=text):
                self.assertNotIn(text, stored)

    def test_usage_errors_and_bad_ids_are_answered_without_any_tool(self) -> None:
        cases = (
            ("read audit", "Use: read audit <id>"),
            ("delete audit", "Use: delete audit <id>"),
            ("read audit abc", ID_MESSAGE),
            ("delete audit 0", ID_MESSAGE),
            ("delete audit -1", ID_MESSAGE),
            ("read audit 1.5", ID_MESSAGE),
            (f"read audit {chr(0x0663)}", ID_MESSAGE),
        )
        before = self.harness.audit_rows()

        for command, expected in cases:
            with self.subTest(command=ascii(command)):
                response = self.session.submit(command)

                self.assertEqual(response.text, expected)
                self.assertFalse(response.needs_confirmation)

        self.assertEqual(self.harness.interactions(), list(cases))
        for spy in (
            self.harness.validate_action,
            self.harness.check,
            self.harness.prepare_confirmation,
            self.harness.execute_tool,
        ):
            spy.assert_not_called()
        self.assertEqual(self.harness.audit_rows(), before)

    def test_delete_audit_asks_for_confirmation_with_the_exact_notice(self) -> None:
        response = self.session.submit("delete audit 2")

        plan = Plan("delete audit 2", (PlanStep("audit", "delete", {"id": 2}),))
        self.assertTrue(response.needs_confirmation)
        self.assertEqual(response.text, DELETE_CONFIRMATION)
        self.assertEqual(response.plan, plan)
        self.assertEqual(self.session.pending_approval.plan, plan)
        self.harness.check.assert_called_once_with("audit", "delete")
        self.harness.prepare_confirmation.assert_called_once()
        self.harness.execute_tool.assert_not_called()
        self.harness.delete_audit_summary.assert_not_called()
        self.assertEqual(self.harness.row_counts(), SEEDED_COUNTS)
        self.assertEqual(self.harness.interactions(), [])

    def test_the_request_is_identical_for_a_summary_that_does_not_exist(
        self,
    ) -> None:
        existing = self.session.submit("delete audit 2")
        missing = self.session.submit("delete audit 99")

        self.assertTrue(missing.needs_confirmation)
        self.assertEqual(missing.text, DELETE_CONFIRMATION)
        self.assertEqual(missing.text, existing.text)
        self.assertEqual(
            self.session.pending_approval.plan,
            Plan("delete audit 99", (PlanStep("audit", "delete", {"id": 99}),)),
        )
        for spy in (
            self.harness.list_audit_summaries,
            self.harness.get_audit_summary,
            self.harness.delete_audit_summary,
            self.harness.execute_tool,
        ):
            spy.assert_not_called()

    def test_confirming_deletes_the_summary_with_its_outcome_and_records_it(
        self,
    ) -> None:
        self.session.submit("delete audit 2")

        response = self.session.confirm()

        self.assertEqual(response.text, "Deleted audit summary 2.")
        self.assertFalse(response.needs_confirmation)
        self.assertIsNone(self.session.pending_approval)
        self.assertEqual(
            self.harness.interactions(),
            [("delete audit 2", "Deleted audit summary 2.")],
        )
        self.assertEqual([row[0] for row in self.harness.audit_rows()], [1, 3])
        self.assertEqual(_outcome_rows(self.harness.database_path), [(1, "succeeded")])
        self.assertEqual(
            self.harness.row_counts(),
            {
                **ZERO_COUNTS,
                "interactions": 1,
                "audit_summaries": 2,
                "audit_outcomes": 1,
            },
        )
        self.harness.create_plan.assert_called_once_with("delete audit 2", timing=ANY)
        self.harness.execute_tool.assert_called_once_with("audit", "delete", {"id": 2})
        self.harness.delete_audit_summary.assert_called_once_with(2)
        self.harness.prepare_confirmation.assert_called_once()
        self.harness.record_audit_summary.assert_not_called()
        self.harness.record_audit_outcome.assert_not_called()
        self.assertEqual(
            self.session.submit("read audit 2").text,
            "Audit summary not found: 2.",
        )

    def test_confirming_a_missing_id_says_not_found_and_changes_nothing(
        self,
    ) -> None:
        self.session.submit("delete audit 99")
        before = self.harness.audit_rows()

        response = self.session.confirm()

        self.assertEqual(response.text, "Audit summary not found: 99.")
        self.assertEqual(self.harness.audit_rows(), before)
        self.assertEqual(
            self.harness.interactions(),
            [("delete audit 99", "Audit summary not found: 99.")],
        )
        self.harness.delete_audit_summary.assert_called_once_with(99)

    def test_the_largest_id_sqlite_can_store_is_simply_not_found(self) -> None:
        # The id rule's ceiling is SQLite's largest integer, so this id still
        # reaches the tool; no row carries it, so it is answered as absent
        # rather than as a failure.
        largest = str(LARGEST_ID)
        before = self.harness.audit_rows()

        read = self.session.submit(f"read audit {largest}")
        offered = self.session.submit(f"delete audit {largest}")
        deleted = self.session.confirm()

        self.assertEqual(read.text, f"Audit summary not found: {largest}.")
        self.assertTrue(offered.needs_confirmation)
        self.assertEqual(deleted.text, f"Audit summary not found: {largest}.")
        self.assertEqual(self.harness.audit_rows(), before)
        self.assertEqual(
            self.harness.interactions(),
            [
                (f"read audit {largest}", f"Audit summary not found: {largest}."),
                (f"delete audit {largest}", f"Audit summary not found: {largest}."),
            ],
        )
        self.assertEqual(
            self.harness.execute_tool.call_args_list,
            [
                call("audit", "read", {"id": LARGEST_ID}),
                call("audit", "delete", {"id": LARGEST_ID}),
            ],
        )

    def test_cancelling_changes_nothing_and_records_nothing(self) -> None:
        self.session.submit("delete audit 1")
        before = self.harness.audit_rows()

        self.assertTrue(self.session.cancel())

        self.assertIsNone(self.session.pending_approval)
        self.assertEqual(self.session.confirm().text, CONFIRMATION_REFUSED_MESSAGE)
        self.assertFalse(self.session.cancel())
        self.assertEqual(self.harness.audit_rows(), before)
        self.assertEqual(self.harness.row_counts(), SEEDED_COUNTS)
        self.assertEqual(self.harness.interactions(), [])
        self.harness.execute_tool.assert_not_called()
        self.harness.delete_audit_summary.assert_not_called()

    def test_a_new_request_discards_a_pending_deletion(self) -> None:
        self.session.submit("delete audit 1")

        self.assertEqual(self.session.submit("audit").text, TRIO_LISTING)
        self.assertEqual(self.session.confirm().text, CONFIRMATION_REFUSED_MESSAGE)
        self.assertEqual(len(self.harness.audit_rows()), 3)
        self.harness.delete_audit_summary.assert_not_called()


class AuditPolicyConfigurationTests(unittest.TestCase):
    """Prove configuration can tighten the audit commands but never loosen them."""

    def test_an_empty_policy_still_requires_confirmation_to_delete(self) -> None:
        harness = _AuditHarness(self)
        _seed_trio(harness.seeder)

        self.assertEqual(harness.config.confirmation_required, frozenset())
        self.assertEqual(harness.config.denied_actions, frozenset())
        self.assertEqual(
            harness.session.submit("delete audit 1").text,
            DELETE_CONFIRMATION,
        )

    def test_a_configured_denial_refuses_deletion_before_any_confirmation(
        self,
    ) -> None:
        harness = _AuditHarness(self, denied_actions=frozenset({"audit.delete"}))
        _seed_trio(harness.seeder)
        denial = "Permission denied: audit.delete is denied by configuration."

        response = harness.session.submit("delete audit 1")

        self.assertEqual(response.text, denial)
        self.assertFalse(response.needs_confirmation)
        self.assertIsNone(harness.session.pending_approval)
        harness.prepare_confirmation.assert_not_called()
        harness.execute_tool.assert_not_called()
        harness.delete_audit_summary.assert_not_called()
        self.assertEqual(harness.interactions(), [("delete audit 1", denial)])
        self.assertEqual(len(harness.audit_rows()), 3)

    def test_configuration_may_hold_listing_and_reading_for_confirmation(
        self,
    ) -> None:
        harness = _AuditHarness(
            self,
            confirmation_required=frozenset({"audit.list", "audit.read"}),
        )
        _seed_trio(harness.seeder)

        listing = harness.session.submit("audit")
        listed = harness.session.confirm()
        reading = harness.session.submit("read audit 1")
        read = harness.session.confirm()

        self.assertEqual(
            listing.text,
            "Confirmation required: audit.list requires explicit confirmation.",
        )
        self.assertEqual(listed.text, TRIO_LISTING)
        self.assertEqual(
            reading.text,
            "Confirmation required: audit.read requires explicit confirmation.",
        )
        self.assertEqual(read.text, BLOCK_ONE)
        self.assertEqual(
            harness.interactions(),
            [("audit", LISTED_RECORD), ("read audit 1", "Displayed audit summary 1.")],
        )


# --- Through a session, reasoning enabled ---------------------------------------


class AuditWithReasoningTests(unittest.TestCase):
    """Prove the audit surface stays deterministic with a local model enabled."""

    def setUp(self) -> None:
        self.provider = ScriptedProvider(lambda request: REPLY)
        self.harness = _AuditHarness(self, self.provider)
        _seed_trio(self.harness.seeder)
        self.session = self.harness.session

    def test_audit_commands_never_reach_the_model_or_read_any_context(
        self,
    ) -> None:
        listed_memories = _spy(self, self.harness.memory, "list_context_memories")
        searched_memories = _spy(self, self.harness.memory, "search_context_memories")
        searched_notes = _spy(self, self.harness.memory, "search_context_notes")
        context_reads = (listed_memories, searched_memories, searched_notes)
        # Each command and the deterministic answer it gets with reasoning
        # off; enabling a model must change none of them.
        answers = (
            ("audit", TRIO_LISTING),
            ("AUDIT", TRIO_LISTING),
            ("read audit 1", BLOCK_ONE),
            ("READ AUDIT 2", BLOCK_TWO),
            ("read audit 99", "Audit summary not found: 99."),
            ("read audit", "Use: read audit <id>"),
            ("delete audit", "Use: delete audit <id>"),
            ("read audit abc", ID_MESSAGE),
            ("delete audit 0", ID_MESSAGE),
            (f"delete audit {chr(0x0663)}", ID_MESSAGE),
            ("list audits", "Not a directory: audits"),
        )
        commands = tuple(command for command, _answer in answers)

        for command, answer in answers:
            with self.subTest(command=ascii(command)):
                response = self.session.submit(command)

                self.assertEqual(response.text, answer)
                self.assertFalse(response.needs_confirmation)
                self.assertIsNone(response.conversation)
        deleting = self.session.submit("delete audit 3")
        deleted = self.session.confirm()
        cancelling = self.session.submit("delete audit 2")
        self.assertTrue(self.session.cancel())

        self.assertEqual(deleting.text, DELETE_CONFIRMATION)
        self.assertEqual(deleted.text, "Deleted audit summary 3.")
        self.assertEqual(cancelling.text, DELETE_CONFIRMATION)

        self.assertEqual(self.provider.calls, 0)
        for spy in context_reads:
            spy.assert_not_called()
        self.assertEqual(self.session.conversation, ())
        self.assertEqual(
            [request for request, _response in self.harness.interactions()],
            [*commands, "delete audit 3"],
        )
        self.assertEqual([row[0] for row in self.harness.audit_rows()], [1, 2])

        # The near misses are the generic fallback, so they do reach the model.
        for command in ("audits", "audit 5"):
            with self.subTest(command=command):
                self.assertEqual(
                    self.session.submit(command).text,
                    REPLY + NO_CONTEXT_SUFFIX,
                )
        self.assertEqual(
            [request.prompt for request in self.provider.requests],
            ["audits", "audit 5"],
        )
        # Positive control: the spies above were live, since a model-backed
        # turn always reads the enabled memories.
        self.assertEqual(listed_memories.call_count, 2)

    def test_an_id_no_summary_can_have_is_answered_before_any_step(self) -> None:
        # Past SQLite's largest integer no summary can exist, and past 4,300
        # digits Python will not convert the text whole. Either way the
        # planner answers with the fixed id message, so no step, approval,
        # model call, or audit change can follow.
        operands = (
            str(LARGEST_ID + 1),
            "9" * 20,
            "9" * len(LONG_ZEROES),
            "1" + LONG_ZEROES,
            LONG_ZEROES + "9" * 20,
            LONG_ZEROES,
        )
        commands = [
            f"{verb} audit {operand}"
            for verb in ("read", "delete")
            for operand in operands
        ]
        before = self.harness.audit_rows()

        for command in commands:
            with self.subTest(command=command[:40], length=len(command)):
                response = self.session.submit(command)

                self.assertEqual(response.text, ID_MESSAGE)
                self.assertFalse(response.needs_confirmation)
                self.assertIsNone(self.session.pending_approval)
        refused = self.session.confirm()

        self.assertEqual(refused.text, CONFIRMATION_REFUSED_MESSAGE)
        self.assertEqual(self.provider.calls, 0)
        for spy in (
            self.harness.validate_action,
            self.harness.check,
            self.harness.prepare_confirmation,
            self.harness.execute_tool,
            self.harness.get_audit_summary,
            self.harness.delete_audit_summary,
            self.harness.record_audit_summary,
            self.harness.record_audit_outcome,
        ):
            spy.assert_not_called()
        self.assertEqual(self.harness.audit_rows(), before)
        self.assertEqual(
            self.harness.interactions(),
            [(command, ID_MESSAGE) for command in commands],
        )

    def test_leading_zeroes_of_any_length_still_name_a_small_summary(
        self,
    ) -> None:
        operand = LONG_ZEROES + "2"

        read = self.session.submit(f"read audit {operand}")
        offered = self.session.submit(f"delete audit {operand}")
        pending = self.session.pending_approval
        deleted = self.session.confirm()

        self.assertEqual(read.text, BLOCK_TWO)
        self.assertEqual(offered.text, DELETE_CONFIRMATION)
        self.assertEqual(
            pending.plan,
            Plan(f"delete audit {operand}", (PlanStep("audit", "delete", {"id": 2}),)),
        )
        self.assertEqual(deleted.text, "Deleted audit summary 2.")
        self.assertEqual([row[0] for row in self.harness.audit_rows()], [1, 3])
        self.assertEqual(
            self.harness.execute_tool.call_args_list,
            [call("audit", "read", {"id": 2}), call("audit", "delete", {"id": 2})],
        )
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(
            self.harness.interactions(),
            [
                (f"read audit {operand}", "Displayed audit summary 2."),
                (f"delete audit {operand}", "Deleted audit summary 2."),
            ],
        )

    def test_summary_fields_never_reach_a_reasoning_request(self) -> None:
        seeder = self.harness.seeder
        memory_id, _ = seeder.store_memory("My favourite calculator is a slide rule.")
        seeder.enable_memory_context(memory_id, seeder.memory_fact_digest(memory_id))
        note_id = seeder.create_note("Desk", "The calculator manual is in the drawer.")
        seeder.enable_note_context(note_id, seeder.note_text_digest(note_id))
        first = "Did the calculator succeed when the owner approved it?"
        second = "Tell me more about that calculator."

        self.session.submit(first)
        for command in ("audit", "read audit 1", "read audit 2", "read audit 3"):
            self.session.submit(command)
        self.session.submit(second)

        self.assertEqual(self.provider.calls, 2)
        for request in self.provider.requests:
            with self.subTest(prompt=request.prompt):
                self.assertEqual(
                    [(item.source, item.record_id) for item in request.context],
                    [(ContextSource.MEMORY, memory_id), (ContextSource.NOTE, note_id)],
                )
        self.assertEqual(self.provider.requests[0].history, ())
        self.assertEqual(
            self.provider.requests[1].history,
            (ReasoningExchange(first, REPLY),),
        )
        self.assertEqual(
            self.session.conversation,
            (ReasoningExchange(first, REPLY), ReasoningExchange(second, REPLY)),
        )
        sent = _everything_sent(self.provider.requests)
        for text in SUMMARY_FIELD_TEXTS:
            with self.subTest(text=text):
                self.assertNotIn(text, sent)
        self.harness.list_audit_summaries.assert_called_once_with(21)
        self.assertEqual(
            self.harness.get_audit_summary.call_args_list,
            [call(1), call(2), call(3)],
        )

    def test_audit_summaries_are_never_automatic_model_context(self) -> None:
        response = self.session.submit(
            "Did the calculator succeed when the owner approved it?"
        )

        self.assertEqual(response.text, REPLY + NO_CONTEXT_SUFFIX)
        (request,) = self.provider.requests
        self.assertEqual(request.context, ())
        self.assertEqual(request.history, ())
        # V1-20's "summary" source is the owner's conversation summaries,
        # read only from their own table, and V1-22's "task" and "reminder"
        # sources read only theirs; no audit summary is ever a source, which
        # the two audit reads below never being called still proves.
        self.assertEqual(
            {source.value for source in ContextSource},
            {"memory", "note", "summary", "task", "reminder"},
        )
        self.harness.list_audit_summaries.assert_not_called()
        self.harness.get_audit_summary.assert_not_called()
        self.assertEqual(self.harness.row_counts(), SEEDED_COUNTS)

    def test_a_model_proposal_naming_an_audit_action_is_refused_unrun(
        self,
    ) -> None:
        proposals = (
            '{"tool":"audit","action":"list","arguments":{}}',
            '{"tool":"audit","action":"read","arguments":{"id":1}}',
            '{"tool":"audit","action":"delete","arguments":{"id":1}}',
            ' \n\t{"tool": "audit", "action": "delete", "arguments": {"id": 2}}',
        )
        provider = ScriptedProvider.replying(*proposals)
        harness = _AuditHarness(self, provider)
        _seed_trio(harness.seeder)
        before = harness.audit_rows()

        for proposal in proposals:
            with self.subTest(proposal=proposal):
                response = harness.session.submit("please tidy up my audit records")

                self.assertEqual(
                    response.text,
                    NOT_MODEL_ELIGIBLE_MESSAGE + NO_CONTEXT_SUFFIX,
                )
                self.assertFalse(response.needs_confirmation)
                self.assertIsNone(harness.session.pending_approval)

        self.assertEqual(provider.calls, len(proposals))
        for spy in (
            harness.validate_action,
            harness.check,
            harness.prepare_confirmation,
            harness.execute_tool,
            harness.record_audit_summary,
            harness.record_audit_outcome,
            harness.list_audit_summaries,
            harness.get_audit_summary,
            harness.delete_audit_summary,
        ):
            spy.assert_not_called()
        self.assertEqual(harness.audit_rows(), before)
        self.assertEqual(harness.row_counts(), SEEDED_COUNTS)
        self.assertEqual(harness.session.conversation, ())

    def test_a_forged_model_plan_for_an_audit_action_is_refused_when_confirmed(
        self,
    ) -> None:
        before = self.harness.audit_rows()

        for action, arguments in AUDIT_STEPS:
            plan = Plan(
                "clear my audit trail",
                (PlanStep("audit", action, arguments),),
                recording=RecordingPolicy.TRANSIENT,
                origin=PlanOrigin.MODEL,
            )
            with self.subTest(action=action):
                self.assertEqual(
                    self.harness.brain.execute_confirmed_plan(plan),
                    BrainResponse(NOT_MODEL_ELIGIBLE_MESSAGE, plan=plan),
                )

        for spy in (
            self.harness.check,
            self.harness.execute_tool,
            self.harness.record_audit_summary,
            self.harness.record_audit_outcome,
            self.harness.list_audit_summaries,
            self.harness.get_audit_summary,
            self.harness.delete_audit_summary,
        ):
            spy.assert_not_called()
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.harness.audit_rows(), before)
        self.assertEqual(self.harness.row_counts(), SEEDED_COUNTS)

    def test_the_audit_commands_show_exactly_what_brain_recorded(self) -> None:
        present = "private-audit-path-5c1d9e7a20"
        absent = "private-audit-missing-0b8f6d3c41"
        provider = ScriptedProvider.replying(
            *(
                json.dumps(
                    {
                        "tool": "filesystem",
                        "action": "list",
                        "arguments": {"path": path},
                    }
                )
                for path in (present, absent)
            )
        )
        harness = _AuditHarness(self, provider)
        _seed_trio(harness.seeder)
        (harness.workspace / present).mkdir()

        offered = harness.session.submit("show me the folder that keeps my drafts")
        with patch("core.memory.audit_timestamp", return_value=FOURTH_TIMESTAMP):
            succeeded = harness.session.confirm()
        harness.session.submit("and the folder I never made")
        with patch("core.memory.audit_timestamp", return_value=FIFTH_TIMESTAMP):
            failed = harness.session.confirm()
        listing = harness.session.submit("audit").text
        block = harness.session.submit("read audit 5").text

        self.assertTrue(offered.needs_confirmation)
        self.assertEqual(
            succeeded.text,
            "Directory is empty.\n\nRecorded as audit summary 4.",
        )
        self.assertEqual(
            failed.text,
            f"Not a directory: {absent}\n\nRecorded as audit summary 5.",
        )
        self.assertEqual(
            harness.audit_rows()[3:],
            [
                (
                    4,
                    FOURTH_TIMESTAMP,
                    "filesystem.list",
                    "owner_approved",
                    "named_filesystem_root",
                    "succeeded",
                ),
                (
                    5,
                    FIFTH_TIMESTAMP,
                    "filesystem.list",
                    "owner_approved",
                    "named_filesystem_root",
                    "failed",
                ),
            ],
        )
        self.assertEqual(
            harness.record_audit_summary.call_args_list,
            [
                call(
                    "filesystem.list",
                    AuditDecision.OWNER_APPROVED,
                    AuditTarget.NAMED_FILESYSTEM_ROOT,
                )
            ]
            * 2,
        )
        self.assertEqual(
            harness.record_audit_outcome.call_args_list,
            [call(4, AuditOutcome.SUCCEEDED), call(5, AuditOutcome.FAILED)],
        )
        harness.prepare_confirmation.assert_not_called()
        self.assertEqual(
            listing,
            "\n".join(
                (
                    "5: 2026-01-15T08:09:10Z filesystem.list (target: named "
                    "filesystem root). Approved by the owner. Outcome: failed.",
                    "4: 2025-11-30T12:34:56Z filesystem.list (target: named "
                    "filesystem root). Approved by the owner. Outcome: succeeded.",
                    TRIO_LISTING,
                )
            ),
        )
        self.assertEqual(
            block,
            "Audit summary 5\n"
            "Created at: 2026-01-15T08:09:10Z\n"
            "Action: filesystem.list\n"
            "Target: named filesystem root\n"
            "Decision: approved by the owner\n"
            "Outcome: failed",
        )
        self.assertEqual(
            harness.interactions(),
            [("audit", LISTED_RECORD), ("read audit 5", "Displayed audit summary 5.")],
        )
        kept = "\n".join(
            (
                listing,
                block,
                repr(harness.audit_rows()),
                repr(_raw_interactions(harness.database_path)),
            )
        )
        for private in (present, absent, "my drafts", "never made"):
            with self.subTest(private=private):
                self.assertNotIn(private, kept)
        self.assertEqual(provider.calls, 2)


# --- Logging ------------------------------------------------------------------


class AuditLogPrivacyTests(unittest.TestCase):
    """Prove no summary field or failure text reaches a temporary log."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.log_dir = Path(temporary.name) / "logs"

        self.maria_logger = logging.getLogger("maria")
        self.original_handlers = tuple(self.maria_logger.handlers)
        self.original_level = self.maria_logger.level
        self.original_propagate = self.maria_logger.propagate
        self.original_disabled = self.maria_logger.disabled
        for handler in self.original_handlers:
            self.maria_logger.removeHandler(handler)
        self.addCleanup(self._restore_maria_logger)

    def _restore_maria_logger(self) -> None:
        """Close the temporary handler and restore the logger as it was."""
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
        self.maria_logger.disabled = self.original_disabled

    def test_audit_commands_and_their_failures_log_no_summary_field(self) -> None:
        configure_logging(self.log_dir, level="DEBUG")
        harness = _AuditHarness(self, logger=get_logger("tests.audit_tool.logged"))
        _seed_trio(harness.seeder)
        session = harness.session

        for command in (
            "audit",
            "read audit 1",
            "read audit 2",
            "read audit 3",
            "read audit 99",
        ):
            session.submit(command)
        session.submit("delete audit 3")
        session.confirm()
        with patch.object(
            harness.memory,
            "list_audit_summaries",
            side_effect=RuntimeError(FAILURE_SENTINEL),
        ):
            failed = session.submit("audit")
        _raw_execute(
            harness.database_path,
            "UPDATE audit_summaries SET action = ? WHERE id = 2",
            (TAMPER_SENTINEL,),
        )
        refused = session.submit("audit")
        unread = session.submit("read audit 2")
        session.end()

        self.assertEqual(failed.text, LIST_FAILED_MESSAGE)
        self.assertEqual(refused.text, LIST_FAILED_MESSAGE)
        self.assertEqual(unread.text, READ_FAILED_MESSAGE)
        for handler in self.maria_logger.handlers:
            handler.flush()
        log_file = self.log_dir / "maria.log"
        log_text = log_file.read_text(encoding="utf-8")

        # Positive controls: every path did log, so an empty log cannot pass
        # for a private one.
        for line in (
            "Executing tool action: audit.list",
            "Executing tool action: audit.read",
            "Executing tool action: audit.delete",
            "Action audit.delete requires confirmation.",
            "Confirmed action: audit.delete",
            "Failed to list audit summaries: a stored value is outside the "
            "audit vocabulary.",
            "Failed to read audit summary: a stored value is outside the "
            "audit vocabulary.",
            "Session ended.",
        ):
            with self.subTest(line=line):
                self.assertIn(line, log_text)

        assert_log_omits(
            self,
            log_file,
            (*SUMMARY_FIELD_TEXTS, FAILURE_SENTINEL, TAMPER_SENTINEL),
        )


if __name__ == "__main__":
    unittest.main()
