"""Application logging helpers for Maria."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.constants import LOG_DIR


LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

# Bounded growth for the managed log. An operational log that never
# stops growing becomes a privacy problem of its own at conversational
# volume, so the active file is capped and a fixed number of previous
# files is kept. The policy is internal on purpose: it is declared here
# rather than exposed through configuration, a CLI option, or an
# environment variable, so no caller can widen or disable it.
LOG_MAX_BYTES = 1 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# Marks the handlers this module creates, so reconfiguration replaces only
# its own and never touches a handler an embedder attached. Ownership is
# deliberately not inferred from the log path, because an embedder may write
# to the same directory on purpose.
_MANAGED_ATTRIBUTE = "_maria_managed"


def configure_logging(
    log_dir: Path = LOG_DIR,
    level: str = "INFO",
) -> logging.Logger:
    """Configure Maria's file logger and return the root Maria logger.

    Configuring again is honoured rather than ignored: the file handlers
    this function created before are flushed, detached, and closed, and a
    single new handler for the requested directory replaces them. A second
    call therefore applies its own directory and level, and never leaves a
    duplicate managed handler writing the same message twice. Handlers an
    embedder attached itself are left attached, open, and usable, whatever
    their type and whatever file they write to.

    The replacement happens only once the new handler exists, so a failure
    to create the directory or open the file propagates with the previous
    configuration still intact.

    The managed handler rotates rather than growing without limit: the
    active file is capped at ``LOG_MAX_BYTES`` and at most
    ``LOG_BACKUP_COUNT`` previous files are kept, so the log directory
    holds a bounded amount of history whatever the volume of use.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "maria.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    setattr(file_handler, _MANAGED_ATTRIBUTE, True)

    logger = logging.getLogger("maria")
    logger.setLevel(_coerce_log_level(level))
    logger.propagate = False
    _close_managed_handlers(logger)
    logger.addHandler(file_handler)

    logger.info("Logging configured.")
    return logger


def _close_managed_handlers(logger: logging.Logger) -> None:
    """Flush, detach, and close only the handlers this module created."""
    for handler in list(logger.handlers):
        if not getattr(handler, _MANAGED_ATTRIBUTE, False):
            continue
        try:
            handler.flush()
        finally:
            logger.removeHandler(handler)
            handler.close()


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under Maria's logger namespace."""
    clean_name = name.replace("core.", "").replace("tools.", "")
    return logging.getLogger(f"maria.{clean_name}")


def _coerce_log_level(level: str) -> int:
    """Convert a text log level to a logging module level."""
    return getattr(logging, level.upper(), logging.INFO)
