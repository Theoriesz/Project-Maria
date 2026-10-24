"""Project files: the checked, read-only walk to an active project's files.

A project file operation is one folder check for ``create project``, one
listing, or one file read (V1-24-R1, D13). Each runs in two phases.

The lexical phase runs first and makes no filesystem call: the path grammar
of ``core/project_records.py``; the candidate path built from the root's
canonical path, as configuration settled it at startup, the project folder,
and the typed components, with ``FilesystemProtections.excludes`` applied to
every prefix from the root to the candidate; and admission by name. A Project
Maria path is admitted only as a member of the approved path list in
``core/project_maria_manifest.py``, compared ignoring ASCII letter case, and
the member's spelling is used from then on. A workspace path may hold no
component beginning with "." and its file must have an admitted type.

The filesystem phase then holds every directory from the root down, checked
on its own handle, through ``core/project_file_access.py``: each component is
looked up by exact name without being opened, opened without following a
link, and checked on the handle (type, no reparse point, neither offline nor
recall on data access, no pending deletion, and its final path) before the
next is looked up. Recall on open is checked only in enumeration data, where
the bit has that meaning. A file must also have exactly one link and at most
``FILE_OPEN_MAX_BYTES`` bytes before any of its data is read, and after the
read its identity, size, last-write time, and every held final path must be
unchanged. Every handle and descriptor is entered in the operation's
ownership list as soon as its call returns and is released exactly once,
newest first, after success and after every failure; the thread's
placeholder mode is exposed for exactly the operation and its restore is
attempted exactly once. A detected restore failure withholds the result and
stops every later operation in the process until Maria restarts.

Content is released for decoding only when every check passed, every release
succeeded, and the restore succeeded. Decoding removes one UTF-8 byte-order
mark, refuses a NUL byte, an undecodable byte, and an empty file, and splits
lines after each line feed. Refusals are fixed and never tell an absent file
from a protected one. Logs name a fixed step code and a Windows error number
at most: never a name, path, or text.

This module also scans a model request's ``@`` references and builds the file
parts a model may receive. It never writes, renames, moves, or deletes
anything, runs no other program, and reads no clock.
"""

from __future__ import annotations

import hashlib
import ntpath
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from logging import Logger
from pathlib import Path

from core.filesystem_roots import FilesystemProtections, FilesystemRoots
from core.logger import get_logger
from core.project_file_access import (
    DIRECTORY_ACCESS,
    DIRECTORY_FLAGS,
    DIRECTORY_SHARE,
    ERROR_FILE_NOT_FOUND,
    ERROR_NO_MORE_FILES,
    ERROR_SHARING_VIOLATION,
    FILE_ACCESS,
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_OFFLINE,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    FILE_ATTRIBUTE_REPARSE_POINT,
    FILE_FLAGS,
    FILE_SHARE,
    FINAL_PATH_BUFFER_CHARACTERS,
    PHCM_EXPOSE_PLACEHOLDERS,
    AccessError,
    FindEntry,
    load_access,
)
from core.project_maria_manifest import APPROVED_PATHS
from core.project_records import (
    CHECKS_INCOMPLETE_MESSAGE,
    EMPTY_FILE_MESSAGE,
    FILE_CHANGED_MESSAGE,
    FILE_OPEN_MAX_BYTES,
    FILE_PART_MAX_BYTES,
    FILE_REFERENCE_LIMIT,
    FILE_SHOW_MAX_BYTES,
    FILE_TOO_LARGE_MESSAGE,
    LINE_RANGE_MESSAGE,
    LINE_RANGE_REASON,
    LISTING_SCAN_LIMIT,
    NOT_UTF8_MESSAGE,
    OPEN_FOR_CHANGES_MESSAGE,
    PROJECT_MARIA_ROOT,
    READING_STOPPED_MESSAGE,
    READING_STOPPED_REASON,
    READING_UNAVAILABLE_MESSAGE,
    READING_UNAVAILABLE_REASON,
    REFERENCE_CHECKS_REASON,
    REFERENCE_COUNT_REASON,
    REFERENCE_FORM_REASON,
    REFERENCE_PART_TOO_LARGE_REASON,
    REFERENCE_UNREADABLE_REASON,
    SHOW_TOO_LARGE_MESSAGE,
    UNREADABLE_FILE_MESSAGE,
    UNREADABLE_FOLDER_MESSAGE,
    WORKSPACE_ROOT,
    Project,
    ProjectFilePart,
    is_admitted_workspace_file,
    is_project_path,
    parse_line_range,
    path_components,
)
from core.task_records import UNSAFE_TEXT_CATEGORIES


# What a read may hold at most: one byte more than a file Maria opens, so a
# file that grew while it was read is recognised without reading further.
_READ_CEILING = FILE_OPEN_MAX_BYTES + 1

# The attributes no entry or handle Maria uses may show. Recall on open has
# its meaning only in enumeration data, so a handle is checked without it.
_ENTRY_REFUSED = (
    FILE_ATTRIBUTE_REPARSE_POINT
    | FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)
_HANDLE_REFUSED = (
    FILE_ATTRIBUTE_REPARSE_POINT
    | FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

_UTF8_BOM = b"\xef\xbb\xbf"
_LONG_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"
_ASCII_FOLD = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)
_SPECIAL_ENTRIES = frozenset({".", ".."})

# The fixed step codes a log line may name.
_STEP_MODE = "placeholder_mode"
_STEP_RESTORE = "placeholder_restore"
_STEP_DESCRIPTOR = "descriptor"
_STEP_BINARY = "binary_mode"
_STEP_INHERITANCE = "inheritance"
_STEP_READ = "read"
_STEP_RELEASE = "release"
_STEP_UNEXPECTED = "unexpected"


class FileRefusal(str, Enum):
    """Why a project file operation, decoding, or display showed nothing.

    ``UNREADABLE`` is the general refusal, which never tells an absent file
    from a protected one. ``INCOMPLETE`` covers a failed Windows call,
    conversion, mode or inheritance step, read, release, or restore.
    ``UNAVAILABLE`` means this computer cannot read project files at all, and
    ``STOPPED`` that a detected restore failure stopped reading until Maria
    restarts.
    """

    UNREADABLE = "unreadable"
    TOO_LARGE = "too_large"
    OPEN_FOR_CHANGES = "open_for_changes"
    CHANGED = "changed"
    INCOMPLETE = "incomplete"
    UNAVAILABLE = "unavailable"
    STOPPED = "stopped"
    NOT_UTF8 = "not_utf8"
    EMPTY = "empty"
    LINE_RANGE = "line_range"
    SHOW_TOO_LARGE = "show_too_large"
    PART_TOO_LARGE = "part_too_large"


# Each refusal's fixed owner-visible text, for a file and for a folder.
_FILE_MESSAGES = {
    FileRefusal.UNREADABLE: UNREADABLE_FILE_MESSAGE,
    FileRefusal.TOO_LARGE: FILE_TOO_LARGE_MESSAGE,
    FileRefusal.OPEN_FOR_CHANGES: OPEN_FOR_CHANGES_MESSAGE,
    FileRefusal.CHANGED: FILE_CHANGED_MESSAGE,
    FileRefusal.INCOMPLETE: CHECKS_INCOMPLETE_MESSAGE,
    FileRefusal.UNAVAILABLE: READING_UNAVAILABLE_MESSAGE,
    FileRefusal.STOPPED: READING_STOPPED_MESSAGE,
    FileRefusal.NOT_UTF8: NOT_UTF8_MESSAGE,
    FileRefusal.EMPTY: EMPTY_FILE_MESSAGE,
    FileRefusal.LINE_RANGE: LINE_RANGE_MESSAGE,
    FileRefusal.SHOW_TOO_LARGE: SHOW_TOO_LARGE_MESSAGE,
    FileRefusal.PART_TOO_LARGE: SHOW_TOO_LARGE_MESSAGE,
}

# Each refusal's reason on the model path. A file whose own checks refused
# it is not a readable file of the active project; a step that could not be
# completed is said to be so.
_MODEL_REASONS = {
    FileRefusal.UNREADABLE: REFERENCE_UNREADABLE_REASON,
    FileRefusal.TOO_LARGE: REFERENCE_UNREADABLE_REASON,
    FileRefusal.OPEN_FOR_CHANGES: REFERENCE_UNREADABLE_REASON,
    FileRefusal.CHANGED: REFERENCE_UNREADABLE_REASON,
    FileRefusal.NOT_UTF8: REFERENCE_UNREADABLE_REASON,
    FileRefusal.EMPTY: REFERENCE_UNREADABLE_REASON,
    FileRefusal.INCOMPLETE: REFERENCE_CHECKS_REASON,
    FileRefusal.UNAVAILABLE: READING_UNAVAILABLE_REASON,
    FileRefusal.STOPPED: READING_STOPPED_REASON,
    FileRefusal.LINE_RANGE: LINE_RANGE_REASON,
    FileRefusal.SHOW_TOO_LARGE: REFERENCE_PART_TOO_LARGE_REASON,
    FileRefusal.PART_TOO_LARGE: REFERENCE_PART_TOO_LARGE_REASON,
}


def refusal_message(refusal: FileRefusal, *, folder: bool = False) -> str:
    """Return a refusal's fixed owner-visible text."""
    if folder and refusal is FileRefusal.UNREADABLE:
        return UNREADABLE_FOLDER_MESSAGE
    return _FILE_MESSAGES[refusal]


def refusal_reason(refusal: FileRefusal) -> str:
    """Return a refusal's fixed reason for refusing a model request."""
    return _MODEL_REASONS[refusal]


class _RestoreLatch:
    """The process-wide record of a detected placeholder-mode restore failure.

    Once tripped it stays tripped until the process ends: Maria cannot say
    the earlier mode is back, so every later project file operation in the
    process is refused before any call.
    """

    def __init__(self) -> None:
        self.tripped = False


_LATCH = _RestoreLatch()


@dataclass(frozen=True, repr=False)
class ListedEntry:
    """One admitted entry of a listing, from its find data alone."""

    name: str
    directory: bool
    size: int

    def __repr__(self) -> str:
        """Name the type without showing the entry's name."""
        return "ListedEntry(<redacted>)"


@dataclass(frozen=True, repr=False)
class FolderListing:
    """One folder's admitted entries, sorted, and whether the scan stopped.

    ``folder`` is the listed folder's path relative to the project, in the
    approved list's or the on-disk spelling, and empty for its top folder.
    """

    folder: str
    entries: tuple[ListedEntry, ...]
    scan_stopped: bool = False

    def __repr__(self) -> str:
        """Name the type and how many entries it holds."""
        return f"FolderListing(count={len(self.entries)})"


@dataclass(frozen=True, repr=False)
class FileContent:
    """Exactly the bytes one checked read released, with their identity.

    ``path`` is the file's path relative to the project, in the approved
    list's or the on-disk spelling, ``size`` its size, and ``sha256`` the
    digest of exactly ``data``, which is the whole file.
    """

    path: str
    data: bytes
    size: int
    sha256: str

    def __repr__(self) -> str:
        """Name the type without showing the path or the bytes."""
        return "FileContent(<redacted>)"


@dataclass(frozen=True, repr=False)
class DecodedFile:
    """One file's decoded lines, each ending with its line feed if it had one."""

    path: str
    lines: tuple[str, ...]
    size: int
    sha256: str

    @property
    def line_count(self) -> int:
        """Return how many lines the file holds."""
        return len(self.lines)

    def text_of(self, start: int, end: int) -> str:
        """Return lines ``start`` to ``end``, exactly as decoded."""
        return "".join(self.lines[start - 1 : end])

    def __repr__(self) -> str:
        """Name the type without showing the path or the text."""
        return f"DecodedFile(lines={len(self.lines)})"


@dataclass(frozen=True, repr=False)
class FileReference:
    """One ``@`` reference: a project path and, optionally, a line range."""

    path: str
    lines: tuple[int, int] | None = None

    def __repr__(self) -> str:
        """Name the type without showing the path."""
        return "FileReference(<redacted>)"


@dataclass(frozen=True, repr=False)
class ReferenceScan:
    """What scanning one model request for ``@`` references found.

    ``present`` says some word begins with exactly one "@". ``references``
    are the distinct valid references in first-seen order, and ``reason`` is
    the fixed reason refusing the whole request instead, when one does.
    """

    present: bool
    references: tuple[FileReference, ...] = ()
    reason: str | None = None

    def __repr__(self) -> str:
        """Name the type and how many references it holds."""
        return f"ReferenceScan(count={len(self.references)})"


@dataclass(frozen=True)
class _Component:
    """One component of a walk: its expected name and what it must be."""

    name: str
    exact: bool
    directory: bool


class _Resource:
    """One handle or descriptor an operation owns, with its one release."""

    __slots__ = ("kind", "value", "state")

    def __init__(self, kind: str, value: int) -> None:
        self.kind = kind
        self.value = value
        self.state = _OWNED


_FIND = "find"
_HANDLE = "handle"
_DESCRIPTOR = "descriptor"
_OWNED = "owned"
_TRANSFERRING = "transferring"
_RELEASED = "released"


class _Refused(Exception):
    """Ends an operation's body with one refusal; carries nothing else."""

    def __init__(self, refusal: FileRefusal) -> None:
        super().__init__(refusal)
        self.refusal = refusal


@dataclass
class _Held:
    """A held, checked handle and the final path it must keep."""

    resource: _Resource
    final_path: str


class _Operation:
    """One project file operation: what it owns and how it ends.

    It runs on the calling thread from start to end. Every resource is
    entered as soon as its call returns and released at most once, newest
    first; the placeholder mode it exposes is restored at most once.
    """

    def __init__(self, access: object, logger: Logger) -> None:
        self.access = access
        self.logger = logger
        self.resources: list[_Resource] = []
        self.held: list[_Held] = []
        self.previous_mode: int | None = None

    def expose_placeholders(self) -> bool:
        """Set this thread's placeholder mode exposed; False if it failed.

        A failure, a negative return or a raised call, leaves nothing to
        restore.
        """
        try:
            previous = self.access.set_placeholder_mode(PHCM_EXPOSE_PLACEHOLDERS)
        except Exception:  # noqa: BLE001 - a failed step only withholds.
            previous = -1
        if type(previous) is not int or previous < 0:
            self.logger.warning("Project file step failed: %s.", _STEP_MODE)
            return False
        self.previous_mode = previous
        return True

    def own(self, kind: str, value: int) -> _Resource:
        """Enter one returned handle or descriptor in the ownership list."""
        resource = _Resource(kind, value)
        self.resources.append(resource)
        return resource

    def release(self, resource: _Resource) -> bool:
        """Release one owned resource, marked released first; False if it failed."""
        if resource.state != _OWNED:
            return True
        resource.state = _RELEASED
        try:
            if resource.kind == _FIND:
                self.access.find_close(resource.value)
            elif resource.kind == _HANDLE:
                self.access.close_handle(resource.value)
            else:
                self.access.close_descriptor(resource.value)
        except AccessError as failure:
            self.logger.warning(
                "Project file release failed: %s, Windows error %s.",
                failure.call,
                failure.error,
            )
            return False
        except Exception:  # noqa: BLE001 - a failed release only withholds.
            self.logger.warning("Project file step failed: %s.", _STEP_RELEASE)
            return False
        return True

    def release_all(self) -> bool:
        """Attempt every owned resource's release once, newest first.

        A resource still being transferred to a descriptor is left alone:
        nothing can say which of the two owns it.
        """
        released = True
        for resource in reversed(self.resources):
            if resource.state == _OWNED and not self.release(resource):
                released = False
        return released

    def restore(self) -> bool:
        """Attempt the placeholder mode's restore once; False if it failed.

        It succeeds only when the call returns the mode this operation set,
        which also shows the whole operation ran with placeholders exposed.
        Any other result trips the process-wide latch.
        """
        if self.previous_mode is None:
            return True
        previous, self.previous_mode = self.previous_mode, None
        try:
            result = self.access.set_placeholder_mode(previous)
        except Exception:  # noqa: BLE001 - a failed restore is detected below.
            result = None
        if result != PHCM_EXPOSE_PLACEHOLDERS:
            _LATCH.tripped = True
            self.logger.error("Project file step failed: %s.", _STEP_RESTORE)
            return False
        return True

    def open_directory(self, path: str) -> _Resource:
        """Open one directory for its attributes, as a link if it is one."""
        return self._open(path, DIRECTORY_ACCESS, DIRECTORY_SHARE, DIRECTORY_FLAGS)

    def open_file(self, path: str) -> _Resource:
        """Open one file for reading, shared for reading only."""
        return self._open(path, FILE_ACCESS, FILE_SHARE, FILE_FLAGS)

    def _open(self, path: str, access: int, share: int, flags: int) -> _Resource:
        """Open one path and own its handle, or refuse it.

        A file another program holds open for changes refuses with its own
        text; any other path that will not open gets the general refusal,
        which never tells an absent path from a protected one.
        """
        refusal: FileRefusal | None = None
        try:
            handle = self.access.create_file(path, access, share, flags)
        except AccessError as failure:
            refusal = (
                FileRefusal.OPEN_FOR_CHANGES
                if access == FILE_ACCESS and failure.error == ERROR_SHARING_VIOLATION
                else FileRefusal.UNREADABLE
            )
        if refusal is not None:
            raise _Refused(refusal)
        return self.own(_HANDLE, handle)

    def check(
        self,
        resource: _Resource,
        expected_final: str,
        *,
        directory: bool,
    ) -> None:
        """Check one held handle, or raise ``_Refused``.

        Its type, no reparse point, neither offline nor recall on data
        access, no pending deletion, and its final path; a file also has
        exactly one link and at most ``FILE_OPEN_MAX_BYTES`` bytes.
        """
        tag = self.access.attribute_tag(resource.value)
        standard = self.access.standard_info(resource.value)
        if (
            bool(tag.attributes & FILE_ATTRIBUTE_DIRECTORY) is not directory
            or standard.directory is not directory
            or tag.attributes & _HANDLE_REFUSED
            or tag.reparse_tag != 0
            or standard.delete_pending
        ):
            raise _Refused(FileRefusal.UNREADABLE)
        self.require_final_path(resource.value, expected_final)
        if not directory:
            if standard.links != 1:
                raise _Refused(FileRefusal.UNREADABLE)
            if not 0 <= standard.end_of_file <= FILE_OPEN_MAX_BYTES:
                raise _Refused(FileRefusal.TOO_LARGE)
        self.held.append(_Held(resource, expected_final))

    def require_final_path(
        self,
        handle: int,
        expected: str,
        refusal: FileRefusal = FileRefusal.UNREADABLE,
    ) -> None:
        """Refuse unless a handle's final path is exactly the expected one."""
        length, final = self.access.final_path(handle)
        if not 1 <= length < FINAL_PATH_BUFFER_CHARACTERS or not _same_path(
            final,
            expected,
        ):
            raise _Refused(refusal)

    def lookup(self, parent: str, component: _Component) -> str:
        """Look one component up by exact name, without opening it.

        Return its on-disk name. The search handle is released before
        anything else happens, and a name, attribute, or type that is not the
        expected one refuses the path.
        """
        try:
            search, entry = self.access.find_first(_child(parent, component.name))
        except AccessError:
            raise _Refused(FileRefusal.UNREADABLE) from None
        resource = self.own(_FIND, search)
        if not self.release(resource):
            raise _Refused(FileRefusal.INCOMPLETE)
        if not _is_expected_entry(entry, component):
            raise _Refused(FileRefusal.UNREADABLE)
        return entry.name


class ProjectFileReader:
    """Run project file operations against the settled roots and protections.

    ``roots`` and ``protections`` are the ones configuration settled at
    startup. The Windows functions are looked up on first use, through
    ``access_loader``; when there are none, every operation is refused before
    any filesystem call. One process-wide latch, shared by every reader,
    stops every operation once a restore failure was detected.
    """

    def __init__(
        self,
        roots: FilesystemRoots,
        protections: FilesystemProtections,
        logger: Logger | None = None,
        access_loader: Callable[[], object | None] = load_access,
    ) -> None:
        self._roots = roots
        self._protections = protections
        self._logger = logger or get_logger(__name__)
        self._access_loader = access_loader
        self._access: object | None = None
        self._loaded = False

    def __repr__(self) -> str:
        """Name the type and nothing configured."""
        return "ProjectFileReader(<redacted>)"

    def check_workspace_folder(self, folder: str) -> str | FileRefusal:
        """Check a workspace folder for ``create project``; return its spelling.

        The folder, empty for the workspace's top, must pass the lexical
        phase and then be held and checked component by component as a
        directory. Every handle the check opened is closed before this
        returns, and the on-disk spelling of the folder is returned.
        """
        unavailable = self._unavailable()
        if unavailable is not None:
            return unavailable
        if folder and not is_project_path(folder):
            return FileRefusal.UNREADABLE
        root = self._root_path(WORKSPACE_ROOT)
        components = tuple(
            _Component(name, False, True) for name in path_components(folder)
        )
        if root is None or self._excluded(root, components):
            return FileRefusal.UNREADABLE

        def body(operation: _Operation) -> str:
            _held, names = self._walk(operation, root, components)
            return "/".join(names)

        return self._run(body)

    def list_folder(self, project: Project, folder: str) -> FolderListing | FileRefusal:
        """List one folder of a project, empty for its top folder.

        Only entries that pass every check are listed, from their find data
        alone, sorted by name ignoring case; a Project Maria listing shows
        only members and the folders that hold them.
        """
        unavailable = self._unavailable()
        if unavailable is not None:
            return unavailable
        if folder and not is_project_path(folder):
            return FileRefusal.UNREADABLE
        maria = project.root == PROJECT_MARIA_ROOT
        if maria:
            spelled = _member_folder(folder)
            if spelled is None:
                return FileRefusal.UNREADABLE
            folder = spelled
        root = self._root_path(project.root)
        components = self._components(project, folder, directory_last=True)
        if root is None or self._excluded(root, components):
            return FileRefusal.UNREADABLE

        def body(operation: _Operation) -> FolderListing:
            held, names = self._walk(operation, root, components)
            shown = "/".join(names[len(path_components(project.folder)) :])
            location = root.joinpath(*names)
            entries, stopped = self._scan(
                operation,
                held,
                location,
                shown if maria else None,
            )
            return FolderListing(shown, entries, stopped)

        return self._run(body)

    def read_file(self, project: Project, path: str) -> FileContent | FileRefusal:
        """Read one admitted file of a project, whole, behind every check."""
        unavailable = self._unavailable()
        if unavailable is not None:
            return unavailable
        if not is_project_path(path):
            return FileRefusal.UNREADABLE
        if project.root == PROJECT_MARIA_ROOT:
            member = _member(path)
            if member is None:
                return FileRefusal.UNREADABLE
            path = member
        elif not is_admitted_workspace_file(path_components(path)[-1]):
            return FileRefusal.UNREADABLE
        root = self._root_path(project.root)
        components = self._components(project, path, directory_last=False)
        if root is None or self._excluded(root, components):
            return FileRefusal.UNREADABLE

        def body(operation: _Operation) -> FileContent:
            held, names = self._walk(operation, root, components)
            data = _read(operation, held)
            shown = "/".join(names[len(path_components(project.folder)) :])
            return FileContent(
                shown,
                data,
                len(data),
                hashlib.sha256(data).hexdigest(),
            )

        return self._run(body)

    def _access_now(self) -> object | None:
        """Look the Windows functions up once, on first use."""
        if not self._loaded:
            self._loaded = True
            try:
                self._access = self._access_loader()
            except Exception:  # noqa: BLE001 - no access means unavailable.
                self._access = None
        return self._access

    def _unavailable(self) -> FileRefusal | None:
        """Return why no operation can run at all, before anything else, or None.

        A detected restore failure stops every operation until Maria
        restarts, and a computer without the Windows functions can run none.
        """
        if _LATCH.tripped:
            return FileRefusal.STOPPED
        if self._access_now() is None:
            return FileRefusal.UNAVAILABLE
        return None

    def _run(self, body: Callable[[_Operation], object]) -> object:
        """Run one operation's filesystem phase and end it, whatever happens.

        The result is released only when the body passed every check, every
        release succeeded, and the restore succeeded. The releases and the
        restore run after success and after every failure.
        """
        unavailable = self._unavailable()
        if unavailable is not None:
            return unavailable
        operation = _Operation(self._access_now(), self._logger)
        outcome: object = FileRefusal.INCOMPLETE
        released = restored = False
        try:
            if operation.expose_placeholders():
                outcome = self._body(operation, body)
        finally:
            try:
                released = operation.release_all()
            finally:
                restored = operation.restore()
        if not restored or not released:
            return FileRefusal.INCOMPLETE
        return outcome

    def _body(
        self,
        operation: _Operation,
        body: Callable[[_Operation], object],
    ) -> object:
        """Run a body, turning a refusal or a failed step into its outcome."""
        try:
            return body(operation)
        except _Refused as refused:
            return refused.refusal
        except AccessError as failure:
            self._logger.warning(
                "Project file call failed: %s, Windows error %s.",
                failure.call,
                failure.error,
            )
        except Exception as failure:  # noqa: BLE001 - nothing is shown.
            self._logger.warning(
                "Project file step failed: %s (%s).",
                _STEP_UNEXPECTED,
                type(failure).__name__,
            )
        return FileRefusal.INCOMPLETE

    def _root_path(self, name: str) -> Path | None:
        """Return a root's canonical path, as configuration settled it."""
        root = self._roots.by_name(name)
        return None if root is None else root.path

    def _components(
        self,
        project: Project,
        typed: str,
        *,
        directory_last: bool,
    ) -> tuple[_Component, ...]:
        """Return the walk's components: the project folder's, then the typed.

        A Project Maria component must have exactly the member's spelling;
        a workspace component is matched ignoring case.
        """
        exact = project.root == PROJECT_MARIA_ROOT
        folder = tuple(
            _Component(name, False, True) for name in path_components(project.folder)
        )
        names = path_components(typed)
        typed_components = tuple(
            _Component(
                name,
                exact,
                directory_last or index < len(names) - 1,
            )
            for index, name in enumerate(names)
        )
        return folder + typed_components

    def _excluded(self, root: Path, components: tuple[_Component, ...]) -> bool:
        """Return whether any prefix from the root to the candidate is excluded.

        Pure path arithmetic: nothing is opened or resolved.
        """
        prefixes = [root]
        for component in components:
            prefixes.append(prefixes[-1] / component.name)
        return self._protections.excludes(*prefixes)

    def _walk(
        self,
        operation: _Operation,
        root: Path,
        components: tuple[_Component, ...],
    ) -> tuple[_Held, list[str]]:
        """Hold and check the root, then each component, in order.

        Return the last held handle and the on-disk names of every component.
        """
        final = _long_form(str(root))
        resource = operation.open_directory(final)
        operation.check(resource, final, directory=True)
        held = operation.held[-1]
        names: list[str] = []
        for component in components:
            name = operation.lookup(held.final_path, component)
            expected = _child(held.final_path, name)
            if component.directory:
                resource = operation.open_directory(expected)
            else:
                resource = operation.open_file(expected)
            operation.check(resource, expected, directory=component.directory)
            held = operation.held[-1]
            names.append(name)
        return held, names

    def _scan(
        self,
        operation: _Operation,
        held: _Held,
        location: Path,
        member_prefix: str | None,
    ) -> tuple[tuple[ListedEntry, ...], bool]:
        """Enumerate one held folder's entries, examining each from find data.

        An empty folder and the end of the directory are the scan's normal
        ends; any other failure withholds the whole listing. At most
        ``LISTING_SCAN_LIMIT`` entries are examined.
        """
        try:
            search, entry = operation.access.find_first(_child(held.final_path, "*"))
        except AccessError as failure:
            if failure.error == ERROR_FILE_NOT_FOUND:
                return (), False
            raise
        resource = operation.own(_FIND, search)
        admitted: list[ListedEntry] = []
        examined = 0
        stopped = False
        current: FindEntry | None = entry
        while current is not None:
            if current.name not in _SPECIAL_ENTRIES:
                if examined == LISTING_SCAN_LIMIT:
                    stopped = True
                    break
                examined += 1
                listed = self._examine(current, location, member_prefix)
                if listed is not None:
                    admitted.append(listed)
            try:
                current = operation.access.find_next(search)
            except AccessError as failure:
                if failure.error != ERROR_NO_MORE_FILES:
                    raise
                current = None
        if not operation.release(resource):
            raise _Refused(FileRefusal.INCOMPLETE)
        admitted.sort(key=lambda listed: (listed.name.casefold(), listed.name))
        return tuple(admitted), stopped

    def _examine(
        self,
        entry: FindEntry,
        location: Path,
        member_prefix: str | None,
    ) -> ListedEntry | None:
        """Return one enumerated entry when it passes every check, or None."""
        name = entry.name
        if not _is_entry_name(name) or entry.attributes & _ENTRY_REFUSED:
            return None
        directory = bool(entry.attributes & FILE_ATTRIBUTE_DIRECTORY)
        if self._protections.excludes(location / name):
            return None
        if member_prefix is not None:
            relative = f"{member_prefix}/{name}" if member_prefix else name
            members = _MEMBER_FOLDERS if directory else _MEMBERS
            if relative not in members:
                return None
        elif not directory and not is_admitted_workspace_file(name):
            return None
        return ListedEntry(name, directory, 0 if directory else entry.size)


def decode_file(content: FileContent) -> DecodedFile | FileRefusal:
    """Decode exactly the bytes a checked read released.

    One leading UTF-8 byte-order mark is removed; a NUL byte, any invalid
    UTF-8, and an empty result are refused. Lines are split after each line
    feed, so a carriage return before a line feed stays at its line's end and
    one alone does not end a line; a final line without a line feed counts.
    """
    data = content.data
    if data.startswith(_UTF8_BOM):
        data = data[len(_UTF8_BOM) :]
    if b"\x00" in data:
        return FileRefusal.NOT_UTF8
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return FileRefusal.NOT_UTF8
    if not text:
        return FileRefusal.EMPTY
    parts = text.split("\n")
    lines = [f"{part}\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return DecodedFile(content.path, tuple(lines), content.size, content.sha256)


def file_display(
    project_id: int,
    decoded: DecodedFile,
    lines: tuple[int, int] | None,
) -> str | FileRefusal:
    """Return a ``read project file`` display, or its refusal.

    The range must lie within the file, and its decoded text must be at most
    ``FILE_SHOW_MAX_BYTES`` UTF-8 bytes. A carriage return before a line feed
    is shown as a line feed, and every other control, format, surrogate, or
    line or paragraph separator character but a tab and a line feed is shown
    as a ``\\u`` escape, so no file text can pose as Maria's own words. The
    file's final line feed is not shown.
    """
    count = decoded.line_count
    start, end = lines if lines is not None else (1, count)
    if end > count:
        return FileRefusal.LINE_RANGE
    text = decoded.text_of(start, end)
    if len(text.encode("utf-8")) > FILE_SHOW_MAX_BYTES:
        return FileRefusal.SHOW_TOO_LARGE
    shown = text.replace("\r\n", "\n")
    if shown.endswith("\n"):
        shown = shown[:-1]
    header = (
        f"Project {project_id} file {decoded.path}, lines {start}-{end} of "
        f"{count}, {decoded.size} bytes, SHA-256 {decoded.sha256[:16]}:"
    )
    return f"{header}\n{''.join(_shown_character(c) for c in shown)}"


def scan_references(request: object) -> ReferenceScan:
    """Find a model request's ``@`` references, by the V1-24-R1 grammar.

    Each whitespace-separated word that begins with exactly one "@" is a
    reference: ``@<path>`` or ``@<path>:<from>-<to>``. A word beginning
    with "@@", and a word that is only "@", are ordinary text. A malformed
    reference refuses the whole request, as does a range of digits outside
    the line limits. References are de-duplicated by path, ignoring ASCII
    case, and range, in first-seen order, and at most
    ``FILE_REFERENCE_LIMIT`` may remain.
    """
    if type(request) is not str:
        return ReferenceScan(False)
    present = False
    found: list[FileReference] = []
    seen: set[tuple[str, tuple[int, int] | None]] = set()
    for word in request.split():
        if not word.startswith("@") or word.startswith("@@") or word == "@":
            continue
        present = True
        path, separator, range_text = word[1:].partition(":")
        lines = None
        if separator:
            if not _is_range_shape(range_text):
                return ReferenceScan(True, reason=REFERENCE_FORM_REASON)
            lines = parse_line_range(range_text)
            if lines is None:
                return ReferenceScan(True, reason=LINE_RANGE_REASON)
        if not is_project_path(path):
            return ReferenceScan(True, reason=REFERENCE_FORM_REASON)
        key = (_folded(path), lines)
        if key not in seen:
            seen.add(key)
            found.append(FileReference(path, lines))
    if len(found) > FILE_REFERENCE_LIMIT:
        return ReferenceScan(True, reason=REFERENCE_COUNT_REASON)
    return ReferenceScan(present, tuple(found))


def file_parts(
    reader: ProjectFileReader,
    project: Project,
    references: tuple[FileReference, ...],
) -> tuple[ProjectFilePart, ...] | str:
    """Read each distinct referenced file once and build the model's parts.

    References to the same file share one read and one SHA-256. A whole-file
    reference needs a file of at most ``FILE_PART_MAX_BYTES`` bytes of
    decoded text, and a range one lying within the file whose lines are
    within that bound. The first refusal, as its fixed reason, stops
    everything.
    """
    decoded: dict[str, DecodedFile] = {}
    for reference in references:
        key = _folded(reference.path)
        if key in decoded:
            continue
        content = reader.read_file(project, reference.path)
        if isinstance(content, FileRefusal):
            return refusal_reason(content)
        settled = decode_file(content)
        if isinstance(settled, FileRefusal):
            return refusal_reason(settled)
        decoded[key] = settled
    parts: list[ProjectFilePart] = []
    for reference in references:
        source = decoded[_folded(reference.path)]
        count = source.line_count
        start, end = reference.lines if reference.lines is not None else (1, count)
        if end > count:
            return refusal_reason(FileRefusal.LINE_RANGE)
        text = source.text_of(start, end)
        if len(text.encode("utf-8")) > FILE_PART_MAX_BYTES:
            return refusal_reason(FileRefusal.PART_TOO_LARGE)
        parts.append(
            ProjectFilePart(
                project.project_id,
                source.path,
                start,
                end,
                count,
                source.size,
                source.sha256,
                text,
            )
        )
    return tuple(parts)


def _read(operation: _Operation, held: _Held) -> bytes:
    """Read a held file whole, through a binary, non-inheritable descriptor.

    Its identity is taken first; after the read it, its size, its
    last-write time, and every held final path must be unchanged.
    """
    access = operation.access
    resource = held.resource
    before = access.file_identity(resource.value)
    resource.state = _TRANSFERRING
    try:
        descriptor = access.open_descriptor(resource.value)
    except OSError:
        # No descriptor was returned, so the handle is still Maria's.
        resource.state = _OWNED
        operation.logger.warning("Project file step failed: %s.", _STEP_DESCRIPTOR)
        raise _Refused(FileRefusal.INCOMPLETE) from None
    resource.kind = _DESCRIPTOR
    resource.value = descriptor
    resource.state = _OWNED
    _descriptor_step(operation, access.set_binary, descriptor, _STEP_BINARY)
    if _descriptor_step(operation, access.inheritable, descriptor, _STEP_INHERITANCE):
        operation.logger.warning("Project file step failed: %s.", _STEP_INHERITANCE)
        raise _Refused(FileRefusal.INCOMPLETE)
    chunks: list[bytes] = []
    total = 0
    while total < _READ_CEILING:
        chunk = _descriptor_step(
            operation,
            access.read,
            descriptor,
            _STEP_READ,
            _READ_CEILING - total,
        )
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    handle = _descriptor_step(
        operation,
        access.descriptor_handle,
        descriptor,
        _STEP_DESCRIPTOR,
    )
    if access.file_identity(handle) != before or total != before.size:
        raise _Refused(FileRefusal.CHANGED)
    for checked in operation.held:
        query = handle if checked.resource is resource else checked.resource.value
        operation.require_final_path(query, checked.final_path, FileRefusal.CHANGED)
    return b"".join(chunks)


def _descriptor_step(
    operation: _Operation,
    step: Callable[..., object],
    descriptor: int,
    code: str,
    *arguments: object,
) -> object:
    """Run one descriptor step; an OSError withholds the whole read."""
    try:
        return step(descriptor, *arguments)
    except OSError:
        operation.logger.warning("Project file step failed: %s.", code)
    raise _Refused(FileRefusal.INCOMPLETE)


def _is_expected_entry(entry: FindEntry, component: _Component) -> bool:
    """Return whether a looked-up entry is exactly the component expected.

    Its name must be the expected one, exactly for a member and ignoring
    case otherwise, so a match through a short name, which returns another
    long name, is refused. It must show no reparse point, offline, or recall
    attribute, and be a directory exactly when the component is one.
    """
    name = entry.name
    if component.exact:
        if name != component.name:
            return False
    elif not _same_path(name, component.name) or not _is_entry_name(name):
        return False
    if entry.attributes & _ENTRY_REFUSED:
        return False
    return bool(entry.attributes & FILE_ATTRIBUTE_DIRECTORY) is component.directory


def _is_entry_name(name: str) -> bool:
    """Return whether an on-disk name is one component the grammar allows."""
    return "/" not in name and is_project_path(name)


def _is_range_shape(value: str) -> bool:
    """Return whether text is ASCII digits, one hyphen, and ASCII digits."""
    first, separator, last = value.partition("-")
    return bool(
        separator
        and first
        and last
        and first.isascii()
        and last.isascii()
        and first.isdigit()
        and last.isdigit()
    )


def _shown_character(character: str) -> str:
    """Return one character as a display shows it."""
    if character in "\t\n":
        return character
    if unicodedata.category(character) not in UNSAFE_TEXT_CATEGORIES:
        return character
    code = ord(character)
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    code -= 0x10000
    return f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}"


def _long_form(path: str) -> str:
    """Return a canonical path in its ``\\\\?\\`` form, drive or network."""
    if path.startswith(_LONG_PREFIX):
        return path
    if path.startswith("\\\\"):
        return f"{_UNC_PREFIX}{path[2:]}"
    return f"{_LONG_PREFIX}{path}"


def _child(parent: str, name: str) -> str:
    """Join one name to a final path with exactly one separator."""
    if parent.endswith("\\"):
        return f"{parent}{name}"
    return f"{parent}\\{name}"


def _same_path(first: str, second: str) -> bool:
    """Compare two Windows paths or names as ``os.path.normcase`` does."""
    return ntpath.normcase(first) == ntpath.normcase(second)


def _folded(path: str) -> str:
    """Return a path with its ASCII letters lowered and nothing else changed."""
    return path.translate(_ASCII_FOLD)


def _member_folders() -> frozenset[str]:
    """Return every folder that holds a member, at any depth, as spelled."""
    folders: set[str] = set()
    for member in APPROVED_PATHS:
        parts = member.split("/")
        for depth in range(1, len(parts)):
            folders.add("/".join(parts[:depth]))
    return frozenset(folders)


_MEMBERS = frozenset(APPROVED_PATHS)
_MEMBERS_BY_FOLDED = {_folded(member): member for member in APPROVED_PATHS}
_MEMBER_FOLDERS = _member_folders()
_MEMBER_FOLDERS_BY_FOLDED = {_folded(folder): folder for folder in _MEMBER_FOLDERS}


def _member(path: str) -> str | None:
    """Return the member a typed path names, ignoring ASCII case, or None."""
    return _MEMBERS_BY_FOLDED.get(_folded(path))


def _member_folder(folder: str) -> str | None:
    """Return a member folder as spelled, the top folder as empty, or None."""
    if not folder:
        return ""
    return _MEMBER_FOLDERS_BY_FOLDED.get(_folded(folder))
