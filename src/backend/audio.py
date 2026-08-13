# -*- coding: utf-8 -*-
"""Mute target audio sessions on every active WASAPI render endpoint."""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from collections.abc import Callable

# Keep later comtypes imports on MTA; start() also initializes and verifies this thread.
sys.coinit_flags = 0

COINIT_MULTITHREADED = 0
RPC_E_CHANGED_MODE = 0x80010106
E_RENDER = 0
MAX_STABILIZE_PASSES = 8

# WASAPI can emit topology notifications faster than one stabilization sweep. Dynamic
# mode tolerates that churn only after one stable baseline has proved that every active
# endpoint was registered and swept. Enumeration failure is never safe to tolerate: a
# newly active endpoint could otherwise play before the next successful refresh.
MIN_SWEEP_INTERVAL = 0.05

ole32 = ctypes.WinDLL("ole32")
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None


class TransientTopologyError(RuntimeError):
    """Endpoint enumeration failed because topology changed during the snapshot.

    This type permits retries within one sweep. Do not wrap deterministic guard failures.
    """


def _session_pid(session) -> int:
    try:
        pid = int(session.ProcessId)
    except Exception as error:
        raise RuntimeError(f"cannot read audio session ProcessId: {error}") from error
    if pid < 0:
        raise RuntimeError(f"invalid audio session ProcessId: {pid}")
    return pid


class AudioGuard:
    def __init__(
        self,
        pids: Callable[[], list[int]],
        policy: str = "dynamic",
    ):
        if policy not in ("dynamic", "strict"):
            raise ValueError(f"invalid audio_device_policy: {policy}")
        self._pids = pids
        self._policy = policy
        self._device_enumerator = None
        self._device_callback = None
        self._session_callback = None
        self._managers: dict[str, object] = {}
        self._topology: tuple | None = None
        self._render_ids: set[str] = set()
        self._armed = False
        self._ready = False
        self._fatal: str | None = None
        self._generation = 0
        self._processed_generation = 0
        self._com_initialized = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._unstable_sweeps = 0
        self._enumeration_failures = 0
        # Zero means no sweep has run, so the first wait is not delayed by the interval floor.
        self._last_sweep = 0.0

    def _initialize_com(self) -> None:
        result = int(ole32.CoInitializeEx(None, COINIT_MULTITHREADED)) & 0xFFFFFFFF
        if result == RPC_E_CHANGED_MODE:
            raise RuntimeError("audio guard requires an MTA COM thread")
        if result not in (0, 1):
            raise OSError(f"CoInitializeEx(MTA) failed: 0x{result:08x}")
        self._com_initialized = True

    def _set_fatal(self, error: Exception | str) -> None:
        with self._lock:
            if self._fatal is None:
                self._fatal = str(error)
        self._wake.set()

    def fatal_error(self) -> str | None:
        with self._lock:
            return self._fatal

    def is_ready(self) -> bool:
        """Return whether one stable full-endpoint sweep completed successfully."""
        with self._lock:
            return self._ready

    def _generation_snapshot(self) -> int:
        with self._lock:
            return self._generation

    def has_pending_notification(self) -> bool:
        with self._lock:
            return self._generation != self._processed_generation

    def _device_flow(self, device_id: str) -> int:
        from pycaw.api.mmdeviceapi import IMMEndpoint

        if self._device_enumerator is None:
            raise RuntimeError("audio device enumerator is unavailable")
        device = self._device_enumerator.GetDevice(device_id)
        if device is None:
            raise RuntimeError(f"audio endpoint disappeared before classification: {device_id}")
        return int(device.QueryInterface(IMMEndpoint).GetDataFlow())

    def _topology_notification(
        self,
        kind: str,
        device_id: str | None = None,
        flow_id: int | None = None,
    ) -> None:
        with self._lock:
            self._generation += 1
            strict = self._armed and self._policy == "strict"
            known_render = bool(device_id and device_id in self._render_ids)
        self._wake.set()
        if not strict:
            return
        if kind == "default":
            if flow_id == E_RENDER:
                self._set_fatal("audio render endpoint default changed under strict policy")
            return
        if kind == "removed":
            if known_render:
                self._set_fatal(
                    f"audio render endpoint topology changed under strict policy: {kind} {device_id}"
                )
            return
        if kind in ("added", "state"):
            try:
                is_render = known_render or (
                    device_id is not None and self._device_flow(device_id) == E_RENDER
                )
            except Exception as error:
                self._set_fatal(
                    f"cannot classify audio endpoint change under strict policy: {error}"
                )
                return
            if is_render:
                self._set_fatal(
                    f"audio render endpoint topology changed under strict policy: {kind} {device_id}"
                )

    def _mute_session(self, session, pids: set[int] | None = None) -> bool:
        pid = _session_pid(session)
        if pid == 0:
            return False
        if pid not in (pids if pids is not None else set(self._pids())):
            return False
        try:
            volume = session.SimpleAudioVolume
            if volume is None:
                raise RuntimeError("SimpleAudioVolume is None")
            volume.SetMute(1, None)
            return True
        except Exception as error:
            raise RuntimeError(f"SetMute failed for session pid={pid}: {error}") from error

    @staticmethod
    def _manager_sessions(manager) -> list:
        try:
            from pycaw.api.audiopolicy import IAudioSessionControl2
            from pycaw.utils import AudioSession

            enumerator = manager.GetSessionEnumerator()
            if enumerator is None:
                raise RuntimeError("GetSessionEnumerator returned None")
            sessions = []
            for index in range(int(enumerator.GetCount())):
                control = enumerator.GetSession(index)
                if control is None:
                    raise RuntimeError(f"GetSession({index}) returned None")
                sessions.append(AudioSession(control.QueryInterface(IAudioSessionControl2)))
            return sessions
        except Exception as error:
            raise RuntimeError(f"audio session enumeration failed: {error}") from error

    def _sweep_manager(self, manager, pids: set[int]) -> int:
        return sum(self._mute_session(session, pids) for session in self._manager_sessions(manager))

    def _enumerate_topology(self) -> tuple[tuple, dict[str, object]]:
        try:
            from _ctypes import COMError
            from pycaw.constants import DEVICE_STATE, EDataFlow

            collection = self._device_enumerator.EnumAudioEndpoints(
                EDataFlow.eRender.value,
                DEVICE_STATE.MASK_ALL.value,
            )
            if collection is None:
                raise RuntimeError("EnumAudioEndpoints returned None")
            states: list[tuple[str, int]] = []
            active: dict[str, object] = {}
            for index in range(int(collection.GetCount())):
                device = collection.Item(index)
                if device is None:
                    raise RuntimeError(f"render endpoint Item({index}) returned None")
                device_id = str(device.GetId())
                state = int(device.GetState())
                if any(existing_id == device_id for existing_id, _ in states):
                    raise RuntimeError(f"duplicate render endpoint id: {device_id}")
                states.append((device_id, state))
                if state == DEVICE_STATE.ACTIVE.value:
                    active[device_id] = device

            defaults: list[str | None] = []
            for role in range(3):
                try:
                    device = self._device_enumerator.GetDefaultAudioEndpoint(
                        EDataFlow.eRender.value,
                        role,
                    )
                    defaults.append(str(device.GetId()) if device is not None else None)
                except COMError as error:
                    if int(error.hresult) & 0xFFFFFFFF != 0x80070490:
                        raise
                    defaults.append(None)
            return (tuple(sorted(states)), tuple(defaults)), active
        except Exception as error:
            # An endpoint may disappear during GetId/GetState. Let sweep retry this snapshot
            # rather than classifying normal device churn as a deterministic guard failure.
            raise TransientTopologyError(f"audio endpoint refresh failed: {error}") from error

    def _register_endpoint(self, device_id: str, device) -> None:
        manager = None
        registered = False
        try:
            import comtypes
            from pycaw.api.audiopolicy import IAudioSessionManager2

            interface = device.Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None)
            manager = interface.QueryInterface(IAudioSessionManager2)
            if manager is None:
                raise RuntimeError("IAudioSessionManager2 activation returned None")
            manager.RegisterSessionNotification(self._session_callback)
            registered = True
            self._sweep_manager(manager, set(self._pids()))
            self._managers[device_id] = manager
        except Exception as error:
            if registered and manager is not None:
                try:
                    manager.UnregisterSessionNotification(self._session_callback)
                except Exception:
                    pass
            raise RuntimeError(
                f"audio endpoint manager registration failed for {device_id}: {error}"
            ) from error

    def _sync_endpoints(self, active: dict[str, object]) -> None:
        for device_id in active.keys() - self._managers.keys():
            self._register_endpoint(device_id, active[device_id])
        for device_id in self._managers.keys() - active.keys():
            manager = self._managers.pop(device_id)
            try:
                manager.UnregisterSessionNotification(self._session_callback)
            except Exception:
                # Normal removal may invalidate the COM manager; dropping the reference is sufficient.
                pass

    def _apply_topology(self, topology: tuple, active: dict[str, object]) -> None:
        if self._topology is not None and topology != self._topology:
            if self._policy == "strict" and self._armed:
                raise RuntimeError("audio render endpoint topology changed under strict policy")
        if topology != self._topology:
            self._sync_endpoints(active)
            self._topology = topology
            with self._lock:
                self._render_ids = {device_id for device_id, _state in topology[0]}

    def start(self) -> None:
        self._initialize_com()
        try:
            from pycaw.callbacks import AudioSessionNotification, MMNotificationClient
            from pycaw.utils import AudioUtilities
        except ImportError as error:
            raise RuntimeError(f"pycaw not installed: {error}") from error

        owner = self

        class SessionCallback(AudioSessionNotification):
            def on_session_created(self, new_session):
                try:
                    owner._mute_session(new_session)
                except Exception as error:  # COM callback failures must reach the broker thread.
                    owner._set_fatal(error)

        class DeviceCallback(MMNotificationClient):
            def on_default_device_changed(
                self, _flow, flow_id, _role, _role_id, default_device_id
            ):
                owner._topology_notification("default", default_device_id, flow_id)

            def on_device_added(self, device_id):
                owner._topology_notification("added", device_id)

            def on_device_removed(self, device_id):
                owner._topology_notification("removed", device_id)

            def on_device_state_changed(self, device_id, _new_state, _new_state_id):
                owner._topology_notification("state", device_id)

        self._device_enumerator = AudioUtilities.GetDeviceEnumerator()
        if self._device_enumerator is None:
            raise RuntimeError("GetDeviceEnumerator returned None")
        self._session_callback = SessionCallback()
        self._device_callback = DeviceCallback()
        try:
            self._device_enumerator.RegisterEndpointNotificationCallback(self._device_callback)
        except Exception as error:
            raise RuntimeError(f"endpoint notification registration failed: {error}") from error
        self.sweep()

    def arm(self) -> None:
        with self._lock:
            self._armed = True

    def sweep(self) -> int:
        try:
            muted = 0
            enumerated = False
            last_transient: Exception | None = None
            for _attempt in range(MAX_STABILIZE_PASSES):
                generation = self._generation_snapshot()
                try:
                    topology, active = self._enumerate_topology()
                except TransientTopologyError as error:
                    # Discard this moving snapshot; complete failure is handled after all passes.
                    last_transient = error
                    continue
                # Registration and strict-policy failures remain fatal because an
                # endpoint without a manager cannot be guaranteed muted.
                self._apply_topology(topology, active)
                enumerated = True
                pids = set(self._pids())
                muted = sum(
                    self._sweep_manager(manager, pids)
                    for manager in self._managers.values()
                )
                fatal = self.fatal_error()
                if fatal:
                    raise RuntimeError(fatal)
                with self._lock:
                    if self._generation == generation:
                        self._processed_generation = generation
                        self._wake.clear()
                        self._enumeration_failures = 0
                        self._ready = True
                        return muted

            if enumerated:
                if not self.is_ready():
                    raise RuntimeError(
                        "audio guard could not establish a stable endpoint topology before target resume"
                    )
                # Every observed endpoint was swept, but notifications kept arriving. Once
                # the stable baseline exists, dynamic mode may retry this external churn.
                # Keep the wake event set so the broker immediately performs another sweep.
                with self._lock:
                    self._unstable_sweeps += 1
                    self._enumeration_failures = 0
                return muted

            with self._lock:
                self._enumeration_failures += 1
            raise RuntimeError(f"audio endpoint enumeration failed: {last_transient}")
        except Exception as error:
            self._set_fatal(error)
            raise
        finally:
            self._last_sweep = time.monotonic()

    def stats(self) -> dict:
        """Expose tolerated churn without silently weakening the mute contract."""
        with self._lock:
            return {
                "unstable_sweeps": self._unstable_sweeps,
                "enumeration_failures": self._enumeration_failures,
            }

    def wait(self, timeout: float) -> None:
        """Enforce the minimum sweep interval before waiting for notifications.

        Immediate notification wakeups can turn device churn into a busy loop. The interval
        adds bounded latency without losing level-triggered wakeups, while new sessions are
        still muted synchronously by on_session_created.
        """
        floor = MIN_SWEEP_INTERVAL - (time.monotonic() - self._last_sweep)
        if floor > 0:
            capped = min(floor, timeout)
            time.sleep(capped)
            timeout -= capped
        if timeout > 0:
            self._wake.wait(timeout)

    def close(self) -> None:
        for manager in self._managers.values():
            try:
                manager.UnregisterSessionNotification(self._session_callback)
            except Exception:
                pass
        self._managers.clear()
        if self._device_enumerator is not None and self._device_callback is not None:
            try:
                self._device_enumerator.UnregisterEndpointNotificationCallback(
                    self._device_callback
                )
            except Exception:
                pass
        self._device_callback = None
        self._session_callback = None
        self._device_enumerator = None
        if self._com_initialized:
            ole32.CoUninitialize()
            self._com_initialized = False
