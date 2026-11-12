"""Tap the periodic 时间药 rows (饲料 / 补药) once each, on their own timers.

Two of the drug panel's six rows are **time drugs** rather than 增益 casts: the
item is consumed by the press itself, so it is tapped exactly ONCE (a double tap
would eat two), and it is sent directly - never through ``MotionArbiter``, whose
directional handoff and attack-animation window exist for 增益 casts.

Row ownership after the 2026-11 split:

======================  ===========================  ==============================
panel row               storage fields               owner
======================  ===========================  ==============================
饲料                     ``buff1_key``/``_interval``  TimeDrugWorker  (one tap)
补药 (was 增益 1)        ``buff2_key``/``_interval``  TimeDrugWorker  (one tap)
增益 1 .. 增益 4         ``buff3`` .. ``buff6``       BuffScheduler   (double tap)
======================  ===========================  ==============================

The storage field names are deliberately unchanged: every saved
``user_config.json`` / ``drug_settings.json`` keeps its keys, intervals and
enables, so an update never silently rebinds or drops a customer's rows.  Only
the label, the tap shape and the owning worker changed.

死花模式 opens a gate for 补药 while the character waits at a reached flower
point, so that request/completion pair lives here instead of in the buff
scheduler.  The timer, the charge guard and the "first refresh is manual" rule
are identical to the buff scheduler's, because those rows behaved that way before
the split.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from status_worker import BINDABLE_KEYS

LOG = logging.getLogger(__name__)

# One press, held long enough that the client cannot drop it.  A time drug is
# consumed per press, so this is intentionally NOT the 增益 double tap.
TIME_DRUG_TAP_HOLD_SECONDS = 0.150

# (storage row, panel label).  The order is the order the panel shows them in.
TIME_DRUG_ROWS: tuple[tuple[str, str], ...] = (
    ("buff1", "饲料"),
    ("buff2", "补药"),
)
TIME_DRUG_NAMES: tuple[str, ...] = tuple(name for name, _label in TIME_DRUG_ROWS)
# 补药 is the row 死花模式 waits on at a flower point.
AUX_DRUG_ROW = "buff2"

# Evaluation cadence.  Deliberately independent of the HUD capture rate: a time
# drug must not stop just because no status frame arrived.
TICK_SECONDS = 0.1


class TimeDrugWorker(threading.Thread):
    """Tap each configured 时间药 row on its own timer, exactly once per refresh.

    ``config_provider`` returns the live drug config (the same object the UI
    writes from the 药品 panel, so an edit applies without a restart).
    ``charge_active_callback`` makes a charged attack finish first: a drug tap
    during a held charge would cancel the charge.
    """

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        *,
        config_provider: Callable[[], Any],
        automation_active_event: Optional[threading.Event] = None,
        charge_active_callback: Any = None,
        tick_seconds: float = TICK_SECONDS,
        tap_hold_seconds: float = TIME_DRUG_TAP_HOLD_SECONDS,
    ) -> None:
        super().__init__(name="time-drug", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self._config_provider = config_provider
        self.automation_active_event = automation_active_event
        self.charge_active_callback = charge_active_callback
        self.tick_seconds = max(0.02, float(tick_seconds))
        self.tap_hold_seconds = max(0.01, min(1.0, float(tap_hold_seconds)))
        # Timers start "just fired": the operator takes the first dose by hand and
        # the schedule takes over one interval later (the rule the drug panel has
        # always followed).
        self._last_drug: dict[str, float] = {
            name: time.monotonic() for name in TIME_DRUG_NAMES
        }
        self._lock = threading.Lock()
        # 死花模式 integration for 补药 only.
        self.aux_drug_flower_gate_callback: Any = None
        self.aux_drug_flower_completed_callback: Any = None

    # ---- helpers -----------------------------------------------------------
    def _config(self) -> Any:
        try:
            return self._config_provider()
        except Exception:
            LOG.exception("time drug worker could not read its config")
            return None

    def _charge_active(self) -> bool:
        callback = self.charge_active_callback
        if not callable(callback):
            return False
        try:
            return bool(callback())
        except Exception:
            LOG.exception("time drug charge probe failed")
            return False

    def _automation_active(self) -> bool:
        event = self.automation_active_event
        return True if event is None else bool(event.is_set())

    def _tap(self, key: str) -> bool:
        """Press the drug key once."""

        tap = getattr(self.key_sender, "tap", None)
        if not callable(tap) or not key:
            return False
        try:
            return tap(key, hold_seconds=self.tap_hold_seconds) is not False
        except TypeError:
            # A sender without the hold argument keeps its own default.
            return tap(key) is not False

    def request_aux_drug_at_flower_point(self) -> bool:
        """Make 补药 due at a recorded 死花 point.

        This is a request, not a key send: the next tick still owns the actual
        delivery and applies the placement gate.  Returns False when 补药 is not
        configured, so a flower route never starts moving without its timing
        source.
        """

        config = self._config()
        if config is None:
            return False
        if not (
            getattr(config, f"{AUX_DRUG_ROW}_enabled", False)
            and getattr(config, f"{AUX_DRUG_ROW}_key", "")
            and float(getattr(config, f"{AUX_DRUG_ROW}_interval", 0) or 0) > 0
        ):
            return False
        with self._lock:
            self._last_drug[AUX_DRUG_ROW] = float("-inf")
        return True

    # ---- schedule ----------------------------------------------------------
    def _check_drugs(self, now: float) -> None:
        """Use each configured time drug whose interval has elapsed once."""

        config = self._config()
        if config is None:
            return
        for name, label in TIME_DRUG_ROWS:
            key = str(getattr(config, f"{name}_key", "") or "")
            interval = float(getattr(config, f"{name}_interval", 0.0) or 0.0)
            enabled = bool(getattr(config, f"{name}_enabled", False))
            # "-" is the UI's unbound placeholder: treat it exactly like an empty
            # key so an enabled-but-unbound row cannot send an invalid key.
            if not enabled or not key or key not in BINDABLE_KEYS or interval <= 0:
                continue
            with self._lock:
                if now - self._last_drug.get(name, now) < interval:
                    continue
            if self._charge_active():
                # The charge owns the keyboard; the timer stays due.
                continue
            flower_cycle = False
            if name == AUX_DRUG_ROW:
                gate = self.aux_drug_flower_gate_callback
                if callable(gate):
                    try:
                        # True only while 死花模式 waits at a reached flower: the
                        # tap then also satisfies that flower cycle.  A closed gate
                        # just means this is the ordinary timed use.
                        flower_cycle = bool(gate())
                    except Exception:
                        LOG.exception(
                            "%s flower placement gate failed; keeping timer due", name
                        )
                        continue
            if not self._tap(key):
                continue
            with self._lock:
                self._last_drug[name] = time.monotonic()
            LOG.warning(
                "%s refresh: used %s (every %.0fs)", label, key, interval
            )
            if flower_cycle:
                completed = self.aux_drug_flower_completed_callback
                if callable(completed):
                    try:
                        completed()
                    except Exception:
                        LOG.exception("%s flower completion callback failed", name)

    # ---- thread ------------------------------------------------------------
    def run(self) -> None:
        while not self.stop_event.is_set():
            if self._automation_active():
                try:
                    self._check_drugs(time.monotonic())
                except Exception:
                    LOG.exception("time drug tick failed")
            self.stop_event.wait(self.tick_seconds)


__all__ = [
    "AUX_DRUG_ROW",
    "TIME_DRUG_NAMES",
    "TIME_DRUG_ROWS",
    "TIME_DRUG_TAP_HOLD_SECONDS",
    "TICK_SECONDS",
    "TimeDrugWorker",
]
