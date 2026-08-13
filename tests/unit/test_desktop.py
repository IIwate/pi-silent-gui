# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
import unittest
from unittest.mock import patch

from tests.support import backend as _backend  # noqa: F401

import desktop as desktop_module


class BrokerLaunchTests(unittest.TestCase):
    def test_shell_token_broker_returns_private_stdin_without_argv_payload(self) -> None:
        class Writer:
            def __init__(self) -> None:
                self.data = bytearray()

            def write(self, data: bytes) -> int:
                self.data.extend(data)
                return len(data)

            def close(self) -> None:
                return None

        writer = Writer()

        def create_process(_token, _flags, _app, _cmd, _creation, _env, _cwd, _si, out):
            info = ctypes.cast(
                out,
                ctypes.POINTER(desktop_module.PROCESS_INFORMATION),
            ).contents
            info.hProcess = 200
            info.hThread = 201
            info.dwProcessId = 42
            return True

        with (
            patch.object(desktop_module.os, "pipe", return_value=(10, 11)),
            patch.object(desktop_module.os, "set_inheritable"),
            patch.object(desktop_module.os, "close"),
            patch.object(desktop_module.os, "fdopen", return_value=writer),
            patch.object(desktop_module.msvcrt, "get_osfhandle", return_value=100),
            patch.object(desktop_module, "_nul_handle", return_value=300),
            patch.object(desktop_module, "_shell_token", return_value=400),
            patch.object(
                desktop_module,
                "_environment_from_token",
                return_value={"Path": "C:\\Windows"},
            ),
            patch.object(
                desktop_module.advapi32,
                "CreateProcessWithTokenW",
                side_effect=create_process,
            ) as create,
            patch.object(desktop_module.kernel32, "CloseHandle", return_value=True),
        ):
            process, stdin = desktop_module.launch_broker_with_shell_token(
                ["python.exe", "silent_gui.py", "broker", "--stdin-json"],
                cwd="C:/backend",
                env={"A": "B"},
            )
            self.assertIs(stdin, writer)
            self.assertEqual(writer.data, b"")
            process.close()

        command_line = create.call_args.args[3].value
        self.assertIn("--stdin-json", command_line)
        self.assertNotIn("sentinel", command_line)

    def test_shell_token_broker_env_uses_token_block_not_os_environ(self) -> None:
        class Writer:
            def write(self, data: bytes) -> int:
                return len(data)

            def close(self) -> None:
                return None

        captured: dict[str, object] = {}

        def create_process(_token, _flags, _app, _cmd, _creation, env_ptr, _cwd, _si, out):
            raw = ctypes.wstring_at(ctypes.addressof(env_ptr), len(env_ptr))
            captured["entries"] = [entry for entry in raw.split("\0") if entry]
            info = ctypes.cast(
                out,
                ctypes.POINTER(desktop_module.PROCESS_INFORMATION),
            ).contents
            info.hProcess = 200
            info.hThread = 201
            info.dwProcessId = 42
            return True

        with (
            patch.object(desktop_module.os, "pipe", return_value=(10, 11)),
            patch.object(desktop_module.os, "set_inheritable"),
            patch.object(desktop_module.os, "close"),
            patch.object(desktop_module.os, "fdopen", return_value=Writer()),
            patch.object(desktop_module.msvcrt, "get_osfhandle", return_value=100),
            patch.object(desktop_module, "_nul_handle", return_value=300),
            patch.object(desktop_module, "_shell_token", return_value=400),
            patch.object(
                desktop_module,
                "_environment_from_token",
                return_value={"Path": "from-token", "KEEP": "yes"},
            ) as from_token,
            patch.object(
                desktop_module.advapi32,
                "CreateProcessWithTokenW",
                side_effect=create_process,
            ),
            patch.object(desktop_module.kernel32, "CloseHandle", return_value=True),
            patch.object(
                desktop_module.os,
                "environ",
                {"Path": "from-admin", "SECRET": "leak-me"},
            ),
        ):
            process, _stdin = desktop_module.launch_broker_with_shell_token(
                ["python.exe", "silent_gui.py", "broker", "--stdin-json"],
                cwd="C:/backend",
                env={"PYTHONUTF8": "1", "path": "overlay"},
            )
            process.close()

        from_token.assert_called_once_with(400)
        entries = captured["entries"]
        assert isinstance(entries, list)
        self.assertEqual(sum(entry.casefold().startswith("path=") for entry in entries), 1)
        self.assertIn("path=overlay", entries)
        self.assertNotIn("KEEP=yes", entries)
        self.assertIn("PYTHONUTF8=1", entries)
        self.assertNotIn("SECRET=leak-me", entries)
        self.assertNotIn("Path=from-admin", entries)


class DesktopEnvironmentTests(unittest.TestCase):
    def test_environment_overrides_are_case_insensitive_and_double_nul_terminated(self) -> None:
        with patch.object(
            desktop_module,
            "_user_environment",
            return_value={"Path": "base", "KEEP": "yes"},
        ):
            buffer = desktop_module._environment_buffer(
                {"PATH": "override", "Added": "value"},
                clean_env=True,
            )
        raw = ctypes.wstring_at(ctypes.addressof(buffer), len(buffer))
        self.assertTrue(raw.endswith("\0\0"))
        entries = [entry for entry in raw.split("\0") if entry]
        self.assertEqual(sum(entry.casefold().startswith("path=") for entry in entries), 1)
        self.assertIn("PATH=override", entries)
        self.assertNotIn("KEEP=yes", entries)
        self.assertIn("Added=value", entries)

    def test_environment_overrides_reject_nul(self) -> None:
        for overrides in ({"BAD\0KEY": "x"}, {"GOOD": "bad\0value"}):
            with self.assertRaisesRegex(ValueError, "invalid Windows variable"):
                desktop_module._environment_buffer(overrides, clean_env=False)

    def test_create_environment_block_failure_is_fatal(self) -> None:
        with (
            patch.object(desktop_module.advapi32, "OpenProcessToken", return_value=True),
            patch.object(desktop_module.userenv, "CreateEnvironmentBlock", return_value=False),
            patch.object(desktop_module.kernel32, "CloseHandle", return_value=True),
            patch.object(ctypes, "get_last_error", return_value=5),
        ):
            with self.assertRaisesRegex(OSError, "CreateEnvironmentBlock failed: 5"):
                desktop_module._user_environment()

    def test_destroy_environment_block_failure_is_fatal(self) -> None:
        native = ctypes.create_unicode_buffer("A=B\0")

        def create_environment(out, _token, _inherit):
            ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(
                native, ctypes.c_void_p
            )
            return True

        with (
            patch.object(desktop_module.advapi32, "OpenProcessToken", return_value=True),
            patch.object(
                desktop_module.userenv,
                "CreateEnvironmentBlock",
                side_effect=create_environment,
            ),
            patch.object(desktop_module.userenv, "DestroyEnvironmentBlock", return_value=False),
            patch.object(desktop_module.kernel32, "CloseHandle", return_value=True),
            patch.object(ctypes, "get_last_error", return_value=6),
        ):
            with self.assertRaisesRegex(OSError, "DestroyEnvironmentBlock failed: 6"):
                desktop_module._user_environment()


if __name__ == "__main__":
    unittest.main()
