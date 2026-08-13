# -*- coding: utf-8 -*-
"""Inject-mode input dispatch: drive a polling engine by writing shared memory.

This is the second implementation of the input port. Message-mode dispatch stays
in window.py and reaches message-driven engines; this reaches polling engines
(GetAsyncKeyState / DirectInput / RawInput / GetCursorPos) by making their polls
read InputStateWriter. The two are selected per session by input_mode; neither
knows about the other.
"""
from __future__ import annotations

import time

from inject_shm import InputStateWriter, input_shm_name

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
TAP_HOLD_SECONDS = 0.03
MAX_HOLD_SECONDS = 10.0


class InjectDispatcher:
    """Open the session's armed table and translate actions into held state.

    A failed open means the payload never armed this session, so the caller must
    surface that rather than pretend the input landed.
    """

    def __init__(self, session_id: str):
        self._writer = InputStateWriter(input_shm_name(session_id), create=False)

    def press(self, vk: int, hold_seconds: float = TAP_HOLD_SECONDS) -> None:
        hold = max(0.0, min(MAX_HOLD_SECONDS, hold_seconds))
        self._writer.set_key(vk, True)
        try:
            time.sleep(hold)
        finally:
            # Release even if interrupted; a stuck key would keep skipping forever.
            self._writer.set_key(vk, False)

    def click(
        self, screen_x: int, screen_y: int, vk: int = VK_LBUTTON, hold_seconds: float = TAP_HOLD_SECONDS
    ) -> None:
        # A polling engine reads the cursor separately from the button, so place
        # the cursor before the button goes down and leave it there afterwards.
        self._writer.set_cursor(screen_x, screen_y)
        self.press(vk, hold_seconds)

    def close(self) -> None:
        self._writer.close()
