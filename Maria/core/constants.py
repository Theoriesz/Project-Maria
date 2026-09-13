"""Constants shared across Maria's core modules."""

from __future__ import annotations

from pathlib import Path


APP_NAME = "Maria"
APP_VERSION = "0.2.0"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "config.toml"
DATABASE_PATH = PROJECT_ROOT / "database" / "maria.db"
LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_LOG_FILE = LOG_DIR / "maria.log"
SYSTEM_PROMPT_PATH = PROJECT_ROOT / "prompts" / "system_prompt.txt"

DEFAULT_TEXT_READ_LIMIT_BYTES = 128 * 1024
DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS = 5.0
