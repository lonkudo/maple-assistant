"""Scheduled game shutdown: Alt+F4 after X hours, then stop every worker.

This module owns no direction keys and consumes no screenshots.  The UI
panel toggles ``enabled`` and ``hours`` live; the worker arms a deadline
from the moment it is enabled.  When the deadline passes it foregrounds the
game, sends Alt+F4 (both keys down together, brief hold, release), then
polls whether the game window is gone.  On success it sets the shared
``stop_event`` so every worker exits.  On failure it retries a few times,
then re-arms the deadline and keeps trying later.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional


LOG = logging.getLogger(__name__)

SECONDS_PER_HOUR = 3600.0


class ShutdownWorker(threading.Thread):
    """Close the game after ``hours`` of enabled uptime, then stop all."""

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        *,
        enabled: bool = False,
        hours: float = 3.0,
        poll_interval: float = 1.0,
        max_attempts: int = 3,
        chord_hold: float = 0.15,
        close_grace: float = 3.0,
    ) -> None:
        super().__init__(name="shutdown-worker", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.enabled = bool(enabled)
        self.hours = max(0.05, float(hours))
        self.poll_interval = max(0.2, float(poll_interval))
        self.max_attempts = max(1, int(max_attempts))
        self.chord_hold = max(0.02, float(chord_hold))
        self.close_grace = max(0.5, float(close_grace))
        self._deadline: Optional[float] = None

    # ---- live configuration (called from the UI thread) ----------------

    def set_hours(self, hours: float) -> None:
        """Update the countdown; re-arms from now when currently enabled."""

        self.hours = max(0.05, float(hours))
        self._deadline = None

    # ---- shutdown sequence ---------------------------------------------

    def _fire_shutdown(self) -> bool:
        """Foreground the game and press Alt+F4 (chord), returns True."""

        sender = self.key_sender
        select = getattr(sender, "select_window", None)
        if select is not None:
            try:
                select()
            except Exception:
                LOG.warning("could not foreground the game for shutdown",
                            exc_info=True)
        if getattr(sender, "dry_run", False):
            LOG.info("SHUTDOWN dry-run: would press Alt+F4")
            return True
        sender.key_down("alt")
        sender.key_down("f4")
        time.sleep(self.chord_hold)
        sender.key_up("f4")
        sender.key_up("alt")
        LOG.info("SHUTDOWN: Alt+F4 sent")
        return True

    def _game_closed(self) -> bool:
        """True when the game window no longer exists (or dry-run)."""

        if getattr(self.key_sender, "dry_run", False):
            return True
        finder = getattr(self.key_sender, "_find_target_window", None)
        if finder is None:
            return True
        try:
            finder()
            return False
        except OSError:
            return True

    def _run_deadline(self) -> None:
        """Wait for the deadline, close the game, verify, then stop all."""

        attempts = 0
        while not self.stop_event.is_set():
            if not self.enabled:
                self._deadline = None
                return
            remaining = (self._deadline or 0.0) - time.monotonic()
            if remaining > 0:
                if self.stop_event.wait(min(self.poll_interval, remaining)):
                    return
                continue
            attempts += 1
            LOG.info("SHUTDOWN attempt %d/%d: closing the game",
                     attempts, self.max_attempts)
            try:
                self._fire_shutdown()
            except Exception:
                LOG.warning("SHUTDOWN attempt %d raised", attempts,
                            exc_info=True)
            else:
                if self._game_closed():
                    LOG.info("SHUTDOWN: game closed; stopping all workers")
                    self.stop_event.set()
                    return
                LOG.warning("SHUTDOWN attempt %d failed: game window still "
                            "open", attempts)
            if attempts >= self.max_attempts:
                LOG.warning("SHUTDOWN gave up after %d attempts; re-arming "
                            "the deadline", attempts)
                self._deadline = time.monotonic() + self.hours * SECONDS_PER_HOUR
                attempts = 0
                continue
            if self.stop_event.wait(self.close_grace):
                return

    def run(self) -> None:
        LOG.info("shutdown worker started enabled=%s hours=%.2f",
                 self.enabled, self.hours)
        while not self.stop_event.is_set():
            if self.enabled:
                if self._deadline is None:
                    self._deadline = (
                        time.monotonic() + self.hours * SECONDS_PER_HOUR
                    )
                    LOG.info("SHUTDOWN armed: game will close in %.2f hours",
                             self.hours)
                self._run_deadline()
            else:
                self._deadline = None
                if self.stop_event.wait(self.poll_interval):
                    break
        LOG.info("shutdown worker stopped")


__all__ = ["ShutdownWorker"]
