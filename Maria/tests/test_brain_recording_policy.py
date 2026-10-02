"""Tests for Maria's generic per-plan interaction recording policy."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import itertools
import logging
import sqlite3
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, NamedTuple
from unittest.mock import Mock, call, patch

import core.brain as brain_module
import core.memory as memory_module
import tools.system as system_module
from core.action_contracts import (
    BUILT_IN_ACTION_CONTRACTS,
    ContractFailure,
    ContractViolation,
)
from core.brain import BrainResponse, MariaBrain
from core.config import MariaConfig
from core.constants import APP_VERSION
from core.filesystem_roots import (
    FilesystemProtections,
    FilesystemRoot,
    FilesystemRoots,
    RootPolicy,
)
from core.memory import Memory
from core.permissions import (
    PermissionDecision,
    PermissionManager,
    PermissionResult,
)
from core.planner import Plan, Planner, PlanStep, RecordingPolicy
from core.session import MariaSession
from core.tool_manager import PreparedConfirmation, ToolExecutionResult, ToolManager


RUN = "run"
CONFIRM = "confirm"
CANCEL = "cancel"
REFUSE = "refuse"

# One character over the calculator's declared expression ceiling, so the
# real planner emits a step the action contract refuses.
OVERSIZED_EXPRESSION = "1+" * 100 + "1"

# Every expected row below was captured from the controller as it stood
# before the recording policy existed, with the clock, the host facts, and
# the stored creation timestamps pinned by _controlled_environment.
HELP_TEXT = (
    "Supported commands:\n"
    "- calculate <expression>\n"
    "- list <path>\n"
    "- read file <path>\n"
    "- rename <source> to <destination>\n"
    "- move <source> to <destination>\n"
    "- create note <title>: <body>\n"
    "- notes\n"
    "- read note <id or exact title>\n"
    "- retitle note <id>: <new title>\n"
    "- edit note <id>: <new body>\n"
    "- delete note <id>\n"
    "- enable note context <id>\n"
    "- disable note context <id>\n"
    "- remember <fact>\n"
    "- recall <query>\n"
    "- memories\n"
    "- update memory <id>: <new fact>\n"
    "- forget memory <id>\n"
    "- enable memory context <id>\n"
    "- disable memory context <id>\n"
    "- system\n"
    "- time\n"
    "- history\n"
    "- search history <query>\n"
    "- read interaction <id>\n"
    "- delete interaction <id>\n"
    "- audit\n"
    "- read audit <id>\n"
    "- delete audit <id>"
)
UNSUPPORTED_RESPONSE = (
    "I do not have an executable plan for that yet. "
    "Type 'help' to see supported commands."
)
SYSTEM_OVERVIEW = (
    "OS: CorpusOS-1.0\n"
    "CPU count: 8\n"
    "RAM: 16.00 GB\n"
    "Disk: 200.00 GB used / 500.00 GB total (300.00 GB free)\n"
    "Current time: 2031-02-03 04:05:06 UTC+08:00"
)
TIME_REPORT = (
    "Date: 2031-02-03\n"
    "Time: 04:05:06\n"
    "UTC offset: +08:00\n"
    "ISO-8601: 2031-02-03T04:05:06+08:00"
)
HISTORY_LISTING = (
    "55: delete note 2\n"
    "54: delete note 2\n"
    "53: delete note 0\n"
    "52: delete note\n"
    "51: edit note 1:\n"
    "50: edit note 1: Replaced body text\n"
    "49: edit note\n"
    "48: retitle note zero: Title\n"
    "47: retitle note 1: Renamed Release Check\n"
    "46: retitle note\n"
    "45: read note 99\n"
    "44: read note Groceries\n"
    "43: read note 1\n"
    "42: notes\n"
    "41: create note Empty Body:\n"
    "40: create note missing colon\n"
    "39: note Groceries: Milk and eggs\n"
    "38: create note Release Check: Original body text\n"
    "37: move nothing\n"
    "36: move inbox/final.txt to archive/final.txt\n"
    "Showing the newest 20 interactions. More interactions exist."
)
HISTORY_SEARCH_CALCULATE = (
    f"11: calculate {OVERSIZED_EXPRESSION}\n"
    "10: CALCULATE 1 + 1\n"
    "9: calculate 1 / 0\n"
    "7: calculate 6 * 7\n"
    "3: HELP\n"
    "2: commands\n"
    "1: help"
)

Row = tuple[str, str]
CorpusEntry = tuple[str, str, tuple[Row, ...]]


def _records(command: str, response: str, outcome: str = RUN) -> CorpusEntry:
    """Return an entry whose one recorded row repeats its command verbatim."""
    return (command, outcome, ((command, response),))


def _records_nothing(command: str, outcome: str) -> CorpusEntry:
    """Return an entry that must leave the interaction table untouched."""
    return (command, outcome, ())


# Run in this order through one session over one temporary database. Later
# entries depend on earlier ones exactly as they would for the owner: ids,
# listings, and not-found results all follow from what ran before.
COMMAND_CORPUS: tuple[CorpusEntry, ...] = (
    _records("help", HELP_TEXT),
    _records("commands", HELP_TEXT),
    _records("HELP", HELP_TEXT),
    _records("sing me an unsupported song", UNSUPPORTED_RESPONSE),
    _records("list", UNSUPPORTED_RESPONSE),
    _records("histories", UNSUPPORTED_RESPONSE),
    _records("calculate 6 * 7", "6 * 7 = 42"),
    _records("calc 2 + 2", "2 + 2 = 4"),
    _records("calculate 1 / 0", "Calculation failed: division by zero"),
    ("  CALCULATE 1 + 1  ", RUN, (("CALCULATE 1 + 1", "1 + 1 = 2"),)),
    _records(
        f"calculate {OVERSIZED_EXPRESSION}",
        "Argument out of supported range for calculator.calculate: expression.",
    ),
    _records("remember", "Use: remember <fact>"),
    _records(
        "remember My editor is VS Code",
        "Remembered memory 1: My editor is VS Code",
    ),
    _records(
        "remember MY EDITOR IS VS CODE",
        "Already remembered as memory 1: My editor is VS Code",
    ),
    _records("recall", "Use: recall <query>"),
    _records("recall editor", "1: My editor is VS Code"),
    _records("recall nothing-matches-this", "No matching memories found."),
    _records("memories", "1: My editor is VS Code"),
    _records("update memory", "Use: update memory <id>: <new fact>"),
    _records("update memory 1", "Use: update memory <id>: <new fact>"),
    _records("update memory x: fact", "Memory id must be a positive whole number."),
    _records("update memory 1: My editor is Vim", "Updated memory 1.", CONFIRM),
    _records("update memory 99: Missing", "Memory not found: 99.", CONFIRM),
    _records("forget memory", "Use: forget memory <id>"),
    _records("forget memory abc", "Memory id must be a positive whole number."),
    _records_nothing("forget memory 1", CANCEL),
    _records("forget memory 1", "Forgot memory 1.", CONFIRM),
    _records("forget memory 1", "Memory not found: 1.", CONFIRM),
    _records("list inbox", "file: alpha.txt\nfile: draft.txt"),
    _records("ls reports", "file: summary.txt"),
    _records("list notes", "file: todo.txt"),
    _records(
        "list .",
        "directory: archive\ndirectory: inbox\ndirectory: notes\ndirectory: reports",
    ),
    _records("read file inbox/alpha.txt", "Alpha line one.\nAlpha line two."),
    _records("read file inbox/missing.txt", "Not a file: inbox/missing.txt"),
    _records("rename inbox/draft.txt", "Use: rename <source> to <destination>"),
    _records(
        "rename inbox/draft.txt to inbox/final.txt",
        "Renamed inbox/draft.txt to inbox/final.txt.",
        CONFIRM,
    ),
    _records(
        "move inbox/final.txt to archive/final.txt",
        "Moved inbox/final.txt to archive/final.txt.",
        CONFIRM,
    ),
    _records("move nothing", "Use: move <source> to <destination>"),
    _records_nothing("move inbox/alpha.txt to archive/alpha.txt", CANCEL),
    _records(
        "create note Release Check: Original body text",
        "Created note 1: Release Check",
    ),
    _records("note Groceries: Milk and eggs", "Created note 2: Groceries"),
    _records("create note missing colon", "Use: create note <title>: <body>"),
    _records(
        "create note Empty Body:",
        "Argument out of supported range for notes.create: body.",
    ),
    _records("notes", "2: Groceries\n1: Release Check"),
    _records("read note 1", "Release Check\nOriginal body text"),
    _records("read note Groceries", "Groceries\nMilk and eggs"),
    _records("read note 99", "Note not found: 99"),
    _records("retitle note", "Use: retitle note <id>: <new title>"),
    _records(
        "retitle note 1: Renamed Release Check",
        "Retitled note 1.",
        CONFIRM,
    ),
    _records("retitle note zero: Title", "Note id must be a positive whole number."),
    _records("edit note", "Use: edit note <id>: <new body>"),
    _records("edit note 1: Replaced body text", "Edited note 1.", CONFIRM),
    _records("edit note 1:", "Use: edit note <id>: <new body>"),
    _records("delete note", "Use: delete note <id>"),
    _records("delete note 0", "Note id must be a positive whole number."),
    _records("delete note 2", "Deleted note 2.", CONFIRM),
    _records("delete note 2", "Note not found: 2.", CONFIRM),
    _records("history", HISTORY_LISTING),
    _records("search history", "Use: search history <query>"),
    _records("search history calculate", HISTORY_SEARCH_CALCULATE),
    _records("search history no-such-marker", "No matching interactions found."),
    _records("read interaction", "Use: read interaction <id>"),
    _records(
        "read interaction abc",
        "Interaction id must be a positive whole number.",
    ),
    _records("read interaction 1", "Displayed interaction 1."),
    _records("read interaction 9999", "Interaction not found: 9999."),
    _records("system", SYSTEM_OVERVIEW),
    _records("system info", SYSTEM_OVERVIEW),
    _records("status", SYSTEM_OVERVIEW),
    _records("sys", SYSTEM_OVERVIEW),
    _records("sys info", SYSTEM_OVERVIEW),
    _records("time", TIME_REPORT),
    _records("current time", TIME_REPORT),
    _records("time now", TIME_REPORT),
    # V1-15's consent commands run last, so every id listed above is
    # unchanged. The earlier duplicate remember consumed an id, so this
    # memory is 3. Every recorded consent row names the id, never the fact.
    _records(
        "remember My shell is PowerShell",
        "Remembered memory 3: My shell is PowerShell",
    ),
    _records("enable memory context", "Use: enable memory context <id>"),
    _records(
        "enable memory context x",
        "Memory id must be a positive whole number.",
    ),
    _records_nothing("enable memory context 3", CANCEL),
    _records("enable memory context 3", "Enabled context for memory 3.", CONFIRM),
    _records("enable memory context 99", "Memory not found: 99.", CONFIRM),
    _records(
        "update memory 3: My shell is Bash",
        "Updated memory 3. Context consent for memory 3 was turned off because "
        "its text changed.",
        CONFIRM,
    ),
    _records("update memory 3: My shell is Zsh", "Updated memory 3.", CONFIRM),
    _records("disable memory context", "Use: disable memory context <id>"),
    _records(
        "disable memory context 0",
        "Memory id must be a positive whole number.",
    ),
    _records("enable memory context 3", "Enabled context for memory 3.", CONFIRM),
    _records(
        "disable memory context 3",
        "Disabled context for memory 3.",
        CONFIRM,
    ),
    _records("disable memory context 99", "Memory not found: 99.", CONFIRM),
    # V1-16's note consent commands run after V1-15's, so every id listed
    # above is unchanged. Note 1 still exists, retitled and edited above.
    # Every recorded consent row names the id, never the title or body.
    _records("enable note context", "Use: enable note context <id>"),
    _records(
        "enable note context x",
        "Note id must be a positive whole number.",
    ),
    _records_nothing("enable note context 1", CANCEL),
    _records("enable note context 1", "Enabled context for note 1.", CONFIRM),
    _records("enable note context 99", "Note not found: 99.", CONFIRM),
    _records(
        "edit note 1: Edited again",
        "Edited note 1. Context consent for note 1 was turned off because "
        "its body changed.",
        CONFIRM,
    ),
    _records("edit note 1: Edited once more", "Edited note 1.", CONFIRM),
    _records("enable note context 1", "Enabled context for note 1.", CONFIRM),
    _records(
        "retitle note 1: Retitled again",
        "Retitled note 1. Context consent for note 1 was turned off because "
        "its title changed.",
        CONFIRM,
    ),
    _records("disable note context", "Use: disable note context <id>"),
    _records(
        "disable note context 0",
        "Note id must be a positive whole number.",
    ),
    _records("enable note context 1", "Enabled context for note 1.", CONFIRM),
    _records("disable note context 1", "Disabled context for note 1.", CONFIRM),
    _records("disable note context 99", "Note not found: 99.", CONFIRM),
    # V1-18's audit commands run after V1-16's, so every id listed above is
    # unchanged. No reasoning model runs here, so no audit summary exists and
    # each command meets an empty audit table. Every recorded row is fixed
    # text naming at most an id, and a listing records only that it happened.
    _records("audit", "Listed audit summaries."),
    _records("read audit", "Use: read audit <id>"),
    _records(
        "read audit x",
        "Audit summary id must be a positive whole number.",
    ),
    _records("read audit 1", "Audit summary not found: 1."),
    _records("delete audit", "Use: delete audit <id>"),
    _records(
        "delete audit 0",
        "Audit summary id must be a positive whole number.",
    ),
    _records_nothing("delete audit 1", CANCEL),
    _records("delete audit 1", "Audit summary not found: 1.", CONFIRM),
    _records_nothing("   ", RUN),
    _records_nothing("", REFUSE),
)

# Run through a controller whose configuration denies two registered
# actions, one of which also requires confirmation, so denial is shown to
# take precedence over confirmation as well as over allowed execution.
DENIED_ACTIONS = frozenset({"notes.list", "memory.forget"})
DENIAL_CORPUS: tuple[CorpusEntry, ...] = (
    _records("notes", "Permission denied: notes.list is denied by configuration."),
    _records(
        "forget memory 1",
        "Permission denied: memory.forget is denied by configuration.",
    ),
    _records("memories", "No memories found."),
)

SEEDED_ROW = (1, "seeded request", "seeded response")

# Each deletion entry runs alone in a fresh database holding exactly one
# seeded row, and is checked twice: by the row it recorded, and by the
# complete table it left behind.
DELETION_CORPUS: tuple[
    tuple[str, str, tuple[Row, ...], tuple[tuple[int, str, str], ...]], ...
] = (
    (
        "delete interaction 1",
        CONFIRM,
        (("delete interaction 1", "Deleted interaction 1."),),
        ((2, "delete interaction 1", "Deleted interaction 1."),),
    ),
    (
        "delete interaction 9999",
        CONFIRM,
        (("delete interaction 9999", "Interaction not found: 9999."),),
        (SEEDED_ROW, (2, "delete interaction 9999", "Interaction not found: 9999.")),
    ),
    ("delete interaction 1", CANCEL, (), (SEEDED_ROW,)),
    (
        "delete interaction",
        RUN,
        (("delete interaction", "Use: delete interaction <id>"),),
        (SEEDED_ROW, (2, "delete interaction", "Use: delete interaction <id>")),
    ),
    (
        "delete interaction -1",
        RUN,
        (
            (
                "delete interaction -1",
                "Interaction id must be a positive whole number.",
            ),
        ),
        (
            SEEDED_ROW,
            (
                2,
                "delete interaction -1",
                "Interaction id must be a positive whole number.",
            ),
        ),
    ),
)


class CommandCorpusRecordingRegressionTests(unittest.TestCase):
    """Lock every existing command's recorded row to its baseline bytes.

    Each corpus runs through real planner, contract, permission, tool,
    session, and memory components over temporary resources, and each
    entry is compared against the exact rows that entry alone recorded.
    """

    def setUp(self) -> None:
        _controlled_environment(self)

    def test_every_command_records_its_baseline_row_byte_for_byte(self) -> None:
        harness = _CorpusHarness(self)

        for command, outcome, expected in COMMAND_CORPUS:
            with self.subTest(command=command, outcome=outcome):
                recorded = harness.drive(command, outcome)

                self.assertEqual(_encoded(recorded), _encoded(expected))

    def test_configured_denials_record_their_baseline_rows_byte_for_byte(
        self,
    ) -> None:
        harness = _CorpusHarness(self, denied_actions=DENIED_ACTIONS)

        for command, outcome, expected in DENIAL_CORPUS:
            with self.subTest(command=command, outcome=outcome):
                recorded = harness.drive(command, outcome)

                self.assertEqual(_encoded(recorded), _encoded(expected))

    def test_each_interaction_deletion_is_measured_in_its_own_database(
        self,
    ) -> None:
        for command, outcome, expected, expected_table in DELETION_CORPUS:
            with self.subTest(command=command, outcome=outcome):
                harness = _CorpusHarness(self)
                seeded_id = harness.memory.record_interaction(
                    SEEDED_ROW[1],
                    SEEDED_ROW[2],
                )
                self.assertEqual(seeded_id, SEEDED_ROW[0])

                recorded = harness.drive(command, outcome)

                self.assertEqual(_encoded(recorded), _encoded(expected))
                self.assertEqual(harness.all_rows(), expected_table)

    def test_corpus_covers_every_registered_action_and_outcome(self) -> None:
        planner = Planner(logger=_quiet_logger("tests.recording_policy.coverage"))
        entries = (
            COMMAND_CORPUS
            + DENIAL_CORPUS
            + tuple(entry[:3] for entry in DELETION_CORPUS)
        )

        planned = {
            f"{step.tool_name}.{step.action}"
            for command, _outcome, _expected in entries
            for step in planner.create_plan(command).steps
        }
        zero_step = [
            command
            for command, _outcome, expected in entries
            if expected and not planner.create_plan(command).steps
        ]

        self.assertEqual(planned, BUILT_IN_ACTION_CONTRACTS.action_identifiers)
        self.assertEqual(
            {outcome for _command, outcome, _expected in entries},
            {RUN, CONFIRM, CANCEL, REFUSE},
        )
        self.assertIn("help", zero_step)
        self.assertIn("sing me an unsupported song", zero_step)


class PlannerDefaultRecordingTests(unittest.TestCase):
    """Verify no deterministic command becomes transient in this stage."""

    def test_every_corpus_command_plans_with_the_default_policy(self) -> None:
        planner = Planner(logger=_quiet_logger("tests.recording_policy.default"))
        commands = {
            entry[0]
            for entry in COMMAND_CORPUS + DENIAL_CORPUS + DELETION_CORPUS
            if entry[0].strip()
        }

        for command in sorted(commands):
            with self.subTest(command=command):
                self.assertIs(
                    planner.create_plan(command).recording,
                    RecordingPolicy.RECORD,
                )


ALLOWED = PermissionResult(PermissionDecision.ALLOWED, "allowed")
CONFIRMATION = PermissionResult(
    PermissionDecision.REQUIRES_CONFIRMATION,
    "notes.retitle requires explicit confirmation.",
)
UNREGISTERED = ContractViolation(ContractFailure.UNKNOWN_ACTION, "filesystem", "write")


class _Observation(NamedTuple):
    """What one run through the controller displayed, dispatched, and wrote."""

    displayed: str
    recorded: list[Any]
    dispatched: list[Any]
    checked: list[Any]
    plan_returned: bool


class _MockComponents:
    """A controller over mocked boundaries, fresh for every observation."""

    def __init__(self) -> None:
        self.planner = Mock(spec=Planner)
        self.memory = Mock(spec=Memory)
        self.memory.get_recent_interactions.return_value = []
        self.tool_manager = Mock(spec=ToolManager)
        # No mocked tool pins confirmation state, so the plan offered
        # for approval is the plan itself.
        self.tool_manager.prepare_confirmation.side_effect = PreparedConfirmation
        self.tool_manager.validate_action.return_value = None
        self.permissions = Mock(spec=PermissionManager)
        self.permissions.check.return_value = ALLOWED
        self.brain = MariaBrain(
            config=Mock(spec=MariaConfig),
            planner=self.planner,
            memory=self.memory,
            tool_manager=self.tool_manager,
            permissions=self.permissions,
            logger=Mock(spec=logging.Logger),
        )

    def plan(self, plan: Plan) -> BrainResponse:
        """Have the planner return one plan and process its request."""
        self.planner.create_plan.return_value = plan
        return self.brain.process(plan.original_request)


def _zero_step(components: _MockComponents, policy: dict[str, Any]) -> BrainResponse:
    """Process a plan that performs no tool action."""
    return components.plan(Plan("help", (), "supported commands", **policy))


def _contract_refusal(
    components: _MockComponents,
    policy: dict[str, Any],
) -> BrainResponse:
    """Process a plan the action contracts refuse."""
    components.tool_manager.validate_action.return_value = UNREGISTERED
    step = PlanStep("filesystem", "write", {"path": "notes.txt"})
    return components.plan(Plan("write notes.txt", (step,), **policy))


def _permission_denied(
    components: _MockComponents,
    policy: dict[str, Any],
) -> BrainResponse:
    """Process a plan whose only step is denied."""
    components.permissions.check.return_value = PermissionResult(
        PermissionDecision.DENIED,
        "notes.list is denied by configuration.",
    )
    return components.plan(Plan("notes", (PlanStep("notes", "list", {}),), **policy))


def _completed(components: _MockComponents, policy: dict[str, Any]) -> BrainResponse:
    """Process a plan whose only step runs successfully."""
    components.tool_manager.execute_tool.return_value = ToolExecutionResult(True, "4")
    step = PlanStep("calculator", "calculate", {"expression": "2 + 2"})
    return components.plan(Plan("calculate 2 + 2", (step,), **policy))


def _completed_after_a_failed_step(
    components: _MockComponents,
    policy: dict[str, Any],
) -> BrainResponse:
    """Process a two-step plan whose second step fails."""
    components.tool_manager.execute_tool.side_effect = (
        ToolExecutionResult(True, "4"),
        ToolExecutionResult(False, "File not found: missing.txt"),
    )
    steps = (
        PlanStep("calculator", "calculate", {"expression": "2 + 2"}),
        PlanStep("filesystem", "read", {"path": "missing.txt"}),
    )
    return components.plan(Plan("synthetic partial failure", steps, **policy))


def _response_override(
    components: _MockComponents,
    policy: dict[str, Any],
) -> BrainResponse:
    """Process a plan whose result replaces its recorded response half."""
    components.tool_manager.execute_tool.return_value = ToolExecutionResult(
        True,
        "Interaction 5\nRequest:\nsecret\nResponse:\nsecret answer",
        {"id": 5},
        recorded_message="Displayed interaction 5.",
    )
    step = PlanStep("history", "read", {"id": 5})
    return components.plan(Plan("read interaction 5", (step,), **policy))


def _empty_response_override(
    components: _MockComponents,
    policy: dict[str, Any],
) -> BrainResponse:
    """Process a plan whose result records an explicit empty response."""
    components.tool_manager.execute_tool.return_value = ToolExecutionResult(
        True,
        "4",
        None,
        recorded_message="",
    )
    step = PlanStep("calculator", "calculate", {"expression": "2 + 2"})
    return components.plan(Plan("calculate 2 + 2", (step,), **policy))


def _confirmed(components: _MockComponents, policy: dict[str, Any]) -> BrainResponse:
    """Hold a plan for confirmation, then execute the approved plan."""
    components.permissions.check.return_value = CONFIRMATION
    components.tool_manager.execute_tool.return_value = ToolExecutionResult(
        True,
        "Retitled note 1.",
    )
    step = PlanStep("notes", "retitle", {"id": 1, "title": "New Title"})
    pending = components.plan(Plan("retitle note 1: New Title", (step,), **policy))
    if not pending.needs_confirmation or components.memory.record_interaction.called:
        raise AssertionError("A pending confirmation must record nothing.")
    return components.brain.execute_confirmed_plan(pending.plan)


def _confirmed_through_a_session(
    components: _MockComponents,
    policy: dict[str, Any],
) -> BrainResponse:
    """Drive a complete submit and confirm cycle through a session."""
    components.permissions.check.return_value = CONFIRMATION
    components.tool_manager.execute_tool.return_value = ToolExecutionResult(
        True,
        "Retitled note 1.",
    )
    step = PlanStep("notes", "retitle", {"id": 1, "title": "New Title"})
    components.planner.create_plan.return_value = Plan(
        "retitle note 1: New Title",
        (step,),
        **policy,
    )
    session = MariaSession(components.brain, logger=Mock(spec=logging.Logger))
    if not session.submit("retitle note 1: New Title").needs_confirmation:
        raise AssertionError("Expected the session to hold the plan.")
    return session.confirm()


# Each path, what it displays, and the one row it records by default. The
# four recording paths are zero-step, contract refusal, permission denied,
# and completed execution; the rest are completed execution reached in the
# other ways it can be reached.
RECORDING_PATHS: tuple[tuple[str, Any, str, tuple[str, str]], ...] = (
    (
        "zero-step",
        _zero_step,
        "supported commands",
        ("help", "supported commands"),
    ),
    (
        "contract refusal",
        _contract_refusal,
        "Unsupported action: filesystem.write is not a supported action.",
        (
            "write notes.txt",
            "Unsupported action: filesystem.write is not a supported action.",
        ),
    ),
    (
        "permission denied",
        _permission_denied,
        "Permission denied: notes.list is denied by configuration.",
        ("notes", "Permission denied: notes.list is denied by configuration."),
    ),
    ("completed", _completed, "4", ("calculate 2 + 2", "4")),
    (
        "completed after a failed step",
        _completed_after_a_failed_step,
        "4\nFile not found: missing.txt",
        ("synthetic partial failure", "4\nFile not found: missing.txt"),
    ),
    (
        "completed with a response override",
        _response_override,
        "Interaction 5\nRequest:\nsecret\nResponse:\nsecret answer",
        ("read interaction 5", "Displayed interaction 5."),
    ),
    (
        "completed with an empty response override",
        _empty_response_override,
        "4",
        ("calculate 2 + 2", ""),
    ),
    (
        "confirmed",
        _confirmed,
        "Retitled note 1.",
        ("retitle note 1: New Title", "Retitled note 1."),
    ),
    (
        "confirmed through a session",
        _confirmed_through_a_session,
        "Retitled note 1.",
        ("retitle note 1: New Title", "Retitled note 1."),
    ),
)


def _observe(path: Any, policy: dict[str, Any]) -> _Observation:
    """Run one path over fresh mocked boundaries and report what happened."""
    components = _MockComponents()
    response = path(components, policy)
    return _Observation(
        displayed=response.text,
        recorded=components.memory.record_interaction.call_args_list,
        dispatched=components.tool_manager.execute_tool.call_args_list,
        checked=components.permissions.check.call_args_list,
        plan_returned=response.plan is not None,
    )


class RecordingPolicyPathTests(unittest.TestCase):
    """Verify every recording path honours the plan's policy the same way."""

    def test_default_and_explicit_record_write_the_existing_row(self) -> None:
        for name, path, displayed, row in RECORDING_PATHS:
            for label, policy in (
                ("default", {}),
                ("explicit record", {"recording": RecordingPolicy.RECORD}),
            ):
                with self.subTest(path=name, policy=label):
                    observed = _observe(path, policy)

                    self.assertEqual(observed.displayed, displayed)
                    self.assertEqual(observed.recorded, [call(*row)])

    def test_transient_plans_write_no_row_on_any_path(self) -> None:
        for name, path, displayed, _row in RECORDING_PATHS:
            with self.subTest(path=name):
                observed = _observe(path, {"recording": RecordingPolicy.TRANSIENT})

                self.assertEqual(observed.displayed, displayed)
                self.assertEqual(observed.recorded, [])

    def test_transient_plans_display_dispatch_and_check_exactly_as_before(
        self,
    ) -> None:
        for name, path, _displayed, _row in RECORDING_PATHS:
            with self.subTest(path=name):
                recorded = _observe(path, {"recording": RecordingPolicy.RECORD})
                transient = _observe(path, {"recording": RecordingPolicy.TRANSIENT})

                self.assertEqual(transient.displayed, recorded.displayed)
                self.assertEqual(transient.dispatched, recorded.dispatched)
                self.assertEqual(transient.checked, recorded.checked)
                self.assertTrue(transient.plan_returned)

    def test_a_pending_transient_plan_records_nothing_and_stays_bound(self) -> None:
        components = _MockComponents()
        components.permissions.check.return_value = CONFIRMATION
        step = PlanStep("notes", "retitle", {"id": 1, "title": "New Title"})
        plan = Plan(
            "retitle note 1: New Title",
            (step,),
            recording=RecordingPolicy.TRANSIENT,
        )
        components.planner.create_plan.return_value = plan
        session = MariaSession(components.brain, logger=Mock(spec=logging.Logger))

        response = session.submit("retitle note 1: New Title")

        self.assertTrue(response.needs_confirmation)
        self.assertIs(session.pending_approval.plan, plan)
        self.assertEqual(session.pending_approval.fingerprint, plan.fingerprint)
        components.memory.record_interaction.assert_not_called()
        components.tool_manager.execute_tool.assert_not_called()


class _PolicyPlanner:
    """Plan with the real planner, then apply one recording policy."""

    def __init__(self, policy: RecordingPolicy | None) -> None:
        self.inner = Planner(logger=_quiet_logger("tests.recording_policy.planner"))
        self.policy = policy

    def create_plan(self, user_input: str) -> Plan:
        """Return the real plan, rebuilt with the chosen policy if one is set."""
        plan = self.inner.create_plan(user_input)
        if self.policy is None:
            return plan
        return dataclasses.replace(plan, recording=self.policy)


# Each real-component path, the configured denials it needs, and the row
# the same command records under the default policy.
DATABASE_PATHS: tuple[tuple[str, str, str, frozenset[str], str], ...] = (
    ("zero-step", "help", RUN, frozenset(), HELP_TEXT),
    (
        "contract refusal",
        f"calculate {OVERSIZED_EXPRESSION}",
        RUN,
        frozenset(),
        "Argument out of supported range for calculator.calculate: expression.",
    ),
    (
        "permission denied",
        "notes",
        RUN,
        frozenset({"notes.list"}),
        "Permission denied: notes.list is denied by configuration.",
    ),
    ("completed", "calculate 6 * 7", RUN, frozenset(), "6 * 7 = 42"),
    (
        "completed with a stored memory",
        "remember Transient fact",
        RUN,
        frozenset(),
        "Remembered memory 2: Transient fact",
    ),
    (
        "confirmed",
        "update memory 1: Replaced fact",
        CONFIRM,
        frozenset(),
        "Updated memory 1.",
    ),
)


class TransientPlanDatabaseTests(unittest.TestCase):
    """Prove against real temporary databases that transient means no row."""

    def _run(
        self,
        command: str,
        outcome: str,
        denied_actions: frozenset[str],
        policy: RecordingPolicy | None,
    ) -> tuple[_CorpusHarness, str, int, int]:
        """Run one command under one policy and count rows around it."""
        harness = _CorpusHarness(self, denied_actions=denied_actions)
        harness.memory.store_memory("Original fact")
        harness.brain.planner = _PolicyPlanner(policy)

        before = _interaction_count(harness.database_path)
        response = harness.session.submit(command)
        if outcome == CONFIRM:
            self.assertTrue(response.needs_confirmation)
            response = harness.session.confirm()
        after = _interaction_count(harness.database_path)
        return harness, response.text, before, after

    def test_transient_plans_write_zero_rows_on_every_path(self) -> None:
        for name, command, outcome, denied, _response in DATABASE_PATHS:
            with self.subTest(path=name):
                harness, _displayed, before, after = self._run(
                    command,
                    outcome,
                    denied,
                    RecordingPolicy.TRANSIENT,
                )

                self.assertEqual(before, 0)
                self.assertEqual(after, 0)
                self.assertEqual(harness.all_rows(), ())

    def test_default_and_explicit_record_write_exactly_one_row(self) -> None:
        for name, command, outcome, denied, response in DATABASE_PATHS:
            for label, policy in (
                ("default", None),
                ("explicit record", RecordingPolicy.RECORD),
            ):
                with self.subTest(path=name, policy=label):
                    harness, displayed, before, after = self._run(
                        command,
                        outcome,
                        denied,
                        policy,
                    )

                    self.assertEqual((before, after), (0, 1))
                    self.assertEqual(harness.all_rows(), ((1, command, response),))
                    self.assertEqual(displayed, response)

    def test_transient_plans_display_the_same_response(self) -> None:
        for name, command, outcome, denied, response in DATABASE_PATHS:
            with self.subTest(path=name):
                _harness, displayed, _before, _after = self._run(
                    command,
                    outcome,
                    denied,
                    RecordingPolicy.TRANSIENT,
                )

                self.assertEqual(displayed, response)

    def test_transient_plans_still_carry_out_their_tool_work(self) -> None:
        stored, _displayed, _before, stored_after = self._run(
            "remember Transient fact",
            RUN,
            frozenset(),
            RecordingPolicy.TRANSIENT,
        )
        updated, _displayed, _before, updated_after = self._run(
            "update memory 1: Replaced fact",
            CONFIRM,
            frozenset(),
            RecordingPolicy.TRANSIENT,
        )

        self.assertEqual(
            [memory["fact"] for memory in stored.memory.list_memories()],
            ["Transient fact", "Original fact"],
        )
        self.assertEqual(
            [memory["fact"] for memory in updated.memory.list_memories()],
            ["Replaced fact"],
        )
        self.assertEqual((stored_after, updated_after), (0, 0))

    def test_a_transient_read_leaves_existing_rows_untouched(self) -> None:
        harness = _CorpusHarness(self)
        seeded_id = harness.memory.record_interaction(
            "seeded request",
            "seeded response",
        )
        harness.brain.planner = _PolicyPlanner(RecordingPolicy.TRANSIENT)

        response = harness.session.submit(f"read interaction {seeded_id}")

        self.assertIn("seeded response", response.text)
        self.assertEqual(
            harness.all_rows(),
            ((seeded_id, "seeded request", "seeded response"),),
        )


def _interaction_count(database_path: Path) -> int:
    """Count interaction rows by querying the temporary database directly."""
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute("SELECT COUNT(*) FROM interactions").fetchone()
    finally:
        connection.close()
    return int(row[0])


# Attribute and variable names that carry a step's tool or action identity.
STEP_IDENTITY = frozenset({"tool_name", "action"})
# The attribute holding the owner's request text.
REQUEST_IDENTITY = "original_request"
# The local names Brain's entry path holds the raw and cleaned request in.
REQUEST_INPUTS = frozenset({"user_input", "cleaned_input"})
# The collaborators Brain legitimately hands a step's identity to.
DELEGATES = frozenset({"logger", "tool_manager", "permissions"})
GATE = "_record_interaction"
WRITE = "record_interaction"
FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)

# Rule codes a violation starts with, so every control can assert the reason
# it was refused rather than only that something complained.
GATE_SHAPE = "gate-shape"
WRITE_RULE = "write"
GATE_CALL = "gate-call"
POLICY_CHOICE = "policy-choice"
IDENTITY_DECISION = "identity-decision"
IDENTITY_FLOW = "identity-flow"
RECORDED_TEXT = "recorded-text"
REQUEST_COMMAND = "request-command"


def registered_names() -> frozenset[str]:
    """Return every declared tool name, action name, and tool.action id."""
    return (
        BUILT_IN_ACTION_CONTRACTS.tool_names
        | BUILT_IN_ACTION_CONTRACTS.action_identifiers
        | {contract.action for contract in BUILT_IN_ACTION_CONTRACTS.contracts}
    )


class _BoundaryReport(NamedTuple):
    """What the recording-boundary scan reached and what it refused."""

    gate_callers: tuple[str, ...]
    reaching: frozenset[str]
    recorded_text: frozenset[str]
    violations: list[str]


def recording_boundary_report(source: str, names: frozenset[str]) -> _BoundaryReport:
    """Statically check the recording boundary of a Brain source.

    The source is parsed, never executed. The gate's first statement after
    its docstring must be exactly the transient early return, and the one
    interaction write must follow it inside the gate, with no alias of the
    write anywhere. Every call to the gate must hand it the plan, and Brain
    may read the recording policy but never choose one.

    The decision rules cover the methods whose calls lead to the gate, any
    helper those methods call inside a condition, and every helper that
    produces the text handed to the gate. In that scope no condition or
    comparison may turn on a step's tool or action, on the request text, or
    on a registered tool or action name; a step's tool or action may only be
    handed to the logger, Tool Manager, or PermissionManager; and recorded
    text may not name a registered tool or action. A log message elsewhere
    in scope may name anything, and a condition may call a helper.

    In the same scope, the raw or cleaned request held in ``user_input`` or
    ``cleaned_input`` may not be compared with, or matched against, nonblank
    literal text, since that is a command-specific decision taken before the
    plan reaches the gate. That covers a comparison, a matcher call receiving
    the request and the literal positionally or by keyword, and a ``match``
    statement on the request with a nonblank literal case pattern. Checking
    the request for blankness stays allowed, including a ``case ""``.

    This is not a data-flow analysis. It follows calls made through self or
    the class, not aliases, values held in variables, or other modules, and
    it knows the request input only by those two names.
    """
    tree = ast.parse(source)
    parents = {
        child: node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, FUNCTION_NODES)
    }
    classes = frozenset(
        node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    )
    gates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, FUNCTION_NODES) and node.name == GATE
    ]
    gate_calls = [node for node in ast.walk(tree) if _is_method_call(node, GATE)]
    violations: list[str] = []

    if len(gates) == 1:
        violations.extend(_gate_shape_violations(gates[0]))
    else:
        violations.append(f"{GATE_SHAPE}: expected one gate, found {len(gates)}")
    violations.extend(_write_violations(tree, parents))
    violations.extend(_gate_call_violations(tree, parents, gate_calls))
    violations.extend(_policy_choice_violations(tree, gates))

    reaching = _reaching_the_gate(functions, classes)
    predicates = _followed(
        functions,
        classes,
        {
            name
            for function in reaching
            for condition in _conditions(functions[function])
            for name in _helper_calls(condition, functions, classes)
        },
    )
    recorded_text = _followed(
        functions,
        classes,
        {
            name
            for gate_call in gate_calls
            for argument in gate_call.args[1:]
            + [keyword.value for keyword in gate_call.keywords]
            for name in _helper_calls(argument, functions, classes)
        },
    )

    for name in sorted(reaching | predicates | recorded_text):
        function = functions[name]
        for decision in _conditions(function) + _comparisons(function):
            reason = _decision_reason(decision, names)
            if reason is not None:
                violations.append(f"{IDENTITY_DECISION}: {name} turns on {reason}")
            if _matches_request_command(decision):
                violations.append(
                    f"{REQUEST_COMMAND}: {name} matches a literal command"
                )
        for node in ast.walk(function):
            if _is_request_command_match(node):
                violations.append(
                    f"{REQUEST_COMMAND}: {name} matches a literal command"
                )
            if _names_step_identity(node) and not _handed_to_delegate(node, parents):
                violations.append(f"{IDENTITY_FLOW}: {name} passes on step identity")
    for name in sorted(recorded_text):
        for node in ast.walk(functions[name]):
            if isinstance(node, ast.Constant) and node.value in names:
                violations.append(f"{RECORDED_TEXT}: {name} names {node.value!r}")

    return _BoundaryReport(
        gate_callers=tuple(
            getattr(_enclosing_function(gate_call, parents), "name", "<module>")
            for gate_call in gate_calls
        ),
        reaching=frozenset(reaching),
        recorded_text=frozenset(recorded_text),
        violations=violations,
    )


def _gate_shape_violations(gate: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Check the gate opens with the transient return and writes after it."""
    violations: list[str] = []
    parameters = [argument.arg for argument in gate.args.args]
    if parameters[1:2] != ["plan"]:
        violations.append(f"{GATE_SHAPE}: the gate does not take the plan first")
    body = gate.body[1:] if gate.body and _is_docstring(gate.body[0]) else gate.body
    if not body or not _is_transient_return(body[0]):
        violations.append(f"{GATE_SHAPE}: the gate does not open with the guard")
        return violations

    writes = [
        node
        for statement in body[1:]
        for node in ast.walk(statement)
        if _is_method_call(node, WRITE)
    ]
    if len(writes) != 1:
        violations.append(f"{GATE_SHAPE}: the write does not follow the guard")
    elif not writes[0].args or not _is_plan_attribute(
        writes[0].args[0],
        REQUEST_IDENTITY,
    ):
        violations.append(f"{GATE_SHAPE}: the write does not record the request")
    return violations


def _is_transient_return(statement: ast.stmt) -> bool:
    """Return whether a statement is exactly the gate's transient early return."""
    return (
        isinstance(statement, ast.If)
        and not statement.orelse
        and len(statement.body) == 1
        and isinstance(statement.body[0], ast.Return)
        and statement.body[0].value is None
        and isinstance(statement.test, ast.Compare)
        and len(statement.test.ops) == 1
        and isinstance(statement.test.ops[0], ast.Is)
        and _is_plan_attribute(statement.test.left, "recording")
        and _is_transient_member(statement.test.comparators[0])
    )


def _is_transient_member(node: ast.AST) -> bool:
    """Return whether a node is exactly RecordingPolicy.TRANSIENT."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "TRANSIENT"
        and isinstance(node.value, ast.Name)
        and node.value.id == "RecordingPolicy"
    )


def _is_plan_attribute(node: ast.AST | None, attribute: str) -> bool:
    """Return whether a node is exactly plan.<attribute>."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attribute
        and isinstance(node.value, ast.Name)
        and node.value.id == "plan"
    )


def _is_docstring(statement: ast.stmt) -> bool:
    """Return whether a statement is a bare string literal."""
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def _write_violations(tree: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[str]:
    """Check there is one interaction write, inside the gate, never aliased."""
    violations: list[str] = []
    writes = [node for node in ast.walk(tree) if _is_method_call(node, WRITE)]
    if len(writes) != 1:
        violations.append(f"{WRITE_RULE}: expected one write, found {len(writes)}")
    for write in writes:
        owner = _enclosing_function(write, parents)
        if owner is None or owner.name != GATE:
            violations.append(f"{WRITE_RULE}: a write sits outside the gate")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == WRITE:
            parent = parents.get(node)
            if not (isinstance(parent, ast.Call) and parent.func is node):
                violations.append(f"{WRITE_RULE}: the write is aliased")
        if isinstance(node, ast.Constant) and node.value == WRITE:
            violations.append(f"{WRITE_RULE}: the write is named as text")
    return violations


def _gate_call_violations(
    tree: ast.AST,
    parents: dict[ast.AST, ast.AST],
    gate_calls: list[ast.Call],
) -> list[str]:
    """Check every call hands the gate the plan and the gate is never aliased."""
    violations: list[str] = []
    for gate_call in gate_calls:
        first = gate_call.args[0] if gate_call.args else None
        if not (
            (isinstance(first, ast.Name) and first.id == "plan")
            or (isinstance(first, ast.Attribute) and first.attr == "plan")
        ):
            violations.append(f"{GATE_CALL}: the gate is called without the plan")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == GATE:
            parent = parents.get(node)
            if not (isinstance(parent, ast.Call) and parent.func is node):
                violations.append(f"{GATE_CALL}: the gate is aliased")
    return violations


def _policy_choice_violations(
    tree: ast.AST,
    gates: list[ast.FunctionDef | ast.AsyncFunctionDef],
) -> list[str]:
    """Check Brain only reads the recording policy, in the gate's guard."""
    guard_reads = {
        statement.test.comparators[0].value
        for gate in gates
        for statement in gate.body
        if _is_transient_return(statement)
    }
    violations: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and node.id == "RecordingPolicy"
            and node not in guard_reads
        ):
            violations.append(f"{POLICY_CHOICE}: Brain names a recording policy")
        if isinstance(node, ast.keyword) and node.arg == "recording":
            violations.append(f"{POLICY_CHOICE}: Brain passes a recording policy")
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Attribute) and target.attr == "recording"
                for target in targets
            ):
                violations.append(f"{POLICY_CHOICE}: Brain assigns a recording policy")
    return violations


def _is_method_call(node: ast.AST, name: str) -> bool:
    """Return whether a node calls an attribute with the given name."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
    )


def _helper_calls(
    node: ast.AST,
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    classes: frozenset[str],
) -> set[str]:
    """Return the defined helpers a node calls through self, cls, or its class."""
    return {
        call.func.attr
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and (call.func.value.id in {"self", "cls"} or call.func.value.id in classes)
        and call.func.attr in functions
    }


def _reaching_the_gate(
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    classes: frozenset[str],
) -> set[str]:
    """Return the gate and every function whose helper calls lead to it."""
    callees = {
        name: _helper_calls(function, functions, classes)
        for name, function in functions.items()
    }
    reaching = {GATE} & set(functions)
    grew = True
    while grew:
        grew = False
        for name, called in callees.items():
            if name not in reaching and called & reaching:
                reaching.add(name)
                grew = True
    return reaching


def _followed(
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    classes: frozenset[str],
    roots: set[str],
) -> set[str]:
    """Return the root helpers and every helper they call, transitively."""
    followed: set[str] = set()
    pending = sorted(roots)
    while pending:
        name = pending.pop()
        if name in followed:
            continue
        followed.add(name)
        pending.extend(_helper_calls(functions[name], functions, classes) - followed)
    return followed


def _enclosing_function(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the innermost function containing a node."""
    while node in parents:
        node = parents[node]
        if isinstance(node, FUNCTION_NODES):
            return node
    return None


def _names_step_identity(node: ast.AST) -> bool:
    """Return whether a node reads a tool or action identity."""
    return (isinstance(node, ast.Attribute) and node.attr in STEP_IDENTITY) or (
        isinstance(node, ast.Name) and node.id in STEP_IDENTITY
    )


def _decision_reason(decision: ast.AST, names: frozenset[str]) -> str | None:
    """Return what tool-, action-, or request-specific value a decision reads."""
    for node in ast.walk(decision):
        if _names_step_identity(node):
            return "step identity"
        if isinstance(node, ast.Attribute) and node.attr == REQUEST_IDENTITY:
            return "the request text"
        if isinstance(node, ast.Constant) and node.value in names:
            return f"the registered name {node.value!r}"
    return None


def _matches_request_command(decision: ast.AST) -> bool:
    """Return whether a decision matches the request input against literal text.

    A comparison matches when one operand is the request input and another
    is, or literally contains, nonblank text. A call matches when the request
    input is its receiver or an argument and a nonblank literal is also an
    argument, positional or keyword, as in ``cleaned_input.startswith("read ")``
    or ``re.fullmatch(pattern="read", string=cleaned_input)``. Blankness
    checks such as ``not cleaned_input`` or ``cleaned_input == ""`` never
    match.
    """
    for node in ast.walk(decision):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(_is_request_input(operand) for operand in operands) and any(
                _is_command_literal(operand) for operand in operands
            ):
                return True
        if isinstance(node, ast.Call):
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            inputs = list(arguments)
            if isinstance(node.func, ast.Attribute):
                inputs.append(node.func.value)
            if any(_is_request_input(value) for value in inputs) and any(
                _is_command_literal(argument) for argument in arguments
            ):
                return True
    return False


def _is_request_command_match(node: ast.AST) -> bool:
    """Return whether a match statement matches the request against literal text.

    The subject must be the request input, directly or through a method on
    it, and some case pattern must name nonblank literal text, alone or as
    one alternative of an or-pattern. A ``case ""`` used for blank
    validation never matches.
    """
    return (
        isinstance(node, ast.Match)
        and _is_request_input(node.subject)
        and any(
            isinstance(pattern, ast.MatchValue) and _is_command_literal(pattern.value)
            for case in node.cases
            for pattern in ast.walk(case.pattern)
        )
    )


def _is_request_input(node: ast.AST) -> bool:
    """Return whether a value is the request input, or read directly from it."""
    while isinstance(node, (ast.Attribute, ast.Call, ast.Subscript)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return isinstance(node, ast.Name) and node.id in REQUEST_INPUTS


def _is_command_literal(node: ast.AST) -> bool:
    """Return whether a value is, or literally contains, nonblank text."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and bool(node.value.strip())
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_is_command_literal(element) for element in node.elts)
    return False


def _handed_to_delegate(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Return whether a node is an argument to self.<delegate>.<method>.

    The value may be handed over directly, as a keyword value, or inside an
    f-string that is itself one of those arguments.
    """
    argument = node
    parent = parents.get(argument)
    while isinstance(parent, (ast.FormattedValue, ast.JoinedStr)):
        argument = parent
        parent = parents.get(argument)
    return (
        isinstance(parent, ast.Call)
        and (
            argument in parent.args
            or argument in [keyword.value for keyword in parent.keywords]
        )
        and isinstance(parent.func, ast.Attribute)
        and isinstance(parent.func.value, ast.Attribute)
        and parent.func.value.attr in DELEGATES
        and isinstance(parent.func.value.value, ast.Name)
        and parent.func.value.value.id == "self"
    )


def _conditions(function: ast.AST) -> list[ast.AST]:
    """Return every expression that decides a branch inside a function."""
    conditions: list[ast.AST] = []
    for node in ast.walk(function):
        if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            conditions.append(node.test)
        elif isinstance(node, ast.Match):
            conditions.append(node.subject)
            conditions.extend(case.pattern for case in node.cases)
            conditions.extend(case.guard for case in node.cases if case.guard)
        elif isinstance(node, ast.comprehension):
            conditions.extend(node.ifs)
    return conditions


def _comparisons(function: ast.AST) -> list[ast.AST]:
    """Return every comparison inside a function, wherever its result goes."""
    return [node for node in ast.walk(function) if isinstance(node, ast.Compare)]


# A minimal controller shaped like Brain that the guard must accept. Each
# entry in GUARD_BYPASSES rewrites one fragment of it into a way of letting
# tool, action, or request identity steer recording, together with the rule
# that must refuse it; each entry in GUARD_HARMLESS rewrites one fragment
# into something that does not touch a recording decision and must pass.
COMPLIANT_BRAIN = '''
class MariaBrain:
    def process(self, user_input):
        plan = self.planner.create_plan(user_input)
        return self._run_plan(plan, confirmed=False)

    def _run_plan(self, plan, *, confirmed):
        if not plan.steps:
            self._record_interaction(plan, "no action")
            return None
        for step in plan.steps:
            self.logger.info("Planned: %s.%s", step.tool_name, step.action)
        return self._execute_resolved_plan(plan)

    def _execute_resolved_plan(self, resolved):
        results = []
        for step in resolved.plan.steps:
            results.append(
                self.tool_manager.execute_tool(step.tool_name, step.action, {})
            )
        self._record_interaction(resolved.plan, self._format_recorded_results(results))

    def _record_interaction(self, plan, response):
        if plan.recording is RecordingPolicy.TRANSIENT:
            return
        self.memory.record_interaction(plan.original_request, response)

    @staticmethod
    def _format_recorded_results(results):
        return "\\n".join(result.message for result in results)
'''
COMPLETED_RECORDING = (
    "        self._record_interaction(resolved.plan, "
    "self._format_recorded_results(results))\n"
)
GATE_CHECK = "        if plan.recording is RecordingPolicy.TRANSIENT:\n"
GATE_WRITE = "        self.memory.record_interaction(plan.original_request, response)\n"
PLAN_LOOP = "        for step in plan.steps:\n"
PLAN_LOG = (
    '            self.logger.info("Planned: %s.%s", step.tool_name, step.action)\n'
)
ENTRY_RUN = "        return self._run_plan(plan, confirmed=False)\n"
ENTRY_PLAN = "        plan = self.planner.create_plan(user_input)\n"
RECORDED_JOIN = '        return "\\n".join(result.message for result in results)\n'
GUARD_BYPASSES: tuple[tuple[str, str, str, str], ...] = (
    (
        "early return in the entry point",
        ENTRY_RUN,
        '        if plan.steps and plan.steps[0].tool_name == "history":\n'
        "            return None\n" + ENTRY_RUN,
        IDENTITY_DECISION,
    ),
    (
        "brain rewrites the policy for a tool",
        ENTRY_RUN,
        "        plan = self._adjusted(plan)\n"
        + ENTRY_RUN
        + "\n"
        "    def _adjusted(self, plan):\n"
        "        return dataclasses.replace(\n"
        "            plan,\n"
        "            recording=RecordingPolicy.TRANSIENT,\n"
        "        ) if SKIP.get(plan.original_request) else plan\n",
        POLICY_CHOICE,
    ),
    (
        "direct tool branch",
        COMPLETED_RECORDING,
        '        if step.tool_name != "history":\n    ' + COMPLETED_RECORDING,
        IDENTITY_DECISION,
    ),
    (
        "indirect predicate on the action",
        COMPLETED_RECORDING,
        "        if should_record(step.action):\n    " + COMPLETED_RECORDING,
        IDENTITY_DECISION,
    ),
    (
        "predicate held in a variable",
        COMPLETED_RECORDING,
        "        persist = should_record(step.action)\n"
        "        if persist:\n    " + COMPLETED_RECORDING,
        IDENTITY_FLOW,
    ),
    (
        "whole step handed to a predicate helper",
        COMPLETED_RECORDING,
        "        if self._persists(step):\n    "
        + COMPLETED_RECORDING
        + "\n"
        "    def _persists(self, step):\n"
        '        return step.tool_name != "history"\n',
        IDENTITY_DECISION,
    ),
    (
        "identifier lookup",
        COMPLETED_RECORDING,
        '        if f"{step.tool_name}.{step.action}" in SKIPPED:\n'
        "            return None\n" + COMPLETED_RECORDING,
        IDENTITY_DECISION,
    ),
    (
        "early return for an action",
        PLAN_LOOP,
        '        if plan.steps[0].action == "read":\n'
        "            return None\n" + PLAN_LOOP,
        IDENTITY_DECISION,
    ),
    (
        "match on tool identity",
        COMPLETED_RECORDING,
        "        match step.tool_name:\n"
        '            case "history":\n'
        "                return None\n" + COMPLETED_RECORDING,
        IDENTITY_DECISION,
    ),
    (
        "tool-specific recorded text",
        RECORDED_JOIN,
        '        return "\\n".join(\n'
        '            "hidden" if result.origin == "history" else result.message\n'
        "            for result in results\n"
        "        )\n",
        RECORDED_TEXT,
    ),
    (
        "tool-specific recorded text in a delegated helper",
        RECORDED_JOIN,
        "        return MariaBrain._render(results)\n"
        "\n"
        "    @staticmethod\n"
        "    def _render(results):\n"
        '        return "\\n".join(\n'
        '            "hidden" if result.origin == "history" else result.message\n'
        "            for result in results\n"
        "        )\n",
        RECORDED_TEXT,
    ),
    (
        "request-specific early return",
        ENTRY_RUN,
        '        if plan.original_request == "read interaction 5":\n'
        "            return None\n" + ENTRY_RUN,
        IDENTITY_DECISION,
    ),
    (
        "literal command check on the raw input",
        ENTRY_PLAN,
        '        if user_input == "read interaction 5":\n'
        "            return None\n" + ENTRY_PLAN,
        REQUEST_COMMAND,
    ),
    (
        "literal command check on the cleaned input",
        ENTRY_PLAN,
        "        cleaned_input = user_input.strip()\n"
        '        if cleaned_input == "read interaction 5":\n'
        "            return None\n"
        "        plan = self.planner.create_plan(cleaned_input)\n",
        REQUEST_COMMAND,
    ),
    (
        "command prefix match on the cleaned input",
        ENTRY_PLAN,
        "        cleaned_input = user_input.strip()\n"
        '        if cleaned_input.lower().startswith(("read interaction", "time")):\n'
        "            return None\n"
        "        plan = self.planner.create_plan(cleaned_input)\n",
        REQUEST_COMMAND,
    ),
    (
        "match statement on the raw input",
        ENTRY_PLAN,
        "        match user_input:\n"
        '            case "read interaction 5":\n'
        "                return None\n" + ENTRY_PLAN,
        REQUEST_COMMAND,
    ),
    (
        "match statement on the cleaned input",
        ENTRY_PLAN,
        "        cleaned_input = user_input.strip()\n"
        "        match cleaned_input:\n"
        '            case "read interaction 5":\n'
        "                return None\n"
        "        plan = self.planner.create_plan(cleaned_input)\n",
        REQUEST_COMMAND,
    ),
    (
        "match or-pattern hiding a command behind a blank case",
        ENTRY_PLAN,
        "        cleaned_input = user_input.strip()\n"
        "        match cleaned_input:\n"
        '            case "" | "read interaction 5":\n'
        "                return None\n"
        "        plan = self.planner.create_plan(cleaned_input)\n",
        REQUEST_COMMAND,
    ),
    (
        "match case guard on the cleaned input",
        ENTRY_PLAN,
        "        cleaned_input = user_input.strip()\n"
        "        match cleaned_input:\n"
        '            case _ if cleaned_input.startswith("read interaction"):\n'
        "                return None\n"
        "        plan = self.planner.create_plan(cleaned_input)\n",
        REQUEST_COMMAND,
    ),
    (
        "keyword matcher call on the cleaned input",
        ENTRY_PLAN,
        "        cleaned_input = user_input.strip()\n"
        "        if re.fullmatch(\n"
        '            pattern="read interaction 5",\n'
        "            string=cleaned_input,\n"
        "        ):\n"
        "            return None\n"
        "        plan = self.planner.create_plan(cleaned_input)\n",
        REQUEST_COMMAND,
    ),
    (
        "gate consults step identity",
        GATE_CHECK,
        "        if plan.recording is RecordingPolicy.TRANSIENT or "
        'plan.steps[0].tool_name == "notes":\n',
        GATE_SHAPE,
    ),
    (
        "gate delegates to a predicate",
        GATE_CHECK,
        "        if not self._should_record(plan):\n",
        GATE_SHAPE,
    ),
    (
        "gate ignores the policy",
        GATE_CHECK + "            return\n",
        "",
        GATE_SHAPE,
    ),
    (
        "gate compares the wrong member",
        GATE_CHECK,
        "        if plan.recording is RecordingPolicy.RECORD:\n",
        GATE_SHAPE,
    ),
    (
        "gate inverts the comparison",
        GATE_CHECK,
        "        if plan.recording is not RecordingPolicy.TRANSIENT:\n",
        GATE_SHAPE,
    ),
    (
        "gate writes before the transient return",
        GATE_CHECK + "            return\n" + GATE_WRITE,
        GATE_WRITE + GATE_CHECK + "            return\n",
        GATE_SHAPE,
    ),
    (
        "second interaction write",
        '            self._record_interaction(plan, "no action")\n',
        "            self.memory.record_interaction("
        'plan.original_request, "no action")\n',
        WRITE_RULE,
    ),
    (
        "second write through an alias",
        ENTRY_RUN,
        "        writer = self.memory.record_interaction\n"
        '        writer(user_input, "no action")\n' + ENTRY_RUN,
        WRITE_RULE,
    ),
    (
        "gate handed a request instead of the plan",
        '            self._record_interaction(plan, "no action")\n',
        "            self._record_interaction("
        'plan.original_request, "no action")\n',
        GATE_CALL,
    ),
)
GUARD_HARMLESS: tuple[tuple[str, str, str], ...] = (
    (
        "a condition calling an undefined helper",
        ENTRY_RUN,
        "        if self._ready():\n            pass\n" + ENTRY_RUN,
    ),
    (
        "a condition calling a harmless defined helper",
        ENTRY_RUN,
        "        if self._ready():\n"
        "            pass\n"
        + ENTRY_RUN
        + "\n"
        "    def _ready(self):\n"
        "        return self.memory is not None\n",
    ),
    (
        "a log message naming a tool",
        ENTRY_RUN,
        '        self.logger.info("history")\n' + ENTRY_RUN,
    ),
    (
        "a formatted log message carrying step identity",
        PLAN_LOG,
        '            self.logger.info(f"Planned: {step.tool_name}.{step.action}")\n',
    ),
    (
        "a generic condition calling a method on the input",
        ENTRY_RUN,
        "        if plan.steps and not user_input.isspace():\n"
        "            pass\n" + ENTRY_RUN,
    ),
    (
        "blank-input validation on the cleaned input",
        ENTRY_PLAN,
        "        cleaned_input = user_input.strip()\n"
        "        if not cleaned_input:\n"
        "            return None\n"
        "        plan = self.planner.create_plan(cleaned_input)\n",
    ),
    (
        "blank-input validation by comparison with empty text",
        ENTRY_PLAN,
        '        if user_input.strip() == "":\n'
        "            return None\n" + ENTRY_PLAN,
    ),
    (
        "blank-input validation by an empty-string match",
        ENTRY_PLAN,
        "        cleaned_input = user_input.strip()\n"
        "        match cleaned_input:\n"
        '            case "":\n'
        "                return None\n"
        "        plan = self.planner.create_plan(cleaned_input)\n",
    ),
    (
        "blank-input validation by a keyword matcher call",
        ENTRY_PLAN,
        "        cleaned_input = user_input.strip()\n"
        '        if re.fullmatch(pattern="", string=cleaned_input):\n'
        "            return None\n"
        "        plan = self.planner.create_plan(cleaned_input)\n",
    ),
    (
        "recorded text delegated to a harmless helper",
        RECORDED_JOIN,
        "        return MariaBrain._render(results)\n"
        "\n"
        "    @staticmethod\n"
        "    def _render(results):\n"
        '        return "\\n".join(result.message for result in results)\n',
    ),
)


class RecordingBoundarySourceGuardTests(unittest.TestCase):
    """Guard that Brain's recording boundary never branches on a tool."""

    def test_brain_recording_boundary_has_no_tool_or_action_branch(self) -> None:
        report = recording_boundary_report(
            inspect.getsource(brain_module),
            registered_names(),
        )

        self.assertEqual(report.violations, [])

    def test_the_scan_reaches_every_recording_path_in_brain(self) -> None:
        """Positive coverage, so the guard cannot pass by seeing nothing.

        This proves what the scan must reach rather than pinning the exact
        set of scanned methods, which would fail whenever an unrelated
        helper was added: the four current recording paths, the entry
        points that lead to them, and the helper producing recorded text.
        """
        report = recording_boundary_report(
            inspect.getsource(brain_module),
            registered_names(),
        )

        # Zero-step, contract refusal, and permission denied record in
        # _run_plan; completed execution records in _execute_resolved_plan.
        self.assertEqual(
            Counter(report.gate_callers),
            Counter({"_run_plan": 3, "_execute_resolved_plan": 1}),
        )
        self.assertLessEqual(
            {
                "process",
                "execute_confirmed_plan",
                "_run_plan",
                "_execute_resolved_plan",
                GATE,
            },
            report.reaching,
        )
        self.assertIn("_format_recorded_results", report.recorded_text)
        self.assertIn("history", registered_names())
        self.assertIn("history.read", registered_names())
        self.assertIn("read", registered_names())

    def test_the_compliant_control_passes(self) -> None:
        report = recording_boundary_report(COMPLIANT_BRAIN, registered_names())

        self.assertEqual(report.violations, [])
        self.assertEqual(
            Counter(report.gate_callers),
            Counter({"_run_plan": 1, "_execute_resolved_plan": 1}),
        )
        self.assertIn("_format_recorded_results", report.recorded_text)

    def test_every_bypass_control_is_refused_for_its_own_reason(self) -> None:
        for name, fragment, replacement, rule in GUARD_BYPASSES:
            with self.subTest(bypass=name):
                self.assertEqual(COMPLIANT_BRAIN.count(fragment), 1)
                source = COMPLIANT_BRAIN.replace(fragment, replacement)

                report = recording_boundary_report(source, registered_names())

                reasons = {
                    violation.split(":", 1)[0] for violation in report.violations
                }
                self.assertIn(rule, reasons, report.violations)

    def test_every_harmless_control_passes(self) -> None:
        for name, fragment, replacement in GUARD_HARMLESS:
            with self.subTest(control=name):
                self.assertEqual(COMPLIANT_BRAIN.count(fragment), 1)
                source = COMPLIANT_BRAIN.replace(fragment, replacement)

                report = recording_boundary_report(source, registered_names())

                self.assertEqual(report.violations, [])

    def test_a_delegated_recorded_text_helper_is_reached(self) -> None:
        name, fragment, replacement = GUARD_HARMLESS[-1]
        source = COMPLIANT_BRAIN.replace(fragment, replacement)

        report = recording_boundary_report(source, registered_names())

        self.assertEqual(name, "recorded text delegated to a harmless helper")
        self.assertIn("_render", report.recorded_text)

    def test_literal_command_checks_in_the_real_entry_point_are_refused(self) -> None:
        """Inject each entry-path bypass into Brain's real process() source.

        The unchanged source keeps its blank-input check and passes, which the
        test above asserts; these copies add a literal command decision on the
        raw or the cleaned request before planning, as a comparison, a match
        statement, or a keyword matcher call, and each is refused.
        """
        source = inspect.getsource(brain_module)
        cleaning = "        cleaned_input = user_input.strip()\n"
        blank_check = "        if not cleaned_input:\n"
        self.assertEqual(source.count(cleaning), 1)
        self.assertEqual(source.count(blank_check), 1)
        bypasses = {
            "raw input": (
                cleaning,
                '        if user_input == "read interaction 5":\n'
                "            return None\n" + cleaning,
            ),
            "cleaned input": (
                blank_check,
                '        if cleaned_input == "read interaction 5":\n'
                "            return None\n" + blank_check,
            ),
            "match on the raw input": (
                cleaning,
                "        match user_input:\n"
                '            case "read interaction 5":\n'
                "                return None\n" + cleaning,
            ),
            "match on the cleaned input": (
                blank_check,
                "        match cleaned_input:\n"
                '            case "read interaction 5":\n'
                "                return None\n" + blank_check,
            ),
            "keyword matcher call on the cleaned input": (
                blank_check,
                "        if re.fullmatch(\n"
                '            pattern="read interaction 5",\n'
                "            string=cleaned_input,\n"
                "        ):\n"
                "            return None\n" + blank_check,
            ),
        }

        for name, (fragment, replacement) in bypasses.items():
            with self.subTest(bypass=name):
                report = recording_boundary_report(
                    source.replace(fragment, replacement),
                    registered_names(),
                )

                self.assertIn(
                    f"{REQUEST_COMMAND}: process matches a literal command",
                    report.violations,
                )

    def test_blank_checks_in_the_real_entry_point_stay_allowed(self) -> None:
        """Inject blank-input validation variants into the real process()."""
        source = inspect.getsource(brain_module)
        blank_check = "        if not cleaned_input:\n"
        self.assertEqual(source.count(blank_check), 1)
        blank_variants = {
            "empty-string match": (
                "        match cleaned_input:\n"
                '            case "":\n'
                '                return BrainResponse("Please enter a request.")\n'
                + blank_check
            ),
            "empty-pattern keyword matcher call": (
                '        if re.fullmatch(pattern="", string=cleaned_input):\n'
                '            return BrainResponse("Please enter a request.")\n'
                + blank_check
            ),
        }

        for name, replacement in blank_variants.items():
            with self.subTest(variant=name):
                report = recording_boundary_report(
                    source.replace(blank_check, replacement),
                    registered_names(),
                )

                self.assertEqual(report.violations, [])


class _CorpusHarness:
    """A complete Maria component set over its own temporary resources."""

    def __init__(
        self,
        test: unittest.TestCase,
        denied_actions: frozenset[str] = frozenset(),
    ) -> None:
        temporary = TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        workspace = root / "workspace"
        _seed_workspace(workspace)

        self.database_path = root / "database" / "maria.db"
        config = MariaConfig(
            app_name="Maria",
            version=APP_VERSION,
            environment="test",
            database_path=self.database_path,
            log_dir=root / "logs",
            prompt_path=root / "prompts" / "system.txt",
            filesystem_roots=FilesystemRoots(
                (
                    FilesystemRoot(
                        name="workspace",
                        path=workspace,
                        policy=RootPolicy.ORGANIZE,
                        enabled=True,
                    ),
                ),
                "workspace",
            ),
            filesystem_protections=FilesystemProtections(
                database_path=self.database_path,
                log_dir=root / "logs",
                prompt_path=root / "prompts" / "system.txt",
            ),
            log_level="DEBUG",
            confirmation_required=frozenset(),
            denied_actions=denied_actions,
        )

        logger = _quiet_logger("tests.recording_policy.corpus")
        self.memory = Memory(self.database_path, logger=logger)
        self.memory.initialize()
        tool_manager = ToolManager(config=config, memory=self.memory, logger=logger)
        tool_manager.load_tools()
        self.brain = MariaBrain(
            config=config,
            planner=Planner(logger=logger),
            memory=self.memory,
            tool_manager=tool_manager,
            permissions=PermissionManager(
                confirmation_required=config.confirmation_required,
                denied_actions=config.denied_actions,
                logger=logger,
            ),
            logger=logger,
        )
        self.session = MariaSession(self.brain, logger=logger)

    def last_interaction_id(self) -> int:
        """Return the highest interaction id currently stored, or 0."""
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM interactions"
            ).fetchone()
        finally:
            connection.close()
        return int(row[0])

    def rows_after(self, interaction_id: int) -> tuple[tuple[str, str], ...]:
        """Return every stored request and response newer than an id."""
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT user_input, response FROM interactions "
                "WHERE id > ? ORDER BY id",
                (interaction_id,),
            ).fetchall()
        finally:
            connection.close()
        return tuple((row[0], row[1]) for row in rows)

    def all_rows(self) -> tuple[tuple[int, str, str], ...]:
        """Return the whole interaction table with ids, oldest first."""
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT id, user_input, response FROM interactions ORDER BY id"
            ).fetchall()
        finally:
            connection.close()
        return tuple((int(row[0]), row[1], row[2]) for row in rows)

    def drive(self, command: str, outcome: str) -> tuple[tuple[str, str], ...]:
        """Run one corpus entry and return only the rows it recorded.

        Rows are selected by id rather than by count, and interaction ids
        are never reused, so a command that removes an earlier row can
        neither hide nor stand in for the row the command itself wrote.
        """
        before = self.last_interaction_id()

        if outcome == REFUSE:
            self.session.confirm()
            return self.rows_after(before)

        response = self.session.submit(command)
        if outcome == RUN:
            if response.needs_confirmation:
                raise AssertionError(f"Unexpected confirmation for {command!r}.")
        else:
            if not response.needs_confirmation:
                raise AssertionError(f"Expected a confirmation for {command!r}.")
            if outcome == CONFIRM:
                self.session.confirm()
            else:
                self.session.cancel()

        return self.rows_after(before)


def _seed_workspace(workspace: Path) -> None:
    """Create the fixed files the filesystem commands operate on."""
    (workspace / "inbox").mkdir(parents=True)
    (workspace / "archive").mkdir()
    (workspace / "reports").mkdir()
    (workspace / "notes").mkdir()
    (workspace / "inbox" / "alpha.txt").write_text(
        "Alpha line one.\nAlpha line two.",
        encoding="utf-8",
    )
    (workspace / "inbox" / "draft.txt").write_text("Draft body.", encoding="utf-8")
    (workspace / "reports" / "summary.txt").write_text(
        "Quarterly summary.",
        encoding="utf-8",
    )
    (workspace / "notes" / "todo.txt").write_text("Todo list.", encoding="utf-8")


def _encoded(rows: tuple[Row, ...]) -> tuple[tuple[bytes, bytes], ...]:
    """Return rows as UTF-8 bytes so the comparison is byte-for-byte."""
    return tuple(
        (request.encode("utf-8"), response.encode("utf-8"))
        for request, response in rows
    )


def _quiet_logger(name: str) -> logging.Logger:
    """Return an isolated logger that never writes an application log file."""
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


def _controlled_environment(test: unittest.TestCase) -> None:
    """Pin every clock and host value a recorded response can contain.

    The system and time commands render the local clock and host facts, and
    interaction listing order follows the stored creation timestamp, so all
    of them are fixed for the duration of one test. Tool warnings such as a
    failed calculation are kept off the console while the test runs.
    """
    namespace = logging.getLogger("maria")
    quiet = logging.NullHandler()
    namespace.addHandler(quiet)
    test.addCleanup(namespace.removeHandler, quiet)

    moment = datetime(2031, 2, 3, 4, 5, 6, tzinfo=timezone(timedelta(hours=8)))
    ticks = itertools.count(1)
    gibibyte = 1024**3
    patches = (
        patch.object(
            memory_module,
            "_utc_timestamp",
            side_effect=lambda: (
                f"2031-01-01T00:00:00.{next(ticks):06d}+00:00"
            ),
        ),
        patch.object(
            system_module,
            "_current_moment",
            return_value=moment,
        ),
        patch.object(
            system_module.platform,
            "platform",
            return_value="CorpusOS-1.0",
        ),
        patch.object(system_module.os, "cpu_count", return_value=8),
        patch.object(
            system_module,
            "_get_total_ram_bytes",
            return_value=16 * gibibyte,
        ),
        patch.object(
            system_module.shutil,
            "disk_usage",
            return_value=SimpleNamespace(
                total=500 * gibibyte,
                used=200 * gibibyte,
                free=300 * gibibyte,
            ),
        ),
    )
    for active in patches:
        active.start()
        test.addCleanup(active.stop)


if __name__ == "__main__":
    unittest.main()
