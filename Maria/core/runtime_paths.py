"""Where Maria keeps its database and logs: the tracked ``[paths]`` rule.

The tracked ``config/config.toml`` names the database file and the log
directory in its ``[paths]`` table, relative to the application directory
unless absolute. This module holds that one rule, which ``core.config``
applies for both interfaces, and reads the tracked file's ``[paths]`` for the
closed-app reminder pass (V1-23-R1), so the pass finds exactly the database
and log directory the interfaces use without loading the rest of Maria's
configuration.

It is a leaf: it imports only the standard library and ``core.constants``.
It reads no other section of the tracked file, never the private override,
and nothing here creates, writes, or opens anything but the one file it is
asked to read.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.constants import CONFIG_PATH, PROJECT_ROOT


# The values the tracked ``[paths]`` table stands for when it omits a key.
DEFAULT_DATABASE_SETTING = "database/maria.db"
DEFAULT_LOGS_SETTING = "logs"

# Why the pass could not learn its paths. Fixed text that repeats nothing the
# file held.
PATHS_UNREADABLE_MESSAGE = (
    "The tracked configuration's [paths] table could not be read."
)


@dataclass(frozen=True)
class RuntimePaths:
    """The database file and log directory the tracked configuration names."""

    database_path: Path
    log_dir: Path


def resolve_project_path(value: Any) -> Path:
    """Resolve one configured path against the application directory.

    An absolute path is kept as it is; any other is taken relative to the
    application directory, not the working directory. Both come back
    resolved.
    """
    path = Path(str(value))
    if path.is_absolute():
        return path.resolve()
    return (PROJECT_ROOT / path).resolve()


def read_runtime_paths(config_path: Path = CONFIG_PATH) -> RuntimePaths:
    """Return the database file and log directory the tracked file names.

    Only the ``[paths]`` table is consulted, with the same defaults and the
    same rule ``core.config`` applies. A file that cannot be read or parsed,
    or a ``[paths]`` that is not a table, raises ``ValueError`` with a fixed
    message and no cause; nothing it held is repeated.
    """
    failed = False
    paths: object = None
    try:
        with config_path.open("rb") as config_file:
            paths = tomllib.load(config_file).get("paths", {})
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        failed = True
    if failed or not isinstance(paths, dict):
        raise ValueError(PATHS_UNREADABLE_MESSAGE)
    return RuntimePaths(
        database_path=resolve_project_path(
            paths.get("database", DEFAULT_DATABASE_SETTING)
        ),
        log_dir=resolve_project_path(paths.get("logs", DEFAULT_LOGS_SETTING)),
    )
