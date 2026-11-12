"""增益 schedule tests: the buff timers, now independent of the HP/MP worker.

These were ``test_status_worker`` cases while the schedule lived inside the HUD
worker.  They now target ``buff_scheduler`` and, more importantly, prove the
decoupling: a buff fires with no status frame at all, and its hold time is the
bound 200 ms rather than the 45 ms action fallback.
"""

import threading
import time
import unittest
from dataclasses import replace

from buff_scheduler import BUFF_TAP_HOLD_SECONDS, BuffScheduler
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
    scheduler = BuffScheduler(
        sender,
        threading.Event(),
        config_provider=lambda: config,
        automation_active_event=event,
        motion_arbiter=arbiter,
    )
    return scheduler


class BuffScheduleTests(unittest.TestCase):
    def test_periodic_buff_taps_on_interval_timer(self) -> None:
        sender = FakeSender()
        config = replace(
            StatusConfig(),
            buff1_key="home", buff1_interval=60.0, buff1_enabled=True,
            buff2_key="insert", buff2_interval=60.0, buff2_enabled=True,
        )
        scheduler = scheduler_with(config, sender)
        # 增益不从开局立即触发（用户会先手动触发第一次）。
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, [])
        # 计时器到期后按一次（回拨时间戳模拟时间流逝）。
        scheduler._last_buff["buff1"] = time.monotonic() - 61.0
        scheduler._last_buff["buff2"] = time.monotonic() - 61.0
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, ["home", "insert"])
        # Interval not elapsed yet: no repeat.
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, ["home", "insert"])
        # Backdate both timers again: both refresh.
        scheduler._last_buff["buff1"] = time.monotonic() - 61.0
        scheduler._last_buff["buff2"] = time.monotonic() - 61.0
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, ["home", "insert", "home", "insert"])

    def test_buff_tap_uses_the_buff_hold_not_the_action_hold(self) -> None:
        # A 45 ms action tap is dropped by the client; periodic buffs must ask for
        # the longer buff hold.
        sender = FakeSender()
        config = replace(
            StatusConfig(),
            buff1_key="home", buff1_interval=60.0, buff1_enabled=True,
        )
        scheduler = scheduler_with(config, sender)
        scheduler._last_buff["buff1"] = time.monotonic() - 61.0
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, ["home"])
        self.assertEqual(sender.holds, [BUFF_TAP_HOLD_SECONDS])
        self.assertAlmostEqual(BUFF_TAP_HOLD_SECONDS, 0.2)

    def test_queueing_keeps_the_timer_due_until_the_arbiter_completes(self) -> None:
        sender = FakeSender()
        arbiter = DeferredBuffArbiter()
        config = replace(
            StatusConfig(),
            buff1_enabled=False,
            buff2_key="insert", buff2_interval=60.0, buff2_enabled=True,
        )
        scheduler = scheduler_with(config, sender, arbiter=arbiter)
        original = time.monotonic() - 61.0
        scheduler._last_buff["buff2"] = original
        scheduler._check_buffs(time.monotonic())
        self.assertEqual([key for key, _ in arbiter.requests], ["insert"])
        # A queued request does not reset the deadline or pile up a duplicate.
        self.assertEqual(scheduler._last_buff["buff2"], original)
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(len(arbiter.requests), 1)
        arbiter.requests[0][1](True)
        self.assertGreater(scheduler._last_buff["buff2"], original)
        self.assertFalse(scheduler._buff_pending["buff2"])

    def test_pet_food_is_direct_periodic_drug_not_arbiter_motion(self) -> None:
        sender = FakeSender()
        arbiter = DeferredBuffArbiter()
        config = replace(
            StatusConfig(),
            buff1_key="home", buff1_interval=60.0, buff1_enabled=True,
            buff2_enabled=False, buff3_enabled=False,
        )
        scheduler = scheduler_with(config, sender, arbiter=arbiter)
        scheduler._last_buff["buff1"] = time.monotonic() - 61.0
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, ["home"])
        self.assertEqual(arbiter.requests, [])

    def test_disabled_or_unbound_buff_never_taps(self) -> None:
        sender = FakeSender()
        config = StatusConfig()          # every row disabled by default
        scheduler = scheduler_with(config, sender)
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, [])
        # Enabled but unbound ("-"/empty) keys still never fire.
        sender2 = FakeSender()
        scheduler2 = scheduler_with(
            replace(StatusConfig(), buff1_key="", buff2_key="-",
                    buff1_enabled=True, buff2_enabled=True),
            sender2,
        )
        scheduler2._last_buff["buff1"] = time.monotonic() - 10_000.0
        scheduler2._last_buff["buff2"] = time.monotonic() - 10_000.0
        scheduler2._check_buffs(time.monotonic())
        self.assertEqual(sender2.keys, [])

    def test_flower_point_request_makes_buff2_due(self) -> None:
        sender = FakeSender()
        config = replace(
            StatusConfig(),
            buff2_key="insert", buff2_interval=600.0, buff2_enabled=True,
        )
        scheduler = scheduler_with(config, sender)
        scheduler._last_buff["buff2"] = time.monotonic()
        self.assertTrue(scheduler.request_buff2_at_flower_point())
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, ["insert"])
        # Not configured -> the route must not rely on it.
        idle = BuffScheduler(
            FakeSender(), threading.Event(),
            config_provider=lambda: replace(StatusConfig(), buff2_enabled=False),
        )
        self.assertFalse(idle.request_buff2_at_flower_point())

    def test_schedule_needs_no_status_frame_and_no_hp_or_mp(self) -> None:
        # The whole point of the split: nothing about HP/MP can stop a buff.
        sender = FakeSender()
        config = replace(
            StatusConfig(),
            buff1_key="home", buff1_interval=0.05, buff1_enabled=True,
            buff2_enabled=False, buff3_enabled=False, buff4_enabled=False,
            buff5_enabled=False,
        )
        scheduler = scheduler_with(config, sender)
        scheduler._last_buff["buff1"] = time.monotonic() - 1.0
        scheduler.start()
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not sender.keys:
                time.sleep(0.02)
        finally:
            scheduler.stop_event.set()
            scheduler.join(1.0)
        self.assertEqual(sender.keys[:1], ["home"])

    def test_no_tap_while_automation_is_inactive(self) -> None:
        sender = FakeSender()
        config = replace(
            StatusConfig(),
            buff1_key="home", buff1_interval=60.0, buff1_enabled=True,
        )
        scheduler = scheduler_with(config, sender, automation=False)
        scheduler._last_buff["buff1"] = time.monotonic() - 61.0
        scheduler.start()
        time.sleep(0.25)
        scheduler.stop_event.set()
        scheduler.join(1.0)
        self.assertEqual(sender.keys, [])

    def test_charge_defers_pet_food(self) -> None:
        sender = FakeSender()
        config = replace(
            StatusConfig(),
            buff1_key="home", buff1_interval=60.0, buff1_enabled=True,
        )
        scheduler = scheduler_with(config, sender)
        scheduler.charge_active_callback = lambda: True
        scheduler._last_buff["buff1"] = time.monotonic() - 61.0
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, [])

    def test_fifth_buff_row_is_scheduled(self) -> None:
        # 增益5 (the sixth drug row, ``buff6``) is an ordinary periodic buff: it
        # fires on its timer and is a default-unbound row like 增益4.
        sender = FakeSender()
        config = replace(
            StatusConfig(),
            buff6_key="m", buff6_interval=45.0, buff6_enabled=True,
        )
        scheduler = scheduler_with(config, sender)
        self.assertIn("buff6", scheduler._last_buff)
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, [])          # not due yet
        scheduler._last_buff["buff6"] = time.monotonic() - 46.0
        scheduler._check_buffs(time.monotonic())
        self.assertEqual(sender.keys, ["m"])
        self.assertEqual(sender.holds, [BUFF_TAP_HOLD_SECONDS])
        # And the shipped default keeps it unbound/disabled like the others.
        self.assertEqual(StatusConfig().buff6_key, "-")
        self.assertFalse(StatusConfig().buff6_enabled)


if __name__ == "__main__":
    unittest.main()
