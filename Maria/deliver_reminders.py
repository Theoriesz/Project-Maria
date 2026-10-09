"""Maria's closed-app reminder pass: one pass, then exit (V1-23-R1).

The owner's per-user Task Scheduler task, MariaReminderDelivery, starts
pythonw on this script once a minute while the owner is signed in. Each run
is one pass: it shows the due reminders it claims in one Windows message box,
records what became of them, and exits with a fixed status that Task
Scheduler shows as the task's last result. Maria never registers, changes,
runs, or deletes the task herself.

This is the composition point for the pass only. It is not an interface:
it accepts no command, builds no Brain, never reads the private override,
and imports neither ``main.py``, the GUI, ``core.config``, reasoning, nor the
adapter. It reads the tracked configuration's ``[paths]``, asks Windows for
the notification state before opening anything, and opens the database only
if it already exists and is exactly the current schema; it never creates or
migrates one.

Exit statuses: 0 done (nothing due, delivery off, deferred, or every claimed
reminder displayed and recorded); 1 the tracked configuration could not be
read; 2 desktop notices are unavailable, or the notification state could not
be read or is unknown; 3 the database was missing, busy, or not current, or a
storage failure stopped the claims or an outcome write; 4 the box could not
be shown; 5 an unexpected failure, logged by exception type only. When more
than one applies, 5 outranks 4, which outranks 3, which outranks 0.

The entry boundary catches ``Exception`` only. KeyboardInterrupt and
SystemExit are never caught and keep Python's default handling.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from core import desktop_notice
from core.constants import CONFIG_PATH
from core.logger import configure_delivery_logging, get_logger
from core.memory import Memory
from core.reminder_delivery import ClaimStop, claim_notice, record_notice
from core.runtime_paths import RuntimePaths, read_runtime_paths
from core.task_records import DELIVERY_CHANNEL_DESKTOP, DeliveryStorageError


EXIT_DONE = 0
EXIT_CONFIGURATION_UNREADABLE = 1
EXIT_NOTICES_UNAVAILABLE = 2
EXIT_STORAGE = 3
EXIT_DISPLAY_FAILED = 4
EXIT_UNEXPECTED = 5


class DesktopNotices(Protocol):
    """The Windows notice functions a pass uses: ``core.desktop_notice``."""

    def available(self) -> bool:
        """Return whether both notice functions exist."""

    def notification_state(self) -> desktop_notice.NotificationState:
        """Ask Windows whether a notification may be shown now."""

    def show(self, title: str, text: str) -> bool:
        """Show one message box until it is closed."""


def utc_now() -> datetime:
    """Return the current instant, aware, in UTC: the pass's one clock read."""
    return datetime.now(timezone.utc)


def run_pass(
    *,
    clock: Callable[[], datetime] = utc_now,
    config_path: Path = CONFIG_PATH,
    notices: DesktopNotices = desktop_notice,
    memory_factory: Callable[[Path], Memory] | None = None,
    configure_log: Callable[[Path], object] = configure_delivery_logging,
) -> int:
    """Run one pass and return its exit status.

    In order: read the clock once; read the tracked ``[paths]``; check that
    the notice functions exist; ask Windows for the notification state;
    check the database read-only and read the setting; configure the
    delivery log, which opens no file until a line is written; claim, show,
    and record. Nothing is written before the first claim except, at most,
    the log directory. Every argument is a seam a test replaces.
    """
    now = clock()
    try:
        paths: RuntimePaths = read_runtime_paths(config_path)
    except ValueError:
        return EXIT_CONFIGURATION_UNREADABLE
    if not notices.available():
        return EXIT_NOTICES_UNAVAILABLE
    state = notices.notification_state()
    if state is desktop_notice.NotificationState.DEFERS:
        return EXIT_DONE
    if state is not desktop_notice.NotificationState.ACCEPTS:
        return EXIT_NOTICES_UNAVAILABLE
    memory = (memory_factory or _existing_memory)(paths.database_path)
    try:
        setting = memory.delivery_state()
    except DeliveryStorageError:
        return EXIT_STORAGE
    if not setting.enabled:
        return EXIT_DONE
    try:
        configure_log(paths.log_dir)
    except Exception:  # noqa: BLE001 - claim nothing without a log.
        return EXIT_UNEXPECTED
    logger = get_logger("delivery")
    try:
        batch = claim_notice(memory, now, DELIVERY_CHANNEL_DESKTOP)
    except DeliveryStorageError:
        logger.error("A storage failure stopped the pass before any claim.")
        return EXIT_STORAGE
    status = EXIT_DONE
    if batch.stopped is ClaimStop.STORAGE:
        status = EXIT_STORAGE
    elif batch.stopped is ClaimStop.UNEXPECTED:
        status = EXIT_UNEXPECTED
    notice = batch.notice
    if notice is None:
        return status
    try:
        shown = notices.show(notice.title, notice.text)
    except Exception as error:  # noqa: BLE001 - recorded as not displayed.
        logger.error("The reminder box failed: %s.", type(error).__name__)
        shown = False
    if not shown:
        logger.error("The reminder box could not be shown.")
        status = max(status, EXIT_DISPLAY_FAILED)
    failures = record_notice(memory, notice, shown)
    if failures.storage:
        status = max(status, EXIT_STORAGE)
    if failures.unexpected:
        status = max(status, EXIT_UNEXPECTED)
    return status


def main() -> int:
    """Run one pass behind the entry boundary, and return its exit status.

    An ``Exception`` from anywhere in the pass is logged by its type alone,
    if the delivery log is configured by then, and ends the pass with
    status 5. Nothing else is caught.
    """
    try:
        return run_pass()
    except Exception as error:  # noqa: BLE001 - the pass's entry boundary.
        get_logger("delivery").error(
            "The reminder pass failed: %s.",
            type(error).__name__,
        )
        return EXIT_UNEXPECTED


def _existing_memory(database_path: Path) -> Memory:
    """Return a memory layer that never creates the database."""
    return Memory(database_path, existing_only=True)


if __name__ == "__main__":
    raise SystemExit(main())
