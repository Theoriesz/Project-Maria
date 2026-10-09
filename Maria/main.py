"""Entry point for Maria: the command line, or the desktop window.

This is also Maria's runtime composition point: the one production module
that names the local reasoning adapter, which it builds only when reasoning is
enabled and hands to Brain through the ``ReasoningProvider`` interface.

The command line runs by default. ``--gui`` opens the desktop window instead,
over a Brain built the same way and the same session contract. The GUI
package, and Qt with it, is imported only then, so the command line never
needs Qt.

Each interface's session also gets the reminder-delivery seam (V1-23-R1),
built from the started Brain's memory, so due reminders surface in session:
the command line checks before each prompt and prints what it claimed. The
closed-app pass has its own entry point, ``deliver_reminders.py``, and this
module's argument handling is unchanged.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from functools import partial

from core.brain import BrainResponse, MariaBrain
from core.constants import APP_NAME, APP_VERSION
from core.failures import TypedFailureError
from core.logger import get_logger
from core.ollama_adapter import OllamaReasoningProvider
from core.reasoning import ReasoningProvider, ReasoningSettings
from core.reminder_delivery import ReminderDeliverySeam
from core.session import MariaSession
from core.task_records import DELIVERY_CHANNEL_CLI, DELIVERY_CHANNEL_GUI


# Builds an interface's reminder-delivery seam from its started Brain, or
# returns None for a session without one.
DeliveryFactory = Callable[[MariaBrain], ReminderDeliverySeam | None]


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


def utc_now() -> datetime:
    """Return the current instant, aware, in UTC: one reminder check's clock."""
    return datetime.now(timezone.utc)


def build_runtime_delivery(brain: MariaBrain, channel: str) -> ReminderDeliverySeam:
    """Return the reminder-delivery seam over the runtime Brain's memory.

    The seam always exists at run time, because delivery can be turned on
    at any time; with it off, a check reads the setting and returns.
    """
    return ReminderDeliverySeam(brain.memory, channel, utc_now)


def run_cli(
    brain_factory: Callable[[], MariaBrain] | None = None,
    delivery_factory: DeliveryFactory | None = None,
) -> int:
    """Run Maria's minimal command line interface.

    Before each prompt, the session's reminder check runs inside the
    prompt's own handling of EOF and Ctrl+C, so an interrupt during the
    check, or while its notice prints, ends Maria exactly as one at the
    prompt does. A caller that injects a Brain factory gets no delivery seam
    unless it also injects a delivery factory.
    """
    started = _start_brain(brain_factory)
    if isinstance(started, int):
        return started

    session = MariaSession(
        started,
        delivery=_start_delivery(
            started,
            brain_factory,
            delivery_factory,
            DELIVERY_CHANNEL_CLI,
        ),
    )

    try:
        print(f"{APP_NAME} v{APP_VERSION}")
        print("Type 'help' for supported commands. Type 'exit' to quit.")

        while True:
            try:
                show_due_reminders(session)
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


def show_due_reminders(session: MariaSession) -> None:
    """Print the due reminders this check claimed, and record how it went.

    Nothing happens without a delivery seam, or when nothing was claimed. A
    notice that prints is recorded as displayed; one whose printing raises
    an ``Exception`` is recorded as not displayed, and the command line goes
    on to its prompt. Anything that is not an ``Exception`` propagates to
    the prompt's own handling.
    """
    if not session.delivers_reminders:
        return
    notice = session.claim_due_reminders()
    if notice is None:
        return
    try:
        print(f"{APP_NAME}: {notice.text}")
    except Exception:  # noqa: BLE001 - recorded as not displayed.
        session.record_reminder_notice(notice, displayed=False)
        return
    session.record_reminder_notice(notice, displayed=True)


def run_gui(
    brain_factory: Callable[[], MariaBrain] | None = None,
    delivery_factory: DeliveryFactory | None = None,
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

    delivery = _start_delivery(
        started,
        brain_factory,
        delivery_factory,
        DELIVERY_CHANNEL_GUI,
    )
    try:
        status = run_window(partial(MariaSession, started, delivery=delivery))
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


def _start_delivery(
    brain: MariaBrain,
    brain_factory: Callable[[], MariaBrain] | None,
    delivery_factory: DeliveryFactory | None,
    channel: str,
) -> ReminderDeliverySeam | None:
    """Return an interface's delivery seam, or None for a session without one.

    An injected delivery factory decides. Otherwise the runtime default
    applies only to the runtime Brain: a caller that injected a Brain factory
    gets no seam.
    """
    if delivery_factory is not None:
        return delivery_factory(brain)
    if brain_factory is not None:
        return None
    return build_runtime_delivery(brain, channel)


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
