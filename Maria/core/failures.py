"""Shared typed failures for Maria's internal boundaries.

A boundary that cannot complete an operation has to say so without handing the
owner, or the operational log, whatever the underlying failure happened to
carry. This module supplies the small vocabulary for that: a closed set of
failure kinds and one immutable value object holding a kind and nothing else.

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


# The one owner-visible sentence each kind is allowed to produce. The text is
# fixed per kind and interpolates nothing, so a message can never widen to
# carry a caller's value or an underlying failure's text.
_FAILURE_MESSAGES: Mapping[FailureKind, str] = MappingProxyType(
    {
        FailureKind.TOOL_EXECUTION_FAILED: "Tool execution failed.",
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
