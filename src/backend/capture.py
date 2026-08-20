# -*- coding: utf-8 -*-
"""Capture one complete window with PrintWindow and write a lossless PNG."""
from __future__ import annotations

import ctypes
import os
import struct
import tempfile
import zlib
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import BinaryIO, Iterator

from common import _close_handles, _lexical_path, _open_directory_chain, _path_key

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

PW_RENDERFULLCONTENT = 2
DIB_RGB_COLORS = 0
BLACKNESS = 0x00000042
MAX_CAPTURE_PIXELS = 32 * 1024 * 1024
MAX_CAPTURE_SCANLINE_BYTES = 16 * 1024 * 1024
PNG_IDAT_CHUNK_BYTES = 64 * 1024
HGDI_ERROR = ctypes.c_void_p(-1).value

user32.GetWindowDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.restype = wintypes.HGDIOBJ


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.PatBlt.argtypes = [
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
gdi32.PatBlt.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [
    wintypes.HDC,
    wintypes.HBITMAP,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.LPVOID,
    ctypes.POINTER(BITMAPINFO),
    wintypes.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int


@contextmanager
def _prepared_output_path(
    out_path: str | Path, *, overwrite: bool
) -> Iterator[Path]:
    path = _lexical_path(out_path)
    # Denying delete sharing blocks path replacement through the commit. It is not a
    # sandbox against a hostile same-user process that can rewrite reparse metadata.
    kernel32, handles, _leaf = _open_directory_chain(path.parent, create=True)
    try:
        if not overwrite and path.exists():
            raise FileExistsError(f"output already exists: {path}")
        yield path
    finally:
        _close_handles(kernel32, handles)


def _write_png_chunk(
    out: BinaryIO, tag: bytes, data: bytes | bytearray | memoryview
) -> None:
    view = memoryview(data).cast("B")
    out.write(struct.pack(">I", len(view)))
    out.write(tag)
    out.write(view)
    crc = zlib.crc32(view, zlib.crc32(tag))
    out.write(struct.pack(">I", crc & 0xFFFFFFFF))


def _write_png_from_bgr(
    out: BinaryIO,
    width: int,
    height: int,
    bgr,
    dib_row: int,
) -> bool:
    """Stream a top-down 24-bit BGR DIB to PNG and report whether it is all black."""

    def write_idat(data: bytes) -> None:
        view = memoryview(data)
        for offset in range(0, len(view), PNG_IDAT_CHUNK_BYTES):
            _write_png_chunk(
                out, b"IDAT", view[offset : offset + PNG_IDAT_CHUNK_BYTES]
            )

    stride = width * 3
    row = bytearray(1 + stride)
    row[0] = 0
    pixels = memoryview(row)[1:]
    mv = memoryview(bgr).cast("B")
    compressor = zlib.compressobj(9)
    all_black = True

    out.write(b"\x89PNG\r\n\x1a\n")
    _write_png_chunk(
        out, b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    )
    for y in range(height):
        src = mv[y * dib_row : y * dib_row + stride]
        if all_black and any(src):
            all_black = False
        # BGR DIB -> RGB PNG, one scanline, no full-frame twin.
        pixels[0::3] = src[2::3]
        pixels[1::3] = src[1::3]
        pixels[2::3] = src[0::3]
        data = compressor.compress(row)
        if data:
            write_idat(data)
    data = compressor.flush()
    if data:
        write_idat(data)
    _write_png_chunk(out, b"IEND", b"")
    out.flush()
    os.fsync(out.fileno())
    return all_black


def _validated_pending_path(path: Path, pending_path: str | Path | None) -> Path | None:
    if pending_path is None:
        return None
    pending = _lexical_path(pending_path)
    if (
        _path_key(pending.parent) != _path_key(path.parent)
        or not pending.name.startswith(f".{path.name}.")
        or not pending.name.endswith(".tmp")
    ):
        raise ValueError("capture pending path must be a same-directory internal temp file")
    return pending


def _capture_window_png(
    hwnd: int,
    path: Path,
    *,
    window_size: tuple[int, int] | None,
    overwrite: bool,
    pending_path: str | Path | None,
) -> tuple[Path, int, int, bool]:
    requested_pending = _validated_pending_path(path, pending_path)
    if not user32.IsWindow(hwnd):
        raise LookupError(f"window no longer exists: hwnd={hwnd}")

    if window_size is None:
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise OSError(f"GetWindowRect failed: {ctypes.get_last_error()}")
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
    else:
        width, height = window_size
    # Preserve the full Window Rect so public coordinates match original PNG dimensions.
    if width <= 0 or height <= 0:
        raise OSError(f"empty window area {width}x{height}")
    if width * height > MAX_CAPTURE_PIXELS:
        raise OSError(f"window capture exceeds {MAX_CAPTURE_PIXELS} pixels: {width}x{height}")
    row = ((width * 3 + 3) // 4) * 4
    if row > MAX_CAPTURE_SCANLINE_BYTES:
        raise OSError(
            "window capture scanline exceeds "
            f"{MAX_CAPTURE_SCANLINE_BYTES} bytes: {row} bytes for width={width}"
        )

    window_dc = user32.GetWindowDC(hwnd)
    if not window_dc:
        raise OSError(f"GetWindowDC failed: {ctypes.get_last_error()}")
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    if not memory_dc:
        user32.ReleaseDC(hwnd, window_dc)
        raise OSError(f"CreateCompatibleDC failed: {ctypes.get_last_error()}")
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    if not bitmap:
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)
        raise OSError(f"CreateCompatibleBitmap failed: {ctypes.get_last_error()}")
    old = gdi32.SelectObject(memory_dc, bitmap)
    if not old or int(old) == HGDI_ERROR:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)
        raise OSError(f"SelectObject failed: {ctypes.get_last_error()}")

    selected = True
    # Never truncate the destination in place: a failed encode would already have
    # erased the previous file. Same-dir temp is the only uncommitted bytes; rename
    # refuses a pre-existing name, replace is the explicit overwrite commit.
    pending: Path | None = None
    try:
        # PrintWindow may leave regions untouched. Clear them now, before old memory
        # masquerades as pixels from a window that never painted there.
        if not gdi32.PatBlt(memory_dc, 0, 0, width, height, BLACKNESS):
            raise OSError(f"PatBlt failed: {ctypes.get_last_error()}")
        if not user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT):
            raise OSError(f"PrintWindow failed: {ctypes.get_last_error()}")

        # GetDIBits requires the bitmap to be deselected from the device context.
        restored = gdi32.SelectObject(memory_dc, old)
        if not restored or int(restored) == HGDI_ERROR:
            raise OSError(f"SelectObject restore failed: {ctypes.get_last_error()}")
        selected = False

        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 24
        info.bmiHeader.biCompression = 0
        buffer = (ctypes.c_ubyte * (row * height))()
        lines = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            ctypes.byref(buffer),
            ctypes.byref(info),
            DIB_RGB_COLORS,
        )
        if int(lines) != height:
            raise OSError(
                f"GetDIBits returned partial image: lines={int(lines)} expected={height} error={ctypes.get_last_error()}"
            )

        # O_EXCL creation closes the hard-link trap left by predictable temporary names.
        if requested_pending is None:
            stream_context = tempfile.NamedTemporaryFile(
                mode="w+b",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            )
        else:
            stream_context = requested_pending.open("x+b")
        with stream_context as stream:
            pending = Path(stream.name)
            # Preserve legitimate black frames and report all_black instead of guessing capture failure.
            all_black = _write_png_from_bgr(stream, width, height, buffer, row)
        if overwrite:
            os.replace(pending, path)
        else:
            os.rename(pending, path)
        pending = None
        return path, width, height, all_black
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
        if selected:
            restored = gdi32.SelectObject(memory_dc, old)
            if restored and int(restored) != HGDI_ERROR:
                selected = False
        if selected:
            # A bitmap selected into a surviving DC cannot be deleted. If the DC
            # refuses deletion too, leaking is the last honest state left to us.
            if gdi32.DeleteDC(memory_dc):
                memory_dc = None
                selected = False
        if not selected:
            gdi32.DeleteObject(bitmap)
            if memory_dc:
                gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


def print_window_png(
    hwnd: int,
    out_path: str | Path,
    *,
    window_size: tuple[int, int] | None = None,
    overwrite: bool = False,
    pending_path: str | Path | None = None,
) -> tuple[Path, int, int, bool]:
    with _prepared_output_path(out_path, overwrite=overwrite) as path:
        return _capture_window_png(
            hwnd,
            path,
            window_size=window_size,
            overwrite=overwrite,
            pending_path=pending_path,
        )
