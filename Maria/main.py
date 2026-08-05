"""Command line entry point for Maria v0.1."""

from __future__ import annotations

from core.brain import BrainResponse, MariaBrain
from core.constants import APP_NAME, APP_VERSION


EXIT_COMMANDS = {"exit", "quit", "q"}


def ask_for_confirmation() -> bool:
    """Ask the user to explicitly confirm a sensitive action."""
    answer = input("Confirm action? Type 'yes' to continue: ").strip().lower()
    return answer == "yes"


def print_response(response: BrainResponse) -> None:
    """Print a response returned by the brain."""
    print(f"{APP_NAME}: {response.text}")


def run_cli() -> None:
    """Run Maria's minimal v0.1 command line interface."""
    brain = MariaBrain.from_defaults()

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


if __name__ == "__main__":
    run_cli()
