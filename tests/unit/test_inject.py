# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
import os
import re
import secrets
import time
import unittest
import unittest.mock
from ctypes import wintypes
from pathlib import Path

from tests.support import backend as _backend  # noqa: F401

import inject
import inject_ipc
import inject_shm
from input_dispatch import VK_LBUTTON, InjectDispatcher

_HEADER = Path(__file__).resolve().parents[2] / "src" / "backend" / "pi_silent_input.h"


def _header_defines() -> dict[str, str]:
    defines: dict[str, str] = {}
    for line in _HEADER.read_text(encoding="utf-8").splitlines():
        match = re.match(r"#define\s+(PI_SILENT_INPUT_\w+)\s+(.+?)\s*$", line)
        if match:
            defines[match.group(1)] = match.group(2)
    return defines


class ContractLayoutTests(unittest.TestCase):
    """The Python mirror must not drift from the C header the payload compiles against."""

    def test_struct_is_exactly_the_declared_size(self) -> None:
        self.assertEqual(ctypes.sizeof(inject_shm.InputState), inject_shm.SIZE)

    def test_field_offsets_match_the_documented_layout(self) -> None:
        state = inject_shm.InputState
        self.assertEqual(state.magic.offset, 0)
        self.assertEqual(state.version.offset, 4)
        self.assertEqual(state.seq.offset, 8)
        self.assertEqual(state.flags.offset, 12)
        self.assertEqual(state.cursor_x.offset, 16)
        self.assertEqual(state.cursor_y.offset, 20)
        self.assertEqual(state.keys.offset, 24)
        self.assertEqual(state.reserved.offset, 24 + inject_shm.KEY_COUNT)

    def test_header_constants_match_python(self) -> None:
        defines = _header_defines()
        self.assertEqual(int(defines["PI_SILENT_INPUT_VERSION"].rstrip("u")), inject_shm.VERSION)
        self.assertEqual(int(defines["PI_SILENT_INPUT_SIZE"].rstrip("u")), inject_shm.SIZE)
        self.assertEqual(int(defines["PI_SILENT_INPUT_KEY_COUNT"].rstrip("u")), inject_shm.KEY_COUNT)
        self.assertEqual(int(defines["PI_SILENT_INPUT_KEY_DOWN"].rstrip("u"), 16), inject_shm.KEY_DOWN)
        self.assertEqual(int(defines["PI_SILENT_INPUT_FLAG_ACTIVE"].rstrip("u"), 16), inject_shm.FLAG_ACTIVE)
        self.assertEqual(defines["PI_SILENT_INPUT_MAGIC"].strip('"').encode(), inject_shm.MAGIC)
        self.assertEqual(defines["PI_SILENT_INPUT_SHM_ENV"].strip('"'), inject_shm.SHM_ENV)
        self.assertEqual(defines["PI_SILENT_INPUT_PIPE_ENV"].strip('"'), inject_shm.PIPE_ENV)
        self.assertEqual(defines["PI_SILENT_INPUT_PROTO"].strip('"'), inject_ipc.PROTO)


class NameDerivationTests(unittest.TestCase):
    def test_names_are_stable_and_session_scoped(self) -> None:
        self.assertEqual(inject_shm.input_shm_name("0123456789ab"), "pi_silent_input_0123456789ab")
        self.assertEqual(
            inject_shm.input_pipe_name("0123456789ab"), r"\\.\pipe\pi_silent_input_0123456789ab"
        )

    def test_names_reject_invalid_session_id(self) -> None:
        for bad in ("", "XYZ", "0123456789ab_extra", "../escape"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    inject_shm.input_shm_name(bad)


class SharedMemoryWriterTests(unittest.TestCase):
    def _fresh_name(self) -> str:
        return f"pi_silent_input_test_{os.getpid()}_{time.perf_counter_ns():x}"[:120]

    def test_state_is_visible_across_handles(self) -> None:
        name = self._fresh_name()
        writer = inject_shm.InputStateWriter(name, create=True)
        self.addCleanup(writer.close)
        writer.arm()
        writer.set_key(0x11, True)
        writer.set_cursor(11, 22)

        reader = inject_shm.InputStateWriter(name, create=False)
        self.addCleanup(reader.close)
        self.assertEqual(reader._state.keys[0x11], inject_shm.KEY_DOWN)
        self.assertEqual(reader._state.flags & inject_shm.FLAG_ACTIVE, inject_shm.FLAG_ACTIVE)
        self.assertEqual((reader._state.cursor_x, reader._state.cursor_y), (11, 22))

    def test_reset_releases_keys_and_clears_active(self) -> None:
        name = self._fresh_name()
        writer = inject_shm.InputStateWriter(name, create=True)
        self.addCleanup(writer.close)
        writer.arm()
        writer.set_key(0x11, True)
        writer.reset()
        self.assertEqual(writer._state.keys[0x11], 0)
        self.assertEqual(writer._state.flags & inject_shm.FLAG_ACTIVE, 0)

    def test_opening_an_unarmed_name_fails_closed(self) -> None:
        # create=False on a never-armed name must not silently mint a fresh table.
        with self.assertRaisesRegex(RuntimeError, "not an armed"):
            inject_shm.InputStateWriter(self._fresh_name(), create=False)


class InjectDispatcherTests(unittest.TestCase):
    def _armed(self) -> tuple[str, inject_shm.InputStateWriter]:
        session_id = f"{os.getpid():012x}"[:12]
        writer = inject_shm.InputStateWriter(inject_shm.input_shm_name(session_id), create=True)
        writer.arm()
        self.addCleanup(writer.close)
        return session_id, writer

    def test_click_places_cursor_and_releases_button(self) -> None:
        session_id, writer = self._armed()
        dispatcher = InjectDispatcher(session_id)
        self.addCleanup(dispatcher.close)
        dispatcher.click(640, 400)
        # The cursor is left in place for a polling engine; the button is a tap.
        self.assertEqual((writer._state.cursor_x, writer._state.cursor_y), (640, 400))
        self.assertEqual(writer._state.keys[VK_LBUTTON], 0)

    def test_open_without_armed_table_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            InjectDispatcher("ffffffffffff")


def _kernel32_paths() -> list[str]:
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    candidates = [
        os.path.join(windir, "System32", "kernel32.dll"),
        os.path.join(windir, "SysWOW64", "kernel32.dll"),
    ]
    return [path for path in candidates if os.path.isfile(path)]


class InjectorSelectionTests(unittest.TestCase):
    def test_select_payload_matches_bitness(self) -> None:
        self.assertEqual(inject.select_payload(32, "a.dll", "b.dll"), "a.dll")
        self.assertEqual(inject.select_payload(64, "a.dll", "b.dll"), "b.dll")

    def test_host_bitness_is_32_or_64(self) -> None:
        self.assertIn(inject.host_bitness(), (32, 64))

    def test_missing_payload_for_target_bitness_fails(self) -> None:
        with unittest.mock.patch.object(inject, "target_bitness", return_value=inject.host_bitness()):
            with self.assertRaisesRegex(inject.InjectionError, "no payload configured"):
                inject.inject_payload(0, 0, None, None)

    def test_uninitialized_target_degrades_instead_of_injecting(self) -> None:
        # If the loader never maps kernel32, injection must fail (caller drops to
        # message mode) rather than resolve LoadLibraryW against a missing module.
        with unittest.mock.patch.object(inject, "target_bitness", return_value=inject.host_bitness()), \
                unittest.mock.patch.object(inject, "wait_until_initialized", return_value=False):
            with self.assertRaisesRegex(inject.InjectionError, "did not map kernel32"):
                inject.inject_payload(0, 0, "a.dll", "b.dll")


class ExportResolutionTests(unittest.TestCase):
    """LoadLibraryW address resolution is the load-bearing part of cross-bitness
    injection; assert the on-disk PE parse against the real kernel32 flavors so a
    64-bit backend can trust the RVA it will add to a 32-bit target's base."""

    def test_loadlibraryw_resolves_in_every_kernel32(self) -> None:
        paths = _kernel32_paths()
        self.assertTrue(paths, "no kernel32 found to parse")
        for path in paths:
            with self.subTest(path=path):
                rva = inject.export_rva(path, "LoadLibraryW")
                self.assertGreater(rva, 0)

    def test_missing_export_raises(self) -> None:
        path = _kernel32_paths()[0]
        with self.assertRaisesRegex(inject.InjectionError, "not exported"):
            inject.export_rva(path, "ThisSymbolDoesNotExist___")


_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def _write_pipe_line(name: str, line: bytes, retries: int = 50) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = None
    for _ in range(retries):
        handle = kernel32.CreateFileW(name, _GENERIC_WRITE, 0, None, _OPEN_EXISTING, 0, None)
        if handle and int(handle) != _INVALID_HANDLE:
            break
        time.sleep(0.02)  # server may not have reached ConnectNamedPipe yet
    if not handle or int(handle) == _INVALID_HANDLE:
        raise OSError(f"could not open pipe {name!r}: {ctypes.get_last_error()}")
    written = wintypes.DWORD(0)
    try:
        kernel32.WriteFile(wintypes.HANDLE(int(handle)), line, len(line), ctypes.byref(written), None)
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(int(handle)))


class HandshakeServerTests(unittest.TestCase):
    def _pipe_name(self) -> str:
        return inject_shm.input_pipe_name(secrets.token_hex(6))

    def test_accepts_a_valid_handshake(self) -> None:
        name = self._pipe_name()
        server = inject_ipc.HandshakeServer(name)
        self.addCleanup(server.close)
        _write_pipe_line(
            name,
            b'{"proto":"pi-silent-input","version":1,"ok":true,"pid":9,"bitness":64,"hooks":["GetAsyncKeyState"]}\n',
        )
        hello = server.wait(3.0)
        self.assertIsNotNone(hello)
        self.assertTrue(hello["ok"])
        self.assertEqual(hello["hooks"], ["GetAsyncKeyState"])

    def test_malformed_handshake_degrades(self) -> None:
        name = self._pipe_name()
        server = inject_ipc.HandshakeServer(name)
        self.addCleanup(server.close)
        _write_pipe_line(name, b"not json at all\n")
        self.assertIsNone(server.wait(3.0))
        self.assertIsNotNone(server.error())

    def test_absent_payload_times_out(self) -> None:
        name = self._pipe_name()
        server = inject_ipc.HandshakeServer(name)
        self.addCleanup(server.close)
        started = time.perf_counter()
        self.assertIsNone(server.wait(0.3))
        self.assertLess(time.perf_counter() - started, 2.5)


if __name__ == "__main__":
    unittest.main()
