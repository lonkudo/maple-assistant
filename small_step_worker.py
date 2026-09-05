"""Independent timed trigger for the optional 小碎步 motion."""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Optional


LOG = logging.getLogger(__name__)


class SmallStepWorker(threading.Thread):
    """Queue short left/right movement pairs at a safe, user-set cadence."""

    def __init__(
        self,
        stop_event: threading.Event,
        *,
        step_interval: float = 5.0,
        automation_active_event: Optional[threading.Event] = None,
        climbing_active_event: Optional[threading.Event] = None,
        initial_offset: Optional[float] = None,
        step_jitter_seconds: float = 0.1,
        motion_arbiter: Any = None,
    ) -> None:
        super().__init__(name="small-step-worker", daemon=True)
        self.stop_event = stop_event
        self.step_interval = max(3.0, float(step_interval))
        self.step_jitter_seconds = max(0.0, float(step_jitter_seconds))
        self.automation_active_event = automation_active_event
        self.climbing_active_event = climbing_active_event
        self.motion_arbiter = motion_arbiter
        self.enabled = False
        self.initial_offset = (
            self.step_interval / 2.0
            if initial_offset is None else max(0.0, float(initial_offset))
        )

    def next_delay(self) -> float:
        return max(
            0.05,
            self.step_interval + random.uniform(0.0, self.step_jitter_seconds),
        )

    def run(self) -> None:
        LOG.info(
            "small-step worker started offset=%.3fs interval=%.3fs enabled=%s",
            self.initial_offset, self.step_interval, self.enabled,
        )
        next_step = time.monotonic() + self.initial_offset
        while not self.stop_event.is_set():
            if self.stop_event.wait(max(0.0, next_step - time.monotonic())):
                break
            if not self.enabled:
                pass
            elif (self.automation_active_event is not None
                  and not self.automation_active_event.is_set()):
                pass
            elif (self.climbing_active_event is not None
                  and self.climbing_active_event.is_set()):
                LOG.info("small-step skipped: climb/return input is active")
            elif self.motion_arbiter is not None:
                if self.motion_arbiter.request_micro_step():
                    LOG.info("small-step queued")
                else:
                    LOG.info("small-step skip: arbiter stopping")
            else:
                LOG.warning("small-step skipped: motion arbiter unavailable")
            next_step = time.monotonic() + self.next_delay()
        LOG.info("small-step worker stopped")


__all__ = ["SmallStepWorker"]
