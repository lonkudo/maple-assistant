"""Small, reusable atomic input sequences.

This layer deliberately is not a :mod:`motion_arbiter` queue.  Its purpose is
to keep the phases of one short combat sequence together while preserving any
ordinary patrol Left/Right hold already owned by ``MovementWorker``.  The
caller still obtains the appropriate arbiter admission first (the fixed attack
lease or an existing correction/counterattack token); this runner simply makes
the multi-key portion indivisible.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable


LOG = logging.getLogger(__name__)


# Requested timings, measured from Alt-down.  The combat key therefore begins
# 100 ms after the jump starts: Alt held 45 ms, then 55 ms of airborne gap.
JUMP_ATTACK_ALT_HOLD_SECONDS = 0.045
JUMP_ATTACK_AIR_GAP_SECONDS = 0.055
ATTACK_TAP_HOLD_SECONDS = 0.045


class AtomicMotionRunner:
    """Execute one named combat sequence without phase interleaving.

    This class has no queue and never claims directional keys.  ``execute``
    runs on the caller's worker thread, under a non-blocking combat lock.  A
    duplicate caller therefore loses one beat instead of waiting and injecting
    an old action late.  Existing MotionArbiter admission remains responsible
    for ordering against buffs, corrections and jumps.
    """

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        *,
        automation_active_event: threading.Event | None = None,
    ) -> None:
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.automation_active_event = automation_active_event
        self._combat_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_name = ""

    def active_name(self) -> str:
        """Return the active sequence label, or ``''`` when idle."""

        with self._state_lock:
            return self._active_name

    def _allowed(self, extra_allowed: Callable[[], bool] | None) -> bool:
        if self.stop_event.is_set():
            return False
        if (self.automation_active_event is not None
                and not self.automation_active_event.is_set()):
            return False
        if callable(extra_allowed):
            try:
                return bool(extra_allowed())
            except Exception:
                LOG.exception("atomic motion eligibility callback failed")
                return False
        return True

    def _wait_allowed(
        self,
        seconds: float,
        extra_allowed: Callable[[], bool] | None,
    ) -> bool:
        """Wait without delaying cancellation beyond one 10 ms slice."""

        deadline = time.monotonic() + max(0.0, float(seconds))
        while True:
            if not self._allowed(extra_allowed):
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return True
            if self.stop_event.wait(min(0.010, remaining)):
                return False

    def _tap_attack(self, key: str) -> bool:
        tap = getattr(self.key_sender, "tap_attack", None)
        if not callable(tap):
            tap = getattr(self.key_sender, "tap", None)
        if not callable(tap):
            return False
        return tap(key, hold_seconds=ATTACK_TAP_HOLD_SECONDS) is not False

    def execute_combat_attack(
        self,
        key: str,
        *,
        jump_attack_enabled: bool,
        allowed: Callable[[], bool] | None = None,
        label: str = "combat-attack",
    ) -> bool:
        """Emit a normal or jump-combat attack as one indivisible sequence.

        ``jump_attack_enabled`` is supplied as a value, not read later from
        UI state.  A checkbox change during the 55 ms airborne gap therefore
        cannot turn an already-started sequence into a malformed half-motion.
        """

        key = str(key).strip().casefold()
        if not key or key == "-" or not self._allowed(allowed):
            return False
        if not self._combat_lock.acquire(blocking=False):
            LOG.info("atomic motion %s skipped: combat lane already owns %s",
                     label, self.active_name() or "another sequence")
            return False
        try:
            with self._state_lock:
                self._active_name = str(label)
            if not jump_attack_enabled:
                return self._tap_attack(key)

            tap = getattr(self.key_sender, "tap", None)
            if not callable(tap):
                return False
            if not self._allowed(allowed):
                return False
            if tap("alt", hold_seconds=JUMP_ATTACK_ALT_HOLD_SECONDS) is False:
                return False
            if not self._wait_allowed(JUMP_ATTACK_AIR_GAP_SECONDS, allowed):
                LOG.info("atomic motion %s cancelled during %.0fms airborne gap",
                         label, JUMP_ATTACK_AIR_GAP_SECONDS * 1000.0)
                return False
            if not self._allowed(allowed):
                return False
            sent = self._tap_attack(key)
            if sent:
                LOG.info(
                    "atomic motion %s executed: alt %.0fms + gap %.0fms + attack %s",
                    label,
                    JUMP_ATTACK_ALT_HOLD_SECONDS * 1000.0,
                    JUMP_ATTACK_AIR_GAP_SECONDS * 1000.0,
                    key,
                )
            return sent
        finally:
            with self._state_lock:
                self._active_name = ""
            self._combat_lock.release()


__all__ = [
    "AtomicMotionRunner",
    "ATTACK_TAP_HOLD_SECONDS",
    "JUMP_ATTACK_AIR_GAP_SECONDS",
    "JUMP_ATTACK_ALT_HOLD_SECONDS",
]
