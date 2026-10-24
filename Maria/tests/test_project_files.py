"""Tests for the project file reader of V1-24-R1, through a fake volume.

Every Windows call goes to ``tests.project_fakes.FakeAccess``, so these run
anywhere and can make any call fail. They prove the lexical phase makes no
call at all; that each component is looked up, opened, and checked on its
own handle; that a hard link and an oversized file are refused before any
data is read; that the placeholder mode is set once and restored once, and a
detected restore failure stops every later operation; that every handle,
search, and descriptor is released exactly once, by its own function, with
each call made to fail in turn; that the normal ends of a listing are not
failures; and how bytes are decoded, shown, and turned into model parts.
Real Windows calls are tested in ``tests.test_project_file_access``.
"""

from __future__ import annotations

import hashlib
import logging
import unittest
from unittest.mock import Mock, patch

import core.project_files as project_files
from core.project_file_access import (
    FILE_ATTRIBUTE_OFFLINE,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    FILE_ATTRIBUTE_REPARSE_POINT,
    PHCM_EXPOSE_PLACEHOLDERS,
)
from core.project_files import (
    DecodedFile,
    FileContent,
    FileRefusal,
    FolderListing,
    ProjectFileReader,
    decode_file,
    file_display,
    file_parts,
    refusal_message,
    refusal_reason,
    scan_references,
)
from core.project_records import (
    FILE_PART_MAX_BYTES,
    FILE_SHOW_MAX_BYTES,
    LINE_RANGE_REASON,
    REFERENCE_COUNT_REASON,
    REFERENCE_FORM_REASON,
    REFERENCE_PART_TOO_LARGE_REASON,
    REFERENCE_UNREADABLE_REASON,
    Project,
)
from tests.project_fakes import (
    REPOSITORY_PATH,
    WORKSPACE_PATH,
    FakeAccess,
    FakeVolume,
    StubProtections,
    StubRoots,
    long_form,
)


STAMP = "2026-10-09T21:24:13Z"
GARDEN = Project(1, "Garden", "workspace", "garden", STAMP, STAMP)
TOP = Project(2, "Everything", "workspace", "", STAMP, STAMP)
MARIA = Project(3, "Self review", "project_maria", "", STAMP, STAMP)
PLAN = b"first line\r\nsecond line\nthird\n"


def _quiet() -> logging.Logger:
    """Return a logger that records and prints nothing."""
    return Mock(spec=logging.Logger)


def _garden_volume() -> FakeVolume:
    """Return a workspace holding one project folder with a few files."""
    volume = FakeVolume(WORKSPACE_PATH, REPOSITORY_PATH)
    volume.add_file("garden/plan.md", PLAN)
    volume.add_file("garden/notes/beds.txt", b"tomatoes\n")
    volume.add_file("garden/photo.jpg", b"\xff\xd8")
    volume.add_directory("garden/empty")
    return volume


class _ReaderCase(unittest.TestCase):
    """A reader over a fake volume, with a fresh process-wide latch."""

    def setUp(self) -> None:
        latch = patch.object(project_files, "_LATCH", project_files._RestoreLatch())
        latch.start()
        self.addCleanup(latch.stop)
        self.volume = _garden_volume()
        self.protections = StubProtections()

    def reader(self, access: FakeAccess | None) -> ProjectFileReader:
        """Return a reader whose Windows functions are ``access``."""
        return ProjectFileReader(
            StubRoots(),
            self.protections,
            _quiet(),
            access_loader=lambda: access,
        )

    def access(self, **options: object) -> FakeAccess:
        """Return a fake over this test's volume."""
        return FakeAccess(self.volume, **options)

    def assert_all_released(self, access: FakeAccess) -> None:
        """Assert nothing the operation entered is still open."""
        self.assertEqual(access.leaked(), (set(), set(), set()))


class LexicalPhaseTests(_ReaderCase):
    """A path the lexical phase refuses makes no Windows call at all."""

    def test_a_path_outside_the_grammar_makes_no_call(self) -> None:
        access = self.access()
        reader = self.reader(access)

        for path in ("../secret.md", ".env", "a\\b.md", "C:x.md", "x~.md", "CON.md"):
            with self.subTest(path=path):
                self.assertIs(reader.read_file(GARDEN, path), FileRefusal.UNREADABLE)
        self.assertEqual(access.calls, [])

    def test_a_workspace_file_without_an_admitted_type_makes_no_call(self) -> None:
        access = self.access()

        self.assertIs(
            self.reader(access).read_file(GARDEN, "photo.jpg"),
            FileRefusal.UNREADABLE,
        )
        self.assertEqual(access.calls, [])

    def test_an_excluded_prefix_makes_no_call(self) -> None:
        self.protections = StubProtections("notes")
        access = self.access()

        self.assertIs(
            self.reader(access).read_file(GARDEN, "notes/beds.txt"),
            FileRefusal.UNREADABLE,
        )
        self.assertEqual(access.calls, [])
        asked = [str(path) for path in self.protections.asked]
        self.assertEqual(
            asked,
            [
                str(WORKSPACE_PATH),
                str(WORKSPACE_PATH / "garden"),
                str(WORKSPACE_PATH / "garden" / "notes"),
                str(WORKSPACE_PATH / "garden" / "notes" / "beds.txt"),
            ],
        )

    def test_a_project_maria_path_must_be_a_member_and_takes_its_spelling(
        self,
    ) -> None:
        self.volume.add_file("Maria/core/brain.py", b"x = 1\n", REPOSITORY_PATH)
        self.volume.add_file("Maria/core/secret.py", b"x = 2\n", REPOSITORY_PATH)
        access = self.access()
        reader = self.reader(access)

        self.assertIs(
            reader.read_file(MARIA, "Maria/core/secret.py"),
            FileRefusal.UNREADABLE,
        )
        self.assertEqual(access.calls, [])
        content = reader.read_file(MARIA, "MARIA/CORE/BRAIN.PY")
        self.assertIs(type(content), FileContent)
        self.assertEqual(content.path, "Maria/core/brain.py")

    def test_a_member_at_a_configured_runtime_location_is_refused_unopened(
        self,
    ) -> None:
        # A member is still refused when configuration puts the database, a
        # log, or the prompt at its place: the run-time exclusions apply to
        # members too.
        self.volume.add_file("Maria/README.md", b"# Maria\n", REPOSITORY_PATH)
        self.protections = StubProtections("README.md")
        access = self.access()

        self.assertIs(
            self.reader(access).read_file(MARIA, "Maria/README.md"),
            FileRefusal.UNREADABLE,
        )
        self.assertEqual(access.calls, [])

    def test_unavailable_and_stopped_come_before_any_other_answer(self) -> None:
        reader = self.reader(None)

        self.assertIs(reader.read_file(GARDEN, "../x"), FileRefusal.UNAVAILABLE)
        self.assertIs(reader.list_folder(GARDEN, ""), FileRefusal.UNAVAILABLE)
        self.assertIs(reader.check_workspace_folder("x"), FileRefusal.UNAVAILABLE)
        project_files._LATCH.tripped = True
        access = self.access()
        self.assertIs(
            self.reader(access).read_file(GARDEN, "plan.md"),
            FileRefusal.STOPPED,
        )
        self.assertEqual(access.calls, [])


class FileReadTests(_ReaderCase):
    """A file is read whole, behind every check, through one descriptor."""

    def test_a_read_returns_exactly_the_bytes_and_their_identity(self) -> None:
        access = self.access(chunk=7)

        content = self.reader(access).read_file(GARDEN, "PLAN.MD")

        self.assertEqual(content.path, "plan.md")
        self.assertEqual(content.data, PLAN)
        self.assertEqual(content.size, len(PLAN))
        self.assertEqual(content.sha256, hashlib.sha256(PLAN).hexdigest())
        self.assertEqual(access.modes, [PHCM_EXPOSE_PLACEHOLDERS, 0])
        self.assert_all_released(access)

    def test_the_walk_holds_each_component_and_releases_newest_first(self) -> None:
        access = self.access()

        self.reader(access).read_file(GARDEN, "notes/beds.txt")

        self.assertEqual(access.calls[0], "set_placeholder_mode")
        self.assertEqual(access.calls[-1], "set_placeholder_mode")
        opened = [path.split("\\")[-1] for path in access.opened_paths]
        self.assertEqual(opened, ["workspace", "garden", "notes", "beds.txt"])
        # Each lookup's search is released at once; the held handles and the
        # descriptor are released newest first, the descriptor first.
        releases = [kind for kind, _ in access.released]
        self.assertEqual(
            releases,
            ["search", "search", "search", "descriptor", "handle", "handle", "handle"],
        )
        self.assert_all_released(access)

    def test_the_descriptor_is_binary_and_not_inheritable_before_any_read(
        self,
    ) -> None:
        access = self.access()

        self.reader(access).read_file(GARDEN, "plan.md")

        first_read = access.calls.index("read")
        self.assertLess(access.calls.index("set_binary"), first_read)
        self.assertLess(access.calls.index("inheritable"), first_read)
        self.assertLess(access.calls.index("open_descriptor"), first_read)

    def test_an_interrupt_during_the_transfer_releases_nothing_twice(self) -> None:
        access = self.access()
        transfer = Mock(side_effect=KeyboardInterrupt)
        access.open_descriptor = transfer

        with self.assertRaises(KeyboardInterrupt):
            self.reader(access).read_file(GARDEN, "plan.md")

        # The file's handle was in transfer, so nothing releases it: it is the
        # stated residual, left open until the process ends. Everything else
        # was released once, and the mode's restore was still attempted.
        handles, searches, descriptors = access.leaked()
        self.assertEqual(len(handles), 1)
        self.assertEqual((searches, descriptors), (set(), set()))
        self.assertEqual(len(access.released), len(set(access.released)))
        self.assertEqual(access.modes, [PHCM_EXPOSE_PLACEHOLDERS, 0])
        transfer.assert_called_once()

    def test_an_inheritable_descriptor_reads_nothing(self) -> None:
        access = self.access(inheritable=True)

        self.assertIs(
            self.reader(access).read_file(GARDEN, "plan.md"),
            FileRefusal.INCOMPLETE,
        )
        self.assertNotIn("read", access.calls)
        self.assert_all_released(access)

    def test_a_hard_link_is_refused_on_its_handle_before_any_data_is_read(
        self,
    ) -> None:
        self.volume.find(long_form(WORKSPACE_PATH) + "\\garden\\plan.md").links = 2
        access = self.access()

        self.assertIs(
            self.reader(access).read_file(GARDEN, "plan.md"),
            FileRefusal.UNREADABLE,
        )
        self.assertIn("standard_info", access.calls)
        for step in ("open_descriptor", "read", "file_identity"):
            self.assertNotIn(step, access.calls)
        self.assert_all_released(access)

    def test_a_file_over_the_open_limit_is_refused_before_reading(self) -> None:
        self.volume.add_file("garden/big.txt", b"x" * 524_289)
        self.volume.add_file("garden/fits.txt", b"x" * 524_288)
        access = self.access(chunk=65_536)
        reader = self.reader(access)

        self.assertIs(reader.read_file(GARDEN, "big.txt"), FileRefusal.TOO_LARGE)
        self.assertNotIn("read", access.calls)
        self.assertEqual(len(reader.read_file(GARDEN, "fits.txt").data), 524_288)
        self.assert_all_released(access)

    def test_a_file_open_for_changes_is_refused_with_its_own_text(self) -> None:
        self.volume.find(
            long_form(WORKSPACE_PATH) + "\\garden\\plan.md"
        ).sharing_violation = True
        access = self.access()

        refusal = self.reader(access).read_file(GARDEN, "plan.md")

        self.assertIs(refusal, FileRefusal.OPEN_FOR_CHANGES)
        self.assertEqual(
            refusal_message(refusal),
            "That file is open for changes in another program. Close it and try "
            "again.",
        )
        self.assert_all_released(access)

    def test_an_absent_file_and_a_protected_one_read_the_same(self) -> None:
        self.protections = StubProtections("secret.md")
        self.volume.add_file("garden/secret.md", b"hidden\n")
        reader = self.reader(self.access())

        absent = reader.read_file(GARDEN, "missing.md")
        protected = reader.read_file(GARDEN, "secret.md")

        self.assertIs(absent, FileRefusal.UNREADABLE)
        self.assertIs(protected, FileRefusal.UNREADABLE)
        self.assertEqual(refusal_message(absent), refusal_message(protected))

    def test_a_change_during_the_read_withholds_everything(self) -> None:
        def touch(node: object, position: int) -> None:
            node.last_write += 1

        access = self.access(read_hook=touch, chunk=4)

        self.assertIs(
            self.reader(access).read_file(GARDEN, "plan.md"),
            FileRefusal.CHANGED,
        )
        self.assert_all_released(access)

    def test_a_file_that_grows_during_the_read_is_refused(self) -> None:
        def grow(node: object, position: int) -> None:
            if position and not getattr(node, "grown", False):
                node.grown = True
                node.data = node.data + b"more"

        access = self.access(read_hook=grow, chunk=4)

        self.assertIs(
            self.reader(access).read_file(GARDEN, "plan.md"),
            FileRefusal.CHANGED,
        )

    def test_a_folder_renamed_above_after_its_check_is_caught_after_the_read(
        self,
    ) -> None:
        folder = self.volume.find(long_form(WORKSPACE_PATH) + "\\garden")

        def move(node: object, position: int) -> None:
            folder.final_path = long_form(WORKSPACE_PATH) + "\\elsewhere"

        access = self.access(read_hook=move)

        self.assertIs(
            self.reader(access).read_file(GARDEN, "plan.md"),
            FileRefusal.CHANGED,
        )
        self.assert_all_released(access)


class HandleCheckTests(_ReaderCase):
    """Each component is checked on its own handle, and by name first."""

    def node(self, relative: str) -> object:
        return self.volume.find(
            long_form(WORKSPACE_PATH) + "\\" + relative.replace("/", "\\")
        )

    def test_a_reparse_point_in_enumeration_is_refused_before_it_is_opened(
        self,
    ) -> None:
        self.node("garden/notes").find_attributes = FILE_ATTRIBUTE_REPARSE_POINT
        access = self.access()

        self.assertIs(
            self.reader(access).read_file(GARDEN, "notes/beds.txt"),
            FileRefusal.UNREADABLE,
        )
        opened = [path.split("\\")[-1] for path in access.opened_paths]
        self.assertNotIn("notes", opened)
        self.assert_all_released(access)

    def test_every_locality_attribute_in_enumeration_is_refused_unopened(
        self,
    ) -> None:
        for attribute in (
            FILE_ATTRIBUTE_OFFLINE,
            FILE_ATTRIBUTE_RECALL_ON_OPEN,
            FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
        ):
            with self.subTest(attribute=hex(attribute)):
                self.node("garden/plan.md").find_attributes = attribute
                access = self.access()

                self.assertIs(
                    self.reader(access).read_file(GARDEN, "plan.md"),
                    FileRefusal.UNREADABLE,
                )
                self.assertNotIn(
                    "plan.md",
                    [path.split("\\")[-1] for path in access.opened_paths],
                )

    def test_a_folder_named_like_a_synchronized_one_is_read_as_ordinary(
        self,
    ) -> None:
        # This is not cloud detection: only the attributes Maria observes
        # refuse an entry, so a name alone changes nothing.
        self.volume.add_file("garden/OneDrive/notes.md", b"synced\n")
        access = self.access()

        content = self.reader(access).read_file(GARDEN, "OneDrive/notes.md")

        self.assertIs(type(content), FileContent)
        self.assertEqual(content.data, b"synced\n")
        self.assert_all_released(access)

    def test_a_placeholder_that_appears_after_its_lookup_is_refused_on_its_handle(
        self,
    ) -> None:
        # Enumeration saw an ordinary file; the handle now shows a reparse
        # point and offline data: it is opened, then refused before any read.
        node = self.node("garden/plan.md")
        node.find_attributes = 0
        node.attributes = FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_OFFLINE
        access = self.access()

        self.assertIs(
            self.reader(access).read_file(GARDEN, "plan.md"),
            FileRefusal.UNREADABLE,
        )
        self.assertIn("plan.md", [path.split("\\")[-1] for path in access.opened_paths])
        self.assertNotIn("read", access.calls)

    def test_recall_on_open_on_a_handle_is_not_checked_there(self) -> None:
        # Owner correction 1: on a handle the bit can mean something else
        # entirely, so only enumeration data is checked for it.
        node = self.node("garden/plan.md")
        node.find_attributes = 0
        node.attributes = FILE_ATTRIBUTE_RECALL_ON_OPEN
        access = self.access()

        self.assertIs(
            type(self.reader(access).read_file(GARDEN, "plan.md")),
            FileContent,
        )

    def test_a_handle_showing_offline_or_recall_on_data_is_refused(self) -> None:
        for attribute in (FILE_ATTRIBUTE_OFFLINE, FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS):
            with self.subTest(attribute=hex(attribute)):
                node = self.node("garden/plan.md")
                node.find_attributes = 0
                node.attributes = attribute
                self.assertIs(
                    self.reader(self.access()).read_file(GARDEN, "plan.md"),
                    FileRefusal.UNREADABLE,
                )

    def test_a_nonzero_reparse_tag_a_pending_deletion_or_a_wrong_type_refuses(
        self,
    ) -> None:
        for change in (
            {"reparse_tag": 0xA000000C},
            {"delete_pending": True},
        ):
            with self.subTest(change=list(change)):
                node = self.node("garden/plan.md")
                for key, value in change.items():
                    setattr(node, key, value)
                self.assertIs(
                    self.reader(self.access()).read_file(GARDEN, "plan.md"),
                    FileRefusal.UNREADABLE,
                )
                node.reparse_tag = 0
                node.delete_pending = False
        # A directory where a file is expected is refused from its find data.
        self.volume.add_directory("garden/folder.md")
        access = self.access()
        self.assertIs(
            self.reader(access).read_file(GARDEN, "folder.md"),
            FileRefusal.UNREADABLE,
        )
        self.assertNotIn(
            "folder.md",
            [path.split("\\")[-1] for path in access.opened_paths],
        )

    def test_a_short_name_match_returns_another_name_and_is_refused(self) -> None:
        # A generated short name holds "~", which the grammar already
        # refuses; any other alias still finds the long name and is refused.
        self.node("garden/plan.md").short_name = "PLANALIAS.MD"
        access = self.access()

        self.assertIs(
            self.reader(access).read_file(GARDEN, "PLAN~1.MD"),
            FileRefusal.UNREADABLE,
        )
        self.assertEqual(access.calls, [])
        self.assertIs(
            self.reader(access).read_file(GARDEN, "planalias.md"),
            FileRefusal.UNREADABLE,
        )
        self.assertIn("find_first", access.calls)
        self.assert_all_released(access)

    def test_a_final_path_that_differs_or_does_not_fit_is_refused(self) -> None:
        root = self.volume.root()
        root.final_path = long_form(WORKSPACE_PATH) + "-moved"
        self.assertIs(
            self.reader(self.access()).read_file(GARDEN, "plan.md"),
            FileRefusal.UNREADABLE,
        )
        root.final_path = "\\\\?\\C:\\" + "x" * 32_767
        self.assertIs(
            self.reader(self.access()).read_file(GARDEN, "plan.md"),
            FileRefusal.UNREADABLE,
        )
        root.final_path = long_form(WORKSPACE_PATH).upper()
        self.assertIs(
            type(self.reader(self.access()).read_file(GARDEN, "plan.md")),
            FileContent,
        )


class ModeAndLatchTests(_ReaderCase):
    """The placeholder mode is exposed once and restored once, every time."""

    def test_a_mode_that_cannot_be_set_ends_before_any_filesystem_call(self) -> None:
        access = self.access(set_result=-1)

        self.assertIs(
            self.reader(access).read_file(GARDEN, "plan.md"),
            FileRefusal.INCOMPLETE,
        )
        self.assertEqual(access.calls, ["set_placeholder_mode"])
        self.assertFalse(project_files._LATCH.tripped)

    def test_the_restore_is_attempted_once_after_a_failure_too(self) -> None:
        access = self.access(fail={"attribute_tag": {1}}, previous_mode=1)

        self.assertIs(
            self.reader(access).read_file(GARDEN, "plan.md"),
            FileRefusal.INCOMPLETE,
        )
        self.assertEqual(access.modes, [PHCM_EXPOSE_PLACEHOLDERS, 1])
        self.assert_all_released(access)

    def test_a_detected_restore_failure_withholds_and_stops_every_later_one(
        self,
    ) -> None:
        for result in (-2, 0, 1):
            with self.subTest(result=result):
                project_files._LATCH.tripped = False
                access = self.access(restore_result=result)
                reader = self.reader(access)

                self.assertIs(
                    reader.read_file(GARDEN, "plan.md"),
                    FileRefusal.INCOMPLETE,
                )
                self.assertTrue(project_files._LATCH.tripped)
                later = self.access()
                for outcome in (
                    self.reader(later).read_file(GARDEN, "plan.md"),
                    self.reader(later).list_folder(GARDEN, ""),
                    self.reader(later).check_workspace_folder("garden"),
                ):
                    self.assertIs(outcome, FileRefusal.STOPPED)
                self.assertEqual(later.calls, [])
        self.assertEqual(
            refusal_message(FileRefusal.STOPPED),
            "Project file reading is not available until Maria restarts.",
        )


class FailureInjectionTests(_ReaderCase):
    """Each call made to fail in turn releases everything exactly once."""

    def _calls(self, operation: str) -> list[tuple[str, int]]:
        """Return every (call, occurrence) a successful operation makes."""
        access = self.access()
        self._run(operation, access)
        seen: dict[str, int] = {}
        sequence = []
        for call in access.calls:
            seen[call] = seen.get(call, 0) + 1
            sequence.append((call, seen[call]))
        return sequence

    def _run(self, operation: str, access: FakeAccess) -> object:
        reader = self.reader(access)
        if operation == "read":
            return reader.read_file(GARDEN, "notes/beds.txt")
        if operation == "list":
            return reader.list_folder(GARDEN, "notes")
        return reader.check_workspace_folder("garden/notes")

    def test_every_call_failing_in_turn_shows_nothing_and_leaks_nothing(
        self,
    ) -> None:
        for operation in ("read", "list", "folder"):
            for call, occurrence in self._calls(operation):
                with self.subTest(operation=operation, call=call, at=occurrence):
                    project_files._LATCH.tripped = False
                    access = self.access(fail={call: {occurrence}})

                    outcome = self._run(operation, access)

                    self.assertIs(type(outcome), FileRefusal)
                    released = [value for _, value in access.released]
                    self.assertEqual(len(released), len(set(released)))
                    handles, searches, descriptors = access.leaked()
                    if call in {"find_close", "close_handle", "close_descriptor"}:
                        # A failed release is not retried: that one resource
                        # alone stays open, and the outcome is withheld.
                        self.assertEqual(outcome, FileRefusal.INCOMPLETE)
                        self.assertEqual(
                            len(handles) + len(searches) + len(descriptors),
                            1,
                        )
                    else:
                        self.assertEqual(
                            (handles, searches, descriptors),
                            (set(), set(), set()),
                        )
                    if call == "set_placeholder_mode" and occurrence == 1:
                        self.assertEqual(len(access.modes), 1)
                    else:
                        self.assertEqual(len(access.modes), 2)


class ListingTests(_ReaderCase):
    """A listing shows admitted entries from find data, sorted, and bounded."""

    def test_a_listing_sorts_and_shows_only_admitted_entries(self) -> None:
        self.volume.add_file("garden/Beta.md", b"b")
        self.volume.add_file("garden/alpha.TXT", b"aa")
        self.volume.add_file("garden/.hidden.md", b"h")
        self.volume.add_file("garden/run.exe", b"x")
        self.volume.add_file(
            "garden/offline.md",
            b"o",
            find_attributes=FILE_ATTRIBUTE_OFFLINE,
        )
        self.volume.add_directory(
            "garden/linked",
            find_attributes=FILE_ATTRIBUTE_REPARSE_POINT,
        )
        access = self.access()

        listing = self.reader(access).list_folder(GARDEN, "")

        self.assertIs(type(listing), FolderListing)
        self.assertEqual(listing.folder, "")
        self.assertEqual(
            [(entry.name, entry.directory, entry.size) for entry in listing.entries],
            [
                ("alpha.TXT", False, 2),
                ("Beta.md", False, 1),
                ("empty", True, 0),
                ("notes", True, 0),
                ("plan.md", False, len(PLAN)),
            ],
        )
        self.assertFalse(listing.scan_stopped)
        self.assertNotIn("open_descriptor", access.calls)
        self.assert_all_released(access)

    def test_an_empty_folder_is_a_valid_listing_by_either_normal_end(self) -> None:
        # Owner correction 2: FILE_NOT_FOUND from the first search and the
        # end of the directory from a later one both return the listing.
        for error in (2, 0):
            with self.subTest(empty_listing_error=error):
                access = self.access(empty_listing_error=error)
                listing = self.reader(access).list_folder(GARDEN, "empty")
                self.assertIs(type(listing), FolderListing)
                self.assertEqual(listing.entries, ())
                self.assert_all_released(access)

    def test_a_failure_that_is_not_a_normal_end_withholds_the_listing(self) -> None:
        access = self.access(fail={"find_next": {2}})

        self.assertIs(
            self.reader(access).list_folder(GARDEN, ""),
            FileRefusal.INCOMPLETE,
        )
        self.assert_all_released(access)

    def test_the_scan_stops_after_two_thousand_entries(self) -> None:
        for count, stopped in ((2_000, False), (2_001, True)):
            with self.subTest(count=count):
                volume = FakeVolume(WORKSPACE_PATH, REPOSITORY_PATH)
                for index in range(count):
                    volume.add_file(f"many/f{index:05d}.txt", b"x")
                access = FakeAccess(volume)
                reader = ProjectFileReader(
                    StubRoots(),
                    StubProtections(),
                    _quiet(),
                    access_loader=lambda access=access: access,
                )

                listing = reader.list_folder(
                    Project(9, "Many", "workspace", "many", STAMP, STAMP),
                    "",
                )

                self.assertEqual(len(listing.entries), 2_000)
                self.assertIs(listing.scan_stopped, stopped)
                self.assertEqual(access.leaked(), (set(), set(), set()))

    def test_a_project_maria_listing_shows_only_members_and_their_folders(
        self,
    ) -> None:
        self.volume.add_file("Maria/core/brain.py", b"x", REPOSITORY_PATH)
        self.volume.add_file("Maria/core/unlisted.py", b"x", REPOSITORY_PATH)
        self.volume.add_file("Maria/CHANGELOG.md", b"x", REPOSITORY_PATH)
        self.volume.add_directory("Maria/database", REPOSITORY_PATH)
        self.volume.add_file("Maria/.gitignore", b"x", REPOSITORY_PATH)
        self.volume.add_file("README.md", b"x", REPOSITORY_PATH)
        self.volume.add_file("notes.txt", b"x", REPOSITORY_PATH)
        reader = self.reader(self.access())

        top = reader.list_folder(MARIA, "")
        maria = reader.list_folder(MARIA, "maria")
        core = reader.list_folder(MARIA, "Maria/core")

        self.assertEqual([entry.name for entry in top.entries], ["Maria", "README.md"])
        self.assertEqual(maria.folder, "Maria")
        self.assertEqual(
            [entry.name for entry in maria.entries],
            ["CHANGELOG.md", "core"],
        )
        self.assertEqual([entry.name for entry in core.entries], ["brain.py"])
        self.assertIs(
            reader.list_folder(MARIA, "Maria/database"),
            FileRefusal.UNREADABLE,
        )

    def test_a_workspace_folder_check_returns_its_on_disk_spelling(self) -> None:
        reader = self.reader(self.access())

        self.assertEqual(reader.check_workspace_folder("GARDEN/Notes"), "garden/notes")
        self.assertEqual(reader.check_workspace_folder(""), "")
        self.assertIs(reader.check_workspace_folder("missing"), FileRefusal.UNREADABLE)
        self.assertIs(
            reader.check_workspace_folder("garden/plan.md"),
            FileRefusal.UNREADABLE,
        )


class DecodingTests(unittest.TestCase):
    """Bytes become lines only when they are text Maria can show."""

    def content(self, data: bytes) -> FileContent:
        return FileContent("a.md", data, len(data), hashlib.sha256(data).hexdigest())

    def test_one_byte_order_mark_is_removed_and_lines_split_after_line_feeds(
        self,
    ) -> None:
        decoded = decode_file(self.content(b"\xef\xbb\xbfone\r\ntwo\rstill two\nlast"))

        self.assertIs(type(decoded), DecodedFile)
        self.assertEqual(decoded.lines, ("one\r\n", "two\rstill two\n", "last"))
        self.assertEqual(decoded.line_count, 3)
        self.assertEqual(
            decode_file(self.content(b"\xef\xbb\xbf\xef\xbb\xbfx")).lines,
            ("\ufeffx",),
        )

    def test_nul_invalid_utf8_and_empty_files_are_refused(self) -> None:
        for data, refusal in (
            (b"a\x00b", FileRefusal.NOT_UTF8),
            (b"\xff", FileRefusal.NOT_UTF8),
            (b"\xed\xa0\x80", FileRefusal.NOT_UTF8),
            (b"", FileRefusal.EMPTY),
            (b"\xef\xbb\xbf", FileRefusal.EMPTY),
        ):
            with self.subTest(data=data):
                self.assertIs(decode_file(self.content(data)), refusal)

    def test_a_display_shows_the_range_with_every_hidden_character_escaped(
        self,
    ) -> None:
        decoded = decode_file(
            self.content(b"plain\ttab\r\nbell\x07 and\xe2\x80\x8bzero\nlast\rcr\n")
        )

        shown = file_display(4, decoded, None)

        self.assertEqual(
            shown,
            "Project 4 file a.md, lines 1-3 of 3, "
            f"{decoded.size} bytes, SHA-256 {decoded.sha256[:16]}:\n"
            "plain\ttab\nbell\\u0007 and\\u200bzero\nlast\\u000dcr",
        )
        self.assertEqual(
            file_display(4, decoded, (2, 2)).split("\n", 1)[1],
            "bell\\u0007 and\\u200bzero",
        )
        self.assertIs(file_display(4, decoded, (2, 4)), FileRefusal.LINE_RANGE)

    def test_a_display_holds_at_most_one_hundred_twenty_eight_kib(self) -> None:
        fits = decode_file(self.content(b"x" * FILE_SHOW_MAX_BYTES))
        over = decode_file(self.content(b"x" * (FILE_SHOW_MAX_BYTES + 1)))

        self.assertIs(type(file_display(1, fits, None)), str)
        self.assertIs(file_display(1, over, None), FileRefusal.SHOW_TOO_LARGE)

    def test_a_character_beyond_the_basic_plane_is_escaped_as_a_pair(self) -> None:
        decoded = decode_file(self.content("tag\U000e0041".encode("utf-8")))

        self.assertTrue(file_display(1, decoded, None).endswith("tag\\udb40\\udc41"))


class ReferenceGrammarTests(unittest.TestCase):
    """The ``@`` grammar: which words are references, and which refuse."""

    def test_ordinary_text_has_no_reference(self) -> None:
        for request in (
            "write to me@example.com today",
            "a lone @ sign",
            "@@literal at",
            "no references here",
            "",
        ):
            with self.subTest(request=request):
                self.assertFalse(scan_references(request).present)

    def test_whole_files_and_ranges_are_references(self) -> None:
        scan = scan_references("compare @docs/a.md and @b.py:3-9 please")

        self.assertTrue(scan.present)
        self.assertIsNone(scan.reason)
        self.assertEqual(
            [(reference.path, reference.lines) for reference in scan.references],
            [("docs/a.md", None), ("b.py", (3, 9))],
        )

    def test_a_malformed_reference_refuses_the_whole_request(self) -> None:
        for request in (
            "see @README.",
            "see @../secret.md",
            "see @a.md:",
            "see @a.md:x-y",
            "see @a.md:1-",
            "see @a.md:1:2",
            "see @a<b>.md",
        ):
            with self.subTest(request=request):
                scan = scan_references(request)
                self.assertTrue(scan.present)
                self.assertEqual(scan.reason, REFERENCE_FORM_REASON)

    def test_a_range_of_digits_outside_the_limits_gets_the_range_reason(self) -> None:
        for range_text in ("0-3", "5-2", "1-1000000", "1-" + "9" * 5_000):
            with self.subTest(range_text=range_text[:12]):
                scan = scan_references(f"see @a.md:{range_text}")
                self.assertEqual(scan.reason, LINE_RANGE_REASON)
        self.assertEqual(
            scan_references("@a.md:000001-999999").references[0].lines,
            (1, 999_999),
        )

    def test_duplicates_ignoring_ascii_case_and_range_collapse_in_order(self) -> None:
        scan = scan_references("@A.md @a.MD @a.md:1-2 @b.md @a.md:1-2")

        self.assertEqual(
            [(reference.path, reference.lines) for reference in scan.references],
            [("A.md", None), ("a.md", (1, 2)), ("b.md", None)],
        )

    def test_at_most_three_distinct_references_remain(self) -> None:
        self.assertIsNone(scan_references("@a.md @b.md @c.md").reason)
        self.assertEqual(
            scan_references("@a.md @b.md @c.md @d.md").reason,
            REFERENCE_COUNT_REASON,
        )


class FilePartTests(_ReaderCase):
    """Parts are built from one read per file, within the part bound."""

    def test_references_to_one_file_share_one_read_and_one_digest(self) -> None:
        access = self.access()
        scan = scan_references("@plan.md @PLAN.md:2-3")

        parts = file_parts(self.reader(access), GARDEN, scan.references)

        self.assertEqual(len(parts), 2)
        self.assertEqual(access.calls.count("open_descriptor"), 1)
        self.assertEqual(parts[0].sha256, parts[1].sha256)
        self.assertEqual((parts[0].start, parts[0].end, parts[0].of_lines), (1, 3, 3))
        self.assertEqual(parts[0].text, PLAN.decode("utf-8"))
        self.assertEqual(parts[1].text, "second line\nthird\n")
        self.assertEqual(parts[1].path, "plan.md")

    def test_the_part_bound_holds_exactly(self) -> None:
        self.volume.add_file("garden/fits.md", b"x" * FILE_PART_MAX_BYTES)
        self.volume.add_file("garden/over.md", b"x" * (FILE_PART_MAX_BYTES + 1))
        reader = self.reader(self.access())

        fits = file_parts(reader, GARDEN, scan_references("@fits.md").references)
        over = file_parts(reader, GARDEN, scan_references("@over.md").references)

        self.assertEqual(len(fits[0].text), FILE_PART_MAX_BYTES)
        self.assertEqual(over, REFERENCE_PART_TOO_LARGE_REASON)

    def test_a_range_beyond_the_file_or_an_unreadable_file_refuses(self) -> None:
        reader = self.reader(self.access())

        self.assertEqual(
            file_parts(reader, GARDEN, scan_references("@plan.md:2-4").references),
            LINE_RANGE_REASON,
        )
        self.assertEqual(
            file_parts(reader, GARDEN, scan_references("@missing.md").references),
            REFERENCE_UNREADABLE_REASON,
        )

    def test_each_refusal_has_its_model_reason(self) -> None:
        self.assertEqual(
            refusal_reason(FileRefusal.INCOMPLETE),
            "the checks of a project file you named could not be completed",
        )
        self.assertEqual(
            refusal_reason(FileRefusal.UNAVAILABLE),
            "project file reading is not available on this computer",
        )
        for refusal in (
            FileRefusal.UNREADABLE,
            FileRefusal.TOO_LARGE,
            FileRefusal.OPEN_FOR_CHANGES,
            FileRefusal.CHANGED,
            FileRefusal.NOT_UTF8,
            FileRefusal.EMPTY,
        ):
            with self.subTest(refusal=refusal):
                self.assertEqual(refusal_reason(refusal), REFERENCE_UNREADABLE_REASON)


if __name__ == "__main__":
    unittest.main()
