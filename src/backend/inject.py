# -*- coding: utf-8 -*-
"""Load a payload DLL into a running target via a remote LoadLibraryW thread.

This is the plumbing that gets a payload into the target; the payload's hooks
are out of scope here.

Timing: injection runs *after* the target is resumed, not while suspended. A
freshly CREATE_SUSPENDED process has no modules mapped yet — not even kernel32 —
so LoadLibraryW does not exist to call remotely. We wait for the loader to map
kernel32, then inject; the payload's hooks land during the target's own startup,
before it reads meaningful input.

Address resolution is uniform across bitness. We read the target's own kernel32
base from its module list and add LoadLibraryW's RVA parsed from the matching
on-disk kernel32 (System32 for 64-bit, SysWOW64 for 32-bit). This makes a 64-bit
backend injecting a 32-bit target — the common galgame case — take the same path
as same-bitness, without assuming a shared ASLR base. LoadLibraryW is a real
(non-forwarded) export in both kernel32 flavors on supported Windows; a forwarder
would need an extra hop, so we fail closed on that rather than call a wrong
address and crash the target.
"""
from __future__ import annotations

import ctypes
import os
import struct
import time
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_MEM_COMMIT_RESERVE = 0x00003000
_MEM_RELEASE = 0x00008000
_PAGE_READWRITE = 0x04
_WAIT_OBJECT_0 = 0
_REMOTE_THREAD_TIMEOUT_MS = 10_000
_TH32CS_SNAPMODULE = 0x8
_TH32CS_SNAPMODULE32 = 0x10
_ERROR_BAD_LENGTH = 299
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# kernel32 is mapped within a few hundred ms of resume in practice; this ceiling
# only bounds a pathological startup so a stuck target degrades to message mode
# instead of blocking the spawn forever.
INIT_WAIT_SECONDS = 3.0


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


kernel32.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
kernel32.IsWow64Process.restype = wintypes.BOOL
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
kernel32.Module32FirstW.restype = wintypes.BOOL
kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
kernel32.Module32NextW.restype = wintypes.BOOL
kernel32.VirtualAllocEx.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    ctypes.c_size_t,
    wintypes.DWORD,
    wintypes.DWORD,
]
kernel32.VirtualAllocEx.restype = wintypes.LPVOID
kernel32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.LPCVOID,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.WriteProcessMemory.restype = wintypes.BOOL
kernel32.VirtualFreeEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]
kernel32.VirtualFreeEx.restype = wintypes.BOOL
kernel32.CreateRemoteThread.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    ctypes.c_size_t,
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.CreateRemoteThread.restype = wintypes.HANDLE
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeThread.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


class InjectionError(RuntimeError):
    """Injection could not complete; the caller should degrade to message mode."""


def host_bitness() -> int:
    return 8 * struct.calcsize("P")


def target_bitness(process_handle: int) -> int:
    """Bitness of the target as this backend can see it (WOW64 => 32)."""
    if host_bitness() == 32:
        return 32
    wow64 = wintypes.BOOL()
    if not kernel32.IsWow64Process(wintypes.HANDLE(process_handle), ctypes.byref(wow64)):
        raise InjectionError(f"IsWow64Process failed: {ctypes.get_last_error()}")
    return 32 if wow64.value else 64


def select_payload(bits: int, dll32: str | None, dll64: str | None) -> str | None:
    return dll32 if bits == 32 else dll64


def module_base(pid: int, module_name: str) -> int | None:
    """Base address of module_name in the target, or None if not mapped.

    Snapshots both native and WOW64 module lists so a 64-bit backend can see a
    32-bit target's kernel32. Toolhelp reports ERROR_BAD_LENGTH while the module
    list is mutating; that is transient, so we retry rather than fail.
    """
    target = module_name.lower()
    snap_val = 0
    for _ in range(100):
        snap = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPMODULE | _TH32CS_SNAPMODULE32, pid)
        snap_val = 0 if snap is None else int(snap)
        if snap_val and snap_val != _INVALID_HANDLE_VALUE:
            break
        if ctypes.get_last_error() == _ERROR_BAD_LENGTH:
            time.sleep(0.01)
            continue
        return None
    else:
        return None
    handle = wintypes.HANDLE(snap_val)
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
        if not kernel32.Module32FirstW(handle, ctypes.byref(entry)):
            return None
        while True:
            if entry.szModule.lower() == target:
                return ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value
            if not kernel32.Module32NextW(handle, ctypes.byref(entry)):
                return None
    finally:
        kernel32.CloseHandle(handle)


def wait_until_initialized(pid: int, timeout_s: float = INIT_WAIT_SECONDS) -> bool:
    """Block until the target's loader has mapped kernel32, bounded by timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if module_base(pid, "kernel32.dll") is not None:
            return True
        time.sleep(0.02)
    return False


def _kernel32_path(bits: int) -> str:
    windir = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return os.path.join(windir, "System32" if bits == 64 else "SysWOW64", "kernel32.dll")


def export_rva(dll_path: str, func_name: str) -> int:
    """Parse a PE export directory on disk and return func_name's RVA.

    Reads the file (not the mapped image), so RVAs are translated to file offsets
    through the section table. Raises InjectionError if the export is missing or a
    forwarder, so the caller degrades to message mode rather than jumping to a
    bogus address.
    """
    with open(dll_path, "rb") as handle:
        data = handle.read()

    def u16(offset: int) -> int:
        return struct.unpack_from("<H", data, offset)[0]

    def u32(offset: int) -> int:
        return struct.unpack_from("<I", data, offset)[0]

    def cstr(offset: int) -> bytes:
        return data[offset:data.index(b"\x00", offset)]

    pe = u32(0x3C)
    if data[pe:pe + 4] != b"PE\x00\x00":
        raise InjectionError(f"{dll_path}: not a PE image")
    coff = pe + 4
    section_count = u16(coff + 2)
    optional_size = u16(coff + 16)
    optional = coff + 20
    magic = u16(optional)
    directories = optional + (96 if magic == 0x10B else 112)  # data directory[0] = export
    export_rva = u32(directories)
    export_size = u32(directories + 4)

    sections = []
    section_table = optional + optional_size
    for index in range(section_count):
        base = section_table + index * 40
        virtual_addr = u32(base + 12)
        span = max(u32(base + 8), u32(base + 16))  # max(VirtualSize, SizeOfRawData)
        raw_ptr = u32(base + 20)
        sections.append((virtual_addr, span, raw_ptr))

    def to_offset(rva: int) -> int:
        for virtual_addr, span, raw_ptr in sections:
            if virtual_addr <= rva < virtual_addr + span:
                return raw_ptr + (rva - virtual_addr)
        raise InjectionError(f"{dll_path}: rva {rva:#x} maps to no section")

    export = to_offset(export_rva)
    name_count = u32(export + 0x18)
    functions = to_offset(u32(export + 0x1C))
    names = to_offset(u32(export + 0x20))
    ordinals = to_offset(u32(export + 0x24))
    wanted = func_name.encode()
    for index in range(name_count):
        if cstr(to_offset(u32(names + index * 4))) == wanted:
            ordinal = u16(ordinals + index * 2)
            func_rva = u32(functions + ordinal * 4)
            if export_rva <= func_rva < export_rva + export_size:
                raise InjectionError(f"{func_name} is a forwarded export in {dll_path}")
            return func_rva
    raise InjectionError(f"{func_name} not exported by {dll_path}")


def _resolve_loadlibrary(pid: int, bits: int) -> int:
    base = module_base(pid, "kernel32.dll")
    if base is None:
        raise InjectionError("kernel32 not present in target module list")
    return base + export_rva(_kernel32_path(bits), "LoadLibraryW")


def _remote_load_library(process_handle: int, load_addr: int, dll_path: str, target_bits: int) -> None:
    encoded = dll_path.encode("utf-16-le") + b"\x00\x00"
    remote = kernel32.VirtualAllocEx(
        wintypes.HANDLE(process_handle), None, len(encoded), _MEM_COMMIT_RESERVE, _PAGE_READWRITE
    )
    if not remote:
        raise InjectionError(f"VirtualAllocEx failed: {ctypes.get_last_error()}")
    thread = None
    try:
        written = ctypes.c_size_t(0)
        if not kernel32.WriteProcessMemory(
            wintypes.HANDLE(process_handle),
            remote,
            encoded,
            len(encoded),
            ctypes.byref(written),
        ) or written.value != len(encoded):
            raise InjectionError(f"WriteProcessMemory failed: {ctypes.get_last_error()}")
        thread = kernel32.CreateRemoteThread(
            wintypes.HANDLE(process_handle),
            None,
            0,
            ctypes.c_void_p(load_addr),
            remote,
            0,
            None,
        )
        if not thread:
            raise InjectionError(f"CreateRemoteThread failed: {ctypes.get_last_error()}")
        if kernel32.WaitForSingleObject(thread, _REMOTE_THREAD_TIMEOUT_MS) != _WAIT_OBJECT_0:
            raise InjectionError("remote LoadLibraryW thread did not finish in time")
        # The exit code is a truncated HMODULE, so the pipe handshake is the
        # authoritative proof of a live payload. This only catches a hard load
        # refusal (0) on a 32-bit target, where the value is not truncated.
        code = wintypes.DWORD(0)
        kernel32.GetExitCodeThread(thread, ctypes.byref(code))
        if target_bits == 32 and code.value == 0:
            raise InjectionError("remote LoadLibraryW returned NULL (payload failed to load)")
    finally:
        if thread:
            kernel32.CloseHandle(thread)
        kernel32.VirtualFreeEx(wintypes.HANDLE(process_handle), remote, 0, _MEM_RELEASE)


def inject_payload(process_handle: int, pid: int, dll32: str | None, dll64: str | None) -> str:
    """Choose the payload for the target's bitness and load it. Returns its path.

    The target must already be resumed; this waits for its loader to map kernel32
    before injecting.
    """
    bits = target_bitness(process_handle)
    dll_path = select_payload(bits, dll32, dll64)
    if not dll_path:
        raise InjectionError(f"no payload configured for {bits}-bit target")
    if not wait_until_initialized(pid):
        raise InjectionError("target did not map kernel32 before the initialization deadline")
    load_addr = _resolve_loadlibrary(pid, bits)
    _remote_load_library(process_handle, load_addr, dll_path, bits)
    return dll_path
