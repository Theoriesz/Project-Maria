"""Permission decisions for Maria actions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from logging import Logger
from types import MappingProxyType

from core.logger import get_logger


MANDATORY_CONFIRMATION_ACTIONS = frozenset(
    {
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
    }
)

# What the owner is told, beyond the standard sentence, before approving an
# action whose consequence is not obvious from its name. Each notice is fixed
# text and interpolates nothing, so no stored content can reach it.
CONFIRMATION_NOTICES = MappingProxyType(
    {
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
            notice = CONFIRMATION_NOTICES.get(action_id)
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
