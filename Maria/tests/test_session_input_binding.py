"""Focused tests for V1-20's contract-declared session inputs.

Two actions take text only Maria may supply from the owner's session: a save
takes the canonical transcript of exactly the retained exchanges, and a
summary takes a draft the local model writes from exactly those exchanges.
The declaration lives on the action's contract, Brain asks Tool Manager for
it generically, and the conversation module decides whether a plan's
declaration is acceptable and builds every plan that results. Session text
is supplied only in ``process``, for a planner's plan holding exactly
DETERMINISTIC as its own origin: never at confirmation, never for a
model-originated plan, and never on the conversational path, and a plan that
already carries the text, or holds anything beside the one step, is refused.

Every database here is temporary, and every model is a scripted stub.
"""

from __future__ import annotations

import ast
import unittest
from unittest.mock import Mock, patch

from core import brain as brain_module
from core.action_contracts import (
    BUILT_IN_ACTION_CONTRACTS,
    ActionContract,
    ArgumentContract,
    ModelEligibility,
    RootClass,
    SessionInput,
)
from core.constants import PROJECT_ROOT
from core.conversation import (
    SESSION_INPUT_REFUSED_MESSAGE,
    SessionInputRequest,
    declared_session_input,
)
from core.conversation_records import encode_transcript
from core.planner import Plan, PlanOrigin, PlanStep, RecordingPolicy
from tests.test_conversation import ScriptedProvider, _Harness, enabled_settings


TRANSCRIPT = encode_transcript((("Hello there.", "Hello."),))
REFUSED = (
    "Maria refused that request because only Maria may supply its session "
    "text."
)
SUMMARY_PROMPT_START = "Summarize the conversation above"


def _lookup(tool_name: str, action: str) -> ArgumentContract | None:
    """Answer as Tool Manager does, from the shipped registry."""
    contract = BUILT_IN_ACTION_CONTRACTS.contract_for(tool_name, action)
    return None if contract is None else contract.session_input_argument


def _save_step(**arguments: object) -> PlanStep:
    """Return a save step with the given arguments."""
    return PlanStep("conversations", "save", arguments)


def _create_step(**arguments: object) -> PlanStep:
    """Return a summary step with the given arguments."""
    return PlanStep("conversation_summaries", "create", arguments)


def _saved_count(harness: _Harness) -> int:
    """Return how many saved conversations the temporary database holds."""
    return harness.row_counts()["saved_conversations"]


class SessionInputDeclarationTests(unittest.TestCase):
    """Pin which arguments take session text and what such a field may be."""

    def test_the_two_members_are_pinned(self) -> None:
        self.assertEqual(
            [(member.name, member.value) for member in SessionInput],
            [
                ("CONVERSATION", "conversation"),
                ("CONVERSATION_SUMMARY_DRAFT", "conversation_summary_draft"),
            ],
        )

    def test_exactly_two_actions_take_session_text(self) -> None:
        declared = {
            contract.identifier: (
                contract.session_input_argument.name,
                contract.session_input_argument.session_input,
                contract.session_input_argument.maximum_length,
            )
            for contract in BUILT_IN_ACTION_CONTRACTS.contracts
            if contract.session_input_argument is not None
        }

        self.assertEqual(
            declared,
            {
                "conversations.save": (
                    "transcript",
                    SessionInput.CONVERSATION,
                    65536,
                ),
                "conversation_summaries.create": (
                    "text",
                    SessionInput.CONVERSATION_SUMMARY_DRAFT,
                    2048,
                ),
            },
        )
        for identifier in declared:
            with self.subTest(action=identifier):
                contract = BUILT_IN_ACTION_CONTRACTS.contract_for(
                    *identifier.split(".")
                )
                self.assertIs(
                    contract.model_eligibility,
                    ModelEligibility.NOT_MODEL_ELIGIBLE,
                )
                argument = contract.session_input_argument
                self.assertIs(argument.required, True)
                self.assertIs(argument.allow_blank, False)
                self.assertIsNone(argument.root_class)

    def test_a_session_input_must_be_required_bounded_unrooted_text(self) -> None:
        base = {
            "name": "text",
            "value_type": str,
            "required": True,
            "allow_blank": False,
            "maximum_length": 10,
            "session_input": SessionInput.CONVERSATION,
        }
        ArgumentContract(**base)
        for label, change in (
            ("a plain string", {"session_input": "conversation"}),
            ("optional", {"required": False}),
            ("blank allowed", {"allow_blank": True}),
            ("unbounded", {"maximum_length": None}),
            ("rooted", {"root_class": RootClass.NAMED_FILESYSTEM_ROOT}),
            (
                "a number",
                {
                    "value_type": int,
                    "allow_blank": None,
                    "maximum_length": None,
                    "minimum": 1,
                    "maximum": 10,
                },
            ),
        ):
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    ArgumentContract(**{**base, **change})

    def test_an_action_takes_at_most_one_and_is_never_model_eligible(self) -> None:
        first = ArgumentContract(
            "one",
            str,
            required=True,
            allow_blank=False,
            maximum_length=10,
            session_input=SessionInput.CONVERSATION,
        )
        second = ArgumentContract(
            "two",
            str,
            required=True,
            allow_blank=False,
            maximum_length=10,
            session_input=SessionInput.CONVERSATION_SUMMARY_DRAFT,
        )

        with self.assertRaisesRegex(ValueError, "at most one session input"):
            ActionContract(
                "demo",
                "act",
                (first, second),
                model_eligibility=ModelEligibility.NOT_MODEL_ELIGIBLE,
            )
        with self.assertRaisesRegex(ValueError, "cannot be model-eligible"):
            ActionContract(
                "demo",
                "act",
                (first,),
                model_eligibility=ModelEligibility.MODEL_ELIGIBLE,
            )
        self.assertIs(
            ActionContract(
                "demo",
                "act",
                (first,),
                model_eligibility=ModelEligibility.NOT_MODEL_ELIGIBLE,
            ).session_input_argument,
            first,
        )

    def test_tool_manager_answers_from_its_registry_without_naming_a_tool(
        self,
    ) -> None:
        harness = _Harness(self)
        manager = harness.tool_manager

        self.assertIs(
            manager.session_input_argument("conversations", "save"),
            BUILT_IN_ACTION_CONTRACTS.contract_for(
                "conversations",
                "save",
            ).session_input_argument,
        )
        for tool_name, action in (
            ("conversations", "list"),
            ("calculator", "calculate"),
            ("conversations", "unknown"),
            ("unknown", "save"),
        ):
            with self.subTest(action=f"{tool_name}.{action}"):
                self.assertIsNone(manager.session_input_argument(tool_name, action))


class DeclaredSessionInputTests(unittest.TestCase):
    """Prove how the conversation module settles a plan's declaration."""

    def test_a_plan_declaring_nothing_is_not_asked_for_anything(self) -> None:
        plan = Plan("calculate 1 + 1", (PlanStep("calculator", "calculate", {}),))

        self.assertIsNone(declared_session_input(plan, _lookup))
        self.assertIsNone(declared_session_input(Plan("help", ()), _lookup))

    def test_a_well_formed_request_names_its_one_step_and_argument(self) -> None:
        plan = Plan("save conversation", (_save_step(),))

        request = declared_session_input(plan, _lookup)

        self.assertIsInstance(request, SessionInputRequest)
        self.assertIs(request.plan, plan)
        self.assertIs(request.step, plan.steps[0])
        self.assertEqual(request.argument.name, "transcript")
        self.assertIs(request.kind, SessionInput.CONVERSATION)
        self.assertIsNone(request.refusal)
        self.assertEqual(repr(request), "SessionInputRequest(<redacted>)")

    def test_only_a_plan_holding_exactly_deterministic_is_asked(self) -> None:
        model_plan = Plan(
            "save my conversation",
            (_save_step(transcript=TRANSCRIPT),),
            recording=RecordingPolicy.TRANSIENT,
            origin=PlanOrigin.MODEL,
        )

        class _Subclass(Plan):
            """A plan subtype, which never passes as the planner's own."""

        subclassed = _Subclass("save conversation", (_save_step(),))
        self.assertIsNone(declared_session_input(model_plan, _lookup))
        self.assertIsNone(declared_session_input(subclassed, _lookup))
        self.assertIsNone(declared_session_input("save conversation", _lookup))

    def test_a_lookup_answer_that_is_not_a_declaration_counts_as_none(self) -> None:
        plan = Plan("save conversation", (_save_step(),))

        for answer in (None, "transcript", Mock(spec=ArgumentContract), object()):
            with self.subTest(answer=type(answer).__name__):
                self.assertIsNone(
                    declared_session_input(plan, lambda tool, action: answer)
                )

    def test_a_plan_already_carrying_the_text_is_refused(self) -> None:
        for plan, recording in (
            (
                Plan("save conversation", (_save_step(transcript=TRANSCRIPT),)),
                RecordingPolicy.RECORD,
            ),
            (
                Plan("summarize conversation", (_create_step(text="A draft."),)),
                RecordingPolicy.TRANSIENT,
            ),
        ):
            with self.subTest(action=plan.steps[0].action):
                request = declared_session_input(plan, _lookup)

                self.assertIsNone(request.step)
                self.assertEqual(
                    request.refusal,
                    Plan(plan.original_request, (), REFUSED, recording),
                )

    def test_a_session_step_beside_any_other_step_is_refused(self) -> None:
        for steps, recording in (
            (
                (_save_step(), PlanStep("calculator", "calculate", {})),
                RecordingPolicy.RECORD,
            ),
            ((_save_step(), _save_step()), RecordingPolicy.RECORD),
            ((_save_step(), _create_step()), RecordingPolicy.TRANSIENT),
        ):
            with self.subTest(steps=[step.action for step in steps]):
                plan = Plan("save conversation", steps)

                request = declared_session_input(plan, _lookup)

                self.assertEqual(
                    request.refusal,
                    Plan("save conversation", (), REFUSED, recording),
                )
        self.assertEqual(SESSION_INPUT_REFUSED_MESSAGE, REFUSED)


class BrainBindingTests(unittest.TestCase):
    """Prove Brain supplies session text only in ``process``, and generically."""

    def setUp(self) -> None:
        self.provider = ScriptedProvider(lambda request: "A conversational reply.")
        self.harness = _Harness(
            self,
            self.provider,
            settings=enabled_settings(max_input_bytes=8192),
        )
        self.session = self.harness.session()

    def _planned(self, plan: Plan):
        """Make the planner answer every request with ``plan``."""
        active = patch.object(self.harness.planner, "create_plan", return_value=plan)
        active.start()
        self.addCleanup(active.stop)

    def test_a_plan_carrying_its_own_transcript_is_refused_and_stores_nothing(
        self,
    ) -> None:
        self._planned(Plan("save conversation", (_save_step(transcript=TRANSCRIPT),)))

        response = self.session.submit("save conversation")

        self.assertEqual(response.text, REFUSED)
        self.assertEqual(_saved_count(self.harness), 0)
        self.harness.execute_tool.assert_not_called()
        self.assertEqual(self.harness.interactions(), [("save conversation", REFUSED)])

    def test_a_plan_carrying_its_own_draft_is_refused_without_a_request(
        self,
    ) -> None:
        self.session.submit("hello there")
        self._planned(Plan("summarize conversation", (_create_step(text="A draft."),)))

        response = self.session.submit("summarize conversation")

        self.assertEqual(response.text, REFUSED)
        self.assertFalse(response.needs_confirmation)
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.harness.row_counts()["conversation_summaries"], 0)
        self.assertEqual(self.harness.interactions(), [])

    def test_a_session_step_beside_another_step_runs_nothing(self) -> None:
        self.session.submit("hello there")
        self._planned(
            Plan(
                "save conversation",
                (
                    _save_step(),
                    PlanStep("calculator", "calculate", {"expression": "1 + 1"}),
                ),
            )
        )

        response = self.session.submit("save conversation")

        self.assertEqual(response.text, REFUSED)
        self.harness.execute_tool.assert_not_called()
        self.assertEqual(_saved_count(self.harness), 0)

    def test_a_declared_input_of_any_other_kind_is_refused_and_supplies_nothing(
        self,
    ) -> None:
        self.session.submit("hello there")
        calls = self.provider.calls
        future = Mock(spec=ArgumentContract)
        future.name = "transcript"
        future.session_input = "future"
        plan = Plan("save conversation", (_save_step(),))
        request = SessionInputRequest(plan, plan.steps[0], future)
        self.assertEqual(request.kind, "future")

        with patch.object(
            brain_module,
            "declared_session_input",
            return_value=request,
        ):
            response = self.session.submit("save conversation")

        self.assertEqual(response.text, REFUSED)
        self.assertFalse(response.needs_confirmation)
        self.assertEqual(self.provider.calls, calls)
        self.harness.execute_tool.assert_not_called()
        counts = self.harness.row_counts()
        self.assertEqual(
            (counts["saved_conversations"], counts["conversation_summaries"]),
            (0, 0),
        )
        self.assertEqual(self.harness.interactions(), [])
        self.assertEqual(len(self.session.conversation), 1)

    def test_session_text_is_never_supplied_at_confirmation(self) -> None:
        self.session.submit("hello there")
        with patch.object(
            brain_module,
            "declared_session_input",
            side_effect=AssertionError("nothing is bound at confirmation"),
        ):
            response = self.harness.brain.execute_confirmed_plan(
                Plan("save conversation", (_save_step(),))
            )

        self.assertEqual(
            response.text,
            "Missing required argument for conversations.save: transcript.",
        )
        self.harness.execute_tool.assert_not_called()
        self.assertEqual(_saved_count(self.harness), 0)

    def test_session_text_is_never_supplied_for_a_model_proposal(self) -> None:
        for proposal in (
            '{"tool": "conversations", "action": "save", "arguments": {}}',
            '{"tool": "conversation_summaries", "action": "create", '
            '"arguments": {"text": "A draft."}}',
        ):
            with self.subTest(proposal=proposal[:40]):
                provider = ScriptedProvider(lambda request, reply=proposal: reply)
                harness = _Harness(
                    self,
                    provider,
                    settings=enabled_settings(max_input_bytes=8192),
                )
                spy = patch.object(
                    brain_module,
                    "declared_session_input",
                    wraps=brain_module.declared_session_input,
                )

                with spy as declared:
                    response = harness.session().submit("please keep this for me")

                declared.assert_not_called()
                self.assertFalse(response.needs_confirmation)
                harness.execute_tool.assert_not_called()
                counts = harness.row_counts()
                self.assertEqual(
                    (counts["saved_conversations"], counts["conversation_summaries"]),
                    (0, 0),
                )

    def test_session_text_is_never_supplied_on_the_conversational_path(self) -> None:
        with patch.object(
            brain_module,
            "declared_session_input",
            side_effect=AssertionError("a conversational turn binds nothing"),
        ):
            response = self.session.submit("tell me something")

        self.assertTrue(response.text.startswith("A conversational reply."))
        self.assertEqual(len(self.session.conversation), 1)

    def test_a_plan_declaring_nothing_runs_exactly_as_before(self) -> None:
        with patch.object(
            brain_module,
            "declared_session_input",
            wraps=brain_module.declared_session_input,
        ) as declared:
            response = self.session.submit("calculate 6 * 7")

        self.assertEqual(response.text, "6 * 7 = 42")
        declared.assert_called_once()
        plan = self.harness.execute_tool.call_args.args
        self.assertEqual(plan[:2], ("calculator", "calculate"))
        self.assertEqual(
            self.harness.interactions(),
            [("calculate 6 * 7", "6 * 7 = 42")],
        )


class LeafModuleTests(unittest.TestCase):
    """Prove the two declarations the conversation module imports are leaves."""

    STANDARD_LIBRARY = frozenset(
        {
            "__future__",
            "collections.abc",
            "dataclasses",
            "datetime",
            "enum",
            "json",
            "re",
            "types",
            "typing",
        }
    )

    def _imports(self, *path: str) -> set[str]:
        """Return every module one source file imports."""
        source = (PROJECT_ROOT.joinpath(*path)).read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        return imported

    def test_the_two_leaf_declarations_import_nothing_from_maria(self) -> None:
        for path in (
            ("core", "action_contracts.py"),
            ("core", "conversation_records.py"),
        ):
            with self.subTest(module="/".join(path)):
                imported = self._imports(*path)

                self.assertLessEqual(imported, self.STANDARD_LIBRARY)
                self.assertFalse(
                    any(
                        name.split(".")[0] in {"core", "tools", "main", "tests"}
                        for name in imported
                    )
                )

    def test_brain_names_every_session_input_kind_and_no_other(self) -> None:
        # Brain branches on each kind by name, and refuses any it does not
        # name, so a new member stays refused until Brain is changed for it.
        source = (PROJECT_ROOT / "core" / "brain.py").read_text(encoding="utf-8")
        named = {
            node.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "SessionInput"
        }

        self.assertEqual(named, {member.name for member in SessionInput})

    def test_brain_names_no_new_tool_action_or_argument(self) -> None:
        source = (PROJECT_ROOT / "core" / "brain.py").read_text(encoding="utf-8")
        constants = {
            node.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

        for name in (
            "conversations",
            "conversation_summaries",
            "save",
            "create",
            "transcript",
            "text",
            "save conversation",
            "summarize conversation",
            # V1-22: no task or reminder tool, action, or argument either.
            "tasks",
            "reminders",
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
            "when",
            "repeat",
            "never",
            "as_of",
            "status",
            "id",
            "text_digest",
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, constants)


if __name__ == "__main__":
    unittest.main()
