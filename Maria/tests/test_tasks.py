"""Focused tests for V1-22's task tool, ``tools/tasks.py``.

Every test runs the tool over a temporary database. Each stamp the memory
layer writes is pinned, so every reply and stored value is exact, and the
tool itself never reads a clock or time zone data: every date it stores was
resolved before the plan was built.
"""

from __future__ import annotations

import ast
import inspect
import logging
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from core.context import ContextConsentOutcome, task_digest
from core.memory import Memory
from core.task_records import (
    AS_OF_DATE_MESSAGE,
    DUE_DATE_MESSAGE,
    TASK_ID_MESSAGE,
    TASK_STATUS_MESSAGE,
    TASK_TEXT_MESSAGE,
    Task,
)
from core.tool_manager import ConfirmationBinding, ToolExecutionResult
from tools import tasks as tasks_module
from tools.tasks import (
    ADD_FAILED_MESSAGE,
    CHANGE_FAILED_MESSAGE,
    CONTEXT_FAILED_MESSAGE,
    DELETE_FAILED_MESSAGE,
    LIST_FAILED_MESSAGE,
    MORE_TASKS_MESSAGE,
    READ_FAILED_MESSAGE,
    TasksTool,
    build_tool,
)


TEXT_SENTINEL = "private-task-text-5c1e9a"
STAMPS = tuple(f"2026-10-04T05:{minute:02d}:00Z" for minute in range(60))


class _TaskToolTestCase(unittest.TestCase):
    """A task tool over a temporary database, with pinned write stamps."""

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database_path = Path(temporary.name) / "db" / "maria.db"
        self.logger = Mock(spec=logging.Logger)
        self.memory = Memory(self.database_path, logger=self.logger)
        self.memory.initialize()
        self.tool = TasksTool(memory=self.memory, logger=self.logger)
        stamps = iter(STAMPS)
        clock = patch("core.memory.task_timestamp", side_effect=lambda: next(stamps))
        clock.start()
        self.addCleanup(clock.stop)

    def add(self, text: str = "buy milk", due: str | None = None) -> int:
        arguments = {"text": text} if due is None else {"text": text, "due": due}
        result = self.tool.execute("create", arguments)
        self.assertTrue(result.success, result.message)
        return result.data["id"]

    def rows(self) -> list[tuple[object, ...]]:
        connection = sqlite3.connect(self.database_path)
        try:
            return connection.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        finally:
            connection.close()

    def logged(self) -> str:
        return "\n".join(
            str(call) for call in self.logger.mock_calls if call[0] != "isEnabledFor"
        )


class TaskToolIdentityTests(_TaskToolTestCase):
    """Prove the tool's identity and factory."""

    def test_identity_actions_and_factory(self) -> None:
        self.assertEqual(self.tool.name, "tasks")
        self.assertEqual(
            self.tool.actions,
            frozenset(
                {
                    "create",
                    "list",
                    "due",
                    "read",
                    "complete",
                    "reopen",
                    "edit",
                    "reschedule",
                    "delete",
                    "enable_context",
                    "disable_context",
                }
            ),
        )
        built = build_tool(memory=self.memory, config=object(), logger=self.logger)
        self.assertIsInstance(built, TasksTool)
        self.assertIs(built.memory, self.memory)

    def test_an_unknown_action_is_refused(self) -> None:
        self.assertEqual(
            self.tool.execute("archive", {"id": 1}),
            ToolExecutionResult(False, "Unsupported task action."),
        )


class TaskCreateTests(_TaskToolTestCase):
    """Prove a task stores exactly the approved text and due date."""

    def test_a_dated_task_is_stored_and_reported_exactly(self) -> None:
        result = self.tool.execute("create", {"text": "buy milk", "due": "2026-10-09"})

        self.assertEqual(
            result,
            ToolExecutionResult(
                True,
                'Added task 1: "buy milk", due Friday 2026-10-09. Its context '
                "consent is off.",
                {"id": 1},
                recorded_message="Added task 1.",
            ),
        )
        self.assertEqual(
            self.rows(),
            [(1, "buy milk", "2026-10-09", "open", STAMPS[0], STAMPS[0], None, None)],
        )

    def test_an_undated_task_has_no_due_date(self) -> None:
        result = self.tool.execute("create", {"text": "buy milk"})

        self.assertEqual(
            result.message,
            'Added task 1: "buy milk", no due date. Its context consent is off.',
        )
        self.assertEqual(self.rows()[0][2], None)

    def test_a_past_due_date_in_an_approved_plan_is_stored_unchanged(self) -> None:
        # Approval stores the approved values even when they are overdue now.
        self.add(due="2001-01-01")
        self.assertEqual(self.rows()[0][2], "2001-01-01")

    def test_invalid_values_are_refused_before_storage(self) -> None:
        for arguments, message in (
            ({"text": " buy milk"}, TASK_TEXT_MESSAGE),
            ({"text": "a" * 513}, TASK_TEXT_MESSAGE),
            ({"text": "buy\nmilk"}, TASK_TEXT_MESSAGE),
            ({}, TASK_TEXT_MESSAGE),
            ({"text": "buy milk", "due": "2026-02-30"}, DUE_DATE_MESSAGE),
            ({"text": "buy milk", "due": "none"}, DUE_DATE_MESSAGE),
            ({"text": "buy milk", "due": "tomorrow"}, DUE_DATE_MESSAGE),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    self.tool.execute("create", arguments),
                    ToolExecutionResult(False, message),
                )
        self.assertEqual(self.rows(), [])

    def test_a_storage_failure_is_a_fixed_message(self) -> None:
        with patch.object(
            self.memory,
            "create_task",
            side_effect=RuntimeError(TEXT_SENTINEL),
        ):
            result = self.tool.execute("create", {"text": TEXT_SENTINEL})
        self.assertEqual(result, ToolExecutionResult(False, ADD_FAILED_MESSAGE))
        self.assertNotIn(TEXT_SENTINEL, self.logged())


class TaskPreviewTests(_TaskToolTestCase):
    """Prove each approval shows exactly what it covers, before storage."""

    def test_an_add_shows_the_exact_text_and_resolved_date(self) -> None:
        self.assertEqual(
            self.tool.confirmation_binding(
                "create",
                {"text": 'buy "oat" milk', "due": "2026-10-09"},
            ),
            ConfirmationBinding(
                {},
                "This approval adds a task, shown quoted:\n"
                'Text: "buy \\"oat\\" milk"\n'
                "Due: Friday 2026-10-09, a calendar date with no time",
            ),
        )
        self.assertEqual(
            self.tool.confirmation_binding("create", {"text": "buy milk"}).preview,
            'This approval adds a task, shown quoted:\nText: "buy milk"\nDue: none',
        )
        self.assertEqual(self.rows(), [])

    def test_changes_show_the_saved_values_beside_the_new_ones(self) -> None:
        task_id = self.add(due="2026-10-09")

        self.assertEqual(
            self.tool.confirmation_binding(
                "edit",
                {"id": task_id, "text": "buy oat milk"},
            ).preview,
            "This approval replaces the text of task 1, shown quoted:\n"
            'Now: "buy milk"\n'
            'New: "buy oat milk"',
        )
        self.assertEqual(
            self.tool.confirmation_binding(
                "reschedule",
                {"id": task_id, "due": "none"},
            ).preview,
            "This approval reschedules task 1, shown quoted:\n"
            'Text: "buy milk"\n'
            "Now: Friday 2026-10-09, a calendar date with no time\n"
            "New: none",
        )
        self.assertEqual(
            self.tool.confirmation_binding("delete", {"id": task_id}),
            ConfirmationBinding(
                {},
                "This approval deletes task 1, shown quoted:\n"
                'Text: "buy milk"\n'
                "Due: Friday 2026-10-09, a calendar date with no time",
            ),
        )

    def test_an_enable_pins_the_identity_of_exactly_what_it_shows(self) -> None:
        task_id = self.add(due="2026-10-09")

        self.assertEqual(
            self.tool.confirmation_binding("enable_context", {"id": task_id}),
            ConfirmationBinding(
                {"text_digest": task_digest("buy milk", "2026-10-09")},
                "This approval covers task 1 exactly as it is stored now, shown "
                "quoted:\n"
                'Text: "buy milk"\n'
                "Due: Friday 2026-10-09, a calendar date with no time",
            ),
        )

    def test_the_shown_text_and_the_identity_come_from_one_read(self) -> None:
        task_id = self.add(TEXT_SENTINEL, "2026-10-09")
        statements: list[str] = []
        original_connect = self.memory._connect

        def tracing_connect() -> sqlite3.Connection:
            connection = original_connect()
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(self.memory, "_connect", tracing_connect):
            binding = self.tool.confirmation_binding(
                "enable_context",
                {"id": task_id},
            )

        reads = [statement for statement in statements if "FROM tasks" in statement]
        self.assertEqual(len(reads), 1)
        self.assertTrue(reads[0].lstrip().startswith("SELECT"))
        self.assertEqual(
            binding.arguments,
            {"text_digest": task_digest(TEXT_SENTINEL, "2026-10-09")},
        )
        self.assertIn(f'Text: "{TEXT_SENTINEL}"', binding.preview)

    def test_nothing_is_shown_or_pinned_when_nothing_can_be_shown(self) -> None:
        self.add()
        for action, arguments in (
            ("create", {"text": " bad"}),
            ("create", {"text": "ok", "due": "none"}),
            ("edit", {"id": 9, "text": "x"}),
            ("edit", {"id": 1, "text": " bad"}),
            ("reschedule", {"id": 1, "due": "someday"}),
            ("delete", {"id": 9}),
            ("delete", {"id": True}),
            ("enable_context", {"id": 9}),
            ("list", {"status": "open"}),
            ("due", {"as_of": "2026-10-09"}),
            ("read", {"id": 1}),
            ("complete", {"id": 1}),
            ("reopen", {"id": 1}),
            ("disable_context", {"id": 1}),
        ):
            with self.subTest(action=action, arguments=arguments):
                self.assertEqual(self.tool.confirmation_binding(action, arguments), {})


class TaskListingTests(_TaskToolTestCase):
    """Prove listings, their order, their limit, and their recorded line."""

    def test_empty_listings(self) -> None:
        self.assertEqual(
            self.tool.execute("list", {"status": "open"}),
            ToolExecutionResult(
                True,
                "No open tasks.",
                [],
                recorded_message="Listed open tasks.",
            ),
        )
        self.assertEqual(
            self.tool.execute("list", {"status": "completed"}),
            ToolExecutionResult(
                True,
                "No completed tasks.",
                [],
                recorded_message="Listed completed tasks.",
            ),
        )

    def test_open_tasks_come_by_due_date_with_undated_last(self) -> None:
        self.add("undated one")
        self.add("later", "2026-12-01")
        self.add("sooner", "2026-10-09")
        self.add("undated two")

        result = self.tool.execute("list", {"status": "open"})

        self.assertEqual(
            result,
            ToolExecutionResult(
                True,
                '3: "sooner", due Friday 2026-10-09\n'
                '2: "later", due Tuesday 2026-12-01\n'
                '1: "undated one", no due date\n'
                '4: "undated two", no due date',
                [3, 2, 1, 4],
                recorded_message="Listed open tasks.",
            ),
        )

    def test_completed_tasks_come_latest_completion_first(self) -> None:
        for text in ("first", "second", "third"):
            self.add(text)
        for task_id in (2, 1, 3):
            self.tool.execute("complete", {"id": task_id})

        result = self.tool.execute("list", {"status": "completed"})

        self.assertEqual(
            result.message,
            f'3: "third", completed at {STAMPS[5]}\n'
            f'1: "first", completed at {STAMPS[4]}\n'
            f'2: "second", completed at {STAMPS[3]}',
        )
        self.assertEqual(result.recorded_message, "Listed completed tasks.")

    def test_twenty_are_shown_and_more_is_said_when_more_exist(self) -> None:
        for number in range(21):
            self.add(f"task {number}")

        result = self.tool.execute("list", {"status": "open"})

        lines = result.message.split("\n")
        self.assertEqual(len(lines), 21)
        self.assertEqual(lines[-1], MORE_TASKS_MESSAGE)
        self.assertEqual(
            MORE_TASKS_MESSAGE,
            "Showing the first 20 tasks. More tasks exist.",
        )
        self.assertEqual(result.data, list(range(1, 21)))

    def test_an_invalid_status_or_failure_is_a_fixed_message(self) -> None:
        self.assertEqual(
            self.tool.execute("list", {"status": "done"}),
            ToolExecutionResult(False, TASK_STATUS_MESSAGE),
        )
        with patch.object(self.memory, "list_tasks", side_effect=RuntimeError("x")):
            self.assertEqual(
                self.tool.execute("list", {"status": "open"}),
                ToolExecutionResult(False, LIST_FAILED_MESSAGE),
            )


class TaskDueTests(_TaskToolTestCase):
    """Prove the due query: a question the owner asks, never a delivery."""

    def test_due_tasks_are_open_and_dated_on_or_before_the_plans_date(self) -> None:
        self.add("overdue", "2026-10-01")
        self.add("today", "2026-10-04")
        self.add("tomorrow", "2026-10-05")
        self.add("undated")
        done = self.add("done", "2026-10-02")
        self.tool.execute("complete", {"id": done})

        result = self.tool.execute("due", {"as_of": "2026-10-04"})

        self.assertEqual(
            result,
            ToolExecutionResult(
                True,
                '1: "overdue", due Thursday 2026-10-01 (overdue)\n'
                '2: "today", due Sunday 2026-10-04 (due today)',
                [1, 2],
                recorded_message="Listed due tasks.",
            ),
        )

    def test_due_tasks_come_earliest_date_first_whatever_their_ids(self) -> None:
        self.add("third", "2026-10-04")
        self.add("first", "2026-09-30")
        self.add("second", "2026-10-02")
        self.add("third again", "2026-10-04")

        result = self.tool.execute("due", {"as_of": "2026-10-04"})

        # By date, and by id within one date.
        self.assertEqual(result.data, [2, 3, 1, 4])

    def test_twenty_due_are_shown_and_more_is_said_when_more_exist(self) -> None:
        # Added latest date first, so the earliest twenty are the newest ids.
        for number in range(21):
            self.add(f"task {number}", f"2026-09-{30 - number:02d}")

        result = self.tool.execute("due", {"as_of": "2026-10-04"})

        lines = result.message.split("\n")
        self.assertEqual(len(lines), 21)
        self.assertEqual(lines[-1], MORE_TASKS_MESSAGE)
        self.assertEqual(result.data, list(range(21, 1, -1)))
        self.assertEqual(result.recorded_message, "Listed due tasks.")

    def test_nothing_due_and_invalid_dates(self) -> None:
        self.add("later", "2026-10-05")
        self.assertEqual(
            self.tool.execute("due", {"as_of": "2026-10-04"}),
            ToolExecutionResult(
                True,
                "No tasks are due.",
                [],
                recorded_message="Listed due tasks.",
            ),
        )
        for as_of in ("2026-02-30", "today", "2026-10-04T00:00:00Z", None):
            with self.subTest(as_of=as_of):
                self.assertEqual(
                    self.tool.execute("due", {"as_of": as_of}),
                    ToolExecutionResult(False, AS_OF_DATE_MESSAGE),
                )


class TaskReadTests(_TaskToolTestCase):
    """Prove reading one task, and its content-free recorded line."""

    def test_a_task_is_shown_in_full(self) -> None:
        task_id = self.add(due="2026-10-09")

        self.assertEqual(
            self.tool.execute("read", {"id": task_id}),
            ToolExecutionResult(
                True,
                "Task 1\n"
                'Text: "buy milk"\n'
                "Due: Friday 2026-10-09, a calendar date with no time\n"
                "Status: open\n"
                f"Created at: {STAMPS[0]}\n"
                f"Updated at: {STAMPS[0]}\n"
                "Context: off",
                {"id": 1},
                recorded_message="Displayed task 1.",
            ),
        )

    def test_missing_invalid_and_failed_reads(self) -> None:
        self.assertEqual(
            self.tool.execute("read", {"id": 7}),
            ToolExecutionResult(False, "Task not found: 7."),
        )
        self.assertEqual(
            self.tool.execute("read", {"id": 2**63}),
            ToolExecutionResult(False, "Task not found: 9223372036854775808."),
        )
        for task_id in (0, -1, True, "1", None):
            with self.subTest(task_id=task_id):
                self.assertEqual(
                    self.tool.execute("read", {"id": task_id}),
                    ToolExecutionResult(False, TASK_ID_MESSAGE),
                )
        with patch.object(self.memory, "get_task", side_effect=RuntimeError("x")):
            self.assertEqual(
                self.tool.execute("read", {"id": 1}),
                ToolExecutionResult(False, READ_FAILED_MESSAGE),
            )


class TaskLifecycleTests(_TaskToolTestCase):
    """Prove complete, reopen, edit, reschedule, and delete."""

    def test_complete_and_reopen_change_the_status_only(self) -> None:
        task_id = self.add(due="2026-10-09")
        before = self.rows()[0]

        self.assertEqual(
            self.tool.execute("complete", {"id": task_id}),
            ToolExecutionResult(
                True,
                "Completed task 1.",
                {"id": 1, "status": "completed"},
            ),
        )
        completed = self.rows()[0]
        self.assertEqual(completed[3:7], ("completed", STAMPS[0], STAMPS[0], STAMPS[1]))
        self.assertEqual(completed[:3], before[:3])

        self.assertEqual(
            self.tool.execute("reopen", {"id": task_id}),
            ToolExecutionResult(True, "Reopened task 1.", {"id": 1, "status": "open"}),
        )
        self.assertEqual(self.rows()[0], before)

        # Completing again stamps a new time; the first is never restored.
        self.tool.execute("complete", {"id": task_id})
        self.assertEqual(self.rows()[0][6], STAMPS[2])

    def test_repeated_and_missing_transitions_change_nothing(self) -> None:
        task_id = self.add()
        self.assertEqual(
            self.tool.execute("reopen", {"id": task_id}),
            ToolExecutionResult(False, "Task 1 is already open, so nothing changed."),
        )
        self.tool.execute("complete", {"id": task_id})
        snapshot = self.rows()
        self.assertEqual(
            self.tool.execute("complete", {"id": task_id}),
            ToolExecutionResult(
                False,
                "Task 1 is already completed, so nothing changed.",
            ),
        )
        self.assertEqual(self.rows(), snapshot)
        for action in ("complete", "reopen"):
            with self.subTest(action=action):
                self.assertEqual(
                    self.tool.execute(action, {"id": 9}),
                    ToolExecutionResult(False, "Task not found: 9."),
                )
                self.assertEqual(
                    self.tool.execute(action, {"id": 0}),
                    ToolExecutionResult(False, TASK_ID_MESSAGE),
                )

    def test_an_overdue_task_stays_fully_usable(self) -> None:
        task_id = self.add(due="2001-01-01")
        for action, arguments in (
            ("read", {"id": task_id}),
            ("edit", {"id": task_id, "text": "buy oat milk"}),
            ("complete", {"id": task_id}),
            ("reopen", {"id": task_id}),
            ("delete", {"id": task_id}),
        ):
            with self.subTest(action=action):
                self.assertTrue(self.tool.execute(action, arguments).success)

    def test_edit_replaces_the_text_and_stamps_the_update(self) -> None:
        task_id = self.add(due="2026-10-09")

        self.assertEqual(
            self.tool.execute("edit", {"id": task_id, "text": "buy oat milk"}),
            ToolExecutionResult(
                True,
                'Edited task 1: "buy oat milk".',
                {"id": 1},
                recorded_message="Edited task 1.",
            ),
        )
        self.assertEqual(
            self.rows()[0],
            (1, "buy oat milk", "2026-10-09", "open", STAMPS[0], STAMPS[1], None, None),
        )

    def test_reschedule_replaces_or_clears_the_due_date(self) -> None:
        task_id = self.add(due="2026-10-09")

        self.assertEqual(
            self.tool.execute("reschedule", {"id": task_id, "due": "2026-10-12"}),
            ToolExecutionResult(
                True,
                "Rescheduled task 1: due Monday 2026-10-12.",
                {"id": 1},
                recorded_message="Rescheduled task 1.",
            ),
        )
        self.assertEqual(
            self.tool.execute("reschedule", {"id": task_id, "due": "none"}).message,
            "Rescheduled task 1: no due date.",
        )
        self.assertIsNone(self.rows()[0][2])
        for due in ("someday", "2026-02-30", None):
            with self.subTest(due=due):
                self.assertEqual(
                    self.tool.execute("reschedule", {"id": task_id, "due": due}),
                    ToolExecutionResult(False, DUE_DATE_MESSAGE),
                )

    def test_changes_to_a_missing_task_and_failures(self) -> None:
        self.assertEqual(
            self.tool.execute("edit", {"id": 4, "text": "x"}),
            ToolExecutionResult(False, "Task not found: 4."),
        )
        self.assertEqual(
            self.tool.execute("reschedule", {"id": 4, "due": "none"}),
            ToolExecutionResult(False, "Task not found: 4."),
        )
        self.assertEqual(
            self.tool.execute("edit", {"id": 4, "text": " x"}),
            ToolExecutionResult(False, TASK_TEXT_MESSAGE),
        )
        for name, action, arguments, message in (
            ("edit_task", "edit", {"id": 1, "text": "x"}, CHANGE_FAILED_MESSAGE),
            (
                "reschedule_task",
                "reschedule",
                {"id": 1, "due": "none"},
                CHANGE_FAILED_MESSAGE,
            ),
            ("complete_task", "complete", {"id": 1}, CHANGE_FAILED_MESSAGE),
            ("delete_task", "delete", {"id": 1}, DELETE_FAILED_MESSAGE),
            (
                "enable_task_context",
                "enable_context",
                {"id": 1},
                CONTEXT_FAILED_MESSAGE,
            ),
        ):
            with self.subTest(name=name):
                with patch.object(self.memory, name, side_effect=RuntimeError("x")):
                    self.assertEqual(
                        self.tool.execute(action, arguments),
                        ToolExecutionResult(False, message),
                    )

    def test_delete_removes_one_task_and_ids_are_never_reused(self) -> None:
        first = self.add("first")
        second = self.add("second")

        self.assertEqual(
            self.tool.execute("delete", {"id": second}),
            ToolExecutionResult(True, "Deleted task 2.", {"id": 2}),
        )
        self.assertEqual(
            self.tool.execute("delete", {"id": second}),
            ToolExecutionResult(False, "Task not found: 2."),
        )
        self.assertEqual(self.add("third"), 3)
        self.assertEqual([row[0] for row in self.rows()], [first, 3])


class TaskContextConsentTests(_TaskToolTestCase):
    """Prove consent is off by default and bound to the exact text and date."""

    def test_consent_is_enabled_only_for_the_pinned_identity(self) -> None:
        task_id = self.add(due="2026-10-09")
        binding = self.tool.confirmation_binding("enable_context", {"id": task_id})

        result = self.tool.execute(
            "enable_context",
            {"id": task_id, **binding.arguments},
        )

        self.assertEqual(
            result,
            ToolExecutionResult(
                True,
                "Enabled context for task 1.",
                {"id": 1, "context_enabled": True},
            ),
        )
        self.assertTrue(self.memory.get_task(task_id).context_enabled)
        self.assertEqual(self.rows()[0][7], task_digest("buy milk", "2026-10-09"))

    def test_a_stale_or_missing_identity_enables_nothing(self) -> None:
        task_id = self.add(due="2026-10-09")
        binding = self.tool.confirmation_binding("enable_context", {"id": task_id})
        self.tool.execute("reschedule", {"id": task_id, "due": "2026-10-10"})
        refusal = ToolExecutionResult(
            False,
            "Context was not enabled for task 1 because its text and due date "
            "could not be confirmed unchanged since confirmation was requested. "
            "Request confirmation again with: enable task context 1",
        )

        self.assertEqual(
            self.tool.execute("enable_context", {"id": task_id, **binding.arguments}),
            refusal,
        )
        self.assertEqual(self.tool.execute("enable_context", {"id": task_id}), refusal)
        self.assertIsNone(self.rows()[0][7])
        self.assertEqual(
            self.tool.execute("enable_context", {"id": 8, **binding.arguments}),
            ToolExecutionResult(False, "Task not found: 8."),
        )

    def test_edit_and_reschedule_turn_consent_off_and_say_so(self) -> None:
        task_id = self.add(due="2026-10-09")
        for action, arguments, changed in (
            ("edit", {"text": "buy oat milk"}, "text"),
            ("reschedule", {"due": "2026-10-10"}, "due date"),
        ):
            with self.subTest(action=action):
                binding = self.tool.confirmation_binding(
                    "enable_context",
                    {"id": task_id},
                )
                enabled = self.memory.enable_task_context(
                    task_id,
                    binding.arguments["text_digest"],
                )
                self.assertIs(enabled, ContextConsentOutcome.ENABLED)

                result = self.tool.execute(action, {"id": task_id, **arguments})

                self.assertTrue(result.message.endswith(
                    f"Context consent for task 1 was turned off because its "
                    f"{changed} changed."
                ))
                self.assertEqual(result.recorded_message.count(" "), 2)
                self.assertIsNone(self.rows()[0][7])

    def test_complete_and_reopen_keep_consent_as_it_was(self) -> None:
        task_id = self.add()
        binding = self.tool.confirmation_binding("enable_context", {"id": task_id})
        self.tool.execute("enable_context", {"id": task_id, **binding.arguments})

        self.tool.execute("complete", {"id": task_id})
        self.tool.execute("reopen", {"id": task_id})

        self.assertTrue(self.memory.get_task(task_id).context_enabled)

    def test_disable_turns_consent_off(self) -> None:
        task_id = self.add()
        binding = self.tool.confirmation_binding("enable_context", {"id": task_id})
        self.tool.execute("enable_context", {"id": task_id, **binding.arguments})

        self.assertEqual(
            self.tool.execute("disable_context", {"id": task_id}),
            ToolExecutionResult(
                True,
                "Disabled context for task 1.",
                {"id": 1, "context_enabled": False},
            ),
        )
        self.assertFalse(self.memory.get_task(task_id).context_enabled)
        self.assertEqual(
            self.tool.execute("disable_context", {"id": 5}),
            ToolExecutionResult(False, "Task not found: 5."),
        )


class TaskPrivacyTests(_TaskToolTestCase):
    """Prove no text or date reaches a recorded line or the log."""

    def test_recorded_lines_and_logs_carry_ids_and_fixed_phrases_only(self) -> None:
        due = "2031-02-03"
        task_id = self.add(TEXT_SENTINEL, due)
        results = [
            self.tool.execute("list", {"status": "open"}),
            self.tool.execute("due", {"as_of": "2031-02-03"}),
            self.tool.execute("read", {"id": task_id}),
            self.tool.execute("edit", {"id": task_id, "text": f"{TEXT_SENTINEL} 2"}),
            self.tool.execute("reschedule", {"id": task_id, "due": "2031-02-04"}),
            self.tool.execute("complete", {"id": task_id}),
            self.tool.execute("reopen", {"id": task_id}),
        ]
        binding = self.tool.confirmation_binding("enable_context", {"id": task_id})
        results.append(
            self.tool.execute("enable_context", {"id": task_id, **binding.arguments})
        )
        results.append(self.tool.execute("delete", {"id": task_id}))

        for result in results:
            with self.subTest(message=result.recorded_message):
                self.assertTrue(result.success)
                if result.recorded_message is not None:
                    self.assertNotIn(TEXT_SENTINEL, result.recorded_message)
                    self.assertNotIn("2031", result.recorded_message)
        logged = self.logged()
        for secret in (
            TEXT_SENTINEL,
            due,
            "2031-02-04",
            binding.arguments["text_digest"],
        ):
            self.assertNotIn(secret, logged)

    def test_the_tool_reads_no_clock_and_no_zone_data(self) -> None:
        tree = ast.parse(inspect.getsource(tasks_module))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {"now", "utcnow", "today", "monotonic"})
        self.assertFalse(
            imported
            & {
                "datetime",
                "time",
                "zoneinfo",
                "threading",
                "sched",
                "asyncio",
                "signal",
                "sqlite3",
                "core.time_resolution",
            }
        )

    def test_a_record_value_object_never_reaches_the_reply_unsettled(self) -> None:
        # A listing that is not a list of tasks is refused as a whole.
        with patch.object(self.memory, "list_tasks", return_value=[object()]):
            self.assertEqual(
                self.tool.execute("list", {"status": "open"}),
                ToolExecutionResult(False, LIST_FAILED_MESSAGE),
            )
        with patch.object(self.memory, "get_task", return_value=object()):
            self.assertEqual(
                self.tool.execute("read", {"id": 1}),
                ToolExecutionResult(False, READ_FAILED_MESSAGE),
            )
        self.assertIs(Task, tasks_module.Task)


if __name__ == "__main__":
    unittest.main()
