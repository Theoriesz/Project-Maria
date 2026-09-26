"""Focused tests for Maria's local Ollama reasoning adapter.

Every exchange here runs against a local stub transport. For the whole of
every test the adapter's one connection opener is replaced: by a scripted
peer on one end of a local socket pair when a test installs one, and
otherwise by a guard that fails the test outright. No test can reach Ollama,
needs a downloaded model, listens on a port, or leaves this machine. The
stub's sockets and threads live only here, never in production.

Timing tests use the shortest deadline settings allow, one second, against a
deliberately slow peer. Each asserts an upper bound with generous slack, so a
busy machine cannot turn a pass into a failure, while a deadline that failed
to hold would overrun by many seconds.

The last class is the network import-boundary guard: only the adapter may
import a network module, and nothing Brain, the planner, permissions,
ToolManager, the memory layer, configuration, or any tool imports, directly
or through any other Maria module, may import a network or threading one.
"""

from __future__ import annotations

import ast
import codecs
import json
import logging
import os
import re
import socket
import tempfile
import threading
import time
import tracemalloc
import unittest
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn
from unittest.mock import patch

import core.ollama_adapter as ollama_adapter
from core.brain import MariaBrain
from core.config import load_config
from core.constants import CONFIG_PATH, PROJECT_ROOT
from core.failures import Failure, FailureKind, TypedFailureError
from core.logger import configure_logging
from core.ollama_adapter import (
    JSON_ESCAPE_EXPANSION,
    MAXIMUM_RESPONSE_HEADER_BYTES,
    OLLAMA_ADDRESS,
    OLLAMA_CHAT_PATH,
    OLLAMA_CLOUD_MODEL_MESSAGE,
    OLLAMA_DESTINATION_MESSAGE,
    OLLAMA_HOST,
    OLLAMA_PORT,
    RESPONSE_ENVELOPE_OVERHEAD_BYTES,
    OllamaReasoningProvider,
)
from core.planner import Planner
from core.reasoning import (
    DISABLED_REASONING,
    REASONING_DEADLINE_MESSAGE,
    REASONING_HOST_MESSAGE,
    REASONING_MODEL_MESSAGE,
    REASONING_PROMPT_MESSAGE,
    REASONING_REQUEST_TYPE_MESSAGE,
    REASONING_SETTINGS_TYPE_MESSAGE,
    ReasoningRequest,
    ReasoningResponse,
    ReasoningSettings,
    ReasoningTransport,
    request_reasoning,
)
from core.tool_manager import BUILT_IN_TOOL_MODULES, ToolManager
from tests.log_assertions import assert_log_omits
from tests.reasoning_fakes import HostileOutput, hostile_payload


# Distinctive values that must never reach a failure or a log. The model is a
# valid, non-cloud identifier, so it travels through enabled settings.
PROMPT_SENTINEL = "private-ollama-prompt-7d41c9e2a8"
REPLY_SENTINEL = "private-ollama-reply-3b8f06d5c1"
MODEL_SENTINEL = "sentinel-local-model-5e2a91:7b"
EXCEPTION_SENTINEL = "raw-provider-exception-0c6b4f19e7"
HEADER_SENTINEL = "private-provider-header-9a17e3d05b"
TOOL_SENTINEL = "private-tool-call-4e9d2b7a16"
THINKING_SENTINEL = "private-thinking-text-b58c30f4e2"

# The fixed destination, spelled out independently of the adapter's own
# constants, and every form of it no failure or log line may carry.
EXPECTED_ADDRESS = ("127.0.0.1", 11434)
DESTINATION_TEXT = ("127.0.0.1", "11434", "/api/chat", "api/chat")

BUDGET = 4096
ENVELOPE = RESPONSE_ENVELOPE_OVERHEAD_BYTES + JSON_ESCAPE_EXPANSION * BUDGET

DEADLINE = 5
SHORT_DEADLINE = 1

# How far past a one-second deadline a timing test may finish and still pass.
# A deadline that failed to hold would overrun by far more than this.
SLACK = 2.0

# The longest any stub peer waits for, or keeps talking to, the adapter.
PEER_PATIENCE = 10.0

# The adapter may read at most one receive past an allowance before it
# notices, and a receive is at most 64 KiB; twice that is the most a bounded
# read can overshoot, however much the peer offers.
READ_OVERSHOOT = 2 * 64 * 1024

# Stands for a field a reply body leaves out.
ABSENT = object()

# The original opener, kept before any test replaces it, so the one test of
# the real opener can reach it with the socket module itself stubbed.
REAL_OPEN_CONNECTION = ollama_adapter._open_connection

Script = Callable[[socket.socket], None]


def adapter_settings(
    *,
    transport: ReasoningTransport = ReasoningTransport.LOOPBACK,
    host: str = "127.0.0.1",
    model: str = MODEL_SENTINEL,
    deadline: int = DEADLINE,
    max_input_bytes: int = BUDGET,
    max_output_bytes: int = BUDGET,
) -> ReasoningSettings:
    """Return valid enabled settings, loopback and 127.0.0.1 by default."""
    return ReasoningSettings(
        enabled=True,
        transport=transport,
        host=host,
        model=model,
        request_deadline_seconds=deadline,
        max_input_bytes=max_input_bytes,
        max_output_bytes=max_output_bytes,
    )


def chat_body(
    content: object = REPLY_SENTINEL,
    *,
    escape_html: bool = False,
    **fields: object,
) -> bytes:
    """Return an Ollama-shaped ``/api/chat`` reply body as JSON bytes.

    Any field may be replaced, and one given as ``ABSENT`` is left out.
    ``escape_html`` writes ``<`` the way Ollama's encoder does, as the six
    bytes ``\\u003c``.
    """
    document: dict[str, object] = {
        "model": MODEL_SENTINEL,
        "created_at": "2026-01-01T00:00:00.000000Z",
        "message": {"role": "assistant", "content": content},
        "done_reason": "stop",
        "done": True,
        "total_duration": 1234567,
        "load_duration": 12345,
        "prompt_eval_count": 12,
        "prompt_eval_duration": 1234,
        "eval_count": 34,
        "eval_duration": 123456,
    }
    document.update(fields)
    document = {
        key: value for key, value in document.items() if value is not ABSENT
    }
    body = json.dumps(document).encode("utf-8")
    if escape_html:
        body = body.replace(b"<", b"\\u003c")
    return body


def http_response(
    body: bytes,
    *,
    status: bytes = b"200 OK",
    fields: Iterable[bytes] = (),
    framing: str = "length",
) -> bytes:
    """Return one whole HTTP/1.1 response, framed by length or in chunks."""
    head = [
        b"HTTP/1.1 " + status,
        b"Content-Type: application/json; charset=utf-8",
        *fields,
    ]
    if framing == "length":
        head.append(b"Content-Length: %d" % len(body))
    elif framing == "chunked":
        head.append(b"Transfer-Encoding: chunked")
        body = chunked(body)
    return b"\r\n".join(head) + b"\r\n\r\n" + body


def chunked(
    body: bytes,
    *,
    size: int = 7,
    extension: bytes = b"",
    trailers: Iterable[bytes] = (),
    final: bool = True,
) -> bytes:
    """Return a body in chunked transfer coding, optionally without its end."""
    pieces = [
        b"%x%s\r\n%s\r\n" % (len(piece), extension, piece)
        for piece in (
            body[start : start + size] for start in range(0, len(body), size)
        )
    ]
    if final:
        pieces.append(b"0\r\n" + b"".join(t + b"\r\n" for t in trailers))
        pieces.append(b"\r\n")
    return b"".join(pieces)


def split_request(raw: bytes) -> tuple[list[bytes], bytes]:
    """Split one recorded request into its head lines and its body."""
    head, _, body = raw.partition(b"\r\n\r\n")
    return head.split(b"\r\n"), body


def rendered(value: object) -> str:
    """Render a value through every ordinary formatting route at once."""
    return " ".join(
        (repr(value), str(value), f"{value}", f"{value!r}", "%s" % (value,))
    )


# --- Stub transport --------------------------------------------------------


class StubConnection:
    """The adapter's end of a local socket pair, recording what it is asked.

    Every call passes through to the real socket, while the timeouts the
    adapter sets, the bytes it receives, and whether it closed are recorded.
    ``stall_send`` makes ``sendall`` behave like a runtime that never reads:
    it waits out the timeout it was given and then times out, which a real
    loopback socket cannot be made to do reliably because the operating
    system buffers megabytes. ``reset_after`` makes the connection fail, as
    a reset would, once that many bytes have been received.
    """

    def __init__(
        self,
        sock: socket.socket,
        *,
        stall_send: bool,
        reset_after: int | None,
    ) -> None:
        self._socket = sock
        self._stall_send = stall_send
        self._reset_after = reset_after
        self.timeouts: list[float] = []
        self.received = 0
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)
        self._socket.settimeout(timeout)

    def sendall(self, data: bytes) -> None:
        if self._stall_send:
            time.sleep(min(self.timeouts[-1], PEER_PATIENCE))
            raise TimeoutError(EXCEPTION_SENTINEL)
        self._socket.sendall(data)

    def recv(self, size: int) -> bytes:
        if self._reset_after is not None and self.received >= self._reset_after:
            raise ConnectionResetError(EXCEPTION_SENTINEL)
        if self._reset_after is not None:
            size = min(size, self._reset_after - self.received)
        data = self._socket.recv(size)
        self.received += len(data)
        return data

    def close(self) -> None:
        self.closed = True
        self._socket.close()


class StubTransport:
    """A scripted local peer standing in for the Ollama runtime.

    Installed in place of the adapter's connection opener, it records every
    destination and timeout the adapter asks for. It then either fails the
    way it was told to, or hands the adapter one end of a fresh local socket
    pair and, on a test thread, reads the adapter's request from the other
    end and runs ``script`` against it. The peer's end is closed when the
    script returns.
    """

    def __init__(
        self,
        script: Script | None = None,
        *,
        connect_error: BaseException | None = None,
        hang_connect: bool = False,
        connect_delay: float = 0.0,
        stall_send: bool = False,
        reset_after: int | None = None,
    ) -> None:
        self._script = script
        self._connect_error = connect_error
        self._hang_connect = hang_connect
        self._connect_delay = connect_delay
        self._stall_send = stall_send
        self._reset_after = reset_after
        self._threads: list[threading.Thread] = []
        self.opened: list[tuple[object, float]] = []
        self.connections: list[StubConnection] = []
        self.requests: list[bytes] = []

    def open(self, address: object, timeout: float) -> StubConnection:
        """Stand in for the adapter's opener, as the test was scripted to."""
        self.opened.append((address, timeout))
        if self._hang_connect:
            time.sleep(min(timeout, PEER_PATIENCE))
            raise TimeoutError(EXCEPTION_SENTINEL)
        if self._connect_error is not None:
            raise self._connect_error
        time.sleep(self._connect_delay)

        client, peer = socket.socketpair()
        connection = StubConnection(
            client,
            stall_send=self._stall_send,
            reset_after=self._reset_after,
        )
        self.connections.append(connection)
        thread = threading.Thread(target=self._serve, args=(peer,), daemon=True)
        self._threads.append(thread)
        thread.start()
        return connection

    def _serve(self, peer: socket.socket) -> None:
        """Read the request, then run the script, on the peer's end."""
        with peer:
            peer.settimeout(PEER_PATIENCE)
            try:
                self.requests.append(read_http_request(peer))
                if self._script is not None:
                    self._script(peer)
            except OSError:
                # The adapter may hang up at any moment; a timing test
                # expects exactly that.
                pass

    def finish(self) -> None:
        """Wait for every peer to finish, so no thread outlives its test."""
        for thread in self._threads:
            thread.join(PEER_PATIENCE + 5)
            if thread.is_alive():
                raise AssertionError("A stub peer outlived its test.")


def read_http_request(peer: socket.socket) -> bytes:
    """Read one whole request, framed by its Content-Length, from the peer."""
    data = bytearray()
    while b"\r\n\r\n" not in data:
        data += _received(peer)
    head, _, body = bytes(data).partition(b"\r\n\r\n")
    declared = re.search(rb"(?im)^content-length: *([0-9]+) *$", head)
    length = int(declared.group(1)) if declared else 0
    while len(body) < length:
        body += _received(peer)
    return head + b"\r\n\r\n" + body


def _received(peer: socket.socket) -> bytes:
    """Receive more from the adapter, treating its hang-up as an error."""
    data = peer.recv(64 * 1024)
    if not data:
        raise ConnectionAbortedError("The adapter hung up.")
    return data


def send(data: bytes) -> Script:
    """Send the data at once."""
    return lambda peer: peer.sendall(data)


def pause(seconds: float) -> Script:
    """Wait before the next step."""
    return lambda peer: time.sleep(seconds)


def hold(peer: socket.socket) -> None:
    """Keep the connection open, silently, until the adapter hangs up."""
    peer.recv(1)


def steps(*scripts: Script) -> Script:
    """Run several scripts in order."""

    def script(peer: socket.socket) -> None:
        for step in scripts:
            step(peer)

    return script


def in_pieces(data: bytes, *offsets: int) -> Script:
    """Send the data in pieces split at the offsets, pausing between them."""

    def script(peer: socket.socket) -> None:
        start = 0
        for end in (*offsets, len(data)):
            peer.sendall(data[start:end])
            time.sleep(0.02)
            start = end

    return script


def trickle(data: bytes, interval: float) -> Script:
    """Send the data a byte at a time, stopping when the adapter hangs up.

    The peer also gives up after ``PEER_PATIENCE``, so an adapter whose
    deadline failed to hold ends up failing its test, not hanging it.
    """

    def script(peer: socket.socket) -> None:
        give_up = time.monotonic() + PEER_PATIENCE
        for index in range(len(data)):
            if time.monotonic() > give_up:
                return
            peer.sendall(data[index : index + 1])
            peer.settimeout(interval)
            try:
                if not peer.recv(1):
                    return
            except TimeoutError:
                pass

    return script


def endless_chunks(peer: socket.socket) -> None:
    """Offer far more chunked body than any envelope, until hung up on."""
    peer.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
    piece = b"%x\r\n%s\r\n" % (1000, b"x" * 1000)
    for _ in range(4 * ENVELOPE // 1000):
        peer.sendall(piece)


def _refuse_real_connection(address: object, timeout: object) -> NoReturn:
    """Fail any test that reaches for a real connection."""
    raise AssertionError("A test reached the adapter's real connection opener.")


class StubbedAdapterTestCase(unittest.TestCase):
    """Keep every test in this module off the real network.

    The adapter's opener is replaced before each test with one that fails
    the test outright, so a test that installs no stub proves, by passing,
    that no connection was attempted.
    """

    def setUp(self) -> None:
        guard = patch.object(
            ollama_adapter,
            "_open_connection",
            _refuse_real_connection,
        )
        guard.start()
        self.addCleanup(guard.stop)

    def install(self, transport: StubTransport) -> StubTransport:
        """Route the adapter's connections to a stub for this test."""
        opener = patch.object(ollama_adapter, "_open_connection", transport.open)
        opener.start()
        self.addCleanup(opener.stop)
        self.addCleanup(transport.finish)
        return transport

    def provider(self, **overrides: Any) -> OllamaReasoningProvider:
        """Return an adapter over valid settings with the given overrides."""
        return OllamaReasoningProvider(adapter_settings(**overrides))

    def ask(
        self,
        provider: OllamaReasoningProvider | None = None,
        prompt: str = PROMPT_SENTINEL,
    ) -> ReasoningResponse:
        """Ask one question and return the reply."""
        return (provider or self.provider()).respond(ReasoningRequest(prompt))

    def assertRefusedAs(
        self,
        kind: FailureKind,
        provider: OllamaReasoningProvider | None = None,
        request: ReasoningRequest | None = None,
    ) -> TypedFailureError:
        """Assert one exchange fails as exactly this kind, chaining nothing."""
        with self.assertRaises(TypedFailureError) as caught:
            (provider or self.provider()).respond(
                request or ReasoningRequest(PROMPT_SENTINEL)
            )
        error = caught.exception
        self.assertIs(type(error), TypedFailureError)
        self.assertIs(error.failure.kind, kind)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        return error

    def assertAllClosed(self, transport: StubTransport) -> None:
        """Assert the adapter closed every connection it was given."""
        for connection in transport.connections:
            self.assertTrue(connection.closed)

    def assertOnlyTheFixedDestination(self, transport: StubTransport) -> None:
        """Assert exactly one connection, to exactly 127.0.0.1:11434."""
        self.assertEqual(
            [address for address, _ in transport.opened],
            [EXPECTED_ADDRESS],
        )


# --- Settings and destination ----------------------------------------------


class OllamaAdapterSettingsTests(StubbedAdapterTestCase):
    """Prove the adapter exists only for the one fixed loopback destination."""

    def test_the_destination_is_fixed_to_loopback_ollama_chat(self) -> None:
        self.assertEqual(OLLAMA_ADDRESS, EXPECTED_ADDRESS)
        self.assertEqual((OLLAMA_HOST, OLLAMA_PORT), EXPECTED_ADDRESS)
        self.assertEqual(OLLAMA_CHAT_PATH, "/api/chat")

    def test_settings_must_be_exact_enabled_reasoning_settings(self) -> None:
        with self.assertRaises(ValueError) as caught:
            OllamaReasoningProvider(DISABLED_REASONING)
        self.assertEqual(str(caught.exception), OLLAMA_DESTINATION_MESSAGE)

        look_alike = SimpleNamespace(**vars(adapter_settings()))
        for settings in (None, "loopback", look_alike):
            with self.subTest(settings=type(settings).__name__):
                with self.assertRaises(ValueError) as caught:
                    OllamaReasoningProvider(settings)  # type: ignore[arg-type]
                self.assertEqual(
                    str(caught.exception),
                    REASONING_SETTINGS_TYPE_MESSAGE,
                )

    def test_private_lan_is_refused_before_any_connection(self) -> None:
        for host in ("192.168.1.20", "10.0.0.5", "172.16.0.9", "fd00::1"):
            with self.subTest(host=host):
                settings = adapter_settings(
                    transport=ReasoningTransport.PRIVATE_LAN,
                    host=host,
                )
                with self.assertRaises(ValueError) as caught:
                    OllamaReasoningProvider(settings)
                self.assertEqual(
                    str(caught.exception),
                    OLLAMA_DESTINATION_MESSAGE,
                )
                self.assertNotIn(host, str(caught.exception))

    def test_another_loopback_host_is_refused_before_any_connection(
        self,
    ) -> None:
        for host in ("127.0.0.2", "127.1.0.1", "127.255.255.254", "::1"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError) as caught:
                    OllamaReasoningProvider(adapter_settings(host=host))
                self.assertEqual(
                    str(caught.exception),
                    OLLAMA_DESTINATION_MESSAGE,
                )

    def test_cloud_designated_models_are_refused_before_any_connection(
        self,
    ) -> None:
        for model in (
            "gpt-oss:120b-cloud",
            "gpt-oss:20b-cloud",
            "glm-4.6:cloud",
            "Qwen3-Coder:480B-CLOUD",
            "library/deepseek-v3.1:671b-cloud",
            "kimi-k2:1t-cloud@sha256:0123abcd",
        ):
            with self.subTest(model=model):
                with self.assertRaises(ValueError) as caught:
                    OllamaReasoningProvider(adapter_settings(model=model))
                self.assertEqual(
                    str(caught.exception),
                    OLLAMA_CLOUD_MODEL_MESSAGE,
                )
                self.assertNotIn(model, str(caught.exception))

    def test_local_model_identifiers_are_accepted(self) -> None:
        for model in (
            "llama3.2:3b",
            "qwen2.5:7b-instruct-q4_K_M",
            "publisher/model:tag",
            "mistral",
            "cloud:latest",
            "cloudy-model:q4",
            "my-cloud-notes",
        ):
            with self.subTest(model=model):
                OllamaReasoningProvider(adapter_settings(model=model))

    def test_tampered_settings_are_validated_again(self) -> None:
        cases = (
            ("host", "192.168.1.20", REASONING_HOST_MESSAGE),
            ("model", "C:/models/local.gguf", REASONING_MODEL_MESSAGE),
            ("request_deadline_seconds", 10**6, REASONING_DEADLINE_MESSAGE),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                settings = adapter_settings()
                object.__setattr__(settings, field, value)
                with self.assertRaises(ValueError) as caught:
                    OllamaReasoningProvider(settings)
                self.assertEqual(str(caught.exception), message)

    def test_later_changes_to_the_settings_never_reach_the_adapter(
        self,
    ) -> None:
        transport = self.install(
            StubTransport(send(http_response(chat_body())))
        )
        settings = adapter_settings()
        provider = OllamaReasoningProvider(settings)
        object.__setattr__(settings, "transport", ReasoningTransport.PRIVATE_LAN)
        object.__setattr__(settings, "host", "192.168.1.20")
        object.__setattr__(settings, "model", "another-model:1b")

        self.assertEqual(self.ask(provider).text, REPLY_SENTINEL)
        self.assertOnlyTheFixedDestination(transport)
        _, body = split_request(transport.requests[0])
        self.assertEqual(json.loads(body)["model"], MODEL_SENTINEL)

    def test_the_adapter_never_shows_its_settings(self) -> None:
        text = rendered(self.provider())

        self.assertIn("OllamaReasoningProvider", text)
        for private in (MODEL_SENTINEL, *DESTINATION_TEXT):
            self.assertNotIn(private, text)

    def test_the_tracked_configuration_cannot_enable_the_adapter(self) -> None:
        """Re-verify, for V1-13, the gate an enabled adapter must pass."""
        tracked = load_config(CONFIG_PATH).reasoning
        self.assertEqual(tracked, DISABLED_REASONING)
        with self.assertRaises(ValueError):
            OllamaReasoningProvider(tracked)

        rules = [
            line.strip()
            for line in (PROJECT_ROOT / ".gitignore").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertIn("config/config.local.toml", rules)
        self.assertNotIn("!config/config.local.toml", rules)


# --- Exchanges -------------------------------------------------------------


class OllamaAdapterExchangeTests(StubbedAdapterTestCase):
    """Prove one complete exchange, and each way a runtime can be missing."""

    def test_one_complete_bounded_answer(self) -> None:
        transport = self.install(
            StubTransport(send(http_response(chat_body())))
        )

        response = self.ask()

        self.assertIs(type(response), ReasoningResponse)
        self.assertEqual(response.text, REPLY_SENTINEL)
        self.assertOnlyTheFixedDestination(transport)
        (_, timeout), = transport.opened
        self.assertTrue(0 < timeout <= DEADLINE)
        self.assertAllClosed(transport)

    def test_the_request_carries_the_model_and_prompt_and_nothing_else(
        self,
    ) -> None:
        prompt = "Grüße → 世界, " + PROMPT_SENTINEL
        transport = self.install(
            StubTransport(send(http_response(chat_body())))
        )

        self.ask(prompt=prompt)

        lines, body = split_request(transport.requests[0])
        self.assertEqual(lines[0], b"POST /api/chat HTTP/1.1")
        self.assertCountEqual(
            lines[1:],
            [
                b"Host: 127.0.0.1:11434",
                b"Content-Type: application/json",
                b"Content-Length: %d" % len(body),
                b"Connection: close",
            ],
        )
        self.assertEqual(
            json.loads(body.decode("utf-8")),
            {
                "model": MODEL_SENTINEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
        )

    def test_a_chunked_answer_is_reassembled_exactly(self) -> None:
        content = "Ünïcödé answer, chunked. " * 20
        raw = (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            + chunked(
                chat_body(content),
                size=7,
                extension=b";name=value",
                trailers=[b"X-Trailer: done"],
            )
        )
        transport = self.install(StubTransport(send(raw)))

        self.assertEqual(self.ask().text, content)
        self.assertAllClosed(transport)

    def test_a_response_split_across_many_reads_is_reassembled(self) -> None:
        raw = http_response(chat_body())
        head_end = raw.index(b"\r\n\r\n")
        self.install(
            StubTransport(
                in_pieces(
                    raw,
                    5,
                    16,
                    17,
                    30,
                    head_end + 1,
                    head_end + 3,
                    len(raw) - 9,
                )
            )
        )

        self.assertEqual(self.ask().text, REPLY_SENTINEL)

    def test_an_answer_exactly_at_the_output_budget_is_returned(self) -> None:
        content = "é" * (BUDGET // 2)
        self.install(StubTransport(send(http_response(chat_body(content)))))

        self.assertEqual(self.ask().text, content)

    def test_an_answer_escaped_to_six_times_its_size_still_fits(self) -> None:
        content = "<" * BUDGET
        body = chat_body(content, escape_html=True)
        self.assertGreater(len(body), JSON_ESCAPE_EXPANSION * BUDGET)
        self.install(StubTransport(send(http_response(body))))

        self.assertEqual(self.ask().text, content)

    def test_an_unavailable_runtime_is_unavailable(self) -> None:
        for error in (
            ConnectionRefusedError(EXCEPTION_SENTINEL),
            ConnectionResetError(EXCEPTION_SENTINEL),
            OSError(EXCEPTION_SENTINEL),
        ):
            with self.subTest(error=type(error).__name__):
                transport = self.install(StubTransport(connect_error=error))

                failure = self.assertRefusedAs(
                    FailureKind.REASONING_UNAVAILABLE
                )

                self.assertNotIn(EXCEPTION_SENTINEL, rendered(failure))
                # One attempt, and no retry or fallback anywhere else.
                self.assertOnlyTheFixedDestination(transport)

    def test_a_runtime_that_hangs_up_without_answering_is_unavailable(
        self,
    ) -> None:
        transport = self.install(StubTransport())

        self.assertRefusedAs(FailureKind.REASONING_UNAVAILABLE)
        self.assertAllClosed(transport)

    def test_a_connection_that_fails_before_any_reply_is_unavailable(
        self,
    ) -> None:
        transport = self.install(StubTransport(hold, reset_after=0))

        self.assertRefusedAs(FailureKind.REASONING_UNAVAILABLE)
        self.assertAllClosed(transport)

    def test_a_missing_model_is_unavailable(self) -> None:
        error = json.dumps(
            {"error": f"model '{MODEL_SENTINEL}' not found"}
        ).encode("utf-8")
        transport = self.install(
            StubTransport(
                send(http_response(error, status=b"404 Not Found"))
            )
        )

        failure = self.assertRefusedAs(FailureKind.REASONING_UNAVAILABLE)

        self.assertNotIn(MODEL_SENTINEL, rendered(failure))
        self.assertAllClosed(transport)

    def test_every_status_but_success_is_unavailable(self) -> None:
        for status in (
            b"100 Continue",
            b"201 Created",
            b"204 No Content",
            b"400 Bad Request",
            b"403 Forbidden",
            b"500 Internal Server Error",
            b"503 Service Unavailable",
        ):
            with self.subTest(status=status):
                transport = self.install(
                    StubTransport(
                        send(http_response(chat_body(), status=status))
                    )
                )

                self.assertRefusedAs(FailureKind.REASONING_UNAVAILABLE)
                self.assertAllClosed(transport)

    def test_a_redirect_is_never_followed(self) -> None:
        for status, location in (
            (b"301 Moved Permanently", b"https://ollama.com/api/chat"),
            (b"302 Found", b"http://203.0.113.7:11434/api/chat"),
            (b"307 Temporary Redirect", b"http://localhost:8080/api/chat"),
            (b"308 Permanent Redirect", b"/api/generate"),
        ):
            with self.subTest(status=status):
                transport = self.install(
                    StubTransport(
                        send(
                            http_response(
                                chat_body(),
                                status=status,
                                fields=[b"Location: " + location],
                            )
                        )
                    )
                )

                self.assertRefusedAs(FailureKind.REASONING_UNAVAILABLE)
                self.assertOnlyTheFixedDestination(transport)
                self.assertEqual(len(transport.requests), 1)

    def test_proxy_settings_are_never_consulted(self) -> None:
        proxy = "http://203.0.113.9:3128"
        environment = {
            name: proxy
            for name in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
            )
        }
        environment["NO_PROXY"] = ""
        transport = self.install(
            StubTransport(send(http_response(chat_body())))
        )

        with patch.dict(os.environ, environment):
            self.assertEqual(self.ask().text, REPLY_SENTINEL)

        self.assertOnlyTheFixedDestination(transport)
        lines, _ = split_request(transport.requests[0])
        # Origin-form, as sent to the server itself; a proxied request
        # would name a scheme and host on its request line.
        self.assertEqual(lines[0], b"POST /api/chat HTTP/1.1")
        self.assertNotIn(b"203.0.113.9", transport.requests[0])
        self.assertFalse(
            [line for line in lines if line.lower().startswith(b"proxy")]
        )

    def test_the_real_opener_connects_straight_to_the_numeric_address(
        self,
    ) -> None:
        created: list[RecordingSocket] = []

        class RecordingSocket:
            """A socket that records its calls and never touches a network."""

            def __init__(self, family: int, kind: int) -> None:
                self.family = family
                self.kind = kind
                self.calls: list[tuple[object, ...]] = []
                created.append(self)

            def settimeout(self, timeout: float) -> None:
                self.calls.append(("settimeout", timeout))

            def connect(self, address: object) -> None:
                self.calls.append(("connect", address))

            def close(self) -> None:
                self.calls.append(("close",))

        with _no_resolution(), patch.object(socket, "socket", RecordingSocket):
            connection = REAL_OPEN_CONNECTION(OLLAMA_ADDRESS, 2.5)

        self.assertEqual(created, [connection])
        self.assertEqual(
            (connection.family, connection.kind),
            (socket.AF_INET, socket.SOCK_STREAM),
        )
        self.assertEqual(
            connection.calls,
            [("settimeout", 2.5), ("connect", EXPECTED_ADDRESS)],
        )

    def test_the_real_opener_closes_a_socket_that_fails_to_connect(
        self,
    ) -> None:
        created: list[RefusedSocket] = []

        class RefusedSocket:
            """A socket whose connection is always refused."""

            def __init__(self, family: int, kind: int) -> None:
                self.closed = False
                created.append(self)

            def settimeout(self, timeout: float) -> None:
                pass

            def connect(self, address: object) -> None:
                raise ConnectionRefusedError(EXCEPTION_SENTINEL)

            def close(self) -> None:
                self.closed = True

        with _no_resolution(), patch.object(socket, "socket", RefusedSocket):
            with self.assertRaises(ConnectionRefusedError):
                REAL_OPEN_CONNECTION(OLLAMA_ADDRESS, 2.5)

        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].closed)


@contextmanager
def _no_resolution() -> Iterator[None]:
    """Make every name-resolving or connecting helper fail loudly."""

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("A resolving or connecting helper was used.")

    with ExitStack() as stack:
        for name in (
            "getaddrinfo",
            "gethostbyname",
            "gethostbyname_ex",
            "create_connection",
        ):
            stack.enter_context(patch.object(socket, name, forbidden))
        yield


# --- Deadline --------------------------------------------------------------


class OllamaAdapterDeadlineTests(StubbedAdapterTestCase):
    """Prove one absolute deadline bounds the whole exchange."""

    def assertTimesOutWithinTheDeadline(self, transport: StubTransport) -> None:
        """Assert a one-second exchange times out, and does so in time."""
        provider = self.provider(deadline=SHORT_DEADLINE)
        started = time.monotonic()

        self.assertRefusedAs(FailureKind.REASONING_TIMEOUT, provider)

        self.assertLess(time.monotonic() - started, SHORT_DEADLINE + SLACK)
        self.assertAllClosed(transport)
        for _, timeout in transport.opened:
            self.assertTrue(0 < timeout <= SHORT_DEADLINE)
        for connection in transport.connections:
            self.assertTrue(
                all(0 < wait <= SHORT_DEADLINE for wait in connection.timeouts)
            )

    def test_a_connection_that_never_opens_times_out(self) -> None:
        transport = self.install(StubTransport(hang_connect=True))

        self.assertTimesOutWithinTheDeadline(transport)

    def test_a_request_the_runtime_never_accepts_times_out(self) -> None:
        transport = self.install(StubTransport(stall_send=True))

        self.assertTimesOutWithinTheDeadline(transport)

    def test_headers_that_never_arrive_time_out(self) -> None:
        transport = self.install(StubTransport(hold))

        self.assertTimesOutWithinTheDeadline(transport)

    def test_trickled_headers_cannot_stretch_the_deadline(self) -> None:
        head = b"HTTP/1.1 200 OK\r\nX-Padding: " + b"a" * 2000
        transport = self.install(StubTransport(trickle(head, 0.05)))

        self.assertTimesOutWithinTheDeadline(transport)
        self._assert_every_wait_was_given_only_what_remained(transport)

    def test_a_trickled_body_cannot_stretch_the_deadline(self) -> None:
        head = b"HTTP/1.1 200 OK\r\nContent-Length: 3000\r\n\r\n"
        transport = self.install(
            StubTransport(steps(send(head), trickle(b"x" * 3000, 0.05)))
        )

        self.assertTimesOutWithinTheDeadline(transport)
        self._assert_every_wait_was_given_only_what_remained(transport)

    def test_a_body_that_stops_midway_times_out(self) -> None:
        raw = http_response(chat_body())
        transport = self.install(
            StubTransport(steps(send(raw[: len(raw) - 20]), hold))
        )

        self.assertTimesOutWithinTheDeadline(transport)

    def test_one_deadline_spans_connecting_and_the_response(self) -> None:
        # Each stage alone fits well inside the deadline and only their sum
        # does not, so a deadline restarted per stage would have succeeded.
        transport = self.install(
            StubTransport(
                steps(pause(0.6), send(http_response(chat_body()))),
                connect_delay=0.6,
            )
        )

        self.assertTimesOutWithinTheDeadline(transport)

    def test_slow_stages_that_fit_the_deadline_succeed(self) -> None:
        transport = self.install(
            StubTransport(
                steps(pause(0.2), send(http_response(chat_body()))),
                connect_delay=0.2,
            )
        )

        response = self.ask(self.provider(deadline=SHORT_DEADLINE))

        self.assertEqual(response.text, REPLY_SENTINEL)
        self.assertAllClosed(transport)

    def _assert_every_wait_was_given_only_what_remained(
        self,
        transport: StubTransport,
    ) -> None:
        """Assert many waits happened, each shorter than the one before.

        Each wait was satisfied by a byte well within any per-wait timeout,
        so only a shrinking, shared deadline could have ended the exchange.
        """
        waits = transport.connections[0].timeouts
        self.assertGreater(len(waits), 5)
        self.assertEqual(waits, sorted(waits, reverse=True))


# --- Bounds ----------------------------------------------------------------


class OllamaAdapterBoundsTests(StubbedAdapterTestCase):
    """Prove input, headers, the response, and the answer are all bounded."""

    def test_an_oversized_prompt_is_refused_before_any_connection(
        self,
    ) -> None:
        provider = self.provider(max_input_bytes=16)
        # The guard opener fails the test if reached, so each refusal also
        # proves no connection was attempted.
        for prompt in ("x" * 17, "é" * 9):
            with self.subTest(characters=len(prompt)):
                self.assertRefusedAs(
                    FailureKind.REASONING_INPUT_OVER_BUDGET,
                    provider,
                    ReasoningRequest(prompt),
                )

    def test_a_prompt_exactly_at_the_input_budget_is_sent(self) -> None:
        transport = self.install(
            StubTransport(send(http_response(chat_body())))
        )

        self.ask(self.provider(max_input_bytes=16), prompt="é" * 8)

        _, body = split_request(transport.requests[0])
        self.assertEqual(
            json.loads(body.decode("utf-8"))["messages"][0]["content"],
            "é" * 8,
        )

    def test_a_prompt_enlarged_after_construction_is_refused_uncopied(
        self,
    ) -> None:
        request = ReasoningRequest("small")
        huge = "x" * (8 * 1024 * 1024)
        object.__setattr__(request, "prompt", huge)
        provider = self.provider()

        tracemalloc.start()
        try:
            self.assertRefusedAs(
                FailureKind.REASONING_INPUT_OVER_BUDGET,
                provider,
                request,
            )
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 1024 * 1024)

    def test_a_prompt_tampered_into_anything_but_text_is_refused(
        self,
    ) -> None:
        for value in (b"bytes", "", "\ud800", None):
            with self.subTest(value=type(value).__name__):
                request = ReasoningRequest("small")
                object.__setattr__(request, "prompt", value)
                with self.assertRaises(ValueError) as caught:
                    self.provider().respond(request)
                self.assertEqual(str(caught.exception), REASONING_PROMPT_MESSAGE)

    def test_anything_but_a_request_is_refused(self) -> None:
        for request in (None, PROMPT_SENTINEL, SimpleNamespace(prompt="x")):
            with self.subTest(request=type(request).__name__):
                with self.assertRaises(ValueError) as caught:
                    self.provider().respond(request)  # type: ignore[arg-type]
                self.assertEqual(
                    str(caught.exception),
                    REASONING_REQUEST_TYPE_MESSAGE,
                )

    def test_a_declared_oversized_body_is_refused_unread(self) -> None:
        head = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % (ENVELOPE + 1)
        transport = self.install(StubTransport(steps(send(head), hold)))
        started = time.monotonic()

        self.assertRefusedAs(FailureKind.REASONING_OUTPUT_OVER_BUDGET)

        # Refused on the declaration alone: nothing was waited for.
        self.assertLess(time.monotonic() - started, DEADLINE / 2)
        self.assertLessEqual(transport.connections[0].received, len(head))
        self.assertAllClosed(transport)

    def test_a_body_filling_the_envelope_is_read_and_one_byte_more_is_not(
        self,
    ) -> None:
        base = len(chat_body(padding=""))
        exact = chat_body(padding="p" * (ENVELOPE - base))
        over = chat_body(padding="p" * (ENVELOPE - base + 1))
        self.assertEqual(len(exact), ENVELOPE)

        self.install(StubTransport(send(http_response(exact))))
        self.assertEqual(self.ask().text, REPLY_SENTINEL)

        self.install(StubTransport(send(http_response(over))))
        self.assertRefusedAs(FailureKind.REASONING_OUTPUT_OVER_BUDGET)

    def test_an_endless_chunked_body_is_refused_at_the_envelope(self) -> None:
        transport = self.install(StubTransport(endless_chunks))

        self.assertRefusedAs(FailureKind.REASONING_OUTPUT_OVER_BUDGET)

        self.assertLessEqual(
            transport.connections[0].received,
            MAXIMUM_RESPONSE_HEADER_BYTES + ENVELOPE + READ_OVERSHOOT,
        )
        self.assertAllClosed(transport)

    def test_a_huge_chunk_size_is_refused_before_it_is_read(self) -> None:
        head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        transport = self.install(
            StubTransport(steps(send(head + b"FFFFFFFFFFFFFFFF\r\n"), hold))
        )
        started = time.monotonic()

        self.assertRefusedAs(FailureKind.REASONING_OUTPUT_OVER_BUDGET)

        self.assertLess(time.monotonic() - started, DEADLINE / 2)
        self.assertAllClosed(transport)

    def test_oversized_headers_are_refused_unread(self) -> None:
        cases = {
            "one long field": (
                b"HTTP/1.1 200 OK\r\nX-Padding: "
                + b"a" * MAXIMUM_RESPONSE_HEADER_BYTES
            ),
            "many fields": b"HTTP/1.1 200 OK\r\n"
            + b"X-Field: value\r\n" * (MAXIMUM_RESPONSE_HEADER_BYTES // 16 + 1),
            "a long status line": b"HTTP/1.1 200 " + b"O" * (64 * 1024),
        }
        for name, head in cases.items():
            with self.subTest(case=name):
                transport = self.install(StubTransport(steps(send(head), hold)))

                self.assertRefusedAs(FailureKind.REASONING_MALFORMED_OUTPUT)

                self.assertLessEqual(
                    transport.connections[0].received,
                    MAXIMUM_RESPONSE_HEADER_BYTES + READ_OVERSHOOT,
                )
                self.assertAllClosed(transport)

    def test_oversized_assistant_text_is_discarded_whole(self) -> None:
        for content in ("x" * (BUDGET + 1), "é" * (BUDGET // 2) + "x"):
            with self.subTest(characters=len(content)):
                self.install(
                    StubTransport(send(http_response(chat_body(content))))
                )

                self.assertRefusedAs(FailureKind.REASONING_OUTPUT_OVER_BUDGET)


# --- Reply shape -----------------------------------------------------------


class OllamaAdapterReplyShapeTests(StubbedAdapterTestCase):
    """Prove cut-off, incomplete, and malformed replies each fail as named."""

    def test_a_response_cut_off_in_its_headers_is_truncated(self) -> None:
        for partial in (
            b"HTTP/1.",
            b"HTTP/1.1 200 OK\r\nContent-Le",
            b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n",
        ):
            with self.subTest(partial=partial):
                transport = self.install(StubTransport(send(partial)))

                self.assertRefusedAs(FailureKind.REASONING_OUTPUT_TRUNCATED)
                self.assertAllClosed(transport)

    def test_a_body_cut_short_of_its_length_is_truncated(self) -> None:
        raw = http_response(chat_body())
        self.install(StubTransport(send(raw[: len(raw) - 20])))

        self.assertRefusedAs(FailureKind.REASONING_OUTPUT_TRUNCATED)

    def test_a_chunked_body_without_its_last_chunk_is_truncated(self) -> None:
        head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        whole = chunked(chat_body(), final=False)
        for body in (whole, whole[:-5]):
            with self.subTest(length=len(body)):
                self.install(StubTransport(send(head + body)))

                self.assertRefusedAs(FailureKind.REASONING_OUTPUT_TRUNCATED)

    def test_a_connection_that_fails_midway_is_truncated(self) -> None:
        raw = http_response(chat_body())
        transport = self.install(
            StubTransport(steps(send(raw), hold), reset_after=len(raw) // 2)
        )

        failure = self.assertRefusedAs(FailureKind.REASONING_OUTPUT_TRUNCATED)

        self.assertNotIn(EXCEPTION_SENTINEL, rendered(failure))
        self.assertAllClosed(transport)

    def test_json_cut_off_inside_a_complete_body_is_malformed(self) -> None:
        # The frame says the body is whole, so a body that is not valid JSON
        # is what the runtime sent, not a reply that was cut off in transit.
        self.install(
            StubTransport(send(http_response(chat_body()[:-10])))
        )

        self.assertRefusedAs(FailureKind.REASONING_MALFORMED_OUTPUT)

    def test_an_incomplete_model_response_is_truncated(self) -> None:
        for body in (
            chat_body(done=False),
            chat_body(done=False, done_reason=ABSENT),
            chat_body(done_reason="length"),
        ):
            with self.subTest(body=body[-60:]):
                self.install(StubTransport(send(http_response(body))))

                self.assertRefusedAs(FailureKind.REASONING_OUTPUT_TRUNCATED)

    def test_done_reason_is_optional(self) -> None:
        for reason in (ABSENT, None, "stop"):
            with self.subTest(reason=reason):
                self.install(
                    StubTransport(
                        send(http_response(chat_body(done_reason=reason)))
                    )
                )

                self.assertEqual(self.ask().text, REPLY_SENTINEL)

    def test_malformed_reply_bodies_are_malformed(self) -> None:
        cases = {
            "not JSON": b"not json",
            "empty": b"",
            "an array": b"[]",
            "a string": b'"text"',
            "invalid UTF-8": chat_body("X").replace(b'"X"', b'"\xff"'),
            "a byte order mark": codecs.BOM_UTF8 + chat_body(),
            "nesting too deep to parse": b"[" * 20_000 + b"]" * 20_000,
            "an integer too long to parse": b'{"n": ' + b"9" * 5000 + b"}",
            "an error object": b'{"error": "failed"}',
            "no done": chat_body(done=ABSENT),
            "done as text": chat_body(done="true"),
            "done as a number": chat_body(done=1),
            "done_reason not text": chat_body(done_reason=7),
            "no message": chat_body(message=ABSENT),
            "message not an object": chat_body(message=[REPLY_SENTINEL]),
            "no role": chat_body(message={"content": REPLY_SENTINEL}),
            "a user role": chat_body(
                message={"role": "user", "content": REPLY_SENTINEL}
            ),
            "no content": chat_body(message={"role": "assistant"}),
            "content as a number": chat_body(42),
            "content as a list": chat_body([REPLY_SENTINEL]),
            "null content": chat_body(None),
            "empty content": chat_body(""),
            "an unpaired surrogate": chat_body("\ud800"),
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                self.install(StubTransport(send(http_response(body))))

                self.assertRefusedAs(FailureKind.REASONING_MALFORMED_OUTPUT)

    def test_malformed_http_framing_is_malformed(self) -> None:
        body = chat_body()
        length = b"Content-Length: %d" % len(body)
        cases = {
            "another protocol": b"HTTP/2 200 OK\r\n" + length,
            "not HTTP": b"ICY 200 OK\r\n" + length,
            "a short status": b"HTTP/1.1 20 OK\r\n" + length,
            "a long status": b"HTTP/1.1 2000 OK\r\n" + length,
            "a doubled space": b"HTTP/1.1  200 OK\r\n" + length,
            "a field without a colon": b"HTTP/1.1 200 OK\r\nBroken\r\n" + length,
            "a spaced field name": b"HTTP/1.1 200 OK\r\nContent Length: 5",
            "a folded field": b"HTTP/1.1 200 OK\r\n" + length + b"\r\n folded",
            "no framing": b"HTTP/1.1 200 OK\r\nX-Other: 1",
            "two lengths": b"HTTP/1.1 200 OK\r\n" + length + b"\r\n" + length,
            "a signed length": b"HTTP/1.1 200 OK\r\nContent-Length: +5",
            "a negative length": b"HTTP/1.1 200 OK\r\nContent-Length: -1",
            "a textual length": b"HTTP/1.1 200 OK\r\nContent-Length: 12abc",
            "length and chunked": b"HTTP/1.1 200 OK\r\n"
            + length
            + b"\r\nTransfer-Encoding: chunked",
            "another coding": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip",
            "stacked codings": (
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked"
            ),
            "chunked twice": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked"
            b"\r\nTransfer-Encoding: chunked",
        }
        for name, head in cases.items():
            with self.subTest(case=name):
                self.install(
                    StubTransport(send(head + b"\r\n\r\n" + body))
                )

                self.assertRefusedAs(FailureKind.REASONING_MALFORMED_OUTPUT)

    def test_malformed_chunks_are_malformed(self) -> None:
        head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        body = chat_body()
        cases = {
            "a size that is not hexadecimal": b"zz\r\n",
            "a prefixed size": b"0x10\r\n",
            "an empty size": b"\r\n",
            "data not followed by CRLF": (
                b"%x\r\n%sXX\r\n0\r\n\r\n" % (len(body), body)
            ),
        }
        for name, chunks in cases.items():
            with self.subTest(case=name):
                self.install(StubTransport(send(head + chunks)))

                self.assertRefusedAs(FailureKind.REASONING_MALFORMED_OUTPUT)


# --- Hostile provider data -------------------------------------------------


class OllamaAdapterHostileReplyTests(StubbedAdapterTestCase):
    """Prove action- or tool-shaped provider data never becomes an action."""

    def setUp(self) -> None:
        super().setUp()
        # Every route by which a reply could become an action. The adapter
        # has no import path to any of them; these prove none is reached.
        self.spies = []
        for owner, name in (
            (MariaBrain, "process"),
            (MariaBrain, "execute_confirmed_plan"),
            (Planner, "create_plan"),
            (ToolManager, "execute_tool"),
            (ToolManager, "load_tools"),
        ):
            spy = patch.object(owner, name, side_effect=AssertionError(name))
            self.spies.append(spy.start())
            self.addCleanup(spy.stop)

    def tearDown(self) -> None:
        for spy in self.spies:
            spy.assert_not_called()

    def test_tool_calls_and_thinking_are_dropped_unread(self) -> None:
        message = {
            "role": "assistant",
            "content": REPLY_SENTINEL,
            "thinking": THINKING_SENTINEL,
            "tool_calls": [
                {
                    "function": {
                        "name": "filesystem",
                        "arguments": {"action": "delete", "path": TOOL_SENTINEL},
                    }
                }
            ],
            "images": [TOOL_SENTINEL],
        }
        body = chat_body(message=message, tools=[TOOL_SENTINEL])
        self.install(StubTransport(send(http_response(body))))

        response = self.ask()

        self.assertIs(type(response.text), str)
        self.assertEqual(response.text, REPLY_SENTINEL)
        for dropped in (THINKING_SENTINEL, TOOL_SENTINEL):
            self.assertNotIn(dropped, response.text)
            self.assertNotIn(dropped, rendered(response))

    def test_action_shaped_content_comes_back_as_inert_text(self) -> None:
        for output in HostileOutput:
            if output is HostileOutput.OVERSIZED_OUTPUT:
                continue
            with self.subTest(output=output.value):
                payload = hostile_payload(output)
                self.install(
                    StubTransport(send(http_response(chat_body(payload))))
                )

                response = self.ask()

                self.assertIs(type(response.text), str)
                self.assertEqual(response.text, payload)

    def test_a_reply_holding_only_a_tool_call_is_unusable(self) -> None:
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "shell", "arguments": TOOL_SENTINEL}}
            ],
        }
        self.install(
            StubTransport(send(http_response(chat_body(message=message))))
        )

        failure = self.assertRefusedAs(FailureKind.REASONING_MALFORMED_OUTPUT)

        self.assertNotIn(TOOL_SENTINEL, rendered(failure))


# --- Privacy ---------------------------------------------------------------


class OllamaAdapterPrivacyTests(StubbedAdapterTestCase):
    """Prove no prompt, reply, destination, model, or runtime text escapes."""

    PRIVATE = (
        PROMPT_SENTINEL,
        REPLY_SENTINEL,
        MODEL_SENTINEL,
        EXCEPTION_SENTINEL,
        HEADER_SENTINEL,
        TOOL_SENTINEL,
        THINKING_SENTINEL,
        *DESTINATION_TEXT,
    )

    def setUp(self) -> None:
        super().setUp()
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

    def failing_transports(self) -> list[tuple[FailureKind, StubTransport]]:
        """Return one stub per failure path, each carrying private text."""
        private_header = b"X-Private: " + HEADER_SENTINEL.encode("ascii")
        leaking_error = json.dumps(
            {"error": EXCEPTION_SENTINEL, "model": MODEL_SENTINEL}
        ).encode("utf-8")
        with_tools = chat_body(
            "",
            message={
                "role": "assistant",
                "content": "",
                "thinking": THINKING_SENTINEL,
                "tool_calls": [{"function": {"name": TOOL_SENTINEL}}],
            },
        )
        return [
            (
                FailureKind.REASONING_UNAVAILABLE,
                StubTransport(
                    connect_error=ConnectionRefusedError(EXCEPTION_SENTINEL)
                ),
            ),
            (
                FailureKind.REASONING_UNAVAILABLE,
                StubTransport(
                    send(
                        http_response(
                            leaking_error,
                            status=b"500 Internal Server Error",
                            fields=[private_header],
                        )
                    )
                ),
            ),
            (
                FailureKind.REASONING_MALFORMED_OUTPUT,
                StubTransport(
                    send(
                        http_response(
                            REPLY_SENTINEL.encode("ascii") + b"{",
                            fields=[private_header],
                        )
                    )
                ),
            ),
            (
                FailureKind.REASONING_MALFORMED_OUTPUT,
                StubTransport(send(http_response(with_tools))),
            ),
            (
                FailureKind.REASONING_OUTPUT_OVER_BUDGET,
                StubTransport(
                    send(
                        http_response(
                            chat_body(REPLY_SENTINEL + "x" * BUDGET)
                        )
                    )
                ),
            ),
            (
                FailureKind.REASONING_OUTPUT_TRUNCATED,
                StubTransport(
                    send(http_response(chat_body(done_reason="length")))
                ),
            ),
            (
                FailureKind.REASONING_OUTPUT_TRUNCATED,
                StubTransport(
                    steps(send(http_response(chat_body())), hold),
                    reset_after=80,
                ),
            ),
        ]

    def test_failures_carry_no_private_text_or_chained_exception(
        self,
    ) -> None:
        for kind, transport in self.failing_transports():
            with self.subTest(kind=kind.value):
                self.install(transport)

                failure = self.assertRefusedAs(kind)

                shown = " ".join(
                    (rendered(failure), rendered(failure.failure))
                    + tuple(rendered(argument) for argument in failure.args)
                )
                for private in self.PRIVATE:
                    self.assertNotIn(private, shown)
                self.assertEqual(str(failure), Failure(kind).message)

    def test_success_and_failure_log_outcomes_and_nothing_private(
        self,
    ) -> None:
        configure_logging(self.log_dir, level="DEBUG")
        settings = adapter_settings()
        provider = OllamaReasoningProvider(settings)
        request = ReasoningRequest(PROMPT_SENTINEL)

        self.install(StubTransport(send(http_response(chat_body()))))
        answered = request_reasoning(provider, request, settings)
        self.assertEqual(answered.text, REPLY_SENTINEL)

        outcomes = []
        for kind, transport in self.failing_transports():
            self.install(transport)
            outcomes.append((kind, request_reasoning(provider, request, settings)))

        short = adapter_settings(deadline=SHORT_DEADLINE)
        self.install(StubTransport(hang_connect=True))
        outcomes.append(
            (
                FailureKind.REASONING_TIMEOUT,
                request_reasoning(
                    OllamaReasoningProvider(short),
                    request,
                    short,
                ),
            )
        )
        for kind, outcome in outcomes:
            self.assertEqual(outcome, Failure(kind))

        log_file = self.log_dir / "maria.log"
        for handler in self.maria_logger.handlers:
            handler.flush()
        log_text = log_file.read_text(encoding="utf-8")

        # Positive controls: the boundary did log, so an empty log cannot
        # pass for a private one.
        self.assertIn("Reasoning request completed.", log_text)
        for kind, _ in outcomes:
            self.assertIn(f"code={kind.value}", log_text)

        assert_log_omits(self, log_file, self.PRIVATE)


# --- Network import boundary -----------------------------------------------

# Modules whose own import would give a module a network capability.
NETWORK_MODULES = frozenset(
    {
        "socket",
        "_socket",
        "ssl",
        "_ssl",
        "http",
        "urllib",
        "urllib3",
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

# Modules whose own import would give a module threads, asynchronous tasks,
# or other processes.
THREADING_MODULES = frozenset(
    {
        "threading",
        "_thread",
        "concurrent",
        "asyncio",
        "multiprocessing",
        "subprocess",
        "_posixsubprocess",
    }
)

FORBIDDEN_MODULES = NETWORK_MODULES | THREADING_MODULES

ADAPTER_MODULE = "core.ollama_adapter"
ADAPTER_PATH = PROJECT_ROOT / "core" / "ollama_adapter.py"

# The one network module the adapter may import, and nothing else from the
# forbidden set: no HTTP library, which would bring proxy and redirect
# handling, and no thread, task, or process.
ADAPTER_NETWORK_IMPORTS = frozenset({"socket"})

# Every Maria module the adapter may reach, directly or not: the failure
# vocabulary, the reasoning interface, and what that interface itself uses.
ADAPTER_REACH = frozenset(
    {
        "core",
        ADAPTER_MODULE,
        "core.failures",
        "core.reasoning",
        "core.logger",
        "core.constants",
    }
)

# The core modules the roadmap names. Every production tool is added to
# these, found rather than listed.
GUARDED_CORE_MODULES = (
    "core.brain",
    "core.planner",
    "core.permissions",
    "core.tool_manager",
    "core.memory",
    "core.config",
)

PRODUCTION_PACKAGES = ("core", "tools")
PRODUCTION_ENTRY_POINTS = ("main.py",)

# The one module that imports others by a name held in a variable, and the
# names it can import that way. Any other such import fails the guard.
KNOWN_DYNAMIC_IMPORTS = {"core.tool_manager": frozenset(BUILT_IN_TOOL_MODULES)}

DYNAMIC_IMPORT_FUNCTIONS = frozenset({"import_module", "__import__"})


def production_modules(root: Path = PROJECT_ROOT) -> list[Path]:
    """Return every production module at any depth, found rather than listed."""
    modules = [
        root / name for name in PRODUCTION_ENTRY_POINTS if (root / name).is_file()
    ]
    for package in PRODUCTION_PACKAGES:
        for current, directories, files in os.walk(root / package):
            directories[:] = [
                name
                for name in directories
                if name != "__pycache__" and not name.startswith(".")
            ]
            modules.extend(
                Path(current) / name for name in files if name.endswith(".py")
            )
    return sorted(modules)


def module_name(path: Path, root: Path = PROJECT_ROOT) -> str:
    """Return the dotted name a production module is imported by."""
    parts = path.relative_to(root).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def module_path(name: str, root: Path = PROJECT_ROOT) -> Path | None:
    """Return the production file a dotted name imports, or None if none."""
    parts = name.split(".")
    if name == "main":
        return root / "main.py"
    if parts[0] not in PRODUCTION_PACKAGES:
        return None
    for candidate in (
        root.joinpath(*parts).with_suffix(".py"),
        root.joinpath(*parts, "__init__.py"),
    ):
        if candidate.is_file():
            return candidate
    return None


def imports_of(
    path: Path,
    root: Path = PROJECT_ROOT,
) -> tuple[frozenset[str], bool]:
    """Return every module a source imports itself, at any depth.

    Statements anywhere count, including inside functions; relative imports
    are resolved against the module's package; and a dynamic import whose
    name is a literal counts as that module. The flag says whether a
    dynamic import names its module some other way, which this cannot follow.
    Nothing is imported or run to find any of this out.
    """
    package = path.relative_to(root).with_suffix("").parts[:-1]
    names: set[str] = set()
    unresolved = False
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level > len(package):
                    continue
                base_parts = package[: len(package) - node.level + 1]
                base = ".".join(
                    (*base_parts, *(node.module.split(".") if node.module else ()))
                )
            else:
                base = node.module or ""
            names.add(base)
            names.update(
                f"{base}.{alias.name}" for alias in node.names if alias.name != "*"
            )
        elif isinstance(node, ast.Call) and _is_dynamic_import(node):
            argument = _first_argument(node)
            if isinstance(argument, ast.Constant) and isinstance(
                argument.value, str
            ):
                names.add(argument.value)
            else:
                unresolved = True
    return frozenset(names), unresolved


def _is_dynamic_import(node: ast.Call) -> bool:
    """Return whether a call is recognisably a dynamic import."""
    if isinstance(node.func, ast.Name):
        return node.func.id in DYNAMIC_IMPORT_FUNCTIONS
    if isinstance(node.func, ast.Attribute):
        return node.func.attr in DYNAMIC_IMPORT_FUNCTIONS
    return False


def _first_argument(node: ast.Call) -> ast.expr | None:
    """Return the module-name argument of a dynamic import call."""
    if node.args:
        return node.args[0]
    for keyword in node.keywords:
        if keyword.arg == "name":
            return keyword.value
    return None


def forbidden_in(path: Path, root: Path = PROJECT_ROOT) -> set[str]:
    """Return the forbidden top-level modules a source imports itself."""
    names, _ = imports_of(path, root)
    return {
        name.split(".")[0]
        for name in names
        if name.split(".")[0] in FORBIDDEN_MODULES
    }


def reachable_modules(
    start: str,
    root: Path = PROJECT_ROOT,
) -> tuple[dict[str, Path], set[str]]:
    """Return every Maria module ``start`` can import, at any depth.

    The walk follows each module's own imports, and the imports of those, to
    the end, so a forbidden import two modules away is found as surely as
    one in ``start`` itself. Importing ``a.b.c`` imports ``a`` and ``a.b``
    as well. Also returned are the modules whose dynamic imports could not
    be followed and are not in ``KNOWN_DYNAMIC_IMPORTS``.
    """
    reached: dict[str, Path] = {}
    unfollowed: set[str] = set()
    pending = [start]
    while pending:
        name = pending.pop()
        path = module_path(name, root)
        if path is None or name in reached:
            continue
        reached[name] = path
        names, unresolved = imports_of(path, root)
        if unresolved:
            if name in KNOWN_DYNAMIC_IMPORTS:
                names = names | KNOWN_DYNAMIC_IMPORTS[name]
            else:
                unfollowed.add(name)
        for imported in names:
            parts = imported.split(".")
            pending.extend(".".join(parts[:end]) for end in range(1, len(parts) + 1))
    return reached, unfollowed


def guarded_module_names(root: Path = PROJECT_ROOT) -> list[str]:
    """Return the roadmap's guarded core modules and every production tool."""
    tools = [
        module_name(path, root)
        for path in production_modules(root)
        if path.relative_to(root).parts[0] == "tools"
    ]
    return [*GUARDED_CORE_MODULES, *tools]


class NetworkImportBoundaryTests(unittest.TestCase):
    """Guard which production modules can reach a network or a thread."""

    def test_only_the_adapter_imports_a_network_or_threading_module(
        self,
    ) -> None:
        owners = {
            module_name(path): found
            for path in production_modules()
            if (found := forbidden_in(path))
        }

        self.assertEqual(owners, {ADAPTER_MODULE: set(ADAPTER_NETWORK_IMPORTS)})

    def test_guarded_modules_reach_no_network_or_threading_module(
        self,
    ) -> None:
        for name in guarded_module_names():
            with self.subTest(module=name):
                reached, unfollowed = reachable_modules(name)

                self.assertIn(name, reached)
                self.assertEqual(unfollowed, set())
                self.assertNotIn(ADAPTER_MODULE, reached)
                violations = {
                    f"{reached_name}: {sorted(found)}"
                    for reached_name, path in reached.items()
                    if (found := forbidden_in(path))
                }
                self.assertEqual(violations, set())

    def test_every_production_tool_is_guarded(self) -> None:
        guarded = set(guarded_module_names())

        self.assertLessEqual(set(BUILT_IN_TOOL_MODULES), guarded)
        self.assertLessEqual(set(GUARDED_CORE_MODULES), guarded)
        # ToolManager's dynamic loading is followed, so its reach includes
        # every built-in tool as well.
        reached, _ = reachable_modules("core.tool_manager")
        self.assertLessEqual(set(BUILT_IN_TOOL_MODULES), set(reached))

    def test_the_adapter_reaches_only_failures_and_the_reasoning_interface(
        self,
    ) -> None:
        reached, unfollowed = reachable_modules(ADAPTER_MODULE)

        self.assertEqual(set(reached), set(ADAPTER_REACH))
        self.assertEqual(unfollowed, set())

    def test_no_production_module_imports_the_adapter_yet(self) -> None:
        """V1-13 wires the adapter into nothing; a later stage changes this."""
        for path in production_modules():
            name = module_name(path)
            if name == ADAPTER_MODULE:
                continue
            with self.subTest(module=name):
                reached, _ = reachable_modules(name)
                source = path.read_text(encoding="utf-8")

                self.assertNotIn(ADAPTER_MODULE, reached)
                self.assertNotIn("ollama_adapter", source)
                self.assertNotIn("OllamaReasoningProvider", source)

    def test_the_guard_follows_nested_relative_dynamic_and_transitive_imports(
        self,
    ) -> None:
        """Positive control, so the guards cannot pass by seeing nothing."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = {
                "main.py": "",
                "core/__init__.py": "",
                "core/brain.py": "from core import helper\n",
                "core/helper.py": (
                    "def later():\n    from .deeper import probe\n"
                ),
                "core/deeper/__init__.py": "",
                "core/deeper/probe.py": (
                    "import importlib\nimportlib.import_module('core.wire')\n"
                ),
                "core/wire.py": "def later():\n    import socket\n",
                "core/loader.py": (
                    "import importlib\n"
                    "def load(name):\n"
                    "    return importlib.import_module(name)\n"
                ),
                "tools/__init__.py": "",
                "tools/nested/__init__.py": "",
                "tools/nested/sneaky.py": "from concurrent import futures\n",
            }
            for relative, text in sources.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")

            reached, unfollowed = reachable_modules("core.brain", root)
            self.assertLessEqual(
                {"core.helper", "core.deeper.probe", "core.wire"},
                set(reached),
            )
            self.assertEqual(unfollowed, set())
            self.assertEqual(forbidden_in(reached["core.wire"], root), {"socket"})

            self.assertEqual(reachable_modules("core.loader", root)[1], {"core.loader"})
            self.assertIn("tools.nested.sneaky", guarded_module_names(root))
            self.assertEqual(
                forbidden_in(root / "tools" / "nested" / "sneaky.py", root),
                {"concurrent"},
            )


if __name__ == "__main__":
    unittest.main()
