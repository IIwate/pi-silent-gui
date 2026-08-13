# -*- coding: utf-8 -*-
"""Attach only the helper thread to a private desktop without switching user input."""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# DESKTOP_ALL = STANDARD_RIGHTS_REQUIRED | DESKTOP_CREATEMENU | DESKTOP_CREATEWINDOW | …
DESKTOP_ALL = 0x000F01FF
ERROR_INVALID_PARAMETER = 87
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = -3
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

user32.OpenDesktopW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.DWORD,
]
user32.OpenDesktopW.restype = wintypes.HANDLE
user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
user32.GetThreadDesktop.restype = wintypes.HANDLE
user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
user32.SetThreadDesktop.restype = wintypes.BOOL
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.CloseDesktop.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

_set_thread_dpi_awareness_context = getattr(user32, "SetThreadDpiAwarenessContext", None)
if _set_thread_dpi_awareness_context is not None:
    _set_thread_dpi_awareness_context.argtypes = [wintypes.HANDLE]
    _set_thread_dpi_awareness_context.restype = wintypes.HANDLE


@contextmanager
def operation_dpi_awareness():
    # Change DPI awareness only for the message/capture helper thread, never the target process.
    setter = _set_thread_dpi_awareness_context
    if setter is None:
        yield
        return
    old = None
    for context in (
        DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
        DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE,
    ):
        ctypes.set_last_error(0)
        old = setter(ctypes.c_void_p(context))
        if old:
            break
        if ctypes.get_last_error() != ERROR_INVALID_PARAMETER:
            raise OSError(
                f"SetThreadDpiAwarenessContext failed: {ctypes.get_last_error()}"
            )
    if not old:
        yield
        return
    try:
        yield
    finally:
        if not setter(old):
            raise OSError(
                f"restore thread DPI awareness failed: {ctypes.get_last_error()}"
            )


@contextmanager
def on_desktop(desktop_name: str):
    if not desktop_name:
        raise ValueError("desktop_name required to access private-desktop windows")
    hdesk = user32.OpenDesktopW(desktop_name, 0, False, DESKTOP_ALL)
    if not hdesk:
        raise OSError(f"OpenDesktop({desktop_name!r}) failed: {ctypes.get_last_error()}")
    tid = kernel32.GetCurrentThreadId()
    hold = user32.GetThreadDesktop(tid)
    if not user32.SetThreadDesktop(hdesk):
        err = ctypes.get_last_error()
        user32.CloseDesktop(hdesk)
        raise OSError(f"SetThreadDesktop failed: {err}")
    try:
        yield int(hdesk)
    finally:
        if hold:
            user32.SetThreadDesktop(hold)
        user32.CloseDesktop(hdesk)
