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
JUMP_COUNTERATTACK_OPPOSITE_HOLD_SECONDS = 0.045
JUMP_COUNTERATTACK_ATTACK_GAP_SECONDS = 0.010
ATTACK_TAP_HOLD_SECONDS = 0.045
CHARGE_HOLD_MIN_SECONDS = 0.1
CHARGE_HOLD_MAX_SECONDS = 5.0


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
        # A charge is an attack-key hold rather than a normal tap.  Other
        # workers use this state to defer nonessential direct key sends while
        # ordinary Left/Right patrol movement remains independent.
        self._charge_active = threading.Event()
        self._charge_cancel = threading.Event()
        self._charge_finished = threading.Event()
        self._charge_finished.set()
        # The MovementWorker registers this optional callback after it exists.
        # It owns the authoritative Left/Right hold ledger, which is the only
        # safe place to borrow a patrol direction for 跳反打.
        self._jump_counterattack_executor: Callable[..., bool | None] | None = None

    def set_jump_counterattack_executor(
        self, executor: Callable[..., bool | None] | None,
    ) -> None:
        """Attach the movement-owned directional recoil executor."""

        self._jump_counterattack_executor = executor

    def active_name(self) -> str:
        """Return the active sequence label, or ``''`` when idle."""

        with self._state_lock:
            return self._active_name

    def charge_active(self) -> bool:
        """Whether an attack key is currently held by a charge sequence."""

        return self._charge_active.is_set()

    def interrupt_charge(self, timeout_seconds: float = 0.08) -> bool:
        """Ask an active charge to release, waiting only for a short handoff.

        HP/MP recovery uses this before its priority potion tap.  The charge
        runner checks cancellation every 10 ms, so this never waits for the
        configured charge duration.
        """

        if not self._charge_active.is_set():
            return True
        self._charge_cancel.set()
        return self._charge_finished.wait(max(0.0, float(timeout_seconds)))

    def _allowed(self, extra_allowed: Callable[[], bool] | None) -> bool:
        if self.stop_event.is_set():
            return False
        if (self.automation_active_event is not None
                and not self.automation_active_event.is_set()):
            return False
        input_enabled = getattr(self.key_sender, "input_is_enabled", None)
        if callable(input_enabled) and not input_enabled():
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
        jump_counterattack_enabled: bool = False,
        charge_attack_enabled: bool = False,
        charge_hold_seconds: float = 0.5,
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
            if charge_attack_enabled:
                return self._execute_charge(key, charge_hold_seconds, allowed, label)
            if not jump_attack_enabled:
                return self._tap_attack(key)

            tap = getattr(self.key_sender, "tap", None)
            if not callable(tap):
                return False
            if not self._allowed(allowed):
                return False
            recoil_executor = self._jump_counterattack_executor
            if jump_counterattack_enabled and callable(recoil_executor):
                # The movement callback must claim its direction lock BEFORE
                # Alt is sent.  Emitting Alt here first let a route update
                # retain that lock through its handoff, after which the
                # callback saw no eligible held patrol direction and silently
                # degraded into an ordinary jump attack.
                recoil_result = recoil_executor(
                    key,
                    lambda seconds: self._wait_allowed(seconds, allowed),
                    lambda: tap(
                        "alt", hold_seconds=JUMP_ATTACK_ALT_HOLD_SECONDS,
                    ) is not False,
                    self._tap_attack,
                    JUMP_COUNTERATTACK_OPPOSITE_HOLD_SECONDS,
                    JUMP_COUNTERATTACK_ATTACK_GAP_SECONDS,
                )
                if recoil_result is not None:
                    if recoil_result:
                        LOG.info(
                            "atomic motion %s executed: alt %.0fms + opposite walk %.0fms "
                            "+ attack %s while the opposite direction is held "
                            "+ settle %.0fms",
                            label,
                            JUMP_ATTACK_ALT_HOLD_SECONDS * 1000.0,
                            JUMP_COUNTERATTACK_OPPOSITE_HOLD_SECONDS * 1000.0,
                            key,
                            JUMP_COUNTERATTACK_ATTACK_GAP_SECONDS * 1000.0,
                        )
                    return bool(recoil_result)
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

    def _execute_charge(
        self,
        key: str,
        hold_seconds: float,
        allowed: Callable[[], bool] | None,
        label: str,
    ) -> bool:
        """Hold and release one attack key without taking patrol directions."""

        key_down = getattr(self.key_sender, "key_down", None)
        key_up = getattr(self.key_sender, "key_up", None)
        if not callable(key_down) or not callable(key_up):
            return False
        duration = min(
            CHARGE_HOLD_MAX_SECONDS,
            max(CHARGE_HOLD_MIN_SECONDS, float(hold_seconds)),
        )
        self._charge_cancel.clear()
        self._charge_finished.clear()
        self._charge_active.set()
        started = time.monotonic()
        claimed = False
        own_ctrl = key == "ctrl"
        begin_ctrl = getattr(self.key_sender, "_begin_ctrl_chord", None)
        end_ctrl = getattr(self.key_sender, "_end_ctrl_chord", None)
        try:
            if own_ctrl and callable(begin_ctrl):
                begin_ctrl()
            if not self._allowed(allowed):
                return False
            claimed = key_down(key, owner="charge", attack_diagnostic=True) is not False
            if not claimed:
                return False
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                if self._charge_cancel.is_set() or not self._allowed(allowed):
                    LOG.info("atomic motion %s charge interrupted after %.0fms",
                             label, (time.monotonic() - started) * 1000.0)
                    return False
                if self.stop_event.wait(min(0.010, deadline - time.monotonic())):
                    return False
            LOG.info("atomic motion %s executed: charge %s %.1fs", label, key, duration)
            return True
        finally:
            if claimed:
                key_up(
                    key, owner="charge",
                    attack_diagnostic_started_at=started,
                )
            if own_ctrl and callable(end_ctrl):
                end_ctrl()
            self._charge_active.clear()
            self._charge_finished.set()


__all__ = [
    "AtomicMotionRunner",
    "ATTACK_TAP_HOLD_SECONDS",
    "CHARGE_HOLD_MAX_SECONDS",
    "CHARGE_HOLD_MIN_SECONDS",
    "JUMP_ATTACK_AIR_GAP_SECONDS",
    "JUMP_ATTACK_ALT_HOLD_SECONDS",
]
