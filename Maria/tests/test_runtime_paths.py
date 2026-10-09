"""Focused tests for the tracked ``[paths]`` rule, ``core/runtime_paths.py``.

The closed-app reminder pass reads the tracked configuration's ``[paths]``
through this leaf (V1-23-R1), and ``core.config`` applies the same rule for
both interfaces, so the pass must find exactly the database and the log
directory the interfaces use. Every configuration here is a temporary copy of
the tracked file with only its paths changed; the private override is never
named or read.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core.config import load_config
from core.constants import CONFIG_PATH, PROJECT_ROOT
from core.runtime_paths import (
    DEFAULT_DATABASE_SETTING,
    DEFAULT_LOGS_SETTING,
    PATHS_UNREADABLE_MESSAGE,
    RuntimePaths,
    read_runtime_paths,
)

TRACKED = CONFIG_PATH.read_text(encoding="utf-8")


def _with_paths(database: str | None, logs: str | None) -> str:
    """Return the tracked text with its two paths replaced, or left out."""
    lines = []
    for line in TRACKED.splitlines():
        if line.startswith("database = "):
            if database is None:
                continue
            line = f'database = "{database}"'
        elif line.startswith("logs = "):
            if logs is None:
                continue
            line = f'logs = "{logs}"'
        lines.append(line)
    return "\n".join(lines) + "\n"


class RuntimePathsTests(unittest.TestCase):
    """The pass's paths follow the interfaces' own rule exactly."""

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        home = self.root / "home"
        home.mkdir()
        # Home expansion lands here, so the owner's real ~/MariaWorkspace is
        # never named, created, or read while the tracked roots load.
        environment = patch.dict(
            os.environ,
            {"HOME": str(home), "USERPROFILE": str(home)},
        )
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, text: str) -> Path:
        """Write one temporary tracked configuration and return its path."""
        path = self.root / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def assert_same_as_the_interfaces(self, config_path: Path) -> RuntimePaths:
        """Return the pass's paths, having checked them against core.config's."""
        paths = read_runtime_paths(config_path)
        config = load_config(config_path)
        self.assertEqual(
            paths,
            RuntimePaths(config.database_path, config.log_dir),
        )
        return paths

    def test_the_tracked_text_names_each_path_once(self) -> None:
        lines = TRACKED.splitlines()
        self.assertEqual(
            [line for line in lines if line.startswith(("database = ", "logs = "))],
            ['database = "database/maria.db"', 'logs = "logs"'],
        )

    def test_missing_keys_take_the_defaults_under_the_application_directory(
        self,
    ) -> None:
        self.assertEqual(
            (DEFAULT_DATABASE_SETTING, DEFAULT_LOGS_SETTING),
            ("database/maria.db", "logs"),
        )

        paths = self.assert_same_as_the_interfaces(
            self.write(_with_paths(None, None))
        )

        self.assertEqual(
            paths,
            RuntimePaths(
                (PROJECT_ROOT / "database" / "maria.db").resolve(),
                (PROJECT_ROOT / "logs").resolve(),
            ),
        )

    def test_relative_paths_resolve_under_the_application_directory(self) -> None:
        config_path = self.write(_with_paths("data/other.db", "var/log"))
        # Not the working directory: the pass starts wherever Task
        # Scheduler starts it.
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)

        paths = self.assert_same_as_the_interfaces(config_path)

        self.assertEqual(
            paths,
            RuntimePaths(
                (PROJECT_ROOT / "data" / "other.db").resolve(),
                (PROJECT_ROOT / "var" / "log").resolve(),
            ),
        )
        self.assertFalse(str(paths.database_path).startswith(str(self.root)))

    def test_absolute_paths_are_kept(self) -> None:
        database = self.root / "elsewhere" / "maria.db"
        logs = self.root / "elsewhere" / "logs"

        paths = self.assert_same_as_the_interfaces(
            self.write(_with_paths(database.as_posix(), logs.as_posix()))
        )

        self.assertEqual(paths, RuntimePaths(database, logs))

    def test_an_unreadable_file_or_table_is_refused_with_a_fixed_message(
        self,
    ) -> None:
        missing = self.root / "missing.toml"
        not_toml = self.write("[paths\n")
        not_utf8 = self.root / "latin.toml"
        not_utf8.write_bytes(b'[paths]\ndatabase = "caf\xe9.db"\n')
        not_a_table = self.root / "scalar.toml"
        not_a_table.write_text('paths = "database/maria.db"\n', encoding="utf-8")

        for label, config_path in (
            ("missing", missing),
            ("not TOML", not_toml),
            ("not UTF-8", not_utf8),
            ("not a table", not_a_table),
        ):
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as caught:
                    read_runtime_paths(config_path)
                self.assertEqual(str(caught.exception), PATHS_UNREADABLE_MESSAGE)
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
