"""Safe filesystem tool for Maria.

The tool performs ``read``, ``list``, ``rename``, and ``move`` inside Maria's
named filesystem roots and nothing else. There is no write, delete, copy,
overwrite, glob, recursive, bulk, or archive action, and none is reachable by
argument.

Three gates settle where a command may land, in this order. ``FilesystemRoots``
decides which enabled root, if any, holds the canonical target, and reports
both identities of the submitted path. ``FilesystemProtections`` then removes
what no filesystem capability may ever reach, which includes the locations
where the running configuration actually keeps Maria's database, log, and
system prompt, wherever those were configured. Finally this module applies its
own repository barrier: no path at or beneath the repository root is available
to these four generic commands, so Maria's source, documentation, prompts,
shipped configuration, and any untracked file that happens to sit beside them
stay out of reach even if a root were declared around them. The barrier lives
here rather than in the root model because it is a property of the generic
commands, not of the path model a later, narrower capability will reuse.

The last two gates are applied to the candidate spelling and to the canonical
target alike. Checking only the target would let a protected name such as
``Maria/logs`` be reached by making it a junction out of the repository;
checking only the candidate would let an ordinary name be a link back in.

``rename`` and ``move`` pass a fourth gate the reading actions do not, because
relocating a directory carries everything beneath it. An operand that merely
*contains* an active runtime location, or that contains the repository, is
refused even though reading and listing it are fine. That asymmetry is the
point: an ordinary parent directory stays listable with its protected children
filtered out, while still being immovable.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from logging import Logger
from pathlib import Path
from typing import Any

from core.config import MariaConfig
from core.constants import DEFAULT_TEXT_READ_LIMIT_BYTES, REPOSITORY_ROOT
from core.filesystem_roots import (
    FilesystemProtections,
    FilesystemRoot,
    FilesystemRoots,
    ResolvedPath,
    RootPolicy,
)
from core.logger import get_logger
from core.tool_manager import ToolExecutionResult


PATH_REQUIRED_MESSAGE = "Path is required."
PATH_NOT_TEXT_MESSAGE = "Path must be text."
PATH_OUTSIDE_ROOT_MESSAGE = "Path is outside Maria's configured safe root."
PATH_EXCLUDED_MESSAGE = (
    "Path is permanently excluded from Maria's filesystem access."
)
PATH_READ_ONLY_ROOT_MESSAGE = "This filesystem root is read-only."
CROSS_ROOT_MESSAGE = (
    "Source and destination must be inside the same filesystem root."
)
UNCONTAINED_DISPLAY = "<path outside safe root>"
UNSPECIFIED_FAILURE_REASON = "The operating system reported an unspecified error."

# The complete set of controlled messages a response may repeat. Each one is
# fixed and interpolates nothing, so none of them can widen to carry the
# submitted path, a root name, an excluded filename, or a host location.
_CONTROLLED_MESSAGES = frozenset(
    {
        PATH_REQUIRED_MESSAGE,
        PATH_NOT_TEXT_MESSAGE,
        PATH_OUTSIDE_ROOT_MESSAGE,
        PATH_EXCLUDED_MESSAGE,
        PATH_READ_ONLY_ROOT_MESSAGE,
        CROSS_ROOT_MESSAGE,
    }
)


def _errno_reason(number: int | None) -> str:
    """Describe an OS error using only its numeric code."""
    if not isinstance(number, int):
        return UNSPECIFIED_FAILURE_REASON
    try:
        reason = os.strerror(number).strip()
    except (OverflowError, ValueError):
        return UNSPECIFIED_FAILURE_REASON
    return reason or UNSPECIFIED_FAILURE_REASON


def _failure_reason(exc: OSError | ValueError) -> str:
    """Describe a filesystem failure without exposing host paths.

    An ``OSError`` reason is derived from its numeric ``errno``. Only Maria's
    fixed controlled ``ValueError`` messages are preserved, and every other
    failure uses the generic fallback. Exception-supplied text is read solely
    as a whitelist comparison key, so raw exception text and path fields such
    as ``strerror``, ``filename``, ``filename2``, and ``args`` are never
    returned or interpolated into user-facing response text.
    """
    if isinstance(exc, OSError):
        return _errno_reason(exc.errno)

    message = str(exc)
    if message in _CONTROLLED_MESSAGES:
        return message
    return UNSPECIFIED_FAILURE_REASON


def _is_inside_repository(*paths: Path) -> bool:
    """Return whether any supplied identity lies at or beneath the repository.

    This is the general-filesystem barrier. It is deliberately broader than
    the universal exclusions: it covers tracked source, documentation,
    prompts, the shipped configuration, and every untracked file beside them,
    because none of those is content the generic filesystem commands exist to
    hand out. A later Project Maria capability will reach approved repository
    content through its own admission rule rather than by relaxing this.

    It is applied symmetrically. A repository path that links outward is
    refused even though its target is elsewhere, and a path elsewhere that
    links into the repository is refused even though its name is not a
    repository name.
    """
    return any(path.is_relative_to(REPOSITORY_ROOT) for path in paths)


def _mutation_touches_repository(*paths: Path) -> bool:
    """Return whether relocating any identity would disturb the repository.

    A mutation reaches further than a read, so the barrier widens for it: a
    path that *contains* the repository is refused as well as one inside it.
    Without that, a synthetic root declared around the checkout could move
    Maria's own repository by naming its parent. Production configuration
    already forbids a workspace that overlaps the repository, so this is
    defence in depth for the generic tool rather than the only thing standing
    in the way.
    """
    return any(
        path.is_relative_to(REPOSITORY_ROOT) or REPOSITORY_ROOT.is_relative_to(path)
        for path in paths
    )


def _path_argument(
    arguments: Mapping[str, Any],
    name: str,
    default: str = "",
) -> str:
    """Return a path argument without converting a non-text value.

    A value of any other type is refused rather than rendered into a path,
    so a malformed direct call can no longer be turned into a real
    filesystem target. An absent argument still falls back to the caller's
    existing default.
    """
    value = arguments.get(name, default)
    if type(value) is not str:
        raise ValueError(PATH_NOT_TEXT_MESSAGE)
    return value


class FilesystemTool:
    """Perform safe filesystem operations inside Maria's named roots."""

    name = "filesystem"
    description = "Reads, lists, renames, and moves files inside a safe root."
    actions = frozenset({"read", "list", "rename", "move"})

    def __init__(
        self,
        roots: FilesystemRoots,
        protections: FilesystemProtections,
        logger: Logger | None = None,
    ) -> None:
        self.roots = roots
        self.protections = protections
        self.logger = logger or get_logger(__name__)

    def execute(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> ToolExecutionResult:
        """Execute a filesystem action."""
        if action == "read":
            return self._read(arguments)
        if action == "list":
            return self._list(arguments)
        if action == "rename":
            return self._rename(arguments)
        if action == "move":
            return self._move(arguments)
        return ToolExecutionResult(False, f"Unsupported filesystem action: {action}")

    def _read(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Read a text file inside a reachable root."""
        try:
            resolved = self._safe_path(_path_argument(arguments, "path"))
            root, path = resolved.root, resolved.target
            if not path.is_file():
                return ToolExecutionResult(
                    False,
                    f"Not a file: {self._display_path(path, root)}",
                )
            if path.stat().st_size > DEFAULT_TEXT_READ_LIMIT_BYTES:
                return ToolExecutionResult(
                    False,
                    "File exceeds Maria's text-read limit.",
                )

            text = path.read_text(encoding="utf-8", errors="replace")
            self.logger.info("Read file: %s", path)
            return ToolExecutionResult(True, text, {"path": str(path)})
        except (OSError, ValueError) as exc:
            self.logger.warning("File read failed: %s", exc)
            return ToolExecutionResult(
                False,
                f"File read failed: {_failure_reason(exc)}",
            )

    def _list(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """List a directory inside a reachable root.

        A reasoning model may propose this action, so its operational log
        lines are fixed text: neither the resolved directory, the submitted
        path, nor an exception's text ever reaches the log. What the owner
        is shown is unchanged.
        """
        try:
            resolved = self._safe_path(_path_argument(arguments, "path", "."))
            root, path = resolved.root, resolved.target
            if not path.is_dir():
                return ToolExecutionResult(
                    False,
                    f"Not a directory: {self._display_path(path, root)}",
                )

            entries = self._entries(path, root)
            self.logger.info("Listed a directory.")
            if not entries:
                return ToolExecutionResult(True, "Directory is empty.", entries)
            listing = "\n".join(f"{entry['type']}: {entry['name']}" for entry in entries)
            return ToolExecutionResult(True, listing, entries)
        except (OSError, ValueError) as exc:
            self.logger.warning("Directory listing failed.")
            return ToolExecutionResult(
                False,
                f"Directory listing failed: {_failure_reason(exc)}",
            )

    def _entries(
        self,
        path: Path,
        root: FilesystemRoot,
    ) -> list[dict[str, str]]:
        """Return the listable children of one already-validated directory.

        Each child is settled before it is described. The raw entry name gives
        the child's candidate spelling beneath the already-canonical parent,
        and resolving it gives the target; both go through the same
        protections and repository barrier a directly submitted path does.
        Only a child that survives all of it is classified, so a link pointing
        out of the root, a link pointing into a protected location, and a
        protected name that links outward are all omitted without ever being
        stat'ed or named.

        A child that is conclusively outside the root, universally excluded,
        or behind the repository barrier is omitted silently: the parent
        listing still succeeds and its ordinary siblings remain visible, so
        the response reveals neither the omitted name nor what it pointed at.
        Silence is limited to exactly those three answers. An unexpected
        ``OSError`` while resolving or classifying a child is not swallowed:
        it propagates so the whole listing fails through the caller's
        sanitised failure path rather than quietly returning a short listing
        that looks complete.
        """
        entries: list[dict[str, str]] = []
        for child in sorted(path.iterdir(), key=lambda item: item.name.lower()):
            candidate = path / child.name
            target = child.resolve()
            if not root.contains(target):
                continue
            if not self._is_reachable(candidate, target):
                continue
            entries.append(
                {
                    "name": child.name,
                    "type": "directory" if target.is_dir() else "file",
                }
            )
        return entries

    def _rename(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Rename a file or directory inside one organizing root."""
        try:
            source_root, source, destination_root, destination = self._operands(
                arguments
            )
            if not source.exists():
                return ToolExecutionResult(
                    False,
                    f"Source does not exist: "
                    f"{self._display_path(source, source_root)}",
                )
            if destination.exists():
                return ToolExecutionResult(
                    False,
                    f"Destination already exists: "
                    f"{self._display_path(destination, destination_root)}",
                )

            source.rename(destination)
            self.logger.info("Renamed %s to %s", source, destination)
            return ToolExecutionResult(
                True,
                f"Renamed {self._display_path(source, source_root)} to "
                f"{self._display_path(destination, destination_root)}.",
                {"source": str(source), "destination": str(destination)},
            )
        except (OSError, ValueError) as exc:
            self.logger.warning("Rename failed: %s", exc)
            return ToolExecutionResult(
                False,
                f"Rename failed: {_failure_reason(exc)}",
            )

    def _move(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Move a file or directory inside one organizing root."""
        try:
            source_root, source, destination_root, destination = self._operands(
                arguments
            )
            if not source.exists():
                return ToolExecutionResult(
                    False,
                    f"Source does not exist: "
                    f"{self._display_path(source, source_root)}",
                )
            if destination.exists():
                return ToolExecutionResult(
                    False,
                    f"Destination already exists: "
                    f"{self._display_path(destination, destination_root)}",
                )

            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            self.logger.info("Moved %s to %s", source, destination)
            return ToolExecutionResult(
                True,
                f"Moved {self._display_path(source, source_root)} to "
                f"{self._display_path(destination, destination_root)}.",
                {"source": str(source), "destination": str(destination)},
            )
        except (OSError, ValueError) as exc:
            self.logger.warning("Move failed: %s", exc)
            return ToolExecutionResult(
                False,
                f"Move failed: {_failure_reason(exc)}",
            )

    def _refuse_protected_mutation(self, *operands: ResolvedPath) -> None:
        """Refuse a rename or move that would disturb a protected location.

        Both identities of both operands are asked about together, so a
        protected location is out of reach whether it is named directly,
        reached through a link, carried along inside a source directory, or
        covered by a destination that does not exist yet.

        The refusal is the same fixed message every other exclusion uses and
        says nothing about which operand, which identity, or which active
        location caused it. Ordinary path resolution carries the usual
        limitation: this settles what the paths were when they were checked,
        and no path check can close the window between validating a path and
        acting on it.
        """
        identities = tuple(
            identity
            for operand in operands
            for identity in (operand.candidate, operand.target)
        )
        if self.protections.intersects_mutation(*identities):
            raise ValueError(PATH_EXCLUDED_MESSAGE)
        if _mutation_touches_repository(*identities):
            raise ValueError(PATH_EXCLUDED_MESSAGE)

    def _operands(
        self,
        arguments: Mapping[str, Any],
    ) -> tuple[FilesystemRoot, Path, FilesystemRoot, Path]:
        """Settle both operands of an organizing action.

        Source and destination are resolved independently through the same
        pipeline, so neither inherits the other's placement. The complete
        resolutions are kept until the mutation-specific check has run,
        because that check needs both identities of both operands rather than
        the canonical targets alone.

        Order matters here. Placement and direct reachability are settled
        first, then the mutation check, and only then policy and the same-root
        rule. Running the mutation check before either of those means a
        protected operand is refused for being protected rather than for
        whichever other rule it happened to trip, and running it before the
        existence checks in the callers means nothing is created, renamed, or
        moved on the way to finding out.

        Both operands must then belong to an organizing root, and both must
        belong to the *same* root: a cross-root rename or move is refused even
        when both roots organize, because moving content between named roots
        is a different capability from rearranging content within one.
        """
        resolved_source = self._safe_path(_path_argument(arguments, "source"))
        resolved_destination = self._safe_path(
            _path_argument(arguments, "destination")
        )
        self._refuse_protected_mutation(resolved_source, resolved_destination)

        source_root, source = resolved_source.root, resolved_source.target
        destination_root = resolved_destination.root
        destination = resolved_destination.target
        for root in (source_root, destination_root):
            if root.policy is not RootPolicy.ORGANIZE:
                raise ValueError(PATH_READ_ONLY_ROOT_MESSAGE)
        if source_root != destination_root:
            raise ValueError(CROSS_ROOT_MESSAGE)
        return source_root, source, destination_root, destination

    def _display_path(self, path: Path, root: FilesystemRoot) -> str:
        """Render an already-resolved contained path for user-facing text."""
        try:
            return path.relative_to(root.path).as_posix()
        except ValueError:
            return UNCONTAINED_DISPLAY

    def _is_reachable(self, candidate: Path, target: Path) -> bool:
        """Return whether both identities of a path survive every gate.

        The protections carry the active runtime locations as well as the
        fixed ones, so a database, log directory, or system prompt configured
        inside an enabled root is refused here rather than handed out. The
        repository barrier then applies to both identities too.
        """
        if self.protections.excludes(candidate, target):
            return False
        return not _is_inside_repository(candidate, target)

    def _safe_path(self, value: str) -> ResolvedPath:
        """Settle one submitted path against every gate that applies to it.

        The order is deliberate. Placement is settled first, so a path outside
        every enabled root is refused without Maria ever saying anything about
        what is inside a root. Only then are the permanent protections and the
        repository barrier applied, to the candidate spelling and the
        canonical target alike, and every one of them reports the same fixed
        message, so a refusal never distinguishes a protected file from an
        absent one, nor says which identity failed.
        """
        if not value.strip():
            raise ValueError(PATH_REQUIRED_MESSAGE)

        resolution = self.roots.resolve_path(value)
        if resolution is None:
            raise ValueError(PATH_OUTSIDE_ROOT_MESSAGE)
        if not self._is_reachable(resolution.candidate, resolution.target):
            raise ValueError(PATH_EXCLUDED_MESSAGE)
        return resolution


def build_tool(config: MariaConfig, **_: Any) -> FilesystemTool:
    """Build a filesystem tool instance.

    The tool is handed the same settled protections startup used to prepare
    the roots, so the boundary a command meets is the one configuration
    established rather than a second opinion assembled here.
    """
    return FilesystemTool(
        roots=config.filesystem_roots,
        protections=config.filesystem_protections,
    )
