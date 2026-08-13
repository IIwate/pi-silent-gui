from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
imm32 = ctypes.WinDLL("imm32", use_last_error=True)

CLASS_NAME = "PiSilentGuiFixtureWindow"
WINDOW_TITLE = "pi-silent-gui native fixture"
IDOK = 1
CONTAINER_ID = 2
BN_CLICKED = 0
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
BS_DEFPUSHBUTTON = 0x00000001
CW_USEDEFAULT = 0x80000000
SW_SHOW = 5
COLOR_WINDOW = 5
GA_ROOT = 2

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.DefWindowProcW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.UpdateWindow.argtypes = [wintypes.HWND]
user32.UpdateWindow.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
imm32.ImmDisableIME.argtypes = [wintypes.DWORD]
imm32.ImmDisableIME.restype = wintypes.BOOL


def loword(value: int) -> int:
    return value & 0xFFFF


def hiword(value: int) -> int:
    return (value >> 16) & 0xFFFF


@WNDPROC
def window_proc(hwnd, message, wparam, lparam):
    if message == WM_COMMAND and loword(int(wparam)) == IDOK and hiword(int(wparam)) == BN_CLICKED:
        user32.DestroyWindow(user32.GetAncestor(hwnd, GA_ROOT))
        return 0
    if message == WM_CLOSE:
        user32.DestroyWindow(hwnd)
        return 0
    if message == WM_DESTROY and not user32.GetParent(hwnd):
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


def main() -> int:
    # The fixture has no text input. Disabling IME avoids pulling TSF/CTF COM helpers into
    # the target Job, which would turn a native routing check into a system service smoke.
    imm32.ImmDisableIME(0xFFFFFFFF)
    instance = kernel32.GetModuleHandleW(None)
    window_class = WNDCLASSW(
        0,
        window_proc,
        0,
        0,
        instance,
        None,
        None,
        ctypes.cast(COLOR_WINDOW + 1, wintypes.HBRUSH),
        None,
        CLASS_NAME,
    )
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        raise OSError(f"RegisterClassW failed: {ctypes.get_last_error()}")

    hwnd = user32.CreateWindowExW(
        0,
        CLASS_NAME,
        WINDOW_TITLE,
        WS_OVERLAPPEDWINDOW | WS_VISIBLE,
        CW_USEDEFAULT,
        CW_USEDEFAULT,
        480,
        240,
        None,
        None,
        instance,
        None,
    )
    if not hwnd:
        raise OSError(f"CreateWindowExW(top) failed: {ctypes.get_last_error()}")

    container = user32.CreateWindowExW(
        0,
        CLASS_NAME,
        None,
        WS_CHILD | WS_VISIBLE,
        80,
        50,
        320,
        130,
        hwnd,
        wintypes.HMENU(CONTAINER_ID),
        instance,
        None,
    )
    if not container:
        raise OSError(f"CreateWindowExW(container) failed: {ctypes.get_last_error()}")

    button = user32.CreateWindowExW(
        0,
        "BUTTON",
        "Close",
        WS_CHILD | WS_VISIBLE | BS_DEFPUSHBUTTON,
        90,
        60,
        120,
        36,
        container,
        wintypes.HMENU(IDOK),
        instance,
        None,
    )
    if not button:
        raise OSError(f"CreateWindowExW(button) failed: {ctypes.get_last_error()}")

    user32.ShowWindow(hwnd, SW_SHOW)
    user32.UpdateWindow(hwnd)
    message = wintypes.MSG()
    while True:
        result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
        if result == -1:
            raise OSError(f"GetMessageW failed: {ctypes.get_last_error()}")
        if result == 0:
            return int(message.wParam)
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))


if __name__ == "__main__":
    raise SystemExit(main())
