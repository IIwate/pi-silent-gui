# -*- coding: utf-8 -*-
"""Minimal backend CLI and session cleanup support for tests."""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST_GUI_EXE = os.environ.get("PI_SILENT_GUI_TEST_EXE") or "notepad.exe"
TEST_CLEANUP_TOKEN = "d" * 64
PY = ROOT / "src" / "backend" / "silent_gui.py"
BACKEND = PY.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

PROTOCOL_PREFIX = "PI_SILENT_GUI_FRAME"
PROTOCOL_VERSION = "1"


def protocol_payload(output: str, command: str) -> dict:
    frames = []
    for line in (output or "").splitlines():
        parts = line.rstrip("\r").split("\t", 3)
        if len(parts) == 4 and parts[0] == PROTOCOL_PREFIX:
            frames.append(parts)
    if len(frames) != 1:
        raise AssertionError(f"expected one protocol frame for {command}, got {len(frames)}")
    _prefix, version, frame_command, payload = frames[0]
    if version != PROTOCOL_VERSION or frame_command != command:
        raise AssertionError(
            f"protocol identity mismatch: version={version!r} command={frame_command!r}"
        )
    data = json.loads(payload)
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        raise AssertionError(f"invalid protocol payload: {data!r}")
    return data


def call(command: str, params: dict, timeout: float = 30) -> dict:
    result = subprocess.run(
        [sys.executable, str(PY), command, "--stdin-json"],
        input=json.dumps(params),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if not (result.stdout or "").strip():
        raise AssertionError(
            f"empty stdout command={command} code={result.returncode} err={result.stderr!r}"
        )
    return protocol_payload(result.stdout, command)


def run(command: str, params: dict, timeout: float = 30) -> dict:
    data = call(command, params, timeout)
    if not data.get("ok"):
        raise AssertionError(data)
    return data


def registered_params(spawned: dict, params: dict | None = None) -> dict:
    return {
        **(params or {}),
        "session_id": spawned["session_id"],
        "_job_name": spawned["job_name"],
        "_desktop": spawned["desktop"],
        "_broker_pid": spawned["broker_pid"],
        "_broker_created": spawned["broker_created"],
        "_root_pid": spawned["pid"],
        "_root_created": spawned["root_created"],
        "cleanup_token": spawned["_cleanup_token"],
    }


def registered_call(command: str, spawned: dict, params: dict | None = None) -> dict:
    return call(command, registered_params(spawned, params))


def registered_run(command: str, spawned: dict, params: dict | None = None) -> dict:
    return run(command, registered_params(spawned, params))


def registered_kill(spawned: dict) -> dict:
    return call(
        "kill",
        {
            "session_id": spawned["session_id"],
            "_job_name": spawned["job_name"],
            "_broker_pid": spawned["broker_pid"],
            "_broker_created": spawned["broker_created"],
            "cleanup_token": spawned["_cleanup_token"],
        },
    )


def cleanup_spawned(spawned: dict) -> None:
    result = registered_kill(spawned)
    if not result.get("ok") or result.get("failed") or result.get("errors"):
        raise AssertionError(f"session cleanup failed: {result}")


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:24]
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise AssertionError(f"invalid PNG header: {path}")
    return struct.unpack(">II", data[16:24])


class BackendTestCase(unittest.TestCase):
    def spawn_backend(self, params: dict) -> dict:
        # Use the test process as owner so the broker does not watch the short-lived launcher.
        cleanup_token = params.get("cleanup_token", TEST_CLEANUP_TOKEN)
        spawned = run(
            "spawn",
            {"owner_pid": os.getpid(), "cleanup_token": cleanup_token, **params},
        )
        spawned["_cleanup_token"] = cleanup_token
        # Register cleanup before making any behavioral assertion.
        self.addCleanup(cleanup_spawned, spawned)
        return spawned
