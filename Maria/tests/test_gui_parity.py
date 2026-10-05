"""One shared script through the command line and the desktop GUI (V1-21).

Each test writes one script of requests and answers and runs it twice, over
two identical fresh worlds: once through ``main.run_cli`` with scripted
terminal input, and once through the GUI controller with its real worker.
Each world is a real planner, contracts, permissions, tools, memory layer,
Brain, and session over its own temporary database, and the same scripted
provider stands in for the local model in both. Every clock a record or a
reply can show is pinned, so the two runs can be compared exactly.

The tests then assert that both interfaces showed the same text, offered the
same approvals for the same resolved plans, confirmed the same plans, refused
the same tampered fingerprint, asked the model the same things, dispatched
the same tool calls, and left the same records, audit summaries included.
Cancelling is compared the same way, so the GUI records exactly what the CLI
records.

Nothing here imports Qt, contacts a model, or reads Maria's live database,
live log, or private override.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from unittest.mock import patch

import main as main_module
from core import memory as memory_module
from core.brain import BrainResponse
from core.planner import Plan
from core.reasoning import ReasoningRequest
from core.session import CONFIRMATION_REFUSED_MESSAGE, MariaSession
from core.time_resolution import TimingFailure
from tests.gui_fakes import RunningGui
from tests.test_brain_recording_policy import _controlled_environment
from tests.test_conversation import ScriptedProvider, _Harness, enabled_settings
from tests.test_conversation_persistence import DRAFT, SUMMARY_PROMPT
from tests.test_model_approval import CALCULATE_PROPOSAL, REQUEST
from tests.zone_fixtures import SYNTHETIC_KEY


CONFIRMATION_PROMPT = (
    "Confirm action? Type 'y' or 'yes' to continue, or 'n' or 'no' to cancel: "
)
STAMP = "2026-10-03T08:09:10Z"
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

# A request the model answers with a plan of three steps, whose approval
# text is the longest the core produces.
MULTI_STEP_REQUEST = "Please add two numbers, list my files, then tell the time."
MULTI_STEP_PROPOSAL = json.dumps(
    {
        "steps": [
            {
                "tool": "calculator",
                "action": "calculate",
                "arguments": {"expression": "2 + 3"},
            },
            {"tool": "filesystem", "action": "list", "arguments": {}},
            {"tool": "system", "action": "current_time", "arguments": {}},
        ]
    }
)

# V1-22: a supported task request the model proposes faithfully, and a vague
# reminder request it answers with an invented time, which is refused.
TASK_REQUEST = "add a task to water the plants by friday"
TASK_PROPOSAL = json.dumps(
    {
        "tool": "tasks",
        "action": "create",
        "arguments": {"due": "friday", "text": "water the plants"},
    }
)
VAGUE_REQUEST = "remind me tomorrow to call mom"
INVENTED_PROPOSAL = json.dumps(
    {
        "tool": "reminders",
        "action": "create",
        "arguments": {
            "repeat": "never",
            "text": "call mom",
            "when": "tomorrow at 09:00",
        },
    }
)
# Two reminders in the owner's own words, proposed faithfully: their approval
# shows the reminder notice once (owner decision A).
REMINDER_REQUEST = (
    "remind me tomorrow at 9:00 to call mom and remind me to stretch in 2 hours"
)
REMINDER_PROPOSAL = json.dumps(
    {
        "steps": [
            {
                "tool": "reminders",
                "action": "create",
                "arguments": {
                    "repeat": "never",
                    "text": "call mom",
                    "when": "tomorrow at 9:00",
                },
            },
            {
                "tool": "reminders",
                "action": "create",
                "arguments": {
                    "repeat": "never",
                    "text": "stretch",
                    "when": "in 2 hours",
                },
            },
        ]
    }
)
REMINDER_NOTICE = (
    "Maria will not notify you when this reminder is due, and it does not "
    "repeat: type due reminders to see the reminders whose time has come."
)

# One script step: ("send", text), ("approve",), ("cancel",), or
# ("tamper_approve",), which spoils the pending fingerprint, then approves.
Step = tuple[str, ...]


def _model(request: ReasoningRequest) -> str:
    """Draft a summary, propose an action or a plan, or reply, by the prompt."""
    if request.prompt == SUMMARY_PROMPT:
        return DRAFT
    if request.prompt == REQUEST:
        return CALCULATE_PROPOSAL
    if request.prompt == MULTI_STEP_REQUEST:
        return MULTI_STEP_PROPOSAL
    if request.prompt == TASK_REQUEST:
        return TASK_PROPOSAL
    if request.prompt == VAGUE_REQUEST:
        return INVENTED_PROPOSAL
    if request.prompt == REMINDER_REQUEST:
        return REMINDER_PROPOSAL
    return f"Reply to {request.prompt}"


def _tamper(session: MariaSession) -> None:
    """Spoil the pending approval's fingerprint, keeping the same object."""
    pending = session.pending_approval
    assert pending is not None
    object.__setattr__(pending, "fingerprint", "0" * 64)


@dataclass(frozen=True)
class Outcome:
    """Everything one interface's run showed, asked, ran, and stored."""

    shown: str
    offered: tuple[tuple[bool, str | None], ...]
    confirmed: tuple[tuple[str, tuple[object, ...]], ...]
    tool_calls: tuple[tuple[object, ...], ...]
    prompts: tuple[str, ...]
    records: dict[str, list[tuple[object, ...]]]


class _World:
    """One fresh world for one interface's run, with its calls recorded."""

    def __init__(
        self,
        test: unittest.TestCase,
        *,
        reasoning: bool,
        time_zone: str | None = SYNTHETIC_KEY,
    ) -> None:
        _controlled_environment(test)
        for name in ("audit_timestamp", "record_timestamp", "task_timestamp"):
            active = patch.object(memory_module, name, return_value=STAMP)
            active.start()
            test.addCleanup(active.stop)

        self.provider = ScriptedProvider(_model) if reasoning else None
        self.harness = _Harness(
            test,
            self.provider,
            settings=enabled_settings(max_input_bytes=8192) if reasoning else None,
        )
        # V1-22: the synthetic zone the controlled environment installs is
        # the configured one, so timing commands resolve in both runs alike,
        # unless a test configures no zone at all.
        self.harness.brain.config = dataclasses.replace(
            self.harness.brain.config,
            time_zone=time_zone,
        )
        self.brain = self.harness.brain
        self.processed: list[BrainResponse] = []
        self.confirmed: list[Plan] = []
        process = self.brain.process
        execute = self.brain.execute_confirmed_plan

        def recorded_process(*args: object, **kwargs: object) -> BrainResponse:
            response = process(*args, **kwargs)
            self.processed.append(response)
            return response

        def recorded_execute(plan: Plan) -> BrainResponse:
            self.confirmed.append(plan)
            return execute(plan)

        self.brain.process = recorded_process
        self.brain.execute_confirmed_plan = recorded_execute

    def outcome(self, shown: str) -> Outcome:
        """Collect what this world's run did, once the run has ended."""
        return Outcome(
            shown=shown,
            offered=tuple(
                (
                    response.needs_confirmation,
                    None if response.plan is None else response.plan.fingerprint,
                )
                for response in self.processed
            ),
            confirmed=tuple(
                (plan.fingerprint, tuple(plan.steps)) for plan in self.confirmed
            ),
            tool_calls=tuple(
                tuple(call.args) for call in self.harness.execute_tool.call_args_list
            ),
            prompts=tuple(
                request.prompt
                for request in getattr(self.provider, "requests", ())
            ),
            records=_records(self.harness.database_path),
        )


def _records(database_path) -> dict[str, list[tuple[object, ...]]]:
    """Read every record table, leaving out the clock columns."""
    tables: dict[str, list[tuple[object, ...]]] = {}
    with closing(sqlite3.connect(database_path)) as connection:
        for table in RECORD_TABLES:
            cursor = connection.execute(f"SELECT * FROM {table} ORDER BY 1")
            kept = [
                index
                for index, column in enumerate(cursor.description)
                if not column[0].endswith("_at")
            ]
            tables[table] = [
                tuple(row[index] for index in kept) for row in cursor.fetchall()
            ]
    return tables


def run_through_cli(
    test: unittest.TestCase,
    script: list[Step],
    *,
    reasoning: bool,
    time_zone: str | None = SYNTHETIC_KEY,
) -> Outcome:
    """Run the script through ``main.run_cli`` with scripted terminal input."""
    world = _World(test, reasoning=reasoning, time_zone=time_zone)
    sessions: list[MariaSession] = []

    class RecordingSession(MariaSession):
        """A real session that the script can reach to tamper with."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            sessions.append(self)

    steps = [*script, ("send", "exit")]

    def answer(prompt: str) -> str:
        kind, *rest = steps.pop(0)
        if prompt == "You: ":
            test.assertEqual(kind, "send", "the script expected an answer")
            return rest[0]
        test.assertEqual(prompt, CONFIRMATION_PROMPT)
        test.assertIn(kind, ("approve", "cancel", "tamper_approve"))
        if kind == "tamper_approve":
            _tamper(sessions[0])
        return "n" if kind == "cancel" else "y"

    stdout = StringIO()
    stderr = StringIO()
    with (
        patch.object(main_module, "MariaSession", RecordingSession),
        patch("builtins.input", side_effect=answer),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        status = main_module.run_cli(lambda: world.brain)

    test.assertEqual(status, 0)
    test.assertEqual(stderr.getvalue(), "")
    test.assertEqual(steps, [])
    shown = stdout.getvalue()
    test.assertTrue(shown.endswith("Maria: Goodbye.\n"))
    return world.outcome(shown.removesuffix("Maria: Goodbye.\n"))


def run_through_gui(
    test: unittest.TestCase,
    script: list[Step],
    *,
    reasoning: bool,
    time_zone: str | None = SYNTHETIC_KEY,
) -> Outcome:
    """Run the script through the GUI controller and its real worker."""
    world = _World(test, reasoning=reasoning, time_zone=time_zone)
    sessions: list[MariaSession] = []

    def session_factory() -> MariaSession:
        session = MariaSession(world.brain)
        sessions.append(session)
        return session

    gui = RunningGui(test, session_factory)
    for kind, *rest in script:
        if kind == "send":
            gui.send(rest[0])
        elif kind == "approve":
            gui.approve()
        elif kind == "cancel":
            gui.cancel()
        else:
            _tamper(sessions[0])
            gui.approve()
    pending_at_close = sessions[0].pending_approval is not None
    gui.close()

    test.assertFalse(pending_at_close)
    test.assertEqual(gui.controller.exit_status, 0)
    shown = "".join(
        f"{line}\n" for line in gui.view.lines if not line.startswith("You: ")
    )
    return world.outcome(shown)


class InterfaceParityTests(unittest.TestCase):
    """Run one script through both interfaces and compare everything."""

    def assert_same_through_both(
        self,
        script: list[Step],
        *,
        reasoning: bool = False,
        time_zone: str | None = SYNTHETIC_KEY,
    ) -> Outcome:
        """Run the script through both interfaces and return the CLI's run."""
        through_cli = run_through_cli(
            self,
            script,
            reasoning=reasoning,
            time_zone=time_zone,
        )
        through_gui = run_through_gui(
            self,
            script,
            reasoning=reasoning,
            time_zone=time_zone,
        )

        self.assertEqual(through_gui.shown, through_cli.shown)
        self.assertEqual(through_gui.offered, through_cli.offered)
        self.assertEqual(through_gui.confirmed, through_cli.confirmed)
        self.assertEqual(through_gui.tool_calls, through_cli.tool_calls)
        self.assertEqual(through_gui.prompts, through_cli.prompts)
        self.assertEqual(through_gui.records, through_cli.records)
        return through_cli

    def test_deterministic_and_record_commands_match(self) -> None:
        outcome = self.assert_same_through_both(
            [
                ("send", "help"),
                ("send", "calculate 6 * 7"),
                ("send", "create note Groceries: milk and eggs"),
                ("send", "notes"),
                ("send", "read note 1"),
                ("send", "remember I prefer green tea"),
                ("send", "memories"),
                ("send", "recall tea"),
                ("send", "edit note 1: bread and butter"),
                ("approve",),
                ("send", "read note 1"),
                ("send", "enable memory context 1"),
                ("approve",),
                ("send", "delete note 1"),
                ("cancel",),
                ("send", "delete note 1"),
                ("approve",),
                ("send", "notes"),
                ("send", "save conversation"),
                ("send", "conversations"),
                ("send", "summaries"),
                ("send", "audit"),
                ("send", "history"),
                ("send", "system"),
                ("send", "time"),
                ("send", "this is not a command"),
            ]
        )

        # Positive controls: the script really did what it says.
        self.assertIn("6 * 7 = 42", outcome.shown)
        self.assertIn("bread and butter", outcome.shown)
        self.assertIn("Maria: Action cancelled.\n", outcome.shown)
        self.assertEqual(outcome.records["notes"], [])
        self.assertEqual(len(outcome.records["memories"]), 1)
        self.assertEqual(
            [offered for offered, _ in outcome.offered].count(True),
            4,
        )
        self.assertEqual(len(outcome.confirmed), 3)

    def test_conversation_save_and_summary_approval_match(self) -> None:
        outcome = self.assert_same_through_both(
            [
                ("send", "hello there"),
                ("send", "what did I just say"),
                ("send", "save conversation"),
                ("send", "conversations"),
                ("send", "read conversation 1"),
                ("send", "summarize conversation"),
                ("approve",),
                ("send", "summaries"),
                ("send", "read summary 1"),
                ("send", "summarize conversation"),
                ("cancel",),
                ("send", "enable summary context 1"),
                ("approve",),
            ],
            reasoning=True,
        )

        self.assertIn("Maria: Reply to hello there\n", outcome.shown)
        self.assertEqual(len(outcome.records["saved_conversations"]), 1)
        self.assertEqual(
            [row[1] for row in outcome.records["conversation_summaries"]],
            [DRAFT],
        )
        self.assertEqual(outcome.prompts.count(SUMMARY_PROMPT), 2)

    def test_model_plan_approval_audit_and_cancellation_match(self) -> None:
        outcome = self.assert_same_through_both(
            [
                ("send", REQUEST),
                ("approve",),
                ("send", REQUEST),
                ("cancel",),
                ("send", "audit"),
                ("send", "read audit 1"),
            ],
            reasoning=True,
        )

        self.assertIn("Tool: calculator", outcome.shown)
        self.assertIn("6 * 7 = 42", outcome.shown)
        self.assertEqual(len(outcome.records["audit_summaries"]), 1)
        self.assertEqual(len(outcome.records["audit_outcomes"]), 1)
        self.assertEqual(len(outcome.confirmed), 1)
        # A model-originated plan is transient, so neither run recorded the
        # proposal's request.
        self.assertNotIn(
            REQUEST,
            [row[1] for row in outcome.records["interactions"]],
        )

    def test_a_model_plan_of_several_steps_matches(self) -> None:
        outcome = self.assert_same_through_both(
            [
                ("send", MULTI_STEP_REQUEST),
                ("approve",),
                ("send", MULTI_STEP_REQUEST),
                ("cancel",),
                ("send", "audit"),
            ],
            reasoning=True,
        )

        self.assertIn("proposed this plan of 3 actions", outcome.shown)
        self.assertIn("Step 3 of 3, system.current_time: completed.", outcome.shown)
        self.assertEqual(len(outcome.confirmed), 1)
        self.assertEqual(len(outcome.confirmed[0][1]), 3)
        self.assertEqual(len(outcome.records["audit_summaries"]), 3)
        self.assertEqual(len(outcome.records["audit_outcomes"]), 3)

    def test_a_tampered_fingerprint_is_refused_identically(self) -> None:
        outcome = self.assert_same_through_both(
            [
                ("send", "create note Plan: alpha"),
                ("send", "edit note 1: beta"),
                ("tamper_approve",),
                ("send", "read note 1"),
                ("send", REQUEST),
                ("tamper_approve",),
                ("send", "audit"),
            ],
            reasoning=True,
        )

        self.assertEqual(
            outcome.shown.count(f"Maria: {CONFIRMATION_REFUSED_MESSAGE}\n"),
            2,
        )
        self.assertEqual(outcome.confirmed, ())
        self.assertEqual(
            [row[2] for row in outcome.records["notes"]],
            ["alpha"],
        )
        self.assertEqual(outcome.records["audit_summaries"], [])

    def test_cancelling_records_exactly_what_the_cli_records(self) -> None:
        outcome = self.assert_same_through_both(
            [
                ("send", "create note Keep: this"),
                ("send", "delete note 1"),
                ("cancel",),
                ("send", "hello there"),
                ("send", "summarize conversation"),
                ("cancel",),
                ("send", REQUEST),
                ("cancel",),
            ],
            reasoning=True,
        )

        self.assertEqual(outcome.shown.count("Maria: Action cancelled.\n"), 3)
        self.assertEqual(outcome.confirmed, ())
        self.assertEqual(len(outcome.records["notes"]), 1)
        self.assertEqual(outcome.records["conversation_summaries"], [])
        self.assertEqual(outcome.records["audit_summaries"], [])

    def test_task_and_reminder_commands_and_requests_match(self) -> None:
        outcome = self.assert_same_through_both(
            [
                ("send", "add task due tomorrow: buy milk"),
                ("approve",),
                ("send", "add reminder in 20 minutes: stretch"),
                ("approve",),
                ("send", "add task call the bank"),
                ("cancel",),
                ("send", "tasks"),
                ("send", "reminders"),
                ("send", "due tasks"),
                ("send", "due reminders"),
                ("send", "read task 1"),
                ("send", "complete task 1"),
                ("send", "completed tasks"),
                ("send", "reopen task 1"),
                ("send", "enable reminder context 1"),
                ("approve",),
                ("send", "read reminder 1"),
                ("send", "reschedule task 1: none"),
                ("approve",),
                ("send", TASK_REQUEST),
                ("approve",),
                ("send", VAGUE_REQUEST),
                ("send", "delete task 1"),
                ("approve",),
                ("send", "tasks"),
            ],
            reasoning=True,
        )

        self.assertIn("This approval adds a task, shown quoted:", outcome.shown)
        self.assertIn("This approval adds a reminder, shown quoted:", outcome.shown)
        self.assertIn("Tool: tasks", outcome.shown)
        self.assertIn("Nothing was added.", outcome.shown)
        self.assertEqual(outcome.shown.count("Maria: Action cancelled.\n"), 1)
        self.assertEqual(
            outcome.records["tasks"],
            [(2, "water the plants", "2031-02-07", "open", None)],
        )
        self.assertEqual(len(outcome.records["reminders"]), 1)
        self.assertEqual(len(outcome.records["audit_summaries"]), 1)
        self.assertEqual(outcome.prompts, (TASK_REQUEST, VAGUE_REQUEST))

    def test_every_task_and_reminder_command_matches_without_reasoning(self) -> None:
        # Every one of the twenty-six command forms, with reasoning off; the
        # controlled clock reads Sunday 2031-02-02 21:05:06 local at +01:00.
        outcome = self.assert_same_through_both(
            [
                ("send", "add task buy milk"),
                ("approve",),
                ("send", "add task due friday: pay rent"),
                ("approve",),
                ("send", "add reminder tomorrow 9:00: call mom"),
                ("approve",),
                ("send", "add reminder 2031-02-10 18:30 +01:00: dentist"),
                ("approve",),
                ("send", "add reminder in 2 hours: stretch"),
                ("approve",),
                ("send", "tasks"),
                ("send", "reminders"),
                ("send", "due tasks"),
                ("send", "due reminders"),
                ("send", "read task 2"),
                ("send", "read reminder 1"),
                ("send", "edit task 1: buy oat milk"),
                ("approve",),
                ("send", "edit reminder 1: call mom back"),
                ("approve",),
                ("send", "reschedule task 1: in 3 days"),
                ("approve",),
                ("send", "reschedule reminder 2: 2031-02-11 18:30"),
                ("approve",),
                ("send", "enable task context 2"),
                ("approve",),
                ("send", "disable task context 2"),
                ("approve",),
                ("send", "enable reminder context 3"),
                ("approve",),
                ("send", "disable reminder context 3"),
                ("approve",),
                ("send", "complete task 2"),
                ("send", "completed tasks"),
                ("send", "reopen task 2"),
                ("send", "complete reminder 3"),
                ("send", "completed reminders"),
                ("send", "reopen reminder 3"),
                ("send", "delete reminder 2"),
                ("cancel",),
                ("send", "delete reminder 2"),
                ("approve",),
                ("send", "delete task 1"),
                ("approve",),
                ("send", "tasks"),
                ("send", "reminders"),
            ]
        )

        # Positive controls: the script really did what it says.
        self.assertEqual(outcome.prompts, ())
        self.assertEqual(outcome.shown.count("Maria: Action cancelled.\n"), 1)
        self.assertEqual(
            [offered for offered, _ in outcome.offered].count(True),
            16,
        )
        self.assertEqual(len(outcome.confirmed), 15)
        self.assertEqual(
            outcome.records["tasks"],
            [(2, "pay rent", "2031-02-07", "open", None)],
        )
        self.assertEqual(
            [row[:3] for row in outcome.records["reminders"]],
            [
                (1, "call mom back", f"2031-02-03T09:00:00+01:00[{SYNTHETIC_KEY}]"),
                (3, "stretch", f"2031-02-02T23:05:00+01:00[{SYNTHETIC_KEY}]"),
            ],
        )
        self.assertEqual(outcome.records["audit_summaries"], [])

    def test_task_and_reminder_commands_match_without_a_zone(self) -> None:
        unavailable = TimingFailure.ZONE_UNAVAILABLE.message
        outcome = self.assert_same_through_both(
            [
                ("send", "add task buy milk"),
                ("approve",),
                ("send", "add task due friday: pay rent"),
                ("send", "due tasks"),
                ("send", "add reminder in 2 hours: stretch"),
                ("send", "tasks"),
                ("send", "reminders"),
                ("send", "due reminders"),
                ("send", "edit task 1: buy oat milk"),
                ("approve",),
                ("send", "complete task 1"),
                ("send", "reschedule task 1: none"),
                ("approve",),
                ("send", "delete task 1"),
                ("approve",),
                ("send", "tasks"),
            ],
            time_zone=None,
        )

        # Only new timing and due tasks needed the zone; everything else ran.
        self.assertEqual(outcome.shown.count(unavailable), 3)
        self.assertEqual(len(outcome.confirmed), 4)
        self.assertEqual(outcome.records["tasks"], [])
        self.assertEqual(outcome.records["reminders"], [])

    def test_a_model_reminder_approval_shows_its_notice_alike(self) -> None:
        outcome = self.assert_same_through_both(
            [
                ("send", REMINDER_REQUEST),
                ("approve",),
                ("send", REMINDER_REQUEST),
                ("cancel",),
            ],
            reasoning=True,
        )

        # Each offer of the two-reminder plan showed the notice once.
        self.assertEqual(outcome.shown.count(REMINDER_NOTICE), 2)
        self.assertEqual(outcome.shown.count("Maria: Action cancelled.\n"), 1)
        self.assertEqual(len(outcome.confirmed), 1)
        self.assertEqual(len(outcome.confirmed[0][1]), 2)
        self.assertEqual(len(outcome.records["reminders"]), 2)
        self.assertEqual(len(outcome.records["audit_summaries"]), 2)
        self.assertEqual(outcome.prompts, (REMINDER_REQUEST, REMINDER_REQUEST))


if __name__ == "__main__":
    unittest.main()
