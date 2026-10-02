"""Audit-summary tool for Maria.

The tool lists, reads, and deletes the content-free audit summaries Brain
writes when the owner approves an action the local reasoning model proposed.
Only the owner's own commands reach it: none of its actions may be proposed
by a reasoning model, and audit summaries never become model context.

Everything it shows comes from the audit vocabulary in ``core/audit.py``: a
summary's id, Maria's own UTC timestamp, the canonical action identifier, the
decision, the generalised target, and the outcome, or the fixed statement that
no outcome was recorded. What it records as interaction history is fixed text
and ids alone, never a summary's fields or what a listing showed. Every
failure is a fixed message.
"""

from __future__ import annotations

from collections.abc import Mapping
from logging import Logger
from types import MappingProxyType
from typing import Any

from core.audit import (
    AUDIT_LIST_LIMIT,
    AUDIT_QUERY_LIMIT,
    AUDIT_SUMMARY_ID_MESSAGE,
    AuditDecision,
    AuditOutcome,
    AuditSummary,
    AuditTarget,
    is_audit_summary_id,
)
from core.logger import get_logger
from core.memory import Memory
from core.tool_manager import ToolExecutionResult


# What a summary without an outcome says: Maria may have stopped after
# authorising the attempt, during the tool's run, or before the outcome could
# be written, so nothing is known about whether it ran.
OUTCOME_UNKNOWN_TEXT = "Outcome not recorded; execution is unknown."

NO_AUDIT_SUMMARIES_MESSAGE = "No audit summaries found."
MORE_AUDIT_SUMMARIES_MESSAGE = (
    f"Showing the newest {AUDIT_LIST_LIMIT} audit summaries. More audit "
    "summaries exist."
)
AUDIT_LISTED_RECORD = "Listed audit summaries."
AUDIT_LIST_FAILED_MESSAGE = "Unable to list audit summaries."
AUDIT_READ_FAILED_MESSAGE = "Unable to read audit summary."
AUDIT_DELETE_FAILED_MESSAGE = "Unable to delete audit summary."
UNSUPPORTED_AUDIT_ACTION_MESSAGE = "Unsupported audit action."

_DECISION_TEXT = MappingProxyType(
    {AuditDecision.OWNER_APPROVED: "approved by the owner"}
)
_TARGET_TEXT = MappingProxyType(
    {
        AuditTarget.NONE: "none",
        AuditTarget.NAMED_FILESYSTEM_ROOT: "named filesystem root",
    }
)
_OUTCOME_TEXT = MappingProxyType(
    {
        AuditOutcome.SUCCEEDED: "Outcome: succeeded",
        AuditOutcome.FAILED: "Outcome: failed",
    }
)


def audit_not_found_message(summary_id: int) -> str:
    """Return the fixed reply for an id no audit summary has."""
    return f"Audit summary not found: {summary_id}."


def summary_line(summary: AuditSummary) -> str:
    """Render one summary as a single listing line."""
    decision = _DECISION_TEXT[summary.decision]
    return (
        f"{summary.summary_id}: {summary.created_at} {summary.action} "
        f"(target: {_TARGET_TEXT[summary.target]}). "
        f"{decision[0].upper()}{decision[1:]}. {_outcome_text(summary, '.')}"
    )


def summary_block(summary: AuditSummary) -> str:
    """Render one summary as a labelled block, one field per line."""
    return "\n".join(
        (
            f"Audit summary {summary.summary_id}",
            f"Created at: {summary.created_at}",
            f"Action: {summary.action}",
            f"Target: {_TARGET_TEXT[summary.target]}",
            f"Decision: {_DECISION_TEXT[summary.decision]}",
            _outcome_text(summary, ""),
        )
    )


def _outcome_text(summary: AuditSummary, ending: str) -> str:
    """Name a summary's outcome, or say that none was recorded."""
    if summary.outcome is None:
        return OUTCOME_UNKNOWN_TEXT
    return f"{_OUTCOME_TEXT[summary.outcome]}{ending}"


class AuditTool:
    """List, read, and delete audit summaries through the memory layer."""

    name = "audit"
    description = (
        "Lists, reads, and deletes audit summaries of approved model-proposed "
        "actions."
    )
    actions = frozenset({"list", "read", "delete"})

    def __init__(self, memory: Memory, logger: Logger | None = None) -> None:
        self.memory = memory
        self.logger = logger or get_logger(__name__)

    def execute(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> ToolExecutionResult:
        """Execute an audit action."""
        if action == "list":
            return self._list()
        if action == "read":
            return self._read(arguments)
        if action == "delete":
            return self._delete(arguments)
        return ToolExecutionResult(False, UNSUPPORTED_AUDIT_ACTION_MESSAGE)

    def _list(self) -> ToolExecutionResult:
        """List the newest summaries, saying when more exist.

        One row more than is shown is read, so more summaries are recognised
        without counting them. History records only that a listing happened.
        """
        try:
            summaries = self.memory.list_audit_summaries(AUDIT_QUERY_LIMIT)
        except (RuntimeError, ValueError):
            self.logger.warning("Audit summary listing failed.")
            return ToolExecutionResult(False, AUDIT_LIST_FAILED_MESSAGE)
        if type(summaries) is not list or any(
            type(summary) is not AuditSummary for summary in summaries
        ):
            self.logger.warning("Audit summary listing failed.")
            return ToolExecutionResult(False, AUDIT_LIST_FAILED_MESSAGE)

        if not summaries:
            return ToolExecutionResult(
                True,
                NO_AUDIT_SUMMARIES_MESSAGE,
                [],
                recorded_message=AUDIT_LISTED_RECORD,
            )

        visible = summaries[:AUDIT_LIST_LIMIT]
        lines = [summary_line(summary) for summary in visible]
        if len(summaries) > AUDIT_LIST_LIMIT:
            lines.append(MORE_AUDIT_SUMMARIES_MESSAGE)
        self.logger.info("Listed audit summaries through audit tool.")
        return ToolExecutionResult(
            True,
            "\n".join(lines),
            visible,
            recorded_message=AUDIT_LISTED_RECORD,
        )

    def _read(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Show one summary by id; history records only which id was shown."""
        summary_id = arguments.get("id")
        if not is_audit_summary_id(summary_id):
            return ToolExecutionResult(False, AUDIT_SUMMARY_ID_MESSAGE)

        try:
            summary = self.memory.get_audit_summary(summary_id)
        except (RuntimeError, ValueError):
            self.logger.warning("Audit summary read failed.")
            return ToolExecutionResult(False, AUDIT_READ_FAILED_MESSAGE)

        if summary is None:
            return ToolExecutionResult(False, audit_not_found_message(summary_id))
        if type(summary) is not AuditSummary:
            self.logger.warning("Audit summary read failed.")
            return ToolExecutionResult(False, AUDIT_READ_FAILED_MESSAGE)

        self.logger.info("Displayed audit summary %s through audit tool.", summary_id)
        return ToolExecutionResult(
            True,
            summary_block(summary),
            summary,
            recorded_message=f"Displayed audit summary {summary_id}.",
        )

    def _delete(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Delete one summary and its outcome together, by id."""
        summary_id = arguments.get("id")
        if not is_audit_summary_id(summary_id):
            return ToolExecutionResult(False, AUDIT_SUMMARY_ID_MESSAGE)

        try:
            deleted = self.memory.delete_audit_summary(summary_id)
        except (RuntimeError, ValueError):
            self.logger.warning("Audit summary deletion failed.")
            return ToolExecutionResult(False, AUDIT_DELETE_FAILED_MESSAGE)

        if deleted is not True:
            return ToolExecutionResult(False, audit_not_found_message(summary_id))

        self.logger.info("Deleted audit summary %s through audit tool.", summary_id)
        return ToolExecutionResult(
            True,
            f"Deleted audit summary {summary_id}.",
            {"id": summary_id},
        )


def build_tool(memory: Memory, **_: Any) -> AuditTool:
    """Build an audit tool instance."""
    return AuditTool(memory=memory)
