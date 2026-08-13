# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import threading
import unittest
from unittest.mock import Mock, patch

from tests.support import backend as _backend  # noqa: F401

import broker as broker_module
from process import process_creation_time


class AudioGuardReadinessTests(unittest.TestCase):
    def test_refuses_target_resume_before_audio_guard_is_ready(self) -> None:
        guard = Mock()
        guard.fatal_error.return_value = None
        guard.is_ready.return_value = False

        with self.assertRaisesRegex(RuntimeError, "refusing to resume target"):
            broker_module._check_audio_guard(guard, require_ready=True)

    def test_accepts_ready_audio_guard(self) -> None:
        guard = Mock()
        guard.fatal_error.return_value = None
        guard.is_ready.return_value = True

        broker_module._check_audio_guard(guard, require_ready=True)


class AudioGuardCleanupTests(unittest.TestCase):
    def test_hung_com_cleanup_forces_disposable_broker_exit(self) -> None:
        release = threading.Event()
        exit_called = threading.Event()
        guard = Mock()
        guard.close.side_effect = lambda: release.wait(1)

        def fake_exit(code: int) -> None:
            self.assertEqual(code, 3)
            exit_called.set()
            release.set()

        with patch.object(broker_module.os, "_exit", side_effect=fake_exit):
            broker_module._close_audio_guard(guard, timeout=0.01)

        self.assertTrue(exit_called.is_set(), "hung COM cleanup must trigger broker exit")


class OwnerWatchValidationTests(unittest.TestCase):
    """The broker rejects startup unless it can bind the exact live owner."""

    def test_accepts_live_owner_identity(self) -> None:
        pid = os.getpid()
        handle = broker_module._open_owner_watch(
            {"owner_pid": pid, "owner_created": str(process_creation_time(pid))}
        )
        self.addCleanup(broker_module.close_handle, handle)
        self.assertTrue(handle)

    def test_rejects_missing_or_malformed_owner(self) -> None:
        pid = os.getpid()
        created = str(process_creation_time(pid))
        cases = [
            ({}, "owner_pid"),
            ({"owner_pid": pid}, "owner_created"),
            ({"owner_created": created}, "owner_pid"),
            ({"owner_pid": 0, "owner_created": created}, "owner_pid"),
            ({"owner_pid": -1, "owner_created": created}, "owner_pid"),
            ({"owner_pid": True, "owner_created": created}, "owner_pid"),
            ({"owner_pid": str(pid), "owner_created": created}, "owner_pid"),
            ({"owner_pid": pid, "owner_created": 0}, "owner_created"),
            ({"owner_pid": pid, "owner_created": "not-decimal"}, "owner_created"),
            ({"owner_pid": pid, "owner_created": ""}, "owner_created"),
        ]
        for params, expected in cases:
            with self.subTest(params=params):
                with self.assertRaises(RuntimeError) as caught:
                    broker_module._open_owner_watch(params)
                self.assertIn(expected, str(caught.exception))

    def test_rejects_owner_creation_time_mismatch(self) -> None:
        # A reused PID with a different creation time must never impersonate the owner.
        pid = os.getpid()
        wrong = int(process_creation_time(pid)) + 1
        with self.assertRaises(RuntimeError) as caught:
            broker_module._open_owner_watch({"owner_pid": pid, "owner_created": str(wrong)})
        self.assertIn("not live", str(caught.exception))

    def test_rejects_owner_that_cannot_be_opened(self) -> None:
        with patch.object(broker_module, "open_process_watch", return_value=None):
            with self.assertRaises(RuntimeError) as caught:
                broker_module._open_owner_watch({"owner_pid": 4321, "owner_created": "99"})
        self.assertIn("not live", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
