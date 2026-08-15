"""Independent timed Ctrl attack worker.

This module does not import movement or status analysis. It owns no direction
keys and consumes no screenshots.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional


LOG = logging.getLogger(__name__)


class AttackWorker(threading.Thread):
    """Tap Ctrl on a monotonic timer, except during active jump/climb input."""

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        attack_interval: float = 3.0,
        *,
        climbing_active_event: Optional[threading.Event] = None,
        initial_offset: Optional[float] = None,
    ) -> None:
        super().__init__(name="attack-worker", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.attack_interval = max(0.25, attack_interval)
        self.climbing_active_event = climbing_active_event
        self.initial_offset = (
            self.attack_interval / 2.0
            if initial_offset is None else max(0.0, initial_offset)
        )

    def attack_once(self) -> bool:
        """Send exactly Ctrl down/up; never call a movement operation."""

        key_down = getattr(self.key_sender, "key_down", None)
        key_up = getattr(self.key_sender, "key_up", None)
        if key_down is not None and key_up is not None:
            claimed = key_down("ctrl") is not False
            if not claimed:
                return False
            return key_up("ctrl") is not False
        return self.key_sender.tap("ctrl") is not False

    def run(self) -> None:
        LOG.info("attack worker started offset=%.3fs interval=%.3fs",
                 self.initial_offset, self.attack_interval)
        next_attack = time.monotonic() + self.initial_offset
        while not self.stop_event.is_set():
            if self.stop_event.wait(max(0.0, next_attack - time.monotonic())):
                break
            if (self.climbing_active_event is not None
                    and self.climbing_active_event.is_set()):
                LOG.info("attack skipped: jump-climb input is active")
            else:
                LOG.info("attack repetition: ctrl")
                self.attack_once()
            next_attack = time.monotonic() + self.attack_interval
        LOG.info("attack worker stopped")


__all__ = ["AttackWorker"]
