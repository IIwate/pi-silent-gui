# -*- coding: utf-8 -*-
"""Create a real WASAPI render session that submits only zero PCM for smoke tests."""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import HRESULT, POINTER, c_ubyte
from ctypes import c_uint32 as UINT32
from pathlib import Path

import comtypes
from comtypes import COMMETHOD, GUID, IUnknown
from pycaw.api.audioclient import IAudioClient
from pycaw.utils import AudioUtilities

AUDCLNT_BUFFERFLAGS_SILENT = 0x2


class IAudioRenderClient(IUnknown):
    _iid_ = GUID("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "GetBuffer",
            (["in"], UINT32, "NumFramesRequested"),
            (["out"], POINTER(POINTER(c_ubyte)), "ppData"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "ReleaseBuffer",
            (["in"], UINT32, "NumFramesWritten"),
            (["in"], UINT32, "dwFlags"),
        ),
    )


def _fill(render, frames: int, bytes_per_frame: int) -> None:
    if frames <= 0:
        return
    buffer = render.GetBuffer(frames)
    ctypes.memset(buffer, 0, frames * bytes_per_frame)
    render.ReleaseBuffer(frames, AUDCLNT_BUFFERFLAGS_SILENT)


def main() -> int:
    marker = Path(sys.argv[1])
    device = AudioUtilities.GetSpeakers()
    if device is None:
        raise RuntimeError("no default render endpoint")
    interface = device._dev.Activate(IAudioClient._iid_, comtypes.CLSCTX_ALL, None)
    client = interface.QueryInterface(IAudioClient)
    mix = client.GetMixFormat()
    if not mix:
        raise RuntimeError("GetMixFormat returned None")
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    try:
        bytes_per_frame = int(mix.contents.nBlockAlign)
        client.Initialize(0, 0, 10_000_000, 0, mix, None)
    finally:
        ole32.CoTaskMemFree(mix)
    service = client.GetService(IAudioRenderClient._iid_)
    render = service.QueryInterface(IAudioRenderClient)
    capacity = int(client.GetBufferSize())
    _fill(render, capacity, bytes_per_frame)
    client.Start()
    try:
        marker.write_text(str(ctypes.windll.kernel32.GetCurrentProcessId()), encoding="ascii")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            _fill(render, capacity - int(client.GetCurrentPadding()), bytes_per_frame)
            time.sleep(0.02)
    finally:
        client.Stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
