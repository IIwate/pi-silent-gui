# -*- coding: utf-8 -*-
"""Shared JSON helpers and constrained session temporary directories."""
from __future__ import annotations

import ctypes
import json
import msvcrt
import os
import re
import sys
import uuid
from ctypes import wintypes
from pathlib import Path

_SESSION_RE = re.compile(r"^[0-9a-f]{12}$")
FOLDERID_LOCAL_APPDATA = "f1b32785-6fba-4fcf-9d55-7b8e7f157091"

FILE_LIST_DIRECTORY = 0x0001
FILE_READ_ATTRIBUTES = 0x0080
GENERIC_READ = 0x80000000
DELETE = 0x00010000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_INFO_BY_HANDLE_CLASS_BASIC = 0
FILE_INFO_BY_HANDLE_CLASS_DISPOSITION = 4
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ALREADY_EXISTS = 183
MAX_SESSION_ID_ATTEMPTS = 16
PROTOCOL_PREFIX = "PI_SILENT_GUI_FRAME"
PROTOCOL_VERSION = "1"
_protocol_command = "direct"


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class FILE_BASIC_INFO(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_int64),
        ("LastAccessTime", ctypes.c_int64),
        ("LastWriteTime", ctypes.c_int64),
        ("ChangeTime", ctypes.c_int64),
        ("FileAttributes", wintypes.DWORD),
    ]


class FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = [("DeleteFile", wintypes.BOOL)]


def _guid(value: str) -> GUID:
    parsed = uuid.UUID(value)
    fields = parsed.fields
    tail = bytes((fields[3], fields[4])) + int(fields[5]).to_bytes(6, "big")
    return GUID(fields[0], fields[1], fields[2], (ctypes.c_ubyte * 8).from_buffer_copy(tail))


def is_windows() -> bool:
    return sys.platform == "win32"


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, wintypes.LPVOID]
    kernel32.CreateDirectoryW.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel32.GetLongPathNameW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    kernel32.GetLongPathNameW.restype = wintypes.DWORD
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _local_appdata() -> Path:
    if not is_windows():
        raise RuntimeError("Windows LocalAppData is required")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(GUID),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [wintypes.LPVOID]
    ole32.CoTaskMemFree.restype = None
    folder = _guid(FOLDERID_LOCAL_APPDATA)
    raw = ctypes.c_void_p()
    result = shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(raw))
    if result != 0 or not raw.value:
        raise OSError(f"SHGetKnownFolderPath(LocalAppData) failed: 0x{result & 0xFFFFFFFF:08x}")
    try:
        return _lexical_path(ctypes.wstring_at(raw.value))
    finally:
        ole32.CoTaskMemFree(raw)


def _lexical_path(path: str | os.PathLike[str]) -> Path:
    normalized = os.path.abspath(os.path.normpath(os.fspath(path)))
    if is_windows():
        try:
            kernel32 = _kernel32()
            size = kernel32.GetLongPathNameW(normalized, None, 0)
            if size:
                buffer = ctypes.create_unicode_buffer(size + 1)
                length = kernel32.GetLongPathNameW(normalized, buffer, len(buffer))
                if length and length < len(buffer):
                    normalized = buffer.value
        except Exception:
            pass
    return Path(normalized)


def _path_key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(str(_lexical_path(path)))


def _exact_temp_root() -> Path:
    return _lexical_path(_local_appdata() / "Temp" / "pi-silent-gui")


def _handle_attributes(kernel32, handle: int) -> int:
    info = FILE_BASIC_INFO()
    if not kernel32.GetFileInformationByHandleEx(
        wintypes.HANDLE(handle),
        FILE_INFO_BY_HANDLE_CLASS_BASIC,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        raise OSError(f"GetFileInformationByHandleEx failed: {ctypes.get_last_error()}")
    return int(info.FileAttributes)


def _handle_path(kernel32, handle: int) -> Path:
    size = kernel32.GetFinalPathNameByHandleW(wintypes.HANDLE(handle), None, 0, 0)
    if not size:
        raise OSError(f"GetFinalPathNameByHandleW(size) failed: {ctypes.get_last_error()}")
    buffer = ctypes.create_unicode_buffer(size + 1)
    length = kernel32.GetFinalPathNameByHandleW(
        wintypes.HANDLE(handle), buffer, len(buffer), 0
    )
    if not length or length >= len(buffer):
        raise OSError(f"GetFinalPathNameByHandleW failed: {ctypes.get_last_error()}")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return _lexical_path(value)


def _open_path(
    kernel32,
    path: Path,
    *,
    directory: bool,
    delete: bool = False,
    lock_writes: bool = False,
    read: bool = False,
) -> int | None:
    access = FILE_READ_ATTRIBUTES | (FILE_LIST_DIRECTORY if directory else 0)
    if read:
        access |= GENERIC_READ
    if delete:
        access |= DELETE
    ctypes.set_last_error(0)
    handle = kernel32.CreateFileW(
        str(path),
        access,
        FILE_SHARE_READ | (0 if lock_writes else FILE_SHARE_WRITE),
        None,
        OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT | (FILE_FLAG_BACKUP_SEMANTICS if directory else 0),
        None,
    )
    if handle and int(handle) != INVALID_HANDLE_VALUE:
        return int(handle)
    if ctypes.get_last_error() in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
        return None
    raise OSError(f"CreateFileW failed: {ctypes.get_last_error()} path={path}")


def _close_handles(kernel32, handles: list[int]) -> None:
    for handle in reversed(handles):
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def _open_directory_chain(
    path: Path,
    *,
    create: bool = False,
    exclusive_leaf: bool = False,
    delete_leaf: bool = False,
    missing_leaf_ok: bool = False,
    lock_writes: bool = False,
) -> tuple[object, list[int], int | None]:
    """Hold every path component and reject reparse points throughout the chain."""
    exact = _lexical_path(path)
    if not exact.is_absolute() or not exact.anchor:
        raise ValueError(f"absolute path required: {exact}")
    kernel32 = _kernel32()
    handles: list[int] = []
    current = Path(exact.anchor)
    parts = exact.parts[1:]
    try:
        for index, part in enumerate((exact.anchor, *parts)):
            if index:
                current /= part
            leaf = index == len(parts)
            handle = _open_path(
                kernel32,
                current,
                directory=True,
                delete=delete_leaf and leaf,
                lock_writes=lock_writes,
            )
            if handle is not None and create and exclusive_leaf and leaf:
                kernel32.CloseHandle(wintypes.HANDLE(handle))
                raise FileExistsError(f"session directory already exists: {current}")
            if handle is None and create:
                ctypes.set_last_error(0)
                if not kernel32.CreateDirectoryW(str(current), None):
                    error = ctypes.get_last_error()
                    if error == ERROR_ALREADY_EXISTS and exclusive_leaf and leaf:
                        raise FileExistsError(f"session directory already exists: {current}")
                    if error != ERROR_ALREADY_EXISTS:
                        raise OSError(f"CreateDirectoryW failed: {error} path={current}")
                handle = _open_path(
                    kernel32,
                    current,
                    directory=True,
                    delete=delete_leaf and leaf,
                    lock_writes=lock_writes,
                )
            if handle is None:
                if missing_leaf_ok and leaf:
                    return kernel32, handles, None
                raise FileNotFoundError(current)
            attributes = _handle_attributes(kernel32, handle)
            if not attributes & FILE_ATTRIBUTE_DIRECTORY:
                kernel32.CloseHandle(wintypes.HANDLE(handle))
                raise OSError(f"path component is not a directory: {current}")
            if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                kernel32.CloseHandle(wintypes.HANDLE(handle))
                raise OSError(f"reparse point rejected in session path: {current}")
            resolved = _handle_path(kernel32, handle)
            if _path_key(resolved) != _path_key(current):
                kernel32.CloseHandle(wintypes.HANDLE(handle))
                raise OSError(f"resolved path mismatch: expected={current} actual={resolved}")
            handles.append(handle)
        return kernel32, handles, handles[-1]
    except Exception:
        _close_handles(kernel32, handles)
        raise


def temp_root() -> Path:
    exact = _exact_temp_root()
    kernel32, handles, root_handle = _open_directory_chain(exact, create=True)
    try:
        if root_handle is None or _path_key(_handle_path(kernel32, root_handle)) != _path_key(exact):
            raise OSError(f"resolved temp root differs from exact root: {exact}")
    finally:
        _close_handles(kernel32, handles)
    return exact


def validate_session_id(session_id: str) -> str:
    value = str(session_id)
    if not _SESSION_RE.fullmatch(value):
        raise ValueError("invalid session_id")
    return value


def session_tmp(session_id: str, *, create: bool = True) -> Path:
    sid = validate_session_id(session_id)
    root = temp_root()
    expected = _lexical_path(root / sid)
    if expected.parent != root:
        raise ValueError("session path escaped exact temp root")
    kernel32, handles, session_handle = _open_directory_chain(
        expected,
        create=create,
        missing_leaf_ok=not create,
    )
    try:
        if session_handle is not None:
            resolved = _handle_path(kernel32, session_handle)
            if _path_key(resolved) != _path_key(expected):
                raise OSError(
                    f"resolved session differs from expected session: expected={expected} actual={resolved}"
                )
    finally:
        _close_handles(kernel32, handles)
    return expected


def session_state_path(session_id: str, *, create: bool = True) -> Path:
    return session_tmp(session_id, create=create) / "session.json"


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def create_session_tmp() -> tuple[str, Path]:
    """Create a fresh session directory without ever reusing existing state."""
    root = temp_root()
    for _attempt in range(MAX_SESSION_ID_ATTEMPTS):
        session_id = new_session_id()
        expected = _lexical_path(root / session_id)
        try:
            kernel32, handles, session_handle = _open_directory_chain(
                expected,
                create=True,
                exclusive_leaf=True,
            )
        except FileExistsError:
            continue
        try:
            if session_handle is None:
                raise OSError(f"exclusive session directory was not opened: {expected}")
            if _path_key(_handle_path(kernel32, session_handle)) != _path_key(expected):
                raise OSError(f"exclusive session path mismatch: {expected}")
        finally:
            _close_handles(kernel32, handles)
        return session_id, expected
    raise RuntimeError(
        f"could not allocate a unique session directory after {MAX_SESSION_ID_ATTEMPTS} attempts"
    )


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with pending.open("w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def write_json_atomic(path: Path, data: dict) -> None:
    write_text_atomic(path, json.dumps(data, ensure_ascii=True), encoding="utf-8")


def read_json(path: Path) -> dict:
    exact = _lexical_path(path)
    kernel32, handles, _parent = _open_directory_chain(
        exact.parent,
        lock_writes=True,
    )
    file_handle: int | None = None
    try:
        file_handle = _open_path(
            kernel32,
            exact,
            directory=False,
            lock_writes=True,
            read=True,
        )
        if file_handle is None:
            raise FileNotFoundError(exact)
        attributes = _handle_attributes(kernel32, file_handle)
        if attributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT):
            raise OSError(f"regular non-reparse JSON file required: {exact}")
        if _path_key(_handle_path(kernel32, file_handle)) != _path_key(exact):
            raise OSError(f"opened JSON path mismatch: {exact}")
        fd = msvcrt.open_osfhandle(file_handle, os.O_RDONLY)
        file_handle = None  # fd now owns the Windows handle.
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    finally:
        if file_handle:
            kernel32.CloseHandle(wintypes.HANDLE(file_handle))
        _close_handles(kernel32, handles)
    if not isinstance(data, dict):
        raise ValueError(f"JSON object required: {exact}")
    return data


def _delete_by_handle(kernel32, handle: int, path: Path) -> None:
    disposition = FILE_DISPOSITION_INFO(True)
    if not kernel32.SetFileInformationByHandle(
        wintypes.HANDLE(handle),
        FILE_INFO_BY_HANDLE_CLASS_DISPOSITION,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise OSError(f"SetFileInformationByHandle(delete) failed: {ctypes.get_last_error()} path={path}")


def _remove_open_directory(kernel32, path: Path, handle: int) -> None:
    entries = list(os.scandir(path))
    # The state file is the last surviving witness. If another entry is locked,
    # orphan cleanup must retain the identity needed for a later, safer retry.
    entries.sort(key=lambda entry: entry.name.casefold() == "session.json")
    for entry in entries:
        child = _lexical_path(path / entry.name)
        if child.parent != path:
            raise OSError(f"child escaped opened directory: {child}")
        child_handle = _open_path(
            kernel32,
            child,
            directory=entry.is_dir(follow_symlinks=False),
            delete=True,
            lock_writes=True,
        )
        if child_handle is None:
            continue
        try:
            attributes = _handle_attributes(kernel32, child_handle)
            if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                raise OSError(f"reparse point rejected during session deletion: {child}")
            if _path_key(_handle_path(kernel32, child_handle)) != _path_key(child):
                raise OSError(f"opened child path mismatch during deletion: {child}")
            if attributes & FILE_ATTRIBUTE_DIRECTORY:
                _remove_open_directory(kernel32, child, child_handle)
            _delete_by_handle(kernel32, child_handle, child)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(child_handle))


def remove_session_tmp(session_id: str) -> None:
    sid = validate_session_id(session_id)
    exact_root = temp_root()
    expected = _lexical_path(exact_root / sid)
    if expected.parent != exact_root or expected == exact_root:
        raise ValueError("session deletion target must be exact_root/<12hex>")

    kernel32, handles, session_handle = _open_directory_chain(
        expected,
        delete_leaf=True,
        missing_leaf_ok=True,
        lock_writes=True,
    )
    try:
        root_index = len(Path(exact_root).parts) - 1
        if root_index >= len(handles):
            raise OSError("exact temp root was not held during deletion")
        resolved_root = _handle_path(kernel32, handles[root_index])
        if _path_key(resolved_root) != _path_key(exact_root):
            raise OSError(
                f"resolved temp root differs from exact root: expected={exact_root} actual={resolved_root}"
            )
        if session_handle is None:
            return
        resolved_session = _handle_path(kernel32, session_handle)
        if _path_key(resolved_session) != _path_key(expected):
            raise OSError(
                f"resolved session differs from expected session: expected={expected} actual={resolved_session}"
            )
        _remove_open_directory(kernel32, expected, session_handle)
        _delete_by_handle(kernel32, session_handle, expected)
    finally:
        _close_handles(kernel32, handles)
    if os.path.lexists(expected):
        raise OSError(f"failed to remove session temp: {expected}")


def set_protocol_command(command: str) -> None:
    global _protocol_command
    _protocol_command = str(command)


def emit(success: bool, **data) -> None:
    payload = {"ok": success, **data}
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    sys.stdout.write(
        f"{PROTOCOL_PREFIX}\t{PROTOCOL_VERSION}\t{_protocol_command}\t{encoded}\n"
    )
    sys.stdout.flush()


def fail(error: str, **data) -> int:
    emit(False, error=error, **data)
    return 1


def ok(**data) -> int:
    emit(True, **data)
    return 0
