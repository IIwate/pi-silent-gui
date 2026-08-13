# -*- coding: utf-8 -*-
"""Windows Job Object tracking and terminating the complete target process tree."""
from __future__ import annotations

import ctypes
import hashlib
import re
import uuid
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

JOB_OBJECT_QUERY = 0x0004
JOB_OBJECT_TERMINATE = 0x0008
JOB_OBJECT_SET_ATTRIBUTES = 0x0002
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
ERROR_FILE_NOT_FOUND = 2
ERROR_MORE_DATA = 234
ERROR_ALREADY_EXISTS = 183
_CLEANUP_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenJobObjectW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
]
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.QueryInformationJobObject.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryInformationJobObject.restype = wintypes.BOOL
kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateJobObject.restype = wintypes.BOOL


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def close_job(handle: int | None) -> None:
    if handle:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def cleanup_job_name(session_id: str, cleanup_token_hash: str) -> str:
    if not _CLEANUP_HASH_RE.fullmatch(cleanup_token_hash):
        raise ValueError("cleanup token hash must be 64 lowercase hexadecimal characters")
    suffix = hashlib.sha256(
        f"{cleanup_token_hash}:job:{session_id}".encode("ascii")
    ).hexdigest()[:32]
    return f"pi_silent_job_{session_id}_{suffix}"


def create_job(session_id: str, cleanup_token_hash: str | None = None) -> tuple[str, int]:
    names = (
        [cleanup_job_name(session_id, cleanup_token_hash)]
        if cleanup_token_hash is not None
        else [f"pi_silent_job_{session_id}_{uuid.uuid4().hex}" for _ in range(4)]
    )
    for name in names:
        ctypes.set_last_error(0)
        handle = kernel32.CreateJobObjectW(None, name)
        if not handle:
            raise OSError(f"CreateJobObject failed: {ctypes.get_last_error()}")
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            close_job(int(handle))
            if cleanup_token_hash is not None:
                raise RuntimeError("cleanup-bound Job Object name already exists")
            continue
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            err = ctypes.get_last_error()
            close_job(int(handle))
            raise OSError(f"SetInformationJobObject failed: {err}")
        return name, int(handle)
    raise RuntimeError("failed to allocate unique Job Object name")


def open_job(name: str) -> int | None:
    handle = kernel32.OpenJobObjectW(JOB_OBJECT_QUERY | JOB_OBJECT_TERMINATE, False, name)
    if handle:
        return int(handle)
    if ctypes.get_last_error() == ERROR_FILE_NOT_FOUND:
        return None
    raise OSError(f"OpenJobObject failed: {ctypes.get_last_error()} name={name!r}")


def _pid_list_struct(capacity: int) -> type[ctypes.Structure]:
    class PID_LIST(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * capacity),
        ]

    return PID_LIST


_PID_LISTS = tuple(_pid_list_struct(capacity) for capacity in (32, 128, 512, 2048, 8192))


def query_job_pids(job_handle: int) -> list[int]:
    for pid_list in _PID_LISTS:
        data = pid_list()
        returned = wintypes.DWORD()
        if kernel32.QueryInformationJobObject(
            ctypes.c_void_p(job_handle),
            JOB_OBJECT_BASIC_PROCESS_ID_LIST,
            ctypes.byref(data),
            ctypes.sizeof(data),
            ctypes.byref(returned),
        ):
            return [int(data.ProcessIdList[i]) for i in range(int(data.NumberOfProcessIdsInList))]
        if ctypes.get_last_error() != ERROR_MORE_DATA:
            raise OSError(f"QueryInformationJobObject failed: {ctypes.get_last_error()}")
    raise RuntimeError("Job Object process list exceeds 8192 entries")


def query_named_job_pids(name: str) -> list[int]:
    handle = open_job(name)
    if handle is None:
        return []
    try:
        return query_job_pids(handle)
    finally:
        close_job(handle)


def terminate_job(job_handle: int, exit_code: int = 1) -> None:
    if not kernel32.TerminateJobObject(ctypes.c_void_p(job_handle), exit_code):
        raise OSError(f"TerminateJobObject failed: {ctypes.get_last_error()}")
