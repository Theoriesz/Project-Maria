"""Basic calculator tool for Maria."""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Mapping
from logging import Logger
from typing import Any, Callable

from core.logger import get_logger
from core.tool_manager import ToolExecutionResult


NumericValue = int | float
BinaryOperator = Callable[[NumericValue, NumericValue], object]
UnaryOperator = Callable[[NumericValue], object]

MAX_EXPRESSION_LENGTH = 200
MAX_ABSOLUTE_EXPONENT = 100
MAX_INTEGER_BITS = 4096
NUMERIC_LIMIT_MESSAGE = "Calculation exceeds supported numeric limits."
EXPRESSION_NOT_TEXT_MESSAGE = "Expression must be text."
UNSUPPORTED_NUMERIC_TYPE_MESSAGE = (
    "Only integer and floating-point values are supported."
)

ALLOWED_BINARY_OPERATORS: dict[type[ast.operator], BinaryOperator] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

ALLOWED_UNARY_OPERATORS: dict[type[ast.unaryop], UnaryOperator] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


class CalculatorTool:
    """Evaluate simple arithmetic expressions safely."""

    name = "calculator"
    description = "Performs basic arithmetic calculations."
    actions = frozenset({"calculate"})

    def __init__(self, logger: Logger | None = None) -> None:
        self.logger = logger or get_logger(__name__)

    def execute(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> ToolExecutionResult:
        """Execute a calculator action."""
        if action != "calculate":
            return ToolExecutionResult(False, f"Unsupported calculator action: {action}")

        expression = arguments.get("expression", "")
        if type(expression) is not str:
            self.logger.warning("Rejected a non-text calculator expression.")
            return ToolExecutionResult(False, EXPRESSION_NOT_TEXT_MESSAGE)

        expression = expression.strip()
        if not expression:
            return ToolExecutionResult(False, "No expression provided.")

        try:
            result = evaluate_expression(expression)
        except (OverflowError, FloatingPointError):
            self.logger.warning("Calculation exceeded supported numeric limits.")
            return ToolExecutionResult(
                False,
                f"Calculation failed: {NUMERIC_LIMIT_MESSAGE}",
            )
        except (ValueError, ArithmeticError) as exc:
            self.logger.warning("Calculation failed.")
            return ToolExecutionResult(False, f"Calculation failed: {exc}")

        self.logger.info("Calculation completed.")
        return ToolExecutionResult(
            True,
            f"{expression} = {result}",
            {"expression": expression, "result": result},
        )


def evaluate_expression(expression: str) -> float | int:
    """Evaluate a small arithmetic expression using Python's AST."""
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ValueError("Expression is too long.")

    try:
        parsed = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("Expression syntax is invalid.") from exc

    return _validate_numeric(_evaluate_node(parsed))


def _evaluate_node(node: ast.AST) -> float | int:
    """Recursively evaluate an allowed AST node."""
    if isinstance(node, ast.Expression):
        return _evaluate_node(node.body)

    if isinstance(node, ast.Constant) and type(node.value) in (
        bool,
        complex,
        float,
        int,
    ):
        return _validate_numeric(node.value)

    if isinstance(node, ast.BinOp):
        operator_type = type(node.op)
        operation = ALLOWED_BINARY_OPERATORS.get(operator_type)
        if operation is None:
            raise ValueError("Operator is not allowed.")

        left = _evaluate_node(node.left)
        right = _evaluate_node(node.right)
        if operator_type is ast.Pow:
            return _checked_power(left, right, operation)
        if operator_type is ast.Mult:
            return _checked_multiply(left, right, operation)
        return _apply_binary_operation(operation, left, right)

    if isinstance(node, ast.UnaryOp):
        operation = ALLOWED_UNARY_OPERATORS.get(type(node.op))
        if operation is None:
            raise ValueError("Unary operator is not allowed.")
        operand = _evaluate_node(node.operand)
        return _apply_unary_operation(operation, operand)

    raise ValueError("Only basic arithmetic is supported.")


def _validate_numeric(value: object) -> NumericValue:
    """Validate one calculator value without changing it."""
    if type(value) is int:
        if value.bit_length() > MAX_INTEGER_BITS:
            raise ValueError(NUMERIC_LIMIT_MESSAGE)
        return value

    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(NUMERIC_LIMIT_MESSAGE)
        return value

    raise ValueError(UNSUPPORTED_NUMERIC_TYPE_MESSAGE)


def _checked_power(
    base: NumericValue,
    exponent: NumericValue,
    operation: BinaryOperator,
) -> NumericValue:
    """Raise a bounded power after rejecting guaranteed oversized integers."""
    if abs(exponent) > MAX_ABSOLUTE_EXPONENT:
        raise ValueError(NUMERIC_LIMIT_MESSAGE)

    if type(base) is int and type(exponent) is int and exponent >= 0:
        absolute_base = abs(base)
        if exponent and absolute_base > 1:
            minimum_result_bits = (
                (absolute_base.bit_length() - 1) * exponent + 1
            )
            if minimum_result_bits > MAX_INTEGER_BITS:
                raise ValueError(NUMERIC_LIMIT_MESSAGE)

    return _apply_binary_operation(operation, base, exponent)


def _checked_multiply(
    left: NumericValue,
    right: NumericValue,
    operation: BinaryOperator,
) -> NumericValue:
    """Multiply values after rejecting guaranteed oversized integers."""
    if type(left) is int and type(right) is int and left and right:
        minimum_result_bits = (
            abs(left).bit_length() + abs(right).bit_length() - 1
        )
        if minimum_result_bits > MAX_INTEGER_BITS:
            raise ValueError(NUMERIC_LIMIT_MESSAGE)

    return _apply_binary_operation(operation, left, right)


def _apply_binary_operation(
    operation: BinaryOperator,
    left: NumericValue,
    right: NumericValue,
) -> NumericValue:
    """Apply a binary operation and validate its result."""
    try:
        result = operation(left, right)
    except (OverflowError, FloatingPointError) as exc:
        raise ValueError(NUMERIC_LIMIT_MESSAGE) from exc
    return _validate_numeric(result)


def _apply_unary_operation(
    operation: UnaryOperator,
    operand: NumericValue,
) -> NumericValue:
    """Apply a unary operation and validate its result."""
    try:
        result = operation(operand)
    except (OverflowError, FloatingPointError) as exc:
        raise ValueError(NUMERIC_LIMIT_MESSAGE) from exc
    return _validate_numeric(result)


def build_tool(**_: Any) -> CalculatorTool:
    """Build a calculator tool instance."""
    return CalculatorTool()
