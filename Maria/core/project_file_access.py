"""Project file access: the Windows file primitives project reading uses.

This is the only module that calls the Windows functions project file
reading needs (V1-24-R1, D13), through the standard library's ``ctypes``,
with every argument and result type pinned: from kernel32, CreateFileW,
GetFileInformationByHandle, GetFileInformationByHandleEx (FileStandardInfo
and FileAttributeTagInfo), GetFinalPathNameByHandleW, FindFirstFileExW,
FindNextFileW, FindClose, and CloseHandle; from ntdll,
RtlSetThreadPlaceholderCompatibilityMode. It also holds the descriptor steps
of a read: ``msvcrt.open_osfhandle``, ``msvcrt.setmode``,
``os.get_inheritable``, ``os.read``, ``msvcrt.get_osfhandle``, and
``os.close``.

It decides nothing and owns nothing. Each method makes one call and returns
what Windows returned, or raises ``AccessError`` carrying the call's fixed
name and Windows's error number, never a path or a name.
``core/project_files.py`` owns every handle a method returns, makes every
check, and releases everything. Nothing here logs, reads a clock, starts a
thread, or runs another program. The functions are looked up once, when
``load_access`` is called; off Windows, or when one is missing, there is no
access at all. Tests replace ``_library`` and ``_last_error`` with fakes.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass

try:
    import msvcrt
except ImportError:  # Not Windows: project file reading is unavailable.
    msvcrt = None


# CreateFileW: access, sharing, disposition, and flags. A directory and the
# root are opened for attributes only, shared for reading and writing and
# never for deletion; a file for reading, shared for reading only.
FILE_READ_ATTRIBUTES = 0x00000080
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
DIRECTORY_ACCESS = FILE_READ_ATTRIBUTES
DIRECTORY_SHARE = FILE_SHARE_READ | FILE_SHARE_WRITE
DIRECTORY_FLAGS = FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT
FILE_ACCESS = GENERIC_READ
FILE_SHARE = FILE_SHARE_READ
FILE_FLAGS = FILE_FLAG_OPEN_REPARSE_POINT

# GetFileInformationByHandleEx information classes.
FILE_STANDARD_INFO = 1
FILE_ATTRIBUTE_TAG_INFO = 9

# GetFinalPathNameByHandleW: a normalized name with a drive letter, into a
# buffer of this many characters.
FILE_NAME_NORMALIZED = 0x0
VOLUME_NAME_DOS = 0x0
FINAL_PATH_BUFFER_CHARACTERS = 32_768

# FindFirstFileExW: the basic information level, a name match, and on-disk
# entries only.
FIND_EX_INFO_BASIC = 1
FIND_EX_SEARCH_NAME_MATCH = 0
FIND_FIRST_EX_ON_DISK_ENTRIES_ONLY = 0x4

# File attributes Maria's checks read.
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

# The placeholder compatibility mode an operation sets.
PHCM_EXPOSE_PLACEHOLDERS = 2

# Windows error numbers Maria tells apart. Every other number is only
# logged.
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_NO_MORE_FILES = 18
ERROR_SHARING_VIOLATION = 32

# The fixed name of each call, which is all a failure says about itself.
CREATE_FILE = "CreateFileW"
FILE_INFORMATION = "GetFileInformationByHandle"
FILE_INFORMATION_EX = "GetFileInformationByHandleEx"
FINAL_PATH = "GetFinalPathNameByHandleW"
FIND_FIRST = "FindFirstFileExW"
FIND_NEXT = "FindNextFileW"
FIND_CLOSE = "FindClose"
CLOSE_HANDLE = "CloseHandle"
PLACEHOLDER_MODE = "RtlSetThreadPlaceholderCompatibilityMode"

_KERNEL32 = "kernel32"
_NTDLL = "ntdll"

# INVALID_HANDLE_VALUE, as a pointer-sized unsigned value.
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_DWORD = ctypes.c_uint32


class AccessError(Exception):
    """One Windows call failed: its fixed name and error number, nothing else."""

    def __init__(self, call: str, error: int) -> None:
        super().__init__(call, error)
        self.call = call
        self.error = error


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


class _FileStandardInfo(ctypes.Structure):
    _fields_ = [
        ("allocation_size", ctypes.c_int64),
        ("end_of_file", ctypes.c_int64),
        ("links", _DWORD),
        ("delete_pending", ctypes.c_ubyte),
        ("directory", ctypes.c_ubyte),
    ]


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("attributes", _DWORD), ("reparse_tag", _DWORD)]


class _Win32FindDataW(ctypes.Structure):
    _fields_ = [
        ("attributes", _DWORD),
        ("creation", _FileTime),
        ("last_access", _FileTime),
        ("last_write", _FileTime),
        ("size_high", _DWORD),
        ("size_low", _DWORD),
        ("reserved0", _DWORD),
        ("reserved1", _DWORD),
        ("name", ctypes.c_wchar * 260),
        ("alternate_name", ctypes.c_wchar * 14),
    ]


@dataclass(frozen=True)
class AttributeTag:
    """A handle's attributes and reparse tag, from FileAttributeTagInfo."""

    attributes: int
    reparse_tag: int


@dataclass(frozen=True)
class StandardInfo:
    """A handle's size, link count, and flags, from FileStandardInfo."""

    end_of_file: int
    links: int
    delete_pending: bool
    directory: bool


@dataclass(frozen=True)
class FileIdentity:
    """What GetFileInformationByHandle says identifies an open file."""

    volume_serial: int
    file_index: int
    links: int
    size: int
    last_write: int


@dataclass(frozen=True, repr=False)
class FindEntry:
    """One directory entry, from its find data alone.

    ``name`` is the entry's long name, ``attributes`` its attributes,
    ``reparse_tag`` the tag find data carries for a reparse point, and
    ``size`` the size Windows lists. The ``repr`` never shows the name.
    """

    name: str
    attributes: int
    reparse_tag: int
    size: int

    def __repr__(self) -> str:
        """Name the type without showing the entry's name."""
        return "FindEntry(<redacted>)"


class WindowsFileAccess:
    """The pinned Windows functions and descriptor steps, one call each."""

    def __init__(
        self,
        kernel32: object,
        ntdll: object,
        last_error: Callable[[], int],
    ) -> None:
        """Look every function up and pin its argument and result types.

        A missing function raises ``AttributeError``, so no access exists
        unless all nine do.
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
        self._information_ex = _pinned(
            kernel32.GetFileInformationByHandleEx,
            [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, _DWORD],
            ctypes.c_int,
        )
        self._final_path = _pinned(
            kernel32.GetFinalPathNameByHandleW,
            [ctypes.c_void_p, ctypes.c_wchar_p, _DWORD, _DWORD],
            _DWORD,
        )
        self._find_first = _pinned(
            kernel32.FindFirstFileExW,
            [
                ctypes.c_wchar_p,
                ctypes.c_int,
                ctypes.POINTER(_Win32FindDataW),
                ctypes.c_int,
                ctypes.c_void_p,
                _DWORD,
            ],
            ctypes.c_void_p,
        )
        self._find_next = _pinned(
            kernel32.FindNextFileW,
            [ctypes.c_void_p, ctypes.POINTER(_Win32FindDataW)],
            ctypes.c_int,
        )
        self._find_close = _pinned(
            kernel32.FindClose,
            [ctypes.c_void_p],
            ctypes.c_int,
        )
        self._close_handle = _pinned(
            kernel32.CloseHandle,
            [ctypes.c_void_p],
            ctypes.c_int,
        )
        self._placeholder_mode = _pinned(
            ntdll.RtlSetThreadPlaceholderCompatibilityMode,
            [ctypes.c_byte],
            ctypes.c_byte,
        )

    def __repr__(self) -> str:
        """Name the type and nothing else."""
        return "WindowsFileAccess()"

    def set_placeholder_mode(self, mode: int) -> int:
        """Set this thread's placeholder mode; return the previous one.

        A negative value is Windows's own error return: the mode did not
        change.
        """
        return int(self._placeholder_mode(mode))

    def create_file(self, path: str, access: int, share: int, flags: int) -> int:
        """Open one path with no security attributes and return its handle.

        No security attributes means the handle cannot be inherited by a
        child process.
        """
        handle = self._create(path, access, share, None, OPEN_EXISTING, flags, None)
        if handle is None or handle == INVALID_HANDLE_VALUE:
            raise AccessError(CREATE_FILE, self._last_error())
        return int(handle)

    def attribute_tag(self, handle: int) -> AttributeTag:
        """Return a handle's attributes and reparse tag."""
        info = _FileAttributeTagInfo()
        if not self._information_ex(
            handle,
            FILE_ATTRIBUTE_TAG_INFO,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise AccessError(FILE_INFORMATION_EX, self._last_error())
        return AttributeTag(int(info.attributes), int(info.reparse_tag))

    def standard_info(self, handle: int) -> StandardInfo:
        """Return a handle's end of file, link count, and two flags."""
        info = _FileStandardInfo()
        if not self._information_ex(
            handle,
            FILE_STANDARD_INFO,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise AccessError(FILE_INFORMATION_EX, self._last_error())
        return StandardInfo(
            int(info.end_of_file),
            int(info.links),
            bool(info.delete_pending),
            bool(info.directory),
        )

    def file_identity(self, handle: int) -> FileIdentity:
        """Return the volume, file index, links, size, and last-write time."""
        info = _ByHandleFileInformation()
        if not self._information(handle, ctypes.byref(info)):
            raise AccessError(FILE_INFORMATION, self._last_error())
        return FileIdentity(
            int(info.volume_serial),
            (int(info.index_high) << 32) | int(info.index_low),
            int(info.links),
            (int(info.size_high) << 32) | int(info.size_low),
            (int(info.last_write.high) << 32) | int(info.last_write.low),
        )

    def final_path(self, handle: int) -> tuple[int, str]:
        """Return the length GetFinalPathNameByHandleW gave, and the text.

        The call writes into a buffer of ``FINAL_PATH_BUFFER_CHARACTERS``
        characters, normalized, with a drive letter. A return of 0 is a
        failed call; the caller refuses any length that does not fit.
        """
        buffer = ctypes.create_unicode_buffer(FINAL_PATH_BUFFER_CHARACTERS)
        length = int(
            self._final_path(
                handle,
                buffer,
                FINAL_PATH_BUFFER_CHARACTERS,
                FILE_NAME_NORMALIZED | VOLUME_NAME_DOS,
            )
        )
        if length == 0:
            raise AccessError(FINAL_PATH, self._last_error())
        if length >= FINAL_PATH_BUFFER_CHARACTERS:
            return length, ""
        return length, buffer.value[:length]

    def find_first(self, pattern: str) -> tuple[int, FindEntry]:
        """Start one search for ``pattern``; return its handle and first entry."""
        data = _Win32FindDataW()
        handle = self._find_first(
            pattern,
            FIND_EX_INFO_BASIC,
            ctypes.byref(data),
            FIND_EX_SEARCH_NAME_MATCH,
            None,
            FIND_FIRST_EX_ON_DISK_ENTRIES_ONLY,
        )
        if handle is None or handle == INVALID_HANDLE_VALUE:
            raise AccessError(FIND_FIRST, self._last_error())
        return int(handle), _entry(data)

    def find_next(self, search: int) -> FindEntry:
        """Return a search's next entry; its end is ERROR_NO_MORE_FILES."""
        data = _Win32FindDataW()
        if not self._find_next(search, ctypes.byref(data)):
            raise AccessError(FIND_NEXT, self._last_error())
        return _entry(data)

    def find_close(self, search: int) -> None:
        """Close one search handle."""
        if not self._find_close(search):
            raise AccessError(FIND_CLOSE, self._last_error())

    def close_handle(self, handle: int) -> None:
        """Close one handle CreateFileW opened."""
        if not self._close_handle(handle):
            raise AccessError(CLOSE_HANDLE, self._last_error())

    def open_descriptor(self, handle: int) -> int:
        """Return a read-only, non-inheritable descriptor owning ``handle``.

        Never text, wide-text, or append mode. On OSError no descriptor
        exists and the handle is still the caller's; once a descriptor is
        returned, it owns the handle.
        """
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_NOINHERIT)

    def set_binary(self, descriptor: int) -> None:
        """Put a descriptor in untranslated binary mode."""
        msvcrt.setmode(descriptor, os.O_BINARY)

    def inheritable(self, descriptor: int) -> bool:
        """Return whether a child process could inherit the descriptor."""
        return os.get_inheritable(descriptor)

    def read(self, descriptor: int, size: int) -> bytes:
        """Read at most ``size`` bytes; empty bytes mean the end of the file."""
        return os.read(descriptor, size)

    def descriptor_handle(self, descriptor: int) -> int:
        """Return the handle a descriptor owns, for queries only."""
        return msvcrt.get_osfhandle(descriptor)

    def close_descriptor(self, descriptor: int) -> None:
        """Close a descriptor, which also closes the handle it owns."""
        os.close(descriptor)


def load_access() -> WindowsFileAccess | None:
    """Return the pinned functions, or None where project reading is unavailable.

    That is anywhere but Windows, and any Windows without all nine
    functions, such as one older than Windows 10, version 1709.
    """
    if sys.platform != "win32" or msvcrt is None:
        return None
    try:
        return WindowsFileAccess(
            _library(_KERNEL32),
            _library(_NTDLL),
            _last_error,
        )
    except (OSError, AttributeError):
        return None


def _pinned(function: object, argtypes: list[object], restype: object) -> object:
    """Pin one function's argument and result types, and return it."""
    function.argtypes = argtypes
    function.restype = restype
    return function


def _entry(data: _Win32FindDataW) -> FindEntry:
    """Return the parts of one find data structure Maria's checks read."""
    return FindEntry(
        str(data.name),
        int(data.attributes),
        int(data.reserved0),
        (int(data.size_high) << 32) | int(data.size_low),
    )


def _last_error() -> int:
    """Return the calling thread's last Windows error, as ctypes kept it."""
    return int(ctypes.get_last_error())


def _library(name: str) -> object:
    """Load one Windows library by name; tests replace this with fakes."""
    return ctypes.WinDLL(name, use_last_error=True)
