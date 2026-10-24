"""Stand-ins shared by the project file tests (V1-24-R1).

Nothing here calls Windows. ``FakeVolume`` is a small in-memory directory
tree and ``FakeAccess`` answers every call ``core/project_file_access.py``
offers from it, recording each call in order, every handle, search, and
descriptor it hands out, and every release, so a test can prove that each
resource was released exactly once by its own function. Any call can be made
to fail at its nth use; the placeholder mode, a sharing violation, a short
name, a placeholder that appears between a lookup and an open, and a change
during a read can each be staged. ``StubRoots`` and ``StubProtections`` stand
in for the settled roots and protections.
"""

from __future__ import annotations

import ntpath
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import PureWindowsPath

from core.project_file_access import (
    CREATE_FILE,
    ERROR_FILE_NOT_FOUND,
    ERROR_NO_MORE_FILES,
    ERROR_SHARING_VIOLATION,
    FILE_ACCESS,
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_INFORMATION,
    FILE_INFORMATION_EX,
    FINAL_PATH,
    FIND_FIRST,
    FIND_NEXT,
    PHCM_EXPOSE_PLACEHOLDERS,
    AccessError,
    AttributeTag,
    FileIdentity,
    FindEntry,
    StandardInfo,
)


# A Windows error number no check gives a meaning to.
ERROR_ACCESS_DENIED = 5

WORKSPACE_PATH = PureWindowsPath(r"C:\fake\workspace")
REPOSITORY_PATH = PureWindowsPath(r"C:\fake\repository")


def long_form(path: PureWindowsPath | str) -> str:
    """Return a fake drive path in its ``\\\\?\\`` form."""
    return f"\\\\?\\{path}"


@dataclass
class FakeNode:
    """One directory or file of a fake volume.

    ``attributes`` are what a handle reports, and ``find_attributes`` what
    enumeration reports, the same unless a test stages a difference.
    ``links`` is the hard-link count and ``data`` a file's bytes.
    """

    name: str
    directory: bool
    data: bytes = b""
    attributes: int = 0
    find_attributes: int | None = None
    reparse_tag: int = 0
    links: int = 1
    delete_pending: bool = False
    sharing_violation: bool = False
    last_write: int = 1
    index: int = 0
    short_name: str | None = None
    final_path: str | None = None
    children: dict[str, "FakeNode"] = field(default_factory=dict)

    def handle_attributes(self) -> int:
        """Return what a handle on this node reports as its attributes."""
        directory = FILE_ATTRIBUTE_DIRECTORY if self.directory else 0
        return self.attributes | directory

    def entry_attributes(self) -> int:
        """Return what enumeration reports for this node."""
        base = self.attributes if self.find_attributes is None else self.find_attributes
        directory = FILE_ATTRIBUTE_DIRECTORY if self.directory else 0
        return base | directory


class FakeVolume:
    """An in-memory tree under fake root paths, found ignoring case."""

    def __init__(self, *roots: PureWindowsPath) -> None:
        self._roots: dict[str, tuple[str, FakeNode]] = {}
        self._next_index = 1
        for root in roots or (WORKSPACE_PATH,):
            spelled = long_form(root)
            self._roots[ntpath.normcase(spelled)] = (
                spelled,
                FakeNode(root.name, True, index=self._index()),
            )

    def _index(self) -> int:
        value = self._next_index
        self._next_index += 1
        return value

    def root(self, path: PureWindowsPath = WORKSPACE_PATH) -> FakeNode:
        """Return the node of one root."""
        return self._roots[ntpath.normcase(long_form(path))][1]

    def add_directory(
        self,
        relative: str,
        root: PureWindowsPath = WORKSPACE_PATH,
        **values: object,
    ) -> FakeNode:
        """Add a directory, and every parent it needs, and return it."""
        node = self.root(root)
        for name in relative.split("/"):
            child = self.child(node, name, short=False)
            if child is None:
                child = FakeNode(name, True, index=self._index())
                node.children[name] = child
            node = child
        for key, value in values.items():
            setattr(node, key, value)
        return node

    def add_file(
        self,
        relative: str,
        data: bytes,
        root: PureWindowsPath = WORKSPACE_PATH,
        **values: object,
    ) -> FakeNode:
        """Add one file, with every parent directory it needs."""
        parent_path, _, name = relative.rpartition("/")
        parent = (
            self.add_directory(parent_path, root) if parent_path else self.root(root)
        )
        node = FakeNode(name, False, data=data, index=self._index())
        for key, value in values.items():
            setattr(node, key, value)
        parent.children[name] = node
        return node

    def find(self, path: str) -> FakeNode | None:
        """Return the node a full long path names, ignoring case, or None."""
        folded = ntpath.normcase(path)
        for root_path, (_spelled, root) in self._roots.items():
            if folded == root_path:
                return root
            prefix = f"{root_path}\\"
            if folded.startswith(prefix):
                node: FakeNode | None = root
                for name in path[len(prefix) :].split("\\"):
                    node = None if node is None else self.child(node, name, short=False)
                return node
        return None

    def full_path(self, node: FakeNode) -> str:
        """Return a node's full long path as its own names spell it."""
        for spelled, root in self._roots.values():
            names = self._names_to(root, node, [])
            if names is not None:
                return "\\".join([spelled, *names])
        raise LookupError("The node is not on this volume.")

    def _names_to(
        self,
        current: FakeNode,
        node: FakeNode,
        names: list[str],
    ) -> list[str] | None:
        if current is node:
            return names
        for child in current.children.values():
            found = self._names_to(child, node, [*names, child.name])
            if found is not None:
                return found
        return None

    @staticmethod
    def child(node: FakeNode, name: str, *, short: bool = True) -> FakeNode | None:
        """Return a child by name ignoring case, or, when asked, by short name."""
        folded = ntpath.normcase(name)
        for child in node.children.values():
            if ntpath.normcase(child.name) == folded:
                return child
            if short and child.short_name is not None and (
                ntpath.normcase(child.short_name) == folded
            ):
                return child
        return None


@dataclass
class _Search:
    entries: list[FindEntry]
    position: int = 0


class FakeAccess:
    """Answer every project file call from a fake volume, recording it all.

    ``fail`` maps a method name to the call numbers, counted from 1, at which
    it raises ``AccessError`` with ``ERROR_ACCESS_DENIED``, or ``OSError``
    for a descriptor step. ``previous_mode`` is what setting the placeholder
    mode returns, ``set_result`` replaces it, and ``restore_result`` is what
    the restore returns, by default the exposed mode, a restore that worked.
    ``read_hook`` is called after each chunk is read, so a test can change
    the file mid-read.
    """

    DESCRIPTOR_STEPS = frozenset(
        {
            "open_descriptor",
            "set_binary",
            "inheritable",
            "read",
            "descriptor_handle",
            "close_descriptor",
        }
    )

    def __init__(
        self,
        volume: FakeVolume,
        *,
        fail: dict[str, Iterable[int]] | None = None,
        previous_mode: int = 0,
        set_result: int | None = None,
        restore_result: int = PHCM_EXPOSE_PLACEHOLDERS,
        inheritable: bool = False,
        read_hook: Callable[[FakeNode, int], None] | None = None,
        chunk: int = 4096,
        empty_listing_error: int = ERROR_FILE_NOT_FOUND,
    ) -> None:
        self.volume = volume
        self.fail = {name: set(numbers) for name, numbers in (fail or {}).items()}
        self.previous_mode = previous_mode
        self.set_result = set_result
        self.restore_result = restore_result
        self.inheritable_value = inheritable
        self.read_hook = read_hook
        self.chunk = chunk
        self.empty_listing_error = empty_listing_error
        self.calls: list[str] = []
        self.counts: dict[str, int] = {}
        self.modes: list[int] = []
        self.handles: dict[int, FakeNode] = {}
        self.searches: dict[int, _Search] = {}
        self.descriptors: dict[int, list[object]] = {}
        self.query_handles: dict[int, FakeNode] = {}
        self.released: list[tuple[str, int]] = []
        self.opened_paths: list[str] = []
        self._next = 100

    def __repr__(self) -> str:
        return "FakeAccess()"

    def leaked(self) -> tuple[set[int], set[int], set[int]]:
        """Return the handles, searches, and descriptors still open."""
        return set(self.handles), set(self.searches), set(self.descriptors)

    def _enter(self, name: str) -> None:
        """Record one call, and raise when a test asked this one to fail."""
        self.calls.append(name)
        number = self.counts.get(name, 0) + 1
        self.counts[name] = number
        if number in self.fail.get(name, ()):
            if name in self.DESCRIPTOR_STEPS:
                raise OSError("injected")
            raise AccessError(name, ERROR_ACCESS_DENIED)

    def _new(self) -> int:
        self._next += 1
        return self._next

    def set_placeholder_mode(self, mode: int) -> int:
        """Return the previous mode on setting, and the restore result after."""
        self.modes.append(mode)
        self._enter("set_placeholder_mode")
        if len(self.modes) % 2 == 1:
            return self.previous_mode if self.set_result is None else self.set_result
        return self.restore_result

    def create_file(self, path: str, access: int, share: int, flags: int) -> int:
        """Open one fake path, as CreateFileW would."""
        self._enter("create_file")
        self.opened_paths.append(path)
        node = self.volume.find(path)
        if node is None:
            raise AccessError(CREATE_FILE, ERROR_FILE_NOT_FOUND)
        if node.sharing_violation and access == FILE_ACCESS:
            raise AccessError(CREATE_FILE, ERROR_SHARING_VIOLATION)
        handle = self._new()
        self.handles[handle] = node
        return handle

    def attribute_tag(self, handle: int) -> AttributeTag:
        """Return a handle's attributes and reparse tag."""
        self._enter("attribute_tag")
        node = self._node(handle, FILE_INFORMATION_EX)
        return AttributeTag(node.handle_attributes(), node.reparse_tag)

    def standard_info(self, handle: int) -> StandardInfo:
        """Return a handle's size, links, and flags."""
        self._enter("standard_info")
        node = self._node(handle, FILE_INFORMATION_EX)
        return StandardInfo(
            len(node.data),
            node.links,
            node.delete_pending,
            node.directory,
        )

    def file_identity(self, handle: int) -> FileIdentity:
        """Return a handle's identity."""
        self._enter("file_identity")
        node = self._node(handle, FILE_INFORMATION)
        return FileIdentity(7, node.index, node.links, len(node.data), node.last_write)

    def final_path(self, handle: int) -> tuple[int, str]:
        """Return a handle's final path as its own names spell it."""
        self._enter("final_path")
        node = self._node(handle, FINAL_PATH)
        final = node.final_path or self.volume.full_path(node)
        return len(final), final

    def find_first(self, pattern: str) -> tuple[int, FindEntry]:
        """Look one name up exactly, or start a listing for ``*``."""
        self._enter("find_first")
        parent_path, _, name = pattern.rpartition("\\")
        parent = self.volume.find(parent_path)
        if name == "*":
            if parent is None or not parent.directory:
                raise AccessError(FIND_FIRST, ERROR_FILE_NOT_FOUND)
            if not parent.children and self.empty_listing_error:
                raise AccessError(FIND_FIRST, self.empty_listing_error)
            entries = [
                FindEntry(special, FILE_ATTRIBUTE_DIRECTORY, 0, 0)
                for special in (".", "..")
            ] + [self._entry(child) for child in parent.children.values()]
        else:
            node = None if parent is None else self.volume.child(parent, name)
            if node is None:
                raise AccessError(FIND_FIRST, ERROR_FILE_NOT_FOUND)
            entries = [self._entry(node)]
        search = self._new()
        self.searches[search] = _Search(entries, 1)
        return search, entries[0]

    def find_next(self, search: int) -> FindEntry:
        """Return a listing's next entry, or its normal end."""
        self._enter("find_next")
        state = self.searches.get(search)
        if state is None:
            raise AccessError(FIND_NEXT, ERROR_ACCESS_DENIED)
        if state.position >= len(state.entries):
            raise AccessError(FIND_NEXT, ERROR_NO_MORE_FILES)
        entry = state.entries[state.position]
        state.position += 1
        return entry

    def find_close(self, search: int) -> None:
        """Close one search exactly once."""
        self._enter("find_close")
        if search not in self.searches:
            raise AssertionError("A search was closed twice or never opened.")
        del self.searches[search]
        self.released.append(("search", search))

    def close_handle(self, handle: int) -> None:
        """Close one handle exactly once."""
        self._enter("close_handle")
        if handle not in self.handles:
            raise AssertionError("A handle was closed twice or never opened.")
        del self.handles[handle]
        self.released.append(("handle", handle))

    def open_descriptor(self, handle: int) -> int:
        """Return a descriptor that now owns ``handle``."""
        self._enter("open_descriptor")
        if handle not in self.handles:
            raise AssertionError("A descriptor was asked for a closed handle.")
        descriptor = self._new()
        self.descriptors[descriptor] = [handle, 0, self.handles.pop(handle)]
        return descriptor

    def set_binary(self, descriptor: int) -> None:
        """Accept the binary-mode step."""
        self._enter("set_binary")

    def inheritable(self, descriptor: int) -> bool:
        """Return whether a child could inherit the descriptor."""
        self._enter("inheritable")
        return self.inheritable_value

    def read(self, descriptor: int, size: int) -> bytes:
        """Return the next chunk of the file, at most ``size`` bytes."""
        self._enter("read")
        state = self.descriptors[descriptor]
        position, node = state[1], state[2]
        chunk = node.data[position : position + min(size, self.chunk)]
        state[1] = position + len(chunk)
        if self.read_hook is not None:
            self.read_hook(node, state[1])
        return chunk

    def descriptor_handle(self, descriptor: int) -> int:
        """Return the handle a descriptor owns, for queries."""
        self._enter("descriptor_handle")
        handle, _position, node = self.descriptors[descriptor]
        self.query_handles[handle] = node
        return handle

    def close_descriptor(self, descriptor: int) -> None:
        """Close a descriptor and the handle it owns, exactly once."""
        self._enter("close_descriptor")
        if descriptor not in self.descriptors:
            raise AssertionError("A descriptor was closed twice or never opened.")
        handle = self.descriptors.pop(descriptor)[0]
        self.query_handles.pop(handle, None)
        self.released.append(("descriptor", descriptor))

    def _node(self, handle: int, call: str) -> FakeNode:
        node = self.handles.get(handle) or self.query_handles.get(handle)
        if node is None:
            raise AccessError(call, ERROR_ACCESS_DENIED)
        return node

    @staticmethod
    def _entry(node: FakeNode) -> FindEntry:
        return FindEntry(
            node.name,
            node.entry_attributes(),
            node.reparse_tag,
            0 if node.directory else len(node.data),
        )


class StubRoot:
    """A settled root: its name and its canonical path."""

    def __init__(self, name: str, path: PureWindowsPath) -> None:
        self.name = name
        self.path = path


class StubRoots:
    """Stand in for the settled roots, by name."""

    def __init__(
        self,
        workspace: PureWindowsPath = WORKSPACE_PATH,
        repository: PureWindowsPath = REPOSITORY_PATH,
    ) -> None:
        self._roots = {
            "workspace": StubRoot("workspace", workspace),
            "project_maria": StubRoot("project_maria", repository),
        }

    def by_name(self, name: str) -> StubRoot | None:
        """Return one root, or None."""
        return self._roots.get(name)


class StubProtections:
    """Stand in for the settled protections: exclude what a test names.

    ``names`` are final components, compared ignoring case, that make a path
    excluded at any prefix; every path asked about is recorded.
    """

    def __init__(self, *names: str) -> None:
        self.names = {name.casefold() for name in names}
        self.asked: list[PureWindowsPath] = []

    def excludes(self, *paths: PureWindowsPath) -> bool:
        """Return whether any path ends in an excluded name."""
        self.asked.extend(paths)
        return any(path.name.casefold() in self.names for path in paths)
