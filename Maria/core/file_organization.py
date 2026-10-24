"""File organization: the handle logic of ``filesystem.organize`` (V1-25-R1).

``organize folder <folder>`` tidies the top level of one folder of the one
organize root into five fixed category folders. This module holds everything
that decides: the offer (which files, which folders, the exact pins), the check
of a pin, the held chain with its root and NTFS check, one operation's checks,
rename, verification and classification, the batch's stop rules, the release
rules, the counts, and the fixed texts. It calls no Windows function itself:
every call goes through ``core/organize_access.py``, handed in as ``access``.
It imports no configuration and no protections: the tool hands it a protection
predicate, called with an absolute path, that says whether a path is excluded.

What it can and cannot promise is stated where it matters. A move is checked
before and after, never undone, and never retried. A destination observed to
exist is refused, and the rename never replaces, but a link to the same file
that appears after that check can still let the rename succeed. An extra name of
a moved file is detected only at the one read after the move. The final-path
checks compare spellings, so they do not detect an ordinary directory replaced
by another of the same name before it was held. Names, paths, and file text are
never logged or recorded by this module: it logs nothing at all.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from core import organize_access as native
from core.context import quoted_for_owner
from core.organize_access import (
    AccessError,
    FileInfo,
    InvalidName,
    OptionsError,
    OrganizeAccess,
    PendingError,
    StoppedError,
)

MAXIMUM_OPERATIONS = 5
MAXIMUM_ENTRIES = 1000
MAXIMUM_LINKS = 1024  # a sanity bound: a larger count is treated as invalid
REQUIRED_FILESYSTEM = "NTFS"  # compared exactly, case and all

# The fixed category table: category folder -> last extensions, case-folded.
CATEGORY_TABLE = {
    "Documents": (
        "pdf", "doc", "docx", "odt", "rtf", "txt", "md", "csv", "xls", "xlsx",
        "ods", "ppt", "pptx", "odp",
    ),
    "Images": ("jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "heic"),
    "Audio": ("mp3", "wav", "flac", "aac", "ogg", "m4a"),
    "Video": ("mp4", "mkv", "mov", "avi", "wmv", "webm"),
    "Archives": ("zip", "7z", "rar", "tar", "gz"),
}
_CATEGORY_OF_EXTENSION = {
    extension: category
    for category, extensions in CATEGORY_TABLE.items()
    for extension in extensions
}

# File attributes organization reads.
ATTRIBUTE_DIRECTORY = 0x10
ATTRIBUTE_HIDDEN = 0x2
ATTRIBUTE_SYSTEM = 0x4
ATTRIBUTE_REPARSE_POINT = 0x400
ATTRIBUTE_OFFLINE = 0x1000
ATTRIBUTE_RECALL_ON_OPEN = 0x40000
ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
INELIGIBLE_ATTRIBUTES = (
    ATTRIBUTE_HIDDEN
    | ATTRIBUTE_SYSTEM
    | ATTRIBUTE_REPARSE_POINT
    | ATTRIBUTE_OFFLINE
    | ATTRIBUTE_RECALL_ON_OPEN
    | ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

_REFUSED_CHARACTERS = frozenset('*?"<>|')
_DIGEST = re.compile(r"[0-9a-f]{64}")

# The six operation statuses, one per bucket; the buckets sum to the plan.
STATUSES = (
    "moved",
    "reported_unconfirmed",
    "not_moved",
    "unknown",
    "refused",
    "not_attempted",
)

# Fixed refusals. Each one interpolates nothing, so none can carry a name, a
# path, a root name, or a host location.
REFUSAL_TEXTS = {
    "WINDOWS_ONLY": "Organizing folders needs 64-bit Windows; nothing was changed.",
    "FILESYSTEM_UNSUPPORTED": (
        "Maria organizes folders only on NTFS volumes, and could not confirm "
        "that this one is NTFS; nothing was changed."
    ),
    "STOPPED_AFTER_PENDING": (
        "Organizing folders stopped after an unconfirmed Windows request; "
        "restart Maria before trying again."
    ),
    "EXCLUDED": "Path is permanently excluded from Maria's filesystem access.",
    "NOTHING": "Nothing was approved to move, so nothing was changed.",
    "MALFORMED": "The approved moves are incomplete, so nothing was changed.",
    "TOO_LARGE": (
        "This folder has more than 1,000 entries; Maria organizes folders of up "
        "to 1,000 entries."
    ),
    "LINKED": "The path passes through a link, junction, or short name.",
    "ROOT_UNCONFIRMED": (
        "Maria could not confirm the workspace folder, so nothing was changed."
    ),
    "NOT_PLAIN_FILE": "The source is not a plain file Maria may move.",
    "SOURCE_CHANGED": (
        "The file is missing or has changed since the moves were offered."
    ),
    "HARD_LINKED": "The file has more than one name, so Maria does not move it.",
    "IN_USE": "The file is in use by another program.",
    "DESTINATION_EXISTS": "Something already exists at the destination.",
    "FOLDER_NOT_APPROVED": (
        "The destination folder is missing, and creating it was not approved."
    ),
    "FOLDER_NOT_PLAIN": "The destination folder is not a plain folder.",
    "FOLDER_NOT_CREATED": "The destination folder could not be created.",
    "FOLDER_UNCONFIRMED": "The destination folder could not be confirmed.",
    "DIFFERENT_VOLUME": "The file and its destination are on different volumes.",
    "DUPLICATE": "Two approved moves name the same file or destination.",
    "OFFER_FAILED": "Maria could not read this folder safely.",
    "UNEXPECTED": "Maria met an unexpected failure.",
}
UNSPECIFIED_REASON = "The operating system reported an unspecified error."

# The reasons Windows gives that Maria words herself; every other code is
# reported as the unspecified reason.
_NATIVE_REASONS = {
    0xC0000022: "Access is denied.",
    native.STATUS_SHARING_VIOLATION: "The file is in use by another program.",
    native.STATUS_OBJECT_NAME_COLLISION: "Something already exists at the destination.",
    native.STATUS_OBJECT_NAME_NOT_FOUND: "The file was not found.",
    0xC00000D4: "The destination is on a different volume.",
}

NOTHING_ELIGIBLE_TEXT = (
    "Nothing in {folder} matches Maria's file types, so approving changes nothing."
)
CLOSING_CLEAN = "All {planned} planned moves completed."
CLOSING_WITH_WARNINGS = (
    "All {planned} planned moves were made, but {warnings} warning(s) above "
    "prevent calling this a clean success. Nothing was undone."
)
CLOSING_STOPPED = (
    "Stopped: {moved} moved, {unconfirmed} reported but unconfirmed, "
    "{not_moved} not moved after an attempt, {unknown} outcome unknown, "
    "{refused} refused before an attempt, {not_attempted} not attempted; "
    "{created} folders created, {folder_unconfirmed} unconfirmed; "
    "{warnings} warnings. Nothing was undone."
)
EXTRA_NAME_TEXT = (
    "Warning: {source} was moved. When Maria checked after the move, the file "
    "also had another name that she did not move or remove. Maria stopped; "
    "nothing was undone."
)
RELEASE_TEXT = (
    "Warning: Maria could not release a file handle ({where}). The results above "
    "stand, nothing was undone, and she stopped."
)


class Refusal(Exception):
    """A check refused; carries only a fixed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---- names, categories, digests ----------------------------------------------
def check_component(name: str) -> None:
    """Refuse every name that is not one plain path component."""
    try:
        native.utf16_bytes(name)
    except InvalidName as exc:
        raise Refusal("MALFORMED") from exc
    if any(c in _REFUSED_CHARACTERS or ord(c) < 0x20 for c in name):
        raise Refusal("MALFORMED")
    if not name.strip(". ") or name != name.rstrip(". "):
        raise Refusal("MALFORMED")


def category_for(name: str) -> str | None:
    """Return the category folder of a file name by its last extension, or None."""
    if "." not in name:
        return None
    return _CATEGORY_OF_EXTENSION.get(name.rsplit(".", 1)[-1].casefold())


def digest_of(info: FileInfo) -> str:
    """Return the digest of checked metadata, which is not a content hash."""
    text = json.dumps(
        [info.volume, info.index, info.size, info.last_write],
        separators=(",", ":"),
    )
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _strip_prefix(path: str) -> str:
    return path[4:] if path.startswith("\\\\?\\") else path


def same_path(first: str, second: str) -> bool:
    """Compare two path spellings, ignoring case.

    A spelling check only: it does not detect an ordinary directory replaced by
    another of the same name before it was held.
    """
    return ntpath.normcase(_strip_prefix(first)) == ntpath.normcase(_strip_prefix(second))


def _is_protected(protected: Callable[[str], bool] | None, path: str) -> bool:
    return protected is not None and bool(protected(_strip_prefix(path)))


# ---- ownership ---------------------------------------------------------------
class HandleSet:
    """Every handle is entered the moment its call returns and released once.

    A handle is removed before its close is tried, so a handle whose close failed
    or was interrupted, and may therefore already be closed, is never closed
    again.
    """

    def __init__(self, access: OrganizeAccess) -> None:
        self._access = access
        self._open: list[int] = []
        self.last_failures = 0

    def add(self, handle: int) -> int:
        self._open.append(handle)
        return handle

    def release_all(self) -> int:
        """Attempt every release, newest first, then report.

        An ordinary close failure is counted. A KeyboardInterrupt or SystemExit
        from a close is counted, the remaining handles are still attempted, and
        the first such interruption is re-raised after the last attempt.
        """
        failures = 0
        interruption: BaseException | None = None
        while self._open:
            handle = self._open.pop()
            try:
                self._access.close_handle(handle)
            except (KeyboardInterrupt, SystemExit) as exc:
                failures += 1
                if interruption is None:
                    interruption = exc
            except Exception:  # noqa: BLE001 - counted, never retried
                failures += 1
        self.last_failures = failures
        if interruption is not None:
            raise interruption
        return failures


# ---- the held chain -----------------------------------------------------------
def open_root(
    access: OrganizeAccess,
    handles: HandleSet,
    root_path: str,
    protected: Callable[[str], bool] | None = None,
) -> tuple[int, str]:
    """Open and confirm the root, then require NTFS before anything else."""
    handle = handles.add(
        access.create_file(
            root_path,
            native.ROOT_ACCESS,
            native.OPEN_DIRECTORY_SHARE,
            native.ROOT_OPEN_FLAGS,
        )
    )
    info = access.file_info(handle)
    if not _is_plain_directory(info):
        raise Refusal("ROOT_UNCONFIRMED")
    final = access.final_path(handle)
    if not same_path(final, root_path):
        raise Refusal("ROOT_UNCONFIRMED")
    if _is_protected(protected, final):
        raise Refusal("EXCLUDED")
    try:
        name = access.filesystem_name(handle)
    except AccessError as exc:  # unreadable: refuse, never assume
        raise Refusal("FILESYSTEM_UNSUPPORTED") from exc
    if name != REQUIRED_FILESYSTEM:
        raise Refusal("FILESYSTEM_UNSUPPORTED")
    return handle, final


def _open_directory(access: OrganizeAccess, handles: HandleSet, parent: int, name: str) -> int:
    handle, _ = access.open_relative(
        parent,
        name,
        native.OPEN_DIRECTORY_ACCESS,
        native.OPEN_DIRECTORY_SHARE,
        native.OPEN_DIRECTORY_DISPOSITION,
        native.OPEN_DIRECTORY_OPTIONS,
    )
    return handles.add(handle)


def open_chain(
    access: OrganizeAccess,
    handles: HandleSet,
    root_path: str,
    parts: tuple[str, ...],
    protected: Callable[[str], bool] | None = None,
) -> tuple[int, str]:
    """Open the root and every folder component, holding each."""
    handle, final = open_root(access, handles, root_path, protected)
    for part in parts:
        handle = _open_directory(access, handles, handle, part)
        if not _is_plain_directory(access.file_info(handle)):
            raise Refusal("LINKED")
        held = access.final_path(handle)
        if not same_path(held, ntpath.join(_strip_prefix(final), part)):
            raise Refusal("LINKED")
        if _is_protected(protected, held):
            raise Refusal("EXCLUDED")
        final = held
    return handle, final


def _is_plain_directory(info: FileInfo) -> bool:
    return bool(info.attributes & ATTRIBUTE_DIRECTORY) and not (
        info.attributes & ATTRIBUTE_REPARSE_POINT
    )


def _is_plain_file(info: FileInfo) -> bool:
    return not (info.attributes & (ATTRIBUTE_DIRECTORY | ATTRIBUTE_REPARSE_POINT))


# ---- lookups -------------------------------------------------------------------
@dataclass(frozen=True)
class Lookup:
    """What looking one name up under a held directory found."""

    kind: str  # "absent" (a definitive not-found), "object", or "error"
    info: FileInfo | None = None


def lookup(
    access: OrganizeAccess, handles: HandleSet, directory: int, name: str
) -> Lookup:
    """Look one name up under a held directory.

    The handle it opens is entered in the caller's set. A pending request is
    never an answer: it propagates, so the operation stops.
    """
    try:
        handle, _ = access.open_relative(
            directory,
            name,
            native.LOOKUP_ACCESS,
            native.LOOKUP_SHARE,
            native.FILE_OPEN,
            native.LOOKUP_OPTIONS,
        )
    except PendingError:
        raise
    except AccessError as exc:
        if exc.kind == "nt" and exc.code == native.STATUS_OBJECT_NAME_NOT_FOUND:
            return Lookup("absent")
        return Lookup("error")
    except (InvalidName, OptionsError, StoppedError):
        return Lookup("error")
    handles.add(handle)
    try:
        return Lookup("object", access.file_info(handle))
    except PendingError:
        raise
    except Exception:  # noqa: BLE001
        return Lookup("error")


# ---- outcomes -------------------------------------------------------------------
@dataclass
class OperationOutcome:
    """The one status of one planned operation, and what led to it."""

    status: str
    reason: str = ""
    folder: str = "none"  # none, created, existing, unconfirmed, unknown, not_created
    attempted: bool = False
    cleanup_failures: int = 0
    extra_name: bool = False  # a link count above one was observed after the move
    pending: bool = False
    interrupted: bool = False
    code: int = 0  # the native code of the failure, for its fixed wording
    failure_type: str = ""  # the type name of an unexpected failure, never its text


@dataclass
class BatchResult:
    """What a batch did, operation by operation."""

    outcomes: list[OperationOutcome] = field(default_factory=list)
    terminal_cleanup_failures: int = 0
    stopped_reason: str = ""
    pending: bool = False
    interrupted: bool = False
    preflight_index: int = 0  # the 1-based operation a preflight refused, or 0
    preflight_failure: str = ""  # the type name of an unexpected preflight failure
    preflight_cleanup_failures: int = 0  # handles the preflight could not release

    @property
    def failure_types(self) -> list[str]:
        """Return the type names of unexpected failures, never their text."""
        types = [self.preflight_failure] if self.preflight_failure else []
        return types + [o.failure_type for o in self.outcomes if o.failure_type]

    @property
    def counts(self) -> dict[str, int]:
        return {
            status: sum(1 for outcome in self.outcomes if outcome.status == status)
            for status in STATUSES
        }

    @property
    def folders_created(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.folder == "created")

    @property
    def folders_unconfirmed(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.folder in ("unconfirmed", "unknown"))

    @property
    def cleanup_failed(self) -> bool:
        return (
            bool(self.preflight_cleanup_failures)
            or bool(self.terminal_cleanup_failures)
            or any(outcome.cleanup_failures for outcome in self.outcomes)
        )

    @property
    def refused_before_any_change(self) -> bool:
        """Return whether the run stopped before the first creation or rename."""
        return bool(
            self.preflight_index
            or self.stopped_reason == "PREFLIGHT_RELEASE_FAILED"
            or (self.stopped_reason == "ROOT_UNCONFIRMED" and not self.pending)
        )

    @property
    def extra_names(self) -> bool:
        return any(outcome.extra_name for outcome in self.outcomes)

    @property
    def warnings(self) -> int:
        """Count the warnings: each extra name, and each failed release."""
        return (
            sum(1 for outcome in self.outcomes if outcome.extra_name)
            + sum(1 for outcome in self.outcomes if outcome.cleanup_failures)
            + (1 if self.preflight_cleanup_failures else 0)
            + (1 if self.terminal_cleanup_failures else 0)
        )

    @property
    def success(self) -> bool:
        return (
            bool(self.outcomes)
            and all(outcome.status == "moved" for outcome in self.outcomes)
            and not self.cleanup_failed
            and not self.extra_names
            and not self.pending
            and not self.interrupted
        )


def classify_failed_rename(source: FileInfo, found: Lookup, destination: Lookup) -> str:
    """Classify an operation whose rename call failed.

    "not_moved" needs positive evidence on both sides: the source name still
    resolves to a plain regular file with the expected identity (U1), and the
    destination is absent or a plain regular file with a different identity
    (U2). Anything else is "unknown".
    """
    u1 = (
        found.kind == "object"
        and found.info is not None
        and _is_plain_file(found.info)
        and (found.info.volume, found.info.index) == (source.volume, source.index)
    )
    if destination.kind == "absent":
        u2 = True
    elif destination.kind == "object" and destination.info is not None:
        u2 = _is_plain_file(destination.info) and (
            destination.info.volume,
            destination.info.index,
        ) != (source.volume, source.index)
    else:
        u2 = False
    return "not_moved" if (u1 and u2) else "unknown"


def verify_moved(source: FileInfo, found: Lookup, destination: Lookup) -> bool:
    """Check that the destination holds the moved file and the source name does not.

    It does not rely on a stable link count: a second name can appear while the
    source is held.
    """
    identity = (source.volume, source.index)
    if not (
        destination.kind == "object"
        and destination.info is not None
        and _is_plain_file(destination.info)
        and (destination.info.volume, destination.info.index) == identity
    ):
        return False
    if found.kind == "absent":
        return True
    return (
        found.kind == "object"
        and found.info is not None
        and (found.info.volume, found.info.index) != identity
    )


# ---- one operation ----------------------------------------------------------------
def _verify_category(
    access: OrganizeAccess,
    handle: int,
    folder_final: str,
    category: str,
    protected: Callable[[str], bool] | None,
) -> None:
    """A category's final path must be its held parent's path plus the category."""
    final = access.final_path(handle)
    if not same_path(final, ntpath.join(_strip_prefix(folder_final), category)):
        raise Refusal("LINKED")
    if _is_protected(protected, final):
        raise Refusal("EXCLUDED")


def _open_category(
    access: OrganizeAccess,
    handles: HandleSet,
    folder: int,
    folder_final: str,
    category: str,
    create_approved: bool,
    protected: Callable[[str], bool] | None,
    outcome: OperationOutcome,
    create: bool,
) -> int | None:
    """Open the category folder; create it only when that was approved.

    With ``create`` false (a preflight) an absent approved folder returns None.
    """
    try:
        handle = _open_directory(access, handles, folder, category)
    except PendingError:
        raise
    except AccessError as exc:
        if not (exc.kind == "nt" and exc.code == native.STATUS_OBJECT_NAME_NOT_FOUND):
            raise Refusal("FOLDER_NOT_PLAIN") from exc
        if not create_approved:
            raise Refusal("FOLDER_NOT_APPROVED") from exc
        if not create:
            return None
        return _create_category(
            access, handles, folder, folder_final, category, protected, outcome
        )
    if not _is_plain_directory(access.file_info(handle)):
        raise Refusal("FOLDER_NOT_PLAIN")
    _verify_category(access, handle, folder_final, category, protected)
    outcome.folder = "existing"
    return handle


def _create_category(
    access: OrganizeAccess,
    handles: HandleSet,
    folder: int,
    folder_final: str,
    category: str,
    protected: Callable[[str], bool] | None,
    outcome: OperationOutcome,
) -> int:
    try:
        handle, information = access.open_relative(
            folder,
            category,
            native.CREATE_DIRECTORY_ACCESS,
            native.CREATE_DIRECTORY_SHARE,
            native.CREATE_DIRECTORY_DISPOSITION,
            native.CREATE_DIRECTORY_OPTIONS,
        )
    except (StoppedError, InvalidName, OptionsError):
        raise  # refused before any native call: nothing was created
    except (PendingError, KeyboardInterrupt, SystemExit):
        outcome.folder = "unknown"  # the directory may still have been created
        raise
    except AccessError as exc:
        if exc.kind == "nt" and exc.code == native.STATUS_OBJECT_NAME_COLLISION:
            try:
                handle = _open_directory(access, handles, folder, category)
                if _is_plain_directory(access.file_info(handle)):
                    _verify_category(access, handle, folder_final, category, protected)
                    outcome.folder = "existing"
                    return handle
            except PendingError:
                raise
            except AccessError:
                pass
            raise Refusal("FOLDER_NOT_PLAIN") from exc
        outcome.folder = "unknown"  # until the lookup below says otherwise
        found = lookup(access, handles, folder, category)
        outcome.folder = "not_created" if found.kind == "absent" else "unknown"
        raise Refusal("FOLDER_NOT_CREATED") from exc
    except Exception:  # noqa: BLE001 - the result of the call cannot be established.
        outcome.folder = "unknown"
        raise
    handles.add(handle)
    outcome.folder = "unconfirmed"  # created; stays so unless the checks below finish
    try:
        ok = information == native.FILE_CREATED and _is_plain_directory(
            access.file_info(handle)
        )
        if ok:
            _verify_category(access, handle, folder_final, category, protected)
    except PendingError:
        raise
    except (Refusal, AccessError):
        ok = False
    outcome.folder = "created" if ok else "unconfirmed"
    if not ok:
        raise Refusal("FOLDER_UNCONFIRMED")
    return handle


def _open_source(
    access: OrganizeAccess,
    handles: HandleSet,
    folder: int,
    name: str,
    digest: str | None,
    volume: int | None,
) -> tuple[int, FileInfo]:
    """Open the source for moving and check it; return its handle and information."""
    try:
        handle, _ = access.open_relative(
            folder,
            name,
            native.SOURCE_ACCESS,
            native.SOURCE_SHARE,
            native.FILE_OPEN,
            native.SOURCE_OPTIONS,
        )
    except PendingError:
        raise
    except AccessError as exc:
        if exc.kind == "nt" and exc.code == native.STATUS_SHARING_VIOLATION:
            raise Refusal("IN_USE") from exc
        if exc.kind == "nt" and exc.code == native.STATUS_OBJECT_NAME_NOT_FOUND:
            raise Refusal("SOURCE_CHANGED") from exc
        raise
    handles.add(handle)
    info = access.file_info(handle)
    if info.attributes & ATTRIBUTE_DIRECTORY or info.attributes & INELIGIBLE_ATTRIBUTES:
        raise Refusal("NOT_PLAIN_FILE")
    if info.links != 1:
        raise Refusal("HARD_LINKED")
    if info.index == 0:
        raise Refusal("NOT_PLAIN_FILE")
    if volume is not None and info.volume != volume:
        raise Refusal("DIFFERENT_VOLUME")
    if digest is not None and digest_of(info) != digest:
        raise Refusal("SOURCE_CHANGED")
    return handle, info


def move_one(
    access: OrganizeAccess,
    folder: int,
    folder_final: str,
    name: str,
    category: str,
    create_approved: bool,
    digest: str | None = None,
    protected: Callable[[str], bool] | None = None,
    volume: int | None = None,
) -> OperationOutcome:
    """Run one operation under a held folder; release every handle it opened.

    An interruption (KeyboardInterrupt or SystemExit) from the operation or from
    its release is re-raised after every handle has been attempted, carrying the
    outcome known so far in ``maria_outcome``.
    """
    outcome = OperationOutcome("refused")
    handles = HandleSet(access)
    interruption: BaseException | None = None
    try:
        try:
            check_component(name)  # lexical refusal before any native call
            check_component(category)
            _move_one(
                access, handles, folder, folder_final, name, category,
                create_approved, digest, protected, volume, outcome,
            )
        except Refusal as exc:
            outcome.status, outcome.reason = "refused", exc.code
        except StoppedError:
            outcome.status, outcome.reason = "refused", "STOPPED_AFTER_PENDING"
        except (InvalidName, OptionsError):
            outcome.status, outcome.reason = "refused", "MALFORMED"
        except PendingError:
            native.latch_pending()
            outcome.pending = True
            outcome.reason = "PENDING"
            outcome.status = "unknown" if outcome.attempted else "refused"
        except AccessError as exc:
            outcome.status = "unknown" if outcome.attempted else "refused"
            outcome.reason = "NATIVE"
            outcome.code = exc.code
        except (KeyboardInterrupt, SystemExit) as exc:
            interruption = exc
            outcome.interrupted = True
            outcome.reason = "INTERRUPTED"
            outcome.status = "unknown" if outcome.attempted else outcome.status
        except Exception as exc:  # noqa: BLE001 - reported, never an opaque failure.
            # A failure nobody expected must not hide what already happened: once
            # the rename was called the outcome is unknown, never refused, and the
            # run stops with its report. Only the type name is kept.
            outcome.status = "unknown" if outcome.attempted else "refused"
            outcome.reason = "UNEXPECTED"
            outcome.failure_type = type(exc).__name__
    finally:
        try:
            outcome.cleanup_failures = handles.release_all()
        except (KeyboardInterrupt, SystemExit) as exc:
            outcome.cleanup_failures = handles.last_failures
            outcome.interrupted = True
            interruption = interruption or exc
    if interruption is not None:
        interruption.maria_outcome = outcome  # type: ignore[attr-defined]
        raise interruption
    return outcome


def _move_one(
    access: OrganizeAccess,
    handles: HandleSet,
    folder: int,
    folder_final: str,
    name: str,
    category: str,
    create_approved: bool,
    digest: str | None,
    protected: Callable[[str], bool] | None,
    volume: int | None,
    outcome: OperationOutcome,
) -> None:
    category_handle = _open_category(
        access, handles, folder, folder_final, category, create_approved,
        protected, outcome, True,
    )
    source, source_info = _open_source(access, handles, folder, name, digest, volume)
    if lookup(access, handles, category_handle, name).kind != "absent":
        raise Refusal("DESTINATION_EXISTS")

    outcome.attempted = True
    try:
        access.rename_into(source, category_handle, name)
    except PendingError:
        raise  # move_one records the unknown outcome and stops the run
    except StoppedError:
        outcome.attempted = False
        raise
    except AccessError as exc:
        # Only a native failure the call itself reported is classified; anything
        # else propagates to move_one as an unexpected failure (unknown here).
        found = lookup(access, handles, folder, name)
        destination = lookup(access, handles, category_handle, name)
        outcome.status = classify_failed_rename(source_info, found, destination)
        outcome.reason = "RENAME_FAILED"
        outcome.code = exc.code
        return
    try:
        found = lookup(access, handles, folder, name)
        destination = lookup(access, handles, category_handle, name)
        moved = verify_moved(source_info, found, destination)
    except PendingError:
        raise
    except Exception:  # noqa: BLE001
        moved = False
    if not moved:
        outcome.status, outcome.reason = "reported_unconfirmed", "VERIFY_FAILED"
        return
    try:
        after = access.file_info(source)
    except PendingError:
        raise
    except Exception:  # noqa: BLE001
        after = None
    # Zero, implausible, failed, or inconsistent information is never evidence of
    # another name and never a clean success: the post-move checks were not done.
    if (
        after is None
        or (after.volume, after.index) != (source_info.volume, source_info.index)
        or not 1 <= after.links <= MAXIMUM_LINKS
    ):
        outcome.status, outcome.reason = "reported_unconfirmed", "POST_MOVE_READ_FAILED"
        return
    outcome.status = "moved"
    if after.links > 1:
        outcome.extra_name, outcome.reason = True, "EXTRA_NAME"


# ---- the batch ----------------------------------------------------------------------
def _preflight(
    access: OrganizeAccess,
    folder: int,
    folder_final: str,
    operations: list[dict],
    protected: Callable[[str], bool] | None,
    volume: int,
    result: BatchResult,
) -> tuple[int, str, str] | None:
    """Check every operation without changing anything; return the first refusal.

    Returns the 1-based index, the fixed code, and, for an unexpected failure,
    its type name, of the first operation that cannot run, or None. Nothing is
    created and nothing is renamed here. The handles it could not release are
    counted on ``result``, whatever else happened.
    """
    handles = HandleSet(access)
    try:
        for index, operation in enumerate(operations, start=1):
            scratch = OperationOutcome("refused")
            try:
                check_component(operation["name"])
                check_component(operation["category"])
                category_handle = _open_category(
                    access, handles, folder, folder_final, operation["category"],
                    operation["create_approved"], protected, scratch, False,
                )
                _open_source(
                    access, handles, folder, operation["name"],
                    operation.get("digest"), volume,
                )
                if category_handle is not None and (
                    lookup(access, handles, category_handle, operation["name"]).kind
                    != "absent"
                ):
                    raise Refusal("DESTINATION_EXISTS")
            except Refusal as exc:
                return index, exc.code, ""
            except StoppedError:
                return index, "STOPPED_AFTER_PENDING", ""
            except (InvalidName, OptionsError):
                return index, "MALFORMED", ""
            except PendingError:
                native.latch_pending()
                return index, "STOPPED_AFTER_PENDING", ""
            except AccessError:
                return index, "SOURCE_CHANGED", ""
            except Exception as exc:  # noqa: BLE001 - nothing changed; say so.
                return index, "UNEXPECTED", type(exc).__name__
        return None
    finally:
        # Every release is attempted once. The failures are kept on the batch even
        # when a refusal is already known or an interruption comes out of a release,
        # so that the run stops before any creation or rename.
        try:
            handles.release_all()
        finally:
            result.preflight_cleanup_failures = handles.last_failures


def _refuse_all(result: BatchResult, operations: list[dict], code: str, index: int = 0) -> None:
    result.outcomes = [
        OperationOutcome("refused", code if not index or position == index else "")
        for position in range(1, len(operations) + 1)
    ]
    result.preflight_index = index
    result.stopped_reason = "ROOT_UNCONFIRMED" if not index else code


def _run_operations(
    access: OrganizeAccess,
    folder: int,
    folder_final: str,
    operations: list[dict],
    result: BatchResult,
    protected: Callable[[str], bool] | None,
    volume: int,
) -> BaseException | None:
    """Run the operations in order; return an interruption, or None."""
    stopped = False
    interruption: BaseException | None = None
    for operation in operations:
        if stopped:
            result.outcomes.append(OperationOutcome("not_attempted"))
            continue
        if native.pending_latched():
            result.outcomes.append(OperationOutcome("refused", "STOPPED_AFTER_PENDING"))
            stopped = True
            result.stopped_reason = "STOPPED_AFTER_PENDING"
            continue
        try:
            outcome = move_one(
                access, folder, folder_final, operation["name"], operation["category"],
                operation["create_approved"], operation.get("digest"), protected, volume,
            )
        except (KeyboardInterrupt, SystemExit) as exc:
            interruption = exc
            known = getattr(exc, "maria_outcome", None)
            result.outcomes.append(known if known is not None else OperationOutcome("unknown"))
            result.interrupted = True
            stopped = True
            result.stopped_reason = "INTERRUPTED"
            continue
        result.outcomes.append(outcome)
        if outcome.pending:
            result.pending = True
        if outcome.status != "moved" or outcome.folder in ("unconfirmed", "unknown", "not_created"):
            stopped = True
            result.stopped_reason = outcome.reason or outcome.status
        elif outcome.cleanup_failures:
            stopped = True
            result.stopped_reason = "RELEASE_FAILED"
        elif outcome.extra_name:
            stopped = True
            result.stopped_reason = "EXTRA_NAME"
    return interruption


def run_batch(
    access: OrganizeAccess,
    root_path: str,
    folder_parts: tuple[str, ...],
    operations: list[dict],
    protected: Callable[[str], bool] | None = None,
) -> BatchResult:
    """Run a batch under one held chain; report every operation exactly once.

    Operations are dictionaries with ``name``, ``category``, ``create_approved``
    and an optional ``digest``. Every operation is checked before the first one
    changes anything. An interruption (KeyboardInterrupt or SystemExit) is
    re-raised after every owned handle has been attempted, carrying the partial
    result in ``maria_partial``.
    """
    result = BatchResult()
    if native.pending_latched():
        _refuse_all(result, operations, "STOPPED_AFTER_PENDING")
        result.stopped_reason = "STOPPED_AFTER_PENDING"
        return result
    chain = HandleSet(access)
    interruption: BaseException | None = None
    try:
        folder = None
        try:
            folder, folder_final = open_chain(access, chain, root_path, folder_parts, protected)
            volume = access.file_info(folder).volume
        except PendingError:
            native.latch_pending()
            result.pending = True
            _refuse_all(result, operations, "ROOT_UNCONFIRMED")
            folder = None
        except StoppedError:
            _refuse_all(result, operations, "STOPPED_AFTER_PENDING")
            folder = None
        except (Refusal, AccessError) as exc:
            _refuse_all(
                result, operations, exc.code if isinstance(exc, Refusal) else "ROOT_UNCONFIRMED"
            )
            folder = None
        except (KeyboardInterrupt, SystemExit) as exc:
            interruption = exc
            result.interrupted = True
            folder = None  # the volume may be unread: no preflight, creation or rename
        if folder is not None:
            try:
                problem = _preflight(
                    access, folder, folder_final, operations, protected, volume, result
                )
            except (KeyboardInterrupt, SystemExit) as exc:
                interruption = exc
                result.interrupted = True
                problem = (0, "INTERRUPTED", "")
            if problem is not None:
                if native.pending_latched():
                    result.pending = True
                if interruption is None:
                    _refuse_all(result, operations, problem[1], problem[0])
                    result.preflight_failure = problem[2]
            elif result.preflight_cleanup_failures:
                # Nothing was changed, and nothing will be: a handle that could not
                # be released is a failure the run does not go on from.
                _refuse_all(result, operations, "PREFLIGHT_RELEASE_FAILED")
                result.stopped_reason = "PREFLIGHT_RELEASE_FAILED"
            else:
                interruption = _run_operations(
                    access, folder, folder_final, operations, result, protected, volume
                )
    finally:
        # The chain is released last: no later operation can refuse, so a failure
        # is reported on the batch itself, and every handle is still attempted.
        try:
            result.terminal_cleanup_failures = chain.release_all()
        except (KeyboardInterrupt, SystemExit) as exc:
            result.terminal_cleanup_failures = chain.last_failures
            result.interrupted = True
            interruption = interruption or exc
    if interruption is not None:
        while len(result.outcomes) < len(operations):
            result.outcomes.append(OperationOutcome("not_attempted"))
        interruption.maria_partial = result  # type: ignore[attr-defined]
        raise interruption
    return result


# ---- the pins ------------------------------------------------------------------------
def _relative(parts: Iterable[str]) -> str:
    return "/".join(parts)


def _prefixes(root_path: str, relative: str) -> list[str]:
    """Return the root and every prefix of an absolute path built from ``relative``."""
    paths = [root_path]
    current = root_path
    for part in relative.split("/"):
        current = ntpath.join(current, part)
        paths.append(current)
    return paths


def _protected_paths(
    root_path: str, relatives: Iterable[str | None], protected: Callable[[str], bool] | None
) -> bool:
    """Return whether the root, or any prefix of any path, is protected."""
    if protected is None:
        return False
    return any(
        protected(path)
        for relative in relatives
        if relative is not None
        for path in _prefixes(root_path, relative)
    )


def validate_pins(
    root_path: str,
    folder_parts: tuple[str, ...],
    pins: list,
    protected: Callable[[str], bool] | None = None,
) -> list[dict]:
    """Check every pin's exact shape and protections before any native call.

    Each pin is a dictionary with ``source``, ``destination``, ``create`` (or
    None) and ``identity``, with root-relative "/" paths. The destination must be
    the folder, the table category of the name, and the name; ``create`` must be
    the destination's category parent exactly, or None. Every source,
    destination and creation path, and every prefix of each, root included, is
    asked of the protection predicate.
    """
    for part in folder_parts:
        check_component(part)
    if type(pins) is not list or not 1 <= len(pins) <= MAXIMUM_OPERATIONS:
        raise Refusal("MALFORMED")
    folder = _relative(folder_parts)
    prefix = folder + "/" if folder else ""
    operations: list[dict] = []
    seen: set[str] = set()
    for pin in pins:
        if (
            type(pin) is not dict
            or not {"source", "destination", "identity"} <= set(pin)
            or set(pin) - {"source", "destination", "create", "identity"}
        ):
            raise Refusal("MALFORMED")
        source, destination = pin["source"], pin["destination"]
        create, identity = pin.get("create"), pin["identity"]
        if type(source) is not str or type(destination) is not str:
            raise Refusal("MALFORMED")
        if not source.startswith(prefix):
            raise Refusal("MALFORMED")
        name = source[len(prefix):]
        check_component(name)
        category = category_for(name)
        if category is None or destination != f"{prefix}{category}/{name}":
            raise Refusal("MALFORMED")
        if create is not None and create != f"{prefix}{category}":
            raise Refusal("MALFORMED")
        if type(identity) is not str or not _DIGEST.fullmatch(identity):
            raise Refusal("MALFORMED")
        keys = (source.casefold(), destination.casefold())
        if keys[0] in seen or keys[1] in seen:
            raise Refusal("DUPLICATE")
        seen.update(keys)
        if _protected_paths(root_path, (source, destination, create), protected):
            raise Refusal("EXCLUDED")
        operations.append(
            {
                "name": name,
                "category": category,
                "create_approved": create is not None,
                "digest": identity,
            }
        )
    return operations


def run_pinned(
    access: OrganizeAccess,
    root_path: str,
    folder_parts: tuple[str, ...],
    pins: list,
    protected: Callable[[str], bool] | None = None,
) -> BatchResult:
    """Validate the pins (no native call), then run the batch."""
    operations = validate_pins(root_path, folder_parts, pins, protected)
    return run_batch(access, root_path, folder_parts, operations, protected)


# ---- the offer ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Offer:
    """What organizing a folder would do: the exact pins, and what to show."""

    pins: tuple[dict, ...]
    preview: str
    refusal: str | None = None
    planned: int = 0
    remaining: int = 0


def _display_folder(folder_parts: tuple[str, ...]) -> str:
    return quoted_for_owner(_relative(folder_parts) or ".")


def refused_preview(reason: str, folder_parts: tuple[str, ...] | None = None) -> str:
    """Return the preview of an offer that holds no move, from a fixed reason.

    The reason is fixed text that interpolates nothing, so the preview names a
    folder only when the caller knows it and was able to settle it.
    """
    folder = "" if folder_parts is None else f" {_display_folder(folder_parts)}"
    return f"Organize folder{folder}: {reason} Approving changes nothing."


def _refused_offer(code: str, folder_parts: tuple[str, ...]) -> Offer:
    return Offer((), refused_preview(REFUSAL_TEXTS[code], folder_parts), code)


def _list_names(scandir: Callable, path: str) -> list[str] | None:
    """Return the names in a folder, or None when it holds more than the limit.

    The listing is path-based and happens before any handle on a candidate is
    held. It stops after detecting entry 1,001, so it never builds a larger list.
    """
    names: list[str] = []
    with scandir(path) as entries:
        for entry in entries:
            if len(names) >= MAXIMUM_ENTRIES:
                return None
            names.append(entry.name)
    return names


def plan_offer(
    access: OrganizeAccess,
    root_path: str,
    folder_parts: tuple[str, ...],
    protected: Callable[[str], bool] | None = None,
    scandir: Callable = os.scandir,
) -> Offer:
    """Work out the moves to offer, holding nothing once it returns.

    The folder's names come from a path-based listing and are not trusted:
    every name is re-opened relative to the held folder and re-checked. At most
    five eligible files are selected, in order of their case-folded names. A
    failure to release a handle, or an unexpected Windows failure, raises so the
    approval carries no pins.
    """
    if native.pending_latched():
        return _refused_offer("STOPPED_AFTER_PENDING", folder_parts)
    chain = HandleSet(access)
    offer: Offer | None = None
    try:
        try:
            folder, folder_final = open_chain(access, chain, root_path, folder_parts, protected)
            volume = access.file_info(folder).volume
        except Refusal as exc:
            offer = _refused_offer(exc.code, folder_parts)
        except StoppedError:
            offer = _refused_offer("STOPPED_AFTER_PENDING", folder_parts)
        except PendingError:
            native.latch_pending()
            offer = _refused_offer("STOPPED_AFTER_PENDING", folder_parts)
        if offer is None:
            names = _list_names(scandir, ntpath.join(root_path, *folder_parts))
            if names is None:
                offer = _refused_offer("TOO_LARGE", folder_parts)
            else:
                try:
                    offer = _select(
                        access, root_path, folder_parts, folder, folder_final, volume,
                        names, protected,
                    )
                except PendingError:
                    native.latch_pending()
                    offer = _refused_offer("STOPPED_AFTER_PENDING", folder_parts)
    finally:
        failures = chain.release_all()
    if failures:
        raise RuntimeError("A handle could not be released while preparing the offer.")
    return offer


def _select(
    access: OrganizeAccess,
    root_path: str,
    folder_parts: tuple[str, ...],
    folder: int,
    folder_final: str,
    volume: int,
    names: list[str],
    protected: Callable[[str], bool] | None,
) -> Offer:
    selected: list[dict] = []
    keys: set[str] = set()
    remaining = 0
    failures = 0
    for name in sorted(names, key=lambda text: (text.casefold(), text)):
        pin = _eligible(
            access, root_path, folder_parts, folder, folder_final, volume, name, protected
        )
        if pin is None:
            continue
        if isinstance(pin, int):
            failures += pin
            continue
        pin_keys = (pin["source"].casefold(), pin["destination"].casefold())
        if pin_keys[0] in keys or pin_keys[1] in keys:
            continue
        if len(selected) < MAXIMUM_OPERATIONS:
            keys.update(pin_keys)
            selected.append(pin)
        else:
            remaining += 1
    if failures:
        raise RuntimeError("A handle could not be released while preparing the offer.")
    if not selected:
        text = NOTHING_ELIGIBLE_TEXT.format(folder=_display_folder(folder_parts))
        return Offer((), text, "NOTHING_ELIGIBLE")
    return Offer(tuple(selected), _preview(folder_parts, selected, remaining), None, len(selected), remaining)


def _eligible(
    access: OrganizeAccess,
    root_path: str,
    folder_parts: tuple[str, ...],
    folder: int,
    folder_final: str,
    volume: int,
    name: str,
    protected: Callable[[str], bool] | None,
) -> dict | int | None:
    """Return the pin of an eligible name, None when it is not eligible, or a
    count of failed releases.

    A pending request is never an answer: it propagates, so the offer latches
    and stops. Every handle is released whatever happened, and a release
    failure is reported in place of the pin.
    """
    handles = HandleSet(access)
    try:
        pin = _pin_of(
            access, handles, root_path, folder_parts, folder, folder_final,
            volume, name, protected,
        )
    finally:
        failures = handles.release_all()
    return failures if failures else pin


def _pin_of(
    access: OrganizeAccess,
    handles: HandleSet,
    root_path: str,
    folder_parts: tuple[str, ...],
    folder: int,
    folder_final: str,
    volume: int,
    name: str,
    protected: Callable[[str], bool] | None,
) -> dict | None:
    """Check one listed name through its own handles; return its pin or None."""
    try:
        check_component(name)
        category = category_for(name)
        if category is None:
            return None
        source = _relative((*folder_parts, name))
        destination = _relative((*folder_parts, category, name))
        create_path = _relative((*folder_parts, category))
        if _protected_paths(root_path, (source, destination), protected):
            return None
        found = lookup(access, handles, folder, name)
        info = found.info
        if (
            found.kind != "object"
            or info is None
            or info.attributes & ATTRIBUTE_DIRECTORY
            or info.attributes & INELIGIBLE_ATTRIBUTES
            or info.links != 1
            or info.index == 0
            or info.volume != volume
        ):
            return None
        category_absent = False
        try:
            category_handle = _open_directory(access, handles, folder, category)
        except PendingError:
            raise
        except AccessError as exc:
            if not (exc.kind == "nt" and exc.code == native.STATUS_OBJECT_NAME_NOT_FOUND):
                return None
            category_absent = True
            category_handle = 0
        if not category_absent:
            if not _is_plain_directory(access.file_info(category_handle)):
                return None
            _verify_category(access, category_handle, folder_final, category, protected)
            if lookup(access, handles, category_handle, name).kind != "absent":
                return None
        return {
            "source": source,
            "destination": destination,
            "create": create_path if category_absent else None,
            "identity": digest_of(info),
        }
    except PendingError:
        raise
    except (Refusal, InvalidName, OptionsError, StoppedError, AccessError):
        return None


def _preview(folder_parts: tuple[str, ...], pins: list[dict], remaining: int) -> str:
    lines = [
        f"Organize folder {_display_folder(folder_parts)}: {len(pins)} planned moves "
        f"(at most {MAXIMUM_OPERATIONS} per approval); {remaining} more eligible "
        "files stay for a later request."
    ]
    for index, pin in enumerate(pins, start=1):
        line = f"{index}. {quoted_for_owner(pin['source'])} -> {quoted_for_owner(pin['destination'])}"
        if pin["create"] is not None:
            line += f" (creates folder {quoted_for_owner(pin['create'])} if it is still missing)"
        lines.append(line)
    return "\n".join(lines)


# ---- the report ---------------------------------------------------------------------------
def native_reason(code: int) -> str:
    """Return Maria's fixed wording of a native code, or the unspecified reason."""
    return _NATIVE_REASONS.get(code, UNSPECIFIED_REASON)


def refusal_text(code: str) -> str:
    """Return the fixed text of a refusal code, or the unspecified reason."""
    return REFUSAL_TEXTS.get(code, UNSPECIFIED_REASON)


def refused_report(code: str) -> str:
    """Return the owner-facing answer to a run refused before any change."""
    text = refusal_text(code)
    return text if "nothing was changed" in text.lower() else f"Nothing was changed. {text}"


def render_report(result: BatchResult, pins: list[dict]) -> str:
    """Render what a batch did, one block per operation and a closing line."""
    planned = len(result.outcomes)
    if result.refused_before_any_change:
        code = next((o.reason for o in result.outcomes if o.reason), "ROOT_UNCONFIRMED")
        if result.preflight_index:
            text = (
                f"Nothing was changed: move {result.preflight_index} cannot run. "
                f"{refusal_text(code)}"
            )
        elif result.stopped_reason == "PREFLIGHT_RELEASE_FAILED":
            text = "Nothing was changed."
        else:
            text = refused_report(code)
        if result.preflight_cleanup_failures:
            text += "\n" + RELEASE_TEXT.format(where="while checking the moves")
        if result.terminal_cleanup_failures:
            text += "\n" + RELEASE_TEXT.format(where="at the end")
        return text
    lines: list[str] = []
    for index, outcome in enumerate(result.outcomes, start=1):
        pin = pins[index - 1] if index <= len(pins) else {"source": "", "destination": ""}
        source, destination = quoted_for_owner(pin["source"]), quoted_for_owner(pin["destination"])
        folder_text = _folder_line(outcome, pin)
        if folder_text:
            lines.append(folder_text)
        lines.append(_operation_line(index, outcome, source, destination))
        if outcome.extra_name:
            lines.append(EXTRA_NAME_TEXT.format(source=source))
        if outcome.cleanup_failures:
            lines.append(RELEASE_TEXT.format(where=f"after move {index}"))
    if result.terminal_cleanup_failures:
        lines.append(RELEASE_TEXT.format(where="at the end"))
    lines.append(_closing_line(result, planned))
    return "\n".join(lines)


def _folder_line(outcome: OperationOutcome, pin: dict) -> str:
    folder = quoted_for_owner(pin["create"]) if pin.get("create") else ""
    if outcome.folder == "created":
        return f"Created folder {folder}."
    if outcome.folder == "unconfirmed":
        return (
            f"Windows reported creating folder {folder}, but Maria could not confirm "
            "it is a plain folder inside the workspace."
        )
    if outcome.folder == "not_created":
        return f"Could not create folder {folder}: {native_reason(outcome.code)}"
    if outcome.folder == "unknown":
        return f"Maria tried to create folder {folder} and cannot confirm what happened."
    return ""


def _operation_line(index: int, outcome: OperationOutcome, source: str, destination: str) -> str:
    status = outcome.status
    if status == "moved":
        return f"{index}. Moved {source} to {destination}."
    if status == "reported_unconfirmed":
        return (
            f"{index}. Windows reported moving {source} to {destination}, but Maria "
            "could not complete her checks afterwards and cannot confirm where the "
            "file is now."
        )
    if status == "not_moved":
        return (
            f"{index}. Not moved: {source}. Windows refused the move: "
            f"{native_reason(outcome.code)}"
        )
    if status == "unknown":
        return (
            f"{index}. Maria tried to move {source} to {destination} and cannot "
            "confirm what happened. Check both places."
        )
    if status == "refused":
        return (
            f"{index}. Not moved: {source}. {refusal_text(outcome.reason)} "
            "Maria did not try to move it."
        )
    return f"{index}. Not attempted: {source}."


def _closing_line(result: BatchResult, planned: int) -> str:
    counts = result.counts
    warnings = result.warnings
    if result.success:
        return CLOSING_CLEAN.format(planned=planned)
    if counts["moved"] == planned and not result.pending and not result.interrupted:
        return CLOSING_WITH_WARNINGS.format(planned=planned, warnings=warnings)
    return CLOSING_STOPPED.format(
        moved=counts["moved"],
        unconfirmed=counts["reported_unconfirmed"],
        not_moved=counts["not_moved"],
        unknown=counts["unknown"],
        refused=counts["refused"],
        not_attempted=counts["not_attempted"],
        created=result.folders_created,
        folder_unconfirmed=result.folders_unconfirmed,
        warnings=warnings,
    )


def recorded_response(result: BatchResult) -> str:
    """Return the count-only text recorded as the response: no name, no path."""
    counts = result.counts
    if result.refused_before_any_change:
        return "Organize folder: refused before any change."
    return (
        f"Organize folder: {len(result.outcomes)} planned; {counts['moved']} moved, "
        f"{counts['reported_unconfirmed']} reported but unconfirmed, "
        f"{counts['not_moved']} not moved after an attempt, {counts['unknown']} "
        f"outcome unknown, {counts['refused']} refused before an attempt, "
        f"{counts['not_attempted']} not attempted; folders: {result.folders_created} "
        f"created, {result.folders_unconfirmed} unconfirmed; warnings: {result.warnings}."
    )


def count_line(result: BatchResult) -> str:
    """Return the fixed count-only line logged when a run ends or is interrupted."""
    counts = result.counts
    state = "interrupted" if result.interrupted else "finished"
    return (
        f"Organize run {state}: moved=%d unconfirmed=%d not_moved=%d unknown=%d "
        "refused=%d not_attempted=%d folders_created=%d folders_unconfirmed=%d "
        "warnings=%d"
    ) % (
        counts["moved"], counts["reported_unconfirmed"], counts["not_moved"],
        counts["unknown"], counts["refused"], counts["not_attempted"],
        result.folders_created, result.folders_unconfirmed, result.warnings,
    )
