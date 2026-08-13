# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

from tests.support.backend import BackendTestCase, registered_kill, registered_run
from tests.support.diagnose import window_exit_snapshot

import window as window_module
from desktop_ctx import on_desktop, operation_dpi_awareness
from job import query_named_job_pids
from process import process_creation_time, same_process

window_module.user32.FindWindowExW.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
]
window_module.user32.FindWindowExW.restype = wintypes.HWND

# Normal target exit is about 30ms. A 130s diagnostic timeout crosses the known
# 30/60/120s COM registration boundaries when the intermittent WinVer stall recurs.
EXIT_TIMEOUT_ENV = "PI_SILENT_GUI_DIAG_TIMEOUT"
DEFAULT_EXIT_TIMEOUT = 10.0
SLOW_EXIT_WARN_SECONDS = 2.0


def exit_timeout() -> float:
    """Use the default when diagnostic timeout configuration is absent or invalid."""
    raw = os.environ.get(EXIT_TIMEOUT_ENV)
    if not raw:
        return DEFAULT_EXIT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_EXIT_TIMEOUT
    return value if value > 0 else DEFAULT_EXIT_TIMEOUT


def native_rect(hwnd: int) -> wintypes.RECT:
    rect = wintypes.RECT()
    if not window_module.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise AssertionError(ctypes.get_last_error())
    return rect


def job_identities(spawned: dict) -> list[tuple[int, int]]:
    pids = query_named_job_pids(spawned["job_name"])
    identities = [(pid, process_creation_time(pid)) for pid in pids]
    if not identities or not all(created is not None for _, created in identities):
        raise AssertionError(identities)
    return [(pid, int(created)) for pid, created in identities]


def wait_for_job_exit(
    spawned: dict,
    identities: list[tuple[int, int]],
    hwnd: int | None = None,
    diagnostic_name: str = "WinVer",
) -> None:
    """Wait for every Job process to exit and capture diagnostics on timeout.

    Poll only Job membership and process identity so observation cannot affect the window.
    """
    timeout = exit_timeout()
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        if not query_named_job_pids(spawned["job_name"]) and all(
            not same_process(pid, created) for pid, created in identities
        ):
            elapsed = time.monotonic() - started
            if elapsed >= SLOW_EXIT_WARN_SECONDS:
                # Slow exit suggests a bounded wait rather than a permanent stall. Preserve
                # that signal without making a completed exit fail the compatibility smoke.
                print(
                    f"[diag] {diagnostic_name} exit was slow: {elapsed:.1f}s "
                    f"(timeout={timeout:.1f}s, job={spawned['job_name']})",
                    file=sys.stderr,
                    flush=True,
                )
            return
        time.sleep(0.05)

    # Keep the stable prefix so failure reports remain searchable across runs.
    remaining = query_named_job_pids(spawned["job_name"])
    snapshot = window_exit_snapshot(
        remaining or [pid for pid, _created in identities],
        hwnd=hwnd,
        desktop=spawned.get("desktop"),
    )
    raise AssertionError(
        f"{diagnostic_name} Job process did not exit: {remaining}\n"
        f"waited={timeout:.1f}s ({EXIT_TIMEOUT_ENV} to change)\n"
        f"diagnostics={json.dumps(snapshot, ensure_ascii=False, indent=2)}"
    )


def find_htclose_point(hwnd: int) -> tuple[int, int]:
    rect = native_rect(hwnd)
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    # The close control is always in the top-right caption region. Keep probing bounded:
    # flooding a private-desktop window with WM_NCHITTEST can trigger unrelated shell/COM churn.
    x_offsets = (4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 48, 56, 64, 72)
    ys = (4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48)
    for y in ys:
        if y >= height:
            break
        for offset in x_offsets:
            x = width - offset
            if x < 0:
                break
            if window_module.hit_test(hwnd, int(rect.left + x), int(rect.top + y)) == window_module.HTCLOSE:
                return x, y
    raise AssertionError("WM_NCHITTEST did not find HTCLOSE in the bounded caption region")


def exercise_window(
    case: BackendTestCase,
    spawned: dict,
    route: str,
    *,
    expected_class: str | None = None,
    nested_child_class: str | None = None,
    diagnostic_name: str = "WinVer",
) -> None:
    captured = registered_run("capture", spawned)
    hwnd = int(captured["hwnd"])
    if expected_class is not None:
        case.assertEqual(captured["window"]["class"], expected_class)
    identities = job_identities(spawned)
    with on_desktop(spawned["desktop"]), operation_dpi_awareness():
        top = native_rect(hwnd)
        if route == "idok":
            child_parent = hwnd
            if nested_child_class is not None:
                child_parent = int(
                    window_module.user32.FindWindowExW(
                        hwnd,
                        None,
                        nested_child_class,
                        None,
                    )
                )
                case.assertTrue(child_parent, "native child container was not found")
                case.assertEqual(int(window_module.user32.GetParent(child_parent)), hwnd)
            child = int(window_module.user32.GetDlgItem(child_parent, 1))
            case.assertTrue(child, "GetDlgItem(parent, IDOK=1) failed")
            case.assertEqual(int(window_module.user32.GetParent(child)), child_parent)
            child_rect = native_rect(child)
            x = (int(child_rect.left + child_rect.right) // 2) - int(top.left)
            y = (int(child_rect.top + child_rect.bottom) // 2) - int(top.top)
        else:
            child = 0
            x, y = find_htclose_point(hwnd)

    case.assertGreaterEqual(x, 0)
    case.assertGreaterEqual(y, 0)
    case.assertLess(x, captured["window"]["width"])
    case.assertLess(y, captured["window"]["height"])
    clicked = registered_run(
        "message",
        spawned,
        {"action": "click", "x": x, "y": y},
    )
    case.assertEqual(clicked["point"]["window"], {"x": x, "y": y})
    if route == "idok":
        case.assertEqual(clicked["dispatch"], "client")
        case.assertEqual(clicked["point"]["target_space"], "client")
        case.assertEqual(clicked["target_hwnd"], child)
    else:
        case.assertEqual(clicked["dispatch"], "nonclient")
        case.assertEqual(clicked["point"]["target_space"], "screen")
        case.assertEqual(clicked["hit_test"], window_module.HTCLOSE)
        case.assertEqual(clicked["target_hwnd"], hwnd)
        case.assertEqual(clicked["system_command"], "close")

    wait_for_job_exit(
        spawned,
        identities,
        hwnd=hwnd,
        diagnostic_name=diagnostic_name,
    )
    killed = registered_kill(spawned)
    case.assertEqual(killed["failed"], [])
    case.assertFalse(Path(spawned["tmp_dir"]).exists())


class NativeWindowSmoke(BackendTestCase):
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "win32_child_window.py"

    def setUp(self) -> None:
        self.assertTrue(self.fixture.is_file(), f"native GUI fixture is missing: {self.fixture}")

    def exercise(self, route: str) -> None:
        spawned = self.spawn_backend(
            {"exe": sys.executable, "args": ["-S", str(self.fixture)]}
        )
        exercise_window(
            self,
            spawned,
            route,
            expected_class="PiSilentGuiFixtureWindow",
            nested_child_class="PiSilentGuiFixtureWindow",
            diagnostic_name="native fixture",
        )

    def test_idok_routes_to_native_child_button(self) -> None:
        self.exercise("idok")

    def test_htclose_uses_native_nonclient_route(self) -> None:
        self.exercise("htclose")


class WinVerSmoke(BackendTestCase):
    def setUp(self) -> None:
        self.winver = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "winver.exe"
        self.assertTrue(self.winver.is_file(), f"required Windows 10+ WinVer is missing: {self.winver}")

    def exercise(self, route: str) -> None:
        spawned = self.spawn_backend({"exe": str(self.winver)})
        exercise_window(self, spawned, route)

    def test_idok_routes_to_real_child_button(self) -> None:
        self.exercise("idok")

    def test_htclose_uses_nonclient_route(self) -> None:
        self.exercise("htclose")


if __name__ == "__main__":
    import unittest

    unittest.main()
