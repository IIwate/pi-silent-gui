# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests.support.backend import (
    TEST_CLEANUP_TOKEN,
    TEST_GUI_EXE,
    BackendTestCase,
    ROOT,
    call,
    png_size,
    protocol_payload,
    registered_call,
    registered_kill,
    registered_params,
    registered_run,
    run,
)

from common import new_session_id, remove_session_tmp, session_tmp, temp_root, write_json_atomic
from job import cleanup_job_name, query_named_job_pids
from process import process_creation_time, process_is_elevated, same_process


class BackendCliTests(BackendTestCase):
    def test_temp_root_ignores_environment_poisoning(self) -> None:
        known = temp_root()
        with patch.dict(os.environ, {"LOCALAPPDATA": str(ROOT)}):
            self.assertEqual(temp_root(), known)

    def test_reparse_session_delete_is_rejected(self) -> None:
        target_sid = new_session_id()
        link_sid = new_session_id()
        target = session_tmp(target_sid)
        link = session_tmp(link_sid, create=False)
        sentinel = target / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        self.addCleanup(remove_session_tmp, target_sid)

        made = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)

        def remove_link() -> None:
            if os.path.lexists(link):
                os.rmdir(link)  # Remove only the test junction, never its target.

        self.addCleanup(remove_link)
        with self.assertRaisesRegex(OSError, "reparse point rejected"):
            remove_session_tmp(link_sid)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_capture_waits_for_a_delayed_job_window(self) -> None:
        delayed = (
            "import subprocess,time; "
            "time.sleep(3); "
            f"subprocess.Popen([{TEST_GUI_EXE!r}]).wait()"
        )
        spawned = self.spawn_backend(
            {"exe": sys.executable, "args": ["-S", "-c", delayed]}
        )
        started = time.monotonic()
        captured = registered_run("capture", spawned)
        elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 2.5)
        self.assertLess(elapsed, 6.0)
        self.assertTrue(Path(captured["path"]).is_file())

    def test_message_waits_for_a_delayed_job_window(self) -> None:
        delayed = (
            "import subprocess,time; "
            "time.sleep(3); "
            f"subprocess.Popen([{TEST_GUI_EXE!r}]).wait()"
        )
        spawned = self.spawn_backend(
            {"exe": sys.executable, "args": ["-S", "-c", delayed]}
        )
        started = time.monotonic()
        messaged = registered_run(
            "message", spawned, {"action": "key", "key": "return"}
        )
        elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 2.5)
        self.assertLess(elapsed, 6.0)
        self.assertGreater(messaged["hwnd"], 0)

    def test_message_and_capture_require_registered_runtime_capability(self) -> None:
        spawned = self.spawn_backend({"exe": TEST_GUI_EXE})
        time.sleep(0.8)
        for command, request in (
            ("capture", {}),
            ("message", {"action": "key", "key": "return"}),
        ):
            with self.subTest(command=command, boundary="missing"):
                denied = call(command, {"session_id": spawned["session_id"], **request})
                self.assertFalse(denied.get("ok"))
                self.assertIn("runtime capability required", denied.get("error", ""))
            with self.subTest(command=command, boundary="mismatch"):
                forged = registered_params(spawned, request)
                forged["cleanup_token"] = "e" * 64
                denied = call(command, forged)
                self.assertFalse(denied.get("ok"))
                self.assertIn("runtime capability mismatch", denied.get("error", ""))

        Path(spawned["tmp_dir"], "session.json").write_text("{broken", encoding="utf-8")
        captured = registered_run("capture", spawned)
        self.assertEqual(captured["desktop"], spawned["desktop"])
        self.assertTrue(Path(captured["path"]).is_file())

    def test_wait_ends_when_the_session_process_exits(self) -> None:
        spawned = self.spawn_backend(
            {
                "exe": sys.executable,
                "args": ["-S", "-c", "import time; time.sleep(0.4); raise SystemExit(7)"],
            }
        )
        started = time.monotonic()
        result = registered_call(
            "wait",
            spawned,
            {"timeout_ms": 8000, "title": "this-title-will-not-match"},
        )
        elapsed = time.monotonic() - started
        self.assertFalse(result.get("ok"), result)
        self.assertFalse(result.get("alive"), result)
        self.assertEqual(result.get("exit_code"), 7)
        self.assertEqual(result.get("windows"), [])
        self.assertLess(elapsed, 3.0)
        self.assertIn("no live process", result.get("error", ""))

    def test_notepad_lifecycle_messages_and_cleanup(self) -> None:
        spawned = self.spawn_backend({"exe": TEST_GUI_EXE, "desktop_name": "Default"})
        sid = spawned["session_id"]
        self.assertRegex(spawned["desktop"], rf"^pi_silent_{sid}_[0-9a-f]{{32}}$")
        self.assertRegex(spawned["job_name"], rf"^pi_silent_job_{sid}_[0-9a-f]{{32}}$")
        self.assertTrue(same_process(int(spawned["broker_pid"]), int(spawned["broker_created"])))
        expected_elevated = os.environ.get("PI_SILENT_GUI_TEST_ALLOW_ELEVATED") == "1"
        self.assertEqual(bool(spawned["target_elevated"]), expected_elevated)
        self.assertEqual(Path(spawned["cwd"]), Path.cwd().resolve())
        self.assertEqual(bool(process_is_elevated(int(spawned["pid"]))), expected_elevated)
        self.assertIn(int(spawned["pid"]), query_named_job_pids(spawned["job_name"]))

        waited = registered_run("wait", spawned, {"timeout_ms": 5000})
        self.assertGreater(waited["hwnd"], 0)
        self.assertTrue(waited["alive"])
        self.assertIsNone(waited["exit_code"])
        self.assertTrue(any(int(item["hwnd"]) == int(waited["hwnd"]) for item in waited["windows"]))
        captured = registered_run("capture", spawned, {"hwnd": waited["hwnd"]})
        image = Path(captured["path"])
        self.assertTrue(image.is_absolute() and image.is_file())
        self.assertGreater(image.stat().st_size, 100)
        self.assertIsInstance(captured["all_black"], bool)
        client = captured["window"]["client"]
        self.assertTrue(all(isinstance(client[key], int) for key in ("x", "y", "width", "height")))
        self.assertEqual(
            png_size(image),
            (int(captured["width"]), int(captured["height"])),
        )
        click_x = client["x"] + client["width"] // 2
        click_y = client["y"] + client["height"] // 2

        keyed = registered_run(
            "message",
            spawned,
            {
                "action": "key",
                "key": "return",
                "hwnd": captured["hwnd"],
                "count": 2,
                "interval_ms": 50,
            },
        )
        self.assertEqual(keyed["hwnd"], captured["hwnd"])
        self.assertEqual(keyed["count"], 2)
        wrong_hwnd = registered_call(
            "message",
            spawned,
            {
                "action": "key",
                "key": "return",
                "hwnd": 0x7FFFFFFF,
            },
        )
        self.assertFalse(wrong_hwnd.get("ok"), wrong_hwnd)
        self.assertIn("expected hwnd 2147483647", wrong_hwnd.get("error", ""))
        clicked = registered_run(
            "message", spawned, {"action": "click", "x": click_x, "y": click_y}
        )
        self.assertEqual(clicked["hwnd"], captured["hwnd"])
        typed = registered_run(
            "message", spawned, {"action": "type", "text": "hello"}
        )
        self.assertEqual(typed["chars"], 5)

        outside = registered_call(
            "message",
            spawned,
            {
                "action": "click",
                "x": captured["width"],
                "y": 0,
            },
        )
        self.assertFalse(outside.get("ok"))
        self.assertIn("outside window", outside.get("error", ""))
        empty = registered_call("message", spawned, {"action": "type", "text": ""})
        self.assertFalse(empty.get("ok"))
        self.assertIn("type requires text", empty.get("error", ""))

        killed = run(
            "kill",
            {
                "session_id": sid,
                "tmp_dir": str(ROOT),
                "_job_name": spawned["job_name"],
                "_broker_pid": spawned["broker_pid"],
                "_broker_created": spawned["broker_created"],
                "_root_pid": spawned["pid"],
                "_root_created": spawned["root_created"],
                "cleanup_token": spawned["_cleanup_token"],
            },
        )
        self.assertEqual(killed["failed"], [])
        time.sleep(0.3)
        self.assertFalse(same_process(int(spawned["broker_pid"]), int(spawned["broker_created"])))
        self.assertFalse(Path(spawned["tmp_dir"]).exists())

    def test_clean_env_defaults_to_clean_and_explicit_false_remains_supported(self) -> None:
        temp = tempfile.TemporaryDirectory(
            prefix="pi-silent-gui-env-", dir=Path.cwd()
        )
        self.addCleanup(temp.cleanup)
        relative_cwd = os.path.relpath(temp.name, Path.cwd())
        expected_cwd = Path(temp.name).resolve()
        inherited = self.spawn_backend(
            {
                "exe": sys.executable,
                "args": ["-S", "-c", "import time; time.sleep(10)"],
                "cwd": relative_cwd,
                "clean_env": False,
            }
        )
        clean = self.spawn_backend(
            {
                "exe": sys.executable,
                "args": ["-S", "-c", "import time; time.sleep(10)"],
                "cwd": relative_cwd,
            }
        )
        self.assertFalse(inherited["clean_env"])
        self.assertTrue(clean["clean_env"])
        self.assertEqual(Path(inherited["cwd"]), expected_cwd)
        self.assertEqual(Path(clean["cwd"]), expected_cwd)

    def test_unregistered_kill_rejects_missing_or_corrupt_state(self) -> None:
        for content in (None, "{broken"):
            sid = new_session_id()
            tmp = session_tmp(sid)
            self.addCleanup(remove_session_tmp, sid)
            if content is not None:
                (tmp / "session.json").write_text(content, encoding="utf-8")
            result = call("kill", {"session_id": sid})
            self.assertFalse(result.get("ok"))
            self.assertEqual(result.get("killed"), [])
            self.assertEqual(result.get("failed"), [])
            self.assertTrue(result.get("errors"))
            self.assertTrue(tmp.exists())

    def test_kill_reports_absent_session_as_already_cleaned(self) -> None:
        sid = new_session_id()
        self.assertFalse(session_tmp(sid, create=False).exists())
        result = call("kill", {"session_id": sid})
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("already_absent"))
        self.assertEqual(result.get("killed"), [])
        self.assertEqual(result.get("failed"), [])
        self.assertEqual(result.get("errors"), [])

    def test_cli_accepts_only_bounded_stdin_json(self) -> None:
        sid = new_session_id()
        cli = [sys.executable, str(ROOT / "src" / "backend" / "silent_gui.py"), "kill"]
        accepted = subprocess.run(
            [*cli, "--stdin-json"],
            input=json.dumps({"session_id": sid}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        accepted_data = protocol_payload(accepted.stdout, "kill")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertTrue(accepted_data.get("ok"), accepted_data)
        self.assertTrue(accepted_data.get("already_absent"))

        missing_flag = subprocess.run(
            cli,
            input=json.dumps({"session_id": sid}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        missing_flag_data = protocol_payload(missing_flag.stdout, "kill")
        self.assertNotEqual(missing_flag.returncode, 0)
        self.assertEqual(missing_flag_data.get("error"), "--stdin-json required")

        argv_transport = subprocess.run(
            [*cli, "--json", json.dumps({"session_id": sid})],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(argv_transport.returncode, 2)
        self.assertIn("unrecognized arguments: --json", argv_transport.stderr)

        oversized = subprocess.run(
            [*cli, "--stdin-json"],
            input=json.dumps({"session_id": sid, "pad": "x" * (1024 * 1024)}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        oversized_data = protocol_payload(oversized.stdout, "kill")
        self.assertNotEqual(oversized.returncode, 0)
        self.assertEqual(
            oversized_data.get("error"), "stdin JSON exceeds 1048576 bytes"
        )

    def test_kill_is_idempotent_once_the_session_is_gone(self) -> None:
        spawned = run(
            "spawn",
            {
                "owner_pid": os.getpid(),
                "exe": "notepad.exe",
                "cleanup_token": TEST_CLEANUP_TOKEN,
            },
        )
        spawned["_cleanup_token"] = TEST_CLEANUP_TOKEN
        sid = spawned["session_id"]
        self.addCleanup(remove_session_tmp, sid)  # Fallback if the first kill fails.

        first = registered_kill(spawned)
        self.assertTrue(first.get("ok"), first)
        self.assertEqual(first.get("failed"), [])
        self.assertEqual(first.get("errors"), [])
        self.assertFalse(session_tmp(sid, create=False).exists())

        # Repeated cleanup must succeed: callers and automatic retries should not fail when
        # absence already proves there is no remaining session state.
        second = call("kill", {"session_id": sid})
        self.assertTrue(second.get("ok"), second)
        self.assertTrue(second.get("already_absent"))
        self.assertEqual(second.get("killed"), [])
        self.assertEqual(second.get("failed"), [])
        self.assertEqual(second.get("errors"), [])

    def test_orphan_cleanup_rejects_forged_live_owner_identity(self) -> None:
        owner = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        def stop_owner() -> None:
            if owner.poll() is None:
                owner.terminate()
                owner.wait(timeout=3)

        self.addCleanup(stop_owner)
        spawned = self.spawn_backend({"owner_pid": owner.pid, "exe": TEST_GUI_EXE})
        sid = spawned["session_id"]

        blocked = call("kill", {"session_id": sid, "owner_pid": owner.pid})
        self.assertFalse(blocked.get("ok"))
        self.assertIn("another live Pi owner", blocked.get("error", ""))
        self.assertTrue(same_process(int(spawned["pid"]), int(spawned["root_created"])))
        self.assertTrue(Path(spawned["tmp_dir"]).exists())

    def test_new_runtime_token_may_remove_dead_owner_stale_directory_only(self) -> None:
        sid = new_session_id()
        tmp = session_tmp(sid)
        self.addCleanup(remove_session_tmp, sid)
        old_token = "a" * 64
        token_hash = hashlib.sha256(old_token.encode("ascii")).hexdigest()
        write_json_atomic(
            tmp / "session.json",
            {
                "status": "ready",
                "session_id": sid,
                "desktop": f"pi_silent_{sid}_{'1' * 32}",
                "job_name": cleanup_job_name(sid, token_hash),
                "broker_pid": 99999991,
                "broker_created": "1",
                "pid": 99999992,
                "root_created": "1",
                "owner_pid": 99999993,
                "owner_created": "1",
                "cleanup_token_hash": token_hash,
            },
        )

        result = call("kill", {"session_id": sid, "cleanup_token": "e" * 64})
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("stale_only"), result)
        self.assertFalse(tmp.exists())

    def test_tampered_dead_owner_state_cannot_terminate_a_live_job(self) -> None:
        spawned = self.spawn_backend({"exe": TEST_GUI_EXE})
        state_path = Path(spawned["tmp_dir"]) / "session.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update(
            {
                "owner_pid": 99999991,
                "owner_created": "1",
                "broker_pid": 99999992,
                "broker_created": "1",
                "pid": 99999993,
                "root_created": "1",
            }
        )
        write_json_atomic(state_path, state)

        blocked = call(
            "kill",
            {"session_id": spawned["session_id"], "cleanup_token": "e" * 64},
        )
        self.assertFalse(blocked.get("ok"), blocked)
        self.assertIn("runtime capability required while Job exists", blocked.get("error", ""))
        self.assertTrue(same_process(int(spawned["pid"]), int(spawned["root_created"])))
        self.assertTrue(Path(spawned["tmp_dir"]).exists())

    def test_unregistered_kill_does_not_trust_forged_process_identities(self) -> None:
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        def stop_sleeper() -> None:
            if sleeper.poll() is None:
                sleeper.terminate()
                sleeper.wait(timeout=3)

        self.addCleanup(stop_sleeper)
        created = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and created is None:
            created = process_creation_time(sleeper.pid)
            if created is None:
                time.sleep(0.05)
        self.assertIsNotNone(created)

        sid = new_session_id()
        tmp = session_tmp(sid)
        self.addCleanup(remove_session_tmp, sid)
        write_json_atomic(
            tmp / "session.json",
            {
                "status": "ready",
                "session_id": sid,
                "desktop": f"pi_silent_{sid}_{'0' * 32}",
                "job_name": f"pi_silent_job_{sid}_{'0' * 32}",
                "broker_pid": sleeper.pid,
                "broker_created": str(created),
                "pid": sleeper.pid,
                "root_created": str(created),
                "owner_pid": os.getpid(),
                "owner_created": str(process_creation_time(os.getpid())),
            },
        )
        result = call("kill", {"session_id": sid})
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("killed"), [])
        self.assertTrue(same_process(sleeper.pid, int(created)))

    def test_broker_stdin_delivery_obeys_the_shared_ready_deadline(self) -> None:
        started = time.monotonic()
        with patch.dict(
            os.environ,
            {"PI_SILENT_GUI_TEST_BROKER_STDIN_DELAY": "30"},
        ):
            result = call(
                "spawn",
                {
                    "exe": TEST_GUI_EXE,
                    "owner_pid": os.getpid(),
                    "cleanup_token": TEST_CLEANUP_TOKEN,
                    "env": {"PAYLOAD": "x" * 300_000},
                },
                timeout=20,
            )
        elapsed = time.monotonic() - started

        self.assertFalse(result.get("ok"), result)
        self.assertIn("timeout", result.get("error", ""))
        self.assertTrue(result.get("cleanup_ok"), result)
        self.assertLess(elapsed, 15, f"broker stdin timeout took {elapsed:.1f}s")
        sid = result["session_id"]
        self.assertFalse(
            session_tmp(sid, create=False).exists(),
            f"tracked session leaked: {sid}",
        )

    def test_create_process_failure_does_not_persist_target_arguments(self) -> None:
        result = call(
            "spawn",
            {
                "exe": "Z:/pi-silent-gui/does-not-exist.exe",
                "args": ["sentinel-secret-argument"],
                "cleanup_token": TEST_CLEANUP_TOKEN,
            },
        )

        self.assertFalse(result.get("ok"), result)
        self.assertNotIn("sentinel-secret-argument", json.dumps(result))
        self.assertTrue(result.get("cleanup_ok"), result)

    def test_spawn_requires_caller_owned_cleanup_capability(self) -> None:
        result = call("spawn", {"exe": TEST_GUI_EXE, "owner_pid": os.getpid()})
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "cleanup_token required")
        # Spawn returned before creating any session directory; no temp leak possible.
        self.assertNotIn("session_id", result)

    def test_spawn_rejects_nul_in_environment_overrides(self) -> None:
        for env in ({"BAD\0KEY": "x"}, {"GOOD": "bad\0value"}):
            result = call("spawn", {"exe": "notepad.exe", "env": env})
            self.assertFalse(result.get("ok"))
            self.assertIn("NUL", result.get("error", ""))

    def test_spawn_rejects_nul_and_unpaired_surrogates_in_argv(self) -> None:
        for params in (
            {"exe": "notepad.exe\0hidden"},
            {"exe": "notepad.exe", "args": ["visible\0hidden"]},
            {"exe": "notepad.exe", "args": ["\ud800"]},
        ):
            with self.subTest(params=params):
                result = call("spawn", params)
                self.assertFalse(result.get("ok"), result)
                self.assertNotIn("session_id", result)

    def test_stop_write_failure_is_warning_after_verified_force_cleanup(self) -> None:
        import silent_gui as backend_cli

        spawned = self.spawn_backend({"exe": TEST_GUI_EXE})
        output = io.StringIO()
        with patch.object(
            backend_cli,
            "write_text_atomic",
            side_effect=OSError("test stop write failure"),
        ), redirect_stdout(output):
            backend_cli.set_protocol_command("kill")
            code = backend_cli.cmd_kill(registered_params(spawned))
        first = protocol_payload(output.getvalue(), "kill")

        self.assertEqual(code, 0)
        self.assertTrue(first.get("ok"), first)
        self.assertEqual(first.get("failed"), [])
        self.assertEqual(first.get("errors"), [])
        self.assertTrue(
            any(
                "failed to write broker stop request" in warning
                for warning in first["warnings"]
            ),
            first,
        )
        self.assertEqual(query_named_job_pids(spawned["job_name"]), [])
        self.assertFalse(
            same_process(int(spawned["pid"]), int(spawned["root_created"]))
        )
        self.assertFalse(
            same_process(int(spawned["broker_pid"]), int(spawned["broker_created"]))
        )
        self.assertFalse(Path(spawned["tmp_dir"]).exists())

        retried = registered_kill(spawned)
        self.assertTrue(retried.get("ok"), retried)
        self.assertEqual(retried.get("failed"), [])
        self.assertEqual(retried.get("errors"), [])

    def test_registered_kill_requires_the_job_bound_runtime_capability(self) -> None:
        spawned = self.spawn_backend({"exe": TEST_GUI_EXE})
        payload = {
            "session_id": spawned["session_id"],
            "_job_name": spawned["job_name"],
            "_broker_pid": spawned["broker_pid"],
            "_broker_created": spawned["broker_created"],
            "_root_pid": spawned["pid"],
            "_root_created": spawned["root_created"],
        }
        for cleanup_token in (None, "e" * 64):
            attempt = dict(payload)
            if cleanup_token is not None:
                attempt["cleanup_token"] = cleanup_token
            blocked = call("kill", attempt)
            self.assertFalse(blocked.get("ok"), blocked)
            self.assertIn("runtime capability", blocked.get("error", ""))
            self.assertTrue(
                same_process(int(spawned["pid"]), int(spawned["root_created"]))
            )
            self.assertTrue(Path(spawned["tmp_dir"]).exists())

    def test_registered_kill_cleans_when_state_is_missing(self) -> None:
        spawned = self.spawn_backend(
            {
                "exe": "./notepad.exe",
                "cwd": str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"),
            }
        )
        state = Path(spawned["tmp_dir"]) / "session.json"
        state.rename(state.with_name("session.missing"))
        cleaned = registered_kill(spawned)
        self.assertEqual(cleaned["failed"], [])
        self.assertFalse(Path(spawned["tmp_dir"]).exists())

    def test_registered_kill_ignores_corrupt_state_identity(self) -> None:
        spawned = self.spawn_backend({"exe": TEST_GUI_EXE})
        sid = spawned["session_id"]
        write_json_atomic(
            Path(spawned["tmp_dir"]) / "session.json",
            {
                "status": "ready",
                "session_id": sid,
                "job_name": f"pi_silent_job_{sid}_{'0' * 32}",
                "desktop": f"pi_silent_{sid}_{'0' * 32}",
                "broker_pid": "not-a-number",
                "broker_created": {"broken": True},
                "pid": "also-broken",
                "root_created": [],
            },
        )
        cleaned = registered_kill(spawned)
        self.assertEqual(cleaned["failed"], [])
        self.assertFalse(Path(spawned["tmp_dir"]).exists())

    def test_allow_elevated_requires_elevated_runner(self) -> None:
        if process_is_elevated(os.getpid()):
            spawned = self.spawn_backend({"exe": TEST_GUI_EXE, "allow_elevated": True})
            self.assertTrue(spawned["target_elevated"])
            self.assertTrue(process_is_elevated(int(spawned["pid"])))
        else:
            result = call(
                "spawn",
                {
                    "exe": TEST_GUI_EXE,
                    "allow_elevated": True,
                    "cleanup_token": TEST_CLEANUP_TOKEN,
                },
            )
            self.assertFalse(result.get("ok"))
            self.assertIn("requires Pi/broker to already be elevated", result.get("error", ""))


if __name__ == "__main__":
    import unittest

    unittest.main()
