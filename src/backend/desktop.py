# -*- coding: utf-8 -*-
"""Create a unique private desktop and launch a suspended target atomically in its Job."""
from __future__ import annotations

import ctypes
import msvcrt
import os
import shutil
import subprocess
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import BinaryIO

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
userenv = ctypes.WinDLL("userenv", use_last_error=True)

GENERIC_ALL = 0x10000000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
STILL_ACTIVE = 259
CREATE_UNICODE_ENVIRONMENT = 0x00000400
LOGON_WITH_PROFILE = 0x00000001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20
MAXIMUM_ALLOWED = 0x02000000
SECURITY_IMPERSONATION = 2
TOKEN_PRIMARY = 1
STARTF_USESHOWWINDOW = 0x00000001
STARTF_USESTDHANDLES = 0x00000100
SW_SHOW = 5
DESKTOP_READOBJECTS = 0x0001
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
RESUME_THREAD_FAILED = wintypes.DWORD(-1).value
CLEAN_ENV_KEYS = frozenset(
    key.casefold()
    for key in (
        "ALLUSERSPROFILE",
        "APPDATA",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
        "COMMONPROGRAMW6432",
        "COMPUTERNAME",
        "COMSPEC",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "LOGONSERVER",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "PROCESSOR_LEVEL",
        "PROCESSOR_REVISION",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "PUBLIC",
        "SESSIONNAME",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERDOMAIN",
        "USERDOMAIN_ROAMINGPROFILE",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
    )
)


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEX(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", wintypes.LPVOID)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


user32.CreateDesktopW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
]
user32.CreateDesktopW.restype = wintypes.HANDLE
user32.OpenDesktopW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.DWORD,
]
user32.OpenDesktopW.restype = wintypes.HANDLE
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.CloseDesktop.restype = wintypes.BOOL
user32.GetShellWindow.argtypes = []
user32.GetShellWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(SECURITY_ATTRIBUTES),
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.GetCurrentProcess.argtypes = []
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.BOOL,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFO),
    ctypes.POINTER(PROCESS_INFORMATION),
]
kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.InitializeProcThreadAttributeList.argtypes = [
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
kernel32.UpdateProcThreadAttribute.argtypes = [
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.c_size_t,
    wintypes.LPVOID,
    ctypes.c_size_t,
    wintypes.LPVOID,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
kernel32.DeleteProcThreadAttributeList.argtypes = [wintypes.LPVOID]
kernel32.DeleteProcThreadAttributeList.restype = None
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateProcess.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
kernel32.ResumeThread.restype = wintypes.DWORD
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
advapi32.DuplicateTokenEx.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPVOID,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.POINTER(wintypes.HANDLE),
]
advapi32.DuplicateTokenEx.restype = wintypes.BOOL
advapi32.CreateProcessWithTokenW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFO),
    ctypes.POINTER(PROCESS_INFORMATION),
]
advapi32.CreateProcessWithTokenW.restype = wintypes.BOOL
advapi32.ImpersonateLoggedOnUser.argtypes = [wintypes.HANDLE]
advapi32.ImpersonateLoggedOnUser.restype = wintypes.BOOL
advapi32.RevertToSelf.argtypes = []
advapi32.RevertToSelf.restype = wintypes.BOOL
userenv.CreateEnvironmentBlock.argtypes = [
    ctypes.POINTER(wintypes.LPVOID),
    wintypes.HANDLE,
    wintypes.BOOL,
]
userenv.CreateEnvironmentBlock.restype = wintypes.BOOL
userenv.DestroyEnvironmentBlock.argtypes = [wintypes.LPVOID]
userenv.DestroyEnvironmentBlock.restype = wintypes.BOOL


class NativeProcess:
    """Native process handle owned by an elevated launcher for a non-elevated broker."""

    def __init__(self, pid: int, handle: int):
        self.pid = pid
        self.handle = handle
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(wintypes.HANDLE(self.handle), ctypes.byref(code)):
            raise OSError(f"GetExitCodeProcess failed: {ctypes.get_last_error()}")
        if code.value == STILL_ACTIVE:
            return None
        self.returncode = int(code.value)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        milliseconds = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        waited = kernel32.WaitForSingleObject(wintypes.HANDLE(self.handle), milliseconds)
        if waited == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(str(self.pid), timeout)
        if waited != WAIT_OBJECT_0:
            raise OSError(f"WaitForSingleObject failed: {ctypes.get_last_error()}")
        return int(self.poll() or 0)

    def terminate(self) -> None:
        if self.poll() is None and not kernel32.TerminateProcess(wintypes.HANDLE(self.handle), 1):
            raise OSError(f"TerminateProcess(broker) failed: {ctypes.get_last_error()}")

    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(wintypes.HANDLE(self.handle))
            self.handle = 0

    def __del__(self):
        self.close()


def create_private_desktop(
    session_id: str, *, allow_elevated: bool = False
) -> tuple[str, int]:
    token: int | None = None
    impersonating = False
    current_elevated = _current_token_is_elevated()
    if allow_elevated and not current_elevated:
        raise RuntimeError("allow_elevated requires Pi/broker to already be elevated; UAC is disabled")
    if current_elevated and not allow_elevated:
        token = _shell_token()
        if not advapi32.ImpersonateLoggedOnUser(wintypes.HANDLE(token)):
            kernel32.CloseHandle(wintypes.HANDLE(token))
            raise OSError(f"ImpersonateLoggedOnUser failed: {ctypes.get_last_error()}")
        impersonating = True
    try:
        for _ in range(4):
            name = f"pi_silent_{session_id}_{uuid.uuid4().hex}"
            existing = user32.OpenDesktopW(name, 0, False, DESKTOP_READOBJECTS)
            if existing:
                user32.CloseDesktop(existing)
                continue
            handle = user32.CreateDesktopW(name, None, None, 0, GENERIC_ALL, None)
            if handle:
                return name, int(handle)
            raise OSError(f"CreateDesktop failed: {ctypes.get_last_error()}")
        raise RuntimeError("failed to allocate unique private desktop")
    finally:
        if impersonating:
            advapi32.RevertToSelf()
        if token:
            kernel32.CloseHandle(wintypes.HANDLE(token))


def resolve_exe(exe: str, cwd: str | None = None) -> str:
    path = Path(exe)
    if not path.is_absolute() and cwd:
        candidate = Path(cwd) / path
        if candidate.is_file():
            return str(candidate.resolve())
    if path.is_file():
        return str(path.resolve())
    found = shutil.which(exe)
    if found:
        return str(Path(found).resolve())
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / exe
    if system32.is_file():
        return str(system32.resolve())
    raise FileNotFoundError(exe)


def _nul_handle() -> int:
    sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
    handle = kernel32.CreateFileW(
        "NUL",
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        ctypes.byref(sa),
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if not handle or int(handle) == INVALID_HANDLE_VALUE:
        raise OSError(f"CreateFile(NUL) failed: {ctypes.get_last_error()}")
    return int(handle)


def _token_is_elevated(token: int) -> bool:
    elevated = wintypes.DWORD()
    returned = wintypes.DWORD()
    if not advapi32.GetTokenInformation(
        wintypes.HANDLE(token),
        TOKEN_ELEVATION,
        ctypes.byref(elevated),
        ctypes.sizeof(elevated),
        ctypes.byref(returned),
    ):
        raise OSError(f"GetTokenInformation(TokenElevation) failed: {ctypes.get_last_error()}")
    return bool(elevated.value)


def _current_token_is_elevated() -> bool:
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise OSError(f"OpenProcessToken(current) failed: {ctypes.get_last_error()}")
    try:
        return _token_is_elevated(int(token.value))
    finally:
        kernel32.CloseHandle(token)


def _shell_token() -> int:
    shell = user32.GetShellWindow()
    if not shell:
        raise RuntimeError("cannot find Explorer shell for non-elevated target token")
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(shell, ctypes.byref(pid))
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not process:
        raise OSError(f"OpenProcess(shell) failed: {ctypes.get_last_error()}")
    token = wintypes.HANDLE()
    primary = wintypes.HANDLE()
    try:
        access = TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY
        if not advapi32.OpenProcessToken(process, access, ctypes.byref(token)):
            raise OSError(f"OpenProcessToken(shell) failed: {ctypes.get_last_error()}")
        if not advapi32.DuplicateTokenEx(
            token,
            MAXIMUM_ALLOWED,
            None,
            SECURITY_IMPERSONATION,
            TOKEN_PRIMARY,
            ctypes.byref(primary),
        ):
            raise OSError(f"DuplicateTokenEx(shell) failed: {ctypes.get_last_error()}")
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(process)
    if _token_is_elevated(int(primary.value)):
        kernel32.CloseHandle(primary)
        raise RuntimeError("Explorer shell token is elevated; refusing to launch an elevated target")
    return int(primary.value)


def launch_broker_with_shell_token(
    argv: list[str], *, cwd: str, env: dict[str, str]
) -> tuple[NativeProcess, BinaryIO]:
    """Launch a non-elevated broker and return its private stdin writer.

    *env* is overlay-only. The child block is the shell token's
    CreateEnvironmentBlock, never the elevated caller's os.environ.
    """
    if not argv:
        raise ValueError("broker argv required")
    app = str(Path(argv[0]).resolve())
    cmd_buf = ctypes.create_unicode_buffer(subprocess.list2cmdline([app, *argv[1:]]))
    read_fd = write_fd = -1
    null = token = 0
    pi = PROCESS_INFORMATION()
    created = False
    try:
        read_fd, write_fd = os.pipe()
        os.set_inheritable(read_fd, True)
        os.set_inheritable(write_fd, False)
        null = _nul_handle()
        token = _shell_token()
        # The elevated launcher and the shell-token broker do not share a trust
        # boundary. os.environ here still carries whatever the admin process was
        # given; copy it down and those secrets keep living in a process that was
        # deliberately stripped of elevation. CreateEnvironmentBlock(token) is the
        # only base allowed on this path. *env* is overlay-only: runtime knobs the
        # caller owns (PYTHONUTF8 and friends), never a reconstituted admin block.
        # Same case-insensitive override rules as target clean_env. Create or
        # destroy failure fails closed — a block we cannot account for is not a
        # block we will hand to CreateProcessWithTokenW.
        env_ptr = _environment_buffer(
            env,
            clean_env=True,
            base=_environment_from_token(token),
        )
        si = STARTUPINFO()
        si.cb = ctypes.sizeof(STARTUPINFO)
        si.dwFlags = STARTF_USESTDHANDLES
        si.hStdInput = wintypes.HANDLE(msvcrt.get_osfhandle(read_fd))
        si.hStdOutput = wintypes.HANDLE(null)
        si.hStdError = wintypes.HANDLE(null)
        created = bool(
            advapi32.CreateProcessWithTokenW(
                wintypes.HANDLE(token),
                LOGON_WITH_PROFILE,
                app,
                cmd_buf,
                CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                env_ptr,
                cwd,
                ctypes.byref(si),
                ctypes.byref(pi),
            )
        )
        if not created:
            raise OSError(
                f"CreateProcessWithTokenW(broker) failed: {ctypes.get_last_error()}"
            )
        kernel32.CloseHandle(pi.hThread)
        pi.hThread = None
        os.close(read_fd)
        read_fd = -1
        stream = os.fdopen(write_fd, "wb", closefd=True)
        write_fd = -1
        return NativeProcess(int(pi.dwProcessId), int(pi.hProcess)), stream
    except Exception:
        if created and pi.hProcess:
            kernel32.TerminateProcess(pi.hProcess, 1)
            kernel32.CloseHandle(pi.hProcess)
        raise
    finally:
        if read_fd >= 0:
            os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        if token:
            kernel32.CloseHandle(wintypes.HANDLE(token))
        if null:
            kernel32.CloseHandle(wintypes.HANDLE(null))


def _parse_environment_block(block: wintypes.LPVOID) -> dict[str, str]:
    """Parse a CreateEnvironmentBlock double-NUL WCHAR payload into a dict."""
    result: dict[str, str] = {}
    address = ctypes.cast(block, ctypes.c_void_p).value
    if not address:
        raise RuntimeError("CreateEnvironmentBlock returned a null block")
    offset = 0
    while ctypes.c_wchar.from_address(
        address + offset * ctypes.sizeof(ctypes.c_wchar)
    ).value != "\0":
        entry = ctypes.wstring_at(address + offset * ctypes.sizeof(ctypes.c_wchar))
        split = entry.find("=", 1 if entry.startswith("=") else 0)
        if split <= 0:
            raise RuntimeError("CreateEnvironmentBlock returned a malformed entry")
        result[entry[:split]] = entry[split + 1 :]
        offset += len(entry) + 1
    return result


def _environment_from_token(token: int | wintypes.HANDLE) -> dict[str, str]:
    """User environment for *token* via CreateEnvironmentBlock; fail closed.

    Accepts a raw HANDLE (current-process clean_env path) or an int handle value
    (shell-token broker path). Does not close *token*. DestroyEnvironmentBlock
    failure raises even after a successful parse so a leaked native block cannot
    be mistaken for success.
    """
    block = wintypes.LPVOID()
    handle = token if isinstance(token, wintypes.HANDLE) else wintypes.HANDLE(token)
    if not userenv.CreateEnvironmentBlock(ctypes.byref(block), handle, False):
        raise OSError(f"CreateEnvironmentBlock failed: {ctypes.get_last_error()}")
    try:
        return _parse_environment_block(block)
    finally:
        destroy_error = (
            ctypes.get_last_error()
            if block and not userenv.DestroyEnvironmentBlock(block)
            else 0
        )
        if destroy_error:
            raise OSError(f"DestroyEnvironmentBlock failed: {destroy_error}")


def _user_environment() -> dict[str, str]:
    """Environment block for the current process token (target clean_env base)."""
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY | TOKEN_DUPLICATE, ctypes.byref(token)
    ):
        raise OSError(f"OpenProcessToken(target environment) failed: {ctypes.get_last_error()}")
    try:
        # Pass the HANDLE object through so CreateEnvironmentBlock sees the same
        # carrier OpenProcessToken filled (tests stub the BOOL without a value).
        return _environment_from_token(token)
    finally:
        kernel32.CloseHandle(token)


def _minimal_user_environment(environment: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in environment.items()
        if key.casefold() in CLEAN_ENV_KEYS
    }


def _environment_buffer(
    overrides: dict[str, str] | None,
    *,
    clean_env: bool,
    base: dict[str, str] | None = None,
):
    """Build a CREATE_UNICODE_ENVIRONMENT buffer with case-insensitive overlays.

    clean_env uses the target/current token block (or an explicit *base*, e.g. the
    shell token block for the elevated-launcher broker path). Otherwise the base is
    this process's os.environ. *base* is for callers that already resolved the
    token block; it is not a back door to admin os.environ on the shell-token path.
    """
    if base is None and overrides is None and not clean_env:
        return None
    if base is None:
        base = _user_environment() if clean_env else dict(os.environ)
    if clean_env:
        base = _minimal_user_environment(base)
    merged = {key.casefold(): (key, value) for key, value in base.items()}
    for key, value in (overrides or {}).items():
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError("environment overrides contain an invalid Windows variable")
        merged[key.casefold()] = (str(key), str(value))
    entries = sorted(merged.values(), key=lambda item: item[0].casefold())
    # create_unicode_buffer adds one NUL; the explicit suffix produces the required double NUL.
    return ctypes.create_unicode_buffer(
        "\0".join(f"{key}={value}" for key, value in entries) + "\0"
    )


def spawn_suspended_on_desktop(
    exe: str,
    desktop: str,
    job_handle: int,
    *,
    cwd: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    clean_env: bool = False,
    allow_elevated: bool = False,
) -> tuple[int, int, int]:
    """Create the suspended target atomically inside the existing Job on Windows 10+."""
    if not job_handle:
        raise ValueError("job_handle required for atomic process creation")
    if "\0" in exe or (cwd is not None and "\0" in cwd):
        raise ValueError("executable and cwd must not contain NUL")
    target_args = [] if args is None else args
    if not isinstance(target_args, list) or any(
        not isinstance(arg, str) or "\0" in arg for arg in target_args
    ):
        raise ValueError("target args must be strings without NUL")
    app = resolve_exe(exe, cwd)
    cmdline = subprocess.list2cmdline([app, *target_args])

    si_ex = STARTUPINFOEX()
    si_ex.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEX)
    si_ex.StartupInfo.lpDesktop = desktop
    si_ex.StartupInfo.dwFlags = STARTF_USESHOWWINDOW | STARTF_USESTDHANDLES
    si_ex.StartupInfo.wShowWindow = SW_SHOW
    null = _nul_handle()
    si_ex.StartupInfo.hStdInput = null
    si_ex.StartupInfo.hStdOutput = null
    si_ex.StartupInfo.hStdError = null
    pi = PROCESS_INFORMATION()

    attribute_size = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attribute_size))
    if not attribute_size.value:
        kernel32.CloseHandle(wintypes.HANDLE(null))
        raise OSError(
            f"InitializeProcThreadAttributeList(size) failed: {ctypes.get_last_error()}"
        )
    attribute_storage = ctypes.create_string_buffer(attribute_size.value)
    si_ex.lpAttributeList = ctypes.cast(attribute_storage, wintypes.LPVOID)
    initialized = False
    try:
        if not kernel32.InitializeProcThreadAttributeList(
            si_ex.lpAttributeList, 1, 0, ctypes.byref(attribute_size)
        ):
            raise OSError(
                f"InitializeProcThreadAttributeList failed: {ctypes.get_last_error()}"
            )
        initialized = True
        job_value = wintypes.HANDLE(job_handle)
        if not kernel32.UpdateProcThreadAttribute(
            si_ex.lpAttributeList,
            0,
            PROC_THREAD_ATTRIBUTE_JOB_LIST,
            ctypes.byref(job_value),
            ctypes.sizeof(job_value),
            None,
            None,
        ):
            raise OSError(
                "PROC_THREAD_ATTRIBUTE_JOB_LIST is unavailable or rejected; "
                f"refusing non-atomic launch: {ctypes.get_last_error()}"
            )

        creation = (
            CREATE_SUSPENDED
            | CREATE_NO_WINDOW
            | EXTENDED_STARTUPINFO_PRESENT
        )
        env_ptr = _environment_buffer(env, clean_env=clean_env)
        if env_ptr is not None:
            creation |= CREATE_UNICODE_ENVIRONMENT

        cmd_buf = ctypes.create_unicode_buffer(cmdline)
        current_elevated = _current_token_is_elevated()
        if allow_elevated and not current_elevated:
            raise RuntimeError(
                "allow_elevated requires Pi/broker to already be elevated; UAC is disabled"
            )
        if current_elevated and not allow_elevated:
            raise RuntimeError(
                "non-elevated targets require the launcher-created non-elevated broker; "
                "refusing a non-atomic token fallback"
            )
        startup_ptr = ctypes.cast(ctypes.byref(si_ex), ctypes.POINTER(STARTUPINFO))
        created = bool(
            kernel32.CreateProcessW(
                app,
                cmd_buf,
                None,
                None,
                True,
                creation,
                env_ptr,
                cwd,
                startup_ptr,
                ctypes.byref(pi),
            )
        )
        if not created:
            error = ctypes.get_last_error()
            # Command lines often carry credentials despite the public warning. Persisting
            # them in broker state would turn one launch failure into a quieter second leak.
            raise OSError(f"CreateProcessW failed: {error}")
        return int(pi.dwProcessId), int(pi.hProcess), int(pi.hThread)
    finally:
        if initialized:
            kernel32.DeleteProcThreadAttributeList(si_ex.lpAttributeList)
        kernel32.CloseHandle(wintypes.HANDLE(null))


def terminate_process_handle(process_handle: int, timeout_ms: int = 3000) -> None:
    if not kernel32.TerminateProcess(wintypes.HANDLE(process_handle), 1):
        raise OSError(f"TerminateProcess failed: {ctypes.get_last_error()}")
    waited = kernel32.WaitForSingleObject(wintypes.HANDLE(process_handle), timeout_ms)
    if waited != WAIT_OBJECT_0:
        raise TimeoutError(f"process handle did not terminate within {timeout_ms}ms")


def resume_thread(thread_handle: int) -> None:
    if kernel32.ResumeThread(wintypes.HANDLE(thread_handle)) == RESUME_THREAD_FAILED:
        raise OSError(f"ResumeThread failed: {ctypes.get_last_error()}")


def close_process_handle(handle: int | None) -> None:
    if handle:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def close_desktop_handle(handle: int | None) -> None:
    if handle:
        user32.CloseDesktop(wintypes.HANDLE(handle))
