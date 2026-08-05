"""Application logging helpers for Maria."""

from __future__ import annotations

import logging
from pathlib import Path

from core.constants import LOG_DIR


LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


def configure_logging(
    log_dir: Path = LOG_DIR,
    level: str = "INFO",
) -> logging.Logger:
    """Configure Maria's file logger and return the root Maria logger."""
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("maria")
    logger.setLevel(_coerce_log_level(level))
    logger.propagate = False

    if not logger.handlers:
        log_file = log_dir / "maria.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(file_handler)

    logger.info("Logging configured.")
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under Maria's logger namespace."""
    clean_name = name.replace("core.", "").replace("tools.", "")
    return logging.getLogger(f"maria.{clean_name}")


def _coerce_log_level(level: str) -> int:
    """Convert a text log level to a logging module level."""
    return getattr(logging, level.upper(), logging.INFO)
