"""Safe filesystem tool for Maria."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from logging import Logger
from pathlib import Path
from typing import Any

from core.config import MariaConfig
from core.constants import DEFAULT_TEXT_READ_LIMIT_BYTES
from core.logger import get_logger
from core.tool_manager import ToolExecutionResult


PATH_REQUIRED_MESSAGE = "Path is required."
PATH_OUTSIDE_ROOT_MESSAGE = "Path is outside Maria's configured safe root."
UNCONTAINED_DISPLAY = "<path outside safe root>"
UNSPECIFIED_FAILURE_REASON = "The operating system reported an unspecified error."


def _errno_reason(number: int | None) -> str:
    """Describe an OS error using only its numeric code."""
    if not isinstance(number, int):
        return UNSPECIFIED_FAILURE_REASON
    try:
        reason = os.strerror(number).strip()
    except (OverflowError, ValueError):
        return UNSPECIFIED_FAILURE_REASON
    return reason or UNSPECIFIED_FAILURE_REASON


def _failure_reason(exc: OSError | ValueError) -> str:
    """Describe a filesystem failure without exposing host paths.

    An ``OSError`` reason is derived from its numeric ``errno``. Only Maria's
    two fixed controlled ``ValueError`` messages are preserved, and every other
    failure uses the generic fallback. Exception-supplied text is read solely
    as a whitelist comparison key, so raw exception text and path fields such
    as ``strerror``, ``filename``, ``filename2``, and ``args`` are never
    returned or interpolated into user-facing response text.
    """
    if isinstance(exc, OSError):
        return _errno_reason(exc.errno)

    message = str(exc)
    if message == PATH_REQUIRED_MESSAGE:
        return PATH_REQUIRED_MESSAGE
    if message == PATH_OUTSIDE_ROOT_MESSAGE:
        return PATH_OUTSIDE_ROOT_MESSAGE
    return UNSPECIFIED_FAILURE_REASON


class FilesystemTool:
    """Perform safe filesystem operations inside a configured root."""

    name = "filesystem"
    description = "Reads, lists, renames, and moves files inside a safe root."
    actions = frozenset({"read", "list", "rename", "move"})

    def __init__(
        self,
        root_path: Path,
        logger: Logger | None = None,
    ) -> None:
        self.root_path = root_path.resolve()
        self.logger = logger or get_logger(__name__)

    def execute(
        self,
        action: str,
        arguments: Mapping[str, Any],
    ) -> ToolExecutionResult:
        """Execute a filesystem action."""
        if action == "read":
            return self._read(arguments)
        if action == "list":
            return self._list(arguments)
        if action == "rename":
            return self._rename(arguments)
        if action == "move":
            return self._move(arguments)
        return ToolExecutionResult(False, f"Unsupported filesystem action: {action}")

    def _read(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Read a text file inside the safe root."""
        try:
            path = self._safe_path(str(arguments.get("path", "")))
            if not path.is_file():
                return ToolExecutionResult(
                    False,
                    f"Not a file: {self._display_path(path)}",
                )
            if path.stat().st_size > DEFAULT_TEXT_READ_LIMIT_BYTES:
                return ToolExecutionResult(
                    False,
                    "File exceeds Maria's text-read limit.",
                )

            text = path.read_text(encoding="utf-8", errors="replace")
            self.logger.info("Read file: %s", path)
            return ToolExecutionResult(True, text, {"path": str(path)})
        except (OSError, ValueError) as exc:
            self.logger.warning("File read failed: %s", exc)
            return ToolExecutionResult(
                False,
                f"File read failed: {_failure_reason(exc)}",
            )

    def _list(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """List a directory inside the safe root."""
        try:
            path = self._safe_path(str(arguments.get("path", ".")))
            if not path.is_dir():
                return ToolExecutionResult(
                    False,
                    f"Not a directory: {self._display_path(path)}",
                )

            entries = [
                {
                    "name": child.name,
                    "type": "directory" if child.is_dir() else "file",
                }
                for child in sorted(path.iterdir(), key=lambda item: item.name.lower())
            ]
            self.logger.info("Listed directory: %s", path)
            if not entries:
                return ToolExecutionResult(True, "Directory is empty.", entries)
            listing = "\n".join(f"{entry['type']}: {entry['name']}" for entry in entries)
            return ToolExecutionResult(True, listing, entries)
        except (OSError, ValueError) as exc:
            self.logger.warning("Directory listing failed: %s", exc)
            return ToolExecutionResult(
                False,
                f"Directory listing failed: {_failure_reason(exc)}",
            )

    def _rename(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Rename a file or directory inside the safe root."""
        try:
            source = self._safe_path(str(arguments.get("source", "")))
            destination = self._safe_path(str(arguments.get("destination", "")))
            if not source.exists():
                return ToolExecutionResult(
                    False,
                    f"Source does not exist: {self._display_path(source)}",
                )
            if destination.exists():
                return ToolExecutionResult(
                    False,
                    f"Destination already exists: {self._display_path(destination)}",
                )

            source.rename(destination)
            self.logger.info("Renamed %s to %s", source, destination)
            return ToolExecutionResult(
                True,
                f"Renamed {self._display_path(source)} to "
                f"{self._display_path(destination)}.",
                {"source": str(source), "destination": str(destination)},
            )
        except (OSError, ValueError) as exc:
            self.logger.warning("Rename failed: %s", exc)
            return ToolExecutionResult(
                False,
                f"Rename failed: {_failure_reason(exc)}",
            )

    def _move(self, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """Move a file or directory inside the safe root."""
        try:
            source = self._safe_path(str(arguments.get("source", "")))
            destination = self._safe_path(str(arguments.get("destination", "")))
            if not source.exists():
                return ToolExecutionResult(
                    False,
                    f"Source does not exist: {self._display_path(source)}",
                )
            if destination.exists():
                return ToolExecutionResult(
                    False,
                    f"Destination already exists: {self._display_path(destination)}",
                )

            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            self.logger.info("Moved %s to %s", source, destination)
            return ToolExecutionResult(
                True,
                f"Moved {self._display_path(source)} to "
                f"{self._display_path(destination)}.",
                {"source": str(source), "destination": str(destination)},
            )
        except (OSError, ValueError) as exc:
            self.logger.warning("Move failed: %s", exc)
            return ToolExecutionResult(
                False,
                f"Move failed: {_failure_reason(exc)}",
            )

    def _display_path(self, path: Path) -> str:
        """Render an already-resolved contained path for user-facing text."""
        try:
            return path.relative_to(self.root_path).as_posix()
        except ValueError:
            return UNCONTAINED_DISPLAY

    def _safe_path(self, value: str) -> Path:
        """Resolve a path and ensure it remains inside the safe root."""
        if not value.strip():
            raise ValueError("Path is required.")

        raw_path = Path(value).expanduser()
        candidate = raw_path if raw_path.is_absolute() else self.root_path / raw_path
        resolved = candidate.resolve()

        if not resolved.is_relative_to(self.root_path):
            raise ValueError("Path is outside Maria's configured safe root.")
        return resolved


def build_tool(config: MariaConfig, **_: Any) -> FilesystemTool:
    """Build a filesystem tool instance."""
    return FilesystemTool(root_path=config.safe_filesystem_root)
