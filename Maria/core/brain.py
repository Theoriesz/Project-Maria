"""Main application controller for Maria.

The brain coordinates the configured planner, memory layer, permission
manager, and tool manager. It does not directly perform database work or tool
logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from logging import Logger

from core.config import MariaConfig, load_config
from core.logger import configure_logging, get_logger
from core.memory import Memory
from core.permissions import PermissionDecision, PermissionManager
from core.planner import Plan, Planner
from core.tool_manager import ToolExecutionResult, ToolManager


@dataclass(frozen=True)
class BrainResponse:
    """Response returned by Maria's controller."""

    text: str
    needs_confirmation: bool = False
    plan: Plan | None = None


class MariaBrain:
    """Coordinate Maria's request handling workflow."""

    def __init__(
        self,
        config: MariaConfig,
        planner: Planner,
        memory: Memory,
        tool_manager: ToolManager,
        permissions: PermissionManager,
        logger: Logger | None = None,
    ) -> None:
        self.config = config
        self.planner = planner
        self.memory = memory
        self.tool_manager = tool_manager
        self.permissions = permissions
        self.logger = logger or get_logger(__name__)

    @classmethod
    def from_defaults(cls) -> "MariaBrain":
        """Create a fully wired MariaBrain from config.toml."""
        config = load_config()
        configure_logging(config.log_dir, config.log_level)
        logger = get_logger(__name__)

        memory = Memory(config.database_path, logger=get_logger("memory"))
        memory.initialize()

        tool_manager = ToolManager(
            config=config,
            memory=memory,
            logger=get_logger("tool_manager"),
        )
        tool_manager.load_tools()

        permissions = PermissionManager(
            confirmation_required=config.confirmation_required,
            denied_actions=config.denied_actions,
            logger=get_logger("permissions"),
        )

        return cls(
            config=config,
            planner=Planner(logger=get_logger("planner")),
            memory=memory,
            tool_manager=tool_manager,
            permissions=permissions,
            logger=logger,
        )

    def process(self, user_input: str) -> BrainResponse:
        """Plan one user request and run it unless it needs confirmation."""
        cleaned_input = user_input.strip()
        if not cleaned_input:
            return BrainResponse("Please enter a request.")

        self.logger.info("Received user request.")

        try:
            recent_memory = self.memory.get_recent_interactions(limit=3)
        except RuntimeError as exc:
            self.logger.error("Unable to retrieve recent memory: %s", exc)
            recent_memory = []

        plan = self.planner.create_plan(
            cleaned_input,
            context={"recent_interactions": recent_memory},
        )

        return self._run_plan(plan, confirmed=False)

    def execute_confirmed_plan(self, plan: Plan) -> BrainResponse:
        """Run an already-approved plan without planning the request again.

        The caller has bound the owner's approval to this exact plan, so
        Brain retrieves no context and invokes no planner. It resumes the
        ordinary permission, dispatch, and recording flow for the steps it
        is handed.
        """
        return self._run_plan(plan, confirmed=True)

    def _run_plan(self, plan: Plan, *, confirmed: bool) -> BrainResponse:
        """Check, dispatch, and record one plan's steps generically."""
        if not plan.steps:
            response = plan.response_if_no_action or "I do not know how to handle that yet."
            self._record_interaction(plan.original_request, response)
            return BrainResponse(response, plan=plan)

        results: list[ToolExecutionResult] = []

        for step in plan.steps:
            permission = self.permissions.check(step.tool_name, step.action)

            if permission.decision == PermissionDecision.DENIED:
                self.logger.warning(
                    "Denied action: %s.%s",
                    step.tool_name,
                    step.action,
                )
                response = f"Permission denied: {permission.reason}"
                self._record_interaction(plan.original_request, response)
                return BrainResponse(response, plan=plan)

            if (
                permission.decision == PermissionDecision.REQUIRES_CONFIRMATION
                and not confirmed
            ):
                self.logger.info(
                    "Confirmation required for action: %s.%s",
                    step.tool_name,
                    step.action,
                )
                return BrainResponse(
                    text=f"Confirmation required: {permission.reason}",
                    needs_confirmation=True,
                    plan=plan,
                )

            if permission.decision == PermissionDecision.REQUIRES_CONFIRMATION:
                self.logger.info(
                    "Confirmed action: %s.%s",
                    step.tool_name,
                    step.action,
                )

            result = self.tool_manager.execute_tool(
                step.tool_name,
                step.action,
                step.arguments,
            )
            results.append(result)

            if not result.success:
                break

        response = self._format_results(results)
        self._record_interaction(
            plan.original_request,
            self._format_recorded_results(results),
        )
        return BrainResponse(response, plan=plan)

    def _record_interaction(self, user_input: str, response: str) -> None:
        """Store an interaction without allowing memory failures to crash Maria."""
        try:
            self.memory.record_interaction(user_input, response)
        except RuntimeError as exc:
            self.logger.error("Unable to record interaction: %s", exc)

    @staticmethod
    def _format_results(results: list[ToolExecutionResult]) -> str:
        """Format tool results for the user."""
        if not results:
            return "No action was executed."

        messages = [result.message for result in results]
        return "\n".join(messages)

    @staticmethod
    def _format_recorded_results(results: list[ToolExecutionResult]) -> str:
        """Format tool results for interaction-history recording.

        Each result contributes its recorded_message when set, or its
        displayed message when recorded_message is None, so a tool can
        override what gets persisted without Brain inspecting its name or
        action.
        """
        if not results:
            return "No action was executed."

        recorded_texts = [
            result.message if result.recorded_message is None else result.recorded_message
            for result in results
        ]
        return "\n".join(recorded_texts)
