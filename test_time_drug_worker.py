"""时间药 tests (``time_drug_worker``): 饲料 and 补药.

A time drug is consumed by the press itself, so every rule here is about being
exactly ONE tap: one press per refresh, never the 增益 double tap, never the
motion arbiter.
"""

import threading
import time
import unittest
from dataclasses import replace

from status_worker import StatusConfig
from time_drug_worker import (
    AUX_DRUG_ROW,
    TIME_DRUG_NAMES,
    TIME_DRUG_TAP_HOLD_SECONDS,
    TimeDrugWorker,
)


class FakeSender:
    """Records every press and the hold it asked for."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = bool(ok)
        self.keys: list[str] = []
        self.holds: list[float | None] = []

    def tap(self, key: str, *, hold_seconds: float | None = None) -> bool:
        self.keys.append(key)
        self.holds.append(hold_seconds)
        return self.ok


def worker_with(config: StatusConfig, sender: FakeSender,
                *, automation: bool = True) -> TimeDrugWorker:
    event = threading.Event()
    if automation:
        event.set()
    return TimeDrugWorker(
        sender,
        threading.Event(),
        config_provider=lambda: config,
        automation_active_event=event,
    )


class TimeDrugTests(unittest.TestCase):
    def test_the_two_rows_are_pet_food_and_the_aux_drug(self) -> None:
        self.assertEqual(TIME_DRUG_NAMES, ("buff1", "buff2"))
        self.assertEqual(AUX_DRUG_ROW, "buff2")
        # The storage row of 补药 is the row the 增益 1 label used to own.
        self.assertEqual(StatusConfig().buff2_key, "insert")

    def test_a_refresh_is_exactly_one_tap_with_the_drug_hold(self) -> None:
        sender = FakeSender()
        config = replace(
            StatusConfig(),
            buff1_key="home", buff1_interval=60.0, buff1_enabled=True,
        )
        worker = worker_with(config, sender)
        worker._last_drug["buff1"] = time.monotonic() - 61.0
        worker._check_drugs(time.monotonic())
        # ONE press: a double tap would consume two items.
        self.assertEqual(sender.keys, ["home"])
        self.assertEqual(sender.holds, [TIME_DRUG_TAP_HOLD_SECONDS])
        self.assertAlmostEqual(TIME_DRUG_TAP_HOLD_SECONDS, 0.15)

    def test_both_rows_fire_once_independently(self) -> None:
        sender = FakeSender()
        config = replace(
            StatusConfig(),
            buff1_key="home", buff1_interval=60.0, buff1_enabled=True,
            buff2_key="insert", buff2_interval=120.0, buff2_enabled=True,
        )
        worker = worker_with(config, sender)
        worker._last_drug["buff1"] = time.monotonic() - 61.0
        worker._last_drug["buff2"] = time.monotonic() - 121.0
        worker._check_drugs(time.monotonic())
        self.assertEqual(sender.keys, ["home", "insert"])
        # 饲料 is due again a minute later; 补药 is not for two.
        sender.keys.clear()
        worker._last_drug["buff1"] = time.monotonic() - 61.0
        worker._check_drugs(time.monotonic())
        self.assertEqual(sender.keys, ["home"])

    def test_the_first_dose_is_taken_by_hand(self) -> None:
        sender = FakeSender()
        worker = worker_with(
            replace(StatusConfig(), buff1_key="home", buff1_interval=60.0,
                    buff1_enabled=True),
            sender,
        )
        worker._check_drugs(time.monotonic())      # fresh timer: not due
        self.assertEqual(sender.keys, [])

    def test_disabled_or_unbound_never_taps(self) -> None:
        sender = FakeSender()
        worker = worker_with(StatusConfig(), sender)          # disabled defaults
        worker._last_drug.update({"buff1": float("-inf"), "buff2": float("-inf")})
        worker._check_drugs(time.monotonic())
        self.assertEqual(sender.keys, [])
        sender2 = FakeSender()
        worker2 = worker_with(
            replace(StatusConfig(), buff1_key="", buff2_key="-",
                    buff1_enabled=True, buff2_enabled=True),
            sender2,
        )
        worker2._last_drug.update({"buff1": float("-inf"), "buff2": float("-inf")})
        worker2._check_drugs(time.monotonic())
        self.assertEqual(sender2.keys, [])

    def test_a_charge_defers_the_dose(self) -> None:
        sender = FakeSender()
        worker = worker_with(
            replace(StatusConfig(), buff1_key="home", buff1_interval=60.0,
                    buff1_enabled=True),
            sender,
        )
        worker.charge_active_callback = lambda: True
        worker._last_drug["buff1"] = time.monotonic() - 61.0
        worker._check_drugs(time.monotonic())
        self.assertEqual(sender.keys, [])
        # The timer stays due, so the dose lands as soon as the charge is over.
        worker.charge_active_callback = lambda: False
        worker._check_drugs(time.monotonic())
        self.assertEqual(sender.keys, ["home"])

    def test_a_refused_press_keeps_the_timer_due(self) -> None:
        sender = FakeSender(ok=False)
        worker = worker_with(
            replace(StatusConfig(), buff1_key="home", buff1_interval=60.0,
                    buff1_enabled=True),
            sender,
        )
        worker._last_drug["buff1"] = time.monotonic() - 61.0
        worker._check_drugs(time.monotonic())
        self.assertEqual(len(sender.keys), 1)
        self.assertLess(worker._last_drug["buff1"], time.monotonic() - 60.0)
        sender.ok = True
        worker._check_drugs(time.monotonic())
        self.assertEqual(sender.keys, ["home", "home"])

    def test_flower_point_requests_the_aux_drug(self) -> None:
        sender = FakeSender()
        config = replace(
            StatusConfig(),
            buff2_key="insert", buff2_interval=600.0, buff2_enabled=True,
        )
        worker = worker_with(config, sender)
        gate = {"open": True}
        completed: list[bool] = []
        worker.aux_drug_flower_gate_callback = lambda: gate["open"]
        worker.aux_drug_flower_completed_callback = lambda: completed.append(True)

        self.assertTrue(worker.request_aux_drug_at_flower_point())
        worker._check_drugs(time.monotonic())
        self.assertEqual(sender.keys, ["insert"])
        self.assertEqual(completed, [True])

        # Gate closed: the ordinary timed use still happens (one tap), but it is
        # not reported as a flower cycle.
        gate["open"] = False
        worker._last_drug["buff2"] = time.monotonic() - 601.0
        worker._check_drugs(time.monotonic())
        self.assertEqual(sender.keys, ["insert", "insert"])
        self.assertEqual(completed, [True])

        # Not configured: the route must not rely on it.
        idle = worker_with(StatusConfig(), FakeSender())
        self.assertFalse(idle.request_aux_drug_at_flower_point())

    def test_no_tap_while_automation_is_inactive(self) -> None:
        sender = FakeSender()
        worker = worker_with(
            replace(StatusConfig(), buff1_key="home", buff1_interval=60.0,
                    buff1_enabled=True),
            sender,
            automation=False,
        )
        worker.tick_seconds = 0.02
        worker._last_drug["buff1"] = time.monotonic() - 61.0
        worker.start()
        try:
            time.sleep(0.15)
        finally:
            worker.stop_event.set()
            worker.join(timeout=2.0)
        self.assertEqual(sender.keys, [])

    def test_the_thread_uses_the_dose_once_per_interval(self) -> None:
        sender = FakeSender()
        worker = worker_with(
            replace(StatusConfig(), buff1_key="home", buff1_interval=0.05,
                    buff1_enabled=True),
            sender,
        )
        worker.tick_seconds = 0.02
        worker._last_drug["buff1"] = time.monotonic() - 1.0
        worker.start()
        try:
            deadline = time.monotonic() + 0.4
            while time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            worker.stop_event.set()
            worker.join(timeout=2.0)
        # Every press is one tap, so an odd/even count cannot hide a double tap.
        self.assertGreaterEqual(len(sender.keys), 2)
        self.assertTrue(all(key == "home" for key in sender.keys))
        self.assertTrue(all(
            hold == TIME_DRUG_TAP_HOLD_SECONDS for hold in sender.holds
        ))


if __name__ == "__main__":
    unittest.main()
