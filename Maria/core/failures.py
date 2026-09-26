"""Shared typed failures for Maria's internal boundaries.

A boundary that cannot complete an operation has to say so without handing the
owner, or the operational log, whatever the underlying failure happened to
carry. This module supplies the small vocabulary for that: a closed set of
failure kinds, one immutable value object holding a kind and nothing else, and
one exception that carries such a value for a boundary that must stop rather
than return.

What the object deliberately cannot hold is the point. There is no field for a
request, a response, an arguments mapping, an exception, its text, a path, an
endpoint, a credential, a header, or any other free-form payload, so no call
site can attach one by accident. The value is carried in a tuple and the type
is closed to subclassing, so neither an instance dictionary, nor a writable
attribute, nor a derived type can smuggle one in either, and no later reader
can be tempted to format one into a message or a log record. Each kind's
owner-visible text is fixed here and interpolates nothing, which also keeps
that wording out of the modules that report it.

The module is deliberately self-contained: it imports nothing from Maria, logs
nothing, and touches no file, so any boundary may depend on it without
creating a cycle or a side effect. Domain-specific failures that already carry
their own names stay where they are; ``ContractFailure`` in
``core.action_contracts`` remains the action-contract vocabulary.
"""

from __future__ import annotations

from collections import namedtuple
from collections.abc import Iterable, Mapping
from enum import Enum
from types import MappingProxyType
from typing import NoReturn


class FailureKind(str, Enum):
    """Why an internal boundary could not complete an operation."""

    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    DATABASE_SCHEMA_TOO_NEW = "database_schema_too_new"

    # Why a request to the owner's local reasoning model produced no usable
    # reply. The two budget kinds are separate because they happen at
    # different moments: an oversized request is refused before the provider
    # is asked anything, while an oversized reply is discarded afterwards.
    REASONING_UNAVAILABLE = "reasoning_unavailable"
    REASONING_TIMEOUT = "reasoning_timeout"
    REASONING_REFUSED = "reasoning_refused"
    REASONING_MALFORMED_OUTPUT = "reasoning_malformed_output"
    REASONING_OUTPUT_TRUNCATED = "reasoning_output_truncated"
    REASONING_INPUT_OVER_BUDGET = "reasoning_input_over_budget"
    REASONING_OUTPUT_OVER_BUDGET = "reasoning_output_over_budget"


# The one owner-visible sentence each kind is allowed to produce. The text is
# fixed per kind and interpolates nothing, so a message can never widen to
# carry a caller's value or an underlying failure's text.
_FAILURE_MESSAGES: Mapping[FailureKind, str] = MappingProxyType(
    {
        FailureKind.TOOL_EXECUTION_FAILED: "Tool execution failed.",
        FailureKind.DATABASE_SCHEMA_TOO_NEW: (
            "This database was written by a newer version of Maria and "
            "cannot be opened by this one."
        ),
        FailureKind.REASONING_UNAVAILABLE: (
            "The local reasoning model is unavailable."
        ),
        FailureKind.REASONING_TIMEOUT: (
            "The local reasoning model did not respond in time."
        ),
        FailureKind.REASONING_REFUSED: (
            "The local reasoning model refused the request."
        ),
        FailureKind.REASONING_MALFORMED_OUTPUT: (
            "The local reasoning model returned an unusable response."
        ),
        FailureKind.REASONING_OUTPUT_TRUNCATED: (
            "The local reasoning model returned an incomplete response."
        ),
        FailureKind.REASONING_INPUT_OVER_BUDGET: (
            "The request is too large for the local reasoning model."
        ),
        FailureKind.REASONING_OUTPUT_OVER_BUDGET: (
            "The local reasoning model's response was too large and was "
            "discarded."
        ),
    }
)


# The tuple the value object is built on. A frozen dataclass is not enough
# here: its field is a writable slot, and ``object.__setattr__`` reaches past
# the frozen guard to replace it, so a constructed failure's kind could still
# be swapped for an arbitrary object afterwards. A tuple's contents are fixed
# by the interpreter once it exists, which leaves no such descriptor to write
# through.
_FailureBase = namedtuple("Failure", ("kind",))


class Failure(_FailureBase):
    """One typed failure, carrying its kind and nothing else.

    The emptiness is structural rather than a convention. The kind lives in
    the underlying tuple, so there is no instance dictionary to write an
    undeclared field into and no writable attribute to replace the kind
    with, by ordinary assignment or by stepping around it with
    ``object.__setattr__``. Subclassing is refused outright, because a
    subtype could otherwise declare the payload field this deliberately
    lacks. A new failure is added by extending ``FailureKind`` and its
    message table, never by deriving from this class.

    Equality and representation follow from the kind alone, so two failures of
    the same kind are interchangeable and a failure's repr is safe to log.
    Because the kind cannot drift after construction, a failure also stays
    findable in the set or mapping it was put into. Constructing one has no
    side effect: nothing is written, recorded, or logged here, and the caller
    decides what to do with it.
    """

    __slots__ = ()

    def __new__(cls, kind: FailureKind) -> Failure:
        """Settle the kind by exact type before the value exists.

        The check goes through ``type()`` rather than ``isinstance``, so a
        plain string that merely equals a member's value cannot impersonate
        one and reach the message table. It runs here rather than after
        construction because a tuple cannot be corrected once built.
        """
        if type(kind) is not FailureKind:
            raise ValueError("A failure kind must be a FailureKind member.")
        return super().__new__(cls, kind)

    def __init_subclass__(cls, **kwargs: object) -> NoReturn:
        """Refuse every subclass, so no subtype can add a payload field.

        The refusal repeats nothing it was handed: the rejected class, its
        bases, and its keyword arguments are never interpolated or repr'd
        into the message, so the guard itself cannot become the leak.
        """
        raise TypeError(
            "Failure may not be subclassed; add a FailureKind member instead."
        )

    @classmethod
    def _make(cls, iterable: Iterable[FailureKind]) -> Failure:
        """Build one from an iterable through the validating constructor.

        The inherited version calls ``tuple.__new__`` directly, which would
        step around the kind check. Routing it back through ``cls`` keeps
        every construction path validated, including ``_replace``, which
        builds its result through this method.
        """
        return cls(*iterable)

    @property
    def code(self) -> str:
        """Return the stable identifier a log line may record for this kind."""
        return self.kind.value

    @property
    def message(self) -> str:
        """Return the fixed owner-visible message for this kind."""
        return _FAILURE_MESSAGES[self.kind]


class TypedFailureError(Exception):
    """An exception whose whole meaning is the one typed failure it carries.

    A boundary that has to stop rather than return a value needs a way to say
    which failure stopped it without the exception itself becoming the leak an
    ordinary ``raise RuntimeError(detail)`` would be. This carries a
    ``Failure`` and derives everything it shows from that failure's kind: its
    ``args`` hold the failure alone, ``str()`` is the kind's fixed
    owner-visible sentence, and ``repr()`` names the kind. No caller value,
    underlying exception, path, or SQL reaches any of them, so a handler may
    print ``str(exc)`` straight to the owner and a log may record
    ``exc.failure.code``.

    It derives from ``Exception`` rather than a more specific built-in on
    purpose. An existing ``except RuntimeError`` or ``except sqlite3.Error``
    handler, written to contain an ordinary operational failure, must not
    silently absorb a typed refusal that the owner is meant to see.
    """

    def __init__(self, failure: Failure) -> None:
        """Settle the carried failure by exact type before the value exists.

        The check goes through ``type()`` for the same reason ``Failure``
        does: a look-alike object that merely quacks like one must not reach
        a handler that will print its message. The failure is stored only in
        ``args``, so there is no second place for a payload to be kept.
        """
        if type(failure) is not Failure:
            raise ValueError("A typed failure error must carry a Failure.")
        super().__init__(failure)

    @property
    def failure(self) -> Failure:
        """Return the typed failure this exception was raised with."""
        return self.args[0]

    def __str__(self) -> str:
        """Return the carried kind's fixed owner-visible message."""
        return self.failure.message
