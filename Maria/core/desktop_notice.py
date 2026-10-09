"""The closed-app reminder pass's Windows message box (V1-23-R1).

This is the only module that calls a Windows notice function, through the
standard library's ``ctypes``. It asks Windows for the signed-in user's
notification state with ``SHQueryUserNotificationState``, and shows one
standard message box with ``MessageBoxW``: an OK button, the information
icon, and the MB_TOPMOST flag, and nothing more. It passes no foreground,
system-modal, or service-notification flag and calls no foreground or
clipboard function, so Windows alone decides whether the box is activated
or focused, and Maria copies nothing anywhere.

A message box is an operating-system notification primitive, not a GUI
toolkit. Nothing here imports Qt, starts a thread, or reads a clock; nothing
is logged, so the text shown never reaches a log. Tests never call the real
functions: they replace ``_library`` with fakes.
"""

from __future__ import annotations

import ctypes
import sys
from enum import Enum


# The message box's buttons, icon, and placement, and nothing else.
MB_OK = 0x00000000
MB_ICONINFORMATION = 0x00000040
MB_TOPMOST = 0x00040000
MESSAGE_BOX_STYLE = MB_OK | MB_ICONINFORMATION | MB_TOPMOST

# SHQueryUserNotificationState's answers. Only
# QUNS_ACCEPTS_NOTIFICATIONS lets the pass claim (decision D3-A); every other
# known state defers it.
QUNS_NOT_PRESENT = 1
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
QUNS_ACCEPTS_NOTIFICATIONS = 5
QUNS_QUIET_TIME = 6
QUNS_APP = 7
_DEFERRING_STATES = frozenset(
    {
        QUNS_NOT_PRESENT,
        QUNS_BUSY,
        QUNS_RUNNING_D3D_FULL_SCREEN,
        QUNS_PRESENTATION_MODE,
        QUNS_QUIET_TIME,
        QUNS_APP,
    }
)

# The success value an HRESULT has, read as an unsigned 32-bit number.
_S_OK = 0
_HRESULT_MASK = 0xFFFFFFFF

_USER32 = "user32"
_SHELL32 = "shell32"


class NotificationState(str, Enum):
    """What Windows says about showing a notification now.

    ``ACCEPTS`` is S_OK with QUNS_ACCEPTS_NOTIFICATIONS: notifications can
    be freely sent. ``DEFERS`` is S_OK with any other known state. ``UNKNOWN``
    is a failing HRESULT, a call that raised, or a value Maria does not
    know.
    """

    ACCEPTS = "accepts"
    DEFERS = "defers"
    UNKNOWN = "unknown"


def available() -> bool:
    """Return whether this computer offers both notice functions.

    That needs Windows, and both ``user32``'s ``MessageBoxW`` and
    ``shell32``'s ``SHQueryUserNotificationState``. Anything missing, or a
    library that cannot be loaded, means notices are unavailable.
    """
    if sys.platform != "win32":
        return False
    try:
        _library(_USER32).MessageBoxW
        _library(_SHELL32).SHQueryUserNotificationState
    except (OSError, AttributeError):
        return False
    return True


def notification_state() -> NotificationState:
    """Ask Windows once whether a notification may be shown now.

    A failing HRESULT, a raised call, and a state number Maria does not know
    all read as ``UNKNOWN``, never as permission.
    """
    try:
        query = _library(_SHELL32).SHQueryUserNotificationState
        query.restype = ctypes.c_long
        query.argtypes = [ctypes.POINTER(ctypes.c_int)]
        state = ctypes.c_int(0)
        result = query(ctypes.byref(state))
        hresult = int(result) & _HRESULT_MASK
        value = int(state.value)
    except Exception:  # noqa: BLE001 - an unreadable state defers, never shows.
        return NotificationState.UNKNOWN
    if hresult != _S_OK:
        return NotificationState.UNKNOWN
    if value == QUNS_ACCEPTS_NOTIFICATIONS:
        return NotificationState.ACCEPTS
    if value in _DEFERRING_STATES:
        return NotificationState.DEFERS
    return NotificationState.UNKNOWN


def show(title: str, text: str) -> bool:
    """Show one message box and wait until it is closed.

    Returns True when the box returned a button, which means it was shown
    and then closed by any means, and False when ``MessageBoxW`` returned 0,
    its failure value. A raised call propagates to the caller.
    """
    box = _library(_USER32).MessageBoxW
    box.restype = ctypes.c_int
    box.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
    ]
    return int(box(None, text, title, MESSAGE_BOX_STYLE)) != 0


def _library(name: str) -> ctypes.WinDLL:
    """Load one Windows library by name; tests replace this with fakes."""
    return ctypes.WinDLL(name)
