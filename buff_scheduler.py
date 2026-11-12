"""Periodic 增益1..5 schedule, deliberately outside the HP/MP worker.

Why this is its own worker
--------------------------
The buff timers used to live inside ``StatusWorker`` - the worker that reads the
bottom HUD bar and drinks HP/MP potions.  That coupling made a buff depend on
things that have nothing to do with a buff:

* the timers were only evaluated when a *status frame* arrived (the 5 fps HUD
  capture, which stops when the capture gate closes), so a stalled or unreadable
  HP/MP region froze every buff;
* "did the cast land?" was answered from an HP/MP delta, which is the wrong
  signal for a buff that does not touch HP/MP: the log then claimed the game cast
  nothing while the buff was fine, and because the timer had already been
  restarted the next real attempt was a whole interval away.

This module owns only the schedule: one thread, one lock, a 100 ms tick, and the
``MotionArbiter`` as the delivery path.  The arbiter keeps the safe-stage gate,
the attack-animation window and the single-key sender, so a buff is still never
injected into the middle of an attack or a climb - but the decision *when* a buff
is due no longer touches HP/MP, potions, or the frame stream.

Cast verification is intentionally NOT here yet: the honest signal is the buff
icon on the HUD, not HP/MP, and that is a separate change.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

# The UI's bindable-key set lives with the HUD worker, which owns the drug panel
# config this scheduler reads.  Importing the constant keeps one source of truth.
from status_worker import BINDABLE_KEYS

LOG = logging.getLogger(__name__)

# How long a buff key is held down.  Measured: a shorter press is dropped by the
# game client often enough that a whole refresh is lost, and each miss costs one
# interval (minutes).
BUFF_TAP_HOLD_SECONDS = 0.200

# Evaluation cadence.  Deliberately independent of the HUD capture rate: the
# buff must not stop because a status frame stopped.
TICK_SECONDS = 0.1

BUFF_NAMES = ("buff1", "buff2", "buff3", "buff4", "buff5", "buff6")


class BuffScheduler(threading.Thread):
    """Tap the periodic 增益 rows on their own timers.

    ``config_provider`` returns the live drug/buff config (the same object the
    UI writes when the 药品/增益 panel changes, so edits apply without a
    restart).  ``motion_arbiter`` is optional: with it the taps are queued behind
    the motion gates; without it they are tapped directly, which is what a
    reduced configuration or a test rig does.
    """

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        *,
        config_provider: Callable[[], Any],
        automation_active_event: Optional[threading.Event] = None,
        motion_arbiter: Any = None,
        charge_active_callback: Any = None,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        super().__init__(name="buff-scheduler", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self._config_provider = config_provider
        self.automation_active_event = automation_active_event
        self.motion_arbiter = motion_arbiter
        # Ordinary timed buffs defer while the attack key is held for a charge.
        self.charge_active_callback = charge_active_callback
        self.tick_seconds = max(0.02, float(tick_seconds))
        # Timers start "just fired": the operator triggers the first buff by hand
        # and the schedule takes over one interval later.
        self._last_buff: dict[str, float] = {name: time.monotonic() for name in BUFF_NAMES}
        # Buff 2/3/4/5 can wait inside MotionArbiter (climbing, post-attack).  One
        # local in-flight marker prevents an elapsed timer from queueing
        # duplicates; it clears only when the arbiter reports the outcome.
        self._buff_pending: dict[str, bool] = {
            "buff2": False, "buff3": False, "buff4": False, "buff5": False,
            "buff6": False,
        }
        self._lock = threading.Lock()
        # Optional 死花模式 integration.  The UI mapping is fixed: buff1 is 饲料,
        # buff2 is 增益1.  The flower cadence only makes 增益1 due at a reached
        # flower point; 饲料 stays an ordinary periodic drug.
        self.buff2_flower_gate_callback: Any = None
        self.buff2_flower_completed_callback: Any = None

    # ---- helpers -----------------------------------------------------------
    def _config(self) -> Any:
        try:
            return self._config_provider()
        except Exception:
            LOG.exception("buff scheduler could not read its config")
            return None

    def _charge_active(self) -> bool:
        callback = self.charge_active_callback
        if not callable(callback):
            return False
        try:
            return bool(callback())
        except Exception:
            LOG.exception("buff scheduler charge probe failed")
            return False

    def _automation_active(self) -> bool:
        event = self.automation_active_event
        return True if event is None else bool(event.is_set())

    def request_buff2_at_flower_point(self) -> bool:
        """Make 增益1 due at a recorded Dead Flower point.

        This is a request, not a key send: the next tick still owns the actual
        delivery and applies the placement gate.  It returns false when 增益1 is
        not configured, so a flower route never silently starts moving without
        its timing source.
        """

        config = self._config()
        if config is None:
            return False
        if not (
            getattr(config, "buff2_enabled", False)
            and getattr(config, "buff2_key", "")
            and float(getattr(config, "buff2_interval", 0) or 0) > 0
        ):
            return False
        with self._lock:
            self._last_buff["buff2"] = float("-inf")
        return True

    # ---- schedule ----------------------------------------------------------
    def _check_buffs(self, now: float) -> None:
        """Tap each configured buff whose interval has elapsed."""

        config = self._config()
        if config is None:
            return
        for name in BUFF_NAMES:
            key = str(getattr(config, f"{name}_key", "") or "")
            interval = float(getattr(config, f"{name}_interval", 0.0) or 0.0)
            enabled = bool(getattr(config, f"{name}_enabled", False))
            # "-" is the UI's unbound placeholder: treat it exactly like an empty
            # key so an enabled-but-unbound row cannot send an invalid key.
            if not enabled or not key or key not in BINDABLE_KEYS or interval <= 0:
                continue
            with self._lock:
                due = now - self._last_buff[name] >= interval
                pending = self._buff_pending.get(name, False)
            if not due or pending:
                continue

            if name == "buff1":
                # 宠物食品 is a periodic drug, not an action buff.  It is sent
                # directly and never joins the arbiter's directional handoff.
                if self._charge_active():
                    continue
                if self.key_sender.tap(key, hold_seconds=BUFF_TAP_HOLD_SECONDS):
                    with self._lock:
                        self._last_buff[name] = time.monotonic()
                    LOG.warning("宠物食品 refresh: used %s (every %.0fs)",
                                key, interval)
                continue

            if name == "buff2":
                # Dead Flower opens this gate only while waiting at a reached
                # flower.  In every other mode buff2 follows the arbiter path.
                gate = self.buff2_flower_gate_callback
                flower_gate_open = False
                if callable(gate):
                    try:
                        flower_gate_open = bool(gate())
                    except Exception:
                        LOG.exception("增益1 flower placement gate failed; keeping timer due")
                        continue
                if flower_gate_open:
                    if self._charge_active():
                        continue
                    if self.key_sender.tap(key, hold_seconds=BUFF_TAP_HOLD_SECONDS):
                        with self._lock:
                            self._last_buff[name] = time.monotonic()
                        LOG.warning("增益1 花点 refresh: used %s (every %.0fs)",
                                    key, interval)
                        completed = self.buff2_flower_completed_callback
                        if callable(completed):
                            try:
                                completed()
                            except Exception:
                                LOG.exception("增益1 flower completion callback failed")
                    continue

            if self.motion_arbiter is not None:
                # The timer is deliberately NOT restarted when queued.  It
                # restarts only after the arbiter has tapped the key, waited
                # through the action window and called this completion hook.
                def _completed(ok: bool, *, _name: str = name,
                               _key: str = key, _interval: float = interval) -> None:
                    with self._lock:
                        self._buff_pending[_name] = False
                        if ok:
                            self._last_buff[_name] = time.monotonic()
                    if ok:
                        LOG.warning("%s refresh: completed %s (every %.0fs)",
                                    _name.upper(), _key, _interval)
                    else:
                        LOG.warning("%s refresh: not sent; timer remains due",
                                    _name.upper())

                # Claim the pending marker *before* handing the request over: the
                # arbiter can execute immediately on an idle patrol, and claiming
                # afterwards could overwrite its already delivered completion.
                with self._lock:
                    self._buff_pending[name] = True
                if self.motion_arbiter.request_buff(key, _completed):
                    LOG.warning("%s refresh: queued %s (every %.0fs)",
                                name.upper(), key, interval)
                else:
                    with self._lock:
                        self._buff_pending[name] = False
                continue

            # No arbiter: tap directly, still with the buff hold time.
            if self.key_sender.tap(key, hold_seconds=BUFF_TAP_HOLD_SECONDS):
                with self._lock:
                    self._last_buff[name] = time.monotonic()
                LOG.warning("%s refresh: tapped %s (every %.0fs)",
                            name.upper(), key, interval)

    # ---- thread ------------------------------------------------------------
    def run(self) -> None:
        while not self.stop_event.is_set():
            if self._automation_active():
                try:
                    self._check_buffs(time.monotonic())
                except Exception:
                    LOG.exception("buff schedule tick failed")
            self.stop_event.wait(self.tick_seconds)


__all__ = ["BuffScheduler", "BUFF_NAMES", "BUFF_TAP_HOLD_SECONDS", "TICK_SECONDS"]
