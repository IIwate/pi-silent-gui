# -*- coding: utf-8 -*-
"""Session-scoped named pipe that receives the payload's one-line handshake.

The handshake is the backend's only proof that hooks are live: the payload
connects and writes one JSON line after installing them. A missing or malformed
line degrades the session to message mode, so this server must always settle
within the caller's deadline.

Overlapped I/O rather than a worker thread: a synchronous ConnectNamedPipe
cannot be reliably cancelled from another thread and can even wedge CloseHandle,
so the deadline is enforced in-band with an event wait plus CancelIoEx.
"""
from __future__ import annotations

import ctypes
import json
import time
from ctypes import wintypes

PROTO = "pi-silent-input"
VERSION = 1
_MAX_HELLO_BYTES = 8192

_PIPE_ACCESS_INBOUND = 0x00000001
_FILE_FLAG_OVERLAPPED = 0x40000000
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_ERROR_IO_PENDING = 997
_ERROR_PIPE_CONNECTED = 535
_ERROR_BROKEN_PIPE = 109
_WAIT_OBJECT_0 = 0
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
]
kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]
kernel32.ConnectNamedPipe.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(_OVERLAPPED),
]
kernel32.ReadFile.restype = wintypes.BOOL
kernel32.GetOverlappedResult.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_OVERLAPPED),
    ctypes.POINTER(wintypes.DWORD),
    wintypes.BOOL,
]
kernel32.GetOverlappedResult.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]
kernel32.CancelIoEx.restype = wintypes.BOOL
kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
kernel32.ResetEvent.restype = wintypes.BOOL
kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def _validate_hello(raw: bytes) -> dict:
    line = raw.split(b"\n", 1)[0]
    payload = json.loads(line.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("handshake is not a JSON object")
    if payload.get("proto") != PROTO or payload.get("version") != VERSION:
        raise ValueError("handshake proto/version mismatch")
    if not isinstance(payload.get("ok"), bool):
        raise ValueError("handshake ok flag missing")
    return payload


class HandshakeServer:
    """Create the pipe and begin an async connect so the payload can connect at once."""

    def __init__(self, name: str):
        self._name = name
        self._result: dict | None = None
        self._error: str | None = None
        self._closed = False
        self._event = kernel32.CreateEventW(None, True, False, None)
        if not self._event:
            raise OSError(f"CreateEvent failed: {ctypes.get_last_error()}")
        self._ov = _OVERLAPPED()
        self._ov.hEvent = self._event
        handle = kernel32.CreateNamedPipeW(
            name,
            _PIPE_ACCESS_INBOUND | _FILE_FLAG_OVERLAPPED,
            _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT,
            1,
            _MAX_HELLO_BYTES,
            _MAX_HELLO_BYTES,
            0,
            None,
        )
        if not handle or int(handle) == _INVALID_HANDLE_VALUE:
            kernel32.CloseHandle(self._event)
            raise OSError(f"CreateNamedPipe({name!r}) failed: {ctypes.get_last_error()}")
        self._handle = int(handle)
        self._connect_pending = False
        ctypes.set_last_error(0)
        if kernel32.ConnectNamedPipe(wintypes.HANDLE(self._handle), ctypes.byref(self._ov)):
            self._connect_pending = False  # connected synchronously (unusual)
        else:
            err = ctypes.get_last_error()
            if err == _ERROR_IO_PENDING:
                self._connect_pending = True
            elif err != _ERROR_PIPE_CONNECTED:
                self.close()
                raise OSError(f"ConnectNamedPipe failed: {err}")

    def _wait_event(self, deadline: float) -> bool:
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        return kernel32.WaitForSingleObject(self._event, remaining_ms) == _WAIT_OBJECT_0

    def wait(self, timeout_s: float) -> dict | None:
        """Return the validated handshake, or None on timeout/failure."""
        deadline = time.monotonic() + timeout_s
        transferred = wintypes.DWORD(0)
        buffer = ctypes.create_string_buffer(_MAX_HELLO_BYTES)
        chunks = bytearray()
        try:
            if self._connect_pending:
                if not self._wait_event(deadline):
                    kernel32.CancelIoEx(wintypes.HANDLE(self._handle), None)
                    return None
                if not kernel32.GetOverlappedResult(
                    wintypes.HANDLE(self._handle), ctypes.byref(self._ov), ctypes.byref(transferred), False
                ):
                    raise OSError(f"connect completion failed: {ctypes.get_last_error()}")
            while b"\n" not in chunks and len(chunks) < _MAX_HELLO_BYTES:
                kernel32.ResetEvent(self._event)
                transferred = wintypes.DWORD(0)
                ctypes.set_last_error(0)
                if kernel32.ReadFile(
                    wintypes.HANDLE(self._handle),
                    buffer,
                    _MAX_HELLO_BYTES,
                    ctypes.byref(transferred),
                    ctypes.byref(self._ov),
                ):
                    count = transferred.value
                else:
                    err = ctypes.get_last_error()
                    if err == _ERROR_IO_PENDING:
                        if not self._wait_event(deadline):
                            kernel32.CancelIoEx(wintypes.HANDLE(self._handle), None)
                            return None
                        if not kernel32.GetOverlappedResult(
                            wintypes.HANDLE(self._handle),
                            ctypes.byref(self._ov),
                            ctypes.byref(transferred),
                            False,
                        ):
                            break  # cancelled or broken; degrade
                        count = transferred.value
                    elif err == _ERROR_BROKEN_PIPE:
                        break
                    else:
                        raise OSError(f"ReadFile failed: {err}")
                if count == 0:
                    break
                chunks.extend(buffer.raw[: count])
            self._result = _validate_hello(bytes(chunks))
        except Exception as error:  # any bad handshake degrades, never crashes the broker
            self._error = str(error)
        return self._result

    def error(self) -> str | None:
        return self._error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            kernel32.CancelIoEx(wintypes.HANDLE(self._handle), None)
            kernel32.DisconnectNamedPipe(wintypes.HANDLE(self._handle))
        except Exception:
            pass
        kernel32.CloseHandle(wintypes.HANDLE(self._handle))
        kernel32.CloseHandle(self._event)
