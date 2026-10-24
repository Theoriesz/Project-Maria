"""Projects tool for Maria (V1-24-R1).

The tool lists, creates, reads, renames, and deletes the owner's projects,
activates and deactivates one, lists, adds, edits, and deletes a project's
facts, and lists and shows the active project's files, through the memory
layer and ``core/project_files.py``. None of its actions is ever a model's to
select.

Renaming, deleting, activating, adding a fact, editing a fact, and deleting
a fact are confirmation-required, and this tool's binding shows the owner
exactly what an approval covers: the project's quoted name and location and
its fact count, the project an activation replaces, the quoted fact, or the
saved and the new text side by side. Editing a fact pins the fact's approval
digest as it was shown, so an edit applies only while the fact is still that
text. Creating a project first checks its folder through the file reader,
which closes every handle before the project is stored, and is refused when
the location overlaps another project's.

A file command reads the activation and the active project in one snapshot
and works only while a project is active. Every successful action records a
fixed line naming ids at most; a refusal records its fixed text, which holds
no name, path, or file text. Logs name ids and counts only.
"""

from __future__ import annotations

from collections.abc import Mapping
from logging import Logger
from typing import Any

from core.context import quoted_for_owner
from core.logger import get_logger
from core.memory import Memory
from core.project_files import (
    FileRefusal,
    ProjectFileReader,
    decode_file,
    file_display,
    refusal_message,
)
from core.project_records import (
    ACTIVATION_LIMIT_MESSAGE,
    ACTIVATION_UNREADABLE_MESSAGE,
    ALREADY_ACTIVE_SUFFIX,
    DEACTIVATED_MESSAGE,
    FACT_CHANGED_MESSAGE,
    FACT_ID_MESSAGE,
    FACT_LIST_LIMIT,
    FACT_QUERY_LIMIT,
    FACT_TEXT_MESSAGE,
    FACTS_MORE_MESSAGE,
    LINE_RANGE_MESSAGE,
    LISTING_SHOW_LIMIT,
    LOCATION_MESSAGE,
    MORE_ENTRIES_MESSAGE,
    MORE_PROJECTS_MESSAGE,
    NO_ACTIVE_PROJECT_MESSAGE,
    NO_PROJECTS_MESSAGE,
    NONE_WAS_ACTIVE_MESSAGE,
    NOTHING_TO_LIST_MESSAGE,
    OVERLAP_MESSAGE,
    PROJECT_ID_MESSAGE,
    PROJECT_LIST_LIMIT,
    PROJECT_MARIA_PREVIEW,
    PROJECT_MARIA_ROOT,
    PROJECT_NAME_MESSAGE,
    PROJECT_QUERY_LIMIT,
    PROJECT_STORAGE_MESSAGE,
    PROJECTS_HEADER,
    SCAN_STOPPED_MESSAGE,
    TOP_FOLDER_LABEL,
    WORKSPACE_FOLDER_MESSAGE,
    WORKSPACE_ROOT,
    ActivationChange,
    ActivationResult,
    ActivationState,
    DeactivationChange,
    FactEdit,
    FactListing,
    Project,
    ProjectContextRead,
    ProjectDeletion,
    ProjectFact,
    ProjectListing,
    ProjectReading,
    describe_location,
    is_fact_text,
    is_project_name,
    is_project_path,
    parse_location,
)
from core.task_records import is_record_id
from core.tool_manager import ConfirmationBinding, ToolExecutionResult


UNSUPPORTED_ACTION_MESSAGE = "Unsupported project action."

# The shared busy text the memory layer raises, shown unchanged; every other
# storage failure is shown as the fixed project storage text.
DATABASE_BUSY_MESSAGE = "Database is busy. Try again shortly."

LISTED_PROJECTS_RECORD = "Listed projects."
LISTED_FILES_RECORD = "Listed project files."
DISPLAYED_FILE_RECORD = "Displayed a project file."


def project_not_found_message(project_id: int) -> str:
    """Return the fixed reply for an id no project has."""
    return f"Project {project_id} was not found."


def fact_not_found_message(fact_id: int) -> str:
    """Return the fixed reply for an id no fact has."""
    return f"Fact {fact_id} was not found."


def project_line(project: Project, active_id: int | None) -> str:
    """Render one project as one listing line, its name quoted."""
    line = (
        f"Project {project.project_id}: {quoted_for_owner(project.name)} in "
        f"{project.location}"
    )
    if project.project_id == active_id:
        line = f"{line}{ALREADY_ACTIVE_SUFFIX}"
    return line


class ProjectsTool:
    """List, create, read, change, activate, and delete projects and facts."""

    name = "projects"
    description = (
        "Lists, creates, reads, renames, and deletes projects, activates and "
        "deactivates one, lists, adds, edits, and deletes project facts, and "
        "lists and shows the active project's files."
    )
    actions = frozenset(
        {
            "list",
            "create",
            "read",
            "retitle",
            "delete",
            "activate",
            "deactivate",
            "list_facts",
            "add_fact",
            "edit_fact",
            "delete_fact",
            "list_files",
            "read_file",
        }
    )

    def __init__(
        self,
        memory: Memory,
        reader: ProjectFileReader,
        logger: Logger | None = None,
    ) -> None:
        self.memory = memory
        self.reader = reader
        self.logger = logger or get_logger(__name__)

    def confirmation_binding(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any] | ConfirmationBinding:
        """Show the owner exactly what an approval covers, before any change.

        Each confirmed action reads what it changes once and shows it, its
        stored text quoted. Editing a fact pins the fact's approval digest as
        ``text_digest``, so the edit applies only while the fact is exactly
        what was shown. The preview is never recorded, logged, or sent to a
        model, and nothing is added when nothing can be shown.
        """
        try:
            if action in {"retitle", "delete", "activate", "add_fact"}:
                return self._project_binding(action, arguments)
            if action in {"edit_fact", "delete_fact"}:
                return self._fact_binding(action, arguments)
        except (RuntimeError, ValueError):
            self.logger.warning("Could not prepare approval for a project action.")
        return {}

    def execute(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> ToolExecutionResult:
        """Execute a project action."""
        handlers = {
            "list": self._list,
            "create": self._create,
            "read": self._read,
            "retitle": self._retitle,
            "delete": self._delete,
            "activate": self._activate,
            "deactivate": self._deactivate,
            "list_facts": self._list_facts,
            "add_fact": self._add_fact,
            "edit_fact": self._edit_fact,
            "delete_fact": self._delete_fact,
            "list_files": self._list_files,
            "read_file": self._read_file,
        }
        handler = handlers.get(action)
        if handler is None:
            return ToolExecutionResult(False, UNSUPPORTED_ACTION_MESSAGE)
        try:
            return handler(arguments)
        except RuntimeError as failure:
            self.logger.warning("A project action could not use storage.")
            return ToolExecutionResult(False, _storage_message(failure))
        except ValueError:
            self.logger.warning("A project action could not use storage.")
            return ToolExecutionResult(False, PROJECT_STORAGE_MESSAGE)

    def _project_binding(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any] | ConfirmationBinding:
        """Read one project once and show what the approval covers."""
        project_id = arguments.get("id")
        if not is_record_id(project_id):
            return {}
        reading = self.memory.get_project(project_id)
        project = reading.project if type(reading) is ProjectReading else None
        if type(project) is not Project:
            return {}
        quoted = quoted_for_owner(project.name)
        if action == "retitle":
            name = arguments.get("name")
            if not is_project_name(name):
                return {}
            return ConfirmationBinding(
                {},
                "\n".join(
                    (
                        f"This approval renames project {project_id}, shown "
                        "quoted:",
                        f"Now: {quoted}",
                        f"New: {quoted_for_owner(name)}",
                    )
                ),
            )
        details = (
            f"Name: {quoted}",
            f"Location: {project.location}",
            f"Facts: {reading.fact_count}",
        )
        if action == "delete":
            return ConfirmationBinding(
                {},
                "\n".join(
                    (
                        f"This approval deletes project {project_id}, shown "
                        "quoted:",
                        *details,
                    )
                ),
            )
        if action == "add_fact":
            text = arguments.get("text")
            if not is_fact_text(text):
                return {}
            active = reading.activation.active_project_id == project_id
            return ConfirmationBinding(
                {},
                "\n".join(
                    (
                        f"This approval adds a fact to project {project_id}, "
                        "shown quoted:",
                        f"Project: {project_id}, {quoted}",
                        f"Active: {'yes' if active else 'no'}",
                        f"Fact: {quoted_for_owner(text)}",
                    )
                ),
            )
        lines = [
            f"This approval activates project {project_id}, shown quoted:",
            *details,
        ]
        replaced = reading.activation.active_project_id
        if replaced is not None and replaced != project_id:
            lines.append(f"Replaces: project {replaced}")
        if project.root == PROJECT_MARIA_ROOT:
            lines.append(PROJECT_MARIA_PREVIEW)
        return ConfirmationBinding({}, "\n".join(lines))

    def _fact_binding(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any] | ConfirmationBinding:
        """Read one fact once, show it, and pin its digest for an edit."""
        fact_id = arguments.get("id")
        if not is_record_id(fact_id):
            return {}
        fact = self.memory.get_project_fact(fact_id)
        if type(fact) is not ProjectFact:
            return {}
        if action == "delete_fact":
            return ConfirmationBinding(
                {},
                "\n".join(
                    (
                        f"This approval deletes fact {fact_id} of project "
                        f"{fact.project_id}, shown quoted:",
                        f"Fact: {quoted_for_owner(fact.text)}",
                    )
                ),
            )
        text = arguments.get("text")
        if not is_fact_text(text):
            return {}
        return ConfirmationBinding(
            {"text_digest": fact.approved_digest},
            "\n".join(
                (
                    f"This approval replaces the text of fact {fact_id} of "
                    f"project {fact.project_id}, shown quoted:",
                    f"Now: {quoted_for_owner(fact.text)}",
                    f"New: {quoted_for_owner(text)}",
                )
            ),
        )

    def _list(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """List projects by id, marking the active one."""
        listing = self.memory.list_projects(PROJECT_QUERY_LIMIT)
        if type(listing) is not ProjectListing:
            return ToolExecutionResult(False, PROJECT_STORAGE_MESSAGE)
        activation = listing.activation
        active_id = activation.active_project_id
        visible = listing.projects[:PROJECT_LIST_LIMIT]
        if visible:
            lines = [PROJECTS_HEADER]
            lines.extend(project_line(project, active_id) for project in visible)
            if len(listing.projects) > PROJECT_LIST_LIMIT:
                lines.append(MORE_PROJECTS_MESSAGE)
        else:
            lines = [NO_PROJECTS_MESSAGE]
        if activation.state is ActivationState.UNREADABLE:
            lines.append(ACTIVATION_UNREADABLE_MESSAGE)
        self.logger.info("Listed %s project(s).", len(visible))
        return ToolExecutionResult(
            True,
            "\n".join(lines),
            [project.project_id for project in visible],
            recorded_message=LISTED_PROJECTS_RECORD,
        )

    def _create(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Store a project after its folder passes the file checks."""
        name = arguments.get("name")
        if not is_project_name(name):
            return ToolExecutionResult(False, PROJECT_NAME_MESSAGE)
        location = parse_location(arguments.get("location"))
        if location is None:
            return ToolExecutionResult(False, LOCATION_MESSAGE)
        root, folder = location
        if root == WORKSPACE_ROOT:
            checked = self.reader.check_workspace_folder(folder)
            if isinstance(checked, FileRefusal):
                if checked is FileRefusal.UNREADABLE:
                    return ToolExecutionResult(False, WORKSPACE_FOLDER_MESSAGE)
                return ToolExecutionResult(
                    False,
                    refusal_message(checked, folder=True),
                )
            folder = checked
        project_id = self.memory.create_project(name, root, folder)
        if project_id is None:
            return ToolExecutionResult(False, OVERLAP_MESSAGE)
        self.logger.info("Created project %s through projects tool.", project_id)
        return ToolExecutionResult(
            True,
            (
                f"Created project {project_id}: {quoted_for_owner(name)} in "
                f"{describe_location(root, folder)}. It is not active; type "
                f"activate project {project_id} to use it."
            ),
            {"id": project_id},
            recorded_message=f"Created project {project_id}.",
        )

    def _read(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Show one project's stored values; history records only its id."""
        project_id = arguments.get("id")
        if not is_record_id(project_id):
            return ToolExecutionResult(False, PROJECT_ID_MESSAGE)
        reading = self.memory.get_project(project_id)
        if type(reading) is not ProjectReading:
            return ToolExecutionResult(False, PROJECT_STORAGE_MESSAGE)
        project = reading.project
        if project is None:
            return ToolExecutionResult(False, project_not_found_message(project_id))
        active = reading.activation.active_project_id == project_id
        self.logger.info("Displayed project %s through projects tool.", project_id)
        return ToolExecutionResult(
            True,
            "\n".join(
                (
                    f"Project {project_id}",
                    f"Name: {quoted_for_owner(project.name)}",
                    f"Location: {project.location}",
                    f"Active: {'yes' if active else 'no'}",
                    f"Facts: {reading.fact_count}",
                    f"Created at: {project.created_at}",
                    f"Updated at: {project.updated_at}",
                )
            ),
            {"id": project_id},
            recorded_message=f"Displayed project {project_id}.",
        )

    def _retitle(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Replace one project's name; nothing else changes."""
        project_id = arguments.get("id")
        if not is_record_id(project_id):
            return ToolExecutionResult(False, PROJECT_ID_MESSAGE)
        name = arguments.get("name")
        if not is_project_name(name):
            return ToolExecutionResult(False, PROJECT_NAME_MESSAGE)
        if self.memory.retitle_project(project_id, name) is not True:
            return ToolExecutionResult(False, project_not_found_message(project_id))
        message = f"Renamed project {project_id}."
        return ToolExecutionResult(
            True,
            message,
            {"id": project_id},
            recorded_message=message,
        )

    def _delete(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Delete one project unless it is active or still has facts."""
        project_id = arguments.get("id")
        if not is_record_id(project_id):
            return ToolExecutionResult(False, PROJECT_ID_MESSAGE)
        outcome = self.memory.delete_project(project_id)
        if outcome is ProjectDeletion.ACTIVE:
            return ToolExecutionResult(
                False,
                f"Project {project_id} is the active project. Type deactivate "
                "project first. Nothing was deleted.",
            )
        if outcome is ProjectDeletion.HAS_FACTS:
            return ToolExecutionResult(
                False,
                f"Project {project_id} still has facts. Delete them first. "
                "Nothing was deleted.",
            )
        if outcome is not ProjectDeletion.DELETED:
            return ToolExecutionResult(False, project_not_found_message(project_id))
        message = f"Deleted project {project_id}."
        return ToolExecutionResult(
            True,
            message,
            {"id": project_id},
            recorded_message=message,
        )

    def _activate(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Make one project the active one, as the activation rules allow."""
        project_id = arguments.get("id")
        if not is_record_id(project_id):
            return ToolExecutionResult(False, PROJECT_ID_MESSAGE)
        result = self.memory.activate_project(project_id)
        if type(result) is not ActivationResult:
            return ToolExecutionResult(False, PROJECT_STORAGE_MESSAGE)
        if result.change is ActivationChange.NOT_FOUND:
            return ToolExecutionResult(False, project_not_found_message(project_id))
        if result.change is ActivationChange.AT_LIMIT:
            return ToolExecutionResult(False, ACTIVATION_LIMIT_MESSAGE)
        if result.change is ActivationChange.ALREADY_ACTIVE:
            message = f"Project {project_id} was already the active project."
            return ToolExecutionResult(
                True,
                message,
                {"id": project_id},
                recorded_message=message,
            )
        shown = f"Project {project_id} is now the active project."
        if result.replaced_project_id is not None:
            shown = f"{shown} It replaced project {result.replaced_project_id}."
        return ToolExecutionResult(
            True,
            shown,
            {"id": project_id},
            recorded_message=f"Activated project {project_id}.",
        )

    def _deactivate(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Leave no project active; it only ever narrows exposure."""
        outcome = self.memory.deactivate_project()
        message = (
            DEACTIVATED_MESSAGE
            if outcome is DeactivationChange.DEACTIVATED
            else NONE_WAS_ACTIVE_MESSAGE
        )
        return ToolExecutionResult(True, message, None, recorded_message=message)

    def _list_facts(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """List one project's facts by id, their text quoted."""
        project_id = arguments.get("id")
        if not is_record_id(project_id):
            return ToolExecutionResult(False, PROJECT_ID_MESSAGE)
        listing = self.memory.list_project_facts(project_id, FACT_QUERY_LIMIT)
        if type(listing) is not FactListing:
            return ToolExecutionResult(False, PROJECT_STORAGE_MESSAGE)
        if listing.project is None:
            return ToolExecutionResult(False, project_not_found_message(project_id))
        visible = listing.facts[:FACT_LIST_LIMIT]
        if visible:
            lines = [f"Facts of project {project_id}:"]
            lines.extend(
                f"Fact {fact.fact_id}: {quoted_for_owner(fact.text)}"
                for fact in visible
            )
            if len(listing.facts) > FACT_LIST_LIMIT:
                lines.append(FACTS_MORE_MESSAGE)
        else:
            lines = [f"Project {project_id} has no facts."]
        self.logger.info(
            "Listed %s fact(s) of project %s.",
            len(visible),
            project_id,
        )
        return ToolExecutionResult(
            True,
            "\n".join(lines),
            [fact.fact_id for fact in visible],
            recorded_message=f"Listed facts of project {project_id}.",
        )

    def _add_fact(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Store exactly the approved fact, with its approval digest."""
        project_id = arguments.get("id")
        if not is_record_id(project_id):
            return ToolExecutionResult(False, PROJECT_ID_MESSAGE)
        text = arguments.get("text")
        if not is_fact_text(text):
            return ToolExecutionResult(False, FACT_TEXT_MESSAGE)
        fact_id = self.memory.add_project_fact(project_id, text)
        if fact_id is None:
            return ToolExecutionResult(False, project_not_found_message(project_id))
        message = f"Added fact {fact_id} to project {project_id}."
        return ToolExecutionResult(
            True,
            message,
            {"id": fact_id, "project_id": project_id},
            recorded_message=message,
        )

    def _edit_fact(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Replace one fact's text, only while it is still as it was shown."""
        fact_id = arguments.get("id")
        if not is_record_id(fact_id):
            return ToolExecutionResult(False, FACT_ID_MESSAGE)
        text = arguments.get("text")
        if not is_fact_text(text):
            return ToolExecutionResult(False, FACT_TEXT_MESSAGE)
        outcome = self.memory.edit_project_fact(
            fact_id,
            text,
            arguments.get("text_digest"),
        )
        if outcome is FactEdit.NOT_FOUND:
            return ToolExecutionResult(False, fact_not_found_message(fact_id))
        if outcome is not FactEdit.EDITED:
            return ToolExecutionResult(False, FACT_CHANGED_MESSAGE)
        message = f"Updated fact {fact_id}."
        return ToolExecutionResult(
            True,
            message,
            {"id": fact_id},
            recorded_message=message,
        )

    def _delete_fact(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Delete one fact by id."""
        fact_id = arguments.get("id")
        if not is_record_id(fact_id):
            return ToolExecutionResult(False, FACT_ID_MESSAGE)
        if self.memory.delete_project_fact(fact_id) is not True:
            return ToolExecutionResult(False, fact_not_found_message(fact_id))
        message = f"Deleted fact {fact_id}."
        return ToolExecutionResult(
            True,
            message,
            {"id": fact_id},
            recorded_message=message,
        )

    def _list_files(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """List one folder of the active project, by name and listed size."""
        project = self._active_project()
        if type(project) is str:
            return ToolExecutionResult(False, project)
        folder = arguments.get("folder", "")
        if type(folder) is not str or (folder and not is_project_path(folder)):
            return ToolExecutionResult(
                False,
                refusal_message(FileRefusal.UNREADABLE, folder=True),
            )
        listing = self.reader.list_folder(project, folder)
        if isinstance(listing, FileRefusal):
            return ToolExecutionResult(False, refusal_message(listing, folder=True))
        label = listing.folder or TOP_FOLDER_LABEL
        lines = [f"Project {project.project_id}, {label}:"]
        visible = listing.entries[:LISTING_SHOW_LIMIT]
        for entry in visible:
            if entry.directory:
                lines.append(f"directory: {entry.name}")
            else:
                lines.append(
                    f"file: {entry.name}, {entry.size} bytes as listed by Windows"
                )
        if not visible:
            lines.append(NOTHING_TO_LIST_MESSAGE)
        if len(listing.entries) > LISTING_SHOW_LIMIT:
            lines.append(MORE_ENTRIES_MESSAGE)
        if listing.scan_stopped:
            lines.append(SCAN_STOPPED_MESSAGE)
        self.logger.info(
            "Listed %s entr(ies) of project %s.",
            len(visible),
            project.project_id,
        )
        return ToolExecutionResult(
            True,
            "\n".join(lines),
            {"id": project.project_id, "count": len(visible)},
            recorded_message=LISTED_FILES_RECORD,
        )

    def _read_file(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Show a bounded part of one admitted file of the active project."""
        project = self._active_project()
        if type(project) is str:
            return ToolExecutionResult(False, project)
        path = arguments.get("path")
        start = arguments.get("start")
        end = arguments.get("end")
        if not is_project_path(path):
            return ToolExecutionResult(
                False,
                refusal_message(FileRefusal.UNREADABLE),
            )
        if (start is None) != (end is None) or (
            start is not None and not _is_line_range(start, end)
        ):
            return ToolExecutionResult(False, LINE_RANGE_MESSAGE)
        content = self.reader.read_file(project, path)
        if isinstance(content, FileRefusal):
            return ToolExecutionResult(False, refusal_message(content))
        decoded = decode_file(content)
        if isinstance(decoded, FileRefusal):
            return ToolExecutionResult(False, refusal_message(decoded))
        shown = file_display(
            project.project_id,
            decoded,
            None if start is None else (start, end),
        )
        if isinstance(shown, FileRefusal):
            return ToolExecutionResult(False, refusal_message(shown))
        self.logger.info(
            "Displayed a file of project %s through projects tool.",
            project.project_id,
        )
        return ToolExecutionResult(
            True,
            shown,
            {"id": project.project_id},
            recorded_message=DISPLAYED_FILE_RECORD,
        )

    def _active_project(self) -> Project | str:
        """Return the active project from one snapshot, or the fixed refusal."""
        snapshot = self.memory.active_project()
        if type(snapshot) is not ProjectContextRead:
            return PROJECT_STORAGE_MESSAGE
        state = snapshot.activation.state
        if state is ActivationState.UNREADABLE:
            return ACTIVATION_UNREADABLE_MESSAGE
        if state is not ActivationState.ACTIVE or snapshot.project is None:
            return NO_ACTIVE_PROJECT_MESSAGE
        return snapshot.project


def _is_line_range(start: object, end: object) -> bool:
    """Return whether two line numbers form a range, the first no greater."""
    return (
        type(start) is int
        and type(end) is int
        and 1 <= start <= end
    )


def _storage_message(failure: RuntimeError) -> str:
    """Return the shared busy text when storage was busy, else the fixed text."""
    if failure.args == (DATABASE_BUSY_MESSAGE,):
        return DATABASE_BUSY_MESSAGE
    return PROJECT_STORAGE_MESSAGE


def build_tool(config: Any, memory: Memory, **_: Any) -> ProjectsTool:
    """Build a projects tool reading files within the settled roots."""
    return ProjectsTool(
        memory=memory,
        reader=ProjectFileReader(
            config.filesystem_roots,
            config.filesystem_protections,
            get_logger("project_files"),
        ),
    )
