"""Reminder tool for Maria.

The tool adds, lists, queries, reads, completes, reopens, edits, reschedules,
and deletes the owner's reminders, and turns one reminder's context consent
on or off, through the memory layer. A reminder is one line of the owner's
text and one moment: the canonical local time, offset, and time zone the
owner approved, and the saved UTC instant they encode, which alone orders
reminders and selects due ones. A reminder never repeats, and Maria never
delivers, notifies, schedules, or polls anything: ``due reminders`` is a
query the owner asks.

Adding, editing, rescheduling, deleting, and both consent changes are
confirmation-required, and this tool's binding shows the owner exactly what
an approval covers before anything is stored: the exact text and resolved
time for an add, and the saved values, beside any new ones, for every
change to an existing reminder. Enabling consent pins the identity of
exactly the text and saved time shown. Listing, the due query, reading,
completing, and reopening are allowed, and complete and reopen change the
status only.

Every time a plan carries was resolved before the plan was built, at the
request's own reference instant, so this tool reads no clock. Showing a
saved reminder needs no time zone data: the saved values are always shown as
saved. When the saved zone's current rules give a different offset for the
saved local time, a fixed warning is added, and when the zone cannot be
loaded, a fixed note; neither ever fails an operation, rewrites a record, or
moves its instant. A reply that would show a reminder's text or time records
only a fixed, content-free line, and the log names ids and fixed phrases
only. Every failure is a fixed message.
"""

from __future__ import annotations

from collections.abc import Mapping
from logging import Logger
from typing import Any

from core.context import ContextConsentOutcome, quoted_for_owner, reminder_digest
from core.logger import get_logger
from core.memory import Memory
from core.task_records import (
    AS_OF_INSTANT_MESSAGE,
    REMIND_TIME_MESSAGE,
    REMINDER_ID_MESSAGE,
    REPEAT_NEVER,
    RULES_CHANGED_WARNING,
    RULES_UNCHECKED_NOTE,
    STATUS_COMPLETED,
    STATUS_OPEN,
    TASK_LIST_LIMIT,
    TASK_QUERY_LIMIT,
    TASK_STATUS_MESSAGE,
    TASK_TEXT_MESSAGE,
    RecordUpdate,
    Reminder,
    StatusChange,
    describe_remind_time,
    is_record_id,
    is_status,
    is_task_text,
    is_utc_instant,
    parse_remind_time,
)
from core.time_resolution import RuleCheck, check_rules
from core.tool_manager import ConfirmationBinding, ToolExecutionResult


ADD_FAILED_MESSAGE = "Unable to add the reminder."
LIST_FAILED_MESSAGE = "Unable to list reminders."
READ_FAILED_MESSAGE = "Unable to read the reminder."
CHANGE_FAILED_MESSAGE = "Unable to change the reminder."
DELETE_FAILED_MESSAGE = "Unable to delete the reminder."
CONTEXT_FAILED_MESSAGE = "Unable to change the reminder's context."
UNSUPPORTED_ACTION_MESSAGE = "Unsupported reminder action."
REPEAT_MESSAGE = "A reminder does not repeat; its repeat must be never."
NO_OPEN_REMINDERS_MESSAGE = "No open reminders."
NO_COMPLETED_REMINDERS_MESSAGE = "No completed reminders."
NO_DUE_REMINDERS_MESSAGE = "No reminders are due."
LISTED_RECORDS = {
    STATUS_OPEN: "Listed open reminders.",
    STATUS_COMPLETED: "Listed completed reminders.",
}
DUE_LISTED_RECORD = "Listed due reminders."
MORE_REMINDERS_MESSAGE = (
    f"Showing the first {TASK_LIST_LIMIT} reminders. More reminders exist."
)

# The compact markers a listing adds to one reminder, each explained once
# below the listing in the fixed sentence it stands for.
RULES_CHANGED_MARKER = "[rules changed]"
RULES_UNCHECKED_MARKER = "[rules unchecked]"
_MARKER_LEGENDS = (
    (RULES_CHANGED_MARKER, RULES_CHANGED_WARNING),
    (RULES_UNCHECKED_MARKER, RULES_UNCHECKED_NOTE),
)


def not_found_message(reminder_id: int) -> str:
    """Return the fixed reply for an id no reminder has."""
    return f"Reminder not found: {reminder_id}."


def rules_marker(remind_time: str) -> str | None:
    """Return a listing's marker for one saved time, or None when it agrees."""
    check = _rule_check(remind_time)
    if check is RuleCheck.CHANGED:
        return RULES_CHANGED_MARKER
    if check is RuleCheck.UNAVAILABLE:
        return RULES_UNCHECKED_MARKER
    return None


def rules_sentence(remind_time: str) -> str | None:
    """Return the fixed warning or note for one saved time, or None."""
    check = _rule_check(remind_time)
    if check is RuleCheck.CHANGED:
        return RULES_CHANGED_WARNING
    if check is RuleCheck.UNAVAILABLE:
        return RULES_UNCHECKED_NOTE
    return None


def reminder_line(reminder: Reminder) -> str:
    """Render one reminder as one listing line, from its saved values."""
    line = (
        f"{reminder.reminder_id}: {quoted_for_owner(reminder.text)}, "
        f"{describe_remind_time(reminder.remind_time, reminder.remind_utc)}"
    )
    if reminder.status == STATUS_COMPLETED:
        line = f"{line}, completed at {reminder.completed_at}"
    marker = rules_marker(reminder.remind_time)
    return line if marker is None else f"{line} {marker}"


def reminder_block(reminder: Reminder) -> str:
    """Render one reminder in full, from its saved values."""
    status = (
        f"completed at {reminder.completed_at}"
        if reminder.status == STATUS_COMPLETED
        else "open"
    )
    lines = [
        f"Reminder {reminder.reminder_id}",
        f"Text: {quoted_for_owner(reminder.text)}",
        f"Time: {describe_remind_time(reminder.remind_time, reminder.remind_utc)}",
        "Repeats: never",
        f"Status: {status}",
        f"Created at: {reminder.created_at}",
        f"Updated at: {reminder.updated_at}",
        f"Context: {'on' if reminder.context_enabled else 'off'}",
    ]
    sentence = rules_sentence(reminder.remind_time)
    if sentence is not None:
        lines.append(sentence)
    return "\n".join(lines)


class RemindersTool:
    """Add, list, query, read, change, and delete the owner's reminders."""

    name = "reminders"
    description = (
        "Adds, lists, reads, completes, reopens, edits, reschedules, and "
        "deletes reminders, answers which reminders are due, and turns one "
        "reminder's context consent on or off. It never delivers or notifies."
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

        An add shows the exact text and the resolved time the plan holds,
        with no read. Every change to an existing reminder reads it once and
        shows its saved values, with the rule-change warning or note when
        one applies, beside any new ones. Enabling consent pins the identity
        of exactly the text and saved time shown as ``text_digest``. Only
        the identity enters the plan; the shown text is the binding's
        preview, never recorded, logged, or sent to a model. Nothing is
        added when nothing can be shown, and an enable without an identity
        can never turn consent on.
        """
        if action == "create":
            text = arguments.get("text")
            when = _time_detail(arguments.get("when"))
            if not is_task_text(text) or when is None:
                return {}
            return ConfirmationBinding(
                {},
                "\n".join(
                    (
                        "This approval adds a reminder, shown quoted:",
                        f"Text: {quoted_for_owner(text)}",
                        f"Time: {when}",
                        "Repeats: never",
                    )
                ),
            )
        if action not in {"edit", "reschedule", "delete", "enable_context"}:
            return {}
        reminder_id = arguments.get("id")
        if not is_record_id(reminder_id):
            return {}
        try:
            reminder = self.memory.get_reminder(reminder_id)
        except (RuntimeError, ValueError):
            self.logger.warning(
                "Could not prepare approval for reminder %s.",
                reminder_id,
            )
            return {}
        if type(reminder) is not Reminder:
            return {}
        saved_time = describe_remind_time(reminder.remind_time, reminder.remind_utc)
        rules = rules_sentence(reminder.remind_time)
        notes = () if rules is None else (rules,)
        if action == "enable_context":
            return ConfirmationBinding(
                {"text_digest": reminder_digest(reminder.text, reminder.remind_time)},
                "\n".join(
                    (
                        f"This approval covers reminder {reminder_id} exactly as "
                        "it is stored now, shown quoted:",
                        f"Text: {quoted_for_owner(reminder.text)}",
                        f"Time: {saved_time}",
                        *notes,
                    )
                ),
            )
        if action == "delete":
            return ConfirmationBinding(
                {},
                "\n".join(
                    (
                        f"This approval deletes reminder {reminder_id}, shown "
                        "quoted:",
                        f"Text: {quoted_for_owner(reminder.text)}",
                        f"Time: {saved_time}",
                        *notes,
                    )
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
                        f"This approval replaces the text of reminder "
                        f"{reminder_id}, shown quoted:",
                        f"Time: {saved_time}",
                        *notes,
                        f"Now: {quoted_for_owner(reminder.text)}",
                        f"New: {quoted_for_owner(text)}",
                    )
                ),
            )
        new_time = _time_detail(arguments.get("when"))
        if new_time is None:
            return {}
        return ConfirmationBinding(
            {},
            "\n".join(
                (
                    f"This approval reschedules reminder {reminder_id}, shown "
                    "quoted:",
                    f"Text: {quoted_for_owner(reminder.text)}",
                    f"Now: {saved_time}",
                    *notes,
                    f"New: {new_time}",
                    "Repeats: never",
                )
            ),
        )

    def execute(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> ToolExecutionResult:
        """Execute a reminder action."""
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
        """Store exactly the approved text and time as a new reminder.

        The time is stored as approved even when its instant has passed by
        now; such a reminder is simply due at once.
        """
        text = arguments.get("text")
        if not is_task_text(text):
            return ToolExecutionResult(False, TASK_TEXT_MESSAGE)
        if arguments.get("repeat") != REPEAT_NEVER:
            return ToolExecutionResult(False, REPEAT_MESSAGE)
        when = arguments.get("when")
        parts = parse_remind_time(when)
        if parts is None:
            return ToolExecutionResult(False, REMIND_TIME_MESSAGE)
        try:
            reminder_id = self.memory.create_reminder(text, when, parts.instant_text)
        except (RuntimeError, ValueError):
            self.logger.warning("Reminder storage failed.")
            return ToolExecutionResult(False, ADD_FAILED_MESSAGE)
        self.logger.info("Added reminder %s through reminders tool.", reminder_id)
        return ToolExecutionResult(
            True,
            (
                f"Added reminder {reminder_id}: {quoted_for_owner(text)}, "
                f"{describe_remind_time(when, parts.instant_text)}. It does not "
                "repeat, Maria will not notify you when it is due, and its "
                "context consent is off."
            ),
            {"id": reminder_id},
            recorded_message=f"Added reminder {reminder_id}.",
        )

    def _list(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """List open or completed reminders, saying when more exist."""
        status = arguments.get("status")
        if not is_status(status):
            return ToolExecutionResult(False, TASK_STATUS_MESSAGE)
        try:
            reminders = self.memory.list_reminders(status, TASK_QUERY_LIMIT)
        except (RuntimeError, ValueError):
            self.logger.warning("Reminder listing failed.")
            return ToolExecutionResult(False, LIST_FAILED_MESSAGE)
        empty = (
            NO_OPEN_REMINDERS_MESSAGE
            if status == STATUS_OPEN
            else NO_COMPLETED_REMINDERS_MESSAGE
        )
        return self._listing(reminders, empty, LISTED_RECORDS[status], due=False)

    def _due(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """List open reminders whose saved instant is at or before the plan's."""
        as_of = arguments.get("as_of")
        if not is_utc_instant(as_of):
            return ToolExecutionResult(False, AS_OF_INSTANT_MESSAGE)
        try:
            reminders = self.memory.due_reminders(as_of, TASK_QUERY_LIMIT)
        except (RuntimeError, ValueError):
            self.logger.warning("Due reminder query failed.")
            return ToolExecutionResult(False, LIST_FAILED_MESSAGE)
        return self._listing(
            reminders,
            NO_DUE_REMINDERS_MESSAGE,
            DUE_LISTED_RECORD,
            due=True,
        )

    def _listing(
        self,
        reminders: object,
        empty: str,
        recorded: str,
        *,
        due: bool,
    ) -> ToolExecutionResult:
        """Return one listing's lines and its content-free recorded line."""
        if type(reminders) is not list or any(
            type(reminder) is not Reminder for reminder in reminders
        ):
            self.logger.warning("Reminder listing failed.")
            return ToolExecutionResult(False, LIST_FAILED_MESSAGE)
        if not reminders:
            return ToolExecutionResult(True, empty, [], recorded_message=recorded)
        visible = reminders[:TASK_LIST_LIMIT]
        lines = []
        for reminder in visible:
            line = reminder_line(reminder)
            lines.append(f"{line} (time reached)" if due else line)
        if len(reminders) > TASK_LIST_LIMIT:
            lines.append(MORE_REMINDERS_MESSAGE)
        shown = "\n".join(lines)
        for marker, legend in _MARKER_LEGENDS:
            if marker in shown:
                lines.append(f"{marker}: {legend}")
        self.logger.info("Listed reminders through reminders tool.")
        return ToolExecutionResult(
            True,
            "\n".join(lines),
            [reminder.reminder_id for reminder in visible],
            recorded_message=recorded,
        )

    def _read(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Show one reminder; history records only its id."""
        reminder_id = arguments.get("id")
        if not is_record_id(reminder_id):
            return ToolExecutionResult(False, REMINDER_ID_MESSAGE)
        try:
            reminder = self.memory.get_reminder(reminder_id)
        except (RuntimeError, ValueError):
            self.logger.warning("Reminder read failed.")
            return ToolExecutionResult(False, READ_FAILED_MESSAGE)
        if reminder is None:
            return ToolExecutionResult(False, not_found_message(reminder_id))
        if type(reminder) is not Reminder:
            self.logger.warning("Reminder read failed.")
            return ToolExecutionResult(False, READ_FAILED_MESSAGE)
        self.logger.info("Displayed reminder %s through reminders tool.", reminder_id)
        return ToolExecutionResult(
            True,
            reminder_block(reminder),
            {"id": reminder_id},
            recorded_message=f"Displayed reminder {reminder_id}.",
        )

    def _set_status(
        self,
        arguments: Mapping[str, Any],
        *,
        completed: bool,
    ) -> ToolExecutionResult:
        """Complete or reopen one reminder, changing its status only."""
        reminder_id = arguments.get("id")
        if not is_record_id(reminder_id):
            return ToolExecutionResult(False, REMINDER_ID_MESSAGE)
        try:
            if completed:
                outcome = self.memory.complete_reminder(reminder_id)
            else:
                outcome = self.memory.reopen_reminder(reminder_id)
        except (RuntimeError, ValueError):
            self.logger.warning("Reminder status change failed.")
            return ToolExecutionResult(False, CHANGE_FAILED_MESSAGE)
        if outcome is StatusChange.NOT_FOUND:
            return ToolExecutionResult(False, not_found_message(reminder_id))
        if outcome is StatusChange.UNCHANGED:
            state = "completed" if completed else "open"
            return ToolExecutionResult(
                False,
                f"Reminder {reminder_id} is already {state}, so nothing changed.",
            )
        if outcome is not StatusChange.CHANGED:
            return ToolExecutionResult(False, CHANGE_FAILED_MESSAGE)
        verb = "Completed" if completed else "Reopened"
        return ToolExecutionResult(
            True,
            f"{verb} reminder {reminder_id}.",
            {
                "id": reminder_id,
                "status": STATUS_COMPLETED if completed else STATUS_OPEN,
            },
        )

    def _edit(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Replace one reminder's text by id, turning its consent off."""
        reminder_id = arguments.get("id")
        if not is_record_id(reminder_id):
            return ToolExecutionResult(False, REMINDER_ID_MESSAGE)
        text = arguments.get("text")
        if not is_task_text(text):
            return ToolExecutionResult(False, TASK_TEXT_MESSAGE)
        try:
            result = self.memory.edit_reminder(reminder_id, text)
        except (RuntimeError, ValueError):
            self.logger.warning("Reminder edit failed.")
            return ToolExecutionResult(False, CHANGE_FAILED_MESSAGE)
        return self._changed(
            reminder_id,
            result,
            f"Edited reminder {reminder_id}: {quoted_for_owner(text)}.",
            f"Edited reminder {reminder_id}.",
            "text",
        )

    def _reschedule(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Replace one reminder's saved time by id, turning its consent off."""
        reminder_id = arguments.get("id")
        if not is_record_id(reminder_id):
            return ToolExecutionResult(False, REMINDER_ID_MESSAGE)
        if arguments.get("repeat") != REPEAT_NEVER:
            return ToolExecutionResult(False, REPEAT_MESSAGE)
        when = arguments.get("when")
        parts = parse_remind_time(when)
        if parts is None:
            return ToolExecutionResult(False, REMIND_TIME_MESSAGE)
        try:
            result = self.memory.reschedule_reminder(
                reminder_id,
                when,
                parts.instant_text,
            )
        except (RuntimeError, ValueError):
            self.logger.warning("Reminder reschedule failed.")
            return ToolExecutionResult(False, CHANGE_FAILED_MESSAGE)
        return self._changed(
            reminder_id,
            result,
            (
                f"Rescheduled reminder {reminder_id}: "
                f"{describe_remind_time(when, parts.instant_text)}."
            ),
            f"Rescheduled reminder {reminder_id}.",
            "time",
        )

    def _changed(
        self,
        reminder_id: int,
        result: object,
        shown: str,
        recorded: str,
        changed: str,
    ) -> ToolExecutionResult:
        """Report an edit or a reschedule, saying when consent was turned off."""
        if type(result) is not RecordUpdate:
            return ToolExecutionResult(False, CHANGE_FAILED_MESSAGE)
        if not result.updated:
            return ToolExecutionResult(False, not_found_message(reminder_id))
        self.logger.info("Changed reminder %s through reminders tool.", reminder_id)
        if result.context_consent_cleared:
            # Consent covered the text and saved time the owner approved,
            # and this change replaced one of them, so it is off now.
            shown = (
                f"{shown} Context consent for reminder {reminder_id} was turned "
                f"off because its {changed} changed."
            )
        return ToolExecutionResult(
            True,
            shown,
            {"id": reminder_id},
            recorded_message=recorded,
        )

    def _delete(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Delete one reminder by id."""
        reminder_id = arguments.get("id")
        if not is_record_id(reminder_id):
            return ToolExecutionResult(False, REMINDER_ID_MESSAGE)
        try:
            deleted = self.memory.delete_reminder(reminder_id)
        except (RuntimeError, ValueError):
            self.logger.warning("Reminder deletion failed.")
            return ToolExecutionResult(False, DELETE_FAILED_MESSAGE)
        if deleted is not True:
            return ToolExecutionResult(False, not_found_message(reminder_id))
        return ToolExecutionResult(
            True,
            f"Deleted reminder {reminder_id}.",
            {"id": reminder_id},
        )

    def _set_context(
        self,
        arguments: Mapping[str, Any],
        *,
        enabled: bool,
    ) -> ToolExecutionResult:
        """Turn one reminder's context consent on or off by id.

        Enabling is bound to the identity pinned when confirmation was
        offered; if the reminder's text or saved time is no longer exactly
        what was shown, or no identity was pinned, nothing changes and the
        owner is told to ask again. Disabling only narrows what can be sent.
        """
        reminder_id = arguments.get("id")
        if not is_record_id(reminder_id):
            return ToolExecutionResult(False, REMINDER_ID_MESSAGE)
        try:
            if enabled:
                outcome = self.memory.enable_reminder_context(
                    reminder_id,
                    arguments.get("text_digest"),
                )
                matched = outcome is not ContextConsentOutcome.NOT_FOUND
            else:
                outcome = None
                matched = self.memory.disable_reminder_context(reminder_id)
        except (RuntimeError, ValueError):
            self.logger.warning("Reminder context change failed.")
            return ToolExecutionResult(False, CONTEXT_FAILED_MESSAGE)
        if matched is not True:
            return ToolExecutionResult(False, not_found_message(reminder_id))
        if outcome is ContextConsentOutcome.TARGET_CHANGED:
            return ToolExecutionResult(
                False,
                f"Context was not enabled for reminder {reminder_id} because its "
                "text and time could not be confirmed unchanged since "
                "confirmation was requested. Request confirmation again with: "
                f"enable reminder context {reminder_id}",
            )
        verb = "Enabled" if enabled else "Disabled"
        return ToolExecutionResult(
            True,
            f"{verb} context for reminder {reminder_id}.",
            {"id": reminder_id, "context_enabled": enabled},
        )


def _time_detail(when: object) -> str | None:
    """Return a canonical time as the owner reads it, or None if it is not one."""
    parts = parse_remind_time(when)
    if parts is None:
        return None
    return describe_remind_time(when, parts.instant_text)


def _rule_check(remind_time: str) -> RuleCheck:
    """Check one saved time against its own zone's current rules.

    The check is best effort: a time that cannot even be read back is
    reported as unchecked, never as an error.
    """
    parts = parse_remind_time(remind_time)
    if parts is None:
        return RuleCheck.UNAVAILABLE
    return check_rules(parts.local, parts.offset, parts.key)


def build_tool(memory: Memory, **_: Any) -> RemindersTool:
    """Build a reminder tool instance."""
    return RemindersTool(memory=memory)
