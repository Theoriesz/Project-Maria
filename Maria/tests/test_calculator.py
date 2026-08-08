"""Focused tests for Maria's bounded calculator tool."""

from __future__ import annotations

import ast
import logging
import operator
import os
import unittest
from unittest.mock import Mock, call, patch

import tools.calculator as calculator_module
from core.tool_manager import ToolExecutionResult
from tools.calculator import CalculatorTool, build_tool, evaluate_expression


class EvaluateExpressionTests(unittest.TestCase):
    """Verify safe arithmetic evaluation and numeric resource limits."""

    def test_supported_binary_arithmetic(self) -> None:
        cases = (
            ("2 + 3", 5),
            ("7 - 10", -3),
            ("6 * 7", 42),
            ("7 / 2", 3.5),
            ("7 // 2", 3),
            ("7 % 4", 3),
            ("2 ** 10", 1024),
            ("2 * 2.5", 5.0),
        )

        for expression, expected in cases:
            with self.subTest(expression=expression):
                result = evaluate_expression(expression)
                self.assertEqual(result, expected)
                self.assertIs(type(result), type(expected))

    def test_supported_unary_arithmetic(self) -> None:
        cases = (("+5", 5), ("-5", -5), ("-5 + +2", -3))

        for expression, expected in cases:
            with self.subTest(expression=expression):
                self.assertEqual(evaluate_expression(expression), expected)

    def test_precedence_parentheses_and_result_types(self) -> None:
        cases = (
            ("2 + 3 * 4", 14, int),
            ("-2 ** 2", -4, int),
            ("(2 + 3) * 4", 20, int),
            ("(-2) ** 2", 4, int),
            ("(42 - 6) / 3", 12.0, float),
        )

        for expression, expected, expected_type in cases:
            with self.subTest(expression=expression):
                result = evaluate_expression(expression)
                self.assertEqual(result, expected)
                self.assertIs(type(result), expected_type)

    def test_representative_calculations_remain_supported(self) -> None:
        cases = (
            ("125 * 8 + 17", 1017),
            ("2 ** 10", 1024),
            ("7 // 2", 3),
            ("7 % 2", 1),
            ("2 ** -3", 0.125),
        )

        for expression, expected in cases:
            with self.subTest(expression=expression):
                self.assertEqual(evaluate_expression(expression), expected)

    def test_empty_whitespace_and_invalid_syntax_are_rejected(self) -> None:
        for expression in ("", "   ", "\t\r\n", "2 +"):
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError) as raised:
                    evaluate_expression(expression)
                self.assertEqual(
                    str(raised.exception),
                    "Expression syntax is invalid.",
                )

    def test_expression_length_boundaries(self) -> None:
        exact_limit = "+1" + "+0" * 99
        over_limit = "++1" + "+0" * 99

        self.assertEqual(len(exact_limit), 200)
        self.assertEqual(evaluate_expression(exact_limit), 1)
        self.assertEqual(len(over_limit), 201)
        with self.assertRaises(ValueError) as raised:
            evaluate_expression(over_limit)
        self.assertEqual(str(raised.exception), "Expression is too long.")

    def test_exponent_boundaries_for_integer_and_float_exponents(self) -> None:
        accepted = (
            ("2 ** 100", int),
            ("2 ** -100", float),
            ("2 ** 100.0", float),
            ("2 ** -100.0", float),
        )
        rejected = (
            "2 ** 101",
            "2 ** -101",
            "2 ** 100.5",
            "2 ** -100.5",
            "1 ** 101",
            "(-1) ** -101",
        )

        for expression, expected_type in accepted:
            with self.subTest(expression=expression):
                result = evaluate_expression(expression)
                self.assertIs(type(result), expected_type)

        for expression in rejected:
            with self.subTest(expression=expression):
                self._assert_numeric_limit(expression)

    def test_zero_division_operations_remain_controlled_exceptions(self) -> None:
        for expression in ("1 / 0", "1 // 0", "1 % 0"):
            with self.subTest(expression=expression):
                with self.assertRaises(ZeroDivisionError):
                    evaluate_expression(expression)

    def test_non_arithmetic_ast_syntax_is_rejected(self) -> None:
        expressions = (
            "value",
            "payload()",
            "value.real",
            "value[0]",
            "'text'",
            "b'text'",
            "[1, 2]",
            "(1, 2)",
            "{1, 2}",
            "{'key': 1}",
            "lambda: 1",
            "[item for item in [1]]",
            "{item for item in [1]}",
            "{item: item for item in [1]}",
            "(item for item in [1])",
            "1 and 2",
            "1 < 2",
            "1 if True else 2",
            "(target := 1)",
        )

        for expression in expressions:
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError) as raised:
                    evaluate_expression(expression)
                self.assertEqual(
                    str(raised.exception),
                    "Only basic arithmetic is supported.",
                )

    def test_call_shaped_code_never_imports_or_invokes_os_functions(self) -> None:
        expression = "__import__('os').system('MARIA_CALCULATOR_SENTINEL')"

        with patch.object(os, "system") as system_call:
            with patch("builtins.__import__") as import_call:
                with self.assertRaises(ValueError):
                    evaluate_expression(expression)

        import_call.assert_not_called()
        system_call.assert_not_called()

    def test_booleans_are_rejected_as_constants_and_results(self) -> None:
        for expression in ("True", "False"):
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError) as raised:
                    evaluate_expression(expression)
                self.assertEqual(
                    str(raised.exception),
                    calculator_module.UNSUPPORTED_NUMERIC_TYPE_MESSAGE,
                )

        bool_operation = Mock(return_value=True)
        with patch.dict(
            calculator_module.ALLOWED_BINARY_OPERATORS,
            {ast.Add: bool_operation},
        ):
            with self.assertRaises(ValueError) as raised:
                evaluate_expression("1 + 1")

        self.assertEqual(
            str(raised.exception),
            calculator_module.UNSUPPORTED_NUMERIC_TYPE_MESSAGE,
        )

    def test_non_finite_floats_are_rejected(self) -> None:
        for expression in ("1e9999", "-1e9999", "1e308 * 1e308"):
            with self.subTest(expression=expression):
                self._assert_numeric_limit(expression)

    def test_complex_constants_and_results_are_rejected(self) -> None:
        for expression in ("1j", "(-1) ** 0.5"):
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError) as raised:
                    evaluate_expression(expression)
                self.assertEqual(
                    str(raised.exception),
                    calculator_module.UNSUPPORTED_NUMERIC_TYPE_MESSAGE,
                )

    def test_integer_constant_bit_boundaries(self) -> None:
        accepted = (
            (1 << (calculator_module.MAX_INTEGER_BITS - 1)) - 1,
            1 << (calculator_module.MAX_INTEGER_BITS - 1),
        )
        rejected = 1 << calculator_module.MAX_INTEGER_BITS

        for magnitude in accepted:
            for value in (magnitude, -magnitude):
                with self.subTest(bits=value.bit_length(), sign=value < 0):
                    result = calculator_module._evaluate_node(
                        ast.Constant(value=value)
                    )
                    self.assertEqual(result, value)

        for value in (rejected, -rejected):
            with self.subTest(sign=value < 0):
                with self.assertRaises(ValueError) as raised:
                    calculator_module._evaluate_node(ast.Constant(value=value))
                self.assertEqual(
                    str(raised.exception),
                    calculator_module.NUMERIC_LIMIT_MESSAGE,
                )

    def test_final_result_is_validated(self) -> None:
        oversized = 1 << calculator_module.MAX_INTEGER_BITS

        with patch.object(
            calculator_module,
            "_evaluate_node",
            return_value=oversized,
        ):
            self._assert_numeric_limit("1")

    def test_results_at_and_below_integer_limit_are_accepted(self) -> None:
        immediately_below = evaluate_expression("(2 ** 45) ** 91 - 1")
        at_limit = evaluate_expression("(2 ** 45) ** 91")

        self.assertEqual(
            immediately_below.bit_length(),
            calculator_module.MAX_INTEGER_BITS - 1,
        )
        self.assertEqual(
            at_limit.bit_length(),
            calculator_module.MAX_INTEGER_BITS,
        )

    def test_result_immediately_above_integer_limit_is_rejected(self) -> None:
        self._assert_numeric_limit("(2 ** 64) ** 64")

    def test_oversized_nested_power_regression_is_rejected(self) -> None:
        self._assert_numeric_limit("(2 ** 100) ** 100")

    def test_safe_nested_power_beneath_limit_succeeds(self) -> None:
        result = evaluate_expression("(2 ** 63) ** 65")

        self.assertEqual(result.bit_length(), calculator_module.MAX_INTEGER_BITS)

    def test_oversized_exponent_expression_is_rejected(self) -> None:
        self._assert_numeric_limit("2 ** (2 ** 100)")

    def test_power_guard_runs_before_expensive_outer_operation(self) -> None:
        raw_power = Mock(wraps=operator.pow)

        with patch.dict(
            calculator_module.ALLOWED_BINARY_OPERATORS,
            {ast.Pow: raw_power},
        ):
            self._assert_numeric_limit("(2 ** 100) ** 100")

        self.assertEqual(raw_power.call_args_list, [call(2, 100)])

    def test_special_integer_power_bases_remain_safe(self) -> None:
        cases = (
            ("0 ** 100", 0),
            ("1 ** 100", 1),
            ("(-1) ** 100", 1),
            ("(-1) ** 99", -1),
            ("(2 ** 100) ** 0", 1),
        )

        for expression, expected in cases:
            with self.subTest(expression=expression):
                self.assertEqual(evaluate_expression(expression), expected)

    def test_safe_large_integer_multiplication_succeeds(self) -> None:
        result = evaluate_expression("(2 ** 100) ** 40 * (2 ** 95)")

        self.assertEqual(result.bit_length(), calculator_module.MAX_INTEGER_BITS)

    def test_oversized_integer_multiplication_is_rejected_preemptively(self) -> None:
        raw_multiply = Mock(wraps=operator.mul)

        with patch.dict(
            calculator_module.ALLOWED_BINARY_OPERATORS,
            {ast.Mult: raw_multiply},
        ):
            self._assert_numeric_limit(
                "(2 ** 100) ** 30 * (2 ** 100) ** 30"
            )

        raw_multiply.assert_not_called()

    def test_multiplication_immediately_above_limit_is_rejected(self) -> None:
        self._assert_numeric_limit("(2 ** 100) ** 40 * (2 ** 96)")

    def test_operation_overflow_is_normalized_to_numeric_limit(self) -> None:
        overflowing_operation = Mock(side_effect=OverflowError("overflow"))

        with patch.dict(
            calculator_module.ALLOWED_BINARY_OPERATORS,
            {ast.Add: overflowing_operation},
        ):
            self._assert_numeric_limit("1 + 1")

    def _assert_numeric_limit(self, expression: str) -> None:
        """Assert that an expression fails with the stable limit message."""
        with self.assertRaises(ValueError) as raised:
            evaluate_expression(expression)
        self.assertEqual(
            str(raised.exception),
            calculator_module.NUMERIC_LIMIT_MESSAGE,
        )


class CalculatorToolTests(unittest.TestCase):
    """Verify the calculator's ToolExecutionResult and logging behavior."""

    def setUp(self) -> None:
        self.logger = Mock(spec=logging.Logger)
        self.tool = CalculatorTool(logger=self.logger)

    def test_success_returns_existing_formatted_result(self) -> None:
        result = self.tool.execute("calculate", {"expression": "  2 + 2  "})

        self.assertEqual(
            result,
            ToolExecutionResult(
                True,
                "2 + 2 = 4",
                {"expression": "2 + 2", "result": 4},
            ),
        )
        self.logger.info.assert_called_once_with("Calculation completed.")

    def test_numeric_limit_failure_is_controlled(self) -> None:
        result = self.tool.execute(
            "calculate",
            {"expression": "(2 ** 100) ** 100"},
        )

        self.assertEqual(
            result,
            ToolExecutionResult(
                False,
                "Calculation failed: Calculation exceeds supported numeric limits.",
            ),
        )

    def test_syntax_failure_is_controlled(self) -> None:
        result = self.tool.execute("calculate", {"expression": "2 +"})

        self.assertEqual(
            result,
            ToolExecutionResult(
                False,
                "Calculation failed: Expression syntax is invalid.",
            ),
        )

    def test_zero_division_failures_are_controlled(self) -> None:
        for expression in ("1 / 0", "1 // 0", "1 % 0"):
            with self.subTest(expression=expression):
                result = self.tool.execute(
                    "calculate",
                    {"expression": expression},
                )
                self.assertFalse(result.success)
                self.assertTrue(result.message.startswith("Calculation failed: "))
                self.assertIn("zero", result.message.lower())
                self.assertIsNone(result.data)

    def test_unknown_action_remains_rejected(self) -> None:
        result = self.tool.execute("estimate", {"expression": "2 + 2"})

        self.assertEqual(
            result,
            ToolExecutionResult(False, "Unsupported calculator action: estimate"),
        )

    def test_missing_expression_is_controlled(self) -> None:
        for arguments in ({}, {"expression": ""}, {"expression": " \t\r\n "}):
            with self.subTest(arguments=arguments):
                result = self.tool.execute("calculate", arguments)
                self.assertEqual(
                    result,
                    ToolExecutionResult(False, "No expression provided."),
                )

    def test_logs_never_contain_full_expressions_or_submitted_code(self) -> None:
        success_expression = "731 * 1 + 9"
        malicious_expression = (
            "__import__('os').system('MARIA_CALCULATOR_LOG_SENTINEL')"
        )

        self.tool.execute("calculate", {"expression": success_expression})
        self.tool.execute("calculate", {"expression": malicious_expression})

        logged_calls = " ".join(str(entry) for entry in self.logger.method_calls)
        self.assertNotIn(success_expression, logged_calls)
        self.assertNotIn(malicious_expression, logged_calls)
        self.assertNotIn("MARIA_CALCULATOR_LOG_SENTINEL", logged_calls)

    def test_expected_arithmetic_exceptions_do_not_escape(self) -> None:
        cases = (
            (
                ValueError("validation failed"),
                "Calculation failed: validation failed",
            ),
            (
                ZeroDivisionError("division by zero"),
                "Calculation failed: division by zero",
            ),
            (
                OverflowError("overflow"),
                "Calculation failed: Calculation exceeds supported numeric limits.",
            ),
            (
                FloatingPointError("floating failure"),
                "Calculation failed: Calculation exceeds supported numeric limits.",
            ),
            (
                ArithmeticError("arithmetic failure"),
                "Calculation failed: arithmetic failure",
            ),
        )

        for exception, expected_message in cases:
            with self.subTest(exception_type=type(exception).__name__):
                with patch.object(
                    calculator_module,
                    "evaluate_expression",
                    side_effect=exception,
                ):
                    result = self.tool.execute(
                        "calculate",
                        {"expression": "3 + 4"},
                    )
                self.assertEqual(
                    result,
                    ToolExecutionResult(False, expected_message),
                )

    def test_memory_error_is_not_treated_as_validation_failure(self) -> None:
        with patch.object(
            calculator_module,
            "evaluate_expression",
            side_effect=MemoryError("out of memory"),
        ):
            with self.assertRaises(MemoryError):
                self.tool.execute("calculate", {"expression": "3 + 4"})

    def test_factory_and_tool_identity_remain_unchanged(self) -> None:
        tool = build_tool(unused_dependency=object())

        self.assertIsInstance(tool, CalculatorTool)
        self.assertEqual(tool.name, "calculator")
        self.assertEqual(tool.actions, frozenset({"calculate"}))


if __name__ == "__main__":
    unittest.main()
