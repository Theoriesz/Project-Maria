"""Test doubles for file organization (V1-25-R1).

``FakeWindows`` stands in for kernel32 and ntdll at the ctypes boundary, so the
access module's marshalling is tested without a real call. ``FakeVolume`` is an
in-memory volume that implements the access module's methods, so the handle
logic is tested against races and failures that no real call can be made to
produce on demand: a destination that appears, a failed or interrupted close, a
completion that is only pending, a link count that changes. It can fail or
interrupt each call in turn.
"""

from __future__ import annotations

import ctypes
import itertools
from collections import Counter
from types import SimpleNamespace

from core import organize_access as access_module
from core.organize_access import AccessError, FileInfo, OrganizeAccess, SetResult

ROOT_PATH = "C:\\fake\\root"
NON_BMP = chr(0x1F600)
NAME_NON_BMP = "a" + NON_BMP + ".txt"


def address_of(value: object) -> int | None:
    """Return a ctypes object's address, or None for anything else."""
    try:
        return ctypes.addressof(value)
    except TypeError:
        return None


class Function:
    """A callable that, like a ctypes function, accepts pinned argument types."""

    def __init__(self, call) -> None:
        self._call = call
        self.argtypes = None
        self.restype = None

    def __call__(self, *arguments: object) -> object:
        return self._call(*arguments)


class FakeWindows:
    """kernel32 and ntdll as plain functions, recording what they were given."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.create_result = 0x77
        self.open_status = 0
        self.open_handle = 0x1234
        self.open_information = 1
        self.set_status = 0
        self.set_block_status = 0
        self.set_information = 0
        self.close_ok = 1
        self.error = 0
        self.filesystem = "NTFS"
        self.filesystem_ok = 1
        self.seen: dict[str, dict] = {}

    def library(self) -> tuple[SimpleNamespace, SimpleNamespace]:
        """Return the two libraries an ``OrganizeAccess`` is built from."""
        kernel = SimpleNamespace(
            CreateFileW=Function(self.create_file),
            CloseHandle=Function(self.close_handle),
            GetFileInformationByHandle=Function(self.file_information),
            GetFinalPathNameByHandleW=Function(self.final_path),
            GetVolumeInformationByHandleW=Function(self.volume_information),
        )
        ntdll = SimpleNamespace(
            NtCreateFile=Function(self.nt_create_file),
            NtSetInformationFile=Function(self.nt_set_information_file),
        )
        return kernel, ntdll

    def access(self) -> OrganizeAccess:
        """Return an access over these fakes."""
        kernel, ntdll = self.library()
        return OrganizeAccess(kernel, ntdll, lambda: self.error)

    def create_file(self, *arguments: object) -> int:
        self.calls.append(("CreateFileW", arguments))
        return self.create_result

    def close_handle(self, handle: int) -> int:
        self.calls.append(("CloseHandle", handle))
        return self.close_ok

    def file_information(self, handle: int, reference: object) -> int:
        data = reference._obj
        data.attributes = 0x20
        data.volume_serial = 9
        data.index_high, data.index_low = 1, 2
        data.links = 1
        data.size_high, data.size_low = 0, 77
        data.last_write.high, data.last_write.low = 5, 6
        return 1

    def final_path(self, handle: int, buffer: object, size: int, flags: int) -> int:
        text = "\\\\?\\C:\\fake"
        buffer.value = text
        return len(text)

    def volume_information(
        self,
        handle: int,
        volume_name: object,
        volume_size: int,
        serial: object,
        component: object,
        flags: object,
        buffer: object,
        size: int,
    ) -> int:
        self.calls.append(
            (
                "Volume",
                {
                    "handle": handle,
                    "volume_name": volume_name,
                    "volume_size": volume_size,
                    "serial": serial,
                    "component": component,
                    "flags": flags,
                    "size": size,
                },
            )
        )
        if self.filesystem_ok:
            buffer.value = self.filesystem
        return self.filesystem_ok

    def nt_create_file(
        self,
        handle_reference: object,
        access: int,
        attributes_reference: object,
        block_reference: object,
        allocation: object,
        file_attributes: int,
        share: int,
        disposition: int,
        options: int,
        extended: object,
        extended_length: int,
    ) -> int:
        attributes = attributes_reference._obj
        name = attributes.object_name.contents
        self.calls.append(
            (
                "NtCreateFile",
                {
                    "access": access,
                    "share": share,
                    "disposition": disposition,
                    "options": options,
                    "attributes": attributes.attributes,
                    "directory": attributes.root_directory,
                    "attributes_length": attributes.length,
                    "length": name.length,
                    "maximum": name.maximum_length,
                    "name": ctypes.string_at(name.buffer, name.length),
                    "terminator": ctypes.string_at(name.buffer + name.length, 2),
                },
            )
        )
        self.seen["create"] = {
            "handle": id(handle_reference._obj),
            "block": id(block_reference._obj),
            "attributes": id(attributes),
            "buffer": name.buffer,
        }
        handle_reference._obj.value = self.open_handle
        block_reference._obj.information = self.open_information
        return self.open_status

    def nt_set_information_file(
        self,
        handle: int,
        block_reference: object,
        buffer: object,
        length: int,
        information_class: int,
    ) -> int:
        self.calls.append(
            (
                "NtSetInformationFile",
                {
                    "handle": handle,
                    "class": information_class,
                    "length": length,
                    "raw": bytes(buffer.raw),
                    "storage": ctypes.sizeof(buffer),
                },
            )
        )
        self.seen["set"] = {
            "block": id(block_reference._obj),
            "buffer": address_of(buffer),
        }
        block_reference._obj.status = self.set_block_status
        block_reference._obj.information = self.set_information
        return self.set_status

    def last(self) -> dict:
        """Return the arguments of the most recent native call."""
        return self.calls[-1][1]


class FakeEntry:
    """One directory entry, as ``os.scandir`` yields it: only a name is used."""

    def __init__(self, name: str) -> None:
        self.name = name


class FakeListing:
    """A scandir result: a context manager that yields entries and counts them."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.yielded = 0

    def __enter__(self) -> "FakeListing":
        return self

    def __exit__(self, *exception: object) -> bool:
        return False

    def __iter__(self):
        for name in self.names:
            self.yielded += 1
            yield FakeEntry(name)


def fake_scandir(node: "Node", extra: list[str] | None = None):
    """Return a scandir stand-in listing a fake folder's names, and ``extra``.

    The listings it handed out are kept on ``scandir.listings``, so a test can
    see how many entries a caller read.
    """
    listings: list[FakeListing] = []

    def scandir(path: str) -> FakeListing:
        listing = FakeListing([child.name for child in node.children.values()] + (extra or []))
        listings.append(listing)
        return listing

    scandir.listings = listings
    return scandir


class Node:
    """One file or directory of a fake volume."""

    _indexes = itertools.count(1000)

    def __init__(self, name: str, kind: str, parent: "Node | None" = None) -> None:
        self.name, self.kind, self.parent = name, kind, parent
        self.children: dict[str, Node] = {}
        self.index = next(Node._indexes)
        self.links, self.size, self.last_write = 1, 5, 123456
        self.reparse, self.hidden, self.unreadable = False, False, False
        self.volume = 7
        self.final_name: str | None = None

    @property
    def attributes(self) -> int:
        value = 0x10 if self.kind == "dir" else 0x20
        if self.reparse:
            value |= 0x400
        if self.hidden:
            value |= 0x2
        return value


class FakeVolume:
    """An in-memory volume with the access module's methods and failure hooks."""

    def __init__(self, root_path: str = ROOT_PATH) -> None:
        self.root_path = root_path
        self.root = Node("root", "dir")
        self.handles: dict[int, Node] = {}
        self.purpose: dict[int, str] = {}
        self.counts: Counter = Counter()
        self.close_calls: Counter = Counter()
        self.opened_names: list[str] = []
        self.hooks: dict[str, list] = {}
        self.failures: dict[tuple[str, int], BaseException] = {}
        self.interruptions: dict[int, BaseException] = {}  # close call number
        self.locked: set[int] = set()
        self.close_fails: set[int] = set()  # node ids whose close reports failure
        self.close_fail_purposes: set[str] = set()
        self.filesystem = "NTFS"
        self.filesystem_error = False
        self.filesystem_calls = 0
        self.renamed = False
        self.rename_effect_then_raise = False
        self.pending_on_rename = False
        self.interrupt_on_rename = False
        self.fail_lookups_after_rename = False
        self.after_info: str | None = None
        self._handles = itertools.count(100, 4)

    # -- building -------------------------------------------------------------
    def mkdir(self, parent: Node, name: str) -> Node:
        node = Node(name, "dir", parent)
        parent.children[name.casefold()] = node
        return node

    def mkfile(self, parent: Node, name: str) -> Node:
        node = Node(name, "file", parent)
        parent.children[name.casefold()] = node
        return node

    def listing(self, node: Node) -> list[str]:
        return sorted(child.name for child in node.children.values())

    # -- internals ------------------------------------------------------------
    def _hold(self, node: Node, purpose: str = "other") -> int:
        handle = next(self._handles)
        self.handles[handle] = node
        self.purpose[handle] = purpose
        return handle

    def _path(self, node: Node) -> str:
        names = []
        while node is not self.root:
            names.append(node.final_name or node.name)
            node = node.parent
        return "\\\\?\\" + self.root_path + "".join("\\" + name for name in reversed(names))

    def _tick(self, call: str, **detail: object) -> None:
        self.counts[call] += 1
        for hook in self.hooks.get(call, ()):
            hook(self, detail)
        error = self.failures.get((call, self.counts[call]))
        if error is not None:
            raise error

    # -- the access module's methods -------------------------------------------
    def create_file(self, path: str, access: int, share: int, flags: int) -> int:
        self._tick("create_file")
        if path.casefold().rstrip("\\") != self.root_path.casefold():
            raise AccessError("CreateFileW", 3, "win32")
        return self._hold(self.root)

    def open_relative(
        self,
        directory: int,
        name: str,
        access: int,
        share: int,
        disposition: int,
        options: int,
    ) -> tuple[int, int]:
        if options & access_module.FILE_DIRECTORY_FILE and (
            options & ~access_module.DIRECTORY_FILE_COMPATIBLE_OPTIONS
        ):
            raise access_module.OptionsError()
        access_module.utf16_bytes(name)
        self.opened_names.append(name)
        self._tick("open_relative", name=name, access=access, disposition=disposition)
        if (
            self.renamed
            and self.fail_lookups_after_rename
            and access == access_module.LOOKUP_ACCESS
        ):
            raise AccessError("NtCreateFile", 0xC0000022, "nt")
        parent = self.handles[directory]
        child = parent.children.get(name.casefold())
        purpose = "lookup" if access == access_module.LOOKUP_ACCESS else "other"
        if disposition == access_module.FILE_CREATE:
            if child is not None:
                raise AccessError(
                    "NtCreateFile", access_module.STATUS_OBJECT_NAME_COLLISION, "nt"
                )
            node = self.mkdir(parent, name)
            return self._hold(node, purpose), access_module.FILE_CREATED
        if child is None:
            raise AccessError(
                "NtCreateFile", access_module.STATUS_OBJECT_NAME_NOT_FOUND, "nt"
            )
        if child.unreadable and access == access_module.LOOKUP_ACCESS:
            raise AccessError("NtCreateFile", 0xC0000022, "nt")
        if access & access_module.ACCESS_DELETE and id(child) in self.locked:
            raise AccessError(
                "NtCreateFile", access_module.STATUS_SHARING_VIOLATION, "nt"
            )
        if options & access_module.FILE_NON_DIRECTORY_FILE and child.kind == "dir":
            raise AccessError("NtCreateFile", 0xC00000BA, "nt")
        return self._hold(child, purpose), 1

    def rename_into(self, handle: int, directory: int, name: str) -> SetResult:
        access_module.utf16_bytes(name)
        self._tick("rename", name=name)
        if self.interrupt_on_rename:
            raise KeyboardInterrupt
        if self.pending_on_rename:
            raise access_module.PendingError("NtSetInformationFile", 0x103, "nt")
        node, target = self.handles[handle], self.handles[directory]
        if name.casefold() in target.children:
            raise AccessError(
                "NtSetInformationFile", access_module.STATUS_OBJECT_NAME_COLLISION, "nt"
            )
        del node.parent.children[node.name.casefold()]
        node.parent, node.name = target, name
        target.children[name.casefold()] = node
        self.renamed = True
        if self.rename_effect_then_raise:
            raise AccessError("NtSetInformationFile", 0xC0000001, "nt")
        return SetResult(0, 0, 0, 0)

    def file_info(self, handle: int) -> FileInfo:
        self._tick("info", handle=handle)
        node = self.handles[handle]
        links, index = node.links, node.index
        if (
            self.renamed
            and self.after_info
            and self.purpose.get(handle) == "other"
            and node.kind == "file"
        ):
            if self.after_info == "raise":
                raise AccessError("GetFileInformationByHandle", 5, "win32")
            if self.after_info == "zero":
                links = 0
            elif self.after_info == "huge":
                links = 5000
            elif self.after_info == "identity":
                index += 1
        return FileInfo(node.attributes, node.volume, index, links, node.size, node.last_write)

    def final_path(self, handle: int) -> str:
        return self._path(self.handles[handle])

    def filesystem_name(self, handle: int) -> str:
        self.filesystem_calls += 1
        if self.filesystem_error:
            raise AccessError("GetVolumeInformationByHandleW", 5, "win32")
        return self.filesystem

    def close_handle(self, handle: int) -> None:
        self._tick("close")
        self.close_calls[handle] += 1
        node = self.handles.pop(handle)
        raised = self.interruptions.get(self.counts["close"])
        if raised is not None:
            raise raised
        if id(node) in self.close_fails or self.purpose[handle] in self.close_fail_purposes:
            raise AccessError("CloseHandle", 6, "win32")
