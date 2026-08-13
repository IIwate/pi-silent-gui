# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
import time
import unittest
from unittest.mock import Mock, patch

from tests.support import backend as _backend  # noqa: F401

import audio as audio_module
from audio import AudioGuard, TransientTopologyError


class AudioTopologyPolicyTests(unittest.TestCase):
    def test_dynamic_refreshes_changed_render_endpoints(self) -> None:
        guard = AudioGuard(lambda: [], policy="dynamic")
        guard._topology = ((("old", 1),), ("old", "old", "old"))
        changed = ((("new", 1),), ("new", "new", "new"))
        with (
            patch.object(guard, "_enumerate_topology", return_value=(changed, {"new": object()})),
            patch.object(guard, "_sync_endpoints") as sync,
        ):
            guard.sweep()
        sync.assert_called_once()
        self.assertEqual(guard._topology, changed)

    def test_sweep_repeats_until_notification_generation_is_stable(self) -> None:
        guard = AudioGuard(lambda: [], policy="dynamic")
        topology = ((("render", 1),), ("render", "render", "render"))
        guard._topology = topology
        calls = 0

        def enumerate_topology():
            nonlocal calls
            calls += 1
            if calls == 1:
                guard._topology_notification("added", "transient")
            return topology, {}

        with patch.object(guard, "_enumerate_topology", side_effect=enumerate_topology):
            guard.sweep()
        self.assertEqual(calls, 2)
        self.assertFalse(guard.has_pending_notification())

    def test_strict_fails_closed_on_enumerated_topology_change(self) -> None:
        guard = AudioGuard(lambda: [], policy="strict")
        guard._topology = ((("old", 1),), ("old", "old", "old"))
        guard.arm()
        changed = ((("new", 1),), ("new", "new", "new"))
        with patch.object(guard, "_enumerate_topology", return_value=(changed, {"new": object()})):
            with self.assertRaisesRegex(RuntimeError, "strict policy"):
                guard.sweep()
        self.assertIn("strict policy", guard.fatal_error() or "")

    def test_strict_known_render_notification_is_immediately_fatal(self) -> None:
        guard = AudioGuard(lambda: [], policy="strict")
        guard._render_ids = {"render"}
        guard.arm()
        guard._topology_notification("removed", "render")
        self.assertIn("strict policy", guard.fatal_error() or "")

    def test_strict_transient_added_render_is_classified_before_next_sweep(self) -> None:
        guard = AudioGuard(lambda: [], policy="strict")
        guard.arm()
        with patch.object(guard, "_device_flow", return_value=audio_module.E_RENDER):
            guard._topology_notification("added", "transient-render")
        self.assertIn("strict policy", guard.fatal_error() or "")

    def test_dynamic_endpoint_notification_is_pending_but_not_fatal(self) -> None:
        guard = AudioGuard(lambda: [], policy="dynamic")
        guard.arm()
        guard._topology_notification("added", "render")
        self.assertTrue(guard.has_pending_notification())
        self.assertIsNone(guard.fatal_error())

    def test_new_endpoint_is_swept_before_registration_becomes_visible(self) -> None:
        guard = AudioGuard(lambda: [7], policy="dynamic")
        guard._session_callback = object()
        manager = Mock()
        interface = Mock()
        interface.QueryInterface.return_value = manager
        device = Mock()
        device.Activate.return_value = interface
        with patch.object(guard, "_sweep_manager", side_effect=RuntimeError("first sweep failed")):
            with self.assertRaisesRegex(RuntimeError, "registration failed"):
                guard._register_endpoint("render", device)
        manager.RegisterSessionNotification.assert_called_once()
        manager.UnregisterSessionNotification.assert_called_once()
        self.assertNotIn("render", guard._managers)

    def test_session_pid_read_failure_is_not_silently_ignored(self) -> None:
        class BrokenSession:
            @property
            def ProcessId(self):
                raise OSError("injected ProcessId failure")

        guard = AudioGuard(lambda: [7])
        with self.assertRaisesRegex(RuntimeError, "ProcessId"):
            guard._mute_session(BrokenSession())

    def test_changed_mode_rejects_non_mta_audio_thread(self) -> None:
        changed_mode = ctypes.c_long(audio_module.RPC_E_CHANGED_MODE).value
        guard = AudioGuard(lambda: [])
        with patch.object(audio_module.ole32, "CoInitializeEx", return_value=changed_mode):
            with self.assertRaisesRegex(RuntimeError, "MTA"):
                guard._initialize_com()
        self.assertFalse(guard._com_initialized)


class AudioDeviceChurnTests(unittest.TestCase):
    """Dynamic churn is tolerated only after a stable mute baseline exists."""

    @staticmethod
    def _stable_topology() -> tuple:
        return ((("render", 1),), ("render", "render", "render"))

    def test_initial_persistent_topology_churn_fails_before_ready(self) -> None:
        guard = AudioGuard(lambda: [], policy="dynamic")
        topology = self._stable_topology()

        def enumerate_topology():
            guard._topology_notification("state", "render")
            return topology, {}

        with patch.object(guard, "_enumerate_topology", side_effect=enumerate_topology):
            with self.assertRaisesRegex(RuntimeError, "before target resume"):
                guard.sweep()

        self.assertFalse(guard.is_ready())
        self.assertIn("before target resume", guard.fatal_error() or "")

    def test_persistent_topology_churn_is_tolerated_after_ready(self) -> None:
        guard = AudioGuard(lambda: [], policy="dynamic")
        topology = self._stable_topology()
        with patch.object(guard, "_enumerate_topology", return_value=(topology, {})):
            guard.sweep()
        self.assertTrue(guard.is_ready())

        def enumerate_topology():
            guard._topology_notification("state", "render")
            return topology, {}

        with patch.object(guard, "_enumerate_topology", side_effect=enumerate_topology):
            guard.sweep()

        self.assertIsNone(guard.fatal_error())
        self.assertEqual(guard.stats()["unstable_sweeps"], 1)
        self.assertTrue(guard.has_pending_notification())

    def test_enumeration_failure_is_immediately_fatal(self) -> None:
        guard = AudioGuard(lambda: [], policy="dynamic")
        with patch.object(
            guard,
            "_enumerate_topology",
            side_effect=TransientTopologyError("endpoint vanished"),
        ):
            with self.assertRaisesRegex(RuntimeError, "enumeration failed"):
                guard.sweep()
        self.assertEqual(guard.stats()["enumeration_failures"], 1)
        self.assertIn("enumeration failed", guard.fatal_error() or "")

    def test_transient_enumeration_failure_can_succeed_within_one_sweep(self) -> None:
        guard = AudioGuard(lambda: [], policy="dynamic")
        topology = self._stable_topology()
        calls = 0

        def enumerate_topology():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TransientTopologyError("endpoint vanished")
            return topology, {}

        with patch.object(guard, "_enumerate_topology", side_effect=enumerate_topology):
            guard.sweep()
        self.assertTrue(guard.is_ready())
        self.assertEqual(guard.stats()["enumeration_failures"], 0)
        self.assertIsNone(guard.fatal_error())

    def test_set_mute_failure_remains_fatal(self) -> None:
        guard = AudioGuard(lambda: [7], policy="dynamic")
        topology = self._stable_topology()
        guard._topology = topology
        guard._managers = {"render": object()}
        with (
            patch.object(guard, "_enumerate_topology", return_value=(topology, {})),
            patch.object(
                guard,
                "_sweep_manager",
                side_effect=RuntimeError("SetMute failed for session pid=7"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "SetMute failed"):
                guard.sweep()
        self.assertIn("SetMute failed", guard.fatal_error() or "")

    def test_wait_enforces_minimum_interval_after_sweep(self) -> None:
        guard = AudioGuard(lambda: [], policy="dynamic")
        guard._wake.set()  # A pending notification would return immediately without the floor.
        guard._last_sweep = time.monotonic()
        start = time.monotonic()
        guard.wait(0.2)
        self.assertGreaterEqual(
            time.monotonic() - start, audio_module.MIN_SWEEP_INTERVAL * 0.8
        )

    def test_wait_never_exceeds_requested_timeout(self) -> None:
        guard = AudioGuard(lambda: [], policy="dynamic")
        guard._wake.set()
        guard._last_sweep = time.monotonic()
        start = time.monotonic()
        guard.wait(0.01)  # A shorter requested timeout must cap the interval floor.
        self.assertLess(time.monotonic() - start, audio_module.MIN_SWEEP_INTERVAL)


if __name__ == "__main__":
    unittest.main()
