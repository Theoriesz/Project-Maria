"""Focused regression tests for Maria's logging helpers."""

from __future__ import annotations

import logging
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch

import core.logger as logger_module
import tests.log_assertions as log_assertions
from core.logger import configure_logging, get_logger
from tests.log_assertions import assert_log_omits


class LoggerTests(unittest.TestCase):
    """Verify file logging behavior without touching Maria's runtime log."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.log_dir = Path(self.temporary_directory.name) / "nested" / "logs"

        self.maria_logger = logging.getLogger("maria")
        self.original_handlers = tuple(self.maria_logger.handlers)
        self.original_level = self.maria_logger.level
        self.original_propagate = self.maria_logger.propagate
        self.original_disabled = self.maria_logger.disabled
        for handler in self.original_handlers:
            self.maria_logger.removeHandler(handler)
        self.addCleanup(self._restore_maria_logger)

    def _restore_maria_logger(self) -> None:
        """Close test handlers and restore non-handler logger settings."""
        _remove_and_close_handlers(self.maria_logger)
        for handler in self.original_handlers:
            self.maria_logger.addHandler(handler)
        self.maria_logger.setLevel(self.original_level)
        self.maria_logger.propagate = self.original_propagate
        self.maria_logger.disabled = self.original_disabled

    def _attach_foreign_file_handler(self) -> tuple[Path, logging.FileHandler]:
        """Attach a real file handler Maria did not create, and clean it up."""
        foreign_dir = Path(self.temporary_directory.name) / "embedder" / "logs"
        foreign_dir.mkdir(parents=True, exist_ok=True)
        foreign_path = foreign_dir / "embedder.log"
        foreign = logging.FileHandler(foreign_path, encoding="utf-8")
        self.addCleanup(self._detach_and_close, foreign)
        self.maria_logger.addHandler(foreign)
        return foreign_path, foreign

    def _detach_and_close(self, handler: logging.Handler) -> None:
        """Detach and close one handler before the temporary directory goes."""
        self.maria_logger.removeHandler(handler)
        handler.close()

    def test_configuration_creates_directory_file_and_uses_utf8(self) -> None:
        logger = configure_logging(self.log_dir)

        self.assertTrue(self.log_dir.is_dir())
        self.assertTrue((self.log_dir / "maria.log").is_file())
        file_handlers = _file_handlers(logger)
        self.assertEqual(len(file_handlers), 1)
        self.assertEqual(file_handlers[0].encoding.lower().replace("-", ""), "utf8")

    def test_unicode_message_and_expected_format_reach_file(self) -> None:
        logger = configure_logging(self.log_dir)
        message = "Unicode survives: Mar\u00eda \u2603 \u6f22\u5b57"

        logger.info(message)
        _flush_handlers(logger)
        lines = (self.log_dir / "maria.log").read_text(encoding="utf-8").splitlines()
        matching_line = next(line for line in lines if message in line)

        self.assertRegex(matching_line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
        self.assertTrue(matching_line.endswith(f" | INFO | maria | {message}"))

    def test_configured_level_and_propagation_are_applied(self) -> None:
        logger = configure_logging(self.log_dir, level="debug")

        self.assertEqual(logger.level, logging.DEBUG)
        self.assertFalse(logger.propagate)

    def test_get_logger_returns_expected_maria_child_name(self) -> None:
        self.assertEqual(get_logger("core.brain").name, "maria.brain")
        self.assertEqual(get_logger("tools.notes").name, "maria.notes")

    def test_child_messages_reach_configured_file(self) -> None:
        logger = configure_logging(self.log_dir)
        child = get_logger("tools.regression")

        child.warning("child-message-sentinel")
        _flush_handlers(logger)

        log_text = (self.log_dir / "maria.log").read_text(encoding="utf-8")
        self.assertIn(" | WARNING | maria.regression | child-message-sentinel", log_text)

    def test_managed_handler_applies_the_bounded_growth_policy(self) -> None:
        logger = configure_logging(self.log_dir)

        handler = _managed_handlers(logger)[0]
        self.assertIsInstance(handler, RotatingFileHandler)
        self.assertEqual(handler.maxBytes, 1 * 1024 * 1024)
        self.assertEqual(handler.backupCount, 3)

    def test_declared_bounded_growth_policy_is_the_shipped_one(self) -> None:
        self.assertEqual(logger_module.LOG_MAX_BYTES, 1048576)
        self.assertEqual(logger_module.LOG_BACKUP_COUNT, 3)

    def test_sustained_writing_rotates_within_the_backup_inventory(self) -> None:
        with (
            patch.object(logger_module, "LOG_MAX_BYTES", 256),
            patch.object(logger_module, "LOG_BACKUP_COUNT", 3),
        ):
            logger = configure_logging(self.log_dir)
            handler = _managed_handlers(logger)[0]
            self.assertEqual(handler.maxBytes, 256)
            self.assertEqual(handler.backupCount, 3)

            highest_file_count = 0
            for index in range(200):
                logger.info("rotation-filler-%03d", index)
                highest_file_count = max(
                    highest_file_count,
                    len(list(self.log_dir.iterdir())),
                )
            _flush_handlers(logger)

        # One active file plus at most three backups, at every point during
        # the run rather than only once it finished.
        self.assertEqual(highest_file_count, 4)
        self.assertEqual(
            sorted(path.name for path in self.log_dir.iterdir()),
            ["maria.log", "maria.log.1", "maria.log.2", "maria.log.3"],
        )
        self.assertFalse((self.log_dir / "maria.log.4").exists())

    def test_rotation_keeps_utf8_the_existing_format_and_child_messages(
        self,
    ) -> None:
        message = "Unicode survives rotation: Mar\u00eda \u2603 \u6f22\u5b57"

        with patch.object(logger_module, "LOG_MAX_BYTES", 256):
            logger = configure_logging(self.log_dir)
            for index in range(200):
                logger.info("rotation-filler-%03d", index)
            get_logger("tools.regression").warning(message)
            _flush_handlers(logger)

        active = (self.log_dir / "maria.log").read_text(encoding="utf-8")
        backup = (self.log_dir / "maria.log.1").read_text(encoding="utf-8")

        # A rollover happens before the record that triggered it is written,
        # so the newest record is in the active file rather than a backup.
        matching_line = next(
            line for line in active.splitlines() if message in line
        )
        self.assertRegex(matching_line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
        self.assertTrue(
            matching_line.endswith(f" | WARNING | maria.regression | {message}")
        )
        self.assertIn("rotation-filler-", backup)

    def test_repeated_configuration_does_not_duplicate_handlers_or_message(self) -> None:
        first_logger = configure_logging(self.log_dir)
        second_logger = configure_logging(self.log_dir)

        self.assertIs(second_logger, first_logger)
        self.assertEqual(len(_file_handlers(first_logger)), 1)

        marker = "single-dispatch-marker"
        first_logger.info(marker)
        _flush_handlers(first_logger)
        log_text = (self.log_dir / "maria.log").read_text(encoding="utf-8")
        self.assertEqual(log_text.count(marker), 1)

    def test_second_configuration_targets_the_new_directory(self) -> None:
        second_dir = Path(self.temporary_directory.name) / "second" / "logs"
        first_logger = configure_logging(self.log_dir)
        first_logger.info("first-directory-marker")

        second_logger = configure_logging(second_dir)
        second_logger.info("second-directory-marker")
        _flush_handlers(second_logger)

        self.assertIs(second_logger, first_logger)
        handlers = _file_handlers(second_logger)
        self.assertEqual(len(handlers), 1)
        self.assertEqual(
            Path(handlers[0].baseFilename).resolve(),
            (second_dir / "maria.log").resolve(),
        )
        first_text = (self.log_dir / "maria.log").read_text(encoding="utf-8")
        second_text = (second_dir / "maria.log").read_text(encoding="utf-8")
        self.assertIn("first-directory-marker", first_text)
        self.assertNotIn("second-directory-marker", first_text)
        self.assertIn("second-directory-marker", second_text)

    def test_second_configuration_closes_the_handler_it_replaces(self) -> None:
        first_logger = configure_logging(self.log_dir)
        replaced = _file_handlers(first_logger)[0]

        configure_logging(self.log_dir)

        self.assertNotIn(replaced, first_logger.handlers)
        self.assertIsNone(replaced.stream)

    def test_second_configuration_applies_the_new_level(self) -> None:
        configure_logging(self.log_dir, level="info")

        logger = configure_logging(self.log_dir, level="debug")

        self.assertEqual(logger.level, logging.DEBUG)

    def test_reconfiguration_leaves_a_foreign_handler_attached(self) -> None:
        configure_logging(self.log_dir)
        foreign = logging.NullHandler()
        self.maria_logger.addHandler(foreign)

        logger = configure_logging(self.log_dir)

        self.assertIn(foreign, logger.handlers)
        self.assertEqual(len(_file_handlers(logger)), 1)

    def test_first_configuration_leaves_a_foreign_file_handler_untouched(self) -> None:
        foreign_path, foreign = self._attach_foreign_file_handler()

        logger = configure_logging(self.log_dir)

        self.assertIn(foreign, logger.handlers)
        self.assertIsNotNone(foreign.stream)
        managed = _managed_handlers(logger)
        self.assertEqual(len(managed), 1)
        self.assertNotIn(foreign, managed)
        self.assertEqual(
            Path(managed[0].baseFilename).resolve(),
            (self.log_dir / "maria.log").resolve(),
        )

        logger.info("first-configuration-marker")
        _flush_handlers(logger)
        self.assertIn(
            "first-configuration-marker",
            foreign_path.read_text(encoding="utf-8"),
        )

    def test_reconfiguration_leaves_a_foreign_file_handler_untouched(self) -> None:
        foreign_path, foreign = self._attach_foreign_file_handler()
        second_dir = Path(self.temporary_directory.name) / "second" / "logs"
        configure_logging(self.log_dir)

        logger = configure_logging(second_dir)

        self.assertIn(foreign, logger.handlers)
        self.assertIsNotNone(foreign.stream)
        self.assertNotIn(foreign, _managed_handlers(logger))

        logger.info("foreign-survivor-marker")
        _flush_handlers(logger)
        self.assertIn(
            "foreign-survivor-marker",
            foreign_path.read_text(encoding="utf-8"),
        )

        managed = _managed_handlers(logger)
        self.assertEqual(len(managed), 1)
        self.assertEqual(
            Path(managed[0].baseFilename).resolve(),
            (second_dir / "maria.log").resolve(),
        )
        self.assertNotIn(
            "foreign-survivor-marker",
            (self.log_dir / "maria.log").read_text(encoding="utf-8"),
        )

    def test_ownership_is_not_inferred_from_the_log_path(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        shared = logging.FileHandler(self.log_dir / "maria.log", encoding="utf-8")
        self.addCleanup(self._detach_and_close, shared)
        self.maria_logger.addHandler(shared)

        logger = configure_logging(self.log_dir)

        self.assertIn(shared, logger.handlers)
        self.assertIsNotNone(shared.stream)
        self.assertNotIn(shared, _managed_handlers(logger))
        self.assertEqual(len(_managed_handlers(logger)), 1)

    def test_ownership_marker_is_not_public_api(self) -> None:
        public_names = [
            name for name in vars(logger_module) if not name.startswith("_")
        ]

        self.assertEqual(
            [name for name in public_names if "managed" in name.lower()],
            [],
        )

    def test_failed_reconfiguration_keeps_managed_and_foreign_handlers_working(
        self,
    ) -> None:
        logger = configure_logging(self.log_dir)
        original_handler = _managed_handlers(logger)[0]
        foreign_path, foreign = self._attach_foreign_file_handler()
        second_dir = Path(self.temporary_directory.name) / "failed" / "logs"

        with patch(
            "core.logger.RotatingFileHandler",
            side_effect=OSError("handler construction failed"),
        ):
            with self.assertRaisesRegex(OSError, "handler construction failed"):
                configure_logging(second_dir)

        self.assertEqual(_managed_handlers(logger), [original_handler])
        self.assertIn(foreign, logger.handlers)
        self.assertIsNotNone(original_handler.stream)
        self.assertIsNotNone(foreign.stream)

        logger.info("survivor-marker")
        _flush_handlers(logger)
        log_text = (self.log_dir / "maria.log").read_text(encoding="utf-8")
        self.assertIn("survivor-marker", log_text)
        self.assertIn("survivor-marker", foreign_path.read_text(encoding="utf-8"))

    def test_directory_creation_failure_propagates(self) -> None:
        with patch.object(
            Path,
            "mkdir",
            side_effect=OSError("directory creation failed"),
        ):
            with self.assertRaisesRegex(OSError, "directory creation failed"):
                configure_logging(self.log_dir)

        self.assertEqual(self.maria_logger.handlers, [])

    def test_file_handler_construction_failure_propagates(self) -> None:
        with patch(
            "core.logger.RotatingFileHandler",
            side_effect=OSError("handler construction failed"),
        ):
            with self.assertRaisesRegex(OSError, "handler construction failed"):
                configure_logging(self.log_dir)

        self.assertEqual(self.maria_logger.handlers, [])

    def test_cleanup_flushes_removes_and_closes_handlers(self) -> None:
        logger = configure_logging(self.log_dir)
        handler = _file_handlers(logger)[0]
        logger.info("cleanup-marker")

        _remove_and_close_handlers(logger)

        self.assertEqual(logger.handlers, [])
        self.assertIsNone(handler.stream)
        log_text = (self.log_dir / "maria.log").read_text(encoding="utf-8")
        self.assertIn("cleanup-marker", log_text)


class LogAssertionHelperTests(unittest.TestCase):
    """Verify the reusable sentinel helper refuses Maria's live log tree.

    Maria's real ``LOG_DIR`` is never used as the live root here: it is
    patched to a temporary directory, so every rejection probe below names a
    path that exists only for the duration of the test.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)

        self.fake_live_root = root / "live" / "logs"
        (self.fake_live_root / "nested").mkdir(parents=True)
        for path in (
            self.fake_live_root / "maria.log",
            self.fake_live_root / "nested" / "maria.log",
        ):
            path.write_text("live-content-must-not-be-read\n", encoding="utf-8")

        self.outside_dir = root / "temporary"
        self.outside_dir.mkdir()
        self.outside_log = self.outside_dir / "maria.log"
        self.outside_log.write_text(
            "2026-01-01 00:00:00,000 | INFO | maria | ordinary-line\n",
            encoding="utf-8",
        )

        patcher = patch.object(
            log_assertions,
            "LOG_DIR",
            self.fake_live_root,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_path_inside_the_live_root_is_refused(self) -> None:
        cases = {
            "the live root itself": self.fake_live_root,
            "a file directly inside it": self.fake_live_root / "maria.log",
            "a nested descendant": self.fake_live_root / "nested" / "maria.log",
            "a traversal back into it": (
                self.fake_live_root / "nested" / ".." / "maria.log"
            ),
            "a traversal into a nested child": (
                self.fake_live_root / "nested" / ".." / "nested" / "maria.log"
            ),
            "a traversal from outside it": (
                self.outside_dir / ".." / "live" / "logs" / "maria.log"
            ),
        }

        for label, target in cases.items():
            with self.subTest(target=label):
                with self.assertRaises(ValueError) as caught:
                    assert_log_omits(self, target, ("absent-value",))

                self.assertIn("live log directory", str(caught.exception))

    def test_a_live_root_path_is_refused_before_anything_is_read(self) -> None:
        with patch.object(Path, "read_text", autospec=True) as read_text:
            with self.assertRaises(ValueError):
                assert_log_omits(
                    self,
                    self.fake_live_root / "nested" / "maria.log",
                    ("absent-value",),
                )

        read_text.assert_not_called()

    def test_a_temporary_path_outside_the_live_root_is_read(self) -> None:
        assert_log_omits(self, self.outside_log, ("absent-value",))
        assert_log_omits(self, self.outside_log, ("absent-value", "also-absent"))

    def test_a_blank_sentinel_is_refused(self) -> None:
        for sentinel in ("", "   ", "\t\n"):
            with self.subTest(sentinel=repr(sentinel)):
                with self.assertRaises(ValueError) as caught:
                    assert_log_omits(self, self.outside_log, (sentinel,))

                self.assertIn("non-blank", str(caught.exception))

    def test_supplying_no_sentinel_at_all_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            assert_log_omits(self, self.outside_log, ())

    def test_a_leak_names_the_sentinel_and_nothing_around_it(self) -> None:
        leaked = "leaked-sentinel-6b2f0a9d41"
        unrelated = "unrelated-log-content-cc71e4b8"
        log_file = self.outside_dir / "leaky.log"
        log_file.write_text(
            f"... | INFO | maria | {unrelated}\n... | INFO | maria | {leaked}\n",
            encoding="utf-8",
        )

        with self.assertRaises(AssertionError) as caught:
            assert_log_omits(self, log_file, (leaked, "absent-value"))

        report = str(caught.exception)
        self.assertIn(leaked, report)
        self.assertIn("leaky.log", report)
        self.assertNotIn(unrelated, report)
        self.assertNotIn("absent-value", report.split("Private values reached")[-1])
        self.assertNotIn(str(log_file), report)
        self.assertNotIn(str(self.outside_dir), report)


def _file_handlers(logger: logging.Logger) -> list[logging.FileHandler]:
    """Return the file handlers currently attached to a logger."""
    return [
        handler
        for handler in logger.handlers
        if isinstance(handler, logging.FileHandler)
    ]


def _managed_handlers(logger: logging.Logger) -> list[logging.FileHandler]:
    """Return only the file handlers configure_logging created and owns."""
    return [
        handler
        for handler in logger.handlers
        if getattr(handler, logger_module._MANAGED_ATTRIBUTE, False)
    ]


def _flush_handlers(logger: logging.Logger) -> None:
    """Flush all handlers attached to a logger."""
    for handler in logger.handlers:
        handler.flush()


def _remove_and_close_handlers(logger: logging.Logger) -> None:
    """Flush, detach, and close every handler on a logger."""
    for handler in list(logger.handlers):
        try:
            handler.flush()
        finally:
            logger.removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    unittest.main()
