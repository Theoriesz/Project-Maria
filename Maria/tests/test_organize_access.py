"""Tests for Maria's Windows file primitives of V1-25-R1.

The binding tests replace the libraries with fakes and pin the argument and
result types of each of the seven functions, every structure's size, the exact
masks of each purpose, every length taken from UTF-16LE bytes, how each status
is read, what happens to a pending request's memory, and that there is no access
off Windows, on a 32-bit process, or when any function is missing. The real
tests call Windows only on temporary folders this test creates, on a volume the
test confirms is NTFS: a held root's file system name, a relative open and
creation, a name collision, a junction seen and not followed, a rename that
never replaces, a name outside the BMP, held directories and sources that
resist a rename, a removal, and an open that shares no deletion, in this process
and in a child, and no handle left open. They skip
explicitly on any other system. A real pending request, a real wrong-length
rename and a malformed native buffer are never induced.
"""

from __future__ import annotations

import ctypes
import gc
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import core.organize_access as access_module
from core.organize_access import (
    AccessError,
    InvalidName,
    OptionsError,
    OrganizeAccess,
    PendingError,
    StoppedError,
    load_access,
    pending_latched,
    utf16_bytes,
)
from tests.organize_fakes import (
    NAME_NON_BMP,
    FakeWindows,
    address_of,
)

try:
    import _winapi
except ImportError:  # not Windows
    _winapi = None

WINDOWS_ONLY = "File organization needs 64-bit Windows."


def reset_pending_state() -> None:
    """Clear the process-wide latch, which tests must not leak between them."""
    access_module._PENDING_LATCH = False


class LayoutAndMaskTests(unittest.TestCase):
    """The structure layouts and every exact mask."""

    def test_structure_sizes_and_offsets(self) -> None:
        text = access_module._UnicodeString
        attributes = access_module._ObjectAttributes
        self.assertEqual(ctypes.sizeof(text), 16)
        self.assertEqual(text.buffer.offset, 8)
        self.assertEqual(ctypes.sizeof(attributes), 48)
        self.assertEqual(
            {name: getattr(attributes, name).offset for name, _ in attributes._fields_},
            {
                "length": 0,
                "root_directory": 8,
                "object_name": 16,
                "attributes": 24,
                "security_descriptor": 32,
                "security_quality_of_service": 40,
            },
        )
        self.assertEqual(ctypes.sizeof(access_module._IoStatusBlock), 16)
        self.assertEqual(access_module._IoStatusBlock.information.offset, 8)
        self.assertEqual(ctypes.sizeof(access_module._ByHandleFileInformation), 52)

    def test_the_rename_information_header(self) -> None:
        header = access_module._RENAME_HEADER
        self.assertEqual((header.size, access_module._RENAME_SIZE), (20, 24))
        packed = header.pack(1, 0x1122334455667788, 14)
        self.assertEqual(packed[0:4], b"\x01\x00\x00\x00")
        self.assertEqual(packed[4:8], b"\x00" * 4)
        self.assertEqual(int.from_bytes(packed[8:16], "little"), 0x1122334455667788)
        self.assertEqual(int.from_bytes(packed[16:20], "little"), 14)

    def test_constants_are_pinned(self) -> None:
        pinned = {
            "ACCESS_DELETE": 0x10000,
            "SYNCHRONIZE": 0x100000,
            "FILE_TRAVERSE": 0x20,
            "FILE_READ_ATTRIBUTES": 0x80,
            "FILE_OPEN": 1,
            "FILE_CREATE": 2,
            "FILE_DIRECTORY_FILE": 1,
            "FILE_SYNCHRONOUS_IO_NONALERT": 0x20,
            "FILE_NON_DIRECTORY_FILE": 0x40,
            "FILE_OPEN_REPARSE_POINT": 0x200000,
            "OBJ_CASE_INSENSITIVE": 0x40,
            "FILE_CREATED": 2,
            "FILE_RENAME_INFORMATION": 10,
            "STATUS_PENDING": 0x103,
            "STATUS_OBJECT_NAME_NOT_FOUND": 0xC0000034,
            "STATUS_OBJECT_NAME_COLLISION": 0xC0000035,
            "STATUS_SHARING_VIOLATION": 0xC0000043,
            "ROOT_OPEN_FLAGS": 0x02200000,
        }
        for name, value in pinned.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(access_module, name), value)

    def test_each_purpose_has_its_own_exact_set(self) -> None:
        m = access_module
        self.assertEqual(
            (m.ROOT_ACCESS, m.OPEN_DIRECTORY_ACCESS, m.CREATE_DIRECTORY_ACCESS),
            (0x1000A0, 0x1000A0, 0x1000A0),
        )
        self.assertEqual(
            (
                m.OPEN_DIRECTORY_SHARE,
                m.OPEN_DIRECTORY_DISPOSITION,
                m.OPEN_DIRECTORY_OPTIONS,
            ),
            (3, 1, 0x200020),
        )
        self.assertEqual(
            (
                m.CREATE_DIRECTORY_SHARE,
                m.CREATE_DIRECTORY_DISPOSITION,
                m.CREATE_DIRECTORY_OPTIONS,
            ),
            (3, 2, 0x21),
        )
        self.assertEqual((m.LOOKUP_ACCESS, m.LOOKUP_SHARE, m.LOOKUP_OPTIONS), (0x100080, 7, 0x200020))
        self.assertEqual(
            (m.SOURCE_ACCESS, m.SOURCE_SHARE, m.SOURCE_OPTIONS),
            (0x110080, 1, 0x200060),
        )
        # An existing directory is opened without FILE_DIRECTORY_FILE, so a
        # junction is seen; creation carries only options compatible with it.
        self.assertEqual(m.OPEN_DIRECTORY_OPTIONS & m.FILE_DIRECTORY_FILE, 0)
        self.assertEqual(m.CREATE_DIRECTORY_OPTIONS & m.FILE_OPEN_REPARSE_POINT, 0)
        self.assertEqual(
            m.CREATE_DIRECTORY_OPTIONS & ~m.DIRECTORY_FILE_COMPATIBLE_OPTIONS, 0
        )
        for mask in (m.OPEN_DIRECTORY_ACCESS, m.CREATE_DIRECTORY_ACCESS):
            self.assertEqual(mask & 0x6, 0)  # no add-file or add-subdirectory bit


class BindingTests(unittest.TestCase):
    """The access over fakes: marshalling, lengths, statuses, and options."""

    def setUp(self) -> None:
        reset_pending_state()
        self.addCleanup(reset_pending_state)
        self.fake = FakeWindows()
        self.access = self.fake.access()

    def test_every_length_comes_from_the_utf16_bytes_of_a_non_bmp_name(self) -> None:
        raw = NAME_NON_BMP.encode("utf-16-le")
        self.assertEqual((len(NAME_NON_BMP), len(raw)), (6, 14))
        self.assertNotEqual(2 * len(NAME_NON_BMP), len(raw))
        self.access.open_relative(
            0x55, NAME_NON_BMP, access_module.LOOKUP_ACCESS, 7, 1, 0x200020
        )
        sent = self.fake.last()
        self.assertEqual(sent["length"], 14)  # no terminator in a length
        self.assertEqual(sent["maximum"], 16)  # the storage holds it
        self.assertEqual(sent["name"], raw)
        self.assertEqual(sent["terminator"], b"\x00\x00")

    def test_the_rename_information_lengths_and_separate_storage(self) -> None:
        raw = NAME_NON_BMP.encode("utf-16-le")
        self.access.rename_into(0x10, 0x20, NAME_NON_BMP)
        sent = self.fake.last()
        self.assertEqual((sent["class"], sent["handle"]), (10, 0x10))
        self.assertEqual(int.from_bytes(sent["raw"][8:16], "little"), 0x20)
        self.assertEqual(int.from_bytes(sent["raw"][16:20], "little"), 14)
        self.assertEqual(sent["raw"][20:34], raw)
        self.assertEqual(sent["raw"][34:36], b"\x00\x00")
        self.assertEqual(sent["raw"][0], 0)  # ReplaceIfExists is false, always
        self.assertEqual(sent["length"], 24 + 14)
        self.assertGreaterEqual(sent["storage"], sent["length"])

    def test_there_is_no_way_to_ask_for_a_replacement(self) -> None:
        with self.assertRaises(TypeError):
            self.access.rename_into(1, 2, "f.txt", True)  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            self.access.rename_into(1, 2, "f.txt", replace=True)  # type: ignore[call-arg]
        self.access.rename_into(1, 2, "f.txt")
        self.assertEqual(self.fake.last()["raw"][0], 0)

    def test_a_name_that_cannot_be_one_simple_component_is_refused_before_a_call(self) -> None:
        for bad in (
            "a" + chr(0xD800),
            "x" * 256,
            "",
            "a\\b",
            "a/b",
            "a:b",
            ":stream",
            "a\x00b",
            b"bytes",
            None,
        ):
            with self.subTest(name=repr(bad)):
                before = len(self.fake.calls)
                with self.assertRaises(InvalidName):
                    self.access.open_relative(1, bad, 1, 1, 1, 1)  # type: ignore[arg-type]
                with self.assertRaises(InvalidName):
                    self.access.rename_into(1, 2, bad)  # type: ignore[arg-type]
                self.assertEqual(len(self.fake.calls), before)

    def test_a_name_of_non_bmp_characters_is_measured_in_units_not_characters(self) -> None:
        astral = chr(0x1F600)
        self.assertEqual(len(utf16_bytes(astral * 127)), 508)  # 254 units
        with self.assertRaises(InvalidName):
            utf16_bytes(astral * 128)  # 256 units, though only 128 characters

    def test_a_name_of_255_units_is_accepted_and_256_refused(self) -> None:
        self.access.open_relative(1, "x" * 255, 1, 1, 1, 1)
        self.assertEqual(self.fake.last()["length"], 510)
        with self.assertRaises(InvalidName):
            utf16_bytes("x" * 256)

    def test_the_object_attributes_are_filled_explicitly(self) -> None:
        self.access.open_relative(
            0x55,
            "plain.txt",
            access_module.SOURCE_ACCESS,
            access_module.SOURCE_SHARE,
            access_module.FILE_OPEN,
            access_module.SOURCE_OPTIONS,
        )
        sent = self.fake.last()
        self.assertEqual(sent["directory"], 0x55)
        self.assertEqual(sent["attributes_length"], 48)
        self.assertEqual(sent["attributes"], access_module.OBJ_CASE_INSENSITIVE)
        self.assertEqual(
            (sent["access"], sent["share"], sent["disposition"], sent["options"]),
            (0x110080, 1, 1, 0x200060),
        )

    def test_an_existing_directory_open_and_a_creation_send_their_exact_sets(self) -> None:
        m = access_module
        self.access.open_relative(
            1, "Docs", m.OPEN_DIRECTORY_ACCESS, m.OPEN_DIRECTORY_SHARE,
            m.OPEN_DIRECTORY_DISPOSITION, m.OPEN_DIRECTORY_OPTIONS,
        )
        opened = self.fake.last()
        self.access.open_relative(
            1, "Docs", m.CREATE_DIRECTORY_ACCESS, m.CREATE_DIRECTORY_SHARE,
            m.CREATE_DIRECTORY_DISPOSITION, m.CREATE_DIRECTORY_OPTIONS,
        )
        created = self.fake.last()
        self.assertEqual(
            (opened["access"], opened["share"], opened["disposition"], opened["options"]),
            (0x1000A0, 3, 1, 0x200020),
        )
        self.assertEqual(
            (created["access"], created["share"], created["disposition"], created["options"]),
            (0x1000A0, 3, 2, 0x21),
        )

    def test_an_incompatible_directory_option_set_is_refused_before_the_call(self) -> None:
        m = access_module
        before = len(self.fake.calls)
        with self.assertRaises(OptionsError):
            self.access.open_relative(
                1, "Docs", 1, 3, m.FILE_CREATE,
                m.CREATE_DIRECTORY_OPTIONS | m.FILE_OPEN_REPARSE_POINT,
            )
        with self.assertRaises(OptionsError):
            self.access.open_relative(1, "Docs", 1, 3, 5, m.FILE_DIRECTORY_FILE)
        self.assertEqual(len(self.fake.calls), before)

    def test_only_a_returned_success_status_is_success(self) -> None:
        self.fake.set_information = 24
        result = self.access.rename_into(1, 2, "f.txt")
        self.assertEqual(
            (result.returned, result.block_status, result.information, result.length),
            (0, 0, 24, 24 + 10),
        )

    def test_a_failure_status_is_a_coded_error_that_names_nothing(self) -> None:
        self.fake.set_status = -1073741771  # 0xC0000035 as a signed value
        with self.assertRaises(AccessError) as raised:
            self.access.rename_into(1, 2, "secret-name.txt")
        error = raised.exception
        self.assertEqual(
            (error.call, error.kind, error.code), ("NtSetInformationFile", "nt", 0xC0000035)
        )
        self.assertNotIsInstance(error, PendingError)
        self.assertNotIn("secret", str(error))
        self.fake.open_status = -1073741772  # 0xC0000034
        with self.assertRaises(AccessError) as opened:
            self.access.open_relative(1, "secret-name.txt", 1, 1, 1, 1)
        self.assertEqual(opened.exception.code, 0xC0000034)
        self.assertNotIn("secret", str(opened.exception))

    def test_every_other_non_success_status_is_an_error(self) -> None:
        for status in (0x102, 0x40000000, 0x80000005, 0xC0000022):
            with self.subTest(status=hex(status)):
                self.fake.set_status = status - (1 << 32) if status >= 1 << 31 else status
                with self.assertRaises(AccessError):
                    self.access.rename_into(1, 2, "f.txt")

    def test_windows_failures_carry_the_last_error_only(self) -> None:
        self.fake.create_result, self.fake.error = 0xFFFFFFFFFFFFFFFF, 5
        with self.assertRaises(AccessError) as raised:
            self.access.create_file("C:\\secret", 1, 1, 1)
        self.assertEqual((raised.exception.kind, raised.exception.code), ("win32", 5))
        self.assertNotIn("secret", str(raised.exception))
        self.fake.close_ok, self.fake.error = 0, 6
        with self.assertRaises(AccessError):
            self.access.close_handle(1)

    def test_information_and_final_path_are_read_from_the_calls(self) -> None:
        info = self.access.file_info(1)
        self.assertEqual(
            (info.attributes, info.volume, info.index, info.links, info.size, info.last_write),
            (0x20, 9, (1 << 32) | 2, 1, 77, (5 << 32) | 6),
        )
        self.assertEqual(self.access.final_path(1), "\\\\?\\C:\\fake")

    def test_the_file_system_query_asks_only_for_the_name(self) -> None:
        self.assertEqual(self.access.filesystem_name(0x77), "NTFS")
        sent = self.fake.last()
        self.assertEqual(
            (sent["handle"], sent["volume_name"], sent["volume_size"], sent["serial"],
             sent["component"], sent["flags"], sent["size"]),
            (0x77, None, 0, None, None, None, 261),
        )

    def test_a_failed_file_system_query_is_an_error_not_a_name(self) -> None:
        self.fake.filesystem_ok, self.fake.error = 0, 5
        with self.assertRaises(AccessError) as raised:
            self.access.filesystem_name(1)
        self.assertEqual(
            (raised.exception.call, raised.exception.kind, raised.exception.code),
            ("GetVolumeInformationByHandleW", "win32", 5),
        )

    def test_the_bindings_are_pinned(self) -> None:
        kernel, ntdll = self.fake.library()
        OrganizeAccess(kernel, ntdll, lambda: 0)
        self.assertEqual(len(kernel.CreateFileW.argtypes), 7)
        self.assertIs(kernel.CreateFileW.restype, ctypes.c_void_p)
        self.assertEqual(len(kernel.GetVolumeInformationByHandleW.argtypes), 8)
        self.assertEqual(len(ntdll.NtCreateFile.argtypes), 11)
        self.assertIs(ntdll.NtCreateFile.restype, ctypes.c_long)
        self.assertEqual(len(ntdll.NtSetInformationFile.argtypes), 5)
        self.assertIs(ntdll.NtSetInformationFile.restype, ctypes.c_long)


class PendingLifetimeTests(unittest.TestCase):
    """An unexpected pending request: retention, the latch, and nothing added."""

    def setUp(self) -> None:
        reset_pending_state()
        self.addCleanup(reset_pending_state)

    def test_a_pending_open_keeps_every_request_owned_object_beyond_the_access(self) -> None:
        fake = FakeWindows()
        fake.open_status = 0x103
        access = fake.access()
        before = len(access_module._PENDING_KEEPALIVE)
        with self.assertRaises(PendingError):
            access.open_relative(0x55, "Docs", 0x100080, 7, 1, 0x200020)
        del access
        gc.collect()
        kept = access_module._PENDING_KEEPALIVE[before:]
        self.assertEqual(len(kept), 1)
        ids = {id(item) for item in kept[0]}
        seen = fake.seen["create"]
        self.assertIn(seen["handle"], ids)  # the output-handle storage
        self.assertIn(seen["block"], ids)
        self.assertIn(seen["attributes"], ids)
        self.assertIn(seen["buffer"], {address_of(item) for item in kept[0]})
        self.assertTrue(any(isinstance(item, access_module._UnicodeString) for item in kept[0]))

    def test_a_pending_rename_keeps_its_block_and_storage_beyond_the_access(self) -> None:
        fake = FakeWindows()
        fake.set_status = 0x103
        access = fake.access()
        before = len(access_module._PENDING_KEEPALIVE)
        with self.assertRaises(PendingError) as raised:
            access.rename_into(1, 2, "f.txt")
        self.assertEqual(raised.exception.code, 0x103)
        del access
        gc.collect()
        kept = access_module._PENDING_KEEPALIVE[before:]
        self.assertEqual(len(kept), 1)
        self.assertIn(fake.seen["set"]["block"], {id(item) for item in kept[0]})
        self.assertIn(fake.seen["set"]["buffer"], {address_of(item) for item in kept[0]})

    def test_a_pending_request_latches_and_stops_creation_and_renaming(self) -> None:
        fake = FakeWindows()
        fake.open_status = 0x103
        with self.assertRaises(PendingError):
            fake.access().open_relative(0x55, "Docs", 1, 7, 1, 0x200020)
        self.assertTrue(pending_latched())
        other = FakeWindows()
        access = other.access()
        with self.assertRaises(StoppedError):
            access.rename_into(1, 2, "f.txt")
        with self.assertRaises(StoppedError):
            access.open_relative(
                1, "Docs", access_module.CREATE_DIRECTORY_ACCESS, 3,
                access_module.FILE_CREATE, access_module.CREATE_DIRECTORY_OPTIONS,
            )
        self.assertEqual(other.calls, [])  # nothing reached the native layer

    def test_an_ordinary_failure_never_latches(self) -> None:
        fake = FakeWindows()
        fake.set_status = -1073741790  # 0xC0000022
        with self.assertRaises(AccessError):
            fake.access().rename_into(1, 2, "f.txt")
        self.assertFalse(pending_latched())

    def test_the_module_adds_no_thread_wait_poll_or_cancellation(self) -> None:
        source = Path(access_module.__file__).read_text(encoding="utf-8")
        for word in (
            "threading", "import time", "sleep(", "CancelIo", "WaitForSingleObject",
            "asyncio", "OVERLAPPED", "subprocess", "import os",
        ):
            with self.subTest(word=word):
                self.assertNotIn(word, source)


class AvailabilityTests(unittest.TestCase):
    """There is no access unless Windows, 64 bits, and all seven functions exist."""

    def test_no_access_off_windows(self) -> None:
        with patch.object(sys, "platform", "linux"):
            self.assertIsNone(load_access())

    def test_no_access_on_a_32_bit_process(self) -> None:
        with patch.object(access_module.ctypes, "sizeof", return_value=4):
            self.assertIsNone(load_access())

    def test_no_access_when_a_function_is_missing_or_a_library_fails(self) -> None:
        fake = FakeWindows()
        kernel, ntdll = fake.library()
        del ntdll.NtSetInformationFile
        libraries = {"kernel32": kernel, "ntdll": ntdll}
        with patch.object(sys, "platform", "win32"), patch.object(
            access_module, "_library", side_effect=lambda name: libraries[name]
        ):
            self.assertIsNone(load_access())
        with patch.object(sys, "platform", "win32"), patch.object(
            access_module, "_library", side_effect=OSError()
        ):
            self.assertIsNone(load_access())

    def test_access_is_built_when_all_seven_exist(self) -> None:
        fake = FakeWindows()
        kernel, ntdll = fake.library()
        libraries = {"kernel32": kernel, "ntdll": ntdll}
        with patch.object(sys, "platform", "win32"), patch.object(
            access_module, "_library", side_effect=lambda name: libraries[name]
        ):
            if ctypes.sizeof(ctypes.c_void_p) == 8:
                self.assertIsInstance(load_access(), OrganizeAccess)

    def test_exactly_seven_windows_functions_are_named(self) -> None:
        source = Path(access_module.__file__).read_text(encoding="utf-8")
        named = set(re.findall(r"\b(?:kernel32|ntdll)\.([A-Z][A-Za-z]+)", source))
        self.assertEqual(
            named,
            {
                "CreateFileW",
                "GetFileInformationByHandle",
                "GetFinalPathNameByHandleW",
                "GetVolumeInformationByHandleW",
                "CloseHandle",
                "NtCreateFile",
                "NtSetInformationFile",
            },
        )

    def test_the_repr_names_the_type_only(self) -> None:
        self.assertEqual(repr(FakeWindows().access()), "OrganizeAccess()")


def _volume_is_ntfs(directory: Path) -> bool:
    access = load_access()
    if access is None:
        return False
    try:
        handle = access.create_file(
            str(directory),
            access_module.ROOT_ACCESS,
            access_module.OPEN_DIRECTORY_SHARE,
            access_module.ROOT_OPEN_FLAGS,
        )
    except AccessError:
        return False
    try:
        return access.filesystem_name(handle) == "NTFS"
    finally:
        access.close_handle(handle)


@unittest.skipUnless(
    sys.platform == "win32" and ctypes.sizeof(ctypes.c_void_p) == 8, WINDOWS_ONLY
)
class RealWindowsTests(unittest.TestCase):
    """The real calls, on temporary folders this test creates (read and write)."""

    def setUp(self) -> None:
        reset_pending_state()
        self.addCleanup(reset_pending_state)
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        if not _volume_is_ntfs(self.root):
            self.skipTest("The temporary folder is not on an NTFS volume.")
        self.access = load_access()
        self.opened: list[int] = []
        self.addCleanup(self._close_all)
        self.root_handle = self._track(
            self.access.create_file(
                str(self.root),
                access_module.ROOT_ACCESS,
                access_module.OPEN_DIRECTORY_SHARE,
                access_module.ROOT_OPEN_FLAGS,
            )
        )

    def _track(self, handle: int) -> int:
        self.opened.append(handle)
        return handle

    def _close_all(self) -> None:
        while self.opened:
            self.access.close_handle(self.opened.pop())

    def _open_directory(self, name: str) -> int:
        m = access_module
        handle, _ = self.access.open_relative(
            self.root_handle, name, m.OPEN_DIRECTORY_ACCESS, m.OPEN_DIRECTORY_SHARE,
            m.OPEN_DIRECTORY_DISPOSITION, m.OPEN_DIRECTORY_OPTIONS,
        )
        return self._track(handle)

    def _open_source(self, directory: int, name: str) -> int:
        m = access_module
        handle, _ = self.access.open_relative(
            directory, name, m.SOURCE_ACCESS, m.SOURCE_SHARE, m.FILE_OPEN, m.SOURCE_OPTIONS
        )
        return self._track(handle)

    def test_the_held_root_reports_its_file_system_and_final_path(self) -> None:
        self.assertEqual(self.access.filesystem_name(self.root_handle), "NTFS")
        final = self.access.final_path(self.root_handle)
        self.assertEqual(os.path.normcase(final[4:] if final.startswith("\\\\?\\") else final),
                         os.path.normcase(str(self.root)))
        info = self.access.file_info(self.root_handle)
        self.assertTrue(info.attributes & 0x10)
        self.assertFalse(info.attributes & 0x400)

    def test_a_missing_name_is_the_documented_not_found_status(self) -> None:
        m = access_module
        with self.assertRaises(AccessError) as raised:
            self.access.open_relative(
                self.root_handle, "absent.txt", m.LOOKUP_ACCESS, m.LOOKUP_SHARE,
                m.FILE_OPEN, m.LOOKUP_OPTIONS,
            )
        self.assertEqual((raised.exception.kind, raised.exception.code), ("nt", 0xC0000034))

    def test_a_creation_returns_created_and_a_second_creation_collides(self) -> None:
        m = access_module
        handle, information = self.access.open_relative(
            self.root_handle, "Docs", m.CREATE_DIRECTORY_ACCESS, m.CREATE_DIRECTORY_SHARE,
            m.CREATE_DIRECTORY_DISPOSITION, m.CREATE_DIRECTORY_OPTIONS,
        )
        self._track(handle)
        self.assertEqual(information, m.FILE_CREATED)
        self.assertTrue((self.root / "Docs").is_dir())
        with self.assertRaises(AccessError) as raised:
            self.access.open_relative(
                self.root_handle, "Docs", m.CREATE_DIRECTORY_ACCESS, m.CREATE_DIRECTORY_SHARE,
                m.CREATE_DIRECTORY_DISPOSITION, m.CREATE_DIRECTORY_OPTIONS,
            )
        self.assertEqual(raised.exception.code, 0xC0000035)

    def test_a_junction_is_seen_and_never_followed(self) -> None:
        if _winapi is None or not hasattr(_winapi, "CreateJunction"):
            self.skipTest("This Python cannot make a junction.")
        target = self.root / "target"
        target.mkdir()
        _winapi.CreateJunction(str(target), str(self.root / "link"))
        handle = self._open_directory("link")
        info = self.access.file_info(handle)
        self.assertTrue(info.attributes & 0x400)
        target_info = self.access.file_info(self._open_directory("target"))
        self.assertNotEqual((info.volume, info.index), (target_info.volume, target_info.index))

    def test_a_rename_moves_the_file_and_never_replaces_a_destination(self) -> None:
        (self.root / "A").mkdir()
        (self.root / "B").mkdir()
        (self.root / "A" / "f.txt").write_bytes(b"payload-A")
        (self.root / "B" / "taken.txt").write_bytes(b"payload-B")
        (self.root / "A" / "taken.txt").write_bytes(b"payload-A2")
        directory_a, directory_b = self._open_directory("A"), self._open_directory("B")
        source = self._open_source(directory_a, "f.txt")
        result = self.access.rename_into(source, directory_b, "f.txt")
        self.assertEqual(result.returned, 0)
        # A held source handle has DELETE access, so it is released before any read.
        self.opened.remove(source)
        self.access.close_handle(source)
        self.assertEqual((self.root / "B" / "f.txt").read_bytes(), b"payload-A")
        self.assertFalse((self.root / "A" / "f.txt").exists())
        refused = self._open_source(directory_a, "taken.txt")
        with self.assertRaises(AccessError) as raised:
            self.access.rename_into(refused, directory_b, "taken.txt")
        self.assertEqual(raised.exception.code, 0xC0000035)
        self.opened.remove(refused)
        self.access.close_handle(refused)
        self.assertEqual((self.root / "B" / "taken.txt").read_bytes(), b"payload-B")
        self.assertEqual((self.root / "A" / "taken.txt").read_bytes(), b"payload-A2")

    def test_a_name_outside_the_bmp_is_renamed_under_its_exact_name(self) -> None:
        (self.root / "A").mkdir()
        (self.root / "B").mkdir()
        (self.root / "A" / NAME_NON_BMP).write_bytes(b"non-bmp")
        directory_a, directory_b = self._open_directory("A"), self._open_directory("B")
        source = self._open_source(directory_a, NAME_NON_BMP)
        self.access.rename_into(source, directory_b, NAME_NON_BMP)
        self.assertEqual(os.listdir(self.root / "B"), [NAME_NON_BMP])

    def test_a_held_directory_resists_a_path_rename_and_removal_and_a_control_does_not(
        self,
    ) -> None:
        (self.root / "Held").mkdir()
        (self.root / "Free").mkdir()
        self._open_directory("Held")

        with self.assertRaises(OSError):
            os.rename(self.root / "Held", self.root / "Held2")
        with self.assertRaises(OSError):
            os.rmdir(self.root / "Held")
        os.rename(self.root / "Free", self.root / "Free2")  # an unheld control

        self.assertTrue((self.root / "Held").is_dir())
        self.assertTrue((self.root / "Free2").is_dir())

    def test_a_held_source_resists_rename_removal_and_any_open_that_shares_no_deletion(
        self,
    ) -> None:
        (self.root / "A").mkdir()
        source = self.root / "A" / "f.txt"
        control = self.root / "A" / "control.txt"
        source.write_bytes(b"payload")
        control.write_bytes(b"control")
        self._open_source(self._open_directory("A"), "f.txt")

        with self.assertRaises(OSError):
            os.rename(source, source.with_name("g.txt"))
        with self.assertRaises(OSError):
            os.unlink(source)
        with self.assertRaises(OSError):
            open(source, "r+b").close()
        with self.assertRaises(OSError):
            open(source, "rb").close()
        os.rename(control, control.with_name("moved.txt"))  # an unheld control

        self.assertTrue(source.exists())
        self.assertTrue((self.root / "A" / "moved.txt").exists())

    def test_a_child_process_cannot_rename_or_remove_what_is_held_either(self) -> None:
        if _winapi is None:
            self.skipTest("This Python has no Windows API module.")
        (self.root / "A").mkdir()
        source = self.root / "A" / "f.txt"
        control = self.root / "A" / "control.txt"
        source.write_bytes(b"payload")
        control.write_bytes(b"control")
        self._open_source(self._open_directory("A"), "f.txt")
        code = "import os, sys; os.rename(sys.argv[1], sys.argv[2])"

        held = subprocess.run(
            [sys.executable, "-I", "-c", code, str(source), str(source.with_name("g.txt"))],
            capture_output=True,
            check=False,
        )
        free = subprocess.run(
            [sys.executable, "-I", "-c", code, str(control), str(control.with_name("c2.txt"))],
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(held.returncode, 0)
        self.assertEqual(free.returncode, 0)
        self.assertTrue(source.exists())

    def test_a_conflicting_writer_blocks_the_source_open_and_a_share_delete_reader_does_not(
        self,
    ) -> None:
        if _winapi is None:
            self.skipTest("This Python has no Windows API module.")
        m = access_module
        (self.root / "A").mkdir()
        source = self.root / "A" / "f.txt"
        source.write_bytes(b"payload")
        directory = self._open_directory("A")

        with open(source, "r+b"):  # a writer that shares no deletion
            with self.assertRaises(AccessError) as raised:
                self.access.open_relative(
                    directory, "f.txt", m.SOURCE_ACCESS, m.SOURCE_SHARE, m.FILE_OPEN,
                    m.SOURCE_OPTIONS,
                )
        self.assertEqual(raised.exception.code, m.STATUS_SHARING_VIOLATION)

        reader = _winapi.CreateFile(str(source), 0x80000000, 7, 0, 3, 0, 0)
        try:
            self._open_source(directory, "f.txt")  # a reader that shares deletion
        finally:
            _winapi.CloseHandle(reader)

    def test_every_real_handle_closes(self) -> None:
        (self.root / "A").mkdir()
        handle = self._open_directory("A")
        self.opened.remove(handle)
        self.access.close_handle(handle)


if __name__ == "__main__":
    unittest.main()
