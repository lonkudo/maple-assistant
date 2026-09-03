"""Serialize jump/buff taps against the fixed attack cadence.

MapleStory action motion drops a key pressed while the character is still
performing another action, so independent attack / random-jump / periodic
buff timers collide and the jump or buff tap silently does nothing.

``MotionArbiter`` is the single executor for those motion keys:

- a FIFO queue temporarily registers jump and buff events (requests are
  non-blocking; duplicate pending events collapse to one);
- one worker thread dequeues and executes them one at a time;
- a jump locks out further motion for ``jump_motion_seconds`` (default 0.9s)
  and a buff for ``buff_motion_seconds`` (default 0.6s);
- while events are queued or executing, fixed attack is suppressed;
- only when the motion window is over does the next event dequeue - or the
  attack worker fire again;
- every event additionally waits a short ``attack_grace_seconds`` (default
  0.3s) after the last attack tap, so an attack motion that just started
  cannot swallow the jump/buff tap.

Attack taps are NOT queued here (they keep their own cadence); the attack
worker asks whether the arbiter is idle before firing and reports each
successful tap via ``note_attack``.  HP/MP potions also stay on their own
urgent path (bar-verified retries), outside this queue.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Optional


LOG = logging.getLogger(__name__)

JUMP = "jump"


class MotionArbiter(threading.Thread):
    """One-at-a-time executor for jump/buff motion keys."""

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        *,
        climbing_active_event: Optional[threading.Event] = None,
        jump_motion_seconds: float = 0.9,
        buff_motion_seconds: float = 0.6,
        attack_grace_seconds: float = 0.3,
    ) -> None:
        super().__init__(name="motion-arbiter", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.climbing_active_event = climbing_active_event
        self.jump_motion_seconds = max(0.0, float(jump_motion_seconds))
        self.buff_motion_seconds = max(0.0, float(buff_motion_seconds))
        self.attack_grace_seconds = max(0.0, float(attack_grace_seconds))
        self._cv = threading.Condition()
        self._pending: "deque[str]" = deque()
        # Dedupe identities: "jump" or "buff:<key>".  A pending event of the
        # same kind means "do it once when free" - never pile up repeats.
        self._queued: set[str] = set()
        self._busy_until = 0.0  # monotonic end of the running motion window
        self._last_attack_at = float("-inf")

    # ------------------------------------------------------------------ #
    # Registration (any thread)                                          #
    # ------------------------------------------------------------------ #

    def request_jump(self) -> bool:
        """Queue one jump (Alt tap). Duplicate pending jumps collapse."""

        with self._cv:
            if self.stop_event.is_set():
                return False
            if JUMP in self._queued:
                return True
            self._pending.append(JUMP)
            self._queued.add(JUMP)
            self._cv.notify_all()
            return True

    def request_buff(self, key: str) -> bool:
        """Queue one periodic buff/pet-food key tap (per-key collapse)."""

        token = f"buff:{str(key).casefold()}"
        with self._cv:
            if self.stop_event.is_set():
                return False
            if token in self._queued:
                return True
            self._pending.append(token)
            self._queued.add(token)
            self._cv.notify_all()
            return True

    def note_attack(self) -> None:
        """Record one successful attack tap (anchor for the grace window)."""

        with self._cv:
            self._last_attack_at = time.monotonic()

    # ------------------------------------------------------------------ #
    # Attack-facing state                                                #
    # ------------------------------------------------------------------ #

    def is_idle(self) -> bool:
        """True when no event is queued and no motion window is running."""

        with self._cv:
            return (not self._pending
                    and time.monotonic() >= self._busy_until)

    def wait_until_idle(self) -> bool:
        """Block until the arbiter is idle; False when stopped first."""

        with self._cv:
            while not self.stop_event.is_set():
                if (not self._pending
                        and time.monotonic() >= self._busy_until):
                    return True
                self._cv.wait(0.1)
            return False

    # ------------------------------------------------------------------ #
    # Executor thread                                                    #
    # ------------------------------------------------------------------ #

    def _duration_for(self, token: str) -> float:
        return (self.jump_motion_seconds if token == JUMP
                else self.buff_motion_seconds)

    def _pop_locked(self, token: str) -> None:
        """Drop *token* from the queue head (single executor owns the head)."""

        if self._pending and self._pending[0] == token:
            self._pending.popleft()
        self._queued.discard(token)

    def run(self) -> None:
        LOG.info("motion arbiter started")
        while not self.stop_event.is_set():
            with self._cv:
                while not self._pending and not self.stop_event.is_set():
                    self._cv.wait(0.2)
                if self.stop_event.is_set():
                    break
                token = self._pending[0]
            self._execute(token)
        LOG.info("motion arbiter stopped")

    def _execute(self, token: str) -> None:
        # Wait out the tail of any attack motion before pressing a motion
        # key; the token stays queued meanwhile, so attack stays suppressed.
        while not self.stop_event.is_set():
            with self._cv:
                grace = self.attack_grace_seconds - (
                    time.monotonic() - self._last_attack_at
                )
            if grace <= 0:
                break
            self.stop_event.wait(grace)
        if self.stop_event.is_set():
            return

        if token == JUMP:
            if (self.climbing_active_event is not None
                    and self.climbing_active_event.is_set()):
                # Movement owns Alt during climb/return: drop the stale jump
                # instead of injecting a chord-breaking Alt press.
                with self._cv:
                    self._pop_locked(token)
                LOG.info("motion arbiter dropped jump: climb/return input "
                         "is active")
                return
            key = "alt"
        else:
            key = token.partition(":")[2]

        tap_ok = False
        try:
            tap_ok = self.key_sender.tap(key) is not False
        except Exception:
            LOG.exception("motion arbiter tap failed key=%s", key)
        with self._cv:
            self._pop_locked(token)
            if tap_ok:
                self._busy_until = time.monotonic() + self._duration_for(token)
        if tap_ok:
            duration = self._duration_for(token)
            LOG.info("motion arbiter executed %s (lock %.2fs)", token, duration)
            if duration > 0:
                # Only the next dequeue - or an attack - may run after this.
                self.stop_event.wait(duration)
        else:
            # Input disabled / window not foreground: drain fast, no window.
            LOG.warning("motion arbiter tap blocked key=%s; event drained",
                        key)


__all__ = ["MotionArbiter"]
