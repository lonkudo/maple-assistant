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

Attack taps are not queued (they keep their own cadence), but they do acquire
an atomic short-lived reservation before emitting input.  This matters: an
``is_idle()`` check followed by a key tap is otherwise a race with a newly
queued micro-step.  HP/MP potions stay on their own urgent path (bar-verified
retries), outside this queue.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Optional


LOG = logging.getLogger(__name__)

JUMP = "jump"
MICRO_STEP = "micro_step"


class MotionArbiter(threading.Thread):
    """One-at-a-time executor for jump/buff motion keys."""

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        *,
        climbing_active_event: Optional[threading.Event] = None,
        automation_active_event: Optional[threading.Event] = None,
        jump_motion_seconds: float = 0.9,
        buff_motion_seconds: float = 0.6,
        micro_step_motion_seconds: float = 0.25,
        attack_grace_seconds: float = 0.3,
    ) -> None:
        super().__init__(name="motion-arbiter", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.climbing_active_event = climbing_active_event
        self.automation_active_event = automation_active_event
        self.jump_motion_seconds = max(0.0, float(jump_motion_seconds))
        self.buff_motion_seconds = max(0.0, float(buff_motion_seconds))
        self.micro_step_motion_seconds = max(
            0.0, float(micro_step_motion_seconds)
        )
        self.attack_grace_seconds = max(0.0, float(attack_grace_seconds))
        # Installed after MovementWorker exists.  The arbiter serializes the
        # timing, while movement owns the directional key handoff itself.
        self._micro_step_callback: Any = None
        # Movement owns direction holds, so a queued buff borrows that same
        # owner for one atomic key tap before patrol resumes.
        self._buff_callback: Any = None
        self._motion_gate_callback: Any = None
        self._cv = threading.Condition()
        self._pending: "deque[str]" = deque()
        # Dedupe identities: "jump" or "buff:<key>".  A pending event of the
        # same kind means "do it once when free" - never pile up repeats.
        self._queued: set[str] = set()
        self._busy_until = 0.0  # monotonic end of the running motion window
        self._last_attack_at = float("-inf")
        # Attack reservations make the idle-check/tap pair atomic.  A queued
        # motion sees this reservation and waits; a fresh attack sees a queued
        # motion and defers.  Therefore neither can begin inside the other.
        self._attack_reserved = False
        self._executing_token: Optional[str] = None
        # Completion callbacks belong to periodic-buff timers.  They are
        # invoked only once the key has completed its full motion window.
        self._buff_completion_callbacks: dict[str, list[Any]] = {}

    # ------------------------------------------------------------------ #
    # Registration (any thread)                                          #
    # ------------------------------------------------------------------ #

    def request_jump(self) -> bool:
        """Queue one jump (Alt tap). Duplicate pending jumps collapse."""

        with self._cv:
            if (not self._automation_allowed_locked()
                    or not self._motion_gate_allows_locked()):
                return False
            if JUMP in self._queued:
                return True
            self._pending.append(JUMP)
            self._queued.add(JUMP)
            self._cv.notify_all()
            return True

    def request_buff(self, key: str, on_complete: Any = None) -> bool:
        """Queue one periodic buff key tap and notify after it completes.

        Buffs may be registered while climbing or transitioning, but are held
        until MovementWorker reports a safe left/right or rope-approach stage.
        This prevents an elapsed buff timer from being silently lost.
        """

        token = f"buff:{str(key).casefold()}"
        with self._cv:
            if not self._automation_allowed_locked():
                return False
            if token in self._queued:
                if callable(on_complete):
                    self._buff_completion_callbacks.setdefault(token, []).append(
                        on_complete
                    )
                return True
            self._pending.append(token)
            self._queued.add(token)
            if callable(on_complete):
                self._buff_completion_callbacks[token] = [on_complete]
            self._cv.notify_all()
            return True

    def request_micro_step(self) -> bool:
        """Queue one Left/Right micro-step; duplicate requests collapse."""

        with self._cv:
            if (not self._automation_allowed_locked()
                    or not self._motion_gate_allows_locked()):
                return False
            if MICRO_STEP in self._queued:
                return True
            self._pending.append(MICRO_STEP)
            self._queued.add(MICRO_STEP)
            self._cv.notify_all()
            return True

    def set_micro_step_callback(self, callback: Any) -> None:
        """Install MovementWorker's serialized micro-step implementation."""

        with self._cv:
            self._micro_step_callback = callback

    def set_buff_callback(self, callback: Any) -> None:
        """Install MovementWorker's atomic buff handoff implementation."""

        with self._cv:
            self._buff_callback = callback

    def set_motion_gate_callback(self, callback: Any) -> None:
        """Allow queued motion only when MovementWorker reports safe travel."""

        with self._cv:
            self._motion_gate_callback = callback

    def _automation_allowed_locked(self) -> bool:
        return bool(
            not self.stop_event.is_set()
            and (self.automation_active_event is None
                 or self.automation_active_event.is_set())
        )

    def _motion_gate_allows_locked(self) -> bool:
        callback = self._motion_gate_callback
        if not callable(callback):
            return True
        try:
            return bool(callback())
        except Exception:
            LOG.exception("motion arbiter safe-stage gate failed")
            return False

    def try_begin_attack(self) -> bool:
        """Atomically reserve the keyboard for one fixed-attack tap.

        Call ``finish_attack`` in a ``finally`` block after a successful
        reservation.  This replaces the unsafe ``is_idle`` then ``tap``
        sequence: a micro-step, jump, or buff cannot slip between those two
        operations and change the character's direction mid-animation.
        """

        with self._cv:
            pending_blocks = bool(self._pending) and (
                any(not token.startswith("buff:") for token in self._pending)
                or self._motion_gate_allows_locked()
            )
            if (not self._automation_allowed_locked()
                    or self._attack_reserved
                    or self._executing_token is not None
                    or pending_blocks
                    or time.monotonic() < self._busy_until):
                return False
            self._attack_reserved = True
            return True

    def finish_attack(self, sent: bool) -> None:
        """Release an attack reservation and start its grace window if sent."""

        with self._cv:
            if sent:
                self._last_attack_at = time.monotonic()
            self._attack_reserved = False
            self._cv.notify_all()

    def note_attack(self) -> None:
        """Backward-compatible shorthand for legacy direct attack callers."""

        with self._cv:
            self._last_attack_at = time.monotonic()
            self._cv.notify_all()

    # ------------------------------------------------------------------ #
    # Attack-facing state                                                #
    # ------------------------------------------------------------------ #

    def is_idle(self) -> bool:
        """True when no event is queued and no motion window is running."""

        with self._cv:
            return (not self._pending
                    and not self._attack_reserved
                    and self._executing_token is None
                    and time.monotonic() >= self._busy_until)

    def wait_until_idle(self) -> bool:
        """Block until the arbiter is idle; False when stopped first."""

        with self._cv:
            while not self.stop_event.is_set():
                if (not self._pending
                        and not self._attack_reserved
                        and self._executing_token is None
                        and time.monotonic() >= self._busy_until):
                    return True
                self._cv.wait(0.1)
            return False

    # ------------------------------------------------------------------ #
    # Executor thread                                                    #
    # ------------------------------------------------------------------ #

    def _duration_for(self, token: str) -> float:
        if token == JUMP:
            return self.jump_motion_seconds
        if token == MICRO_STEP:
            return self.micro_step_motion_seconds
        return self.buff_motion_seconds

    def _pop_locked(self, token: str) -> list[Any]:
        """Drop *token* from the queue head (single executor owns the head)."""

        if self._pending and self._pending[0] == token:
            self._pending.popleft()
        self._queued.discard(token)
        return self._buff_completion_callbacks.pop(token, [])

    @staticmethod
    def _notify_buff_completion(callbacks: list[Any], succeeded: bool) -> None:
        for callback in callbacks:
            try:
                callback(succeeded)
            except Exception:
                LOG.exception("motion arbiter buff completion callback failed")

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
        # A request made before Stop Patrol must never execute later.  Drop it
        # here as well as at registration because it may have been queued just
        # before the automation gate was cleared.
        with self._cv:
            while not self.stop_event.is_set():
                if not self._automation_allowed_locked():
                    callbacks = self._pop_locked(token)
                    self._cv.notify_all()
                    self._notify_buff_completion(callbacks, False)
                    return
                if not self._motion_gate_allows_locked():
                    if token.startswith("buff:"):
                        # A buff stays registered through climb/drop/landing
                        # and wakes itself as soon as ordinary travel returns.
                        self._cv.wait(0.10)
                        continue
                    callbacks = self._pop_locked(token)
                    self._cv.notify_all()
                    self._notify_buff_completion(callbacks, False)
                    return
                if self._attack_reserved:
                    self._cv.wait(0.05)
                    continue
                self._executing_token = token
                break
            else:
                return
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
            with self._cv:
                self._executing_token = None
                callbacks = self._pop_locked(token)
                self._cv.notify_all()
            self._notify_buff_completion(callbacks, False)
            return

        # The grace wait can overlap a movement transition.  Recheck the safe
        # stage immediately before injecting the key; a buff remains queued
        # and tries again later, whereas jump/micro-step are stale and drain.
        with self._cv:
            dropped = False
            if not self._automation_allowed_locked():
                self._executing_token = None
                callbacks = self._pop_locked(token)
                self._cv.notify_all()
                dropped = True
            elif not self._motion_gate_allows_locked():
                self._executing_token = None
                if token.startswith("buff:"):
                    self._cv.notify_all()
                    return
                callbacks = self._pop_locked(token)
                self._cv.notify_all()
                dropped = True
            else:
                callbacks = []
        if dropped:
            self._notify_buff_completion(callbacks, False)
            return

        if token == JUMP:
            if (self.climbing_active_event is not None
                    and self.climbing_active_event.is_set()):
                # Movement owns Alt during climb/return: drop the stale jump
                # instead of injecting a chord-breaking Alt press.
                with self._cv:
                    callbacks = self._pop_locked(token)
                    self._executing_token = None
                    self._cv.notify_all()
                self._notify_buff_completion(callbacks, False)
                LOG.info("motion arbiter dropped jump: climb/return input "
                         "is active")
                return
            key = "alt"
        elif token == MICRO_STEP:
            if (self.climbing_active_event is not None
                    and self.climbing_active_event.is_set()):
                with self._cv:
                    callbacks = self._pop_locked(token)
                    self._executing_token = None
                    self._cv.notify_all()
                self._notify_buff_completion(callbacks, False)
                LOG.info("motion arbiter dropped micro-step: climb/return input "
                         "is active")
                return
            with self._cv:
                callback = self._micro_step_callback
            if not callable(callback):
                with self._cv:
                    callbacks = self._pop_locked(token)
                    self._executing_token = None
                    self._cv.notify_all()
                self._notify_buff_completion(callbacks, False)
                LOG.warning("motion arbiter dropped micro-step: movement unavailable")
                return
            try:
                tap_ok = callback() is not False
            except Exception:
                LOG.exception("motion arbiter micro-step failed")
                tap_ok = False
            with self._cv:
                self._pop_locked(token)
                self._executing_token = None
                if tap_ok:
                    self._busy_until = time.monotonic() + self._duration_for(token)
                self._cv.notify_all()
            if tap_ok:
                LOG.info("motion arbiter executed micro-step (lock %.2fs)",
                         self._duration_for(token))
                self.stop_event.wait(self._duration_for(token))
            else:
                LOG.warning("motion arbiter micro-step blocked; event drained")
            return
        else:
            key = token.partition(":")[2]

        tap_ok = False
        try:
            callback = None
            if token.startswith("buff:"):
                with self._cv:
                    callback = self._buff_callback
            if callable(callback):
                tap_ok = callback(key) is not False
            else:
                tap_ok = self.key_sender.tap(key) is not False
        except Exception:
            LOG.exception("motion arbiter tap failed key=%s", key)
        with self._cv:
            callbacks = self._pop_locked(token)
            self._executing_token = None
            if tap_ok:
                self._busy_until = time.monotonic() + self._duration_for(token)
            self._cv.notify_all()
        if tap_ok:
            duration = self._duration_for(token)
            LOG.info("motion arbiter executed %s (lock %.2fs)", token, duration)
            if duration > 0:
                # Only the next dequeue - or an attack - may run after this.
                self.stop_event.wait(duration)
            self._notify_buff_completion(
                callbacks, not self.stop_event.is_set()
            )
        else:
            # Input disabled / window not foreground: drain fast, no window.
            LOG.warning("motion arbiter tap blocked key=%s; event drained",
                        key)
            self._notify_buff_completion(callbacks, False)


__all__ = ["MotionArbiter"]
