"""Moving-gated Z pickup worker for patrol.

Holds Z while the character is actively walking (patrol left/right or rope
approach) and releases it when movement stops - Z is pressed and released
exactly like a movement key, never tapped on a timer.  Dropped items on the
ground are collected continuously as the character walks its route.

This worker is a pure key holder: it owns no screenshots and no direction
keys, and it yields to climb/drop input and the UI's live-input gate.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional


LOG = logging.getLogger(__name__)


class PickupWorker(threading.Thread):
    """Hold Z while the character is walking; release when it stops."""

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
        poll_seconds: float = 0.05,
        initial_offset: Optional[float] = None,
    ) -> None:
        super().__init__(name="pickup-worker", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        # Kept for interface compatibility; the worker is event-driven now.
        self.pickup_interval = max(0.2, pickup_interval)
        self.climbing_active_event = climbing_active_event
        self.dropping_active_event = dropping_active_event
        self.automation_active_event = automation_active_event
        self.moving_active_event = moving_active_event
        self.poll_seconds = max(0.01, float(poll_seconds))
        self.initial_offset = (
            self.pickup_interval / 2.0
            if initial_offset is None else max(0.0, initial_offset)
        )
        self._z_held = False
        self._pickup_count = 0
        if self.initial_offset:
            # Preserve the old startup grace period behavior.
            time.sleep(min(0.5, self.initial_offset))

    @property
    def pickup_count(self) -> int:
        return self._pickup_count

    def _should_hold(self) -> bool:
        """True when the character is walking and input is allowed."""

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
        LOG.info("pickup: Z held (#%d)", self._pickup_count)
        return True

    def _release_z(self) -> None:
        if not self._z_held:
            return
        key_up = getattr(self.key_sender, "key_up", None)
        if key_up is not None:
            key_up("z")
        self._z_held = False
        LOG.info("pickup: Z released")

    def run(self) -> None:
        LOG.info("pickup worker started (hold-while-moving)")
        try:
            while not self.stop_event.is_set():
                if self.stop_event.wait(self.poll_seconds):
                    break
                if self._should_hold():
                    if not self._z_held:
                        self._press_z()
                elif self._z_held:
                    self._release_z()
        finally:
            self._release_z()
        LOG.info("pickup worker stopped (held %d times)", self._pickup_count)


__all__ = ["PickupWorker"]
