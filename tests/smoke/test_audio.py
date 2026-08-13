# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from tests.support.backend import BackendTestCase, registered_kill

from audio import AudioGuard
from job import query_named_job_pids
from process import same_process
from pycaw.constants import DEVICE_STATE, EDataFlow
from pycaw.utils import AudioUtilities


class AudioGuardSmoke(BackendTestCase):
    def test_zero_pcm_wasapi_session_is_muted_and_cleaned(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="pi-silent-gui-audio-")
        self.addCleanup(temp.cleanup)
        marker = Path(temp.name) / "started.txt"
        helper = Path(__file__).resolve().parents[1] / "support" / "wasapi_silence.py"
        spawned = self.spawn_backend(
            {
                "exe": sys.executable,
                "args": [str(helper), str(marker)],
                "audio_device_policy": "strict",
            }
        )

        self.assertEqual(spawned["audio_device_policy"], "strict")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not marker.is_file():
            time.sleep(0.05)
        self.assertTrue(marker.is_file(), "zero-PCM WASAPI helper did not start")
        self.assertEqual(int(marker.read_text(encoding="ascii")), int(spawned["pid"]))

        muted = None
        while time.monotonic() < deadline and muted != 1:
            for device in AudioUtilities.GetAllDevices(
                EDataFlow.eRender.value,
                DEVICE_STATE.ACTIVE.value,
            ):
                for session in AudioGuard._manager_sessions(device.AudioSessionManager):
                    if int(session.ProcessId) == int(spawned["pid"]):
                        muted = int(session.SimpleAudioVolume.GetMute())
            if muted != 1:
                time.sleep(0.05)
        self.assertEqual(muted, 1, "target WASAPI session was not muted")

        killed = registered_kill(spawned)
        self.assertTrue(killed.get("ok"), killed)
        self.assertEqual(killed["failed"], [])
        self.assertEqual(killed["errors"], [])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and same_process(
            int(spawned["broker_pid"]), int(spawned["broker_created"])
        ):
            time.sleep(0.05)
        self.assertFalse(same_process(int(spawned["broker_pid"]), int(spawned["broker_created"])))
        self.assertEqual(query_named_job_pids(spawned["job_name"]), [])
        self.assertFalse(Path(spawned["tmp_dir"]).exists())


if __name__ == "__main__":
    import unittest

    unittest.main()
