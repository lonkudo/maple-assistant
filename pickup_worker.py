"""Route-walking Z pickup worker for patrol (independent thread).

Presses Z in bursts ONLY while the character is walking one of the three
route phases - move-to-left-most, move-to-right-most, move-to-rope: hold Z
for ``pickup_hold_seconds`` (default 1s), release, then repeat quickly after
a short gap.  Any other logic (jumping onto the rope, climbing, dropping,
patrol paused, route complete) immediately blocks pickup and releases Z.

The worker owns no screenshots and no direction keys; it only drives the Z
key through the shared key sender, gated by events published by the movement
worker:
  - moving_active_event  : set ONLY for left/right decisions of the three
                           route-walk phases (patrol walk, rope approach)
  - climbing_active_event: blocks Z while a climb/jump is in progress
  - dropping_active_event: blocks Z while dropping through platforms
  - automation_active_event: the UI live-input gate
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional


LOG = logging.getLogger(__name__)


class PickupWorker(threading.Thread):
    """Burst Z pickup: short tap, quick repeat, while route-walking.

    Z is pressed like a human taps it - a short hold (~0.2s) with a short
    gap (~0.1s), repeated while the character walks one of the three route
    phases.  A long (1s) Z hold made the game stop the character mid-walk
    (skill/stance on key-hold), while brief taps behave like manual play.
    """

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        pickup_interval: float = 1.0,
        *,
        climbing_active_event: Optional[threading.Event] = None,
        dropping_active_event: Optional[threading.Event] = None,
        automation_active_event: Optional[threading.Event] = None,
        moving_active_event: Optional[threading.Event] = None,
        pickup_active_event: Optional[threading.Event] = None,
        poll_seconds: float = 0.02,
        initial_offset: Optional[float] = None,
        pickup_hold_seconds: Optional[float] = None,
        pickup_gap_seconds: float = 0.1,
    ) -> None:
        super().__init__(name="pickup-worker", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        # ``pickup_interval`` is kept as the legacy hold-length alias.
        self.pickup_hold_seconds = max(0.05, float(
            pickup_hold_seconds if pickup_hold_seconds is not None
            else (pickup_interval if pickup_interval != 1.0 else 0.2)
        ))
        self.pickup_gap_seconds = max(0.02, float(pickup_gap_seconds))
        self.climbing_active_event = climbing_active_event
        self.dropping_active_event = dropping_active_event
        self.automation_active_event = automation_active_event
        self.moving_active_event = moving_active_event
        # Set while the Z key is physically held; the movement worker waits
        # for it to clear before sending climb/jump/drop keys, so Z can never
        # overlap the Up hold and interrupt a rope grab.
        self.pickup_active_event = pickup_active_event
        self.poll_seconds = max(0.01, float(poll_seconds))
        self.initial_offset = (
            self.pickup_hold_seconds / 2.0
            if initial_offset is None else max(0.0, initial_offset)
        )
        self._z_held = False
        self._hold_until = 0.0
        self._pause_until = 0.0
        self._pickup_count = 0
        if self.initial_offset:
            # Preserve the old startup grace period behavior.
            time.sleep(min(0.5, self.initial_offset))

    @property
    def pickup_count(self) -> int:
        return self._pickup_count

    def _should_pickup(self) -> bool:
        """True only during the route-walk phases with input allowed.

        moving_active_event is set by the movement worker exclusively for
        move-to-left-most / move-to-right-most / move-to-rope left/right
        decisions, so this is the strict phase gate the user asked for.
        """

        if (self.automation_active_event is not None
                and not self.automation_active_event.is_set()):
            return False
        if (self.moving_active_event is not None
                and not self.moving_active_event.is_set()):
            return False
        if (self.climbing_active_event is not None
                and self.climbing_active_event.is_set()):
            return False
        if (self.dropping_active_event is not None
                and self.dropping_active_event.is_set()):
            return False
        return True

    def _press_z(self) -> bool:
        key_down = getattr(self.key_sender, "key_down", None)
        if key_down is not None:
            claimed = key_down("z") is not False
            if not claimed:
                return False
        else:
            return False
        self._z_held = True
        self._pickup_count += 1
        if self.pickup_active_event is not None:
            self.pickup_active_event.set()
        LOG.info("pickup: Z held (#%d)", self._pickup_count)
        return True

    def _release_z(self) -> None:
        if not self._z_held:
            return
        key_up = getattr(self.key_sender, "key_up", None)
        if key_up is not None:
            key_up("z")
        self._z_held = False
        if self.pickup_active_event is not None:
            self.pickup_active_event.clear()
        LOG.info("pickup: Z released")

    def run(self) -> None:
        LOG.info("pickup worker started (burst: hold %.2fs gap %.2fs)",
                 self.pickup_hold_seconds, self.pickup_gap_seconds)
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                if self._should_pickup():
                    if self._z_held:
                        # Hold window elapsed: release, then a short gap
                        # before the next burst starts.
                        if now >= self._hold_until:
                            self._release_z()
                            self._pause_until = now + self.pickup_gap_seconds
                    elif now < self._pause_until:
                        pass  # brief pause between bursts
                    else:
                        self._press_z()
                        self._hold_until = now + self.pickup_hold_seconds
                        self._pause_until = 0.0
                else:
                    # Not a route-walk phase anymore: block pickup NOW.
                    self._release_z()
                    self._hold_until = 0.0
                    self._pause_until = 0.0
                if self.stop_event.wait(self.poll_seconds):
                    break
        finally:
            self._release_z()
        LOG.info("pickup worker stopped (held %d times)", self._pickup_count)


__all__ = ["PickupWorker"]
