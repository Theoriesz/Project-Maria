"""Focused tests for Maria's immutable, fingerprintable plan value objects."""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import unittest
from types import MappingProxyType
from typing import Any

from core.planner import (
    MODEL_PLAN_SHAPE_MESSAGE,
    PLAN_STEP_LIMIT_MESSAGE,
    Plan,
    PlanArguments,
    Planner,
    PlanOrigin,
    PlanStep,
    RecordingPolicy,
    is_model_plan,
)


SENTINEL_REQUEST = "sentinel-request-4f7a21c8e9"
SENTINEL_FACT = "sentinel-fact-6b1d09af35"

# Golden digests lock the canonical serialization, so a later stage binding
# approval to a fingerprint cannot be broken by a silent format change.
NO_ACTION_FINGERPRINT = (
    "1db8991f7c681b41e7950a63928c33ca2955943c1db7d9d85f249ccce183ebc2"
)
SINGLE_STEP_FINGERPRINT = (
    "1a6b3bd0ced2c697281afe009cbcb6b79d9a61e0e670c58532fff82eeef997b3"
)
TRANSIENT_NO_ACTION_FINGERPRINT = (
    "14bea83058b03660145137c624a08014d7e993c63d3928cff08e837f3a9a286a"
)
MODEL_STEP_FINGERPRINT = (
    "42be365953cb90b6c9dd10f721893e623ddad655236ebb5eb8dcc80e267cc15b"
)

# The canonical bytes each golden digest above is the SHA-256 of, written
# out by hand so the places of the recording policy and the origin in the
# format are stated rather than inferred from the implementation. A default
# plan carries neither a recording key nor an origin key at all.
NO_ACTION_CANONICAL = (
    b'{"original_request":"help",'
    b'"response_if_no_action":"Supported commands","steps":[]}'
)
SINGLE_STEP_CANONICAL = (
    b'{"original_request":"remember My Fact","response_if_no_action":null,'
    b'"steps":[{"action":"store","arguments":{"fact":"My Fact"},'
    b'"tool_name":"memory"}]}'
)
TRANSIENT_NO_ACTION_CANONICAL = (
    b'{"original_request":"help","recording":"transient",'
    b'"response_if_no_action":"Supported commands","steps":[]}'
)
# A model-originated plan always carries both keys, because its policy is
# always transient, and "origin" sorts before "original_request".
MODEL_STEP_CANONICAL = (
    b'{"origin":"model","original_request":"calculate 2 + 2",'
    b'"recording":"transient","response_if_no_action":null,'
    b'"steps":[{"action":"calculate","arguments":{"expression":"2 + 2"},'
    b'"tool_name":"calculator"}]}'
)


class PlanHashabilityTests(unittest.TestCase):
    """Verify Plan and PlanStep satisfy Python's equality and hash contract."""

    def test_plan_step_is_hashable(self) -> None:
        step = PlanStep("memory", "store", {"fact": "One Fact"})

        self.assertIsInstance(hash(step), int)

    def test_plan_is_hashable(self) -> None:
        plan = Plan("memories", (PlanStep("memory", "list", {}),))

        self.assertIsInstance(hash(plan), int)

    def test_structurally_equal_plan_steps_have_equal_hashes(self) -> None:
        first = PlanStep("notes", "retitle", {"id": 5, "title": "New Title"})
        second = PlanStep("notes", "retitle", {"id": 5, "title": "New Title"})

        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertEqual(hash(first), hash(second))

    def test_structurally_equal_plans_have_equal_hashes(self) -> None:
        first = Plan("forget memory 5", (PlanStep("memory", "forget", {"id": 5}),))
        second = Plan("forget memory 5", (PlanStep("memory", "forget", {"id": 5}),))

        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertEqual(hash(first), hash(second))

    def test_hash_uses_structure_rather_than_object_identity(self) -> None:
        plan = Plan("memories", (PlanStep("memory", "list", {}),))
        equal_plan = Plan("memories", (PlanStep("memory", "list", {}),))

        self.assertEqual(len({plan, equal_plan}), 1)

    def test_reversed_step_order_produces_unequal_hashable_plans(self) -> None:
        first = PlanStep("memory", "list", {})
        second = PlanStep("system", "overview", {})
        forward = Plan("request", (first, second))
        reversed_plan = Plan("request", (second, first))

        self.assertNotEqual(forward, reversed_plan)
        self.assertIsInstance(hash(forward), int)
        self.assertIsInstance(hash(reversed_plan), int)
        self.assertEqual(hash(forward), hash(Plan("request", (first, second))))

    def test_argument_insertion_order_does_not_change_hashes(self) -> None:
        first = PlanStep("notes", "create", {"title": "T", "body": "B"})
        second = PlanStep("notes", "create", {"body": "B", "title": "T"})

        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))
        self.assertEqual(
            hash(Plan("create note T: B", (first,))),
            hash(Plan("create note T: B", (second,))),
        )


class PlanFingerprintTests(unittest.TestCase):
    """Verify the stable plan fingerprint contract."""

    def test_fingerprint_is_lowercase_sha256_hexadecimal(self) -> None:
        fingerprint = Plan("memories", (PlanStep("memory", "list", {}),)).fingerprint

        self.assertEqual(len(fingerprint), 64)
        self.assertEqual(fingerprint, fingerprint.lower())
        self.assertTrue(set(fingerprint) <= set("0123456789abcdef"))

    def test_structurally_equal_plans_share_one_fingerprint(self) -> None:
        arguments = {"fact": "My Fact"}
        first = Plan("remember My Fact", (PlanStep("memory", "store", arguments),))
        second = Plan("remember My Fact", (PlanStep("memory", "store", arguments),))

        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_repeated_reads_return_the_same_fingerprint(self) -> None:
        plan = Plan("memories", (PlanStep("memory", "list", {}),))

        self.assertEqual(plan.fingerprint, plan.fingerprint)

    def test_fingerprint_matches_its_recorded_canonical_value(self) -> None:
        no_action = Plan("help", (), "Supported commands")
        single_step = Plan(
            "remember My Fact",
            (PlanStep("memory", "store", {"fact": "My Fact"}),),
        )

        self.assertEqual(no_action.fingerprint, NO_ACTION_FINGERPRINT)
        self.assertEqual(single_step.fingerprint, SINGLE_STEP_FINGERPRINT)

    def test_no_action_plan_fingerprint_is_deterministic(self) -> None:
        first = Plan("help", (), "Supported commands")
        second = Plan("help", (), "Supported commands")

        self.assertEqual(first.steps, ())
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_argument_insertion_order_does_not_change_the_fingerprint(self) -> None:
        first = PlanStep("notes", "create", {"title": "T", "body": "B"})
        second = PlanStep("notes", "create", {"body": "B", "title": "T"})

        self.assertEqual(
            Plan("create note T: B", (first,)).fingerprint,
            Plan("create note T: B", (second,)).fingerprint,
        )

    def test_step_order_changes_the_fingerprint(self) -> None:
        first = PlanStep("memory", "list", {})
        second = PlanStep("system", "overview", {})

        self.assertNotEqual(
            Plan("request", (first, second)).fingerprint,
            Plan("request", (second, first)).fingerprint,
        )

    def test_single_character_argument_change_changes_the_fingerprint(self) -> None:
        first = Plan("remember Fact", (PlanStep("memory", "store", {"fact": "Fact"}),))
        second = Plan("remember Fact", (PlanStep("memory", "store", {"fact": "Fadt"}),))

        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_original_request_participates_in_the_fingerprint(self) -> None:
        step = PlanStep("memory", "list", {})

        self.assertNotEqual(
            Plan("memories", (step,)).fingerprint,
            Plan("MEMORIES", (step,)).fingerprint,
        )

    def test_response_if_no_action_participates_in_the_fingerprint(self) -> None:
        self.assertNotEqual(
            Plan("help", (), "first response").fingerprint,
            Plan("help", (), "second response").fingerprint,
        )

    def test_absent_response_differs_from_an_empty_response(self) -> None:
        self.assertNotEqual(
            Plan("help", (), None).fingerprint,
            Plan("help", (), "").fingerprint,
        )

    def test_tool_name_and_action_participate_in_the_fingerprint(self) -> None:
        base = Plan("request", (PlanStep("notes", "list", {}),))

        self.assertNotEqual(
            base.fingerprint,
            Plan("request", (PlanStep("memory", "list", {}),)).fingerprint,
        )
        self.assertNotEqual(
            base.fingerprint,
            Plan("request", (PlanStep("notes", "read", {}),)).fingerprint,
        )

    def test_supported_value_types_stay_distinguishable(self) -> None:
        numeric = Plan("delete note 5", (PlanStep("notes", "delete", {"id": 5}),))
        textual = Plan("delete note 5", (PlanStep("notes", "delete", {"id": "5"}),))

        self.assertNotEqual(numeric.fingerprint, textual.fingerprint)

    def test_only_the_digest_is_publicly_exposed(self) -> None:
        plan = Plan("memories", (PlanStep("memory", "list", {}),))

        public_names = {name for name in dir(plan) if not name.startswith("_")}

        self.assertIn("fingerprint", public_names)
        self.assertEqual(
            {name for name in public_names if "canonical" in name or "payload" in name},
            set(),
        )


class PlanRecordingPolicyTests(unittest.TestCase):
    """Verify the plan-level recording policy as part of the value object."""

    def setUp(self) -> None:
        self.step = PlanStep("memory", "store", {"fact": "My Fact"})

    def test_the_policy_has_exactly_two_members(self) -> None:
        self.assertEqual(
            {member.name: member.value for member in RecordingPolicy},
            {"RECORD": "record", "TRANSIENT": "transient"},
        )

    def test_default_policy_is_record(self) -> None:
        for plan in (
            Plan("help", ()),
            Plan("help", (), "Supported commands"),
            Plan("remember My Fact", (self.step,)),
        ):
            with self.subTest(plan=plan.original_request):
                self.assertIs(plan.recording, RecordingPolicy.RECORD)

    def test_existing_positional_construction_is_unchanged(self) -> None:
        plan = Plan("remember My Fact", (self.step,), "unused response")

        self.assertEqual(plan.original_request, "remember My Fact")
        self.assertEqual(plan.steps, (self.step,))
        self.assertEqual(plan.response_if_no_action, "unused response")
        self.assertIs(plan.recording, RecordingPolicy.RECORD)
        self.assertIs(plan.origin, PlanOrigin.DETERMINISTIC)

    def test_explicit_record_is_indistinguishable_from_the_default(self) -> None:
        pairs = (
            (
                Plan("help", (), "Supported commands"),
                Plan("help", (), "Supported commands", RecordingPolicy.RECORD),
            ),
            (
                Plan("remember My Fact", (self.step,)),
                Plan(
                    "remember My Fact",
                    (self.step,),
                    recording=RecordingPolicy.RECORD,
                ),
            ),
        )

        for default, explicit in pairs:
            with self.subTest(plan=default.original_request):
                self.assertEqual(default, explicit)
                self.assertEqual(hash(default), hash(explicit))
                self.assertEqual(default.fingerprint, explicit.fingerprint)

    def test_default_plans_keep_their_recorded_golden_digests(self) -> None:
        self.assertEqual(
            Plan("help", (), "Supported commands", RecordingPolicy.RECORD).fingerprint,
            NO_ACTION_FINGERPRINT,
        )
        self.assertEqual(
            Plan(
                "remember My Fact",
                (self.step,),
                recording=RecordingPolicy.RECORD,
            ).fingerprint,
            SINGLE_STEP_FINGERPRINT,
        )

    def test_golden_digests_hash_the_written_out_canonical_bytes(self) -> None:
        for canonical, digest in (
            (NO_ACTION_CANONICAL, NO_ACTION_FINGERPRINT),
            (SINGLE_STEP_CANONICAL, SINGLE_STEP_FINGERPRINT),
            (TRANSIENT_NO_ACTION_CANONICAL, TRANSIENT_NO_ACTION_FINGERPRINT),
        ):
            with self.subTest(digest=digest):
                self.assertEqual(hashlib.sha256(canonical).hexdigest(), digest)

    def test_transient_fingerprint_matches_its_recorded_canonical_value(self) -> None:
        transient = Plan(
            "help",
            (),
            "Supported commands",
            recording=RecordingPolicy.TRANSIENT,
        )

        self.assertEqual(transient.fingerprint, TRANSIENT_NO_ACTION_FINGERPRINT)

    def test_transient_plans_differ_from_otherwise_identical_default_plans(
        self,
    ) -> None:
        pairs = (
            (
                Plan("help", (), "Supported commands"),
                Plan(
                    "help",
                    (),
                    "Supported commands",
                    recording=RecordingPolicy.TRANSIENT,
                ),
            ),
            (
                Plan("remember My Fact", (self.step,)),
                Plan(
                    "remember My Fact",
                    (self.step,),
                    recording=RecordingPolicy.TRANSIENT,
                ),
            ),
        )

        for default, transient in pairs:
            with self.subTest(plan=default.original_request):
                self.assertNotEqual(default, transient)
                self.assertNotEqual(default.fingerprint, transient.fingerprint)
                self.assertEqual(len({default, transient}), 2)

    def test_structurally_equal_transient_plans_share_one_fingerprint(self) -> None:
        first = Plan("help", (), "Supported commands", RecordingPolicy.TRANSIENT)
        second = Plan("help", (), "Supported commands", RecordingPolicy.TRANSIENT)

        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_values_that_are_not_exactly_a_policy_member_are_refused(self) -> None:
        spoof = _ClassSpoofingPolicy()
        refused: tuple[Any, ...] = (
            "record",
            "transient",
            "RECORD",
            "TRANSIENT",
            _TextSubclass("transient"),
            b"transient",
            True,
            False,
            None,
            0,
            1,
            RecordingPolicy,
            spoof,
        )

        # The spoof defeats an isinstance check, which is why the plan has to
        # check the exact type instead.
        self.assertIsInstance(spoof, RecordingPolicy)
        for value in refused:
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(TypeError) as raised:
                    Plan("help", (), "Supported commands", value)

                self.assertEqual(
                    str(raised.exception),
                    "Plan recording policy must be a RecordingPolicy.",
                )

    def test_rebuilding_a_plan_revalidates_its_policy(self) -> None:
        plan = Plan("help", (), "Supported commands")

        with self.assertRaises(TypeError):
            dataclasses.replace(plan, recording="transient")

        rebuilt = dataclasses.replace(plan, recording=RecordingPolicy.TRANSIENT)
        self.assertIs(rebuilt.recording, RecordingPolicy.TRANSIENT)
        self.assertIs(plan.recording, RecordingPolicy.RECORD)

    def test_the_policy_of_a_constructed_plan_cannot_be_reassigned(self) -> None:
        plan = Plan("help", (), "Supported commands")

        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.recording = RecordingPolicy.TRANSIENT

        self.assertIs(plan.recording, RecordingPolicy.RECORD)
        self.assertEqual(plan.fingerprint, NO_ACTION_FINGERPRINT)


class PlanOriginTests(unittest.TestCase):
    """Verify the plan-level origin as part of the value object."""

    def setUp(self) -> None:
        self.step = PlanStep("calculator", "calculate", {"expression": "2 + 2"})

    def _model_plan(self) -> Plan:
        """Return the one shape a model-originated plan may take."""
        return Plan(
            "calculate 2 + 2",
            (self.step,),
            recording=RecordingPolicy.TRANSIENT,
            origin=PlanOrigin.MODEL,
        )

    def test_the_origin_has_exactly_two_members(self) -> None:
        self.assertEqual(
            {member.name: member.value for member in PlanOrigin},
            {"DETERMINISTIC": "deterministic", "MODEL": "model"},
        )

    def test_the_origin_is_the_last_field_after_the_recording_policy(self) -> None:
        self.assertEqual(
            [field.name for field in dataclasses.fields(Plan)],
            [
                "original_request",
                "steps",
                "response_if_no_action",
                "recording",
                "origin",
            ],
        )

        plan = Plan(
            "calculate 2 + 2",
            (self.step,),
            None,
            RecordingPolicy.TRANSIENT,
            PlanOrigin.MODEL,
        )

        self.assertIs(plan.origin, PlanOrigin.MODEL)
        self.assertEqual(plan, self._model_plan())

    def test_default_origin_is_deterministic(self) -> None:
        for plan in (
            Plan("help", ()),
            Plan("help", (), "Supported commands"),
            Plan("help", (), "Supported commands", RecordingPolicy.TRANSIENT),
            Plan("calculate 2 + 2", (self.step,)),
            Plan("calculate 2 + 2", (self.step,), recording=RecordingPolicy.TRANSIENT),
        ):
            with self.subTest(plan=plan.original_request, policy=plan.recording):
                self.assertIs(plan.origin, PlanOrigin.DETERMINISTIC)

    def test_explicit_deterministic_is_indistinguishable_from_the_default(
        self,
    ) -> None:
        pairs = (
            (
                Plan("help", (), "Supported commands"),
                Plan(
                    "help",
                    (),
                    "Supported commands",
                    origin=PlanOrigin.DETERMINISTIC,
                ),
            ),
            (
                Plan("calculate 2 + 2", (self.step,)),
                Plan("calculate 2 + 2", (self.step,), origin=PlanOrigin.DETERMINISTIC),
            ),
            (
                Plan("help", (), "Supported commands", RecordingPolicy.TRANSIENT),
                Plan(
                    "help",
                    (),
                    "Supported commands",
                    RecordingPolicy.TRANSIENT,
                    PlanOrigin.DETERMINISTIC,
                ),
            ),
        )

        for default, explicit in pairs:
            with self.subTest(plan=default.original_request, policy=default.recording):
                self.assertEqual(default, explicit)
                self.assertEqual(hash(default), hash(explicit))
                self.assertEqual(default.fingerprint, explicit.fingerprint)

    def test_every_existing_golden_digest_is_unchanged_by_the_origin(self) -> None:
        # The origin joins the canonical bytes only when it is not the
        # default, so a deterministic plan hashes exactly the bytes written
        # out above, byte for byte, whether its origin is explicit or not.
        cases = (
            (
                Plan(
                    "help",
                    (),
                    "Supported commands",
                    origin=PlanOrigin.DETERMINISTIC,
                ),
                NO_ACTION_CANONICAL,
                NO_ACTION_FINGERPRINT,
            ),
            (
                Plan(
                    "remember My Fact",
                    (PlanStep("memory", "store", {"fact": "My Fact"}),),
                    origin=PlanOrigin.DETERMINISTIC,
                ),
                SINGLE_STEP_CANONICAL,
                SINGLE_STEP_FINGERPRINT,
            ),
            (
                Plan(
                    "help",
                    (),
                    "Supported commands",
                    RecordingPolicy.TRANSIENT,
                    PlanOrigin.DETERMINISTIC,
                ),
                TRANSIENT_NO_ACTION_CANONICAL,
                TRANSIENT_NO_ACTION_FINGERPRINT,
            ),
        )

        for plan, canonical, digest in cases:
            with self.subTest(digest=digest):
                self.assertNotIn(b'"origin"', canonical)
                self.assertEqual(hashlib.sha256(canonical).hexdigest(), digest)
                self.assertEqual(plan.fingerprint, digest)

    def test_model_fingerprint_matches_its_recorded_canonical_value(self) -> None:
        self.assertEqual(
            hashlib.sha256(MODEL_STEP_CANONICAL).hexdigest(),
            MODEL_STEP_FINGERPRINT,
        )
        self.assertEqual(self._model_plan().fingerprint, MODEL_STEP_FINGERPRINT)

    def test_model_plans_differ_from_otherwise_identical_deterministic_plans(
        self,
    ) -> None:
        model = self._model_plan()
        deterministic = Plan(
            "calculate 2 + 2",
            (self.step,),
            recording=RecordingPolicy.TRANSIENT,
        )

        self.assertNotEqual(model, deterministic)
        self.assertNotEqual(model.fingerprint, deterministic.fingerprint)
        self.assertEqual(len({model, deterministic}), 2)

    def test_structurally_equal_model_plans_share_one_fingerprint(self) -> None:
        first = self._model_plan()
        second = self._model_plan()

        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertEqual(hash(first), hash(second))
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_values_that_are_not_exactly_an_origin_member_are_refused(self) -> None:
        spoof = _ClassSpoofingOrigin()
        refused: tuple[Any, ...] = (
            "deterministic",
            "model",
            "DETERMINISTIC",
            "MODEL",
            _TextSubclass("model"),
            b"model",
            True,
            False,
            None,
            0,
            1,
            PlanOrigin,
            RecordingPolicy.RECORD,
            RecordingPolicy.TRANSIENT,
            spoof,
        )

        # The spoof defeats an isinstance check, which is why the plan has to
        # check the exact type instead.
        self.assertIsInstance(spoof, PlanOrigin)
        for value in refused:
            for policy in RecordingPolicy:
                with self.subTest(value=type(value).__name__, policy=policy):
                    with self.assertRaises(TypeError) as raised:
                        Plan("calculate 2 + 2", (self.step,), None, policy, value)

                    self.assertEqual(
                        str(raised.exception),
                        "Plan origin must be a PlanOrigin.",
                    )

    def test_a_model_plan_of_any_other_shape_is_refused(self) -> None:
        other = PlanStep("system", "current_time", {})
        shapes: dict[str, dict[str, Any]] = {
            "recorded": {"steps": (self.step,), "recording": RecordingPolicy.RECORD},
            "no step": {"steps": ()},
            # V1-19 admits two steps; every step is still held to the shape.
            "two steps, the second a step subclass": {
                "steps": (
                    other,
                    _PlanStepSubclass("calculator", "calculate", {"expression": "1"}),
                ),
            },
            "a canned response": {
                "steps": (self.step,),
                "response_if_no_action": "Supported commands",
            },
            "an empty canned response": {
                "steps": (self.step,),
                "response_if_no_action": "",
            },
            "a canned response and no step": {
                "steps": (),
                "response_if_no_action": "Supported commands",
            },
            "steps in a list": {"steps": [self.step]},
            "a step subclass": {
                "steps": (
                    _PlanStepSubclass("calculator", "calculate", {"expression": "1"}),
                ),
            },
            "a tool name subclass": {
                "steps": (
                    PlanStep(
                        _TextSubclass("calculator"),
                        "calculate",
                        {"expression": "1"},
                    ),
                ),
            },
            "an action subclass": {
                "steps": (
                    PlanStep(
                        "calculator",
                        _TextSubclass("calculate"),
                        {"expression": "1"},
                    ),
                ),
            },
        }

        self.assertEqual(
            MODEL_PLAN_SHAPE_MESSAGE,
            "A model-originated plan must be transient, with 1 to 5 steps and "
            "no canned response.",
        )
        for shape, fields in shapes.items():
            arguments = {"recording": RecordingPolicy.TRANSIENT, **fields}
            with self.subTest(shape=shape):
                with self.assertRaises(ValueError) as raised:
                    Plan("calculate 2 + 2", origin=PlanOrigin.MODEL, **arguments)

                self.assertEqual(str(raised.exception), MODEL_PLAN_SHAPE_MESSAGE)

                # The same shape with the default origin is an ordinary plan.
                self.assertIs(
                    Plan("calculate 2 + 2", **arguments).origin,
                    PlanOrigin.DETERMINISTIC,
                )

        # Six steps are refused with either origin, each with its own message.
        six = {"steps": (self.step,) * 6, "recording": RecordingPolicy.TRANSIENT}
        with self.assertRaises(ValueError) as raised:
            Plan("calculate 2 + 2", origin=PlanOrigin.MODEL, **six)
        self.assertEqual(str(raised.exception), MODEL_PLAN_SHAPE_MESSAGE)
        with self.assertRaises(ValueError) as raised:
            Plan("calculate 2 + 2", **six)
        self.assertEqual(str(raised.exception), PLAN_STEP_LIMIT_MESSAGE)
        self.assertEqual(PLAN_STEP_LIMIT_MESSAGE, "A plan may hold at most 5 steps.")

    def test_a_plan_subclass_can_never_carry_the_model_origin(self) -> None:
        with self.assertRaises(ValueError) as raised:
            _PlanSubclass(
                "calculate 2 + 2",
                (self.step,),
                recording=RecordingPolicy.TRANSIENT,
                origin=PlanOrigin.MODEL,
            )

        self.assertEqual(str(raised.exception), MODEL_PLAN_SHAPE_MESSAGE)

    def test_rebuilding_a_plan_revalidates_its_origin_and_shape(self) -> None:
        plan = Plan("calculate 2 + 2", (self.step,))
        model = self._model_plan()

        with self.assertRaises(TypeError):
            dataclasses.replace(plan, origin="model")
        refused: tuple[tuple[Plan, dict[str, Any]], ...] = (
            (plan, {"origin": PlanOrigin.MODEL}),
            (model, {"recording": RecordingPolicy.RECORD}),
            (model, {"steps": ()}),
            (model, {"steps": (self.step,) * 6}),
            (model, {"response_if_no_action": "reply"}),
        )
        for source, changes in refused:
            with self.subTest(origin=source.origin, changes=sorted(changes)):
                with self.assertRaises(ValueError) as raised:
                    dataclasses.replace(source, **changes)

                self.assertEqual(str(raised.exception), MODEL_PLAN_SHAPE_MESSAGE)

        # Since V1-19 two steps rebuild into a model plan bound to both.
        widened = dataclasses.replace(model, steps=(self.step, self.step))
        self.assertTrue(is_model_plan(widened))
        self.assertNotEqual(widened.fingerprint, model.fingerprint)

        rebuilt = dataclasses.replace(
            plan,
            recording=RecordingPolicy.TRANSIENT,
            origin=PlanOrigin.MODEL,
        )
        self.assertEqual(rebuilt, model)
        self.assertIs(plan.origin, PlanOrigin.DETERMINISTIC)
        self.assertIs(
            dataclasses.replace(model, origin=PlanOrigin.DETERMINISTIC).origin,
            PlanOrigin.DETERMINISTIC,
        )

    def test_the_origin_of_a_constructed_plan_cannot_be_reassigned(self) -> None:
        plan = Plan("help", (), "Supported commands")

        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.origin = PlanOrigin.MODEL

        self.assertIs(plan.origin, PlanOrigin.DETERMINISTIC)
        self.assertEqual(plan.fingerprint, NO_ACTION_FINGERPRINT)

    def test_only_the_exact_model_shape_is_a_model_plan(self) -> None:
        self.assertTrue(is_model_plan(self._model_plan()))
        for value in (
            Plan("calculate 2 + 2", (self.step,)),
            Plan("calculate 2 + 2", (self.step,), recording=RecordingPolicy.TRANSIENT),
            Plan("help", (), "Supported commands"),
            self._model_plan().steps,
            self.step,
            None,
            "calculate 2 + 2",
        ):
            with self.subTest(value=type(value).__name__):
                self.assertFalse(is_model_plan(value))

    def test_a_model_plan_altered_after_construction_is_no_longer_one(self) -> None:
        # A frozen dataclass can still be altered through object.__setattr__,
        # which is why every field is read again rather than trusted.
        alterations: tuple[tuple[str, Any], ...] = (
            ("origin", PlanOrigin.DETERMINISTIC),
            ("origin", "model"),
            ("recording", RecordingPolicy.RECORD),
            # Equal to the member, as a str enum member is, yet not it.
            ("recording", "transient"),
            ("response_if_no_action", ""),
            ("steps", ()),
            ("steps", (self.step,) * 6),
            ("steps", (self.step, "calculator.calculate")),
            ("steps", [self.step]),
        )

        for name, value in alterations:
            with self.subTest(field=name, value=value):
                plan = self._model_plan()
                self.assertTrue(is_model_plan(plan))

                object.__setattr__(plan, name, value)

                self.assertFalse(is_model_plan(plan))

    def test_a_model_plan_whose_step_is_altered_is_no_longer_one(self) -> None:
        # The step is read again too. None of these alterations changes a
        # byte the fingerprint covers, so only the exact-type checks on the
        # step's names and arguments can tell the plan is no longer one.
        alterations: tuple[tuple[str, Any], ...] = (
            ("tool_name", _TextSubclass("calculator")),
            ("action", _TextSubclass("calculate")),
            ("arguments", {"expression": "2 + 2"}),
            ("arguments", MappingProxyType({"expression": "2 + 2"})),
            ("arguments", _MutablePlanArguments({"expression": "2 + 2"})),
        )

        for name, value in alterations:
            with self.subTest(field=name, value=type(value).__name__):
                # A fresh step each time, so no alteration outlives its case.
                step = PlanStep("calculator", "calculate", {"expression": "2 + 2"})
                plan = Plan(
                    "calculate 2 + 2",
                    (step,),
                    recording=RecordingPolicy.TRANSIENT,
                    origin=PlanOrigin.MODEL,
                )
                self.assertTrue(is_model_plan(plan))

                object.__setattr__(step, name, value)

                self.assertEqual(plan.fingerprint, MODEL_STEP_FINGERPRINT)
                self.assertFalse(is_model_plan(plan))


class PlanStepImmutabilityTests(unittest.TestCase):
    """Verify plan-step arguments are a deeply immutable snapshot."""

    def setUp(self) -> None:
        self.step = PlanStep("memory", "store", {"fact": "Original Fact"})

    def test_arguments_are_normalized_to_an_immutable_mapping(self) -> None:
        self.assertIsInstance(self.step.arguments, PlanArguments)

    def test_item_assignment_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            self.step.arguments["fact"] = "Replaced"

        self.assertEqual(self.step.arguments, {"fact": "Original Fact"})

    def test_new_key_assignment_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            self.step.arguments["injected"] = "Injected"

        self.assertEqual(self.step.arguments, {"fact": "Original Fact"})

    def test_item_deletion_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            del self.step.arguments["fact"]

        self.assertEqual(self.step.arguments, {"fact": "Original Fact"})

    def test_mutating_mapping_methods_are_absent(self) -> None:
        for method_name in ("update", "pop", "popitem", "clear", "setdefault"):
            with self.subTest(method=method_name):
                self.assertFalse(hasattr(self.step.arguments, method_name))

    def test_internal_state_cannot_be_rebound(self) -> None:
        with self.assertRaises(AttributeError):
            self.step.arguments._values = {"fact": "Replaced"}

        self.assertEqual(self.step.arguments, {"fact": "Original Fact"})

    def test_arguments_field_cannot_be_replaced_on_the_step(self) -> None:
        with self.assertRaises(AttributeError):
            self.step.arguments = {"fact": "Replaced"}

        self.assertEqual(self.step.arguments, {"fact": "Original Fact"})

    def test_later_mutation_of_constructor_input_does_not_alter_the_step(self) -> None:
        supplied = {"fact": "Original Fact"}
        step = PlanStep("memory", "store", supplied)
        original_fingerprint = Plan("remember", (step,)).fingerprint

        supplied["fact"] = "Mutated Fact"
        supplied["injected"] = "Injected"
        supplied.clear()

        self.assertEqual(step.arguments, {"fact": "Original Fact"})
        self.assertEqual(Plan("remember", (step,)).fingerprint, original_fingerprint)

    def test_the_original_input_object_is_never_stored(self) -> None:
        supplied = {"fact": "Original Fact"}
        step = PlanStep("memory", "store", supplied)

        self.assertIsNot(step.arguments, supplied)
        self.assertIsNot(dict(step.arguments), supplied)

    def test_converting_to_a_dict_yields_a_detached_copy(self) -> None:
        detached = dict(self.step.arguments)

        detached["fact"] = "Replaced"
        detached["injected"] = "Injected"

        self.assertEqual(self.step.arguments, {"fact": "Original Fact"})

    def test_steps_built_from_one_mapping_stay_independent(self) -> None:
        shared = {"fact": "Original Fact"}
        first = PlanStep("memory", "store", shared)
        second = PlanStep("memory", "store", shared)

        shared["fact"] = "Mutated Fact"

        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))
        self.assertEqual(first.arguments, {"fact": "Original Fact"})


class PlanArgumentsInternalStorageTests(unittest.TestCase):
    """Verify the internal argument storage itself cannot be mutated.

    A private snapshot that is merely unreachable by assignment is not
    enough: reading the attribute must not hand back a mutable object,
    because content mutated that way would leave the hash computed at
    construction stale while still changing the plan fingerprint.
    """

    def setUp(self) -> None:
        self.source = {"secret": "before"}
        self.step = PlanStep(
            tool_name="example",
            action="inspect",
            arguments=self.source,
        )
        self.plan = Plan(
            original_request="probe request",
            steps=(self.step,),
            response_if_no_action="probe response",
        )
        self.before_mapping = dict(self.step.arguments)
        self.before_arguments_hash = hash(self.step.arguments)
        self.before_step_hash = hash(self.step)
        self.before_plan_hash = hash(self.plan)
        self.before_fingerprint = self.plan.fingerprint

    def assert_nothing_changed(self) -> None:
        """Assert every derived value still matches its before-attempt value."""
        self.assertEqual(dict(self.step.arguments), self.before_mapping)
        self.assertEqual(hash(self.step.arguments), self.before_arguments_hash)
        self.assertEqual(hash(self.step), self.before_step_hash)
        self.assertEqual(hash(self.plan), self.before_plan_hash)
        self.assertEqual(self.plan.fingerprint, self.before_fingerprint)

    def test_internal_storage_is_not_a_mutable_dictionary(self) -> None:
        internal_values = self.step.arguments._values

        self.assertNotIsInstance(internal_values, dict)
        self.assertIsInstance(internal_values, MappingProxyType)

    def test_internal_storage_item_assignment_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            self.step.arguments._values["secret"] = "after"

        self.assert_nothing_changed()

    def test_internal_storage_rejects_every_mutating_route(self) -> None:
        internal_values = self.step.arguments._values
        routes = (
            ("setitem", lambda: internal_values.__setitem__("secret", "after")),
            ("new key", lambda: internal_values.__setitem__("added", "after")),
            ("delitem", lambda: internal_values.__delitem__("secret")),
            ("update", lambda: internal_values.update({"secret": "after"})),
            ("pop", lambda: internal_values.pop("secret")),
            ("popitem", lambda: internal_values.popitem()),
            ("clear", lambda: internal_values.clear()),
            ("setdefault", lambda: internal_values.setdefault("added", "after")),
        )

        for name, route in routes:
            with self.subTest(route=name):
                with self.assertRaises((TypeError, AttributeError)):
                    route()

        self.assert_nothing_changed()

    def test_no_attribute_exposes_a_mutable_dictionary(self) -> None:
        arguments = self.step.arguments

        for name in type(arguments).__slots__:
            with self.subTest(attribute=name):
                self.assertNotIsInstance(getattr(arguments, name), dict)

    def test_mutating_the_constructor_input_afterwards_changes_nothing(self) -> None:
        self.source["secret"] = "after"
        self.source["added"] = "after"
        self.source.clear()

        self.assert_nothing_changed()

    def test_mutating_a_detached_dictionary_changes_nothing(self) -> None:
        detached = dict(self.step.arguments)

        detached["secret"] = "after"
        detached["added"] = "after"
        detached.clear()

        self.assert_nothing_changed()

    def test_read_access_and_equality_survive_the_correction(self) -> None:
        arguments = self.step.arguments

        self.assertEqual(arguments["secret"], "before")
        self.assertEqual(arguments.get("secret"), "before")
        self.assertEqual(arguments.get("absent", "fallback"), "fallback")
        self.assertIn("secret", arguments)
        self.assertEqual(len(arguments), 1)
        self.assertEqual(list(arguments), ["secret"])
        self.assertEqual(list(arguments.items()), [("secret", "before")])
        self.assertEqual(arguments, {"secret": "before"})
        self.assertEqual({"secret": "before"}, arguments)
        self.assertEqual(
            self.step,
            PlanStep("example", "inspect", {"secret": "before"}),
        )
        self.assertEqual(
            self.plan,
            Plan("probe request", (self.step,), "probe response"),
        )


class PlanStepSnapshotNormalizationTests(unittest.TestCase):
    """Verify a step always rebuilds its own exact arguments snapshot.

    Satisfying isinstance(value, PlanArguments) is not proof of
    immutability: a subclass can keep the Mapping interface while backing
    it with mutable state, so a step that reused the supplied object would
    still be changeable by its caller.
    """

    def setUp(self) -> None:
        self.backing = {"secret": "before"}
        self.source = _MutablePlanArguments(self.backing)
        self.step = PlanStep(
            tool_name="example",
            action="inspect",
            arguments=self.source,
        )
        self.plan = Plan(
            original_request="probe request",
            steps=(self.step,),
            response_if_no_action="probe response",
        )
        self.before_mapping = dict(self.step.arguments)
        self.before_arguments_hash = hash(self.step.arguments)
        self.before_step_hash = hash(self.step)
        self.before_plan_hash = hash(self.plan)
        self.before_fingerprint = self.plan.fingerprint

    def test_fixture_passes_the_isinstance_check_it_subverts(self) -> None:
        self.assertIsInstance(self.source, PlanArguments)
        self.assertIsNot(type(self.source), PlanArguments)

    def test_step_does_not_retain_the_supplied_subclass(self) -> None:
        self.assertIsNot(self.step.arguments, self.source)

    def test_step_holds_an_exact_plan_arguments_snapshot(self) -> None:
        self.assertIs(type(self.step.arguments), PlanArguments)
        self.assertEqual(self.step.arguments, {"secret": "before"})

    def test_mutating_the_subclass_backing_mapping_changes_nothing(self) -> None:
        self.backing["secret"] = "after"
        self.backing["added"] = "after"
        self.backing.clear()

        self.assertEqual(dict(self.step.arguments), self.before_mapping)
        self.assertEqual(hash(self.step.arguments), self.before_arguments_hash)
        self.assertEqual(hash(self.step), self.before_step_hash)
        self.assertEqual(hash(self.plan), self.before_plan_hash)
        self.assertEqual(self.plan.fingerprint, self.before_fingerprint)

    def test_a_plain_plan_arguments_input_is_also_re_snapshotted(self) -> None:
        original = PlanStep("notes", "delete", {"id": 5}).arguments

        rebuilt = PlanStep("notes", "delete", original).arguments

        self.assertIsNot(rebuilt, original)
        self.assertIs(type(rebuilt), PlanArguments)
        self.assertEqual(rebuilt, original)
        self.assertEqual(hash(rebuilt), hash(original))

    def test_subclass_cannot_smuggle_an_unsupported_value(self) -> None:
        smuggled = _MutablePlanArguments({"payload": ["unsupported"]})

        with self.assertRaises(TypeError):
            PlanStep("notes", "create", smuggled)


class PlanArgumentContainerTests(unittest.TestCase):
    """Verify only a Mapping may supply plan arguments."""

    def test_non_mapping_containers_are_rejected(self) -> None:
        for container in (None, [], (), "not a mapping", ["key"], 42, set()):
            with self.subTest(container=type(container).__name__):
                with self.assertRaises(TypeError) as raised:
                    PlanStep("notes", "create", container)

                self.assertEqual(
                    str(raised.exception),
                    "Plan arguments must be a mapping.",
                )

    def test_rejection_message_leaks_no_supplied_content(self) -> None:
        with self.assertRaises(TypeError) as raised:
            PlanStep("notes", "create", [SENTINEL_FACT])

        message = str(raised.exception)

        self.assertEqual(message, "Plan arguments must be a mapping.")
        self.assertNotIn(SENTINEL_FACT, message)
        self.assertNotIn("0x", message)

    def test_an_empty_mapping_remains_the_way_to_build_no_arguments(self) -> None:
        step = PlanStep("memory", "list", {})

        self.assertEqual(step.arguments, {})
        self.assertEqual(len(step.arguments), 0)

    def test_other_mapping_types_are_accepted_and_snapshotted(self) -> None:
        proxy = MappingProxyType({"fact": "One Fact"})

        step = PlanStep("memory", "store", proxy)

        self.assertIs(type(step.arguments), PlanArguments)
        self.assertEqual(step.arguments, {"fact": "One Fact"})


class PlanArgumentDomainTests(unittest.TestCase):
    """Verify the supported argument-value domain and its refusals."""

    def test_supported_values_are_strings_and_integers(self) -> None:
        step = PlanStep("memory", "update", {"id": 5, "fact": "New Fact"})

        self.assertEqual(step.arguments, {"id": 5, "fact": "New Fact"})

    def test_nested_mutable_values_are_refused_at_construction(self) -> None:
        nested_values: tuple[Any, ...] = (
            {"nested": "value"},
            ["nested"],
            {"nested"},
            bytearray(b"nested"),
        )

        for value in nested_values:
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(TypeError):
                    PlanStep("notes", "create", {"payload": value})

    def test_unsupported_scalar_values_are_refused_at_construction(self) -> None:
        for value in (1.5, True, None, object(), (1, 2), b"bytes"):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(TypeError):
                    PlanStep("notes", "create", {"payload": value})

    def test_non_string_keys_are_refused_at_construction(self) -> None:
        for key in (1, None, ("tuple",)):
            with self.subTest(key=type(key).__name__):
                with self.assertRaises(TypeError):
                    PlanStep("notes", "create", {key: "value"})

    def test_refusal_names_the_key_and_type_without_the_value(self) -> None:
        with self.assertRaises(TypeError) as raised:
            PlanStep("notes", "create", {"body": [SENTINEL_FACT]})

        message = str(raised.exception)

        self.assertIn("body", message)
        self.assertIn("list", message)
        self.assertNotIn(SENTINEL_FACT, message)


class PlanArgumentsMappingApiTests(unittest.TestCase):
    """Verify the read-only mapping API that tools and dispatch consume."""

    def setUp(self) -> None:
        self.arguments = PlanStep(
            "notes",
            "retitle",
            {"id": 5, "title": "New Title"},
        ).arguments

    def test_key_lookup_matches_the_supplied_mapping(self) -> None:
        self.assertEqual(self.arguments["id"], 5)
        self.assertEqual(self.arguments["title"], "New Title")

    def test_missing_key_lookup_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.arguments["absent"]

    def test_get_returns_values_and_defaults(self) -> None:
        self.assertEqual(self.arguments.get("title"), "New Title")
        self.assertIsNone(self.arguments.get("absent"))
        self.assertEqual(self.arguments.get("absent", "fallback"), "fallback")

    def test_membership_length_and_iteration_are_supported(self) -> None:
        self.assertIn("id", self.arguments)
        self.assertNotIn("absent", self.arguments)
        self.assertEqual(len(self.arguments), 2)
        self.assertEqual(sorted(self.arguments), ["id", "title"])
        self.assertEqual(sorted(self.arguments.keys()), ["id", "title"])
        self.assertEqual(
            sorted(self.arguments.items()),
            [("id", 5), ("title", "New Title")],
        )

    def test_mapping_equality_with_a_plain_dictionary_is_symmetric(self) -> None:
        expected = {"id": 5, "title": "New Title"}

        self.assertEqual(self.arguments, expected)
        self.assertEqual(expected, self.arguments)
        self.assertNotEqual(self.arguments, {"id": 5})

    def test_empty_arguments_compare_equal_to_an_empty_dictionary(self) -> None:
        empty = PlanStep("memory", "list", {}).arguments

        self.assertEqual(empty, {})
        self.assertEqual(len(empty), 0)
        self.assertEqual(hash(empty), hash(PlanArguments({})))

    def test_arguments_survive_a_round_trip_through_a_plain_dictionary(self) -> None:
        rebuilt = PlanStep("notes", "retitle", dict(self.arguments))

        self.assertEqual(rebuilt.arguments, self.arguments)
        self.assertEqual(hash(rebuilt.arguments), hash(self.arguments))


class PlannerBranchArgumentTests(unittest.TestCase):
    """Verify every deterministic planner branch still emits its arguments."""

    def setUp(self) -> None:
        logger, _ = _capturing_logger("tests.value_objects.branches")
        self.planner = Planner(logger=logger)

    def test_every_action_branch_emits_its_existing_argument_mapping(self) -> None:
        cases: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
            ("calculate 2 + 2", "calculator", "calculate", {"expression": "2 + 2"}),
            ("calc 2 + 2", "calculator", "calculate", {"expression": "2 + 2"}),
            ("remember My Fact", "memory", "store", {"fact": "My Fact"}),
            ("recall My Query", "memory", "search", {"query": "My Query"}),
            ("memories", "memory", "list", {}),
            (
                "update memory 5: New Fact",
                "memory",
                "update",
                {"id": 5, "fact": "New Fact"},
            ),
            ("forget memory 5", "memory", "forget", {"id": 5}),
            ("list reports", "filesystem", "list", {"path": "reports"}),
            ("ls reports", "filesystem", "list", {"path": "reports"}),
            ("read file notes.txt", "filesystem", "read", {"path": "notes.txt"}),
            (
                "rename a.txt to b.txt",
                "filesystem",
                "rename",
                {"source": "a.txt", "destination": "b.txt"},
            ),
            (
                "move a.txt to b.txt",
                "filesystem",
                "move",
                {"source": "a.txt", "destination": "b.txt"},
            ),
            ("create note T: B", "notes", "create", {"title": "T", "body": "B"}),
            ("note T: B", "notes", "create", {"title": "T", "body": "B"}),
            ("notes", "notes", "list", {}),
            # "list notes" matches the earlier "list " prefix branch, unchanged.
            ("list notes", "filesystem", "list", {"path": "notes"}),
            ("read note 3", "notes", "read", {"target": "3"}),
            (
                "retitle note 5: New Title",
                "notes",
                "retitle",
                {"id": 5, "title": "New Title"},
            ),
            ("edit note 5: New Body", "notes", "edit", {"id": 5, "body": "New Body"}),
            ("delete note 5", "notes", "delete", {"id": 5}),
            ("history", "history", "list", {}),
            ("search history trip", "history", "search", {"query": "trip"}),
            ("read interaction 42", "history", "read", {"id": 42}),
            ("delete interaction 42", "history", "delete", {"id": 42}),
            ("audit", "audit", "list", {}),
            ("read audit 3", "audit", "read", {"id": 3}),
            ("delete audit 3", "audit", "delete", {"id": 3}),
            ("system", "system", "overview", {}),
            ("time", "system", "current_time", {}),
        )

        for text, tool_name, action, expected_arguments in cases:
            with self.subTest(command=text):
                plan = self.planner.create_plan(text)

                self.assertEqual(len(plan.steps), 1)
                step = plan.steps[0]
                self.assertEqual(step.tool_name, tool_name)
                self.assertEqual(step.action, action)
                self.assertEqual(step.arguments, expected_arguments)
                self.assertIsInstance(step.arguments, PlanArguments)
                self.assertIsInstance(hash(plan), int)
                self.assertEqual(len(plan.fingerprint), 64)
                # The planner is deterministic code, so it never claims a
                # plan came from the model.
                self.assertIs(plan.origin, PlanOrigin.DETERMINISTIC)

    def test_no_action_branches_stay_hashable_and_fingerprintable(self) -> None:
        for text in ("help", "commands", "remember", "recall", "unsupported request"):
            with self.subTest(command=text):
                plan = self.planner.create_plan(text)

                self.assertEqual(plan.steps, ())
                self.assertIsInstance(hash(plan), int)
                self.assertEqual(len(plan.fingerprint), 64)
                self.assertIs(plan.origin, PlanOrigin.DETERMINISTIC)


class PlanningLogPrivacyTests(unittest.TestCase):
    """Verify planning never logs request or argument content."""

    def test_planning_logs_omit_request_and_argument_content(self) -> None:
        logger, records = _capturing_logger("tests.value_objects.privacy")
        planner = Planner(logger=logger)

        plan = planner.create_plan(f"remember {SENTINEL_REQUEST} {SENTINEL_FACT}")

        self.assertEqual(plan.steps[0].tool_name, "memory")
        logged = "\n".join(records)
        self.assertNotEqual(logged, "")
        self.assertNotIn(SENTINEL_REQUEST, logged)
        self.assertNotIn(SENTINEL_FACT, logged)
        self.assertNotIn("sentinel", logged)

    def test_fingerprinting_a_plan_emits_no_log_output(self) -> None:
        logger, records = _capturing_logger("tests.value_objects.fingerprint")
        planner = Planner(logger=logger)
        plan = planner.create_plan(f"remember {SENTINEL_FACT}")
        records.clear()

        fingerprint = plan.fingerprint

        self.assertEqual(records, [])
        self.assertNotIn(SENTINEL_FACT, fingerprint)


class _MutablePlanArguments(PlanArguments):
    """A PlanArguments subclass deliberately backed by mutable state.

    It satisfies isinstance(value, PlanArguments) while every Mapping
    method reads through to a caller-held dictionary, which is exactly the
    bypass PlanStep normalization has to defeat.
    """

    def __init__(self, backing: dict[str, Any]) -> None:
        object.__setattr__(self, "backing", backing)

    def __getitem__(self, key: str) -> Any:
        """Read through to the caller's mutable mapping."""
        return self.backing[key]

    def __iter__(self):
        """Iterate the caller's mutable mapping."""
        return iter(self.backing)

    def __len__(self) -> int:
        """Return the caller's current mapping length."""
        return len(self.backing)

    def __hash__(self) -> int:
        """Hash current backing content, so staleness would be visible."""
        return hash(frozenset(self.backing.items()))


class _ClassSpoofingPolicy:
    """An object that claims to be a RecordingPolicy through __class__."""

    @property
    def __class__(self) -> type:
        """Report RecordingPolicy, which is enough to satisfy isinstance."""
        return RecordingPolicy


class _ClassSpoofingOrigin:
    """An object that claims to be a PlanOrigin through __class__."""

    @property
    def __class__(self) -> type:
        """Report PlanOrigin, which is enough to satisfy isinstance."""
        return PlanOrigin


class _TextSubclass(str):
    """A str subclass spelling a policy value."""


class _PlanSubclass(Plan):
    """A plan subtype, which must never pass as an exact plan."""


class _PlanStepSubclass(PlanStep):
    """A plan-step subtype, which must never pass as an exact step."""


class _RecordingHandler(logging.Handler):
    """Collect rendered log output for privacy assertions."""

    def __init__(self, records: list[str]) -> None:
        super().__init__()
        self.records = records

    def emit(self, record: logging.LogRecord) -> None:
        """Store the rendered message together with its raw arguments."""
        self.records.append(f"{record.getMessage()} {record.args!r}")


def _capturing_logger(name: str) -> tuple[logging.Logger, list[str]]:
    """Return an isolated logger that records instead of writing a log file."""
    records: list[str] = []
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.addHandler(_RecordingHandler(records))
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, records


if __name__ == "__main__":
    unittest.main()
