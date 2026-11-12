"""Periodic 增益 1..4 schedule, deliberately outside the HP/MP worker.

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
# The buff tap shape is shared with every other buff owner (MotionArbiter and
# MovementWorker.perform_arbiter_buff): 150 ms press, short release, 150 ms press.
from motion_arbiter import BUFF_TAP_HOLD_SECONDS, buff_double_tap

LOG = logging.getLogger(__name__)

# How a 增益 key is pressed: twice, 150 ms each, with a short release between
# (``motion_arbiter.buff_double_tap``).  Measured: a single shorter press is
# dropped by the game client often enough that a whole refresh is lost, and each
# miss costs one interval (minutes); one longer hold is not what the client wants.
# The constant itself lives with the other buff tap parameters.
#
# Only 增益 rows live here.  The panel's 时间药 rows (饲料 / 补药) are
# consumed per press, so they are single-tapped by ``time_drug_worker`` instead.

# Evaluation cadence.  Deliberately independent of the HUD capture rate: the
# buff must not stop because a status frame stopped.
TICK_SECONDS = 0.1

# 增益 1 .. 增益 4.  The storage fields keep their historical names (the two
# 时间药 rows own ``buff1``/``buff2``), so every saved configuration still maps to
# the row it was recorded on.
BUFF_NAMES = ("buff3", "buff4", "buff5", "buff6")


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
        # A direct (no-arbiter) tap still defers while the attack key is held for a
        # charge; the arbiter path serializes it anyway.
        self.charge_active_callback = charge_active_callback
        self.tick_seconds = max(0.02, float(tick_seconds))
        # Timers start "just fired": the operator triggers the first buff by hand
        # and the schedule takes over one interval later.
        self._last_buff: dict[str, float] = {name: time.monotonic() for name in BUFF_NAMES}
        # Buffs can wait inside MotionArbiter (climbing, post-attack).  One local
        # in-flight marker prevents an elapsed timer from queueing duplicates; it
        # clears only when the arbiter reports the outcome.
        self._buff_pending: dict[str, bool] = {name: False for name in BUFF_NAMES}
        self._lock = threading.Lock()

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

    # ---- schedule ----------------------------------------------------------
    def _check_buffs(self, now: float) -> None:
        """Tap each configured 增益 whose interval has elapsed."""

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

            # No arbiter: double-tap directly, still with the buff hold time.  A
            # held charge owns the keyboard, so the timer simply stays due.
            if self._charge_active():
                continue
            if self._tap_buff(key):
                with self._lock:
                    self._last_buff[name] = time.monotonic()
                LOG.warning("%s refresh: tapped %s (every %.0fs)",
                            name.upper(), key, interval)

    def _tap_buff(self, key: str) -> bool:
        """Double-tap one 增益 key with the shared buff tap shape."""

        return buff_double_tap(
            self.key_sender, key, hold_seconds=BUFF_TAP_HOLD_SECONDS,
            wait=self.stop_event.wait,
        )

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
