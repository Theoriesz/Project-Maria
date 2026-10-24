"""Focused tests for Maria's reasoning interface and its adversarial fake.

These prove what the interface itself guarantees: bounded, context-free,
redacted values; strictly validated settings; and one boundary that returns
every provider outcome as either inert text or a typed failure. They do not
prove anything about plan compilation or tool execution: since V1-18 only
Brain hands a model's proposal attempt to the plan compiler, and that path
is proven where Brain and the compiler are tested.
"""

from __future__ import annotations

import ast
import json
import logging
import tempfile
import tracemalloc
import unittest
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import core.reasoning as reasoning_module
from core.action_contracts import BUILT_IN_ACTION_CONTRACTS, ContractFailure
from core.constants import PROJECT_ROOT
from core.failures import Failure, FailureKind, TypedFailureError
from core.logger import configure_logging
from core.reasoning import (
    DISABLED_REASONING,
    REASONING_DEADLINE_MESSAGE,
    REASONING_DISABLED_FIELDS_MESSAGE,
    REASONING_ENABLED_MESSAGE,
    REASONING_EXCHANGE_MESSAGE,
    REASONING_FAILURE_KINDS,
    REASONING_HISTORY_MESSAGE,
    REASONING_HOST_MESSAGE,
    REASONING_MAX_INPUT_MESSAGE,
    REASONING_MAX_OUTPUT_MESSAGE,
    REASONING_MAXIMUM_INPUT_BYTES,
    REASONING_MAXIMUM_OUTPUT_BYTES,
    REASONING_MODEL_MESSAGE,
    REASONING_PROMPT_MESSAGE,
    REASONING_REQUEST_TYPE_MESSAGE,
    REASONING_SETTINGS_TYPE_MESSAGE,
    REASONING_TEXT_MESSAGE,
    REASONING_TRANSPORT_MESSAGE,
    ReasoningExchange,
    ReasoningRequest,
    ReasoningResponse,
    ReasoningSettings,
    ReasoningTransport,
    history_content_bytes,
    request_content_bytes,
    request_reasoning,
)
from tests.log_assertions import assert_log_omits
from tests.reasoning_fakes import (
    FAKE_EXCEPTION_SENTINEL,
    FAKE_PAYLOAD_SENTINEL,
    OVERSIZED_BUDGET_MESSAGE,
    TRUNCATED_SOURCE,
    AdversarialReasoningProvider,
    HostileOutput,
    hostile_payload,
)


# Distinctive values that must never be shown, logged, or carried by a
# failure. The host and model are valid settings, so they can travel all the
# way through an enabled configuration.
PROMPT_SENTINEL = "private-reasoning-prompt-2e7b90c4d1"
HOST_SENTINEL = "fd4c:2b19:83a7::5e"
MODEL_SENTINEL = "sentinel-model-4f8a2c:7b"

OUTPUT_LIMIT = 4096

# Text far larger than either hard ceiling. Encoding it would allocate a
# buffer at least this many bytes long, which is what the bounded checks
# must never do.
HUGE_TEXT_LENGTH = 8 * max(
    REASONING_MAXIMUM_INPUT_BYTES,
    REASONING_MAXIMUM_OUTPUT_BYTES,
)

# Modules whose own import would give this interface a network, concurrency,
# asynchronous, or process capability.
FORBIDDEN_MODULES = frozenset(
    {
        "socket",
        "_socket",
        "ssl",
        "_ssl",
        "http",
        "urllib",
        "urllib3",
        "asyncio",
        "threading",
        "_thread",
        "concurrent",
        "subprocess",
        "_posixsubprocess",
        "multiprocessing",
        "selectors",
        "select",
        "socketserver",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "nntplib",
        "telnetlib",
        "xmlrpc",
        "webbrowser",
        "requests",
        "httpx",
        "aiohttp",
    }
)

# Builtins whose call would evaluate text or import a module chosen at run
# time, and the importlib function that does the latter. ``re.compile`` is an
# attribute call and is not the builtin ``compile``.
FORBIDDEN_BUILTIN_CALLS = frozenset({"eval", "exec", "compile", "__import__"})
FORBIDDEN_ATTRIBUTE_CALLS = frozenset({"import_module"})

# The only Maria modules the interface may depend on. Anything reaching
# planning, permissions, dispatch, persistence, sessions, or tools is refused.
PERMITTED_MARIA_IMPORTS = frozenset(
    # V1-24-R1 adds the project vocabulary, a pure leaf, for file parts.
    {"core.context", "core.failures", "core.logger", "core.project_records"}
)

PRODUCTION_SOURCES = (
    *sorted((PROJECT_ROOT / "core").glob("*.py")),
    *sorted((PROJECT_ROOT / "tools").glob("*.py")),
    *sorted((PROJECT_ROOT / "gui").glob("*.py")),
    PROJECT_ROOT / "main.py",
)

# Project Maria's approved path list (V1-24-R1) is data only: tuples of path
# strings, which tests.test_project_maria_manifest proves hold nothing else.
# Its paths name test files and modules, so a guard that searches source text
# for a module's name skips it; every import guard still covers it.
DATA_ONLY_MANIFEST = "project_maria_manifest.py"


def enabled_settings(
    *,
    max_input_bytes: int = OUTPUT_LIMIT,
    max_output_bytes: int = OUTPUT_LIMIT,
) -> ReasoningSettings:
    """Return valid enabled settings carrying the sentinel host and model."""
    return ReasoningSettings(
        enabled=True,
        transport=ReasoningTransport.PRIVATE_LAN,
        host=HOST_SENTINEL,
        model=MODEL_SENTINEL,
        request_deadline_seconds=30,
        max_input_bytes=max_input_bytes,
        max_output_bytes=max_output_bytes,
    )


def own_imports(source: str) -> set[str]:
    """Return every module a source imports itself, at any nesting depth.

    Only the source's own statements count. What a standard-library module
    imports in turn is deliberately not followed: ``logging`` itself imports
    ``threading``, and that says nothing about this module's capabilities.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def forbidden_calls(source: str) -> set[str]:
    """Return every evaluating or dynamically importing call a source makes."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_BUILTIN_CALLS | FORBIDDEN_ATTRIBUTE_CALLS:
                names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            if node.func.attr in FORBIDDEN_ATTRIBUTE_CALLS:
                names.add(node.func.attr)
    return names


def forbidden_imports(source: str) -> set[str]:
    """Return the forbidden modules a source imports itself."""
    return {
        name
        for name in own_imports(source)
        if name.split(".")[0] in FORBIDDEN_MODULES
    }


def peak_allocation(action: Callable[[], object]) -> tuple[object, int]:
    """Run an action and return its outcome and the peak bytes it allocated.

    Anything the action raises is returned as its outcome, so a refusal can
    be measured as well as a result. Only allocations made during the action
    are traced; text built beforehand is not counted.
    """
    tracemalloc.start()
    try:
        try:
            outcome: object = action()
        except Exception as raised:  # noqa: BLE001 - measured, then asserted.
            outcome = raised
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return outcome, peak


def rendered(value: object) -> str:
    """Render a value through every ordinary formatting route at once."""
    return " ".join(
        (
            repr(value),
            str(value),
            f"{value}",
            f"{value!r}",
            "%s %r" % (value, value),
            "{} {!r}".format(value, value),
            format(value, ""),
        )
    )


class ReasoningValueTests(unittest.TestCase):
    """Verify requests and replies are frozen, bounded, and redacted."""

    def test_the_request_carries_a_prompt_and_history_the_response_one_text(
        self,
    ) -> None:
        self.assertEqual(
            [f.name for f in fields(ReasoningRequest)],
            ["prompt", "history", "context", "proposal_instructions", "files"],
        )
        self.assertEqual([f.name for f in fields(ReasoningResponse)], ["text"])
        self.assertEqual(ReasoningRequest(PROMPT_SENTINEL).history, ())
        self.assertEqual(ReasoningRequest(PROMPT_SENTINEL).context, ())
        self.assertIs(
            ReasoningRequest(PROMPT_SENTINEL).proposal_instructions,
            False,
        )

    def test_the_request_has_no_system_template_or_raw_history_field(
        self,
    ) -> None:
        request = ReasoningRequest(PROMPT_SENTINEL)

        # "history" left this list in V1-14: it is the conversation's earlier
        # exchanges. "context" left it in V1-15: it is the typed Tier A
        # memory context, and nothing else retrieved on the owner's behalf.
        # V1-18's "proposal_instructions" is no system field either: it is an
        # exact bool selecting Maria's fixed text, which no caller can fill.
        for name in ("system", "template", "memories", "interactions"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(request, name))

    def test_requests_and_responses_are_frozen(self) -> None:
        request = ReasoningRequest("question")
        response = ReasoningResponse("answer")

        with self.assertRaises(FrozenInstanceError):
            request.prompt = "changed"
        with self.assertRaises(FrozenInstanceError):
            response.text = "changed"

    def test_text_must_be_exact_non_empty_encodable_str(self) -> None:
        class Text(str):
            """A str subclass that must not pass as exact text."""

        for value in (None, 7, b"bytes", "", Text("subclass"), "\ud800"):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ValueError) as request_error:
                    ReasoningRequest(value)
                with self.assertRaises(ValueError) as response_error:
                    ReasoningResponse(value)

                self.assertEqual(
                    str(request_error.exception),
                    REASONING_PROMPT_MESSAGE,
                )
                self.assertEqual(
                    str(response_error.exception),
                    REASONING_TEXT_MESSAGE,
                )

    def test_text_is_bounded_in_utf8_bytes_not_characters(self) -> None:
        cases = (
            (ReasoningRequest, REASONING_MAXIMUM_INPUT_BYTES),
            (ReasoningResponse, REASONING_MAXIMUM_OUTPUT_BYTES),
        )
        for value_type, maximum in cases:
            with self.subTest(value_type=value_type.__name__):
                value_type("a" * maximum)
                value_type("\u00e9" * (maximum // 2))

                with self.assertRaises(ValueError):
                    value_type("a" * (maximum + 1))
                # Fewer characters than the bound, but more bytes.
                with self.assertRaises(ValueError):
                    value_type("\u00e9" * (maximum // 2 + 1))

    def test_the_measurement_would_see_an_encoded_copy(self) -> None:
        """Positive control for the no-copy tests below."""
        huge = "a" * HUGE_TEXT_LENGTH

        _, peak = peak_allocation(lambda: huge.encode("utf-8"))

        self.assertGreaterEqual(peak, HUGE_TEXT_LENGTH)

    def test_text_far_beyond_the_hard_ceiling_is_refused_without_a_copy(
        self,
    ) -> None:
        huge = "a" * HUGE_TEXT_LENGTH
        cases = (
            (ReasoningRequest, REASONING_MAXIMUM_INPUT_BYTES, REASONING_PROMPT_MESSAGE),
            (ReasoningResponse, REASONING_MAXIMUM_OUTPUT_BYTES, REASONING_TEXT_MESSAGE),
        )
        for value_type, ceiling, message in cases:
            with self.subTest(value_type=value_type.__name__):
                refused, peak = peak_allocation(lambda: value_type(huge))

                self.assertIs(type(refused), ValueError)
                self.assertEqual(str(refused), message)
                # An encoded duplicate would need HUGE_TEXT_LENGTH bytes; the
                # refusal must not allocate even one ceiling's worth.
                self.assertLess(peak, ceiling)

    def test_no_route_shows_the_prompt_or_reply(self) -> None:
        for value in (
            ReasoningRequest(PROMPT_SENTINEL),
            ReasoningResponse(FAKE_PAYLOAD_SENTINEL),
        ):
            with self.subTest(value=type(value).__name__):
                text = rendered(value)

                self.assertNotIn(PROMPT_SENTINEL, text)
                self.assertNotIn(FAKE_PAYLOAD_SENTINEL, text)
                self.assertIn("<redacted>", repr(value))

    def test_a_refused_value_never_echoes_what_was_submitted(self) -> None:
        submitted = PROMPT_SENTINEL + "\ud800"

        with self.assertRaises(ValueError) as caught:
            ReasoningRequest(submitted)

        self.assertNotIn(PROMPT_SENTINEL, str(caught.exception))
        self.assertNotIn(PROMPT_SENTINEL, repr(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)


class ReasoningSettingsTests(unittest.TestCase):
    """Verify settings are strict when built directly, not only from TOML."""

    def test_disabled_settings_carry_no_provider_value(self) -> None:
        self.assertFalse(DISABLED_REASONING.enabled)
        for field in fields(ReasoningSettings):
            if field.name != "enabled":
                with self.subTest(field=field.name):
                    self.assertIsNone(getattr(DISABLED_REASONING, field.name))

    def test_disabled_settings_refuse_any_provider_value(self) -> None:
        with self.assertRaises(ValueError) as caught:
            ReasoningSettings(enabled=False, host="127.0.0.1")

        self.assertEqual(str(caught.exception), REASONING_DISABLED_FIELDS_MESSAGE)

    def test_enabled_must_be_an_exact_bool(self) -> None:
        for value in (1, 0, "true", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    ReasoningSettings(enabled=value)
                self.assertEqual(str(caught.exception), REASONING_ENABLED_MESSAGE)

    def test_the_transport_must_be_an_exact_member(self) -> None:
        for value in ("loopback", "private_lan", None, 1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    self._settings(transport=value)
                self.assertEqual(
                    str(caught.exception),
                    REASONING_TRANSPORT_MESSAGE,
                )

    def test_the_transport_classes_are_exactly_loopback_and_private_lan(
        self,
    ) -> None:
        self.assertEqual(
            {transport.value for transport in ReasoningTransport},
            {"loopback", "private_lan"},
        )

    def test_hosts_inside_their_class_are_accepted(self) -> None:
        accepted = {
            ReasoningTransport.LOOPBACK: ("127.0.0.1", "127.8.9.10", "::1"),
            ReasoningTransport.PRIVATE_LAN: (
                "10.0.0.1",
                "172.16.0.1",
                "172.31.255.254",
                "192.168.1.20",
                "fc00::1",
                "fd12:3456:789a::1",
            ),
        }
        for transport, hosts in accepted.items():
            for host in hosts:
                with self.subTest(transport=transport.value, host=host):
                    settings = self._settings(transport=transport, host=host)
                    self.assertEqual(settings.host, host)

    def test_hosts_outside_their_class_or_not_canonical_are_refused(
        self,
    ) -> None:
        refused = {
            ReasoningTransport.LOOPBACK: (
                "10.0.0.1",
                "fd00::1",
                "::ffff:127.0.0.1",
                "localhost",
            ),
            ReasoningTransport.PRIVATE_LAN: (
                "127.0.0.1",
                "::1",
                "169.254.10.1",
                "fe80::1",
                "fe80::1%eth0",
                "100.64.0.1",
                "172.32.0.1",
                "8.8.8.8",
                "2001:db8::1",
                "0.0.0.0",
                "::",
                "224.0.0.1",
                "255.255.255.255",
                "::ffff:10.0.0.1",
                "FD00::1",
                "010.0.0.1",
                " 10.0.0.1",
                "10.0.0.1:8080",
                "http://10.0.0.1",
                "inference.lan",
                "",
                "1" * 40,
            ),
        }
        for transport, hosts in refused.items():
            for host in hosts:
                with self.subTest(transport=transport.value, host=host):
                    with self.assertRaises(ValueError) as caught:
                        self._settings(transport=transport, host=host)
                    self.assertEqual(str(caught.exception), REASONING_HOST_MESSAGE)

    def test_model_identifiers_are_opaque_bounded_labels(self) -> None:
        for model in ("m", "llama3.1:8b", "publisher/name@sha256:ab12", "a" * 128):
            with self.subTest(model=model[:20]):
                self.assertEqual(self._settings(model=model).model, model)

        for model in (
            "",
            "a" * 129,
            " model",
            "model name",
            "/models/local.gguf",
            ".hidden",
            "~/model",
            "C:\\models\\local.gguf",
            "m\u00f6del",
            "model\n",
            "model\"",
            7,
            None,
        ):
            with self.subTest(model=repr(model)[:20]):
                with self.assertRaises(ValueError) as caught:
                    self._settings(model=model)
                self.assertEqual(str(caught.exception), REASONING_MODEL_MESSAGE)

    def test_windows_drive_paths_are_not_model_identifiers(self) -> None:
        for model in (
            "C:/models/local.gguf",
            "c:/models/local.gguf",
            "Z:/m",
            "D:/",
            "C:models/local.gguf",
            "C:",
        ):
            with self.subTest(model=model):
                with self.assertRaises(ValueError) as caught:
                    self._settings(model=model)
                self.assertEqual(str(caught.exception), REASONING_MODEL_MESSAGE)

        for model in (
            "publisher/model:tag",
            "llama3.1:8b",
            "hf.co/publisher/model:Q4_K_M",
            "registry.lan:5000/publisher/model:tag",
            "ab:c",
        ):
            with self.subTest(model=model):
                self.assertEqual(self._settings(model=model).model, model)

    def test_integer_fields_are_exact_ints_within_their_range(self) -> None:
        cases = {
            "request_deadline_seconds": (300, REASONING_DEADLINE_MESSAGE),
            "max_input_bytes": (
                REASONING_MAXIMUM_INPUT_BYTES,
                REASONING_MAX_INPUT_MESSAGE,
            ),
            "max_output_bytes": (
                REASONING_MAXIMUM_OUTPUT_BYTES,
                REASONING_MAX_OUTPUT_MESSAGE,
            ),
        }
        for name, (maximum, message) in cases.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(self._settings(**{name: 1}), name), 1)
                self.assertEqual(
                    getattr(self._settings(**{name: maximum}), name),
                    maximum,
                )
                for value in (0, -1, maximum + 1, True, 1.0, "1", None):
                    with self.subTest(field=name, value=repr(value)):
                        with self.assertRaises(ValueError) as caught:
                            self._settings(**{name: value})
                        self.assertEqual(str(caught.exception), message)

    def test_enabled_settings_never_show_their_values(self) -> None:
        settings = enabled_settings()
        text = rendered(settings)

        self.assertNotIn(HOST_SENTINEL, text)
        self.assertNotIn(MODEL_SENTINEL, text)
        self.assertEqual(repr(settings), "ReasoningSettings(enabled=True, <redacted>)")
        self.assertEqual(repr(DISABLED_REASONING), "ReasoningSettings(enabled=False)")

    def test_a_refused_setting_never_echoes_the_value(self) -> None:
        for override in (
            {"host": HOST_SENTINEL + "x"},
            {"model": MODEL_SENTINEL + " "},
        ):
            with self.subTest(field=next(iter(override))):
                with self.assertRaises(ValueError) as caught:
                    self._settings(**override)

                self.assertNotIn(HOST_SENTINEL, str(caught.exception))
                self.assertNotIn(MODEL_SENTINEL, str(caught.exception))
                self.assertIsNone(caught.exception.__context__)

    @staticmethod
    def _settings(**overrides: object) -> ReasoningSettings:
        """Build enabled settings, replacing only the named fields."""
        values: dict[str, object] = {
            "transport": ReasoningTransport.PRIVATE_LAN,
            "host": "10.0.0.1",
            "model": "model:tag",
            "request_deadline_seconds": 30,
            "max_input_bytes": 1024,
            "max_output_bytes": 1024,
        }
        values.update(overrides)
        return ReasoningSettings(enabled=True, **values)


class RequestReasoningBoundaryTests(unittest.TestCase):
    """Verify every provider outcome becomes inert text or a typed failure."""

    def setUp(self) -> None:
        self.settings = enabled_settings()
        self.request = ReasoningRequest(PROMPT_SENTINEL)
        self.log = Mock(spec=logging.Logger)

    def _ask(
        self,
        provider: AdversarialReasoningProvider,
        request: ReasoningRequest | None = None,
        settings: ReasoningSettings | None = None,
    ) -> ReasoningResponse | Failure:
        """Ask through the boundary with this test's recording logger."""
        return request_reasoning(
            provider,
            self.request if request is None else request,
            self.settings if settings is None else settings,
            self.log,
        )

    def test_disabled_reasoning_answers_unavailable_without_asking(self) -> None:
        provider = AdversarialReasoningProvider.emitting(
            HostileOutput.INJECTED_INSTRUCTION
        )

        result = self._ask(provider, settings=DISABLED_REASONING)

        self.assertEqual(result, Failure(FailureKind.REASONING_UNAVAILABLE))
        self.assertEqual(provider.calls, 0)
        self.log.warning.assert_called_once_with(
            "Reasoning request failed: code=%s",
            "reasoning_unavailable",
        )

    def test_the_log_records_the_outcome_and_nothing_it_was_given(self) -> None:
        self._ask(AdversarialReasoningProvider.emitting(HostileOutput.UNKNOWN_TOOL))
        self._ask(AdversarialReasoningProvider.crashing())

        self.log.info.assert_called_once_with("Reasoning request completed.")
        self.log.warning.assert_called_once_with(
            "Reasoning request failed: code=%s",
            "reasoning_unavailable",
        )
        recorded = repr(self.log.mock_calls)
        for sentinel in (
            PROMPT_SENTINEL,
            FAKE_PAYLOAD_SENTINEL,
            FAKE_EXCEPTION_SENTINEL,
            HOST_SENTINEL,
            MODEL_SENTINEL,
        ):
            with self.subTest(sentinel=sentinel):
                self.assertNotIn(sentinel, recorded)

    def test_an_oversized_prompt_is_refused_before_the_provider_is_asked(
        self,
    ) -> None:
        settings = enabled_settings(max_input_bytes=10)
        cases = {
            "at the budget": ("a" * 10, 1),
            "one byte over": ("a" * 11, 0),
            "fewer characters, more bytes": ("\u00e9" * 6, 0),
        }
        for label, (prompt, expected_calls) in cases.items():
            with self.subTest(case=label):
                provider = AdversarialReasoningProvider.returning(
                    ReasoningResponse("reply")
                )

                result = self._ask(
                    provider,
                    request=ReasoningRequest(prompt),
                    settings=settings,
                )

                self.assertEqual(provider.calls, expected_calls)
                if expected_calls == 0:
                    self.assertEqual(
                        result,
                        Failure(FailureKind.REASONING_INPUT_OVER_BUDGET),
                    )
                else:
                    self.assertEqual(result, ReasoningResponse("reply"))

    def test_hostile_text_comes_back_inert_and_byte_for_byte(self) -> None:
        for output in HostileOutput:
            if output is HostileOutput.OVERSIZED_OUTPUT:
                continue
            with self.subTest(output=output.value):
                provider = AdversarialReasoningProvider.emitting(
                    output,
                    OUTPUT_LIMIT,
                )

                result = self._ask(provider)

                self.assertIs(type(result), ReasoningResponse)
                self.assertIs(type(result.text), str)
                self.assertEqual(
                    result.text,
                    hostile_payload(output, OUTPUT_LIMIT),
                )
                self.assertEqual(provider.calls, 1)
                self.assertIs(provider.requests[0], self.request)

    def test_an_accepted_reply_is_a_fresh_value(self) -> None:
        answered = ReasoningResponse("reply")
        provider = AdversarialReasoningProvider.returning(answered)

        result = self._ask(provider)

        self.assertEqual(result, answered)
        self.assertIsNot(result, answered)

    def test_an_oversized_reply_is_discarded_whole_never_truncated(self) -> None:
        settings = enabled_settings(max_output_bytes=10)
        cases = {
            "hostile oversized output": ReasoningResponse(
                hostile_payload(HostileOutput.OVERSIZED_OUTPUT, 10)
            ),
            "fewer characters, more bytes": ReasoningResponse("\u00e9" * 6),
        }
        for label, answer in cases.items():
            with self.subTest(case=label):
                provider = AdversarialReasoningProvider.returning(answer)

                result = self._ask(provider, settings=settings)

                self.assertEqual(
                    result,
                    Failure(FailureKind.REASONING_OUTPUT_OVER_BUDGET),
                )

        at_budget = AdversarialReasoningProvider.returning(
            ReasoningResponse("a" * 10)
        )
        self.assertEqual(
            self._ask(at_budget, settings=settings),
            ReasoningResponse("a" * 10),
        )

    def test_the_fake_emits_oversized_output_the_boundary_discards(self) -> None:
        provider = AdversarialReasoningProvider.emitting(
            HostileOutput.OVERSIZED_OUTPUT,
            OUTPUT_LIMIT,
        )

        result = self._ask(provider)

        self.assertEqual(result, Failure(FailureKind.REASONING_OUTPUT_OVER_BUDGET))
        self.assertEqual(provider.calls, 1)

    def test_oversized_output_at_the_hard_ceiling_is_over_budget(self) -> None:
        # The largest budget the fake accepts makes it emit a reply exactly
        # as large as the hard ceiling: a valid response that only the
        # configured budget refuses, never a construction failure reported
        # as unavailability.
        budget = REASONING_MAXIMUM_OUTPUT_BYTES - 1
        provider = AdversarialReasoningProvider.emitting(
            HostileOutput.OVERSIZED_OUTPUT,
            budget,
        )

        result = self._ask(
            provider,
            settings=enabled_settings(max_output_bytes=budget),
        )

        self.assertEqual(result, Failure(FailureKind.REASONING_OUTPUT_OVER_BUDGET))
        self.assertEqual(provider.calls, 1)

    def test_accepted_text_is_returned_byte_for_byte(self) -> None:
        text = "Café 漢字 \U0001f600 line one\nline two\t"
        size = len(text.encode("utf-8"))
        provider = AdversarialReasoningProvider.returning(ReasoningResponse(text))

        result = self._ask(
            provider,
            settings=enabled_settings(max_output_bytes=size),
        )

        self.assertIs(type(result), ReasoningResponse)
        self.assertEqual(result.text, text)
        self.assertEqual(result.text.encode("utf-8"), text.encode("utf-8"))

    def test_a_prompt_enlarged_far_beyond_the_ceiling_is_refused_unread(
        self,
    ) -> None:
        request = ReasoningRequest("question")
        object.__setattr__(request, "prompt", "a" * HUGE_TEXT_LENGTH)
        provider = AdversarialReasoningProvider.returning(ReasoningResponse("reply"))

        result, peak = peak_allocation(
            lambda: self._ask(provider, request=request)
        )

        self.assertEqual(result, Failure(FailureKind.REASONING_INPUT_OVER_BUDGET))
        self.assertEqual(provider.calls, 0)
        self.assertLess(peak, REASONING_MAXIMUM_INPUT_BYTES)

    def test_a_reply_enlarged_far_beyond_the_ceiling_is_refused_uncopied(
        self,
    ) -> None:
        cases = {
            "ordinary text": "a" * HUGE_TEXT_LENGTH,
            # Over-length text is over budget before its encodability is
            # even considered, because deciding that would mean encoding it.
            "over-length and unencodable": "a" * HUGE_TEXT_LENGTH + "\ud800",
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                answer = ReasoningResponse("reply")
                object.__setattr__(answer, "text", text)
                provider = AdversarialReasoningProvider.returning(answer)

                result, peak = peak_allocation(lambda: self._ask(provider))

                self.assertEqual(
                    result,
                    Failure(FailureKind.REASONING_OUTPUT_OVER_BUDGET),
                )
                self.assertLess(peak, REASONING_MAXIMUM_OUTPUT_BYTES)

    def test_every_typed_reasoning_failure_passes_through_as_its_kind(
        self,
    ) -> None:
        for kind in sorted(REASONING_FAILURE_KINDS, key=lambda item: item.value):
            with self.subTest(kind=kind.value):
                provider = AdversarialReasoningProvider.failing(kind)

                result = self._ask(provider)

                self.assertEqual(result, Failure(kind))
                self.assertEqual(provider.calls, 1)

    def test_every_required_failure_category_is_reachable(self) -> None:
        required = {
            "unavailable": FailureKind.REASONING_UNAVAILABLE,
            "timeout": FailureKind.REASONING_TIMEOUT,
            "malformed output": FailureKind.REASONING_MALFORMED_OUTPUT,
            "input over budget": FailureKind.REASONING_INPUT_OVER_BUDGET,
            "output over budget": FailureKind.REASONING_OUTPUT_OVER_BUDGET,
            "refused": FailureKind.REASONING_REFUSED,
        }

        self.assertLessEqual(set(required.values()), REASONING_FAILURE_KINDS)
        for label, kind in required.items():
            with self.subTest(category=label):
                failure = Failure(kind)
                self.assertTrue(failure.message)
                self.assertEqual(failure.code, kind.value)

    def test_a_provider_cannot_impersonate_another_failure_vocabulary(
        self,
    ) -> None:
        for kind in (
            FailureKind.TOOL_EXECUTION_FAILED,
            FailureKind.DATABASE_SCHEMA_TOO_NEW,
        ):
            with self.subTest(kind=kind.value):
                provider = AdversarialReasoningProvider.failing(kind)

                result = self._ask(provider)

                self.assertEqual(result, Failure(FailureKind.REASONING_UNAVAILABLE))

    def test_a_tampered_or_derived_typed_failure_is_not_believed(self) -> None:
        class Impostor(TypedFailureError):
            """A derived carrier that must not be believed."""

        tampered = TypedFailureError(Failure(FailureKind.REASONING_TIMEOUT))
        tampered.args = (FAKE_EXCEPTION_SENTINEL,)

        for raised in (
            tampered,
            Impostor(Failure(FailureKind.REASONING_REFUSED)),
        ):
            with self.subTest(raised=type(raised).__name__):
                provider = AdversarialReasoningProvider(_raiser(raised))

                result = self._ask(provider)

                self.assertEqual(result, Failure(FailureKind.REASONING_UNAVAILABLE))

    def test_an_untyped_exception_is_contained_without_its_text(self) -> None:
        provider = AdversarialReasoningProvider.crashing()

        result = self._ask(provider)

        self.assertEqual(result, Failure(FailureKind.REASONING_UNAVAILABLE))
        self.assertIs(type(result), Failure)
        self.assertNotIn(FAKE_EXCEPTION_SENTINEL, rendered(result))
        self.assertNotIn(FAKE_EXCEPTION_SENTINEL, result.message)

    def test_an_interrupt_is_never_contained(self) -> None:
        for raised in (KeyboardInterrupt(), SystemExit(1)):
            with self.subTest(raised=type(raised).__name__):
                provider = AdversarialReasoningProvider(_raiser(raised))

                with self.assertRaises(type(raised)):
                    self._ask(provider)

    def test_anything_but_an_exact_well_formed_response_is_malformed(
        self,
    ) -> None:
        class DerivedResponse(ReasoningResponse):
            """A subclass that must not pass as a response."""

        class Text(str):
            """A str subclass that must not pass as reply text."""

        def swapped(value: object) -> ReasoningResponse:
            answer = ReasoningResponse("reply")
            object.__setattr__(answer, "text", value)
            return answer

        missing = ReasoningResponse("reply")
        object.__delattr__(missing, "text")

        answers = {
            "None": None,
            "plain str": "reply",
            "mapping": {"text": "reply"},
            "lookalike": SimpleNamespace(text="reply"),
            "subclass": DerivedResponse("reply"),
            "swapped to int": swapped(7),
            "swapped to empty": swapped(""),
            "swapped to str subclass": swapped(Text("reply")),
            "swapped to unencodable": swapped("\ud800"),
            "text deleted": missing,
        }
        for label, answer in answers.items():
            with self.subTest(answer=label):
                provider = AdversarialReasoningProvider.returning(answer)

                result = self._ask(provider)

                self.assertEqual(
                    result,
                    Failure(FailureKind.REASONING_MALFORMED_OUTPUT),
                )

    def test_a_reply_enlarged_after_construction_is_still_bounded(self) -> None:
        answer = ReasoningResponse("reply")
        object.__setattr__(answer, "text", "a" * (OUTPUT_LIMIT + 1))
        provider = AdversarialReasoningProvider.returning(answer)

        result = self._ask(provider)

        self.assertEqual(result, Failure(FailureKind.REASONING_OUTPUT_OVER_BUDGET))

    def test_callers_must_pass_exact_settings_and_requests(self) -> None:
        provider = AdversarialReasoningProvider.emitting(
            HostileOutput.UNKNOWN_TOOL
        )
        cases = (
            (self.request, None, REASONING_SETTINGS_TYPE_MESSAGE),
            (self.request, "enabled", REASONING_SETTINGS_TYPE_MESSAGE),
            (PROMPT_SENTINEL, self.settings, REASONING_REQUEST_TYPE_MESSAGE),
            (None, self.settings, REASONING_REQUEST_TYPE_MESSAGE),
        )
        for request, settings, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(ValueError) as caught:
                    request_reasoning(provider, request, settings, self.log)

                self.assertEqual(str(caught.exception), message)
                self.assertNotIn(PROMPT_SENTINEL, str(caught.exception))
        self.assertEqual(provider.calls, 0)


class ReasoningHistoryTests(unittest.TestCase):
    """Verify earlier exchanges are typed, bounded, redacted, and re-measured.

    A request's size is the UTF-8 length of its prompt plus every earlier
    exchange's user and assistant text, and nothing else. It is bounded by
    the hard ceiling at construction and by the configured budget at the
    boundary, where it is measured again however the request was built.
    """

    def setUp(self) -> None:
        self.log = Mock(spec=logging.Logger)
        self.exchange = ReasoningExchange("u", "a")

    def _ask(
        self,
        provider: AdversarialReasoningProvider,
        request: ReasoningRequest,
        settings: ReasoningSettings | None = None,
    ) -> ReasoningResponse | Failure:
        """Ask through the boundary with this test's recording logger."""
        return request_reasoning(
            provider,
            request,
            enabled_settings() if settings is None else settings,
            self.log,
        )

    def test_an_exchange_is_frozen_redacted_and_exact_text(self) -> None:
        exchange = ReasoningExchange(PROMPT_SENTINEL, FAKE_PAYLOAD_SENTINEL)
        request = ReasoningRequest("question", (exchange,))

        with self.assertRaises(FrozenInstanceError):
            exchange.user = "changed"
        with self.assertRaises(FrozenInstanceError):
            request.history = ()
        for value in (exchange, request):
            with self.subTest(value=type(value).__name__):
                text = rendered(value)
                self.assertNotIn(PROMPT_SENTINEL, text)
                self.assertNotIn(FAKE_PAYLOAD_SENTINEL, text)
                self.assertIn("<redacted>", repr(value))

    def test_exchange_text_must_be_exact_non_empty_encodable_str(self) -> None:
        class Text(str):
            """A str subclass that must not pass as exact text."""

        for user, assistant in (
            (None, "a"),
            ("u", None),
            ("", "a"),
            ("u", ""),
            (Text("u"), "a"),
            ("u", Text("a")),
            ("\ud800", "a"),
            ("u", b"a"),
        ):
            with self.subTest(user=type(user).__name__, assistant=assistant):
                with self.assertRaises(ValueError) as caught:
                    ReasoningExchange(user, assistant)
                self.assertEqual(str(caught.exception), REASONING_EXCHANGE_MESSAGE)

    def test_exchange_text_is_bounded_by_the_hard_ceilings(self) -> None:
        ReasoningExchange(
            "é" * (REASONING_MAXIMUM_INPUT_BYTES // 2),
            "é" * (REASONING_MAXIMUM_OUTPUT_BYTES // 2),
        )
        for user, assistant in (
            ("é" * (REASONING_MAXIMUM_INPUT_BYTES // 2) + "a", "a"),
            ("u", "é" * (REASONING_MAXIMUM_OUTPUT_BYTES // 2) + "a"),
        ):
            with self.assertRaises(ValueError) as caught:
                ReasoningExchange(user, assistant)
            self.assertEqual(str(caught.exception), REASONING_EXCHANGE_MESSAGE)

    def test_history_must_be_an_exact_tuple_of_exact_exchanges(self) -> None:
        class History(tuple):
            """A tuple subclass that must not pass as exact history."""

        for history in (
            [self.exchange],
            History((self.exchange,)),
            (("u", "a"),),
            ("u",),
            (self.exchange, None),
            None,
        ):
            with self.subTest(history=type(history).__name__):
                with self.assertRaises(ValueError) as caught:
                    ReasoningRequest("question", history)
                self.assertEqual(str(caught.exception), REASONING_HISTORY_MESSAGE)

    def test_prompt_and_history_together_meet_the_hard_ceiling(self) -> None:
        # The exchange is two bytes, so this prompt fills the ceiling exactly.
        at_ceiling = "p" * (REASONING_MAXIMUM_INPUT_BYTES - 2)

        request = ReasoningRequest(at_ceiling, (self.exchange,))
        self.assertEqual(request.history, (self.exchange,))
        with self.assertRaises(ValueError) as caught:
            ReasoningRequest(at_ceiling + "p", (self.exchange,))
        self.assertEqual(str(caught.exception), REASONING_HISTORY_MESSAGE)

    def test_the_accounting_counts_utf8_text_and_stops_once_over(self) -> None:
        history = (
            ReasoningExchange("é", "€"),
            ReasoningExchange("a", "\U0001f600"),
        )
        self.assertEqual(history_content_bytes(history, 100), 10)
        self.assertEqual(request_content_bytes("é", history, 100), 12)

        # Past the bound nothing further is inspected, so a broken exchange
        # beyond it is never reached.
        broken = ReasoningExchange("u", "a")
        object.__setattr__(broken, "assistant", "")
        self.assertGreater(
            request_content_bytes("é", (*history, broken), 11),
            11,
        )
        with self.assertRaises(ValueError) as caught:
            request_content_bytes("é", (*history, broken), 100)
        self.assertEqual(str(caught.exception), REASONING_HISTORY_MESSAGE)

    def test_the_boundary_measures_prompt_and_history_against_the_budget(
        self,
    ) -> None:
        settings = enabled_settings(max_input_bytes=10)
        # Two bytes of user text and six of assistant text: eight in all.
        history = (ReasoningExchange("é", "€€"),)
        cases = {
            "at the budget": ("ab", 1),
            "one byte over": ("abc", 0),
            "fewer characters, more bytes": ("éa", 0),
        }
        for label, (prompt, expected_calls) in cases.items():
            with self.subTest(case=label):
                provider = AdversarialReasoningProvider.returning(
                    ReasoningResponse("reply")
                )
                request = ReasoningRequest(prompt, history)

                result = self._ask(provider, request, settings)

                self.assertEqual(provider.calls, expected_calls)
                if expected_calls:
                    self.assertEqual(result, ReasoningResponse("reply"))
                    self.assertIs(provider.requests[0], request)
                else:
                    self.assertEqual(
                        result,
                        Failure(FailureKind.REASONING_INPUT_OVER_BUDGET),
                    )

    def test_a_history_enlarged_after_construction_is_refused_uncopied(
        self,
    ) -> None:
        enlarged = ReasoningExchange("u", "a")
        object.__setattr__(enlarged, "assistant", "a" * HUGE_TEXT_LENGTH)
        cases = {
            "an enlarged exchange": (enlarged,),
            "far too many exchanges": (self.exchange,) * 500_000,
        }
        for label, history in cases.items():
            with self.subTest(case=label):
                request = ReasoningRequest("question")
                object.__setattr__(request, "history", history)
                provider = AdversarialReasoningProvider.returning(
                    ReasoningResponse("reply")
                )

                result, peak = peak_allocation(lambda: self._ask(provider, request))

                self.assertEqual(
                    result,
                    Failure(FailureKind.REASONING_INPUT_OVER_BUDGET),
                )
                self.assertEqual(provider.calls, 0)
                self.assertLess(peak, REASONING_MAXIMUM_INPUT_BYTES)

    def test_a_history_tampered_into_anything_but_exchanges_is_refused(
        self,
    ) -> None:
        emptied = ReasoningExchange("u", "a")
        object.__setattr__(emptied, "assistant", "")
        for history in (None, [self.exchange], ("u",), (emptied,)):
            with self.subTest(history=repr(history)[:30]):
                request = ReasoningRequest("question")
                object.__setattr__(request, "history", history)
                provider = AdversarialReasoningProvider.returning(
                    ReasoningResponse("reply")
                )

                with self.assertRaises(ValueError) as caught:
                    self._ask(provider, request)

                self.assertEqual(str(caught.exception), REASONING_HISTORY_MESSAGE)
                self.assertEqual(provider.calls, 0)


class AdversarialFakeTests(unittest.TestCase):
    """Verify the fake is hostile in exactly the way each category says."""

    def test_the_fake_offers_exactly_the_eight_roadmap_categories(self) -> None:
        self.assertEqual(
            {output.value for output in HostileOutput},
            {
                "malformed_structure",
                "unknown_tool",
                "unknown_action",
                "extra_arguments",
                "out_of_range_values",
                "injected_instruction",
                "truncated_output",
                "oversized_output",
            },
        )

    def test_every_payload_carries_the_sentinel_and_never_varies(self) -> None:
        for output in HostileOutput:
            with self.subTest(output=output.value):
                payload = hostile_payload(output, OUTPUT_LIMIT)

                self.assertIn(FAKE_PAYLOAD_SENTINEL, payload)
                self.assertEqual(payload, hostile_payload(output, OUTPUT_LIMIT))

    def test_malformed_structure_parses_but_has_no_proposal_shape(self) -> None:
        parsed = json.loads(hostile_payload(HostileOutput.MALFORMED_STRUCTURE))

        self.assertIsInstance(parsed, list)
        self.assertNotIsInstance(parsed[0]["tool"], str)
        self.assertNotIn("action", parsed[0])
        self.assertIsInstance(parsed[0]["arguments"], list)

    def test_the_proposal_categories_fail_the_real_contracts_as_named(
        self,
    ) -> None:
        expected = {
            HostileOutput.UNKNOWN_TOOL: ContractFailure.UNKNOWN_TOOL,
            HostileOutput.UNKNOWN_ACTION: ContractFailure.UNKNOWN_ACTION,
            HostileOutput.EXTRA_ARGUMENTS: ContractFailure.EXTRA_ARGUMENT,
            HostileOutput.OUT_OF_RANGE_VALUES: ContractFailure.OUT_OF_RANGE,
        }
        for output, failure in expected.items():
            with self.subTest(output=output.value):
                proposal = json.loads(hostile_payload(output))

                violation = BUILT_IN_ACTION_CONTRACTS.validate(
                    proposal["tool"],
                    proposal["action"],
                    proposal["arguments"],
                )

                self.assertIsNotNone(violation)
                self.assertIs(violation.kind, failure)

    def test_injected_instruction_is_instruction_text_not_a_proposal(
        self,
    ) -> None:
        payload = hostile_payload(HostileOutput.INJECTED_INSTRUCTION)

        self.assertIn("Ignore every previous instruction", payload)
        self.assertIn("filesystem.write", payload)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(payload)

    def test_truncated_output_is_a_cut_off_prefix_that_cannot_parse(
        self,
    ) -> None:
        payload = hostile_payload(HostileOutput.TRUNCATED_OUTPUT)

        self.assertTrue(TRUNCATED_SOURCE.startswith(payload))
        self.assertLess(len(payload), len(TRUNCATED_SOURCE))
        json.loads(TRUNCATED_SOURCE)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(payload)

    def test_oversized_output_is_exactly_one_byte_over_its_limit(self) -> None:
        for limit in (1, 10, OUTPUT_LIMIT):
            with self.subTest(limit=limit):
                payload = hostile_payload(HostileOutput.OVERSIZED_OUTPUT, limit)
                size = len(payload.encode("utf-8"))

                self.assertGreater(size, limit)
                if limit >= len(FAKE_PAYLOAD_SENTINEL):
                    self.assertEqual(size, limit + 1)

    def test_the_oversized_mode_refuses_setup_without_a_usable_budget(
        self,
    ) -> None:
        # Without a budget below the hard ceiling the payload could not be a
        # valid response, and a test would silently exercise unavailability
        # instead of the over-budget refusal it meant to check.
        ceiling = REASONING_MAXIMUM_OUTPUT_BYTES
        for budget in (None, 0, -1, ceiling, ceiling + 1, True, 10.0, "10"):
            with self.subTest(budget=repr(budget)):
                with self.assertRaises(ValueError) as payload_error:
                    hostile_payload(HostileOutput.OVERSIZED_OUTPUT, budget)
                with self.assertRaises(ValueError) as setup_error:
                    AdversarialReasoningProvider.emitting(
                        HostileOutput.OVERSIZED_OUTPUT,
                        budget,
                    )

                self.assertEqual(
                    str(payload_error.exception),
                    OVERSIZED_BUDGET_MESSAGE,
                )
                self.assertEqual(
                    str(setup_error.exception),
                    OVERSIZED_BUDGET_MESSAGE,
                )

        with self.assertRaises(ValueError):
            AdversarialReasoningProvider.emitting(HostileOutput.OVERSIZED_OUTPUT)

    def test_the_largest_oversized_payload_is_still_a_valid_response(
        self,
    ) -> None:
        payload = hostile_payload(
            HostileOutput.OVERSIZED_OUTPUT,
            REASONING_MAXIMUM_OUTPUT_BYTES - 1,
        )

        self.assertEqual(
            len(payload.encode("utf-8")),
            REASONING_MAXIMUM_OUTPUT_BYTES,
        )
        self.assertEqual(ReasoningResponse(payload).text, payload)

    def test_every_other_mode_ignores_the_budget(self) -> None:
        for output in HostileOutput:
            if output is HostileOutput.OVERSIZED_OUTPUT:
                continue
            with self.subTest(output=output.value):
                payload = hostile_payload(output)

                self.assertEqual(hostile_payload(output, OUTPUT_LIMIT), payload)
                self.assertEqual(
                    AdversarialReasoningProvider.emitting(output).respond(
                        ReasoningRequest("question")
                    ),
                    ReasoningResponse(payload),
                )

    def test_the_fake_records_every_request_it_receives(self) -> None:
        provider = AdversarialReasoningProvider.emitting(
            HostileOutput.UNKNOWN_ACTION
        )
        first = ReasoningRequest("first")
        second = ReasoningRequest("second")

        provider.respond(first)
        provider.respond(second)

        self.assertEqual(provider.calls, 2)
        self.assertEqual(provider.requests, [first, second])

    def test_hostile_payload_refuses_an_unknown_category(self) -> None:
        with self.assertRaises(ValueError):
            hostile_payload("unknown_tool")


class ReasoningImportBoundaryTests(unittest.TestCase):
    """Guard what the interface can reach and who can reach the fake."""

    def setUp(self) -> None:
        self.source = Path(reasoning_module.__file__).read_text(encoding="utf-8")

    def test_the_interface_imports_no_network_concurrency_or_process_module(
        self,
    ) -> None:
        self.assertEqual(forbidden_imports(self.source), set())

    def test_configuration_imports_none_of_them_either(self) -> None:
        source = (PROJECT_ROOT / "core" / "config.py").read_text(encoding="utf-8")

        self.assertEqual(forbidden_imports(source), set())

    def test_the_interface_evaluates_no_text_and_imports_nothing_dynamically(
        self,
    ) -> None:
        self.assertEqual(forbidden_calls(self.source), set())

    def test_the_interface_reaches_no_planning_dispatch_or_persistence(
        self,
    ) -> None:
        maria_modules = {
            name
            for name in own_imports(self.source)
            if name.split(".")[0] in {"core", "tools", "main", "tests"}
            and name.count(".") == 1
        }

        self.assertLessEqual(maria_modules, PERMITTED_MARIA_IMPORTS)
        self.assertIn("core.failures", maria_modules)

    def test_the_scan_sees_forbidden_imports_at_any_depth(self) -> None:
        """Positive control, so the guards cannot pass by seeing nothing."""
        source = (
            "import socket\n"
            "from concurrent import futures\n"
            "def later():\n"
            "    import subprocess\n"
            "    from urllib.request import urlopen\n"
            "    import_module('asyncio')\n"
            "    eval('1')\n"
            "    re.compile('allowed')\n"
            "    return __import__('threading')\n"
        )

        self.assertEqual(
            forbidden_imports(source),
            {
                "socket",
                "concurrent",
                "concurrent.futures",
                "subprocess",
                "urllib.request",
                "urllib.request.urlopen",
            },
        )
        self.assertEqual(
            forbidden_calls(source),
            {"__import__", "import_module", "eval"},
        )

    def test_no_production_module_imports_the_fake_or_the_tests(self) -> None:
        self.assertIn(PROJECT_ROOT / "core" / "reasoning.py", PRODUCTION_SOURCES)
        for path in PRODUCTION_SOURCES:
            with self.subTest(module=path.name):
                source = path.read_text(encoding="utf-8")
                imported = {name.split(".")[0] for name in own_imports(source)}

                self.assertNotIn("tests", imported)
                if path.name != DATA_ONLY_MANIFEST:
                    self.assertNotIn("reasoning_fakes", source)
                self.assertNotIn("AdversarialReasoningProvider", source)


class ReasoningLogPrivacyTests(unittest.TestCase):
    """Prove no prompt, reply, host, model, or provider text reaches a log."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.log_dir = Path(self.temporary_directory.name) / "logs"

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

    def test_success_and_failure_paths_log_codes_and_nothing_private(
        self,
    ) -> None:
        configure_logging(self.log_dir, level="DEBUG")
        settings = enabled_settings()
        request = ReasoningRequest(PROMPT_SENTINEL)

        providers = [
            AdversarialReasoningProvider.emitting(output, OUTPUT_LIMIT)
            for output in HostileOutput
        ]
        providers += [
            AdversarialReasoningProvider.failing(kind)
            for kind in REASONING_FAILURE_KINDS
        ]
        providers += [
            AdversarialReasoningProvider.crashing(),
            AdversarialReasoningProvider.returning(None),
            AdversarialReasoningProvider.returning(FAKE_PAYLOAD_SENTINEL),
        ]
        for provider in providers:
            request_reasoning(provider, request, settings)
        request_reasoning(providers[0], request, DISABLED_REASONING)
        request_reasoning(
            providers[0],
            request,
            enabled_settings(max_input_bytes=1),
        )

        log_file = self.log_dir / "maria.log"
        for handler in self.maria_logger.handlers:
            handler.flush()
        log_text = log_file.read_text(encoding="utf-8")

        # Positive controls: the boundary did log, so an empty log cannot
        # pass for a private one.
        self.assertIn("Reasoning request completed.", log_text)
        for code in (
            "reasoning_unavailable",
            "reasoning_timeout",
            "reasoning_malformed_output",
            "reasoning_input_over_budget",
            "reasoning_output_over_budget",
        ):
            with self.subTest(code=code):
                self.assertIn(f"code={code}", log_text)

        assert_log_omits(
            self,
            log_file,
            (
                PROMPT_SENTINEL,
                FAKE_PAYLOAD_SENTINEL,
                FAKE_EXCEPTION_SENTINEL,
                HOST_SENTINEL,
                MODEL_SENTINEL,
            ),
        )


def _raiser(raised: BaseException):
    """Return an answer function that raises exactly the given exception."""

    def answer() -> None:
        raise raised

    return answer


if __name__ == "__main__":
    unittest.main()
