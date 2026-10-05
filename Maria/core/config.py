"""Configuration loading for Maria."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover - Python < 3.11 only.
    raise RuntimeError("Maria requires Python 3.11 or newer for TOML support.") from exc

from core.constants import (
    APP_VERSION,
    CONFIG_PATH,
    PROJECT_ROOT,
    REPOSITORY_ROOT,
)
from core.filesystem_roots import (
    FilesystemProtections,
    FilesystemRoot,
    FilesystemRoots,
    RootPolicy,
)
from core.permissions import (
    MANDATORY_CONFIRMATION_ACTIONS,
    MANDATORY_DENIED_ACTIONS,
)
from core.reasoning import (
    DISABLED_REASONING,
    REASONING_DISABLED_FIELDS_MESSAGE,
    REASONING_ENABLED_MESSAGE,
    REASONING_TRANSPORT_MESSAGE,
    ReasoningSettings,
    ReasoningTransport,
)
from core.time_resolution import is_zone_key


_PERMISSION_ACTION_PATTERN = re.compile(
    r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*"
)

# The exact keys the filesystem section and each root table accept. Anything
# else is a misspelling or an attempt to widen the surface, and either way it
# is refused rather than ignored.
_FILESYSTEM_KEYS = frozenset({"default_root", "roots"})
_ROOT_KEYS = frozenset({"name", "path", "policy", "enabled"})

_POLICIES_BY_NAME = {policy.value: policy for policy in RootPolicy}

# The two roots production accepts, and nothing else. An owner-declared third
# root is not a configuration option at this stage: every accepted root has
# semantics enforced below, so a name Maria has no semantics for is refused
# rather than admitted on trust.
WORKSPACE_ROOT_NAME = "workspace"
PROJECT_MARIA_ROOT_NAME = "project_maria"
PRODUCTION_ROOT_NAMES = (WORKSPACE_ROOT_NAME, PROJECT_MARIA_ROOT_NAME)

# The workspace's configured spelling is part of the contract, not just its
# canonical result: the shipped value stays home-relative so Maria's workspace
# follows the owner's home directory rather than one machine's absolute path.
WORKSPACE_CONFIGURED_PATH = "~/MariaWorkspace"


# Every filesystem configuration message below is fixed and interpolates
# nothing. A malformed configuration therefore cannot carry a submitted root
# name, a configured path, the owner's home location, or an underlying
# exception's text into the startup failure that reports it.
FILESYSTEM_SECTION_REQUIRED_MESSAGE = (
    "The [filesystem] section is required and must declare Maria's named roots."
)
FILESYSTEM_SECTION_TABLE_MESSAGE = (
    "The [filesystem] section must be a TOML table."
)
FILESYSTEM_UNKNOWN_KEY_MESSAGE = (
    "The [filesystem] section accepts only default_root and roots."
)
FILESYSTEM_ROOTS_ARRAY_MESSAGE = (
    "filesystem.roots must be a TOML array of root tables."
)
FILESYSTEM_ROOT_KEYS_MESSAGE = (
    "A filesystem root table accepts only name, path, policy, and enabled."
)
FILESYSTEM_ROOT_MISSING_FIELD_MESSAGE = (
    "A filesystem root table must declare name, path, policy, and enabled."
)
FILESYSTEM_ROOT_TEXT_MESSAGE = (
    "A filesystem root's name, path, and policy must be TOML strings."
)
FILESYSTEM_ROOT_ENABLED_MESSAGE = (
    "A filesystem root's enabled value must be a TOML boolean."
)
FILESYSTEM_ROOT_POLICY_MESSAGE = (
    "A filesystem root policy must be organize or read_only."
)
FILESYSTEM_DUPLICATE_NAME_MESSAGE = (
    "filesystem.roots must not declare the same root name twice."
)
FILESYSTEM_DUPLICATE_PATH_MESSAGE = (
    "filesystem.roots must not declare the same canonical path twice."
)
FILESYSTEM_OVERLAP_MESSAGE = (
    "No filesystem root may contain another filesystem root."
)
FILESYSTEM_DEFAULT_ROOT_TEXT_MESSAGE = (
    "filesystem.default_root is required and must be a TOML string."
)
FILESYSTEM_UNKNOWN_DEFAULT_MESSAGE = (
    "filesystem.default_root must name one of the declared roots."
)
FILESYSTEM_DISABLED_DEFAULT_MESSAGE = (
    "filesystem.default_root must name a root that is enabled."
)
FILESYSTEM_ROOT_IS_A_FILE_MESSAGE = (
    "A filesystem root path must name a directory location, not a file."
)
FILESYSTEM_ROOT_INVENTORY_MESSAGE = (
    "filesystem.roots must declare exactly the workspace and project_maria "
    "roots."
)
FILESYSTEM_WORKSPACE_PATH_MESSAGE = (
    "The workspace root path must be configured as ~/MariaWorkspace."
)
FILESYSTEM_WORKSPACE_POLICY_MESSAGE = (
    "The workspace root policy must be organize."
)
FILESYSTEM_WORKSPACE_DISABLED_MESSAGE = "The workspace root must be enabled."
FILESYSTEM_WORKSPACE_DEFAULT_MESSAGE = (
    "The workspace root must be Maria's default filesystem root."
)
FILESYSTEM_WORKSPACE_OVERLAP_MESSAGE = (
    "The workspace root must lie outside Maria's own repository."
)
FILESYSTEM_PROJECT_MARIA_PATH_MESSAGE = (
    "The project_maria root path must resolve to Maria's repository root."
)
FILESYSTEM_PROJECT_MARIA_POLICY_MESSAGE = (
    "The project_maria root policy must be read_only."
)
FILESYSTEM_PROJECT_MARIA_ENABLED_MESSAGE = (
    "The project_maria root must remain disabled."
)


# The exact keys a [reasoning] table accepts. The six active keys are all
# required once reasoning is enabled and none of them has a default. There is
# deliberately no key for a credential, a token, a header, an endpoint URL, a
# port, or a model path, so any such key is refused as unknown.
_REASONING_ACTIVE_KEYS = frozenset(
    {
        "transport",
        "host",
        "model",
        "request_deadline_seconds",
        "max_input_bytes",
        "max_output_bytes",
    }
)
_REASONING_KEYS = _REASONING_ACTIVE_KEYS | {"enabled"}

_TRANSPORTS_BY_NAME = {
    transport.value: transport for transport in ReasoningTransport
}

# The only things the private override may hold: a [reasoning] table, a
# [time] table, or both.
_LOCAL_OVERRIDE_SECTIONS = frozenset({"reasoning", "time"})

# The exact keys a [time] table accepts: the owner's one IANA time zone key.
_TIME_KEYS = frozenset({"zone"})

# The override holds a handful of short settings, so anything near this size
# is not a reasoning block and is refused rather than read in full.
MAXIMUM_LOCAL_OVERRIDE_BYTES = 64 * 1024

# How the override is opened. Where the platform supports it, the open never
# follows a link and never waits on a special file; the entry is checked
# before the open and the opened descriptor is checked again after it.
_LOCAL_OVERRIDE_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


# Like the filesystem messages, every reasoning and override message is fixed
# and interpolates nothing, so a refusal cannot carry the override's path, its
# text, a configured host or model identifier, or a parser's or the operating
# system's error text.
REASONING_SECTION_TABLE_MESSAGE = "The [reasoning] section must be a TOML table."
REASONING_UNKNOWN_KEY_MESSAGE = (
    "The [reasoning] section accepts only enabled, transport, host, model, "
    "request_deadline_seconds, max_input_bytes, and max_output_bytes."
)
REASONING_ENABLED_REQUIRED_MESSAGE = (
    "The [reasoning] section must declare enabled."
)
REASONING_MISSING_FIELD_MESSAGE = (
    "An enabled [reasoning] section must declare transport, host, model, "
    "request_deadline_seconds, max_input_bytes, and max_output_bytes."
)
REASONING_TRACKED_ENABLED_MESSAGE = (
    "The tracked configuration must keep reasoning disabled; active reasoning "
    "settings belong only in the local override."
)
LOCAL_OVERRIDE_ENTRY_MESSAGE = (
    "The local configuration override must be a regular file, not a link, "
    "directory, or other special entry."
)
LOCAL_OVERRIDE_UNREADABLE_MESSAGE = (
    "The local configuration override could not be read."
)
LOCAL_OVERRIDE_TOO_LARGE_MESSAGE = (
    "The local configuration override must be at most "
    f"{MAXIMUM_LOCAL_OVERRIDE_BYTES} bytes."
)
LOCAL_OVERRIDE_TOML_MESSAGE = (
    "The local configuration override is not valid UTF-8 TOML."
)
LOCAL_OVERRIDE_SECTIONS_MESSAGE = (
    "The local configuration override must contain a [reasoning] section, a "
    "[time] section, or both, and nothing else."
)
TIME_SECTION_TABLE_MESSAGE = "The [time] section must be a TOML table."
TIME_SECTION_KEYS_MESSAGE = (
    "The [time] section must declare exactly one key, zone."
)
TIME_ZONE_MESSAGE = (
    "time.zone must be an IANA time zone key, such as Area/City, of at most "
    "64 ASCII letters, digits, underscores, plus or minus signs, and slashes."
)
TIME_TRACKED_MESSAGE = (
    "The tracked configuration must not declare a [time] section; the time "
    "zone belongs only in the local override."
)


@dataclass(frozen=True)
class MariaConfig:
    """Runtime configuration loaded from config.toml.

    ``reasoning`` defaults to the disabled settings, which is what every
    configuration without an enabled local override means. ``time_zone`` is
    the owner's IANA time zone key from the override's ``[time]`` table, or
    None when none is configured; only resolving new task and reminder
    timing and the due-task query need it, and nothing falls back to another
    zone without it.
    """

    app_name: str
    version: str
    environment: str
    database_path: Path
    log_dir: Path
    prompt_path: Path
    filesystem_roots: FilesystemRoots
    filesystem_protections: FilesystemProtections
    log_level: str
    confirmation_required: frozenset[str]
    denied_actions: frozenset[str]
    reasoning: ReasoningSettings = DISABLED_REASONING
    time_zone: str | None = None


def load_config(
    config_path: Path = CONFIG_PATH,
    local_override_path: Path | None = None,
) -> MariaConfig:
    """Load Maria configuration from a TOML file and an optional override.

    Loading is side-effect free: it reads the configuration file, settles
    canonical paths, and classifies what it was given. Nothing here creates a
    directory, writes a file, or otherwise prepares a root; startup calls
    ``prepare_roots`` separately for that.

    Only the tracked file is read unless a caller names an override
    explicitly. Production startup is the one caller that does, so every
    other caller, including every test that loads the shipped file, never
    reads the owner's private settings. When an override path is named and
    nothing is there, the tracked configuration applies unchanged. When
    something is there, it must be a regular file holding a ``[reasoning]``
    table, a ``[time]`` table, or both, and nothing else. A ``[reasoning]``
    table replaces the tracked one completely: nothing is merged and no
    missing field is borrowed. A ``[time]`` table names the owner's one time
    zone, which the tracked file may never declare. Anything else stops
    loading with a fixed message.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with config_path.open("rb") as config_file:
        raw_config = tomllib.load(config_file)

    # A time zone is location-like and personal, so it lives only in the
    # untracked override; a tracked one is refused rather than ignored.
    if "time" in raw_config:
        raise ValueError(TIME_TRACKED_MESSAGE)

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

    # The three active runtime locations are settled once and then handed to
    # the protection value, so the filesystem boundary protects where Maria
    # actually keeps its database, its log, and its prompt rather than only
    # where the shipped layout would put them.
    database_path = _resolve_project_path(paths.get("database", "database/maria.db"))
    log_dir = _resolve_project_path(paths.get("logs", "logs"))
    prompt_path = _resolve_project_path(
        paths.get("system_prompt", "prompts/system_prompt.txt")
    )

    config = MariaConfig(
        app_name=str(app.get("name", "Maria")),
        version=str(app.get("version", APP_VERSION)),
        environment=str(app.get("environment", "development")),
        database_path=database_path,
        log_dir=log_dir,
        prompt_path=prompt_path,
        filesystem_roots=_filesystem_roots(raw_config),
        filesystem_protections=FilesystemProtections(
            database_path=database_path,
            log_dir=log_dir,
            prompt_path=prompt_path,
        ),
        log_level=str(logging_config.get("level", "INFO")),
        confirmation_required=frozenset(
            configured_confirmation_actions | MANDATORY_CONFIRMATION_ACTIONS
        ),
        denied_actions=frozenset(
            configured_denied_actions | MANDATORY_DENIED_ACTIONS
        ),
        reasoning=_tracked_reasoning(raw_config),
    )

    # The tracked file is settled in full before the private override is
    # even looked at, and the override can change the reasoning settings and
    # the time zone alone: every other field comes from the tracked file
    # whatever the override holds.
    if local_override_path is None:
        return config
    overriding = _local_override(local_override_path)
    if overriding is None:
        return config
    if overriding.reasoning is not None:
        config = replace(config, reasoning=overriding.reasoning)
    if overriding.time_zone is not None:
        config = replace(config, time_zone=overriding.time_zone)
    return config


def _resolve_project_path(value: Any) -> Path:
    """Resolve a config path relative to the project root."""
    path = Path(str(value))
    if path.is_absolute():
        return path.resolve()
    return (PROJECT_ROOT / path).resolve()


def _filesystem_roots(raw_config: dict[str, Any]) -> FilesystemRoots:
    """Validate and build Maria's named filesystem roots.

    The whole section fails closed. A missing or malformed declaration is a
    refusal rather than a fallback, because a silently repaired root would
    decide where Maria may read and write.
    """
    if "filesystem" not in raw_config:
        raise ValueError(FILESYSTEM_SECTION_REQUIRED_MESSAGE)

    filesystem = raw_config["filesystem"]
    if not isinstance(filesystem, dict):
        raise ValueError(FILESYSTEM_SECTION_TABLE_MESSAGE)
    if not set(filesystem) <= _FILESYSTEM_KEYS:
        raise ValueError(FILESYSTEM_UNKNOWN_KEY_MESSAGE)

    declared = filesystem.get("roots")
    if not isinstance(declared, list) or not declared:
        raise ValueError(FILESYSTEM_ROOTS_ARRAY_MESSAGE)

    configured = tuple(_filesystem_root(entry) for entry in declared)
    roots = tuple(root for _, root in configured)
    _reject_conflicting_roots(roots)

    default_name = filesystem.get("default_root")
    if type(default_name) is not str:
        raise ValueError(FILESYSTEM_DEFAULT_ROOT_TEXT_MESSAGE)
    matching = [root for root in roots if root.name == default_name]
    if not matching:
        raise ValueError(FILESYSTEM_UNKNOWN_DEFAULT_MESSAGE)
    if not matching[0].enabled:
        raise ValueError(FILESYSTEM_DISABLED_DEFAULT_MESSAGE)

    _reject_unapproved_production_roots(configured, default_name)

    return FilesystemRoots(roots, default_name)


def _filesystem_root(entry: Any) -> tuple[str, FilesystemRoot]:
    """Validate one declared root table and settle it as a value object.

    The configured path text is returned alongside the settled root, because
    the workspace's spelling is part of the production contract and cannot be
    recovered from the canonical path afterwards.
    """
    if not isinstance(entry, dict):
        raise ValueError(FILESYSTEM_ROOTS_ARRAY_MESSAGE)
    if not set(entry) <= _ROOT_KEYS:
        raise ValueError(FILESYSTEM_ROOT_KEYS_MESSAGE)
    if not _ROOT_KEYS <= set(entry):
        raise ValueError(FILESYSTEM_ROOT_MISSING_FIELD_MESSAGE)

    name = entry["name"]
    raw_path = entry["path"]
    policy_name = entry["policy"]
    enabled = entry["enabled"]

    for value in (name, raw_path, policy_name):
        if type(value) is not str:
            raise ValueError(FILESYSTEM_ROOT_TEXT_MESSAGE)
    if type(enabled) is not bool:
        raise ValueError(FILESYSTEM_ROOT_ENABLED_MESSAGE)
    if policy_name not in _POLICIES_BY_NAME:
        raise ValueError(FILESYSTEM_ROOT_POLICY_MESSAGE)

    root = FilesystemRoot(
        name=name,
        path=_root_path(raw_path),
        policy=_POLICIES_BY_NAME[policy_name],
        enabled=enabled,
    )
    if root.path.is_file():
        raise ValueError(FILESYSTEM_ROOT_IS_A_FILE_MESSAGE)
    return raw_path, root


def _root_path(value: str) -> Path:
    """Interpret one configured root path without consulting ambient state.

    A leading ``~`` expands first, an absolute result stays absolute, and any
    other relative text is taken against the project root. The current working
    directory is never consulted, so Maria's roots do not move with the shell
    that started it. Canonicalisation itself belongs to ``FilesystemRoot``,
    which settles it once for every root.
    """
    try:
        expanded = Path(value).expanduser()
    except (RuntimeError, ValueError) as exc:
        raise ValueError(FILESYSTEM_ROOT_TEXT_MESSAGE) from exc
    if expanded.is_absolute():
        return expanded
    return PROJECT_ROOT / expanded


def _reject_unapproved_production_roots(
    configured: tuple[tuple[str, FilesystemRoot], ...],
    default_name: str,
) -> None:
    """Enforce the meaning of each production root, not merely its name.

    Maria accepts exactly two roots, and each one's path, policy, and enabled
    flag is fixed rather than advisory. Checking the semantics rather than
    trusting the names is what stops a root called ``workspace`` from quietly
    pointing at the repository, or a ``project_maria`` root from being turned
    on before the capability that is allowed to use it exists.

    Nothing is repaired here. A wrong value is refused, because silently
    correcting one would decide where Maria may read and write on the owner's
    behalf.
    """
    by_name = {root.name: (raw_path, root) for raw_path, root in configured}
    if tuple(sorted(by_name)) != tuple(sorted(PRODUCTION_ROOT_NAMES)):
        raise ValueError(FILESYSTEM_ROOT_INVENTORY_MESSAGE)

    workspace_raw_path, workspace = by_name[WORKSPACE_ROOT_NAME]
    if workspace_raw_path != WORKSPACE_CONFIGURED_PATH:
        raise ValueError(FILESYSTEM_WORKSPACE_PATH_MESSAGE)
    if workspace.policy is not RootPolicy.ORGANIZE:
        raise ValueError(FILESYSTEM_WORKSPACE_POLICY_MESSAGE)
    if not workspace.enabled:
        raise ValueError(FILESYSTEM_WORKSPACE_DISABLED_MESSAGE)
    if default_name != WORKSPACE_ROOT_NAME:
        raise ValueError(FILESYSTEM_WORKSPACE_DEFAULT_MESSAGE)
    if workspace.path.is_relative_to(
        REPOSITORY_ROOT
    ) or REPOSITORY_ROOT.is_relative_to(workspace.path):
        raise ValueError(FILESYSTEM_WORKSPACE_OVERLAP_MESSAGE)

    _, project_maria = by_name[PROJECT_MARIA_ROOT_NAME]
    if project_maria.path != REPOSITORY_ROOT:
        raise ValueError(FILESYSTEM_PROJECT_MARIA_PATH_MESSAGE)
    if project_maria.policy is not RootPolicy.READ_ONLY:
        raise ValueError(FILESYSTEM_PROJECT_MARIA_POLICY_MESSAGE)
    if project_maria.enabled:
        raise ValueError(FILESYSTEM_PROJECT_MARIA_ENABLED_MESSAGE)


def _reject_conflicting_roots(roots: tuple[FilesystemRoot, ...]) -> None:
    """Refuse repeated names, repeated canonical paths, and nesting."""
    names = [root.name for root in roots]
    if len(set(names)) != len(names):
        raise ValueError(FILESYSTEM_DUPLICATE_NAME_MESSAGE)

    for index, root in enumerate(roots):
        for other in roots[index + 1:]:
            if root.path == other.path:
                raise ValueError(FILESYSTEM_DUPLICATE_PATH_MESSAGE)
            if root.contains(other.path) or other.contains(root.path):
                raise ValueError(FILESYSTEM_OVERLAP_MESSAGE)


def _tracked_reasoning(raw_config: dict[str, Any]) -> ReasoningSettings:
    """Settle the tracked reasoning template, which may only be disabled.

    An absent section is the disabled default, which is what every older
    configuration means. A present one must be exactly ``enabled = false``:
    active settings are machine-specific and belong only in the untracked
    override, so a tracked file that enables reasoning, or names any
    provider field at all, is refused rather than trusted.
    """
    if "reasoning" not in raw_config:
        return DISABLED_REASONING
    table = _reasoning_table(raw_config["reasoning"])
    if table["enabled"]:
        raise ValueError(REASONING_TRACKED_ENABLED_MESSAGE)
    return _reasoning_settings(table)


def _reasoning_table(value: Any) -> dict[str, Any]:
    """Check the shape every [reasoning] table shares, wherever it came from.

    Unknown keys are refused inside this table only; the ignore-unknown-keys
    rule for Maria's older sections is unchanged.
    """
    if not isinstance(value, dict):
        raise ValueError(REASONING_SECTION_TABLE_MESSAGE)
    if not set(value) <= _REASONING_KEYS:
        raise ValueError(REASONING_UNKNOWN_KEY_MESSAGE)
    if "enabled" not in value:
        raise ValueError(REASONING_ENABLED_REQUIRED_MESSAGE)
    if type(value["enabled"]) is not bool:
        raise ValueError(REASONING_ENABLED_MESSAGE)
    return value


def _reasoning_settings(table: dict[str, Any]) -> ReasoningSettings:
    """Build settings from one checked table, with no default for any field.

    A disabled table must say nothing else. An enabled one must name all six
    active fields; a missing one is a refusal, never a gap filled from
    anywhere. Each value's type, range, and enumeration is then settled by
    ``ReasoningSettings`` itself.
    """
    if not table["enabled"]:
        if set(table) != {"enabled"}:
            raise ValueError(REASONING_DISABLED_FIELDS_MESSAGE)
        return DISABLED_REASONING

    if not _REASONING_ACTIVE_KEYS <= set(table):
        raise ValueError(REASONING_MISSING_FIELD_MESSAGE)

    transport_name = table["transport"]
    if (
        type(transport_name) is not str
        or transport_name not in _TRANSPORTS_BY_NAME
    ):
        raise ValueError(REASONING_TRANSPORT_MESSAGE)

    return ReasoningSettings(
        enabled=True,
        transport=_TRANSPORTS_BY_NAME[transport_name],
        host=table["host"],
        model=table["model"],
        request_deadline_seconds=table["request_deadline_seconds"],
        max_input_bytes=table["max_input_bytes"],
        max_output_bytes=table["max_output_bytes"],
    )


@dataclass(frozen=True, repr=False)
class _LocalOverride:
    """What the private override settled: its reasoning settings and zone.

    Each is None when the override does not declare that section. Neither
    is shown by ``repr``.
    """

    reasoning: ReasoningSettings | None
    time_zone: str | None

    def __repr__(self) -> str:
        """Name the type without showing any setting."""
        return "_LocalOverride(<redacted>)"


def _local_override(path: Path) -> _LocalOverride | None:
    """Return the private override's settings, or None when it is absent.

    The override may hold a [reasoning] table, a [time] table, or both, and
    nothing else, so it can never reach permissions, roots, logging, paths,
    or any other section.
    """
    entry = _local_override_entry(path)
    if entry is None:
        return None

    document = _local_override_document(_local_override_bytes(path, entry))
    sections = set(document)
    if not sections or not sections <= _LOCAL_OVERRIDE_SECTIONS:
        raise ValueError(LOCAL_OVERRIDE_SECTIONS_MESSAGE)
    reasoning = None
    if "reasoning" in document:
        reasoning = _reasoning_settings(_reasoning_table(document["reasoning"]))
    time_zone = None
    if "time" in document:
        time_zone = _time_zone(document["time"])
    return _LocalOverride(reasoning, time_zone)


def _time_zone(value: Any) -> str:
    """Return the [time] table's one zone key, refusing anything else.

    Only the key's syntax is settled here: whether time zone data for it can
    be loaded is a question for the operations that need it, so a missing
    data source never stops startup.
    """
    if not isinstance(value, dict):
        raise ValueError(TIME_SECTION_TABLE_MESSAGE)
    if set(value) != _TIME_KEYS:
        raise ValueError(TIME_SECTION_KEYS_MESSAGE)
    zone = value["zone"]
    if not is_zone_key(zone):
        raise ValueError(TIME_ZONE_MESSAGE)
    return zone


# Each reader below settles its failure inside the handler and raises its
# fixed refusal only after the handler has finished. A refusal therefore has
# neither a cause nor a context: the operating system's error, which names
# the path, and Python's TOML error, which keeps the whole document it was
# parsing, are both gone before anything is raised.


def _local_override_entry(path: Path) -> os.stat_result | None:
    """Describe the override's own directory entry, or None when it is absent.

    ``lstat`` does not follow a link, so a dangling link is present rather
    than absent, and it is refused like every other link. Only the entry
    being missing means there is no override.
    """
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        pass
    else:
        if not stat.S_ISREG(entry.st_mode):
            raise ValueError(LOCAL_OVERRIDE_ENTRY_MESSAGE)
        return entry
    raise ValueError(LOCAL_OVERRIDE_UNREADABLE_MESSAGE)


def _local_override_bytes(path: Path, entry: os.stat_result) -> bytes:
    """Read the override, refusing any entry that changed since inspection."""
    try:
        return _read_regular_file(path, entry)
    except OSError:
        pass
    raise ValueError(LOCAL_OVERRIDE_UNREADABLE_MESSAGE)


def _read_regular_file(path: Path, entry: os.stat_result) -> bytes:
    """Read at most one byte past the size bound from a verified regular file.

    The opened descriptor must still be a regular file with the identity
    ``lstat`` reported, so an entry swapped for a link, a directory, or a
    special file between the two looks is refused rather than read.
    """
    descriptor = os.open(path, _LOCAL_OVERRIDE_OPEN_FLAGS)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (entry.st_dev, entry.st_ino):
            raise ValueError(LOCAL_OVERRIDE_ENTRY_MESSAGE)

        content = b""
        while len(content) <= MAXIMUM_LOCAL_OVERRIDE_BYTES:
            chunk = os.read(
                descriptor,
                MAXIMUM_LOCAL_OVERRIDE_BYTES + 1 - len(content),
            )
            if not chunk:
                break
            content += chunk
    finally:
        os.close(descriptor)

    if len(content) > MAXIMUM_LOCAL_OVERRIDE_BYTES:
        raise ValueError(LOCAL_OVERRIDE_TOO_LARGE_MESSAGE)
    return content


def _local_override_document(content: bytes) -> dict[str, Any]:
    """Parse the override as UTF-8 TOML, or refuse without keeping any text."""
    try:
        return tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        pass
    raise ValueError(LOCAL_OVERRIDE_TOML_MESSAGE)


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
