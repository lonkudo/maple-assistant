"""The drug panel has exactly six timed rows; each has exactly one owner.

This is the guard against the split silently drifting: a row that both workers
would tap (two doses per refresh) or that neither would tap (a row that looks
configured and never fires) is a field bug nobody notices until a customer does.
"""

from __future__ import annotations

import threading
import time
import unittest
from dataclasses import replace

from buff_scheduler import BUFF_NAMES, BuffScheduler
from status_worker import StatusConfig
from time_drug_worker import TIME_DRUG_NAMES, TimeDrugWorker

# The panel order: 饲料, 补药, 增益 1, 增益 2, 增益 3, 增益 4.
PANEL_ROWS = ("buff1", "buff2", "buff3", "buff4", "buff5", "buff6")


class RecordingSender:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.holds: list[float | None] = []

    def tap(self, key: str, *, hold_seconds: float | None = None) -> bool:
        self.keys.append(key)
        self.holds.append(hold_seconds)
        return True


class RowOwnershipTests(unittest.TestCase):
    def test_the_two_workers_own_disjoint_rows(self) -> None:
        time_drugs = set(TIME_DRUG_NAMES)
        buffs = set(BUFF_NAMES)
        self.assertEqual(time_drugs & buffs, set(), "a row would be tapped twice")
        self.assertEqual(time_drugs | buffs, set(PANEL_ROWS), "a row would never fire")

    def test_every_row_has_its_settings_on_the_config(self) -> None:
        config = StatusConfig()
        for row in PANEL_ROWS:
            for suffix in ("key", "interval", "enabled"):
                self.assertTrue(
                    hasattr(config, f"{row}_{suffix}"), f"{row}_{suffix} is missing"
                )

    def test_the_time_drug_rows_are_exactly_pet_food_and_the_aux_drug(self) -> None:
        # Panel order is what the operator reads: 饲料 first, 补药 second.
        self.assertEqual(TIME_DRUG_NAMES, ("buff1", "buff2"))
        # And the four 增益 rows keep the remaining rows in order.
        self.assertEqual(BUFF_NAMES, ("buff3", "buff4", "buff5", "buff6"))


class SplitIntegrationTests(unittest.TestCase):
    """One configuration, both workers, one tick: the row split must hold."""

    def test_one_tick_taps_time_drugs_once_and_buffs_twice(self) -> None:
        config = replace(
            StatusConfig(),
            buff1_key="home", buff1_interval=60.0, buff1_enabled=True,
            buff2_key="insert", buff2_interval=60.0, buff2_enabled=True,
            buff3_key="pageup", buff3_interval=60.0, buff3_enabled=True,
            buff4_key="pagedown", buff4_interval=60.0, buff4_enabled=True,
        )
        sender = RecordingSender()
        event = threading.Event()
        event.set()
        drugs = TimeDrugWorker(
            sender, threading.Event(),
            config_provider=lambda: config, automation_active_event=event,
        )
        buffs = BuffScheduler(
            sender, threading.Event(),
            config_provider=lambda: config, automation_active_event=event,
        )
        # Make every row due (each worker keeps its own timer map).
        for row in TIME_DRUG_NAMES:
            drugs._last_drug[row] = float("-inf")
        for row in BUFF_NAMES:
            buffs._last_buff[row] = float("-inf")

        now = time.monotonic()
        drugs._check_drugs(now)
        buffs._check_buffs(now)

        # Time drugs: exactly one press each.
        self.assertEqual(sender.keys.count("home"), 1)
        self.assertEqual(sender.keys.count("insert"), 1)
        # 增益 rows: exactly two presses each.
        self.assertEqual(sender.keys.count("pageup"), 2)
        self.assertEqual(sender.keys.count("pagedown"), 2)
        self.assertEqual(len(sender.keys), 6)


    def test_the_flower_contract_between_the_workers_still_has_its_names(self) -> None:
        # assistant.main() wires these by name; a rename in either module would
        # only show up as an AttributeError on a 死花 run in the field.
        import movement_worker
        import time_drug_worker

        worker = time_drug_worker.TimeDrugWorker(
            RecordingSender(), threading.Event(),
            config_provider=lambda: StatusConfig(),
        )
        self.assertTrue(callable(worker.request_aux_drug_at_flower_point))
        for name in ("aux_drug_flower_gate_callback",
                     "aux_drug_flower_completed_callback"):
            self.assertTrue(hasattr(worker, name), name)
        for name in ("dead_flower_buff2_delivery_allowed",
                     "notify_dead_flower_buff2_delivered",
                     "set_dead_flower_buff2_request"):
            self.assertTrue(
                callable(getattr(movement_worker.MovementWorker, name, None)), name
            )


if __name__ == "__main__":
    unittest.main()
