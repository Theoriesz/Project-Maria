"""Focused tests for V1-19's foreground multi-step plans.

A plan the local model proposes may now hold two to five steps, proposed in a
``steps`` envelope, and every plan of any origin holds at most five. These
tests prove the bound, counted from the steps a plan actually holds and never
from the length its container reports, and the flat shape at construction and
again at every boundary that confirms or runs a plan; that a plan missing any
of its own fields is refused with fixed text, never raised or read as a class
default; that approval and execution are bound to one bounded snapshot of the
plan; that a proposal compiles all or nothing; that a plan of several steps is
offered for exactly one approval, rendered in full from the very plan the
approval binds, and refused whole when any step fails a compiler gate or a
permission; that the approved sequence runs exactly as approved, each step
under its own committed audit summary, and stops at the first failure, audit
write that cannot be confirmed, or interruption, with a report that says
truthfully what ran and what did not; that nothing private reaches an audit
row, the log, interaction history, or the conversation; and that single-step
and deterministic behavior is unchanged.

Every database, log, and workspace is temporary. No test contacts a model or
opens a network connection, and Maria's live database, live log, and private
override are never read.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import unittest
from collections.abc import Callable, Iterator, Mapping
from functools import partial
from typing import Any
from unittest.mock import DEFAULT, call, patch

import core.brain as brain_module
import core.memory as memory_module
from core.action_contracts import BUILT_IN_ACTION_CONTRACTS, ModelEligibility
from core.audit import AuditDecision, AuditOutcome, AuditTarget
from core.brain import (
    CONVERSATION_RESULT_REFUSED_MESSAGE,
    MODEL_PLAN_REFUSED_MESSAGE,
    PLAN_STEP_LIMIT_REFUSED_MESSAGE,
    BrainResponse,
)
from core.conversation import (
    CONVERSATION_MAX_BYTES,
    GENERIC_FALLBACK_RESPONSE,
    ConversationTurn,
    converse,
    is_proposal_attempt,
    transient_plan,
)
from core.memory import Memory
from core.plan_compiler import (
    COMPILER_PLAN_MESSAGE,
    MODEL_PROPOSAL_MAX_BYTES,
    MODEL_PROPOSAL_MAX_NESTING,
    MODEL_PROPOSAL_MIN_STEPS,
    ProposalRejection,
    compile_proposal,
    recheck_model_plan,
)
from core.planner import (
    MODEL_PLAN_SHAPE_MESSAGE,
    PLAN_MAX_STEPS,
    PLAN_STEP_LIMIT_MESSAGE,
    Plan,
    PlanArguments,
    PlanOrigin,
    PlanStep,
    RecordingPolicy,
    bounded_steps,
    is_model_plan,
    is_within_step_bound,
    own_origin,
    plan_snapshot,
)
from core.reasoning import (
    PROPOSAL_INSTRUCTIONS,
    ReasoningExchange,
    ReasoningResponse,
    request_content_bytes,
)
from core.session import MODEL_APPROVAL_MESSAGE, PendingApproval, render_model_approval
from core.tool_manager import ToolExecutionResult
from tests.log_assertions import assert_log_omits
from tests.reasoning_fakes import FAKE_NAME_SENTINEL, FAKE_PAYLOAD_SENTINEL
from tests.test_brain_recording_policy import _quiet_logger
from tests.test_conversation import ScriptedProvider, enabled_settings
from tests.test_model_approval import _registry_with_eligibility
from tests.test_model_audit_gate import (
    CLOCK,
    CLOCK_REPORT,
    CONFIRMATION_REFUSED,
    NO_CONTEXT_NOTICE,
    OUTCOME_UNKNOWN,
    ROOT_NAME_SENTINEL,
    SHORT_BUSY_TIMEOUT_SECONDS,
    ZERO_ROWS,
    _approval as _one_step_approval,
    _block,
    _GateTestCase,
    _Harness,
    _IntSubclass,
    _InterruptedCommit,
    _Model,
    _spy,
    _summary_row,
)
from tests.test_planner_value_objects import MODEL_STEP_FINGERPRINT
from tools.calculator import CalculatorTool
from tools.filesystem import FilesystemTool
from tools.system import SystemTool


# A request the deterministic planner answers with its generic fallback, so
# with reasoning enabled it reaches the model.
REQUEST = "Please tell me the time and then add some numbers."

# Distinctive values a privacy check can search for. The path is a plain
# relative path, so it compiles and reaches every place it must not leak to.
REQUEST_SENTINEL = "private-multi-step-request-71c4e9a2d0"
PATH_SENTINEL = "private-multi-step-path-3f8b26d1e5"
EXCEPTION_SENTINEL = "private-multi-step-exception-c05a7e93b4"
PRIVATE_REQUEST = f"Please look through {REQUEST_SENTINEL} for me."

# Owner-visible wording, written out so it is pinned rather than agreed with.
MALFORMED_PLAN = (
    "The proposed plan did not have exactly a steps list of 2 to 5 actions, "
    "each with exactly a tool, an action, and an arguments object."
)
MALFORMED_PROPOSAL = (
    "The proposed action did not have exactly a tool, an action, and an "
    "arguments object."
)
NOT_MODEL_ELIGIBLE = "Maria does not accept that action from the reasoning model."
UNSAFE_PATH = (
    "The proposed action had a path that is not a plain relative path inside "
    "Maria's default filesystem root."
)
CALCULATION_FAILED = "Calculation failed: Only basic arithmetic is supported."
TOOL_EXECUTION_FAILED = "Tool execution failed."
NOT_AUTHORISED = "not run, because its audit summary could not be recorded."
SUMMARY_UNCONFIRMED = "Maria could not confirm whether its audit summary was recorded."
OVER_BUDGET_TEXT = (
    "The request is too large for the local reasoning model.\n"
    "\n"
    "No memory context supplied."
)


def _action(tool: str, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Return one proposed action exactly as a cooperative model writes it."""
    return {"tool": tool, "action": action, "arguments": arguments}


def _envelope(*actions: Any) -> str:
    """Return a proposal of several actions in its steps envelope."""
    return json.dumps({"steps": list(actions)})


def _sum(number: int) -> dict[str, Any]:
    """Return a calculation of ``number + number``."""
    return _action("calculator", "calculate", {"expression": f"{number} + {number}"})


def _sum_result(number: int) -> str:
    """Return what the calculator answers for ``_sum(number)``."""
    return f"{number} + {number} = {2 * number}"


def _list(path: str) -> dict[str, Any]:
    """Return a listing of a path beneath the default root."""
    return _action("filesystem", "list", {"path": path})


TIME = _action("system", "current_time", {})
LIST_ROOT = _action("filesystem", "list", {})
LIST_MISSING = _list("missing")
# Text the calculator refuses when it runs, though every compiler gate admits it.
BAD_SUM = _action("calculator", "calculate", {"expression": "x + 1"})
# A registered action a model may never propose.
CLOSED = _action("notes", "delete", {"id": 1})


def _plan_header(count: int) -> str:
    """Return the first line of a model-proposed plan of several steps."""
    return (
        f"Maria's local reasoning model proposed this plan of {count} actions. "
        "They run in order, and only if you approve the whole plan. Maria "
        "stops if a step fails, an audit write fails, or the run is "
        "interrupted."
    )


def _approval(*steps: tuple[str, str, str]) -> str:
    """Return the approval rendering of several steps, notice below it."""
    total = len(steps)
    lines = [_plan_header(total)]
    for position, (tool, action, arguments) in enumerate(steps, start=1):
        lines += [
            f"Step {position} of {total}",
            f"Tool: {tool}",
            f"Action: {action}",
            f"Arguments: {arguments}",
        ]
    return "\n".join(lines) + "\n\n" + NO_CONTEXT_NOTICE


def _step(position: int, total: int, identifier: str, status: str, *lines: str) -> str:
    """Return one step's block of a plan's report."""
    return "\n".join((f"Step {position} of {total}, {identifier}: {status}", *lines))


def _recorded(summary_id: int) -> str:
    """Return the notice for a step whose outcome was recorded."""
    return f"Recorded as audit summary {summary_id}."


def _unrecorded(summary_id: int) -> str:
    """Return the notice for a step whose outcome could not be recorded."""
    return (
        f"Recorded as audit summary {summary_id}, but its outcome could not be "
        "recorded, so that summary shows the outcome as unknown."
    )


def _missing(summary_id: int) -> str:
    """Return the notice for a step given no outcome at all."""
    return (
        f"Recorded as audit summary {summary_id}, but no outcome was recorded, "
        "so that summary shows the outcome as unknown."
    )


def _unconfirmed(summary_id: int) -> str:
    """Return the notice for a step whose outcome write was interrupted."""
    return (
        f"Recorded as audit summary {summary_id}, but Maria could not confirm "
        "whether its outcome was recorded."
    )


def _later(first: int, total: int) -> str:
    """Name the steps from ``first`` to ``total`` as the report does."""
    if first == total:
        return f"step {first}"
    if first + 1 == total:
        return f"steps {first} and {total}"
    return f"steps {first} to {total}"


def _stopped(where: str, position: int, total: int, reason: str = "", ran: bool = True) -> str:
    """Return the closing sentence of a plan that stopped at ``position``."""
    sentence = f"Maria stopped {where} step {position}{reason}"
    if position < total:
        sentence += f"{',' if reason else ''} and did not run {_later(position + 1, total)}"
    sentence += "."
    if ran:
        sentence += " Nothing that ran was undone."
    return sentence


def _report(*blocks: str) -> str:
    """Return a plan's report from its blocks."""
    return "\n\n".join(blocks)


def _sums_report(total: int, completed: int, *tail: str) -> list[str]:
    """Return the blocks of ``completed`` successful calculations."""
    return [
        _step(n, total, "calculator.calculate", "completed.", _sum_result(n), _recorded(n))
        for n in range(1, completed + 1)
    ] + list(tail)


def _not_run(first: int, total: int) -> list[str]:
    """Return the blocks of the calculations from ``first`` that never ran."""
    return [
        _step(n, total, "calculator.calculate", "not run.")
        for n in range(first, total + 1)
    ]


def _compile(text: str, contracts: Any = BUILT_IN_ACTION_CONTRACTS) -> Plan | ProposalRejection:
    """Compile one reply against the shipped registry, quietly."""
    return compile_proposal(
        REQUEST,
        ReasoningResponse(text),
        contracts,
        _quiet_logger("tests.multi_step_plans.compile"),
    )


def _model_plan(*steps: PlanStep) -> Plan:
    """Return, built by hand, a well-formed model-originated plan."""
    return Plan(
        REQUEST,
        steps,
        recording=RecordingPolicy.TRANSIENT,
        origin=PlanOrigin.MODEL,
    )


def _calculation(number: int) -> PlanStep:
    """Return the deterministic step calculating ``number + number``."""
    return PlanStep("calculator", "calculate", {"expression": f"{number} + {number}"})


def _calculations(count: int) -> list[PlanStep]:
    """Return the steps calculating ``n + n`` for every n from 1 to ``count``."""
    return [_calculation(number) for number in range(1, count + 1)]


class _Underreported(list):
    """A list that reports five elements, whatever it actually holds."""

    def __len__(self) -> int:
        return 5


class _UnderreportedTuple(tuple):
    """A tuple that reports five elements, whatever it actually holds."""

    def __len__(self) -> int:
        return 5


class _Endless:
    """An iterable that never ends, counting every step it hands out."""

    def __init__(self, step: PlanStep) -> None:
        self.step = step
        self.handed = 0

    def __iter__(self) -> Iterator[PlanStep]:
        while True:
            self.handed += 1
            yield self.step


class _Growing(list):
    """A list that hands out its own steps once, then ``later`` on every read.

    A read counts only once an iterator it handed out is advanced, so an
    iterator taken and never read counts for nothing.
    """

    def __init__(self, steps: tuple[PlanStep, ...], later: tuple[PlanStep, ...]) -> None:
        super().__init__(steps)
        self.later = later
        self.reads = 0

    def __iter__(self) -> Iterator[PlanStep]:
        return self._read()

    def _read(self) -> Iterator[PlanStep]:
        self.reads += 1
        yield from (list.__iter__(self) if self.reads == 1 else self.later)


class _SharedIterator:
    """An iterable that hands out the same iterator every time it is asked."""

    def __init__(self, steps: list[PlanStep]) -> None:
        self._iterator = iter(steps)

    def __iter__(self) -> Iterator[PlanStep]:
        return self._iterator


class _Scripted(list):
    """A list whose every read hands out a scripted sequence of steps.

    It holds its first read's steps itself. Read ``i`` hands out
    ``reads[i]``, and every read after the script ends hands out its last
    entry. A read counts once its iterator is advanced, and every step
    handed out is counted as well.
    """

    def __init__(self, *reads: tuple[PlanStep, ...]) -> None:
        super().__init__(reads[0])
        self.scripted = reads
        self.reads = 0
        self.handed = 0

    def __iter__(self) -> Iterator[PlanStep]:
        return self._read()

    def _read(self) -> Iterator[PlanStep]:
        self.reads += 1
        for step in self.scripted[min(self.reads, len(self.scripted)) - 1]:
            self.handed += 1
            yield step


class _PlainStepList(list):
    """An ordinary list subclass."""


class _LyingFields(dict):
    """An instance dictionary whose item reads show ``shown``, not what it holds.

    Attribute reads use a dictionary's own storage rather than its methods, so
    a plan holding this would show one set of fields to anything reading the
    dictionary and another to anything reading the plan's attributes.
    """

    def __init__(self, held: dict[str, Any], shown: dict[str, Any]) -> None:
        super().__init__(held)
        self.shown = shown

    def __getitem__(self, name: str) -> Any:
        return self.shown[name] if name in self.shown else super().__getitem__(name)


class _SwappingArguments(Mapping):
    """Step arguments that swap their plan's steps the first time they are read."""

    def __init__(self, values: dict[str, Any], plan: Plan, steps: tuple[PlanStep, ...]) -> None:
        self.values = values
        self.plan: Plan | None = plan
        self.steps = steps

    def __getitem__(self, name: str) -> Any:
        return self.values[name]

    def __len__(self) -> int:
        return len(self.values)

    def __iter__(self) -> Iterator[str]:
        if self.plan is not None:
            object.__setattr__(self.plan, "steps", self.steps)
            self.plan = None
        return iter(self.values)


def _lie_about_steps(plan: Plan, real: Any) -> None:
    """Make ``plan`` show its own steps through its dictionary but hold ``real``."""
    held = dict(vars(plan))
    shown = {"steps": held["steps"]}
    held["steps"] = real
    object.__setattr__(plan, "__dict__", _LyingFields(held, shown))


# What the planner is made to answer in the deterministic tests below.
DETERMINISTIC_REQUEST = "synthetic calculations"


def _approved() -> tuple[PlanStep, ...]:
    """Return the two calculations an approval covers, as new steps."""
    return (_calculation(1), _calculation(2))


def _others() -> tuple[PlanStep, ...]:
    """Return two other calculations a container could hand out instead."""
    return (_calculation(7), _calculation(8))


APPROVED_RESULT = "1 + 1 = 2\n2 + 2 = 4"


def _planned(harness: _PlanHarness, steps: Any) -> Plan:
    """Make the planner answer every request with a plan of ``steps``."""
    plan = Plan(DETERMINISTIC_REQUEST, steps)
    harness.create_plan.return_value = plan
    return plan


class _Runs:
    """Every real run of a model-eligible tool, in order, as it was reached.

    Each run notes the tool, the action, the very arguments object it was
    handed, and how many audit summaries were committed at that moment, read
    through a separate connection. ``errors`` and ``answers`` replace the run
    with that index: an error is raised, as a tool that crashed or was
    interrupted would raise it, and an answer is returned instead of running
    the tool. ``after`` runs a callable once the real tool has run.
    """

    def __init__(self, test: unittest.TestCase, harness: _Harness) -> None:
        self.calls: list[tuple[str, str, Mapping[str, Any]]] = []
        self.committed: list[int] = []
        self.errors: dict[int, BaseException] = {}
        self.answers: dict[int, object] = {}
        self.after: dict[int, Callable[[], None]] = {}
        runs = self
        for tool_class in (CalculatorTool, FilesystemTool, SystemTool):

            def execute(
                tool: Any,
                action: str,
                arguments: Mapping[str, Any],
                *,
                _original: Callable[..., Any] = tool_class.execute,
            ) -> object:
                index = len(runs.calls)
                runs.calls.append((tool.name, action, arguments))
                runs.committed.append(harness.row_counts()["audit_summaries"])
                if index in runs.errors:
                    raise runs.errors[index]
                if index in runs.answers:
                    return runs.answers[index]
                result = _original(tool, action, arguments)
                if index in runs.after:
                    runs.after[index]()
                return result

            active = patch.object(tool_class, "execute", execute)
            active.start()
            test.addCleanup(active.stop)

    @property
    def actions(self) -> list[str]:
        """Return the identifier of every action run, in order."""
        return [f"{tool}.{action}" for tool, action, _ in self.calls]


class _PlanHarness(_Harness):
    """The audit gate's harness, with every eligible tool's runs recorded.

    The compiler and its re-check are spied on too, as Brain reaches them, so
    a test can prove neither is asked anything after the approval.
    """

    def __init__(self, test: unittest.TestCase, *replies: str, **options: Any) -> None:
        super().__init__(test, _Model(*replies), **options)
        self.runs = _Runs(test, self)
        self.compile = _spy(test, brain_module, "compile_proposal")
        self.recheck = _spy(test, brain_module, "recheck_model_plan")
        self.real_summary = partial(Memory.record_audit_summary, self.memory)
        self.real_outcome = partial(Memory.record_audit_outcome, self.memory)
        self.holders: list[sqlite3.Connection] = []
        test.addCleanup(self._release)

    def lock(self) -> None:
        """Hold an exclusive lock on the database until the test ends."""
        holder = sqlite3.connect(self.database_path, isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        self.holders.append(holder)

    def _release(self) -> None:
        """Release every lock this harness took."""
        for holder in self.holders:
            holder.close()


def _on_call(spy: Any, number: int, action: Callable[..., Any]) -> list[tuple]:
    """Make call ``number`` of a spied method run ``action`` instead.

    Every other call runs the real method, as the spy wraps it.
    """
    made: list[tuple] = []

    def effect(*args: Any, **kwargs: Any) -> Any:
        made.append(args)
        if len(made) == number:
            return action(*args, **kwargs)
        return DEFAULT

    spy.side_effect = effect
    return made


def _then_interrupt(real: Callable[..., Any]) -> Callable[..., Any]:
    """Return a write that commits for real and is then interrupted."""

    def run(*args: Any, **kwargs: Any) -> Any:
        real(*args, **kwargs)
        raise KeyboardInterrupt

    return run


def _raise(error: BaseException) -> Callable[..., Any]:
    """Return a callable that raises ``error``."""

    def run(*args: Any, **kwargs: Any) -> Any:
        raise error

    return run


class _PlanTestCase(_GateTestCase):
    """Drivers for a model-proposed plan's offer and its confirmation."""

    def harness(self, *replies: str, **options: Any) -> _PlanHarness:
        """Return a harness whose model answers with ``replies`` in turn."""
        return _PlanHarness(self, *replies, **options)

    def offer_plan(
        self,
        harness: _PlanHarness,
        expected: str | None = None,
        request: str = REQUEST,
    ) -> BrainResponse:
        """Submit a request the model answers with a plan awaiting approval."""
        offered = harness.session.submit(request)
        if expected is not None:
            self.assertEqual(offered.text, expected)
        self.assertTrue(offered.needs_confirmation)
        self.assertIsNotNone(harness.session.pending_approval)
        return offered

    def confirm(self, harness: _PlanHarness) -> BrainResponse:
        """Confirm the pending plan with the clock fixed."""
        with patch("tools.system._current_moment", return_value=CLOCK):
            return harness.session.confirm()

    def interrupt_connection(self, harness: _PlanHarness, number: int) -> list[Any]:
        """Interrupt the commit of the memory layer's ``number``th connection."""
        connect = harness.memory._connect
        opened: list[Any] = []

        def connecting() -> Any:
            connection = connect()
            if len(opened) + 1 == number:
                connection = _InterruptedCommit(connection)
            opened.append(connection)
            return connection

        active = patch.object(harness.memory, "_connect", side_effect=connecting)
        active.start()
        self.addCleanup(active.stop)
        return opened

    def assert_spent(self, harness: _PlanHarness) -> None:
        """Assert the approval is gone and nothing runs if confirmed again."""
        runs = len(harness.runs.calls)
        summaries = harness.record_audit_summary.call_count
        self.assertIsNone(harness.session.pending_approval)
        self.assertEqual(self.confirm(harness).text, CONFIRMATION_REFUSED)
        self.assertEqual(len(harness.runs.calls), runs)
        self.assertEqual(harness.record_audit_summary.call_count, summaries)
        self.assertEqual(harness.provider.calls, 1)
        self.assertEqual(harness.create_plan.call_count, 1)


# --- The bound and the shape -------------------------------------------------


class PlanBoundTests(unittest.TestCase):
    """Prove every plan holds at most five steps, and a model plan one to five."""

    def setUp(self) -> None:
        self.step = PlanStep("calculator", "calculate", {"expression": "2 + 2"})

    def test_the_bounds_and_their_fixed_texts(self) -> None:
        self.assertEqual(PLAN_MAX_STEPS, 5)
        self.assertEqual(MODEL_PROPOSAL_MIN_STEPS, 2)
        self.assertEqual(PLAN_STEP_LIMIT_MESSAGE, "A plan may hold at most 5 steps.")
        self.assertEqual(
            MODEL_PLAN_SHAPE_MESSAGE,
            "A model-originated plan must be transient, with 1 to 5 steps and no "
            "canned response.",
        )
        self.assertEqual(
            PLAN_STEP_LIMIT_REFUSED_MESSAGE,
            "Maria refused that plan because a plan may hold at most 5 steps.",
        )

    def test_model_plans_hold_one_to_five_steps_and_no_other_count(self) -> None:
        for count in range(1, 6):
            with self.subTest(count=count):
                plan = _model_plan(*(self.step,) * count)
                self.assertTrue(is_model_plan(plan))
                self.assertTrue(is_within_step_bound(plan))
        for count in (0, 6, 7):
            with self.subTest(count=count):
                with self.assertRaises(ValueError) as raised:
                    _model_plan(*(self.step,) * count)
                self.assertEqual(str(raised.exception), MODEL_PLAN_SHAPE_MESSAGE)

    def test_deterministic_plans_keep_zero_steps_and_hold_at_most_five(self) -> None:
        help_plan = Plan("help", (), "Supported commands")
        self.assertEqual(help_plan.steps, ())
        self.assertTrue(is_within_step_bound(help_plan))
        for count in range(0, 6):
            with self.subTest(count=count):
                plan = Plan(REQUEST, (self.step,) * count)
                self.assertIs(plan.origin, PlanOrigin.DETERMINISTIC)
                self.assertTrue(is_within_step_bound(plan))
        for steps in ((self.step,) * 6, [self.step] * 6):
            with self.subTest(container=type(steps).__name__):
                with self.assertRaises(ValueError) as raised:
                    Plan(REQUEST, steps)
                self.assertEqual(str(raised.exception), PLAN_STEP_LIMIT_MESSAGE)

    def test_no_new_shape_is_asked_of_a_deterministic_plan(self) -> None:
        # Only the count is new: the model plan's exact tuple, exact step, and
        # exact argument types are never asked of a deterministic plan.
        class _StepSubclass(PlanStep):
            """A PlanStep subtype, which no model plan may hold."""

        listed = Plan(REQUEST, [self.step] * 5)
        self.assertIs(type(listed.steps), list)
        subclassed = Plan(REQUEST, (_StepSubclass("system", "current_time", {}),))
        self.assertTrue(is_within_step_bound(subclassed))
        self.assertFalse(is_model_plan(subclassed))
        # Steps that cannot be read without consuming them are not refused
        # when the plan is built, exactly as before, but no boundary will
        # confirm or run them.
        generated = Plan(REQUEST, (step for step in (self.step,)))
        self.assertFalse(is_within_step_bound(generated))

    def test_a_plan_altered_after_construction_is_counted_again(self) -> None:
        for origin in PlanOrigin:
            with self.subTest(origin=origin):
                if origin is PlanOrigin.MODEL:
                    plan = _model_plan(self.step, self.step)
                else:
                    plan = Plan(REQUEST, (self.step, self.step))
                self.assertTrue(is_within_step_bound(plan))

                object.__setattr__(plan, "steps", (self.step,) * 6)

                self.assertFalse(is_within_step_bound(plan))
                self.assertFalse(is_model_plan(plan))
        nothing = Plan(REQUEST, ())
        object.__setattr__(nothing, "steps", None)
        self.assertFalse(is_within_step_bound(nothing))
        self.assertFalse(is_within_step_bound(object()))

    def test_six_steps_in_a_container_reporting_five_are_refused_when_built(self) -> None:
        for container in (_Underreported, _UnderreportedTuple):
            with self.subTest(container=container.__name__):
                six = container(_calculations(6))
                self.assertEqual(len(six), 5)

                with self.assertRaises(ValueError) as raised:
                    Plan(REQUEST, six)

                self.assertEqual(str(raised.exception), PLAN_STEP_LIMIT_MESSAGE)

    def test_a_plan_altered_to_six_steps_reporting_five_is_out_of_bounds(self) -> None:
        for origin in PlanOrigin:
            with self.subTest(origin=origin):
                if origin is PlanOrigin.MODEL:
                    plan = _model_plan(self.step, self.step)
                else:
                    plan = Plan(REQUEST, (self.step, self.step))

                object.__setattr__(plan, "steps", _Underreported(_calculations(6)))

                self.assertFalse(is_within_step_bound(plan))
                self.assertIsNone(bounded_steps(plan))
                self.assertFalse(is_model_plan(plan))

    def test_containers_of_up_to_five_steps_are_kept_and_read_exactly(self) -> None:
        class _StepList(list):
            """An ordinary list subclass."""

        class _StepTuple(tuple):
            """An ordinary tuple subclass."""

        for container in (tuple, list, _StepList, _StepTuple, _Underreported):
            for count in range(PLAN_MAX_STEPS + 1):
                with self.subTest(container=container.__name__, count=count):
                    members = _calculations(count)
                    steps = container(members)

                    plan = Plan(REQUEST, steps)

                    # The container is kept as it is: never copied or converted.
                    self.assertIs(plan.steps, steps)
                    read = bounded_steps(plan)
                    self.assertIs(type(read), tuple)
                    self.assertEqual(len(read), count)
                    for held, member in zip(read, members):
                        self.assertIs(held, member)
                    self.assertTrue(is_within_step_bound(plan))

    def test_steps_are_read_one_past_the_bound_and_never_consumed(self) -> None:
        endless = _Endless(self.step)
        with self.assertRaises(ValueError) as raised:
            Plan(REQUEST, endless)
        self.assertEqual(str(raised.exception), PLAN_STEP_LIMIT_MESSAGE)
        self.assertEqual(endless.handed, PLAN_MAX_STEPS + 1)

        plan = Plan(REQUEST, ())
        object.__setattr__(plan, "steps", endless)
        self.assertFalse(is_within_step_bound(plan))
        self.assertEqual(endless.handed, 2 * (PLAN_MAX_STEPS + 1))

        # One-shot steps are never read at all, so nothing they hold is lost:
        # an iterator, and anything handing out the same iterator each time.
        members = _calculations(2)
        for label, steps in (
            ("an iterator", (step for step in members)),
            ("a shared iterator", _SharedIterator(members)),
        ):
            with self.subTest(steps=label):
                plan = Plan(REQUEST, steps)
                self.assertIsNone(bounded_steps(plan))
                self.assertFalse(is_within_step_bound(plan))
                self.assertEqual(list(plan.steps), members)

    def test_every_step_of_a_model_plan_keeps_the_strict_shape(self) -> None:
        good = PlanStep("system", "current_time", {})
        alterations = (
            ("a later step that is not a step", (good, "system.current_time")),
            ("a later step held twice in a list", [good, good]),
        )
        for label, steps in alterations:
            with self.subTest(alteration=label):
                plan = _model_plan(good, good)
                object.__setattr__(plan, "steps", steps)
                self.assertFalse(is_model_plan(plan))

        later = PlanStep("calculator", "calculate", {"expression": "2 + 2"})
        plan = _model_plan(good, later)
        object.__setattr__(later, "arguments", {"expression": "2 + 2"})
        self.assertFalse(is_model_plan(plan))

    def test_a_plan_is_flat_and_no_step_can_hold_a_step_or_a_plan(self) -> None:
        nested = (
            self.step,
            Plan(REQUEST, (self.step,)),
            (self.step,),
            [self.step],
            {"tool": "system"},
            PlanArguments({}),
            1.5,
            True,
            None,
        )
        for value in nested:
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(TypeError):
                    PlanStep("filesystem", "list", {"path": value})


class OwnFieldsTests(unittest.TestCase):
    """Prove the boundaries read a plan's own fields and snapshot it once."""

    def test_a_built_plan_holds_every_field_itself(self) -> None:
        plan = Plan(REQUEST, ())
        self.assertIs(vars(plan)["origin"], PlanOrigin.DETERMINISTIC)
        self.assertIs(own_origin(plan), PlanOrigin.DETERMINISTIC)
        self.assertIs(own_origin(_model_plan(_calculation(1))), PlanOrigin.MODEL)
        # The default origin's fingerprint is the one it always had.
        self.assertEqual(
            Plan(REQUEST, (_calculation(1),)).fingerprint,
            Plan(REQUEST, (_calculation(1),), origin=PlanOrigin.DETERMINISTIC).fingerprint,
        )

    def test_a_missing_or_inexact_field_leaves_no_origin_and_no_snapshot(self) -> None:
        alterations: dict[str, Callable[[Plan], None]] = {
            "origin deleted": lambda plan: object.__delattr__(plan, "origin"),
            "origin as text": lambda plan: object.__setattr__(plan, "origin", "deterministic"),
            "origin as None": lambda plan: object.__setattr__(plan, "origin", None),
            "recording deleted": lambda plan: object.__delattr__(plan, "recording"),
            "recording as text": lambda plan: object.__setattr__(plan, "recording", "record"),
            "response deleted": lambda plan: object.__delattr__(plan, "response_if_no_action"),
            "request deleted": lambda plan: object.__delattr__(plan, "original_request"),
            "steps deleted": lambda plan: object.__delattr__(plan, "steps"),
        }
        for label, alter in alterations.items():
            with self.subTest(alteration=label):
                plan = Plan(REQUEST, (_calculation(1),))
                alter(plan)
                self.assertIsNone(own_origin(plan))
                self.assertIsNone(plan_snapshot(plan))
        # Attribute lookup would have read the class default instead.
        plan = Plan(REQUEST, ())
        object.__delattr__(plan, "origin")
        self.assertIs(plan.origin, PlanOrigin.DETERMINISTIC)
        self.assertIsNone(own_origin(plan))
        self.assertIsNone(own_origin(object()))
        self.assertIsNone(plan_snapshot(object()))

    def test_a_plan_holding_an_exact_tuple_is_its_own_snapshot(self) -> None:
        plans = {
            "deterministic": Plan(REQUEST, _approved()),
            "no action": Plan("help", (), "Supported commands"),
            "model": _model_plan(_calculation(1), _calculation(2)),
        }
        for label, plan in plans.items():
            with self.subTest(plan=label):
                self.assertIs(plan_snapshot(plan), plan)

    def test_any_other_container_is_copied_with_every_field_exactly(self) -> None:
        for container in (list, _PlainStepList, _Underreported):
            with self.subTest(container=container.__name__):
                members = _calculations(3)
                plan = Plan(
                    REQUEST,
                    container(members),
                    "a canned response",
                    RecordingPolicy.TRANSIENT,
                )

                snapshot = plan_snapshot(plan)

                self.assertIs(type(snapshot), Plan)
                self.assertIsNot(snapshot, plan)
                self.assertIs(type(snapshot.steps), tuple)
                self.assertEqual(len(snapshot.steps), 3)
                for held, member in zip(snapshot.steps, members):
                    self.assertIs(held, member)
                # Every field the dataclass declares, not a list kept by hand.
                for field in dataclasses.fields(Plan):
                    if field.name != "steps":
                        self.assertIs(vars(snapshot)[field.name], vars(plan)[field.name])
                self.assertEqual(snapshot.fingerprint, plan.fingerprint)
                # The plan itself keeps the container it was built with.
                self.assertIs(type(plan.steps), container)

    def test_a_model_plan_needs_exact_text_as_its_request(self) -> None:
        class _Text(str):
            """A str subclass, equal to the text it holds."""

        for label, request in (("a number", 42), ("a str subclass", _Text(REQUEST))):
            with self.subTest(request=label):
                with self.assertRaises(ValueError) as raised:
                    Plan(
                        request,
                        (_calculation(1),),
                        recording=RecordingPolicy.TRANSIENT,
                        origin=PlanOrigin.MODEL,
                    )
                self.assertEqual(str(raised.exception), MODEL_PLAN_SHAPE_MESSAGE)

                plan = _model_plan(_calculation(1))
                object.__setattr__(plan, "original_request", request)

                self.assertFalse(is_model_plan(plan))
                self.assertIsNone(plan_snapshot(plan))

    def test_a_model_plan_is_never_repaired_into_a_snapshot(self) -> None:
        plan = _model_plan(_calculation(1), _calculation(2))
        object.__setattr__(plan, "steps", list(plan.steps))

        self.assertIsNone(plan_snapshot(plan))

    def test_a_snapshot_reads_the_steps_once_and_within_the_bound(self) -> None:
        plan = Plan(REQUEST, ())
        endless = _Endless(_calculation(1))
        object.__setattr__(plan, "steps", endless)
        self.assertIsNone(plan_snapshot(plan))
        self.assertEqual(endless.handed, PLAN_MAX_STEPS + 1)

        scripted = _Scripted(_approved(), tuple(_calculations(6)))
        object.__setattr__(plan, "steps", scripted)
        snapshot = plan_snapshot(plan)
        self.assertEqual(len(snapshot.steps), 2)
        # Fingerprinting the snapshot never reads the container again.
        self.assertEqual(snapshot.fingerprint, Plan(REQUEST, _approved()).fingerprint)
        self.assertEqual(scripted.reads, 1)


# --- Compilation -------------------------------------------------------------


class EnvelopeCompilationTests(unittest.TestCase):
    """Prove a proposal compiles all or nothing, each action through every gate."""

    def test_two_and_five_actions_compile_in_order_exactly_as_proposed(self) -> None:
        plan = _compile(_envelope(TIME, _sum(2)))

        self.assertEqual(
            plan,
            _model_plan(
                PlanStep("system", "current_time", {}),
                PlanStep("calculator", "calculate", {"expression": "2 + 2"}),
            ),
        )
        self.assertTrue(is_model_plan(plan))
        canonical = (
            b'{"origin":"model","original_request":"' + REQUEST.encode() + b'",'
            b'"recording":"transient","response_if_no_action":null,'
            b'"steps":[{"action":"current_time","arguments":{},'
            b'"tool_name":"system"},{"action":"calculate",'
            b'"arguments":{"expression":"2 + 2"},"tool_name":"calculator"}]}'
        )
        self.assertEqual(plan.fingerprint, hashlib.sha256(canonical).hexdigest())

        reordered = _compile(_envelope(_sum(2), TIME))
        self.assertEqual(reordered.steps, tuple(reversed(plan.steps)))
        self.assertNotEqual(reordered.fingerprint, plan.fingerprint)

        five = _compile(_envelope(*(_sum(n) for n in range(1, 6))))
        self.assertEqual(
            five.steps,
            tuple(
                PlanStep("calculator", "calculate", {"expression": f"{n} + {n}"})
                for n in range(1, 6)
            ),
        )

    def test_one_action_keeps_its_own_shape_and_its_fingerprint(self) -> None:
        plan = compile_proposal(
            "calculate 2 + 2",
            ReasoningResponse(json.dumps(_sum(2))),
            BUILT_IN_ACTION_CONTRACTS,
            _quiet_logger("tests.multi_step_plans.single"),
        )

        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.fingerprint, MODEL_STEP_FINGERPRINT)

    def test_an_envelope_must_hold_two_to_five_actions(self) -> None:
        for count in (0, 1, 6, 7):
            with self.subTest(count=count):
                outcome = _compile(_envelope(*[TIME] * count))
                self.assertIs(outcome, ProposalRejection.MALFORMED_PLAN)
                self.assertEqual(outcome.message, MALFORMED_PLAN)

    def test_every_other_envelope_fault_is_the_one_fixed_refusal(self) -> None:
        faults = {
            "steps as text": "system.current_time",
            "steps as an object": {"0": TIME, "1": TIME},
            "steps as null": None,
            "steps as a number": 2,
            "steps as true": True,
            "a step that is text": [TIME, "system.current_time"],
            "a nested list": [TIME, [TIME, TIME]],
            "a nested envelope": [TIME, {"steps": [TIME, TIME]}],
            "a step missing its arguments": [
                TIME,
                {"tool": "system", "action": "current_time"},
            ],
            "a step with an approval field": [TIME, {**TIME, "approved": True}],
            "a step with a depends_on field": [TIME, {**TIME, "depends_on": 1}],
            "a step whose arguments are a list": [TIME, {**TIME, "arguments": []}],
            "a step whose tool is a number": [TIME, {**TIME, "tool": 7}],
        }
        for label, steps in faults.items():
            with self.subTest(fault=label):
                text = json.dumps({"steps": steps})
                self.assertIs(_compile(text), ProposalRejection.MALFORMED_PLAN)

    def test_an_object_that_is_not_exactly_an_envelope_is_a_malformed_action(
        self,
    ) -> None:
        not_envelopes = {
            "an approval field beside the steps": {
                "steps": [TIME, TIME],
                "approved": True,
            },
            "a tool beside the steps": {"steps": [TIME, TIME], "tool": "system"},
            "steps beside one action": {**TIME, "steps": [TIME, TIME]},
            "a differently spelled steps": {"Steps": [TIME, TIME]},
            "an empty object": {},
        }
        for label, document in not_envelopes.items():
            with self.subTest(document=label):
                outcome = _compile(json.dumps(document))
                self.assertIs(outcome, ProposalRejection.MALFORMED_PROPOSAL)
                self.assertEqual(outcome.message, MALFORMED_PROPOSAL)

    def test_duplicate_keys_at_every_level_are_refused(self) -> None:
        texts = (
            '{"steps":[{"tool":"system","action":"current_time","arguments":{}},'
            '{"tool":"system","action":"current_time","arguments":{}}],'
            '"steps":[]}',
            '{"steps":[{"tool":"system","tool":"system","action":"current_time",'
            '"arguments":{}},{"tool":"system","action":"current_time",'
            '"arguments":{}}]}',
            '{"steps":[{"tool":"filesystem","action":"list",'
            '"arguments":{"path":"a","path":"b"}},{"tool":"system",'
            '"action":"current_time","arguments":{}}]}',
        )
        for text in texts:
            with self.subTest(text=text[:40]):
                self.assertIs(_compile(text), ProposalRejection.MALFORMED_OUTPUT)

    def test_strict_parsing_still_refuses_what_json_lacks_or_adds(self) -> None:
        envelope = _envelope(TIME, TIME)
        texts = (
            envelope + " " + envelope,
            envelope + " 7",
            "Here is the plan: " + envelope,
            "```json\n" + envelope + "\n```",
            envelope.replace("{}", '{"expression": NaN}', 1),
        )
        for text in texts:
            with self.subTest(text=ascii(text[:24])):
                self.assertIs(_compile(text), ProposalRejection.MALFORMED_OUTPUT)

    def test_a_list_of_proposals_is_never_a_proposal_attempt(self) -> None:
        listed = json.dumps([TIME, TIME])
        self.assertFalse(is_proposal_attempt(listed))
        self.assertFalse(is_proposal_attempt(" \n" + listed))
        self.assertTrue(is_proposal_attempt(_envelope(TIME, TIME)))
        self.assertIs(_compile(listed), ProposalRejection.MALFORMED_PROPOSAL)

    def test_the_byte_cap_covers_the_whole_envelope(self) -> None:
        self.assertEqual(MODEL_PROPOSAL_MAX_BYTES, 4096)
        base = len(_envelope(_list(""), TIME).encode("utf-8"))
        at_cap = _envelope(_list("a" * (MODEL_PROPOSAL_MAX_BYTES - base)), TIME)
        self.assertEqual(len(at_cap.encode("utf-8")), MODEL_PROPOSAL_MAX_BYTES)
        self.assertTrue(is_model_plan(_compile(at_cap)))

        over = _envelope(_list("a" * (MODEL_PROPOSAL_MAX_BYTES - base + 1)), TIME)
        self.assertIs(_compile(over), ProposalRejection.OVERSIZED)
        # Five long but individually valid listings are refused together.
        five = _envelope(*[_list("b" * 900)] * 5)
        self.assertGreater(len(five.encode("utf-8")), MODEL_PROPOSAL_MAX_BYTES)
        self.assertIs(_compile(five), ProposalRejection.OVERSIZED)

    def test_nesting_beyond_eight_is_refused_before_parsing(self) -> None:
        self.assertEqual(MODEL_PROPOSAL_MAX_NESTING, 8)
        # Envelope, list, action, and arguments are four levels.
        within = {"path": [[[["x"]]]]}
        beyond = {"path": [[[[["x"]]]]]}
        self.assertIs(
            _compile(_envelope(_action("filesystem", "list", within), TIME)),
            ProposalRejection.WRONG_TYPE,
        )
        self.assertIs(
            _compile(_envelope(_action("filesystem", "list", beyond), TIME)),
            ProposalRejection.MALFORMED_OUTPUT,
        )

    def test_an_invalid_action_at_any_position_refuses_the_whole_proposal(
        self,
    ) -> None:
        invalid = {
            ProposalRejection.NOT_MODEL_ELIGIBLE: CLOSED,
            ProposalRejection.UNKNOWN_TOOL: _action("weather", "forecast", {}),
            ProposalRejection.UNKNOWN_ACTION: _action("system", "reboot", {}),
            ProposalRejection.MISSING_ARGUMENT: _action("calculator", "calculate", {}),
            ProposalRejection.EXTRA_ARGUMENT: _action(
                "system", "current_time", {"zone": "UTC"}
            ),
            ProposalRejection.WRONG_TYPE: _action(
                "calculator", "calculate", {"expression": 7}
            ),
            ProposalRejection.OUT_OF_RANGE: _action(
                "calculator", "calculate", {"expression": "1" * 201}
            ),
            ProposalRejection.UNSAFE_TEXT: _action(
                "calculator", "calculate", {"expression": "2 + 2" + chr(0x202E)}
            ),
            ProposalRejection.UNSAFE_PATH: _list("../outside"),
        }
        for total in (2, 5):
            for position in range(total):
                for rejection, bad in invalid.items():
                    with self.subTest(total=total, position=position, bad=rejection):
                        actions = [TIME] * total
                        actions[position] = bad
                        self.assertIs(_compile(_envelope(*actions)), rejection)

    def test_the_first_failing_action_decides_the_refusal(self) -> None:
        unsafe = _list("../outside")
        self.assertIs(
            _compile(_envelope(TIME, CLOSED, TIME, unsafe)),
            ProposalRejection.NOT_MODEL_ELIGIBLE,
        )
        self.assertIs(
            _compile(_envelope(TIME, unsafe, TIME, CLOSED)),
            ProposalRejection.UNSAFE_PATH,
        )

    def test_reference_like_values_compile_as_literal_text(self) -> None:
        literals = (
            _action("calculator", "calculate", {"expression": "$1 + 1"}),
            _list("{{step1}}"),
            _list("$1"),
            _action("calculator", "calculate", {"expression": "{{steps[0].result}}"}),
        )
        plan = _compile(_envelope(*literals))

        self.assertEqual(
            [dict(step.arguments) for step in plan.steps],
            [literal["arguments"] for literal in literals],
        )
        self.assertIsNone(recheck_model_plan(plan, BUILT_IN_ACTION_CONTRACTS))

    def test_the_envelope_refusal_repeats_nothing_supplied(self) -> None:
        self.assertEqual(ProposalRejection.MALFORMED_PLAN.code, "malformed_plan")
        hostile = json.dumps(
            {"steps": [TIME, {FAKE_NAME_SENTINEL: FAKE_PAYLOAD_SENTINEL}]}
        )
        logger = _quiet_logger("tests.multi_step_plans.privacy")
        with self.assertLogs(logger, level="DEBUG") as captured:
            outcome = compile_proposal(
                REQUEST,
                ReasoningResponse(hostile),
                BUILT_IN_ACTION_CONTRACTS,
                logger,
            )

        self.assertIs(outcome, ProposalRejection.MALFORMED_PLAN)
        for text in (outcome.message, *captured.output):
            self.assertNotIn(FAKE_NAME_SENTINEL, text)
            self.assertNotIn(FAKE_PAYLOAD_SENTINEL, text)
        self.assertTrue(
            any("code=malformed_plan" in line for line in captured.output)
        )

    def test_the_recheck_covers_every_step(self) -> None:
        plan = _compile(_envelope(TIME, _sum(2), LIST_ROOT))
        self.assertIsNone(recheck_model_plan(plan, BUILT_IN_ACTION_CONTRACTS))

        closed = _registry_with_eligibility(
            "filesystem.list",
            ModelEligibility.NOT_MODEL_ELIGIBLE,
        )
        self.assertIs(
            recheck_model_plan(plan, closed),
            ProposalRejection.NOT_MODEL_ELIGIBLE,
        )

        object.__setattr__(
            plan.steps[1],
            "arguments",
            PlanArguments({"expression": "1" * 201}),
        )
        self.assertIs(
            recheck_model_plan(plan, BUILT_IN_ACTION_CONTRACTS),
            ProposalRejection.OUT_OF_RANGE,
        )

        later = _compile(_envelope(TIME, _sum(2), LIST_ROOT))
        object.__setattr__(later.steps[2], "arguments", PlanArguments({"path": "../x"}))
        self.assertIs(
            recheck_model_plan(later, BUILT_IN_ACTION_CONTRACTS),
            ProposalRejection.UNSAFE_PATH,
        )

        object.__setattr__(later, "steps", later.steps * 2)
        with self.assertRaises(ValueError) as raised:
            recheck_model_plan(later, BUILT_IN_ACTION_CONTRACTS)
        self.assertEqual(str(raised.exception), COMPILER_PLAN_MESSAGE)


# --- The offer ----------------------------------------------------------------


class MultiStepOfferTests(_PlanTestCase):
    """Prove a plan of several steps is offered once, whole, or refused whole."""

    def test_a_plan_of_three_steps_is_offered_once_exactly_as_compiled(self) -> None:
        harness = self.harness(_envelope(TIME, _sum(2), LIST_MISSING))

        offered = self.offer_plan(
            harness,
            _plan_header(3) + "\n"
            "Step 1 of 3\n"
            "Tool: system\n"
            "Action: current_time\n"
            "Arguments: {}\n"
            "Step 2 of 3\n"
            "Tool: calculator\n"
            "Action: calculate\n"
            'Arguments: {"expression": "2 + 2"}\n'
            "Step 3 of 3\n"
            "Tool: filesystem\n"
            "Action: list\n"
            'Arguments: {"path": "missing"}\n'
            "\n"
            "No memory context supplied.",
        )

        plan = offered.plan
        self.assertTrue(is_model_plan(plan))
        self.assertEqual(
            plan.steps,
            (
                PlanStep("system", "current_time", {}),
                PlanStep("calculator", "calculate", {"expression": "2 + 2"}),
                PlanStep("filesystem", "list", {"path": "missing"}),
            ),
        )
        pending = harness.session.pending_approval
        self.assertIs(pending.plan, plan)
        self.assertEqual(pending.fingerprint, plan.fingerprint)
        self.assertEqual(repr(pending), "PendingApproval(<redacted>)")
        self.assertEqual(offered.text.count(NO_CONTEXT_NOTICE), 1)
        # Every step's permission was decided before the offer, and nothing
        # was prepared, audited, or run.
        self.assertEqual(
            harness.check.call_args_list,
            [
                call("system", "current_time"),
                call("calculator", "calculate"),
                call("filesystem", "list"),
            ],
        )
        harness.recheck.assert_called_once()
        harness.prepare_confirmation.assert_not_called()
        harness.record_audit_summary.assert_not_called()
        harness.execute_tool.assert_not_called()
        self.assertEqual(harness.runs.calls, [])
        self.assertEqual(harness.row_counts(), ZERO_ROWS)
        self.assertEqual(harness.provider.calls, 1)
        self.assertEqual(harness.session.conversation, ())
        self.assertIsNone(offered.conversation)

    def test_two_and_five_step_plans_number_every_step_exactly(self) -> None:
        harness = self.harness(_envelope(_list("My  Files"), TIME))
        self.offer_plan(
            harness,
            _approval(
                ("filesystem", "list", '{"path": "My\\u0020\\u0020Files"}'),
                ("system", "current_time", "{}"),
            ),
        )

        harness = self.harness(_envelope(*(_sum(n) for n in range(1, 6))))
        self.offer_plan(
            harness,
            _approval(
                *(
                    ("calculator", "calculate", f'{{"expression": "{n} + {n}"}}')
                    for n in range(1, 6)
                )
            ),
        )

    def test_a_denied_step_at_any_position_refuses_the_whole_plan(self) -> None:
        denial = (
            "Permission denied: system.current_time is denied by configuration."
            "\n\nNo memory context supplied."
        )
        for position in range(3):
            with self.subTest(position=position + 1):
                actions = [_sum(1), _sum(2), _sum(3)]
                actions[position] = TIME
                harness = self.harness(
                    _envelope(*actions),
                    denied_actions=frozenset({"system.current_time"}),
                )

                refused = harness.session.submit(REQUEST)

                self.assertEqual(refused.text, denial)
                self.assertFalse(refused.needs_confirmation)
                self.assertIsNone(harness.session.pending_approval)
                self.assertEqual(harness.check.call_count, 3)
                harness.record_audit_summary.assert_not_called()
                harness.execute_tool.assert_not_called()
                self.assertEqual(harness.runs.calls, [])
                self.assertEqual(harness.row_counts(), ZERO_ROWS)
                self.assertEqual(harness.session.confirm().text, CONFIRMATION_REFUSED)
                self.assertEqual(harness.runs.calls, [])

    def test_the_first_denied_step_supplies_the_reason(self) -> None:
        harness = self.harness(
            _envelope(LIST_ROOT, _sum(1), TIME),
            denied_actions=frozenset({"system.current_time", "calculator.calculate"}),
        )

        refused = harness.session.submit(REQUEST)

        self.assertEqual(
            refused.text,
            "Permission denied: calculator.calculate is denied by configuration."
            "\n\nNo memory context supplied.",
        )
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_a_compiler_refused_step_at_any_position_refuses_the_whole_plan(
        self,
    ) -> None:
        for bad, message in ((CLOSED, NOT_MODEL_ELIGIBLE), (_list("../x"), UNSAFE_PATH)):
            for position in range(3):
                with self.subTest(message=message[:20], position=position + 1):
                    actions = [TIME, _sum(2), LIST_ROOT]
                    actions[position] = bad
                    harness = self.harness(_envelope(*actions))

                    refused = harness.session.submit(REQUEST)

                    self.assertEqual(refused.text, f"{message}\n\n{NO_CONTEXT_NOTICE}")
                    self.assertIsNone(harness.session.pending_approval)
                    harness.check.assert_not_called()
                    harness.record_audit_summary.assert_not_called()
                    harness.execute_tool.assert_not_called()
                    self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_a_malformed_envelope_is_refused_with_its_fixed_message(self) -> None:
        harness = self.harness(_envelope(TIME, TIME, TIME, TIME, TIME, TIME))

        refused = harness.session.submit(REQUEST)

        self.assertEqual(refused.text, f"{MALFORMED_PLAN}\n\n{NO_CONTEXT_NOTICE}")
        self.assertIsNone(harness.session.pending_approval)
        harness.check.assert_not_called()
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_a_confirmation_required_step_still_needs_exactly_one_approval(
        self,
    ) -> None:
        harness = self.harness(
            _envelope(TIME, _sum(2)),
            confirmation_required=frozenset({"calculator.calculate"}),
        )
        self.offer_plan(
            harness,
            _approval(
                ("system", "current_time", "{}"),
                ("calculator", "calculate", '{"expression": "2 + 2"}'),
            ),
        )
        harness.prepare_confirmation.assert_not_called()

        confirmed = self.confirm(harness)

        self.assertEqual(
            confirmed.text,
            _report(
                _step(1, 2, "system.current_time", "completed.", CLOCK_REPORT, _recorded(1)),
                _step(2, 2, "calculator.calculate", "completed.", "2 + 2 = 4", _recorded(2)),
            ),
        )
        self.assertEqual(harness.check.call_count, 4)
        harness.prepare_confirmation.assert_not_called()


# --- Confirmation and binding ---------------------------------------------------


class MultiStepConfirmationTests(_PlanTestCase):
    """Prove the approved sequence runs exactly once, and nothing else can."""

    def test_confirming_runs_the_approved_sequence_exactly_and_asks_nothing(
        self,
    ) -> None:
        harness = self.harness(_envelope(TIME, _sum(2), LIST_ROOT))
        plan = self.offer_plan(harness).plan

        confirmed = self.confirm(harness)

        self.assertEqual(
            confirmed,
            BrainResponse(
                _report(
                    _step(1, 3, "system.current_time", "completed.", CLOCK_REPORT, _recorded(1)),
                    _step(2, 3, "calculator.calculate", "completed.", "2 + 2 = 4", _recorded(2)),
                    _step(3, 3, "filesystem.list", "completed.", "Directory is empty.", _recorded(3)),
                ),
                plan=plan,
            ),
        )
        dispatched = harness.execute_tool.call_args_list
        self.assertEqual(
            [made.args[:2] for made in dispatched],
            [("system", "current_time"), ("calculator", "calculate"), ("filesystem", "list")],
        )
        for index, made in enumerate(dispatched):
            with self.subTest(step=index + 1):
                self.assertIs(made.args[2], plan.steps[index].arguments)
                self.assertIs(type(made.args[2]), PlanArguments)
        # Each step's own summary was committed before that step ran.
        self.assertEqual(harness.runs.committed, [1, 2, 3])
        self.assertEqual(
            harness.record_audit_summary.call_args_list,
            [
                call("system.current_time", AuditDecision.OWNER_APPROVED, AuditTarget.NONE),
                call("calculator.calculate", AuditDecision.OWNER_APPROVED, AuditTarget.NONE),
                call(
                    "filesystem.list",
                    AuditDecision.OWNER_APPROVED,
                    AuditTarget.NAMED_FILESYSTEM_ROOT,
                ),
            ],
        )
        self.assertEqual(
            harness.record_audit_outcome.call_args_list,
            [call(n, AuditOutcome.SUCCEEDED) for n in (1, 2, 3)],
        )
        self.assertEqual(
            harness.audit_rows(),
            (
                [
                    _summary_row(1, "system.current_time", "none"),
                    _summary_row(2, "calculator.calculate", "none"),
                    _summary_row(3, "filesystem.list", "named_filesystem_root"),
                ],
                [(1, "succeeded"), (2, "succeeded"), (3, "succeeded")],
            ),
        )
        # The whole plan was checked again before the first summary, and
        # nothing was planned, compiled, prepared, or asked of the model.
        self.assertEqual(harness.recheck.call_count, 2)
        self.assertEqual(harness.check.call_count, 6)
        self.assertEqual(harness.compile.call_count, 1)
        self.assertEqual(harness.create_plan.call_count, 1)
        self.assertEqual(harness.provider.calls, 1)
        harness.prepare_confirmation.assert_not_called()
        # A model-originated plan is transient.
        self.assertEqual(harness.interactions(), [])
        self.assertIsNone(confirmed.conversation)
        self.assertEqual(harness.session.conversation, ())
        self.assert_spent(harness)

    def test_five_steps_all_completing_report_each_under_its_summary(self) -> None:
        harness = self.harness(_envelope(*(_sum(n) for n in range(1, 6))))
        self.offer_plan(harness)

        confirmed = self.confirm(harness)

        self.assertEqual(confirmed.text, _report(*_sums_report(5, 5)))
        self.assertEqual(harness.runs.committed, [1, 2, 3, 4, 5])
        summaries, outcomes = harness.audit_rows()
        self.assertEqual(len(summaries), 5)
        self.assertEqual(outcomes, [(n, "succeeded") for n in range(1, 6)])

    def test_any_change_to_the_pending_plan_runs_nothing(self) -> None:
        def reorder(plan: Plan) -> None:
            first, second, third = plan.steps
            object.__setattr__(plan, "steps", (second, first, third))

        alterations: dict[str, Callable[[Plan], None]] = {
            "steps reordered": reorder,
            "a step's arguments": lambda plan: object.__setattr__(
                plan.steps[1], "arguments", PlanArguments({"expression": "3 + 3"})
            ),
            "a step replaced": lambda plan: object.__setattr__(
                plan,
                "steps",
                (
                    plan.steps[0],
                    PlanStep("calculator", "calculate", {"expression": "9 + 9"}),
                    plan.steps[2],
                ),
            ),
            "a step dropped": lambda plan: object.__setattr__(
                plan, "steps", plan.steps[:2]
            ),
            "a step added": lambda plan: object.__setattr__(
                plan, "steps", plan.steps + plan.steps[:1]
            ),
            "six steps": lambda plan: object.__setattr__(
                plan, "steps", plan.steps * 2
            ),
            "steps in a list": lambda plan: object.__setattr__(
                plan, "steps", list(plan.steps)
            ),
            "a step that is not one": lambda plan: object.__setattr__(
                plan, "steps", (plan.steps[0], "calculator.calculate", plan.steps[2])
            ),
            "the origin": lambda plan: object.__setattr__(
                plan, "origin", PlanOrigin.DETERMINISTIC
            ),
            "the recording policy": lambda plan: object.__setattr__(
                plan, "recording", RecordingPolicy.RECORD
            ),
        }
        for label, alter in alterations.items():
            with self.subTest(alteration=label):
                harness = self.harness(_envelope(TIME, _sum(2), LIST_ROOT))
                self.offer_plan(harness)
                alter(harness.session.pending_approval.plan)

                refused = self.confirm(harness)

                self.assertEqual(refused.text, CONFIRMATION_REFUSED)
                harness.record_audit_summary.assert_not_called()
                harness.execute_tool.assert_not_called()
                self.assertEqual(harness.runs.calls, [])
                self.assertEqual(harness.row_counts(), ZERO_ROWS)
                self.assert_spent(harness)

    def test_cancelling_a_new_request_or_ending_discards_the_approval(self) -> None:
        endings: dict[str, Callable[[_PlanHarness], None]] = {
            "cancel": lambda harness: self.assertTrue(harness.session.cancel()),
            "a new request": lambda harness: harness.session.submit("help"),
            "ending the session": lambda harness: harness.session.end(),
        }
        for label, ending in endings.items():
            with self.subTest(ending=label):
                harness = self.harness(_envelope(TIME, _sum(2)))
                self.offer_plan(harness)

                ending(harness)

                self.assertIsNone(harness.session.pending_approval)
                self.assertEqual(self.confirm(harness).text, CONFIRMATION_REFUSED)
                harness.record_audit_summary.assert_not_called()
                self.assertEqual(harness.runs.calls, [])

    def test_an_approval_is_spent_by_its_one_run_even_when_a_step_fails(self) -> None:
        harness = self.harness(_envelope(_sum(1), BAD_SUM, _sum(3)))
        self.offer_plan(harness)

        self.confirm(harness)

        self.assertEqual(len(harness.runs.calls), 2)
        self.assert_spent(harness)

    def test_a_registry_change_after_the_offer_refuses_the_whole_plan(self) -> None:
        harness = self.harness(_envelope(TIME, _sum(2), LIST_ROOT))
        self.offer_plan(harness)
        harness.tool_manager.contracts = _registry_with_eligibility(
            "filesystem.list",
            ModelEligibility.NOT_MODEL_ELIGIBLE,
        )

        refused = self.confirm(harness)

        self.assertEqual(refused.text, NOT_MODEL_ELIGIBLE)
        harness.record_audit_summary.assert_not_called()
        self.assertEqual(harness.runs.calls, [])
        self.assertEqual(harness.row_counts(), ZERO_ROWS)
        self.assert_spent(harness)

    def test_a_permission_change_after_the_offer_refuses_the_whole_plan(self) -> None:
        harness = self.harness(_envelope(TIME, _sum(2), LIST_ROOT))
        self.offer_plan(harness)
        harness.permissions.denied_actions = harness.permissions.denied_actions | {
            "filesystem.list"
        }

        refused = self.confirm(harness)

        self.assertEqual(
            refused.text,
            "Permission denied: filesystem.list is denied by configuration.",
        )
        harness.record_audit_summary.assert_not_called()
        self.assertEqual(harness.runs.calls, [])
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_a_model_plan_holding_too_many_steps_is_refused_by_brain(self) -> None:
        harness = self.harness(_envelope(TIME, _sum(2)))
        plan = self.offer_plan(harness).plan
        object.__setattr__(plan, "steps", plan.steps * 3)

        refused = harness.brain.execute_confirmed_plan(plan)

        self.assertEqual(refused.text, MODEL_PLAN_REFUSED_MESSAGE)
        harness.record_audit_summary.assert_not_called()
        self.assertEqual(harness.runs.calls, [])
        self.assertEqual(harness.row_counts(), ZERO_ROWS)


# --- Missing fields -----------------------------------------------------------


# The fields every step needs, none of which has a default.
STEP_FIELDS = ("tool_name", "action", "arguments")

# What refusing an altered plan may never write to the log: the request, a
# private argument, or anything of the error reading a missing field raises.
UNLOGGED = (PATH_SENTINEL, REQUEST_SENTINEL, "Traceback", "AttributeError", "has no attribute")


class MissingFieldTests(_PlanTestCase):
    """Prove a pending plan missing any field is refused, never raised."""

    def _offered(self, *actions: dict[str, Any]) -> tuple[_PlanHarness, Plan]:
        """Offer the model's plan of ``actions``, then return it pending."""
        reply = _envelope(*actions) if len(actions) > 1 else json.dumps(actions[0])
        harness = self.harness(reply)
        self.offer_plan(harness, request=PRIVATE_REQUEST)
        return harness, harness.session.pending_approval.plan

    def assert_refused_without_effect(
        self,
        harness: _PlanHarness,
        plan: Plan,
        *,
        by_brain: bool = True,
    ) -> None:
        """Confirm the altered plan, then hand it to Brain directly; nothing may run."""
        checks = harness.check.call_count
        rechecks = harness.recheck.call_count

        refused = self.confirm(harness)

        self.assertEqual(refused, BrainResponse(CONFIRMATION_REFUSED))
        self.assertIsNone(harness.session.pending_approval)
        if by_brain:
            direct = harness.brain.execute_confirmed_plan(plan)
            self.assertEqual(direct.text, MODEL_PLAN_REFUSED_MESSAGE)
            self.assertFalse(direct.needs_confirmation)
        # Nothing was asked of the model, the planner, the compiler, the
        # permissions, the audit trail, or a tool after the offer.
        self.assertEqual(harness.provider.calls, 1)
        self.assertEqual(harness.create_plan.call_count, 1)
        self.assertEqual(harness.compile.call_count, 1)
        self.assertEqual(harness.check.call_count, checks)
        self.assertEqual(harness.recheck.call_count, rechecks)
        harness.record_audit_summary.assert_not_called()
        harness.execute_tool.assert_not_called()
        self.assertEqual(harness.runs.calls, [])
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_the_owner_example_is_refused_with_the_fixed_messages(self) -> None:
        # The second step of an offered plan of three loses its arguments.
        self.start_log()
        harness, plan = self._offered(TIME, _list(PATH_SENTINEL), _sum(2))

        object.__delattr__(plan.steps[1], "arguments")

        self.assertFalse(is_model_plan(plan))
        self.assert_refused_without_effect(harness, plan)
        logged = self.logged()
        self.assertIn("Refused a confirmation that binds no pending plan.", logged)
        self.assertIn("Refused a model-originated plan that is not well formed.", logged)
        assert_log_omits(self, self.log_path, UNLOGGED)

    def test_a_field_deleted_from_any_step_is_refused_the_same_way(self) -> None:
        # A first step missing a field failed the same way before V1-19.
        self.start_log()
        plans = {
            "one step": (_list(PATH_SENTINEL),),
            "three steps": (TIME, _list(PATH_SENTINEL), _sum(2)),
        }
        for label, actions in plans.items():
            for index in range(len(actions)):
                for field in STEP_FIELDS:
                    with self.subTest(plan=label, step=index + 1, field=field):
                        harness, plan = self._offered(*actions)

                        object.__delattr__(plan.steps[index], field)

                        self.assertFalse(is_model_plan(plan))
                        self.assert_refused_without_effect(harness, plan)
        assert_log_omits(self, self.log_path, UNLOGGED)

    def test_a_field_deleted_from_the_plan_is_refused_the_same_way(self) -> None:
        # Fields with a default are refused too: a plan must hold every one
        # of its own fields, so a deleted one is never read as its default.
        self.start_log()
        for field in (
            "steps",
            "original_request",
            "response_if_no_action",
            "recording",
            "origin",
        ):
            with self.subTest(field=field):
                harness, plan = self._offered(TIME, _list(PATH_SENTINEL), _sum(2))

                object.__delattr__(plan, field)

                self.assertFalse(is_model_plan(plan))
                self.assertIsNone(own_origin(plan))
                self.assert_refused_without_effect(harness, plan)
        assert_log_omits(self, self.log_path, UNLOGGED)

    def test_a_request_that_is_not_exact_text_is_refused(self) -> None:
        # Equal to the request it replaces, so the fingerprint cannot tell;
        # only the rule that a model plan's request is exact text refuses it.
        class _Text(str):
            """A str subclass, equal to the text it holds."""

        harness, plan = self._offered(TIME, _sum(2))
        approved = harness.session.pending_approval.fingerprint

        object.__setattr__(plan, "original_request", _Text(plan.original_request))

        self.assertEqual(plan.fingerprint, approved)
        self.assertFalse(is_model_plan(plan))
        self.assert_refused_without_effect(harness, plan)

    def test_a_field_deleted_while_it_held_its_default_is_still_refused(self) -> None:
        # A model plan's canned response is None, its default, so reading it
        # through the class would change nothing the fingerprint covers;
        # only the rule that a plan holds its own fields refuses it.
        harness, plan = self._offered(TIME, _sum(2))
        approved = harness.session.pending_approval.fingerprint

        object.__delattr__(plan, "response_if_no_action")

        self.assertIsNone(plan.response_if_no_action)
        self.assertEqual(plan.fingerprint, approved)
        self.assertFalse(is_model_plan(plan))
        self.assert_refused_without_effect(harness, plan)

    def test_an_approval_missing_its_plan_or_fingerprint_is_refused(self) -> None:
        self.start_log()
        for field in ("plan", "fingerprint"):
            with self.subTest(field=field):
                harness, plan = self._offered(TIME, _list(PATH_SENTINEL))

                object.__delattr__(harness.session.pending_approval, field)

                self.assert_refused_without_effect(harness, plan, by_brain=False)
        assert_log_omits(self, self.log_path, UNLOGGED)

    def test_every_reader_of_the_shape_refuses_rather_than_raises(self) -> None:
        # The compiler's recheck and the approval rendering ask the same
        # question, so each answers a missing field with its fixed message.
        deletions = (
            ("plan", "steps"),
            ("plan", "original_request"),
            ("step", "action"),
            ("step", "arguments"),
        )
        for owner, field in deletions:
            with self.subTest(owner=owner, field=field):
                plan = _model_plan(PlanStep("system", "current_time", {}), _calculation(2))
                object.__delattr__(plan if owner == "plan" else plan.steps[1], field)

                self.assertFalse(is_model_plan(plan))
                with self.assertRaises(ValueError) as rechecked:
                    recheck_model_plan(plan, BUILT_IN_ACTION_CONTRACTS)
                self.assertEqual(str(rechecked.exception), COMPILER_PLAN_MESSAGE)
                with self.assertRaises(ValueError) as rendered:
                    render_model_approval(plan)
                self.assertEqual(str(rendered.exception), MODEL_APPROVAL_MESSAGE)


# --- Reports ----------------------------------------------------------------


class MultiStepReportTests(_PlanTestCase):
    """Prove a plan stops at the first failure and reports exactly what ran."""

    def test_the_owner_approved_failure_report(self) -> None:
        self.start_log()
        harness = self.harness(_envelope(TIME, LIST_MISSING, _sum(2)))
        self.offer_plan(harness)

        confirmed = self.confirm(harness)

        self.assertEqual(
            confirmed.text,
            "Step 1 of 3, system.current_time: completed.\n"
            f"{CLOCK_REPORT}\n"
            "Recorded as audit summary 1.\n"
            "\n"
            "Step 2 of 3, filesystem.list: failed.\n"
            "Not a directory: missing\n"
            "Recorded as audit summary 2.\n"
            "\n"
            "Step 3 of 3, calculator.calculate: not run.\n"
            "\n"
            "Maria stopped at step 2 and did not run step 3. Nothing that ran "
            "was undone.",
        )
        self.assertEqual(harness.runs.actions, ["system.current_time", "filesystem.list"])
        self.assertEqual(
            harness.audit_rows(),
            (
                [
                    _summary_row(1, "system.current_time", "none"),
                    _summary_row(2, "filesystem.list", "named_filesystem_root"),
                ],
                [(1, "succeeded"), (2, "failed")],
            ),
        )
        self.assertIn(
            "Stopped a model-proposed plan at step 2 of 3: step_failed.",
            self.logged(),
        )
        self.assert_spent(harness)

    def test_a_failure_at_each_position_stops_the_plan_there(self) -> None:
        for total in (3, 5):
            for failing in range(1, total + 1):
                with self.subTest(total=total, failing=failing):
                    actions = [_sum(n) for n in range(1, total + 1)]
                    actions[failing - 1] = BAD_SUM
                    harness = self.harness(_envelope(*actions))
                    self.offer_plan(harness)

                    confirmed = self.confirm(harness)

                    blocks = _sums_report(
                        total,
                        failing - 1,
                        _step(
                            failing,
                            total,
                            "calculator.calculate",
                            "failed.",
                            CALCULATION_FAILED,
                            _recorded(failing),
                        ),
                    )
                    blocks += _not_run(failing + 1, total)
                    blocks.append(_stopped("at", failing, total))
                    self.assertEqual(confirmed.text, _report(*blocks))
                    self.assertEqual(len(harness.runs.calls), failing)
                    summaries, outcomes = harness.audit_rows()
                    self.assertEqual(len(summaries), failing)
                    self.assertEqual(
                        outcomes,
                        [(n, "succeeded") for n in range(1, failing)]
                        + [(failing, "failed")],
                    )

    def test_a_tool_that_raises_counts_as_failed_and_stops_the_plan(self) -> None:
        self.start_log()
        harness = self.harness(_envelope(_sum(1), _sum(2), _sum(3)))
        self.offer_plan(harness)
        harness.runs.errors[1] = RuntimeError(EXCEPTION_SENTINEL)

        confirmed = self.confirm(harness)

        self.assertEqual(
            confirmed.text,
            _report(
                *_sums_report(
                    3,
                    1,
                    _step(2, 3, "calculator.calculate", "failed.", TOOL_EXECUTION_FAILED, _recorded(2)),
                ),
                *_not_run(3, 3),
                _stopped("at", 2, 3),
            ),
        )
        self.assertEqual(harness.audit_rows()[1], [(1, "succeeded"), (2, "failed")])
        self.assertNotIn(EXCEPTION_SENTINEL, confirmed.text)
        assert_log_omits(self, self.log_path, (EXCEPTION_SENTINEL,))

    def test_an_answer_that_is_not_a_tool_result_counts_as_failed(self) -> None:
        harness = self.harness(_envelope(_sum(1), _sum(2), _sum(3)))
        self.offer_plan(harness)
        harness.runs.answers[1] = "2 + 2 = 4"

        confirmed = self.confirm(harness)

        self.assertEqual(
            confirmed.text,
            _report(
                *_sums_report(
                    3,
                    1,
                    _step(2, 3, "calculator.calculate", "failed.", TOOL_EXECUTION_FAILED, _recorded(2)),
                ),
                *_not_run(3, 3),
                _stopped("at", 2, 3),
            ),
        )
        self.assertEqual(harness.audit_rows()[1], [(1, "succeeded"), (2, "failed")])

    def test_a_result_shaped_like_a_proposal_adds_no_step(self) -> None:
        harness = self.harness(_envelope(_sum(1), _sum(2)))
        self.offer_plan(harness)
        proposal_text = _envelope(TIME, TIME, TIME)
        harness.runs.answers[0] = ToolExecutionResult(True, proposal_text)

        confirmed = self.confirm(harness)

        self.assertEqual(
            confirmed.text,
            _report(
                _step(1, 2, "calculator.calculate", "completed.", proposal_text, _recorded(1)),
                _step(2, 2, "calculator.calculate", "completed.", _sum_result(2), _recorded(2)),
            ),
        )
        self.assertEqual(harness.runs.actions, ["calculator.calculate"] * 2)
        self.assertEqual(harness.compile.call_count, 1)
        self.assertEqual(harness.provider.calls, 1)

    def test_reference_like_values_reach_their_tools_as_literal_text(self) -> None:
        harness = self.harness(_envelope(_list("$1"), _list("{{step1}}")))
        (harness.workspace / "$1").mkdir()
        (harness.workspace / "{{step1}}").mkdir()
        self.offer_plan(
            harness,
            _approval(
                ("filesystem", "list", '{"path": "$1"}'),
                ("filesystem", "list", '{"path": "{{step1}}"}'),
            ),
        )

        confirmed = self.confirm(harness)

        self.assertEqual(
            [dict(arguments) for _, _, arguments in harness.runs.calls],
            [{"path": "$1"}, {"path": "{{step1}}"}],
        )
        self.assertEqual(
            confirmed.text,
            _report(
                _step(1, 2, "filesystem.list", "completed.", "Directory is empty.", _recorded(1)),
                _step(2, 2, "filesystem.list", "completed.", "Directory is empty.", _recorded(2)),
            ),
        )


# --- Audit writes that fail -------------------------------------------------------


class MultiStepAuditFailureTests(_PlanTestCase):
    """Prove a step runs only behind its recorded summary, and stops after either write fails."""

    def _offered(self) -> _PlanHarness:
        """Return a harness holding three calculations awaiting approval."""
        harness = self.harness(_envelope(_sum(1), _sum(2), _sum(3)))
        self.offer_plan(harness)
        return harness

    def test_a_summary_that_cannot_be_recorded_stops_before_its_step(self) -> None:
        self.start_log()
        for position in (1, 2, 3):
            with self.subTest(position=position):
                harness = self._offered()
                _on_call(
                    harness.record_audit_summary,
                    position,
                    _raise(RuntimeError(EXCEPTION_SENTINEL)),
                )

                confirmed = self.confirm(harness)

                blocks = _sums_report(
                    3,
                    position - 1,
                    _step(position, 3, "calculator.calculate", NOT_AUTHORISED),
                )
                blocks += _not_run(position + 1, 3)
                blocks.append(
                    _stopped(
                        "at",
                        position,
                        3,
                        " because its audit summary could not be recorded",
                        ran=position > 1,
                    )
                )
                self.assertEqual(confirmed.text, _report(*blocks))
                self.assertEqual(len(harness.runs.calls), position - 1)
                self.assertEqual(harness.record_audit_summary.call_count, position)
                self.assertEqual(harness.record_audit_outcome.call_count, position - 1)
                summaries, outcomes = harness.audit_rows()
                self.assertEqual(len(summaries), position - 1)
                self.assertEqual(outcomes, [(n, "succeeded") for n in range(1, position)])
                self.assertNotIn(EXCEPTION_SENTINEL, confirmed.text)
                self.assert_spent(harness)
        assert_log_omits(self, self.log_path, (EXCEPTION_SENTINEL,))
        self.assertIn(
            "Stopped a model-proposed plan at step 2 of 3: summary_unrecorded.",
            self.logged(),
        )

    def test_a_summary_id_that_is_not_valid_stops_before_its_step(self) -> None:
        for value in (True, False, 0, -1, "2", 2.0, None, _IntSubclass(2)):
            with self.subTest(returned=repr(value)):
                harness = self._offered()
                _on_call(harness.record_audit_summary, 2, lambda *args, **kwargs: value)

                confirmed = self.confirm(harness)

                self.assertEqual(
                    confirmed.text,
                    _report(
                        *_sums_report(3, 1, _step(2, 3, "calculator.calculate", NOT_AUTHORISED)),
                        *_not_run(3, 3),
                        _stopped("at", 2, 3, " because its audit summary could not be recorded"),
                    ),
                )
                self.assertEqual(len(harness.runs.calls), 1)
                self.assertEqual(harness.audit_rows()[1], [(1, "succeeded")])

    def test_locked_storage_before_a_summary_stops_before_its_step(self) -> None:
        harness = self._offered()
        _on_call(
            harness.record_audit_outcome,
            1,
            lambda *args, **kwargs: (harness.real_outcome(*args, **kwargs), harness.lock())[0],
        )

        with patch.object(
            memory_module,
            "DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS",
            SHORT_BUSY_TIMEOUT_SECONDS,
        ):
            confirmed = self.confirm(harness)
        harness._release()
        harness.holders.clear()

        self.assertEqual(
            confirmed.text,
            _report(
                *_sums_report(3, 1, _step(2, 3, "calculator.calculate", NOT_AUTHORISED)),
                *_not_run(3, 3),
                _stopped("at", 2, 3, " because its audit summary could not be recorded"),
            ),
        )
        self.assertEqual(len(harness.runs.calls), 1)
        self.assertEqual(harness.record_audit_summary.call_count, 2)
        self.assertEqual(
            harness.audit_rows(),
            ([_summary_row(1, "calculator.calculate", "none")], [(1, "succeeded")]),
        )

    def test_an_outcome_that_cannot_be_recorded_stops_after_its_step(self) -> None:
        self.start_log()
        for position in (1, 2, 3):
            with self.subTest(position=position):
                harness = self._offered()
                _on_call(
                    harness.record_audit_outcome,
                    position,
                    _raise(RuntimeError(EXCEPTION_SENTINEL)),
                )

                confirmed = self.confirm(harness)

                blocks = _sums_report(
                    3,
                    position - 1,
                    _step(
                        position,
                        3,
                        "calculator.calculate",
                        "completed.",
                        _sum_result(position),
                        _unrecorded(position),
                    ),
                )
                blocks += _not_run(position + 1, 3)
                # Even after the last step the audit write that failed is
                # named, though no step was left unrun.
                blocks.append(
                    _stopped(
                        "after",
                        position,
                        3,
                        " because its audit outcome could not be recorded",
                    )
                )
                self.assertEqual(confirmed.text, _report(*blocks))
                self.assertEqual(len(harness.runs.calls), position)
                summaries, outcomes = harness.audit_rows()
                self.assertEqual(len(summaries), position)
                self.assertEqual(outcomes, [(n, "succeeded") for n in range(1, position)])
                self.assert_spent(harness)
        assert_log_omits(self, self.log_path, (EXCEPTION_SENTINEL,))

    def test_locked_storage_after_a_tool_stops_after_its_step(self) -> None:
        harness = self._offered()
        harness.runs.after[1] = harness.lock

        with patch.object(
            memory_module,
            "DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS",
            SHORT_BUSY_TIMEOUT_SECONDS,
        ):
            confirmed = self.confirm(harness)
        harness._release()
        harness.holders.clear()

        self.assertEqual(
            confirmed.text,
            _report(
                *_sums_report(
                    3,
                    1,
                    _step(2, 3, "calculator.calculate", "completed.", _sum_result(2), _unrecorded(2)),
                ),
                *_not_run(3, 3),
                _stopped("after", 2, 3, " because its audit outcome could not be recorded"),
            ),
        )
        self.assertEqual(len(harness.runs.calls), 2)
        self.assertEqual(
            harness.audit_rows(),
            (
                [
                    _summary_row(1, "calculator.calculate", "none"),
                    _summary_row(2, "calculator.calculate", "none"),
                ],
                [(1, "succeeded")],
            ),
        )

    def test_a_failed_step_whose_outcome_is_unrecorded_reports_the_failure(self) -> None:
        harness = self.harness(_envelope(_sum(1), BAD_SUM, _sum(3)))
        self.offer_plan(harness)
        _on_call(harness.record_audit_outcome, 2, _raise(RuntimeError("no outcome")))

        confirmed = self.confirm(harness)

        self.assertEqual(
            confirmed.text,
            _report(
                *_sums_report(
                    3,
                    1,
                    _step(2, 3, "calculator.calculate", "failed.", CALCULATION_FAILED, _unrecorded(2)),
                ),
                *_not_run(3, 3),
                _stopped("at", 2, 3),
            ),
        )
        self.assertEqual(harness.audit_rows()[1], [(1, "succeeded")])


# --- Interruptions ----------------------------------------------------------------


class MultiStepInterruptionTests(_PlanTestCase):
    """Prove an interrupted run stops, says only what is known, and never reruns."""

    STOP = _stopped("at", 2, 3, " because the run was interrupted")

    def _offered(self, *actions: Any) -> _PlanHarness:
        """Return a harness holding a plan awaiting approval."""
        harness = self.harness(_envelope(*(actions or (_sum(1), _sum(2), _sum(3)))))
        self.offer_plan(harness)
        return harness

    def test_an_interruption_before_dispatch_reports_the_step_not_run(self) -> None:
        self.start_log()
        harness = self._offered()
        info = harness.logger.info
        dispatching: list[tuple] = []

        def interrupting(message: str, *args: Any, **kwargs: Any) -> None:
            if message.startswith("Dispatching approved model-proposed action"):
                dispatching.append(args)
                if len(dispatching) == 2:
                    raise KeyboardInterrupt
            info(message, *args, **kwargs)

        with patch.object(harness.logger, "info", side_effect=interrupting):
            confirmed = self.confirm(harness)

        self.assertEqual(
            confirmed.text,
            _report(
                *_sums_report(3, 1, _step(2, 3, "calculator.calculate", "not run.", _missing(2))),
                *_not_run(3, 3),
                self.STOP,
            ),
        )
        # The summary was committed, the tool never reached, no outcome
        # written for it, and nothing after it summarised.
        self.assertEqual(len(harness.runs.calls), 1)
        self.assertEqual(harness.execute_tool.call_count, 1)
        self.assertEqual(harness.record_audit_outcome.call_count, 1)
        self.assertEqual(
            harness.audit_rows(),
            (
                [
                    _summary_row(1, "calculator.calculate", "none"),
                    _summary_row(2, "calculator.calculate", "none"),
                ],
                [(1, "succeeded")],
            ),
        )
        self.assertIn(
            "Stopped a model-proposed plan at step 2 of 3: interrupted.",
            self.logged(),
        )
        self.assert_spent(harness)

    def test_an_interruption_inside_a_tool_leaves_its_outcome_unknown(self) -> None:
        harness = self._offered()
        harness.runs.errors[1] = KeyboardInterrupt()

        confirmed = self.confirm(harness)

        self.assertEqual(
            confirmed.text,
            _report(
                *_sums_report(
                    3,
                    1,
                    _step(
                        2,
                        3,
                        "calculator.calculate",
                        "interrupted, so its outcome is unknown.",
                        _missing(2),
                    ),
                ),
                *_not_run(3, 3),
                self.STOP,
            ),
        )
        # The tool was entered; no failed outcome was invented for it.
        self.assertEqual(len(harness.runs.calls), 2)
        harness.record_audit_outcome.assert_called_once_with(1, AuditOutcome.SUCCEEDED)
        self.assertEqual(harness.audit_rows()[1], [(1, "succeeded")])
        self.assertEqual(len(harness.audit_rows()[0]), 2)
        self.assert_spent(harness)
        harness.record_audit_outcome.side_effect = None
        self.assertEqual(
            harness.session.submit("read audit 2").text,
            _block(2, "calculator.calculate", "none", OUTCOME_UNKNOWN),
        )
        self.assertEqual(len(harness.runs.calls), 2)

    def test_an_interrupted_summary_write_that_did_not_commit_is_unconfirmed(
        self,
    ) -> None:
        harness = self._offered()
        # Connections: summary 1, outcome 1, then summary 2.
        opened = self.interrupt_connection(harness, 3)

        confirmed = self.confirm(harness)

        self.assertEqual(
            confirmed.text,
            _report(
                *_sums_report(3, 1, _step(2, 3, "calculator.calculate", "not run.", SUMMARY_UNCONFIRMED)),
                *_not_run(3, 3),
                self.STOP,
            ),
        )
        self.assertEqual(len(opened), 3)
        self.assertEqual(opened[2].commits, 1)
        self.assertEqual(len(harness.runs.calls), 1)
        self.assertEqual(
            harness.audit_rows(),
            ([_summary_row(1, "calculator.calculate", "none")], [(1, "succeeded")]),
        )

    def test_a_summary_committed_before_an_interruption_is_still_unconfirmed(
        self,
    ) -> None:
        harness = self._offered()
        _on_call(harness.record_audit_summary, 2, _then_interrupt(harness.real_summary))

        confirmed = self.confirm(harness)

        # The row exists, but Maria never learned so, and says only that.
        self.assertEqual(
            confirmed.text,
            _report(
                *_sums_report(3, 1, _step(2, 3, "calculator.calculate", "not run.", SUMMARY_UNCONFIRMED)),
                *_not_run(3, 3),
                self.STOP,
            ),
        )
        self.assertEqual(len(harness.runs.calls), 1)
        self.assertEqual(
            harness.audit_rows(),
            (
                [
                    _summary_row(1, "calculator.calculate", "none"),
                    _summary_row(2, "calculator.calculate", "none"),
                ],
                [(1, "succeeded")],
            ),
        )

    def test_an_interrupted_outcome_write_that_did_not_commit_is_unconfirmed(
        self,
    ) -> None:
        harness = self._offered()
        # Connections: summary 1, outcome 1, summary 2, then outcome 2.
        opened = self.interrupt_connection(harness, 4)

        confirmed = self.confirm(harness)

        self.assertEqual(
            confirmed.text,
            _report(
                *_sums_report(
                    3,
                    1,
                    _step(2, 3, "calculator.calculate", "completed.", _sum_result(2), _unconfirmed(2)),
                ),
                *_not_run(3, 3),
                _stopped("after", 2, 3, " because the run was interrupted"),
            ),
        )
        self.assertEqual(opened[3].commits, 1)
        self.assertEqual(len(harness.runs.calls), 2)
        self.assertEqual(harness.audit_rows()[1], [(1, "succeeded")])

    def test_an_outcome_committed_before_an_interruption_is_still_unconfirmed(
        self,
    ) -> None:
        harness = self._offered()
        _on_call(harness.record_audit_outcome, 2, _then_interrupt(harness.real_outcome))

        confirmed = self.confirm(harness)

        self.assertEqual(
            confirmed.text,
            _report(
                *_sums_report(
                    3,
                    1,
                    _step(2, 3, "calculator.calculate", "completed.", _sum_result(2), _unconfirmed(2)),
                ),
                *_not_run(3, 3),
                _stopped("after", 2, 3, " because the run was interrupted"),
            ),
        )
        self.assertEqual(harness.audit_rows()[1], [(1, "succeeded"), (2, "succeeded")])

    def test_an_interrupted_failed_step_keeps_its_known_failure(self) -> None:
        harness = self._offered(_sum(1), BAD_SUM, _sum(3))
        _on_call(harness.record_audit_outcome, 2, _raise(KeyboardInterrupt()))

        confirmed = self.confirm(harness)

        self.assertEqual(
            confirmed.text,
            _report(
                *_sums_report(
                    3,
                    1,
                    _step(2, 3, "calculator.calculate", "failed.", CALCULATION_FAILED, _unconfirmed(2)),
                ),
                *_not_run(3, 3),
                self.STOP,
            ),
        )
        self.assertEqual(harness.audit_rows()[1], [(1, "succeeded")])

    def test_an_interruption_at_the_first_step_reports_nothing_as_run(self) -> None:
        harness = self._offered()
        opened = self.interrupt_connection(harness, 1)

        confirmed = self.confirm(harness)

        self.assertEqual(
            confirmed.text,
            _report(
                _step(1, 3, "calculator.calculate", "not run.", SUMMARY_UNCONFIRMED),
                *_not_run(2, 3),
                _stopped("at", 1, 3, " because the run was interrupted", ran=False),
            ),
        )
        self.assertEqual(len(opened), 1)
        self.assertEqual(harness.runs.calls, [])
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_an_interruption_between_steps_reports_the_next_step_not_run(
        self,
    ) -> None:
        harness = self._offered()
        real = brain_module._StepAttempt
        begun: list[int] = []

        def beginning(index: int) -> Any:
            begun.append(index)
            if index == 1:
                raise KeyboardInterrupt
            return real(index)

        with patch.object(brain_module, "_StepAttempt", side_effect=beginning):
            confirmed = self.confirm(harness)

        # Step 1 was fully reported before step 2 began, so nothing of step
        # 1's attempt is carried over to step 2.
        self.assertEqual(begun, [0, 1])
        self.assertEqual(
            confirmed.text,
            _report(
                *_sums_report(3, 1, _step(2, 3, "calculator.calculate", "not run.")),
                *_not_run(3, 3),
                self.STOP,
            ),
        )
        self.assertEqual(harness.record_audit_summary.call_count, 1)
        self.assertEqual(len(harness.runs.calls), 1)
        self.assertEqual(
            harness.audit_rows(),
            ([_summary_row(1, "calculator.calculate", "none")], [(1, "succeeded")]),
        )
        self.assert_spent(harness)

    def test_an_interruption_before_a_summary_write_reports_no_audit_line(
        self,
    ) -> None:
        harness = self._offered()
        real = brain_module._StepAttempt

        class _InterruptedAuthorising(real):
            """An attempt interrupted just as its second step's summary begins."""

            __slots__ = ()

            def __setattr__(self, name: str, value: Any) -> None:
                if (
                    name == "stage"
                    and value is brain_module._StepStage.AUTHORISING
                    and self.index == 1
                ):
                    raise KeyboardInterrupt
                super().__setattr__(name, value)

        with patch.object(brain_module, "_StepAttempt", _InterruptedAuthorising):
            confirmed = self.confirm(harness)

        self.assertEqual(
            confirmed.text,
            _report(
                *_sums_report(3, 1, _step(2, 3, "calculator.calculate", "not run.")),
                *_not_run(3, 3),
                self.STOP,
            ),
        )
        # The second summary write never began, so it is not reported at all.
        self.assertEqual(harness.record_audit_summary.call_count, 1)
        self.assertEqual(len(harness.runs.calls), 1)
        self.assertEqual(harness.row_counts()["audit_summaries"], 1)
        self.assert_spent(harness)

    def test_only_a_keyboard_interrupt_is_handled(self) -> None:
        harness = self._offered()
        harness.runs.errors[1] = SystemExit(3)

        with self.assertRaises(SystemExit):
            self.confirm(harness)

        # Nothing caught it, so nothing reported or recorded anything for
        # it: step 2's summary stands without an outcome, and step 3 has none.
        self.assertEqual(len(harness.runs.calls), 2)
        self.assertEqual(
            harness.audit_rows(),
            (
                [
                    _summary_row(1, "calculator.calculate", "none"),
                    _summary_row(2, "calculator.calculate", "none"),
                ],
                [(1, "succeeded")],
            ),
        )
        self.assert_spent(harness)

    def test_a_single_step_plan_still_lets_an_interruption_through(self) -> None:
        harness = self.harness(json.dumps(_sum(2)))
        self.offer_plan(harness)
        harness.runs.errors[0] = KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.confirm(harness)

        self.assertEqual(len(harness.runs.calls), 1)
        self.assertEqual(harness.audit_rows(), ([_summary_row(1, "calculator.calculate", "none")], []))
        self.assertIsNone(harness.session.pending_approval)


# --- Privacy ------------------------------------------------------------------


class MultiStepPrivacyTests(_PlanTestCase):
    """Prove nothing private reaches an audit row, the log, history, or the conversation."""

    def test_no_private_value_leaks_from_a_plan_of_several_steps(self) -> None:
        self.start_log()
        harness = self.harness(_envelope(TIME, _list(PATH_SENTINEL), _sum(2)))

        offered = self.offer_plan(harness, request=PRIVATE_REQUEST)
        # Positive controls: the owner is shown the path, and the tool's own
        # message, which the report keeps as it is, names it as well.
        self.assertIn(PATH_SENTINEL, offered.text)
        confirmed = self.confirm(harness)
        self.assertIn(f"Not a directory: {PATH_SENTINEL}", confirmed.text)

        for line in confirmed.text.split("\n"):
            if line.startswith(("Step ", "Maria stopped", "Recorded as")):
                with self.subTest(line=line[:30]):
                    self.assertNotIn(PATH_SENTINEL, line)
                    self.assertNotIn(REQUEST_SENTINEL, line)
        summaries, outcomes = harness.audit_rows()
        stored = [str(value) for row in (*summaries, *outcomes) for value in row]
        self.assertEqual(len(summaries), 2)
        for value in stored:
            for sentinel in (PATH_SENTINEL, REQUEST_SENTINEL, ROOT_NAME_SENTINEL):
                self.assertNotIn(sentinel, value)
        assert_log_omits(
            self,
            self.log_path,
            (PATH_SENTINEL, REQUEST_SENTINEL, ROOT_NAME_SENTINEL, CLOCK_REPORT),
        )
        self.assertEqual(harness.interactions(), [])
        self.assertEqual(harness.session.conversation, ())
        self.assertEqual(harness.session.supplied_context, frozenset())


# --- Instructions and budget ---------------------------------------------------------


class MultiStepInstructionTests(unittest.TestCase):
    """Prove the instructions name the compiler's limits and no budget grew."""

    def test_the_instructions_name_the_compiler_step_limits(self) -> None:
        self.assertEqual(len(PROPOSAL_INSTRUCTIONS.encode("utf-8")), 1327)
        span = f"{MODEL_PROPOSAL_MIN_STEPS} to {PLAN_MAX_STEPS}"
        self.assertIn(f"A proposal for {span} actions must be one JSON object", PROPOSAL_INSTRUCTIONS)
        self.assertIn(f'"steps": an array of {span} such action objects', PROPOSAL_INSTRUCTIONS)
        self.assertIn(
            "Maria stops if a step fails, an audit write fails, or the run is interrupted.",
            PROPOSAL_INSTRUCTIONS,
        )
        self.assertIn("a step cannot use an earlier step's result", PROPOSAL_INSTRUCTIONS)

    def test_no_cap_or_budget_grew_with_the_instructions(self) -> None:
        self.assertEqual(MODEL_PROPOSAL_MAX_BYTES, 4096)
        self.assertEqual(MODEL_PROPOSAL_MAX_NESTING, 8)
        self.assertEqual(CONVERSATION_MAX_BYTES, 8 * 1024)

    def test_a_bare_request_may_carry_6865_prompt_bytes_at_the_ceiling(self) -> None:
        self.assertEqual(request_content_bytes("x" * 6865, (), 8192, (), True), 8192)
        self.assertGreater(request_content_bytes("x" * 6866, (), 8192, (), True), 8192)

        settings = enabled_settings(max_input_bytes=8192)
        logger = _quiet_logger("tests.multi_step_plans.budget")
        within = ScriptedProvider.replying("A reply.")
        converse(within, settings, "x" * 6865, (), logger, proposals=True)
        self.assertEqual(within.calls, 1)

        beyond = ScriptedProvider.replying("A reply.")
        turn = converse(beyond, settings, "x" * 6866, (), logger, proposals=True)
        self.assertEqual(beyond.calls, 0)
        self.assertEqual(turn.plan.response_if_no_action, OVER_BUDGET_TEXT)


# --- Deterministic plans ----------------------------------------------------------


class DeterministicCompatibilityTests(_PlanTestCase):
    """Prove deterministic plans keep their behavior, under the five-step bound."""

    def _deterministic(self, harness: _PlanHarness, *expressions: str) -> Plan:
        """Make the planner answer with a deterministic plan of calculations."""
        plan = Plan(
            "synthetic calculations",
            tuple(
                PlanStep("calculator", "calculate", {"expression": expression})
                for expression in expressions
            ),
        )
        harness.create_plan.return_value = plan
        return plan

    def test_five_deterministic_steps_still_run_and_report_as_before(self) -> None:
        harness = self.harness("unused")
        self._deterministic(harness, *(f"{n} + {n}" for n in range(1, 6)))

        response = harness.session.submit("synthetic calculations")

        joined = "\n".join(_sum_result(n) for n in range(1, 6))
        self.assertEqual(response.text, joined)
        self.assertFalse(response.needs_confirmation)
        self.assertEqual(harness.interactions(), [("synthetic calculations", joined)])
        self.assertEqual(harness.audit_rows(), ([], []))
        self.assertEqual(harness.provider.calls, 0)

    def test_a_deterministic_failure_keeps_its_joined_report(self) -> None:
        harness = self.harness("unused")
        self._deterministic(harness, "1 + 1", "x + 1", "3 + 3")

        response = harness.session.submit("synthetic calculations")

        self.assertEqual(response.text, f"1 + 1 = 2\n{CALCULATION_FAILED}")
        self.assertNotIn("Step ", response.text)
        self.assertEqual(
            harness.interactions(),
            [("synthetic calculations", f"1 + 1 = 2\n{CALCULATION_FAILED}")],
        )

    def test_a_deterministic_plan_altered_to_six_steps_is_refused_everywhere(
        self,
    ) -> None:
        harness = self.harness("unused")
        plan = self._deterministic(harness, "1 + 1", "2 + 2")
        object.__setattr__(plan, "steps", plan.steps * 3)

        processed = harness.session.submit("synthetic calculations")
        confirmed = harness.brain.execute_confirmed_plan(plan)
        harness.session._pending = PendingApproval(plan, plan.fingerprint)
        bound = harness.session.confirm()

        self.assertEqual(processed.text, PLAN_STEP_LIMIT_REFUSED_MESSAGE)
        self.assertEqual(confirmed.text, PLAN_STEP_LIMIT_REFUSED_MESSAGE)
        self.assertEqual(bound.text, CONFIRMATION_REFUSED)
        harness.check.assert_not_called()
        harness.validate_action.assert_not_called()
        self.assertEqual(harness.runs.calls, [])
        self.assertEqual(harness.interactions(), [])

    def test_six_steps_reporting_five_are_refused_by_brain_and_the_session(self) -> None:
        harness = self.harness("unused")
        plan = self._deterministic(harness, "1 + 1", "2 + 2")
        object.__setattr__(plan, "steps", _Underreported(_calculations(6)))

        processed = harness.session.submit("synthetic calculations")
        confirmed = harness.brain.execute_confirmed_plan(plan)
        harness.session._pending = PendingApproval(plan, plan.fingerprint)
        bound = harness.session.confirm()

        self.assertEqual(processed.text, PLAN_STEP_LIMIT_REFUSED_MESSAGE)
        self.assertEqual(confirmed.text, PLAN_STEP_LIMIT_REFUSED_MESSAGE)
        self.assertEqual(bound, BrainResponse(CONFIRMATION_REFUSED))
        self.assertIsNone(harness.session.pending_approval)
        harness.validate_action.assert_not_called()
        harness.check.assert_not_called()
        harness.prepare_confirmation.assert_not_called()
        harness.execute_tool.assert_not_called()
        harness.record_audit_summary.assert_not_called()
        self.assertEqual(harness.runs.calls, [])
        self.assertEqual(harness.row_counts(), ZERO_ROWS)
        self.assertEqual(harness.provider.calls, 0)

    def test_a_pending_plan_altered_to_six_steps_reporting_five_runs_nothing(self) -> None:
        harness = self.harness(
            "unused",
            confirmation_required=frozenset({"calculator.calculate"}),
        )
        self._deterministic(harness, "1 + 1", "2 + 2")
        self.assertTrue(harness.session.submit("synthetic calculations").needs_confirmation)
        plan = harness.session.pending_approval.plan
        object.__setattr__(plan, "steps", _Underreported(_calculations(6)))
        # Bound to the altered plan's own fingerprint, so only the step bound
        # stands between it and its tools.
        harness.session._pending = PendingApproval(plan, plan.fingerprint)
        checks = harness.check.call_count

        refused = harness.session.confirm()

        self.assertEqual(refused, BrainResponse(CONFIRMATION_REFUSED))
        self.assertIsNone(harness.session.pending_approval)
        self.assertEqual(harness.check.call_count, checks)
        harness.execute_tool.assert_not_called()
        self.assertEqual(harness.runs.calls, [])
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_brain_runs_exactly_the_one_bounded_read_it_checked(self) -> None:
        harness = self.harness("unused")
        plan = self._deterministic(harness, "1 + 1", "2 + 2")
        growing = _Growing(plan.steps, tuple(_calculations(6)))
        object.__setattr__(plan, "steps", growing)

        response = harness.brain.execute_confirmed_plan(plan)

        # Brain read the steps once; a later read holding six never ran.
        joined = f"{_sum_result(1)}\n{_sum_result(2)}"
        self.assertEqual(response.text, joined)
        self.assertEqual(growing.reads, 1)
        self.assertEqual(harness.check.call_count, 2)
        self.assertEqual(harness.execute_tool.call_count, 2)
        self.assertEqual(harness.runs.actions, ["calculator.calculate"] * 2)
        self.assertEqual(harness.interactions(), [("synthetic calculations", joined)])

    def test_a_plan_offered_for_approval_holds_exactly_the_one_bounded_read(self) -> None:
        for label, later in (
            ("six steps", tuple(_calculations(6))),
            ("a thousand steps", (_calculation(9),) * 1000),
        ):
            with self.subTest(later_read=label):
                harness = self.harness(
                    "unused",
                    confirmation_required=frozenset({"calculator.calculate"}),
                )
                plan = self._deterministic(harness, "1 + 1")
                growing = _Growing(plan.steps, later)
                object.__setattr__(plan, "steps", growing)

                offered = harness.session.submit("synthetic calculations")

                # Preparation, the pending approval, and its fingerprint all
                # hold the one step Brain read; what a later read would hand
                # out never reaches a binder, a preview, or the approval.
                self.assertTrue(offered.needs_confirmation)
                prepared = harness.prepare_confirmation.call_args.args[0]
                self.assertIs(type(prepared.steps), tuple)
                self.assertEqual(len(prepared.steps), 1)
                self.assertIs(prepared.steps[0], list.__getitem__(growing, 0))
                pending = harness.session.pending_approval
                self.assertIs(pending.plan, prepared)
                self.assertEqual(
                    pending.fingerprint,
                    Plan("synthetic calculations", prepared.steps).fingerprint,
                )
                self.assertEqual(harness.check.call_count, 1)

                confirmed = harness.session.confirm()

                self.assertEqual(confirmed.text, _sum_result(1))
                self.assertEqual(harness.runs.actions, ["calculator.calculate"])
                self.assertEqual(
                    harness.interactions(),
                    [("synthetic calculations", _sum_result(1))],
                )
                self.assertEqual(growing.reads, 1)

    def test_a_list_subclass_of_five_steps_still_runs_as_before(self) -> None:
        harness = self.harness("unused")
        harness.create_plan.return_value = Plan(
            "synthetic calculations",
            _Underreported(_calculations(5)),
        )

        response = harness.session.submit("synthetic calculations")

        joined = "\n".join(_sum_result(n) for n in range(1, 6))
        self.assertEqual(response.text, joined)
        self.assertEqual(harness.runs.actions, ["calculator.calculate"] * 5)
        self.assertEqual(harness.interactions(), [("synthetic calculations", joined)])

    def test_uncountable_deterministic_steps_never_run(self) -> None:
        step = PlanStep("calculator", "calculate", {"expression": "1 + 1"})
        for label, steps in (
            ("an iterator", (each for each in (step,))),
            ("a shared iterator", _SharedIterator([step])),
        ):
            with self.subTest(steps=label):
                harness = self.harness("unused")
                plan = Plan("synthetic calculations", steps)

                refused = harness.brain.execute_confirmed_plan(plan)

                self.assertEqual(refused.text, PLAN_STEP_LIMIT_REFUSED_MESSAGE)
                self.assertEqual(harness.runs.calls, [])
                self.assertEqual(harness.interactions(), [])

    def test_a_zero_step_plan_is_answered_and_recorded_as_before(self) -> None:
        harness = self.harness("unused")

        help_text = harness.session.submit("help").text

        self.assertTrue(help_text.startswith("Supported commands:\n"))
        self.assertEqual(harness.interactions(), [("help", help_text)])


# --- Own origin and the approved snapshot --------------------------------------


# Ways a built plan can stop holding exactly its own origin.
ORIGIN_ALTERATIONS: dict[str, Callable[[Plan], None]] = {
    "deleted": lambda plan: object.__delattr__(plan, "origin"),
    "replaced by text": lambda plan: object.__setattr__(plan, "origin", "deterministic"),
    "replaced by None": lambda plan: object.__setattr__(plan, "origin", None),
}

# What refusing a plan may never write to the log.
UNRAISED = ("Traceback", "AttributeError", "has no attribute")


class _SnapshotTestCase(_PlanTestCase):
    """Drivers for deterministic plans offered for approval."""

    def offer_deterministic(self, steps: Any) -> tuple[_PlanHarness, BrainResponse, Plan]:
        """Offer a deterministic plan of ``steps`` that needs confirmation.

        Returns the harness, the offer, and the plan that became pending.
        """
        harness = self.harness(
            "unused",
            confirmation_required=frozenset({"calculator.calculate"}),
        )
        _planned(harness, steps)
        offered = harness.session.submit(DETERMINISTIC_REQUEST)
        self.assertTrue(offered.needs_confirmation)
        return harness, offered, harness.session.pending_approval.plan

    def assert_nothing_reached(self, harness: _PlanHarness) -> None:
        """Assert no contract, permission, preparation, audit, or tool was reached."""
        harness.validate_action.assert_not_called()
        harness.check.assert_not_called()
        harness.prepare_confirmation.assert_not_called()
        harness.record_audit_summary.assert_not_called()
        harness.execute_tool.assert_not_called()
        self.assertEqual(harness.runs.calls, [])
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def assert_refused_after_the_offer(self, harness: _PlanHarness) -> None:
        """Confirm, then assert the refusal reached and recorded nothing new."""
        validations = harness.validate_action.call_count
        checks = harness.check.call_count

        refused = harness.session.confirm()

        self.assertEqual(refused, BrainResponse(CONFIRMATION_REFUSED))
        self.assertIsNone(harness.session.pending_approval)
        self.assertEqual(harness.validate_action.call_count, validations)
        self.assertEqual(harness.check.call_count, checks)
        harness.record_audit_summary.assert_not_called()
        harness.execute_tool.assert_not_called()
        self.assertEqual(harness.runs.calls, [])
        self.assertEqual(harness.row_counts(), ZERO_ROWS)


class OwnOriginRoutingTests(_SnapshotTestCase):
    """Prove a plan not holding exactly its own fields is refused before routing."""

    def test_brain_refuses_a_plan_without_its_own_origin_before_routing(self) -> None:
        self.start_log()
        for label, alter in ORIGIN_ALTERATIONS.items():
            with self.subTest(origin=label):
                harness = self.harness("unused")
                plan = Plan(DETERMINISTIC_REQUEST, _approved())
                alter(plan)
                model_path = _spy(self, harness.brain, "_run_model_plan")

                refused = harness.brain.execute_confirmed_plan(plan)

                self.assertEqual(refused.text, MODEL_PLAN_REFUSED_MESSAGE)
                self.assertFalse(refused.needs_confirmation)
                # Refused before either path was chosen.
                model_path.assert_not_called()
                self.assert_nothing_reached(harness)
        self.assertIn("Refused a model-originated plan that is not well formed.", self.logged())
        assert_log_omits(self, self.log_path, UNRAISED)

    def test_a_plan_whose_fields_are_held_in_a_lying_dictionary_is_refused(self) -> None:
        # Shown the approved steps, the plan's attributes hold others.
        for kind in ("deterministic", "model"):
            with self.subTest(plan=kind):
                if kind == "deterministic":
                    harness, _, pending = self.offer_deterministic(_approved())
                else:
                    harness = self.harness(_envelope(TIME, _sum(2)))
                    pending = self.offer_plan(harness).plan
                _lie_about_steps(pending, _Scripted(pending.steps, _others()))

                self.assertIsNone(own_origin(pending))
                self.assertIsNone(plan_snapshot(pending))
                self.assert_refused_after_the_offer(harness)

        harness = self.harness("unused")
        plan = Plan(DETERMINISTIC_REQUEST, _approved())
        _lie_about_steps(plan, _Scripted(_approved(), _others()))

        refused = harness.brain.execute_confirmed_plan(plan)

        self.assertEqual(refused.text, MODEL_PLAN_REFUSED_MESSAGE)
        self.assert_nothing_reached(harness)

    def test_a_fallback_plan_without_its_own_origin_never_reaches_the_model(self) -> None:
        harness = self.harness("A reply.")
        plan = Plan(REQUEST, (), GENERIC_FALLBACK_RESPONSE)
        object.__delattr__(plan, "origin")
        harness.create_plan.return_value = plan

        response = harness.session.submit(REQUEST)

        self.assertEqual(response.text, MODEL_PLAN_REFUSED_MESSAGE)
        self.assertIsNone(response.conversation)
        self.assertEqual(harness.provider.calls, 0)
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_a_conversational_plan_without_its_own_origin_is_refused(self) -> None:
        harness = self.harness("unused")
        result = transient_plan(REQUEST, "A reply.")
        object.__delattr__(result, "origin")
        turn = ConversationTurn(
            result,
            (ReasoningExchange(REQUEST, "A reply."),),
            frozenset(),
        )

        with patch.object(brain_module, "converse", return_value=turn):
            response = harness.session.submit(REQUEST)

        # The turn's conversation is not kept with a result Brain refused.
        self.assertEqual(response.text, CONVERSATION_RESULT_REFUSED_MESSAGE)
        self.assertEqual(response.conversation, ())
        self.assertEqual(harness.session.conversation, ())
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_a_planned_plan_without_its_own_origin_is_refused_before_routing(self) -> None:
        for label, alter in ORIGIN_ALTERATIONS.items():
            with self.subTest(origin=label):
                harness = self.harness("unused")
                alter(_planned(harness, _approved()))

                processed = harness.session.submit(DETERMINISTIC_REQUEST)

                self.assertEqual(processed.text, MODEL_PLAN_REFUSED_MESSAGE)
                self.assertFalse(processed.needs_confirmation)
                self.assertIsNone(harness.session.pending_approval)
                self.assertEqual(harness.provider.calls, 0)
                self.assert_nothing_reached(harness)

    def test_the_session_refuses_a_pending_plan_without_its_own_origin(self) -> None:
        self.start_log()
        for label, alter in ORIGIN_ALTERATIONS.items():
            for kind in ("deterministic", "model"):
                with self.subTest(origin=label, plan=kind):
                    if kind == "deterministic":
                        harness, _, pending = self.offer_deterministic(_approved())
                    else:
                        harness = self.harness(_envelope(TIME, _sum(2)))
                        pending = self.offer_plan(harness).plan

                    alter(pending)

                    self.assert_refused_after_the_offer(harness)
        self.assertIn("Refused a confirmation that binds no pending plan.", self.logged())
        assert_log_omits(self, self.log_path, UNRAISED)

    def test_a_field_deleted_while_it_held_its_default_is_never_read_as_it(self) -> None:
        for field in ("response_if_no_action", "recording", "origin"):
            with self.subTest(field=field):
                harness = self.harness("unused")
                plan = Plan(DETERMINISTIC_REQUEST, _approved())
                object.__delattr__(plan, field)

                refused = harness.brain.execute_confirmed_plan(plan)

                self.assertEqual(refused.text, MODEL_PLAN_REFUSED_MESSAGE)
                self.assert_nothing_reached(harness)

                harness, _, pending = self.offer_deterministic(_approved())
                approved = harness.session.pending_approval.fingerprint
                object.__delattr__(pending, field)

                # Read through the class, plan and fingerprint look unchanged.
                self.assertEqual(pending.fingerprint, approved)
                self.assert_refused_after_the_offer(harness)

    def test_copying_a_plan_never_restores_a_deleted_field(self) -> None:
        # Held in a list subclass, these steps need a copy to be snapshotted,
        # and no copy may bring the deleted policy back as its default.
        harness = self.harness(
            "unused",
            confirmation_required=frozenset({"calculator.calculate"}),
        )
        plan = Plan(
            PRIVATE_REQUEST,
            _PlainStepList(_approved()),
            recording=RecordingPolicy.TRANSIENT,
        )
        object.__delattr__(plan, "recording")
        harness.create_plan.return_value = plan

        offered = harness.session.submit(PRIVATE_REQUEST)

        self.assertEqual(offered.text, MODEL_PLAN_REFUSED_MESSAGE)
        self.assertFalse(offered.needs_confirmation)
        self.assertIsNone(harness.session.pending_approval)
        self.assertIsNone(plan_snapshot(plan))
        self.assert_nothing_reached(harness)


class ApprovedSnapshotTests(_SnapshotTestCase):
    """Prove approval and execution are bound to one bounded snapshot."""

    def test_steps_handed_out_in_turn_never_run_the_other_steps(self) -> None:
        scripts: dict[str, tuple[tuple[tuple[PlanStep, ...], ...], str, list[str]]] = {
            "approved, then other": (
                (_approved(), _others()),
                APPROVED_RESULT,
                ["1 + 1", "2 + 2"],
            ),
            "other, then approved": (
                (_others(), _approved()),
                CONFIRMATION_REFUSED,
                [],
            ),
            "approved twice, then other": (
                (_approved(), _approved(), _others()),
                APPROVED_RESULT,
                ["1 + 1", "2 + 2"],
            ),
        }
        for label, (reads, text, expressions) in scripts.items():
            with self.subTest(reads=label):
                harness, _, pending = self.offer_deterministic(_approved())
                scripted = _Scripted(*reads)
                object.__setattr__(pending, "steps", scripted)

                confirmed = harness.session.confirm()

                # One read: its fingerprint is compared, and it alone can run.
                self.assertEqual(confirmed.text, text)
                self.assertEqual(scripted.reads, 1)
                self.assertEqual(
                    [arguments["expression"] for _, _, arguments in harness.runs.calls],
                    expressions,
                )

    def test_an_endless_container_substituted_after_the_offer_is_refused(self) -> None:
        for kind in ("deterministic", "model"):
            with self.subTest(plan=kind):
                if kind == "deterministic":
                    harness, _, pending = self.offer_deterministic(_approved())
                else:
                    harness = self.harness(_envelope(TIME, _sum(2)))
                    pending = self.offer_plan(harness).plan
                endless = _Endless(_calculation(9))
                object.__setattr__(pending, "steps", endless)

                self.assert_refused_after_the_offer(harness)

                # At most one step past the bound was asked for; a model plan
                # must hold an exact tuple, so its steps are not read at all.
                self.assertEqual(
                    endless.handed,
                    PLAN_MAX_STEPS + 1 if kind == "deterministic" else 0,
                )

    def test_code_run_while_a_plan_is_fingerprinted_cannot_swap_what_runs(self) -> None:
        # A deterministic step's arguments are not type-checked, so reading
        # them can run code; this code swaps the plan's steps mid-fingerprint.
        harness, _, pending = self.offer_deterministic(_approved())
        first = pending.steps[0]
        object.__setattr__(
            first,
            "arguments",
            _SwappingArguments(dict(first.arguments), pending, _others()),
        )

        self.assert_refused_after_the_offer(harness)

        # The fingerprint read the approved steps and matched, so only the
        # check that the snapshot still holds them refused the swap.
        self.assertEqual(
            [step.arguments["expression"] for step in pending.steps],
            ["7 + 7", "8 + 8"],
        )

    def test_a_container_whose_later_reads_are_long_is_read_once(self) -> None:
        harness, _, pending = self.offer_deterministic(_approved())
        scripted = _Scripted(_approved(), (_calculation(9),) * 10_000)
        object.__setattr__(pending, "steps", scripted)

        confirmed = harness.session.confirm()

        self.assertEqual(confirmed.text, APPROVED_RESULT)
        self.assertEqual(scripted.reads, 1)
        self.assertEqual(scripted.handed, 2)

    def test_direct_confirmed_execution_runs_one_consistent_read(self) -> None:
        harness = self.harness("unused")
        first = _approved()
        plan = Plan(DETERMINISTIC_REQUEST, _approved())
        scripted = _Scripted(first, _others())
        object.__setattr__(plan, "steps", scripted)

        response = harness.brain.execute_confirmed_plan(plan)

        self.assertEqual(response.text, APPROVED_RESULT)
        self.assertEqual(scripted.reads, 1)
        # Validated, resolved, and dispatched: the very steps of one read.
        for made in harness.validate_action.call_args_list:
            self.assertTrue(any(made.args[2] is step.arguments for step in first))
        self.assertEqual(harness.check.call_count, 2)
        dispatched = [made.args[2] for made in harness.execute_tool.call_args_list]
        self.assertEqual(len(dispatched), 2)
        for sent, step in zip(dispatched, first):
            self.assertIs(sent, step.arguments)
        self.assertEqual(
            harness.interactions(),
            [(DETERMINISTIC_REQUEST, APPROVED_RESULT)],
        )

    def test_the_session_snapshots_whatever_plan_it_is_offered(self) -> None:
        # Brain always offers a snapshot; the session takes its own anyway,
        # so what it renders, fingerprints, and keeps cannot change later.
        harness = self.harness(
            "unused",
            confirmation_required=frozenset({"calculator.calculate"}),
        )
        listed = Plan(DETERMINISTIC_REQUEST, _PlainStepList(_approved()))
        offer = BrainResponse("Confirmation required.", needs_confirmation=True, plan=listed)
        with patch.object(harness.brain, "process", return_value=offer):
            offered = harness.session.submit(DETERMINISTIC_REQUEST)

        pending = harness.session.pending_approval
        self.assertIsNot(pending.plan, listed)
        self.assertIs(offered.plan, pending.plan)
        self.assertIs(type(pending.plan.steps), tuple)
        self.assertEqual(pending.fingerprint, listed.fingerprint)
        self.assertEqual(harness.session.confirm().text, APPROVED_RESULT)

    def test_lists_and_list_subclasses_keep_their_behaviour(self) -> None:
        _, held_offer, _ = self.offer_deterministic(_approved())
        for container in (list, _PlainStepList, _Underreported):
            with self.subTest(container=container.__name__):
                # Run at once.
                harness = self.harness("unused")
                plan = _planned(harness, container(_approved()))

                self.assertEqual(
                    harness.session.submit(DETERMINISTIC_REQUEST).text,
                    APPROVED_RESULT,
                )
                self.assertIs(type(plan.steps), container)
                self.assertEqual(
                    harness.interactions(),
                    [(DETERMINISTIC_REQUEST, APPROVED_RESULT)],
                )

                # Offered and confirmed exactly as a plan holding a tuple is.
                harness, offered, pending = self.offer_deterministic(
                    container(_approved())
                )
                self.assertEqual(offered.text, held_offer.text)
                self.assertIs(type(pending.steps), tuple)
                self.assertEqual(
                    harness.session.pending_approval.fingerprint,
                    Plan(DETERMINISTIC_REQUEST, _approved()).fingerprint,
                )
                self.assertEqual(harness.session.confirm().text, APPROVED_RESULT)
                self.assertEqual(
                    harness.interactions(),
                    [(DETERMINISTIC_REQUEST, APPROVED_RESULT)],
                )

    def test_single_step_plans_behave_exactly_as_before(self) -> None:
        # A model-proposed action is rendered, bound, and run as V1-18 did.
        harness = self.harness(json.dumps(TIME))
        offered = self.offer_plan(
            harness,
            _one_step_approval("system", "current_time", "{}", NO_CONTEXT_NOTICE),
        )
        pending = harness.session.pending_approval
        self.assertIs(pending.plan, offered.plan)
        self.assertEqual(pending.fingerprint, offered.plan.fingerprint)

        confirmed = self.confirm(harness)

        self.assertEqual(
            confirmed,
            BrainResponse(f"{CLOCK_REPORT}\n\n{_recorded(1)}", plan=offered.plan),
        )
        self.assertIs(confirmed.plan, offered.plan)
        self.assertEqual(
            harness.audit_rows(),
            ([_summary_row(1, "system.current_time", "none")], [(1, "succeeded")]),
        )

        # A deterministic step awaiting confirmation is its own snapshot.
        harness, offered, pending = self.offer_deterministic((_calculation(1),))
        plan = harness.create_plan.return_value
        self.assertIs(offered.plan, plan)
        self.assertIs(pending, plan)
        self.assertEqual(harness.session.pending_approval.fingerprint, plan.fingerprint)

        confirmed = harness.session.confirm()

        self.assertEqual(confirmed, BrainResponse(_sum_result(1), plan=plan))
        self.assertEqual(
            harness.interactions(),
            [(DETERMINISTIC_REQUEST, _sum_result(1))],
        )


if __name__ == "__main__":
    unittest.main()
