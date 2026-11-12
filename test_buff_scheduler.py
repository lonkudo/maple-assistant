"""增益 1..4 schedule tests (the ``buff_scheduler`` worker).

These rows are casts, so each refresh is a DOUBLE tap and the delivery goes
through MotionArbiter when one is wired.  The two 时间药 rows (饲料 /
补药) are a different worker - see ``test_time_drug_worker`` - and must never be
touched from here.
"""

import threading
import time
import unittest
from dataclasses import replace

from buff_scheduler import BUFF_NAMES, BUFF_TAP_HOLD_SECONDS, BuffScheduler
from status_worker import StatusConfig


class FakeSender:
    """Records keys and the hold each tap asked for."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.holds: list[float | None] = []

    def tap(self, key: str, *, hold_seconds: float | None = None) -> bool:
        self.keys.append(key)
        self.holds.append(hold_seconds)
        return True


class DeferredBuffArbiter:
    """Records a buff request and completes it only when the test says so."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, object]] = []

    def request_buff(self, key, on_complete=None) -> bool:
        self.requests.append((key, on_complete))
        return True


def scheduler_with(config: StatusConfig, sender: FakeSender,
                   *, arbiter=None, automation: bool = True) -> BuffScheduler:
    event = threading.Event()
    if automation:
        event.set()
    return BuffScheduler(
        sender,
        threading.Event(),
        config_provider=lambda: config,
        automation_active_event=event,
        motion_arbiter=arbiter,
    )


class BuffScheduleTests(unittest.TestCase):
    def test_only_the_four_gain_rows_belong_to_this_worker(self) -> None:
        # buff1/buff2 are the 时间药 rows; they moved to TimeDrugWorker.
        self.assertEqual(BUFF_NAMES, ("buff3", "buff4", "buff5", "buff6"))

    def test_periodic_buff_taps_on_interval_timer(self) -> None:
        sender = FakeSender()
        config = replace(
            StatusConfig(),
            buff3_key="pageup", buff3_interval=60.0, buff3_enabled=True,
            buff4_key="pagedown", buff4_interval=60.0, buff4_enabled=True,
        )
        scheduler = scheduler_with(config, sender)
        # 增益不从开局立即触发（用户会先手动触发第一次）。
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, [])
        # 计时器到期后按一次（回拨时间戳模拟时间流逝）；每一行是双击。
        scheduler._last_buff["buff3"] = time.monotonic() - 61.0
        scheduler._last_buff["buff4"] = time.monotonic() - 61.0
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, ["pageup", "pageup", "pagedown", "pagedown"])
        # Interval not elapsed yet: no repeat.
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, ["pageup", "pageup", "pagedown", "pagedown"])

    def test_buff_is_a_double_tap_with_the_buff_hold(self) -> None:
        sender = FakeSender()
        config = replace(
            StatusConfig(),
            buff3_key="pageup", buff3_interval=60.0, buff3_enabled=True,
        )
        scheduler = scheduler_with(config, sender)
        scheduler._last_buff["buff3"] = time.monotonic() - 61.0
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, ["pageup", "pageup"])
        self.assertEqual(
            sender.holds, [BUFF_TAP_HOLD_SECONDS, BUFF_TAP_HOLD_SECONDS]
        )
        self.assertAlmostEqual(BUFF_TAP_HOLD_SECONDS, 0.15)

    def test_time_drug_rows_are_never_tapped_here(self) -> None:
        # 饲料 / 补药 belong to TimeDrugWorker; this worker must ignore
        # those storage rows even when they are due and enabled.
        sender = FakeSender()
        config = replace(
            StatusConfig(),
            buff1_key="home", buff1_interval=60.0, buff1_enabled=True,
            buff2_key="insert", buff2_interval=60.0, buff2_enabled=True,
        )
        scheduler = scheduler_with(config, sender)
        scheduler._last_buff.update({"buff1": float("-inf"), "buff2": float("-inf")})
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, [])

    def test_queueing_keeps_the_timer_due_until_the_arbiter_completes(self) -> None:
        sender = FakeSender()
        arbiter = DeferredBuffArbiter()
        config = replace(
            StatusConfig(),
            buff3_key="pageup", buff3_interval=60.0, buff3_enabled=True,
        )
        scheduler = scheduler_with(config, sender, arbiter=arbiter)
        original = time.monotonic() - 61.0
        scheduler._last_buff["buff3"] = original
        scheduler._check_buffs(time.monotonic())
        self.assertEqual([key for key, _ in arbiter.requests], ["pageup"])
        # A queued request does not reset the deadline or pile up a duplicate.
        self.assertEqual(scheduler._last_buff["buff3"], original)
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(len(arbiter.requests), 1)
        arbiter.requests[0][1](True)
        self.assertGreater(scheduler._last_buff["buff3"], original)
        self.assertFalse(scheduler._buff_pending["buff3"])

    def test_disabled_or_unbound_buff_never_taps(self) -> None:
        sender = FakeSender()
        scheduler = scheduler_with(StatusConfig(), sender)   # all rows disabled
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, [])
        # Enabled but unbound ("-"/empty) keys still never fire.
        sender2 = FakeSender()
        scheduler2 = scheduler_with(
            replace(StatusConfig(), buff3_key="", buff4_key="-",
                    buff3_enabled=True, buff4_enabled=True),
            sender2,
        )
        scheduler2._last_buff.update({"buff3": float("-inf"), "buff4": float("-inf")})
        scheduler2._check_buffs(time.monotonic())
        self.assertEqual(sender2.keys, [])

    def test_schedule_needs_no_status_frame_and_no_hp_or_mp(self) -> None:
        # The worker owns its own timer: nothing in this path reads a bar.
        sender = FakeSender()
        scheduler = scheduler_with(
            replace(StatusConfig(), buff3_key="pageup", buff3_interval=0.05,
                    buff3_enabled=True),
            sender,
        )
        scheduler.tick_seconds = 0.02
        scheduler._last_buff["buff3"] = time.monotonic() - 1.0
        scheduler.start()
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not sender.keys:
                time.sleep(0.02)
        finally:
            scheduler.stop_event.set()
            scheduler.join(timeout=2.0)
        self.assertEqual(sender.keys[:2], ["pageup", "pageup"])

    def test_no_tap_while_automation_is_inactive(self) -> None:
        sender = FakeSender()
        scheduler = scheduler_with(
            replace(StatusConfig(), buff3_key="pageup", buff3_interval=60.0,
                    buff3_enabled=True),
            sender,
            automation=False,
        )
        scheduler.tick_seconds = 0.02
        scheduler._last_buff["buff3"] = time.monotonic() - 61.0
        scheduler.start()
        try:
            time.sleep(0.15)
        finally:
            scheduler.stop_event.set()
            scheduler.join(timeout=2.0)
        self.assertEqual(sender.keys, [])

    def test_charge_defers_a_direct_tap(self) -> None:
        sender = FakeSender()
        scheduler = scheduler_with(
            replace(StatusConfig(), buff3_key="pageup", buff3_interval=60.0,
                    buff3_enabled=True),
            sender,
        )
        scheduler.charge_active_callback = lambda: True
        scheduler._last_buff["buff3"] = time.monotonic() - 61.0
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, [])

    def test_fourth_gain_row_is_scheduled(self) -> None:
        # 增益 4 (storage row buff6) is an ordinary periodic buff.
        sender = FakeSender()
        scheduler = scheduler_with(
            replace(StatusConfig(), buff6_key="m", buff6_interval=45.0,
                    buff6_enabled=True),
            sender,
        )
        self.assertIn("buff6", scheduler._last_buff)
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, [])          # not due yet
        scheduler._last_buff["buff6"] = time.monotonic() - 46.0
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, ["m", "m"])
        self.assertEqual(
            sender.holds, [BUFF_TAP_HOLD_SECONDS, BUFF_TAP_HOLD_SECONDS]
        )
        # And the shipped default keeps it unbound/disabled like the others.
        self.assertEqual(StatusConfig().buff6_key, "-")
        self.assertFalse(StatusConfig().buff6_enabled)


if __name__ == "__main__":
    unittest.main()
