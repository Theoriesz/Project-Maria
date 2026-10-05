"""Task tool for Maria.

The tool adds, lists, queries, reads, completes, reopens, edits, reschedules,
and deletes the owner's tasks, and turns one task's context consent on or
off, through the memory layer. A task is one line of the owner's text with an
optional calendar due date, a date with no time or zone.

Adding, editing, rescheduling, deleting, and both consent changes are
confirmation-required, and this tool's binding shows the owner exactly what
an approval covers before anything is stored: the exact text and the
resolved due date for an add, the saved and the new values for an edit or a
reschedule, and the saved values for a deletion. Enabling consent pins the
identity of exactly the text and due date shown. Listing, the due query,
reading, completing, and reopening are allowed. Complete and reopen change
the status only: complete stamps the time of that completion, reopen clears
it, and an earlier completion time is never restored.

Every due date a plan carries was resolved before the plan was built, at the
request's own reference instant, so this tool reads no clock and needs no
time zone data. A reply that would show a task's text or date records only a
fixed, content-free line in interaction history, and the log names ids and
fixed phrases only. Every failure is a fixed message.
"""

from __future__ import annotations

from collections.abc import Mapping
from logging import Logger
from typing import Any

from core.context import ContextConsentOutcome, quoted_for_owner, task_digest
from core.logger import get_logger
from core.memory import Memory
from core.task_records import (
    AS_OF_DATE_MESSAGE,
    DUE_DATE_MESSAGE,
    NO_DUE_DATE,
    STATUS_COMPLETED,
    STATUS_OPEN,
    TASK_ID_MESSAGE,
    TASK_LIST_LIMIT,
    TASK_QUERY_LIMIT,
    TASK_STATUS_MESSAGE,
    TASK_TEXT_MESSAGE,
    RecordUpdate,
    StatusChange,
    Task,
    describe_due_date,
    is_due_date,
    is_record_id,
    is_status,
    is_task_text,
)
from core.tool_manager import ConfirmationBinding, ToolExecutionResult


ADD_FAILED_MESSAGE = "Unable to add the task."
LIST_FAILED_MESSAGE = "Unable to list tasks."
READ_FAILED_MESSAGE = "Unable to read the task."
CHANGE_FAILED_MESSAGE = "Unable to change the task."
DELETE_FAILED_MESSAGE = "Unable to delete the task."
CONTEXT_FAILED_MESSAGE = "Unable to change the task's context."
UNSUPPORTED_ACTION_MESSAGE = "Unsupported task action."
NO_OPEN_TASKS_MESSAGE = "No open tasks."
NO_COMPLETED_TASKS_MESSAGE = "No completed tasks."
NO_DUE_TASKS_MESSAGE = "No tasks are due."
LISTED_RECORDS = {
    STATUS_OPEN: "Listed open tasks.",
    STATUS_COMPLETED: "Listed completed tasks.",
}
DUE_LISTED_RECORD = "Listed due tasks."
MORE_TASKS_MESSAGE = (
    f"Showing the first {TASK_LIST_LIMIT} tasks. More tasks exist."
)


def not_found_message(task_id: int) -> str:
    """Return the fixed reply for an id no task has."""
    return f"Task not found: {task_id}."


def task_line(task: Task) -> str:
    """Render one task as one listing line, its text quoted."""
    if task.status == STATUS_COMPLETED:
        return (
            f"{task.task_id}: {quoted_for_owner(task.text)}, completed at "
            f"{task.completed_at}"
        )
    return (
        f"{task.task_id}: {quoted_for_owner(task.text)}, "
        f"{describe_due_date(task.due_date)}"
    )


def task_block(task: Task) -> str:
    """Render one task in full, its text quoted on one line."""
    status = (
        f"completed at {task.completed_at}"
        if task.status == STATUS_COMPLETED
        else "open"
    )
    return "\n".join(
        (
            f"Task {task.task_id}",
            f"Text: {quoted_for_owner(task.text)}",
            f"Due: {_due_detail(task.due_date)}",
            f"Status: {status}",
            f"Created at: {task.created_at}",
            f"Updated at: {task.updated_at}",
            f"Context: {'on' if task.context_enabled else 'off'}",
        )
    )


class TasksTool:
    """Add, list, query, read, change, and delete the owner's tasks."""

    name = "tasks"
    description = (
        "Adds, lists, reads, completes, reopens, edits, reschedules, and "
        "deletes tasks, answers which tasks are due, and turns one task's "
        "context consent on or off."
    )
    actions = frozenset(
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
    )

    def __init__(self, memory: Memory, logger: Logger | None = None) -> None:
        self.memory = memory
        self.logger = logger or get_logger(__name__)

    def confirmation_binding(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any] | ConfirmationBinding:
        """Show the owner exactly what an approval covers, before storage.

        An add shows the exact text and resolved due date the plan holds,
        with no read. An edit, a reschedule, and a deletion read the task
        once and show its saved values beside any new ones. Enabling consent
        reads the task once, shows it, and pins the identity of exactly that
        text and due date as ``text_digest``; at execution consent is turned
        on only while the task is still exactly that. Only the identity
        enters the plan: the shown text is the binding's preview, which is
        never recorded, logged, or sent to a model. Nothing is added when
        nothing can be shown, and an enable without an identity can never
        turn consent on.
        """
        if action == "create":
            text = arguments.get("text")
            due = arguments.get("due")
            if not is_task_text(text) or not (due is None or is_due_date(due)):
                return {}
            return ConfirmationBinding(
                {},
                "\n".join(
                    (
                        "This approval adds a task, shown quoted:",
                        f"Text: {quoted_for_owner(text)}",
                        f"Due: {_due_detail(due)}",
                    )
                ),
            )
        if action not in {"edit", "reschedule", "delete", "enable_context"}:
            return {}
        task_id = arguments.get("id")
        if not is_record_id(task_id):
            return {}
        try:
            task = self.memory.get_task(task_id)
        except (RuntimeError, ValueError):
            self.logger.warning("Could not prepare approval for task %s.", task_id)
            return {}
        if type(task) is not Task:
            return {}
        saved = (
            f"Text: {quoted_for_owner(task.text)}",
            f"Due: {_due_detail(task.due_date)}",
        )
        if action == "enable_context":
            return ConfirmationBinding(
                {"text_digest": task_digest(task.text, task.due_date)},
                "\n".join(
                    (
                        f"This approval covers task {task_id} exactly as it is "
                        "stored now, shown quoted:",
                        *saved,
                    )
                ),
            )
        if action == "delete":
            return ConfirmationBinding(
                {},
                "\n".join(
                    (f"This approval deletes task {task_id}, shown quoted:", *saved)
                ),
            )
        if action == "edit":
            text = arguments.get("text")
            if not is_task_text(text):
                return {}
            return ConfirmationBinding(
                {},
                "\n".join(
                    (
                        f"This approval replaces the text of task {task_id}, "
                        "shown quoted:",
                        f"Now: {quoted_for_owner(task.text)}",
                        f"New: {quoted_for_owner(text)}",
                    )
                ),
            )
        due = arguments.get("due")
        if not (due == NO_DUE_DATE or is_due_date(due)):
            return {}
        return ConfirmationBinding(
            {},
            "\n".join(
                (
                    f"This approval reschedules task {task_id}, shown quoted:",
                    f"Text: {quoted_for_owner(task.text)}",
                    f"Now: {_due_detail(task.due_date)}",
                    f"New: {_due_detail(None if due == NO_DUE_DATE else due)}",
                )
            ),
        )

    def execute(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> ToolExecutionResult:
        """Execute a task action."""
        if action == "create":
            return self._create(arguments)
        if action == "list":
            return self._list(arguments)
        if action == "due":
            return self._due(arguments)
        if action == "read":
            return self._read(arguments)
        if action == "complete":
            return self._set_status(arguments, completed=True)
        if action == "reopen":
            return self._set_status(arguments, completed=False)
        if action == "edit":
            return self._edit(arguments)
        if action == "reschedule":
            return self._reschedule(arguments)
        if action == "delete":
            return self._delete(arguments)
        if action == "enable_context":
            return self._set_context(arguments, enabled=True)
        if action == "disable_context":
            return self._set_context(arguments, enabled=False)
        return ToolExecutionResult(False, UNSUPPORTED_ACTION_MESSAGE)

    def _create(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Store exactly the approved text and due date as a new task."""
        text = arguments.get("text")
        due = arguments.get("due")
        if not is_task_text(text):
            return ToolExecutionResult(False, TASK_TEXT_MESSAGE)
        if due is not None and not is_due_date(due):
            return ToolExecutionResult(False, DUE_DATE_MESSAGE)
        try:
            task_id = self.memory.create_task(text, due)
        except (RuntimeError, ValueError):
            self.logger.warning("Task storage failed.")
            return ToolExecutionResult(False, ADD_FAILED_MESSAGE)
        self.logger.info("Added task %s through tasks tool.", task_id)
        return ToolExecutionResult(
            True,
            (
                f"Added task {task_id}: {quoted_for_owner(text)}, "
                f"{describe_due_date(due)}. Its context consent is off."
            ),
            {"id": task_id},
            recorded_message=f"Added task {task_id}.",
        )

    def _list(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """List open or completed tasks, saying when more exist."""
        status = arguments.get("status")
        if not is_status(status):
            return ToolExecutionResult(False, TASK_STATUS_MESSAGE)
        try:
            tasks = self.memory.list_tasks(status, TASK_QUERY_LIMIT)
        except (RuntimeError, ValueError):
            self.logger.warning("Task listing failed.")
            return ToolExecutionResult(False, LIST_FAILED_MESSAGE)
        empty = (
            NO_OPEN_TASKS_MESSAGE
            if status == STATUS_OPEN
            else NO_COMPLETED_TASKS_MESSAGE
        )
        return self._listing(tasks, empty, LISTED_RECORDS[status], due_label=False)

    def _due(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """List open tasks due on or before the plan's local date."""
        as_of = arguments.get("as_of")
        if not is_due_date(as_of):
            return ToolExecutionResult(False, AS_OF_DATE_MESSAGE)
        try:
            tasks = self.memory.due_tasks(as_of, TASK_QUERY_LIMIT)
        except (RuntimeError, ValueError):
            self.logger.warning("Due task query failed.")
            return ToolExecutionResult(False, LIST_FAILED_MESSAGE)
        return self._listing(
            tasks,
            NO_DUE_TASKS_MESSAGE,
            DUE_LISTED_RECORD,
            due_label=True,
            as_of=as_of,
        )

    def _listing(
        self,
        tasks: object,
        empty: str,
        recorded: str,
        *,
        due_label: bool,
        as_of: str | None = None,
    ) -> ToolExecutionResult:
        """Return one listing's lines and its content-free recorded line."""
        if type(tasks) is not list or any(type(task) is not Task for task in tasks):
            self.logger.warning("Task listing failed.")
            return ToolExecutionResult(False, LIST_FAILED_MESSAGE)
        if not tasks:
            return ToolExecutionResult(True, empty, [], recorded_message=recorded)
        visible = tasks[:TASK_LIST_LIMIT]
        lines = []
        for task in visible:
            line = task_line(task)
            if due_label:
                label = "due today" if task.due_date == as_of else "overdue"
                line = f"{line} ({label})"
            lines.append(line)
        if len(tasks) > TASK_LIST_LIMIT:
            lines.append(MORE_TASKS_MESSAGE)
        self.logger.info("Listed tasks through tasks tool.")
        return ToolExecutionResult(
            True,
            "\n".join(lines),
            [task.task_id for task in visible],
            recorded_message=recorded,
        )

    def _read(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Show one task; history records only its id."""
        task_id = arguments.get("id")
        if not is_record_id(task_id):
            return ToolExecutionResult(False, TASK_ID_MESSAGE)
        try:
            task = self.memory.get_task(task_id)
        except (RuntimeError, ValueError):
            self.logger.warning("Task read failed.")
            return ToolExecutionResult(False, READ_FAILED_MESSAGE)
        if task is None:
            return ToolExecutionResult(False, not_found_message(task_id))
        if type(task) is not Task:
            self.logger.warning("Task read failed.")
            return ToolExecutionResult(False, READ_FAILED_MESSAGE)
        self.logger.info("Displayed task %s through tasks tool.", task_id)
        return ToolExecutionResult(
            True,
            task_block(task),
            {"id": task_id},
            recorded_message=f"Displayed task {task_id}.",
        )

    def _set_status(
        self,
        arguments: Mapping[str, Any],
        *,
        completed: bool,
    ) -> ToolExecutionResult:
        """Complete or reopen one task, changing its status only."""
        task_id = arguments.get("id")
        if not is_record_id(task_id):
            return ToolExecutionResult(False, TASK_ID_MESSAGE)
        try:
            if completed:
                outcome = self.memory.complete_task(task_id)
            else:
                outcome = self.memory.reopen_task(task_id)
        except (RuntimeError, ValueError):
            self.logger.warning("Task status change failed.")
            return ToolExecutionResult(False, CHANGE_FAILED_MESSAGE)
        if outcome is StatusChange.NOT_FOUND:
            return ToolExecutionResult(False, not_found_message(task_id))
        if outcome is StatusChange.UNCHANGED:
            state = "completed" if completed else "open"
            return ToolExecutionResult(
                False,
                f"Task {task_id} is already {state}, so nothing changed.",
            )
        if outcome is not StatusChange.CHANGED:
            return ToolExecutionResult(False, CHANGE_FAILED_MESSAGE)
        verb = "Completed" if completed else "Reopened"
        return ToolExecutionResult(
            True,
            f"{verb} task {task_id}.",
            {"id": task_id, "status": STATUS_COMPLETED if completed else STATUS_OPEN},
        )

    def _edit(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Replace one task's text by id, turning its consent off."""
        task_id = arguments.get("id")
        if not is_record_id(task_id):
            return ToolExecutionResult(False, TASK_ID_MESSAGE)
        text = arguments.get("text")
        if not is_task_text(text):
            return ToolExecutionResult(False, TASK_TEXT_MESSAGE)
        try:
            result = self.memory.edit_task(task_id, text)
        except (RuntimeError, ValueError):
            self.logger.warning("Task edit failed.")
            return ToolExecutionResult(False, CHANGE_FAILED_MESSAGE)
        return self._changed(
            task_id,
            result,
            f"Edited task {task_id}: {quoted_for_owner(text)}.",
            f"Edited task {task_id}.",
            "text",
        )

    def _reschedule(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Replace or clear one task's due date by id, turning its consent off."""
        task_id = arguments.get("id")
        if not is_record_id(task_id):
            return ToolExecutionResult(False, TASK_ID_MESSAGE)
        due = arguments.get("due")
        if not (due == NO_DUE_DATE or is_due_date(due)):
            return ToolExecutionResult(False, DUE_DATE_MESSAGE)
        due_date = None if due == NO_DUE_DATE else due
        try:
            result = self.memory.reschedule_task(task_id, due_date)
        except (RuntimeError, ValueError):
            self.logger.warning("Task reschedule failed.")
            return ToolExecutionResult(False, CHANGE_FAILED_MESSAGE)
        return self._changed(
            task_id,
            result,
            f"Rescheduled task {task_id}: {describe_due_date(due_date)}.",
            f"Rescheduled task {task_id}.",
            "due date",
        )

    def _changed(
        self,
        task_id: int,
        result: object,
        shown: str,
        recorded: str,
        changed: str,
    ) -> ToolExecutionResult:
        """Report an edit or a reschedule, saying when consent was turned off."""
        if type(result) is not RecordUpdate:
            return ToolExecutionResult(False, CHANGE_FAILED_MESSAGE)
        if not result.updated:
            return ToolExecutionResult(False, not_found_message(task_id))
        self.logger.info("Changed task %s through tasks tool.", task_id)
        if result.context_consent_cleared:
            # Consent covered the text and due date the owner approved, and
            # this change replaced one of them, so it is off now.
            shown = (
                f"{shown} Context consent for task {task_id} was turned off "
                f"because its {changed} changed."
            )
        return ToolExecutionResult(
            True,
            shown,
            {"id": task_id},
            recorded_message=recorded,
        )

    def _delete(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Delete one task by id."""
        task_id = arguments.get("id")
        if not is_record_id(task_id):
            return ToolExecutionResult(False, TASK_ID_MESSAGE)
        try:
            deleted = self.memory.delete_task(task_id)
        except (RuntimeError, ValueError):
            self.logger.warning("Task deletion failed.")
            return ToolExecutionResult(False, DELETE_FAILED_MESSAGE)
        if deleted is not True:
            return ToolExecutionResult(False, not_found_message(task_id))
        return ToolExecutionResult(True, f"Deleted task {task_id}.", {"id": task_id})

    def _set_context(
        self,
        arguments: Mapping[str, Any],
        *,
        enabled: bool,
    ) -> ToolExecutionResult:
        """Turn one task's context consent on or off by id.

        Enabling is bound to the identity pinned when confirmation was
        offered; if the task's text or due date is no longer exactly what
        was shown, or no identity was pinned, nothing changes and the owner
        is told to ask again. Disabling only narrows what can be sent.
        """
        task_id = arguments.get("id")
        if not is_record_id(task_id):
            return ToolExecutionResult(False, TASK_ID_MESSAGE)
        try:
            if enabled:
                outcome = self.memory.enable_task_context(
                    task_id,
                    arguments.get("text_digest"),
                )
                matched = outcome is not ContextConsentOutcome.NOT_FOUND
            else:
                outcome = None
                matched = self.memory.disable_task_context(task_id)
        except (RuntimeError, ValueError):
            self.logger.warning("Task context change failed.")
            return ToolExecutionResult(False, CONTEXT_FAILED_MESSAGE)
        if matched is not True:
            return ToolExecutionResult(False, not_found_message(task_id))
        if outcome is ContextConsentOutcome.TARGET_CHANGED:
            return ToolExecutionResult(
                False,
                f"Context was not enabled for task {task_id} because its text "
                "and due date could not be confirmed unchanged since "
                "confirmation was requested. Request confirmation again with: "
                f"enable task context {task_id}",
            )
        verb = "Enabled" if enabled else "Disabled"
        return ToolExecutionResult(
            True,
            f"{verb} context for task {task_id}.",
            {"id": task_id, "context_enabled": enabled},
        )


def _due_detail(due_date: str | None) -> str:
    """Return a due date for a preview or a reading, saying it has no time."""
    if due_date is None:
        return "none"
    weekday_and_date = describe_due_date(due_date)[len("due ") :]
    return f"{weekday_and_date}, a calendar date with no time"


def build_tool(memory: Memory, **_: Any) -> TasksTool:
    """Build a task tool instance."""
    return TasksTool(memory=memory)
