"""Application logging helpers for Maria."""

from __future__ import annotations

import logging
from pathlib import Path

from core.constants import LOG_DIR


LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

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
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "maria.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
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
