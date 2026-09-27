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

# The owner's optional, untracked local configuration override. Maria never
# creates this file. Startup reads it, only when it is present, as the one
# place machine-specific reasoning settings may live; it can hold a
# [reasoning] table and nothing else. The permanent-exclusion boundary names
# it too, so no filesystem capability can ever read it.
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

# The schema the running build expects its database to have. This is the
# database's own identity and is deliberately a separate number from
# APP_VERSION: the application version stays frozen across a release effort
# while the schema may advance, and a schema change may ship without any
# application version change at all. It is therefore written as a literal
# integer and is never parsed, derived, or formatted from "0.2.0" or from any
# other release string.
#
# The numbering is a plain counter over Maria's schema history, not a release
# number. 0 is SQLite's default marker and means "never stamped"; 1 is the
# earliest schema Maria ever created, before explicit memories existed; 2 is
# the published v0.2.0 baseline, which adds the ``memories`` table and is the
# current schema. A new version is added by appending one migration, never by
# editing this value's meaning.
DATABASE_SCHEMA_VERSION = 2
