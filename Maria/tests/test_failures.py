"""Focused regression tests for Maria's shared typed failures."""

from __future__ import annotations

import copy
import inspect
import pickle
import unittest
from enum import Enum

import core.failures as failures_module
from core.failures import Failure, FailureKind, TypedFailureError


# The exact owner-visible sentence. Declared as a literal here rather than
# read back from the module, so the test asserts the contract instead of
# agreeing with whatever the implementation currently says.
TOOL_EXECUTION_FAILED_MESSAGE = "Tool execution failed."

# Likewise for the schema refusal the owner sees at startup.
DATABASE_SCHEMA_TOO_NEW_MESSAGE = (
    "This database was written by a newer version of Maria and "
    "cannot be opened by this one."
)

# The reasoning kinds and their exact sentences, written out rather than read
# back from the module for the same reason.
REASONING_FAILURE_MESSAGES = {
    "reasoning_unavailable": "The local reasoning model is unavailable.",
    "reasoning_timeout": "The local reasoning model did not respond in time.",
    "reasoning_refused": "The local reasoning model refused the request.",
    "reasoning_malformed_output": (
        "The local reasoning model returned an unusable response."
    ),
    "reasoning_output_truncated": (
        "The local reasoning model returned an incomplete response."
    ),
    "reasoning_input_over_budget": (
        "The request is too large for the local reasoning model."
    ),
    "reasoning_output_over_budget": (
        "The local reasoning model's response was too large and was discarded."
    ),
}

# The exact representation, likewise written out rather than derived, so
# a change to what a failure shows in a log line has to be deliberate.
EXPECTED_REPR = (
    "Failure(kind=<FailureKind.TOOL_EXECUTION_FAILED: "
    "'tool_execution_failed'>)"
)

# A distinctive value that must never survive an attempt to force it in.
KIND_PAYLOAD_SENTINEL = "private-kind-payload-3d9f1a7c25"


class _LookalikeKind(str, Enum):
    """A foreign enum whose member value matches a real failure kind's."""

    TOOL_EXECUTION_FAILED = "tool_execution_failed"


class FailureTests(unittest.TestCase):
    """Verify the shared failure stays typed, fixed, immutable, and empty."""

    def test_tool_execution_failure_carries_the_fixed_owner_message(self) -> None:
        failure = Failure(FailureKind.TOOL_EXECUTION_FAILED)

        self.assertEqual(failure.message, TOOL_EXECUTION_FAILED_MESSAGE)
        self.assertEqual(failure.code, "tool_execution_failed")
        self.assertEqual(repr(failure), EXPECTED_REPR)

    def test_every_declared_kind_has_one_fixed_message(self) -> None:
        for kind in FailureKind:
            with self.subTest(kind=kind):
                message = Failure(kind).message

                self.assertTrue(message)
                self.assertNotIn("{", message)
                self.assertNotIn("%", message)

    def test_structurally_equal_failures_compare_and_hash_equal(self) -> None:
        first = Failure(FailureKind.TOOL_EXECUTION_FAILED)
        second = Failure(FailureKind.TOOL_EXECUTION_FAILED)

        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))
        self.assertEqual(repr(first), repr(second))

    def test_failure_refuses_attribute_assignment_and_deletion(self) -> None:
        failure = Failure(FailureKind.TOOL_EXECUTION_FAILED)

        with self.assertRaises(AttributeError):
            failure.kind = FailureKind.TOOL_EXECUTION_FAILED
        with self.assertRaises(AttributeError):
            failure.exception = RuntimeError("private detail")
        with self.assertRaises(AttributeError):
            del failure.kind

    def test_a_failure_has_no_instance_dictionary(self) -> None:
        failure = Failure(FailureKind.TOOL_EXECUTION_FAILED)

        self.assertEqual(Failure.__slots__, ())
        self.assertFalse(hasattr(failure, "__dict__"))
        with self.assertRaises(AttributeError):
            _ = failure.__dict__

    def test_an_undeclared_payload_cannot_be_forced_onto_a_failure(self) -> None:
        failure = Failure(FailureKind.TOOL_EXECUTION_FAILED)
        sentinel = "private-payload-sentinel-8c47d1b0f3"

        # Ordinary assignment is stopped by frozen, and the slot layout stops
        # the two routes that step around it.
        with self.assertRaises((AttributeError, TypeError)):
            failure.private_payload = sentinel
        with self.assertRaises((AttributeError, TypeError)):
            object.__setattr__(failure, "private_payload", sentinel)
        with self.assertRaises(AttributeError):
            failure.__dict__["private_payload"] = sentinel

        self.assertFalse(hasattr(failure, "private_payload"))
        rendered = f"{failure!r} {failure.message} {failure.code}"
        self.assertNotIn(sentinel, rendered)

    def test_the_kind_cannot_be_replaced_after_construction(self) -> None:
        failure = Failure(FailureKind.TOOL_EXECUTION_FAILED)
        original_repr = repr(failure)
        original_hash = hash(failure)
        carrier = RuntimeError(KIND_PAYLOAD_SENTINEL)

        # Ordinary assignment, and the reflective route that steps around a
        # frozen dataclass's own guard, must both fail.
        with self.assertRaises((AttributeError, TypeError)):
            failure.kind = carrier
        with self.assertRaises((AttributeError, TypeError)):
            object.__setattr__(failure, "kind", carrier)

        self.assertIs(failure.kind, FailureKind.TOOL_EXECUTION_FAILED)
        self.assertEqual(failure.code, "tool_execution_failed")
        self.assertEqual(failure.message, TOOL_EXECUTION_FAILED_MESSAGE)
        self.assertEqual(repr(failure), original_repr)
        self.assertEqual(repr(failure), EXPECTED_REPR)
        self.assertEqual(hash(failure), original_hash)
        self.assertEqual(failure, Failure(FailureKind.TOOL_EXECUTION_FAILED))
        self.assertNotIn(
            KIND_PAYLOAD_SENTINEL,
            f"{failure!r} {failure.message} {failure.code}",
        )

    def test_a_failure_stays_findable_after_every_rejected_mutation(self) -> None:
        failure = Failure(FailureKind.TOOL_EXECUTION_FAILED)
        registry = {failure: "recorded"}
        seen = {failure}
        carrier = RuntimeError(KIND_PAYLOAD_SENTINEL)

        for attempt in (
            lambda: setattr(failure, "kind", carrier),
            lambda: object.__setattr__(failure, "kind", carrier),
            lambda: object.__setattr__(
                failure, "private_payload", KIND_PAYLOAD_SENTINEL
            ),
            lambda: delattr(failure, "kind"),
        ):
            with self.subTest(attempt=attempt):
                with self.assertRaises((AttributeError, TypeError)):
                    attempt()

        rebuilt = Failure(FailureKind.TOOL_EXECUTION_FAILED)
        self.assertEqual(registry[rebuilt], "recorded")
        self.assertIn(rebuilt, seen)
        self.assertEqual(registry[failure], "recorded")
        self.assertIn(failure, seen)

    def test_the_inherited_tuple_builders_stay_validated(self) -> None:
        failure = Failure(FailureKind.TOOL_EXECUTION_FAILED)
        carrier = RuntimeError(KIND_PAYLOAD_SENTINEL)

        # A tuple-backed value object inherits builders that would otherwise
        # construct straight through tuple.__new__, skipping the kind check.
        with self.assertRaises(ValueError):
            Failure._make([carrier])
        with self.assertRaises(ValueError):
            failure._replace(kind=carrier)

        self.assertEqual(
            Failure._make([FailureKind.TOOL_EXECUTION_FAILED]),
            failure,
        )

    def test_copy_deepcopy_and_pickle_preserve_the_exact_value(self) -> None:
        failure = Failure(FailureKind.TOOL_EXECUTION_FAILED)
        restored = {
            "copy": copy.copy(failure),
            "deepcopy": copy.deepcopy(failure),
            "pickle": pickle.loads(pickle.dumps(failure)),
        }

        for label, clone in restored.items():
            with self.subTest(route=label):
                self.assertIs(type(clone), Failure)
                self.assertIs(clone.kind, FailureKind.TOOL_EXECUTION_FAILED)
                self.assertEqual(clone, failure)
                self.assertEqual(hash(clone), hash(failure))
                self.assertEqual(repr(clone), EXPECTED_REPR)
                self.assertEqual(clone.code, "tool_execution_failed")
                self.assertEqual(clone.message, TOOL_EXECUTION_FAILED_MESSAGE)

    def test_a_failure_cannot_be_subclassed(self) -> None:
        with self.assertRaises(TypeError) as caught:

            class _DerivedFailure(Failure):
                pass

        self.assertEqual(
            str(caught.exception),
            "Failure may not be subclassed; add a FailureKind member instead.",
        )

    def test_the_subclass_refusal_repeats_nothing_it_was_handed(self) -> None:
        body_sentinel = "private-subclass-body-2e6a90fd57"
        keyword_sentinel = "private-subclass-keyword-b41d7c8e03"

        with self.assertRaises(TypeError) as from_body:

            class _PayloadFailure(Failure):
                payload = body_sentinel

        with self.assertRaises(TypeError) as from_keyword:

            class _KeywordFailure(Failure, note=keyword_sentinel):
                pass

        for caught in (from_body, from_keyword):
            message = str(caught.exception)
            self.assertNotIn(body_sentinel, message)
            self.assertNotIn(keyword_sentinel, message)
            self.assertNotIn("Failure(", message)
            self.assertEqual(
                message,
                "Failure may not be subclassed; add a FailureKind member instead.",
            )

    def test_an_untyped_or_foreign_kind_is_refused(self) -> None:
        candidates = (
            "tool_execution_failed",
            None,
            0,
            _LookalikeKind.TOOL_EXECUTION_FAILED,
        )

        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError) as caught:
                    Failure(candidate)

                self.assertIn("FailureKind", str(caught.exception))

    def test_the_failure_holds_the_kind_and_nothing_else(self) -> None:
        self.assertEqual(Failure._fields, ("kind",))
        self.assertEqual(
            tuple(Failure(FailureKind.TOOL_EXECUTION_FAILED)),
            (FailureKind.TOOL_EXECUTION_FAILED,),
        )

    def test_no_exception_object_or_text_survives_into_the_failure(self) -> None:
        sentinel = "private-failure-detail-4a1c8ef920"
        try:
            raise RuntimeError(sentinel)
        except RuntimeError:
            failure = Failure(FailureKind.TOOL_EXECUTION_FAILED)

        rendered = f"{failure!r} {failure.message} {failure.code}"
        self.assertNotIn(sentinel, rendered)
        self.assertNotIn("RuntimeError", rendered)
        self.assertIn("TOOL_EXECUTION_FAILED", repr(failure))

    def test_constructing_a_failure_logs_nothing(self) -> None:
        with self.assertNoLogs("maria"):
            Failure(FailureKind.TOOL_EXECUTION_FAILED).message

    def test_module_imports_no_subsystem_that_could_carry_private_context(
        self,
    ) -> None:
        source = inspect.getsource(failures_module)

        self.assertNotRegex(
            source,
            r"(?m)^\s*(?:import|from)\s+"
            r"(?:core|tools|main|sqlite3|logging|tomllib|configparser"
            r"|urllib|http|socket|ssl|subprocess|os|pathlib)\b",
        )


class DatabaseSchemaFailureTests(unittest.TestCase):
    """Verify the schema refusal joins the vocabulary on the same terms."""

    def test_the_kind_carries_its_fixed_owner_message_and_stable_code(self) -> None:
        failure = Failure(FailureKind.DATABASE_SCHEMA_TOO_NEW)

        self.assertEqual(failure.message, DATABASE_SCHEMA_TOO_NEW_MESSAGE)
        self.assertEqual(failure.code, "database_schema_too_new")

    def test_the_message_names_no_path_version_or_record(self) -> None:
        message = Failure(FailureKind.DATABASE_SCHEMA_TOO_NEW).message

        for forbidden in ("{", "%", "/", "\\", ".db", "sqlite", "SELECT"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, message)

    def test_the_kinds_stay_distinct_and_closed(self) -> None:
        self.assertEqual(
            {kind.value for kind in FailureKind},
            {"tool_execution_failed", "database_schema_too_new"}
            | set(REASONING_FAILURE_MESSAGES),
        )
        self.assertNotEqual(
            Failure(FailureKind.DATABASE_SCHEMA_TOO_NEW),
            Failure(FailureKind.TOOL_EXECUTION_FAILED),
        )


class ReasoningFailureTests(unittest.TestCase):
    """Verify the reasoning kinds join the vocabulary on the same terms."""

    def test_each_kind_carries_its_fixed_message_and_stable_code(self) -> None:
        for code, message in REASONING_FAILURE_MESSAGES.items():
            with self.subTest(code=code):
                failure = Failure(FailureKind(code))

                self.assertEqual(failure.code, code)
                self.assertEqual(failure.message, message)

    def test_no_message_names_a_host_model_path_or_placeholder(self) -> None:
        for code in REASONING_FAILURE_MESSAGES:
            message = Failure(FailureKind(code)).message
            for forbidden in ("{", "%", "/", "\\", "http", "127.", "::"):
                with self.subTest(code=code, forbidden=forbidden):
                    self.assertNotIn(forbidden, message)

    def test_every_reasoning_kind_is_distinct(self) -> None:
        failures = {Failure(FailureKind(code)) for code in REASONING_FAILURE_MESSAGES}

        self.assertEqual(len(failures), len(REASONING_FAILURE_MESSAGES))

    def test_provider_text_never_survives_into_a_reasoning_failure(self) -> None:
        sentinel = "private-provider-detail-61d0b9e3f4"
        try:
            raise RuntimeError(sentinel)
        except RuntimeError:
            raised = TypedFailureError(Failure(FailureKind.REASONING_TIMEOUT))

        rendered = f"{raised!r} {raised} {raised.failure!r} {raised.failure.code}"
        self.assertNotIn(sentinel, rendered)
        self.assertNotIn("RuntimeError", rendered)


class TypedFailureErrorTests(unittest.TestCase):
    """Verify the raising carrier shows its kind and never a payload."""

    def test_it_renders_only_the_carried_kinds_fixed_message(self) -> None:
        raised = TypedFailureError(Failure(FailureKind.DATABASE_SCHEMA_TOO_NEW))

        self.assertEqual(str(raised), DATABASE_SCHEMA_TOO_NEW_MESSAGE)
        self.assertEqual(
            raised.failure,
            Failure(FailureKind.DATABASE_SCHEMA_TOO_NEW),
        )
        self.assertEqual(raised.args, (raised.failure,))
        self.assertIn("DATABASE_SCHEMA_TOO_NEW", repr(raised))

    def test_it_refuses_anything_that_is_not_a_failure(self) -> None:
        candidates = (
            "database_schema_too_new",
            FailureKind.DATABASE_SCHEMA_TOO_NEW,
            None,
            RuntimeError("private detail"),
        )

        for candidate in candidates:
            with self.subTest(candidate=type(candidate).__name__):
                with self.assertRaises(ValueError) as caught:
                    TypedFailureError(candidate)

                self.assertIn("Failure", str(caught.exception))

    def test_no_exception_text_survives_into_what_it_renders(self) -> None:
        sentinel = "private-schema-detail-77b1c4e802"
        try:
            raise sqlite_like_error(sentinel)
        except RuntimeError:
            raised = TypedFailureError(Failure(FailureKind.DATABASE_SCHEMA_TOO_NEW))

        rendered = f"{raised!r} {raised} {raised.failure.code}"
        self.assertNotIn(sentinel, rendered)
        self.assertNotIn("RuntimeError", rendered)

    def test_it_is_not_caught_by_handlers_written_for_operational_failures(
        self,
    ) -> None:
        raised = TypedFailureError(Failure(FailureKind.DATABASE_SCHEMA_TOO_NEW))

        self.assertIsInstance(raised, Exception)
        self.assertNotIsInstance(raised, RuntimeError)
        self.assertNotIsInstance(raised, ValueError)
        self.assertNotIsInstance(raised, OSError)

    def test_raising_one_logs_nothing(self) -> None:
        with self.assertNoLogs("maria"):
            with self.assertRaises(TypedFailureError):
                raise TypedFailureError(
                    Failure(FailureKind.DATABASE_SCHEMA_TOO_NEW)
                )


def sqlite_like_error(detail: str) -> RuntimeError:
    """Return an exception carrying a value that must never travel."""
    return RuntimeError(detail)


if __name__ == "__main__":
    unittest.main()
