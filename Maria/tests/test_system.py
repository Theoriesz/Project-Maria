"""Focused tests for Maria's read-only system information tool."""

from __future__ import annotations

import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import tools.system as system_module
from core.tool_manager import ToolExecutionResult
from tools.system import SystemTool, build_tool


class SystemToolTests(unittest.TestCase):
    """Verify deterministic system reporting without reading the live host."""

    def setUp(self) -> None:
        self.logger = Mock(spec=logging.Logger)
        self.tool = SystemTool(logger=self.logger)

    def test_public_contract_and_factory(self) -> None:
        factory_logger = Mock(spec=logging.Logger)

        with patch.object(
            system_module,
            "get_logger",
            return_value=factory_logger,
        ) as get_logger:
            built = build_tool(
                config=object(),
                memory=object(),
                logger=object(),
            )

        self.assertEqual(built.name, "system")
        self.assertEqual(
            built.description,
            "Reports read-only system information.",
        )
        self.assertEqual(built.actions, frozenset({"overview", "current_time"}))
        self.assertIs(built.logger, factory_logger)
        get_logger.assert_called_once_with("tools.system")

    def test_overview_includes_all_mocked_system_values(self) -> None:
        disk = SimpleNamespace(
            total=100 * 1024**3,
            used=40 * 1024**3,
            free=60 * 1024**3,
        )
        home = Path("mocked-home")
        timestamp = "2030-01-02T03:04:05+08:00"

        with (
            patch.object(system_module.platform, "platform", return_value="MockOS-1")
            as platform_name,
            patch.object(system_module.os, "cpu_count", return_value=12) as cpu_count,
            patch.object(system_module.Path, "home", return_value=home) as path_home,
            patch.object(system_module.shutil, "disk_usage", return_value=disk)
            as disk_usage,
            patch.object(
                system_module,
                "_get_total_ram_bytes",
                return_value=8 * 1024**3,
            ) as total_ram,
            patch.object(system_module, "_local_time", return_value=timestamp)
            as local_time,
        ):
            result = self.tool.execute("overview", {})

        expected_data = {
            "os": "MockOS-1",
            "cpu_count": 12,
            "total_ram": "8.00 GB",
            "disk_total": "100.00 GB",
            "disk_used": "40.00 GB",
            "disk_free": "60.00 GB",
            "current_time": timestamp,
        }
        expected_message = (
            "OS: MockOS-1\n"
            "CPU count: 12\n"
            "RAM: 8.00 GB\n"
            "Disk: 40.00 GB used / 100.00 GB total (60.00 GB free)\n"
            f"Current time: {timestamp}"
        )
        self.assertEqual(
            result,
            ToolExecutionResult(True, expected_message, expected_data),
        )
        platform_name.assert_called_once_with()
        cpu_count.assert_called_once_with()
        path_home.assert_called_once_with()
        disk_usage.assert_called_once_with(home)
        total_ram.assert_called_once_with()
        local_time.assert_called_once_with()
        self.logger.info.assert_called_once_with("System overview read.")

    def test_current_time_returns_mocked_timezone_aware_timestamp(self) -> None:
        timestamp = "2040-05-06T07:08:09-04:00"

        with patch.object(system_module, "_local_time", return_value=timestamp):
            result = self.tool.execute("current_time", {})

        self.assertEqual(
            result,
            ToolExecutionResult(
                True,
                timestamp,
                {"current_time": timestamp},
            ),
        )
        self.logger.info.assert_called_once_with("Current time read.")

    def test_unknown_action_is_rejected(self) -> None:
        result = self.tool.execute("write", {"value": "ignored"})

        self.assertEqual(
            result,
            ToolExecutionResult(False, "Unsupported system action: write"),
        )
        self.logger.info.assert_not_called()

    def test_byte_formatting_at_unit_boundaries(self) -> None:
        cases = (
            (0, "0.00 B"),
            (1023, "1023.00 B"),
            (1024, "1.00 KB"),
            (1024**2, "1.00 MB"),
            (1024**3, "1.00 GB"),
            (1024**4, "1.00 TB"),
            (1024**5, "1024.00 TB"),
        )

        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(system_module._format_bytes(value), expected)

    def test_unknown_ram_is_rendered_as_unknown(self) -> None:
        disk = SimpleNamespace(total=4096, used=2048, free=2048)

        with (
            patch.object(system_module.platform, "platform", return_value="MockOS"),
            patch.object(system_module.os, "cpu_count", return_value=4),
            patch.object(system_module.Path, "home", return_value=Path("mock-home")),
            patch.object(system_module.shutil, "disk_usage", return_value=disk),
            patch.object(system_module, "_get_total_ram_bytes", return_value=None),
            patch.object(
                system_module,
                "_local_time",
                return_value="2050-01-01T00:00:00+00:00",
            ),
        ):
            result = self.tool.execute("overview", {})

        self.assertTrue(result.success)
        self.assertEqual(result.data["total_ram"], "Unknown")
        self.assertIn("RAM: Unknown", result.message)

    def test_posix_ram_uses_mocked_page_size_and_page_count(self) -> None:
        values = {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 250_000}

        with (
            patch.object(system_module.platform, "system", return_value="Linux"),
            patch.object(
                system_module.os,
                "sysconf",
                side_effect=values.__getitem__,
                create=True,
            ) as sysconf,
        ):
            result = system_module._get_total_ram_bytes()

        self.assertEqual(result, 1_024_000_000)
        self.assertEqual(
            sysconf.call_args_list,
            [call("SC_PAGE_SIZE"), call("SC_PHYS_PAGES")],
        )

    def test_missing_posix_sysconf_returns_unknown(self) -> None:
        original_hasattr = hasattr

        def sysconf_is_missing(value: object, name: str) -> bool:
            if value is system_module.os and name == "sysconf":
                return False
            return original_hasattr(value, name)

        with (
            patch.object(system_module.platform, "system", return_value="Linux"),
            patch.object(
                system_module,
                "hasattr",
                side_effect=sysconf_is_missing,
                create=True,
            ),
        ):
            self.assertIsNone(system_module._get_total_ram_bytes())

    def test_failing_posix_sysconf_returns_unknown(self) -> None:
        for error in (
            AttributeError("unavailable"),
            OSError("unavailable"),
            ValueError("unavailable"),
        ):
            with self.subTest(error=type(error).__name__):
                with (
                    patch.object(
                        system_module.platform,
                        "system",
                        return_value="Linux",
                    ),
                    patch.object(
                        system_module.os,
                        "sysconf",
                        side_effect=error,
                        create=True,
                    ),
                ):
                    self.assertIsNone(system_module._get_total_ram_bytes())

    def test_windows_ram_uses_mocked_operating_system_boundary(self) -> None:
        status = SimpleNamespace(dwLength=0, ullTotalPhys=0)
        kernel32 = SimpleNamespace()
        global_memory_status = Mock()
        memory_status_factory = Mock(return_value=status)

        def populate_total_ram(received_status: object) -> int:
            self.assertIs(received_status, status)
            status.ullTotalPhys = 16 * 1024**3
            return 1

        global_memory_status.side_effect = populate_total_ram
        kernel32.GlobalMemoryStatusEx = global_memory_status
        windll = SimpleNamespace(kernel32=kernel32)

        with (
            patch.object(system_module.platform, "system", return_value="Windows"),
            patch.object(system_module, "_MemoryStatusEx", memory_status_factory),
            patch.object(system_module.ctypes, "sizeof", return_value=72) as sizeof,
            patch.object(system_module.ctypes, "byref", return_value=status) as byref,
            patch.object(system_module.ctypes, "windll", windll, create=True),
        ):
            result = system_module._get_total_ram_bytes()

        self.assertEqual(result, 16 * 1024**3)
        self.assertEqual(status.dwLength, 72)
        sizeof.assert_called_once_with(memory_status_factory)
        byref.assert_called_once_with(status)
        global_memory_status.assert_called_once_with(status)

    def test_windows_ram_failure_returns_unknown(self) -> None:
        status = SimpleNamespace(dwLength=0, ullTotalPhys=64 * 1024**3)
        global_memory_status = Mock(return_value=0)
        windll = SimpleNamespace(
            kernel32=SimpleNamespace(GlobalMemoryStatusEx=global_memory_status)
        )

        with (
            patch.object(system_module.platform, "system", return_value="Windows"),
            patch.object(system_module, "_MemoryStatusEx", return_value=status),
            patch.object(system_module.ctypes, "sizeof", return_value=72),
            patch.object(system_module.ctypes, "byref", return_value=status),
            patch.object(system_module.ctypes, "windll", windll, create=True),
        ):
            self.assertIsNone(system_module._get_total_ram_bytes())

    def test_actions_are_read_only_and_arguments_are_not_mutated(self) -> None:
        arguments = {"host": "must remain untouched"}
        original_arguments = arguments.copy()

        with patch.object(
            system_module,
            "_local_time",
            return_value="2030-01-02T03:04:05+08:00",
        ):
            result = self.tool.execute("current_time", arguments)

        self.assertTrue(result.success)
        self.assertEqual(arguments, original_arguments)
        self.assertTrue(self.tool.actions.isdisjoint({"write", "delete", "set"}))

    def test_logging_does_not_add_sensitive_host_details(self) -> None:
        sensitive_host = "distinctive-sensitive-host-9f4e"
        disk = SimpleNamespace(total=8192, used=4096, free=4096)

        with (
            patch.object(
                system_module.platform,
                "platform",
                return_value=sensitive_host,
            ),
            patch.object(system_module.os, "cpu_count", return_value=99),
            patch.object(system_module.Path, "home", return_value=Path("private-home")),
            patch.object(system_module.shutil, "disk_usage", return_value=disk),
            patch.object(system_module, "_get_total_ram_bytes", return_value=123456),
            patch.object(
                system_module,
                "_local_time",
                return_value="2060-01-01T00:00:00+00:00",
            ),
        ):
            result = self.tool.execute("overview", {})

        self.assertIn(sensitive_host, result.message)
        self.assertEqual(
            self.logger.info.call_args_list,
            [call("System overview read.")],
        )
        self.assertNotIn(sensitive_host, str(self.logger.method_calls))
        self.assertNotIn("private-home", str(self.logger.method_calls))


if __name__ == "__main__":
    unittest.main()
