"""Tests for Maria's Windows file primitives of V1-24-R1.

The binding tests replace the libraries with fakes, as the notice module's
tests do, and pin the argument and result types of each of the nine
functions, what each method makes of a failing return, and that there is no
access off Windows or when any function is missing. The real tests call
Windows only on temporary folders this test creates: a plain read, a case
variant, a junction, a hard link, a symbolic link (skipped without the
privilege to make one), a short-name alias (skipped when the volume keeps
none), a writer that is already present, a live writable file mapping with
no writer handle left, writes, renames, and deletes tried during a read, a
file replaced at its path, and no handle left open and the thread's
placeholder mode back as it was after each operation. They skip explicitly on
any other system.
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import sys
import unittest
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import core.project_file_access as access_module
import core.project_files as project_files
from core.project_file_access import (
    CLOSE_HANDLE,
    CREATE_FILE,
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    FINAL_PATH,
    FINAL_PATH_BUFFER_CHARACTERS,
    FIND_CLOSE,
    FIND_FIRST,
    FIND_NEXT,
    GENERIC_READ,
    INVALID_HANDLE_VALUE,
    OPEN_EXISTING,
    AccessError,
    WindowsFileAccess,
    load_access,
)
from core.project_files import (
    FileContent,
    FileRefusal,
    FolderListing,
    ProjectFileReader,
    decode_file,
)
from core.project_records import Project
from tests.project_fakes import StubProtections


STAMP = "2026-10-09T21:24:13Z"
REAL_WINDOWS = sys.platform == "win32"

# What the writable-mapping test asks of Windows beyond Maria's own flags.
GENERIC_WRITE = 0x40000000
FILE_SHARE_DELETE = 0x00000004
PAGE_READWRITE = 0x04
FILE_MAP_WRITE = 0x0002


class _Function:
    """A fake Windows function: it records its pinned types and its calls."""

    def __init__(self, name: str, result: object) -> None:
        self.__name__ = name
        self.argtypes: object = None
        self.restype: object = None
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *arguments: object) -> object:
        self.calls.append(arguments)
        return self.result


class _Library:
    """A fake library holding exactly the named functions."""

    def __init__(self, **results: object) -> None:
        for name, result in results.items():
            setattr(self, name, _Function(name, result))


KERNEL32_FUNCTIONS = (
    "CreateFileW",
    "GetFileInformationByHandle",
    "GetFileInformationByHandleEx",
    "GetFinalPathNameByHandleW",
    "FindFirstFileExW",
    "FindNextFileW",
    "FindClose",
    "CloseHandle",
)


def _libraries(**results: object) -> tuple[_Library, _Library]:
    """Return fake kernel32 and ntdll libraries, every function present."""
    kernel32 = _Library(
        **{name: results.get(name, 1) for name in KERNEL32_FUNCTIONS}
    )
    ntdll = _Library(
        RtlSetThreadPlaceholderCompatibilityMode=results.get(
            "RtlSetThreadPlaceholderCompatibilityMode",
            0,
        )
    )
    return kernel32, ntdll


class BindingTests(unittest.TestCase):
    """Every function is looked up once and pinned exactly."""

    def test_each_function_has_its_argument_and_result_types_pinned(self) -> None:
        kernel32, ntdll = _libraries()

        WindowsFileAccess(kernel32, ntdll, lambda: 0)

        dword = ctypes.c_uint32
        expected = {
            "CreateFileW": (
                [
                    ctypes.c_wchar_p,
                    dword,
                    dword,
                    ctypes.c_void_p,
                    dword,
                    dword,
                    ctypes.c_void_p,
                ],
                ctypes.c_void_p,
            ),
            "GetFileInformationByHandle": (
                [
                    ctypes.c_void_p,
                    ctypes.POINTER(access_module._ByHandleFileInformation),
                ],
                ctypes.c_int,
            ),
            "GetFileInformationByHandleEx": (
                [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, dword],
                ctypes.c_int,
            ),
            "GetFinalPathNameByHandleW": (
                [ctypes.c_void_p, ctypes.c_wchar_p, dword, dword],
                dword,
            ),
            "FindFirstFileExW": (
                [
                    ctypes.c_wchar_p,
                    ctypes.c_int,
                    ctypes.POINTER(access_module._Win32FindDataW),
                    ctypes.c_int,
                    ctypes.c_void_p,
                    dword,
                ],
                ctypes.c_void_p,
            ),
            "FindNextFileW": (
                [ctypes.c_void_p, ctypes.POINTER(access_module._Win32FindDataW)],
                ctypes.c_int,
            ),
            "FindClose": ([ctypes.c_void_p], ctypes.c_int),
            "CloseHandle": ([ctypes.c_void_p], ctypes.c_int),
        }
        for name, (argtypes, restype) in expected.items():
            with self.subTest(function=name):
                function = getattr(kernel32, name)
                self.assertEqual(function.argtypes, argtypes)
                self.assertIs(function.restype, restype)
        placeholder = ntdll.RtlSetThreadPlaceholderCompatibilityMode
        self.assertEqual(placeholder.argtypes, [ctypes.c_byte])
        self.assertIs(placeholder.restype, ctypes.c_byte)

    def test_the_structures_have_their_windows_sizes(self) -> None:
        self.assertEqual(ctypes.sizeof(access_module._ByHandleFileInformation), 52)
        self.assertEqual(ctypes.sizeof(access_module._FileStandardInfo), 24)
        self.assertEqual(ctypes.sizeof(access_module._FileAttributeTagInfo), 8)
        if ctypes.sizeof(ctypes.c_wchar) == 2:
            self.assertEqual(ctypes.sizeof(access_module._Win32FindDataW), 592)

    def test_a_missing_function_or_another_system_means_no_access(self) -> None:
        for missing in (
            *KERNEL32_FUNCTIONS,
            "RtlSetThreadPlaceholderCompatibilityMode",
        ):
            with self.subTest(missing=missing):
                kernel32, ntdll = _libraries()
                for library in (kernel32, ntdll):
                    if hasattr(library, missing):
                        delattr(library, missing)
                with (
                    patch.object(access_module.sys, "platform", "win32"),
                    patch.object(access_module, "msvcrt", Mock()),
                    patch.object(
                        access_module,
                        "_library",
                        side_effect=lambda name: (
                            kernel32 if name == "kernel32" else ntdll
                        ),
                    ),
                ):
                    self.assertIsNone(load_access())
        with patch.object(access_module.sys, "platform", "linux"):
            self.assertIsNone(load_access())
        with (
            patch.object(access_module.sys, "platform", "win32"),
            patch.object(access_module, "msvcrt", Mock()),
            patch.object(access_module, "_library", side_effect=OSError("no")),
        ):
            self.assertIsNone(load_access())

    def test_failing_returns_become_named_failures_with_the_error_number(
        self,
    ) -> None:
        kernel32, ntdll = _libraries(
            CreateFileW=INVALID_HANDLE_VALUE,
            GetFinalPathNameByHandleW=0,
            FindFirstFileExW=INVALID_HANDLE_VALUE,
            FindNextFileW=0,
            FindClose=0,
            CloseHandle=0,
            GetFileInformationByHandle=0,
            GetFileInformationByHandleEx=0,
        )
        access = WindowsFileAccess(kernel32, ntdll, lambda: 1234)

        for call, name in (
            (lambda: access.create_file("x", 0, 0, 0), CREATE_FILE),
            (lambda: access.final_path(5), FINAL_PATH),
            (lambda: access.find_first("x"), FIND_FIRST),
            (lambda: access.find_next(5), FIND_NEXT),
            (lambda: access.find_close(5), FIND_CLOSE),
            (lambda: access.close_handle(5), CLOSE_HANDLE),
        ):
            with self.subTest(call=name):
                with self.assertRaises(AccessError) as raised:
                    call()
                self.assertEqual(
                    (raised.exception.call, raised.exception.error),
                    (name, 1234),
                )
                self.assertEqual(raised.exception.args, (name, 1234))

    def test_a_final_path_that_does_not_fit_is_returned_unread(self) -> None:
        kernel32, ntdll = _libraries(
            GetFinalPathNameByHandleW=FINAL_PATH_BUFFER_CHARACTERS + 1
        )
        access = WindowsFileAccess(kernel32, ntdll, lambda: 0)

        self.assertEqual(
            access.final_path(5),
            (FINAL_PATH_BUFFER_CHARACTERS + 1, ""),
        )
        call = kernel32.GetFinalPathNameByHandleW.calls[0]
        self.assertEqual(call[2], FINAL_PATH_BUFFER_CHARACTERS)
        self.assertEqual(call[3], 0)

    def test_the_root_and_a_file_are_opened_as_stated(self) -> None:
        kernel32, ntdll = _libraries(CreateFileW=77)
        access = WindowsFileAccess(kernel32, ntdll, lambda: 0)

        self.assertEqual(
            access.create_file(
                "\\\\?\\C:\\x",
                access_module.DIRECTORY_ACCESS,
                access_module.DIRECTORY_SHARE,
                access_module.DIRECTORY_FLAGS,
            ),
            77,
        )
        (path, desired, share, security, disposition, flags, template) = (
            kernel32.CreateFileW.calls[0]
        )
        self.assertEqual(desired, 0x80)
        self.assertEqual(share, 0x1 | 0x2)
        self.assertIsNone(security)
        self.assertEqual(disposition, 3)
        self.assertEqual(flags, 0x02000000 | 0x00200000)
        self.assertIsNone(template)
        self.assertEqual(access_module.FILE_ACCESS, 0x80000000)
        self.assertEqual(access_module.FILE_SHARE, 0x1)
        self.assertEqual(access_module.FILE_FLAGS, 0x00200000)

    def test_the_module_imports_only_what_it_needs(self) -> None:
        import ast

        source = Path(access_module.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        self.assertEqual(
            imported,
            {
                "__future__",
                "collections",
                "ctypes",
                "dataclasses",
                "msvcrt",
                "os",
                "sys",
            },
        )
        self.assertNotIn("logging", source)
        self.assertNotIn("subprocess", source)


@unittest.skipUnless(REAL_WINDOWS, "Real project file reading runs only on Windows.")
class RealWindowsTests(unittest.TestCase):
    """Real calls, only on temporary folders this test creates."""

    def setUp(self) -> None:
        self.access = load_access()
        if self.access is None:
            self.skipTest("This Windows lacks a function project reading needs.")
        latch = patch.object(project_files, "_LATCH", project_files._RestoreLatch())
        latch.start()
        self.addCleanup(latch.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.outside = self.root.parent / f"{self.root.name}-outside"
        self.outside.mkdir()
        self.addCleanup(self._remove_outside)
        (self.root / "garden").mkdir()
        self.plan = self.root / "garden" / "plan.md"
        self.plan.write_bytes(b"line one\r\nline two\n")
        self.project = Project(1, "Garden", "workspace", "garden", STAMP, STAMP)

    def _remove_outside(self) -> None:
        for entry in sorted(self.outside.rglob("*"), reverse=True):
            if entry.is_dir():
                entry.rmdir()
            else:
                entry.unlink()
        self.outside.rmdir()

    def reader(
        self,
        access: object | None = None,
        root: Path | None = None,
    ) -> ProjectFileReader:
        """Return a reader whose workspace root is this test's folder."""

        class _Roots:
            def __init__(self, path: Path) -> None:
                self.path = path

            def by_name(inner, name: str) -> object:
                return inner if name == "workspace" else None

        roots = _Roots(self.root if root is None else root)
        chosen = self.access if access is None else access
        return ProjectFileReader(
            roots,
            StubProtections(),
            Mock(spec=logging.Logger),
            access_loader=lambda: chosen,
        )

    def handle_count(self) -> int:
        """Return how many handles this process holds now."""
        count = ctypes.c_uint32(0)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetProcessHandleCount.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel32.GetProcessHandleCount.restype = ctypes.c_int
        if not kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(),
            ctypes.byref(count),
        ):
            self.skipTest("The process handle count could not be read.")
        return int(count.value)

    def placeholder_mode(self) -> int:
        """Return this thread's placeholder mode without changing it."""
        ntdll = ctypes.WinDLL("ntdll")
        query = ntdll.RtlQueryThreadPlaceholderCompatibilityMode
        query.argtypes = []
        query.restype = ctypes.c_byte
        return int(query())

    def test_a_plain_file_is_read_exactly_and_its_case_variant_takes_disk_spelling(
        self,
    ) -> None:
        mode = self.placeholder_mode()
        before = self.handle_count()

        content = self.reader().read_file(self.project, "PLAN.MD")

        self.assertIs(type(content), FileContent)
        self.assertEqual(content.path, "plan.md")
        self.assertEqual(content.data, b"line one\r\nline two\n")
        self.assertEqual(content.sha256, hashlib.sha256(content.data).hexdigest())
        self.assertEqual(self.handle_count(), before)
        self.assertEqual(self.placeholder_mode(), mode)

    def test_a_listing_and_a_folder_check_leave_no_handle(self) -> None:
        before = self.handle_count()

        listing = self.reader().list_folder(self.project, "")
        checked = self.reader().check_workspace_folder("GARDEN")

        self.assertIs(type(listing), FolderListing)
        self.assertEqual([entry.name for entry in listing.entries], ["plan.md"])
        self.assertEqual(checked, "garden")
        self.assertEqual(self.handle_count(), before)

    def test_an_empty_folder_lists_as_empty(self) -> None:
        (self.root / "garden" / "empty").mkdir()

        listing = self.reader().list_folder(self.project, "empty")

        self.assertIs(type(listing), FolderListing)
        self.assertEqual(listing.entries, ())

    def test_a_junction_is_refused_before_its_target_is_opened(self) -> None:
        import _winapi

        (self.outside / "secret.md").write_bytes(b"secret\n")
        _winapi.CreateJunction(str(self.outside), str(self.root / "garden" / "linked"))
        spy = _Spy(self.access)

        refused = self.reader(spy).read_file(self.project, "linked/secret.md")
        listing = self.reader(spy).list_folder(self.project, "")

        self.assertIs(refused, FileRefusal.UNREADABLE)
        self.assertFalse(any("secret.md" in path for path in spy.opened))
        self.assertNotIn("linked", [entry.name for entry in listing.entries])

    def test_a_hard_link_is_refused_before_any_data_is_read(self) -> None:
        (self.outside / "secret.md").write_bytes(b"secret\n")
        os.link(self.outside / "secret.md", self.root / "garden" / "alias.md")
        spy = _Spy(self.access)

        refused = self.reader(spy).read_file(self.project, "alias.md")

        self.assertIs(refused, FileRefusal.UNREADABLE)
        self.assertEqual(spy.reads, 0)

    def test_a_symbolic_link_is_refused(self) -> None:
        (self.outside / "secret.md").write_bytes(b"secret\n")
        try:
            os.symlink(self.outside / "secret.md", self.root / "garden" / "sym.md")
        except OSError:
            self.skipTest("This account cannot create symbolic links.")
        spy = _Spy(self.access)

        self.assertIs(
            self.reader(spy).read_file(self.project, "sym.md"),
            FileRefusal.UNREADABLE,
        )
        self.assertEqual(spy.reads, 0)

    def test_a_short_name_alias_is_never_a_path(self) -> None:
        long_name = self.root / "garden" / "a long file name.markdown.md"
        long_name.write_bytes(b"x\n")
        buffer = ctypes.create_unicode_buffer(32_768)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetShortPathNameW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
        ]
        kernel32.GetShortPathNameW.restype = ctypes.c_uint32
        if not kernel32.GetShortPathNameW(str(long_name), buffer, 32_768):
            self.skipTest("The short name could not be read.")
        alias = PureWindowsPath(buffer.value).name
        if alias.casefold() == long_name.name.casefold():
            self.skipTest("This volume keeps no short names.")

        self.assertIs(
            self.reader().read_file(self.project, alias),
            FileRefusal.UNREADABLE,
        )

    def test_a_writer_already_present_makes_the_open_fail(self) -> None:
        with open(self.plan, "ab"):
            refused = self.reader().read_file(self.project, "plan.md")

        self.assertIs(refused, FileRefusal.OPEN_FOR_CHANGES)

    def test_a_live_writable_mapping_alone_refuses_the_read_before_any_data(
        self,
    ) -> None:
        original = self.plan.read_bytes()
        marker = b"MAPPED"
        api = _mapping_api()
        # The control: the same file reads fine while nothing maps it.
        control = self.reader().read_file(self.project, "plan.md")
        self.assertEqual(control.data, original)
        before = self.handle_count()
        held: dict[str, int] = {}

        def release() -> None:
            """Unmap the view and close what is still held, whatever happened."""
            if "view" in held:
                api.UnmapViewOfFile(held.pop("view"))
            for name in ("section", "writer"):
                if name in held:
                    api.CloseHandle(held.pop(name))

        self.addCleanup(release)

        # An ordinary writer handle, a writable section on it, and a view of
        # the whole file: the only way to write through a mapping.
        held["writer"] = api.CreateFileW(
            str(self.plan),
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        self.assertNotIn(held["writer"], (None, INVALID_HANDLE_VALUE))
        held["section"] = api.CreateFileMappingW(
            held["writer"], None, PAGE_READWRITE, 0, 0, None
        )
        self.assertTrue(held["section"])
        held["view"] = api.MapViewOfFile(held["section"], FILE_MAP_WRITE, 0, 0, 0)
        self.assertTrue(held["view"])

        # The mapping is live and backed by this file: a write through the
        # view reads back, and an ordinary open of the file sees it once
        # flushed.
        ctypes.memmove(held["view"], marker, len(marker))
        self.assertEqual(ctypes.string_at(held["view"], len(marker)), marker)
        self.assertTrue(api.FlushViewOfFile(held["view"], 0))
        self.assertEqual(self.plan.read_bytes()[: len(marker)], marker)

        # Now close the writer handle and the section handle. Only the view
        # keeps the file mapped, and no handle beyond the starting count is
        # open, so this is not a writer that was merely left open.
        self.assertTrue(api.CloseHandle(held.pop("writer")))
        self.assertTrue(api.CloseHandle(held.pop("section")))
        self.assertEqual(self.handle_count(), before)
        self.assertEqual(ctypes.string_at(held["view"], len(marker)), marker)

        spy = _Spy(self.access)
        refused = self.reader(spy).read_file(self.project, "plan.md")

        self.assertIs(refused, FileRefusal.OPEN_FOR_CHANGES)
        self.assertEqual(spy.reads, 0)
        self.assertEqual(self.handle_count(), before)

        # Release the view after putting the bytes back; an ordinary read
        # then succeeds with exactly the original bytes.
        ctypes.memmove(held["view"], original[: len(marker)], len(marker))
        self.assertTrue(api.FlushViewOfFile(held["view"], 0))
        self.assertTrue(api.UnmapViewOfFile(held.pop("view")))

        content = self.reader().read_file(self.project, "plan.md")

        self.assertIs(type(content), FileContent)
        self.assertEqual(content.data, original)
        self.assertEqual(self.handle_count(), before)

    def test_writes_renames_and_deletes_fail_while_a_read_holds_its_handles(
        self,
    ) -> None:
        attempts: dict[str, bool] = {}
        plan = self.plan
        folder = self.root / "garden"

        def interfere() -> None:
            for label, action in (
                ("write", lambda: open(plan, "ab").close()),
                ("rename file", lambda: os.rename(plan, folder / "moved.md")),
                ("delete file", lambda: os.remove(plan)),
                ("rename folder", lambda: os.rename(folder, self.root / "moved")),
            ):
                try:
                    action()
                except PermissionError:
                    attempts[label] = False
                else:
                    attempts[label] = True

        content = self.reader(_Spy(self.access, on_first_read=interfere)).read_file(
            self.project,
            "plan.md",
        )

        self.assertIs(type(content), FileContent)
        self.assertEqual(
            attempts,
            {
                "write": False,
                "rename file": False,
                "delete file": False,
                "rename folder": False,
            },
        )

    def test_a_file_replaced_at_its_path_is_read_as_the_file_now_there(
        self,
    ) -> None:
        first = self.reader().read_file(self.project, "plan.md")
        self.plan.unlink()
        self.plan.write_bytes(b"replacement\n")

        second = self.reader().read_file(self.project, "plan.md")

        self.assertEqual(first.data, b"line one\r\nline two\n")
        self.assertEqual(second.data, b"replacement\n")

    def test_a_junction_as_the_project_folder_or_a_listed_folder_is_refused(
        self,
    ) -> None:
        import _winapi

        (self.outside / "plan.md").write_bytes(b"outside\n")
        _winapi.CreateJunction(str(self.outside), str(self.root / "linked"))
        _winapi.CreateJunction(str(self.outside), str(self.root / "garden" / "nested"))
        linked = Project(2, "Linked", "workspace", "linked", STAMP, STAMP)
        spy = _Spy(self.access)

        self.assertIs(
            self.reader(spy).read_file(linked, "plan.md"),
            FileRefusal.UNREADABLE,
        )
        self.assertIs(self.reader(spy).list_folder(linked, ""), FileRefusal.UNREADABLE)
        self.assertIs(
            self.reader(spy).list_folder(self.project, "nested"),
            FileRefusal.UNREADABLE,
        )
        self.assertEqual(
            [
                path
                for path in spy.opened
                if "linked" in path.casefold() or "nested" in path.casefold()
            ],
            [],
        )

    def test_a_symbolic_link_to_a_folder_is_refused(self) -> None:
        (self.outside / "secret.md").write_bytes(b"secret\n")
        try:
            os.symlink(
                self.outside,
                self.root / "garden" / "dirlink",
                target_is_directory=True,
            )
        except OSError:
            self.skipTest("This account cannot create symbolic links.")
        spy = _Spy(self.access)

        self.assertIs(
            self.reader(spy).read_file(self.project, "dirlink/secret.md"),
            FileRefusal.UNREADABLE,
        )
        self.assertEqual(spy.reads, 0)

    def test_any_bytes_are_read_exactly_through_a_non_inheritable_descriptor(
        self,
    ) -> None:
        data = b"\xef\xbb\xbfone\r\ntwo\rthree\x1afour\nlast"
        (self.root / "garden" / "mixed.txt").write_bytes(data)
        spy = _Spy(self.access)

        content = self.reader(spy).read_file(self.project, "mixed.txt")

        self.assertEqual(content.data, data)
        self.assertEqual(content.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(spy.inheritable_results, [False])
        decoded = decode_file(content)
        # The byte-order mark goes, and only line feeds end lines.
        self.assertEqual(decoded.line_count, 3)
        self.assertEqual(decoded.text_of(2, 2), "two\rthree\x1afour\n")

    def test_after_an_operation_no_handle_keeps_the_file_or_folder(self) -> None:
        self.reader().read_file(self.project, "plan.md")
        self.reader().list_folder(self.project, "")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        # An open that shares nothing succeeds only while no other handle is
        # open, and so do renaming the file and its folder.
        exclusive = kernel32.CreateFileW(
            str(self.plan), 0x80000000, 0, None, 3, 0, None
        )
        self.assertNotIn(exclusive, (None, ctypes.c_void_p(-1).value))
        self.assertTrue(kernel32.CloseHandle(exclusive))
        os.rename(self.plan, self.plan.with_name("renamed.md"))
        os.rename(self.root / "garden", self.root / "garden-renamed")

    def test_a_root_reached_through_a_junction_is_refused_before_any_component(
        self,
    ) -> None:
        import _winapi

        alias = self.outside / "rootlink"
        _winapi.CreateJunction(str(self.root), str(alias))
        self.addCleanup(os.rmdir, alias)
        spy = _Spy(self.access)

        refused = self.reader(spy, root=alias).read_file(self.project, "plan.md")

        self.assertIs(refused, FileRefusal.UNREADABLE)
        self.assertEqual(len(spy.opened), 1)
        self.assertEqual(spy.reads, 0)


def _mapping_api() -> object:
    """Return kernel32 with the calls a writable file mapping needs, typed."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle, size = ctypes.c_void_p, ctypes.c_size_t
    dword, text = ctypes.c_uint32, ctypes.c_wchar_p
    pointer = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [text, dword, dword, pointer, dword, dword, handle]
    kernel32.CreateFileW.restype = handle
    kernel32.CreateFileMappingW.argtypes = [handle, pointer, dword, dword, dword, text]
    kernel32.CreateFileMappingW.restype = handle
    kernel32.MapViewOfFile.argtypes = [handle, dword, dword, dword, size]
    kernel32.MapViewOfFile.restype = ctypes.c_void_p
    kernel32.FlushViewOfFile.argtypes = [ctypes.c_void_p, size]
    kernel32.FlushViewOfFile.restype = ctypes.c_int
    kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    kernel32.UnmapViewOfFile.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [handle]
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


class _Spy:
    """Pass every call through to the real access, watching opens and reads."""

    def __init__(self, access: object, on_first_read: object | None = None) -> None:
        self._access = access
        self._on_first_read = on_first_read
        self.opened: list[str] = []
        self.reads = 0
        self.inheritable_results: list[bool] = []

    def __getattr__(self, name: str) -> object:
        return getattr(self._access, name)

    def create_file(self, path: str, access: int, share: int, flags: int) -> int:
        self.opened.append(path)
        return self._access.create_file(path, access, share, flags)

    def inheritable(self, descriptor: int) -> bool:
        result = self._access.inheritable(descriptor)
        self.inheritable_results.append(result)
        return result

    def read(self, descriptor: int, size: int) -> bytes:
        self.reads += 1
        if self.reads == 1 and self._on_first_read is not None:
            self._on_first_read()
        return self._access.read(descriptor, size)


if __name__ == "__main__":
    unittest.main()
