# -*- coding: utf-8 -*-
"""pi-silent-gui JSON CLI for spawn/message/capture/kill and the persistent broker."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    create_session_tmp,
    fail,
    is_windows,
    ok,
    read_json,
    remove_session_tmp,
    session_state_path,
    session_tmp,
    set_protocol_command,
    validate_session_id,
    write_text_atomic,
)
from job import (  # noqa: E402
    cleanup_job_name,
    close_job,
    open_job,
    query_job_pids,
    query_named_job_pids,
    terminate_job,
)
from process import (  # noqa: E402
    close_handle,
    is_alive,
    open_process_watch,
    process_creation_time,
    process_exit_code,
    process_handle_alive,
    process_identity_status,
    process_is_elevated,
    terminate_same_process,
)

_JOB_NAME_RE = re.compile(r"^pi_silent_job_([0-9a-f]{12})_[0-9a-f]{32}$")
_DESKTOP_NAME_RE = re.compile(r"^pi_silent_([0-9a-f]{12})_[0-9a-f]{32}$")
_CLEANUP_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
BROKER_READY_TIMEOUT_SECONDS = 10.0
# Injection adds latency the message path never pays: after resume the broker waits
# for the target loader to map kernel32, then for the payload handshake. Extend the
# ready deadline only when injection is configured, so a legitimately-injecting
# broker is not declared timed-out mid-handshake.
INJECT_READY_HEADROOM_SECONDS = 8.0
MAX_JSON_BYTES = 1024 * 1024
WINDOW_WAIT_SECONDS = 5.0
WINDOW_WAIT_POLL_SECONDS = 0.25
WAIT_TIMEOUT_MS_DEFAULT = 10_000
WAIT_TIMEOUT_MS_MIN = 100
WAIT_TIMEOUT_MS_MAX = 60_000
REPEAT_COUNT_DEFAULT = 1
REPEAT_COUNT_MAX = 50
REPEAT_INTERVAL_MS_DEFAULT = 300
REPEAT_INTERVAL_MS_MIN = 1
REPEAT_INTERVAL_MS_MAX = 2_000
HOLD_MS_MIN = 1
HOLD_MS_MAX = 10_000
_REGISTERED_IDENTITY_KEYS = (
    "_job_name",
    "_broker_pid",
    "_broker_created",
    "_root_pid",
    "_root_created",
)


def _load_json(raw: str) -> dict:
    data = json.loads(raw) if raw else {}
    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    return data


def _read_stdin_json(limit: int = MAX_JSON_BYTES) -> str:
    # stdin is the only request transport; argv once held secrets and length bombs.
    # Read one byte past the ceiling so oversize is a hard refuse, not a silent trim.
    raw = sys.stdin.buffer.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"stdin JSON exceeds {limit} bytes")
    return raw.decode("utf-8")


def _optional_vk(value, label: str = "vk") -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255:
        raise ValueError(f"{label} must be an integer 1..255")
    return value


def _optional_expected_hwnd(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("expected_hwnd must be a positive integer")
    return value


def _is_utf8_text(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return "\0" not in value


def _casefold_overlay(base: dict[str, str], overrides: dict[str, str]) -> dict[str, str]:
    merged = {key.casefold(): (key, value) for key, value in base.items()}
    for key, value in overrides.items():
        merged[key.casefold()] = (key, value)
    return {key: value for key, value in merged.values()}


def _broker_environment_overrides() -> dict[str, str]:
    result = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    # Fault-injection variables are test-only protocol inputs. Production variables,
    # credentials, and extension state never cross into the persistent broker.
    result.update(
        (key, value)
        for key, value in os.environ.items()
        if key.startswith("PI_SILENT_GUI_TEST_")
    )
    return result


def _optional_title(params: dict) -> str | None:
    title = params.get("title")
    if title is None:
        title = params.get("title_contains")
    if title == "":
        return None
    if title is not None and not isinstance(title, str):
        raise ValueError("title must be a string")
    return title


def _window_filters(params: dict, *, with_hwnd: bool = False) -> dict:
    window_class = params.get("window_class")
    if window_class == "":
        window_class = None
    filters: dict = {
        "window_class": window_class,
        "title_contains": _optional_title(params),
    }
    if with_hwnd:
        raw = params.get("hwnd")
        if raw is None:
            raw = params.get("expected_hwnd")
        expected = _optional_expected_hwnd(raw)
        if expected is not None:
            filters["expected_hwnd"] = expected
    return filters


class SessionObserveError(LookupError):
    """Wait or dispatch ended without a usable window; snapshot is the Session as last seen."""

    def __init__(self, message: str, snapshot: dict):
        super().__init__(message)
        self.snapshot = snapshot


def _snapshot_public(snapshot: dict) -> dict:
    return {
        "alive": bool(snapshot.get("alive")),
        "exit_code": snapshot.get("exit_code"),
        "windows": list(snapshot.get("windows") or []),
    }


def _session_snapshot(
    job_name: str,
    desktop: str,
    *,
    root_pid: int | None = None,
    root_created: int | None = None,
) -> dict:
    from desktop_ctx import on_desktop, operation_dpi_awareness
    from window import list_top_windows

    live_pids = [pid for pid in query_named_job_pids(job_name) if is_alive(pid)]
    windows: list[dict] = []
    if live_pids:
        with on_desktop(desktop), operation_dpi_awareness():
            for pid in live_pids:
                windows.extend(list_top_windows(pid))
        windows.sort(key=lambda item: (-int(item.get("area") or 0), int(item["hwnd"])))
    return {
        "alive": bool(live_pids),
        "exit_code": process_exit_code(root_pid, root_created) if root_pid else None,
        "windows": [_window_public_fields(item) for item in windows],
        "pids": live_pids,
    }


def _root_identity(params: dict) -> tuple[int | None, int | None]:
    pid = _positive_identity(params.get("_root_pid"))
    created = _positive_identity(params.get("_root_created"))
    return (pid or None, created or None)


def _wait_for_session_window(
    job_name: str,
    desktop: str,
    *,
    timeout_seconds: float = WINDOW_WAIT_SECONDS,
    root_pid: int | None = None,
    root_created: int | None = None,
    **filters,
) -> tuple[dict, dict]:
    """Poll Session state until a matching Window exists, the Session ends, or time runs out.

    没有活进程时匹配条件不可能再成立，所以立刻停，而不是空转到超时。
    列表是当时全部顶层窗；选中的那一个仍按筛选后面积最大。
    """
    from desktop_ctx import on_desktop, operation_dpi_awareness
    from window import find_window_in_pids

    deadline = time.monotonic() + timeout_seconds
    last_lookup: LookupError | None = None
    snapshot = _session_snapshot(
        job_name, desktop, root_pid=root_pid, root_created=root_created
    )
    while True:
        if not snapshot["alive"]:
            raise SessionObserveError("session has no live process", snapshot)
        try:
            with on_desktop(desktop), operation_dpi_awareness():
                window = find_window_in_pids(snapshot["pids"], **filters)
            return window, snapshot
        except LookupError as error:
            last_lookup = error
            # 调用方钉死的 HWND 已经不是窗口，再轮询也不会变回同一个身份。
            if filters.get("expected_hwnd") is not None and "is not a valid window" in str(error):
                raise SessionObserveError(str(error), snapshot) from error
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(WINDOW_WAIT_POLL_SECONDS, remaining))
            snapshot = _session_snapshot(
                job_name, desktop, root_pid=root_pid, root_created=root_created
            )
    detail = str(last_lookup) if last_lookup else "no window"
    raise SessionObserveError(
        f"window not found within {timeout_seconds:g}s "
        f"class={filters.get('window_class')!r} "
        f"title~={filters.get('title_contains')!r} "
        f"hwnd={filters.get('expected_hwnd')}: {detail}",
        snapshot,
    )


def _window_public_fields(window: dict) -> dict:
    return {
        "hwnd": int(window["hwnd"]),
        "title": window.get("title") or "",
        "window_class": window.get("class") or "",
        "width": int(window["width"]),
        "height": int(window["height"]),
    }


def _state(session_id: str) -> dict:
    sid = validate_session_id(session_id)
    path = session_state_path(sid, create=False)
    if not path.is_file():
        raise LookupError(f"unknown session_id: {sid}")
    state = read_json(path)
    if state.get("session_id") != sid:
        raise ValueError("session state id mismatch")
    desktop = state.get("desktop")
    job_name = state.get("job_name")
    desktop_match = _DESKTOP_NAME_RE.fullmatch(str(desktop)) if desktop else None
    job_match = _JOB_NAME_RE.fullmatch(str(job_name)) if job_name else None
    if desktop and (desktop_match is None or desktop_match.group(1) != sid):
        raise ValueError("session desktop name is outside the private namespace")
    if job_name and (job_match is None or job_match.group(1) != sid):
        raise ValueError("session Job Object name is outside the private namespace")
    return state


def _orphan_cleanup_state(session_id: str) -> dict:
    state = _state(session_id)
    for key in (
        "desktop",
        "job_name",
        "broker_pid",
        "broker_created",
        "owner_pid",
        "owner_created",
    ):
        if state.get(key) in (None, ""):
            raise ValueError(f"session orphan cleanup state missing {key}")
    if not all(
        _positive_identity(state[key])
        for key in (
            "broker_pid",
            "broker_created",
            "owner_pid",
            "owner_created",
        )
    ):
        raise ValueError("session orphan cleanup identity is invalid")
    root_pid = _positive_identity(state.get("pid"))
    root_created = _positive_identity(state.get("root_created"))
    if bool(root_pid) != bool(root_created):
        raise ValueError("session orphan cleanup root identity is incomplete")
    return state


def _validate_cleanup_capability(job_name: str, session_id: str, cleanup_token) -> None:
    if not isinstance(cleanup_token, str) or not _CLEANUP_TOKEN_RE.fullmatch(cleanup_token):
        raise PermissionError("cleanup refused: runtime capability required")
    token_hash = hashlib.sha256(cleanup_token.encode("ascii")).hexdigest()
    expected = cleanup_job_name(session_id, token_hash)
    if not hmac.compare_digest(job_name, expected):
        raise PermissionError("cleanup refused: runtime capability mismatch")


def _validate_orphan_cleanup_owner(source: dict, caller_owner_pid, cleanup_token=None) -> str:
    recorded_pid = _positive_identity(source.get("owner_pid"))
    recorded_created = _positive_identity(source.get("owner_created"))
    caller_pid = _positive_identity(caller_owner_pid)
    if not recorded_pid or not recorded_created:
        raise ValueError("orphan cleanup state has no valid owner identity")
    if not caller_pid:
        raise ValueError("orphan cleanup caller owner_pid must be a positive integer")
    caller_created = process_creation_time(caller_pid)
    if caller_created is None:
        raise ValueError(f"cannot verify orphan cleanup caller identity: pid={caller_pid}")
    if caller_pid == recorded_pid and int(caller_created) == recorded_created:
        expected_hash = source.get("cleanup_token_hash")
        if not isinstance(expected_hash, str) or not _CLEANUP_TOKEN_RE.fullmatch(expected_hash):
            raise ValueError("orphan cleanup state has no valid runtime capability")
        if not isinstance(cleanup_token, str) or not _CLEANUP_TOKEN_RE.fullmatch(cleanup_token):
            raise PermissionError("orphan cleanup refused: runtime capability required")
        actual_hash = hashlib.sha256(cleanup_token.encode("ascii")).hexdigest()
        if not hmac.compare_digest(actual_hash, expected_hash):
            raise PermissionError("orphan cleanup refused: runtime capability mismatch")
        return "current"
    owner_status = process_identity_status(recorded_pid, recorded_created)
    if owner_status == "live":
        raise PermissionError(
            f"orphan cleanup refused: session belongs to another live Pi owner pid={recorded_pid}"
        )
    if owner_status == "unverifiable":
        raise PermissionError(
            f"orphan cleanup refused: owner identity is unverifiable pid={recorded_pid}"
        )
    return "dead"


def _orphan_identity_status(role: str, pid: int, created: int) -> str:
    status = process_identity_status(pid, created)
    if status == "unverifiable":
        raise PermissionError(f"orphan cleanup {role} identity is unverifiable: pid={pid}")
    return status


def _stop_failed_broker(proc, created: int | None) -> bool:
    try:
        running = proc.poll() is None
    except Exception:
        running = True
    if running:
        try:
            proc.terminate()  # Popen owns the process handle; no bare-PID termination.
            proc.wait(timeout=3)
        except Exception:
            pass
    try:
        if proc.poll() is not None:
            return True
    except Exception:
        pass
    if created is None:
        return False
    return terminate_same_process(proc.pid, created) in ("killed", "gone", "mismatch")


def _failed_spawn_result(
    error,
    session_id: str,
    proc=None,
    created: int | None = None,
    state: dict | None = None,
) -> int:
    cleanup_errors = list((state or {}).get("cleanup_errors") or [])
    try:
        broker_stopped = _stop_failed_broker(proc, created) if proc is not None else True
    except Exception as cleanup_error:
        broker_stopped = False
        cleanup_errors.append(f"broker cleanup failed: {cleanup_error}")
    backend_cleanup_failed = bool((state or {}).get("cleanup_failed"))
    cleaned = broker_stopped and not backend_cleanup_failed
    if cleaned:
        try:
            remove_session_tmp(session_id)
        except Exception as cleanup_error:
            cleaned = False
            cleanup_errors.append(str(cleanup_error))
    response = {
        key: value
        for key, value in (state or {}).items()
        if key not in ("status", "error", "session_id", "cleanup_errors")
    }
    if proc is not None:
        response.setdefault("broker_pid", proc.pid)
        response.setdefault("broker_created", str(created or ""))
    _close_launcher_process(proc)
    return fail(
        str(error),
        session_id=session_id,
        cleanup_ok=cleaned,
        cleanup_errors=cleanup_errors,
        orphan_cleanup_required=not cleaned,
        **response,
    )


def _positive_identity(value) -> int:
    if isinstance(value, bool):
        return 0
    text = str(value) if value is not None else ""
    return int(text) if text.isdecimal() and int(text) > 0 else 0


def _close_launcher_process(proc) -> None:
    close = getattr(proc, "close", None)
    if close is not None:
        close()


def _start_broker_input_writer(stream, payload: bytes):
    done = threading.Event()
    errors: list[str] = []

    def write() -> None:
        try:
            stream.write(payload)
            stream.close()
        except Exception as error:
            errors.append(str(error))
            try:
                stream.close()
            except Exception:
                pass
        finally:
            done.set()

    thread = threading.Thread(target=write, name="pi-silent-gui-broker-stdin", daemon=True)
    thread.start()
    return thread, done, errors


def resolve_payload_dll(params: dict, key: str, env_name: str) -> str | None:
    """Resolve a payload DLL path with param-over-env precedence (SUPER-E).

    An explicit spawn param wins so a caller can pick a DLL per launch; the env var is
    the set-once default. Returns None when neither is set (message mode). Raises
    ValueError on a malformed param so a typo fails the spawn loudly instead of silently
    degrading to message mode.
    """
    value = params.get(key)
    if value not in (None, "") and (not isinstance(value, str) or not _is_utf8_text(value)):
        raise ValueError(f"{key} must be a valid UTF-8 string without NUL")
    return (value or os.environ.get(env_name)) or None


def cmd_spawn(params: dict) -> int:
    exe = params.get("exe")
    if not isinstance(exe, str) or not exe:
        return fail("exe required")
    if not _is_utf8_text(exe):
        return fail("exe must be valid UTF-8 without NUL")
    args = params.get("args", [])
    env = params.get("env")
    cwd = params.get("cwd")
    allow_elevated = params.get("allow_elevated", False)
    clean_env = params.get("clean_env", True)
    audio_device_policy = params.get("audio_device_policy", "dynamic")
    if not isinstance(allow_elevated, bool):
        return fail("allow_elevated must be boolean")
    if not isinstance(clean_env, bool):
        return fail("clean_env must be boolean")
    if audio_device_policy not in ("dynamic", "strict"):
        return fail("audio_device_policy must be dynamic or strict")
    if not isinstance(args, list) or not all(
        isinstance(arg, str) and _is_utf8_text(arg) for arg in args
    ):
        return fail("args must be a valid UTF-8 string list without NUL")
    if env is not None and (
        not isinstance(env, dict)
        or not all(
            isinstance(k, str)
            and bool(k)
            and "=" not in k
            and _is_utf8_text(k)
            and isinstance(v, str)
            and _is_utf8_text(v)
            for k, v in env.items()
        )
    ):
        return fail(
            "env must be a valid UTF-8 string map without '=', NUL, or empty variable names"
        )
    if cwd is None:
        cwd = os.getcwd()
    if not isinstance(cwd, str) or not cwd:
        return fail("cwd must be a non-empty string")
    if not _is_utf8_text(cwd):
        return fail("cwd must be valid UTF-8 without NUL")
    cwd = str(Path(cwd).resolve())
    if not Path(cwd).is_dir():
        return fail(f"cwd is not a directory: {cwd}")

    cleanup_token = params.get("cleanup_token")
    if cleanup_token is None:
        return fail("cleanup_token required")
    if not isinstance(cleanup_token, str) or not _CLEANUP_TOKEN_RE.fullmatch(cleanup_token):
        return fail("cleanup_token must be 64 lowercase hexadecimal characters")
    cleanup_token_hash = hashlib.sha256(cleanup_token.encode("ascii")).hexdigest()

    # The owner is the calling Pi process, not this short-lived launcher. The GUI session
    # must remain bound to Pi after the spawn command returns.
    owner_pid = params.get("owner_pid")
    if owner_pid is None:
        owner_pid = os.getppid()
    if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0:
        return fail("owner_pid must be a positive integer")
    owner_created = process_creation_time(owner_pid)
    if owner_created is None:
        return fail(f"cannot verify owner process identity: pid={owner_pid}")
    owner_watch = open_process_watch(owner_pid, owner_created)
    if owner_watch is None:
        return fail(f"owner process is not live: pid={owner_pid}")
    close_handle(owner_watch)

    current_elevated = process_is_elevated(os.getpid())
    if current_elevated is None:
        return fail("cannot verify launcher elevation")

    session_id, tmp = create_session_tmp()
    state_path = tmp / "session.json"
    script = Path(__file__).resolve()
    payload = {
        "session_id": session_id,
        "exe": exe,
        "cwd": cwd,
        "args": args,
        "env": env,
        "clean_env": clean_env,
        "audio_device_policy": audio_device_policy,
        "allow_elevated": allow_elevated,
        "owner_pid": owner_pid,
        "owner_created": str(owner_created),
        "cleanup_token_hash": cleanup_token_hash,
    }
    # Payload DLL paths follow SUPER-E precedence: explicit call param > environment
    # default. A per-spawn param lets the caller pick a DLL without setting anything
    # global. Resolved here (not in the broker) because the elevated shell-token launch
    # path strips the environment, so the broker must receive the paths in its payload.
    try:
        inject_dll32 = resolve_payload_dll(params, "inject_dll32", "PI_SILENT_GUI_INJECT_DLL32")
        inject_dll64 = resolve_payload_dll(params, "inject_dll64", "PI_SILENT_GUI_INJECT_DLL64")
    except ValueError as error:
        return fail(str(error))
    if inject_dll32:
        payload["inject_dll32"] = inject_dll32
    if inject_dll64:
        payload["inject_dll64"] = inject_dll64
    creation = 0x08000000 if sys.platform == "win32" else 0
    try:
        broker_input = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as error:
        return _failed_spawn_result(error, session_id)
    if len(broker_input) > MAX_JSON_BYTES:
        return _failed_spawn_result(
            f"broker payload exceeds {MAX_JSON_BYTES} bytes",
            session_id,
        )
    broker_args = [sys.executable, str(script), "broker", "--stdin-json"]
    broker_overrides = _broker_environment_overrides()
    ready_budget = BROKER_READY_TIMEOUT_SECONDS
    if inject_dll32 or inject_dll64:
        ready_budget += INJECT_READY_HEADROOM_SECONDS
    deadline = time.monotonic() + ready_budget
    proc = None
    input_thread = None
    input_done = None
    input_errors: list[str] = []
    try:
        if current_elevated and not allow_elevated:
            from desktop import launch_broker_with_shell_token

            proc, broker_stdin = launch_broker_with_shell_token(
                broker_args,
                cwd=str(script.parent),
                env=broker_overrides,
            )
        else:
            # clean_env=true keeps the target off this block. Explicit false is the
            # compatibility escape hatch and deliberately inherits the Pi environment.
            broker_env = _casefold_overlay(dict(os.environ), broker_overrides)
            popen_kwargs: dict = {
                "args": broker_args,
                "stdin": subprocess.PIPE,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "cwd": str(script.parent),
                "env": broker_env,
                "close_fds": True,
            }
            if creation:
                popen_kwargs["creationflags"] = creation
            proc = subprocess.Popen(**popen_kwargs)
            if proc.stdin is None:
                raise RuntimeError("broker stdin pipe was not created")
            broker_stdin = proc.stdin
        input_thread, input_done, input_errors = _start_broker_input_writer(
            broker_stdin, broker_input
        )
    except Exception as error:
        try:
            broker_created = process_creation_time(proc.pid) if proc is not None else None
        except Exception:
            broker_created = None
        if input_thread is not None:
            input_thread.join(timeout=1)
        return _failed_spawn_result(error, session_id, proc, broker_created)

    created = None
    state: dict | None = None
    try:
        while time.monotonic() < deadline:
            created = process_creation_time(proc.pid)
            if created is not None or proc.poll() is not None:
                break
            time.sleep(0.05)

        while time.monotonic() < deadline:
            if input_done.is_set() and input_errors:
                break
            if state_path.is_file():
                try:
                    state = read_json(state_path)
                except Exception:
                    state = None
                if state and state.get("status") in ("ready", "error"):
                    break
            if proc.poll() is not None:
                break
            time.sleep(0.05)
    except Exception as error:
        input_thread.join(timeout=1)
        return _failed_spawn_result(error, session_id, proc, created, state)
    deadline_expired = time.monotonic() >= deadline

    if state and state.get("status") == "ready":
        # Ready is stronger evidence than the writer thread's bookkeeping: the broker
        # parsed EOF and launched from that exact payload before publishing this state.
        input_thread.join(timeout=0.1)
        try:
            if int(state["broker_pid"]) != proc.pid:
                raise ValueError("broker PID mismatch")
            if created is None or int(state["broker_created"]) != created:
                raise ValueError("broker creation time mismatch")
            pids = query_named_job_pids(str(state["job_name"]))
            if not pids:
                raise RuntimeError("broker ready but Job Object has no process")
        except Exception as error:
            input_thread.join(timeout=1)
            return _failed_spawn_result(error, session_id, proc, created, state)
        input_thread.join(timeout=1)
        _close_launcher_process(proc)
        payload = {key: value for key, value in state.items() if key != "status"}
        if os.environ.get("PI_SILENT_GUI_TEST_ORPHAN_CLEANUP") == "1":
            # Test-only handoff: TS retains complete broker/Job identity for cleanup retries.
            return fail(
                "test orphan cleanup handoff",
                orphan_cleanup_required=True,
                **payload,
            )
        return ok(**payload)

    error = (state or {}).get("error") or (
        "broker ready timeout"
        if deadline_expired
        else (
            f"broker stdin delivery failed: {input_errors[0]}"
            if input_errors
            else f"broker exited code={proc.returncode}"
        )
    )
    input_thread.join(timeout=1)
    return _failed_spawn_result(error, session_id, proc, created, state)


def _registered_operation(params: dict) -> dict:
    session_id = params.get("session_id")
    if not session_id:
        raise ValueError("session_id required")
    sid = validate_session_id(str(session_id))
    job_name = str(params.get("_job_name") or "")
    desktop = str(params.get("_desktop") or "")
    _validate_cleanup_capability(job_name, sid, params.get("cleanup_token"))
    job_match = _JOB_NAME_RE.fullmatch(job_name)
    desktop_match = _DESKTOP_NAME_RE.fullmatch(desktop)
    if job_match is None or job_match.group(1) != sid:
        raise PermissionError("registered operation has invalid Job Object namespace")
    if desktop_match is None or desktop_match.group(1) != sid:
        raise PermissionError("registered operation has invalid desktop namespace")

    broker_pid = _positive_identity(params.get("_broker_pid"))
    broker_created = _positive_identity(params.get("_broker_created"))
    if not broker_pid or not broker_created:
        raise PermissionError("registered operation has no valid broker identity")
    broker_status = process_identity_status(broker_pid, broker_created)
    if broker_status == "unverifiable":
        raise PermissionError(
            f"registered operation broker identity is unverifiable: pid={broker_pid}"
        )
    if broker_status != "live":
        raise PermissionError(
            f"registered operation broker is not live: pid={broker_pid} status={broker_status}"
        )

    pids = query_named_job_pids(job_name)
    return {
        "session_id": sid,
        "job_name": job_name,
        "desktop": desktop,
        "pids": pids,
    }


def _coordinate(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _repeat_spec(params: dict) -> tuple[int, int]:
    # 连打只重复同一下。次数封顶，是怕模型把「跳过片头」写成空转；间隔只插在两下之间，
    # 最后一下后面再睡只会让调用方多等一拍却看不见更多变化。
    count = params.get("count", REPEAT_COUNT_DEFAULT)
    if count is None:
        count = REPEAT_COUNT_DEFAULT
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not REPEAT_COUNT_DEFAULT <= count <= REPEAT_COUNT_MAX
    ):
        raise ValueError(f"count must be an integer {REPEAT_COUNT_DEFAULT}..{REPEAT_COUNT_MAX}")
    interval_ms = params.get("interval_ms")
    if count == 1:
        if interval_ms is None:
            return 1, 0
        if (
            isinstance(interval_ms, bool)
            or not isinstance(interval_ms, int)
            or not 0 <= interval_ms <= REPEAT_INTERVAL_MS_MAX
        ):
            raise ValueError(
                f"interval_ms must be an integer 0..{REPEAT_INTERVAL_MS_MAX}"
            )
        return 1, 0
    if interval_ms is None:
        interval_ms = REPEAT_INTERVAL_MS_DEFAULT
    if (
        isinstance(interval_ms, bool)
        or not isinstance(interval_ms, int)
        or not REPEAT_INTERVAL_MS_MIN <= interval_ms <= REPEAT_INTERVAL_MS_MAX
    ):
        raise ValueError(
            f"interval_ms must be an integer {REPEAT_INTERVAL_MS_MIN}..{REPEAT_INTERVAL_MS_MAX} when count > 1"
        )
    return count, interval_ms


def cmd_wait(params: dict) -> int:
    registered = _registered_operation(params)
    session_id = registered["session_id"]
    timeout_ms = params.get("timeout_ms", WAIT_TIMEOUT_MS_DEFAULT)
    if timeout_ms is None:
        timeout_ms = WAIT_TIMEOUT_MS_DEFAULT
    if (
        isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, int)
        or not WAIT_TIMEOUT_MS_MIN <= timeout_ms <= WAIT_TIMEOUT_MS_MAX
    ):
        return fail(
            f"timeout_ms must be an integer {WAIT_TIMEOUT_MS_MIN}..{WAIT_TIMEOUT_MS_MAX}",
            session_id=session_id,
        )
    root_pid, root_created = _root_identity(params)
    try:
        window, snapshot = _wait_for_session_window(
            registered["job_name"],
            registered["desktop"],
            timeout_seconds=timeout_ms / 1000,
            root_pid=root_pid,
            root_created=root_created,
            **_window_filters(params, with_hwnd=True),
        )
    except SessionObserveError as error:
        return fail(str(error), session_id=session_id, **_snapshot_public(error.snapshot))
    return ok(
        session_id=session_id,
        desktop=registered["desktop"],
        **_window_public_fields(window),
        window=window,
        **_snapshot_public(snapshot),
    )


def _optional_hold_ms(params: dict) -> int | None:
    value = params.get("hold_ms")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not HOLD_MS_MIN <= value <= HOLD_MS_MAX:
        raise ValueError(f"hold_ms must be an integer {HOLD_MS_MIN}..{HOLD_MS_MAX}")
    return value


def _dispatch_injected(
    session_id: str,
    action: str,
    params: dict,
    window: dict,
    hwnd: int,
    job_name: str,
    desktop: str,
    root_pid: int | None,
    root_created: int | None,
) -> int:
    """Drive a polling engine by writing the session's shared input table.

    Runs on the caller's private-desktop thread, so GetWindowRect resolves the
    target's on-desktop origin. A missing armed table is reported as a real
    degradation, never swallowed: the caller asked for inject input and did not
    get it.
    """
    from input_dispatch import VK_LBUTTON, InjectDispatcher
    from window import VK_NAMES, verify_window_in_pids, window_screen_origin

    def identity_snapshot() -> dict:
        return _snapshot_public(
            _session_snapshot(job_name, desktop, root_pid=root_pid, root_created=root_created)
        )

    try:
        count, interval_ms = _repeat_spec(params)
        hold_ms = _optional_hold_ms(params)
    except ValueError as error:
        return fail(str(error), session_id=session_id)

    if action == "click":
        if "x" not in params or "y" not in params:
            return fail("click requires x,y", session_id=session_id)
        try:
            x = _coordinate(params["x"], "x")
            y = _coordinate(params["y"], "y")
        except ValueError as error:
            return fail(str(error), session_id=session_id)
        width = int(window["width"])
        height = int(window["height"])
        if x < 0 or y < 0 or x >= width or y >= height:
            return fail(
                f"click outside window: ({x},{y}) not in {width}x{height}",
                session_id=session_id,
            )
        vk = VK_LBUTTON
    else:
        has_vk = params.get("vk") is not None
        has_key = isinstance(params.get("key"), str) and bool(params.get("key"))
        if has_vk == has_key:
            return fail("key requires exactly one of vk or key", session_id=session_id)
        if has_key:
            key_name = str(params["key"]).lower()
            if key_name not in VK_NAMES:
                return fail(f"unknown key name: {params['key']}", session_id=session_id)
            vk = VK_NAMES[key_name]
        else:
            try:
                vk = _optional_vk(params.get("vk"))
            except ValueError as error:
                return fail(str(error), session_id=session_id)

    try:
        dispatcher = InjectDispatcher(session_id)
    except Exception as error:
        return fail(
            f"inject input unavailable: {error}",
            session_id=session_id,
            **identity_snapshot(),
        )

    # hold_ms is a keyboard concept (e.g. hold Ctrl to skip); a click always taps.
    key_hold = {"hold_seconds": hold_ms / 1000} if (hold_ms and action == "key") else {}
    try:
        origin = window_screen_origin(hwnd) if action == "click" else (0, 0)
        for index in range(count):
            if index:
                time.sleep(interval_ms / 1000)
                current_pids = query_named_job_pids(job_name)
                if not verify_window_in_pids(hwnd, current_pids):
                    return fail(
                        f"window identity changed before dispatch: hwnd={hwnd} pids={current_pids}",
                        session_id=session_id,
                        **identity_snapshot(),
                    )
            if action == "click":
                dispatcher.click(origin[0] + x, origin[1] + y, vk)
            else:
                dispatcher.press(vk, **key_hold)
    finally:
        dispatcher.close()

    if action == "click":
        return ok(
            session_id=session_id,
            action="click",
            x=params["x"],
            y=params["y"],
            count=count,
            window=window,
            desktop=desktop,
            input_mode="inject",
            **_window_public_fields(window),
        )
    return ok(
        session_id=session_id,
        action="key",
        key=params.get("key"),
        count=count,
        window=window,
        desktop=desktop,
        input_mode="inject",
        **_window_public_fields(window),
    )


def cmd_message(params: dict) -> int:
    from desktop_ctx import on_desktop, operation_dpi_awareness
    from window import click, key, type_text, verify_window_in_pids

    registered = _registered_operation(params)
    session_id = registered["session_id"]
    job_name = registered["job_name"]
    desktop = registered["desktop"]
    root_pid, root_created = _root_identity(params)
    try:
        window, _snapshot = _wait_for_session_window(
            registered["job_name"],
            desktop,
            root_pid=root_pid,
            root_created=root_created,
            **_window_filters(params, with_hwnd=True),
        )
    except SessionObserveError as error:
        return fail(str(error), session_id=session_id, **_snapshot_public(error.snapshot))

    hwnd = int(window["hwnd"])
    with on_desktop(desktop), operation_dpi_awareness():
        current_pids = query_named_job_pids(job_name)
        if not verify_window_in_pids(hwnd, current_pids):
            return fail(
                f"window identity changed before dispatch: hwnd={hwnd} pids={current_pids}",
                session_id=session_id,
                **_snapshot_public(
                    _session_snapshot(
                        job_name, desktop, root_pid=root_pid, root_created=root_created
                    )
                ),
            )
        input_mode = "inject" if params.get("_input_mode") == "inject" else "message"
        if params.get("hold_ms") is not None and input_mode != "inject":
            return fail("hold_ms requires an inject-mode session", session_id=session_id)
        action = params.get("action")
        if input_mode == "inject" and action in ("click", "key"):
            return _dispatch_injected(
                session_id,
                action,
                params,
                window,
                hwnd,
                job_name,
                desktop,
                root_pid,
                root_created,
            )
        if action == "click":
            if "x" not in params or "y" not in params:
                return fail("click requires x,y")
            try:
                count, interval_ms = _repeat_spec(params)
                x = _coordinate(params["x"], "x")
                y = _coordinate(params["y"], "y")
            except ValueError as error:
                return fail(str(error), session_id=session_id)
            dispatch = None
            for index in range(count):
                if index:
                    time.sleep(interval_ms / 1000)
                    current_pids = query_named_job_pids(job_name)
                    if not verify_window_in_pids(hwnd, current_pids):
                        return fail(
                            f"window identity changed before dispatch: hwnd={hwnd} pids={current_pids}",
                            session_id=session_id,
                            **_snapshot_public(
                                _session_snapshot(
                                    job_name,
                                    desktop,
                                    root_pid=root_pid,
                                    root_created=root_created,
                                )
                            ),
                        )
                dispatch = click(hwnd, x, y)
            response_window = {**window, **dispatch.pop("window")}
            return ok(
                session_id=session_id,
                action="click",
                x=params["x"],
                y=params["y"],
                count=count,
                window=response_window,
                desktop=desktop,
                **_window_public_fields(response_window),
                **dispatch,
            )
        if action == "key":
            has_vk = params.get("vk") is not None
            has_key = isinstance(params.get("key"), str) and bool(params.get("key"))
            if has_vk == has_key:
                return fail("key requires exactly one of vk or key")
            try:
                count, interval_ms = _repeat_spec(params)
            except ValueError as error:
                return fail(str(error), session_id=session_id)
            key_name = params.get("key") if has_key else None
            vk = _optional_vk(params.get("vk"))
            for index in range(count):
                if index:
                    time.sleep(interval_ms / 1000)
                    current_pids = query_named_job_pids(job_name)
                    if not verify_window_in_pids(hwnd, current_pids):
                        return fail(
                            f"window identity changed before dispatch: hwnd={hwnd} pids={current_pids}",
                            session_id=session_id,
                            **_snapshot_public(
                                _session_snapshot(
                                    job_name,
                                    desktop,
                                    root_pid=root_pid,
                                    root_created=root_created,
                                )
                            ),
                        )
                key(hwnd, vk=vk, name=key_name)
            return ok(
                session_id=session_id,
                action="key",
                key=key_name,
                count=count,
                window=window,
                desktop=desktop,
                **_window_public_fields(window),
            )
        if action == "type":
            text = params.get("text")
            if not isinstance(text, str) or not text:
                return fail("type requires text")
            typed = type_text(hwnd, text)
            return ok(
                session_id=session_id,
                action="type",
                chars=typed["chars"],
                window=window,
                desktop=desktop,
                **_window_public_fields(window),
            )
        return fail("action must be click, key, or type")


def cmd_capture(params: dict) -> int:
    from capture import print_window_png
    from desktop_ctx import on_desktop, operation_dpi_awareness
    from window import window_geometry

    registered = _registered_operation(params)
    session_id = registered["session_id"]
    desktop = registered["desktop"]
    overwrite = params.get("overwrite", False)
    if not isinstance(overwrite, bool):
        return fail("overwrite must be boolean")
    requested_out_path = params.get("out_path")
    if requested_out_path is not None and (
        not isinstance(requested_out_path, str)
        or not requested_out_path
        or not _is_utf8_text(requested_out_path)
    ):
        return fail("out_path must be valid UTF-8 without NUL")
    pending_path = params.get("_pending_path")
    if pending_path is not None and (
        not isinstance(pending_path, str)
        or not pending_path
        or not _is_utf8_text(pending_path)
    ):
        return fail("capture pending path must be valid UTF-8 without NUL")
    out_path = requested_out_path or str(
        session_tmp(session_id)
        / f"cap_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.png"
    )
    root_pid, root_created = _root_identity(params)
    try:
        window, _snapshot = _wait_for_session_window(
            registered["job_name"],
            desktop,
            root_pid=root_pid,
            root_created=root_created,
            **_window_filters(params),
        )
    except SessionObserveError as error:
        return fail(str(error), session_id=session_id, **_snapshot_public(error.snapshot))

    try:
        with on_desktop(desktop), operation_dpi_awareness():
            geometry = window_geometry(int(window["hwnd"]))
            window = {**window, **geometry}
            path, width, height, all_black = print_window_png(
                int(window["hwnd"]),
                out_path,
                window_size=(geometry["width"], geometry["height"]),
                overwrite=overwrite,
                pending_path=pending_path,
            )
    except Exception as error:
        return fail(str(error), session_id=session_id)
    window = {**window, "width": width, "height": height}
    return ok(
        session_id=session_id,
        path=str(path),
        all_black=all_black,
        window=window,
        desktop=desktop,
        **_window_public_fields(window),
    )


def cmd_kill(params: dict) -> int:
    session_id = params.get("session_id")
    if not session_id:
        return fail("session_id required")
    sid = validate_session_id(str(session_id))

    registered = any(key in params for key in _REGISTERED_IDENTITY_KEYS)
    owner_scope = "registered"
    if registered:
        source = params
    else:
        # An absent directory is positive evidence that cleanup already completed: both
        # broker owner-loss cleanup and this function remove it only after the Job is empty
        # and the broker is gone. A present but unreadable state is different and must fail
        # closed because private-desktop resources may still exist without provable identity.
        if not session_tmp(sid, create=False).exists():
            return ok(
                session_id=sid,
                killed=[],
                failed=[],
                errors=[],
                already_absent=True,
            )
        try:
            source = _orphan_cleanup_state(sid)
            # The request is untrusted. Only the launcher process identity can authorize
            # live-owner cleanup; request parameters must not impersonate another owner.
            owner_scope = _validate_orphan_cleanup_owner(
                source,
                os.getppid(),
                params.get("cleanup_token"),
            )
        except Exception as error:
            message = f"invalid orphan cleanup state: {error}"
            return fail(
                message,
                session_id=sid,
                killed=[],
                failed=[],
                errors=[message],
                orphan_cleanup_required=True,
            )
    job_name = str(source.get("_job_name" if registered else "job_name") or "")
    broker_pid = _positive_identity(
        source.get("_broker_pid" if registered else "broker_pid")
    )
    broker_created = _positive_identity(
        source.get("_broker_created" if registered else "broker_created")
    )
    root_pid = _positive_identity(source.get("_root_pid" if registered else "pid"))
    root_created = _positive_identity(
        source.get("_root_created" if registered else "root_created")
    )

    job_match = _JOB_NAME_RE.fullmatch(job_name)
    if job_match is None or job_match.group(1) != sid:
        message = "invalid or missing orphan cleanup Job Object namespace"
        return fail(
            message,
            session_id=sid,
            killed=[],
            failed=[],
            errors=[message],
            orphan_cleanup_required=True,
        )
    cleanup_token = params.get("cleanup_token")
    capability_valid = False
    if registered or cleanup_token is not None:
        try:
            _validate_cleanup_capability(job_name, sid, cleanup_token)
            capability_valid = True
        except Exception as error:
            if not registered and owner_scope == "dead":
                capability_valid = False
            else:
                kind = "registered" if registered else "orphan"
                message = f"invalid {kind} cleanup capability: {error}"
                return fail(
                    message,
                    session_id=sid,
                    killed=[],
                    failed=[],
                    errors=[message],
                    orphan_cleanup_required=True,
                )
    if not broker_pid or not broker_created:
        message = "missing registered broker identity"
        return fail(
            message,
            session_id=sid,
            killed=[],
            failed=[],
            errors=[message],
            orphan_cleanup_required=True,
        )

    # A Job name survives only as long as some handle does. Pin the registered broker
    # while opening it; if the broker dies first, the same name may already mean another Job.
    broker_watch = None
    registered_broker_status = None
    if registered:
        registered_broker_status = process_identity_status(broker_pid, broker_created)
        if registered_broker_status == "unverifiable":
            message = f"registered cleanup broker identity is unverifiable: pid={broker_pid}"
            return fail(
                message,
                session_id=sid,
                killed=[],
                failed=[],
                errors=[message],
                orphan_cleanup_required=True,
            )
        if registered_broker_status == "live":
            broker_watch = open_process_watch(broker_pid, broker_created)
            if broker_watch is None:
                message = f"registered cleanup could not pin broker identity: pid={broker_pid}"
                return fail(
                    message,
                    session_id=sid,
                    killed=[],
                    failed=[],
                    errors=[message],
                    orphan_cleanup_required=True,
                )

    handle = None
    try:
        handle = open_job(job_name)
    except Exception as error:
        close_handle(broker_watch)
        close_job(handle)
        message = f"failed to open orphan cleanup Job Object: {error}"
        return fail(
            message,
            session_id=sid,
            killed=[],
            failed=[],
            errors=[message],
            orphan_cleanup_required=True,
        )

    if registered and registered_broker_status != "live":
        close_handle(broker_watch)
        if handle is not None:
            close_job(handle)
            message = "registered cleanup refused: Job name exists after broker identity ended"
            return fail(
                message,
                session_id=sid,
                killed=[],
                failed=[],
                errors=[message],
                orphan_cleanup_required=True,
            )
        try:
            remove_session_tmp(sid)
        except Exception as error:
            return fail(
                "registered session stopped but temp cleanup failed",
                session_id=sid,
                killed=[],
                failed=[],
                errors=[str(error)],
                orphan_cleanup_required=True,
            )
        return ok(session_id=sid, killed=[], failed=[], errors=[], already_stopped=True)

    if registered and broker_watch is not None and not process_handle_alive(broker_watch):
        close_handle(broker_watch)
        close_job(handle)
        message = "registered cleanup refused: broker exited while opening the Job Object"
        return fail(
            message,
            session_id=sid,
            killed=[],
            failed=[],
            errors=[message],
            orphan_cleanup_required=True,
        )

    try:
        before = query_job_pids(handle) if handle else []
    except Exception as error:
        close_handle(broker_watch)
        close_job(handle)
        message = f"failed to query orphan cleanup Job Object: {error}"
        return fail(
            message,
            session_id=sid,
            killed=[],
            failed=[],
            errors=[message],
            orphan_cleanup_required=True,
        )

    if not registered and not capability_valid:
        if handle is not None:
            close_job(handle)
            message = "orphan cleanup refused: runtime capability required while Job exists"
            return fail(
                message,
                session_id=sid,
                killed=[],
                failed=[],
                errors=[message],
                orphan_cleanup_required=True,
            )
        try:
            broker_status = _orphan_identity_status("broker", broker_pid, broker_created)
        except Exception as error:
            message = str(error)
            return fail(
                message,
                session_id=sid,
                killed=[],
                failed=[],
                errors=[message],
                orphan_cleanup_required=True,
            )
        if broker_status == "live":
            message = (
                "orphan cleanup refused: broker identity is "
                f"{broker_status} pid={broker_pid}"
            )
            return fail(
                message,
                session_id=sid,
                killed=[],
                failed=[],
                errors=[message],
                orphan_cleanup_required=True,
            )
        try:
            remove_session_tmp(sid)
        except Exception as error:
            return fail(
                "stale orphan temp cleanup failed",
                session_id=sid,
                killed=[],
                failed=[],
                errors=[str(error)],
                orphan_cleanup_required=True,
            )
        return ok(session_id=sid, killed=[], failed=[], errors=[], stale_only=True)

    # Disk identity is untrusted; a live root is required only while a Job still
    # exists. Error states written before root creation may still be removed safely.
    if not registered and handle is not None:
        if not root_pid or not root_created:
            close_job(handle)
            message = "orphan cleanup root identity is required while Job exists"
            return fail(
                message,
                session_id=sid,
                killed=[],
                failed=[],
                errors=[message],
                orphan_cleanup_required=True,
            )
        try:
            root_status = _orphan_identity_status("root", root_pid, root_created)
        except Exception as error:
            close_job(handle)
            message = str(error)
            return fail(
                message,
                session_id=sid,
                killed=[],
                failed=[],
                errors=[message],
                orphan_cleanup_required=True,
            )
        if root_status == "live" and root_pid not in before:
            close_job(handle)
            message = "orphan cleanup root identity is not a member of the registered Job Object"
            return fail(
                message,
                session_id=sid,
                killed=[],
                failed=[],
                errors=[message],
                orphan_cleanup_required=True,
            )

    killed: list[int] = []
    failed: list[int] = []
    errors: list[str] = []
    warnings: list[str] = []
    job_empty = False
    broker_gone = False
    # Even an empty Job leaves the broker watching its owner. The stop file asks it
    # to release the desktop; process termination remains the registered fallback.
    try:
        write_text_atomic(session_tmp(sid) / "stop", "1", encoding="ascii")
    except Exception as error:
        warnings.append(f"failed to write broker stop request: {error}")

    try:
        if handle and before:
            try:
                terminate_job(handle)
            except Exception as error:
                errors.append(str(error))
            deadline = time.monotonic() + 3.0
            remaining = before
            while time.monotonic() < deadline:
                remaining = query_job_pids(handle)
                if not remaining:
                    break
                time.sleep(0.05)
            if remaining:
                failed.extend(remaining)
            else:
                killed.extend(before)

        deadline = time.monotonic() + 1.0
        try:
            broker_status = _orphan_identity_status("broker", broker_pid, broker_created)
            while time.monotonic() < deadline and broker_status == "live":
                time.sleep(0.05)
                broker_status = _orphan_identity_status("broker", broker_pid, broker_created)
        except Exception as error:
            broker_status = "unverifiable"
            errors.append(str(error))
        if broker_status == "live" and registered:
            termination = terminate_same_process(
                broker_pid,
                broker_created,
                timeout_s=1.0,
            )
            if termination in ("killed", "gone", "mismatch"):
                broker_status = "gone"
            else:
                errors.append(
                    f"registered broker force termination failed: pid={broker_pid} status={termination}"
                )
        if broker_status == "live":
            failed.append(broker_pid)
            errors.append(
                f"session broker did not exit after Job cleanup: pid={broker_pid}"
            )
        elif broker_status == "unverifiable":
            failed.append(broker_pid)
        else:
            broker_gone = True

        if broker_gone and handle is not None:
            # Closing the final trusted handle activates KILL_ON_JOB_CLOSE if the first
            # termination call failed. The named lookup then verifies the observable end.
            close_job(handle)
            handle = None
        remaining = query_named_job_pids(job_name) if handle is None else query_job_pids(handle)
        job_empty = not remaining
        if remaining:
            failed.extend(remaining)
            errors.append(f"registered Job Object is not empty: pids={remaining}")
    except Exception as error:
        errors.append(str(error))
    finally:
        if handle is not None:
            close_job(handle)
        close_handle(broker_watch)

    if not job_empty:
        errors.append("registered Job Object emptiness was not confirmed")
    if not broker_gone:
        errors.append("registered broker disappearance was not confirmed")
    killed = sorted(set(killed))
    failed = sorted(set(failed))
    errors = list(dict.fromkeys(errors))
    if failed or errors:
        return fail(
            "session cleanup incomplete",
            session_id=sid,
            killed=killed,
            failed=failed,
            errors=errors,
            warnings=warnings,
            orphan_cleanup_required=True,
        )
    try:
        remove_session_tmp(sid)
    except Exception as e:
        return fail(
            "session processes stopped but temp cleanup failed",
            session_id=sid,
            killed=killed,
            failed=[],
            errors=[str(e)],
            warnings=warnings,
            orphan_cleanup_required=True,
        )
    return ok(
        session_id=sid,
        killed=killed,
        failed=[],
        errors=[],
        warnings=warnings,
    )


def cmd_broker(params: dict) -> int:
    from broker import run_broker

    return run_broker(params)


def main(argv: list[str] | None = None) -> int:
    if not is_windows():
        return fail("pi-silent-gui only supports Windows")
    parser = argparse.ArgumentParser(prog="silent_gui")
    parser.add_argument(
        "command",
        choices=["spawn", "wait", "message", "capture", "kill", "broker"],
    )
    parser.add_argument("--stdin-json", action="store_true")
    namespace = parser.parse_args(argv)
    set_protocol_command(namespace.command)
    try:
        if not namespace.stdin_json:
            raise ValueError("--stdin-json required")
        if namespace.command == "broker":
            delay = os.environ.get("PI_SILENT_GUI_TEST_BROKER_STDIN_DELAY")
            if delay:
                time.sleep(float(delay))
        params = _load_json(_read_stdin_json())
        code = {
            "spawn": cmd_spawn,
            "wait": cmd_wait,
            "message": cmd_message,
            "capture": cmd_capture,
            "kill": cmd_kill,
            "broker": cmd_broker,
        }[namespace.command](params)
        mismatch_env = {
            "kill": "PI_SILENT_GUI_TEST_KILL_EXIT_MISMATCH",
            "spawn": "PI_SILENT_GUI_TEST_SPAWN_EXIT_MISMATCH",
        }.get(namespace.command)
        if code == 0 and mismatch_env and os.environ.get(mismatch_env) == "1":
            return 1
        return code
    except Exception as e:
        return fail(str(e), command=namespace.command)


if __name__ == "__main__":
    raise SystemExit(main())
