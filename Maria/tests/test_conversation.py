"""Focused tests for V1-14's deterministic-first, session-bounded conversation.

Every provider here is a stub: the adversarial fake from
``tests.reasoning_fakes`` or the scripted one below. No test contacts a model,
opens a connection, or needs a network. Every database, log, and workspace is
temporary, and Maria's live database, live log, and private override are never
read.
"""

from __future__ import annotations

import ast
import dataclasses
import logging
import sqlite3
import tempfile
import unittest
from collections.abc import Callable, Iterable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.action_contracts import BUILT_IN_ACTION_CONTRACTS
from core.brain import BrainResponse, MariaBrain
from core.config import MariaConfig
from core.constants import APP_VERSION, PROJECT_ROOT
from core.conversation import (
    CONVERSATION_EXCHANGE_TOO_LARGE_MESSAGE,
    CONVERSATION_MAX_BYTES,
    CONVERSATION_MAX_EXCHANGES,
    CONVERSATION_UNSENDABLE_MESSAGE,
    GENERIC_FALLBACK_RESPONSE,
    bounded_conversation,
    conversation_budget,
    fitted_history,
    is_generic_fallback,
    transient_plan,
)
from core.failures import Failure, FailureKind, TypedFailureError
from core.filesystem_roots import (
    FilesystemProtections,
    FilesystemRoot,
    FilesystemRoots,
    RootPolicy,
)
from core.logger import configure_logging, get_logger
from core.memory import Memory
from core.permissions import PermissionManager
from core.plan_compiler import ProposalRejection
from core.planner import Plan, Planner, PlanStep, RecordingPolicy
from core.reasoning import (
    DISABLED_REASONING,
    PROPOSAL_INSTRUCTIONS,
    REASONING_FAILURE_KINDS,
    REASONING_HISTORY_MESSAGE,
    REASONING_MAXIMUM_INPUT_BYTES,
    ReasoningExchange,
    ReasoningRequest,
    ReasoningResponse,
    ReasoningSettings,
    ReasoningTransport,
    history_content_bytes,
    request_content_bytes,
)
from core.session import CONFIRMATION_REFUSED_MESSAGE, MariaSession
from core.tool_manager import ToolManager
from tests.log_assertions import assert_log_omits
from tests.reasoning_fakes import (
    FAKE_EXCEPTION_SENTINEL,
    FAKE_PAYLOAD_SENTINEL,
    AdversarialReasoningProvider,
    HostileOutput,
    hostile_payload,
)
from tests.test_brain_recording_policy import (
    COMMAND_CORPUS,
    _controlled_environment,
    _CorpusHarness,
    _encoded,
    _quiet_logger,
)


# The planner's generic fallback, spelled out independently of the marker, so
# a marker that drifted from the planner could not pass by agreeing with
# itself.
UNSUPPORTED_RESPONSE = (
    "I do not have an executable plan for that yet. "
    "Type 'help' to see supported commands."
)
OVER_BUDGET_MESSAGE = "The request is too large for the local reasoning model."
UNAVAILABLE_MESSAGE = "The local reasoning model is unavailable."

# Inputs the planner answers with a zero-step plan that is not the generic
# fallback: help, and every usage error it can return.
NO_ACTION_COMMANDS = (
    "help",
    "commands",
    "HELP",
    "Commands",
    "remember",
    "recall",
    "update memory",
    "update memory 1",
    "update memory 1:",
    "update memory x: fact",
    "forget memory",
    "forget memory abc",
    "rename inbox/draft.txt",
    "move nothing",
    "create note missing colon",
    "note missing colon",
    "retitle note",
    "retitle note 1",
    "retitle note zero: Title",
    "edit note",
    "edit note 1:",
    "delete note",
    "delete note 0",
    "search history",
    "read interaction",
    "read interaction abc",
    "delete interaction",
    "delete interaction -1",
    "read audit",
    "read audit abc",
    "delete audit",
    "delete audit -1",
)

# Inputs the planner turns into a plan with a step, including every action
# that needs confirmation.
STEP_COMMANDS = (
    "calculate 6 * 7",
    "calc 2 + 2",
    "remember My editor is VS Code",
    "recall editor",
    "memories",
    "update memory 1: My editor is Vim",
    "forget memory 1",
    "list inbox",
    "ls .",
    "read file inbox/a.txt",
    "rename inbox/a.txt to inbox/b.txt",
    "move inbox/a.txt to archive/a.txt",
    "create note Title: Body",
    "note Title: Body",
    "notes",
    "list notes",
    "read note 1",
    "retitle note 1: New",
    "edit note 1: New body",
    "delete note 1",
    "history",
    "search history calculate",
    "read interaction 1",
    "delete interaction 1",
    "audit",
    "read audit 1",
    "delete audit 1",
    "system",
    "status",
    "time",
    "current time",
)

COLOUR_STATEMENT = "My favourite colour is teal."
COLOUR_QUESTION = "What is my favourite colour?"
REMEMBERED_REPLY = "I will remember that."

PROMPT_SENTINEL = "private-conversation-prompt-8c2e4a61f0"
REPLY_SENTINEL = "private-conversation-reply-51d7b3e09a"
MEMORY_SENTINEL = "private-stored-memory-2a9c61e4f7"
NOTE_SENTINEL = "private-stored-note-body-7e30b95d12"
FILE_SENTINEL = "private-workspace-file-4d18c2a7e3"
INTERACTION_SENTINEL = "private-stored-interaction-93b0e6c15a"
EARLIER_SESSION_SENTINEL = "earlier-session-turn-6f2d1b8e40"

ZERO_ROWS = {
    "interactions": 0,
    "notes": 0,
    "memories": 0,
    "audit_summaries": 0,
    "audit_outcomes": 0,
    # V1-20: no conversational turn may save a conversation or a summary.
    "saved_conversations": 0,
    "conversation_summaries": 0,
}

# Since V1-15 every model-backed conversational response ends with Maria's own
# notice about memory context, after a blank line and outside the reply the
# session keeps. With no memory enabled, that notice is this fixed sentence,
# written out so the owner-facing wording is pinned rather than agreed with.
NO_MEMORY_CONTEXT_SUFFIX = "\n\nNo memory context supplied."

# Since V1-18 Brain's model path always sends Maria's fixed proposal
# instructions, reserved together with the request before anything else is
# fitted. Without context, as in every bounded turn here, they cost exactly
# their own UTF-8 bytes, so each request boundary below is its V1-14 value
# moved by this much.
PROPOSAL_INSTRUCTION_BYTES = len(PROPOSAL_INSTRUCTIONS.encode("utf-8"))

# The fixed hostile payloads that open with "{". Since V1-18 each is a
# proposal attempt, which the compiler refuses for exactly the reason given
# here. The others open with "[" or with prose, so none of them is ever a
# proposal attempt.
REFUSED_HOSTILE_OUTPUTS = {
    HostileOutput.UNKNOWN_TOOL: ProposalRejection.UNKNOWN_TOOL,
    HostileOutput.UNKNOWN_ACTION: ProposalRejection.UNKNOWN_ACTION,
    HostileOutput.EXTRA_ARGUMENTS: ProposalRejection.EXTRA_ARGUMENT,
    HostileOutput.OUT_OF_RANGE_VALUES: ProposalRejection.NOT_MODEL_ELIGIBLE,
    HostileOutput.TRUNCATED_OUTPUT: ProposalRejection.MALFORMED_OUTPUT,
}

# An action-shaped reply naming a registered action the model may never
# propose. Since V1-18 it is a proposal attempt, which the compiler refuses
# as not model-eligible before any permission or tool is consulted.
CLOSED_ACTION_PROPOSAL = (
    '{"tool": "notes", "action": "delete", "arguments": {"id": 1}}'
)


def _shown(text: str) -> str:
    """Return what the owner sees for a conversational turn with no context."""
    return text + NO_MEMORY_CONTEXT_SUFFIX


def enabled_settings(
    *,
    max_input_bytes: int = 4096,
    max_output_bytes: int = 4096,
) -> ReasoningSettings:
    """Return valid enabled settings; no stub here ever uses the host."""
    return ReasoningSettings(
        enabled=True,
        transport=ReasoningTransport.LOOPBACK,
        host="127.0.0.1",
        model="stub-model:1b",
        request_deadline_seconds=5,
        max_input_bytes=max_input_bytes,
        max_output_bytes=max_output_bytes,
    )


class ScriptedProvider:
    """A cooperative provider that answers from a script.

    It records every request it receives, and its answer may depend on the
    request, which is how a test stands in for a model that understands a
    follow-up from its history. An answer that raises is raised to the
    caller exactly as a provider's own failure would be.
    """

    def __init__(self, answer: Callable[[ReasoningRequest], str]) -> None:
        self._answer = answer
        self.requests: list[ReasoningRequest] = []

    @classmethod
    def replying(cls, *replies: str) -> ScriptedProvider:
        """Answer successive requests with successive replies."""
        remaining = iter(replies)
        return cls(lambda request: next(remaining))

    @property
    def calls(self) -> int:
        """Return how many requests this provider has received."""
        return len(self.requests)

    def respond(self, request: ReasoningRequest) -> ReasoningResponse:
        """Record the request, then answer it from the script."""
        self.requests.append(request)
        return ReasoningResponse(self._answer(request))


def _remembering_model(request: ReasoningRequest) -> str:
    """Answer the colour question only from what the history says."""
    if request.prompt != COLOUR_QUESTION:
        return REMEMBERED_REPLY
    for exchange in request.history:
        if exchange.user.startswith("My favourite colour is "):
            colour = exchange.user.removeprefix("My favourite colour is ")
            return f"Your favourite colour is {colour.rstrip('.')}."
    return "You have not told me your favourite colour."


def sent_texts(requests: Iterable[ReasoningRequest]) -> str:
    """Return every text the requests carried, prompt and history alike."""
    return "\n".join(
        text
        for request in requests
        for text in (
            request.prompt,
            *(
                part
                for exchange in request.history
                for part in (exchange.user, exchange.assistant)
            ),
        )
    )


def _spy(test: unittest.TestCase, owner: object, name: str) -> Mock:
    """Wrap one method so it still runs but its calls are counted."""
    active = patch.object(owner, name, wraps=getattr(owner, name))
    spy = active.start()
    test.addCleanup(active.stop)
    return spy


def _test_config(root: Path, settings: ReasoningSettings) -> MariaConfig:
    """Return a configuration whose every path lies under a temporary root."""
    database_path = root / "database" / "maria.db"
    return MariaConfig(
        app_name="Maria",
        version=APP_VERSION,
        environment="test",
        database_path=database_path,
        log_dir=root / "logs",
        prompt_path=root / "prompts" / "system.txt",
        filesystem_roots=FilesystemRoots(
            (
                FilesystemRoot(
                    name="workspace",
                    path=root / "workspace",
                    policy=RootPolicy.ORGANIZE,
                    enabled=True,
                ),
            ),
            "workspace",
        ),
        filesystem_protections=FilesystemProtections(
            database_path=database_path,
            log_dir=root / "logs",
            prompt_path=root / "prompts" / "system.txt",
        ),
        log_level="DEBUG",
        confirmation_required=frozenset(),
        denied_actions=frozenset(),
        reasoning=settings,
    )


class _PlanSubclass(Plan):
    """A plan subtype, which must never pass as the planner's own plan."""


class _Harness:
    """Real planner, contracts, permissions, tools, memory, Brain, and session.

    Everything lives under one temporary directory. Planning, contract
    validation, permission checks, and tool dispatch are wrapped by spies
    that still call the real components, so a test can prove how often each
    was reached.
    """

    def __init__(
        self,
        test: unittest.TestCase,
        provider: object | None = None,
        *,
        settings: ReasoningSettings | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.workspace = root / "workspace"
        (self.workspace / "inbox").mkdir(parents=True)
        (self.workspace / "archive").mkdir()
        if settings is None:
            settings = DISABLED_REASONING if provider is None else enabled_settings()
        self.config = _test_config(root, settings)
        self.database_path = self.config.database_path

        self.logger = logger or _quiet_logger("tests.conversation")
        self.memory = Memory(self.database_path, logger=self.logger)
        self.memory.initialize()
        self.tool_manager = ToolManager(
            config=self.config,
            memory=self.memory,
            logger=self.logger,
        )
        self.tool_manager.load_tools()
        self.permissions = PermissionManager(
            confirmation_required=self.config.confirmation_required,
            denied_actions=self.config.denied_actions,
            logger=self.logger,
        )
        self.planner = Planner(logger=self.logger)

        self.create_plan = _spy(test, self.planner, "create_plan")
        self.validate_action = _spy(test, self.tool_manager, "validate_action")
        self.check = _spy(test, self.permissions, "check")
        self.execute_tool = _spy(test, self.tool_manager, "execute_tool")

        self.provider = provider
        self.brain = MariaBrain(
            config=self.config,
            planner=self.planner,
            memory=self.memory,
            tool_manager=self.tool_manager,
            permissions=self.permissions,
            logger=self.logger,
            reasoning=provider,
        )

    def session(self) -> MariaSession:
        """Return a fresh session over this harness's Brain."""
        return MariaSession(self.brain, logger=self.logger)

    def row_counts(self) -> dict[str, int]:
        """Count the rows of every record table in the temporary database."""
        connection = sqlite3.connect(self.database_path)
        try:
            return {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "interactions",
                    "notes",
                    "memories",
                    "audit_summaries",
                    "audit_outcomes",
                    "saved_conversations",
                    "conversation_summaries",
                )
            }
        finally:
            connection.close()

    def interactions(self) -> list[tuple[str, str]]:
        """Return every stored request and response, oldest first."""
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT user_input, response FROM interactions ORDER BY id"
            ).fetchall()
        finally:
            connection.close()
        return [(row[0], row[1]) for row in rows]


# --- The fallback marker ----------------------------------------------------


class GenericFallbackMarkerTests(unittest.TestCase):
    """Prove the marker names the planner's one generic fallback, and only it."""

    def setUp(self) -> None:
        self.planner = Planner(logger=_quiet_logger("tests.conversation.marker"))

    def test_the_marker_is_the_planners_own_unsupported_response(self) -> None:
        self.assertEqual(GENERIC_FALLBACK_RESPONSE, UNSUPPORTED_RESPONSE)

    def test_the_marker_matches_what_the_planner_returns_for_unknown_input(
        self,
    ) -> None:
        for text in (
            "sing me an unsupported song",
            "list",
            "histories",
            COLOUR_QUESTION,
            "\xe9t\xe9 \U00006f22\U00005b57",
        ):
            with self.subTest(text=text):
                plan = self.planner.create_plan(text)

                self.assertEqual(plan, Plan(text, (), UNSUPPORTED_RESPONSE))
                self.assertTrue(is_generic_fallback(plan))

    def test_help_and_every_usage_error_are_zero_step_but_not_the_fallback(
        self,
    ) -> None:
        for text in NO_ACTION_COMMANDS:
            with self.subTest(text=text):
                plan = self.planner.create_plan(text)

                self.assertEqual(plan.steps, ())
                self.assertTrue(plan.response_if_no_action)
                self.assertFalse(is_generic_fallback(plan))

    def test_commands_and_confirmation_plans_are_not_the_fallback(self) -> None:
        for text in STEP_COMMANDS:
            with self.subTest(text=text):
                plan = self.planner.create_plan(text)

                self.assertTrue(plan.steps)
                self.assertFalse(is_generic_fallback(plan))

    def test_only_the_exact_fallback_shape_is_recognised(self) -> None:
        near_misses = {
            "transient": Plan(
                "x",
                (),
                UNSUPPORTED_RESPONSE,
                RecordingPolicy.TRANSIENT,
            ),
            "with a step": Plan(
                "x",
                (PlanStep("system", "current_time", {}),),
                UNSUPPORTED_RESPONSE,
            ),
            "different text": Plan("x", (), UNSUPPORTED_RESPONSE + " "),
            "no text": Plan("x", ()),
            "a subtype": _PlanSubclass("x", (), UNSUPPORTED_RESPONSE),
            "not a plan": SimpleNamespace(
                original_request="x",
                steps=(),
                response_if_no_action=UNSUPPORTED_RESPONSE,
                recording=RecordingPolicy.RECORD,
            ),
            "the text alone": UNSUPPORTED_RESPONSE,
            "nothing": None,
        }
        for label, candidate in near_misses.items():
            with self.subTest(case=label):
                self.assertFalse(is_generic_fallback(candidate))


# --- Byte accounting and retention ------------------------------------------


def _exchange(user: str, assistant: str) -> ReasoningExchange:
    """Build one exchange, for readability."""
    return ReasoningExchange(user, assistant)


class ByteAccountingTests(unittest.TestCase):
    """Define and prove how conversational content is counted and bounded."""

    def setUp(self) -> None:
        # Three exchanges of exactly 20 content bytes each: 60 in all.
        self.first = _exchange("a" * 10, "b" * 10)
        self.second = _exchange("c" * 10, "d" * 10)
        self.third = _exchange("e" * 10, "f" * 10)
        self.history = (self.first, self.second, self.third)

    def test_content_is_the_prompt_plus_every_user_and_assistant_text(
        self,
    ) -> None:
        # UTF-8 bytes, not characters: 2 + (2 + 3) + (1 + 4).
        history = (
            _exchange("\xe9", "\N{EURO SIGN}"),
            _exchange("a", "\U0001f600"),
        )

        self.assertEqual(history_content_bytes(history, 1000), 10)
        self.assertEqual(request_content_bytes("\xe9", history, 1000), 12)
        self.assertEqual(request_content_bytes("\xe9", (), 1000), 2)
        self.assertEqual(history_content_bytes((), 1000), 0)

    def test_the_limits_are_eight_exchanges_and_eight_kib(self) -> None:
        self.assertEqual(CONVERSATION_MAX_EXCHANGES, 8)
        self.assertEqual(CONVERSATION_MAX_BYTES, 8 * 1024)

    def test_the_budget_is_eight_kib_or_the_configured_budget_if_smaller(
        self,
    ) -> None:
        cases = {
            1: 1,
            64: 64,
            8191: 8191,
            8192: 8192,
            8193: 8192,
            REASONING_MAXIMUM_INPUT_BYTES: 8192,
        }
        for configured, budget in cases.items():
            with self.subTest(configured=configured):
                settings = enabled_settings(max_input_bytes=configured)
                self.assertEqual(conversation_budget(settings), budget)
        self.assertEqual(conversation_budget(DISABLED_REASONING), 8192)

    def test_fitting_drops_oldest_whole_exchanges_at_the_exact_boundary(
        self,
    ) -> None:
        # Four bytes of request and sixty of history fill 64 exactly.
        self.assertEqual(
            fitted_history("\xe9\xe9", self.history, 64),
            self.history,
        )
        # One byte more drops the oldest exchange whole, never part of it.
        self.assertEqual(
            fitted_history("\xe9\xe9a", self.history, 64),
            (self.second, self.third),
        )
        # A request that fills the budget alone leaves room for nothing.
        self.assertEqual(fitted_history("\xe9" * 32, self.history, 64), ())

    def test_fitting_sends_at_most_eight_exchanges(self) -> None:
        nine = tuple(_exchange(f"u{index}", f"a{index}") for index in range(9))

        self.assertEqual(fitted_history("q", nine, 4096), nine[1:])

    def test_retention_keeps_at_most_eight_whole_exchanges_within_budget(
        self,
    ) -> None:
        nine = tuple(_exchange(f"u{index}", f"a{index}") for index in range(9))
        kept = bounded_conversation(nine)

        self.assertEqual(kept, nine[1:])
        for original, retained in zip(nine[1:], kept):
            self.assertIs(retained, original)

        self.assertEqual(bounded_conversation(self.history, 60), self.history)
        self.assertEqual(
            bounded_conversation(self.history, 59),
            (self.second, self.third),
        )
        self.assertEqual(bounded_conversation(self.history, 19), ())

    def test_retention_refuses_anything_but_an_exact_tuple_of_exchanges(
        self,
    ) -> None:
        for value in (
            list(self.history),
            ("text",),
            ((self.first.user, self.first.assistant),),
            None,
        ):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ValueError) as caught:
                    bounded_conversation(value)  # type: ignore[arg-type]
                self.assertEqual(str(caught.exception), REASONING_HISTORY_MESSAGE)

    def test_a_turn_plan_is_zero_step_and_transient(self) -> None:
        plan = transient_plan("question", "answer")

        self.assertEqual(
            plan,
            Plan("question", (), "answer", RecordingPolicy.TRANSIENT),
        )


# --- Disabled reasoning ------------------------------------------------------


class DisabledReasoningTests(unittest.TestCase):
    """Prove reasoning left off behaves exactly as Maria always has."""

    def test_unknown_input_answers_and_records_exactly_as_before(self) -> None:
        harness = _Harness(self)
        session = harness.session()

        response = session.submit("sing me an unsupported song")

        self.assertEqual(
            response,
            BrainResponse(
                UNSUPPORTED_RESPONSE,
                plan=Plan("sing me an unsupported song", (), UNSUPPORTED_RESPONSE),
            ),
        )
        self.assertEqual(
            harness.interactions(),
            [("sing me an unsupported song", UNSUPPORTED_RESPONSE)],
        )
        self.assertEqual(session.conversation, ())

    def test_a_provider_with_disabled_settings_is_never_asked(self) -> None:
        """Disabled settings keep the fallback as it was, provider or not."""
        provider = ScriptedProvider.replying(REPLY_SENTINEL)
        harness = _Harness(self, provider, settings=DISABLED_REASONING)
        session = harness.session()

        response = session.submit(PROMPT_SENTINEL)

        self.assertEqual(
            response,
            BrainResponse(
                UNSUPPORTED_RESPONSE,
                plan=Plan(PROMPT_SENTINEL, (), UNSUPPORTED_RESPONSE),
            ),
        )
        self.assertEqual(
            harness.interactions(),
            [(PROMPT_SENTINEL, UNSUPPORTED_RESPONSE)],
        )
        self.assertEqual(provider.calls, 0)
        self.assertEqual(session.conversation, ())


# --- Resolution order ---------------------------------------------------------


class ResolutionOrderTests(unittest.TestCase):
    """Prove the deterministic planner answers first, every time."""

    def setUp(self) -> None:
        self.provider = ScriptedProvider(lambda request: "A conversational reply.")
        self.harness = _Harness(self, self.provider)
        self.session = self.harness.session()

    def test_no_deterministic_command_ever_reaches_the_provider(self) -> None:
        (self.harness.workspace / "inbox" / "a.txt").write_text(
            "Alpha.",
            encoding="utf-8",
        )
        commands = (
            *NO_ACTION_COMMANDS,
            "calculate 6 * 7",
            "remember My editor is VS Code",
            "recall editor",
            "memories",
            "create note Title: Body",
            "notes",
            "read note 1",
            "list inbox",
            "read file inbox/a.txt",
            "history",
            "search history calculate",
            "read interaction 1",
            "audit",
            "read audit 1",
            "system",
            "time",
        )
        for command in commands:
            with self.subTest(command=command):
                before = self.harness.row_counts()["interactions"]

                response = self.session.submit(command)

                self.assertFalse(response.needs_confirmation)
                self.assertIsNone(response.conversation)
                self.assertEqual(
                    self.harness.row_counts()["interactions"],
                    before + 1,
                )
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.session.conversation, ())

    def test_the_planner_answers_first_and_exactly_once_per_request(self) -> None:
        planned_before_asking: list[int] = []

        def answer(request: ReasoningRequest) -> str:
            planned_before_asking.append(self.harness.create_plan.call_count)
            return "A conversational reply."

        self.harness.brain.reasoning = ScriptedProvider(answer)

        self.session.submit("sing me an unsupported song")
        self.session.submit("histories")

        self.assertEqual(planned_before_asking, [1, 2])
        self.assertEqual(self.harness.create_plan.call_count, 2)

    def test_blank_input_reaches_neither_the_planner_nor_the_provider(self) -> None:
        response = self.session.submit(" \t\r\n ")

        self.assertEqual(response, BrainResponse("Please enter a request."))
        self.harness.create_plan.assert_not_called()
        self.assertEqual(self.provider.calls, 0)

    def test_a_confirmation_cycle_never_consults_the_provider(self) -> None:
        source = self.harness.workspace / "inbox" / "a.txt"
        source.write_text("Alpha.", encoding="utf-8")

        pending = self.session.submit("rename inbox/a.txt to inbox/b.txt")
        self.assertTrue(pending.needs_confirmation)
        self.assertFalse(is_generic_fallback(pending.plan))
        confirmed = self.session.confirm()

        self.assertEqual(confirmed.text, "Renamed inbox/a.txt to inbox/b.txt.")
        self.assertTrue((self.harness.workspace / "inbox" / "b.txt").is_file())
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.session.conversation, ())

    def test_a_conversational_turn_discards_a_pending_approval(self) -> None:
        (self.harness.workspace / "inbox" / "a.txt").write_text(
            "Alpha.",
            encoding="utf-8",
        )
        self.session.submit("rename inbox/a.txt to inbox/b.txt")
        self.assertIsNotNone(self.session.pending_approval)

        self.session.submit("sing me an unsupported song")

        self.assertIsNone(self.session.pending_approval)
        self.assertEqual(
            self.session.confirm(),
            BrainResponse(CONFIRMATION_REFUSED_MESSAGE),
        )
        self.harness.execute_tool.assert_not_called()
        self.assertTrue((self.harness.workspace / "inbox" / "a.txt").is_file())


# --- Follow-up turns ------------------------------------------------------------


class FollowUpTests(unittest.TestCase):
    """Prove a second turn is answered in the light of the first."""

    def test_a_second_turn_that_depends_on_the_first_is_understood(self) -> None:
        provider = ScriptedProvider(_remembering_model)
        harness = _Harness(self, provider)
        session = harness.session()

        first = session.submit(COLOUR_STATEMENT)
        second = session.submit(COLOUR_QUESTION)

        self.assertEqual(first.text, _shown(REMEMBERED_REPLY))
        self.assertEqual(second.text, _shown("Your favourite colour is teal."))
        opening, follow_up = provider.requests
        self.assertEqual(opening.prompt, COLOUR_STATEMENT)
        self.assertEqual(opening.history, ())
        self.assertEqual(follow_up.prompt, COLOUR_QUESTION)
        self.assertEqual(
            follow_up.history,
            (_exchange(COLOUR_STATEMENT, REMEMBERED_REPLY),),
        )
        self.assertEqual(
            session.conversation,
            (
                _exchange(COLOUR_STATEMENT, REMEMBERED_REPLY),
                _exchange(COLOUR_QUESTION, "Your favourite colour is teal."),
            ),
        )
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_the_follow_up_alone_is_not_understood(self) -> None:
        """Control: the answer above came from the history and nowhere else."""
        provider = ScriptedProvider(_remembering_model)
        harness = _Harness(self, provider)

        response = harness.session().submit(COLOUR_QUESTION)

        self.assertEqual(
            response.text,
            _shown("You have not told me your favourite colour."),
        )


# --- Session ownership, isolation, and end ------------------------------------------


class SessionConversationTests(unittest.TestCase):
    """Prove the conversation belongs to one active session and ends with it."""

    def setUp(self) -> None:
        self.provider = ScriptedProvider(_remembering_model)
        self.harness = _Harness(self, self.provider)

    def test_a_new_session_starts_empty_and_never_sees_another_sessions_turns(
        self,
    ) -> None:
        earlier = self.harness.session()
        earlier.submit(f"My favourite colour is {EARLIER_SESSION_SENTINEL}.")
        self.assertEqual(len(earlier.conversation), 1)

        later = self.harness.session()
        self.assertEqual(later.conversation, ())
        response = later.submit(COLOUR_QUESTION)

        self.assertEqual(self.provider.requests[-1].history, ())
        self.assertEqual(
            response.text,
            _shown("You have not told me your favourite colour."),
        )
        self.assertNotIn(
            EARLIER_SESSION_SENTINEL,
            sent_texts(self.provider.requests[1:]),
        )

    def test_ending_a_session_discards_its_conversation_and_pending_approval(
        self,
    ) -> None:
        (self.harness.workspace / "inbox" / "a.txt").write_text(
            "Alpha.",
            encoding="utf-8",
        )
        session = self.harness.session()
        session.submit(COLOUR_STATEMENT)
        session.submit("rename inbox/a.txt to inbox/b.txt")
        self.assertEqual(len(session.conversation), 1)
        self.assertIsNotNone(session.pending_approval)

        session.end()

        self.assertEqual(session.conversation, ())
        self.assertIsNone(session.pending_approval)
        self.assertEqual(
            session.confirm(),
            BrainResponse(CONFIRMATION_REFUSED_MESSAGE),
        )
        session.end()
        self.assertEqual(session.conversation, ())

        session.submit(COLOUR_QUESTION)
        self.assertEqual(self.provider.requests[-1].history, ())
        self.harness.execute_tool.assert_not_called()

    def test_the_session_keeps_only_what_the_retention_limits_allow(self) -> None:
        """Whatever Brain returns, the session holds no more than the limits."""
        too_many = tuple(_exchange(f"u{index}", f"a{index}") for index in range(12))
        brain = Mock(spec=MariaBrain)
        brain.process.return_value = BrainResponse("reply", conversation=too_many)
        session = MariaSession(brain, logger=_quiet_logger("tests.conversation"))

        session.submit("first")
        self.assertEqual(session.conversation, too_many[-8:])

        oversized = (_exchange("x" * 5000, "y"), _exchange("z" * 4000, "w"))
        brain.process.return_value = BrainResponse("reply", conversation=oversized)
        session.submit("second")
        self.assertEqual(session.conversation, oversized[1:])

    def test_the_session_hands_its_conversation_to_brain_in_order(self) -> None:
        brain = Mock(spec=MariaBrain)
        exchanges = (_exchange("one", "uno"), _exchange("two", "dos"))
        brain.process.side_effect = [
            BrainResponse("reply", conversation=exchanges),
            BrainResponse("command result"),
            BrainResponse("command result"),
        ]
        session = MariaSession(brain, logger=_quiet_logger("tests.conversation"))

        session.submit("first")
        session.submit("second")
        session.submit("third")

        self.assertEqual(
            [call.args for call in brain.process.call_args_list],
            [("first",), ("second", *exchanges), ("third", *exchanges)],
        )

    def test_stored_records_files_and_commands_never_reach_the_provider(
        self,
    ) -> None:
        provider = ScriptedProvider(lambda request: "A conversational reply.")
        harness = _Harness(self, provider)
        (harness.workspace / "inbox" / "secret.txt").write_text(
            FILE_SENTINEL,
            encoding="utf-8",
        )
        harness.memory.record_interaction(INTERACTION_SENTINEL, INTERACTION_SENTINEL)
        session = harness.session()
        for command in (
            f"remember {MEMORY_SENTINEL}",
            f"create note Private: {NOTE_SENTINEL}",
            f"recall {MEMORY_SENTINEL}",
            "memories",
            "read note 1",
            "notes",
            "read file inbox/secret.txt",
            "history",
            "read interaction 1",
        ):
            session.submit(command)

        session.submit("sing me an unsupported song")
        session.submit("histories")

        self.assertEqual(provider.calls, 2)
        sent = sent_texts(provider.requests)
        for private in (
            MEMORY_SENTINEL,
            NOTE_SENTINEL,
            FILE_SENTINEL,
            INTERACTION_SENTINEL,
            "remember",
            "read note",
            "read file",
        ):
            with self.subTest(private=private):
                self.assertNotIn(private, sent)
        self.assertEqual(
            session.conversation,
            (
                _exchange("sing me an unsupported song", "A conversational reply."),
                _exchange("histories", "A conversational reply."),
            ),
        )


# --- Exact bounds -----------------------------------------------------------------


class ConversationBoundsTests(unittest.TestCase):
    """Prove the count and byte limits at their exact boundaries."""

    def _turns(
        self,
        replies: dict[str, str],
        *,
        max_input_bytes: int = 64 + PROPOSAL_INSTRUCTION_BYTES,
    ) -> tuple[_Harness, ScriptedProvider, MariaSession]:
        """Build a session whose provider answers each prompt from a table.

        The default budget leaves exactly 64 bytes beside the proposal
        instructions, so a request and the history sent with it meet the
        boundaries they met before the instructions were sent. A finished
        exchange is still held to the whole budget.
        """
        provider = ScriptedProvider(lambda request: replies[request.prompt])
        harness = _Harness(
            self,
            provider,
            settings=enabled_settings(max_input_bytes=max_input_bytes),
        )
        return harness, provider, harness.session()

    def test_a_request_at_the_budget_is_sent_and_one_byte_over_is_refused(
        self,
    ) -> None:
        at_budget = "\xe9" * 32
        over_budget = at_budget + "a"
        # One byte longer than the instructions, so the finished exchange is
        # one byte over the whole budget.
        reply = "r" * (PROPOSAL_INSTRUCTION_BYTES + 1)
        harness, provider, session = self._turns(
            {at_budget: reply, over_budget: "never asked"},
        )

        sent = session.submit(at_budget)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.requests[0].prompt, at_budget)
        # The instructions that fill the rest of the budget were really sent.
        self.assertIs(provider.requests[0].proposal_instructions, True)
        # Sent, but the finished exchange cannot fit beside its reply.
        self.assertEqual(sent.text, _shown(CONVERSATION_EXCHANGE_TOO_LARGE_MESSAGE))

        refused = session.submit(over_budget)
        self.assertEqual(refused.text, _shown(OVER_BUDGET_MESSAGE))
        self.assertIsNone(refused.conversation)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(session.conversation, ())
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_the_eight_kib_limit_holds_when_the_configured_budget_is_larger(
        self,
    ) -> None:
        # The instructions are reserved first, so the request alone may fill
        # only what they leave of the 8 KiB limit.
        room = 8 * 1024 - PROPOSAL_INSTRUCTION_BYTES
        at_limit = "\xe9" * (room // 2) + "a" * (room % 2)
        over_limit = at_limit + "a"
        harness, provider, session = self._turns(
            {at_limit: "r", over_limit: "never asked"},
            max_input_bytes=REASONING_MAXIMUM_INPUT_BYTES,
        )

        session.submit(at_limit)
        refused = session.submit(over_limit)

        self.assertEqual([request.prompt for request in provider.requests], [at_limit])
        self.assertEqual(refused.text, _shown(OVER_BUDGET_MESSAGE))
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_an_exchange_exactly_at_the_budget_is_kept(self) -> None:
        request = "q" * 30
        # The exchange is held to the whole budget, instructions included.
        reply = "\xe9" * 17 + "r" * PROPOSAL_INSTRUCTION_BYTES
        harness, provider, session = self._turns({request: reply})

        response = session.submit(request)

        self.assertEqual(response.text, _shown(reply))
        self.assertEqual(session.conversation, (_exchange(request, reply),))
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_an_exchange_one_byte_over_is_refused_whole(self) -> None:
        request = "q" * 30
        reply = "\xe9" * 17 + "r" * PROPOSAL_INSTRUCTION_BYTES + "a"
        harness, provider, session = self._turns({request: reply})

        response = session.submit(request)

        self.assertEqual(
            response.text,
            _shown(CONVERSATION_EXCHANGE_TOO_LARGE_MESSAGE),
        )
        self.assertNotIn("\xe9", response.text)
        self.assertIsNone(response.conversation)
        self.assertFalse(response.needs_confirmation)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(session.conversation, ())
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_oldest_whole_exchanges_are_dropped_first_at_the_exact_boundary(
        self,
    ) -> None:
        replies = {
            "a" * 10: "b" * 10,
            "c" * 10: "d" * 10,
            "e" * 10: "f" * 10,
            # One byte longer than the instructions, so keeping all four
            # exchanges would be one byte over the whole budget.
            "\xe9\xe9": "g" * (PROPOSAL_INSTRUCTION_BYTES + 1),
            "\xe9\xe9a": "h",
        }
        first = _exchange("a" * 10, "b" * 10)
        second = _exchange("c" * 10, "d" * 10)
        third = _exchange("e" * 10, "f" * 10)

        for request, sent_history in (
            ("\xe9\xe9", (first, second, third)),
            ("\xe9\xe9a", (second, third)),
        ):
            with self.subTest(request=request):
                _harness, provider, session = self._turns(replies)
                for opening in ("a" * 10, "c" * 10, "e" * 10):
                    session.submit(opening)
                self.assertEqual(session.conversation, (first, second, third))

                session.submit(request)

                self.assertEqual(provider.requests[-1].history, sent_history)
                self.assertEqual(
                    session.conversation,
                    (second, third, _exchange(request, replies[request])),
                )

    def test_at_most_eight_exchanges_are_kept_and_sent(self) -> None:
        prompts = [f"turn number {index}" for index in range(10)]
        harness, provider, session = self._turns(
            {prompt: f"reply {prompt}" for prompt in prompts},
            max_input_bytes=4096,
        )

        for prompt in prompts:
            session.submit(prompt)

        exchanges = [_exchange(prompt, f"reply {prompt}") for prompt in prompts]
        self.assertEqual(
            [len(request.history) for request in provider.requests],
            [0, 1, 2, 3, 4, 5, 6, 7, 8, 8],
        )
        self.assertEqual(provider.requests[8].history, tuple(exchanges[:8]))
        self.assertEqual(provider.requests[9].history, tuple(exchanges[1:9]))
        self.assertEqual(session.conversation, tuple(exchanges[2:]))
        self.assertEqual(harness.row_counts(), ZERO_ROWS)


# --- Failures and hostile replies ----------------------------------------------------


class ProviderFailureTests(unittest.TestCase):
    """Prove every failure is a fixed, transient reply and commands still work."""

    def _assert_failure_then_command_works(
        self,
        provider: object,
        expected: str,
    ) -> None:
        harness = _Harness(self, provider)
        session = harness.session()

        response = session.submit(PROMPT_SENTINEL)

        self.assertEqual(response.text, _shown(expected))
        self.assertIsNone(response.conversation)
        self.assertFalse(response.needs_confirmation)
        self.assertNotIn("Traceback", response.text)
        self.assertNotIn(FAKE_EXCEPTION_SENTINEL, response.text)
        self.assertEqual(session.conversation, ())
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

        command = session.submit("calculate 6 * 7")
        self.assertEqual(command.text, "6 * 7 = 42")
        self.assertEqual(harness.interactions(), [("calculate 6 * 7", "6 * 7 = 42")])

    def test_every_typed_failure_gives_its_fixed_message(self) -> None:
        for kind in sorted(REASONING_FAILURE_KINDS, key=lambda item: item.value):
            with self.subTest(kind=kind.value):
                self._assert_failure_then_command_works(
                    AdversarialReasoningProvider.failing(kind),
                    Failure(kind).message,
                )

    def test_a_crashing_or_misbehaving_provider_gives_a_fixed_message(
        self,
    ) -> None:
        cases = {
            "crashing": (
                AdversarialReasoningProvider.crashing(),
                UNAVAILABLE_MESSAGE,
            ),
            "returning nothing": (
                AdversarialReasoningProvider.returning(None),
                Failure(FailureKind.REASONING_MALFORMED_OUTPUT).message,
            ),
            "returning bare text": (
                AdversarialReasoningProvider.returning(FAKE_PAYLOAD_SENTINEL),
                Failure(FailureKind.REASONING_MALFORMED_OUTPUT).message,
            ),
        }
        for label, (provider, expected) in cases.items():
            with self.subTest(case=label):
                self._assert_failure_then_command_works(provider, expected)

    def test_an_unsendable_request_is_refused_before_the_provider(self) -> None:
        provider = ScriptedProvider.replying("never sent")
        harness = _Harness(self, provider)
        session = harness.session()

        response = session.submit("unpaired \ud800 surrogate")

        self.assertEqual(response.text, _shown(CONVERSATION_UNSENDABLE_MESSAGE))
        self.assertEqual(provider.calls, 0)
        self.assertEqual(harness.row_counts(), ZERO_ROWS)


class HostileReplyTests(unittest.TestCase):
    """Prove model text is inert text or a refused proposal, and never acts.

    Since V1-18 a reply opening with "{" is a proposal attempt. Every one
    here is refused with the compiler's fixed message, so it reaches no
    permission check or tool, offers no approval, and is never kept.
    """

    def _assert_inert(self, harness: _Harness, turns: int) -> None:
        """Assert the turns reached no plan validation, permission, or tool."""
        harness.validate_action.assert_not_called()
        harness.check.assert_not_called()
        harness.execute_tool.assert_not_called()
        self.assertEqual(harness.create_plan.call_count, turns)

    def test_hostile_replies_are_inert_text_or_refused_proposals(self) -> None:
        for output in HostileOutput:
            if output is HostileOutput.OVERSIZED_OUTPUT:
                continue
            with self.subTest(output=output.value):
                provider = AdversarialReasoningProvider.emitting(output)
                harness = _Harness(self, provider)
                session = harness.session()

                response = session.submit(PROMPT_SENTINEL)

                rejection = REFUSED_HOSTILE_OUTPUTS.get(output)
                if rejection is None:
                    self.assertEqual(response.text, _shown(hostile_payload(output)))
                    self.assertEqual(response.plan.steps, ())
                else:
                    # The fixed refusal with the turn's notice: the payload
                    # is neither shown, offered, nor kept.
                    self.assertEqual(response.text, _shown(rejection.message))
                    self.assertNotIn(FAKE_PAYLOAD_SENTINEL, response.text)
                    self.assertIsNone(response.plan)
                    self.assertIsNone(response.conversation)
                    self.assertEqual(session.conversation, ())
                self.assertFalse(response.needs_confirmation)
                self.assertIsNone(session.pending_approval)
                self.assertEqual(provider.calls, 1)
                self._assert_inert(harness, 1)
                self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_an_oversized_reply_is_discarded_with_its_fixed_message(self) -> None:
        provider = AdversarialReasoningProvider.emitting(
            HostileOutput.OVERSIZED_OUTPUT,
            256,
        )
        harness = _Harness(
            self,
            provider,
            settings=enabled_settings(max_output_bytes=256),
        )
        session = harness.session()

        response = session.submit(PROMPT_SENTINEL)

        self.assertEqual(
            response.text,
            _shown(Failure(FailureKind.REASONING_OUTPUT_OVER_BUDGET).message),
        )
        self.assertNotIn(FAKE_PAYLOAD_SENTINEL, response.text)
        self.assertEqual(session.conversation, ())
        self._assert_inert(harness, 1)
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_replies_that_read_as_commands_or_approval_do_nothing(self) -> None:
        replies = (
            "yes",
            "y",
            "delete note 1",
            "forget memory 1",
            "rename inbox/a.txt to inbox/b.txt",
        )
        provider = ScriptedProvider.replying(*replies, CLOSED_ACTION_PROPOSAL)
        harness = _Harness(self, provider)
        source = harness.workspace / "inbox" / "a.txt"
        source.write_text("Alpha.", encoding="utf-8")
        session = harness.session()
        session.submit("create note Keep: This note must survive.")
        session.submit("remember This memory must survive.")
        tool_calls = harness.execute_tool.call_count

        session.submit("rename inbox/a.txt to inbox/b.txt")
        self.assertIsNotNone(session.pending_approval)
        rows = harness.row_counts()
        for _ in (*replies, CLOSED_ACTION_PROPOSAL):
            response = session.submit("sing me an unsupported song")
            self.assertFalse(response.needs_confirmation)
            self.assertIsNone(session.pending_approval)

        # The action-shaped reply came last: refused, and neither retried
        # nor kept.
        self.assertEqual(
            response.text,
            _shown(ProposalRejection.NOT_MODEL_ELIGIBLE.message),
        )
        self.assertIsNone(response.conversation)
        self.assertEqual(provider.calls, len(replies) + 1)
        self.assertEqual(
            session.confirm(),
            BrainResponse(CONFIRMATION_REFUSED_MESSAGE),
        )
        self.assertEqual(harness.execute_tool.call_count, tool_calls)
        self.assertTrue(source.is_file())
        self.assertEqual(harness.row_counts()["notes"], 1)
        self.assertEqual(harness.row_counts()["memories"], 1)
        self.assertEqual(harness.row_counts(), rows)
        self.assertEqual(
            [exchange.assistant for exchange in session.conversation],
            list(replies),
        )


# --- Zero conversation rows -----------------------------------------------------------


class ZeroConversationRowTests(unittest.TestCase):
    """Prove no conversational path writes any row, verified in the database."""

    def test_no_conversational_path_writes_a_row(self) -> None:
        def answer(request: ReasoningRequest) -> str:
            if request.prompt == "please fail":
                raise TypedFailureError(Failure(FailureKind.REASONING_TIMEOUT))
            if request.prompt == "please answer at length":
                return "x" * (70 + PROPOSAL_INSTRUCTION_BYTES)
            if request.prompt == "please propose":
                return CLOSED_ACTION_PROPOSAL
            return "ok"

        provider = ScriptedProvider(answer)
        harness = _Harness(
            self,
            provider,
            settings=enabled_settings(
                max_input_bytes=64 + PROPOSAL_INSTRUCTION_BYTES,
            ),
        )
        session = harness.session()
        harness.memory.record_interaction("seeded request", "seeded response")
        before = harness.row_counts()

        for request in (
            "hello there",
            "a second conversational turn",
            "please fail",
            "please answer at length",
            "please propose",
            "x" * 65,
            "unpaired \ud800 surrogate",
        ):
            with self.subTest(request=request[:20]):
                session.submit(request)
                self.assertEqual(harness.row_counts(), before)

        self.assertEqual(
            harness.interactions(),
            [("seeded request", "seeded response")],
        )
        # Positive controls, so no path above can silently stop running: the
        # five requests within the budget reached the provider, the
        # over-budget and unsendable ones did not, and only the two plain
        # replies were kept.
        self.assertEqual(provider.calls, 5)
        self.assertEqual(len(session.conversation), 2)


class CommandCorpusWithReasoningTests(unittest.TestCase):
    """Prove every command records byte-identical rows with reasoning on.

    The whole command corpus runs through a controller with reasoning
    enabled. Its three generic-fallback entries become conversational and
    record nothing; every other entry is compared, row for row, with the
    same entry run through a disabled controller that skips those three.

    V1-20's two session-input commands read the exchanges a session
    retains, and only a session with reasoning on can retain any. Each is
    compared like for like: the enabled session is ended just before it, so
    both controllers meet the empty conversation a disabled one always has.
    What they do with retained exchanges is pinned in
    ``tests.test_conversation_persistence``.
    """

    def setUp(self) -> None:
        _controlled_environment(self)

    def test_every_command_records_the_same_rows_with_reasoning_enabled(
        self,
    ) -> None:
        planner = Planner(logger=_quiet_logger("tests.conversation.corpus"))
        fallback = {
            command
            for command, _outcome, _expected in COMMAND_CORPUS
            if command.strip()
            and is_generic_fallback(planner.create_plan(command.strip()))
        }
        self.assertEqual(
            fallback,
            {"sing me an unsupported song", "list", "histories"},
        )
        session_bound = {
            command
            for command, _outcome, _expected in COMMAND_CORPUS
            if command.strip()
            for step in planner.create_plan(command.strip()).steps
            if BUILT_IN_ACTION_CONTRACTS.contract_for(
                step.tool_name,
                step.action,
            ).session_input_argument
            is not None
        }
        self.assertEqual(
            session_bound,
            {"save conversation", "summarize conversation"},
        )

        provider = ScriptedProvider(lambda request: "A conversational reply.")
        enabled = _CorpusHarness(self)
        enabled.brain.reasoning = provider
        enabled.brain.config = dataclasses.replace(
            enabled.brain.config,
            reasoning=enabled_settings(),
        )
        disabled = _CorpusHarness(self)
        discarded: list[int] = []

        for command, outcome, _expected in COMMAND_CORPUS:
            with self.subTest(command=command, outcome=outcome):
                if command in session_bound:
                    discarded.append(len(enabled.session.conversation))
                    enabled.session.end()
                recorded = enabled.drive(command, outcome)
                if command in fallback:
                    self.assertEqual(recorded, ())
                    continue
                self.assertEqual(
                    _encoded(recorded),
                    _encoded(disabled.drive(command, outcome)),
                )

        self.assertEqual(enabled.all_rows(), disabled.all_rows())
        # Positive control: the three fallback replies were retained until
        # the first session-input command, so ending the session there was
        # what kept the comparison like for like.
        self.assertEqual(discarded, [3, 0])
        # Neither session-input command reached the provider: with nothing
        # retained, a summary request is refused before any request is built.
        self.assertEqual(
            [request.prompt for request in provider.requests],
            [command for command, _, _ in COMMAND_CORPUS if command in fallback],
        )


# --- Logging --------------------------------------------------------------------------


class ConversationLogPrivacyTests(unittest.TestCase):
    """Prove no request, reply, or history text reaches a temporary log."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.log_dir = Path(temporary.name) / "logs"

        self.maria_logger = logging.getLogger("maria")
        self.original_handlers = tuple(self.maria_logger.handlers)
        self.original_level = self.maria_logger.level
        self.original_propagate = self.maria_logger.propagate
        self.original_disabled = self.maria_logger.disabled
        for handler in self.original_handlers:
            self.maria_logger.removeHandler(handler)
        self.addCleanup(self._restore_maria_logger)

    def _restore_maria_logger(self) -> None:
        """Close the temporary handler and restore the logger as it was."""
        for handler in list(self.maria_logger.handlers):
            try:
                handler.flush()
            finally:
                self.maria_logger.removeHandler(handler)
                handler.close()
        for handler in self.original_handlers:
            self.maria_logger.addHandler(handler)
        self.maria_logger.setLevel(self.original_level)
        self.maria_logger.propagate = self.original_propagate
        self.maria_logger.disabled = self.original_disabled

    def test_every_conversational_path_logs_nothing_private(self) -> None:
        configure_logging(self.log_dir, level="DEBUG")

        def answer(request: ReasoningRequest) -> str:
            if request.prompt.startswith("fail"):
                raise RuntimeError(FAKE_EXCEPTION_SENTINEL)
            if request.prompt.startswith("long"):
                return REPLY_SENTINEL * 3 + "x" * PROPOSAL_INSTRUCTION_BYTES
            if request.prompt.startswith("propose"):
                # A refused proposal whose argument value is the sentinel.
                return (
                    '{"tool": "memory", "action": "store", '
                    '"arguments": {"fact": "' + REPLY_SENTINEL + '"}}'
                )
            return REPLY_SENTINEL

        harness = _Harness(
            self,
            ScriptedProvider(answer),
            settings=enabled_settings(
                max_input_bytes=96 + PROPOSAL_INSTRUCTION_BYTES,
            ),
            logger=get_logger("tests.conversation.logged"),
        )
        session = harness.session()
        for request in (
            PROMPT_SENTINEL,
            f"{PROMPT_SENTINEL} again",
            f"fail {PROMPT_SENTINEL}",
            f"long {PROMPT_SENTINEL}",
            f"propose {PROMPT_SENTINEL}",
            PROMPT_SENTINEL * 4,
            f"unpaired \ud800 {PROMPT_SENTINEL}",
        ):
            session.submit(request)
        session.end()

        for handler in self.maria_logger.handlers:
            handler.flush()
        log_file = self.log_dir / "maria.log"
        log_text = log_file.read_text(encoding="utf-8")

        # Positive controls: every path did log, so an empty log cannot pass
        # for a private one.
        for line in (
            "Answering request conversationally.",
            "Reasoning request completed.",
            "Reasoning request failed: code=reasoning_unavailable",
            "Conversational reply discarded: exchange over budget.",
            "Model reply is a proposal attempt.",
            "Refused a model proposal: code=not_model_eligible",
            "Conversational request refused: over budget.",
            "Conversational request refused: unsendable.",
            "Session ended.",
        ):
            with self.subTest(line=line):
                self.assertIn(line, log_text)

        assert_log_omits(
            self,
            log_file,
            (PROMPT_SENTINEL, REPLY_SENTINEL, FAKE_EXCEPTION_SENTINEL),
        )


# --- Runtime composition -----------------------------------------------------------------


class RuntimeCompositionTests(unittest.TestCase):
    """Prove how startup hands Brain a provider, and that Brain names none."""

    def _startup(
        self,
        factory: Mock | None,
        settings: ReasoningSettings,
    ) -> tuple[MariaBrain | None, Mock, BaseException | None]:
        """Run from_defaults with every side-effecting step replaced."""
        order = Mock()
        if factory is not None:
            order.attach_mock(factory, "factory")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config = _test_config(Path(temporary.name).resolve(), settings)

        raised: BaseException | None = None
        brain: MariaBrain | None = None
        with (
            patch("core.brain.load_config", return_value=config),
            patch("core.brain.prepare_roots", order.prepare_roots),
            patch("core.brain.configure_logging", order.configure_logging),
            patch("core.brain.Memory", order.Memory),
            patch("core.brain.ToolManager", order.ToolManager),
        ):
            try:
                if factory is None:
                    brain = MariaBrain.from_defaults()
                else:
                    brain = MariaBrain.from_defaults(reasoning_factory=factory)
            except ValueError as error:
                raised = error
        return brain, order, raised

    def test_the_factory_receives_the_settled_settings_before_anything_else(
        self,
    ) -> None:
        settings = enabled_settings()
        provider = ScriptedProvider.replying("unused")
        factory = Mock(return_value=provider)

        brain, order, raised = self._startup(factory, settings)

        self.assertIsNone(raised)
        factory.assert_called_once_with(settings)
        self.assertIs(brain.reasoning, provider)
        self.assertEqual(order.mock_calls[0][0], "factory")
        self.assertIn("prepare_roots", [name for name, _, _ in order.mock_calls])

    def test_without_a_factory_brain_is_deterministic_only(self) -> None:
        brain, _order, raised = self._startup(None, enabled_settings())

        self.assertIsNone(raised)
        self.assertIsNone(brain.reasoning)

    def test_a_factory_returning_none_leaves_brain_deterministic_only(
        self,
    ) -> None:
        factory = Mock(return_value=None)

        brain, _order, _raised = self._startup(factory, DISABLED_REASONING)

        factory.assert_called_once_with(DISABLED_REASONING)
        self.assertIsNone(brain.reasoning)

    def test_a_refused_provider_stops_startup_before_anything_is_created(
        self,
    ) -> None:
        factory = Mock(side_effect=ValueError("refused"))

        brain, order, raised = self._startup(factory, enabled_settings())

        self.assertIsNone(brain)
        self.assertIsInstance(raised, ValueError)
        self.assertEqual([name for name, _, _ in order.mock_calls], ["factory"])


class ConversationImportBoundaryTests(unittest.TestCase):
    """Guard what the conversation module can reach."""

    PERMITTED = frozenset(
        {
            "__future__",
            "collections.abc",
            "dataclasses",
            "logging",
            # V1-20: two leaf declarations that themselves import nothing
            # from Maria, which test_session_input_binding asserts, so the
            # module still has no path to dispatch or persistence.
            "core.action_contracts",
            "core.context",
            "core.conversation_records",
            "core.failures",
            "core.logger",
            "core.planner",
            "core.reasoning",
        }
    )

    def test_the_conversation_module_reaches_no_dispatch_or_persistence(
        self,
    ) -> None:
        source = (PROJECT_ROOT / "core" / "conversation.py").read_text(
            encoding="utf-8"
        )
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")

        self.assertLessEqual(imported, self.PERMITTED)
        for forbidden in (
            "core.tool_manager",
            "core.permissions",
            "core.memory",
            "core.session",
            "core.brain",
            "core.ollama_adapter",
            "core.plan_compiler",
            "sqlite3",
        ):
            with self.subTest(module=forbidden):
                self.assertNotIn(forbidden, imported)


if __name__ == "__main__":
    unittest.main()
