"""Tests that Project Maria is read-only self-review that never starts itself.

V1-24-R1 lets the owner activate Project Maria, after which Maria may read,
read-only, the files on her approved list. These tests build a synthetic
repository in a temporary folder, with Maria's database, logs, and system
prompt configured inside it as in the shipped layout, and prove with the
real Windows reader that listed files are read exactly, while the database
and its folder, every log and rotation, the private override, the prompt,
backups, credential files, and unlisted files stay unreachable: by direct
path, case variant, junction, hard link, short-name alias (skipped when the
volume keeps none), symbolic link (skipped without the privilege to make
one), listing, and ``@`` reference. A database, log, or prompt configured
inside the workspace stays unreachable there too. Those tests skip
explicitly on any other system.

The rest run anywhere: Project Maria is never activated by a fresh
database, by its own creation, by either interface starting, by the
closed-app pass, or by a model plan; and while it is active, rename and move
of its paths are refused and write and delete stay denied.

Every database, log, prompt, and folder here is temporary and synthetic. No
test reads Maria's live database, live log, or private override, contacts a
model, or registers or runs an operating-system task.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import core.project_files as project_files
import main as main_module
from core.brain import MariaBrain
from core.config import MariaConfig
from core.constants import APP_VERSION
from core.desktop_notice import NotificationState
from core.filesystem_roots import (
    FilesystemProtections,
    FilesystemRoot,
    FilesystemRoots,
    RootPolicy,
)
from core.memory import Memory
from core.permissions import PermissionDecision, PermissionManager
from core.planner import Planner
from core.project_file_access import load_access
from core.project_files import (
    FileContent,
    FileRefusal,
    FolderListing,
    ProjectFileReader,
)
from core.project_maria_manifest import APPROVED_PATHS, NOT_APPROVED_PATHS
from core.project_records import (
    REFERENCE_FORM_REASON,
    REFERENCE_UNREADABLE_REASON,
    ActivationState,
    Project,
)
from core.reasoning import DISABLED_REASONING
from core.session import MariaSession, ProjectChoicesSeam
from core.tool_manager import ToolManager
from deliver_reminders import EXIT_DONE, run_pass
from tests.gui_fakes import RunningGui
from tests.test_brain_recording_policy import _quiet_logger
from tests.test_conversation import ScriptedProvider, enabled_settings
from tests.test_project_context import REPLY
from tests.test_project_file_context import _refused
from tools.filesystem import PATH_READ_ONLY_ROOT_MESSAGE


REAL_WINDOWS = sys.platform == "win32"
STAMP = "2026-10-10T08:00:00Z"
NOW = datetime(2026, 10, 10, 8, 0, 0, tzinfo=timezone.utc)
MARIA_PROJECT = Project(1, "Self review", "project_maria", "", STAMP, STAMP)
UNSUPPORTED = (
    "I do not have an executable plan for that yet. "
    "Type 'help' to see supported commands."
)

# Listed files of the synthetic repository, each a member of the approved
# list, with synthetic content.
MEMBERS = {
    "README.md": b"# Signpost\n",
    "Maria/README.md": b"# Maria\nA synthetic readme.\n",
    "Maria/config/config.toml": b'[app]\nname = "Maria"\n',
    "Maria/core/brain.py": b'"""A synthetic module."""\n',
    "Maria/docs/api.md": b"# API\n",
    "Maria/docs/tools.md": b"# Tools\n",
}

# Private and unlisted files beside them. The database itself is the
# world's own, created by the memory layer.
PRIVATE = {
    "Maria/database/notes.md": b"private database folder text\n",
    "Maria/logs/maria.log": b"private log line\n",
    "Maria/logs/maria.log.1": b"private rotated log line\n",
    "Maria/logs/delivery.log": b"private delivery log line\n",
    "Maria/logs/notes.md": b"private log folder text\n",
    "Maria/config/config.local.toml": b'[reasoning]\nmodel = "private"\n',
    "Maria/prompts/system_prompt.txt": b"private prompt\n",
    "Maria/maria.db.bak": b"private backup\n",
    "Maria/.env": b"TOKEN=private\n",
    ".env": b"TOKEN=private\n",
    "Maria/tests/untracked_helper.py": b"# unlisted\n",
}

# Every spelling tried for them, case variants included. Each is refused
# before any Windows call: none is on the approved list.
PRIVATE_SPELLINGS = (
    "Maria/database/maria.db",
    "maria/DATABASE/MARIA.DB",
    "Maria/database/maria.db-wal",
    "Maria/database/maria.db-shm",
    "Maria/database/maria.db-journal",
    "Maria/database/notes.md",
    "Maria/logs/maria.log",
    "MARIA/LOGS/maria.log.1",
    "Maria/logs/delivery.log",
    "Maria/logs/notes.md",
    "Maria/config/config.local.toml",
    "Maria/Config/CONFIG.LOCAL.toml",
    "Maria/prompts/system_prompt.txt",
    "Maria/maria.db.bak",
    "Maria/tests/untracked_helper.py",
)


class _Recorder:
    """Pass every call through to the real access, recording each by name."""

    def __init__(self, access: object) -> None:
        self._access = access
        self.calls: list[str] = []
        self.opened: list[str] = []
        self.reads = 0

    def __getattr__(self, name: str) -> object:
        target = getattr(self._access, name)
        if not callable(target):
            return target

        def call(*arguments: object) -> object:
            self.calls.append(name)
            if name == "create_file":
                self.opened.append(str(arguments[0]))
            if name == "read":
                self.reads += 1
            return target(*arguments)

        return call


class _World:
    """A synthetic repository and workspace, with Maria's runtime inside one.

    ``runtime_in`` says which holds the database, logs, and system prompt:
    the repository, as in the shipped layout, or the workspace. Brain reads
    project files through its own reader, built from these roots and
    protections with the real Windows functions.
    """

    def __init__(
        self,
        test: unittest.TestCase,
        provider: object | None = None,
        *,
        runtime_in: str = "repository",
    ) -> None:
        temporary = TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.repository = self.base / "repository"
        self.workspace = self.base / "workspace"
        for path, data in {**MEMBERS, **PRIVATE}.items():
            target = self.repository / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self.workspace.mkdir()
        if runtime_in == "repository":
            runtime = self.repository / "Maria"
            self.database_path = runtime / "database" / "maria.db"
            self.log_dir = runtime / "logs"
            self.prompt_path = runtime / "prompts" / "system_prompt.txt"
        else:
            runtime = self.workspace / "runtime"
            self.database_path = runtime / "maria.db"
            self.log_dir = runtime / "logs"
            self.prompt_path = runtime / "prompts" / "system.txt"
            for path in (
                self.log_dir / "maria.log",
                self.log_dir / "notes.md",
                self.prompt_path,
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"private runtime text\n")
        self.config = MariaConfig(
            app_name="Maria",
            version=APP_VERSION,
            environment="test",
            database_path=self.database_path,
            log_dir=self.log_dir,
            prompt_path=self.prompt_path,
            filesystem_roots=FilesystemRoots(
                (
                    FilesystemRoot(
                        name="workspace",
                        path=self.workspace,
                        policy=RootPolicy.ORGANIZE,
                        enabled=True,
                    ),
                    FilesystemRoot(
                        name="project_maria",
                        path=self.repository,
                        policy=RootPolicy.READ_ONLY,
                        enabled=True,
                    ),
                ),
                "workspace",
            ),
            filesystem_protections=FilesystemProtections(
                database_path=self.database_path,
                log_dir=self.log_dir,
                prompt_path=self.prompt_path,
            ),
            log_level="DEBUG",
            confirmation_required=frozenset(),
            denied_actions=frozenset(),
            reasoning=(
                DISABLED_REASONING
                if provider is None
                else enabled_settings(max_input_bytes=60_000)
            ),
        )
        self.logger = _quiet_logger("tests.project_maria")
        self.memory = Memory(self.database_path, logger=self.logger)
        self.memory.initialize()
        tool_manager = ToolManager(
            config=self.config,
            memory=self.memory,
            logger=self.logger,
        )
        tool_manager.load_tools()
        self.brain = MariaBrain(
            config=self.config,
            planner=Planner(logger=self.logger),
            memory=self.memory,
            tool_manager=tool_manager,
            permissions=PermissionManager(
                confirmation_required=frozenset(),
                denied_actions=frozenset(),
                logger=self.logger,
            ),
            logger=self.logger,
            reasoning=provider,
        )

    def session(self) -> MariaSession:
        """Return a fresh session over this world's Brain."""
        return MariaSession(self.brain, logger=self.logger)

    def reader(self, access: object | None = None) -> ProjectFileReader:
        """Return a reader over this world's roots and protections."""
        chosen = load_access() if access is None else access
        return ProjectFileReader(
            self.config.filesystem_roots,
            self.config.filesystem_protections,
            self.logger,
            access_loader=lambda: chosen,
        )

    def activation(self) -> ActivationState:
        """Return the activation state this world's database holds now."""
        return self.memory.project_activation().state


def _short_name(path: Path) -> str | None:
    """Return a file's 8.3 alias, or None when the volume keeps none."""
    buffer = ctypes.create_unicode_buffer(32_768)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetShortPathNameW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    kernel32.GetShortPathNameW.restype = ctypes.c_uint32
    if not kernel32.GetShortPathNameW(str(path), buffer, 32_768):
        return None
    alias = Path(buffer.value).name
    return None if alias.casefold() == path.name.casefold() else alias


class _RealReaderCase(unittest.TestCase):
    """A fresh process-wide latch and the real Windows functions."""

    def setUp(self) -> None:
        if load_access() is None:
            self.skipTest("This Windows lacks a function project reading needs.")
        latch = patch.object(project_files, "_LATCH", project_files._RestoreLatch())
        latch.start()
        self.addCleanup(latch.stop)


@unittest.skipUnless(REAL_WINDOWS, "Real project file reading runs only on Windows.")
class ProjectMariaReadTests(_RealReaderCase):
    """Listed files are read exactly; nothing else is reachable at all."""

    def setUp(self) -> None:
        super().setUp()
        self.world = _World(self)

    def test_the_synthetic_files_are_classified_as_the_list_says(self) -> None:
        for path in MEMBERS:
            with self.subTest(member=path):
                self.assertIn(path, APPROVED_PATHS)
        for path in PRIVATE:
            with self.subTest(private=path):
                self.assertNotIn(path, APPROVED_PATHS)
        self.assertIn("Maria/prompts/system_prompt.txt", NOT_APPROVED_PATHS)

    def test_listed_files_are_read_exactly_in_their_listed_spelling(self) -> None:
        for typed, member in (
            ("Maria/README.md", "Maria/README.md"),
            ("maria/readme.MD", "Maria/README.md"),
            ("README.md", "README.md"),
            ("Maria/core/brain.py", "Maria/core/brain.py"),
        ):
            with self.subTest(typed=typed):
                content = self.world.reader().read_file(MARIA_PROJECT, typed)

                self.assertIs(type(content), FileContent)
                self.assertEqual(content.path, member)
                self.assertEqual(content.data, MEMBERS[member])

    def test_private_and_unlisted_files_are_refused_before_any_call(self) -> None:
        for path in PRIVATE_SPELLINGS:
            with self.subTest(path=path):
                recorder = _Recorder(load_access())

                refused = self.world.reader(recorder).read_file(MARIA_PROJECT, path)

                self.assertIs(refused, FileRefusal.UNREADABLE)
                self.assertEqual(recorder.calls, [])
        # Each exists, so no refusal above is only an absent file.
        self.assertTrue(self.world.database_path.is_file())
        for path in PRIVATE:
            self.assertTrue((self.world.repository / path).is_file())

    def test_listings_show_only_listed_files_and_the_folders_holding_them(
        self,
    ) -> None:
        reader = self.world.reader()

        for folder, names in (
            ("", ["Maria", "README.md"]),
            ("Maria", ["config", "core", "docs", "README.md", "tests"]),
            ("Maria/config", ["config.toml"]),
            ("Maria/tests", []),
        ):
            with self.subTest(folder=folder):
                listing = reader.list_folder(MARIA_PROJECT, folder)
                self.assertIs(type(listing), FolderListing)
                self.assertEqual([entry.name for entry in listing.entries], names)
        for folder in (
            "Maria/database",
            "maria/DATABASE",
            "Maria/logs",
            "Maria/prompts",
        ):
            with self.subTest(folder=folder):
                self.assertIs(
                    reader.list_folder(MARIA_PROJECT, folder),
                    FileRefusal.UNREADABLE,
                )

    def test_a_junction_at_a_listed_folder_is_refused_before_it_is_opened(
        self,
    ) -> None:
        import _winapi

        docs = self.world.repository / "Maria" / "docs"
        for child in docs.iterdir():
            child.unlink()
        docs.rmdir()
        (self.world.repository / "Maria" / "database" / "api.md").write_bytes(
            b"private\n"
        )
        _winapi.CreateJunction(
            str(self.world.repository / "Maria" / "database"),
            str(docs),
        )
        self.addCleanup(os.rmdir, docs)
        recorder = _Recorder(load_access())

        refused = self.world.reader(recorder).read_file(
            MARIA_PROJECT,
            "Maria/docs/api.md",
        )
        listing = self.world.reader().list_folder(MARIA_PROJECT, "Maria")

        self.assertIs(refused, FileRefusal.UNREADABLE)
        self.assertEqual(
            [path for path in recorder.opened if "docs" in path.casefold()],
            [],
        )
        self.assertNotIn("docs", [entry.name for entry in listing.entries])

    def test_a_hard_link_at_a_listed_file_is_refused_before_any_data_is_read(
        self,
    ) -> None:
        tools = self.world.repository / "Maria" / "docs" / "tools.md"
        tools.unlink()
        os.link(self.world.repository / "Maria" / "database" / "notes.md", tools)
        recorder = _Recorder(load_access())

        refused = self.world.reader(recorder).read_file(
            MARIA_PROJECT,
            "Maria/docs/tools.md",
        )

        self.assertIs(refused, FileRefusal.UNREADABLE)
        self.assertEqual(recorder.reads, 0)

    def test_a_symbolic_link_at_a_listed_file_is_refused(self) -> None:
        tools = self.world.repository / "Maria" / "docs" / "tools.md"
        tools.unlink()
        try:
            os.symlink(self.world.repository / "Maria" / "database" / "notes.md", tools)
        except OSError:
            self.skipTest("This account cannot create symbolic links.")
        recorder = _Recorder(load_access())

        refused = self.world.reader(recorder).read_file(
            MARIA_PROJECT,
            "Maria/docs/tools.md",
        )

        self.assertIs(refused, FileRefusal.UNREADABLE)
        self.assertEqual(recorder.reads, 0)

    def test_a_short_name_alias_is_never_a_path(self) -> None:
        typed = []
        for path in (self.world.prompt_path, self.world.log_dir / "delivery.log"):
            alias = _short_name(path)
            if alias is not None:
                folder = path.parent.relative_to(self.world.repository).as_posix()
                typed.append(f"{folder}/{alias}")
        if not typed:
            self.skipTest("This volume keeps no short names.")

        for path in typed:
            with self.subTest(path=path):
                recorder = _Recorder(load_access())
                refused = self.world.reader(recorder).read_file(MARIA_PROJECT, path)
                self.assertIs(refused, FileRefusal.UNREADABLE)
                self.assertEqual(recorder.calls, [])

    def test_a_reference_reaches_listed_files_only(self) -> None:
        provider = ScriptedProvider(lambda request: REPLY)
        world = _World(self, provider)
        project_id = world.memory.create_project("Self review", "project_maria", "")
        world.memory.activate_project(project_id)
        session = world.session()

        for request, reason in (
            ("see @Maria/database/maria.db", REFERENCE_UNREADABLE_REASON),
            ("see @maria/Database/Maria.DB", REFERENCE_UNREADABLE_REASON),
            ("see @Maria/logs/maria.log.1", REFERENCE_UNREADABLE_REASON),
            ("see @Maria/config/config.local.toml", REFERENCE_UNREADABLE_REASON),
            ("see @Maria/prompts/system_prompt.txt", REFERENCE_UNREADABLE_REASON),
            ("see @Maria/maria.db.bak", REFERENCE_UNREADABLE_REASON),
            ("see @Maria/tests/untracked_helper.py", REFERENCE_UNREADABLE_REASON),
            ("see @Maria/.env", REFERENCE_FORM_REASON),
            ("see @.env", REFERENCE_FORM_REASON),
        ):
            with self.subTest(request=request):
                self.assertEqual(session.submit(request).text, _refused(reason))
        self.assertEqual(provider.calls, 0)

        session.submit("review @maria/readme.md")

        (request,) = provider.requests
        self.assertEqual(
            [(part.path, part.text) for part in request.files],
            [("Maria/README.md", MEMBERS["Maria/README.md"].decode("ascii"))],
        )


@unittest.skipUnless(REAL_WINDOWS, "Real project file reading runs only on Windows.")
class ConfiguredElsewhereTests(_RealReaderCase):
    """A database, log, or prompt configured in the workspace stays out of reach."""

    def setUp(self) -> None:
        super().setUp()
        self.world = _World(self, runtime_in="workspace")
        self.project = Project(1, "Everything", "workspace", "", STAMP, STAMP)

    def test_each_is_refused_by_every_spelling(self) -> None:
        reader = self.world.reader()

        for path in (
            "runtime/prompts/system.txt",
            "RUNTIME/Prompts/SYSTEM.TXT",
            "runtime/logs/notes.md",
            "runtime/logs/maria.log",
            "runtime/maria.db",
        ):
            with self.subTest(path=path):
                self.assertIs(
                    reader.read_file(self.project, path),
                    FileRefusal.UNREADABLE,
                )
        self.assertTrue(self.world.prompt_path.is_file())
        self.assertTrue((self.world.log_dir / "notes.md").is_file())

    def test_their_folders_list_without_them(self) -> None:
        reader = self.world.reader()

        for folder, names in (("runtime", ["prompts"]), ("runtime/prompts", [])):
            with self.subTest(folder=folder):
                listing = reader.list_folder(self.project, folder)
                self.assertIs(type(listing), FolderListing)
                self.assertEqual([entry.name for entry in listing.entries], names)
        self.assertIs(
            reader.list_folder(self.project, "runtime/logs"),
            FileRefusal.UNREADABLE,
        )

    def test_a_reference_to_one_is_refused(self) -> None:
        provider = ScriptedProvider(lambda request: REPLY)
        world = _World(self, provider, runtime_in="workspace")
        project_id = world.memory.create_project("Everything", "workspace", "")
        world.memory.activate_project(project_id)
        session = world.session()

        for request in (
            "see @runtime/prompts/system.txt",
            "see @runtime/logs/notes.md",
        ):
            with self.subTest(request=request):
                self.assertEqual(
                    session.submit(request).text,
                    _refused(REFERENCE_UNREADABLE_REASON),
                )
        self.assertEqual(provider.calls, 0)


class NeverSelfActivatesTests(unittest.TestCase):
    """Only the owner's confirmed command ever activates a project."""

    def test_a_fresh_database_and_creating_project_maria_activate_nothing(
        self,
    ) -> None:
        world = _World(self)
        self.assertIs(world.activation(), ActivationState.NEVER_ACTIVATED)

        world.session().submit("create project Self review: project_maria")

        listing = world.memory.list_projects()
        self.assertEqual(
            [
                (project.project_id, project.root, project.folder)
                for project in listing.projects
            ],
            [(1, "project_maria", "")],
        )
        self.assertIs(listing.activation.state, ActivationState.NEVER_ACTIVATED)

    def test_neither_interface_starting_activates_project_maria(self) -> None:
        world = _World(self)
        world.memory.create_project("Self review", "project_maria", "")

        stdout = StringIO()
        with (
            patch("builtins.input", side_effect=["exit"]),
            redirect_stdout(stdout),
            redirect_stderr(StringIO()),
        ):
            status = main_module.run_cli(lambda: world.brain)
        self.assertEqual(status, 0)
        self.assertIs(world.activation(), ActivationState.NEVER_ACTIVATED)

        gui = RunningGui(
            self,
            lambda: MariaSession(
                world.brain,
                projects=ProjectChoicesSeam(world.memory),
            ),
        )
        gui.send("help")
        gui.close()

        self.assertEqual(gui.controller.exit_status, 0)
        self.assertEqual(gui.view.project_items[0], ("1: Self review",))
        self.assertIs(world.activation(), ActivationState.NEVER_ACTIVATED)

    def test_the_closed_app_pass_activates_nothing(self) -> None:
        world = _World(self)
        world.memory.create_project("Self review", "project_maria", "")
        config_path = world.base / "pass-config.toml"
        config_path.write_text(
            "[paths]\n"
            f'database = "{world.database_path.as_posix()}"\n'
            f'logs = "{(world.base / "pass-logs").as_posix()}"\n',
            encoding="utf-8",
        )
        notices = Mock()
        notices.available.return_value = True
        notices.notification_state.return_value = NotificationState.ACCEPTS

        status = run_pass(
            clock=lambda: NOW,
            config_path=config_path,
            notices=notices,
            memory_factory=lambda path: Memory(
                path,
                logger=Mock(spec=logging.Logger),
                existing_only=True,
            ),
            configure_log=Mock(),
        )

        self.assertEqual(status, EXIT_DONE)
        notices.show.assert_not_called()
        self.assertIs(world.activation(), ActivationState.NEVER_ACTIVATED)

    def test_no_model_plan_can_activate_a_project(self) -> None:
        activate = {"tool": "projects", "action": "activate", "arguments": {"id": 1}}
        for label, proposal in (
            ("one step", activate),
            (
                "inside several steps",
                {
                    "steps": [
                        {
                            "tool": "calculator",
                            "action": "calculate",
                            "arguments": {"expression": "1 + 1"},
                        },
                        activate,
                    ]
                },
            ),
        ):
            with self.subTest(proposal=label):
                text = json.dumps(proposal)
                provider = ScriptedProvider(lambda request, text=text: text)
                world = _World(self, provider)
                world.memory.create_project("Self review", "project_maria", "")
                session = world.session()

                response = session.submit("Please start reviewing yourself now.")

                self.assertEqual(provider.calls, 1)
                self.assertFalse(response.needs_confirmation)
                self.assertIsNone(session.pending_approval)
                self.assertIs(world.activation(), ActivationState.NEVER_ACTIVATED)


class ProjectMariaMutationTests(unittest.TestCase):
    """While Project Maria is active, nothing in its root can change."""

    def setUp(self) -> None:
        self.world = _World(self)
        project_id = self.world.memory.create_project(
            "Self review",
            "project_maria",
            "",
        )
        self.world.memory.activate_project(project_id)

    def test_rename_and_move_of_its_paths_are_refused_after_approval(self) -> None:
        source = self.world.repository / "Maria" / "README.md"
        inbound = self.world.workspace / "note.txt"
        inbound.write_bytes(b"workspace note\n")
        into_docs = self.world.repository / "Maria" / "docs" / "README.md"
        into_repository = self.world.repository / "Maria" / "note.txt"
        cases = (
            ("Rename", f"rename {source} to {source.with_name('RENAMED.md')}"),
            ("Move", f"move {source} to {into_docs}"),
            ("Move", f"move {source} to {self.world.workspace / 'README.md'}"),
            ("Move", f"move {inbound} to {into_repository}"),
        )
        with self.assertLogs("maria.filesystem", level="WARNING") as logged:
            for action, command in cases:
                with self.subTest(command=command.split(" ", 1)[0]):
                    session = self.world.session()

                    offered = session.submit(command)

                    self.assertTrue(offered.needs_confirmation)
                    self.assertEqual(
                        session.confirm().text,
                        f"{action} failed: {PATH_READ_ONLY_ROOT_MESSAGE}",
                    )
        # The tool logs its fixed refusal only, never a path it was given.
        self.assertEqual(
            [record.getMessage() for record in logged.records],
            [
                f"{action} failed: {PATH_READ_ONLY_ROOT_MESSAGE}"
                for action, _ in cases
            ],
        )
        self.assertEqual(source.read_bytes(), MEMBERS["Maria/README.md"])
        self.assertEqual(inbound.read_bytes(), b"workspace note\n")
        for leftover in (
            source.with_name("RENAMED.md"),
            self.world.repository / "Maria" / "docs" / "README.md",
            self.world.workspace / "README.md",
            self.world.repository / "Maria" / "note.txt",
        ):
            self.assertFalse(leftover.exists())
        self.assertIs(self.world.activation(), ActivationState.ACTIVE)

    def test_write_and_delete_stay_denied_and_have_no_command(self) -> None:
        permissions = PermissionManager(
            confirmation_required=frozenset({"filesystem.write"}),
            denied_actions=frozenset(),
            logger=self.world.logger,
        )
        for action in ("write", "delete"):
            with self.subTest(action=action):
                self.assertIs(
                    permissions.check("filesystem", action).decision,
                    PermissionDecision.DENIED,
                )
        response = self.world.session().submit("delete file Maria/README.md")
        self.assertEqual(response.text, UNSUPPORTED)
        self.assertTrue((self.world.repository / "Maria" / "README.md").is_file())


if __name__ == "__main__":
    unittest.main()
