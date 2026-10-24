"""Organization access: the Windows primitives file organization uses.

This is the only module that calls the seven Windows functions file
organization needs (V1-25-R1), through the standard library's ``ctypes``, with
every argument and result type pinned: from kernel32, CreateFileW,
GetFileInformationByHandle, GetFinalPathNameByHandleW,
GetVolumeInformationByHandleW, and CloseHandle; from ntdll, NtCreateFile and
NtSetInformationFile.

It decides nothing and holds no policy. Each method makes one call and returns
what Windows returned, or raises ``AccessError`` carrying the call's fixed name
and a number, never a path or a name. ``core/file_organization.py`` owns every
handle a method returns, makes every check, and releases everything. Nothing
here logs, reads a clock, starts a thread, waits, polls, cancels a request, or
runs another program. The functions are looked up once, when ``load_access`` is
called; off Windows, on a 32-bit process, or when one is missing, there is no
access at all. Tests replace ``_library`` and ``_last_error`` with fakes.

A rename is always ``FileRenameInformation`` with ReplaceIfExists false, and only
a returned STATUS_SUCCESS completes one. STATUS_PENDING is its own error:
every object the pending request owns is kept for the life of the process,
because the request may still write into it, and a process-wide latch then
refuses every later creation and rename until Maria restarts.
"""

from __future__ import annotations

import ctypes
import struct
import sys
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass

# Access rights.
ACCESS_DELETE = 0x00010000
SYNCHRONIZE = 0x00100000
FILE_TRAVERSE = 0x00000020
FILE_READ_ATTRIBUTES = 0x00000080

# Share modes.
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
FILE_SHARE_DELETE = 4
SHARE_READ_WRITE = FILE_SHARE_READ | FILE_SHARE_WRITE
SHARE_ALL = SHARE_READ_WRITE | FILE_SHARE_DELETE

# CreateFileW: the root is opened for attributes only, without following a
# reparse point.
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
ROOT_OPEN_FLAGS = FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT

# NtCreateFile dispositions and options.
FILE_OPEN = 1
FILE_CREATE = 2
FILE_OPEN_IF = 3
FILE_DIRECTORY_FILE = 0x00000001
FILE_WRITE_THROUGH = 0x00000002
FILE_SYNCHRONOUS_IO_ALERT = 0x00000010
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_OPEN_BY_FILE_ID = 0x00002000
FILE_OPEN_FOR_BACKUP_INTENT = 0x00004000
FILE_OPEN_REPARSE_POINT = 0x00200000
OBJ_CASE_INSENSITIVE = 0x00000040
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_CREATED = 2

# The options the NtCreateFile documentation lists as compatible with
# FILE_DIRECTORY_FILE. A set holding anything else is refused before the call.
# That is a policy taken from the documentation, not an observed refusal.
DIRECTORY_FILE_COMPATIBLE_OPTIONS = (
    FILE_DIRECTORY_FILE
    | FILE_SYNCHRONOUS_IO_ALERT
    | FILE_SYNCHRONOUS_IO_NONALERT
    | FILE_WRITE_THROUGH
    | FILE_OPEN_FOR_BACKUP_INTENT
    | FILE_OPEN_BY_FILE_ID
)

# The exact sets, separately for each purpose. An existing directory is opened
# without FILE_DIRECTORY_FILE: the options compatible with it exclude
# FILE_OPEN_REPARSE_POINT, and a junction must be seen, not followed.
ROOT_ACCESS = FILE_TRAVERSE | FILE_READ_ATTRIBUTES | SYNCHRONIZE
OPEN_DIRECTORY_ACCESS = FILE_TRAVERSE | FILE_READ_ATTRIBUTES | SYNCHRONIZE
OPEN_DIRECTORY_SHARE = SHARE_READ_WRITE
OPEN_DIRECTORY_DISPOSITION = FILE_OPEN
OPEN_DIRECTORY_OPTIONS = FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT
CREATE_DIRECTORY_ACCESS = FILE_TRAVERSE | FILE_READ_ATTRIBUTES | SYNCHRONIZE
CREATE_DIRECTORY_SHARE = SHARE_READ_WRITE
CREATE_DIRECTORY_DISPOSITION = FILE_CREATE
CREATE_DIRECTORY_OPTIONS = FILE_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT
LOOKUP_ACCESS = FILE_READ_ATTRIBUTES | SYNCHRONIZE
LOOKUP_SHARE = SHARE_ALL
LOOKUP_OPTIONS = FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT
SOURCE_ACCESS = ACCESS_DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE
SOURCE_SHARE = FILE_SHARE_READ
SOURCE_OPTIONS = (
    FILE_NON_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT
)

# NtSetInformationFile: the native FileRenameInformation class.
FILE_RENAME_INFORMATION = 10

# GetFinalPathNameByHandleW: a normalized name with a drive letter, into a
# buffer of this many characters. The file system name buffer is MAX_PATH + 1.
VOLUME_NAME_DOS = 0x0
FINAL_PATH_BUFFER_CHARACTERS = 32_768
FILE_SYSTEM_NAME_CHARACTERS = 261

# NTSTATUS values Maria tells apart. Every other value is only a number.
STATUS_SUCCESS = 0x00000000
STATUS_PENDING = 0x00000103
STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
STATUS_OBJECT_NAME_COLLISION = 0xC0000035
STATUS_SHARING_VIOLATION = 0xC0000043

# A name is at most 255 UTF-16 code units, which is 510 bytes.
MAXIMUM_NAME_BYTES = 510

# The characters that would make a relative name more than one component or a
# stream name, which no caller may pass.
_NAME_REFUSED = frozenset("\\/:\x00")

# The fixed name of each call, which is all a failure says about itself.
CREATE_FILE = "CreateFileW"
FILE_INFORMATION = "GetFileInformationByHandle"
FINAL_PATH = "GetFinalPathNameByHandleW"
VOLUME_INFORMATION = "GetVolumeInformationByHandleW"
CLOSE_HANDLE = "CloseHandle"
NT_CREATE_FILE = "NtCreateFile"
NT_SET_INFORMATION_FILE = "NtSetInformationFile"

_KERNEL32 = "kernel32"
_NTDLL = "ntdll"

# INVALID_HANDLE_VALUE, as a pointer-sized unsigned value.
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_DWORD = ctypes.c_uint32

# FILE_RENAME_INFORMATION, built as bytes: ReplaceIfExists as one byte at 0 (the
# rest of the 4-byte union zero), 4 bytes of padding, the 8-byte RootDirectory
# at 8, FileNameLength at 16 (UTF-16LE bytes, no terminator), then the name at
# 20. The fixed part is 20 bytes and sizeof(FILE_RENAME_INFORMATION) is 24.
_RENAME_HEADER = struct.Struct("<I4xQI")
_RENAME_SIZE = 24

# Objects kept for the life of the process after a pending request, and the
# latch that stops every later creation and rename. Per-instance ownership is
# not enough: the instance may be collected while the request is still open.
_PENDING_KEEPALIVE: list[tuple[object, ...]] = []
_PENDING_LATCH = False


class AccessError(Exception):
    """One Windows call failed: its fixed name and a number, nothing else."""

    def __init__(self, call: str, code: int, kind: str) -> None:
        super().__init__(call, code, kind)
        self.call = call
        self.code = code & 0xFFFFFFFF
        self.kind = kind  # "win32" for a Windows error, "nt" for an NTSTATUS


class PendingError(AccessError):
    """A call returned STATUS_PENDING: its completion is not confirmed."""


class InvalidName(ValueError):
    """A name that cannot be one simple relative component; carries nothing."""


class StoppedError(Exception):
    """Creation and renaming are stopped until restart after a pending request."""


class OptionsError(ValueError):
    """An option set the documentation does not list for FILE_DIRECTORY_FILE."""


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.USHORT),
        ("maximum_length", wintypes.USHORT),
        ("buffer", ctypes.c_void_p),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.ULONG),
        ("root_directory", ctypes.c_void_p),
        ("object_name", ctypes.POINTER(_UnicodeString)),
        ("attributes", wintypes.ULONG),
        ("security_descriptor", ctypes.c_void_p),
        ("security_quality_of_service", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]


class _FileTime(ctypes.Structure):
    _fields_ = [("low", _DWORD), ("high", _DWORD)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", _DWORD),
        ("creation", _FileTime),
        ("last_access", _FileTime),
        ("last_write", _FileTime),
        ("volume_serial", _DWORD),
        ("size_high", _DWORD),
        ("size_low", _DWORD),
        ("links", _DWORD),
        ("index_high", _DWORD),
        ("index_low", _DWORD),
    ]


@dataclass(frozen=True)
class FileInfo:
    """What GetFileInformationByHandle says about an open file or directory."""

    attributes: int
    volume: int
    index: int
    links: int
    size: int
    last_write: int


@dataclass(frozen=True)
class SetResult:
    """What one NtSetInformationFile call returned, captured exactly."""

    returned: int
    block_status: int
    information: int
    length: int


def pending_latched() -> bool:
    """Return whether a pending request has stopped creation and renaming."""
    return _PENDING_LATCH


def latch_pending() -> None:
    """Stop every later creation and rename until Maria restarts."""
    global _PENDING_LATCH
    _PENDING_LATCH = True


def _retain_pending(*objects: object) -> None:
    """Keep a pending request's objects alive for the life of the process."""
    latch_pending()
    _PENDING_KEEPALIVE.append(objects)


def utf16_bytes(name: str) -> bytes:
    """Return a name as UTF-16LE bytes; every length field comes from them.

    A character outside the BMP is two UTF-16 units, so its byte length is
    four, not the two that twice the character count would give. A name that
    is not text, is empty, cannot be encoded, is longer than 255 units, or
    holds a separator, a colon, or a NUL is refused.
    """
    if type(name) is not str or not name or _NAME_REFUSED & set(name):
        raise InvalidName()
    try:
        raw = name.encode("utf-16-le")
    except UnicodeEncodeError as exc:
        raise InvalidName() from exc
    if len(raw) > MAXIMUM_NAME_BYTES:
        raise InvalidName()
    return raw


class OrganizeAccess:
    """The pinned Windows functions, one call each."""

    def __init__(
        self,
        kernel32: object,
        ntdll: object,
        last_error: Callable[[], int],
    ) -> None:
        """Look every function up and pin its argument and result types.

        A missing function raises ``AttributeError``, so no access exists
        unless all seven do.
        """
        self._last_error = last_error
        self._create = _pinned(
            kernel32.CreateFileW,
            [
                ctypes.c_wchar_p,
                _DWORD,
                _DWORD,
                ctypes.c_void_p,
                _DWORD,
                _DWORD,
                ctypes.c_void_p,
            ],
            ctypes.c_void_p,
        )
        self._information = _pinned(
            kernel32.GetFileInformationByHandle,
            [ctypes.c_void_p, ctypes.POINTER(_ByHandleFileInformation)],
            ctypes.c_int,
        )
        self._final_path = _pinned(
            kernel32.GetFinalPathNameByHandleW,
            [ctypes.c_void_p, ctypes.c_wchar_p, _DWORD, _DWORD],
            _DWORD,
        )
        # BOOL GetVolumeInformationByHandleW(HANDLE, LPWSTR volume name, DWORD,
        # LPDWORD serial, LPDWORD maximum component, LPDWORD flags,
        # LPWSTR file system name, DWORD): only the file system name is asked
        # for; every other output is NULL, which the documentation allows.
        self._volume = _pinned(
            kernel32.GetVolumeInformationByHandleW,
            [
                ctypes.c_void_p,
                ctypes.c_void_p,
                _DWORD,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                _DWORD,
            ],
            ctypes.c_int,
        )
        self._close_handle = _pinned(
            kernel32.CloseHandle,
            [ctypes.c_void_p],
            ctypes.c_int,
        )
        self._nt_create = _pinned(
            ntdll.NtCreateFile,
            [
                ctypes.POINTER(ctypes.c_void_p),
                wintypes.ULONG,
                ctypes.POINTER(_ObjectAttributes),
                ctypes.POINTER(_IoStatusBlock),
                ctypes.c_void_p,
                wintypes.ULONG,
                wintypes.ULONG,
                wintypes.ULONG,
                wintypes.ULONG,
                ctypes.c_void_p,
                wintypes.ULONG,
            ],
            ctypes.c_long,
        )
        # NTSTATUS NtSetInformationFile(HANDLE, PIO_STATUS_BLOCK, PVOID,
        # ULONG Length, FILE_INFORMATION_CLASS).
        self._nt_set_information = _pinned(
            ntdll.NtSetInformationFile,
            [
                ctypes.c_void_p,
                ctypes.POINTER(_IoStatusBlock),
                ctypes.c_void_p,
                wintypes.ULONG,
                wintypes.ULONG,
            ],
            ctypes.c_long,
        )

    def __repr__(self) -> str:
        """Name the type and nothing else."""
        return "OrganizeAccess()"

    def create_file(self, path: str, access: int, share: int, flags: int) -> int:
        """Open one existing path with no security attributes; return its handle.

        No security attributes means the handle cannot be inherited by a
        child process.
        """
        handle = self._create(path, access, share, None, OPEN_EXISTING, flags, None)
        if handle is None or handle == INVALID_HANDLE_VALUE:
            raise AccessError(CREATE_FILE, self._last_error(), "win32")
        return int(handle)

    def open_relative(
        self,
        directory: int,
        name: str,
        access: int,
        share: int,
        disposition: int,
        options: int,
    ) -> tuple[int, int]:
        """Open or create one name relative to a held directory handle.

        Returns the new handle and the call's Information value (FILE_CREATED
        for a creation). An option set that holds anything the documentation
        does not list as compatible with FILE_DIRECTORY_FILE is refused before
        the call, and so is a creation after a pending request.
        """
        if disposition == FILE_CREATE and _PENDING_LATCH:
            raise StoppedError()
        if options & FILE_DIRECTORY_FILE and (
            options & ~DIRECTORY_FILE_COMPATIBLE_OPTIONS
            or disposition not in (FILE_CREATE, FILE_OPEN, FILE_OPEN_IF)
        ):
            raise OptionsError()
        raw = utf16_bytes(name)
        storage = ctypes.create_string_buffer(raw + b"\x00\x00")
        text = _UnicodeString(len(raw), len(raw) + 2, ctypes.addressof(storage))
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes),
            directory,
            ctypes.pointer(text),
            OBJ_CASE_INSENSITIVE,
            None,
            None,
        )
        handle = ctypes.c_void_p()
        block = _IoStatusBlock()
        status = self._nt_create(
            ctypes.byref(handle),
            access,
            ctypes.byref(attributes),
            ctypes.byref(block),
            None,
            FILE_ATTRIBUTE_NORMAL,
            share,
            disposition,
            options,
            None,
            0,
        )
        if status & 0xFFFFFFFF == STATUS_PENDING:
            # Everything the request owns, the output-handle storage included.
            _retain_pending(block, storage, attributes, text, handle)
        self._status(NT_CREATE_FILE, status)
        if not handle.value:
            raise AccessError(NT_CREATE_FILE, 0, "nt")
        return int(handle.value), int(block.information)

    def rename_into(self, handle: int, directory: int, name: str) -> SetResult:
        """Rename an open file into a held directory under a new simple name.

        The handle needs DELETE access. ReplaceIfExists is false, always. Only
        a returned STATUS_SUCCESS is success; nothing is renamed after a
        pending request.
        """
        if _PENDING_LATCH:
            raise StoppedError()
        raw = utf16_bytes(name)
        length = _RENAME_SIZE + len(raw)
        # The storage is allocated apart from the length fields: it always
        # holds the header, the name, and a terminator.
        storage = ctypes.create_string_buffer(
            _RENAME_HEADER.pack(0, directory, len(raw)) + raw + b"\x00\x00",
            length,
        )
        block = _IoStatusBlock()
        returned = (
            self._nt_set_information(
                handle,
                ctypes.byref(block),
                storage,
                length,
                FILE_RENAME_INFORMATION,
            )
            & 0xFFFFFFFF
        )
        if returned == STATUS_PENDING:
            _retain_pending(block, storage)
        self._status(NT_SET_INFORMATION_FILE, returned)
        return SetResult(
            returned,
            int(block.status or 0) & 0xFFFFFFFF,
            int(block.information),
            length,
        )

    def file_info(self, handle: int) -> FileInfo:
        """Return the attributes, volume, file index, links, size, and last write."""
        info = _ByHandleFileInformation()
        if not self._information(handle, ctypes.byref(info)):
            raise AccessError(FILE_INFORMATION, self._last_error(), "win32")
        return FileInfo(
            int(info.attributes),
            int(info.volume_serial),
            (int(info.index_high) << 32) | int(info.index_low),
            int(info.links),
            (int(info.size_high) << 32) | int(info.size_low),
            (int(info.last_write.high) << 32) | int(info.last_write.low),
        )

    def final_path(self, handle: int) -> str:
        """Return a handle's normalized final path, with a drive letter."""
        buffer = ctypes.create_unicode_buffer(FINAL_PATH_BUFFER_CHARACTERS)
        length = int(
            self._final_path(
                handle,
                buffer,
                FINAL_PATH_BUFFER_CHARACTERS,
                VOLUME_NAME_DOS,
            )
        )
        if length == 0 or length >= FINAL_PATH_BUFFER_CHARACTERS:
            raise AccessError(FINAL_PATH, self._last_error(), "win32")
        return buffer.value[:length]

    def filesystem_name(self, handle: int) -> str:
        """Return the name of the file system holding a held handle."""
        buffer = ctypes.create_unicode_buffer(FILE_SYSTEM_NAME_CHARACTERS)
        if not self._volume(
            handle,
            None,
            0,
            None,
            None,
            None,
            buffer,
            FILE_SYSTEM_NAME_CHARACTERS,
        ):
            raise AccessError(VOLUME_INFORMATION, self._last_error(), "win32")
        return buffer.value

    def close_handle(self, handle: int) -> None:
        """Close one handle."""
        if not self._close_handle(handle):
            raise AccessError(CLOSE_HANDLE, self._last_error(), "win32")

    @staticmethod
    def _status(call: str, status: int) -> None:
        """Raise unless an NTSTATUS is STATUS_SUCCESS; pending is its own error."""
        status &= 0xFFFFFFFF
        if status == STATUS_SUCCESS:
            return
        if status == STATUS_PENDING:
            raise PendingError(call, status, "nt")
        raise AccessError(call, status, "nt")


def load_access() -> OrganizeAccess | None:
    """Return the pinned functions, or None where organization is unavailable.

    That is anywhere but 64-bit Windows, and any Windows without all seven
    functions.
    """
    if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
        return None
    try:
        return OrganizeAccess(_library(_KERNEL32), _library(_NTDLL), _last_error)
    except (OSError, AttributeError):
        return None


def _pinned(function: object, argtypes: list[object], restype: object) -> object:
    """Pin one function's argument and result types, and return it."""
    function.argtypes = argtypes
    function.restype = restype
    return function


def _last_error() -> int:
    """Return the calling thread's last Windows error, as ctypes kept it."""
    return int(ctypes.get_last_error())


def _library(name: str) -> object:
    """Load one Windows library by name; tests replace this with fakes."""
    return ctypes.WinDLL(name, use_last_error=True)
