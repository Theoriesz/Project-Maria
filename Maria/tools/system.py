"""Read-only system information tool for Maria."""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
from datetime import datetime
from logging import Logger
from pathlib import Path
from typing import Any

from core.logger import get_logger
from core.tool_manager import ToolExecutionResult


class SystemTool:
    """Return read-only system information."""

    name = "system"
    description = "Reports read-only system information."
    actions = frozenset({"overview", "current_time"})

    def __init__(self, logger: Logger | None = None) -> None:
        self.logger = logger or get_logger(__name__)

    def execute(
        self,
        action: str,
        arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        """Execute a system information action."""
        del arguments

        if action == "overview":
            return self._overview()
        if action == "current_time":
            return self._current_time()
        return ToolExecutionResult(False, f"Unsupported system action: {action}")

    def _overview(self) -> ToolExecutionResult:
        """Return OS, CPU, RAM, disk, and time information."""
        moment = _current_moment()
        disk = shutil.disk_usage(Path.home())
        total_ram = _get_total_ram_bytes()
        data = {
            "os": platform.platform(),
            "cpu_count": os.cpu_count(),
            "total_ram": _format_bytes(total_ram) if total_ram else "Unknown",
            "disk_total": _format_bytes(disk.total),
            "disk_used": _format_bytes(disk.used),
            "disk_free": _format_bytes(disk.free),
            "current_time": _iso_format(moment),
        }
        message = (
            f"OS: {data['os']}\n"
            f"CPU count: {data['cpu_count']}\n"
            f"RAM: {data['total_ram']}\n"
            f"Disk: {data['disk_used']} used / {data['disk_total']} total "
            f"({data['disk_free']} free)\n"
            f"Current time: {_compact_time(moment)}"
        )
        self.logger.info("System overview read.")
        return ToolExecutionResult(True, message, data)

    def _current_time(self) -> ToolExecutionResult:
        """Return a readable, timezone-aware current-time report."""
        moment = _current_moment()
        current_time = _iso_format(moment)
        self.logger.info("Current time read.")
        return ToolExecutionResult(
            True,
            _time_report(moment),
            {"current_time": current_time},
        )


class _MemoryStatusEx(ctypes.Structure):
    """Windows MEMORYSTATUSEX structure."""

    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _get_total_ram_bytes() -> int | None:
    """Return total physical RAM if it can be determined."""
    if platform.system().lower() == "windows":
        memory_status = _MemoryStatusEx()
        memory_status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        success = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory_status))
        if success:
            return int(memory_status.ullTotalPhys)
        return None

    if hasattr(os, "sysconf"):
        try:
            page_size = os.sysconf("SC_PAGE_SIZE")
            physical_pages = os.sysconf("SC_PHYS_PAGES")
            return int(page_size * physical_pages)
        except (AttributeError, OSError, ValueError):
            return None

    return None


def _format_bytes(value: int) -> str:
    """Format a byte count for display."""
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def _current_moment() -> datetime:
    """Capture the current local time as a single timezone-aware datetime."""
    return datetime.now().astimezone()


def _iso_format(moment: datetime) -> str:
    """Format a moment as a timezone-aware ISO-8601 string."""
    return moment.isoformat(timespec="seconds")


def _utc_offset(moment: datetime) -> str:
    """Format a moment's UTC offset as a signed, colonized string."""
    offset = moment.strftime("%z")
    return f"{offset[:3]}:{offset[3:]}"


def _time_report(moment: datetime) -> str:
    """Build the readable four-line current-time report for a moment."""
    return (
        f"Date: {moment.strftime('%Y-%m-%d')}\n"
        f"Time: {moment.strftime('%H:%M:%S')}\n"
        f"UTC offset: {_utc_offset(moment)}\n"
        f"ISO-8601: {_iso_format(moment)}"
    )


def _compact_time(moment: datetime) -> str:
    """Build the compact overview current-time line for a moment."""
    return f"{moment.strftime('%Y-%m-%d %H:%M:%S')} UTC{_utc_offset(moment)}"


def build_tool(**_: Any) -> SystemTool:
    """Build a system tool instance."""
    return SystemTool()
