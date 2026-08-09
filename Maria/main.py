"""Command line entry point for Maria v0.1."""

from __future__ import annotations

import sys
from collections.abc import Callable

from core.brain import BrainResponse, MariaBrain
from core.constants import APP_NAME, APP_VERSION


EXIT_COMMANDS = {"exit", "quit", "q"}
_EXIT_SUCCESS = 0
_EXIT_STARTUP_FAILURE = 1
_EXIT_STARTUP_INTERRUPTED = 130
_STARTUP_FAILURE_MESSAGE = (
    f"{APP_NAME}: Startup failed. Review the configuration and available logs."
)
_STARTUP_INTERRUPTED_MESSAGE = f"{APP_NAME}: Startup interrupted."


def ask_for_confirmation() -> bool:
    """Ask the user to explicitly confirm a sensitive action."""
    try:
        answer = input("Confirm action? Type 'yes' to continue: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer == "yes"


def print_response(response: BrainResponse) -> None:
    """Print a response returned by the brain."""
    print(f"{APP_NAME}: {response.text}")


def run_cli(
    brain_factory: Callable[[], MariaBrain] | None = None,
) -> int:
    """Run Maria's minimal v0.1 command line interface."""
    factory = MariaBrain.from_defaults if brain_factory is None else brain_factory

    try:
        brain = factory()
    except KeyboardInterrupt:
        print(_STARTUP_INTERRUPTED_MESSAGE, file=sys.stderr)
        return _EXIT_STARTUP_INTERRUPTED
    except MemoryError:
        raise
    except Exception:
        print(_STARTUP_FAILURE_MESSAGE, file=sys.stderr)
        return _EXIT_STARTUP_FAILURE

    print(f"{APP_NAME} v{APP_VERSION}")
    print("Type 'help' for supported v0.1 commands. Type 'exit' to quit.")

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

        response = brain.process(user_input)

        if response.needs_confirmation:
            print_response(response)
            if ask_for_confirmation():
                response = brain.process(user_input, confirmed=True)
                print_response(response)
            else:
                print(f"{APP_NAME}: Action cancelled.")
            continue

        print_response(response)

    return _EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(run_cli())
