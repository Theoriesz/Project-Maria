"""End-to-end tests for V1-22's model path and its timing, through a session.

Every test runs the real planner, compiler, contracts, permissions, tools,
memory layer, Brain, and session over a temporary database, with a synthetic
time zone configured and Brain's one clock seam pinned and counted. The
scripted model proposes whatever a test needs, faithful or hostile; Maria
builds every creation step from her own reading of the owner's request, so
a refused proposal must show only its fixed message and create nothing:
no approval is offered, no task, reminder, or audit row is written, and no
tool is dispatched.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from core import brain as brain_module
from core import permissions as permissions_module
from core import plan_compiler
from core.planner import PlanOrigin, PlanStep, RecordingPolicy
from core.plan_compiler import ProposalRejection
from core.reasoning import ReasoningResponse
from core.session import (
    CONFIRMATION_REFUSED_MESSAGE,
    PendingApproval,
    render_model_approval,
)
from core.time_resolution import TimingContext, TimingFailure
from tests.test_context import everything_sent
from tests.test_conversation import ScriptedProvider, _Harness, enabled_settings
from tests.zone_fixtures import CENTRAL_RULES, SYNTHETIC_KEY, SyntheticZones


# Saturday 2027-10-30, 10:03:27 local at +02:00 under the central rules.
REFERENCE = datetime(2027, 10, 30, 8, 3, 27, tzinfo=timezone.utc)
# Sunday 2027-10-31, 10:00 local at +01:00.
SUNDAY = datetime(2027, 10, 31, 9, 0, tzinfo=timezone.utc)
# Tuesday 2027-11-02, 23:59:59.900 local at +01:00, and a moment after midnight.
BEFORE_MIDNIGHT = datetime(2027, 11, 2, 22, 59, 59, 900000, tzinfo=timezone.utc)
AFTER_MIDNIGHT = datetime(2027, 11, 2, 23, 0, 0, 100000, tzinfo=timezone.utc)
ZONE = f"[{SYNTHETIC_KEY}]"
NOTICE = "No memory context supplied."
AGENDA_TABLES = ("tasks", "reminders", "audit_summaries", "audit_outcomes")
# The fixed notice a reminder's approval carries, written out by hand.
REMINDER_NOTICE = (
    "Maria will not notify you when this reminder is due, and it does not "
    "repeat: type due reminders to see the reminders whose time has come."
)


def _step(tool: str, action: str, arguments: dict[str, object]) -> dict[str, object]:
    return {"tool": tool, "action": action, "arguments": arguments}


def task(text: object, due: object = None) -> dict[str, object]:
    arguments: dict[str, object] = {"text": text}
    if due is not None:
        arguments["due"] = due
    return _step("tasks", "create", arguments)


def reminder(text: object, when: object, repeat: object = "never") -> dict[str, object]:
    return _step("reminders", "create", {"text": text, "when": when, "repeat": repeat})


def proposal(*steps: dict[str, object]) -> str:
    """Return one proposal exactly as a model would write it."""
    if len(steps) == 1:
        return json.dumps(steps[0])
    return json.dumps({"steps": list(steps)})


class _ModelPathCase(unittest.TestCase):
    """A real session with reasoning on, a synthetic zone, and a pinned clock."""

    reference = REFERENCE
    zone: str | None = SYNTHETIC_KEY

    def setUp(self) -> None:
        self.zones = SyntheticZones(self, {SYNTHETIC_KEY: CENTRAL_RULES})
        self.replies: list[str] = []
        self.provider = ScriptedProvider(self._reply)
        self.harness = _Harness(
            self,
            self.provider,
            # A larger output budget than the compiler's own byte cap, so the
            # compiler's refusal of an oversized proposal is reachable here.
            settings=enabled_settings(max_input_bytes=8192, max_output_bytes=8192),
        )
        self.harness.brain.config = dataclasses.replace(
            self.harness.brain.config,
            time_zone=self.zone,
        )
        self.clock = Mock(return_value=self.reference)
        seam = patch.object(brain_module, "_reference_instant", self.clock)
        seam.start()
        self.addCleanup(seam.stop)
        self.session = self.harness.session()

    def _reply(self, request: object) -> str:
        return self.replies.pop(0) if self.replies else "A conversational reply."

    def ask(self, request: str, *replies: str):
        self.replies[:] = list(replies)
        return self.session.submit(request)

    def offer(self, request: str, *replies: str):
        response = self.ask(request, *replies)
        self.assertTrue(response.needs_confirmation, response.text)
        return response

    def rows(self, table: str) -> list[tuple[object, ...]]:
        with closing(sqlite3.connect(self.harness.database_path)) as connection:
            return connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()

    def assertNothingCreated(self) -> None:
        counts = self.harness.row_counts()
        self.assertEqual(
            {table: counts[table] for table in AGENDA_TABLES},
            dict.fromkeys(AGENDA_TABLES, 0),
        )
        self.harness.execute_tool.assert_not_called()

    def assertRefused(
        self,
        request: str,
        reply: str,
        rejection: ProposalRejection,
    ) -> None:
        response = self.ask(request, reply)
        self.assertEqual(response.text, f"{rejection.message}\n\n{NOTICE}")
        self.assertFalse(response.needs_confirmation)
        self.assertIsNone(self.session.pending_approval)
        self.assertNothingCreated()


class FaithfulRequestTests(_ModelPathCase):
    """Prove each form, and joined forms, compile from the owner's own words."""

    def test_each_form_compiles_to_marias_canonical_step(self) -> None:
        for request, reply, step in (
            (
                "remind me tomorrow at 9:00 to call mom",
                proposal(reminder("call mom", "Tomorrow At 9:00")),
                PlanStep(
                    "reminders",
                    "create",
                    {
                        "text": "call mom",
                        "when": f"2027-10-31T09:00:00+01:00{ZONE}",
                        "repeat": "never",
                    },
                ),
            ),
            (
                "remind me to stretch in 20 minutes",
                proposal(reminder("stretch", "in 20 minutes")),
                PlanStep(
                    "reminders",
                    "create",
                    {
                        "text": "stretch",
                        "when": f"2027-10-30T10:23:00+02:00{ZONE}",
                        "repeat": "never",
                    },
                ),
            ),
            (
                "add a task to buy milk",
                proposal(task("buy milk")),
                PlanStep("tasks", "create", {"text": "buy milk"}),
            ),
            (
                "add a task to file taxes by friday.",
                proposal(task("file taxes", "friday")),
                PlanStep(
                    "tasks",
                    "create",
                    {"text": "file taxes", "due": "2027-11-05"},
                ),
            ),
        ):
            with self.subTest(request=request):
                response = self.offer(request, reply)
                plan = self.session.pending_approval.plan
                self.assertEqual(plan.steps, (step,))
                self.assertIs(plan.origin, PlanOrigin.MODEL)
                self.assertIs(plan.recording, RecordingPolicy.TRANSIENT)
                # The approval shows exactly the canonical arguments, and a
                # reminder's fixed notice before the context notice.
                notices = (
                    f"{REMINDER_NOTICE}\n{NOTICE}"
                    if step.tool_name == "reminders"
                    else NOTICE
                )
                self.assertEqual(
                    response.text,
                    f"{render_model_approval(plan)}\n\n{notices}",
                )
                self.assertIn(
                    "Arguments: " + json.dumps(dict(step.arguments), sort_keys=True),
                    response.text,
                )
                self.session.cancel()
        self.assertNothingCreated()

    def test_a_phrase_differing_only_in_case_or_spacing_still_binds(self) -> None:
        # The binding compares phrases after canonical tokenization, so only
        # ASCII case and the spaces around tokens may differ, and Maria's own
        # extraction still decides every value.
        for request, reply, arguments in (
            (
                "remind me tomorrow at 9:00 to call mom",
                proposal(reminder("call mom", "  TOMORROW   at 9:00 ")),
                {
                    "text": "call mom",
                    "when": f"2027-10-31T09:00:00+01:00{ZONE}",
                    "repeat": "never",
                },
            ),
            (
                "add a task to file taxes by friday",
                proposal(task("file taxes", " Friday ")),
                {"text": "file taxes", "due": "2027-11-05"},
            ),
        ):
            with self.subTest(request=request):
                self.offer(request, reply)
                (step,) = self.session.pending_approval.plan.steps
                self.assertEqual(dict(step.arguments), arguments)
                self.session.cancel()
        self.assertNothingCreated()

    def test_an_approved_creation_is_stored_under_one_audit_summary(self) -> None:
        self.offer(
            "remind me tomorrow at 9:00 to call mom",
            proposal(reminder("call mom", "tomorrow at 9:00")),
        )
        self.assertEqual(self.rows("reminders"), [])

        result = self.session.confirm()

        self.assertIn('Added reminder 1: "call mom"', result.text)
        self.assertIn("Recorded as audit summary 1.", result.text)
        (row,) = self.rows("reminders")
        self.assertEqual(
            row[1:5],
            (
                "call mom",
                f"2027-10-31T09:00:00+01:00{ZONE}",
                "2027-10-31T08:00:00Z",
                "open",
            ),
        )
        self.assertEqual(
            [row[2:] for row in self.rows("audit_summaries")],
            [("reminders.create", "owner_approved", "none")],
        )
        self.assertEqual(self.rows("audit_outcomes"), [(1, "succeeded")])
        # A model-originated plan is transient: neither request nor result
        # is recorded as interaction history.
        self.assertEqual(self.harness.interactions(), [])

    def test_two_items_joined_by_and_bind_by_position(self) -> None:
        self._assert_two_items_run_in_order(" and ")

    def test_two_items_joined_by_comma_and_bind_by_position(self) -> None:
        self._assert_two_items_run_in_order(", and ")

    def _assert_two_items_run_in_order(self, joiner: str) -> None:
        """Offer, approve, and run two items, auditing each before it runs."""
        request = (
            f"add a task to buy milk{joiner}remind me tomorrow at 9:00 to "
            "call mom"
        )
        self.offer(
            request,
            proposal(task("buy milk"), reminder("call mom", "tomorrow at 9:00")),
        )
        plan = self.session.pending_approval.plan
        # The joiner, comma included, belongs to no body.
        self.assertEqual(
            [dict(step.arguments)["text"] for step in plan.steps],
            ["buy milk", "call mom"],
        )
        audits_seen: list[int] = []
        for name in ("create_task", "create_reminder"):
            real = getattr(self.harness.memory, name)

            def watching(*args: object, _real=real, **kwargs: object) -> int:
                audits_seen.append(self.harness.row_counts()["audit_summaries"])
                return _real(*args, **kwargs)

            active = patch.object(self.harness.memory, name, side_effect=watching)
            active.start()
            self.addCleanup(active.stop)

        self.session.confirm()

        # Each step's summary was committed before its tool ran.
        self.assertEqual(audits_seen, [1, 2])
        self.assertEqual(len(self.rows("tasks")), 1)
        self.assertEqual(len(self.rows("reminders")), 1)
        self.assertEqual(
            [row[2] for row in self.rows("audit_summaries")],
            ["tasks.create", "reminders.create"],
        )

    def test_five_items_are_five_steps_under_one_approval(self) -> None:
        bodies = ("buy milk", "buy bread", "buy eggs", "buy rice", "buy tea")
        request = " and ".join(f"add a task to {body}" for body in bodies)
        self.offer(request, proposal(*(task(body) for body in bodies)))
        self.assertEqual(len(self.session.pending_approval.plan.steps), 5)

        self.session.confirm()

        self.assertEqual([row[1] for row in self.rows("tasks")], list(bodies))
        self.assertEqual(len(self.rows("audit_summaries")), 5)
        self.assertEqual(
            self.rows("audit_outcomes"),
            [(number, "succeeded") for number in range(1, 6)],
        )

    def test_a_failing_step_stops_the_later_ones(self) -> None:
        bodies = ("buy milk", "buy bread", "buy eggs")
        request = " and ".join(f"add a task to {body}" for body in bodies)
        self.offer(request, proposal(*(task(body) for body in bodies)))
        real = self.harness.memory.create_task
        calls: list[str] = []

        def second_fails(text: str, due: object = None) -> int:
            calls.append(text)
            if len(calls) == 2:
                raise RuntimeError("Unable to store task.")
            return real(text, due)

        with patch.object(self.harness.memory, "create_task", side_effect=second_fails):
            self.session.confirm()

        self.assertEqual(calls, ["buy milk", "buy bread"])
        self.assertEqual([row[1] for row in self.rows("tasks")], ["buy milk"])
        # One summary per step reached, and none for the third.
        self.assertEqual(
            [row[2] for row in self.rows("audit_summaries")],
            ["tasks.create", "tasks.create"],
        )
        self.assertEqual(self.rows("audit_outcomes"), [(1, "succeeded"), (2, "failed")])


class RefusedRequestTests(_ModelPathCase):
    """Prove a request that is not exactly a supported form creates nothing."""

    FAITHFUL_LOOKING = proposal(reminder("call mom", "tomorrow at 09:00"))

    def test_text_that_is_not_a_creation_request(self) -> None:
        for request in (
            "What does task mean?",
            "Don't remind me about that.",
            '"remind me tomorrow at 09:00 to call mom"',
            "For example: add a task to buy milk",
            "She said remind me tomorrow at 09:00 to call mom",
            "Please remind me tomorrow at 09:00 to call mom",
            "My task list is long",
            "I keep meaning to add a task to fix the gate",
            "remind me tomorrow at 09:00 to call mom?",
            "remind me tomorrow at 09:00 to call mom?.",
        ):
            with self.subTest(request=request):
                self.assertRefused(
                    request,
                    self.FAITHFUL_LOOKING,
                    ProposalRejection.NOT_A_CREATION_REQUEST,
                )

    def test_retractions_hedges_questions_and_quotations(self) -> None:
        for request, reply in (
            (
                "remind me tomorrow at 09:00 to call mom. Actually, don't.",
                self.FAITHFUL_LOOKING,
            ),
            ("add a task to buy milk? Not sure", proposal(task("buy milk"))),
            ("remind me tomorrow at 09:00 to call mom?!", self.FAITHFUL_LOOKING),
            ('add a task to buy milk" is just an example', proposal(task("buy milk"))),
            ("add a task to buy milk, just kidding", proposal(task("buy milk"))),
        ):
            with self.subTest(request=request):
                self.assertRefused(
                    request,
                    reply,
                    ProposalRejection.CREATION_REQUEST_UNCLEAR,
                )

    def test_invented_or_vague_timing_is_never_used(self) -> None:
        for request, reply in (
            ("remind me tomorrow", self.FAITHFUL_LOOKING),
            ("remind me to call mom", self.FAITHFUL_LOOKING),
            ("remind me tomorrow morning to call mom", self.FAITHFUL_LOOKING),
            ("remind me to call mom at 09:00", self.FAITHFUL_LOOKING),
            ("add a task to buy milk tomorrow", proposal(task("buy milk", "tomorrow"))),
        ):
            with self.subTest(request=request):
                self.assertRefused(
                    request,
                    reply,
                    ProposalRejection.CREATION_REQUEST_UNCLEAR,
                )

    def test_a_proposal_that_changes_the_timing_is_a_mismatch(self) -> None:
        for request, reply in (
            (
                "remind me tomorrow at 09:00 to call mom",
                proposal(reminder("call mom", "tomorrow at 10:00")),
            ),
            (
                "remind me tomorrow at 9:00 to call mom",
                proposal(reminder("call mom", "tomorrow at 09:00")),
            ),
            (
                "remind me tomorrow at 9:00 to call mom",
                proposal(reminder("call mom", f"2027-10-31T09:00:00+01:00{ZONE}")),
            ),
            ("add a task to buy milk", proposal(task("buy milk", "tomorrow"))),
            ("add a task to buy milk by friday", proposal(task("buy milk"))),
        ):
            with self.subTest(request=request, reply=reply):
                self.assertRefused(request, reply, ProposalRejection.CREATION_MISMATCH)

    def test_swapped_or_reordered_items_are_a_mismatch(self) -> None:
        request = (
            "remind me tomorrow at 9:00 to call mom and remind me tomorrow at "
            "10:00 to call dad"
        )
        for label, reply in (
            (
                "swapped timing",
                proposal(
                    reminder("call mom", "tomorrow at 10:00"),
                    reminder("call dad", "tomorrow at 9:00"),
                ),
            ),
            (
                "swapped bodies",
                proposal(
                    reminder("call dad", "tomorrow at 9:00"),
                    reminder("call mom", "tomorrow at 10:00"),
                ),
            ),
            (
                "reordered steps",
                proposal(
                    reminder("call dad", "tomorrow at 10:00"),
                    reminder("call mom", "tomorrow at 9:00"),
                ),
            ),
        ):
            with self.subTest(case=label):
                self.assertRefused(request, reply, ProposalRejection.CREATION_MISMATCH)

    def test_a_joined_second_request_is_unclear(self) -> None:
        for request, reply in (
            (
                "remind me tomorrow at 09:00 to call mom. Remind me tomorrow at "
                "10:00 to call dad",
                proposal(
                    reminder(
                        "call mom. Remind me tomorrow at 10:00 to call dad",
                        "tomorrow at 09:00",
                    )
                ),
            ),
            (
                "add a task to buy milk; add a task to buy bread",
                proposal(task("buy milk; add a task to buy bread")),
            ),
            (
                "remind me tomorrow at 09:00 to call mom then remind me tomorrow "
                "at 10:00 to call dad",
                proposal(
                    reminder(
                        "call mom then remind me tomorrow at 10:00 to call dad",
                        "tomorrow at 09:00",
                    )
                ),
            ),
            (
                "add a task to call Bob at 15:00 by friday",
                proposal(task("call Bob at 15:00", "friday")),
            ),
        ):
            with self.subTest(request=request):
                self.assertRefused(
                    request,
                    reply,
                    ProposalRejection.CREATION_REQUEST_UNCLEAR,
                )

    def test_duplicate_extra_and_unrequested_steps(self) -> None:
        for reply in (
            proposal(task("buy milk"), task("buy milk")),
            proposal(task("buy milk"), task("buy bread")),
            proposal(
                task("buy milk"),
                _step("calculator", "calculate", {"expression": "1 + 1"}),
            ),
        ):
            with self.subTest(reply=reply):
                self.assertRefused(
                    "add a task to buy milk",
                    reply,
                    ProposalRejection.CREATION_MISMATCH,
                )
        self.assertRefused(
            "add a task to buy milk and add a task to buy milk",
            proposal(task("buy milk"), task("buy milk")),
            ProposalRejection.CREATION_REQUEST_UNCLEAR,
        )

    def test_body_fidelity(self) -> None:
        for text in (
            "buy oat",
            "buy oat milk now",
            "Buy oat milk",
            "purchase oat milk",
        ):
            with self.subTest(text=text):
                self.assertRefused(
                    "add a task to buy oat milk",
                    proposal(task(text)),
                    ProposalRejection.CREATION_MISMATCH,
                )

    def test_six_items_are_unclear(self) -> None:
        bodies = (
            "buy milk",
            "buy bread",
            "buy eggs",
            "buy rice",
            "buy tea",
            "buy salt",
        )
        request = " and ".join(f"add a task to {body}" for body in bodies)
        self.assertRefused(
            request,
            proposal(*(task(body) for body in bodies[:5])),
            ProposalRejection.CREATION_REQUEST_UNCLEAR,
        )

    def test_casual_conversation_never_creates_anything(self) -> None:
        always_proposes = proposal(task("buy milk"))
        for request in (
            "hello there",
            "I need to buy milk",
            "what should I do tomorrow at 9:00",
            "tasks are boring",
            "my reminders app is broken",
            "remind me why we did that",
            "add a task",
        ):
            with self.subTest(request=request):
                response = self.ask(request, always_proposes)
                self.assertFalse(response.needs_confirmation)
                self.assertIsNone(self.session.pending_approval)
        self.assertNothingCreated()


class SameItemTwiceTests(_ModelPathCase):
    """Prove equivalent phrases resolving to one time are one item named twice."""

    reference = SUNDAY

    def test_equivalent_phrases_are_a_duplicate(self) -> None:
        self.assertRefused(
            "remind me tomorrow at 9:00 to call mom and remind me on monday at "
            "09:00 to call mom",
            proposal(
                reminder("call mom", "tomorrow at 9:00"),
                reminder("call mom", "on monday at 09:00"),
            ),
            ProposalRejection.CREATION_REQUEST_UNCLEAR,
        )


class RawArgumentTests(_ModelPathCase):
    """Prove raw checks refuse before the request is parsed or resolved."""

    REQUEST = "remind me tomorrow at 9:00 to call mom"

    def setUp(self) -> None:
        super().setUp()
        for name in (
            "resolve_reminder_time",
            "resolve_task_date",
            "parse_creation_request",
        ):
            active = patch.object(
                plan_compiler,
                name,
                side_effect=AssertionError(f"{name} may not be reached"),
            )
            active.start()
            self.addCleanup(active.stop)

    def test_each_raw_check_has_its_own_refusal(self) -> None:
        for label, reply, rejection in (
            (
                "a number for when",
                proposal(reminder("call mom", 900)),
                ProposalRejection.WRONG_TYPE,
            ),
            (
                "null for when",
                proposal(reminder("call mom", None)),
                ProposalRejection.WRONG_TYPE,
            ),
            (
                "65 characters",
                proposal(reminder("call mom", "a" * 65)),
                ProposalRejection.OUT_OF_RANGE,
            ),
            (
                "non-ASCII digits",
                proposal(reminder("call mom", f"tomorrow at {chr(0x0669)}:00")),
                ProposalRejection.OUT_OF_RANGE,
            ),
            (
                "a line separator",
                proposal(reminder("call mom", f"tomorrow{chr(0x2028)}at 9:00")),
                ProposalRejection.UNSAFE_TEXT,
            ),
            (
                "text over 512 bytes",
                proposal(reminder(chr(0x00E9) * 257, "tomorrow at 9:00")),
                ProposalRejection.OUT_OF_RANGE,
            ),
            (
                "a repeat other than never",
                proposal(reminder("call mom", "tomorrow at 9:00", "daily")),
                ProposalRejection.OUT_OF_RANGE,
            ),
            (
                "a missing repeat",
                proposal(
                    _step("reminders", "create", {"text": "call mom", "when": "x"})
                ),
                ProposalRejection.MISSING_ARGUMENT,
            ),
            (
                "an extra argument",
                proposal(
                    _step(
                        "tasks",
                        "create",
                        {"text": "call mom", "zone": "Europe/Paris"},
                    )
                ),
                ProposalRejection.EXTRA_ARGUMENT,
            ),
            (
                "oversized",
                proposal(reminder("call mom", "tomorrow at 9:00")) + " " * 4096,
                ProposalRejection.OVERSIZED,
            ),
            (
                "not JSON",
                "{'tool': 'tasks'}",
                ProposalRejection.MALFORMED_OUTPUT,
            ),
        ):
            with self.subTest(case=label):
                self.assertRefused(self.REQUEST, reply, rejection)


class ClockSeamTests(_ModelPathCase):
    """Prove one clock reading per request, and none at confirmation."""

    def test_one_reading_per_request_whatever_the_step_count(self) -> None:
        bodies = ("buy milk", "buy bread", "buy eggs", "buy rice", "buy tea")
        for count in (0, 1, 2, 5):
            with self.subTest(steps=count):
                self.clock.reset_mock()
                calls_before = self.provider.calls
                if count == 0:
                    self.ask("hello there")
                    self.assertEqual(self.clock.call_count, 1)
                    continue
                request = " and ".join(
                    f"add a task to {body}" for body in bodies[:count]
                )
                self.offer(request, proposal(*(task(body) for body in bodies[:count])))
                self.assertEqual(self.clock.call_count, 1)
                self.session.confirm()
                self.assertEqual(self.clock.call_count, 1)
                self.assertEqual(self.provider.calls, calls_before + 1)

    def test_deterministic_commands_read_the_clock_once_too(self) -> None:
        for request in ("help", "tasks", "due tasks", "add task due friday: buy milk"):
            with self.subTest(request=request):
                self.clock.reset_mock()
                self.ask(request)
                self.assertEqual(self.clock.call_count, 1)
        self.assertIsNotNone(self.session.pending_approval)
        self.clock.reset_mock()

        self.session.confirm()

        self.assertEqual(self.clock.call_count, 0)
        self.assertEqual(
            [row[1:3] for row in self.rows("tasks")],
            [("buy milk", "2027-11-05")],
        )
        self.assertEqual(self.provider.calls, 0)

    def test_an_empty_request_reads_no_clock(self) -> None:
        self.clock.reset_mock()
        self.ask("   ")
        self.clock.assert_not_called()


class MidnightAndLateApprovalTests(_ModelPathCase):
    """Prove the captured instant decides, however late the reply or approval."""

    reference = BEFORE_MIDNIGHT

    def test_a_reply_after_midnight_resolves_against_the_captured_date(self) -> None:
        def reply_after_midnight(request: object) -> str:
            self.clock.return_value = AFTER_MIDNIGHT
            return proposal(task("file taxes", "tomorrow"))

        self.provider._answer = reply_after_midnight
        self.offer("add a task to file taxes by tomorrow")

        self.assertEqual(
            self.session.pending_approval.plan.steps,
            (PlanStep("tasks", "create", {"text": "file taxes", "due": "2027-11-03"}),),
        )
        self.session.confirm()
        self.assertEqual(self.rows("tasks")[0][2], "2027-11-03")
        self.assertEqual(self.clock.call_count, 1)

    def test_due_tasks_uses_the_captured_local_date(self) -> None:
        self.harness.memory.create_task("today", "2027-11-02")
        self.harness.memory.create_task("tomorrow", "2027-11-03")

        response = self.ask("due tasks")

        self.assertIn('"today", due Tuesday 2027-11-02 (due today)', response.text)
        self.assertNotIn("tomorrow", response.text)

    def test_a_reminder_approved_after_its_moment_is_stored_as_approved(
        self,
    ) -> None:
        self.offer(
            "remind me in 1 minute to stir",
            proposal(reminder("stir", "in 1 minute")),
        )
        # The approval comes after midnight and after the reminder's moment.
        self.clock.return_value = AFTER_MIDNIGHT.replace(minute=30)
        calls = (self.clock.call_count, self.provider.calls)

        self.session.confirm()

        self.assertEqual((self.clock.call_count, self.provider.calls), calls)
        (row,) = self.rows("reminders")
        self.assertEqual(
            row[2:4],
            (f"2027-11-03T00:00:00+01:00{ZONE}", "2027-11-02T23:00:00Z"),
        )
        due = self.ask("due reminders")
        self.assertIn('1: "stir"', due.text)
        self.assertIn("(time reached)", due.text)

    def test_a_task_due_today_approved_after_midnight_is_stored_as_approved(
        self,
    ) -> None:
        self.offer(
            "add a task to pay rent by today",
            proposal(task("pay rent", "today")),
        )
        self.clock.return_value = AFTER_MIDNIGHT
        calls = (self.clock.call_count, self.provider.calls)

        self.session.confirm()

        self.assertEqual((self.clock.call_count, self.provider.calls), calls)
        self.assertEqual(self.rows("tasks")[0][2], "2027-11-02")
        overdue = self.ask("due tasks")
        self.assertIn('"pay rent", due Tuesday 2027-11-02 (overdue)', overdue.text)


class PastRefusalScopeTests(_ModelPathCase):
    """Prove only new timing is refused when past; old records stay usable."""

    def test_new_timing_at_or_before_the_reference_is_refused(self) -> None:
        self.assertRefused(
            "remind me today at 10:03 to stir",
            proposal(reminder("stir", "today at 10:03")),
            ProposalRejection.TIME_PAST,
        )
        self.assertRefused(
            "add a task to file taxes by 2027-10-29",
            proposal(task("file taxes", "2027-10-29")),
            ProposalRejection.TIME_PAST,
        )
        self.assertRefused(
            "add a task to file taxes by 2037-10-31",
            proposal(task("file taxes", "2037-10-31")),
            ProposalRejection.TIME_TOO_FAR,
        )
        self.assertRefused(
            "remind me on 2028-03-26 at 02:30 to stir",
            proposal(reminder("stir", "on 2028-03-26 at 02:30")),
            ProposalRejection.TIME_NONEXISTENT,
        )
        self.assertRefused(
            "remind me tomorrow at 02:30 to stir",
            proposal(reminder("stir", "tomorrow at 02:30")),
            ProposalRejection.TIME_AMBIGUOUS,
        )

    def test_a_reschedule_to_past_timing_is_refused_and_writes_nothing(self) -> None:
        self.harness.memory.create_task("file taxes", "2027-11-05")
        self.harness.memory.create_reminder(
            "stir",
            f"2027-10-30T18:00:00+02:00{ZONE}",
            "2027-10-30T16:00:00Z",
        )
        rows = {table: self.rows(table) for table in ("tasks", "reminders")}
        past = f"{TimingFailure.PAST.message} Nothing was saved."
        for request in (
            "reschedule task 1: 2027-10-29",
            # The reference is 10:03:27 local, so 10:03 has passed.
            "reschedule reminder 1: today 10:03",
            "reschedule reminder 1: 2027-10-29 23:00",
        ):
            with self.subTest(request=request):
                response = self.ask(request)
                self.assertEqual(response.text, past)
                self.assertFalse(response.needs_confirmation)
        for table, saved in rows.items():
            self.assertEqual(self.rows(table), saved)
        self.assertEqual(self.provider.calls, 0)

    def test_overdue_records_stay_fully_usable(self) -> None:
        self.harness.memory.create_task("old task", "2001-01-01")
        self.harness.memory.create_reminder(
            "old reminder",
            f"2001-01-01T09:00:00+01:00{ZONE}",
            "2001-01-01T08:00:00Z",
        )
        for request, needs_confirmation in (
            ("read task 1", False),
            ("read reminder 1", False),
            ("complete task 1", False),
            ("reopen task 1", False),
            ("complete reminder 1", False),
            ("reopen reminder 1", False),
            ("edit task 1: older task", True),
            ("edit reminder 1: older reminder", True),
            ("delete task 1", True),
            ("delete reminder 1", True),
        ):
            with self.subTest(request=request):
                response = self.ask(request)
                self.assertEqual(response.needs_confirmation, needs_confirmation)
                if needs_confirmation:
                    response = self.session.confirm()
                self.assertNotIn("Unable", response.text)
        self.assertEqual(self.rows("tasks"), [])
        self.assertEqual(self.rows("reminders"), [])


class DeterministicApprovalTests(_ModelPathCase):
    """Prove the exact previews before storage, cancellation, and fingerprints."""

    def test_adds_show_the_exact_text_and_resolved_timing_first(self) -> None:
        task_offer = self.offer("add task due tomorrow: buy milk")
        self.assertEqual(
            task_offer.text,
            "Confirmation required: tasks.create requires explicit confirmation.\n"
            "This approval adds a task, shown quoted:\n"
            'Text: "buy milk"\n'
            "Due: Sunday 2027-10-31, a calendar date with no time",
        )
        self.assertEqual(self.rows("tasks"), [])
        self.session.cancel()

        reminder_offer = self.offer("add reminder tomorrow 9:00: call mom")
        self.assertEqual(
            reminder_offer.text,
            "Confirmation required: reminders.create requires explicit "
            "confirmation. Maria will not notify you when this reminder is due, "
            "and it does not repeat: type due reminders to see the reminders whose "
            "time has come.\n"
            "This approval adds a reminder, shown quoted:\n"
            'Text: "call mom"\n'
            f"Time: Sunday 2027-10-31 at 09:00, {SYNTHETIC_KEY} (UTC+01:00), "
            "which is 2027-10-31 08:00 UTC\n"
            "Repeats: never",
        )
        self.assertEqual(self.rows("reminders"), [])
        self.assertTrue(self.session.cancel())
        self.assertIsNone(self.session.pending_approval)
        self.assertEqual(self.rows("reminders"), [])
        self.assertEqual(self.provider.calls, 0)

    def test_a_changed_plan_is_refused_by_its_fingerprint(self) -> None:
        self.offer("add task due tomorrow: buy milk")
        pending = self.session.pending_approval
        changed = dataclasses.replace(
            pending.plan,
            steps=(
                PlanStep("tasks", "create", {"text": "buy milk", "due": "2027-12-31"}),
            ),
        )
        self.session._pending = PendingApproval(changed, pending.fingerprint)

        response = self.session.confirm()

        self.assertEqual(response.text, CONFIRMATION_REFUSED_MESSAGE)
        self.assertEqual(self.rows("tasks"), [])


class ModelApprovalNoticeTests(_ModelPathCase):
    """Prove a model plan's approval shows its steps' fixed notices, once.

    Owner decision A: before a model plan is approved, each confirmation-
    required step's fixed permission notice is shown once, in plan order,
    after the exact compiled steps, so a reminder made from the owner's own
    words says that Maria will not notify. Nothing else about the offer, the
    plan, its approval, or what is kept changes.
    """

    REMINDER = "remind me tomorrow at 9:00 to call mom"
    SEVERAL = (
        "remind me tomorrow at 9:00 to call mom and add a task to buy milk "
        "and remind me to stretch in 20 minutes"
    )

    def several_reply(self) -> str:
        return proposal(
            reminder("call mom", "tomorrow at 9:00"),
            task("buy milk"),
            reminder("stretch", "in 20 minutes"),
        )

    def test_a_reminder_offer_shows_its_notice_before_approval(self) -> None:
        response = self.offer(
            self.REMINDER,
            proposal(reminder("call mom", "tomorrow at 9:00")),
        )
        plan = self.session.pending_approval.plan

        self.assertEqual(
            response.text,
            f"{render_model_approval(plan)}\n\n{REMINDER_NOTICE}\n{NOTICE}",
        )
        self.assertEqual(response.text.count(REMINDER_NOTICE), 1)
        self.assertEqual(self.rows("reminders"), [])

    def test_a_plan_of_several_steps_shows_each_notice_once(self) -> None:
        response = self.offer(self.SEVERAL, self.several_reply())
        plan = self.session.pending_approval.plan

        self.assertEqual(
            [f"{step.tool_name}.{step.action}" for step in plan.steps],
            ["reminders.create", "tasks.create", "reminders.create"],
        )
        self.assertEqual(
            response.text,
            f"{render_model_approval(plan)}\n\n{REMINDER_NOTICE}\n{NOTICE}",
        )
        self.assertEqual(response.text.count(REMINDER_NOTICE), 1)

    def test_a_plan_with_no_notice_is_shown_as_before(self) -> None:
        response = self.offer("add a task to buy milk", proposal(task("buy milk")))
        plan = self.session.pending_approval.plan

        self.assertEqual(response.text, f"{render_model_approval(plan)}\n\n{NOTICE}")
        self.assertNotIn(REMINDER_NOTICE, response.text)

    def test_the_notice_changes_nothing_but_the_display(self) -> None:
        compiled = plan_compiler.compile_proposal(
            self.SEVERAL,
            ReasoningResponse(self.several_reply()),
            self.harness.tool_manager.contracts,
            logger=Mock(spec=logging.Logger),
            timing=TimingContext(REFERENCE, SYNTHETIC_KEY),
        )

        response = self.offer(self.SEVERAL, self.several_reply())
        pending = self.session.pending_approval

        # The exact compiled plan and arguments, bound by their fingerprint.
        self.assertEqual(pending.plan, compiled)
        self.assertEqual(pending.fingerprint, compiled.fingerprint)
        self.assertTrue(response.text.startswith(render_model_approval(compiled)))
        self.assertEqual(self.session.conversation, ())
        calls = (self.provider.calls, self.clock.call_count)

        # One approval runs the whole plan, with no model call or clock read.
        result = self.session.confirm()

        self.assertEqual((self.provider.calls, self.clock.call_count), calls)
        self.assertNotIn(REMINDER_NOTICE, result.text)
        self.assertEqual(len(self.rows("reminders")), 2)
        self.assertEqual(len(self.rows("tasks")), 1)
        self.assertEqual(len(self.rows("audit_summaries")), 3)
        self.assertEqual(self.session.conversation, ())

        # The notice is kept nowhere: no conversation, history, or request.
        self.ask("thank you")
        self.assertEqual(len(self.session.conversation), 1)
        for exchange in self.session.conversation:
            self.assertNotIn(REMINDER_NOTICE, exchange.user + exchange.assistant)
        self.assertEqual(self.harness.interactions(), [])
        self.assertNotIn(REMINDER_NOTICE, everything_sent(self.provider.requests))

    def test_notices_come_from_permission_manager_in_plan_order(self) -> None:
        # Give the task action a notice of its own: Brain must show whatever
        # PermissionManager answers, each once, in the order the plan holds.
        request = (
            "add a task to buy milk and remind me tomorrow at 9:00 to call mom "
            "and add a task to buy bread"
        )
        reply = proposal(
            task("buy milk"),
            reminder("call mom", "tomorrow at 9:00"),
            task("buy bread"),
        )
        notices = {
            **permissions_module.CONFIRMATION_NOTICES,
            "tasks.create": "TASK-NOTE",
        }

        with patch.object(permissions_module, "CONFIRMATION_NOTICES", notices):
            response = self.offer(request, reply)
        plan = self.session.pending_approval.plan

        self.assertEqual(
            response.text,
            f"{render_model_approval(plan)}\n\nTASK-NOTE\n{REMINDER_NOTICE}\n{NOTICE}",
        )
        self.assertEqual(response.text.count("TASK-NOTE"), 1)

    def test_only_steps_awaiting_confirmation_are_asked_for_a_notice(self) -> None:
        asked: list[tuple[str, str]] = []

        def answer(tool_name: str, action: str) -> str:
            asked.append((tool_name, action))
            return f"NOTE {tool_name}.{action}"

        with patch.object(
            self.harness.permissions,
            "confirmation_notice",
            side_effect=answer,
        ):
            allowed = self.offer(
                "what is 2 + 2",
                proposal(_step("calculator", "calculate", {"expression": "2 + 2"})),
            )
            self.assertEqual(asked, [])
            self.assertNotIn("NOTE", allowed.text)
            self.session.cancel()

            awaiting = self.offer(self.SEVERAL, self.several_reply())

        # PermissionManager's own check asks too; no allowed step is ever
        # asked about, by it or by Brain.
        self.assertEqual(
            sorted(set(asked)),
            [("reminders", "create"), ("tasks", "create")],
        )
        plan = self.session.pending_approval.plan
        self.assertEqual(
            awaiting.text,
            f"{render_model_approval(plan)}\n\n"
            "NOTE reminders.create\nNOTE tasks.create\n"
            f"{NOTICE}",
        )


class ModelPathOffsetTests(_ModelPathCase):
    """Prove an owner's offset is used only when it matches the zone.

    Owner decision B: an explicit offset is accepted only when it is one the
    configured zone has at that local time. For a repeated local time it
    selects the occurrence; any other offset is refused, and Maria builds
    the step from her own resolution of the owner's phrase.
    """

    def when_offered(self, request: str, phrase: str) -> str:
        self.offer(request, proposal(reminder("check the clocks", phrase)))
        (step,) = self.session.pending_approval.plan.steps
        self.session.cancel()
        return step.arguments["when"]

    def test_a_repeated_time_takes_the_occurrence_its_offset_names(self) -> None:
        for offset, expected in (
            ("+02:00", f"2027-10-31T02:30:00+02:00{ZONE}"),
            ("+01:00", f"2027-10-31T02:30:00+01:00{ZONE}"),
        ):
            with self.subTest(offset=offset):
                phrase = f"tomorrow at 02:30 {offset}"
                self.assertEqual(
                    self.when_offered(
                        f"remind me {phrase} to check the clocks",
                        phrase,
                    ),
                    expected,
                )

    def test_a_matching_offset_on_an_ordinary_time_changes_nothing(self) -> None:
        with_offset = self.when_offered(
            "remind me tomorrow at 09:00 +01:00 to check the clocks",
            "tomorrow at 09:00 +01:00",
        )
        without = self.when_offered(
            "remind me tomorrow at 09:00 to check the clocks",
            "tomorrow at 09:00",
        )

        self.assertEqual(with_offset, f"2027-10-31T09:00:00+01:00{ZONE}")
        self.assertEqual(with_offset, without)

    def test_an_offset_the_zone_does_not_have_there_is_refused(self) -> None:
        for phrase in ("tomorrow at 09:00 +02:00", "tomorrow at 02:30 +03:00"):
            with self.subTest(phrase=phrase):
                self.assertRefused(
                    f"remind me {phrase} to check the clocks",
                    proposal(reminder("check the clocks", phrase)),
                    ProposalRejection.TIME_AMBIGUOUS,
                )


class ZoneUnavailableTests(_ModelPathCase):
    """Prove only resolving new timing and ``due tasks`` need a zone."""

    zone = None

    def test_operations_on_existing_records_need_no_zone(self) -> None:
        self.harness.memory.create_task("old task", "2001-01-01")
        self.harness.memory.create_reminder(
            "old reminder",
            f"2001-01-01T09:00:00+01:00{ZONE}",
            "2001-01-01T08:00:00Z",
        )
        for request, confirm in (
            ("tasks", False),
            ("completed tasks", False),
            ("read task 1", False),
            ("complete task 1", False),
            ("reopen task 1", False),
            ("reminders", False),
            ("completed reminders", False),
            ("due reminders", False),
            ("read reminder 1", False),
            ("complete reminder 1", False),
            ("reopen reminder 1", False),
            ("add task buy milk", True),
            ("reschedule task 1: none", True),
            ("edit task 1: older task", True),
            ("edit reminder 1: older reminder", True),
            ("enable task context 1", True),
            ("disable task context 1", True),
            ("enable reminder context 1", True),
            ("disable reminder context 1", True),
            ("delete reminder 1", True),
            ("delete task 1", True),
        ):
            with self.subTest(request=request):
                response = self.ask(request)
                self.assertEqual(response.needs_confirmation, confirm, response.text)
                if confirm:
                    response = self.session.confirm()
                for refusal in ("Unable", "cannot resolve", "time zone"):
                    self.assertNotIn(refusal, response.text)
        # Every operation took effect: task 1 was edited and then deleted,
        # the added task remains, and the reminder was deleted.
        self.assertEqual(
            [row[:4] for row in self.rows("tasks")],
            [(2, "buy milk", None, "open")],
        )
        self.assertEqual(self.rows("reminders"), [])

    def test_new_timing_and_due_tasks_answer_their_fixed_text(self) -> None:
        refusal = TimingFailure.ZONE_UNAVAILABLE.message
        # Existing records, so a refused reschedule is shown to write nothing.
        self.harness.memory.create_task("old task", "2001-01-01")
        self.harness.memory.create_reminder(
            "old reminder",
            f"2001-01-01T09:00:00+01:00{ZONE}",
            "2001-01-01T08:00:00Z",
        )
        before = self.harness.row_counts()
        rows = {table: self.rows(table) for table in ("tasks", "reminders")}
        for request, expected in (
            ("due tasks", refusal),
            ("add task due tomorrow: buy milk", f"{refusal} Nothing was saved."),
            ("reschedule task 1: friday", f"{refusal} Nothing was saved."),
            ("add reminder tomorrow 9:00: call mom", f"{refusal} Nothing was saved."),
            ("add reminder in 5 minutes: stir", f"{refusal} Nothing was saved."),
            ("reschedule reminder 1: in 2 hours", f"{refusal} Nothing was saved."),
        ):
            with self.subTest(request=request):
                response = self.ask(request)
                self.assertEqual(response.text, expected)
                self.assertFalse(response.needs_confirmation)
        counts = self.harness.row_counts()
        for table in AGENDA_TABLES:
            self.assertEqual(counts[table], before[table])
        for table, saved in rows.items():
            self.assertEqual(self.rows(table), saved)
        self.assertEqual(self.provider.calls, 0)

    def test_a_dated_model_request_is_refused_and_an_undated_one_works(self) -> None:
        self.assertRefused(
            "remind me tomorrow at 9:00 to call mom",
            proposal(reminder("call mom", "tomorrow at 9:00")),
            ProposalRejection.TIME_ZONE_UNAVAILABLE,
        )
        self.offer("add a task to buy milk", proposal(task("buy milk")))
        self.session.confirm()
        self.assertEqual([row[1:3] for row in self.rows("tasks")], [("buy milk", None)])


class UnloadableZoneTests(ZoneUnavailableTests):
    """The same, with a configured zone whose data cannot be loaded."""

    zone = "Maria/Missing"


class DeterministicTimingTests(_ModelPathCase):
    """Prove deterministic timing never involves the model."""

    def test_timing_commands_never_ask_the_model(self) -> None:
        for request in (
            "add reminder tomorrow 9:00: call mom",
            "add task due friday: file taxes",
            "due tasks",
            "due reminders",
            "reschedule task 1: tomorrow",
        ):
            with self.subTest(request=request):
                self.ask(request)
                self.session.cancel()
        self.assertEqual(self.provider.calls, 0)


class LogPrivacyTests(_ModelPathCase):
    """Prove logs carry ids and fixed codes, never text, timing, or digests."""

    def test_no_owner_value_reaches_any_log(self) -> None:
        records: list[logging.LogRecord] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Collect(level=logging.DEBUG)
        watched = [logging.getLogger("maria"), self.harness.logger]
        for logger in watched:
            previous = logger.level
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
            self.addCleanup(logger.removeHandler, handler)
            self.addCleanup(logger.setLevel, previous)

        secret = "private-agenda-sentinel-4e81"
        for request, reply in (
            (f"add a task to {secret} by friday", proposal(task(secret, "friday"))),
            (
                f"remind me tomorrow at 9:00 to {secret}",
                proposal(reminder(secret, "tomorrow at 9:00")),
            ),
            (f"add a task to {secret} by friday!", proposal(task(secret, "friday"))),
        ):
            response = self.ask(request, reply)
            if response.needs_confirmation:
                self.session.confirm()
        for request in (
            f"add reminder tomorrow 18:30: {secret}",
            "enable reminder context 2",
            f"edit task 1: {secret} again",
            "enable task context 1",
            "reschedule reminder 1: in 2 hours",
        ):
            if self.ask(request).needs_confirmation:
                self.session.confirm()
        for request in (
            "tasks",
            "reminders",
            "due tasks",
            "due reminders",
            "read reminder 1",
        ):
            self.ask(request)

        logged = "\n".join(record.getMessage() for record in records)
        self.assertIn("Added task 1 through tasks tool.", logged)
        for value in (
            secret,
            "2027-11-05",
            "2027-10-31",
            # The reference instant, local today (the due tasks query's
            # date), and the rescheduled reminder's time, local and UTC.
            "2027-10-30",
            "08:03",
            "10:03",
            "12:03",
            "09:00",
            "18:30",
            SYNTHETIC_KEY,
            "+01:00",
            "+02:00",
            "T08:00:00Z",
            "friday",
            "tomorrow",
        ):
            with self.subTest(value=value):
                self.assertNotIn(value, logged)
        digests = [
            row[-1]
            for table in ("tasks", "reminders")
            for row in self.rows(table)
            if row[-1]
        ]
        # Task 1 and reminder 2 hold consent, so both kinds are checked.
        self.assertEqual(len(digests), 2)
        for digest in digests:
            with self.subTest(digest=digest[:8]):
                self.assertNotIn(digest, logged)


class NoDeliveryTests(_ModelPathCase):
    """Prove nothing is delivered: no thread, timer, or unasked output."""

    def test_a_due_reminder_is_shown_only_when_asked(self) -> None:
        threads = threading.active_count()
        self.harness.memory.create_reminder(
            "past",
            f"2001-01-01T09:00:00+01:00{ZONE}",
            "2001-01-01T08:00:00Z",
        )
        # One more through the owner's own command, whose moment then passes.
        self.offer("add reminder in 1 minute: stretch")
        self.session.confirm()
        self.clock.return_value = REFERENCE + timedelta(minutes=2)

        unrelated = self.ask("calculate 1 + 1")

        self.assertNotIn("past", unrelated.text)
        self.assertNotIn("stretch", unrelated.text)
        due = self.ask("due reminders").text
        self.assertIn('"past"', due)
        self.assertIn('"stretch"', due)
        self.assertEqual(threading.active_count(), threads)

    def test_the_new_modules_import_no_concurrency_or_clock_module(self) -> None:
        import ast
        import inspect

        from core import task_records, task_requests, time_resolution
        from tools import reminders, tasks

        forbidden = {"threading", "sched", "asyncio", "signal", "time", "_thread"}
        for module in (time_resolution, task_records, task_requests, tasks, reminders):
            with self.subTest(module=module.__name__):
                imported = set()
                for node in ast.walk(ast.parse(inspect.getsource(module))):
                    if isinstance(node, ast.Import):
                        imported.update(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom):
                        imported.add(node.module)
                self.assertFalse(imported & forbidden)


if __name__ == "__main__":
    unittest.main()
