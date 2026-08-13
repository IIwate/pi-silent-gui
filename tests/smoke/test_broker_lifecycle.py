# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from tests.support.backend import (
    PY,
    BackendTestCase,
    call,
    cleanup_spawned,
    protocol_payload,
    registered_kill,
    registered_run,
)

from job import query_named_job_pids
from process import (
    is_alive,
    process_creation_time,
    process_is_elevated,
    same_process,
    terminate_same_process,
)


class BrokerLifecycleSmoke(BackendTestCase):
    def test_launcher_exit_keeps_job_child_tracked(self) -> None:
        launcher = (
            "import os,subprocess,time; "
            "subprocess.Popen([os.environ.get('PI_SILENT_GUI_TEST_EXE') or 'notepad.exe']); "
            "time.sleep(0.2)"
        )
        spawned = self.spawn_backend({"exe": sys.executable, "args": ["-c", launcher]})
        root_pid = int(spawned["pid"])
        root_created = int(spawned["root_created"])
        broker_pid = int(spawned["broker_pid"])
        broker_created = int(spawned["broker_created"])

        deadline = time.time() + 5
        while time.time() < deadline and same_process(root_pid, root_created):
            time.sleep(0.1)
        self.assertFalse(same_process(root_pid, root_created), "launcher root should have exited")
        self.assertTrue(same_process(broker_pid, broker_created), "broker must outlive launcher")

        # The Job API can briefly retain an exited PID after its process is signaled. The
        # contract is that a live child remains assigned after the launcher root has exited.
        assigned_pids = query_named_job_pids(spawned["job_name"])
        live_pids = [pid for pid in assigned_pids if is_alive(pid)]
        self.assertTrue(live_pids)
        self.assertNotIn(root_pid, live_pids)
        self.assertTrue(all(process_is_elevated(pid) is False for pid in live_pids))
        captured = registered_run("capture", spawned)
        self.assertIn(int(captured["window"]["pid"]), live_pids)

        killed = registered_kill(spawned)
        self.assertEqual(killed["failed"], [])
        time.sleep(0.4)
        self.assertFalse(same_process(broker_pid, broker_created))
        self.assertTrue(all(not is_alive(pid) for pid in live_pids))

    def test_natural_target_exit_keeps_owner_watch_until_explicit_cleanup(self) -> None:
        spawned = self.spawn_backend(
            {"exe": sys.executable, "args": ["-c", "import time; time.sleep(0.5)"]}
        )
        root_pid = int(spawned["pid"])
        root_created = int(spawned["root_created"])
        broker_pid = int(spawned["broker_pid"])
        broker_created = int(spawned["broker_created"])

        deadline = time.time() + 10
        while time.time() < deadline and same_process(root_pid, root_created):
            time.sleep(0.1)

        self.assertFalse(same_process(root_pid, root_created))
        # Job 在进程已死后仍可能短暂列出 PID，等它排空再断言。
        empty_deadline = time.time() + 5
        while time.time() < empty_deadline and query_named_job_pids(spawned["job_name"]):
            time.sleep(0.1)
        self.assertEqual(query_named_job_pids(spawned["job_name"]), [])
        self.assertTrue(
            same_process(broker_pid, broker_created),
            "broker must keep watching the owner after the target exits",
        )

        killed = registered_kill(spawned)
        self.assertEqual(killed["failed"], [])
        self.assertFalse(Path(spawned["tmp_dir"]).exists())

    def test_owner_death_terminates_session_without_any_shutdown_event(self) -> None:
        """Simulate a Pi crash where owner death prevents any session_shutdown event.

        The broker must detect owner loss, terminate the Job, and remove its temp directory.
        """
        # A killable idle process acts as Pi without risking the test runner.
        owner = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        owner_created = process_creation_time(owner.pid)
        self.assertIsNotNone(owner_created)

        def reap_owner() -> None:
            terminate_same_process(owner.pid, int(owner_created))
            owner.wait(timeout=10)

        self.addCleanup(reap_owner)

        result = subprocess.run(
            [sys.executable, str(PY), "spawn", "--stdin-json"],
            input=json.dumps(
                {
                    "exe": "notepad.exe",
                    "owner_pid": owner.pid,
                    "cleanup_token": "d" * 64,
                }
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        spawned = protocol_payload(result.stdout, "spawn")
        spawned["_cleanup_token"] = "d" * 64
        self.assertTrue(spawned.get("ok"), spawned)
        self.assertEqual(spawned["owner_pid"], owner.pid)
        # Register fallback cleanup because automatic owner-loss cleanup has not fired yet.
        self.addCleanup(lambda: cleanup_spawned(spawned) if Path(spawned["tmp_dir"]).exists() else None)

        broker_pid = int(spawned["broker_pid"])
        broker_created = int(spawned["broker_created"])
        job_pids = query_named_job_pids(spawned["job_name"])
        self.assertTrue(job_pids, "target must be running before owner death")

        # Kill the owner without sending any graceful notification.
        self.assertEqual(terminate_same_process(owner.pid, int(owner_created)), "killed")

        deadline = time.time() + 15
        while time.time() < deadline and same_process(broker_pid, broker_created):
            time.sleep(0.1)

        self.assertFalse(
            same_process(broker_pid, broker_created),
            "broker must exit after its owner dies",
        )
        self.assertEqual(query_named_job_pids(spawned["job_name"]), [])
        self.assertTrue(all(not is_alive(pid) for pid in job_pids))
        self.assertFalse(
            Path(spawned["tmp_dir"]).exists(),
            "broker must clean its own temp dir when the owner is gone",
        )

    def test_spawn_rejects_dead_owner_identity(self) -> None:
        dead = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        dead.wait(timeout=15)
        result = call(
            "spawn",
            {
                "exe": "notepad.exe",
                "owner_pid": dead.pid,
                "cleanup_token": "d" * 64,
            },
        )
        self.assertFalse(result.get("ok"))
        self.assertIn("owner", result.get("error", ""))


if __name__ == "__main__":
    import unittest

    unittest.main()
