"""Entry point for Maria: the command line, or the desktop window.

This is also Maria's runtime composition point: the one production module
that names the local reasoning adapter, which it builds only when reasoning is
enabled and hands to Brain through the ``ReasoningProvider`` interface.

The command line runs by default. ``--gui`` opens the desktop window instead,
over a Brain built the same way and the same session contract. The GUI
package, and Qt with it, is imported only then, so the command line never
needs Qt.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from functools import partial

from core.brain import BrainResponse, MariaBrain
from core.constants import APP_NAME, APP_VERSION
from core.failures import TypedFailureError
from core.logger import get_logger
from core.ollama_adapter import OllamaReasoningProvider
from core.reasoning import ReasoningProvider, ReasoningSettings
from core.session import MariaSession


EXIT_COMMANDS = {"exit", "quit", "q"}
CONFIRMATION_APPROVE_ANSWERS = frozenset({"y", "yes"})
CONFIRMATION_CANCEL_ANSWERS = frozenset({"n", "no"})
GUI_OPTION = "--gui"
_CONFIRMATION_PROMPT = (
    "Confirm action? Type 'y' or 'yes' to continue, or 'n' or 'no' to cancel: "
)
_EXIT_SUCCESS = 0
_EXIT_STARTUP_FAILURE = 1
_EXIT_GUI_FAILURE = 1
_EXIT_STARTUP_INTERRUPTED = 130
_STARTUP_FAILURE_MESSAGE = (
    f"{APP_NAME}: Startup failed. Review the configuration and available logs."
)
_STARTUP_INTERRUPTED_MESSAGE = f"{APP_NAME}: Startup interrupted."
_GUI_UNAVAILABLE_MESSAGE = (
    f"{APP_NAME}: The desktop GUI is unavailable. Install the optional GUI "
    "requirements, or start Maria without --gui to use the command line."
)
_GUI_FAILURE_MESSAGE = (
    f"{APP_NAME}: The desktop GUI stopped unexpectedly. Review the available "
    "logs, or start Maria without --gui to use the command line."
)


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
    started = _start_brain(brain_factory)
    if isinstance(started, int):
        return started

    session = MariaSession(started)

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


def run_gui(
    brain_factory: Callable[[], MariaBrain] | None = None,
) -> int:
    """Run Maria's desktop window over the same Brain and session contract.

    Qt is loaded and its application object created first, so a missing or
    broken PyQt6, or a Qt that cannot show a window here, stops with a fixed
    message before anything is configured, created, or opened. Brain is then
    built exactly as the command line builds it, with the same sanitized
    startup failures, and the window is given a session factory, never Brain
    itself. A window that fails, or whose session ends unexpectedly, is
    reported with a fixed message as well, and logged by exception type only.
    """
    try:
        from gui.window import prepare_application, run_window

        prepare_application(_GUI_UNAVAILABLE_MESSAGE, _EXIT_STARTUP_FAILURE)
    except KeyboardInterrupt:
        print(_STARTUP_INTERRUPTED_MESSAGE, file=sys.stderr)
        return _EXIT_STARTUP_INTERRUPTED
    except MemoryError:
        raise
    except Exception:
        print(_GUI_UNAVAILABLE_MESSAGE, file=sys.stderr)
        return _EXIT_STARTUP_FAILURE

    started = _start_brain(brain_factory)
    if isinstance(started, int):
        return started

    try:
        status = run_window(partial(MariaSession, started))
    except KeyboardInterrupt:
        print(_STARTUP_INTERRUPTED_MESSAGE, file=sys.stderr)
        return _EXIT_STARTUP_INTERRUPTED
    except MemoryError:
        raise
    except Exception as error:
        get_logger("main").error(
            "The desktop GUI failed: %s.",
            type(error).__name__,
        )
        status = _EXIT_GUI_FAILURE
    if status != _EXIT_SUCCESS:
        print(_GUI_FAILURE_MESSAGE, file=sys.stderr)
        return _EXIT_GUI_FAILURE
    return _EXIT_SUCCESS


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the interface the command line asks for, the CLI by default.

    Only ``--gui`` opens the desktop window. Any other argument is ignored,
    as every argument always was, so plain startup is unchanged.
    """
    selected = sys.argv[1:] if arguments is None else arguments
    if GUI_OPTION in selected:
        return run_gui()
    return run_cli()


def _start_brain(
    brain_factory: Callable[[], MariaBrain] | None,
) -> MariaBrain | int:
    """Build Brain for either interface, or report why startup stopped.

    Returns the Brain, or the exit status once the sanitized startup message
    has been printed, so a refusal reads the same whichever interface the
    owner chose.
    """
    factory = build_runtime_brain if brain_factory is None else brain_factory

    try:
        return factory()
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


if __name__ == "__main__":
    raise SystemExit(main())
