"""Audio playback engine built on libmpv through python-mpv.

The system library (libmpv.so.2) is loaded via ctypes, so the standalone
``mpv`` binary is not required.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable

try:
    import mpv
except ImportError as error:  # pragma: no cover
    raise RuntimeError(
        "python-mpv is required. Run: .venv/bin/pip install -r requirements.txt"
    ) from error


class PlayerError(Exception):
    pass


def _ssh_audio_target() -> str | None:
    if not (os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION")):
        return None
    target = os.environ.get("JMLCLI_SSH_AUDIO_TARGET", "SSH_Stream").strip()
    return target or None


def _matching_audio_device(devices: list[dict[str, Any]], target: str) -> str | None:
    override = os.environ.get("JMLCLI_SSH_AUDIO_DEVICE", "").strip()
    preferred = (f"pipewire/{target}", f"pulse/{target}")
    names = {str(device.get("name", "")) for device in devices}
    if override in names:
        return override
    return next((name for name in preferred if name in names), None)


class PlayerStatus:
    __slots__ = ("state", "position", "duration", "volume", "muted")

    def __init__(
        self,
        state: str = "stopped",
        position: float | None = None,
        duration: float | None = None,
        volume: float | None = None,
        muted: bool | None = None,
    ) -> None:
        self.state = state
        self.position = position
        self.duration = duration
        self.volume = volume
        self.muted = muted

    @property
    def playing(self) -> bool:
        return self.state == "playing"


class MpvPlayer:
    """Thin, thread-safe facade over a single libmpv instance."""

    def __init__(self, on_end_of_track: Callable[[], None] | None = None) -> None:
        self.on_end_of_track = on_end_of_track
        self._lock = threading.RLock()
        self._current_ref: str = ""
        self._ssh_audio_target = _ssh_audio_target()
        self._ssh_audio_device: str | None = None
        try:
            self._mpv: Any = mpv.MPV(
                video=False,
                idle=True,
                osc=False,
                terminal=False,
                input_default_bindings=False,
                input_vo_keyboard=False,
                audio_display="no",
                keep_open="no",
            )
        except Exception as error:
            raise PlayerError(f"Could not initialize audio engine: {error}") from error
        self._configure_ssh_audio()

        @self._mpv.event_callback(mpv.MpvEventID.END_FILE)
        def _on_end_file(event):  # noqa: ANN001 - python-mpv signature
            reason = None
            data = getattr(event, "data", None)
            if data is not None:
                reason = getattr(data, "reason", None)
                if reason is None and hasattr(data, "get"):
                    reason = data.get("reason")
            if isinstance(reason, str):
                hit_eof = reason.upper().endswith("EOF")
            else:
                eof_value = getattr(
                    getattr(mpv, "MpvEventEndFileReason", None), "EOF", 0
                )
                try:
                    hit_eof = int(reason) == int(eof_value)
                except (TypeError, ValueError):
                    hit_eof = False
            if hit_eof:
                callback = self.on_end_of_track
                if callback is not None:
                    try:
                        callback()
                    except Exception:
                        pass

    @property
    def current_ref(self) -> str:
        return self._current_ref

    @property
    def ssh_audio_status(self) -> str | None:
        if self._ssh_audio_target is None:
            return None
        if self._ssh_audio_device:
            return f"SSH audio routed to {self._ssh_audio_target}."
        return (
            f"SSH audio target {self._ssh_audio_target} is unavailable; "
            "using the remote default output."
        )

    def _configure_ssh_audio(self) -> None:
        target = self._ssh_audio_target
        if target is None:
            return
        try:
            devices = list(self._mpv.audio_device_list or [])
            device = _matching_audio_device(devices, target)
            if device:
                self._mpv.audio_device = device
                self._ssh_audio_device = device
        except Exception:
            pass

    def play(self, ref: str) -> None:
        with self._lock:
            self._current_ref = ref
            try:
                self._mpv.command("loadfile", ref, "replace")
                self._mpv.pause = False
            except Exception as error:
                raise PlayerError(f"Cannot play {ref}: {error}") from error

    def stop(self) -> None:
        with self._lock:
            self._current_ref = ""
            try:
                self._mpv.command("stop")
            except Exception:
                pass

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            try:
                self._mpv.pause = bool(paused)
            except Exception:
                pass

    def toggle_pause(self) -> bool:
        with self._lock:
            try:
                self._mpv.pause = not bool(self._mpv.pause)
                return bool(self._mpv.pause)
            except Exception:
                return False

    def seek(self, delta_seconds: float) -> None:
        with self._lock:
            try:
                self._mpv.command("seek", delta_seconds, "relative")
            except Exception:
                pass

    def seek_to(self, position_seconds: float) -> None:
        with self._lock:
            try:
                self._mpv.command("seek", max(position_seconds, 0), "absolute")
            except Exception:
                pass

    def set_volume(self, volume: float) -> None:
        with self._lock:
            try:
                self._mpv.volume = max(0.0, min(volume, 130.0))
            except Exception:
                pass

    def get_volume(self) -> float:
        with self._lock:
            try:
                return float(self._mpv.volume or 100.0)
            except Exception:
                return 100.0

    def toggle_mute(self) -> None:
        with self._lock:
            try:
                self._mpv.mute = not bool(self._mpv.mute)
            except Exception:
                pass

    def status(self) -> PlayerStatus:
        state = "stopped"
        position = None
        duration = None
        volume = None
        muted = None
        with self._lock:
            try:
                if self._current_ref or self._mpv.path:
                    paused = bool(self._mpv.pause)
                    state = "paused" if paused else "playing"
            except Exception:
                state = "stopped"
            for attr, sink in (
                ("playback-time", "position"),
                ("duration", "duration"),
                ("volume", "volume"),
                ("mute", "muted"),
            ):
                try:
                    value = getattr(self._mpv, attr)
                except Exception:
                    continue
                if value is None:
                    continue
                if sink == "muted":
                    muted = bool(value)
                else:
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        continue
                    if attr != "duration" or value > 0:
                        if sink == "position":
                            position = value
                        elif sink == "duration":
                            duration = value
                        elif sink == "volume":
                            volume = value
        return PlayerStatus(state, position, duration, volume, muted)

    def shutdown(self) -> None:
        with self._lock:
            try:
                self._mpv.terminate()
            except Exception:
                pass
