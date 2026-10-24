"""Tests for the projects tool, its commands, and its permissions (V1-24-R1).

The tool runs over a temporary database and, for file commands, a reader
whose Windows calls go to ``tests.project_fakes.FakeAccess``. Each action's
reply and fixed recorded line are pinned, each confirmation preview shows
exactly what the approval covers, and the planner's thirteen commands, their
usage replies, and the order in which longer commands are matched first are
pinned with them. Six actions require confirmation and seven are allowed.
"""

from __future__ import annotations

import logging
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import core.project_files as project_files
from core.memory import Memory
from core.permissions import (
    CONFIRMATION_NOTICES,
    MANDATORY_CONFIRMATION_ACTIONS,
    PermissionDecision,
    PermissionManager,
)
from core.planner import Planner
from core.project_files import ProjectFileReader
from core.project_records import (
    ACTIVATION_SERIAL_MAX,
    PROJECT_MARIA_PREVIEW,
    fact_digest,
)
from core.tool_manager import ConfirmationBinding
from tests.project_fakes import (
    REPOSITORY_PATH,
    WORKSPACE_PATH,
    FakeAccess,
    FakeVolume,
    StubProtections,
    StubRoots,
)
from tools.projects import ProjectsTool


NO_ACTIVE = (
    "No project is active. Type projects to see them, then activate project <id>."
)
UNREADABLE_SETTING = (
    "The active project setting could not be read, so no project is active."
)
BUSY = "Database is busy. Try again shortly."
STORAGE = "Unable to read or write projects."
NOT_A_FILE = "That is not a readable file of the active project."
NOT_A_FOLDER = "That is not a readable folder of the active project."
CREATE_USAGE = (
    "Use: create project <name>: workspace/<folder> or create project <name>: "
    "project_maria"
)
READ_FILE_USAGE = (
    "Use: read project file <path> or read project file <path> lines "
    "<from>-<to>"
)


class _ToolCase(unittest.TestCase):
    """A projects tool over a temporary database and a fake volume."""

    def setUp(self) -> None:
        latch = patch.object(project_files, "_LATCH", project_files._RestoreLatch())
        latch.start()
        self.addCleanup(latch.stop)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database_path = Path(temporary.name) / "database" / "maria.db"
        self.memory = Memory(self.database_path, logger=Mock(spec=logging.Logger))
        self.memory.initialize()
        self.volume = FakeVolume(WORKSPACE_PATH, REPOSITORY_PATH)
        self.volume.add_file("garden/plan.md", b"one\ntwo\nthree\n")
        self.volume.add_file("garden/beds/north.txt", b"tomatoes\n")
        self.volume.add_file("garden/photo.jpg", b"\xff")
        self.access = FakeAccess(self.volume)
        self.tool = self.tool_with(self.access)

    def tool_with(self, access: FakeAccess | None) -> ProjectsTool:
        """Return a tool whose reader's Windows functions are ``access``."""
        return ProjectsTool(
            self.memory,
            ProjectFileReader(
                StubRoots(),
                StubProtections(),
                Mock(spec=logging.Logger),
                access_loader=lambda: access,
            ),
            Mock(spec=logging.Logger),
        )

    def raw(self, statement: str, parameters: tuple[object, ...] = ()) -> None:
        """Write one row from outside Maria, on the temporary database."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(statement, parameters)
            connection.commit()

    def run_action(
        self,
        action: str,
        **arguments: object,
    ) -> tuple[bool, str, str | None]:
        """Run one action; return its success, message, and recorded line."""
        result = self.tool.execute(action, arguments)
        return result.success, result.message, result.recorded_message

    def preview(
        self,
        action: str,
        **arguments: object,
    ) -> ConfirmationBinding | dict:
        """Return what one action's approval would show and pin."""
        return self.tool.confirmation_binding(action, arguments)

    def garden(self) -> int:
        """Create and return the Garden project, as the owner would."""
        success, _message, _recorded = self.run_action(
            "create",
            name="Garden",
            location="workspace/GARDEN",
        )
        self.assertTrue(success)
        return 1


class ProjectActionTests(_ToolCase):
    """Listing, creating, reading, renaming, and deleting projects."""

    def test_the_listing_marks_the_active_project_and_records_a_fixed_line(
        self,
    ) -> None:
        self.assertEqual(
            self.run_action("list"),
            (
                True,
                "There are no projects. Use: create project <name>: "
                "workspace/<folder> or create project <name>: project_maria",
                "Listed projects.",
            ),
        )
        self.garden()
        self.run_action("create", name="Self review", location="project_maria")
        self.memory.activate_project(2)

        self.assertEqual(
            self.run_action("list"),
            (
                True,
                'Projects:\nProject 1: "Garden" in workspace/garden\n'
                'Project 2: "Self review" in project_maria (active)',
                "Listed projects.",
            ),
        )

    def test_the_listing_says_when_more_exist_or_the_setting_is_unreadable(
        self,
    ) -> None:
        for index in range(51):
            self.memory.create_project(f"P{index}", "workspace", f"p{index}")
        lines = self.run_action("list")[1].split("\n")
        self.assertEqual(len(lines), 52)
        self.assertEqual(lines[-1], "More projects are not shown.")

        self.raw(
            "INSERT INTO project_activation VALUES (1, 999, 4, ?)",
            ("2026-10-09T21:24:13Z",),
        )
        lines = self.run_action("list")[1].split("\n")
        self.assertEqual(
            lines[-2:],
            ["More projects are not shown.", UNREADABLE_SETTING],
        )
        self.assertNotIn(" (active)", "\n".join(lines))

    def test_creation_checks_the_folder_and_stores_its_disk_spelling(self) -> None:
        success, message, recorded = self.run_action(
            "create",
            name="Garden",
            location="workspace/GARDEN",
        )

        self.assertEqual(
            (success, recorded),
            (True, "Created project 1."),
        )
        self.assertEqual(
            message,
            'Created project 1: "Garden" in workspace/garden. It is not active; '
            "type activate project 1 to use it.",
        )
        self.assertEqual(self.memory.get_project(1).project.folder, "garden")
        self.assertEqual(self.access.leaked(), (set(), set(), set()))

    def test_creation_refuses_by_its_fixed_texts(self) -> None:
        self.garden()
        for arguments, message in (
            (
                {"name": "Bad: name", "location": "workspace"},
                "A project name must be 1 to 64 UTF-8 bytes on one line, with no "
                "colon and no control or formatting characters.",
            ),
            (
                {"name": "X", "location": "elsewhere"},
                "A project location must be workspace, workspace/<folder>, or "
                "project_maria.",
            ),
            (
                {"name": "X", "location": "workspace/missing"},
                "That folder is not a readable folder of the workspace.",
            ),
            (
                {"name": "X", "location": "workspace/garden/beds"},
                "That location overlaps another project's location. Nothing was "
                "created.",
            ),
        ):
            with self.subTest(location=arguments["location"]):
                self.assertEqual(
                    self.run_action("create", **arguments),
                    (False, message, None),
                )
        unavailable = self.tool_with(None)
        self.assertEqual(
            unavailable.execute(
                "create",
                {"name": "X", "location": "workspace/other"},
            ).message,
            "Project file reading is not available on this computer.",
        )
        self.assertEqual(
            self.run_action("create", name="Maria", location="project_maria")[0],
            True,
        )

    def test_a_reading_shows_every_stored_value(self) -> None:
        self.garden()
        self.memory.add_project_fact(1, "One fact.")
        self.memory.activate_project(1)
        project = self.memory.get_project(1).project

        self.assertEqual(
            self.run_action("read", id=1),
            (
                True,
                "\n".join(
                    (
                        "Project 1",
                        'Name: "Garden"',
                        "Location: workspace/garden",
                        "Active: yes",
                        "Facts: 1",
                        f"Created at: {project.created_at}",
                        f"Updated at: {project.updated_at}",
                    )
                ),
                "Displayed project 1.",
            ),
        )
        self.assertEqual(
            self.run_action("read", id=9),
            (False, "Project 9 was not found.", None),
        )

    def test_renaming_and_deleting_by_their_rules(self) -> None:
        self.garden()
        self.assertEqual(
            self.run_action("retitle", id=1, name="Allotment"),
            (True, "Renamed project 1.", "Renamed project 1."),
        )
        self.memory.add_project_fact(1, "A fact.")
        self.assertEqual(
            self.run_action("delete", id=1)[1],
            "Project 1 still has facts. Delete them first. Nothing was deleted.",
        )
        self.memory.delete_project_fact(1)
        self.memory.activate_project(1)
        self.assertEqual(
            self.run_action("delete", id=1)[1],
            "Project 1 is the active project. Type deactivate project first. "
            "Nothing was deleted.",
        )
        self.memory.deactivate_project()
        self.assertEqual(
            self.run_action("delete", id=1),
            (True, "Deleted project 1.", "Deleted project 1."),
        )
        self.assertEqual(self.run_action("delete", id=1)[1], "Project 1 was not found.")


class ActivationActionTests(_ToolCase):
    """Activating and deactivating, through the memory layer's rules."""

    def test_activation_and_deactivation_replies(self) -> None:
        self.garden()
        self.memory.create_project("Other", "workspace", "other")

        self.assertEqual(
            self.run_action("activate", id=1),
            (True, "Project 1 is now the active project.", "Activated project 1."),
        )
        self.assertEqual(
            self.run_action("activate", id=1),
            (
                True,
                "Project 1 was already the active project.",
                "Project 1 was already the active project.",
            ),
        )
        self.assertEqual(
            self.run_action("activate", id=2),
            (
                True,
                "Project 2 is now the active project. It replaced project 1.",
                "Activated project 2.",
            ),
        )
        self.assertEqual(
            self.run_action("deactivate"),
            (True, "No project is active now.", "No project is active now."),
        )
        self.assertEqual(
            self.run_action("deactivate"),
            (True, "No project was active.", "No project was active."),
        )
        self.assertEqual(
            self.run_action("activate", id=9)[1],
            "Project 9 was not found.",
        )

    def test_at_the_largest_serial_activation_is_refused_with_its_text(self) -> None:
        self.garden()
        self.memory.activate_project(1)
        self.raw("UPDATE project_activation SET serial = ?", (ACTIVATION_SERIAL_MAX,))

        self.assertEqual(
            self.run_action("activate", id=1)[0:2],
            (True, "Project 1 was already the active project."),
        )
        self.memory.create_project("Other", "workspace", "other")
        self.assertEqual(
            self.run_action("activate", id=2),
            (
                False,
                "Maria cannot change the active project any more, because its "
                "change counter is at its limit. Nothing was changed. "
                "Deactivation still works.",
                None,
            ),
        )

    def test_storage_failures_give_the_busy_or_the_fixed_text(self) -> None:
        for raised, shown in (
            (RuntimeError(BUSY), BUSY),
            (RuntimeError(STORAGE), STORAGE),
            (ValueError("x"), STORAGE),
        ):
            with self.subTest(raised=type(raised).__name__):
                with patch.object(self.memory, "activate_project", side_effect=raised):
                    self.assertEqual(
                        self.run_action("activate", id=1),
                        (False, shown, None),
                    )


class FactActionTests(_ToolCase):
    """Listing, adding, editing, and deleting facts."""

    def setUp(self) -> None:
        super().setUp()
        self.garden()

    def test_fact_replies_and_recorded_lines(self) -> None:
        self.assertEqual(
            self.run_action("list_facts", id=1),
            (True, "Project 1 has no facts.", "Listed facts of project 1."),
        )
        self.assertEqual(
            self.run_action("add_fact", id=1, text="Water at dawn."),
            (True, "Added fact 1 to project 1.", "Added fact 1 to project 1."),
        )
        self.assertEqual(
            self.run_action("list_facts", id=1),
            (
                True,
                'Facts of project 1:\nFact 1: "Water at dawn."',
                "Listed facts of project 1.",
            ),
        )
        self.assertEqual(
            self.run_action(
                "edit_fact",
                id=1,
                text="Water at dusk.",
                text_digest=fact_digest(1, "Water at dawn."),
            ),
            (True, "Updated fact 1.", "Updated fact 1."),
        )
        self.assertEqual(
            self.run_action("edit_fact", id=1, text="Again.", text_digest="0" * 64),
            (
                False,
                "That fact changed after it was shown to you. Nothing was changed.",
                None,
            ),
        )
        self.assertEqual(
            self.run_action("delete_fact", id=1),
            (True, "Deleted fact 1.", "Deleted fact 1."),
        )
        self.assertEqual(
            self.run_action("delete_fact", id=1)[1],
            "Fact 1 was not found.",
        )
        self.assertEqual(
            self.run_action("add_fact", id=9, text="x")[1],
            "Project 9 was not found.",
        )
        self.assertEqual(
            self.run_action("add_fact", id=1, text="a\nb")[1],
            "A fact must be 1 to 512 UTF-8 bytes on one line, with no control or "
            "formatting characters.",
        )

    def test_more_than_fifty_facts_say_so(self) -> None:
        for index in range(51):
            self.memory.add_project_fact(1, f"Fact {index}.")

        lines = self.run_action("list_facts", id=1)[1].split("\n")

        self.assertEqual(len(lines), 52)
        self.assertEqual(lines[-1], "More facts are not shown.")


class PreviewTests(_ToolCase):
    """Each confirmation shows exactly what the approval covers."""

    def setUp(self) -> None:
        super().setUp()
        self.garden()

    def test_an_added_fact_shows_its_project_whether_active_and_the_fact(self) -> None:
        binding = self.preview("add_fact", id=1, text='Says "hi" to café.')

        self.assertEqual(binding.arguments, {})
        self.assertEqual(
            binding.preview,
            "This approval adds a fact to project 1, shown quoted:\n"
            'Project: 1, "Garden"\nActive: no\n'
            'Fact: "Says \\"hi\\" to café."',
        )
        self.memory.activate_project(1)
        self.assertIn("Active: yes", self.preview("add_fact", id=1, text="x").preview)
        self.assertEqual(self.preview("add_fact", id=1, text="a\u200bb"), {})

    def test_an_edit_shows_now_and_new_and_pins_the_shown_digest(self) -> None:
        self.memory.add_project_fact(1, "Old.")

        binding = self.preview("edit_fact", id=1, text="New.")

        self.assertEqual(binding.arguments, {"text_digest": fact_digest(1, "Old.")})
        self.assertEqual(
            binding.preview,
            "This approval replaces the text of fact 1 of project 1, shown "
            'quoted:\nNow: "Old."\nNew: "New."',
        )
        self.assertEqual(
            self.preview("delete_fact", id=1).preview,
            'This approval deletes fact 1 of project 1, shown quoted:\nFact: "Old."',
        )

    def test_rename_delete_and_activation_previews(self) -> None:
        self.assertEqual(
            self.preview("retitle", id=1, name="Allotment").preview,
            'This approval renames project 1, shown quoted:\nNow: "Garden"\n'
            'New: "Allotment"',
        )
        self.assertEqual(
            self.preview("delete", id=1).preview,
            'This approval deletes project 1, shown quoted:\nName: "Garden"\n'
            "Location: workspace/garden\nFacts: 0",
        )
        self.memory.create_project("Self review", "project_maria", "")
        self.memory.activate_project(1)
        self.assertEqual(
            self.preview("activate", id=2).preview,
            'This approval activates project 2, shown quoted:\nName: "Self '
            'review"\nLocation: project_maria\nFacts: 0\nReplaces: project 1\n'
            f"{PROJECT_MARIA_PREVIEW}",
        )

    def test_nothing_is_bound_when_nothing_can_be_shown(self) -> None:
        for action, arguments in (
            ("activate", {"id": 9}),
            ("add_fact", {"id": 1, "text": "bad\ntext"}),
            ("edit_fact", {"id": 9, "text": "x"}),
            ("list", {}),
        ):
            with self.subTest(action=action):
                self.assertEqual(self.tool.confirmation_binding(action, arguments), {})
        with patch.object(self.memory, "get_project", side_effect=RuntimeError("x")):
            self.assertEqual(self.preview("activate", id=1), {})


class FileActionTests(_ToolCase):
    """The active project's files, through the reader."""

    def test_file_commands_need_an_active_readable_setting(self) -> None:
        self.garden()
        self.assertEqual(self.run_action("list_files"), (False, NO_ACTIVE, None))
        self.assertEqual(
            self.run_action("read_file", path="plan.md"),
            (False, NO_ACTIVE, None),
        )
        self.raw(
            "INSERT INTO project_activation VALUES (1, 77, 3, ?)",
            ("2026-10-09T21:24:13Z",),
        )
        self.assertEqual(
            self.run_action("list_files"),
            (False, UNREADABLE_SETTING, None),
        )

    def test_a_listing_and_a_reading_of_the_active_project(self) -> None:
        self.garden()
        self.memory.activate_project(1)

        self.assertEqual(
            self.run_action("list_files"),
            (
                True,
                "Project 1, top folder:\ndirectory: beds\n"
                "file: plan.md, 14 bytes as listed by Windows",
                "Listed project files.",
            ),
        )
        self.assertEqual(
            self.run_action("list_files", folder="Beds"),
            (
                True,
                "Project 1, beds:\nfile: north.txt, 9 bytes as listed by Windows",
                "Listed project files.",
            ),
        )
        success, message, recorded = self.run_action(
            "read_file",
            path="plan.md",
            start=2,
            end=3,
        )
        self.assertTrue(success)
        self.assertTrue(
            message.startswith(
                "Project 1 file plan.md, lines 2-3 of 3, 14 bytes, SHA-256 "
            )
        )
        self.assertTrue(message.endswith(":\ntwo\nthree"))
        self.assertEqual(recorded, "Displayed a project file.")

    def test_file_refusals_record_their_fixed_texts(self) -> None:
        self.garden()
        self.memory.activate_project(1)
        self.volume.add_directory("garden/empty")

        line_range = (
            "A line range must be <from>-<to> within the file, each a whole "
            "number from 1 to 999999, with <from> no greater than <to>."
        )
        for action, arguments, message in (
            ("read_file", {"path": "photo.jpg"}, NOT_A_FILE),
            ("read_file", {"path": "missing.md"}, NOT_A_FILE),
            ("read_file", {"path": "plan.md", "start": 2, "end": 4}, line_range),
            ("read_file", {"path": "plan.md", "start": 2}, line_range),
            ("list_files", {"folder": "missing"}, NOT_A_FOLDER),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    self.run_action(action, **arguments),
                    (False, message, None),
                )
        self.assertEqual(
            self.run_action("list_files", folder="empty"),
            (
                True,
                "Project 1, empty:\nMaria can show nothing in that folder.",
                "Listed project files.",
            ),
        )

    def test_a_long_listing_says_what_it_left_out(self) -> None:
        for index in range(2_001):
            self.volume.add_file(f"garden/many/f{index:05d}.md", b"x")
        self.garden()
        self.memory.activate_project(1)

        lines = self.run_action("list_files", folder="many")[1].split("\n")

        self.assertEqual(len(lines), 203)
        self.assertEqual(lines[-2], "More entries are not shown.")
        self.assertEqual(
            lines[-1],
            "Maria examined only the first 2,000 entries; others are not shown.",
        )


class PlannerCommandTests(unittest.TestCase):
    """The thirteen commands, their usage replies, and their matching order."""

    def setUp(self) -> None:
        self.planner = Planner(logger=Mock(spec=logging.Logger))

    def step(self, text: str) -> tuple[str, str, dict[str, object]]:
        """Return the one step a command plans, as tool, action, and arguments."""
        (step,) = self.planner.create_plan(text).steps
        return step.tool_name, step.action, dict(step.arguments)

    def reply(self, text: str) -> str:
        """Return the reply of a command that plans no step."""
        plan = self.planner.create_plan(text)
        self.assertEqual(plan.steps, ())
        return plan.response_if_no_action

    def test_each_command_plans_its_one_action(self) -> None:
        for text, expected in (
            ("projects", ("projects", "list", {})),
            ("PROJECTS", ("projects", "list", {})),
            (
                "create project Garden: workspace/garden",
                (
                    "projects",
                    "create",
                    {"name": "Garden", "location": "workspace/garden"},
                ),
            ),
            ("read project 3", ("projects", "read", {"id": 3})),
            (
                "retitle project 3: New name",
                ("projects", "retitle", {"id": 3, "name": "New name"}),
            ),
            ("delete project 3", ("projects", "delete", {"id": 3})),
            ("activate project 3", ("projects", "activate", {"id": 3})),
            ("deactivate project", ("projects", "deactivate", {})),
            ("project facts 3", ("projects", "list_facts", {"id": 3})),
            (
                "add project fact 3: Tests: run them all.",
                ("projects", "add_fact", {"id": 3, "text": "Tests: run them all."}),
            ),
            (
                "edit project fact 7: New text",
                ("projects", "edit_fact", {"id": 7, "text": "New text"}),
            ),
            ("delete project fact 7", ("projects", "delete_fact", {"id": 7})),
            ("project files", ("projects", "list_files", {})),
            (
                "project files docs/notes",
                ("projects", "list_files", {"folder": "docs/notes"}),
            ),
            (
                "read project file docs/a b.md",
                ("projects", "read_file", {"path": "docs/a b.md"}),
            ),
            (
                "read project file docs/a.md lines 2-40",
                ("projects", "read_file", {"path": "docs/a.md", "start": 2, "end": 40}),
            ),
            (
                "read project file a.md LINES 000001-999999",
                ("projects", "read_file", {"path": "a.md", "start": 1, "end": 999_999}),
            ),
            ("list projects", ("filesystem", "list", {"path": "projects"})),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.step(text), expected)

    def test_usage_and_id_replies(self) -> None:
        for text, reply in (
            ("create project", CREATE_USAGE),
            ("create project Garden", CREATE_USAGE),
            ("create project : workspace", CREATE_USAGE),
            ("read project", "Use: read project <id>"),
            ("read project x", "Project id must be a positive whole number."),
            ("read project files", "Project id must be a positive whole number."),
            ("retitle project", "Use: retitle project <id>: <new name>"),
            ("retitle project 3", "Use: retitle project <id>: <new name>"),
            ("retitle project x: y", "Project id must be a positive whole number."),
            ("delete project", "Use: delete project <id>"),
            ("delete project fact", "Use: delete project fact <fact id>"),
            ("delete project fact x", "Fact id must be a positive whole number."),
            ("activate project", "Use: activate project <id>"),
            (
                "activate project the garden",
                "Project id must be a positive whole number.",
            ),
            ("project facts", "Use: project facts <project id>"),
            ("add project fact", "Use: add project fact <project id>: <text>"),
            ("add project fact 3:", "Use: add project fact <project id>: <text>"),
            ("edit project fact", "Use: edit project fact <fact id>: <new text>"),
            ("edit project fact 0: x", "Fact id must be a positive whole number."),
            ("read project file", READ_FILE_USAGE),
            (
                "read project file a.md lines 0-3",
                "A line range must be <from>-<to> within the file, each a whole "
                "number from 1 to 999999, with <from> no greater than <to>.",
            ),
            (
                "read project file a.md lines 1-1000000",
                "A line range must be <from>-<to> within the file, each a whole "
                "number from 1 to 999999, with <from> no greater than <to>.",
            ),
            (
                "read project file a.md lines abc",
                "A line range must be <from>-<to> within the file, each a whole "
                "number from 1 to 999999, with <from> no greater than <to>.",
            ),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.reply(text), reply)

    def test_a_reference_in_a_question_is_left_for_the_model(self) -> None:
        plan = self.planner.create_plan("what does @docs/a.md say?")

        self.assertEqual(plan.steps, ())
        self.assertTrue(
            plan.response_if_no_action.startswith("I do not have an executable plan")
        )


class PermissionTests(unittest.TestCase):
    """Six project actions need confirmation, three with notices; seven are allowed."""

    def test_the_confirmed_allowed_and_noticed_actions(self) -> None:
        manager = PermissionManager(
            frozenset(),
            frozenset(),
            logger=Mock(spec=logging.Logger),
        )
        confirmed = {
            "projects.retitle",
            "projects.delete",
            "projects.activate",
            "projects.add_fact",
            "projects.edit_fact",
            "projects.delete_fact",
        }
        allowed = {
            "projects.list",
            "projects.create",
            "projects.read",
            "projects.deactivate",
            "projects.list_facts",
            "projects.list_files",
            "projects.read_file",
        }

        self.assertLessEqual(confirmed, MANDATORY_CONFIRMATION_ACTIONS)
        self.assertFalse(allowed & MANDATORY_CONFIRMATION_ACTIONS)
        for identifier in confirmed | allowed:
            with self.subTest(action=identifier):
                expected = (
                    PermissionDecision.REQUIRES_CONFIRMATION
                    if identifier in confirmed
                    else PermissionDecision.ALLOWED
                )
                self.assertIs(manager.check(*identifier.split(".")).decision, expected)
        self.assertEqual(
            {
                action
                for action in CONFIRMATION_NOTICES
                if action.startswith("projects.")
            },
            {"projects.activate", "projects.add_fact", "projects.edit_fact"},
        )


if __name__ == "__main__":
    unittest.main()
