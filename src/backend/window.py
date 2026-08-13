# -*- coding: utf-8 -*-
"""Find Job-owned windows and dispatch bounded client or non-client messages."""
from __future__ import annotations

import ctypes
import time
from contextlib import contextmanager
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_NCHITTEST = 0x0084
WM_NCMOUSEMOVE = 0x00A0
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONUP = 0x00A2
WM_SYSCOMMAND = 0x0112
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
HTCLIENT = 1
HTCLOSE = 20
SC_CLOSE = 0xF060
MK_LBUTTON = 0x0001
CWP_SKIPINVISIBLE = 0x0001
CWP_SKIPTRANSPARENT = 0x0004
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002
MESSAGE_TIMEOUT_MS = 2000
MAX_TYPE_CHARS = 4000

VK_NAMES = {
    "return": 0x0D,
    "enter": 0x0D,
    "space": 0x20,
    "escape": 0x1B,
    "esc": 0x1B,
    "tab": 0x09,
    "backspace": 0x08,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
}
VK_NAMES.update({f"f{index}": 0x70 + index - 1 for index in range(1, 13)})

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ScreenToClient.restype = wintypes.BOOL
user32.ChildWindowFromPointEx.argtypes = [wintypes.HWND, wintypes.POINT, wintypes.UINT]
user32.ChildWindowFromPointEx.restype = wintypes.HWND
user32.MapWindowPoints.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.POINTER(wintypes.POINT),
    wintypes.UINT,
]
user32.MapWindowPoints.restype = ctypes.c_int
user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetDlgItem.restype = wintypes.HWND
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.SetActiveWindow.argtypes = [wintypes.HWND]
user32.SetActiveWindow.restype = wintypes.HWND
user32.SetFocus.argtypes = [wintypes.HWND]
user32.SetFocus.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetFocus.argtypes = []
user32.GetFocus.restype = wintypes.HWND
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_size_t),
]
user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL

_get_dpi_for_window = getattr(user32, "GetDpiForWindow", None)
if _get_dpi_for_window is not None:
    _get_dpi_for_window.argtypes = [wintypes.HWND]
    _get_dpi_for_window.restype = wintypes.UINT


def _get_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 2)
    user32.GetWindowTextW(hwnd, buf, length + 2)
    return buf.value


def _window_rect(hwnd: int) -> wintypes.RECT:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError(f"GetWindowRect failed: {ctypes.get_last_error()}")
    return rect


def _window_snapshot(hwnd: int) -> tuple[dict, wintypes.RECT]:
    if not user32.IsWindow(hwnd):
        raise LookupError(f"window no longer exists: hwnd={hwnd}")
    rect = _window_rect(hwnd)
    client = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(client)):
        raise OSError(f"GetClientRect failed: {ctypes.get_last_error()}")
    origin = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        raise OSError(f"ClientToScreen failed: {ctypes.get_last_error()}")
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    client_width = int(client.right - client.left)
    client_height = int(client.bottom - client.top)
    if width <= 0 or height <= 0 or client_width < 0 or client_height < 0:
        raise OSError(
            f"invalid window/client area {width}x{height}/{client_width}x{client_height}"
        )
    if _get_dpi_for_window is not None:
        dpi = int(_get_dpi_for_window(hwnd))
        if not dpi:
            if not user32.IsWindow(hwnd):
                raise LookupError(f"window no longer exists: hwnd={hwnd}")
            raise OSError("GetDpiForWindow failed")
    else:
        dpi = 96
    return (
        {
            "width": width,
            "height": height,
            "client": {
                "x": int(origin.x - rect.left),
                "y": int(origin.y - rect.top),
                "width": client_width,
                "height": client_height,
            },
            "dpi": dpi,
        },
        rect,
    )


def window_geometry(hwnd: int) -> dict:
    geometry, _rect = _window_snapshot(hwnd)
    return geometry


def window_screen_origin(hwnd: int) -> tuple[int, int]:
    """Top-left of the window in its desktop's screen coordinates.

    Inject-mode clicks fake GetCursorPos, which reports screen coordinates, so a
    window-relative click point must be lifted back to that space. Must be called
    on the target's desktop or the rect belongs to the wrong desktop.
    """
    rect = _window_rect(hwnd)
    return int(rect.left), int(rect.top)


def list_top_windows(pid: int) -> list[dict]:
    found: list[dict] = []

    @WNDENUMPROC
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or _get_pid(hwnd) != pid:
            return True
        if user32.GetParent(hwnd):
            return True
        try:
            geometry = window_geometry(int(hwnd))
        except (LookupError, OSError):
            return True
        found.append(
            {
                "hwnd": int(hwnd),
                "class": _class_name(hwnd),
                "title": _title(hwnd),
                "area": geometry["client"]["width"] * geometry["client"]["height"],
                **geometry,
            }
        )
        return True

    ctypes.set_last_error(0)
    if not user32.EnumWindows(enum_proc, 0):
        error = ctypes.get_last_error()
        if error:
            raise OSError(f"EnumWindows failed: {error}")
    found.sort(key=lambda item: item["area"], reverse=True)
    return found


def _resolve_expected_hwnd(
    hwnd: int,
    pids: list[int],
    window_class: str | None,
    title_contains: str | None,
) -> dict:
    """Validate a known HWND and return the normal candidate metadata."""
    if isinstance(hwnd, bool) or not isinstance(hwnd, int) or hwnd <= 0:
        raise LookupError(f"expected hwnd must be a positive integer: {hwnd!r}")
    if not user32.IsWindow(hwnd):
        raise LookupError(f"expected hwnd {hwnd} is not a valid window")
    if not user32.IsWindowVisible(hwnd):
        raise LookupError(f"expected hwnd {hwnd} is not visible")
    if user32.GetParent(hwnd):
        raise LookupError(f"expected hwnd {hwnd} is not a top-level window")
    pid = _get_pid(hwnd)
    if pid not in pids:
        raise LookupError(
            f"expected hwnd {hwnd} belongs to pid {pid}, expected one of {pids}"
        )
    cls = _class_name(hwnd)
    if window_class and cls != window_class:
        raise LookupError(
            f"expected hwnd {hwnd} class {cls!r} != {window_class!r}"
        )
    t = _title(hwnd) or ""
    if title_contains and title_contains not in t:
        raise LookupError(
            f"expected hwnd {hwnd} title {t!r} does not contain {title_contains!r}"
        )
    try:
        geometry = window_geometry(hwnd)
    except (LookupError, OSError) as e:
        raise LookupError(f"expected hwnd {hwnd} geometry failed: {e}") from e
    return {
        "hwnd": hwnd,
        "class": cls,
        "title": t,
        "pid": pid,
        "area": geometry["client"]["width"] * geometry["client"]["height"],
        **geometry,
    }


def find_window_in_pids(
    pids: list[int],
    *,
    window_class: str | None = None,
    title_contains: str | None = None,
    expected_hwnd: int | None = None,
) -> dict:
    if expected_hwnd is not None:
        return _resolve_expected_hwnd(expected_hwnd, pids, window_class, title_contains)
    candidates: list[dict] = []
    for pid in pids:
        for window in list_top_windows(pid):
            if window_class and window["class"] != window_class:
                continue
            if title_contains and title_contains not in (window["title"] or ""):
                continue
            window["pid"] = pid
            candidates.append(window)
    if not candidates:
        raise LookupError(
            f"no window in pids={pids} class={window_class!r} title~={title_contains!r}"
        )
    return max(candidates, key=lambda item: item["area"])


@contextmanager
def _input_attached(hwnd: int):
    # GetFocus 只在线程输入仍挂在目标上时说真话。点完就摘掉没问题；打字必须趁还挂着。
    if not user32.IsWindow(hwnd):
        raise LookupError(f"window no longer exists: hwnd={hwnd}")
    pid = wintypes.DWORD()
    target_tid = int(user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)))
    current_tid = int(kernel32.GetCurrentThreadId())
    attached = bool(
        target_tid
        and target_tid != current_tid
        and user32.AttachThreadInput(current_tid, target_tid, True)
    )
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetActiveWindow(hwnd)
        user32.SetFocus(hwnd)
        user32.SetForegroundWindow(hwnd)
        yield
    finally:
        if attached:
            user32.AttachThreadInput(current_tid, target_tid, False)


def _activate(hwnd: int) -> None:
    """Activate a window only on the current private desktop without switching user input."""
    with _input_attached(hwnd):
        return


def _send(hwnd: int, message: int, wparam: int, lparam: int) -> int:
    if not user32.IsWindow(hwnd):
        raise LookupError(f"window no longer exists: hwnd={hwnd}")
    result = ctypes.c_size_t()
    ctypes.set_last_error(0)
    sent = user32.SendMessageTimeoutW(
        hwnd,
        message,
        ctypes.c_size_t(wparam).value,
        lparam,
        SMTO_BLOCK | SMTO_ABORTIFHUNG,
        MESSAGE_TIMEOUT_MS,
        ctypes.byref(result),
    )
    if not sent:
        error = ctypes.get_last_error()
        raise TimeoutError(
            f"window message failed or timed out: hwnd={hwnd} msg=0x{message:04x} error={error}"
        )
    return int(result.value)


def _post(hwnd: int, message: int, wparam: int, lparam: int) -> None:
    if not user32.IsWindow(hwnd):
        raise LookupError(f"window no longer exists: hwnd={hwnd}")
    ctypes.set_last_error(0)
    if not user32.PostMessageW(hwnd, message, ctypes.c_size_t(wparam).value, lparam):
        raise OSError(
            f"PostMessage failed: hwnd={hwnd} msg=0x{message:04x} error={ctypes.get_last_error()}"
        )


def _pack_point(x: int, y: int) -> int:
    if not -0x8000 <= x <= 0x7FFF or not -0x8000 <= y <= 0x7FFF:
        raise OverflowError(f"point cannot be represented in LPARAM: ({x},{y})")
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def hit_test(hwnd: int, screen_x: int, screen_y: int) -> int:
    value = _send(hwnd, WM_NCHITTEST, 0, _pack_point(screen_x, screen_y))
    return int(ctypes.c_int32(value & 0xFFFFFFFF).value)


def _screen_to_client(hwnd: int, screen_x: int, screen_y: int) -> wintypes.POINT:
    point = wintypes.POINT(screen_x, screen_y)
    ctypes.set_last_error(0)
    moved = user32.MapWindowPoints(0, hwnd, ctypes.byref(point), 1)
    if moved == 0 and ctypes.get_last_error():
        raise OSError(f"MapWindowPoints(screen->client) failed: {ctypes.get_last_error()}")
    return point


def _is_descendant(root: int, target: int) -> bool:
    current = int(target)
    for _ in range(65):
        if current == int(root):
            return True
        if not current or not user32.IsWindow(current):
            return False
        current = int(user32.GetParent(current) or 0)
    return False


def _deepest_child(hwnd: int, point: wintypes.POINT) -> tuple[int, wintypes.POINT]:
    # Descend through visible non-transparent children and remap coordinates at each level.
    target = int(hwnd)
    current = wintypes.POINT(point.x, point.y)
    flags = CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT
    for _ in range(64):
        ctypes.set_last_error(0)
        child = user32.ChildWindowFromPointEx(target, current, flags)
        if not child:
            if not user32.IsWindow(target):
                raise LookupError(f"window no longer exists: hwnd={target}")
            raise OSError(f"ChildWindowFromPointEx failed: {ctypes.get_last_error()}")
        child_id = int(child)
        if child_id == target:
            return target, current
        if not user32.IsWindow(child_id) or int(user32.GetParent(child_id) or 0) != target:
            raise LookupError(f"child window changed during routing: parent={target} child={child_id}")
        mapped = wintypes.POINT(current.x, current.y)
        ctypes.set_last_error(0)
        moved = user32.MapWindowPoints(target, child_id, ctypes.byref(mapped), 1)
        if moved == 0 and ctypes.get_last_error():
            raise OSError(f"MapWindowPoints failed: {ctypes.get_last_error()}")
        if not user32.IsWindow(child_id) or int(user32.GetParent(child_id) or 0) != target:
            raise LookupError(f"child window changed during mapping: parent={target} child={child_id}")
        target = child_id
        current = mapped
    raise RuntimeError("child window nesting exceeds 64 levels")


def click(hwnd: int, x: int, y: int) -> dict:
    # Public click coordinates match the full PNG; one snapshot drives bounds and auditing.
    geometry, rect = _window_snapshot(hwnd)
    if x < 0 or y < 0 or x >= geometry["width"] or y >= geometry["height"]:
        raise ValueError(
            f"click outside window: ({x},{y}) not in {geometry['width']}x{geometry['height']}"
        )
    screen_x = int(rect.left + x)
    screen_y = int(rect.top + y)
    hit = hit_test(hwnd, screen_x, screen_y)
    _activate(hwnd)

    if hit == HTCLIENT:
        client_point = _screen_to_client(hwnd, screen_x, screen_y)
        target, target_point = _deepest_child(hwnd, client_point)
        if not _is_descendant(hwnd, target):
            raise LookupError(f"click target left window tree: root={hwnd} target={target}")
        lparam = _pack_point(int(target_point.x), int(target_point.y))
        _send(target, WM_MOUSEMOVE, 0, lparam)
        _send(target, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
        time.sleep(0.02)
        _send(target, WM_LBUTTONUP, 0, lparam)
        dispatch = "client"
        actual_x, actual_y = int(target_point.x), int(target_point.y)
    else:
        target = int(hwnd)
        lparam = _pack_point(screen_x, screen_y)
        # Queue non-client messages so title-bar or menu modal loops cannot block the release.
        _post(target, WM_NCMOUSEMOVE, hit, lparam)
        system_command = None
        if hit == HTCLOSE:
            # Post one close system command to avoid duplicate close intent from NC click synthesis.
            _post(target, WM_SYSCOMMAND, SC_CLOSE, lparam)
            system_command = "close"
        else:
            _post(target, WM_NCLBUTTONDOWN, hit, lparam)
            time.sleep(0.02)
            _post(target, WM_NCLBUTTONUP, hit, lparam)
        dispatch = "nonclient"
        actual_x, actual_y = screen_x, screen_y

    result = {
        "window": {"hwnd": int(hwnd), **geometry},
        "hit_test": hit,
        "target_hwnd": target,
        "dispatch": dispatch,
        "point": {
            "window": {"x": x, "y": y},
            "screen": {"x": screen_x, "y": screen_y},
            "target": {"x": actual_x, "y": actual_y},
            "target_space": "client" if dispatch == "client" else "screen",
        },
    }
    if dispatch == "nonclient" and system_command:
        result["system_command"] = system_command
    return result


def _utf16_units(text: str) -> list[int]:
    encoded = text.encode("utf-16-le")
    return [int.from_bytes(encoded[index : index + 2], "little") for index in range(0, len(encoded), 2)]


def type_text(hwnd: int, text: str) -> dict:
    if not isinstance(text, str) or not text:
        raise ValueError("text required")
    if "\0" in text:
        raise ValueError("text cannot contain NUL")
    if len(text) > MAX_TYPE_CHARS:
        raise ValueError(f"text exceeds {MAX_TYPE_CHARS} characters")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    with _input_attached(hwnd):
        focus = int(user32.GetFocus() or 0)
        target = focus if focus and _is_descendant(hwnd, focus) else int(hwnd)
        sent = 0
        for char in normalized:
            if char == "\n":
                # 不走 key()：它会再 Attach/Detach 一次，把这次打字还需要的焦点摘掉。
                _send(target, WM_KEYDOWN, VK_NAMES["return"], 1)
                _send(target, WM_KEYUP, VK_NAMES["return"], 0xC0000001)
            else:
                for unit in _utf16_units(char):
                    _send(target, WM_CHAR, unit, 0)
            sent += 1
    return {"hwnd": int(hwnd), "target_hwnd": int(target), "chars": sent}


def key(hwnd: int, vk: int | None = None, name: str | None = None) -> None:
    if vk is None:
        if not name:
            raise ValueError("vk or name required")
        key_name = name.lower()
        if key_name not in VK_NAMES:
            raise ValueError(f"unknown key name: {name}")
        vk = VK_NAMES[key_name]
    if not isinstance(vk, int) or isinstance(vk, bool):
        raise TypeError(f"vk must be int, not {type(vk).__name__}")
    if not 1 <= vk <= 255:
        raise ValueError(f"vk must be in 1..255, got {vk}")
    _activate(hwnd)
    _send(hwnd, WM_KEYDOWN, int(vk), 1)
    _send(hwnd, WM_KEYUP, int(vk), 0xC0000001)


def verify_window_in_pids(hwnd: int, pids: list[int]) -> bool:
    """Reject common HWND reuse before each queued batch action.

    Same-process reuse remains possible; revisit only if a real target needs a narrower
    Windows identity than HWND plus current Job membership.
    """
    return bool(
        user32.IsWindow(hwnd)
        and user32.IsWindowVisible(hwnd)
        and not user32.GetParent(hwnd)
        and _get_pid(hwnd) in pids
    )
