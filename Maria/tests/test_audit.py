"""Focused tests for V1-18's audit vocabulary and the memory layer's audit API.

``core/audit.py`` declares everything an audit summary may say: the actions a
reasoning model may propose, the owner's decision, a generalised target, an
outcome, and Maria's own UTC timestamp. The memory layer stores, reads, and
deletes summaries and their outcomes in exactly that vocabulary. These tests
pin the vocabulary by hand, prove every field is refused outside it in both
directions, and drive each audit method against a temporary database that is
healthy, refusing, failing, locked, or tampered with. A small real-component
harness then proves what an approved model proposal writes, and when.

Every database, log, and workspace is temporary. No test contacts a model or
opens Maria's live database, live log, or private override.
"""

from __future__ import annotations

import inspect
import logging
import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from contextlib import closing
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, call, patch

import core.audit as audit_module
import core.memory as memory_module
import tools.system as system_module
from core.action_contracts import (
    BUILT_IN_ACTION_CONTRACTS,
    ActionContract,
    ArgumentContract,
    ModelEligibility,
    RootClass,
)
from core.audit import (
    AUDIT_LIST_LIMIT,
    AUDIT_QUERY_LIMIT,
    AUDIT_SUMMARY_ID_MESSAGE,
    AUDIT_SUMMARY_MESSAGE,
    AUDIT_TIMESTAMP_FORMAT,
    AUDITED_ACTIONS,
    AuditDecision,
    AuditOutcome,
    AuditSummary,
    AuditTarget,
    audit_target,
    audit_timestamp,
    is_audit_summary_id,
    is_audit_timestamp,
    is_audited_action,
)
from core.brain import BrainResponse, MariaBrain
from core.logger import configure_logging, get_logger
from core.memory import Memory
from core.permissions import PermissionManager
from core.plan_compiler import MODEL_ACTION_ALLOWLIST
from core.planner import Planner
from core.reasoning import DISABLED_REASONING
from core.session import CONFIRMATION_REFUSED_MESSAGE, MariaSession
from core.tool_manager import ToolManager
from tests.log_assertions import assert_log_omits
from tests.test_brain_recording_policy import _quiet_logger
from tests.test_conversation import ScriptedProvider, _test_config, enabled_settings
from tools.calculator import CalculatorTool


# --- Pinned vocabulary and messages -------------------------------------------
#
# Everything here is written out by hand, so a declaration that drifted could
# not pass by agreeing with itself.

EXPECTED_AUDITED_ACTIONS = frozenset(
    {"calculator.calculate", "filesystem.list", "system.current_time"}
)

# Every registered action whose contract declares a path argument.
PATH_ACTIONS = frozenset(
    {"filesystem.read", "filesystem.list", "filesystem.rename", "filesystem.move"}
)

SUMMARY_MESSAGE = (
    "An audit summary may hold only values from Maria's audit vocabulary."
)
ID_MESSAGE = "Audit summary id must be a positive whole number."
BUSY_MESSAGE = "Database is busy. Try again shortly."
RECORD_FAILED = "Unable to record audit summary."
OUTCOME_FAILED = "Unable to record audit outcome."
LIST_FAILED = "Unable to list audit summaries."
READ_FAILED = "Unable to read audit summary."
DELETE_FAILED = "Unable to delete audit summary."
OUTSIDE_VOCABULARY = "a stored value is outside the audit vocabulary"

AUDIT_SUMMARY_UNRECORDED = (
    "Maria did not run the approved action because its audit summary could "
    "not be recorded."
)
NO_CONTEXT_NOTICE = "No memory context supplied."
APPROVAL_HEADER = (
    "Maria's local reasoning model proposed this action. It runs only if you "
    "approve it."
)

RECORD_TABLES = (
    "interactions",
    "notes",
    "memories",
    "audit_summaries",
    "audit_outcomes",
    "saved_conversations",
    "conversation_summaries",
)
ZERO_ROWS = {table: 0 for table in RECORD_TABLES}

STAMP = "2026-10-01T09:46:17Z"

TAMPER_SENTINEL = "audit-tamper-sentinel-6b1f0e9c27"
# Usable both as a trigger's RAISE text and inside a table name, so SQLite's
# own error text carries it either way.
SQLITE_SENTINEL = "audit_sqlite_sentinel_3d8a52f1c4"
PATH_SENTINEL = "audit-path-sentinel-91c4e7a0b3"
REQUEST_SENTINEL = "audit-request-sentinel-0f5d2c8e61"

# A busy timeout short enough that a held lock is reported almost at once.
SHORT_BUSY_TIMEOUT_SECONDS = 0.001

# The clock tool's local reading, pinned two hours ahead of UTC so its report
# is exact text.
CLOCK_MOMENT = datetime(2026, 10, 1, 11, 46, 17, tzinfo=timezone(timedelta(hours=2)))
CLOCK_ISO = "2026-10-01T11:46:17+02:00"

UNRECOGNISED_REQUEST = "What is six times seven?"
CALCULATION_PROPOSAL = (
    '{"tool": "calculator", "action": "calculate", '
    '"arguments": {"expression": "6 * 7"}}'
)
FAILING_CALCULATION_PROPOSAL = (
    '{"tool": "calculator", "action": "calculate", '
    '"arguments": {"expression": "1 / 0"}}'
)
LISTING_PROPOSAL = '{"tool": "filesystem", "action": "list", "arguments": {}}'
INBOX_LISTING_PROPOSAL = (
    '{"tool": "filesystem", "action": "list", "arguments": {"path": "inbox"}}'
)
CLOCK_PROPOSAL = '{"tool": "system", "action": "current_time", "arguments": {}}'

# The one summary row an approved listing stores, whatever path it named.
STORED_LISTING_SUMMARY = (
    1,
    STAMP,
    "filesystem.list",
    "owner_approved",
    "named_filesystem_root",
)


class _Text(str):
    """A str subclass, which must never pass for Maria's exact text."""


class _Whole(int):
    """An int subclass, which must never pass for an exact id or limit."""


def _arabic_indic(text: str) -> str:
    """Return text with every ASCII digit written as an Arabic-Indic digit."""
    return "".join(
        chr(0x0660 + int(character)) if character in "0123456789" else character
        for character in text
    )


def _labelled(value: object) -> str:
    """Name a value's exact type beside its repr, so a subclass stays visible."""
    return f"{type(value).__name__}({value!r})"


REFUSED_IDS: tuple[object, ...] = (True, False, 0, -1, "1", 1.0, None, _Whole(1))

REFUSED_TIMESTAMPS: tuple[object, ...] = (
    "2026-10-01 09:46:17Z",
    "2026-10-01T09:46:17",
    "2026-10-01T09:46:17z",
    "2026-10-01T09:46:17+00:00",
    "2026-10-01T09:46:17.123456Z",
    "2026-10-01T09:46Z",
    "2026-10-1T09:46:17Z",
    "26-10-01T09:46:17Z",
    "2026-02-30T00:00:00Z",
    "2026-02-29T00:00:00Z",
    "2026-13-01T00:00:00Z",
    "2026-00-10T00:00:00Z",
    "2026-10-00T00:00:00Z",
    "2026-10-01T24:00:00Z",
    "2026-10-01T23:60:00Z",
    "2026-12-31T23:59:60Z",
    "0000-01-01T00:00:00Z",
    " " + STAMP,
    STAMP + " ",
    STAMP + "\n",
    _arabic_indic(STAMP),
    "",
    _Text(STAMP),
    STAMP.encode("ascii"),
    None,
    datetime(2026, 10, 1, 9, 46, 17, tzinfo=timezone.utc),
    1759311977,
    {"created_at": STAMP},
)

REFUSED_ACTIONS: tuple[object, ...] = (
    "audit.list",
    "audit.read",
    "audit.delete",
    "filesystem.read",
    "filesystem.rename",
    "filesystem.move",
    "memory.store",
    "notes.delete",
    "system.overview",
    "Calculator.calculate",
    "CALCULATOR.CALCULATE",
    " calculator.calculate",
    "calculator.calculate ",
    "calculator.calculate\n",
    "calculator.calculate" + chr(0x200B),
    "calculator",
    "calculate",
    "calculator.calculate.extra",
    "calculator_calculate",
    "",
    None,
    b"calculator.calculate",
    _Text("calculator.calculate"),
    ("calculator", "calculate"),
    ["calculator.calculate"],
)

REFUSED_DECISIONS: tuple[object, ...] = (
    "owner_approved",
    "OWNER_APPROVED",
    _Text("owner_approved"),
    AuditTarget.NONE,
    AuditOutcome.SUCCEEDED,
    None,
    True,
    1,
)

REFUSED_TARGETS: tuple[object, ...] = (
    "none",
    "named_filesystem_root",
    _Text("none"),
    AuditDecision.OWNER_APPROVED,
    AuditOutcome.FAILED,
    None,
    False,
)

# None is absent from this list on purpose: it is a summary's valid "no
# outcome", and only the outcome-recording call refuses it.
REFUSED_OUTCOMES: tuple[object, ...] = (
    "succeeded",
    "failed",
    "",
    _Text("succeeded"),
    AuditDecision.OWNER_APPROVED,
    AuditTarget.NONE,
    False,
    0,
)

# Stored values that only resemble a member. A member itself is refused too:
# what was stored can only ever be text.
REFUSED_STORED_MEMBERS: dict[str, tuple[object, ...]] = {
    "decision": (
        AuditDecision.OWNER_APPROVED,
        "OWNER_APPROVED",
        "Owner_Approved",
        " owner_approved",
        "owner_approved ",
        "owner approved",
        "",
        _Text("owner_approved"),
        b"owner_approved",
        None,
        1,
    ),
    "target": (
        AuditTarget.NONE,
        AuditTarget.NAMED_FILESYSTEM_ROOT,
        "NONE",
        "None",
        "named filesystem root",
        "named-filesystem-root",
        "",
        _Text("none"),
        b"none",
        None,
        0,
    ),
    "outcome": (
        AuditOutcome.SUCCEEDED,
        AuditOutcome.FAILED,
        "SUCCEEDED",
        "success",
        "unknown",
        "",
        " failed",
        _Text("failed"),
        b"failed",
        0,
        False,
    ),
}

# Stored rows that each hold exactly one value outside the vocabulary. Text
# carries the sentinel wherever the column could hold it; the rest are near
# misses SQLite stores without complaint. A TEXT column turns a number into
# text, so only bytes come back as another type.
TAMPERED_ROWS: tuple[tuple[str, dict[str, object]], ...] = (
    ("sentinel action", {"action": TAMPER_SENTINEL}),
    ("sentinel decision", {"decision": TAMPER_SENTINEL}),
    ("sentinel target", {"target": TAMPER_SENTINEL}),
    ("sentinel outcome", {"outcome": TAMPER_SENTINEL}),
    ("sentinel timestamp", {"created_at": TAMPER_SENTINEL}),
    ("action with a suffix", {"action": "calculator.calculate " + TAMPER_SENTINEL}),
    ("unaudited action", {"action": "memory.store"}),
    ("decision by member name", {"decision": "OWNER_APPROVED"}),
    ("target as displayed", {"target": "named filesystem root"}),
    ("outcome by member name", {"outcome": "SUCCEEDED"}),
    ("empty outcome", {"outcome": ""}),
    ("offset timestamp", {"created_at": "2026-10-01T09:46:17+00:00"}),
    ("microsecond timestamp", {"created_at": "2026-10-01T09:46:17.000001Z"}),
    ("impossible date", {"created_at": "2026-02-30T00:00:00Z"}),
    ("leap second", {"created_at": "2026-12-31T23:59:60Z"}),
    ("numeric timestamp", {"created_at": 1759311977}),
    ("action as bytes", {"action": b"calculator.calculate"}),
    ("decision as bytes", {"decision": b"owner_approved"}),
    ("target as bytes", {"target": b"none"}),
    ("timestamp as bytes", {"created_at": STAMP.encode("ascii")}),
    ("outcome as bytes", {"outcome": b"succeeded"}),
    ("zero id", {"id": 0}),
    ("negative id", {"id": -1}),
)

_NO_OUTCOME = object()


# --- Helpers ----------------------------------------------------------------------


def _summary_fields(**changes: object) -> dict[str, object]:
    """Return one valid summary's fields with some replaced."""
    values: dict[str, object] = {
        "summary_id": 1,
        "created_at": STAMP,
        "action": "calculator.calculate",
        "decision": AuditDecision.OWNER_APPROVED,
        "target": AuditTarget.NONE,
        "outcome": None,
    }
    values.update(changes)
    return values


def _stored_fields(**changes: object) -> dict[str, object]:
    """Return one valid stored row's values, in from_stored's order."""
    values: dict[str, object] = {
        "summary_id": 1,
        "created_at": STAMP,
        "action": "calculator.calculate",
        "decision": "owner_approved",
        "target": "none",
        "outcome": None,
    }
    values.update(changes)
    return values


def _execute(
    database_path: Path,
    statement: str,
    parameters: tuple[object, ...] = (),
) -> None:
    """Run one statement on a separate connection to a temporary database."""
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(statement, parameters)
        connection.commit()


def _fetch(
    database_path: Path,
    statement: str,
    parameters: tuple[object, ...] = (),
) -> list[tuple[object, ...]]:
    """Return every row one query reads on a separate connection."""
    with closing(sqlite3.connect(database_path)) as connection:
        return connection.execute(statement, parameters).fetchall()


def _sqlite_failure_text(
    database_path: Path,
    statement: str,
    parameters: tuple[object, ...] = (),
) -> str:
    """Return SQLite's own text for one failing statement, which changes nothing.

    A leak test runs this first, so its sentinel is proven to be in what
    SQLite says and the test can never pass merely because it is not.
    """
    with closing(sqlite3.connect(database_path)) as connection:
        try:
            connection.execute(statement, parameters)
        except sqlite3.Error as error:
            return str(error)
    raise AssertionError("The statement was expected to fail.")


def _table_counts(database_path: Path) -> dict[str, int]:
    """Count the rows of all five record tables on a separate connection."""
    with closing(sqlite3.connect(database_path)) as connection:
        return {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in RECORD_TABLES
        }


def _audit_rows(database_path: Path) -> tuple[list[tuple[object, ...]], ...]:
    """Return every stored audit summary and outcome, exactly as stored."""
    return (
        _fetch(
            database_path,
            "SELECT id, created_at, action, decision, target "
            "FROM audit_summaries ORDER BY id",
        ),
        _fetch(
            database_path,
            "SELECT summary_id, outcome FROM audit_outcomes ORDER BY summary_id",
        ),
    )


def _insert_raw(connection: sqlite3.Connection, values: dict[str, object]) -> object:
    """Insert one summary row, and its outcome when one is given, as supplied.

    Returns the stored id: the one supplied, or the one SQLite assigned.
    """
    row: dict[str, object] = {
        "created_at": STAMP,
        "action": "calculator.calculate",
        "decision": "owner_approved",
        "target": "none",
        **values,
    }
    outcome = row.pop("outcome", _NO_OUTCOME)
    columns = ", ".join(row)
    markers = ", ".join("?" for _ in row)
    cursor = connection.execute(
        f"INSERT INTO audit_summaries ({columns}) VALUES ({markers})",
        tuple(row.values()),
    )
    summary_id = row.get("id", cursor.lastrowid)
    if outcome is not _NO_OUTCOME:
        connection.execute(
            "INSERT INTO audit_outcomes (summary_id, outcome) VALUES (?, ?)",
            (summary_id, outcome),
        )
    return summary_id


def _store_raw(database_path: Path, values: dict[str, object]) -> object:
    """Store one summary row as supplied, on a separate connection."""
    with closing(sqlite3.connect(database_path)) as connection:
        summary_id = _insert_raw(connection, values)
        connection.commit()
    return summary_id


def _replace_audit_rows(
    database_path: Path,
    *rows: dict[str, object],
) -> list[object]:
    """Replace every audit row with these, as supplied, in one transaction."""
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("DELETE FROM audit_outcomes")
        connection.execute("DELETE FROM audit_summaries")
        identifiers = [_insert_raw(connection, values) for values in rows]
        connection.commit()
    return identifiers


def _hold_exclusive_lock(
    test: unittest.TestCase,
    database_path: Path,
) -> sqlite3.Connection:
    """Hold an exclusive lock on another connection until it is released."""
    holder = sqlite3.connect(database_path, timeout=0, isolation_level=None)
    test.addCleanup(holder.close)
    holder.execute("BEGIN EXCLUSIVE")
    return holder


def _rendered_calls(logger: Mock) -> list[tuple[str, str]]:
    """Return each call on a mock logger as its method and rendered line."""
    return [
        (name, args[0] % args[1:] if args[1:] else args[0])
        for name, args, _ in logger.method_calls
    ]


def _approval(tool: str, action: str, arguments: str) -> str:
    """Return the approval prompt for a model plan offered with no context."""
    return (
        "\n".join(
            (
                APPROVAL_HEADER,
                f"Tool: {tool}",
                f"Action: {action}",
                f"Arguments: {arguments}",
            )
        )
        + "\n\n"
        + NO_CONTEXT_NOTICE
    )


def _contract(tool: str, action: str) -> ActionContract:
    """Return one registered contract, failing loudly if it is missing."""
    contract = BUILT_IN_ACTION_CONTRACTS.contract_for(tool, action)
    if contract is None:
        raise AssertionError(f"{tool}.{action} is not registered.")
    return contract


# --- The vocabulary -----------------------------------------------------------------


class AuditVocabularyTests(unittest.TestCase):
    """Pin every word an audit summary may use, against hand-written values."""

    def test_audited_actions_are_exactly_the_three_proposable_actions(self) -> None:
        self.assertIs(type(AUDITED_ACTIONS), frozenset)
        self.assertEqual(AUDITED_ACTIONS, EXPECTED_AUDITED_ACTIONS)

    def test_audited_actions_equal_the_separately_declared_allowlist(self) -> None:
        self.assertEqual(AUDITED_ACTIONS, MODEL_ACTION_ALLOWLIST)
        # Two declarations, not one shared object, so changing either alone
        # is caught here rather than silently widening both.
        self.assertIsNot(AUDITED_ACTIONS, MODEL_ACTION_ALLOWLIST)

    def test_audited_actions_equal_the_registry_model_eligible_actions(self) -> None:
        eligible = frozenset(
            contract.identifier
            for contract in BUILT_IN_ACTION_CONTRACTS.contracts
            if contract.model_eligibility is ModelEligibility.MODEL_ELIGIBLE
        )
        self.assertEqual(AUDITED_ACTIONS, eligible)

    def test_no_audit_action_can_itself_be_audited(self) -> None:
        for action in ("list", "read", "delete"):
            with self.subTest(action=action):
                contract = _contract("audit", action)
                self.assertIs(
                    contract.model_eligibility,
                    ModelEligibility.NOT_MODEL_ELIGIBLE,
                )
                self.assertFalse(is_audited_action(f"audit.{action}"))

    def test_listing_limits_and_the_timestamp_form_are_pinned(self) -> None:
        self.assertEqual(AUDIT_LIST_LIMIT, 20)
        self.assertEqual(AUDIT_QUERY_LIMIT, 21)
        self.assertEqual(AUDIT_TIMESTAMP_FORMAT, "%Y-%m-%dT%H:%M:%SZ")

    def test_refusal_messages_are_fixed_text(self) -> None:
        self.assertEqual(AUDIT_SUMMARY_MESSAGE, SUMMARY_MESSAGE)
        self.assertEqual(AUDIT_SUMMARY_ID_MESSAGE, ID_MESSAGE)

    def test_enum_members_and_values_are_exact(self) -> None:
        expected = {
            AuditDecision: [("OWNER_APPROVED", "owner_approved")],
            AuditTarget: [
                ("NONE", "none"),
                ("NAMED_FILESYSTEM_ROOT", "named_filesystem_root"),
            ],
            AuditOutcome: [("SUCCEEDED", "succeeded"), ("FAILED", "failed")],
        }
        for enum_type, members in expected.items():
            with self.subTest(enum=enum_type.__name__):
                self.assertTrue(issubclass(enum_type, str))
                self.assertTrue(issubclass(enum_type, Enum))
                self.assertEqual(
                    [(member.name, member.value) for member in enum_type],
                    members,
                )

    def test_a_summary_has_only_vocabulary_fields_and_is_frozen(self) -> None:
        self.assertEqual(
            [field.name for field in fields(AuditSummary)],
            ["summary_id", "created_at", "action", "decision", "target", "outcome"],
        )
        summary = AuditSummary(
            1,
            STAMP,
            "calculator.calculate",
            AuditDecision.OWNER_APPROVED,
            AuditTarget.NONE,
        )
        self.assertIsNone(summary.outcome)
        with self.assertRaises(FrozenInstanceError):
            summary.action = "filesystem.list"  # type: ignore[misc]
        self.assertEqual(summary.action, "calculator.calculate")


class AuditValidatorTests(unittest.TestCase):
    """Prove each validator accepts exactly the vocabulary's own values."""

    def test_only_exact_positive_ints_are_summary_ids(self) -> None:
        for value in (1, 2, 21, 2**62):
            with self.subTest(value=value):
                self.assertIs(is_audit_summary_id(value), True)
        for value in REFUSED_IDS:
            with self.subTest(value=_labelled(value)):
                self.assertIs(is_audit_summary_id(value), False)

    def test_only_exact_audited_identifiers_are_audited_actions(self) -> None:
        for action in sorted(EXPECTED_AUDITED_ACTIONS):
            with self.subTest(action=action):
                self.assertIs(is_audited_action(action), True)
        for value in REFUSED_ACTIONS:
            with self.subTest(value=_labelled(value)):
                self.assertIs(is_audited_action(value), False)

    def test_only_real_utc_seconds_in_the_one_form_are_timestamps(self) -> None:
        for value in (
            STAMP,
            "2024-02-29T00:00:00Z",
            "2026-01-01T00:00:00Z",
            "2026-12-31T23:59:59Z",
        ):
            with self.subTest(value=value):
                self.assertIs(is_audit_timestamp(value), True)
        for value in REFUSED_TIMESTAMPS:
            with self.subTest(value=_labelled(value)):
                self.assertIs(is_audit_timestamp(value), False)


class AuditSummaryValidationTests(unittest.TestCase):
    """Prove a summary refuses any field outside the vocabulary, by one message."""

    def _assert_refused(self, **changes: object) -> None:
        with self.assertRaises(ValueError) as caught:
            AuditSummary(**_summary_fields(**changes))
        self.assertEqual(caught.exception.args, (SUMMARY_MESSAGE,))

    def test_every_combination_of_vocabulary_values_is_accepted(self) -> None:
        for action in sorted(EXPECTED_AUDITED_ACTIONS):
            for target in AuditTarget:
                for outcome in (None, *AuditOutcome):
                    with self.subTest(action=action, target=target, outcome=outcome):
                        summary = AuditSummary(
                            7,
                            STAMP,
                            action,
                            AuditDecision.OWNER_APPROVED,
                            target,
                            outcome,
                        )
                        self.assertEqual(
                            (
                                summary.summary_id,
                                summary.created_at,
                                summary.action,
                                summary.decision,
                                summary.target,
                                summary.outcome,
                            ),
                            (
                                7,
                                STAMP,
                                action,
                                AuditDecision.OWNER_APPROVED,
                                target,
                                outcome,
                            ),
                        )

    def test_every_refused_id_raises_the_fixed_message(self) -> None:
        for value in REFUSED_IDS:
            with self.subTest(value=_labelled(value)):
                self._assert_refused(summary_id=value)

    def test_every_refused_timestamp_raises_the_fixed_message(self) -> None:
        for value in REFUSED_TIMESTAMPS:
            with self.subTest(value=_labelled(value)):
                self._assert_refused(created_at=value)

    def test_every_refused_action_raises_the_fixed_message(self) -> None:
        for value in REFUSED_ACTIONS:
            with self.subTest(value=_labelled(value)):
                self._assert_refused(action=value)

    def test_enum_fields_refuse_plain_text_and_other_members(self) -> None:
        cases = (
            *(("decision", value) for value in REFUSED_DECISIONS),
            *(("target", value) for value in REFUSED_TARGETS),
            *(("outcome", value) for value in REFUSED_OUTCOMES),
        )
        for field_name, value in cases:
            with self.subTest(field=field_name, value=_labelled(value)):
                self._assert_refused(**{field_name: value})

    def test_a_refusal_repeats_no_value_it_was_given(self) -> None:
        with self.assertRaises(ValueError) as caught:
            AuditSummary(**_summary_fields(action=TAMPER_SENTINEL))
        self.assertNotIn(TAMPER_SENTINEL, str(caught.exception))
        self.assertNotIn(TAMPER_SENTINEL, repr(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)


class AuditSummaryFromStoredTests(unittest.TestCase):
    """Prove stored text maps to exact members, and nothing else maps at all."""

    def test_stored_text_maps_to_exact_members(self) -> None:
        summary = AuditSummary.from_stored(
            3,
            STAMP,
            "filesystem.list",
            "owner_approved",
            "named_filesystem_root",
            "failed",
        )
        self.assertIs(type(summary), AuditSummary)
        self.assertEqual(
            summary,
            AuditSummary(
                3,
                STAMP,
                "filesystem.list",
                AuditDecision.OWNER_APPROVED,
                AuditTarget.NAMED_FILESYSTEM_ROOT,
                AuditOutcome.FAILED,
            ),
        )
        self.assertIs(summary.decision, AuditDecision.OWNER_APPROVED)
        self.assertIs(summary.target, AuditTarget.NAMED_FILESYSTEM_ROOT)
        self.assertIs(summary.outcome, AuditOutcome.FAILED)

    def test_every_member_value_maps_back_to_its_member(self) -> None:
        for target in AuditTarget:
            for outcome in (None, *AuditOutcome):
                with self.subTest(target=target, outcome=outcome):
                    summary = AuditSummary.from_stored(
                        5,
                        STAMP,
                        "system.current_time",
                        AuditDecision.OWNER_APPROVED.value,
                        target.value,
                        None if outcome is None else outcome.value,
                    )
                    self.assertIs(summary.target, target)
                    self.assertIs(summary.outcome, outcome)
                    self.assertIs(summary.decision, AuditDecision.OWNER_APPROVED)

    def test_values_that_only_resemble_a_member_are_refused(self) -> None:
        for field_name, refused in REFUSED_STORED_MEMBERS.items():
            for value in refused:
                with self.subTest(field=field_name, value=_labelled(value)):
                    with self.assertRaises(ValueError) as caught:
                        AuditSummary.from_stored(
                            *_stored_fields(**{field_name: value}).values()
                        )
                    self.assertEqual(caught.exception.args, (SUMMARY_MESSAGE,))

    def test_stored_ids_timestamps_and_actions_are_validated_too(self) -> None:
        cases = (
            *(("summary_id", value) for value in REFUSED_IDS),
            *(("created_at", value) for value in REFUSED_TIMESTAMPS),
            *(("action", value) for value in REFUSED_ACTIONS),
        )
        for field_name, value in cases:
            with self.subTest(field=field_name, value=_labelled(value)):
                with self.assertRaises(ValueError) as caught:
                    AuditSummary.from_stored(
                        *_stored_fields(**{field_name: value}).values()
                    )
                self.assertEqual(caught.exception.args, (SUMMARY_MESSAGE,))

    def test_a_stored_refusal_repeats_no_stored_value(self) -> None:
        with self.assertRaises(ValueError) as caught:
            AuditSummary.from_stored(
                *_stored_fields(target=TAMPER_SENTINEL).values()
            )
        self.assertNotIn(TAMPER_SENTINEL, str(caught.exception))
        self.assertNotIn(TAMPER_SENTINEL, repr(caught.exception))


class AuditTimestampTests(unittest.TestCase):
    """Prove Maria's audit timestamp is the current UTC second in the one form."""

    def test_the_timestamp_is_the_current_utc_second(self) -> None:
        before = datetime.now(timezone.utc).replace(microsecond=0)
        value = audit_timestamp()
        after = datetime.now(timezone.utc)

        self.assertIs(type(value), str)
        self.assertRegex(
            value,
            r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z",
        )
        self.assertTrue(is_audit_timestamp(value))
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        self.assertLessEqual(before, moment)
        self.assertLessEqual(moment, after)

    def test_the_timestamp_renders_the_instant_in_utc_to_the_second(self) -> None:
        instant = datetime(2026, 10, 1, 9, 46, 17, 987654, tzinfo=timezone.utc)
        asked: list[object] = []

        class _FrozenClock(datetime):
            """A clock stopped at one instant, read in whatever zone is asked."""

            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                asked.append(tz)
                if tz is None:
                    # A naive local reading five hours away from UTC, so a
                    # local time can never pass for the UTC one.
                    return (instant + timedelta(hours=5)).replace(tzinfo=None)
                return instant.astimezone(tz)

        with patch.object(audit_module, "datetime", _FrozenClock):
            value = audit_timestamp()

        self.assertTrue(asked)
        # UTC, to the second, with the fraction dropped rather than rounded.
        self.assertEqual(value, "2026-10-01T09:46:17Z")

    def test_the_validator_answers_alike_for_every_four_digit_year(
        self,
    ) -> None:
        # The fixed-width form and the calendar decide; no platform's own
        # year padding can turn a valid early year into an invalid one.
        for value, valid in (
            ("0001-01-01T00:00:00Z", True),
            ("0999-12-31T23:59:59Z", True),
            ("1000-01-01T00:00:00Z", True),
            ("9999-12-31T23:59:59Z", True),
            ("0000-01-01T00:00:00Z", False),
            ("0999-02-29T00:00:00Z", False),
        ):
            with self.subTest(value=value):
                self.assertIs(is_audit_timestamp(value), valid)


class AuditTargetTests(unittest.TestCase):
    """Prove the audited target is decided by a contract, for every contract."""

    def test_every_registered_contract_has_its_target(self) -> None:
        contracts = BUILT_IN_ACTION_CONTRACTS.contracts
        self.assertEqual(len(contracts), 40)
        for contract in contracts:
            with self.subTest(action=contract.identifier):
                expected = (
                    AuditTarget.NAMED_FILESYSTEM_ROOT
                    if contract.identifier in PATH_ACTIONS
                    else AuditTarget.NONE
                )
                self.assertIs(audit_target(contract), expected)

    def test_the_path_actions_are_exactly_those_declaring_a_path(self) -> None:
        declared = frozenset(
            contract.identifier
            for contract in BUILT_IN_ACTION_CONTRACTS.contracts
            if any(argument.root_class is not None for argument in contract.arguments)
        )
        self.assertEqual(declared, PATH_ACTIONS)

    def test_a_listing_targets_a_named_root_although_its_path_is_optional(
        self,
    ) -> None:
        contract = _contract("filesystem", "list")
        [path] = contract.arguments
        self.assertFalse(path.required)
        self.assertIs(audit_target(contract), AuditTarget.NAMED_FILESYSTEM_ROOT)

    def test_hand_built_contracts_follow_their_declarations(self) -> None:
        path = ArgumentContract(
            "path",
            str,
            required=False,
            allow_blank=False,
            root_class=RootClass.NAMED_FILESYSTEM_ROOT,
        )
        other_path = ArgumentContract(
            "other",
            str,
            required=True,
            allow_blank=False,
            root_class=RootClass.NAMED_FILESYSTEM_ROOT,
        )
        identifier = ArgumentContract("id", int, required=True, minimum=1)
        text = ArgumentContract("text", str, required=True, allow_blank=False)
        # A field merely named "path" declares no root class, so it is no
        # target: the declaration decides, never the field's name.
        unrooted = ArgumentContract("path", str, required=True, allow_blank=False)
        cases = (
            ((), AuditTarget.NONE),
            ((identifier,), AuditTarget.NONE),
            ((text, identifier), AuditTarget.NONE),
            ((unrooted,), AuditTarget.NONE),
            ((text, unrooted), AuditTarget.NONE),
            ((path,), AuditTarget.NAMED_FILESYSTEM_ROOT),
            ((other_path,), AuditTarget.NAMED_FILESYSTEM_ROOT),
            ((identifier, path), AuditTarget.NAMED_FILESYSTEM_ROOT),
            ((path, other_path), AuditTarget.NAMED_FILESYSTEM_ROOT),
        )
        for arguments, expected in cases:
            labels = [
                f"{argument.name}{' (root)' if argument.is_path else ''}"
                for argument in arguments
            ]
            with self.subTest(arguments=labels):
                contract = ActionContract(
                    "probe",
                    "act",
                    arguments,
                    model_eligibility=ModelEligibility.NOT_MODEL_ELIGIBLE,
                )
                self.assertIs(audit_target(contract), expected)

    def test_a_root_class_with_no_audited_target_is_refused(self) -> None:
        with patch.object(
            audit_module,
            "_TARGETS_BY_ROOT_CLASS",
            MappingProxyType({}),
        ):
            with self.assertRaises(ValueError) as caught:
                audit_target(_contract("filesystem", "list"))
            self.assertEqual(caught.exception.args, (SUMMARY_MESSAGE,))
            # An action with no path argument is unaffected.
            self.assertIs(
                audit_target(_contract("calculator", "calculate")),
                AuditTarget.NONE,
            )


# --- The memory layer's audit API --------------------------------------------------


class _AuditDatabaseTestCase(unittest.TestCase):
    """One initialized temporary database per test, with a recording logger."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database_path = self.root / "database" / "maria.db"
        self.logger = Mock(spec=logging.Logger)
        self.memory = Memory(self.database_path, logger=self.logger)
        self.memory.initialize()
        self.logger.reset_mock()

    def _record(
        self,
        action: str = "calculator.calculate",
        target: AuditTarget = AuditTarget.NONE,
        *,
        at: str = STAMP,
    ) -> int:
        """Record one summary through the memory layer at a pinned timestamp."""
        with patch.object(memory_module, "audit_timestamp", return_value=at):
            return self.memory.record_audit_summary(
                action,
                AuditDecision.OWNER_APPROVED,
                target,
            )

    def _snapshot(self) -> tuple[object, ...]:
        """Return every audit row and every table's row count."""
        return (*_audit_rows(self.database_path), _table_counts(self.database_path))

    def _assert_logged(self, *expected: tuple[str, str]) -> None:
        """Assert exactly these lines were logged, with no traceback."""
        self.assertEqual(_rendered_calls(self.logger), list(expected))
        self.assertEqual(
            [kwargs for _, _, kwargs in self.logger.method_calls],
            [{} for _ in expected],
        )
        self.logger.exception.assert_not_called()

    def _assert_fixed_failure(
        self,
        error: BaseException,
        message: str,
        *sentinels: str,
    ) -> None:
        """Assert a failure is exactly the fixed one, carrying nothing else."""
        self.assertIs(type(error), RuntimeError)
        self.assertEqual(error.args, (message,))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        for sentinel in sentinels:
            self.assertNotIn(sentinel, str(error))
            self.assertNotIn(sentinel, repr(error))
            self.assertNotIn(sentinel, repr(self.logger.method_calls))

    def _fake_connection(self, *, rowcount: object, lastrowid: object) -> Mock:
        """Return a connection whose one statement reports the given cursor."""
        connection = Mock(spec=sqlite3.Connection)
        connection.execute.return_value = SimpleNamespace(
            rowcount=rowcount,
            lastrowid=lastrowid,
        )
        return connection

    def _traced_statements(self) -> list[str]:
        """Record every statement the memory layer runs from now on."""
        statements: list[str] = []
        original_connect = self.memory._connect

        def tracing_connect() -> sqlite3.Connection:
            connection = original_connect()
            connection.set_trace_callback(statements.append)
            return connection

        patcher = patch.object(self.memory, "_connect", tracing_connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        return statements


class RecordAuditSummaryTests(_AuditDatabaseTestCase):
    """Prove a summary is stored exactly, numbered in order, and committed."""

    def test_summaries_take_ascending_ids_and_store_exact_values(self) -> None:
        stamps = (STAMP, "2026-10-01T09:46:18Z", "2026-10-01T09:46:19Z")
        with patch.object(memory_module, "audit_timestamp", side_effect=stamps):
            identifiers = [
                self.memory.record_audit_summary(
                    "calculator.calculate",
                    AuditDecision.OWNER_APPROVED,
                    AuditTarget.NONE,
                ),
                self.memory.record_audit_summary(
                    "filesystem.list",
                    AuditDecision.OWNER_APPROVED,
                    AuditTarget.NAMED_FILESYSTEM_ROOT,
                ),
                self.memory.record_audit_summary(
                    "system.current_time",
                    AuditDecision.OWNER_APPROVED,
                    AuditTarget.NONE,
                ),
            ]

        self.assertEqual(identifiers, [1, 2, 3])
        self.assertEqual([type(identifier) for identifier in identifiers], [int] * 3)
        self.assertEqual(
            _fetch(
                self.database_path,
                "SELECT id, typeof(id), created_at, typeof(created_at), action, "
                "typeof(action), decision, typeof(decision), target, "
                "typeof(target) FROM audit_summaries ORDER BY id",
            ),
            [
                (
                    1, "integer", STAMP, "text", "calculator.calculate", "text",
                    "owner_approved", "text", "none", "text",
                ),
                (
                    2, "integer", "2026-10-01T09:46:18Z", "text", "filesystem.list",
                    "text", "owner_approved", "text", "named_filesystem_root", "text",
                ),
                (
                    3, "integer", "2026-10-01T09:46:19Z", "text",
                    "system.current_time", "text", "owner_approved", "text",
                    "none", "text",
                ),
            ],
        )
        self.assertEqual(
            _table_counts(self.database_path),
            {**ZERO_ROWS, "audit_summaries": 3},
        )
        self._assert_logged(
            ("info", "Recorded audit summary 1."),
            ("info", "Recorded audit summary 2."),
            ("info", "Recorded audit summary 3."),
        )

    def test_each_audited_action_is_stored_with_its_contract_target(self) -> None:
        for action in sorted(EXPECTED_AUDITED_ACTIONS):
            tool_name, _, action_name = action.partition(".")
            self._record(action, audit_target(_contract(tool_name, action_name)))

        self.assertEqual(
            _fetch(
                self.database_path,
                "SELECT id, action, decision, target FROM audit_summaries ORDER BY id",
            ),
            [
                (1, "calculator.calculate", "owner_approved", "none"),
                (2, "filesystem.list", "owner_approved", "named_filesystem_root"),
                (3, "system.current_time", "owner_approved", "none"),
            ],
        )

    def test_the_stored_timestamp_is_marias_own_utc_second(self) -> None:
        before = datetime.now(timezone.utc).replace(microsecond=0)
        summary_id = self.memory.record_audit_summary(
            "system.current_time",
            AuditDecision.OWNER_APPROVED,
            AuditTarget.NONE,
        )
        after = datetime.now(timezone.utc)

        [(created_at,)] = _fetch(
            self.database_path,
            "SELECT created_at FROM audit_summaries WHERE id = ?",
            (summary_id,),
        )
        self.assertTrue(is_audit_timestamp(created_at))
        moment = datetime.strptime(created_at, AUDIT_TIMESTAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
        self.assertLessEqual(before, moment)
        self.assertLessEqual(moment, after)

    def test_a_caller_can_supply_no_timestamp_and_no_free_text(self) -> None:
        self.assertEqual(
            list(inspect.signature(Memory.record_audit_summary).parameters),
            ["self", "action", "decision", "target"],
        )
        # Neither table has anywhere to keep a request, a proposal, an
        # argument, a path, a result, or an error.
        for table, columns in (
            ("audit_summaries", ["id", "created_at", "action", "decision", "target"]),
            ("audit_outcomes", ["summary_id", "outcome"]),
        ):
            with self.subTest(table=table):
                described = _fetch(self.database_path, f"PRAGMA table_info({table})")
                self.assertEqual([row[1] for row in described], columns)

    def test_a_summary_is_committed_before_the_call_returns(self) -> None:
        observer = sqlite3.connect(self.database_path, timeout=0)
        self.addCleanup(observer.close)
        self.assertEqual(
            observer.execute("SELECT COUNT(*) FROM audit_summaries").fetchall(),
            [(0,)],
        )

        summary_id = self._record()

        # Another connection sees it at once, and nothing still holds a lock.
        self.assertEqual(
            observer.execute("SELECT id, action FROM audit_summaries").fetchall(),
            [(summary_id, "calculator.calculate")],
        )
        observer.execute("BEGIN EXCLUSIVE")
        observer.rollback()

    def test_a_recorded_summary_has_no_outcome_and_touches_no_other_table(
        self,
    ) -> None:
        self._record()
        self.assertEqual(_audit_rows(self.database_path)[1], [])
        self.assertEqual(
            _table_counts(self.database_path),
            {**ZERO_ROWS, "audit_summaries": 1},
        )


class AuditWriteTimestampTests(_AuditDatabaseTestCase):
    """Prove Maria's own timestamp is validated before a summary is written."""

    def test_a_timestamp_outside_the_vocabulary_is_never_written(self) -> None:
        for value in (
            "2026-10-01 09:46:17",
            "2026-10-01T09:46:17+00:00",
            "2026-02-30T00:00:00Z",
            "",
            None,
            1727775977,
        ):
            with self.subTest(value=repr(value)):
                with (
                    patch.object(
                        memory_module,
                        "audit_timestamp",
                        return_value=value,
                    ),
                    patch.object(self.memory, "_connect") as connect,
                ):
                    with self.assertRaises(ValueError) as caught:
                        self.memory.record_audit_summary(
                            "calculator.calculate",
                            AuditDecision.OWNER_APPROVED,
                            AuditTarget.NONE,
                        )

                self.assertEqual(str(caught.exception), SUMMARY_MESSAGE)
                connect.assert_not_called()
                self.assertEqual(_table_counts(self.database_path), ZERO_ROWS)
                self.assertEqual(self.logger.method_calls, [])


class AuditIdRangeTests(_AuditDatabaseTestCase):
    """Prove ids beyond what SQLite can store name no summary, safely."""

    BEYOND = (2**63, 2**63 + 1, 10**30)

    def test_reading_or_deleting_an_impossible_id_finds_nothing(self) -> None:
        self._record()
        before = self._snapshot()

        for summary_id in self.BEYOND:
            with self.subTest(summary_id=summary_id):
                with patch.object(self.memory, "_connect") as connect:
                    self.assertIsNone(self.memory.get_audit_summary(summary_id))
                    self.assertIs(
                        self.memory.delete_audit_summary(summary_id),
                        False,
                    )

                connect.assert_not_called()
        self.assertEqual(self._snapshot(), before)
        self.logger.exception.assert_not_called()

    def test_the_largest_storable_id_is_read_recorded_and_deleted(self) -> None:
        # A summary really stored at the boundary is reached by every method,
        # so the ceiling refuses only ids no row can have.
        largest = 2**63 - 1
        self.assertEqual(_store_raw(self.database_path, {"id": largest}), largest)

        self.memory.record_audit_outcome(largest, AuditOutcome.SUCCEEDED)
        summary = self.memory.get_audit_summary(largest)

        self.assertEqual(
            summary,
            AuditSummary(
                largest,
                STAMP,
                "calculator.calculate",
                AuditDecision.OWNER_APPROVED,
                AuditTarget.NONE,
                AuditOutcome.SUCCEEDED,
            ),
        )
        self.assertEqual(_audit_rows(self.database_path)[1], [(largest, "succeeded")])
        self.assertIs(self.memory.delete_audit_summary(largest), True)
        self.assertIsNone(self.memory.get_audit_summary(largest))
        self.assertEqual(_table_counts(self.database_path), ZERO_ROWS)

    def test_an_outcome_for_an_impossible_id_is_a_fixed_failure(self) -> None:
        for summary_id in self.BEYOND:
            with self.subTest(summary_id=summary_id):
                self.logger.reset_mock()
                with patch.object(self.memory, "_connect") as connect:
                    with self.assertRaises(RuntimeError) as caught:
                        self.memory.record_audit_outcome(
                            summary_id,
                            AuditOutcome.SUCCEEDED,
                        )

                connect.assert_not_called()
                self._assert_fixed_failure(caught.exception, OUTCOME_FAILED)
                self._assert_logged(
                    (
                        "error",
                        "Failed to record audit outcome: no row was inserted.",
                    ),
                )
        self.assertEqual(_table_counts(self.database_path), ZERO_ROWS)


class AuditArgumentValidationTests(_AuditDatabaseTestCase):
    """Prove every audit call refuses bad arguments before opening the database."""

    def _refused_before_opening(self, attempt: Callable[[], object]) -> ValueError:
        with patch.object(
            self.memory,
            "_connect",
            side_effect=AssertionError("A connection was opened."),
        ) as connect:
            with self.assertRaises(ValueError) as caught:
                attempt()
        connect.assert_not_called()
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        return caught.exception

    def test_a_summary_outside_the_vocabulary_is_refused_unopened(self) -> None:
        cases = (
            *(
                (action, AuditDecision.OWNER_APPROVED, AuditTarget.NONE)
                for action in REFUSED_ACTIONS
            ),
            *(
                ("calculator.calculate", decision, AuditTarget.NONE)
                for decision in REFUSED_DECISIONS
            ),
            *(
                ("calculator.calculate", AuditDecision.OWNER_APPROVED, target)
                for target in REFUSED_TARGETS
            ),
        )
        for arguments in cases:
            with self.subTest(arguments=", ".join(map(_labelled, arguments))):
                error = self._refused_before_opening(
                    lambda: self.memory.record_audit_summary(*arguments)
                )
                self.assertEqual(error.args, (SUMMARY_MESSAGE,))

        self.assertEqual(_table_counts(self.database_path), ZERO_ROWS)
        self.assertEqual(self.logger.method_calls, [])

    def test_a_bad_outcome_id_or_outcome_is_refused_unopened(self) -> None:
        summary_id = self._record()
        self.logger.reset_mock()

        for value in REFUSED_IDS:
            with self.subTest(summary_id=_labelled(value)):
                error = self._refused_before_opening(
                    lambda: self.memory.record_audit_outcome(
                        value,
                        AuditOutcome.SUCCEEDED,
                    )
                )
                self.assertEqual(error.args, (ID_MESSAGE,))
        for value in (*REFUSED_OUTCOMES, None):
            with self.subTest(outcome=_labelled(value)):
                error = self._refused_before_opening(
                    lambda: self.memory.record_audit_outcome(summary_id, value)
                )
                self.assertEqual(error.args, (SUMMARY_MESSAGE,))

        # With both wrong, the id is settled first.
        error = self._refused_before_opening(
            lambda: self.memory.record_audit_outcome(0, "succeeded")
        )
        self.assertEqual(error.args, (ID_MESSAGE,))
        self.assertEqual(_audit_rows(self.database_path)[1], [])
        self.assertEqual(self.logger.method_calls, [])

    def test_a_bad_id_is_refused_unopened_by_reads_and_deletes(self) -> None:
        self._record()
        before = self._snapshot()
        self.logger.reset_mock()

        for value in REFUSED_IDS:
            for name, attempt in (
                ("get", lambda: self.memory.get_audit_summary(value)),
                ("delete", lambda: self.memory.delete_audit_summary(value)),
            ):
                with self.subTest(call=name, summary_id=_labelled(value)):
                    error = self._refused_before_opening(attempt)
                    self.assertEqual(error.args, (ID_MESSAGE,))

        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.logger.method_calls, [])

    def test_a_listing_limit_outside_one_to_twenty_one_is_refused_unopened(
        self,
    ) -> None:
        messages = set()
        for limit in (0, -1, 22, 100, True, False, 1.0, 21.0, "21", None, _Whole(5)):
            with self.subTest(limit=_labelled(limit)):
                error = self._refused_before_opening(
                    lambda: self.memory.list_audit_summaries(limit)
                )
                self.assertEqual(len(error.args), 1)
                messages.add(error.args[0])
        # One fixed message, whatever the refused limit was.
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.logger.method_calls, [])

        self._record()
        for limit in (1, 21):
            with self.subTest(limit=limit):
                self.assertEqual(len(self.memory.list_audit_summaries(limit)), 1)


class AuditSummaryWriteFailureTests(_AuditDatabaseTestCase):
    """Prove every failed summary write raises one fixed failure and logs one line."""

    def test_an_insert_that_adds_no_row_is_refused(self) -> None:
        _execute(
            self.database_path,
            "CREATE TRIGGER audit_ignored BEFORE INSERT ON audit_summaries "
            "BEGIN SELECT RAISE(IGNORE); END",
        )

        with self.assertRaises(RuntimeError) as caught:
            self._record()

        self._assert_fixed_failure(caught.exception, RECORD_FAILED)
        self.assertEqual(_audit_rows(self.database_path), ([], []))
        self._assert_logged(
            ("error", "Failed to record audit summary: no row was inserted.")
        )

    def test_a_returned_id_that_is_not_an_exact_positive_int_is_refused(
        self,
    ) -> None:
        for lastrowid in (True, 0, -1, None, "1", 1.0, _Whole(1)):
            with self.subTest(lastrowid=_labelled(lastrowid)):
                self.logger.reset_mock()
                connection = self._fake_connection(rowcount=1, lastrowid=lastrowid)
                with patch.object(self.memory, "_connect", return_value=connection):
                    with self.assertRaises(RuntimeError) as caught:
                        self._record()

                self._assert_fixed_failure(caught.exception, RECORD_FAILED)
                self._assert_logged(
                    (
                        "error",
                        "Failed to record audit summary: no valid id was returned.",
                    )
                )
                connection.close.assert_called_once_with()

        # The same fake reporting a valid id is accepted, so the refusals above
        # come from the id alone.
        self.logger.reset_mock()
        connection = self._fake_connection(rowcount=1, lastrowid=7)
        with patch.object(self.memory, "_connect", return_value=connection):
            self.assertEqual(self._record(), 7)
        self._assert_logged(("info", "Recorded audit summary 7."))

    def test_a_rowcount_other_than_one_is_refused(self) -> None:
        for rowcount in (0, 2, -1):
            with self.subTest(rowcount=rowcount):
                self.logger.reset_mock()
                connection = self._fake_connection(rowcount=rowcount, lastrowid=1)
                with patch.object(self.memory, "_connect", return_value=connection):
                    with self.assertRaises(RuntimeError) as caught:
                        self._record()

                self._assert_fixed_failure(caught.exception, RECORD_FAILED)
                self._assert_logged(
                    ("error", "Failed to record audit summary: no row was inserted.")
                )

    def test_a_refused_insert_never_repeats_sqlites_text(self) -> None:
        _execute(
            self.database_path,
            "CREATE TRIGGER audit_refused BEFORE INSERT ON audit_summaries "
            f"BEGIN SELECT RAISE(ABORT, '{SQLITE_SENTINEL}'); END",
        )
        self.assertIn(
            SQLITE_SENTINEL,
            _sqlite_failure_text(
                self.database_path,
                "INSERT INTO audit_summaries (created_at, action, decision, target) "
                "VALUES (?, ?, ?, ?)",
                (STAMP, "calculator.calculate", "owner_approved", "none"),
            ),
        )

        with self.assertRaises(RuntimeError) as caught:
            self._record()

        self._assert_fixed_failure(caught.exception, RECORD_FAILED, SQLITE_SENTINEL)
        self.assertEqual(_audit_rows(self.database_path), ([], []))
        self._assert_logged(
            ("error", "Failed to record audit summary: database error.")
        )

    def test_a_failed_commit_is_refused_and_rolled_back(self) -> None:
        connection = self._fake_connection(rowcount=1, lastrowid=1)
        connection.commit.side_effect = sqlite3.OperationalError(SQLITE_SENTINEL)
        with patch.object(self.memory, "_connect", return_value=connection):
            with self.assertRaises(RuntimeError) as caught:
                self._record()

        self._assert_fixed_failure(caught.exception, RECORD_FAILED, SQLITE_SENTINEL)
        connection.rollback.assert_called_once_with()
        connection.close.assert_called_once_with()
        self._assert_logged(
            ("error", "Failed to record audit summary: database error.")
        )

    def test_contention_at_commit_keeps_the_busy_message(self) -> None:
        busy = sqlite3.OperationalError(SQLITE_SENTINEL)
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        connection = self._fake_connection(rowcount=1, lastrowid=1)
        connection.commit.side_effect = busy
        with patch.object(self.memory, "_connect", return_value=connection):
            with self.assertRaises(RuntimeError) as caught:
                self._record()

        self._assert_fixed_failure(caught.exception, BUSY_MESSAGE, SQLITE_SENTINEL)
        self._assert_logged(
            ("error", "Failed to record audit summary: database busy.")
        )

    def test_a_held_lock_is_reported_as_busy_without_a_traceback(self) -> None:
        holder = _hold_exclusive_lock(self, self.database_path)
        with patch.object(
            memory_module,
            "DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS",
            SHORT_BUSY_TIMEOUT_SECONDS,
        ):
            with self.assertRaises(RuntimeError) as caught:
                self._record()
        holder.rollback()

        self._assert_fixed_failure(caught.exception, BUSY_MESSAGE)
        self._assert_logged(
            ("error", "Failed to record audit summary: database busy.")
        )
        self.assertEqual(_audit_rows(self.database_path), ([], []))


class RecordAuditOutcomeTests(_AuditDatabaseTestCase):
    """Prove an outcome is stored once, and only for a summary that exists."""

    def test_an_outcome_is_stored_for_its_summary(self) -> None:
        first = self._record()
        second = self._record("filesystem.list", AuditTarget.NAMED_FILESYSTEM_ROOT)
        self.logger.reset_mock()

        stored_first = self.memory.record_audit_outcome(first, AuditOutcome.SUCCEEDED)
        stored_second = self.memory.record_audit_outcome(second, AuditOutcome.FAILED)

        self.assertIsNone(stored_first)
        self.assertIsNone(stored_second)

        self.assertEqual(
            _fetch(
                self.database_path,
                "SELECT summary_id, typeof(summary_id), outcome, typeof(outcome) "
                "FROM audit_outcomes ORDER BY summary_id",
            ),
            [(1, "integer", "succeeded", "text"), (2, "integer", "failed", "text")],
        )
        self._assert_logged(
            ("info", "Recorded the outcome of audit summary 1."),
            ("info", "Recorded the outcome of audit summary 2."),
        )

    def test_an_outcome_for_a_missing_summary_adds_no_row(self) -> None:
        self._record()
        self.logger.reset_mock()

        with self.assertRaises(RuntimeError) as caught:
            self.memory.record_audit_outcome(2, AuditOutcome.SUCCEEDED)

        self._assert_fixed_failure(caught.exception, OUTCOME_FAILED)
        self.assertEqual(_audit_rows(self.database_path)[1], [])
        self._assert_logged(
            ("error", "Failed to record audit outcome: no row was inserted.")
        )

    def test_a_second_outcome_is_refused_and_the_first_is_kept(self) -> None:
        summary_id = self._record()
        self.memory.record_audit_outcome(summary_id, AuditOutcome.SUCCEEDED)
        self.logger.reset_mock()

        with self.assertRaises(RuntimeError) as caught:
            self.memory.record_audit_outcome(summary_id, AuditOutcome.FAILED)

        self._assert_fixed_failure(caught.exception, OUTCOME_FAILED)
        self.assertEqual(_audit_rows(self.database_path)[1], [(1, "succeeded")])
        self._assert_logged(
            ("error", "Failed to record audit outcome: database error.")
        )

    def test_an_outcome_for_a_deleted_summary_leaves_no_orphan(self) -> None:
        summary_id = self._record()
        self.assertIs(self.memory.delete_audit_summary(summary_id), True)
        self.logger.reset_mock()

        with self.assertRaises(RuntimeError) as caught:
            self.memory.record_audit_outcome(summary_id, AuditOutcome.SUCCEEDED)

        self._assert_fixed_failure(caught.exception, OUTCOME_FAILED)
        self.assertEqual(_table_counts(self.database_path), ZERO_ROWS)
        self._assert_logged(
            ("error", "Failed to record audit outcome: no row was inserted.")
        )

    def test_a_refused_outcome_never_repeats_sqlites_text(self) -> None:
        summary_id = self._record()
        _execute(
            self.database_path,
            "CREATE TRIGGER outcome_refused BEFORE INSERT ON audit_outcomes "
            f"BEGIN SELECT RAISE(ABORT, '{SQLITE_SENTINEL}'); END",
        )
        self.assertIn(
            SQLITE_SENTINEL,
            _sqlite_failure_text(
                self.database_path,
                "INSERT INTO audit_outcomes (summary_id, outcome) VALUES (?, ?)",
                (summary_id, "succeeded"),
            ),
        )
        self.logger.reset_mock()

        with self.assertRaises(RuntimeError) as caught:
            self.memory.record_audit_outcome(summary_id, AuditOutcome.SUCCEEDED)

        self._assert_fixed_failure(caught.exception, OUTCOME_FAILED, SQLITE_SENTINEL)
        self.assertEqual(_audit_rows(self.database_path)[1], [])
        self._assert_logged(
            ("error", "Failed to record audit outcome: database error.")
        )


class ListAuditSummariesTests(_AuditDatabaseTestCase):
    """Prove listing is newest first, bounded, joined, and validated on read."""

    def test_an_empty_table_lists_nothing(self) -> None:
        listed = self.memory.list_audit_summaries()
        self.assertIs(type(listed), list)
        self.assertEqual(listed, [])
        self._assert_logged()

    def test_summaries_are_listed_newest_first_with_their_outcomes(self) -> None:
        first = self._record(at=STAMP)
        second = self._record(
            "filesystem.list",
            AuditTarget.NAMED_FILESYSTEM_ROOT,
            at="2026-10-01T09:46:18Z",
        )
        third = self._record("system.current_time", at="2026-10-01T09:46:19Z")
        self.memory.record_audit_outcome(first, AuditOutcome.SUCCEEDED)
        self.memory.record_audit_outcome(third, AuditOutcome.FAILED)

        listed = self.memory.list_audit_summaries()

        self.assertEqual(
            listed,
            [
                AuditSummary(
                    third,
                    "2026-10-01T09:46:19Z",
                    "system.current_time",
                    AuditDecision.OWNER_APPROVED,
                    AuditTarget.NONE,
                    AuditOutcome.FAILED,
                ),
                AuditSummary(
                    second,
                    "2026-10-01T09:46:18Z",
                    "filesystem.list",
                    AuditDecision.OWNER_APPROVED,
                    AuditTarget.NAMED_FILESYSTEM_ROOT,
                    None,
                ),
                AuditSummary(
                    first,
                    STAMP,
                    "calculator.calculate",
                    AuditDecision.OWNER_APPROVED,
                    AuditTarget.NONE,
                    AuditOutcome.SUCCEEDED,
                ),
            ],
        )
        self.assertEqual([type(summary) for summary in listed], [AuditSummary] * 3)

    def test_twenty_five_summaries_yield_the_newest_twenty_one(self) -> None:
        self.assertEqual(
            inspect.signature(Memory.list_audit_summaries).parameters["limit"].default,
            21,
        )
        with patch.object(memory_module, "audit_timestamp", return_value=STAMP):
            for _ in range(25):
                self.memory.record_audit_summary(
                    "calculator.calculate",
                    AuditDecision.OWNER_APPROVED,
                    AuditTarget.NONE,
                )

        newest = [summary.summary_id for summary in self.memory.list_audit_summaries()]
        self.assertEqual(newest, list(range(25, 4, -1)))
        self.assertEqual(len(newest), 21)
        for limit, expected in (
            (21, list(range(25, 4, -1))),
            (20, list(range(25, 5, -1))),
            (1, [25]),
        ):
            with self.subTest(limit=limit):
                self.assertEqual(
                    [
                        summary.summary_id
                        for summary in self.memory.list_audit_summaries(limit)
                    ],
                    expected,
                )

    def test_a_stored_value_outside_the_vocabulary_fails_the_whole_listing(
        self,
    ) -> None:
        for label, values in TAMPERED_ROWS:
            with self.subTest(row=label):
                # One valid row beside the tampered one: nothing is listed
                # partially either.
                _replace_audit_rows(
                    self.database_path,
                    {"outcome": "succeeded"},
                    values,
                )
                self.logger.reset_mock()

                with self.assertRaises(RuntimeError) as caught:
                    self.memory.list_audit_summaries()

                self._assert_fixed_failure(
                    caught.exception,
                    LIST_FAILED,
                    TAMPER_SENTINEL,
                )
                self._assert_logged(
                    ("error", f"Failed to list audit summaries: {OUTSIDE_VOCABULARY}.")
                )

    def test_a_non_integer_stored_id_is_refused(self) -> None:
        # The live table's integer key can hold nothing but an integer, so the
        # table is reshaped behind the memory layer's back.
        _execute(self.database_path, "DROP TABLE audit_summaries")
        _execute(
            self.database_path,
            "CREATE TABLE audit_summaries (id, created_at, action, decision, target)",
        )
        for stored_id in (TAMPER_SENTINEL, 3.0, b"3"):
            with self.subTest(stored_id=_labelled(stored_id)):
                _replace_audit_rows(self.database_path, {"id": stored_id})
                self.logger.reset_mock()

                with self.assertRaises(RuntimeError) as caught:
                    self.memory.list_audit_summaries()

                self._assert_fixed_failure(
                    caught.exception,
                    LIST_FAILED,
                    TAMPER_SENTINEL,
                )
                self._assert_logged(
                    ("error", f"Failed to list audit summaries: {OUTSIDE_VOCABULARY}.")
                )

        # A real number that equals the requested id is found, and refused.
        _replace_audit_rows(self.database_path, {"id": 3.0})
        self.logger.reset_mock()
        with self.assertRaises(RuntimeError) as caught:
            self.memory.get_audit_summary(3)
        self._assert_fixed_failure(caught.exception, READ_FAILED)
        self._assert_logged(
            ("error", f"Failed to read audit summary: {OUTSIDE_VOCABULARY}.")
        )

    def test_a_failed_read_never_repeats_sqlites_text(self) -> None:
        self._record()
        # SQLite names the missing table in its own error text.
        _execute(self.database_path, "DROP TABLE audit_outcomes")
        _execute(
            self.database_path,
            "CREATE VIEW audit_outcomes AS SELECT summary_id, outcome "
            f"FROM missing_{SQLITE_SENTINEL}",
        )
        self.assertIn(
            SQLITE_SENTINEL,
            _sqlite_failure_text(self.database_path, "SELECT * FROM audit_outcomes"),
        )
        self.logger.reset_mock()

        with self.assertRaises(RuntimeError) as caught:
            self.memory.list_audit_summaries()

        self._assert_fixed_failure(caught.exception, LIST_FAILED, SQLITE_SENTINEL)
        self._assert_logged(
            ("error", "Failed to list audit summaries: database error.")
        )


class GetAuditSummaryTests(_AuditDatabaseTestCase):
    """Prove one summary is read with its outcome, or None, or refused."""

    def test_a_summary_is_read_with_its_outcome_or_none(self) -> None:
        first = self._record()
        second = self._record(
            "filesystem.list",
            AuditTarget.NAMED_FILESYSTEM_ROOT,
            at="2026-10-01T09:46:18Z",
        )
        self.memory.record_audit_outcome(first, AuditOutcome.SUCCEEDED)
        self.logger.reset_mock()

        self.assertEqual(
            self.memory.get_audit_summary(first),
            AuditSummary(
                first,
                STAMP,
                "calculator.calculate",
                AuditDecision.OWNER_APPROVED,
                AuditTarget.NONE,
                AuditOutcome.SUCCEEDED,
            ),
        )
        read = self.memory.get_audit_summary(second)
        self.assertIs(type(read), AuditSummary)
        self.assertEqual(
            read,
            AuditSummary(
                second,
                "2026-10-01T09:46:18Z",
                "filesystem.list",
                AuditDecision.OWNER_APPROVED,
                AuditTarget.NAMED_FILESYSTEM_ROOT,
                None,
            ),
        )
        self._assert_logged()

    def test_an_absent_or_deleted_id_reads_as_none(self) -> None:
        self.assertIsNone(self.memory.get_audit_summary(1))
        summary_id = self._record()
        self.assertIsNone(self.memory.get_audit_summary(summary_id + 1))
        self.assertTrue(self.memory.delete_audit_summary(summary_id))
        self.assertIsNone(self.memory.get_audit_summary(summary_id))

    def test_a_tampered_summary_is_refused_with_a_fixed_message(self) -> None:
        for label, values in TAMPERED_ROWS:
            if "id" in values:
                # A non-positive id can never be requested; listing covers it.
                continue
            with self.subTest(row=label):
                [summary_id] = _replace_audit_rows(self.database_path, values)
                self.logger.reset_mock()

                with self.assertRaises(RuntimeError) as caught:
                    self.memory.get_audit_summary(summary_id)

                self._assert_fixed_failure(
                    caught.exception,
                    READ_FAILED,
                    TAMPER_SENTINEL,
                )
                self._assert_logged(
                    ("error", f"Failed to read audit summary: {OUTSIDE_VOCABULARY}.")
                )

    def test_a_failed_read_never_repeats_sqlites_text(self) -> None:
        summary_id = self._record()
        _execute(self.database_path, "DROP TABLE audit_outcomes")
        _execute(
            self.database_path,
            "CREATE VIEW audit_outcomes AS SELECT summary_id, outcome "
            f"FROM missing_{SQLITE_SENTINEL}",
        )
        self.assertIn(
            SQLITE_SENTINEL,
            _sqlite_failure_text(self.database_path, "SELECT * FROM audit_outcomes"),
        )
        self.logger.reset_mock()

        with self.assertRaises(RuntimeError) as caught:
            self.memory.get_audit_summary(summary_id)

        self._assert_fixed_failure(caught.exception, READ_FAILED, SQLITE_SENTINEL)
        self._assert_logged(("error", "Failed to read audit summary: database error."))


class DeleteAuditSummaryTests(_AuditDatabaseTestCase):
    """Prove a summary and its outcome are deleted together, by id, or not at all."""

    def test_a_summary_and_its_outcome_are_deleted_together(self) -> None:
        first = self._record()
        second = self._record("system.current_time")
        third = self._record("filesystem.list", AuditTarget.NAMED_FILESYSTEM_ROOT)
        self.memory.record_audit_outcome(first, AuditOutcome.SUCCEEDED)
        self.memory.record_audit_outcome(second, AuditOutcome.FAILED)
        self.logger.reset_mock()

        self.assertIs(self.memory.delete_audit_summary(second), True)
        self.assertIs(self.memory.delete_audit_summary(third), True)

        self.assertEqual(
            _audit_rows(self.database_path),
            (
                [(first, STAMP, "calculator.calculate", "owner_approved", "none")],
                [(first, "succeeded")],
            ),
        )
        self._assert_logged(
            ("info", f"Deleted audit summary {second}."),
            ("info", f"Deleted audit summary {third}."),
        )

    def test_a_missing_id_returns_false_and_changes_nothing(self) -> None:
        summary_id = self._record()
        self.memory.record_audit_outcome(summary_id, AuditOutcome.SUCCEEDED)
        before = self._snapshot()
        self.logger.reset_mock()

        self.assertIs(self.memory.delete_audit_summary(summary_id + 1), False)
        self.assertEqual(self._snapshot(), before)
        self.assertIs(self.memory.delete_audit_summary(summary_id), True)
        self.assertIs(self.memory.delete_audit_summary(summary_id), False)

        self.assertEqual(_table_counts(self.database_path), ZERO_ROWS)
        self._assert_logged(("info", f"Deleted audit summary {summary_id}."))

    def test_an_id_is_never_reused_after_the_newest_is_deleted(self) -> None:
        self._record()
        self._record()
        newest = self._record()
        self.memory.record_audit_outcome(newest, AuditOutcome.SUCCEEDED)
        self.assertTrue(self.memory.delete_audit_summary(newest))

        replacement = self._record()

        self.assertEqual((newest, replacement), (3, 4))
        # The deleted summary's outcome went with it and attaches to nothing.
        self.assertIsNone(self.memory.get_audit_summary(replacement).outcome)
        self.assertIsNone(self.memory.get_audit_summary(newest))

        for summary_id in (1, 2, replacement):
            self.assertTrue(self.memory.delete_audit_summary(summary_id))
        self.assertEqual(self._record(), 5)

    def test_a_deletion_is_one_immediate_transaction(self) -> None:
        present = self._record()
        self.memory.record_audit_outcome(present, AuditOutcome.SUCCEEDED)
        statements = self._traced_statements()

        self.assertIs(self.memory.delete_audit_summary(present), True)
        deleted = [" ".join(statement.split()) for statement in statements]
        statements.clear()
        self.assertIs(self.memory.delete_audit_summary(present + 1), False)
        missing = [" ".join(statement.split()) for statement in statements]

        for ran, outcome_deletes in ((deleted, 1), (missing, 0)):
            with self.subTest(outcome_deletes=outcome_deletes):
                self.assertEqual(ran[0], "BEGIN IMMEDIATE")
                self.assertEqual(ran[-1], "COMMIT")
                self.assertEqual(ran.count("COMMIT"), 1)
                self.assertEqual(
                    sum("DELETE FROM audit_summaries" in line for line in ran),
                    1,
                )
                # The outcome is deleted only when its summary was.
                self.assertEqual(
                    sum("DELETE FROM audit_outcomes" in line for line in ran),
                    outcome_deletes,
                )

    def test_a_failed_outcome_delete_rolls_the_whole_deletion_back(self) -> None:
        summary_id = self._record()
        self.memory.record_audit_outcome(summary_id, AuditOutcome.SUCCEEDED)
        _execute(
            self.database_path,
            "CREATE TRIGGER outcome_kept BEFORE DELETE ON audit_outcomes "
            f"BEGIN SELECT RAISE(ABORT, '{SQLITE_SENTINEL}'); END",
        )
        self.assertIn(
            SQLITE_SENTINEL,
            _sqlite_failure_text(
                self.database_path,
                "DELETE FROM audit_outcomes WHERE summary_id = ?",
                (summary_id,),
            ),
        )
        before = self._snapshot()
        self.logger.reset_mock()

        with self.assertRaises(RuntimeError) as caught:
            self.memory.delete_audit_summary(summary_id)

        self._assert_fixed_failure(caught.exception, DELETE_FAILED, SQLITE_SENTINEL)
        self.assertEqual(self._snapshot(), before)
        self._assert_logged(
            ("error", "Failed to delete audit summary: database error.")
        )


class AuditContentionTests(_AuditDatabaseTestCase):
    """Prove every audit call reports a held lock as busy, with no traceback."""

    def test_every_audit_call_reports_a_held_lock_as_busy(self) -> None:
        summary_id = self._record()
        before = self._snapshot()
        attempts: tuple[tuple[str, Callable[[], object]], ...] = (
            (
                "record audit summary",
                lambda: self.memory.record_audit_summary(
                    "calculator.calculate",
                    AuditDecision.OWNER_APPROVED,
                    AuditTarget.NONE,
                ),
            ),
            (
                "record audit outcome",
                lambda: self.memory.record_audit_outcome(
                    summary_id,
                    AuditOutcome.SUCCEEDED,
                ),
            ),
            ("list audit summaries", self.memory.list_audit_summaries),
            ("read audit summary", lambda: self.memory.get_audit_summary(summary_id)),
            (
                "delete audit summary",
                lambda: self.memory.delete_audit_summary(summary_id),
            ),
        )

        holder = _hold_exclusive_lock(self, self.database_path)
        with patch.object(
            memory_module,
            "DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS",
            SHORT_BUSY_TIMEOUT_SECONDS,
        ):
            for operation, attempt in attempts:
                with self.subTest(operation=operation):
                    self.logger.reset_mock()
                    with self.assertRaises(RuntimeError) as caught:
                        attempt()
                    self._assert_fixed_failure(caught.exception, BUSY_MESSAGE)
                    self._assert_logged(
                        ("error", f"Failed to {operation}: database busy.")
                    )
        holder.rollback()

        self.assertEqual(self._snapshot(), before)


# --- Through Maria's real components ------------------------------------------------


class _AuditHarness:
    """Real planner, contracts, permissions, tools, memory, Brain, and session.

    Everything lives under one temporary directory, and the configured
    confirmation-required and denied actions are the test's own. Planning,
    contract validation, dispatch, confirmation preparation, permission
    checks, and both audit writes are wrapped by spies that still call the
    real components; ``events`` notes each call in the order it happened,
    and ``dispatch_counts`` holds the row counts a separate connection saw
    at the moment of each dispatch.
    """

    def __init__(
        self,
        test: unittest.TestCase,
        provider: object | None = None,
        *,
        confirmation_required: frozenset[str] = frozenset(),
        denied_actions: frozenset[str] = frozenset(),
        logger: logging.Logger | None = None,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.workspace = root / "workspace"
        (self.workspace / "inbox").mkdir(parents=True)
        (self.workspace / "archive").mkdir()
        settings = DISABLED_REASONING if provider is None else enabled_settings()
        self.config = replace(
            _test_config(root, settings),
            confirmation_required=frozenset(confirmation_required),
            denied_actions=frozenset(denied_actions),
        )
        self.database_path = self.config.database_path

        # Tool warnings stay off the console. A test capturing the log adds
        # its own handler beside this one.
        namespace = logging.getLogger("maria")
        quiet = logging.NullHandler()
        namespace.addHandler(quiet)
        test.addCleanup(namespace.removeHandler, quiet)

        self.logger = logger or _quiet_logger("tests.audit.harness")
        self.memory = Memory(self.database_path, logger=self.logger)
        self.memory.initialize()
        self.tool_manager = ToolManager(
            config=self.config,
            memory=self.memory,
            logger=self.logger,
        )
        self.tool_manager.load_tools()
        self.permissions = PermissionManager(
            confirmation_required=self.config.confirmation_required,
            denied_actions=self.config.denied_actions,
            logger=self.logger,
        )
        self.planner = Planner(logger=self.logger)

        self.events: list[str] = []
        self.dispatch_counts: list[dict[str, int]] = []
        self.create_plan = self._observe(test, self.planner, "create_plan")
        self.validate_action = self._observe(
            test,
            self.tool_manager,
            "validate_action",
        )
        self.execute_tool = self._observe(
            test,
            self.tool_manager,
            "execute_tool",
            before=lambda: self.dispatch_counts.append(self.row_counts()),
        )
        self.prepare_confirmation = self._observe(
            test,
            self.tool_manager,
            "prepare_confirmation",
        )
        self.check = self._observe(test, self.permissions, "check")
        self.record_audit_summary = self._observe(
            test,
            self.memory,
            "record_audit_summary",
        )
        self.record_audit_outcome = self._observe(
            test,
            self.memory,
            "record_audit_outcome",
        )

        self.provider = provider
        self.brain = MariaBrain(
            config=self.config,
            planner=self.planner,
            memory=self.memory,
            tool_manager=self.tool_manager,
            permissions=self.permissions,
            logger=self.logger,
            reasoning=provider,
        )

    def _observe(
        self,
        test: unittest.TestCase,
        owner: object,
        name: str,
        before: Callable[[], None] | None = None,
    ) -> Mock:
        """Wrap one method so it still runs, counting its calls and their order."""
        original = getattr(owner, name)

        def observed(*args: object, **kwargs: object) -> object:
            self.events.append(name)
            if before is not None:
                before()
            return original(*args, **kwargs)

        active = patch.object(owner, name, wraps=observed)
        spy = active.start()
        test.addCleanup(active.stop)
        return spy

    def session(self) -> MariaSession:
        """Return a fresh session over this harness's Brain."""
        return MariaSession(self.brain, logger=self.logger)

    def row_counts(self) -> dict[str, int]:
        """Count the rows of all five record tables."""
        return _table_counts(self.database_path)

    def audit_rows(self) -> tuple[list[tuple[object, ...]], ...]:
        """Return every stored audit summary and outcome, exactly as stored."""
        return _audit_rows(self.database_path)


class ApprovedProposalAuditTests(unittest.TestCase):
    """Prove what an approved model proposal writes, and when, end to end."""

    def _offer(
        self,
        reply: str,
        **policy: frozenset[str],
    ) -> tuple[_AuditHarness, MariaSession, BrainResponse]:
        harness = _AuditHarness(self, ScriptedProvider.replying(reply), **policy)
        session = harness.session()
        offered = session.submit(UNRECOGNISED_REQUEST)
        return harness, session, offered

    def _approve(
        self,
        reply: str,
        **policy: frozenset[str],
    ) -> tuple[_AuditHarness, BrainResponse, BrainResponse]:
        harness, session, offered = self._offer(reply, **policy)
        self.assertIs(offered.needs_confirmation, True)
        self.assertEqual(harness.row_counts(), ZERO_ROWS)
        harness.record_audit_summary.assert_not_called()
        harness.execute_tool.assert_not_called()
        with patch.object(memory_module, "audit_timestamp", return_value=STAMP):
            result = session.confirm()
        return harness, offered, result

    def _clock_report(self) -> str:
        """Return the clock tool's own report at the pinned moment."""
        with patch.object(system_module, "_current_moment", return_value=CLOCK_MOMENT):
            report = (
                system_module.SystemTool(logger=_quiet_logger("tests.audit.system"))
                .execute("current_time", {})
                .message
            )
        # The pin took effect, so the report is this moment's exact text.
        self.assertIn(CLOCK_ISO, report)
        return report

    def _audited(self, harness: _AuditHarness) -> list[str]:
        """Return the permission, audit, and dispatch calls, in order."""
        return [
            event
            for event in harness.events
            if event
            in (
                "check",
                "prepare_confirmation",
                "record_audit_summary",
                "execute_tool",
                "record_audit_outcome",
            )
        ]

    def test_a_calculation_is_audited_before_it_runs_and_its_success_after(
        self,
    ) -> None:
        harness, offered, result = self._approve(CALCULATION_PROPOSAL)

        self.assertEqual(
            offered.text,
            _approval("calculator", "calculate", '{"expression": "6 * 7"}'),
        )
        self.assertEqual(result.text, "6 * 7 = 42\n\nRecorded as audit summary 1.")
        self.assertIs(result.needs_confirmation, False)
        self.assertIs(result.plan, offered.plan)

        harness.record_audit_summary.assert_called_once_with(
            "calculator.calculate",
            AuditDecision.OWNER_APPROVED,
            AuditTarget.NONE,
        )
        harness.record_audit_outcome.assert_called_once_with(1, AuditOutcome.SUCCEEDED)
        approved_arguments = offered.plan.steps[0].arguments
        harness.execute_tool.assert_called_once()
        self.assertIs(harness.execute_tool.call_args.args[2], approved_arguments)
        # Dispatch's own contract pass saw exactly the approved arguments.
        harness.validate_action.assert_called_once()
        self.assertIs(harness.validate_action.call_args.args[2], approved_arguments)
        harness.prepare_confirmation.assert_not_called()
        harness.create_plan.assert_called_once_with(UNRECOGNISED_REQUEST)
        self.assertEqual(
            harness.check.call_args_list,
            [call("calculator", "calculate")] * 2,
        )
        self.assertEqual(harness.provider.calls, 1)

        self.assertEqual(
            self._audited(harness),
            [
                "check",
                "check",
                "record_audit_summary",
                "execute_tool",
                "record_audit_outcome",
            ],
        )
        # When the tool ran, its summary was already committed and its
        # outcome not yet written.
        self.assertEqual(
            harness.dispatch_counts,
            [{**ZERO_ROWS, "audit_summaries": 1}],
        )
        self.assertEqual(
            harness.row_counts(),
            {**ZERO_ROWS, "audit_summaries": 1, "audit_outcomes": 1},
        )
        self.assertEqual(
            harness.audit_rows(),
            (
                [(1, STAMP, "calculator.calculate", "owner_approved", "none")],
                [(1, "succeeded")],
            ),
        )
        self.assertEqual(
            harness.memory.list_audit_summaries(),
            [
                AuditSummary(
                    1,
                    STAMP,
                    "calculator.calculate",
                    AuditDecision.OWNER_APPROVED,
                    AuditTarget.NONE,
                    AuditOutcome.SUCCEEDED,
                )
            ],
        )

    def test_a_listing_with_no_path_is_audited_as_acting_in_a_named_root(
        self,
    ) -> None:
        harness, offered, result = self._approve(LISTING_PROPOSAL)

        self.assertEqual(offered.text, _approval("filesystem", "list", "{}"))
        self.assertEqual(
            result.text,
            "directory: archive\ndirectory: inbox\n\nRecorded as audit summary 1.",
        )
        harness.record_audit_summary.assert_called_once_with(
            "filesystem.list",
            AuditDecision.OWNER_APPROVED,
            AuditTarget.NAMED_FILESYSTEM_ROOT,
        )
        self.assertEqual(
            harness.audit_rows(),
            ([STORED_LISTING_SUMMARY], [(1, "succeeded")]),
        )

    def test_a_listing_with_a_path_stores_the_target_class_not_the_path(
        self,
    ) -> None:
        harness, offered, result = self._approve(INBOX_LISTING_PROPOSAL)

        self.assertEqual(
            offered.text,
            _approval("filesystem", "list", '{"path": "inbox"}'),
        )
        self.assertEqual(
            result.text,
            "Directory is empty.\n\nRecorded as audit summary 1.",
        )
        stored = harness.audit_rows()
        self.assertEqual(stored, ([STORED_LISTING_SUMMARY], [(1, "succeeded")]))
        self.assertNotIn("inbox", repr(stored))
        self.assertNotIn(str(harness.workspace), repr(stored))

    def test_reading_the_clock_is_audited_with_no_target(self) -> None:
        with patch.object(system_module, "_current_moment", return_value=CLOCK_MOMENT):
            harness, offered, result = self._approve(CLOCK_PROPOSAL)

        self.assertEqual(offered.text, _approval("system", "current_time", "{}"))
        self.assertEqual(
            result.text,
            f"{self._clock_report()}\n\nRecorded as audit summary 1.",
        )
        harness.record_audit_summary.assert_called_once_with(
            "system.current_time",
            AuditDecision.OWNER_APPROVED,
            AuditTarget.NONE,
        )
        self.assertEqual(
            harness.audit_rows(),
            (
                [(1, STAMP, "system.current_time", "owner_approved", "none")],
                [(1, "succeeded")],
            ),
        )

    def test_a_tool_reporting_failure_is_recorded_as_failed(self) -> None:
        harness, _, result = self._approve(FAILING_CALCULATION_PROPOSAL)

        tool_text = (
            CalculatorTool(logger=_quiet_logger("tests.audit.calculator"))
            .execute("calculate", {"expression": "1 / 0"})
            .message
        )
        self.assertEqual(result.text, f"{tool_text}\n\nRecorded as audit summary 1.")
        harness.record_audit_outcome.assert_called_once_with(1, AuditOutcome.FAILED)
        # A failed run is still one run, under a summary committed before it.
        harness.execute_tool.assert_called_once()
        self.assertEqual(
            self._audited(harness),
            [
                "check",
                "check",
                "record_audit_summary",
                "execute_tool",
                "record_audit_outcome",
            ],
        )
        self.assertEqual(
            harness.dispatch_counts,
            [{**ZERO_ROWS, "audit_summaries": 1}],
        )
        self.assertEqual(
            harness.audit_rows(),
            (
                [(1, STAMP, "calculator.calculate", "owner_approved", "none")],
                [(1, "failed")],
            ),
        )

    def test_a_denied_action_is_refused_before_any_summary(self) -> None:
        harness, session, offered = self._offer(
            CALCULATION_PROPOSAL,
            denied_actions=frozenset({"calculator.calculate"}),
        )

        self.assertEqual(
            offered.text,
            "Permission denied: calculator.calculate is denied by configuration."
            "\n\n" + NO_CONTEXT_NOTICE,
        )
        self.assertIs(offered.needs_confirmation, False)
        self.assertIsNone(session.pending_approval)
        self.assertEqual(session.confirm().text, CONFIRMATION_REFUSED_MESSAGE)

        self.assertEqual(self._audited(harness), ["check"])
        harness.record_audit_summary.assert_not_called()
        harness.record_audit_outcome.assert_not_called()
        harness.validate_action.assert_not_called()
        harness.execute_tool.assert_not_called()
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_configured_confirmation_still_takes_one_approval_and_one_summary(
        self,
    ) -> None:
        with patch.object(system_module, "_current_moment", return_value=CLOCK_MOMENT):
            harness, session, offered = self._offer(
                CLOCK_PROPOSAL,
                confirmation_required=frozenset({"system.current_time"}),
            )
            self.assertIs(offered.needs_confirmation, True)
            # The model approval, not the deterministic confirmation request.
            self.assertEqual(offered.text, _approval("system", "current_time", "{}"))

            with patch.object(memory_module, "audit_timestamp", return_value=STAMP):
                result = session.confirm()
                replayed = session.confirm()

        self.assertEqual(
            result.text,
            f"{self._clock_report()}\n\nRecorded as audit summary 1.",
        )
        self.assertEqual(replayed.text, CONFIRMATION_REFUSED_MESSAGE)
        # One check to offer and one to run; the replay reached nothing.
        self.assertEqual(
            harness.check.call_args_list,
            [call("system", "current_time")] * 2,
        )
        self.assertEqual(
            self._audited(harness),
            [
                "check",
                "check",
                "record_audit_summary",
                "execute_tool",
                "record_audit_outcome",
            ],
        )
        harness.prepare_confirmation.assert_not_called()
        self.assertEqual(harness.provider.calls, 1)
        self.assertEqual(
            harness.audit_rows(),
            (
                [(1, STAMP, "system.current_time", "owner_approved", "none")],
                [(1, "succeeded")],
            ),
        )
        self.assertEqual(
            harness.row_counts(),
            {**ZERO_ROWS, "audit_summaries": 1, "audit_outcomes": 1},
        )

    def test_a_summary_that_cannot_be_recorded_runs_nothing(self) -> None:
        harness, session, offered = self._offer(CALCULATION_PROPOSAL)
        self.assertIs(offered.needs_confirmation, True)
        _execute(
            harness.database_path,
            "CREATE TRIGGER audit_ignored BEFORE INSERT ON audit_summaries "
            "BEGIN SELECT RAISE(IGNORE); END",
        )

        result = session.confirm()
        replayed = session.confirm()

        self.assertEqual(result.text, AUDIT_SUMMARY_UNRECORDED)
        self.assertEqual(replayed.text, CONFIRMATION_REFUSED_MESSAGE)
        harness.record_audit_summary.assert_called_once()
        harness.execute_tool.assert_not_called()
        harness.record_audit_outcome.assert_not_called()
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_an_unrecorded_outcome_leaves_the_summary_showing_unknown(
        self,
    ) -> None:
        harness, session, offered = self._offer(CALCULATION_PROPOSAL)
        self.assertIs(offered.needs_confirmation, True)
        _execute(
            harness.database_path,
            "CREATE TRIGGER outcome_refused BEFORE INSERT ON audit_outcomes "
            f"BEGIN SELECT RAISE(ABORT, '{SQLITE_SENTINEL}'); END",
        )

        with patch.object(memory_module, "audit_timestamp", return_value=STAMP):
            result = session.confirm()

        self.assertEqual(
            result.text,
            "6 * 7 = 42\n\nRecorded as audit summary 1, but its outcome could not "
            "be recorded, so that summary shows the outcome as unknown.",
        )
        harness.execute_tool.assert_called_once()
        harness.record_audit_outcome.assert_called_once_with(1, AuditOutcome.SUCCEEDED)
        self.assertEqual(
            harness.row_counts(),
            {**ZERO_ROWS, "audit_summaries": 1},
        )
        self.assertEqual(
            harness.memory.list_audit_summaries(),
            [
                AuditSummary(
                    1,
                    STAMP,
                    "calculator.calculate",
                    AuditDecision.OWNER_APPROVED,
                    AuditTarget.NONE,
                    None,
                )
            ],
        )

    def test_an_action_with_no_audited_target_never_runs(self) -> None:
        harness, session, offered = self._offer(LISTING_PROPOSAL)
        self.assertIs(offered.needs_confirmation, True)

        with patch.object(
            audit_module,
            "_TARGETS_BY_ROOT_CLASS",
            MappingProxyType({}),
        ):
            result = session.confirm()

        self.assertEqual(result.text, AUDIT_SUMMARY_UNRECORDED)
        harness.record_audit_summary.assert_not_called()
        harness.execute_tool.assert_not_called()
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_an_unapproved_proposal_writes_nothing(self) -> None:
        harness, session, offered = self._offer(CALCULATION_PROPOSAL)
        self.assertIs(offered.needs_confirmation, True)

        self.assertIs(session.cancel(), True)
        self.assertEqual(session.confirm().text, CONFIRMATION_REFUSED_MESSAGE)

        self.assertEqual(self._audited(harness), ["check"])
        harness.record_audit_summary.assert_not_called()
        harness.validate_action.assert_not_called()
        harness.execute_tool.assert_not_called()
        self.assertEqual(harness.row_counts(), ZERO_ROWS)

    def test_every_audited_action_is_a_loaded_tool_action(self) -> None:
        harness = _AuditHarness(self)
        loaded = harness.tool_manager.loaded_action_identifiers()
        self.assertLessEqual(EXPECTED_AUDITED_ACTIONS, loaded)
        for action in ("audit.list", "audit.read", "audit.delete"):
            with self.subTest(action=action):
                self.assertIn(action, loaded)


# --- Logging ---------------------------------------------------------------------


class _TemporaryLogTestCase(unittest.TestCase):
    """Route Maria's logger to a temporary file and restore it afterwards."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log_dir = self.root / "logs"

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

    def _log_text(self) -> str:
        """Flush every handler and return the temporary log's text."""
        for handler in self.maria_logger.handlers:
            handler.flush()
        return (self.log_dir / "maria.log").read_text(encoding="utf-8")


class AuditLogPrivacyTests(_TemporaryLogTestCase):
    """Prove audit writes, reads, and failures log fixed lines and nothing stored."""

    def test_every_audit_failure_writes_one_fixed_line_and_no_traceback(
        self,
    ) -> None:
        configure_logging(self.log_dir, level="DEBUG")
        database_path = self.root / "database" / "maria.db"
        memory = Memory(database_path, logger=get_logger("tests.audit.privacy"))
        memory.initialize()

        def refused(attempt: Callable[[], object]) -> None:
            with self.assertRaises(RuntimeError):
                attempt()

        def record() -> int:
            return memory.record_audit_summary(
                "calculator.calculate",
                AuditDecision.OWNER_APPROVED,
                AuditTarget.NONE,
            )

        # SQLite refusing each write with the sentinel as its own text.
        _execute(
            database_path,
            "CREATE TRIGGER refuse_summary BEFORE INSERT ON audit_summaries "
            f"BEGIN SELECT RAISE(ABORT, '{SQLITE_SENTINEL}'); END",
        )
        refused(record)
        _execute(database_path, "DROP TRIGGER refuse_summary")
        summary_id = record()

        _execute(
            database_path,
            "CREATE TRIGGER refuse_outcome BEFORE INSERT ON audit_outcomes "
            f"BEGIN SELECT RAISE(ABORT, '{SQLITE_SENTINEL}'); END",
        )
        refused(lambda: memory.record_audit_outcome(summary_id, AuditOutcome.FAILED))
        _execute(database_path, "DROP TRIGGER refuse_outcome")
        memory.record_audit_outcome(summary_id, AuditOutcome.SUCCEEDED)

        _execute(
            database_path,
            "CREATE TRIGGER refuse_delete BEFORE DELETE ON audit_outcomes "
            f"BEGIN SELECT RAISE(ABORT, '{SQLITE_SENTINEL}'); END",
        )
        refused(lambda: memory.delete_audit_summary(summary_id))
        _execute(database_path, "DROP TRIGGER refuse_delete")

        # Reads SQLite fails while naming the sentinel.
        _execute(database_path, "ALTER TABLE audit_outcomes RENAME TO kept_outcomes")
        _execute(
            database_path,
            "CREATE VIEW audit_outcomes AS SELECT summary_id, outcome "
            f"FROM missing_{SQLITE_SENTINEL}",
        )
        refused(memory.list_audit_summaries)
        refused(lambda: memory.get_audit_summary(summary_id))
        _execute(database_path, "DROP VIEW audit_outcomes")
        _execute(database_path, "ALTER TABLE kept_outcomes RENAME TO audit_outcomes")

        # A stored value outside the vocabulary.
        tampered = _store_raw(database_path, {"action": TAMPER_SENTINEL})
        refused(memory.list_audit_summaries)
        refused(lambda: memory.get_audit_summary(tampered))

        # A database still locked once the busy timeout has passed.
        holder = _hold_exclusive_lock(self, database_path)
        with patch.object(
            memory_module,
            "DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS",
            SHORT_BUSY_TIMEOUT_SECONDS,
        ):
            refused(record)
        holder.rollback()

        log_text = self._log_text()
        failures = (
            "Failed to record audit summary: database error.",
            "Failed to record audit outcome: database error.",
            "Failed to delete audit summary: database error.",
            "Failed to list audit summaries: database error.",
            "Failed to read audit summary: database error.",
            f"Failed to list audit summaries: {OUTSIDE_VOCABULARY}.",
            f"Failed to read audit summary: {OUTSIDE_VOCABULARY}.",
            "Failed to record audit summary: database busy.",
        )
        for line in (
            *failures,
            "Recorded audit summary 1.",
            "Recorded the outcome of audit summary 1.",
        ):
            with self.subTest(line=line):
                # Each is a whole line of its own, written exactly once.
                self.assertEqual(log_text.count(f" | {line}\n"), 1)
        # One line per failure, and no other failure line.
        self.assertEqual(log_text.count("Failed to "), len(failures))
        self.assertNotIn("Traceback", log_text)
        self.assertNotIn("Database busy or locked.", log_text)
        assert_log_omits(
            self,
            self.log_dir / "maria.log",
            (SQLITE_SENTINEL, TAMPER_SENTINEL),
        )

    def test_an_audited_dispatch_logs_and_stores_no_request_or_path(self) -> None:
        configure_logging(self.log_dir, level="DEBUG")
        reply = (
            '{"tool": "filesystem", "action": "list", '
            f'"arguments": {{"path": "{PATH_SENTINEL}"}}}}'
        )
        harness = _AuditHarness(
            self,
            ScriptedProvider.replying(reply),
            logger=get_logger("tests.audit.dispatch"),
        )
        (harness.workspace / PATH_SENTINEL).mkdir()
        session = harness.session()

        request = f"Show me what is inside {REQUEST_SENTINEL} now"
        offered = session.submit(request)
        self.assertIs(offered.needs_confirmation, True)
        self.assertEqual(
            offered.text,
            _approval("filesystem", "list", f'{{"path": "{PATH_SENTINEL}"}}'),
        )
        # Both sentinels really travelled: the request to the model, and the
        # path to the tool.
        self.assertEqual(harness.provider.requests[0].prompt, request)
        result = session.confirm()
        self.assertEqual(
            dict(harness.execute_tool.call_args.args[2]),
            {"path": PATH_SENTINEL},
        )

        self.assertEqual(
            result.text,
            "Directory is empty.\n\nRecorded as audit summary 1.",
        )
        stored = repr(harness.audit_rows())
        for sentinel in (PATH_SENTINEL, REQUEST_SENTINEL):
            with self.subTest(stored=sentinel):
                self.assertNotIn(sentinel, stored)
        self.assertEqual(
            harness.row_counts(),
            {**ZERO_ROWS, "audit_summaries": 1, "audit_outcomes": 1},
        )

        log_text = self._log_text()
        for line in (
            "Owner approval required for model-proposed action: filesystem.list",
            "Recorded audit summary 1.",
            "Dispatching approved model-proposed action filesystem.list under "
            "audit summary 1.",
            "Listed a directory.",
            "Recorded the outcome of audit summary 1.",
        ):
            with self.subTest(line=line):
                self.assertIn(line, log_text)
        assert_log_omits(
            self,
            self.log_dir / "maria.log",
            (PATH_SENTINEL, REQUEST_SENTINEL),
        )


if __name__ == "__main__":
    unittest.main()
