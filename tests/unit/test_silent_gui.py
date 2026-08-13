from __future__ import annotations

import hashlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch

from tests.support import backend as _backend  # noqa: F401

import silent_gui as silent_gui_module


class BrokerTransportTests(unittest.TestCase):
    def test_unverifiable_elevation_does_not_allocate_a_session_directory(self) -> None:
        with (
            patch.object(silent_gui_module, "process_creation_time", return_value=100),
            patch.object(silent_gui_module, "open_process_watch", return_value=1),
            patch.object(silent_gui_module, "close_handle"),
            patch.object(silent_gui_module, "process_is_elevated", return_value=None),
            patch.object(silent_gui_module, "create_session_tmp") as create_tmp,
            patch.object(
                silent_gui_module,
                "fail",
                side_effect=lambda error, **data: {"ok": False, "error": error, **data},
            ),
        ):
            result = silent_gui_module.cmd_spawn(
                {"exe": "fixture.exe", "owner_pid": 10, "cleanup_token": "a" * 64}
            )

        self.assertEqual(result["error"], "cannot verify launcher elevation")
        create_tmp.assert_not_called()

    def test_broker_launch_failure_reports_session_directory_cleanup(self) -> None:
        session_id = "0123456789ab"
        for cleanup_error in (None, OSError("directory locked")):
            with self.subTest(cleanup_error=cleanup_error):
                with (
                    patch.object(silent_gui_module, "process_creation_time", return_value=100),
                    patch.object(silent_gui_module, "open_process_watch", return_value=1),
                    patch.object(silent_gui_module, "close_handle"),
                    patch.object(silent_gui_module, "process_is_elevated", return_value=False),
                    patch.object(
                        silent_gui_module,
                        "create_session_tmp",
                        return_value=(session_id, MagicMock()),
                    ),
                    patch.object(
                        silent_gui_module,
                        "session_state_path",
                        return_value=MagicMock(),
                    ),
                    patch.object(
                        silent_gui_module.subprocess,
                        "Popen",
                        side_effect=OSError("launch failed"),
                    ),
                    patch.object(
                        silent_gui_module,
                        "remove_session_tmp",
                        side_effect=cleanup_error,
                    ) as remove_tmp,
                    patch.object(
                        silent_gui_module,
                        "fail",
                        side_effect=lambda error, **data: {
                            "ok": False,
                            "error": error,
                            **data,
                        },
                    ),
                ):
                    result = silent_gui_module.cmd_spawn(
                        {"exe": "fixture.exe", "owner_pid": 10, "cleanup_token": "a" * 64}
                    )

                self.assertEqual(result["session_id"], session_id)
                self.assertEqual(result["cleanup_ok"], cleanup_error is None)
                self.assertEqual(result["orphan_cleanup_required"], cleanup_error is not None)
                self.assertEqual(result["cleanup_errors"], [] if cleanup_error is None else ["directory locked"])
                remove_tmp.assert_called_once_with(session_id)

    def test_readiness_exception_stops_broker_and_removes_owned_directory(self) -> None:
        class Sink:
            def write(self, data: bytes) -> int:
                return len(data)

            def close(self) -> None:
                return None

        class FailingPollProcess:
            pid = 20
            returncode = None

            def __init__(self) -> None:
                self.stdin = Sink()
                self.polls = 0
                self.terminated = False

            def poll(self):
                self.polls += 1
                if self.polls == 1:
                    raise OSError("poll failed")
                return 1 if self.terminated else None

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout=None):
                return 1

        process = FailingPollProcess()
        session_id = "0123456789ab"
        with (
            patch.object(
                silent_gui_module,
                "process_creation_time",
                side_effect=lambda pid: 100 if pid == 10 else None,
            ),
            patch.object(silent_gui_module, "open_process_watch", return_value=1),
            patch.object(silent_gui_module, "close_handle"),
            patch.object(silent_gui_module, "process_is_elevated", return_value=False),
            patch.object(
                silent_gui_module,
                "create_session_tmp",
                return_value=(session_id, MagicMock()),
            ),
            patch.object(
                silent_gui_module,
                "session_state_path",
                side_effect=AssertionError("session_state_path must not be called"),
            ),
            patch.object(silent_gui_module.subprocess, "Popen", return_value=process),
            patch.object(silent_gui_module, "remove_session_tmp") as remove_tmp,
            patch.object(
                silent_gui_module,
                "fail",
                side_effect=lambda error, **data: {"ok": False, "error": error, **data},
            ),
        ):
            result = silent_gui_module.cmd_spawn(
                {"exe": "fixture.exe", "owner_pid": 10, "cleanup_token": "a" * 64}
            )

        self.assertIn("poll failed", result["error"])
        self.assertTrue(result["cleanup_ok"])
        self.assertTrue(process.terminated)
        remove_tmp.assert_called_once_with(session_id)

    def test_spawn_sends_secrets_over_broker_stdin_not_argv(self) -> None:
        class StdinSink:
            def __init__(self) -> None:
                self.data = bytearray()
                self.closed = False

            def write(self, data: bytes) -> int:
                self.data.extend(data)
                return len(data)

            def close(self) -> None:
                self.closed = True

        class FakeProcess:
            pid = 20
            returncode = None

            def __init__(self) -> None:
                self.stdin = StdinSink()

            def poll(self):
                return None

        session_id = "0123456789ab"
        fake_process = FakeProcess()
        state = {
            "status": "ready",
            "session_id": session_id,
            "broker_pid": 20,
            "broker_created": "200",
            "pid": 30,
            "root_created": "300",
            "desktop": f"pi_silent_{session_id}_{'1' * 32}",
            "job_name": f"pi_silent_job_{session_id}_{'2' * 32}",
            "tmp_dir": "unused",
        }
        state_path = MagicMock()
        state_path.is_file.return_value = True
        with (
            patch.object(silent_gui_module, "process_creation_time", side_effect=lambda pid: {10: 100, 20: 200}[pid]),
            patch.object(silent_gui_module, "open_process_watch", return_value=1),
            patch.object(silent_gui_module, "close_handle"),
            patch.object(silent_gui_module, "create_session_tmp", return_value=(session_id, MagicMock())),
            patch.object(silent_gui_module, "session_state_path", return_value=state_path),
            patch.object(silent_gui_module, "read_json", return_value=state),
            patch.object(silent_gui_module, "process_is_elevated", return_value=False),
            patch.object(silent_gui_module.subprocess, "Popen", return_value=fake_process) as popen,
            patch.object(silent_gui_module, "query_named_job_pids", return_value=[20, 30]),
            patch.object(silent_gui_module, "ok", side_effect=lambda **data: data),
        ):
            result = silent_gui_module.cmd_spawn(
                {
                    "exe": "fixture.exe",
                    "args": ["--token", "sentinel-argument"],
                    "env": {"API_TOKEN": "sentinel-environment"},
                    "owner_pid": 10,
                    "cleanup_token": "a" * 64,
                }
            )

        argv = popen.call_args.kwargs["args"]
        self.assertEqual(argv[-1], "--stdin-json")
        self.assertNotIn("sentinel", " ".join(argv))
        self.assertTrue(fake_process.stdin.closed)
        payload = json.loads(fake_process.stdin.data.decode("utf-8"))
        self.assertEqual(payload["args"], ["--token", "sentinel-argument"])
        self.assertEqual(payload["env"], {"API_TOKEN": "sentinel-environment"})
        self.assertEqual(result["session_id"], session_id)
        self.assertNotIn("--json", argv)
        self.assertLess(len(fake_process.stdin.data), 4096)

    def test_stdin_json_over_one_mib_is_rejected(self) -> None:
        stdin = MagicMock()
        stdin.buffer = io.BytesIO(b"x" * (silent_gui_module.MAX_JSON_BYTES + 1))
        with (
            patch.object(silent_gui_module.sys, "stdin", stdin),
            self.assertRaisesRegex(ValueError, "stdin JSON exceeds 1048576 bytes"),
        ):
            silent_gui_module._read_stdin_json()

    def test_legacy_json_argument_is_rejected(self) -> None:
        with (
            patch.object(silent_gui_module, "is_windows", return_value=True),
            patch.object(silent_gui_module.sys, "stderr", io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            silent_gui_module.main(["spawn", "--json", "{}"])

        self.assertEqual(raised.exception.code, 2)

    def test_spawn_rejects_nul_and_unpaired_surrogates_before_allocating_state(self) -> None:
        cases = [
            ({"exe": "fixture.exe\0hidden"}, "exe must be valid UTF-8 without NUL"),
            (
                {"exe": "fixture.exe", "args": ["\ud800"]},
                "args must be a valid UTF-8 string list without NUL",
            ),
            (
                {"exe": "fixture.exe", "args": ""},
                "args must be a valid UTF-8 string list without NUL",
            ),
            (
                {"exe": "fixture.exe", "env": {"TOKEN": "\ud800"}},
                "env must be a valid UTF-8 string map",
            ),
        ]
        for params, expected in cases:
            with self.subTest(expected=expected):
                with (
                    patch.object(silent_gui_module, "create_session_tmp") as create_tmp,
                    patch.object(
                        silent_gui_module,
                        "fail",
                        side_effect=lambda error, **data: {"ok": False, "error": error, **data},
                    ),
                ):
                    result = silent_gui_module.cmd_spawn(params)

                self.assertIn(expected, result["error"])
                create_tmp.assert_not_called()


class RegisteredOperationTests(unittest.TestCase):
    def test_unverifiable_broker_identity_fails_closed(self) -> None:
        session_id = "0123456789ab"
        cleanup_token = "a" * 64
        params = {
            "session_id": session_id,
            "_job_name": silent_gui_module.cleanup_job_name(
                session_id,
                hashlib.sha256(cleanup_token.encode("ascii")).hexdigest(),
            ),
            "_desktop": f"pi_silent_{session_id}_{'1' * 32}",
            "_broker_pid": 20,
            "_broker_created": "200",
            "cleanup_token": cleanup_token,
        }
        with patch.object(
            silent_gui_module,
            "process_identity_status",
            return_value="unverifiable",
        ):
            with self.assertRaisesRegex(PermissionError, "broker identity is unverifiable"):
                silent_gui_module._registered_operation(params)


class RegisteredCleanupTests(unittest.TestCase):
    def test_registered_cleanup_never_terminates_request_process_identities(self) -> None:
        session_id = "0123456789ab"
        cleanup_token = "a" * 64
        job_name = silent_gui_module.cleanup_job_name(
            session_id,
            hashlib.sha256(cleanup_token.encode("ascii")).hexdigest(),
        )
        with (
            patch.object(silent_gui_module, "process_identity_status", return_value="live"),
            patch.object(silent_gui_module, "open_process_watch", return_value=99),
            patch.object(silent_gui_module, "process_handle_alive", return_value=True),
            patch.object(silent_gui_module, "open_job", return_value=1),
            patch.object(silent_gui_module, "query_job_pids", side_effect=[[30], [], []]),
            patch.object(silent_gui_module, "terminate_job"),
            patch.object(silent_gui_module, "close_job"),
            patch.object(silent_gui_module, "close_handle"),
            patch.object(silent_gui_module, "session_tmp", return_value=MagicMock()),
            patch.object(silent_gui_module, "write_text_atomic"),
            patch.object(silent_gui_module, "_orphan_identity_status", return_value="gone"),
            patch.object(silent_gui_module, "remove_session_tmp"),
            patch.object(silent_gui_module, "terminate_same_process") as terminate_process,
            patch.object(silent_gui_module, "ok", side_effect=lambda **data: data),
        ):
            result = silent_gui_module.cmd_kill(
                {
                    "session_id": session_id,
                    "_job_name": job_name,
                    "_broker_pid": 20,
                    "_broker_created": "200",
                    "_root_pid": 40,
                    "_root_created": "400",
                    "cleanup_token": cleanup_token,
                }
            )

        self.assertEqual(result["killed"], [30])
        self.assertEqual(result["failed"], [])
        terminate_process.assert_not_called()

    def test_kill_stop_write_failure_is_a_warning_after_verified_cleanup(
        self,
    ) -> None:
        session_id = "0123456789ab"
        cleanup_token = "a" * 64
        job_name = silent_gui_module.cleanup_job_name(
            session_id,
            hashlib.sha256(cleanup_token.encode("ascii")).hexdigest(),
        )
        with (
            patch.object(silent_gui_module, "process_identity_status", return_value="live"),
            patch.object(silent_gui_module, "open_process_watch", return_value=99),
            patch.object(silent_gui_module, "process_handle_alive", return_value=True),
            patch.object(silent_gui_module, "open_job", return_value=1),
            patch.object(
                silent_gui_module,
                "query_job_pids",
                side_effect=[[30], [], []],
            ),
            patch.object(silent_gui_module, "close_job") as close_job,
            patch.object(silent_gui_module, "close_handle"),
            patch.object(silent_gui_module, "session_tmp", return_value=MagicMock()),
            patch.object(
                silent_gui_module,
                "write_text_atomic",
                side_effect=OSError("temp write failed"),
            ),
            patch.object(
                silent_gui_module,
                "_orphan_identity_status",
                return_value="gone",
            ),
            patch.object(silent_gui_module, "remove_session_tmp") as remove_tmp,
            patch.object(
                silent_gui_module,
                "ok",
                side_effect=lambda **data: {"ok": True, **data},
            ),
            patch.object(silent_gui_module, "terminate_job") as terminate_job,
        ):
            result = silent_gui_module.cmd_kill(
                {
                    "session_id": session_id,
                    "_job_name": job_name,
                    "_broker_pid": 20,
                    "_broker_created": "200",
                    "_root_pid": 40,
                    "_root_created": "400",
                    "cleanup_token": cleanup_token,
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(
            result["warnings"],
            ["failed to write broker stop request: temp write failed"],
        )
        self.assertEqual(result["killed"], [30])
        self.assertEqual(result["failed"], [])
        terminate_job.assert_called_once_with(1)
        close_job.assert_called_once_with(1)
        remove_tmp.assert_called_once_with(session_id)

    def test_registered_cleanup_force_terminates_a_stuck_verified_broker(self) -> None:
        session_id = "0123456789ab"
        cleanup_token = "a" * 64
        job_name = silent_gui_module.cleanup_job_name(
            session_id,
            hashlib.sha256(cleanup_token.encode("ascii")).hexdigest(),
        )
        with (
            patch.object(silent_gui_module, "process_identity_status", return_value="live"),
            patch.object(silent_gui_module, "open_process_watch", return_value=99),
            patch.object(silent_gui_module, "process_handle_alive", return_value=True),
            patch.object(silent_gui_module, "open_job", return_value=1),
            patch.object(silent_gui_module, "query_job_pids", side_effect=[[30], []]),
            patch.object(silent_gui_module, "query_named_job_pids", return_value=[]),
            patch.object(silent_gui_module, "terminate_job"),
            patch.object(silent_gui_module, "close_job"),
            patch.object(silent_gui_module, "close_handle"),
            patch.object(silent_gui_module, "session_tmp", return_value=MagicMock()),
            patch.object(silent_gui_module, "write_text_atomic"),
            patch.object(silent_gui_module, "_orphan_identity_status", return_value="live"),
            patch.object(
                silent_gui_module.time,
                "monotonic",
                side_effect=[0.0, 0.1, 1.0, 4.0],
            ),
            patch.object(
                silent_gui_module,
                "terminate_same_process",
                return_value="killed",
            ) as terminate_broker,
            patch.object(silent_gui_module, "remove_session_tmp"),
            patch.object(
                silent_gui_module,
                "ok",
                side_effect=lambda **data: {"ok": True, **data},
            ),
        ):
            result = silent_gui_module.cmd_kill(
                {
                    "session_id": session_id,
                    "_job_name": job_name,
                    "_broker_pid": 20,
                    "_broker_created": "200",
                    "_root_pid": 40,
                    "_root_created": "400",
                    "cleanup_token": cleanup_token,
                }
            )

        self.assertTrue(result["ok"])
        terminate_broker.assert_called_once_with(20, 200, timeout_s=1.0)

    def test_registered_cleanup_refuses_a_reused_job_name_after_broker_exit(self) -> None:
        session_id = "0123456789ab"
        cleanup_token = "a" * 64
        job_name = silent_gui_module.cleanup_job_name(
            session_id,
            hashlib.sha256(cleanup_token.encode("ascii")).hexdigest(),
        )
        with (
            patch.object(silent_gui_module, "process_identity_status", return_value="gone"),
            patch.object(silent_gui_module, "open_job", return_value=1),
            patch.object(silent_gui_module, "close_job") as close_job,
            patch.object(silent_gui_module, "terminate_job") as terminate_job,
            patch.object(
                silent_gui_module,
                "fail",
                side_effect=lambda error, **data: {"ok": False, "error": error, **data},
            ),
        ):
            result = silent_gui_module.cmd_kill(
                {
                    "session_id": session_id,
                    "_job_name": job_name,
                    "_broker_pid": 20,
                    "_broker_created": "200",
                    "cleanup_token": cleanup_token,
                }
            )

        self.assertIn("Job name exists after broker identity ended", result["error"])
        terminate_job.assert_not_called()
        close_job.assert_called_once_with(1)


class OrphanCleanupOwnerTests(unittest.TestCase):
    cleanup_token = "a" * 64

    def test_error_state_without_root_identity_remains_stale_cleanup_eligible(self) -> None:
        state = {
            "session_id": "0123456789ab",
            "desktop": f"pi_silent_0123456789ab_{'1' * 32}",
            "job_name": f"pi_silent_job_0123456789ab_{'2' * 32}",
            "broker_pid": 20,
            "broker_created": "200",
            "owner_pid": 10,
            "owner_created": "100",
        }
        with patch.object(silent_gui_module, "_state", return_value=state):
            self.assertEqual(
                silent_gui_module._orphan_cleanup_state("0123456789ab"),
                state,
            )

    def test_dead_owner_may_remove_pre_root_stale_state_when_job_is_absent(self) -> None:
        session_id = "0123456789ab"
        directory = MagicMock()
        directory.exists.return_value = True
        state = {
            "session_id": session_id,
            "desktop": f"pi_silent_{session_id}_{'1' * 32}",
            "job_name": f"pi_silent_job_{session_id}_{'2' * 32}",
            "broker_pid": 20,
            "broker_created": "200",
            "owner_pid": 10,
            "owner_created": "100",
        }
        with (
            patch.object(silent_gui_module, "session_tmp", return_value=directory),
            patch.object(silent_gui_module, "_orphan_cleanup_state", return_value=state),
            patch.object(
                silent_gui_module,
                "_validate_orphan_cleanup_owner",
                return_value="dead",
            ),
            patch.object(silent_gui_module, "open_job", return_value=None),
            patch.object(silent_gui_module, "_orphan_identity_status", return_value="gone"),
            patch.object(silent_gui_module, "remove_session_tmp") as remove_tmp,
            patch.object(
                silent_gui_module,
                "ok",
                side_effect=lambda **data: {"ok": True, **data},
            ),
        ):
            result = silent_gui_module.cmd_kill({"session_id": session_id})

        self.assertTrue(result["ok"])
        self.assertTrue(result["stale_only"])
        remove_tmp.assert_called_once_with(session_id)

    @classmethod
    def state(cls) -> dict:
        return {
            "owner_pid": 10,
            "owner_created": "100",
            "cleanup_token_hash": hashlib.sha256(cls.cleanup_token.encode("ascii")).hexdigest(),
        }

    def test_current_owner_may_clean_lost_registration(self) -> None:
        with patch.object(silent_gui_module, "process_creation_time", return_value=100):
            silent_gui_module._validate_orphan_cleanup_owner(
                self.state(),
                10,
                self.cleanup_token,
            )

    def test_same_process_runtime_capability_mismatch_is_rejected(self) -> None:
        with patch.object(silent_gui_module, "process_creation_time", return_value=100):
            with self.assertRaisesRegex(PermissionError, "runtime capability mismatch"):
                silent_gui_module._validate_orphan_cleanup_owner(
                    self.state(),
                    10,
                    "b" * 64,
                )

    def test_different_caller_may_clean_after_recorded_owner_dies(self) -> None:
        with (
            patch.object(silent_gui_module, "process_creation_time", return_value=200),
            patch.object(silent_gui_module, "process_identity_status", return_value="gone"),
        ):
            silent_gui_module._validate_orphan_cleanup_owner(self.state(), 20)

    def test_different_caller_cannot_clean_another_live_owner(self) -> None:
        with (
            patch.object(silent_gui_module, "process_creation_time", return_value=200),
            patch.object(silent_gui_module, "process_identity_status", return_value="live"),
        ):
            with self.assertRaisesRegex(PermissionError, "another live Pi owner"):
                silent_gui_module._validate_orphan_cleanup_owner(self.state(), 20)

    def test_unverifiable_recorded_owner_fails_closed(self) -> None:
        with (
            patch.object(silent_gui_module, "process_creation_time", return_value=200),
            patch.object(
                silent_gui_module,
                "process_identity_status",
                return_value="unverifiable",
            ),
        ):
            with self.assertRaisesRegex(PermissionError, "owner identity is unverifiable"):
                silent_gui_module._validate_orphan_cleanup_owner(self.state(), 20)

    def test_unverifiable_root_or_broker_identity_fails_closed(self) -> None:
        with patch.object(
            silent_gui_module,
            "process_identity_status",
            return_value="unverifiable",
        ):
            for role in ("root", "broker"):
                with self.subTest(role=role):
                    with self.assertRaisesRegex(PermissionError, f"{role} identity is unverifiable"):
                        silent_gui_module._orphan_identity_status(role, 10, 100)

    def test_missing_owner_identity_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "no valid owner identity"):
            silent_gui_module._validate_orphan_cleanup_owner({}, 20)


class MessageRepeatTests(unittest.TestCase):
    def _dispatch(self, extra, *, clicks=None, keys=None, sleeps=None):
        window = {
            "hwnd": 7,
            "title": "t",
            "class": "c",
            "width": 200,
            "height": 100,
            "client": {"x": 0, "y": 0, "width": 200, "height": 80},
        }

        def fake_click(hwnd, x, y):
            clicks.append((hwnd, x, y))
            return {
                "window": dict(window),
                "hit_test": 1,
                "target_hwnd": 7,
                "dispatch": "client",
                "point": {
                    "window": {"x": x, "y": y},
                    "screen": {"x": x, "y": y},
                    "target": {"x": x, "y": y},
                    "target_space": "client",
                },
            }

        def fake_key(hwnd, vk=None, name=None):
            keys.append((hwnd, name))

        with (
            patch.object(
                silent_gui_module,
                "_registered_operation",
                return_value={
                    "session_id": "0123456789ab",
                    "job_name": "pi_silent_job_0123456789ab_" + "a" * 32,
                    "desktop": "pi_silent_0123456789ab_" + "a" * 32,
                    "pids": [1],
                },
            ),
            patch.object(
                silent_gui_module,
                "_wait_for_session_window",
                return_value=(window, {"alive": True, "exit_code": None, "windows": []}),
            ),
            patch.object(silent_gui_module, "query_named_job_pids", return_value=[1]),
            patch.object(silent_gui_module.time, "sleep", side_effect=lambda seconds: sleeps.append(seconds)),
            patch.object(silent_gui_module, "ok", side_effect=lambda **data: {"ok": True, **data}),
            patch.object(
                silent_gui_module,
                "fail",
                side_effect=lambda error, **data: {"ok": False, "error": error, **data},
            ),
            patch("window.click", side_effect=fake_click),
            patch("window.key", side_effect=fake_key),
            patch("window.verify_window_in_pids", return_value=True),
            patch("desktop_ctx.on_desktop"),
            patch("desktop_ctx.operation_dpi_awareness"),
        ):
            return silent_gui_module.cmd_message({"session_id": "0123456789ab", **extra})

    def test_single_click_does_not_sleep(self) -> None:
        clicks: list[tuple[int, int, int]] = []
        sleeps: list[float] = []
        result = self._dispatch(
            {"action": "click", "x": 4, "y": 5},
            clicks=clicks,
            keys=[],
            sleeps=sleeps,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["count"], 1)
        self.assertEqual(clicks, [(7, 4, 5)])
        self.assertEqual(sleeps, [])

    def test_click_repeats_same_point_with_interval_between_only(self) -> None:
        clicks: list[tuple[int, int, int]] = []
        sleeps: list[float] = []
        result = self._dispatch(
            {"action": "click", "x": 10, "y": 20, "count": 3, "interval_ms": 40},
            clicks=clicks,
            keys=[],
            sleeps=sleeps,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["count"], 3)
        self.assertEqual(clicks, [(7, 10, 20), (7, 10, 20), (7, 10, 20)])
        self.assertEqual(sleeps, [0.04, 0.04])

    def test_key_repeats_same_key_with_default_interval(self) -> None:
        keys: list[tuple[int, str | None]] = []
        sleeps: list[float] = []
        result = self._dispatch(
            {"action": "key", "key": "return", "count": 2},
            clicks=[],
            keys=keys,
            sleeps=sleeps,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["count"], 2)
        self.assertEqual(keys, [(7, "return"), (7, "return")])
        self.assertEqual(sleeps, [0.3])

    def test_repeat_rejects_zero_interval_and_oversize_count(self) -> None:
        zero = self._dispatch(
            {"action": "key", "key": "return", "count": 2, "interval_ms": 0},
            clicks=[],
            keys=[],
            sleeps=[],
        )
        self.assertFalse(zero["ok"])
        self.assertIn("interval_ms", zero["error"])
        oversize = self._dispatch(
            {"action": "click", "x": 1, "y": 1, "count": 51},
            clicks=[],
            keys=[],
            sleeps=[],
        )
        self.assertFalse(oversize["ok"])
        self.assertIn("count", oversize["error"])


class WaitObservationTests(unittest.TestCase):
    def _wait(self, extra, *, pids, listed, match=None, sleeps=None, exit_code=None):
        window = listed[0] if listed else {
            "hwnd": 7,
            "title": "t",
            "class": "c",
            "width": 200,
            "height": 100,
        }
        sleeps = sleeps if sleeps is not None else []

        def find_window(pids_arg, **_filters):
            if match is None:
                raise LookupError("no window in pids")
            return dict(match)

        with (
            patch.object(
                silent_gui_module,
                "_registered_operation",
                return_value={
                    "session_id": "0123456789ab",
                    "job_name": "pi_silent_job_0123456789ab_" + "a" * 32,
                    "desktop": "pi_silent_0123456789ab_" + "a" * 32,
                    "pids": pids,
                },
            ),
            patch.object(silent_gui_module, "query_named_job_pids", return_value=list(pids)),
            patch.object(silent_gui_module.time, "sleep", side_effect=lambda seconds: sleeps.append(seconds)),
            patch.object(silent_gui_module, "ok", side_effect=lambda **data: {"ok": True, **data}),
            patch.object(
                silent_gui_module,
                "fail",
                side_effect=lambda error, **data: {"ok": False, "error": error, **data},
            ),
            patch.object(silent_gui_module, "is_alive", side_effect=lambda pid: pid in pids),
            patch.object(silent_gui_module, "process_exit_code", return_value=exit_code),
            patch("window.list_top_windows", side_effect=lambda pid: list(listed) if pid in pids else []),
            patch("window.find_window_in_pids", side_effect=find_window),
            patch("desktop_ctx.on_desktop"),
            patch("desktop_ctx.operation_dpi_awareness"),
        ):
            return silent_gui_module.cmd_wait(
                {
                    "session_id": "0123456789ab",
                    "timeout_ms": extra.pop("timeout_ms", 1000),
                    **extra,
                }
            )

    def test_wait_success_includes_session_snapshot(self) -> None:
        listed = [
            {
                "hwnd": 7,
                "title": "Main",
                "class": "App",
                "width": 640,
                "height": 480,
                "area": 307200,
            },
            {
                "hwnd": 8,
                "title": "Other",
                "class": "Dlg",
                "width": 200,
                "height": 100,
                "area": 20000,
            },
        ]
        result = self._wait({}, pids=[10], listed=listed, match=listed[0])
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["alive"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["hwnd"], 7)
        self.assertEqual(
            result["windows"],
            [
                {"hwnd": 7, "title": "Main", "window_class": "App", "width": 640, "height": 480},
                {"hwnd": 8, "title": "Other", "window_class": "Dlg", "width": 200, "height": 100},
            ],
        )

    def test_wait_ends_immediately_when_session_has_no_live_process(self) -> None:
        sleeps: list[float] = []
        result = self._wait(
            {"timeout_ms": 10_000, "_root_pid": 10},
            pids=[],
            listed=[],
            sleeps=sleeps,
            exit_code=7,
        )
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["alive"])
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["windows"], [])
        self.assertIn("no live process", result["error"])
        self.assertEqual(sleeps, [])

    def test_wait_timeout_includes_current_windows(self) -> None:
        listed = [
            {
                "hwnd": 9,
                "title": "Visible",
                "class": "App",
                "width": 100,
                "height": 80,
                "area": 8000,
            }
        ]
        result = self._wait(
            {"timeout_ms": 100, "title": "Missing"},
            pids=[10],
            listed=listed,
        )
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["alive"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(
            result["windows"],
            [{"hwnd": 9, "title": "Visible", "window_class": "App", "width": 100, "height": 80}],
        )
        self.assertIn("window not found", result["error"])

    def test_message_fails_with_the_same_snapshot(self) -> None:
        snapshot = {"alive": False, "exit_code": 3, "windows": []}
        with (
            patch.object(
                silent_gui_module,
                "_registered_operation",
                return_value={
                    "session_id": "0123456789ab",
                    "job_name": "pi_silent_job_0123456789ab_" + "a" * 32,
                    "desktop": "pi_silent_0123456789ab_" + "a" * 32,
                    "pids": [],
                },
            ),
            patch.object(
                silent_gui_module,
                "_wait_for_session_window",
                side_effect=silent_gui_module.SessionObserveError(
                    "session has no live process", snapshot
                ),
            ),
            patch.object(
                silent_gui_module,
                "fail",
                side_effect=lambda error, **data: {"ok": False, "error": error, **data},
            ),
        ):
            result = silent_gui_module.cmd_message(
                {"session_id": "0123456789ab", "action": "key", "key": "return"}
            )
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["alive"])
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["windows"], [])
        self.assertIn("no live process", result["error"])


class PayloadDllResolutionTests(unittest.TestCase):
    """Injection DLL paths accept both a per-spawn param and an env default; the param
    must win so a caller can pick a DLL without touching global config."""

    def test_param_overrides_env(self) -> None:
        with patch.dict("os.environ", {"PI_SILENT_GUI_INJECT_DLL64": r"C:\env\p64.dll"}):
            resolved = silent_gui_module.resolve_payload_dll(
                {"inject_dll64": r"C:\call\p64.dll"},
                "inject_dll64",
                "PI_SILENT_GUI_INJECT_DLL64",
            )
        self.assertEqual(resolved, r"C:\call\p64.dll")

    def test_env_used_when_param_absent(self) -> None:
        with patch.dict("os.environ", {"PI_SILENT_GUI_INJECT_DLL64": r"C:\env\p64.dll"}):
            resolved = silent_gui_module.resolve_payload_dll(
                {}, "inject_dll64", "PI_SILENT_GUI_INJECT_DLL64"
            )
        self.assertEqual(resolved, r"C:\env\p64.dll")

    def test_empty_param_falls_back_to_env(self) -> None:
        with patch.dict("os.environ", {"PI_SILENT_GUI_INJECT_DLL32": r"C:\env\p32.dll"}):
            resolved = silent_gui_module.resolve_payload_dll(
                {"inject_dll32": ""}, "inject_dll32", "PI_SILENT_GUI_INJECT_DLL32"
            )
        self.assertEqual(resolved, r"C:\env\p32.dll")

    def test_none_when_neither_set(self) -> None:
        with patch.dict("os.environ", {"PI_SILENT_GUI_INJECT_DLL32": ""}):
            resolved = silent_gui_module.resolve_payload_dll(
                {}, "inject_dll32", "PI_SILENT_GUI_INJECT_DLL32"
            )
        self.assertIsNone(resolved)

    def test_non_string_param_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            silent_gui_module.resolve_payload_dll(
                {"inject_dll64": 123}, "inject_dll64", "PI_SILENT_GUI_INJECT_DLL64"
            )


if __name__ == "__main__":
    unittest.main()
