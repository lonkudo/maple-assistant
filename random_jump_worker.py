"""Independent optional random-jump timer worker."""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Optional


LOG = logging.getLogger(__name__)


class RandomJumpWorker(threading.Thread):
    """Tap Alt on its own timer while normal automation input is allowed."""

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        jump_interval: float = 3.0,
        *,
        climbing_active_event: Optional[threading.Event] = None,
        automation_active_event: Optional[threading.Event] = None,
        initial_offset: Optional[float] = None,
        jump_jitter_seconds: float = 0.1,
    ) -> None:
        super().__init__(name="random-jump-worker", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.jump_interval = max(0.2, float(jump_interval))
        self.jump_jitter_seconds = max(0.0, float(jump_jitter_seconds))
        self.climbing_active_event = climbing_active_event
        self.automation_active_event = automation_active_event
        self.enabled = False
        self.initial_offset = (
            self.jump_interval / 2.0
            if initial_offset is None else max(0.0, float(initial_offset))
        )

    def next_delay(self) -> float:
        return max(
            0.05,
            self.jump_interval
            + random.uniform(0.0, self.jump_jitter_seconds),
        )

    def jump_once(self) -> bool:
        tap = getattr(self.key_sender, "tap", None)
        if callable(tap):
            return tap("alt") is not False
        key_down = getattr(self.key_sender, "key_down", None)
        key_up = getattr(self.key_sender, "key_up", None)
        if key_down is None or key_up is None:
            return False
        if key_down("alt") is False:
            return False
        return key_up("alt") is not False

    def run(self) -> None:
        LOG.info(
            "random jump worker started offset=%.3fs interval=%.3fs enabled=%s",
            self.initial_offset, self.jump_interval, self.enabled,
        )
        next_jump = time.monotonic() + self.initial_offset
        while not self.stop_event.is_set():
            if self.stop_event.wait(max(0.0, next_jump - time.monotonic())):
                break
            if not self.enabled:
                pass
            elif (self.automation_active_event is not None
                  and not self.automation_active_event.is_set()):
                pass
            elif (self.climbing_active_event is not None
                  and self.climbing_active_event.is_set()):
                LOG.info("random jump skipped: climb/return input is active")
            else:
                LOG.info("random jump repetition: alt")
                self.jump_once()
            next_jump = time.monotonic() + self.next_delay()
        LOG.info("random jump worker stopped")


__all__ = ["RandomJumpWorker"]
