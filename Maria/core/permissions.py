"""Permission decisions for Maria actions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from logging import Logger

from core.logger import get_logger


MANDATORY_CONFIRMATION_ACTIONS = frozenset(
    {
        "filesystem.rename",
        "filesystem.move",
        "history.delete",
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
            return PermissionResult(
                PermissionDecision.REQUIRES_CONFIRMATION,
                f"{action_id} requires explicit confirmation.",
            )

        self.logger.info("Allowed action %s.", action_id)
        return PermissionResult(
            PermissionDecision.ALLOWED,
            f"{action_id} is allowed.",
        )
