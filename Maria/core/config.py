"""Configuration loading for Maria."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover - Python < 3.11 only.
    raise RuntimeError("Maria requires Python 3.11 or newer for TOML support.") from exc

from core.constants import CONFIG_PATH, PROJECT_ROOT
from core.permissions import (
    MANDATORY_CONFIRMATION_ACTIONS,
    MANDATORY_DENIED_ACTIONS,
)


_PERMISSION_ACTION_PATTERN = re.compile(
    r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*"
)


@dataclass(frozen=True)
class MariaConfig:
    """Runtime configuration loaded from config.toml."""

    app_name: str
    version: str
    environment: str
    database_path: Path
    log_dir: Path
    prompt_path: Path
    safe_filesystem_root: Path
    log_level: str
    confirmation_required: frozenset[str]
    denied_actions: frozenset[str]


def load_config(config_path: Path = CONFIG_PATH) -> MariaConfig:
    """Load Maria configuration from a TOML file."""
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with config_path.open("rb") as config_file:
        raw_config = tomllib.load(config_file)

    app = raw_config.get("app", {})
    paths = raw_config.get("paths", {})
    logging_config = raw_config.get("logging", {})
    permissions = raw_config.get("permissions", {})
    if not isinstance(permissions, dict):
        raise ValueError("The [permissions] section must be a TOML table.")

    configured_confirmation_actions = _permission_action_set(
        permissions,
        "require_confirmation",
    )
    configured_denied_actions = _permission_action_set(permissions, "deny")

    return MariaConfig(
        app_name=str(app.get("name", "Maria")),
        version=str(app.get("version", "0.1.0")),
        environment=str(app.get("environment", "development")),
        database_path=_resolve_project_path(paths.get("database", "database/maria.db")),
        log_dir=_resolve_project_path(paths.get("logs", "logs")),
        prompt_path=_resolve_project_path(
            paths.get("system_prompt", "prompts/system_prompt.txt")
        ),
        safe_filesystem_root=_resolve_project_path(
            paths.get("safe_filesystem_root", ".")
        ),
        log_level=str(logging_config.get("level", "INFO")),
        confirmation_required=frozenset(
            configured_confirmation_actions | MANDATORY_CONFIRMATION_ACTIONS
        ),
        denied_actions=frozenset(
            configured_denied_actions | MANDATORY_DENIED_ACTIONS
        ),
    )


def _resolve_project_path(value: Any) -> Path:
    """Resolve a config path relative to the project root."""
    path = Path(str(value))
    if path.is_absolute():
        return path.resolve()
    return (PROJECT_ROOT / path).resolve()


def _permission_action_set(
    permissions: dict[str, Any],
    key: str,
) -> frozenset[str]:
    """Validate and return one configured permission-action array."""
    value = permissions.get(key, [])
    qualified_key = f"permissions.{key}"

    if not isinstance(value, list):
        raise ValueError(
            f"{qualified_key} must be a TOML array of lowercase "
            "tool.action identifiers."
        )

    actions: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError(
                f"{qualified_key} entries must be strings using lowercase "
                "tool.action identifiers."
            )
        if _PERMISSION_ACTION_PATTERN.fullmatch(item) is None:
            raise ValueError(
                f"{qualified_key} entries must use lowercase tool.action "
                "identifiers with one period; each segment must start with a "
                "lowercase ASCII letter and contain only lowercase ASCII "
                "letters, digits, or underscores."
            )
        if item in actions:
            raise ValueError(
                f"{qualified_key} contains duplicate action identifier: {item}."
            )
        actions.add(item)

    return frozenset(actions)
