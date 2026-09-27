"""Command line entry point for Maria.

This is also Maria's runtime composition point: the one production module
that names the local reasoning adapter, which it builds only when reasoning is
enabled and hands to Brain through the ``ReasoningProvider`` interface.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from core.brain import BrainResponse, MariaBrain
from core.constants import APP_NAME, APP_VERSION
from core.failures import TypedFailureError
from core.ollama_adapter import OllamaReasoningProvider
from core.reasoning import ReasoningProvider, ReasoningSettings
from core.session import MariaSession


EXIT_COMMANDS = {"exit", "quit", "q"}
CONFIRMATION_APPROVE_ANSWERS = frozenset({"y", "yes"})
CONFIRMATION_CANCEL_ANSWERS = frozenset({"n", "no"})
_CONFIRMATION_PROMPT = (
    "Confirm action? Type 'y' or 'yes' to continue, or 'n' or 'no' to cancel: "
)
_EXIT_SUCCESS = 0
_EXIT_STARTUP_FAILURE = 1
_EXIT_STARTUP_INTERRUPTED = 130
_STARTUP_FAILURE_MESSAGE = (
    f"{APP_NAME}: Startup failed. Review the configuration and available logs."
)
_STARTUP_INTERRUPTED_MESSAGE = f"{APP_NAME}: Startup interrupted."


def ask_for_confirmation() -> bool:
    """Ask until the user gives an explicit yes/no answer for a sensitive action."""
    while True:
        try:
            answer = input(_CONFIRMATION_PROMPT).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if answer in CONFIRMATION_APPROVE_ANSWERS:
            return True
        if answer in CONFIRMATION_CANCEL_ANSWERS:
            return False


def print_response(response: BrainResponse) -> None:
    """Print a response returned by the brain."""
    print(f"{APP_NAME}: {response.text}")


def build_reasoning_provider(
    settings: ReasoningSettings,
) -> ReasoningProvider | None:
    """Return the owner's local reasoning adapter, or None when disabled.

    The adapter is constructed only for enabled settings. Enabled settings it
    cannot honour raise its fixed ``ValueError``, which stops startup
    through the ordinary sanitized failure.
    """
    if not settings.enabled:
        return None
    return OllamaReasoningProvider(settings)


def build_runtime_brain() -> MariaBrain:
    """Create the runtime Brain, with the local adapter when it is enabled."""
    return MariaBrain.from_defaults(reasoning_factory=build_reasoning_provider)


def run_cli(
    brain_factory: Callable[[], MariaBrain] | None = None,
) -> int:
    """Run Maria's minimal command line interface."""
    factory = build_runtime_brain if brain_factory is None else brain_factory

    try:
        brain = factory()
    except KeyboardInterrupt:
        print(_STARTUP_INTERRUPTED_MESSAGE, file=sys.stderr)
        return _EXIT_STARTUP_INTERRUPTED
    except MemoryError:
        raise
    except TypedFailureError as typed_failure:
        # A startup refusal that already decided what the owner may be told.
        # Its message is the fixed sentence its failure kind declares and
        # interpolates nothing, so printing it directly stays as sanitized as
        # the generic message below while saying which refusal happened.
        print(f"{APP_NAME}: {typed_failure}", file=sys.stderr)
        return _EXIT_STARTUP_FAILURE
    except Exception:
        print(_STARTUP_FAILURE_MESSAGE, file=sys.stderr)
        return _EXIT_STARTUP_FAILURE

    session = MariaSession(brain)

    try:
        print(f"{APP_NAME} v{APP_VERSION}")
        print("Type 'help' for supported commands. Type 'exit' to quit.")

        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{APP_NAME}: Goodbye.")
                break

            if not user_input:
                continue

            if user_input.lower() in EXIT_COMMANDS:
                print(f"{APP_NAME}: Goodbye.")
                break

            response = session.submit(user_input)

            if response.needs_confirmation:
                print_response(response)
                if ask_for_confirmation():
                    print_response(session.confirm())
                else:
                    session.cancel()
                    print(f"{APP_NAME}: Action cancelled.")
                continue

            print_response(response)
    finally:
        # Every way out ends the session, including an exception this
        # boundary deliberately lets propagate, so its conversation and any
        # pending approval never outlive it.
        session.end()

    return _EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(run_cli())
