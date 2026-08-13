# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import os
import unittest
from unittest.mock import patch

from tests.support.backend import TEST_GUI_EXE

import audio as audio_module
import broker as broker_module
import desktop as desktop_module
from common import new_session_id, read_json, remove_session_tmp, session_state_path
from job import query_named_job_pids
from process import (
    process_creation_time,
    process_is_elevated,
    same_process,
    terminate_same_process,
)


class BrokerFailureTests(unittest.TestCase):
    def run_fault(self, patcher) -> tuple[int, dict]:
        sid = new_session_id()
        self.addCleanup(remove_session_tmp, sid)
        with patcher:
            code = broker_module.run_broker(
                {
                    "session_id": sid,
                    "exe": TEST_GUI_EXE,
                    "allow_elevated": process_is_elevated(os.getpid()) is True,
                    "owner_pid": os.getpid(),
                    "owner_created": str(process_creation_time(os.getpid())),
                    "cleanup_token_hash": hashlib.sha256(b"d" * 64).hexdigest(),
                }
            )
        state = read_json(session_state_path(sid, create=False))

        def emergency_cleanup() -> None:
            identities = [
                (pid, process_creation_time(pid))
                for pid in query_named_job_pids(state["job_name"])
            ]
            root = (int(state.get("pid") or 0), int(state.get("root_created") or 0))
            if root[0] and root not in identities:
                identities.append(root)
            for pid, created in identities:
                if created is not None:
                    terminate_same_process(pid, int(created))

        self.addCleanup(emergency_cleanup)
        return code, state

    def test_audio_guard_failure_never_resumes_and_cleans_atomic_job(self) -> None:
        with patch.object(broker_module, "resume_thread") as resumed:
            code, state = self.run_fault(
                patch.object(
                    audio_module.AudioGuard,
                    "start",
                    side_effect=RuntimeError("injected audio guard failure"),
                )
            )
        resumed.assert_not_called()
        self.assertEqual(code, 2)
        self.assertTrue(state["assigned_to_job"])
        self.assertIn("audio guard failure", state["error"])
        self.assertFalse(same_process(int(state["pid"]), int(state["root_created"])))
        self.assertEqual(query_named_job_pids(state["job_name"]), [])

    def test_audio_guard_not_ready_never_resumes_target(self) -> None:
        class NotReadyGuard:
            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                return None

            def arm(self):
                return None

            def sweep(self):
                return 0

            def fatal_error(self):
                return None

            def is_ready(self):
                return False

            def has_pending_notification(self):
                return False

            def close(self):
                return None

        with patch.object(broker_module, "resume_thread") as resumed:
            code, state = self.run_fault(
                patch.object(audio_module, "AudioGuard", NotReadyGuard)
            )
        resumed.assert_not_called()
        self.assertEqual(code, 2)
        self.assertIn("audio guard is not ready", state["error"])
        self.assertFalse(same_process(int(state["pid"]), int(state["root_created"])))
        self.assertEqual(query_named_job_pids(state["job_name"]), [])

    def test_audio_fatal_after_resume_prevents_ready_and_cleans_job(self) -> None:
        class FatalAfterResumeGuard:
            def __init__(self, *_args, **_kwargs):
                self.sweeps = 0

            def start(self):
                return None

            def arm(self):
                return None

            def sweep(self):
                self.sweeps += 1
                if self.sweeps >= 2:
                    raise RuntimeError("injected post-resume audio fatal")
                return 0

            def fatal_error(self):
                return None

            def is_ready(self):
                return True

            def has_pending_notification(self):
                return False

            def wait(self, _timeout):
                return None

            def close(self):
                return None

        with patch.object(
            broker_module,
            "resume_thread",
            wraps=broker_module.resume_thread,
        ) as resumed:
            code, state = self.run_fault(
                patch.object(audio_module, "AudioGuard", FatalAfterResumeGuard)
            )
        resumed.assert_called_once()
        self.assertEqual(code, 2)
        self.assertNotEqual(state["status"], "ready")
        self.assertIn("post-resume audio fatal", state["error"])
        self.assertFalse(same_process(int(state["pid"]), int(state["root_created"])))
        self.assertEqual(query_named_job_pids(state["job_name"]), [])

    def test_resume_thread_failure_cleans_atomic_job(self) -> None:
        code, state = self.run_fault(
            patch.object(
                broker_module,
                "resume_thread",
                side_effect=RuntimeError("injected ResumeThread failure"),
            )
        )
        self.assertEqual(code, 2)
        self.assertEqual(state["status"], "error")
        self.assertFalse(state["cleanup_failed"])
        self.assertTrue(state["assigned_to_job"])
        self.assertIn("ResumeThread failure", state["error"])
        self.assertFalse(same_process(int(state["pid"]), int(state["root_created"])))
        self.assertEqual(query_named_job_pids(state["job_name"]), [])

    def test_missing_job_list_attribute_fails_closed(self) -> None:
        code, state = self.run_fault(
            patch.object(desktop_module.kernel32, "UpdateProcThreadAttribute", return_value=False)
        )
        self.assertEqual(code, 2)
        self.assertEqual(state["status"], "error")
        self.assertFalse(state["cleanup_failed"])
        self.assertFalse(state["assigned_to_job"])
        self.assertIn("refusing non-atomic launch", state["error"])
        self.assertEqual(query_named_job_pids(state["job_name"]), [])


if __name__ == "__main__":
    unittest.main()
