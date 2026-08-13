# -*- coding: utf-8 -*-
"""Persistent per-session broker owning the private desktop, Job, and audio guard."""
from __future__ import annotations

import os
import threading
import time

from common import (
    remove_session_tmp,
    session_state_path,
    session_tmp,
    write_json_atomic,
)
from desktop import (
    close_desktop_handle,
    close_process_handle,
    create_private_desktop,
    resume_thread,
    spawn_suspended_on_desktop,
    terminate_process_handle,
)
from job import close_job, create_job, query_job_pids, terminate_job
from process import (
    close_handle,
    open_process_watch,
    process_creation_time,
    process_creation_time_from_handle,
    process_handle_alive,
    process_is_elevated_from_handle,
)


def _check_audio_guard(guard, *, require_ready: bool = False) -> None:
    fatal = guard.fatal_error()
    if fatal:
        raise RuntimeError(fatal)
    if require_ready and not guard.is_ready():
        raise RuntimeError("audio guard is not ready; refusing to resume target")


def _close_audio_guard(guard, timeout: float = 3.0) -> None:
    done = threading.Event()

    def enforce_deadline() -> None:
        if not done.wait(timeout):
            # COM teardown has no reliable cancellation path. The broker is disposable;
            # process death closes the Job and desktop when that last call never returns.
            os._exit(3)

    watchdog = threading.Thread(
        target=enforce_deadline,
        name="pi-silent-gui-audio-close-watchdog",
        daemon=True,
    )
    watchdog.start()
    try:
        guard.close()
    finally:
        done.set()
        watchdog.join(timeout=max(0.1, timeout))


def _open_owner_watch(params: dict) -> int:
    """Bind the GUI session to the exact Pi process that created it.

    Normal shutdown uses session_shutdown; crashes require the broker to observe owner death.
    Missing, stale, or mismatched identities fail closed to prevent ownerless resources.
    """
    pid = params.get("owner_pid")
    created = params.get("owner_created")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RuntimeError("owner_pid required")
    if not isinstance(created, str) or not created.isdecimal() or int(created) <= 0:
        raise RuntimeError("owner_created required")
    handle = open_process_watch(pid, int(created))
    if handle is None:
        raise RuntimeError(f"owner process identity is not live: pid={pid}")
    return handle


def run_broker(params: dict) -> int:
    session_id = str(params["session_id"])
    tmp = session_tmp(session_id)
    state_path = session_state_path(session_id)
    stop_path = tmp / "stop"
    desktop = ""
    desktop_handle: int | None = None
    job_name = ""
    job_handle: int | None = None
    process_handle: int | None = None
    thread_handle: int | None = None
    assigned = False
    guard = None
    error: Exception | None = None
    cleanup_errors: list[str] = []
    state: dict = {
        "status": "starting",
        "session_id": session_id,
        "broker_pid": os.getpid(),
        "broker_created": str(process_creation_time(os.getpid()) or ""),
        "owner_pid": params.get("owner_pid"),
        "owner_created": str(params.get("owner_created") or ""),
        "cleanup_token_hash": str(params.get("cleanup_token_hash") or ""),
    }

    owner_handle: int | None = None
    owner_lost = False

    try:
        owner_handle = _open_owner_watch(params)
        allow_elevated = params.get("allow_elevated") is True
        audio_device_policy = str(params.get("audio_device_policy") or "dynamic")
        clean_env = params.get("clean_env", True) is True
        target_cwd = str(params.get("cwd") or os.getcwd())
        desktop, desktop_handle = create_private_desktop(
            session_id, allow_elevated=allow_elevated
        )
        job_name, job_handle = create_job(
            session_id,
            str(params.get("cleanup_token_hash") or ""),
        )
        pid, process_handle, thread_handle = spawn_suspended_on_desktop(
            str(params["exe"]),
            desktop,
            job_handle,
            cwd=target_cwd,
            args=params.get("args") or [],
            env=params.get("env"),
            clean_env=clean_env,
            allow_elevated=allow_elevated,
        )
        root_created = process_creation_time_from_handle(process_handle)
        state.update(
            {
                "pid": pid,
                "root_created": str(root_created or ""),
                "desktop": desktop,
                "job_name": job_name,
                "tmp_dir": str(tmp),
            }
        )

        # JOB_LIST membership must exist before CreateProcess returns or the target stays suspended.
        if pid not in query_job_pids(job_handle):
            raise RuntimeError("atomic Job membership verification failed")
        assigned = True
        target_elevated = process_is_elevated_from_handle(process_handle)
        if root_created is None or target_elevated is None:
            raise RuntimeError("cannot verify root process identity/token")
        if target_elevated != allow_elevated:
            raise RuntimeError(
                f"target elevation mismatch: requested={allow_elevated} actual={target_elevated}"
            )

        from audio import AudioGuard

        guard = AudioGuard(
            lambda: query_job_pids(job_handle),
            policy=audio_device_policy,
        )
        guard.start()
        guard.arm()
        guard.sweep()
        _check_audio_guard(guard)
        if guard.has_pending_notification():
            guard.sweep()
            _check_audio_guard(guard)
        _check_audio_guard(guard, require_ready=True)
        resume_thread(thread_handle)
        _check_audio_guard(guard)
        guard.sweep()
        _check_audio_guard(guard)
        close_process_handle(thread_handle)
        thread_handle = None
        close_process_handle(process_handle)
        process_handle = None
        guard.sweep()
        _check_audio_guard(guard)

        state.update(
            {
                "status": "ready",
                "target_elevated": target_elevated,
                "mute": "ok",
                "audio_guard": "all-active-render-endpoints",
                "audio_device_policy": audio_device_policy,
                "clean_env": clean_env,
                "cwd": target_cwd,
                "owner_pid": int(params["owner_pid"]),
                "owner_created": str(params["owner_created"]),
            }
        )
        write_json_atomic(state_path, state)

        started = time.monotonic()
        while True:
            pids = query_job_pids(job_handle)
            # The broker remains the owner's last witness after a natural target exit.
            # Leaving early would strand state if Pi dies before its shutdown hook runs.
            if not process_handle_alive(owner_handle):
                owner_lost = True
                if pids:
                    terminate_job(job_handle)
                    continue
                break
            if stop_path.exists():
                if pids:
                    terminate_job(job_handle)
                    continue
                break
            if not pids:
                time.sleep(0.2)
                continue
            _check_audio_guard(guard)
            guard.sweep()
            _check_audio_guard(guard)
            guard.wait(0.05 if time.monotonic() - started < 3.0 else 0.2)
    except Exception as caught:
        error = caught
    finally:
        if job_handle and assigned:
            try:
                if query_job_pids(job_handle):
                    terminate_job(job_handle)
            except Exception as cleanup_error:
                cleanup_errors.append(f"Job cleanup failed: {cleanup_error}")
        if process_handle and not assigned:
            try:
                terminate_process_handle(process_handle)
            except Exception as cleanup_error:
                cleanup_errors.append(f"unassigned process cleanup failed: {cleanup_error}")
        if guard is not None:
            _close_audio_guard(guard)
        close_process_handle(thread_handle)
        close_process_handle(process_handle)
        close_job(job_handle)
        close_desktop_handle(desktop_handle)
        close_handle(owner_handle)

    if owner_lost and error is None and not cleanup_errors:
        # No launcher can consume state after owner death, so the broker removes its own directory.
        try:
            remove_session_tmp(session_id)
        except Exception as cleanup_error:
            cleanup_errors.append(f"owner-loss temp cleanup failed: {cleanup_error}")

    if error is not None or cleanup_errors:
        state.update(
            {
                "status": "error",
                "error": str(error) if error is not None else "broker cleanup failed",
                "cleanup_failed": bool(cleanup_errors),
                "cleanup_errors": cleanup_errors,
                "assigned_to_job": assigned,
                "desktop": desktop,
                "job_name": job_name,
            }
        )
        try:
            write_json_atomic(state_path, state)
        except Exception:
            pass
        return 2
    return 0
