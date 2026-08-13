# -*- coding: utf-8 -*-
"""Shared-memory input-state contract mirroring pi_silent_input.h (v1).

This module is the single Python source of truth for the layout the injected
payload reads. It must stay byte-for-byte identical to pi_silent_input.h; a
drift test pins the offsets so the two cannot diverge silently.
"""
from __future__ import annotations

import ctypes
import mmap

from common import validate_session_id

VERSION = 1
MAGIC = b"PSI1"
SIZE = 512
KEY_COUNT = 256
KEY_DOWN = 0x80
FLAG_ACTIVE = 0x1

SHM_ENV = "PI_SILENT_INPUT_SHM"
PIPE_ENV = "PI_SILENT_INPUT_PIPE"

_RESERVED = SIZE - 24 - KEY_COUNT


class InputState(ctypes.Structure):
    """Overlay for the mapping; field order and packing match the C struct."""

    _pack_ = 1
    _fields_ = [
        ("magic", ctypes.c_char * 4),
        ("version", ctypes.c_uint32),
        ("seq", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("cursor_x", ctypes.c_int32),
        ("cursor_y", ctypes.c_int32),
        ("keys", ctypes.c_uint8 * KEY_COUNT),
        ("reserved", ctypes.c_uint8 * _RESERVED),
    ]


assert ctypes.sizeof(InputState) == SIZE, "InputState must be exactly SIZE bytes"


def input_shm_name(session_id: str) -> str:
    """Mapping name shared by broker, helper, and payload.

    An unqualified name resolves in the session-local object namespace, so every
    same-session party (broker, per-call helper, injected payload on the private
    desktop) opens the same object. A Local\\ prefix would be equivalent but the
    stdlib mmap tag rejects backslashes, so the prefix stays implicit.
    """
    return f"pi_silent_input_{validate_session_id(session_id)}"


def input_pipe_name(session_id: str) -> str:
    return f"\\\\.\\pipe\\pi_silent_input_{validate_session_id(session_id)}"


class InputStateWriter:
    """Single-writer view over the mapping.

    The extension serializes operations per session, so there is exactly one
    writer at a time; the seqlock guards only against a payload reading a torn
    cursor pair mid-update, never against concurrent writers.
    """

    def __init__(self, name: str, create: bool):
        # mmap(-1, ...) both creates and opens by tag on Windows. When opening we
        # still map create-or-open, then reject a mapping that lacks our magic so
        # a helper cannot silently invent a fresh, broker-less table.
        self._mm = mmap.mmap(-1, SIZE, tagname=name)
        self._state = InputState.from_buffer(self._mm)
        if create:
            ctypes.memset(ctypes.byref(self._state), 0, SIZE)
            self._state.magic = MAGIC
            self._state.version = VERSION
        elif bytes(self._state.magic) != MAGIC or self._state.version != VERSION:
            # Drop the ctypes view first; mmap.close refuses while a buffer export lives.
            self._state = None
            self._mm.close()
            raise RuntimeError(f"input mapping {name!r} is not an armed v{VERSION} table")

    def _begin(self) -> None:
        self._state.seq += 1  # odd: write in progress

    def _end(self) -> None:
        self._state.seq += 1  # even: settled

    def arm(self) -> None:
        self._begin()
        self._state.flags |= FLAG_ACTIVE
        self._end()

    def reset(self) -> None:
        """Release everything. Called on kill/owner-loss so no key stays stuck."""
        self._begin()
        ctypes.memset(ctypes.byref(self._state.keys), 0, KEY_COUNT)
        self._state.cursor_x = 0
        self._state.cursor_y = 0
        self._state.flags &= ~FLAG_ACTIVE
        self._end()

    def set_key(self, vk: int, down: bool) -> None:
        if not 0 <= vk < KEY_COUNT:
            raise ValueError(f"vk out of range: {vk}")
        self._begin()
        self._state.keys[vk] = KEY_DOWN if down else 0
        self._end()

    def set_cursor(self, x: int, y: int) -> None:
        self._begin()
        self._state.cursor_x = int(x)
        self._state.cursor_y = int(y)
        self._end()

    def close(self) -> None:
        # Drop the ctypes view before the mmap so the buffer export is released.
        self._state = None
        self._mm.close()
