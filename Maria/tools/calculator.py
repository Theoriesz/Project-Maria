"""Basic calculator tool for Maria."""

from __future__ import annotations

import ast
import operator
from logging import Logger
from typing import Any, Callable

from core.logger import get_logger
from core.tool_manager import ToolExecutionResult


BinaryOperator = Callable[[float, float], float]
UnaryOperator = Callable[[float], float]

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
        arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        """Execute a calculator action."""
        if action != "calculate":
            return ToolExecutionResult(False, f"Unsupported calculator action: {action}")

        expression = str(arguments.get("expression", "")).strip()
        if not expression:
            return ToolExecutionResult(False, "No expression provided.")

        try:
            result = evaluate_expression(expression)
        except (ValueError, ZeroDivisionError) as exc:
            self.logger.warning("Calculation failed: %s", exc)
            return ToolExecutionResult(False, f"Calculation failed: {exc}")

        self.logger.info("Calculation completed.")
        return ToolExecutionResult(
            True,
            f"{expression} = {result}",
            {"expression": expression, "result": result},
        )


def evaluate_expression(expression: str) -> float | int:
    """Evaluate a small arithmetic expression using Python's AST."""
    if len(expression) > 200:
        raise ValueError("Expression is too long.")

    try:
        parsed = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("Expression syntax is invalid.") from exc

    return _evaluate_node(parsed)


def _evaluate_node(node: ast.AST) -> float | int:
    """Recursively evaluate an allowed AST node."""
    if isinstance(node, ast.Expression):
        return _evaluate_node(node.body)

    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value

    if isinstance(node, ast.BinOp):
        operator_type = type(node.op)
        operation = ALLOWED_BINARY_OPERATORS.get(operator_type)
        if operation is None:
            raise ValueError("Operator is not allowed.")

        left = _evaluate_node(node.left)
        right = _evaluate_node(node.right)
        if operator_type is ast.Pow and abs(right) > 100:
            raise ValueError("Exponent is too large.")
        return operation(left, right)

    if isinstance(node, ast.UnaryOp):
        operation = ALLOWED_UNARY_OPERATORS.get(type(node.op))
        if operation is None:
            raise ValueError("Unary operator is not allowed.")
        operand = _evaluate_node(node.operand)
        return operation(operand)

    raise ValueError("Only basic arithmetic is supported.")


def build_tool(**_: Any) -> CalculatorTool:
    """Build a calculator tool instance."""
    return CalculatorTool()
