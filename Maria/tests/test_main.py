"""Focused tests for Maria's command-line entrypoint boundary."""

from __future__ import annotations

import runpy
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, call, patch

import main as main_module
from core.brain import BrainResponse
from core.config import load_config
from core.constants import CONFIG_PATH
from core.failures import Failure, FailureKind, TypedFailureError
from core.ollama_adapter import OLLAMA_DESTINATION_MESSAGE, OllamaReasoningProvider
from core.planner import Plan, PlanStep
from core.reasoning import (
    DISABLED_REASONING,
    ReasoningExchange,
    ReasoningSettings,
    ReasoningTransport,
)
from core.session import MariaSession


MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
BANNER = "Maria v0.2.0"
HELP_HINT = "Type 'help' for supported commands. Type 'exit' to quit."
CONFIRMATION_PROMPT = (
    "Confirm action? Type 'y' or 'yes' to continue, or 'n' or 'no' to cancel: "
)
STARTUP_FAILURE_MESSAGE = (
    "Maria: Startup failed. Review the configuration and available logs."
)
STARTUP_INTERRUPTED_MESSAGE = "Maria: Startup interrupted."
SCHEMA_TOO_NEW_MESSAGE = (
    "Maria: This database was written by a newer version of Maria and "
    "cannot be opened by this one."
)
RENAME_PLAN = Plan(
    original_request="rename old.txt to new.txt",
    steps=(
        PlanStep(
            "filesystem",
            "rename",
            {"source": "old.txt", "destination": "new.txt"},
        ),
    ),
)
MOVE_PLAN = Plan(
    original_request="move source.txt to docs/source.txt",
    steps=(
        PlanStep(
            "filesystem",
            "move",
            {"source": "source.txt", "destination": "docs/source.txt"},
        ),
    ),
)


class MainCliTests(unittest.TestCase):
    """Exercise CLI behavior using only injected and mocked Brain objects."""

    def test_success_uses_injected_factory_once_and_returns_zero(self) -> None:
        brain = Mock(spec=main_module.MariaBrain)
        factory = Mock(return_value=brain)

        status, stdout, stderr, input_mock = self._run_cli(factory, ["exit"])

        self.assertEqual(status, 0)
        factory.assert_called_once_with()
        brain.process.assert_not_called()
        input_mock.assert_called_once_with("You: ")
        self.assertEqual(
            stdout,
            f"{BANNER}\n{HELP_HINT}\nMaria: Goodbye.\n",
        )
        self.assertEqual(stderr, "")

    def test_none_factory_uses_from_defaults_exactly_once(self) -> None:
        brain = Mock(spec=main_module.MariaBrain)
        stdout = StringIO()
        stderr = StringIO()

        with (
            patch.object(
                main_module.MariaBrain,
                "from_defaults",
                return_value=brain,
            ) as factory,
            patch("builtins.input", side_effect=["exit"]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = main_module.run_cli()

        self.assertEqual(status, 0)
        factory.assert_called_once_with(
            reasoning_factory=main_module.build_reasoning_provider,
        )
        brain.process.assert_not_called()
        self.assertIn(BANNER, stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_controlled_factory_failures_are_sanitized(self) -> None:
        cases = (
            ("file_not_found", FileNotFoundError),
            ("configuration", ValueError),
            ("logging_directory", OSError),
            ("database", RuntimeError),
            ("tool_import", ImportError),
            ("unexpected", Exception),
        )

        for description, exception_type in cases:
            with self.subTest(description=description):
                private_message = f"private-{description}-detail"
                factory = Mock(side_effect=exception_type(private_message))

                status, stdout, stderr, input_mock = self._run_cli(factory, [])

                self.assertEqual(status, 1)
                factory.assert_called_once_with()
                input_mock.assert_not_called()
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, f"{STARTUP_FAILURE_MESSAGE}\n")
                self.assertNotIn("Traceback", stderr)
                self.assertNotIn(exception_type.__name__, stderr)
                self.assertNotIn(private_message, stderr)
                self.assertNotIn(BANNER, stdout)

    def test_startup_busy_database_failure_is_sanitized(self) -> None:
        busy_message = "Database is busy. Try again shortly."
        factory = Mock(side_effect=RuntimeError(busy_message))

        status, stdout, stderr, input_mock = self._run_cli(factory, [])

        self.assertEqual(status, 1)
        factory.assert_called_once_with()
        input_mock.assert_not_called()
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, f"{STARTUP_FAILURE_MESSAGE}\n")
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn(busy_message, stderr)
        self.assertNotIn("busy", stderr.lower())
        self.assertNotIn(BANNER, stdout)

    def test_startup_typed_failure_reports_its_own_fixed_message(self) -> None:
        """A refusal that already chose its wording says which one it was.

        The generic startup message would tell an owner holding a database
        from a newer build only that something failed, which is the silent
        half-working outcome this refusal exists to replace.
        """
        factory = Mock(
            side_effect=TypedFailureError(
                Failure(FailureKind.DATABASE_SCHEMA_TOO_NEW)
            )
        )

        status, stdout, stderr, input_mock = self._run_cli(factory, [])

        self.assertEqual(status, 1)
        factory.assert_called_once_with()
        input_mock.assert_not_called()
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, f"{SCHEMA_TOO_NEW_MESSAGE}\n")
        self.assertNotIn(STARTUP_FAILURE_MESSAGE, stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("TypedFailureError", stderr)
        self.assertNotIn(BANNER, stdout)

    def test_startup_keyboard_interrupt_returns_130(self) -> None:
        factory = Mock(side_effect=KeyboardInterrupt())

        status, stdout, stderr, input_mock = self._run_cli(factory, [])

        self.assertEqual(status, 130)
        factory.assert_called_once_with()
        input_mock.assert_not_called()
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, f"{STARTUP_INTERRUPTED_MESSAGE}\n")
        self.assertNotIn(BANNER, stdout)

    def test_startup_memory_error_propagates(self) -> None:
        error = MemoryError("private-memory-detail")
        factory = Mock(side_effect=error)
        stdout = StringIO()
        stderr = StringIO()

        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(MemoryError) as raised,
        ):
            main_module.run_cli(factory)

        self.assertIs(raised.exception, error)
        factory.assert_called_once_with()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_startup_system_exit_propagates_unchanged(self) -> None:
        error = SystemExit(23)
        factory = Mock(side_effect=error)
        stdout = StringIO()
        stderr = StringIO()

        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main_module.run_cli(factory)

        self.assertIs(raised.exception, error)
        self.assertEqual(raised.exception.code, 23)
        factory.assert_called_once_with()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_startup_generator_exit_is_not_caught(self) -> None:
        error = GeneratorExit("private-generator-detail")
        factory = Mock(side_effect=error)

        with self.assertRaises(GeneratorExit) as raised:
            main_module.run_cli(factory)

        self.assertIs(raised.exception, error)
        factory.assert_called_once_with()

    def test_module_entrypoint_converts_startup_failure_to_exit_one(self) -> None:
        private_message = "private-runpy-startup-detail"
        factory = Mock(side_effect=ValueError(private_message))
        stdout = StringIO()
        stderr = StringIO()

        with (
            patch.object(main_module.MariaBrain, "from_defaults", factory),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            runpy.run_path(str(MAIN_PATH), run_name="__main__")

        self.assertEqual(raised.exception.code, 1)
        self._assert_default_startup_call(factory)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), f"{STARTUP_FAILURE_MESSAGE}\n")
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertNotIn(private_message, stderr.getvalue())
        self.assertNotIn("ValueError", stderr.getvalue())

    def test_module_entrypoint_converts_normal_exit_to_exit_zero(self) -> None:
        brain = Mock(spec=main_module.MariaBrain)
        factory = Mock(return_value=brain)
        stdout = StringIO()
        stderr = StringIO()

        with (
            patch.object(main_module.MariaBrain, "from_defaults", factory),
            patch("builtins.input", side_effect=["exit"]) as input_mock,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            runpy.run_path(str(MAIN_PATH), run_name="__main__")

        self.assertEqual(raised.exception.code, 0)
        self._assert_default_startup_call(factory)
        input_mock.assert_called_once_with("You: ")
        brain.process.assert_not_called()
        self.assertEqual(
            stdout.getvalue(),
            f"{BANNER}\n{HELP_HINT}\nMaria: Goodbye.\n",
        )
        self.assertEqual(stderr.getvalue(), "")

    def test_blank_input_is_ignored_and_normal_request_is_cleaned(self) -> None:
        brain = Mock(spec=main_module.MariaBrain)
        brain.process.return_value = BrainResponse("System ready.")
        factory = Mock(return_value=brain)

        status, stdout, stderr, input_mock = self._run_cli(
            factory,
            ["", " \t ", "  system  ", "exit"],
        )

        self.assertEqual(status, 0)
        brain.process.assert_called_once_with("system")
        self.assertEqual(input_mock.call_count, 4)
        self.assertIn("Maria: System ready.\n", stdout)
        self.assertEqual(stderr, "")

    def test_exit_aliases_are_case_insensitive_and_skip_brain(self) -> None:
        for exit_value in ("exit", "EXIT", " Quit ", "q", " Q "):
            with self.subTest(exit_value=exit_value):
                brain = Mock(spec=main_module.MariaBrain)
                factory = Mock(return_value=brain)

                status, stdout, stderr, _ = self._run_cli(
                    factory,
                    [exit_value],
                )

                self.assertEqual(status, 0)
                brain.process.assert_not_called()
                self.assertTrue(stdout.endswith("Maria: Goodbye.\n"))
                self.assertEqual(stderr, "")

    def test_main_prompt_interruptions_exit_cleanly(self) -> None:
        for interruption in (EOFError(), KeyboardInterrupt()):
            with self.subTest(interruption=type(interruption).__name__):
                brain = Mock(spec=main_module.MariaBrain)
                factory = Mock(return_value=brain)

                status, stdout, stderr, input_mock = self._run_cli(
                    factory,
                    [interruption],
                )

                self.assertEqual(status, 0)
                factory.assert_called_once_with()
                brain.process.assert_not_called()
                input_mock.assert_called_once_with("You: ")
                self.assertEqual(
                    stdout,
                    f"{BANNER}\n{HELP_HINT}\n\nMaria: Goodbye.\n",
                )
                self.assertNotIn("Traceback", stdout)
                self.assertNotIn("Traceback", stderr)
                self.assertEqual(stderr, "")

    def test_confirmation_accepts_y_or_yes_case_insensitive_with_whitespace(
        self,
    ) -> None:
        request = "rename old.txt to new.txt"
        confirmation = BrainResponse(
            "Confirmation required: filesystem.rename requires explicit confirmation.",
            needs_confirmation=True,
            plan=RENAME_PLAN,
        )
        completed = BrainResponse("Renamed old.txt to new.txt")

        for approval in ("yes", " YeS \t", "y", " Y \t"):
            with self.subTest(approval=approval):
                brain = Mock(spec=main_module.MariaBrain)
                brain.process.return_value = confirmation
                brain.execute_confirmed_plan.return_value = completed
                factory = Mock(return_value=brain)

                status, stdout, stderr, input_mock = self._run_cli(
                    factory,
                    [f"  {request}  ", approval, "exit"],
                )

                self.assertEqual(status, 0)
                brain.process.assert_called_once_with(request)
                brain.execute_confirmed_plan.assert_called_once_with(RENAME_PLAN)
                self.assertEqual(
                    input_mock.call_args_list,
                    [call("You: "), call(CONFIRMATION_PROMPT), call("You: ")],
                )
                self.assertIn(f"Maria: {confirmation.text}\n", stdout)
                self.assertIn(f"Maria: {completed.text}\n", stdout)
                self.assertNotIn("Action cancelled", stdout)
                self.assertEqual(stderr, "")

    def test_ordinary_confirmation_cancellation_keeps_cli_available(self) -> None:
        request = "rename old.txt to new.txt"
        confirmation = BrainResponse(
            "Confirmation required: filesystem.rename requires explicit confirmation.",
            needs_confirmation=True,
            plan=RENAME_PLAN,
        )
        next_response = BrainResponse("System ready.")

        for cancellation in ("no", "n", "NO", "N", " No \t"):
            with self.subTest(cancellation=cancellation):
                brain = Mock(spec=main_module.MariaBrain)
                brain.process.side_effect = [confirmation, next_response]
                factory = Mock(return_value=brain)

                status, stdout, stderr, input_mock = self._run_cli(
                    factory,
                    [request, cancellation, "system", "exit"],
                )

                self.assertEqual(status, 0)
                self.assertEqual(
                    brain.process.call_args_list,
                    [call(request), call("system")],
                )
                brain.execute_confirmed_plan.assert_not_called()
                self.assertEqual(stdout.count("Maria: Action cancelled.\n"), 1)
                self.assertIn("Maria: System ready.\n", stdout)
                self.assertEqual(
                    input_mock.call_args_list,
                    [
                        call("You: "),
                        call(CONFIRMATION_PROMPT),
                        call("You: "),
                        call("You: "),
                    ],
                )
                self.assertEqual(stderr, "")

    def test_blank_or_unrecognized_confirmation_reprompts_then_resolves(
        self,
    ) -> None:
        request = "rename old.txt to new.txt"
        confirmation = BrainResponse(
            "Confirmation required: filesystem.rename requires explicit confirmation.",
            needs_confirmation=True,
            plan=RENAME_PLAN,
        )
        completed = BrainResponse("Renamed old.txt to new.txt")

        cases = (
            ("blank_then_yes", "", "yes", True),
            ("unrecognized_then_yes", "maybe", "yes", True),
            ("blank_then_no", "", "no", False),
            ("unrecognized_then_no", "not today", "no", False),
        )

        for description, bad_answer, resolution, approves in cases:
            with self.subTest(description=description):
                brain = Mock(spec=main_module.MariaBrain)
                brain.process.return_value = confirmation
                brain.execute_confirmed_plan.return_value = completed
                factory = Mock(return_value=brain)

                status, stdout, stderr, input_mock = self._run_cli(
                    factory,
                    [request, bad_answer, resolution, "exit"],
                )

                self.assertEqual(status, 0)
                self.assertEqual(
                    input_mock.call_args_list,
                    [
                        call("You: "),
                        call(CONFIRMATION_PROMPT),
                        call(CONFIRMATION_PROMPT),
                        call("You: "),
                    ],
                )
                brain.process.assert_called_once_with(request)
                if approves:
                    brain.execute_confirmed_plan.assert_called_once_with(RENAME_PLAN)
                    self.assertNotIn("Action cancelled", stdout)
                else:
                    brain.execute_confirmed_plan.assert_not_called()
                    self.assertEqual(stdout.count("Maria: Action cancelled.\n"), 1)
                self.assertEqual(stderr, "")

    def test_confirmation_reprompt_loop_advances_once_per_invalid_answer(
        self,
    ) -> None:
        request = "rename old.txt to new.txt"
        confirmation = BrainResponse(
            "Confirmation required: filesystem.rename requires explicit confirmation.",
            needs_confirmation=True,
            plan=RENAME_PLAN,
        )
        completed = BrainResponse("Renamed old.txt to new.txt")
        brain = Mock(spec=main_module.MariaBrain)
        brain.process.return_value = confirmation
        brain.execute_confirmed_plan.return_value = completed
        factory = Mock(return_value=brain)

        status, stdout, stderr, input_mock = self._run_cli(
            factory,
            [request, "", "maybe", "nope", "yes", "exit"],
        )

        self.assertEqual(status, 0)
        self.assertEqual(
            input_mock.call_args_list,
            [
                call("You: "),
                call(CONFIRMATION_PROMPT),
                call(CONFIRMATION_PROMPT),
                call(CONFIRMATION_PROMPT),
                call(CONFIRMATION_PROMPT),
                call("You: "),
            ],
        )
        brain.process.assert_called_once_with(request)
        brain.execute_confirmed_plan.assert_called_once_with(RENAME_PLAN)
        self.assertNotIn("Action cancelled", stdout)
        self.assertEqual(stderr, "")

    def test_confirmation_reprompt_loop_interrupted_after_invalid_answers_cancels(
        self,
    ) -> None:
        for interruption in (EOFError(), KeyboardInterrupt()):
            with self.subTest(interruption=type(interruption).__name__):
                stdout = StringIO()

                with (
                    patch(
                        "builtins.input",
                        side_effect=["", "maybe", interruption],
                    ) as input_mock,
                    redirect_stdout(stdout),
                ):
                    approved = main_module.ask_for_confirmation()

                self.assertFalse(approved)
                self.assertEqual(input_mock.call_count, 3)
                self.assertEqual(stdout.getvalue(), "\n")

    def test_confirmation_prompt_interruptions_print_one_newline(self) -> None:
        for interruption in (EOFError(), KeyboardInterrupt()):
            with self.subTest(interruption=type(interruption).__name__):
                stdout = StringIO()

                with (
                    patch("builtins.input", side_effect=interruption) as input_mock,
                    redirect_stdout(stdout),
                ):
                    approved = main_module.ask_for_confirmation()

                self.assertFalse(approved)
                input_mock.assert_called_once_with(CONFIRMATION_PROMPT)
                self.assertEqual(stdout.getvalue(), "\n")

    def test_confirmation_prompt_does_not_catch_other_base_failures(self) -> None:
        for exception in (MemoryError("memory"), SystemExit(7)):
            with self.subTest(exception=type(exception).__name__):
                with (
                    patch("builtins.input", side_effect=exception),
                    self.assertRaises(type(exception)) as raised,
                ):
                    main_module.ask_for_confirmation()

                self.assertIs(raised.exception, exception)

    def test_interrupted_confirmation_cancels_without_confirmed_call(self) -> None:
        request = "move source.txt to docs/source.txt"
        confirmation = BrainResponse(
            "Confirmation required: filesystem.move requires explicit confirmation.",
            needs_confirmation=True,
            plan=MOVE_PLAN,
        )
        cases = (
            ("keyboard_interrupt", KeyboardInterrupt(), "exit"),
            ("terminal_eof", EOFError(), EOFError()),
        )

        for description, interruption, next_input in cases:
            with self.subTest(description=description):
                brain = Mock(spec=main_module.MariaBrain)
                brain.process.return_value = confirmation
                factory = Mock(return_value=brain)

                status, stdout, stderr, _ = self._run_cli(
                    factory,
                    [request, interruption, next_input],
                )

                self.assertEqual(status, 0)
                brain.process.assert_called_once_with(request)
                brain.execute_confirmed_plan.assert_not_called()
                self.assertEqual(stdout.count("Maria: Action cancelled.\n"), 1)
                self.assertTrue(stdout.endswith("Maria: Goodbye.\n"))
                self.assertNotIn("Traceback", stdout)
                self.assertNotIn("Traceback", stderr)
                self.assertEqual(stderr, "")

    def test_print_response_preserves_existing_format(self) -> None:
        stdout = StringIO()

        with redirect_stdout(stdout):
            main_module.print_response(BrainResponse("Line one\nLine two"))

        self.assertEqual(stdout.getvalue(), "Maria: Line one\nLine two\n")

    def test_injected_factory_does_not_use_default_runtime_construction(self) -> None:
        brain = Mock(spec=main_module.MariaBrain)
        factory = Mock(return_value=brain)

        with patch.object(
            main_module.MariaBrain,
            "from_defaults",
            side_effect=AssertionError("live defaults must not be used"),
        ) as default_factory:
            status, _, stderr, _ = self._run_cli(factory, ["exit"])

        self.assertEqual(status, 0)
        factory.assert_called_once_with()
        default_factory.assert_not_called()
        self.assertEqual(stderr, "")

    def test_the_runtime_factory_builds_the_adapter_only_when_enabled(
        self,
    ) -> None:
        settings = _enabled_settings()

        with patch.object(main_module, "OllamaReasoningProvider") as adapter:
            self.assertIsNone(
                main_module.build_reasoning_provider(DISABLED_REASONING)
            )
            adapter.assert_not_called()

            built = main_module.build_reasoning_provider(settings)

        self.assertIs(built, adapter.return_value)
        adapter.assert_called_once_with(settings)

    def test_the_runtime_factory_builds_the_real_adapter_without_connecting(
        self,
    ) -> None:
        self.assertIsInstance(
            main_module.build_reasoning_provider(_enabled_settings()),
            OllamaReasoningProvider,
        )
        private_lan = _enabled_settings(
            transport=ReasoningTransport.PRIVATE_LAN,
            host="192.168.1.20",
        )
        with self.assertRaises(ValueError) as caught:
            main_module.build_reasoning_provider(private_lan)
        self.assertEqual(str(caught.exception), OLLAMA_DESTINATION_MESSAGE)

    def test_the_tracked_configuration_builds_no_provider(self) -> None:
        tracked = load_config(CONFIG_PATH).reasoning

        self.assertIsNone(main_module.build_reasoning_provider(tracked))

    def test_the_cli_carries_the_session_conversation_to_the_next_request(
        self,
    ) -> None:
        exchange = ReasoningExchange("hello there", "Hello.")
        brain = Mock(spec=main_module.MariaBrain)
        brain.process.side_effect = [
            BrainResponse("Hello.", conversation=(exchange,)),
            BrainResponse("System ready."),
        ]

        status, stdout, stderr, _ = self._run_cli(
            Mock(return_value=brain),
            ["hello there", "system", "exit"],
        )

        self.assertEqual(status, 0)
        self.assertEqual(
            brain.process.call_args_list,
            [call("hello there"), call("system", exchange)],
        )
        self.assertEqual(
            stdout,
            f"{BANNER}\n{HELP_HINT}\n"
            "Maria: Hello.\nMaria: System ready.\nMaria: Goodbye.\n",
        )
        self.assertEqual(stderr, "")

    def test_every_way_out_of_the_cli_ends_the_session(self) -> None:
        exchange = ReasoningExchange("hello there", "Hello.")
        cases = (
            ("exit command", ["hello there", "exit"], None),
            ("end of input", ["hello there", EOFError()], None),
            ("interrupt", ["hello there", KeyboardInterrupt()], None),
            ("request exception", ["hello there", "system"], RuntimeError),
        )
        for label, inputs, raised in cases:
            with self.subTest(case=label):
                ended: list[tuple[ReasoningExchange, ...]] = []
                sessions: list[MariaSession] = []

                class RecordingSession(MariaSession):
                    """A real session that notes each end and what it held."""

                    def __init__(self, *args: object, **kwargs: object) -> None:
                        super().__init__(*args, **kwargs)
                        sessions.append(self)

                    def end(self) -> None:
                        ended.append(self.conversation)
                        super().end()

                brain = Mock(spec=main_module.MariaBrain)
                brain.process.side_effect = [
                    BrainResponse("Hello.", conversation=(exchange,)),
                    RuntimeError("private-request-detail"),
                ]

                with patch.object(main_module, "MariaSession", RecordingSession):
                    if raised is None:
                        status, _, stderr, _ = self._run_cli(
                            Mock(return_value=brain),
                            inputs,
                        )
                        self.assertEqual(status, 0)
                        self.assertEqual(stderr, "")
                    else:
                        with self.assertRaises(raised):
                            self._run_cli(Mock(return_value=brain), inputs)

                self.assertEqual(ended, [(exchange,)])
                (session,) = sessions
                self.assertEqual(session.conversation, ())
                self.assertIsNone(session.pending_approval)

    def test_brain_request_exception_is_not_converted_to_startup_failure(self) -> None:
        error = RuntimeError("private-request-detail")
        brain = Mock(spec=main_module.MariaBrain)
        brain.process.side_effect = error
        factory = Mock(return_value=brain)
        stdout = StringIO()
        stderr = StringIO()

        with (
            patch("builtins.input", side_effect=["system"]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(RuntimeError) as raised,
        ):
            main_module.run_cli(factory)

        self.assertIs(raised.exception, error)
        factory.assert_called_once_with()
        brain.process.assert_called_once_with("system")
        self.assertIn(BANNER, stdout.getvalue())
        self.assertNotIn(STARTUP_FAILURE_MESSAGE, stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def _assert_default_startup_call(self, from_defaults: Mock) -> None:
        """Assert the entrypoint built Brain once with the reasoning factory.

        Run as a script, ``main.py`` is a fresh module, so its factory is
        matched by name rather than by identity with the imported one.
        """
        (args, kwargs), = from_defaults.call_args_list
        self.assertEqual(args, ())
        self.assertEqual(list(kwargs), ["reasoning_factory"])
        self.assertEqual(
            kwargs["reasoning_factory"].__name__,
            "build_reasoning_provider",
        )

    def _run_cli(
        self,
        factory: Mock,
        inputs: list[object],
    ) -> tuple[int, str, str, Mock]:
        """Run the CLI with captured streams and a mocked input sequence."""
        stdout = StringIO()
        stderr = StringIO()

        with (
            patch("builtins.input", side_effect=inputs) as input_mock,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = main_module.run_cli(factory)

        return status, stdout.getvalue(), stderr.getvalue(), input_mock


def _enabled_settings(
    *,
    transport: ReasoningTransport = ReasoningTransport.LOOPBACK,
    host: str = "127.0.0.1",
) -> ReasoningSettings:
    """Return valid enabled reasoning settings; nothing here connects."""
    return ReasoningSettings(
        enabled=True,
        transport=transport,
        host=host,
        model="stub-model:1b",
        request_deadline_seconds=5,
        max_input_bytes=64,
        max_output_bytes=64,
    )


if __name__ == "__main__":
    unittest.main()
