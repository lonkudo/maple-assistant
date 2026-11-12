"""随机休息: occasionally stand the character still for a few seconds.

A long uninterrupted patrol is a visible pattern.  With this option enabled the
assistant takes a short break at an irregular interval:

* gap between breaks: **75–120 minutes**, drawn fresh after every break;
* the break itself: **3–15 seconds**, also drawn fresh.

While a break runs the character stops walking and stops attacking, then carries
on with the same patrol leg — the route, the pinned layer and the current target
are untouched, so nothing re-anchors and the UI keeps showing a running patrol.
Potions and the buff schedule keep working: a rest is a movement pause, not a
suspension of health handling.

Only the attack modes with a patrol/standing posture use it (巡逻攻击 / 站桩攻击 /
双桩模式); the UI greys the option out elsewhere and pushes the resulting enable
flag here through :meth:`set_enabled`.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Callable, Optional

LOG = logging.getLogger(__name__)

# Operator's specification (2026-11-12): "每75m~120m 休息3s~15s".
GAP_SECONDS_RANGE = (75 * 60.0, 120 * 60.0)
REST_SECONDS_RANGE = (3.0, 15.0)
# How often the schedule is polled.  The gap is measured in minutes, so a coarse
# tick is enough; the break length is exact because the worker sleeps it out.
TICK_SECONDS = 0.5
# A refused pause (patrol just stopped, restart in progress) is retried after
# this long instead of eating the whole gap.
RETRY_SECONDS = 10.0


class RandomRestWorker(threading.Thread):
    """Turn 随机休息 on and off, and run the pauses it schedules."""

    def __init__(
        self,
        stop_event: threading.Event,
        *,
        request_rest: Callable[[float], bool],
        end_rest: Optional[Callable[..., None]] = None,
        automation_active_event: Optional[threading.Event] = None,
        gap_seconds: tuple[float, float] = GAP_SECONDS_RANGE,
        rest_seconds: tuple[float, float] = REST_SECONDS_RANGE,
        tick_seconds: float = TICK_SECONDS,
        rng: Any = None,
    ) -> None:
        super().__init__(name="random-rest", daemon=True)
        self.stop_event = stop_event
        self._request_rest = request_rest
        self._end_rest = end_rest
        self.automation_active_event = automation_active_event
        self.gap_seconds = (max(1.0, float(gap_seconds[0])), max(1.0, float(gap_seconds[1])))
        # A break shorter than half a second would not even cover one capture,
        # so the low end is clamped; the operator's range is 3-15 s anyway.
        low = max(0.5, float(rest_seconds[0]))
        high = max(low, float(rest_seconds[1]))
        self.rest_seconds = (low, high)
        self.tick_seconds = max(0.05, float(tick_seconds))
        self._rng = rng or random.Random()
        self._enabled = False
        self._next_break_at: Optional[float] = None
        self._resting_until = 0.0
        self._lock = threading.Lock()
        self.rest_count = 0
        self.last_gap_seconds = 0.0
        self.last_rest_seconds = 0.0

    # ---- control -----------------------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        """Switch the schedule on/off (the UI owns the mode rules).

        Enabling restarts the gap, so a freshly checked box never fires a break
        immediately, and disabling clears the remaining countdown.
        """

        enabled = bool(enabled)
        with self._lock:
            if enabled == self._enabled:
                return
            self._enabled = enabled
            self._next_break_at = None
        if enabled:
            LOG.info("随机休息: enabled (每%.0fm~%.0fm 休息%.0fs~%.0fs)",
                     self.gap_seconds[0] / 60.0, self.gap_seconds[1] / 60.0,
                     self.rest_seconds[0], self.rest_seconds[1])
        else:
            LOG.info("随机休息: disabled")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def next_gap(self) -> float:
        """Draw the gap before the next break."""

        low, high = self.gap_seconds
        return self._rng.uniform(low, high) if high > low else low

    def next_rest(self) -> float:
        """Draw the length of one break."""

        low, high = self.rest_seconds
        return self._rng.uniform(low, high) if high > low else low

    def seconds_until_break(self, now: Optional[float] = None) -> Optional[float]:
        """Remaining time to the next break, or ``None`` while unscheduled."""

        moment = time.monotonic() if now is None else float(now)
        with self._lock:
            if not self._enabled or self._next_break_at is None:
                return None
            return max(0.0, self._next_break_at - moment)

    # ---- schedule ----------------------------------------------------------
    def _automation_active(self) -> bool:
        event = self.automation_active_event
        return True if event is None else bool(event.is_set())

    def tick(self, now: Optional[float] = None) -> None:
        """One scheduling step; separated from ``run`` so it can be driven."""

        now = time.monotonic() if now is None else float(now)
        with self._lock:
            if not self._enabled:
                self._next_break_at = None
                return
            if not self._automation_active():
                # Idle time (patrol stopped, reconnect...) must not count
                # towards the gap: the gap measures patrol activity.
                self._next_break_at = None
                return
            if self._next_break_at is None:
                gap = self.next_gap()
                self.last_gap_seconds = gap
                self._next_break_at = now + gap
                LOG.info("随机休息: next break in %.0f min", gap / 60.0)
                return
            if now < self._next_break_at:
                return
            duration = self.next_rest()
            self._next_break_at = None

        if self._request_rest(duration):
            self.rest_count += 1
            self.last_rest_seconds = duration
            LOG.warning("随机休息: break %.1fs (movement + attack paused)", duration)
            self._resting_until = time.monotonic() + duration
            # Sleep out the break, then hand the input paths back.  The wait is
            # on ``stop_event`` so a closed panel ends a break immediately.
            self.stop_event.wait(duration)
            self._resume()
        else:
            LOG.info("随机休息: pause refused (patrol not ready); retrying in %.0fs",
                     RETRY_SECONDS)
            with self._lock:
                self._next_break_at = now + RETRY_SECONDS

    def _resume(self) -> None:
        self._resting_until = 0.0
        if not callable(self._end_rest):
            return
        try:
            self._end_rest()
        except Exception:
            LOG.exception("随机休息: resume callback failed")

    # ---- thread ------------------------------------------------------------
    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                LOG.exception("随机休息 schedule tick failed")
            self.stop_event.wait(self.tick_seconds)
        # A panel close during a break must not leave the attack block raised.
        if self._resting_until:
            self._resume()


__all__ = [
    "RandomRestWorker",
    "GAP_SECONDS_RANGE",
    "REST_SECONDS_RANGE",
]
