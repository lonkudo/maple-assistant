"""Optional full-screen blue visual alert shared by all beep triggers."""

from __future__ import annotations

import logging
import threading
import time


LOG = logging.getLogger(__name__)


class ScreenBlinker(threading.Thread):
    """Show a short blue full-screen overlay twice for each queued alert.

    The overlay has its own Tk event loop on this worker thread, so beep
    producers never block capture, marker detection, or the main UI thread.
    """

    def __init__(
        self,
        stop_event: threading.Event,
        *,
        enabled: bool = False,
        flashes_per_alert: int = 2,
        flash_seconds: float = 0.16,
        gap_seconds: float = 0.12,
    ) -> None:
        super().__init__(name="screen-blinker", daemon=True)
        self.stop_event = stop_event
        self.flashes_per_alert = max(1, int(flashes_per_alert))
        self.flash_seconds = max(0.02, float(flash_seconds))
        self.gap_seconds = max(0.02, float(gap_seconds))
        self._lock = threading.Lock()
        self._enabled = bool(enabled)
        self._pending = 0
        self._wake_event = threading.Event()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            if not self._enabled:
                self._pending = 0
        self._wake_event.set()
        LOG.info("screen blink alert %s", "enabled" if enabled else "disabled")

    def request_blink(self) -> None:
        """Queue the visual half of one alarm without delaying its caller."""

        with self._lock:
            if not self._enabled:
                return
            self._pending += 1
        self._wake_event.set()

    def _wait(self, seconds: float) -> bool:
        return self.stop_event.wait(seconds)

    def _blink_twice(self) -> None:
        """Create a temporary blue overlay; keep all Tk calls on this thread."""

        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception:
            LOG.warning("screen blink alert is unavailable", exc_info=True)
            return
        try:
            root.configure(background="#0078D7")
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            root.attributes("-fullscreen", True)
            root.withdraw()
            for _ in range(self.flashes_per_alert):
                if self.stop_event.is_set() or not self.enabled:
                    return
                root.deiconify()
                root.lift()
                root.update_idletasks()
                root.update()
                if self._wait(self.flash_seconds):
                    return
                root.withdraw()
                root.update_idletasks()
                root.update()
                if self._wait(self.gap_seconds):
                    return
        except Exception:
            LOG.warning("screen blink alert failed", exc_info=True)
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def run(self) -> None:
        LOG.info("screen blinker started")
        while not self.stop_event.is_set():
            self._wake_event.wait(0.25)
            self._wake_event.clear()
            while not self.stop_event.is_set():
                with self._lock:
                    if not self._enabled or self._pending <= 0:
                        break
                    self._pending -= 1
                self._blink_twice()
        LOG.info("screen blinker stopped")


__all__ = ["ScreenBlinker"]
