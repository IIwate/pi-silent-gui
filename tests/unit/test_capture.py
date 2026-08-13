# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import struct
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.support.backend import png_size

from common import new_session_id, remove_session_tmp, session_tmp
import capture as capture_module


def png_is_all_black(path: Path) -> bool:
    """Return True iff every pixel in the PNG is (0, 0, 0)."""
    import zlib

    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("not a PNG")
    # Skip signature (8) + IHDR length (4) + tag (4) + data (13) + CRC (4)
    i = 8 + 4 + 4 + 13 + 4
    compressed = bytearray()
    while i + 8 <= len(raw):
        length = struct.unpack_from(">I", raw, i)[0]
        tag = raw[i + 4:i + 8]
        if tag == b"IDAT":
            compressed.extend(raw[i + 8:i + 8 + length])
        elif tag == b"IEND":
            break
        i += 12 + length
    dec = zlib.decompress(bytes(compressed))
    return not any(dec)


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sid = new_session_id()
        self.out = session_tmp(self.sid) / "capture.png"
        self.addCleanup(remove_session_tmp, self.sid)

    @staticmethod
    def fakes() -> tuple[Mock, Mock]:
        user32 = Mock()
        gdi32 = Mock()

        def set_rect(_hwnd, rect) -> bool:
            rect._obj.left = 0
            rect._obj.top = 0
            rect._obj.right = 2
            rect._obj.bottom = 2
            return True

        user32.IsWindow.return_value = True
        user32.GetWindowRect.side_effect = set_rect
        user32.GetWindowDC.return_value = 1
        user32.PrintWindow.return_value = True
        user32.ReleaseDC.return_value = 1
        gdi32.CreateCompatibleDC.return_value = 2
        gdi32.CreateCompatibleBitmap.return_value = 3
        gdi32.SelectObject.return_value = 4
        gdi32.PatBlt.return_value = True
        gdi32.DeleteObject.return_value = True
        gdi32.DeleteDC.return_value = True
        gdi32.GetDIBits.return_value = 2
        return user32, gdi32

    def test_black_capture_is_valid_png(self) -> None:
        user32, gdi32 = self.fakes()
        with (
            patch.object(capture_module, "user32", user32),
            patch.object(capture_module, "gdi32", gdi32),
        ):
            path, width, height, all_black = capture_module.print_window_png(1, self.out)
        self.assertEqual(path, self.out.resolve())
        self.assertEqual((width, height, all_black), (2, 2, True))
        self.assertEqual(png_size(path), (2, 2))
        self.assertGreater(path.stat().st_size, 0)

    def test_bitmap_clear_failure_stops_before_printwindow(self) -> None:
        user32, gdi32 = self.fakes()
        gdi32.PatBlt.return_value = False
        with (
            patch.object(capture_module, "user32", user32),
            patch.object(capture_module, "gdi32", gdi32),
            self.assertRaisesRegex(OSError, "PatBlt failed"),
        ):
            capture_module.print_window_png(1, self.out)
        gdi32.PatBlt.assert_called_once_with(
            2, 0, 0, 2, 2, capture_module.BLACKNESS
        )
        user32.PrintWindow.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_printwindow_failure_is_not_retried(self) -> None:
        user32, gdi32 = self.fakes()
        user32.PrintWindow.return_value = False
        with (
            patch.object(capture_module, "user32", user32),
            patch.object(capture_module, "gdi32", gdi32),
            self.assertRaisesRegex(OSError, "PrintWindow failed"),
        ):
            capture_module.print_window_png(1, self.out)
        self.assertEqual(user32.PrintWindow.call_count, 1)
        self.assertFalse(self.out.exists())

    def test_scanline_limit_fails_before_gdi_allocation(self) -> None:
        user32, gdi32 = self.fakes()
        width = capture_module.MAX_CAPTURE_SCANLINE_BYTES // 3 + 1
        with (
            patch.object(capture_module, "user32", user32),
            patch.object(capture_module, "gdi32", gdi32),
            self.assertRaisesRegex(OSError, "capture scanline exceeds.*bytes"),
        ):
            capture_module.print_window_png(1, self.out, window_size=(width, 1))
        user32.GetWindowDC.assert_not_called()

    def test_partial_getdibits_fails_closed(self) -> None:
        user32, gdi32 = self.fakes()
        gdi32.GetDIBits.return_value = 1
        with (
            patch.object(capture_module, "user32", user32),
            patch.object(capture_module, "gdi32", gdi32),
            self.assertRaisesRegex(OSError, "partial image"),
        ):
            capture_module.print_window_png(1, self.out)
        self.assertFalse(self.out.exists())

    def test_idat_output_is_split_into_fixed_size_chunks(self) -> None:
        compressor = Mock()
        compressor.compress.return_value = b"x" * (
            capture_module.PNG_IDAT_CHUNK_BYTES * 2 + 1
        )
        compressor.flush.return_value = b""
        with (
            self.out.open("w+b") as stream,
            patch.object(capture_module.zlib, "compressobj", return_value=compressor),
        ):
            capture_module._write_png_from_bgr(stream, 1, 1, b"\0\0\0\0", 4)

        raw = self.out.read_bytes()
        offset = 8
        idat_lengths = []
        while offset < len(raw):
            length = struct.unpack_from(">I", raw, offset)[0]
            tag = raw[offset + 4 : offset + 8]
            if tag == b"IDAT":
                idat_lengths.append(length)
            offset += 12 + length
        self.assertEqual(
            idat_lengths,
            [
                capture_module.PNG_IDAT_CHUNK_BYTES,
                capture_module.PNG_IDAT_CHUNK_BYTES,
                1,
            ],
        )

    def test_restore_failure_keeps_selected_bitmap_when_dc_survives(self) -> None:
        user32, gdi32 = self.fakes()
        gdi32.SelectObject.side_effect = [4, 0, 0]
        gdi32.DeleteDC.return_value = False
        with (
            patch.object(capture_module, "user32", user32),
            patch.object(capture_module, "gdi32", gdi32),
            self.assertRaisesRegex(OSError, "SelectObject restore failed"),
        ):
            capture_module.print_window_png(1, self.out)
        gdi32.DeleteDC.assert_called_once_with(2)
        gdi32.DeleteObject.assert_not_called()
        user32.ReleaseDC.assert_called_once_with(1, 1)

    def test_default_refuses_existing_file_and_preserves_content(self) -> None:
        self.out.write_bytes(b"original content")
        user32, gdi32 = self.fakes()
        with (
            patch.object(capture_module, "user32", user32),
            patch.object(capture_module, "gdi32", gdi32),
            self.assertRaises(FileExistsError),
        ):
            capture_module.print_window_png(1, self.out)
        self.assertEqual(self.out.read_bytes(), b"original content")

    def test_overwrite_atomic_replace_and_hardlink(self) -> None:
        self.out.write_bytes(b"old content")
        link = self.out.with_name(self.out.name + ".link")
        os.link(str(self.out), str(link))
        user32, gdi32 = self.fakes()
        with (
            patch.object(capture_module, "user32", user32),
            patch.object(capture_module, "gdi32", gdi32),
        ):
            path, w, h, black = capture_module.print_window_png(1, self.out, overwrite=True)
        self.assertEqual(path, self.out.resolve())
        self.assertEqual(png_size(path), (2, 2))
        self.assertTrue(black)
        self.assertEqual(link.read_bytes(), b"old content")
        tmp = self.out.with_name(f".{self.out.name}.{os.getpid()}.tmp")
        self.assertFalse(tmp.exists())

    def test_black_png_pixels_all_zero(self) -> None:
        user32, gdi32 = self.fakes()
        with (
            patch.object(capture_module, "user32", user32),
            patch.object(capture_module, "gdi32", gdi32),
        ):
            path, width, height, black = capture_module.print_window_png(1, self.out)
        self.assertEqual((width, height), (2, 2))
        self.assertTrue(black)
        self.assertTrue(png_is_all_black(path))

    def test_caller_owned_random_pending_name_is_committed_and_removed(self) -> None:
        user32, gdi32 = self.fakes()
        pending = self.out.with_name(f".{self.out.name}.{'a' * 32}.tmp")
        with (
            patch.object(capture_module, "user32", user32),
            patch.object(capture_module, "gdi32", gdi32),
        ):
            path, _width, _height, _black = capture_module.print_window_png(
                1,
                self.out,
                pending_path=pending,
            )

        self.assertEqual(path, self.out.resolve())
        self.assertFalse(pending.exists())

    def test_pending_name_must_be_bound_to_the_output_directory(self) -> None:
        outside = self.out.parent.parent / f".{self.out.name}.{'b' * 32}.tmp"
        with self.assertRaisesRegex(ValueError, "same-directory internal temp"):
            capture_module.print_window_png(1, self.out, pending_path=outside)

    def test_write_failure_cleans_temp_file(self) -> None:
        user32, gdi32 = self.fakes()
        with (
            patch.object(capture_module, "user32", user32),
            patch.object(capture_module, "gdi32", gdi32),
            patch.object(capture_module.os, "replace") as mock_replace,
        ):
            mock_replace.side_effect = OSError("write failed")
            with self.assertRaises(OSError):
                capture_module.print_window_png(1, self.out, overwrite=True)
        self.assertEqual(list(self.out.parent.glob(f".{self.out.name}.*.tmp")), [])

    def test_reparse_parent_refused(self) -> None:
        user32, gdi32 = self.fakes()
        with (
            patch.object(capture_module, "user32", user32),
            patch.object(capture_module, "gdi32", gdi32),
            patch.object(
                capture_module,
                "_open_directory_chain",
                side_effect=OSError("reparse point rejected"),
            ),
            self.assertRaisesRegex(OSError, "reparse"),
        ):
            capture_module.print_window_png(1, self.out)


if __name__ == "__main__":
    unittest.main()
