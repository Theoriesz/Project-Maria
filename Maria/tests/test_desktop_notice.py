"""Focused tests for the closed-app pass's Windows message box (V1-23-R1).

Every test replaces ``desktop_notice._library`` with fakes, so no real
Windows function is called and no box is ever shown.
"""

from __future__ import annotations

import ast
import ctypes
import inspect
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager
from types import SimpleNamespace
from unittest.mock import patch

from core import desktop_notice
from core.desktop_notice import (
    MB_ICONINFORMATION,
    MB_OK,
    MB_TOPMOST,
    MESSAGE_BOX_STYLE,
    NotificationState,
)

S_OK = 0
S_FALSE = 1
E_FAIL = -2147467259  # 0x80004005 as a signed HRESULT.
IDOK = 1


class FakeFunction:
    """A foreign function double: records each call and its declared types."""

    def __init__(self, behaviour: Callable[..., object]) -> None:
        self.behaviour = behaviour
        self.calls: list[tuple[object, ...]] = []
        self.restype: object = None
        self.argtypes: object = None

    def __call__(self, *arguments: object) -> object:
        self.calls.append(arguments)
        return self.behaviour(*arguments)


class FakeLibrary:
    """A loaded library double offering exactly the named functions."""

    def __init__(self, **functions: FakeFunction) -> None:
        self.functions = functions
        self.looked_up: list[str] = []

    def __getattr__(self, name: str) -> FakeFunction:
        self.looked_up.append(name)
        try:
            return self.functions[name]
        except KeyError:
            raise AttributeError(name) from None


class FakeWindows:
    """Both libraries, and a record of which ones were loaded."""

    def __init__(
        self,
        *,
        state: int = 5,
        hresult: int = S_OK,
        box_result: int = IDOK,
    ) -> None:
        self.loaded: list[str] = []
        self.query = FakeFunction(self._answer)
        self.box = FakeFunction(lambda *_arguments: box_result)
        self.state = state
        self.hresult = hresult
        self.libraries = {
            "user32": FakeLibrary(MessageBoxW=self.box),
            "shell32": FakeLibrary(SHQueryUserNotificationState=self.query),
        }

    def _answer(self, reference: object) -> int:
        """Write the state through the pointer, as Windows does, and answer."""
        reference._obj.value = self.state
        return self.hresult

    def library(self, name: str) -> FakeLibrary:
        """Stand in for ``ctypes.WinDLL``."""
        self.loaded.append(name)
        return self.libraries[name]


def _on_windows() -> AbstractContextManager[object]:
    """Patch the platform check, without touching the real ``sys`` module."""
    return patch.object(desktop_notice, "sys", SimpleNamespace(platform="win32"))


class NotificationStateTests(unittest.TestCase):
    """Pin decision D3-A: only S_OK with QUNS_ACCEPTS_NOTIFICATIONS shows."""

    def state_for(self, windows: FakeWindows) -> NotificationState:
        with patch.object(desktop_notice, "_library", windows.library):
            return desktop_notice.notification_state()

    def test_only_accepting_notifications_lets_a_pass_show(self) -> None:
        expected = {
            1: NotificationState.DEFERS,
            2: NotificationState.DEFERS,
            3: NotificationState.DEFERS,
            4: NotificationState.DEFERS,
            5: NotificationState.ACCEPTS,
            6: NotificationState.DEFERS,
            7: NotificationState.DEFERS,
        }
        for value, state in expected.items():
            with self.subTest(value=value):
                windows = FakeWindows(state=value)

                self.assertIs(self.state_for(windows), state)
                self.assertEqual(len(windows.query.calls), 1)

    def test_quiet_time_and_store_apps_defer_without_exception(self) -> None:
        for value in (
            desktop_notice.QUNS_QUIET_TIME,
            desktop_notice.QUNS_APP,
        ):
            with self.subTest(value=value):
                self.assertIs(
                    self.state_for(FakeWindows(state=value)),
                    NotificationState.DEFERS,
                )

    def test_a_value_maria_does_not_know_is_unknown(self) -> None:
        for value in (0, 8, 9, -1, 2**31 - 1):
            with self.subTest(value=value):
                self.assertIs(
                    self.state_for(FakeWindows(state=value)),
                    NotificationState.UNKNOWN,
                )

    def test_any_hresult_but_s_ok_is_unknown_even_when_accepting(self) -> None:
        for hresult in (S_FALSE, E_FAIL, 0x80070005, -1):
            with self.subTest(hresult=hresult):
                self.assertIs(
                    self.state_for(FakeWindows(state=5, hresult=hresult)),
                    NotificationState.UNKNOWN,
                )

    def test_a_raising_call_or_library_is_unknown(self) -> None:
        for error in (OSError("no shell"), RuntimeError("boom"), ValueError()):
            with self.subTest(error=type(error).__name__):
                windows = FakeWindows()

                def raising(_reference: object, error: Exception = error) -> int:
                    raise error

                windows.query.behaviour = raising
                self.assertIs(self.state_for(windows), NotificationState.UNKNOWN)

                def unloadable(_name: str, error: Exception = error) -> object:
                    raise error

                with patch.object(desktop_notice, "_library", unloadable):
                    self.assertIs(
                        desktop_notice.notification_state(),
                        NotificationState.UNKNOWN,
                    )

    def test_the_query_is_declared_and_loads_only_shell32(self) -> None:
        windows = FakeWindows()

        self.state_for(windows)

        self.assertEqual(windows.loaded, ["shell32"])
        self.assertEqual(
            windows.libraries["shell32"].looked_up,
            ["SHQueryUserNotificationState"],
        )
        self.assertIs(windows.query.restype, ctypes.c_long)
        self.assertEqual(windows.query.argtypes, [ctypes.POINTER(ctypes.c_int)])
        self.assertEqual(windows.box.calls, [])


class MessageBoxTests(unittest.TestCase):
    """Pin the one message box: its call, its flags, and its results."""

    def test_the_style_is_exactly_ok_information_and_topmost(self) -> None:
        self.assertEqual(MB_OK, 0x00000000)
        self.assertEqual(MB_ICONINFORMATION, 0x00000040)
        self.assertEqual(MB_TOPMOST, 0x00040000)
        self.assertEqual(MESSAGE_BOX_STYLE, 0x00040040)

    def test_show_calls_message_box_once_with_no_owner_and_that_style(
        self,
    ) -> None:
        windows = FakeWindows()

        with patch.object(desktop_notice, "_library", windows.library):
            shown = desktop_notice.show("Maria reminder", "Reminder 1 is due.")

        self.assertIs(shown, True)
        self.assertEqual(
            windows.box.calls,
            [(None, "Reminder 1 is due.", "Maria reminder", MESSAGE_BOX_STYLE)],
        )
        self.assertEqual(windows.loaded, ["user32"])
        self.assertEqual(windows.libraries["user32"].looked_up, ["MessageBoxW"])
        self.assertIs(windows.box.restype, ctypes.c_int)
        self.assertEqual(
            windows.box.argtypes,
            [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint],
        )
        self.assertEqual(windows.query.calls, [])

    def test_any_button_counts_as_shown_and_zero_as_not_shown(self) -> None:
        for result, shown in ((IDOK, True), (2, True), (0, False)):
            with self.subTest(result=result):
                windows = FakeWindows(box_result=result)

                with patch.object(desktop_notice, "_library", windows.library):
                    self.assertIs(desktop_notice.show("t", "x"), shown)

    def test_a_raising_box_propagates(self) -> None:
        windows = FakeWindows()

        def raising(*_arguments: object) -> int:
            raise OSError("no desktop")

        windows.box.behaviour = raising
        with patch.object(desktop_notice, "_library", windows.library):
            with self.assertRaises(OSError):
                desktop_notice.show("t", "x")


class AvailabilityTests(unittest.TestCase):
    """Notices are available only on Windows with both functions present."""

    def test_both_functions_on_windows_are_available(self) -> None:
        windows = FakeWindows()

        with _on_windows(), patch.object(
            desktop_notice,
            "_library",
            windows.library,
        ):
            self.assertIs(desktop_notice.available(), True)

        self.assertEqual(windows.box.calls, [])
        self.assertEqual(windows.query.calls, [])

    def test_another_platform_is_unavailable_without_loading_anything(
        self,
    ) -> None:
        windows = FakeWindows()

        with patch.object(
            desktop_notice,
            "sys",
            SimpleNamespace(platform="linux"),
        ), patch.object(desktop_notice, "_library", windows.library):
            self.assertIs(desktop_notice.available(), False)

        self.assertEqual(windows.loaded, [])

    def test_a_missing_function_or_library_is_unavailable(self) -> None:
        for missing in ("user32", "shell32"):
            with self.subTest(missing=missing):
                windows = FakeWindows()
                windows.libraries[missing].functions.clear()

                with _on_windows(), patch.object(
                    desktop_notice,
                    "_library",
                    windows.library,
                ):
                    self.assertIs(desktop_notice.available(), False)

        def unloadable(_name: str) -> object:
            raise OSError("cannot load")

        with _on_windows(), patch.object(desktop_notice, "_library", unloadable):
            self.assertIs(desktop_notice.available(), False)


class NoticeModuleBoundaryTests(unittest.TestCase):
    """Pin what the notice module may import, call, and name."""

    def setUp(self) -> None:
        self.tree = ast.parse(inspect.getsource(desktop_notice))

    def test_it_imports_only_ctypes_sys_and_enum(self) -> None:
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)

        self.assertEqual(imported, {"__future__", "ctypes", "sys", "enum"})

    def test_it_names_no_foreground_clipboard_or_extra_box_flag(self) -> None:
        names = {
            node.attr
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Attribute)
        } | {node.id for node in ast.walk(self.tree) if isinstance(node, ast.Name)}

        self.assertLessEqual(
            {"MessageBoxW", "SHQueryUserNotificationState"},
            names,
        )
        for forbidden in (
            "SetForegroundWindow",
            "AllowSetForegroundWindow",
            "BringWindowToTop",
            "OpenClipboard",
            "SetClipboardData",
            "EmptyClipboard",
            "MB_SETFOREGROUND",
            "MB_SYSTEMMODAL",
            "MB_SERVICE_NOTIFICATION",
            "MB_DEFAULT_DESKTOP_ONLY",
            "MessageBoxA",
            "MessageBoxExW",
            "MessageBoxTimeoutW",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, names)

    def test_only_the_two_notice_functions_are_ever_taken_from_windows(
        self,
    ) -> None:
        library_calls = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_library"
        ]
        taken = [
            (ast.unparse(node.value.args[0]), node.attr)
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Attribute)
            and any(node.value is call for call in library_calls)
        ]
        ctypes_names = {
            node.attr
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "ctypes"
        }

        # Every library is read at once, by one of the two names, so no other
        # function can be taken from one through a variable either.
        self.assertEqual(len(taken), len(library_calls))
        self.assertEqual(
            set(taken),
            {
                ("_USER32", "MessageBoxW"),
                ("_SHELL32", "SHQueryUserNotificationState"),
            },
        )
        self.assertEqual(
            (desktop_notice._USER32, desktop_notice._SHELL32),
            ("user32", "shell32"),
        )
        # ctypes loads libraries only inside _library, and otherwise names
        # only the two functions' argument and result types.
        self.assertEqual(
            ctypes_names,
            {
                "WinDLL",
                "POINTER",
                "byref",
                "c_int",
                "c_long",
                "c_uint",
                "c_void_p",
                "c_wchar_p",
            },
        )


if __name__ == "__main__":
    unittest.main()
