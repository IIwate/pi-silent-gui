# -*- coding: utf-8 -*-
"""Process identity and safe termination; target trees are owned by Job Objects."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20
STILL_ACTIVE = 259
WAIT_OBJECT_0 = 0
ERROR_INVALID_PARAMETER = 87

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
]
kernel32.GetProcessTimes.restype = wintypes.BOOL
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateProcess.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
advapi32.GetTokenInformation.restype = wintypes.BOOL


def _open(pid: int, access: int) -> int | None:
    if pid <= 0:
        return None
    ctypes.set_last_error(0)
    handle = kernel32.OpenProcess(access, False, pid)
    return int(handle) if handle else None


def _exit_code(handle: int) -> int | None:
    code = wintypes.DWORD()
    if not kernel32.GetExitCodeProcess(wintypes.HANDLE(handle), ctypes.byref(code)):
        return None
    return int(code.value)


def is_alive(pid: int) -> bool:
    handle = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE)
    if handle is None:
        return False
    try:
        return _exit_code(handle) == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def process_exit_code(pid: int, created: int | None = None) -> int | None:
    """Return the spawn exe exit code, or None if it is still running or unverifiable."""
    handle = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE)
    if handle is None:
        return None
    try:
        if created is not None:
            actual = process_creation_time_from_handle(handle)
            if actual is None or actual != int(created):
                return None
        code = _exit_code(handle)
        if code is None or code == STILL_ACTIVE:
            return None
        return code
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def process_creation_time_from_handle(handle: int) -> int | None:
    created = wintypes.FILETIME()
    exited = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    if not kernel32.GetProcessTimes(
        wintypes.HANDLE(handle),
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        return None
    return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)


def process_creation_time(pid: int) -> int | None:
    handle = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if handle is None:
        return None
    try:
        return process_creation_time_from_handle(handle)
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def process_is_elevated_from_handle(process: int) -> bool | None:
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(
            wintypes.HANDLE(process), TOKEN_QUERY, ctypes.byref(token)
        ):
            return None
        elevated = wintypes.DWORD()
        returned = wintypes.DWORD()
        if not advapi32.GetTokenInformation(
            token,
            TOKEN_ELEVATION,
            ctypes.byref(elevated),
            ctypes.sizeof(elevated),
            ctypes.byref(returned),
        ):
            return None
        return bool(elevated.value)
    finally:
        if token:
            kernel32.CloseHandle(token)


def process_is_elevated(pid: int) -> bool | None:
    process = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if process is None:
        return None
    try:
        return process_is_elevated_from_handle(process)
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(process))


def open_process_watch(pid: int, created: int) -> int | None:
    """Open a long-lived handle bound to one exact live process identity.

    Request only synchronization and limited query rights. The owner is observed, never
    terminated, and the held handle prevents PID reuse from impersonating that identity.
    Exited processes are rejected even when external handles keep their metadata readable.
    """
    handle = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE)
    if handle is None:
        return None
    actual = process_creation_time_from_handle(handle)
    if actual is None or actual != int(created) or _exit_code(handle) != STILL_ACTIVE:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        return None
    return handle


def process_handle_alive(handle: int) -> bool:
    """Check liveness through the held identity handle without resolving the PID again."""
    return _exit_code(handle) == STILL_ACTIVE


def close_handle(handle: int | None) -> None:
    if handle:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def process_identity_status(pid: int, created: int) -> str:
    """Return live, gone, mismatch, or unverifiable for one recorded process identity."""
    handle = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE)
    if handle is None:
        return "gone" if ctypes.get_last_error() == ERROR_INVALID_PARAMETER else "unverifiable"
    try:
        actual = process_creation_time_from_handle(handle)
        if actual is None:
            return "unverifiable"
        if actual != int(created):
            return "mismatch"
        exit_code = _exit_code(handle)
        if exit_code is None:
            return "unverifiable"
        return "live" if exit_code == STILL_ACTIVE else "gone"
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def same_process(pid: int, created: int) -> bool:
    """Verify creation time and liveness through one process handle."""
    return process_identity_status(pid, created) == "live"


def terminate_same_process(pid: int, created: int, timeout_s: float = 2.0) -> str:
    """Verify, terminate, and wait through one handle; return killed/gone/mismatch/failed."""
    access = PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    handle = _open(pid, access)
    if handle is None:
        return "gone" if ctypes.get_last_error() == ERROR_INVALID_PARAMETER else "failed"
    try:
        actual = process_creation_time_from_handle(handle)
        if actual is None:
            return "failed"
        if actual != int(created):
            return "mismatch"
        if _exit_code(handle) != STILL_ACTIVE:
            return "gone"
        if not kernel32.TerminateProcess(wintypes.HANDLE(handle), 1):
            return "failed"
        waited = kernel32.WaitForSingleObject(
            wintypes.HANDLE(handle), max(0, min(0xFFFFFFFF, int(timeout_s * 1000)))
        )
        if waited != WAIT_OBJECT_0:
            return "failed"
        return "killed" if _exit_code(handle) != STILL_ACTIVE else "failed"
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
