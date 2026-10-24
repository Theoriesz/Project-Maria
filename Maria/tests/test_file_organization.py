"""Tests for the file organization logic (V1-25-R1).

Nearly everything here runs against ``FakeVolume``, an in-memory volume that
implements the access module's methods, so races that no real call can be made
to produce on demand (a destination that appears, a failed or interrupted close,
a completion that is only pending, a link count that changes) are tested exactly.
``RealOrganizationTests`` then makes the real Windows calls, only on temporary
folders the test creates on an NTFS volume it confirms, and skips explicitly on
any other system.
"""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import file_organization as fo
from core import organize_access as native
from core.organize_access import AccessError, FileInfo
from tests.organize_fakes import NAME_NON_BMP, ROOT_PATH, FakeVolume, Node, fake_scandir

try:
    import _winapi
except ImportError:  # not Windows
    _winapi = None

COLLISION = native.STATUS_OBJECT_NAME_COLLISION
DENIED = 0xC0000022


def digest(node: Node) -> str:
    """Return the digest the offer would pin for a fake file."""
    return fo.digest_of(FileInfo(0x20, node.volume, node.index, 1, node.size, node.last_write))


def sums_to(test: unittest.TestCase, batch: fo.BatchResult, planned: int) -> None:
    """The six status buckets always add up to the planned operations."""
    test.assertEqual(sum(batch.counts.values()), planned)
    test.assertEqual(len(batch.outcomes), planned)


class OrganizeCase(unittest.TestCase):
    """A fake volume with folder F holding a.pdf, and a clean pending latch."""

    def setUp(self) -> None:
        native._PENDING_LATCH = False
        self.addCleanup(setattr, native, "_PENDING_LATCH", False)
        self.v = FakeVolume()
        self.folder = self.v.mkdir(self.v.root, "F")
        self.file1 = self.v.mkfile(self.folder, "a.pdf")

    def run_ops(self, operations: list[dict], parts: tuple[str, ...] = ("F",)) -> fo.BatchResult:
        batch = fo.run_batch(self.v, ROOT_PATH, parts, operations)
        self.assertEqual(self.v.handles, {}, "a handle was left open")
        return batch

    def op(self, name: str = "a.pdf", category: str = "Documents", create: bool = False, **more):
        return {"name": name, "category": category, "create_approved": create, **more}

    def tree(self) -> list:
        return [
            (node.name, self.v.listing(node))
            for node in (self.v.root, self.folder)
        ]

    def fail_closes_after_the_preflight(self, purposes: set[str]) -> None:
        # A release failure in the preflight stops the batch before any change, so
        # the tests of a release failure after a change arm it once the preflight
        # has finished and released its own handles.
        real = fo._preflight

        def preflight(*arguments, **keywords):
            try:
                return real(*arguments, **keywords)
            finally:
                self.v.close_fail_purposes = set(purposes)

        patcher = patch.object(fo, "_preflight", preflight)
        patcher.start()
        self.addCleanup(patcher.stop)


# ---- one operation, every outcome -------------------------------------------------
class OutcomeTests(OrganizeCase):
    def test_a_plain_move_creates_the_approved_folder_and_moves_the_file(self) -> None:
        batch = self.run_ops([self.op(create=True)])
        self.assertEqual(batch.counts["moved"], 1)
        self.assertEqual(batch.folders_created, 1)
        self.assertTrue(batch.success)
        self.assertEqual(self.v.listing(self.folder), ["Documents"])
        sums_to(self, batch, 1)

    def test_a_folder_that_vanished_since_the_offer_is_never_created_unapproved(self) -> None:
        before = self.tree()
        batch = self.run_ops([self.op(create=False)])
        self.assertEqual(batch.outcomes[0].reason, "FOLDER_NOT_APPROVED")
        self.assertEqual(batch.preflight_index, 1)
        self.assertEqual(self.v.counts["rename"], 0)
        self.assertEqual(self.tree(), before)

    def test_one_approved_creation_serves_every_operation_of_that_category(self) -> None:
        self.v.mkfile(self.folder, "b.txt")
        batch = self.run_ops([self.op(create=True), self.op("b.txt", create=True)])
        self.assertEqual(batch.counts["moved"], 2)
        self.assertEqual(batch.folders_created, 1)

    def test_an_approved_folder_name_that_is_now_a_file_creates_and_moves_nothing(self) -> None:
        self.v.mkfile(self.folder, "Documents")
        before = self.tree()
        batch = self.run_ops([self.op(create=True)])
        self.assertEqual(batch.outcomes[0].reason, "FOLDER_NOT_PLAIN")
        self.assertEqual(self.v.counts["rename"], 0)
        self.assertEqual(self.tree(), before)

    def test_a_destination_made_by_another_program_is_not_replaced(self) -> None:
        docs = self.v.mkdir(self.folder, "Documents")

        def appear(volume: FakeVolume, detail: dict) -> None:
            if "a.pdf" not in docs.children:
                volume.mkfile(docs, "a.pdf")

        self.v.hooks["rename"] = [appear]
        batch = self.run_ops([self.op()])
        outcome = batch.outcomes[0]
        self.assertEqual((outcome.status, outcome.reason), ("not_moved", "RENAME_FAILED"))
        self.assertEqual(outcome.code, COLLISION)
        self.assertIn("a.pdf", self.folder.children)
        self.assertIsNot(docs.children["a.pdf"], self.file1)

    def test_a_destination_that_is_a_second_name_of_the_source_is_unknown(self) -> None:
        docs = self.v.mkdir(self.folder, "Documents")

        def link(volume: FakeVolume, detail: dict) -> None:
            twin = volume.mkfile(docs, "a.pdf")
            twin.index = self.file1.index

        self.v.hooks["rename"] = [link]
        batch = self.run_ops([self.op()])
        self.assertEqual(batch.outcomes[0].status, "unknown")

    def test_destination_evidence_that_is_not_a_plain_file_is_unknown(self) -> None:
        for label, make in (
            ("directory", lambda docs: self.v.mkdir(docs, "a.pdf")),
            ("reparse", lambda docs: setattr(self.v.mkfile(docs, "a.pdf"), "reparse", True)),
        ):
            with self.subTest(label):
                self.setUp()
                docs = self.v.mkdir(self.folder, "Documents")
                self.v.hooks["rename"] = [lambda volume, detail: make(docs)]
                self.assertEqual(self.run_ops([self.op()]).outcomes[0].status, "unknown")

    def test_a_destination_that_cannot_be_read_is_unknown(self) -> None:
        docs = self.v.mkdir(self.folder, "Documents")

        def appear(volume: FakeVolume, detail: dict) -> None:
            volume.mkfile(docs, "a.pdf").unreadable = True

        self.v.hooks["rename"] = [appear]
        self.assertEqual(self.run_ops([self.op()]).outcomes[0].status, "unknown")

    def test_a_rename_that_took_effect_and_then_failed_is_unknown_never_not_moved(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.rename_effect_then_raise = True
        outcome = self.run_ops([self.op()]).outcomes[0]
        self.assertEqual(outcome.status, "unknown")
        self.assertTrue(outcome.attempted)

    def test_a_pending_rename_is_unknown_stops_the_run_and_latches(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.mkfile(self.folder, "b.txt")
        self.v.pending_on_rename = True
        batch = self.run_ops([self.op(), self.op("b.txt")])
        self.assertEqual([o.status for o in batch.outcomes], ["unknown", "not_attempted"])
        self.assertTrue(batch.pending)
        self.assertTrue(native.pending_latched())
        self.assertFalse(batch.success)

    def test_a_failed_verification_is_reported_but_unconfirmed(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.fail_lookups_after_rename = True
        outcome = self.run_ops([self.op()]).outcomes[0]
        self.assertEqual((outcome.status, outcome.reason), ("reported_unconfirmed", "VERIFY_FAILED"))

    def test_a_rename_that_fails_cleanly_with_unchanged_state_is_not_moved(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.failures[("rename", 1)] = AccessError("NtSetInformationFile", DENIED, "nt")
        outcome = self.run_ops([self.op()]).outcomes[0]
        self.assertEqual((outcome.status, outcome.code), ("not_moved", DENIED))
        self.assertIn("a.pdf", self.folder.children)

    def test_a_failed_rename_whose_source_cannot_be_read_is_unknown(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.file1.unreadable = True
        self.v.failures[("rename", 1)] = AccessError("NtSetInformationFile", DENIED, "nt")
        self.assertEqual(self.run_ops([self.op()]).outcomes[0].status, "unknown")

    def test_a_second_name_that_appears_after_the_source_is_held_is_a_warning(self) -> None:
        docs = self.v.mkdir(self.folder, "Documents")
        self.v.mkfile(self.folder, "b.txt")
        self.v.hooks["rename"] = [lambda volume, detail: setattr(self.file1, "links", 2)]
        batch = self.run_ops([self.op(), self.op("b.txt")])
        self.assertEqual([o.status for o in batch.outcomes], ["moved", "not_attempted"])
        self.assertTrue(batch.outcomes[0].extra_name)
        self.assertEqual(batch.warnings, 1)
        self.assertFalse(batch.success)
        self.assertEqual(batch.stopped_reason, "EXTRA_NAME")
        self.assertIn("a.pdf", docs.children)

    def test_a_single_move_with_a_second_name_is_never_a_clean_success(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.hooks["rename"] = [lambda volume, detail: setattr(self.file1, "links", 2)]
        batch = self.run_ops([self.op()])
        self.assertEqual(batch.counts["moved"], 1)
        self.assertFalse(batch.success)

    def test_verification_does_not_rely_on_a_stable_link_count(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.hooks["rename"] = [lambda volume, detail: setattr(self.file1, "links", 3)]
        self.assertEqual(self.run_ops([self.op()]).outcomes[0].status, "moved")

    def test_a_created_folder_is_kept_and_reported_when_the_source_is_then_in_use(self) -> None:
        def lock(volume: FakeVolume, detail: dict) -> None:
            if detail["name"] == "a.pdf" and "documents" in self.folder.children:
                volume.locked.add(id(self.file1))

        self.v.hooks["open_relative"] = [lock]
        batch = self.run_ops([self.op(create=True)])
        outcome = batch.outcomes[0]
        self.assertEqual((outcome.status, outcome.reason, outcome.attempted), ("refused", "IN_USE", False))
        self.assertEqual(batch.folders_created, 1)
        self.assertIn("documents", self.folder.children)

    def test_a_created_folder_is_kept_when_the_rename_then_fails(self) -> None:
        self.v.failures[("rename", 1)] = AccessError("NtSetInformationFile", DENIED, "nt")
        batch = self.run_ops([self.op(create=True)])
        self.assertEqual(batch.outcomes[0].status, "not_moved")
        self.assertEqual(batch.folders_created, 1)
        self.assertIn("documents", self.folder.children)

    def test_a_created_folder_that_cannot_be_confirmed_is_unconfirmed_and_stops(self) -> None:
        self.v.mkfile(self.folder, "b.txt")

        def corrupt(volume: FakeVolume, detail: dict) -> None:
            node = volume.handles[detail["handle"]]
            if node.name == "Documents":
                node.reparse = True

        self.v.hooks["info"] = [corrupt]
        batch = self.run_ops([self.op(create=True), self.op("b.txt", create=True)])
        self.assertEqual(batch.outcomes[0].folder, "unconfirmed")
        self.assertEqual(batch.outcomes[1].status, "not_attempted")
        self.assertEqual(batch.folders_unconfirmed, 1)
        self.assertEqual(self.v.counts["rename"], 0)

    def test_a_creation_that_failed_with_the_folder_absent_is_not_created(self) -> None:
        def refuse(volume: FakeVolume, detail: dict) -> None:
            if detail["disposition"] == native.FILE_CREATE:
                raise AccessError("NtCreateFile", DENIED, "nt")

        self.v.hooks["open_relative"] = [refuse]
        batch = self.run_ops([self.op(create=True)])
        self.assertEqual(batch.outcomes[0].folder, "not_created")
        self.assertEqual(batch.outcomes[0].reason, "FOLDER_NOT_CREATED")
        self.assertEqual(batch.folders_created, 0)

    def test_a_creation_whose_result_cannot_be_checked_is_unknown(self) -> None:
        def refuse(volume: FakeVolume, detail: dict) -> None:
            if detail["name"] == "Documents" and (
                detail["disposition"] == native.FILE_CREATE
                or detail["access"] == native.LOOKUP_ACCESS
            ):
                raise AccessError("NtCreateFile", DENIED, "nt")

        self.v.hooks["open_relative"] = [refuse]
        batch = self.run_ops([self.op(create=True)])
        self.assertEqual(batch.outcomes[0].folder, "unknown")
        self.assertEqual(batch.folders_unconfirmed, 1)

    def test_a_stop_marks_the_rest_not_attempted_and_the_buckets_sum(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.mkfile(self.folder, "b.txt")
        self.v.mkfile(self.folder, "c.txt")
        self.v.failures[("rename", 1)] = AccessError("NtSetInformationFile", DENIED, "nt")
        batch = self.run_ops([self.op(), self.op("b.txt"), self.op("c.txt")])
        self.assertEqual(
            [o.status for o in batch.outcomes], ["not_moved", "not_attempted", "not_attempted"]
        )
        sums_to(self, batch, 3)

    def test_an_error_before_the_rename_call_is_refused_and_not_attempted(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.failures[("open_relative", 3)] = AccessError("NtCreateFile", DENIED, "nt")
        out = self.run_ops([self.op()]).outcomes[0]
        self.assertEqual((out.status, out.attempted), ("refused", False))

    def test_an_error_after_the_rename_call_is_never_refused(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.failures[("rename", 1)] = AccessError("NtSetInformationFile", DENIED, "nt")
        outcome = self.run_ops([self.op()]).outcomes[0]
        self.assertTrue(outcome.attempted)
        self.assertNotEqual(outcome.status, "refused")

    def test_an_unexpected_exception_before_the_rename_is_a_refusal_not_an_opaque_failure(
        self,
    ) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.mkfile(self.folder, "b.txt")
        with patch.object(fo, "_open_source", side_effect=RuntimeError("leaks C:/secret")):
            batch = self.run_ops([self.op(), self.op("b.txt")])
        first = batch.outcomes[0]
        self.assertEqual((first.status, first.reason, first.attempted), ("refused", "UNEXPECTED", False))
        self.assertEqual(batch.preflight_index, 1)
        self.assertEqual(batch.failure_types, ["RuntimeError"])
        self.assertEqual(self.v.counts["rename"], 0)
        self.assertEqual(self.v.handles, {})

    def test_an_unexpected_exception_during_an_operation_stops_the_run_and_is_reported(
        self,
    ) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.mkfile(self.folder, "b.txt")
        with patch.object(fo, "_open_source", side_effect=RuntimeError("leaks C:/secret")):
            # Skip the preflight's own use of it, so the fault is in the run.
            with patch.object(fo, "_preflight", return_value=None):
                batch = self.run_ops([self.op(), self.op("b.txt")])
        first = batch.outcomes[0]
        self.assertEqual((first.status, first.reason), ("refused", "UNEXPECTED"))
        self.assertEqual(first.failure_type, "RuntimeError")
        self.assertEqual(batch.outcomes[1].status, "not_attempted")
        self.assertEqual(batch.failure_types, ["RuntimeError"])
        self.assertFalse(batch.success)

    def test_an_unexpected_exception_after_the_rename_call_is_unknown_never_refused(
        self,
    ) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.failures[("rename", 1)] = AccessError("NtSetInformationFile", DENIED, "nt")
        with patch.object(fo, "classify_failed_rename", side_effect=RuntimeError("x")):
            batch = self.run_ops([self.op()])
        outcome = batch.outcomes[0]
        self.assertEqual((outcome.status, outcome.reason, outcome.attempted), ("unknown", "UNEXPECTED", True))
        self.assertEqual(self.v.handles, {})

    def test_a_runtime_error_thrown_by_the_rename_call_is_unknown_and_stops_the_run(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.mkfile(self.folder, "b.txt")
        self.v.mkfile(self.folder, "c.txt")
        self.v.failures[("rename", 2)] = RuntimeError("leaks C:/secret")
        with patch.object(fo, "classify_failed_rename", wraps=fo.classify_failed_rename) as classify:
            batch = self.run_ops([self.op(), self.op("b.txt"), self.op("c.txt")])
        classify.assert_not_called()
        self.assertEqual([o.status for o in batch.outcomes], ["moved", "unknown", "not_attempted"])
        second = batch.outcomes[1]
        self.assertEqual((second.reason, second.attempted), ("UNEXPECTED", True))
        self.assertEqual(second.failure_type, "RuntimeError")
        self.assertEqual(batch.failure_types, ["RuntimeError"])
        self.assertEqual(self.v.counts["rename"], 2)
        self.assertFalse(batch.success)
        sums_to(self, batch, 3)

    def test_a_runtime_error_after_the_rename_took_effect_is_unknown_not_classified(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        real = self.v.rename_into

        def moved_then_failed(handle: int, directory: int, name: str):
            real(handle, directory, name)
            raise RuntimeError("leaks C:/secret")

        self.v.rename_into = moved_then_failed
        with patch.object(fo, "classify_failed_rename", wraps=fo.classify_failed_rename) as classify:
            batch = self.run_ops([self.op()])
        classify.assert_not_called()
        outcome = batch.outcomes[0]
        self.assertEqual((outcome.status, outcome.reason, outcome.attempted), ("unknown", "UNEXPECTED", True))
        self.assertEqual(outcome.failure_type, "RuntimeError")
        self.assertIn("a.pdf", self.v.listing(self.folder.children["documents"]))

    def test_an_expected_access_error_from_the_rename_call_is_still_classified(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.failures[("rename", 1)] = AccessError("NtSetInformationFile", DENIED, "nt")
        batch = self.run_ops([self.op()])
        outcome = batch.outcomes[0]
        self.assertEqual((outcome.status, outcome.reason, outcome.code), ("not_moved", "RENAME_FAILED", DENIED))
        self.assertEqual(outcome.failure_type, "")
        self.assertEqual(batch.failure_types, [])

    def test_a_native_error_after_the_rename_call_is_never_refused(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        with patch.object(
            fo, "verify_moved", side_effect=AccessError("GetFileInformationByHandle", 5, "win32")
        ):
            outcome = self.run_ops([self.op()]).outcomes[0]
        self.assertNotEqual(outcome.status, "refused")
        self.assertTrue(outcome.attempted)

    def test_the_report_of_an_unexpected_failure_uses_fixed_text_only(self) -> None:
        batch = fo.BatchResult(
            [fo.OperationOutcome("unknown", "UNEXPECTED", attempted=True, failure_type="RuntimeError")]
        )
        text = fo.render_report(batch, [{"source": "F/a.pdf", "destination": "F/Documents/a.pdf", "create": None}])
        self.assertNotIn("RuntimeError", text)
        self.assertIn("cannot confirm what happened", text)

    def test_hostile_names_are_refused_before_any_native_call_for_the_name(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        for bad in ("..\\x", "a:b", "a/b", "..", "x.", ":stream", "a" + chr(0xD800), "a\nb", "a*b"):
            with self.subTest(name=ascii(bad)):
                outcome = self.run_ops([self.op(bad)]).outcomes[0]
                self.assertEqual(outcome.reason, "MALFORMED")
        for bad in ("..", "a\\b"):
            self.assertEqual(self.run_ops([self.op(category=bad)]).outcomes[0].reason, "MALFORMED")
        self.assertEqual(self.v.counts["rename"], 0)
        self.assertNotIn("..\\x", self.v.opened_names)
        self.assertNotIn(":stream", self.v.opened_names)

    def test_source_checks(self) -> None:
        cases = {
            "HARD_LINKED": lambda n: setattr(n, "links", 2),
            "NOT_PLAIN_FILE": lambda n: setattr(n, "hidden", True),
        }
        for expected, mutate in cases.items():
            with self.subTest(expected):
                self.setUp()
                self.v.mkdir(self.folder, "Documents")
                mutate(self.file1)
                outcome = self.run_ops([self.op()]).outcomes[0]
                self.assertEqual(outcome.reason, expected)
                self.assertEqual(self.v.counts["rename"], 0)

    def test_a_source_in_use_is_refused(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.locked.add(id(self.file1))
        self.assertEqual(self.run_ops([self.op()]).outcomes[0].reason, "IN_USE")

    def test_a_different_volume_is_refused(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.file1.volume = 8
        self.assertEqual(self.run_ops([self.op()]).outcomes[0].reason, "DIFFERENT_VOLUME")

    def test_the_digest_is_metadata_and_not_a_content_hash(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        pinned = digest(self.file1)
        self.file1.size = 6
        stale = self.run_ops([self.op(digest=pinned)])
        self.assertEqual(stale.outcomes[0].reason, "SOURCE_CHANGED")
        self.file1.size = 5
        again = self.run_ops([self.op(digest=pinned)])
        self.assertEqual(again.outcomes[0].status, "moved")  # same size and time: undetected

    def test_chain_refusals(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.folder.reparse = True
        batch = self.run_ops([self.op()])
        self.assertEqual(batch.stopped_reason, "ROOT_UNCONFIRMED")
        self.assertEqual(batch.outcomes[0].reason, "LINKED")
        self.folder.reparse = False
        self.folder.final_name = "FSHORT~1"
        self.assertEqual(self.run_ops([self.op()]).outcomes[0].reason, "LINKED")
        self.folder.final_name = None
        other = fo.run_batch(self.v, "C:\\other\\root", ("F",), [self.op()])
        self.assertEqual(other.outcomes[0].status, "refused")

    def test_the_spelling_check_does_not_detect_a_same_path_replacement(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        original = self.folder.children["documents"]
        replacement = Node("Documents", "dir", self.folder)
        self.folder.children["documents"] = replacement
        self.assertNotEqual(original.index, replacement.index)
        self.assertEqual(self.run_ops([self.op()]).outcomes[0].status, "moved")

    def test_a_name_outside_the_bmp_moves(self) -> None:
        self.v.mkfile(self.folder, NAME_NON_BMP)
        self.v.mkdir(self.folder, "Documents")
        self.assertEqual(self.run_ops([self.op(NAME_NON_BMP)]).counts["moved"], 1)

    def test_the_root_itself_can_be_the_folder(self) -> None:
        self.v.mkfile(self.v.root, "top.pdf")
        batch = self.run_ops([self.op("top.pdf", create=True)], parts=())
        self.assertEqual(batch.counts["moved"], 1)
        self.assertIn("documents", self.v.root.children)

    def test_a_later_operation_that_cannot_run_refuses_the_whole_batch_before_any_change(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        before = self.tree()
        batch = self.run_ops([self.op(), self.op("missing.pdf")])
        self.assertEqual(batch.preflight_index, 2)
        self.assertEqual([o.status for o in batch.outcomes], ["refused", "refused"])
        self.assertEqual(batch.outcomes[1].reason, "SOURCE_CHANGED")
        self.assertEqual(self.v.counts["rename"], 0)
        self.assertEqual(self.tree(), before)


# ---- release rules ---------------------------------------------------------------
class CleanupTests(OrganizeCase):
    def test_a_close_failure_on_the_last_operation_is_reported_not_hidden(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.fail_closes_after_the_preflight({"other"})
        batch = self.run_ops([self.op()])
        self.assertEqual(batch.outcomes[0].status, "moved")
        self.assertTrue(batch.outcomes[0].cleanup_failures)
        self.assertTrue(batch.cleanup_failed)
        self.assertFalse(batch.success)

    def test_a_terminal_chain_release_failure_is_reported_on_the_batch(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.close_fails.add(id(self.folder))
        batch = self.run_ops([self.op()])
        self.assertTrue(batch.terminal_cleanup_failures)
        self.assertFalse(batch.success)
        self.assertEqual(batch.counts["moved"], 1)

    def test_a_close_failure_on_a_non_final_operation_stops_the_run(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.mkfile(self.folder, "b.txt")
        self.fail_closes_after_the_preflight({"other"})
        batch = self.run_ops([self.op(), self.op("b.txt")])
        self.assertEqual([o.status for o in batch.outcomes], ["moved", "not_attempted"])
        self.assertEqual(batch.stopped_reason, "RELEASE_FAILED")

    def test_every_handle_is_attempted_once_and_never_retried(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.close_fail_purposes = {"other", "lookup"}
        self.run_ops([self.op()])
        self.assertTrue(self.v.close_calls)
        self.assertTrue(all(count == 1 for count in self.v.close_calls.values()))

    def test_a_close_failure_after_a_failed_run_does_not_change_its_status(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.failures[("rename", 1)] = AccessError("NtSetInformationFile", DENIED, "nt")
        self.fail_closes_after_the_preflight({"other"})
        outcome = self.run_ops([self.op()]).outcomes[0]
        self.assertEqual(outcome.status, "not_moved")
        self.assertTrue(outcome.cleanup_failures)


    def assert_stopped_by_the_preflight_release(self, batch: fo.BatchResult, planned: int, before: list) -> None:
        self.assertEqual(self.v.counts["rename"], 0)
        self.assertEqual([o.status for o in batch.outcomes], ["refused"] * planned)
        self.assertEqual(batch.folders_created, 0)
        self.assertTrue(batch.cleanup_failed)
        self.assertGreaterEqual(batch.warnings, 1)
        self.assertFalse(batch.success)
        self.assertEqual(self.tree(), before)
        self.assertTrue(self.v.close_calls)
        self.assertTrue(all(count == 1 for count in self.v.close_calls.values()))
        sums_to(self, batch, planned)

    def test_a_source_release_failure_in_the_preflight_stops_before_any_creation_or_rename(self) -> None:
        self.v.mkfile(self.folder, "b.txt")
        self.v.close_fails.add(id(self.file1))
        before = self.tree()
        batch = self.run_ops([self.op(create=True), self.op("b.txt", create=True)])
        self.assert_stopped_by_the_preflight_release(batch, 2, before)
        self.assertEqual(batch.stopped_reason, "PREFLIGHT_RELEASE_FAILED")
        self.assertEqual(batch.preflight_cleanup_failures, 1)
        self.assertNotIn("documents", self.folder.children)

    def test_a_category_release_failure_in_the_preflight_stops_before_any_rename(self) -> None:
        self.v.mkfile(self.folder, "b.txt")
        documents = self.v.mkdir(self.folder, "Documents")
        self.v.close_fails.add(id(documents))
        before = self.tree()
        batch = self.run_ops([self.op(), self.op("b.txt")])
        self.assert_stopped_by_the_preflight_release(batch, 2, before)
        self.assertEqual(batch.preflight_cleanup_failures, 2)  # one per operation's category handle
        self.assertEqual(self.v.listing(documents), [])

    def test_a_lookup_release_failure_survives_a_preflight_refusal(self) -> None:
        documents = self.v.mkdir(self.folder, "Documents")
        self.v.mkfile(documents, "a.pdf")
        self.v.close_fail_purposes = {"lookup"}
        before = self.tree()
        batch = self.run_ops([self.op()])
        self.assertEqual(batch.preflight_index, 1)
        self.assertEqual(batch.outcomes[0].reason, "DESTINATION_EXISTS")
        self.assert_stopped_by_the_preflight_release(batch, 1, before)
        self.assertEqual(batch.preflight_cleanup_failures, 1)
        self.assertEqual(self.v.listing(documents), ["a.pdf"])

    def test_every_release_failure_in_a_preflight_is_counted_and_attempted_once(self) -> None:
        documents = self.v.mkdir(self.folder, "Documents")
        self.v.close_fails.update({id(self.file1), id(documents)})
        before = self.tree()
        batch = self.run_ops([self.op()])
        self.assert_stopped_by_the_preflight_release(batch, 1, before)
        self.assertEqual(batch.preflight_cleanup_failures, 2)

    def test_the_report_and_the_log_line_show_a_preflight_release_failure(self) -> None:
        self.v.close_fails.add(id(self.file1))
        pins = [{"source": "F/a.pdf", "destination": "F/Documents/a.pdf", "create": "F/Documents"}]
        batch = self.run_ops([self.op(create=True)])
        text = fo.render_report(batch, pins)
        self.assertTrue(text.startswith("Nothing was changed."))
        self.assertIn("could not release a file handle (while checking the moves)", text)
        self.assertEqual(fo.recorded_response(batch), "Organize folder: refused before any change.")
        self.assertIn("warnings=1", fo.count_line(batch))

    def test_a_preflight_refusal_report_also_shows_the_release_failure(self) -> None:
        documents = self.v.mkdir(self.folder, "Documents")
        self.v.mkfile(documents, "a.pdf")
        self.v.close_fail_purposes = {"lookup"}
        pins = [{"source": "F/a.pdf", "destination": "F/Documents/a.pdf", "create": None}]
        batch = self.run_ops([self.op()])
        text = fo.render_report(batch, pins)
        self.assertIn("Nothing was changed: move 1 cannot run.", text)
        self.assertIn("could not release a file handle (while checking the moves)", text)

    PIN = {"source": "F/a.pdf", "destination": "F/Documents/a.pdf", "create": None}
    RELEASE = "could not release a file handle"
    AT_THE_END = "could not release a file handle (at the end)"
    WHILE_CHECKING = "could not release a file handle (while checking the moves)"

    def assert_refused_without_change(self, batch: fo.BatchResult, before: list) -> None:
        self.assertEqual(self.v.counts["rename"], 0)
        self.assertEqual(self.tree(), before)
        self.assertTrue(all(o.status == "refused" for o in batch.outcomes))
        self.assertEqual(batch.folders_created, 0)
        self.assertFalse(batch.success)
        self.assertEqual(fo.recorded_response(batch), "Organize folder: refused before any change.")
        self.assertTrue(self.v.close_calls)
        self.assertTrue(all(count == 1 for count in self.v.close_calls.values()))

    def test_a_preflight_refusal_followed_by_a_chain_release_failure_shows_the_terminal_warning(self) -> None:
        documents = self.v.mkdir(self.folder, "Documents")
        self.v.mkfile(documents, "a.pdf")
        self.v.close_fails.add(id(self.folder))
        before = self.tree()
        batch = self.run_ops([self.op()])
        self.assertEqual(batch.preflight_index, 1)
        self.assertEqual((batch.preflight_cleanup_failures, batch.terminal_cleanup_failures), (0, 1))
        self.assert_refused_without_change(batch, before)
        text = fo.render_report(batch, [self.PIN])
        self.assertTrue(text.startswith("Nothing was changed: move 1 cannot run."))
        self.assertIn(fo.refusal_text("DESTINATION_EXISTS"), text)
        self.assertEqual(text.count(self.RELEASE), 1)
        self.assertIn(self.AT_THE_END, text)
        self.assertEqual(batch.warnings, 1)

    def test_a_root_refusal_followed_by_a_chain_release_failure_shows_the_terminal_warning(self) -> None:
        self.v.filesystem = "exFAT"
        self.v.close_fails.add(id(self.v.root))
        before = self.tree()
        batch = self.run_ops([self.op()])
        self.assertEqual(batch.outcomes[0].reason, "FILESYSTEM_UNSUPPORTED")
        self.assertEqual((batch.preflight_cleanup_failures, batch.terminal_cleanup_failures), (0, 1))
        self.assert_refused_without_change(batch, before)
        text = fo.render_report(batch, [self.PIN])
        self.assertTrue(text.startswith(fo.refused_report("FILESYSTEM_UNSUPPORTED")))
        self.assertEqual(text.count(self.RELEASE), 1)
        self.assertIn(self.AT_THE_END, text)
        self.assertEqual(batch.warnings, 1)

    def test_a_preflight_refusal_with_both_release_failures_shows_both_warnings_in_order(self) -> None:
        documents = self.v.mkdir(self.folder, "Documents")
        self.v.mkfile(documents, "a.pdf")
        self.v.close_fail_purposes = {"lookup"}
        self.v.close_fails.add(id(self.folder))
        before = self.tree()
        batch = self.run_ops([self.op()])
        self.assertEqual(batch.preflight_index, 1)
        self.assertEqual((batch.preflight_cleanup_failures, batch.terminal_cleanup_failures), (1, 1))
        self.assert_refused_without_change(batch, before)
        text = fo.render_report(batch, [self.PIN])
        self.assertTrue(text.startswith("Nothing was changed: move 1 cannot run."))
        self.assertEqual(text.count(self.RELEASE), 2)
        self.assertLess(text.index(self.WHILE_CHECKING), text.index(self.AT_THE_END))
        self.assertEqual(batch.warnings, 2)

    def test_a_preflight_release_stop_with_a_chain_failure_shows_both_warnings(self) -> None:
        self.v.close_fails.update({id(self.file1), id(self.folder)})
        before = self.tree()
        batch = self.run_ops([self.op(create=True)])
        self.assertEqual(batch.stopped_reason, "PREFLIGHT_RELEASE_FAILED")
        self.assertEqual((batch.preflight_cleanup_failures, batch.terminal_cleanup_failures), (1, 1))
        self.assert_refused_without_change(batch, before)
        text = fo.render_report(batch, [self.PIN])
        self.assertTrue(text.startswith("Nothing was changed."))
        self.assertEqual(text.count(self.RELEASE), 2)
        self.assertLess(text.index(self.WHILE_CHECKING), text.index(self.AT_THE_END))
        self.assertEqual(batch.warnings, 2)

    def test_a_refusal_without_a_terminal_release_failure_has_no_terminal_warning(self) -> None:
        documents = self.v.mkdir(self.folder, "Documents")
        self.v.mkfile(documents, "a.pdf")
        batch = self.run_ops([self.op()])
        text = fo.render_report(batch, [self.PIN])
        self.assertNotIn(self.RELEASE, text)
        self.assertEqual(
            text, "Nothing was changed: move 1 cannot run. " + fo.refusal_text("DESTINATION_EXISTS")
        )

    def test_an_interrupted_preflight_release_keeps_its_evidence_and_never_renames(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.interruptions[1] = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt) as raised:
            fo.run_batch(self.v, ROOT_PATH, ("F",), [self.op()])
        partial = raised.exception.maria_partial
        self.assertTrue(partial.cleanup_failed)
        self.assertEqual(partial.preflight_cleanup_failures, 1)
        self.assertEqual(self.v.counts["rename"], 0)
        self.assertEqual(self.v.handles, {})
        self.assertTrue(all(count == 1 for count in self.v.close_calls.values()))


# ---- classification ------------------------------------------------------------------
class ClassificationTests(unittest.TestCase):
    source = FileInfo(0x20, 7, 1000, 1, 5, 9)

    def found(self, attributes=0x20, index=1000, volume=7, kind="object") -> fo.Lookup:
        if kind != "object":
            return fo.Lookup(kind)
        return fo.Lookup("object", FileInfo(attributes, volume, index, 1, 5, 9))

    def test_not_moved_needs_positive_evidence_on_both_sides(self) -> None:
        classify = fo.classify_failed_rename
        absent = fo.Lookup("absent")
        self.assertEqual(classify(self.source, self.found(), absent), "not_moved")
        self.assertEqual(
            classify(self.source, self.found(), self.found(index=2000)), "not_moved"
        )
        self.assertEqual(classify(self.source, absent, absent), "unknown")
        self.assertEqual(classify(self.source, self.found(kind="error"), absent), "unknown")
        self.assertEqual(classify(self.source, self.found(index=3), absent), "unknown")
        self.assertEqual(classify(self.source, self.found(volume=8), absent), "unknown")
        self.assertEqual(classify(self.source, self.found(attributes=0x10), absent), "unknown")
        self.assertEqual(classify(self.source, self.found(attributes=0x420), absent), "unknown")
        self.assertEqual(
            classify(self.source, self.found(), self.found(kind="error")), "unknown"
        )
        self.assertEqual(classify(self.source, self.found(), self.found()), "unknown")
        self.assertEqual(
            classify(self.source, self.found(), self.found(attributes=0x10, index=2)), "unknown"
        )

    def test_verification_needs_the_destination_identity_and_a_source_that_is_not_it(self) -> None:
        verify = fo.verify_moved
        absent = fo.Lookup("absent")
        self.assertTrue(verify(self.source, absent, self.found()))
        self.assertTrue(verify(self.source, self.found(index=4), self.found()))
        self.assertFalse(verify(self.source, self.found(), self.found()))
        self.assertFalse(verify(self.source, absent, absent))
        self.assertFalse(verify(self.source, absent, self.found(index=4)))
        self.assertFalse(verify(self.source, absent, self.found(attributes=0x10)))
        self.assertFalse(verify(self.source, self.found(kind="error"), self.found()))
        self.assertFalse(verify(self.source, absent, self.found(kind="error")))


# ---- interruption ------------------------------------------------------------------------
class InterruptionTests(OrganizeCase):
    def test_keyboard_interrupt_propagates_and_every_handle_is_released(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.interrupt_on_rename = True
        with self.assertRaises(KeyboardInterrupt) as raised:
            fo.run_batch(self.v, ROOT_PATH, ("F",), [self.op()])
        self.assertEqual(self.v.handles, {})
        partial = raised.exception.maria_partial
        self.assertEqual(partial.outcomes[0].status, "unknown")
        self.assertTrue(partial.interrupted)
        self.assertFalse(partial.success)

    def test_the_rest_of_an_interrupted_batch_is_not_attempted(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.mkfile(self.folder, "b.txt")
        self.v.interrupt_on_rename = True
        with self.assertRaises(KeyboardInterrupt) as raised:
            fo.run_batch(self.v, ROOT_PATH, ("F",), [self.op(), self.op("b.txt")])
        partial = raised.exception.maria_partial
        self.assertEqual([o.status for o in partial.outcomes], ["unknown", "not_attempted"])
        sums_to(self, partial, 2)

    def test_an_interruption_on_the_first_close_still_attempts_every_handle(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.interruptions[1] = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            fo.run_batch(self.v, ROOT_PATH, ("F",), [self.op()])
        self.assertEqual(self.v.handles, {})
        self.assertTrue(all(count == 1 for count in self.v.close_calls.values()))

    def test_a_system_exit_on_a_close_is_handled_the_same_way(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.interruptions[1] = SystemExit(3)
        with self.assertRaises(SystemExit):
            fo.run_batch(self.v, ROOT_PATH, ("F",), [self.op()])
        self.assertEqual(self.v.handles, {})

    def test_ordinary_close_failures_are_counted_beside_an_interruption(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.interruptions[2] = KeyboardInterrupt()
        self.v.close_fail_purposes = {"other"}
        with self.assertRaises(KeyboardInterrupt) as raised:
            fo.run_batch(self.v, ROOT_PATH, ("F",), [self.op()])
        self.assertEqual(self.v.handles, {})
        self.assertTrue(raised.exception.maria_partial.cleanup_failed)

    def test_an_interruption_on_the_chain_release_carries_the_finished_result(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.interruptions[self._chain_close_number()] = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt) as raised:
            fo.run_batch(self.v, ROOT_PATH, ("F",), [self.op()])
        partial = raised.exception.maria_partial
        self.assertEqual(partial.outcomes[0].status, "moved")
        self.assertTrue(partial.interrupted)
        self.assertEqual(self.v.handles, {})

    def _chain_close_number(self) -> int:
        """Count the closes of an uninterrupted run; the chain closes after them."""
        probe = OrganizeCase()
        probe.setUp()
        probe.v.mkdir(probe.folder, "Documents")
        fo.run_batch(probe.v, ROOT_PATH, ("F",), [probe.op()])
        return probe.v.counts["close"] - 1  # the root and F close last, in that order

    def test_an_interruption_inside_a_category_creation_leaves_the_folder_unknown(self) -> None:
        def interrupt(volume: FakeVolume, detail: dict) -> None:
            if detail["disposition"] == native.FILE_CREATE:
                raise KeyboardInterrupt

        self.v.hooks["open_relative"] = [interrupt]
        with self.assertRaises(KeyboardInterrupt) as raised:
            fo.run_batch(self.v, ROOT_PATH, ("F",), [self.op(create=True)])
        partial = raised.exception.maria_partial
        self.assertEqual(partial.outcomes[0].folder, "unknown")
        self.assertEqual(partial.folders_unconfirmed, 1)
        self.assertEqual(self.v.handles, {})

    def test_an_interruption_while_verifying_a_created_folder_leaves_it_unconfirmed(self) -> None:
        def interrupt(volume: FakeVolume, detail: dict) -> None:
            node = volume.handles[detail["handle"]]
            if node.name == "Documents":
                raise KeyboardInterrupt

        self.v.hooks["info"] = [interrupt]
        with self.assertRaises(KeyboardInterrupt) as raised:
            fo.run_batch(self.v, ROOT_PATH, ("F",), [self.op(create=True)])
        partial = raised.exception.maria_partial
        self.assertEqual(partial.outcomes[0].folder, "unconfirmed")
        self.assertEqual(partial.folders_created, 0)
        self.assertEqual(partial.folders_unconfirmed, 1)
        self.assertEqual(self.v.handles, {})

    def interrupt_the_folder_volume_read(self, error: BaseException):
        real = fo.open_chain

        def armed(*arguments, **keywords):
            chain = real(*arguments, **keywords)
            # The next information call is the read of the folder's volume.
            self.v.failures[("info", self.v.counts["info"] + 1)] = error
            return chain

        return patch.object(fo, "open_chain", armed)

    def assert_nothing_ran_after_the_volume_read(self, error: BaseException) -> None:
        self.v.mkfile(self.folder, "b.txt")
        with self.assertRaises(type(error)) as raised:
            with self.interrupt_the_folder_volume_read(error):
                fo.run_batch(self.v, ROOT_PATH, ("F",), [self.op(create=True), self.op("b.txt", create=True)])
        self.assertIs(raised.exception, error)
        partial = raised.exception.maria_partial
        self.assertTrue(partial.interrupted)
        self.assertEqual([o.status for o in partial.outcomes], ["not_attempted", "not_attempted"])
        sums_to(self, partial, 2)
        self.assertEqual(self.v.handles, {})
        self.assertTrue(self.v.close_calls)
        self.assertTrue(all(count == 1 for count in self.v.close_calls.values()))
        self.assertNotIn("a.pdf", self.v.opened_names)
        self.assertNotIn("b.txt", self.v.opened_names)
        self.assertNotIn("Documents", self.v.opened_names)
        self.assertEqual(self.v.counts["rename"], 0)
        self.assertNotIn("documents", self.folder.children)

    def test_a_keyboard_interrupt_of_the_folder_volume_read_enters_no_preflight(self) -> None:
        self.assert_nothing_ran_after_the_volume_read(KeyboardInterrupt())

    def test_a_system_exit_of_the_folder_volume_read_enters_no_preflight(self) -> None:
        self.assert_nothing_ran_after_the_volume_read(SystemExit(3))

    def test_an_interrupted_run_has_a_fixed_count_only_line(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.interrupt_on_rename = True
        with self.assertRaises(KeyboardInterrupt) as raised:
            fo.run_batch(self.v, ROOT_PATH, ("F",), [self.op()])
        line = fo.count_line(raised.exception.maria_partial)
        self.assertIn("unknown=1", line)
        self.assertNotIn("a.pdf", line)
        self.assertNotIn("Documents", line)


# ---- the folder's outcome when creation is uncertain ---------------------------------------
class FolderUncertaintyTests(OrganizeCase):
    def create_then_fail(self, error: BaseException) -> None:
        def hook(volume: FakeVolume, detail: dict) -> None:
            if detail["disposition"] == native.FILE_CREATE:
                volume.mkdir(self.folder, detail["name"])  # the creation took effect
                raise error

        self.v.hooks["open_relative"] = [hook]

    def test_a_creation_that_took_effect_before_an_unexpected_exception_is_unknown(self) -> None:
        self.v.mkfile(self.folder, "b.txt")
        self.create_then_fail(RuntimeError("leaks C:/secret"))
        batch = self.run_ops([self.op(create=True), self.op("b.txt", create=True)])
        first = batch.outcomes[0]
        self.assertEqual(first.folder, "unknown")
        self.assertEqual((first.status, first.reason, first.attempted), ("refused", "UNEXPECTED", False))
        self.assertEqual(first.failure_type, "RuntimeError")
        self.assertEqual(batch.outcomes[1].status, "not_attempted")
        self.assertEqual(batch.outcomes[1].folder, "none")
        self.assertEqual((batch.folders_created, batch.folders_unconfirmed), (0, 1))
        self.assertEqual(batch.failure_types, ["RuntimeError"])
        self.assertIn("documents", self.folder.children)
        self.assertEqual(self.v.counts["rename"], 0)
        self.assertTrue(all(count == 1 for count in self.v.close_calls.values()))
        self.assertFalse(batch.success)
        sums_to(self, batch, 2)

    def test_the_report_of_an_unknown_folder_uses_fixed_text_only(self) -> None:
        self.v.mkfile(self.folder, "b.txt")
        self.create_then_fail(RuntimeError("leaks C:/secret"))
        pins = [
            {"source": "F/a.pdf", "destination": "F/Documents/a.pdf", "create": "F/Documents"},
            {"source": "F/b.txt", "destination": "F/Documents/b.txt", "create": "F/Documents"},
        ]
        batch = self.run_ops([self.op(create=True), self.op("b.txt", create=True)])
        text = fo.render_report(batch, pins)
        self.assertIn("tried to create folder", text)
        self.assertIn("cannot confirm what happened", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("RuntimeError", text)

    def test_an_expected_refusal_before_the_creation_call_leaves_no_folder_outcome(self) -> None:
        cases = [
            (native.StoppedError(), "STOPPED_AFTER_PENDING"),
            (native.InvalidName(), "MALFORMED"),
            (native.OptionsError(), "MALFORMED"),
        ]
        for error, reason in cases:
            with self.subTest(error=type(error).__name__):
                def hook(volume: FakeVolume, detail: dict, error=error) -> None:
                    if detail["disposition"] == native.FILE_CREATE:
                        raise error

                self.v.hooks["open_relative"] = [hook]
                batch = self.run_ops([self.op(create=True)])
                outcome = batch.outcomes[0]
                self.assertEqual((outcome.status, outcome.reason, outcome.folder), ("refused", reason, "none"))
                self.assertEqual(batch.folders_unconfirmed, 0)
                self.assertEqual(batch.failure_types, [])
                self.assertNotIn("documents", self.folder.children)

    def test_an_unexpected_exception_while_verifying_a_created_folder_stays_unconfirmed(self) -> None:
        self.v.mkfile(self.folder, "b.txt")

        def fail(volume: FakeVolume, detail: dict) -> None:
            if volume.handles[detail["handle"]].name == "Documents":
                raise RuntimeError("leaks C:/secret")

        self.v.hooks["info"] = [fail]
        batch = self.run_ops([self.op(create=True), self.op("b.txt", create=True)])
        first = batch.outcomes[0]
        self.assertEqual((first.folder, first.status, first.reason), ("unconfirmed", "refused", "UNEXPECTED"))
        self.assertEqual(batch.outcomes[1].status, "not_attempted")
        self.assertEqual((batch.folders_created, batch.folders_unconfirmed), (0, 1))
        self.assertEqual(self.v.counts["rename"], 0)
        self.assertTrue(all(count == 1 for count in self.v.close_calls.values()))


# ---- pending requests -------------------------------------------------------------------
class PendingTests(OrganizeCase):
    def test_a_pending_chain_open_refuses_everything_and_latches(self) -> None:
        def pend(volume: FakeVolume, detail: dict) -> None:
            raise native.PendingError("NtCreateFile", 0x103, "nt")

        self.v.hooks["open_relative"] = [pend]
        batch = self.run_ops([self.op()])
        self.assertEqual(batch.outcomes[0].status, "refused")
        self.assertTrue(batch.pending)
        self.assertTrue(native.pending_latched())
        self.assertEqual(self.v.counts["rename"], 0)

    def test_a_pending_category_creation_leaves_the_folder_unknown_and_stops(self) -> None:
        def pend(volume: FakeVolume, detail: dict) -> None:
            if detail["disposition"] == native.FILE_CREATE:
                raise native.PendingError("NtCreateFile", 0x103, "nt")

        self.v.mkfile(self.folder, "b.txt")
        self.v.hooks["open_relative"] = [pend]
        batch = self.run_ops([self.op(create=True), self.op("b.txt", create=True)])
        self.assertEqual(batch.outcomes[0].folder, "unknown")
        self.assertEqual(batch.outcomes[1].status, "not_attempted")
        self.assertTrue(batch.pending)
        self.assertEqual(self.v.counts["rename"], 0)

    def test_a_pending_source_open_stops_before_any_rename(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        opens: list[int] = []

        def pend(volume: FakeVolume, detail: dict) -> None:
            if detail["name"] == "a.pdf" and detail["access"] == native.SOURCE_ACCESS:
                opens.append(1)
                if len(opens) == 2:  # the first is the preflight's
                    raise native.PendingError("NtCreateFile", 0x103, "nt")

        self.v.hooks["open_relative"] = [pend]
        batch = self.run_ops([self.op()])
        self.assertTrue(batch.pending)
        self.assertEqual(self.v.counts["rename"], 0)
        self.assertEqual(batch.outcomes[0].status, "refused")
        self.assertTrue(native.pending_latched())

    def test_a_pending_destination_lookup_stops_before_any_rename(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        lookups: list[int] = []

        def pend(volume: FakeVolume, detail: dict) -> None:
            if detail["name"] == "a.pdf" and detail["access"] == native.LOOKUP_ACCESS:
                lookups.append(1)
                if len(lookups) == 2:  # the first is the preflight's
                    raise native.PendingError("NtCreateFile", 0x103, "nt")

        self.v.hooks["open_relative"] = [pend]
        batch = self.run_ops([self.op()])
        self.assertTrue(batch.pending)
        self.assertEqual(self.v.counts["rename"], 0)
        self.assertTrue(native.pending_latched())

    def test_a_later_batch_refuses_at_once_after_a_pending_request(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        native.latch_pending()
        batch = self.run_ops([self.op()])
        self.assertEqual(batch.outcomes[0].reason, "STOPPED_AFTER_PENDING")
        self.assertEqual(self.v.counts["create_file"], 0)
        self.assertEqual(self.v.counts["rename"], 0)

    def test_an_offer_after_a_pending_request_is_refused_without_a_native_call(self) -> None:
        native.latch_pending()
        offer = fo.plan_offer(self.v, ROOT_PATH, ("F",))
        self.assertEqual(offer.refusal, "STOPPED_AFTER_PENDING")
        self.assertEqual(self.v.counts["create_file"], 0)

    def test_a_pending_or_interrupted_batch_is_never_a_success(self) -> None:
        batch = fo.BatchResult([fo.OperationOutcome("moved")])
        self.assertTrue(batch.success)
        batch.pending = True
        self.assertFalse(batch.success)
        batch.pending, batch.interrupted = False, True
        self.assertFalse(batch.success)


# ---- the one post-move read -----------------------------------------------------------
class PostMoveReadTests(OrganizeCase):
    def setUp(self) -> None:
        super().setUp()
        self.v.mkdir(self.folder, "Documents")

    def status(self, mode: str) -> fo.OperationOutcome:
        self.v.after_info = mode
        return self.run_ops([self.op()]).outcomes[0]

    def test_a_failed_final_read_is_reported_but_unconfirmed(self) -> None:
        outcome = self.status("raise")
        self.assertEqual((outcome.status, outcome.reason), ("reported_unconfirmed", "POST_MOVE_READ_FAILED"))

    def test_a_zero_link_count_is_never_an_extra_name_or_a_clean_success(self) -> None:
        outcome = self.status("zero")
        self.assertEqual(outcome.status, "reported_unconfirmed")
        self.assertFalse(outcome.extra_name)

    def test_an_implausible_link_count_is_conservative(self) -> None:
        outcome = self.status("huge")
        self.assertEqual(outcome.status, "reported_unconfirmed")
        self.assertFalse(outcome.extra_name)

    def test_a_changed_identity_in_the_final_read_is_conservative(self) -> None:
        self.assertEqual(self.status("identity").status, "reported_unconfirmed")

    def test_only_a_count_above_one_is_an_extra_name(self) -> None:
        self.v.hooks["rename"] = [lambda volume, detail: setattr(self.file1, "links", 2)]
        outcome = self.run_ops([self.op()]).outcomes[0]
        self.assertEqual((outcome.status, outcome.extra_name), ("moved", True))

    def test_the_largest_plausible_count_is_still_an_extra_name(self) -> None:
        self.v.hooks["rename"] = [
            lambda volume, detail: setattr(self.file1, "links", fo.MAXIMUM_LINKS)
        ]
        outcome = self.run_ops([self.op()]).outcomes[0]
        self.assertEqual((outcome.status, outcome.extra_name), ("moved", True))

    def test_a_count_of_exactly_one_is_a_clean_move(self) -> None:
        outcome = self.run_ops([self.op()]).outcomes[0]
        self.assertEqual((outcome.status, outcome.extra_name), ("moved", False))


# ---- the pins ------------------------------------------------------------------------------
class PinCase(OrganizeCase):
    def setUp(self) -> None:
        super().setUp()
        self.b = self.v.mkfile(self.folder, "b.png")
        self.before = self.tree()

    def pin(self, name="a.pdf", category="Documents", create=True, node=None, **over) -> dict:
        node = node or self.file1
        pin = {
            "source": f"F/{name}",
            "destination": f"F/{category}/{name}",
            "create": f"F/{category}" if create else None,
            "identity": digest(node),
        }
        pin.update(over)
        return pin

    def go(self, pins, protected=None, parts=("F",)) -> fo.BatchResult:
        batch = fo.run_pinned(self.v, ROOT_PATH, parts, pins, protected)
        self.assertEqual(self.v.handles, {})
        return batch

    def refuses(self, pins, code="MALFORMED", protected=None) -> None:
        with self.assertRaises(fo.Refusal) as raised:
            fo.run_pinned(self.v, ROOT_PATH, ("F",), pins, protected)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(sum(self.v.counts.values()), 0, "a native call was made")
        self.assertEqual(self.tree(), self.before)


class PinShapeTests(PinCase):
    def test_a_well_formed_pin_runs(self) -> None:
        batch = self.go([self.pin()])
        self.assertEqual(batch.counts["moved"], 1)
        self.assertEqual(batch.folders_created, 1)

    def test_the_create_path_must_equal_the_destination_category_parent_exactly(self) -> None:
        for create in ("F/Other", "F", "F/Documents/", "G/Documents", "f/documents", "F/Documents/x"):
            with self.subTest(create=create):
                self.setUp()
                pin = self.pin()
                pin["create"] = create
                self.refuses([pin])

    def test_a_pin_may_omit_the_creation_when_the_folder_exists(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.assertEqual(self.go([self.pin(create=False)]).counts["moved"], 1)

    def test_a_malformed_pin_shape_is_refused_before_any_native_call(self) -> None:
        good = self.pin()
        bad_pins = [
            [], None, "x", (good,), [good] * 6, [None], ["x"], [{}],
            [{k: v for k, v in good.items() if k != "source"}],
            [{k: v for k, v in good.items() if k != "identity"}],
            [{**good, "extra": 1}],
            [{**good, "source": 1}], [{**good, "destination": None}],
            [{**good, "source": "G/a.pdf"}],
            [{**good, "destination": "F/Documents/other.pdf"}],
            [{**good, "destination": "F/Images/a.pdf"}],
            [{**good, "destination": "F/a.pdf"}],
            [{**good, "source": "F/sub/a.pdf"}],
            [{**good, "source": "F/"}],
        ]
        for pins in bad_pins:
            with self.subTest(pins=repr(pins)[:60]):
                self.setUp()
                self.refuses(pins)

    def test_an_unlisted_extension_is_refused(self) -> None:
        node = self.v.mkfile(self.folder, "x.exe")
        self.before = self.tree()
        self.refuses([self.pin("x.exe", "Documents", node=node)])

    def test_a_duplicate_source_or_destination_is_refused_after_case_folding(self) -> None:
        self.refuses([self.pin(), self.pin()], "DUPLICATE")
        upper = self.pin()
        upper["source"] = "F/A.PDF"
        upper["destination"] = "F/Documents/A.PDF"
        self.refuses([self.pin(), upper], "DUPLICATE")

    def test_a_full_five_operation_batch_runs_and_a_sixth_is_refused(self) -> None:
        names = [f"n{i}.txt" for i in range(5)]
        nodes = [self.v.mkfile(self.folder, name) for name in names]
        pins = [self.pin(n, node=node) for n, node in zip(names, nodes)]  # each carries its creation
        batch = self.go(pins)
        self.assertEqual(batch.counts["moved"], 5)
        self.assertEqual(batch.folders_created, 1)

    def test_an_operation_without_its_own_creation_cannot_rely_on_another_operations(self) -> None:
        nodes = [self.v.mkfile(self.folder, name) for name in ("n0.txt", "n1.txt")]
        self.before = self.tree()
        batch = self.go([self.pin("n0.txt", node=nodes[0]), self.pin("n1.txt", create=False, node=nodes[1])])
        self.assertEqual(batch.preflight_index, 2)
        self.assertEqual(batch.outcomes[1].reason, "FOLDER_NOT_APPROVED")
        self.assertEqual(self.tree(), self.before)

    def test_a_sixth_operation_is_refused(self) -> None:
        names = [f"n{i}.txt" for i in range(6)]
        nodes = [self.v.mkfile(self.folder, name) for name in names]
        self.before = self.tree()
        self.refuses([self.pin(n, node=node) for n, node in zip(names, nodes)])

    def test_the_digest_must_be_exactly_64_lower_hex_characters(self) -> None:
        good = digest(self.file1)
        for bad in (
            good + "\n", good + " ", "\n" + good, good.upper(), good[:-1], good + "0",
            "", "g" * 64, None, 5, good.encode(),
        ):
            with self.subTest(identity=ascii(bad)):
                self.setUp()
                self.refuses([self.pin(identity=bad)])

    def test_a_digest_plus_a_trailing_newline_is_refused_before_any_native_call(self) -> None:
        self.refuses([self.pin(identity=digest(self.file1) + "\n")])

    def test_a_stale_identity_is_refused_at_execution_and_changes_nothing(self) -> None:
        self.file1.size = 99
        batch = self.go([self.pin(identity=digest(self.b))])
        self.assertEqual(batch.preflight_index, 1)
        self.assertEqual(self.v.counts["rename"], 0)
        self.assertEqual(self.tree(), self.before)

    def test_the_folder_parts_are_checked_as_names(self) -> None:
        for parts in (("..",), ("a:b",), ("a", ""), ("x\\y",)):
            with self.subTest(parts=parts):
                with self.assertRaises(fo.Refusal):
                    fo.validate_pins(ROOT_PATH, parts, [self.pin()])


class ProtectionTests(PinCase):
    def test_every_prefix_of_every_path_is_asked_of_the_predicate_before_any_native_call(self) -> None:
        asked: list[str] = []

        def protect(path: str) -> bool:
            asked.append(path)
            return False

        fo.validate_pins(ROOT_PATH, ("F",), [self.pin()], protect)
        for expected in (
            ROOT_PATH,
            ROOT_PATH + "\\F",
            ROOT_PATH + "\\F\\a.pdf",
            ROOT_PATH + "\\F\\Documents",
            ROOT_PATH + "\\F\\Documents\\a.pdf",
        ):
            self.assertIn(expected, asked)
        self.assertEqual(sum(self.v.counts.values()), 0)

    def test_a_protected_source_destination_creation_path_or_prefix_changes_nothing(self) -> None:
        for fragment in ("\\F\\a.pdf", "\\F\\Documents\\a.pdf", "\\F\\Documents", "\\F", ""):
            with self.subTest(fragment=fragment):
                target = ROOT_PATH + fragment
                self.setUp()
                self.refuses([self.pin()], "EXCLUDED", lambda path, t=target: path == t)

    def test_a_protected_destination_without_a_creation_is_still_refused(self) -> None:
        target = ROOT_PATH + "\\F\\Documents\\a.pdf"
        self.refuses([self.pin(create=False)], "EXCLUDED", lambda path: path == target)

    def test_the_final_path_of_a_held_directory_is_asked_too(self) -> None:
        batch = fo.run_batch(
            self.v, ROOT_PATH, ("F",), [self.op()],
            lambda path: path.endswith("\\F"),
        )
        self.assertEqual(batch.outcomes[0].reason, "EXCLUDED")
        self.assertEqual(self.v.counts["rename"], 0)

    def test_a_protected_root_final_path_is_refused_before_the_ntfs_query(self) -> None:
        batch = fo.run_batch(
            self.v, ROOT_PATH, ("F",), [self.op()], lambda path: path.endswith("root")
        )
        self.assertEqual(batch.outcomes[0].reason, "EXCLUDED")
        self.assertEqual(self.v.filesystem_calls, 0)


class CategoryFinalPathTests(OrganizeCase):
    def test_an_existing_category_whose_final_path_is_an_alias_is_refused(self) -> None:
        docs = self.v.mkdir(self.folder, "Documents")
        docs.final_name = "DOCUME~1"
        self.assertEqual(self.run_ops([self.op()]).outcomes[0].reason, "LINKED")
        self.assertEqual(self.v.counts["rename"], 0)

    def test_a_created_category_is_verified_against_the_parent_path_plus_the_category(self) -> None:
        def alias(volume: FakeVolume, detail: dict) -> None:
            node = volume.handles[detail["handle"]]
            if node.name == "Documents":
                node.final_name = "DOCUME~1"

        self.v.hooks["info"] = [alias]
        batch = self.run_ops([self.op(create=True)])
        self.assertEqual(batch.outcomes[0].folder, "unconfirmed")
        self.assertEqual(self.v.counts["rename"], 0)

    def test_a_category_reopened_after_a_creation_collision_is_verified_too(self) -> None:
        def race(volume: FakeVolume, detail: dict) -> None:
            if detail["disposition"] == native.FILE_CREATE:
                made = volume.mkdir(self.folder, "Documents")
                made.final_name = "DOCUME~1"

        self.v.hooks["open_relative"] = [race]
        batch = self.run_ops([self.op(create=True)])
        self.assertEqual(batch.outcomes[0].status, "refused")
        self.assertEqual(self.v.counts["rename"], 0)

    def test_a_collision_reopen_that_is_a_plain_folder_is_used_without_creating(self) -> None:
        def race(volume: FakeVolume, detail: dict) -> None:
            if detail["disposition"] == native.FILE_CREATE:
                volume.mkdir(self.folder, "Documents")

        self.v.hooks["open_relative"] = [race]
        batch = self.run_ops([self.op(create=True)])
        self.assertEqual(batch.outcomes[0].status, "moved")
        self.assertEqual(batch.folders_created, 0)

    def test_a_category_that_differs_only_in_case_from_the_disk_name_is_accepted(self) -> None:
        self.v.mkdir(self.folder, "documents")
        self.assertEqual(self.run_ops([self.op()]).outcomes[0].status, "moved")

    def test_a_protected_category_final_path_is_refused(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        batch = fo.run_batch(
            self.v, ROOT_PATH, ("F",), [self.op()],
            lambda path: path.endswith("\\F\\Documents"),
        )
        self.assertEqual(batch.outcomes[0].reason, "EXCLUDED")
        self.assertEqual(self.v.counts["rename"], 0)


# ---- the NTFS policy ---------------------------------------------------------------------
class FilesystemPolicyTests(OrganizeCase):
    def test_ntfs_is_accepted_and_queried_once_on_the_root(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.assertEqual(self.run_ops([self.op()]).counts["moved"], 1)
        self.assertEqual(self.v.filesystem_calls, 1)

    def test_a_non_ntfs_or_unreadable_root_refuses_before_any_creation_or_rename(self) -> None:
        for label, setup in (
            ("exfat", lambda: setattr(self.v, "filesystem", "exFAT")),
            ("fat32", lambda: setattr(self.v, "filesystem", "FAT32")),
            ("refs", lambda: setattr(self.v, "filesystem", "ReFS")),
            ("lowercase", lambda: setattr(self.v, "filesystem", "ntfs")),
            ("padded", lambda: setattr(self.v, "filesystem", "NTFS ")),
            ("empty", lambda: setattr(self.v, "filesystem", "")),
            ("error", lambda: setattr(self.v, "filesystem_error", True)),
        ):
            with self.subTest(label):
                self.setUp()
                setup()
                before = self.tree()
                batch = self.run_ops([self.op(create=True)])
                self.assertEqual(batch.outcomes[0].reason, "FILESYSTEM_UNSUPPORTED")
                self.assertEqual(self.v.counts["rename"], 0)
                self.assertEqual(self.tree(), before)

    def test_the_offer_uses_the_same_root_open_and_the_same_refusal(self) -> None:
        self.v.filesystem = "exFAT"
        offer = fo.plan_offer(self.v, ROOT_PATH, ("F",), scandir=fake_scandir(self.folder))
        self.assertEqual(offer.refusal, "FILESYSTEM_UNSUPPORTED")
        self.assertEqual(offer.pins, ())
        self.assertEqual(self.v.handles, {})


# ---- the offer ------------------------------------------------------------------------------
class OfferCase(OrganizeCase):
    def offer(self, extra: list[str] | None = None, protected=None, folder: Node | None = None,
              parts: tuple[str, ...] = ("F",)) -> fo.Offer:
        scandir = fake_scandir(folder or self.folder, extra)
        offer = fo.plan_offer(self.v, ROOT_PATH, parts, protected, scandir)
        self.assertEqual(self.v.handles, {}, "an offer left a handle open")
        self.last_scandir = scandir
        return offer


class OfferTests(OfferCase):
    def test_an_eligible_file_is_pinned_with_its_creation_and_digest(self) -> None:
        offer = self.offer()
        self.assertIsNone(offer.refusal)
        self.assertEqual(
            list(offer.pins),
            [{
                "source": "F/a.pdf",
                "destination": "F/Documents/a.pdf",
                "create": "F/Documents",
                "identity": digest(self.file1),
            }],
        )
        self.assertEqual(offer.planned, 1)
        self.assertEqual(self.v.counts["rename"], 0)

    def test_an_existing_category_needs_no_creation(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.assertIsNone(self.offer().pins[0]["create"])

    def test_the_offer_changes_nothing_and_never_creates_a_folder(self) -> None:
        before = self.tree()
        self.offer()
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.v.counts["rename"], 0)

    def test_the_selection_is_in_case_folded_name_order_and_bounded_at_five(self) -> None:
        for name in ("e.txt", "B.txt", "d.txt", "C.txt", "f.txt", "A.txt"):
            self.v.mkfile(self.folder, name)
        offer = self.offer()
        self.assertEqual(
            [pin["source"] for pin in offer.pins],
            ["F/a.pdf", "F/A.txt", "F/B.txt", "F/C.txt", "F/d.txt"],
        )
        self.assertEqual(offer.remaining, 2)
        self.assertIn("2 more eligible", offer.preview)

    def test_only_listed_extensions_are_eligible_by_their_last_extension(self) -> None:
        for name in ("x.exe", "noext", "a.tar.exe", "photo.JPG", "song.Mp3", "x.pdf.bak"):
            self.v.mkfile(self.folder, name)
        names = [pin["source"] for pin in self.offer().pins]
        self.assertIn("F/photo.JPG", names)
        self.assertIn("F/song.Mp3", names)
        self.assertNotIn("F/x.exe", names)
        self.assertNotIn("F/noext", names)
        self.assertNotIn("F/a.tar.exe", names)
        self.assertNotIn("F/x.pdf.bak", names)

    def test_the_category_table_maps_each_extension_to_its_folder(self) -> None:
        for name, folder in (
            ("a.docx", "Documents"), ("a.heic", "Images"), ("a.flac", "Audio"),
            ("a.webm", "Video"), ("a.7z", "Archives"), ("a.GZ", "Archives"),
        ):
            self.assertEqual(fo.category_for(name), folder)
        for name in ("a", ".pdf.x", "a.", "a.exe", "pdf"):
            self.assertIsNone(fo.category_for(name))

    def test_ineligible_entries_are_never_offered(self) -> None:
        self.v.mkdir(self.folder, "dir.pdf")
        hidden = self.v.mkfile(self.folder, "hidden.pdf")
        hidden.hidden = True
        linked = self.v.mkfile(self.folder, "linked.pdf")
        linked.links = 2
        reparse = self.v.mkfile(self.folder, "reparse.pdf")
        reparse.reparse = True
        other = self.v.mkfile(self.folder, "volume.pdf")
        other.volume = 8
        zero = self.v.mkfile(self.folder, "zero.pdf")
        zero.index = 0
        names = [pin["source"] for pin in self.offer().pins]
        self.assertEqual(names, ["F/a.pdf"])

    def test_a_file_whose_destination_exists_is_never_offered(self) -> None:
        docs = self.v.mkdir(self.folder, "Documents")
        self.v.mkfile(docs, "a.pdf")
        self.v.mkfile(self.folder, "b.txt")
        self.assertEqual([p["source"] for p in self.offer().pins], ["F/b.txt"])

    def test_a_category_that_is_not_a_plain_directory_offers_nothing_for_it(self) -> None:
        self.v.mkfile(self.folder, "Documents")
        self.assertEqual(self.offer().refusal, "NOTHING_ELIGIBLE")

    def test_nothing_eligible_says_so_and_pins_nothing(self) -> None:
        self.file1.hidden = True
        offer = self.offer()
        self.assertEqual((offer.refusal, offer.pins), ("NOTHING_ELIGIBLE", ()))
        self.assertIn("approving changes nothing", offer.preview)

    def test_more_than_a_thousand_entries_is_refused_and_the_listing_stops_early(self) -> None:
        extra = [f"n{i}.txt" for i in range(1000)]
        offer = self.offer(extra)  # a.pdf plus 1,000 more: 1,001 entries
        self.assertEqual(offer.refusal, "TOO_LARGE")
        self.assertEqual(self.last_scandir.listings[0].yielded, 1001)

    def test_exactly_a_thousand_entries_is_accepted(self) -> None:
        extra = [f"n{i}.xyz" for i in range(999)]
        offer = self.offer(extra)
        self.assertIsNone(offer.refusal)

    def test_a_listed_name_that_no_longer_exists_is_skipped(self) -> None:
        offer = self.offer(["ghost.pdf"])
        self.assertEqual([p["source"] for p in offer.pins], ["F/a.pdf"])

    def test_a_protected_path_is_never_offered(self) -> None:
        self.v.mkfile(self.folder, "b.txt")
        offer = self.offer(protected=lambda path: path.endswith("\\F\\a.pdf"))
        self.assertEqual([p["source"] for p in offer.pins], ["F/b.txt"])

    def test_a_protected_destination_is_never_offered(self) -> None:
        offer = self.offer(protected=lambda path: path.endswith("\\F\\Documents\\a.pdf"))
        self.assertEqual(offer.refusal, "NOTHING_ELIGIBLE")

    def test_the_root_itself_can_be_organized(self) -> None:
        self.v.mkfile(self.v.root, "top.pdf")
        offer = self.offer(folder=self.v.root, parts=())
        self.assertEqual(offer.pins[0]["source"], "top.pdf")
        self.assertEqual(offer.pins[0]["destination"], "Documents/top.pdf")
        self.assertEqual(offer.pins[0]["create"], "Documents")

    def test_a_non_bmp_name_is_offered_and_quoted_as_one_line(self) -> None:
        self.v.mkfile(self.folder, NAME_NON_BMP)
        offer = self.offer()
        self.assertTrue(any(pin["source"].endswith(NAME_NON_BMP) for pin in offer.pins))
        self.assertEqual(len(offer.preview.splitlines()), 1 + len(offer.pins))

    def test_a_name_with_a_bidi_or_control_character_is_escaped_in_the_preview(self) -> None:
        bidi, delete = chr(0x202E), chr(0x7F)
        self.v.mkfile(self.folder, "x" + bidi + "fdp.exe.pdf")
        self.v.mkfile(self.folder, "y" + delete + ".txt")
        preview = self.offer().preview
        for raw in (bidi, delete):
            self.assertNotIn(raw, preview)
        self.assertIn(chr(92) + "u202e", preview)
        self.assertEqual(len(preview.splitlines()), 1 + 3)

    def test_a_pending_request_while_preparing_the_offer_latches_and_pins_nothing(self) -> None:
        def pend(volume: FakeVolume, detail: dict) -> None:
            if detail["name"] == "a.pdf" and detail["access"] == native.LOOKUP_ACCESS:
                raise native.PendingError("NtCreateFile", 0x103, "nt")

        self.v.hooks["open_relative"] = [pend]

        offer = self.offer()

        self.assertEqual((offer.refusal, offer.pins), ("STOPPED_AFTER_PENDING", ()))
        self.assertTrue(native.pending_latched())

    def test_a_pending_category_open_while_preparing_the_offer_latches_too(self) -> None:
        self.v.mkdir(self.folder, "Documents")

        def pend(volume: FakeVolume, detail: dict) -> None:
            if detail["name"] == "Documents" and detail["access"] != native.LOOKUP_ACCESS:
                raise native.PendingError("NtCreateFile", 0x103, "nt")

        self.v.hooks["open_relative"] = [pend]

        offer = self.offer()

        self.assertEqual(offer.refusal, "STOPPED_AFTER_PENDING")
        self.assertTrue(native.pending_latched())

    def test_an_interruption_while_preparing_an_offer_is_not_swallowed_by_a_release_failure(
        self,
    ) -> None:
        self.v.mkdir(self.folder, "Documents")

        def interrupt(volume: FakeVolume, detail: dict) -> None:
            if volume.handles[detail["handle"]].name == "Documents":
                raise KeyboardInterrupt

        self.v.hooks["info"] = [interrupt]
        self.v.close_fail_purposes = {"lookup"}

        with self.assertRaises(KeyboardInterrupt):
            fo.plan_offer(self.v, ROOT_PATH, ("F",), None, fake_scandir(self.folder))

        self.assertEqual(self.v.handles, {})

    def test_a_hostile_listed_name_is_skipped(self) -> None:
        offer = self.offer(["a\nb.pdf", "..\\x.pdf", "c:d.pdf"])
        self.assertEqual([p["source"] for p in offer.pins], ["F/a.pdf"])

    def test_a_close_failure_while_preparing_the_offer_raises_so_no_pins_are_offered(self) -> None:
        self.v.close_fail_purposes = {"lookup"}
        with self.assertRaises(RuntimeError):
            self.offer()

    def test_a_refused_folder_gives_a_fixed_refusal_and_no_pins(self) -> None:
        self.folder.reparse = True
        offer = self.offer()
        self.assertEqual((offer.refusal, offer.pins), ("LINKED", ()))
        self.assertIn(fo.REFUSAL_TEXTS["LINKED"], offer.preview)

    def test_the_offer_never_holds_a_handle_when_it_returns(self) -> None:
        for _ in range(3):
            self.offer()
        self.assertEqual(self.v.handles, {})

    def test_the_offered_pins_run_through_the_same_validation_and_batch(self) -> None:
        self.v.mkfile(self.folder, "b.png")
        offer = self.offer()
        batch = fo.run_pinned(self.v, ROOT_PATH, ("F",), list(offer.pins))
        self.assertEqual(batch.counts["moved"], 2)
        self.assertTrue(batch.success)

    def test_a_file_added_after_the_offer_is_never_moved(self) -> None:
        offer = self.offer()
        self.v.mkfile(self.folder, "late.txt")
        batch = fo.run_pinned(self.v, ROOT_PATH, ("F",), list(offer.pins))
        self.assertEqual(batch.counts["moved"], 1)
        self.assertIn("late.txt", self.folder.children)

    def test_a_pinned_file_replaced_after_the_offer_is_refused(self) -> None:
        offer = self.offer()
        del self.folder.children["a.pdf"]
        self.v.mkfile(self.folder, "a.pdf")
        batch = fo.run_pinned(self.v, ROOT_PATH, ("F",), list(offer.pins))
        self.assertEqual(batch.outcomes[0].reason, "SOURCE_CHANGED")
        self.assertEqual(self.v.counts["rename"], 0)


# ---- reports and recorded text ---------------------------------------------------------------
class ReportCase(PinCase):
    def report(self, pins=None) -> tuple[str, fo.BatchResult]:
        pins = pins if pins is not None else [self.pin()]
        batch = self.go(pins)
        return fo.render_report(batch, pins), batch


class ReportTests(ReportCase):
    def test_a_clean_run_reports_each_move_and_a_clean_closing_line(self) -> None:
        pins = [self.pin(), self.pin("b.png", "Images", node=self.b)]
        text, batch = self.report(pins)
        self.assertIn('1. Moved "F/a.pdf" to "F/Documents/a.pdf".', text)
        self.assertIn('Created folder "F/Images".', text)
        self.assertIn("All 2 planned moves completed.", text)
        self.assertTrue(batch.success)

    def test_a_stopped_run_names_every_bucket_and_says_nothing_was_undone(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.failures[("rename", 1)] = AccessError("NtSetInformationFile", DENIED, "nt")
        pins = [self.pin(create=False), self.pin("b.png", "Images", node=self.b)]
        text, batch = self.report(pins)
        self.assertIn("Windows refused the move: Access is denied.", text)
        self.assertIn("2. Not attempted:", text)
        self.assertIn("Nothing was undone.", text)
        self.assertIn("1 not moved after an attempt", text)
        self.assertIn("1 not attempted", text)

    def test_an_unknown_outcome_says_to_check_both_places(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.pending_on_rename = True
        text, _ = self.report([self.pin(create=False)])
        self.assertIn("cannot confirm what happened. Check both places.", text)

    def test_a_reported_but_unconfirmed_move_is_never_called_moved(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.fail_lookups_after_rename = True
        text, _ = self.report([self.pin(create=False)])
        self.assertIn("Windows reported moving", text)
        self.assertNotIn("1. Moved", text)

    def test_an_extra_name_is_a_warning_and_the_closing_line_is_not_clean(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.hooks["rename"] = [lambda volume, detail: setattr(self.file1, "links", 2)]
        text, batch = self.report([self.pin(create=False)])
        self.assertIn("also had another name", text)
        self.assertIn("1 warning(s) above prevent calling this a clean success", text)
        self.assertNotIn("All 1 planned moves completed.", text)

    def test_a_release_failure_is_reported_as_a_warning(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.close_fails.add(id(self.folder))
        text, _ = self.report([self.pin(create=False)])
        self.assertIn("could not release a file handle", text)
        self.assertNotIn("All 1 planned moves completed.", text)

    def test_a_preflight_refusal_names_the_operation_and_says_nothing_changed(self) -> None:
        text, _ = self.report([self.pin(), self.pin("b.png", "Images", node=self.b, identity="0" * 64)])
        self.assertIn("Nothing was changed: move 2 cannot run.", text)

    def test_a_folder_unconfirmed_and_not_created_have_their_own_lines(self) -> None:
        def corrupt(volume: FakeVolume, detail: dict) -> None:
            node = volume.handles[detail["handle"]]
            if node.name == "Documents":
                node.reparse = True

        self.v.hooks["info"] = [corrupt]
        text, _ = self.report()
        self.assertIn("Windows reported creating folder", text)

    def test_every_refusal_code_has_a_fixed_text_without_a_placeholder(self) -> None:
        for code, text in fo.REFUSAL_TEXTS.items():
            with self.subTest(code):
                self.assertNotIn("{", text)
                self.assertTrue(text.endswith("."))
        for code in ("FOLDER_NOT_CREATED", "FOLDER_UNCONFIRMED"):
            self.assertIn(code, fo.REFUSAL_TEXTS)

    def test_a_native_code_has_a_fixed_reason_or_the_unspecified_one(self) -> None:
        self.assertEqual(fo.native_reason(DENIED), "Access is denied.")
        self.assertEqual(fo.native_reason(0xC0001234), fo.UNSPECIFIED_REASON)
        self.assertNotIn("0x", fo.native_reason(0xC0001234))

    def test_no_report_text_carries_a_root_name_or_a_host_path(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.failures[("rename", 1)] = AccessError("NtSetInformationFile", DENIED, "nt")
        text, _ = self.report([self.pin(create=False)])
        self.assertNotIn("fake", text)
        self.assertNotIn("C:\\", text)
        self.assertNotIn("\\\\?\\", text)


class RecordedTextTests(ReportCase):
    def test_the_recorded_response_is_counts_only(self) -> None:
        pins = [self.pin(), self.pin("b.png", "Images", node=self.b)]
        batch = self.go(pins)
        recorded = fo.recorded_response(batch)
        for forbidden in ("a.pdf", "b.png", "Documents", "Images", "F/"):
            self.assertNotIn(forbidden, recorded)
        self.assertIn("2 planned; 2 moved", recorded)

    def test_the_recorded_response_of_a_refusal_has_no_names(self) -> None:
        batch = self.go([self.pin(identity="0" * 64)])
        recorded = fo.recorded_response(batch)
        self.assertEqual(recorded, "Organize folder: refused before any change.")

    def test_the_recorded_response_counts_every_bucket(self) -> None:
        self.v.mkdir(self.folder, "Documents")
        self.v.failures[("rename", 1)] = AccessError("NtSetInformationFile", DENIED, "nt")
        batch = self.go([self.pin(create=False), self.pin("b.png", "Images", node=self.b)])
        recorded = fo.recorded_response(batch)
        self.assertIn("0 moved", recorded)
        self.assertIn("1 not moved after an attempt", recorded)
        self.assertIn("1 not attempted", recorded)

    def test_the_count_line_is_fixed_text_with_numbers_only(self) -> None:
        finished = fo.count_line(fo.BatchResult([fo.OperationOutcome("moved")]))
        self.assertTrue(finished.startswith("Organize run finished: moved=1 "))
        interrupted = fo.count_line(
            fo.BatchResult([fo.OperationOutcome("unknown")], interrupted=True)
        )
        self.assertTrue(interrupted.startswith("Organize run interrupted: moved=0 "))
        for line in (finished, interrupted):
            self.assertNotRegex(line, r"[A-Za-z]:" + chr(92) * 2)
            self.assertNotRegex(line, "[/" + chr(92) * 2 + "]")

    def test_a_refused_preview_and_report_are_fixed_text(self) -> None:
        preview = fo.refused_preview(fo.REFUSAL_TEXTS["LINKED"], ("F",))
        self.assertEqual(
            preview,
            'Organize folder "F": ' + fo.REFUSAL_TEXTS["LINKED"] + " Approving changes nothing.",
        )
        bare = fo.refused_preview("Path is outside Maria's configured safe root.")
        self.assertEqual(
            bare,
            "Organize folder: Path is outside Maria's configured safe root. Approving changes nothing.",
        )
        self.assertEqual(fo.refused_report("NOTHING"), fo.REFUSAL_TEXTS["NOTHING"])
        self.assertEqual(fo.refused_report("MALFORMED"), fo.REFUSAL_TEXTS["MALFORMED"])
        self.assertEqual(
            fo.refused_report("LINKED"), "Nothing was changed. " + fo.REFUSAL_TEXTS["LINKED"]
        )
        self.assertIn("OFFER_FAILED", fo.REFUSAL_TEXTS)


# ---- the real calls, on temporary folders this test creates ------------------------
class RealOrganizationTests(unittest.TestCase):
    """Organization through the real Windows calls, on temporary NTFS folders only."""

    def setUp(self) -> None:
        native._PENDING_LATCH = False
        self.addCleanup(setattr, native, "_PENDING_LATCH", False)
        self.access = native.load_access()
        if self.access is None:
            self.skipTest("Organizing folders needs 64-bit Windows.")
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "root"
        self.root.mkdir()
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        (self.inbox / "a.pdf").write_bytes(b"pdf bytes")
        (self.inbox / "b.png").write_bytes(b"png bytes")
        handle = self.access.create_file(
            str(self.root), native.ROOT_ACCESS, native.OPEN_DIRECTORY_SHARE,
            native.ROOT_OPEN_FLAGS,
        )
        try:
            name = self.access.filesystem_name(handle)
        finally:
            self.access.close_handle(handle)
        if name != "NTFS":
            self.skipTest("The temporary folder is not on an NTFS volume.")

    def snapshot(self, folder: Path) -> dict[str, object]:
        found: dict[str, object] = {}
        for path in sorted(folder.rglob("*")):
            relative = path.relative_to(folder).as_posix()
            found[relative] = "directory" if path.is_dir() else path.read_bytes()
        return found

    def offer(self, parts: tuple[str, ...] = ("inbox",)) -> fo.Offer:
        return fo.plan_offer(self.access, str(self.root), parts, None, os.scandir)

    def organize(self, offer: fo.Offer, parts: tuple[str, ...] = ("inbox",)) -> fo.BatchResult:
        return fo.run_pinned(self.access, str(self.root), parts, list(offer.pins), None)

    def junction(self, link: Path, target: Path) -> None:
        if not hasattr(_winapi, "CreateJunction"):
            self.skipTest("This Python cannot make a junction.")
        _winapi.CreateJunction(str(target), str(link))

    def test_an_offered_batch_runs_for_real_and_the_tree_changes_exactly_as_previewed(self) -> None:
        offer = self.offer()
        before = self.snapshot(self.base)

        batch = self.organize(offer)

        self.assertTrue(batch.success)
        expected = dict(before)
        for gone in ("root/inbox/a.pdf", "root/inbox/b.png"):
            del expected[gone]
        expected.update(
            {
                "root/inbox/Documents": "directory",
                "root/inbox/Images": "directory",
                "root/inbox/Documents/a.pdf": b"pdf bytes",
                "root/inbox/Images/b.png": b"png bytes",
            }
        )
        self.assertEqual(self.snapshot(self.base), expected)

    def test_a_destination_that_appears_after_the_offer_is_never_replaced(self) -> None:
        offer = self.offer()
        (self.inbox / "Documents").mkdir()
        (self.inbox / "Documents" / "a.pdf").write_bytes(b"intruder")

        batch = self.organize(offer)

        # The pinned creation of an existing folder is not needed, and the
        # destination now exists, so nothing of a.pdf moved and nothing was replaced.
        self.assertEqual((self.inbox / "Documents" / "a.pdf").read_bytes(), b"intruder")
        self.assertEqual((self.inbox / "a.pdf").read_bytes(), b"pdf bytes")
        self.assertFalse(batch.success)

    def test_a_second_hard_link_before_the_move_refuses_the_whole_batch(self) -> None:
        offer = self.offer()
        os.link(self.inbox / "a.pdf", self.base / "elsewhere.pdf")
        before = self.snapshot(self.base)

        batch = self.organize(offer)

        self.assertEqual(batch.outcomes[0].reason, "HARD_LINKED")
        self.assertEqual(self.snapshot(self.base), before)

    def test_a_folder_component_that_is_a_junction_is_refused_and_its_target_is_untouched(self) -> None:
        target = self.base / "target"
        target.mkdir()
        (target / "a.pdf").write_bytes(b"target file")
        self.junction(self.root / "link", target)
        before = self.snapshot(self.base)
        offer = self.offer(("link",))

        self.assertEqual(offer.refusal, "LINKED")
        batch = fo.run_batch(
            self.access, str(self.root), ("link",),
            [{"name": "a.pdf", "category": "Documents", "create_approved": True}],
        )

        self.assertEqual(batch.outcomes[0].reason, "LINKED")
        self.assertEqual(self.snapshot(self.base), before)

    def test_a_category_folder_that_is_a_junction_is_never_used(self) -> None:
        offer = self.offer()
        target = self.base / "target"
        target.mkdir()
        self.junction(self.inbox / "Documents", target)
        before = self.snapshot(self.base)

        batch = self.organize(offer)

        self.assertEqual(batch.outcomes[0].reason, "FOLDER_NOT_PLAIN")
        self.assertEqual(self.snapshot(self.base), before)
        self.assertEqual(list(target.iterdir()), [])

    def test_a_source_replaced_by_a_junction_after_the_offer_is_refused(self) -> None:
        offer = self.offer()
        target = self.base / "target"
        target.mkdir()
        (self.inbox / "a.pdf").unlink()
        self.junction(self.inbox / "a.pdf", target)

        batch = self.organize(offer)

        self.assertEqual(batch.preflight_index, 1)
        self.assertNotEqual(batch.outcomes[0].reason, "")
        self.assertTrue(target.is_dir())
        self.assertFalse((self.inbox / "Documents").exists())

    def test_a_source_in_use_by_a_writer_is_refused_and_everything_else_is_untouched(self) -> None:
        offer = self.offer()
        before = self.snapshot(self.base)

        with open(self.inbox / "a.pdf", "r+b"):
            batch = self.organize(offer)

        self.assertEqual(batch.outcomes[0].reason, "IN_USE")
        self.assertEqual(self.snapshot(self.base), before)

    def test_every_handle_is_released_so_the_tree_can_be_removed_afterwards(self) -> None:
        offer = self.offer()
        self.organize(offer)

        shutil.rmtree(self.base / "root")  # a leaked handle would make this fail

        self.assertFalse((self.base / "root").exists())

    def test_the_offer_holds_nothing_when_it_returns(self) -> None:
        self.offer()

        os.rename(self.inbox, self.root / "renamed")  # a held directory could not move

        self.assertTrue((self.root / "renamed").is_dir())

    def test_a_non_bmp_file_name_moves_for_real(self) -> None:
        name = "a" + chr(0x1F600) + ".txt"
        (self.inbox / name).write_bytes(b"emoji")

        batch = self.organize(self.offer())

        self.assertTrue(batch.success)
        self.assertEqual((self.inbox / "Documents" / name).read_bytes(), b"emoji")


# ---- the fixed texts ----------------------------------------------------------------
class FixedTextTests(unittest.TestCase):
    """The approved wording, pinned word for word (V1-25-R1, section 7)."""

    APPROVED = {
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
        "ROOT_UNCONFIRMED": "Maria could not confirm the workspace folder, so nothing was changed.",
        "NOT_PLAIN_FILE": "The source is not a plain file Maria may move.",
        "SOURCE_CHANGED": "The file is missing or has changed since the moves were offered.",
        "HARD_LINKED": "The file has more than one name, so Maria does not move it.",
        "IN_USE": "The file is in use by another program.",
        "DESTINATION_EXISTS": "Something already exists at the destination.",
        "FOLDER_NOT_APPROVED": "The destination folder is missing, and creating it was not approved.",
        "FOLDER_NOT_PLAIN": "The destination folder is not a plain folder.",
        "DIFFERENT_VOLUME": "The file and its destination are on different volumes.",
        "DUPLICATE": "Two approved moves name the same file or destination.",
    }
    # Wording this implementation had to add; reported to the owner, not in section 7.
    ADDED = {
        "FOLDER_NOT_CREATED": "The destination folder could not be created.",
        "FOLDER_UNCONFIRMED": "The destination folder could not be confirmed.",
        "OFFER_FAILED": "Maria could not read this folder safely.",
        "UNEXPECTED": "Maria met an unexpected failure.",
    }

    def test_every_refusal_is_the_approved_text_and_none_is_unlisted(self) -> None:
        self.assertEqual(fo.REFUSAL_TEXTS, {**self.APPROVED, **self.ADDED})

    def test_the_closing_lines_and_the_warnings_are_the_approved_text(self) -> None:
        self.assertEqual(fo.CLOSING_CLEAN.format(planned=3), "All 3 planned moves completed.")
        self.assertEqual(
            fo.CLOSING_WITH_WARNINGS.format(planned=3, warnings=1),
            "All 3 planned moves were made, but 1 warning(s) above prevent calling "
            "this a clean success. Nothing was undone.",
        )
        self.assertEqual(
            fo.CLOSING_STOPPED.format(
                moved=1, unconfirmed=2, not_moved=3, unknown=4, refused=5,
                not_attempted=6, created=7, folder_unconfirmed=8, warnings=9,
            ),
            "Stopped: 1 moved, 2 reported but unconfirmed, 3 not moved after an "
            "attempt, 4 outcome unknown, 5 refused before an attempt, 6 not "
            "attempted; 7 folders created, 8 unconfirmed; 9 warnings. Nothing was undone.",
        )
        self.assertEqual(
            fo.EXTRA_NAME_TEXT.format(source='"x"'),
            'Warning: "x" was moved. When Maria checked after the move, the file '
            "also had another name that she did not move or remove. Maria stopped; "
            "nothing was undone.",
        )
        self.assertEqual(
            fo.RELEASE_TEXT.format(where="after move 2"),
            "Warning: Maria could not release a file handle (after move 2). The "
            "results above stand, nothing was undone, and she stopped.",
        )
        self.assertEqual(
            fo.NOTHING_ELIGIBLE_TEXT.format(folder='"F"'),
            "Nothing in " + chr(34) + "F" + chr(34) + " matches Maria's file types, "
            "so approving changes nothing.",
        )

    def test_the_six_operation_lines_and_the_folder_lines_are_the_approved_text(self) -> None:
        source, destination = '"F/a.pdf"', '"F/Documents/a.pdf"'
        outcome = fo.OperationOutcome
        lines = {
            "moved": (outcome("moved"), f"1. Moved {source} to {destination}."),
            "reported_unconfirmed": (
                outcome("reported_unconfirmed"),
                f"1. Windows reported moving {source} to {destination}, but Maria could "
                "not complete her checks afterwards and cannot confirm where the file is now.",
            ),
            "not_moved": (
                outcome("not_moved", code=DENIED),
                f"1. Not moved: {source}. Windows refused the move: Access is denied.",
            ),
            "unknown": (
                outcome("unknown"),
                f"1. Maria tried to move {source} to {destination} and cannot confirm "
                "what happened. Check both places.",
            ),
            "refused": (
                outcome("refused", "IN_USE"),
                f"1. Not moved: {source}. The file is in use by another program. "
                "Maria did not try to move it.",
            ),
            "not_attempted": (outcome("not_attempted"), f"1. Not attempted: {source}."),
        }
        for status, (made, expected) in lines.items():
            with self.subTest(status):
                self.assertEqual(fo._operation_line(1, made, source, destination), expected)
        pin = {"create": "F/Documents"}
        folders = {
            "created": 'Created folder "F/Documents".',
            "unconfirmed": (
                'Windows reported creating folder "F/Documents", but Maria could not '
                "confirm it is a plain folder inside the workspace."
            ),
            "unknown": 'Maria tried to create folder "F/Documents" and cannot confirm what happened.',
        }
        for state, expected in folders.items():
            with self.subTest(state):
                self.assertEqual(fo._folder_line(outcome("refused", folder=state), pin), expected)
        self.assertEqual(
            fo._folder_line(outcome("refused", folder="not_created", code=DENIED), pin),
            'Could not create folder "F/Documents": Access is denied.',
        )

    def test_the_preview_is_the_approved_shape(self) -> None:
        pins = [
            {"source": "F/a.pdf", "destination": "F/Documents/a.pdf", "create": "F/Documents"},
            {"source": "F/b.png", "destination": "F/Images/b.png", "create": None},
        ]
        self.assertEqual(
            fo._preview(("F",), pins, 3),
            'Organize folder "F": 2 planned moves (at most 5 per approval); 3 more '
            "eligible files stay for a later request." + chr(10)
            + '1. "F/a.pdf" -> "F/Documents/a.pdf" (creates folder "F/Documents" if it is still missing)'
            + chr(10) + '2. "F/b.png" -> "F/Images/b.png"',
        )


# ---- the module's own limits -------------------------------------------------------------
class SourceScanTests(unittest.TestCase):
    def source(self) -> str:
        import inspect

        return inspect.getsource(fo)

    def test_the_module_calls_no_windows_function_and_logs_nothing(self) -> None:
        text = self.source()
        for forbidden in (
            "ctypes", "CreateFileW", "NtCreateFile", "NtSetInformationFile", "MessageBoxW",
            "import logging", "getLogger", "import threading", "import subprocess",
            "import shutil", "os.rename", "os.replace", "os.remove", "os.unlink", "os.rmdir",
            "os.mkdir", "open(", "import time", "sleep(",
        ):
            with self.subTest(forbidden):
                self.assertNotIn(forbidden, text)

    def test_the_module_imports_no_configuration_protections_or_tool(self) -> None:
        text = self.source()
        for forbidden in (
            "core.config", "filesystem_roots", "core.permissions", "tool_manager",
            "tools.", "core.brain", "core.memory", "sqlite3", "project_file_access",
        ):
            with self.subTest(forbidden):
                self.assertNotIn(forbidden, text)

    def test_the_limits_are_the_approved_ones(self) -> None:
        self.assertEqual(
            (fo.MAXIMUM_OPERATIONS, fo.MAXIMUM_ENTRIES, fo.MAXIMUM_LINKS, fo.REQUIRED_FILESYSTEM),
            (5, 1000, 1024, "NTFS"),
        )
        self.assertEqual(
            fo.STATUSES,
            ("moved", "reported_unconfirmed", "not_moved", "unknown", "refused", "not_attempted"),
        )


if __name__ == "__main__":
    unittest.main()
