"""Independent timed Z-pickup worker for patrol.

Repeatedly taps Z (the classic MapleStory pickup key) on a monotonic timer
while patrol automation is active, so dropped items on the ground are
picked up as the character walks its route.

Like ``AttackWorker``, this module owns no screenshots, no direction keys,
and no analysis - it is a pure timed key tapper that yields to climb/drop
input and to the UI's live-input gate.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional


LOG = logging.getLogger(__name__)


class PickupWorker(threading.Thread):
    """Tap Z on a timer while automation is active and not climbing."""

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        pickup_interval: float = 1.0,
        *,
        climbing_active_event: Optional[threading.Event] = None,
        dropping_active_event: Optional[threading.Event] = None,
        automation_active_event: Optional[threading.Event] = None,
        initial_offset: Optional[float] = None,
    ) -> None:
        super().__init__(name="pickup-worker", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.pickup_interval = max(0.2, pickup_interval)
        self.climbing_active_event = climbing_active_event
        self.dropping_active_event = dropping_active_event
        self.automation_active_event = automation_active_event
        self.initial_offset = (
            self.pickup_interval / 2.0
            if initial_offset is None else max(0.0, initial_offset)
        )
        self._pickup_count = 0

    def pickup_once(self) -> bool:
        """Send exactly Z down/up; never touch movement keys."""

        key_down = getattr(self.key_sender, "key_down", None)
        key_up = getattr(self.key_sender, "key_up", None)
        if key_down is not None and key_up is not None:
            claimed = key_down("z") is not False
            if not claimed:
                return False
            ok = key_up("z") is not False
        else:
            ok = self.key_sender.tap("z") is not False
        if ok:
            self._pickup_count += 1
        return ok

    @property
    def pickup_count(self) -> int:
        return self._pickup_count

    def run(self) -> None:
        LOG.info("pickup worker started interval=%.3fs offset=%.3fs",
                 self.pickup_interval, self.initial_offset)
        next_pickup = time.monotonic() + self.initial_offset
        while not self.stop_event.is_set():
            if self.stop_event.wait(max(0.0, next_pickup - time.monotonic())):
                break
            if (self.automation_active_event is not None
                    and not self.automation_active_event.is_set()):
                pass  # patrol paused from the UI: no pickup spam
            elif (self.climbing_active_event is not None
                    and self.climbing_active_event.is_set()):
                LOG.debug("pickup skipped: jump-climb input is active")
            elif (self.dropping_active_event is not None
                    and self.dropping_active_event.is_set()):
                LOG.debug("pickup skipped: drop input is active")
            else:
                self.pickup_once()
                LOG.info("pickup: z (#%d)", self._pickup_count)
            next_pickup = time.monotonic() + self.pickup_interval
        LOG.info("pickup worker stopped (pressed %d times)", self._pickup_count)


__all__ = ["PickupWorker"]
