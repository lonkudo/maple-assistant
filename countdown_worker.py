"""Independent repeating countdown that dispatches selected reminders.

The worker consumes no screenshots, sends no input, and does not inspect any
other automation state. Its interval and remaining time can be changed live
by the UI. At zero it re-arms the full interval before notifying each enabled
output (sound, screen flash, and Telegram message).
"""

from __future__ import annotations

import ctypes
import logging
from pathlib import Path
import threading
import time
from typing import Callable, Optional


LOG = logging.getLogger(__name__)
SECONDS_PER_HOUR = 3600.0


def play_mp3(path: Path) -> None:
    """Play one MP3 to completion through the Windows MCI API."""

    path = Path(path).resolve()
    if not path.is_file():
        LOG.warning("COUNTDOWN sound is missing: %s", path)
        return
    try:
        winmm = ctypes.windll.winmm
    except (AttributeError, OSError):
        LOG.warning("COUNTDOWN MP3 playback requires Windows: %s", path)
        return

    alias = f"maple_countdown_{threading.get_ident()}_{time.monotonic_ns()}"

    def send(command: str) -> int:
        return int(winmm.mciSendStringW(command, None, 0, None))

    opened = False
    try:
        error = send(f'open "{path}" type mpegvideo alias {alias}')
        if error:
            raise OSError(error, "MCI could not open countdown sound")
        opened = True
        error = send(f"play {alias} wait")
        if error:
            raise OSError(error, "MCI could not play countdown sound")
    except OSError:
        LOG.warning("COUNTDOWN sound playback failed: %s", path, exc_info=True)
    finally:
        if opened:
            send(f"close {alias}")


class CountdownWorker(threading.Thread):
    """Repeat an adjustable interval and play a sound at every expiry."""

    def __init__(
        self,
        stop_event: threading.Event,
        *,
        sound_path: Path,
        enabled: bool = False,
        interval_hours: float = 1.0,
        poll_interval: float = 0.2,
        play_sound: Optional[Callable[[Path], None]] = None,
        flash_callback: Optional[Callable[[], None]] = None,
        alert_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(name="countdown-worker", daemon=True)
        self.stop_event = stop_event
        self.sound_path = Path(sound_path)
        self.poll_interval = max(0.02, float(poll_interval))
        self._play_sound = play_sound or play_mp3
        self._flash_callback = flash_callback
        self._alert_callback = alert_callback
        self._lock = threading.Lock()
        self._wake_event = threading.Event()
        self._enabled = bool(enabled)
        self._sound_enabled = True
        self._interval_seconds = max(
            0.01, float(interval_hours) * SECONDS_PER_HOUR
        )
        self._deadline: Optional[float] = None
        # True while an expiry's reminders are being dispatched (the sound
        # playback blocks the worker thread).  A stale ``remaining=0`` from
        # the UI (the bar still shows 0:00 until the next poll) must not
        # re-arm the deadline to "now" during that window: doing so made the
        # run loop fire the end event a SECOND time as soon as the playback
        # finished (ding-dong + flash + notifier all ran twice).
        self._firing = False

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def interval_hours(self) -> float:
        with self._lock:
            return self._interval_seconds / SECONDS_PER_HOUR

    def set_enabled(self, enabled: bool) -> None:
        """Enable from a fresh full interval, or pause and clear the timer."""

        now = time.monotonic()
        with self._lock:
            enabled = bool(enabled)
            if enabled and not self._enabled:
                self._deadline = now + self._interval_seconds
            elif not enabled:
                self._deadline = None
            self._enabled = enabled
        self._wake_event.set()

    def set_sound_enabled(self, enabled: bool) -> None:
        """Enable/disable only audio; visual/message callbacks still fire."""

        with self._lock:
            self._sound_enabled = bool(enabled)

    def set_interval_hours(self, hours: float) -> None:
        """Set the time gap and restart an enabled timer from the full gap."""

        seconds = max(0.01, float(hours) * SECONDS_PER_HOUR)
        now = time.monotonic()
        with self._lock:
            self._interval_seconds = seconds
            if self._enabled:
                self._deadline = now + seconds
        self._wake_event.set()

    def set_remaining_seconds(self, seconds: float) -> None:
        """Move the live deadline within ``0 .. interval`` from the UI bar."""

        now = time.monotonic()
        with self._lock:
            remaining = max(0.0, min(float(seconds), self._interval_seconds))
            if self._enabled and not (self._firing and remaining <= 0.0):
                self._deadline = now + remaining
        self._wake_event.set()

    def snapshot(self) -> tuple[bool, float, float]:
        """Return ``(enabled, interval_seconds, remaining_seconds)``."""

        now = time.monotonic()
        with self._lock:
            interval = self._interval_seconds
            remaining = (
                interval
                if self._deadline is None
                else max(0.0, min(interval, self._deadline - now))
            )
            return self._enabled, interval, remaining

    def _fire_and_reset(self) -> None:
        """Re-arm first, then play; playback time does not lengthen the gap."""

        now = time.monotonic()
        with self._lock:
            if not self._enabled:
                return
            self._deadline = now + self._interval_seconds
            sound_enabled = self._sound_enabled
            self._firing = True
        LOG.info("COUNTDOWN reached zero: triggering reminders and resetting")
        try:
            if self._flash_callback is not None:
                try:
                    self._flash_callback()
                except Exception:
                    LOG.warning("COUNTDOWN screen blink callback failed",
                                exc_info=True)
            if self._alert_callback is not None:
                try:
                    self._alert_callback("循环警报")
                except Exception:
                    LOG.warning("COUNTDOWN message callback failed",
                                exc_info=True)
            if sound_enabled:
                try:
                    self._play_sound(self.sound_path)
                except Exception:
                    LOG.warning("COUNTDOWN sound callback failed",
                                exc_info=True)
        finally:
            with self._lock:
                self._firing = False

    def run(self) -> None:
        LOG.info("countdown worker started")
        while not self.stop_event.is_set():
            enabled, _interval, remaining = self.snapshot()
            if enabled:
                with self._lock:
                    if self._deadline is None:
                        self._deadline = time.monotonic() + self._interval_seconds
                        remaining = self._interval_seconds
                if remaining <= 0.0:
                    self._fire_and_reset()
                    continue
                wait_for = min(self.poll_interval, remaining)
            else:
                wait_for = self.poll_interval
            self._wake_event.wait(wait_for)
            self._wake_event.clear()
        LOG.info("countdown worker stopped")


__all__ = ["CountdownWorker", "play_mp3"]
