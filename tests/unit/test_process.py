# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from tests.support import backend as _backend  # noqa: F401

import process as process_module


class ProcessIdentityTests(unittest.TestCase):
    def test_terminate_uses_one_verified_handle(self) -> None:
        kernel32 = Mock()
        kernel32.TerminateProcess.return_value = True
        kernel32.WaitForSingleObject.return_value = process_module.WAIT_OBJECT_0
        kernel32.CloseHandle.return_value = True
        with (
            patch.object(process_module, "kernel32", kernel32),
            patch.object(process_module, "_open", return_value=123) as opened,
            patch.object(process_module, "process_creation_time_from_handle", return_value=456),
            patch.object(
                process_module,
                "_exit_code",
                side_effect=[process_module.STILL_ACTIVE, 0],
            ),
        ):
            self.assertEqual(process_module.terminate_same_process(7, 456), "killed")
        opened.assert_called_once()
        kernel32.TerminateProcess.assert_called_once()
        kernel32.WaitForSingleObject.assert_called_once()

    def test_pid_reuse_is_not_terminated(self) -> None:
        kernel32 = Mock()
        kernel32.CloseHandle.return_value = True
        with (
            patch.object(process_module, "kernel32", kernel32),
            patch.object(process_module, "_open", return_value=321) as opened,
            patch.object(process_module, "process_creation_time_from_handle", return_value=999),
        ):
            self.assertEqual(process_module.terminate_same_process(7, 456), "mismatch")
        opened.assert_called_once()
        kernel32.TerminateProcess.assert_not_called()

    def test_exit_code_is_none_while_running_or_unverifiable(self) -> None:
        kernel32 = Mock()
        with (
            patch.object(process_module, "kernel32", kernel32),
            patch.object(process_module, "_open", return_value=123),
            patch.object(process_module, "process_creation_time_from_handle", return_value=456),
            patch.object(process_module, "_exit_code", return_value=process_module.STILL_ACTIVE),
        ):
            self.assertIsNone(process_module.process_exit_code(7, 456))
        with (
            patch.object(process_module, "_open", return_value=123),
            patch.object(process_module, "process_creation_time_from_handle", return_value=999),
            patch.object(process_module, "_exit_code", return_value=1),
        ):
            self.assertIsNone(process_module.process_exit_code(7, 456))
        with patch.object(process_module, "_open", return_value=None):
            self.assertIsNone(process_module.process_exit_code(7, 456))

    def test_exit_code_returns_the_spawned_process_code(self) -> None:
        kernel32 = Mock()
        with (
            patch.object(process_module, "kernel32", kernel32),
            patch.object(process_module, "_open", return_value=123),
            patch.object(process_module, "process_creation_time_from_handle", return_value=456),
            patch.object(process_module, "_exit_code", return_value=7),
        ):
            self.assertEqual(process_module.process_exit_code(7, 456), 7)

    def test_identity_query_failure_is_not_reported_as_process_death(self) -> None:
        with (
            patch.object(process_module, "_open", return_value=None),
            patch.object(process_module.ctypes, "get_last_error", return_value=5),
        ):
            self.assertEqual(
                process_module.process_identity_status(7, 456),
                "unverifiable",
            )
            self.assertFalse(process_module.same_process(7, 456))


class OwnerWatchTests(unittest.TestCase):
    """Owner watch handles require matching identity and a live process."""

    def test_live_matching_identity_is_watchable(self) -> None:
        kernel32 = Mock()
        with (
            patch.object(process_module, "kernel32", kernel32),
            patch.object(process_module, "_open", return_value=555),
            patch.object(process_module, "process_creation_time_from_handle", return_value=42),
            patch.object(process_module, "_exit_code", return_value=process_module.STILL_ACTIVE),
        ):
            self.assertEqual(process_module.open_process_watch(9, 42), 555)
        kernel32.CloseHandle.assert_not_called()

    def test_exited_process_is_rejected(self) -> None:
        # External handles may keep exited-process identity metadata readable.
        kernel32 = Mock()
        with (
            patch.object(process_module, "kernel32", kernel32),
            patch.object(process_module, "_open", return_value=555),
            patch.object(process_module, "process_creation_time_from_handle", return_value=42),
            patch.object(process_module, "_exit_code", return_value=0),
        ):
            self.assertIsNone(process_module.open_process_watch(9, 42))
        kernel32.CloseHandle.assert_called_once()

    def test_recycled_pid_is_rejected(self) -> None:
        kernel32 = Mock()
        with (
            patch.object(process_module, "kernel32", kernel32),
            patch.object(process_module, "_open", return_value=555),
            patch.object(process_module, "process_creation_time_from_handle", return_value=999),
        ):
            self.assertIsNone(process_module.open_process_watch(9, 42))
        kernel32.CloseHandle.assert_called_once()

    def test_handle_liveness_never_reresolves_pid(self) -> None:
        with (
            patch.object(process_module, "kernel32", Mock()),
            patch.object(process_module, "_open") as opened,
            patch.object(
                process_module,
                "_exit_code",
                side_effect=[process_module.STILL_ACTIVE, 0],
            ),
        ):
            self.assertTrue(process_module.process_handle_alive(555))
            self.assertFalse(process_module.process_handle_alive(555))
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
