"""Constants shared across Maria's core modules."""

from __future__ import annotations

from pathlib import Path


APP_NAME = "Maria"
APP_VERSION = "0.2.0"

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# The checkout that holds the application directory. It is derived from the
# source layout alone, so nothing here shells out to Git, reads ``.git``, or
# consults the current working directory to decide where the repository is.
REPOSITORY_ROOT = PROJECT_ROOT.parent

CONFIG_PATH = PROJECT_ROOT / "config" / "config.toml"

# Reserved for private local configuration. Maria never creates this file and
# never loads it; the path exists here so the permanent-exclusion boundary can
# name it.
LOCAL_CONFIG_PATH = PROJECT_ROOT / "config" / "config.local.toml"

DATABASE_DIR = PROJECT_ROOT / "database"
DATABASE_PATH = DATABASE_DIR / "maria.db"
LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_LOG_FILE = LOG_DIR / "maria.log"
SYSTEM_PROMPT_PATH = PROJECT_ROOT / "prompts" / "system_prompt.txt"

# The repository's Git metadata, which may exist either as a directory or as a
# one-line gitdir-pointer file. Both shapes live at this exact path.
GIT_DIR = REPOSITORY_ROOT / ".git"

DEFAULT_TEXT_READ_LIMIT_BYTES = 128 * 1024
DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS = 5.0
