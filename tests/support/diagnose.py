# -*- coding: utf-8 -*-
"""Read-only diagnostic snapshot for a window process that did not exit.

A timeout with only one PID cannot distinguish a missed wakeup from work outside the
modal loop or blocked window destruction. Two thread-state samples provide that evidence:

| Signature | Interpretation |
|---|---|
| Stable `WrUserRequest` and context-switch count | Waiting for a window message that never arrived |
| Other wait reason or increasing context switches | Running or blocked outside the modal loop |
| Active thread while the window still exists | Window destruction failed or remains blocked |
"""
from __future__ import annotations

import ctypes
import subprocess
import time
from ctypes import wintypes

import window as window_module
from desktop_ctx import on_desktop

ntdll = ctypes.WinDLL("ntdll")
user32 = window_module.user32

SYSTEM_PROCESS_INFORMATION_CLASS = 5
DESKTOP_ENUMERATE = 0x0040
DESKTOP_READOBJECTS = 0x0001
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_VISIBLE = 0x10000000
WS_DISABLED = 0x08000000
SAMPLE_GAP_SECONDS = 0.4
COM_HEALTH_TIMEOUT_SECONDS = 20
COM_HEALTH_WINDOW_MINUTES = 30

# ntdll KWAIT_REASON; WrUserRequest identifies a thread waiting for window messages.
WAIT_REASONS = {
    0: "Executive",
    1: "FreePage",
    2: "PageIn",
    3: "PoolAllocation",
    4: "DelayExecution",
    5: "Suspended",
    6: "UserRequest",
    7: "WrExecutive",
    8: "WrFreePage",
    9: "WrPageIn",
    10: "WrPoolAllocation",
    11: "WrDelayExecution",
    12: "WrSuspended",
    13: "WrUserRequest",
    14: "WrEventPair",
    15: "WrQueue",
    16: "WrLpcReceive",
    17: "WrLpcReply",
    18: "WrVirtualMemory",
    19: "WrPageOut",
    20: "WrRendezvous",
    21: "WrKeyedEvent",
    22: "WrTerminated",
    23: "WrProcessInSwap",
    24: "WrCpuRateControl",
    25: "WrCalloutStack",
    26: "WrKernel",
    27: "WrResource",
    28: "WrPushLock",
    29: "WrMutex",
    30: "WrQuantumEnd",
    31: "WrDispatchInt",
    32: "WrPreempted",
    33: "WrYieldExecution",
    34: "WrFastMutex",
    35: "WrGuardedMutex",
    36: "WrRundown",
    37: "WrAlertByThreadId",
    38: "WrDeferredPreempt",
}

THREAD_STATES = {
    0: "Initialized",
    1: "Ready",
    2: "Running",
    3: "Standby",
    4: "Terminated",
    5: "Waiting",
    6: "Transition",
    7: "DeferredReady",
}


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class CLIENT_ID(ctypes.Structure):
    _fields_ = [("UniqueProcess", wintypes.HANDLE), ("UniqueThread", wintypes.HANDLE)]


class SYSTEM_THREAD_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("KernelTime", ctypes.c_int64),
        ("UserTime", ctypes.c_int64),
        ("CreateTime", ctypes.c_int64),
        ("WaitTime", wintypes.ULONG),
        ("StartAddress", ctypes.c_void_p),
        ("ClientId", CLIENT_ID),
        ("Priority", ctypes.c_long),
        ("BasePriority", ctypes.c_long),
        ("ContextSwitches", wintypes.ULONG),
        ("ThreadState", wintypes.ULONG),
        ("WaitReason", wintypes.ULONG),
    ]


class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", wintypes.ULONG),
        ("NumberOfThreads", wintypes.ULONG),
        ("WorkingSetPrivateSize", ctypes.c_int64),
        ("HardFaultCount", wintypes.ULONG),
        ("NumberOfThreadsHighWatermark", wintypes.ULONG),
        ("CycleTime", ctypes.c_uint64),
        ("CreateTime", ctypes.c_int64),
        ("UserTime", ctypes.c_int64),
        ("KernelTime", ctypes.c_int64),
        ("ImageName", UNICODE_STRING),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", wintypes.HANDLE),
        ("InheritedFromUniqueProcessId", wintypes.HANDLE),
        ("HandleCount", wintypes.ULONG),
        ("SessionId", wintypes.ULONG),
        ("UniqueProcessKey", ctypes.c_void_p),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", wintypes.ULONG),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivatePageCount", ctypes.c_size_t),
        ("ReadOperationCount", ctypes.c_int64),
        ("WriteOperationCount", ctypes.c_int64),
        ("OtherOperationCount", ctypes.c_int64),
        ("ReadTransferCount", ctypes.c_int64),
        ("WriteTransferCount", ctypes.c_int64),
        ("OtherTransferCount", ctypes.c_int64),
    ]


ENUMDESKTOPSPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.LPWSTR, wintypes.LPARAM)

ntdll.NtQuerySystemInformation.argtypes = [
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.ULONG,
    ctypes.POINTER(wintypes.ULONG),
]
ntdll.NtQuerySystemInformation.restype = ctypes.c_long
user32.GetProcessWindowStation.argtypes = []
user32.GetProcessWindowStation.restype = wintypes.HANDLE
user32.EnumDesktopsW.argtypes = [wintypes.HANDLE, ENUMDESKTOPSPROC, wintypes.LPARAM]
user32.EnumDesktopsW.restype = wintypes.BOOL
user32.EnumDesktopWindows.argtypes = [wintypes.HANDLE, window_module.WNDENUMPROC, wintypes.LPARAM]
user32.EnumDesktopWindows.restype = wintypes.BOOL
user32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenDesktopW.restype = wintypes.HANDLE
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.CloseDesktop.restype = wintypes.BOOL
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t

_is_hung_app_window = getattr(user32, "IsHungAppWindow", None)
if _is_hung_app_window is not None:
    _is_hung_app_window.argtypes = [wintypes.HWND]
    _is_hung_app_window.restype = wintypes.BOOL


def _guarded(section: str, produce):
    """Record section errors without hiding the original smoke-test failure."""
    try:
        return produce()
    except Exception as error:  # noqa: BLE001 - diagnostics must never replace the test failure
        return {"error": f"{section}: {type(error).__name__}: {error}"}


def _process_threads(wanted: set[int]) -> dict[int, list[dict]]:
    size = wintypes.ULONG(0)
    ntdll.NtQuerySystemInformation(
        SYSTEM_PROCESS_INFORMATION_CLASS, None, 0, ctypes.byref(size)
    )
    if not size.value:
        raise OSError("NtQuerySystemInformation(size) returned no size")
    # Leave headroom for process-table growth between the size query and snapshot.
    buffer = ctypes.create_string_buffer(size.value + 262144)
    status = ntdll.NtQuerySystemInformation(
        SYSTEM_PROCESS_INFORMATION_CLASS, buffer, len(buffer), ctypes.byref(size)
    )
    if status < 0:
        raise OSError(f"NtQuerySystemInformation failed: 0x{status & 0xFFFFFFFF:08x}")

    found: dict[int, list[dict]] = {}
    offset = 0
    entry_size = ctypes.sizeof(SYSTEM_PROCESS_INFORMATION)
    thread_size = ctypes.sizeof(SYSTEM_THREAD_INFORMATION)
    while True:
        entry = SYSTEM_PROCESS_INFORMATION.from_buffer(buffer, offset)
        pid = int(entry.UniqueProcessId or 0)
        if pid in wanted:
            threads = []
            base = offset + entry_size
            for index in range(entry.NumberOfThreads):
                thread = SYSTEM_THREAD_INFORMATION.from_buffer(
                    buffer, base + index * thread_size
                )
                threads.append(
                    {
                        "tid": int(thread.ClientId.UniqueThread or 0),
                        "state": int(thread.ThreadState),
                        "state_name": THREAD_STATES.get(int(thread.ThreadState), "?"),
                        "wait_reason": int(thread.WaitReason),
                        "wait_reason_name": WAIT_REASONS.get(int(thread.WaitReason), "?"),
                        "ctxsw": int(thread.ContextSwitches),
                        "kernel_time": int(thread.KernelTime),
                        "user_time": int(thread.UserTime),
                    }
                )
            found[pid] = threads
        if not entry.NextEntryOffset:
            break
        offset += entry.NextEntryOffset
    return found


def _thread_activity(pids: list[int]) -> dict:
    """Use two samples to distinguish a frozen wait from active work."""
    wanted = {int(pid) for pid in pids}
    first = _process_threads(wanted)
    first_at = time.monotonic()
    time.sleep(SAMPLE_GAP_SECONDS)
    second = _process_threads(wanted)
    gap = time.monotonic() - first_at

    report: dict = {"gap_seconds": round(gap, 3), "processes": {}}
    for pid in sorted(wanted):
        before = {thread["tid"]: thread for thread in first.get(pid, [])}
        after = second.get(pid)
        if after is None:
            report["processes"][str(pid)] = {"present": False}
            continue
        threads = []
        for thread in after:
            previous = before.get(thread["tid"])
            threads.append(
                {
                    **thread,
                    "ctxsw_delta": (
                        thread["ctxsw"] - previous["ctxsw"] if previous else None
                    ),
                    "cpu_delta": (
                        (thread["kernel_time"] - previous["kernel_time"])
                        + (thread["user_time"] - previous["user_time"])
                        if previous
                        else None
                    ),
                }
            )
        report["processes"][str(pid)] = {
            "present": True,
            "thread_count": len(threads),
            "frozen": all(thread["ctxsw_delta"] == 0 for thread in threads if thread["ctxsw_delta"] is not None),
            "threads": threads,
        }
    return report


def _window_state(hwnd: int) -> dict:
    exists = bool(user32.IsWindow(hwnd))
    state: dict = {"hwnd": int(hwnd), "exists": exists}
    if not exists:
        return state
    style = int(user32.GetWindowLongPtrW(hwnd, GWL_STYLE))
    state.update(
        {
            "visible_api": bool(user32.IsWindowVisible(hwnd)),
            "style": f"0x{style & 0xFFFFFFFF:08x}",
            "style_visible": bool(style & WS_VISIBLE),
            "style_disabled": bool(style & WS_DISABLED),
            "class": window_module._class_name(hwnd),
            "title": window_module._title(hwnd),
            "pid": window_module._get_pid(hwnd),
            "parent": int(user32.GetParent(hwnd) or 0),
        }
    )
    if _is_hung_app_window is not None:
        state["is_hung"] = bool(_is_hung_app_window(hwnd))
    return state


def _desktop_windows() -> list[dict]:
    """Enumerate every window on the current thread desktop, including hidden helpers."""
    windows: list[dict] = []

    @window_module.WNDENUMPROC
    def collect(hwnd, _lparam):
        windows.append(
            {
                "hwnd": int(hwnd),
                "class": window_module._class_name(hwnd),
                "title": window_module._title(hwnd),
                "pid": window_module._get_pid(hwnd),
                "visible": bool(user32.IsWindowVisible(hwnd)),
            }
        )
        return True

    user32.EnumDesktopWindows(None, collect, 0)
    return windows


def _desktop_inventory() -> dict:
    names: list[str] = []

    @ENUMDESKTOPSPROC
    def collect(name, _lparam):
        if name:
            names.append(str(name))
        return True

    station = user32.GetProcessWindowStation()
    if not station:
        raise OSError(f"GetProcessWindowStation failed: {ctypes.get_last_error()}")
    user32.EnumDesktopsW(station, collect, 0)
    leaked = [name for name in names if name.startswith("pi_silent")]
    return {"total": len(names), "private": len(leaked), "private_names": sorted(leaked)}


def _com_health() -> dict:
    """Record DCOM registration timeouts that correlate with intermittent WinVer stalls."""
    script = (
        "$since=(Get-Date).AddMinutes(-{minutes});"
        "$e=Get-WinEvent -FilterHashtable @{{LogName='System';"
        "ProviderName='Microsoft-Windows-DistributedCOM';Id=10010;StartTime=$since}}"
        " -ErrorAction SilentlyContinue;"
        "if($e){{'' + $e.Count + '|' + ($e | Sort-Object TimeCreated |"
        " Select-Object -Last 1).TimeCreated.ToString('o')}}else{{'0|'}}"
    ).format(minutes=COM_HEALTH_WINDOW_MINUTES)
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=COM_HEALTH_TIMEOUT_SECONDS,
    )
    payload = (completed.stdout or "").strip().splitlines()
    if not payload:
        raise RuntimeError(f"no output (stderr={(completed.stderr or '').strip()[:200]!r})")
    count, _, latest = payload[-1].partition("|")
    return {
        "window_minutes": COM_HEALTH_WINDOW_MINUTES,
        "event_10010_count": int(count),
        "latest": latest or None,
    }


def window_exit_snapshot(
    pids: list[int],
    hwnd: int | None = None,
    desktop: str | None = None,
) -> dict:
    """Capture a JSON-serializable snapshot while isolating section failures.

    pids    -- live Job processes whose thread state must be sampled
    hwnd    -- target top-level window used to detect hidden-but-undestroyed state
    desktop -- private desktop required for window enumeration and inspection
    """
    snapshot: dict = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "job_pids": [int(pid) for pid in pids],
    }
    snapshot["threads"] = _guarded("threads", lambda: _thread_activity(pids))

    if desktop:
        def on_private_desktop() -> dict:
            with on_desktop(desktop):
                return {
                    "window": _window_state(int(hwnd)) if hwnd else None,
                    "desktop_windows": _desktop_windows(),
                }

        snapshot["desktop_view"] = _guarded("desktop_view", on_private_desktop)
    elif hwnd:
        snapshot["desktop_view"] = _guarded(
            "desktop_view", lambda: {"window": _window_state(int(hwnd))}
        )

    snapshot["desktops"] = _guarded("desktops", _desktop_inventory)
    snapshot["com_health"] = _guarded("com_health", _com_health)
    return snapshot
