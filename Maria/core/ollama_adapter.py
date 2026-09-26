"""Maria's one local reasoning adapter: Ollama, over loopback HTTP.

This is the owner-selected runtime behind ``ReasoningProvider`` (decision
V1-13-R1): Ollama's native HTTP API on the owner's own machine, reached only
at the fixed loopback destination ``127.0.0.1:11434`` with one
``POST /api/chat`` request whose ``stream`` is false. Everything specific to
Ollama lives here, and this is the only production module that imports a
network module. Nothing in Maria constructs it yet, so Brain, the CLI,
planning, memory, and tools cannot reach it.

The destination is fixed here rather than configured. Settings must name the
loopback transport and the host ``127.0.0.1``, or the adapter refuses to
exist; the port and path are constants of this module and of nothing else.
The address is a numeric literal handed straight to an IPv4 socket, so
nothing is resolved, no proxy setting is consulted, no redirect is followed,
and no URL is ever built or parsed. The model identifier is only ever a JSON
string value in the request body.

Only the model identifier and the prompt, as one user message, are sent.
Only the assistant message's ``content`` comes back, as one inert string.
Thinking text, tool calls, and every other field of the reply are ignored
after bounded JSON parsing; only ``message.content`` is returned. This module
has no import path to planning, permissions, tool dispatch, or persistence,
so nothing here can turn a reply into an action.

One absolute monotonic deadline covers the whole exchange: connecting,
sending, the response headers, and the body. Every socket wait is given only
the time that remains, so a runtime that trickles bytes cannot stretch it.
The headers and the whole response envelope are bounded as they are read,
before any parsing, and the extracted answer is then held to the configured
output budget. Nothing oversized is read in full, and nothing is truncated or
returned in part.

Every outcome other than a complete reply is one typed reasoning failure,
raised outside any exception handler, so no caught exception travels with it
as a cause or a context. Nothing here logs; ``request_reasoning`` records the
outcome code. No prompt, reply, host, model identifier, endpoint, header, or
runtime error text reaches an exception message or a log.

Ollama's own local-only setting is separate from all of this and is the
owner's to configure. Refusing model identifiers that plainly designate an
Ollama cloud model is defense in depth only: it cannot prove the runtime is
not configured to reach one.
"""

from __future__ import annotations

import json
import re
import socket
import time
from dataclasses import replace
from typing import NoReturn

from core.failures import Failure, FailureKind, TypedFailureError
from core.reasoning import (
    REASONING_PROMPT_MESSAGE,
    REASONING_REQUEST_TYPE_MESSAGE,
    REASONING_SETTINGS_TYPE_MESSAGE,
    ReasoningRequest,
    ReasoningResponse,
    ReasoningSettings,
    ReasoningTransport,
)


# The one destination this adapter will ever contact: Ollama's default
# listener on the IPv4 loopback address. None of it is configurable.
OLLAMA_HOST = "127.0.0.1"
OLLAMA_PORT = 11434
OLLAMA_ADDRESS = (OLLAMA_HOST, OLLAMA_PORT)
OLLAMA_CHAT_PATH = "/api/chat"

# The most the status line and header fields may take together, blank line
# included. Ollama's own come to a few hundred bytes.
MAXIMUM_RESPONSE_HEADER_BYTES = 16 * 1024

# Room in a response body for everything that is not the answer: metadata,
# timing counters, chunk framing, and any thinking text or tool calls, all of
# which are parsed within this bound and then ignored. A reply that needs more
# is refused as too large.
RESPONSE_ENVELOPE_OVERHEAD_BYTES = 64 * 1024

# JSON may spell one byte of the answer as up to six: Ollama's encoder writes
# ``<``, ``>``, ``&``, and control characters as ``\u00XX`` escapes. The
# envelope allows for that, so an answer within budget always fits.
JSON_ESCAPE_EXPANSION = 6

# The most one wait on the socket asks for. It bounds a single read, not the
# response; the allowances below bound the response.
_RECEIVE_BYTES = 64 * 1024

# Both messages are fixed and name no host, port, path, or model.
OLLAMA_DESTINATION_MESSAGE = (
    "The Ollama adapter needs enabled reasoning settings naming the loopback "
    "transport and its one fixed loopback host."
)
OLLAMA_CLOUD_MODEL_MESSAGE = (
    "The Ollama adapter refuses a model identifier that designates an Ollama "
    "cloud model."
)

_STATUS_LINE = re.compile(rb"HTTP/1\.[01] ([0-9]{3})(?: [^\r\n]*)?")
_FIELD_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_CONTENT_LENGTH = re.compile(rb"[0-9]{1,19}")
_CHUNK_SIZE = re.compile(rb"[0-9A-Fa-f]{1,16}")


class OllamaReasoningProvider:
    """The ``ReasoningProvider`` for the owner's local Ollama runtime.

    Construction settles everything the adapter will use, once: the settings
    must be exact, valid, enabled, loopback, and ``127.0.0.1``, and the model
    must not designate a cloud model, or ``ValueError`` is raised with a
    fixed message before any connection could exist. ``respond`` then opens
    one connection per request and never retries or falls back.
    """

    def __init__(self, settings: ReasoningSettings) -> None:
        """Refuse any settings this adapter cannot honour exactly."""
        if type(settings) is not ReasoningSettings:
            raise ValueError(REASONING_SETTINGS_TYPE_MESSAGE)
        # Rebuilding re-runs the settings' own validation, because a frozen
        # dataclass can still be altered through ``object.__setattr__``, and
        # it leaves this adapter holding values no later change can reach.
        settled = replace(settings)
        if (
            not settled.enabled
            or settled.transport is not ReasoningTransport.LOOPBACK
            or settled.host != OLLAMA_HOST
        ):
            raise ValueError(OLLAMA_DESTINATION_MESSAGE)
        if _is_cloud_model(settled.model):
            raise ValueError(OLLAMA_CLOUD_MODEL_MESSAGE)

        self._model = settled.model
        self._deadline_seconds = settled.request_deadline_seconds
        self._max_input_bytes = settled.max_input_bytes
        self._max_output_bytes = settled.max_output_bytes
        self._envelope_bytes = (
            RESPONSE_ENVELOPE_OVERHEAD_BYTES
            + JSON_ESCAPE_EXPANSION * settled.max_output_bytes
        )

    def __repr__(self) -> str:
        """Name the type without showing the model or any budget."""
        return "OllamaReasoningProvider(<redacted>)"

    def respond(self, request: ReasoningRequest) -> ReasoningResponse:
        """Ask the local runtime one question and return its complete answer.

        Raises ``TypedFailureError`` carrying one reasoning kind: input over
        budget before any connection is opened; timeout when the deadline
        passes at any stage; unavailable when nothing answers, or the answer
        is not success; truncated when the reply ends early or says it is
        incomplete; malformed when it is not the expected shape; and output
        over budget when the response or the answer is too large.
        """
        if type(request) is not ReasoningRequest:
            raise ValueError(REASONING_REQUEST_TYPE_MESSAGE)
        outcome = self._exchange(request)
        if type(outcome) is Failure:
            # Raised here, outside every handler, so no caught exception is
            # attached to it as a cause or a context.
            raise TypedFailureError(outcome)
        return outcome

    def _exchange(
        self,
        request: ReasoningRequest,
    ) -> ReasoningResponse | Failure:
        """Run one bounded exchange and settle it as a reply or a failure."""
        deadline = time.monotonic() + self._deadline_seconds

        # The prompt is read again rather than trusted from construction,
        # and measured without being copied when it is plainly too long.
        prompt = request.prompt
        size = _utf8_size(prompt, self._max_input_bytes)
        if size is None or size == 0:
            raise ValueError(REASONING_PROMPT_MESSAGE)
        if size > self._max_input_bytes:
            return Failure(FailureKind.REASONING_INPUT_OVER_BUDGET)

        try:
            body = self._converse(self._request_bytes(prompt), deadline)
        except TypedFailureError as stopped:
            return stopped.failure
        return _answer(body, self._max_output_bytes)

    def _request_bytes(self, prompt: str) -> bytes:
        """Return the one HTTP request: the model and the prompt, and no more.

        There is no system message, template, context, option, tool, or
        thinking request, and no credential or proxy header. The request
        line names only the path, never a scheme or host.
        """
        body = json.dumps(
            {
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        head = (
            f"POST {OLLAMA_CHAT_PATH} HTTP/1.1\r\n"
            f"Host: {OLLAMA_HOST}:{OLLAMA_PORT}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        return head.encode("ascii") + body

    def _converse(self, message: bytes, deadline: float) -> bytes:
        """Send one request and return the complete, bounded response body."""
        connection = _connect(deadline)
        try:
            _send(connection, message, deadline)
            return _ResponseReader(connection, deadline).body(
                self._envelope_bytes
            )
        finally:
            connection.close()


class _ResponseReader:
    """Read one HTTP/1.1 response, bounded and within the deadline.

    Every byte taken is charged to an allowance, and running past it fails
    as the kind that part of the response names: oversized headers are
    malformed, and an oversized body is over budget. Neither is ever read
    without bound.
    """

    def __init__(self, connection: socket.socket, deadline: float) -> None:
        self._connection = connection
        self._deadline = deadline
        self._pending = bytearray()
        self._started = False
        self._allowance = 0
        self._excess = FailureKind.REASONING_MALFORMED_OUTPUT

    def body(self, envelope_bytes: int) -> bytes:
        """Return the body of a successful, explicitly framed response.

        Only ``200`` carries an answer. Any other status, a redirect
        included, means the runtime cannot answer this request, and is
        reported as unavailable without reading further. The body must be
        framed by exactly one ``Content-Length`` or by chunked transfer
        coding, never both and never neither, so a response that ends early
        can always be told from a complete one.
        """
        self._allow(
            MAXIMUM_RESPONSE_HEADER_BYTES,
            FailureKind.REASONING_MALFORMED_OUTPUT,
        )
        status = _STATUS_LINE.fullmatch(self._line())
        if status is None:
            _fail(FailureKind.REASONING_MALFORMED_OUTPUT)
        if status.group(1) != b"200":
            _fail(FailureKind.REASONING_UNAVAILABLE)
        lengths, codings = self._framing_fields()

        self._allow(envelope_bytes, FailureKind.REASONING_OUTPUT_OVER_BUDGET)
        if codings:
            if lengths or codings != [b"chunked"]:
                _fail(FailureKind.REASONING_MALFORMED_OUTPUT)
            return self._chunked()
        if len(lengths) != 1 or not _CONTENT_LENGTH.fullmatch(lengths[0]):
            _fail(FailureKind.REASONING_MALFORMED_OUTPUT)
        return self._take(int(lengths[0]))

    def _framing_fields(self) -> tuple[list[bytes], list[bytes]]:
        """Read every header field, keeping the two that frame the body."""
        lengths: list[bytes] = []
        codings: list[bytes] = []
        while line := self._line():
            name, colon, value = line.partition(b":")
            if not colon or not _FIELD_NAME.fullmatch(name):
                _fail(FailureKind.REASONING_MALFORMED_OUTPUT)
            field = name.lower()
            if field == b"content-length":
                lengths.append(value.strip(b" \t"))
            elif field == b"transfer-encoding":
                codings.append(value.strip(b" \t").lower())
        return lengths, codings

    def _chunked(self) -> bytes:
        """Reassemble a chunked body, charging its framing to the allowance."""
        body = bytearray()
        while True:
            size = self._line().partition(b";")[0].strip(b" \t")
            if not _CHUNK_SIZE.fullmatch(size):
                _fail(FailureKind.REASONING_MALFORMED_OUTPUT)
            if int(size, 16) == 0:
                break
            body += self._take(int(size, 16))
            if self._line():
                _fail(FailureKind.REASONING_MALFORMED_OUTPUT)
        # Trailer fields are read, charged, and ignored up to the blank line.
        while self._line():
            pass
        return bytes(body)

    def _allow(self, allowance: int, excess: FailureKind) -> None:
        """Start charging a new part of the response to its own allowance."""
        self._allowance = allowance
        self._excess = excess

    def _line(self) -> bytes:
        """Take one CRLF-terminated line and return it without the CRLF."""
        searched = 0
        while (end := self._pending.find(b"\r\n", searched)) < 0:
            if len(self._pending) >= self._allowance:
                _fail(self._excess)
            searched = max(0, len(self._pending) - 1)
            self._receive(_RECEIVE_BYTES)
        return self._take(end + 2)[:-2]

    def _take(self, size: int) -> bytes:
        """Take exactly ``size`` bytes, refusing any past the allowance."""
        if size > self._allowance:
            _fail(self._excess)
        while len(self._pending) < size:
            self._receive(size - len(self._pending))
        self._allowance -= size
        taken = bytes(self._pending[:size])
        del self._pending[:size]
        return taken

    def _receive(self, wanted: int) -> None:
        """Wait, no longer than the deadline allows, for more of the response.

        A response that ends, or a connection that fails, before its first
        byte means nothing answered; after it, the response was cut short.
        """
        timeout = _remaining(self._deadline)
        try:
            self._connection.settimeout(timeout)
            data = self._connection.recv(min(wanted, _RECEIVE_BYTES))
        except TimeoutError:
            _fail(FailureKind.REASONING_TIMEOUT)
        except OSError:
            _fail(self._cut_short())
        if not data:
            _fail(self._cut_short())
        self._started = True
        self._pending += data

    def _cut_short(self) -> FailureKind:
        """Name a response that ended before it was complete."""
        if self._started:
            return FailureKind.REASONING_OUTPUT_TRUNCATED
        return FailureKind.REASONING_UNAVAILABLE


def _open_connection(
    address: tuple[str, int],
    timeout: float,
) -> socket.socket:
    """Open one TCP connection to a numeric IPv4 address within the timeout.

    The socket is created for IPv4 and handed the address literal directly,
    so nothing is resolved and no proxy setting is consulted. A socket that
    fails to connect is closed before the error propagates.
    """
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(address)
    except BaseException:
        connection.close()
        raise
    return connection


def _connect(deadline: float) -> socket.socket:
    """Open the one connection an exchange uses, within the deadline."""
    timeout = _remaining(deadline)
    try:
        return _open_connection(OLLAMA_ADDRESS, timeout)
    except TimeoutError:
        _fail(FailureKind.REASONING_TIMEOUT)
    except OSError:
        _fail(FailureKind.REASONING_UNAVAILABLE)


def _send(connection: socket.socket, message: bytes, deadline: float) -> None:
    """Send the whole request, within the deadline.

    A socket timeout bounds the whole of ``sendall``, not each piece of it,
    so a runtime that accepts bytes slowly cannot stretch the wait either.
    """
    timeout = _remaining(deadline)
    try:
        connection.settimeout(timeout)
        connection.sendall(message)
    except TimeoutError:
        _fail(FailureKind.REASONING_TIMEOUT)
    except OSError:
        _fail(FailureKind.REASONING_UNAVAILABLE)


def _remaining(deadline: float) -> float:
    """Return the seconds left before the deadline, or fail as a timeout."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _fail(FailureKind.REASONING_TIMEOUT)
    return remaining


def _fail(kind: FailureKind) -> NoReturn:
    """Stop the exchange with one typed reasoning failure."""
    raise TypedFailureError(Failure(kind))


def _answer(body: bytes, max_output_bytes: int) -> ReasoningResponse | Failure:
    """Settle a complete response body as the answer, or say why it cannot be.

    The body must be one UTF-8 JSON object saying ``done`` is exactly true,
    with an assistant ``message`` whose ``content`` is non-empty text within
    the output budget. ``done_reason`` is optional; when present it must be
    text, and ``length`` means the model stopped before finishing. Nothing
    but ``content`` is taken, and it is returned exactly as received.
    """
    try:
        document = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        # Unbound on purpose: a decoder's message can quote the reply.
        return Failure(FailureKind.REASONING_MALFORMED_OUTPUT)
    if type(document) is not dict:
        return Failure(FailureKind.REASONING_MALFORMED_OUTPUT)

    done = document.get("done")
    reason = document.get("done_reason")
    if type(done) is not bool:
        return Failure(FailureKind.REASONING_MALFORMED_OUTPUT)
    if reason is not None and type(reason) is not str:
        return Failure(FailureKind.REASONING_MALFORMED_OUTPUT)
    if not done or reason == "length":
        return Failure(FailureKind.REASONING_OUTPUT_TRUNCATED)

    message = document.get("message")
    if type(message) is not dict or message.get("role") != "assistant":
        return Failure(FailureKind.REASONING_MALFORMED_OUTPUT)
    content = message.get("content")
    size = _utf8_size(content, max_output_bytes)
    if size is None or size == 0:
        return Failure(FailureKind.REASONING_MALFORMED_OUTPUT)
    if size > max_output_bytes:
        return Failure(FailureKind.REASONING_OUTPUT_OVER_BUDGET)
    return ReasoningResponse(content)


def _utf8_size(text: object, maximum_bytes: int) -> int | None:
    """Measure text against a byte bound without copying oversized text.

    Returns None for anything but exact, encodable text. Every encodable
    character takes at least one UTF-8 byte, so text with more characters
    than the bound is over it and is never encoded; the result is then
    ``maximum_bytes + 1``, a value callers only compare against the bound.
    """
    if type(text) is not str:
        return None
    if len(text) > maximum_bytes:
        return maximum_bytes + 1
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        # The error would name the offending character, so it is dropped.
        return None


def _is_cloud_model(model: str) -> bool:
    """Return whether a model identifier plainly names an Ollama cloud model.

    Ollama marks its cloud models by tag, as ``name:cloud`` or
    ``name:size-cloud`` (``gpt-oss:120b-cloud``, for example). The tag is the
    text after the last colon of the final path segment, ignoring any ``@``
    digest, compared without regard to case.
    """
    segment = model.partition("@")[0].rpartition("/")[2].lower()
    _, colon, tag = segment.rpartition(":")
    return bool(colon) and (tag == "cloud" or tag.endswith("-cloud"))
