"""Safe filesystem tool for Maria.

The tool performs ``read``, ``list``, ``rename``, ``move``, and ``organize``
inside Maria's named filesystem roots and nothing else. There is no write,
delete, copy, overwrite, glob, recursive, or archive action, and none is
reachable by argument. ``organize`` is the one batch: it moves at most five
files of one folder into fixed category folders, each pinned by the exact
state offered to the owner, through ``core/file_organization.py``.

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

``rename`` and ``move`` are Windows-only, call ``os.rename`` and nothing that
could copy or delete, require an existing destination parent, and never move a
folder into itself. They log fixed text only, and they remain path-based
check-then-act: they receive none of the handle protection ``organize`` has.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Mapping
from logging import Logger
from pathlib import Path
from typing import Any

from core import file_organization as organization
from core import organize_access as organize_native
from core.config import MariaConfig
from core.constants import DEFAULT_TEXT_READ_LIMIT_BYTES, REPOSITORY_ROOT
from core.context import quoted_for_owner
from core.filesystem_roots import (
    FilesystemProtections,
    FilesystemRoot,
    FilesystemRoots,
    ResolvedPath,
    RootPolicy,
)
from core.logger import get_logger
from core.tool_manager import ConfirmationBinding, ToolExecutionResult


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

# V1-25-R1 (D5-B): rename and move are Windows-only, move needs an existing
# destination parent, and neither moves a folder into itself.
WINDOWS_ONLY_LEGACY_MESSAGE = (
    "Renaming and moving files needs Windows; nothing was changed."
)
DESTINATION_FOLDER_MISSING_MESSAGE = "The destination folder does not exist."
MOVE_INTO_ITSELF_MESSAGE = "A folder cannot be moved into itself."

# The fixed log lines of rename, move, and organize. Each interpolates only a
# type name, a number, or a fixed code, never a path, a name, or exception text.
RENAMED_LOG_LINE = "Renamed a file or folder."
MOVED_LOG_LINE = "Moved a file or folder."
RENAME_FAILED_LOG = "Rename failed: %s"
MOVE_FAILED_LOG = "Move failed: %s"
ORGANIZE_REFUSED_LOG = "Organize refused: %s"
ORGANIZE_FAILED_LOG = "Organize failed: %s"
ORGANIZE_OFFER_FAILED_LOG = "Organize offer failed: %s"
ORGANIZE_UNEXPECTED_LOG = "Organize met an unexpected failure: %s"

# What an organize request records as its response when nothing was moved.
ORGANIZE_REFUSED_RECORD = "Organize folder: refused before any change."

# One pinned operation's argument name: its kind and its number.
_PIN_NAME = re.compile(r"(source|destination|create|identity)_([1-9][0-9]*)")

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


def _failure_signature(exc: OSError | ValueError) -> str:
    """Name a failure for the log by its type and numbers, never its text.

    An ``OSError`` carries its ``errno`` and, on Windows, its ``winerror``;
    neither can hold a path, unlike the exception's text and filenames. One of
    Maria's own refusals is logged as its fixed message, which interpolates
    nothing, and any other failure as its type name.
    """
    if isinstance(exc, OSError):
        return (
            f"{type(exc).__name__} errno={exc.errno} "
            f"winerror={getattr(exc, 'winerror', None)}"
        )
    message = str(exc)
    return message if message in _CONTROLLED_MESSAGES else type(exc).__name__


def _is_windows() -> bool:
    """Return whether Maria is running on Windows, which rename and move need."""
    return sys.platform == "win32"


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


def _pins_of(arguments: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read an organization's pinned operations from its step's arguments.

    The numbered arguments must be exactly what an offer produces: operations
    numbered from 1 without a gap, each with a source, a destination, and an
    identity, and a creation only beside them. Anything else is malformed, and
    a step that pins nothing is refused as nothing approved. Every value is
    checked by ``core.file_organization`` before any Windows call.
    """
    numbered: dict[int, dict[str, Any]] = {}
    for name, value in arguments.items():
        if name == "folder":
            continue
        match = _PIN_NAME.fullmatch(name)
        if match is None:
            raise organization.Refusal("MALFORMED")
        numbered.setdefault(int(match.group(2)), {})[match.group(1)] = value
    if not numbered:
        raise organization.Refusal("NOTHING")
    if sorted(numbered) != list(range(1, len(numbered) + 1)):
        raise organization.Refusal("MALFORMED")
    pins = []
    for number in sorted(numbered):
        fields = numbered[number]
        if not {"source", "destination", "identity"} <= set(fields):
            raise organization.Refusal("MALFORMED")
        pins.append(
            {
                "source": fields["source"],
                "destination": fields["destination"],
                "create": fields.get("create"),
                "identity": fields["identity"],
            }
        )
    return pins


def _moves_into_itself(source: Path, destination: Path) -> bool:
    """Return whether a folder would be relocated beneath itself.

    A prefix is compared by whole path components, so ``dir2/dir`` is not
    inside ``dir``; Windows path comparison ignores letter case.
    """
    return source.is_dir() and destination.is_relative_to(source)


class FilesystemTool:
    """Perform safe filesystem operations inside Maria's named roots."""

    name = "filesystem"
    description = (
        "Reads, lists, renames, moves, and organizes files inside a safe root."
    )
    actions = frozenset({"read", "list", "rename", "move", "organize"})

    def __init__(
        self,
        roots: FilesystemRoots,
        protections: FilesystemProtections,
        logger: Logger | None = None,
        access_loader: Callable[[], organize_native.OrganizeAccess | None] | None = None,
        scandir: Callable[[str], Any] | None = None,
    ) -> None:
        """Build the tool; the last two arguments let a test stand in for Windows.

        ``access_loader`` answers the object that makes the seven Windows
        calls, or None where they cannot be made. ``scandir`` lists a folder's
        names for the organization offer, path-based.
        """
        self.roots = roots
        self.protections = protections
        self.logger = logger or get_logger(__name__)
        self._access_loader = access_loader or organize_native.load_access
        self._scandir = scandir or os.scandir

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
        if action == "organize":
            return self._organize(arguments)
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
        """Rename a file or directory inside one organizing root.

        It is Windows-only, never moves a folder into itself, and logs fixed
        text only (V1-25-R1, D5-B). It is path-based check-then-act, so a
        path component replaced between the checks and the rename is not
        detected.
        """
        if not _is_windows():
            return ToolExecutionResult(False, WINDOWS_ONLY_LEGACY_MESSAGE)
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
            if _moves_into_itself(source, destination):
                return ToolExecutionResult(False, MOVE_INTO_ITSELF_MESSAGE)

            source.rename(destination)
            self.logger.info(RENAMED_LOG_LINE)
            return ToolExecutionResult(
                True,
                f"Renamed {self._display_path(source, source_root)} to "
                f"{self._display_path(destination, destination_root)}.",
                {"source": str(source), "destination": str(destination)},
            )
        except (OSError, ValueError) as exc:
            self.logger.warning(RENAME_FAILED_LOG, _failure_signature(exc))
            return ToolExecutionResult(
                False,
                f"Rename failed: {_failure_reason(exc)}",
            )

    def _move(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Move a file or directory inside one organizing root.

        The destination's parent must already exist, the move is one
        ``os.rename`` with no copy or delete fallback, so a cross-volume or
        locked move simply fails, and nothing is created on the way. Like
        ``rename`` it is Windows-only, never moves a folder into itself, logs
        fixed text only, and is path-based check-then-act (V1-25-R1, D5-B).
        """
        if not _is_windows():
            return ToolExecutionResult(False, WINDOWS_ONLY_LEGACY_MESSAGE)
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
            if not destination.parent.is_dir():
                return ToolExecutionResult(False, DESTINATION_FOLDER_MISSING_MESSAGE)
            if _moves_into_itself(source, destination):
                return ToolExecutionResult(False, MOVE_INTO_ITSELF_MESSAGE)

            os.rename(source, destination)
            self.logger.info(MOVED_LOG_LINE)
            return ToolExecutionResult(
                True,
                f"Moved {self._display_path(source, source_root)} to "
                f"{self._display_path(destination, destination_root)}.",
                {"source": str(source), "destination": str(destination)},
            )
        except (OSError, ValueError) as exc:
            self.logger.warning(MOVE_FAILED_LOG, _failure_signature(exc))
            return ToolExecutionResult(
                False,
                f"Move failed: {_failure_reason(exc)}",
            )

    def confirmation_binding(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any] | ConfirmationBinding:
        """Pin an organization to the exact files and folders offered for it.

        When the owner is about to decide, the folder's top level is read
        once and at most five eligible files are chosen. Each one's source,
        destination, metadata digest, and, only when its category folder is
        absent, the one folder creation approved, are added to the plan the
        owner approves, and the same list is shown as the preview. Nothing is
        held while the owner decides, and execution enumerates nothing.

        Anything that stops the offer, a refused folder, a file system that is
        not NTFS, too large a folder, nothing eligible, or an unexpected
        failure, adds no move and shows the reason: an approval that carries
        no move runs nothing. Nothing is added for any other action, or for a
        step that already carries more than its folder.
        """
        if action != "organize" or set(arguments) != {"folder"}:
            return {}
        access = self._access_loader()
        if access is None:
            return self._no_offer(organization.REFUSAL_TEXTS["WINDOWS_ONLY"])
        try:
            root, folder, parts = self._organize_folder(arguments)
        except ValueError as exc:
            return self._no_offer(_failure_reason(exc))
        try:
            if not folder.is_dir():
                display = quoted_for_owner(self._display_path(folder, root))
                return self._no_offer(f"Not a directory: {display}.")
            offer = organization.plan_offer(
                access,
                str(root.path),
                parts,
                self._organize_protected,
                self._scandir,
            )
        except Exception as exc:  # noqa: BLE001 - an offer must not crash Maria.
            self.logger.warning(ORGANIZE_OFFER_FAILED_LOG, type(exc).__name__)
            return self._no_offer(organization.REFUSAL_TEXTS["OFFER_FAILED"], parts)
        pinned: dict[str, str] = {}
        for number, pin in enumerate(offer.pins, start=1):
            pinned[f"source_{number}"] = pin["source"]
            pinned[f"destination_{number}"] = pin["destination"]
            if pin["create"] is not None:
                pinned[f"create_{number}"] = pin["create"]
            pinned[f"identity_{number}"] = pin["identity"]
        return ConfirmationBinding(pinned, offer.preview)

    @staticmethod
    def _no_offer(
        reason: str,
        parts: tuple[str, ...] | None = None,
    ) -> ConfirmationBinding:
        """Return an offer that holds no move, showing why."""
        return ConfirmationBinding({}, organization.refused_preview(reason, parts))

    def _organize(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Move exactly the files the owner approved, and report each outcome.

        The step carries the folder and the pins the offer added. A step with
        none, or with pins that are not exactly what an offer could have
        produced, runs nothing. Every refusal answers with fixed text, records
        only that nothing was changed, and logs a fixed code. A run that
        started records counts only: the request is recorded as typed by
        Brain, never a name or path, and the report, which does name the
        files, is displayed and not recorded.
        """
        access = self._access_loader()
        if access is None:
            return self._organize_refused("WINDOWS_ONLY")
        try:
            root, folder, parts = self._organize_folder(arguments)
        except ValueError as exc:
            self.logger.warning(ORGANIZE_FAILED_LOG, _failure_signature(exc))
            return ToolExecutionResult(
                False,
                f"Organize failed: {_failure_reason(exc)} Nothing was changed.",
                None,
                ORGANIZE_REFUSED_RECORD,
            )
        try:
            if not folder.is_dir():
                display = quoted_for_owner(self._display_path(folder, root))
                return ToolExecutionResult(
                    False,
                    f"Not a directory: {display}",
                    None,
                    ORGANIZE_REFUSED_RECORD,
                )
        except OSError as exc:
            self.logger.warning(ORGANIZE_FAILED_LOG, _failure_signature(exc))
            return self._organize_refused("OFFER_FAILED")
        try:
            pins = _pins_of(arguments)
            batch = organization.run_pinned(
                access,
                str(root.path),
                parts,
                pins,
                self._organize_protected,
            )
        except organization.Refusal as exc:
            return self._organize_refused(exc.code)
        except (KeyboardInterrupt, SystemExit) as exc:
            partial = getattr(exc, "maria_partial", None)
            if partial is not None:
                self.logger.warning(organization.count_line(partial))
            raise
        for failure_type in batch.failure_types:
            self.logger.warning(ORGANIZE_UNEXPECTED_LOG, failure_type)
        self.logger.info(organization.count_line(batch))
        return ToolExecutionResult(
            batch.success,
            organization.render_report(batch, pins),
            batch.counts,
            organization.recorded_response(batch),
        )

    def _organize_refused(self, code: str) -> ToolExecutionResult:
        """Answer a run refused before any change, by a fixed code."""
        self.logger.warning(ORGANIZE_REFUSED_LOG, code)
        return ToolExecutionResult(
            False,
            organization.refused_report(code),
            None,
            ORGANIZE_REFUSED_RECORD,
        )

    def _organize_folder(
        self,
        arguments: Mapping[str, Any],
    ) -> tuple[FilesystemRoot, Path, tuple[str, ...]]:
        """Settle the folder to organize, and its components below the root.

        The folder passes every gate a rename or move source does, and must
        belong to an organizing root. Its components are taken from the
        canonical target, so a folder named through a link is organized where
        the link points, inside the same root, and the preview names that
        folder.
        """
        resolved = self._safe_path(_path_argument(arguments, "folder"))
        self._refuse_protected_mutation(resolved)
        root = resolved.root
        if root.policy is not RootPolicy.ORGANIZE:
            raise ValueError(PATH_READ_ONLY_ROOT_MESSAGE)
        try:
            parts = resolved.target.relative_to(root.path).parts
        except ValueError:
            raise ValueError(PATH_OUTSIDE_ROOT_MESSAGE) from None
        return root, resolved.target, parts

    def _organize_protected(self, path: str) -> bool:
        """Say whether organization may not touch one absolute path.

        This is the existing protection, handed to the handle logic as one
        predicate: the permanent exclusions and the active runtime locations,
        the mutation check that also refuses a path containing one, and the
        repository barrier, each applied to the single identity it is given.
        """
        identity = Path(path)
        return (
            not self._is_reachable(identity, identity)
            or self.protections.intersects_mutation(identity)
            or _mutation_touches_repository(identity)
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
