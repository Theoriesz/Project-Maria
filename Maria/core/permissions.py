"""Permission decisions for Maria actions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from logging import Logger
from types import MappingProxyType

from core.logger import get_logger


MANDATORY_CONFIRMATION_ACTIONS = frozenset(
    {
        "audit.delete",
        "conversation_summaries.create",
        "conversation_summaries.edit",
        "conversation_summaries.delete",
        "conversation_summaries.enable_context",
        "conversation_summaries.disable_context",
        "conversations.delete",
        "filesystem.rename",
        "filesystem.move",
        "history.delete",
        "notes.retitle",
        "notes.edit",
        "notes.delete",
        "notes.enable_context",
        "notes.disable_context",
        "memory.update",
        "memory.forget",
        "memory.enable_context",
        "memory.disable_context",
        "projects.retitle",
        "projects.delete",
        "projects.activate",
        "projects.add_fact",
        "projects.edit_fact",
        "projects.delete_fact",
        "reminders.create",
        "reminders.edit",
        "reminders.reschedule",
        "reminders.delete",
        "reminders.enable_context",
        "reminders.disable_context",
        "reminders.enable_delivery",
        "reminders.disable_delivery",
        "tasks.create",
        "tasks.edit",
        "tasks.reschedule",
        "tasks.delete",
        "tasks.enable_context",
        "tasks.disable_context",
    }
)

# What the owner is told, beyond the standard sentence, before approving an
# action whose consequence is not obvious from its name, in a deterministic
# confirmation and a model plan's approval alike. Each notice is fixed text
# and interpolates nothing, so no stored content can reach it.
CONFIRMATION_NOTICES = MappingProxyType(
    {
        "audit.delete": (
            "Deleting an audit summary removes it from Maria's database. It "
            "does not undo the action, and Maria's operational log may still "
            "show that a model-proposed action ran."
        ),
        "conversation_summaries.create": (
            "Storing this summary keeps text your local reasoning model wrote, "
            "which can repeat anything said in this session, including text "
            "from memories or notes it was given. Its context consent starts "
            "off, and Maria does not check it for passwords or other secrets."
        ),
        "conversation_summaries.enable_context": (
            "Once enabled, this summary's entire text may be sent to your "
            "local reasoning model on a later conversational turn when a word "
            "of four or more characters from your request appears literally "
            "in it, within Maria's context limits. Maria does not check the "
            "summary for passwords or other secrets, and enabling it does not "
            "mean it contains none."
        ),
        "memory.enable_context": (
            "Once enabled, this memory may be sent to your local reasoning "
            "model on any conversational turn, within Maria's memory context "
            "limits. Maria does not choose memories by relevance."
        ),
        "notes.enable_context": (
            "Once enabled, this note's entire title and body may be sent to "
            "your local reasoning model on a later conversational turn when a "
            "word of four or more characters from your request appears "
            "literally in its title or its body, within Maria's context "
            "limits. Maria does not check the note for passwords or other "
            "secrets, and enabling it does not mean it contains none."
        ),
        "projects.activate": (
            "While a project is active, each of its facts may be sent to your "
            "local reasoning model on every conversational turn, within "
            "Maria's memory context limits, and Maria can read the project's "
            "files when you ask with project commands or @ references; she "
            "reads none on her own. The project stays active, also after "
            "Maria restarts, until you deactivate it or activate another "
            "project."
        ),
        "projects.add_fact": (
            "While its project is active, this fact may be sent to your local "
            "reasoning model on every conversational turn, within Maria's "
            "memory context limits, so it is not always sent. Maria does not "
            "check the fact for passwords or other secrets, and approving it "
            "does not mean it contains none."
        ),
        "projects.edit_fact": (
            "While its project is active, this fact may be sent to your local "
            "reasoning model on every conversational turn, within Maria's "
            "memory context limits, so it is not always sent. Maria does not "
            "check the fact for passwords or other secrets, and approving it "
            "does not mean it contains none."
        ),
        "reminders.create": (
            "Maria shows a reminder at most once when it is due, and only "
            "while reminder delivery is turned on and able to run; it does "
            "not repeat. Type due reminders to see the reminders whose time "
            "has come."
        ),
        "reminders.disable_delivery": (
            "Turning reminder delivery off stops Maria from starting any new "
            "reminder notice from the moment it is saved. A notice Maria has "
            "already started may still appear and be recorded, and a message "
            "box already on screen stays until you close it."
        ),
        "reminders.enable_context": (
            "Once enabled, this reminder's text and saved time may be sent to "
            "your local reasoning model on a conversational turn while it is "
            "open, after your enabled memories and within Maria's memory "
            "context limits, so it is not always sent. Maria does not check "
            "the reminder for passwords or other secrets, and enabling it does "
            "not mean it contains none."
        ),
        "reminders.enable_delivery": (
            "Once enabled, Maria shows each due reminder at most once: in the "
            "desktop window or the command line while you use them, and, "
            "while Maria is closed, as a Windows message box if you have "
            "installed the MariaReminderDelivery task, which Maria cannot see "
            "or install. It can show a reminder again only after you move it "
            "to a different time, even back to an earlier one. A message box "
            "shows the reminder's text to anyone at your unlocked screen. Open "
            "reminders whose time has passed and that were not shown since "
            "they were created or last moved are shown too, five at a time, "
            "and marked overdue when more than fifteen minutes late."
        ),
        "tasks.enable_context": (
            "Once enabled, this task's text and due date may be sent to your "
            "local reasoning model on a conversational turn while it is open, "
            "after your enabled memories and within Maria's memory context "
            "limits, so it is not always sent. Maria does not check the task "
            "for passwords or other secrets, and enabling it does not mean it "
            "contains none."
        ),
    }
)

MANDATORY_DENIED_ACTIONS = frozenset(
    {
        "filesystem.delete",
        "filesystem.write",
    }
)


class PermissionDecision(str, Enum):
    """Possible permission outcomes."""

    ALLOWED = "allowed"
    REQUIRES_CONFIRMATION = "requires_confirmation"
    DENIED = "denied"


@dataclass(frozen=True)
class PermissionResult:
    """Permission result for one requested action."""

    decision: PermissionDecision
    reason: str


class PermissionManager:
    """Decide whether a tool action may run."""

    def __init__(
        self,
        confirmation_required: frozenset[str],
        denied_actions: frozenset[str],
        logger: Logger | None = None,
    ) -> None:
        self.confirmation_required = frozenset(confirmation_required).union(
            MANDATORY_CONFIRMATION_ACTIONS
        )
        self.denied_actions = frozenset(denied_actions).union(
            MANDATORY_DENIED_ACTIONS
        )
        self.logger = logger or get_logger(__name__)

    def check(self, tool_name: str, action: str) -> PermissionResult:
        """Return the permission decision for a tool action."""
        action_id = f"{tool_name}.{action}"

        if action_id in self.denied_actions:
            self.logger.warning("Denied configured action %s.", action_id)
            return PermissionResult(
                PermissionDecision.DENIED,
                f"{action_id} is denied by configuration.",
            )

        if action_id in self.confirmation_required:
            self.logger.info("Action %s requires confirmation.", action_id)
            reason = f"{action_id} requires explicit confirmation."
            notice = self.confirmation_notice(tool_name, action)
            if notice is not None:
                reason = f"{reason} {notice}"
            return PermissionResult(
                PermissionDecision.REQUIRES_CONFIRMATION,
                reason,
            )

        self.logger.info("Allowed action %s.", action_id)
        return PermissionResult(
            PermissionDecision.ALLOWED,
            f"{action_id} is allowed.",
        )

    def confirmation_notice(self, tool_name: str, action: str) -> str | None:
        """Return the fixed notice an approval of this action carries, or None.

        Only an action that requires confirmation, and is not denied, carries
        one: its entry in ``CONFIRMATION_NOTICES``, which is fixed text.
        ``check`` appends the same notice to its reason, and a model plan's
        approval shows it too. Nothing is logged here.
        """
        action_id = f"{tool_name}.{action}"
        if (
            action_id in self.denied_actions
            or action_id not in self.confirmation_required
        ):
            return None
        return CONFIRMATION_NOTICES.get(action_id)
