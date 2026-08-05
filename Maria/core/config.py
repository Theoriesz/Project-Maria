"""Configuration loading for Maria."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover - Python < 3.11 only.
    raise RuntimeError("Maria requires Python 3.11 or newer for TOML support.") from exc

from core.constants import CONFIG_PATH, PROJECT_ROOT


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
            _string_list(permissions.get("require_confirmation", []))
        ),
        denied_actions=frozenset(_string_list(permissions.get("deny", []))),
    )


def _resolve_project_path(value: Any) -> Path:
    """Resolve a config path relative to the project root."""
    path = Path(str(value))
    if path.is_absolute():
        return path.resolve()
    return (PROJECT_ROOT / path).resolve()


def _string_list(value: Any) -> list[str]:
    """Return a list of strings from TOML array values."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]
