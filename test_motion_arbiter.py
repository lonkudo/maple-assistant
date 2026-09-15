import threading
import time
import unittest

from motion_arbiter import MICRO_STEP, MotionArbiter, JUMP


class FakeSender:
    """Records taps; returns ``ok`` from tap() to simulate input gating.

    ``failures`` makes the first N taps fail, which is what a focus dip looks
    like: the key is "sent" but the game never receives it.
    """

    def __init__(self, ok: bool = True, failures: int = 0) -> None:
        self.ok = ok
        self.failures = int(failures)
        self.taps: list[str] = []

    def tap(self, key: str) -> bool:
        self.taps.append(key)
        if self.failures > 0:
            self.failures -= 1
            return False
        return self.ok


def wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class MotionArbiterTests(unittest.TestCase):
    def _arbiter(
        self, sender: FakeSender, stop: threading.Event,
        *, jump: float = 0.05, buff: float = 0.04, grace: float = 0.0,
        climbing: threading.Event = None, delivery_retry: float = 0.25,
    ) -> MotionArbiter:
        arbiter = MotionArbiter(
            sender, stop,
            climbing_active_event=climbing,
            jump_motion_seconds=jump,
            buff_motion_seconds=buff,
            attack_grace_seconds=grace,
            delivery_retry_seconds=delivery_retry,
        )
        arbiter.start()
        self.addCleanup(stop.set)
        return arbiter

    def test_jump_and_buff_execute_in_fifo_order(self) -> None:
        sender = FakeSender()
        arbiter = self._arbiter(sender, threading.Event())
        self.assertTrue(arbiter.request_buff("home"))
        self.assertTrue(arbiter.request_jump())
        self.assertTrue(wait_until(lambda: len(sender.taps) >= 2))
        self.assertEqual(sender.taps, ["home", "alt"])

    def test_duplicate_requests_collapse_while_pending(self) -> None:
        # Without an executor thread the queue is pure: two jump requests
        # while the first is still pending must stay ONE event, and the same
        # per buff key.  This is the guard that prevents a backlog when a
        # timer tick arrives while an earlier event is still waiting.
        sender = FakeSender()
        stop = threading.Event()
        arbiter = MotionArbiter(sender, stop)  # deliberately not started
        self.assertTrue(arbiter.request_jump())
        self.assertTrue(arbiter.request_jump())
        self.assertTrue(arbiter.request_jump())
        self.assertTrue(arbiter.request_buff("home"))
        self.assertTrue(arbiter.request_buff("home"))
        self.assertTrue(arbiter.request_buff("insert"))
        with arbiter._cv:
            self.assertEqual(
                list(arbiter._pending), [JUMP, "buff:home", "buff:insert"]
            )
            self.assertEqual(
                arbiter._queued, {JUMP, "buff:home", "buff:insert"}
            )
        stop.set()
        self.assertFalse(arbiter.request_jump())

    def test_attack_is_suppressed_while_events_queued_or_busy(self) -> None:
        sender = FakeSender()
        stop = threading.Event()
        arbiter = self._arbiter(
            sender, stop, jump=0.3, buff=0.2, grace=0.0
        )
        # Idle before any request.
        self.assertTrue(arbiter.is_idle())
        self.assertTrue(arbiter.request_jump())
        # Pending/executing jump keeps the arbiter busy -> attack deferred.
        self.assertFalse(arbiter.is_idle())
        self.assertTrue(wait_until(lambda: arbiter.is_idle(), timeout=2.0))
        self.assertTrue(arbiter.is_idle())

    def test_jump_is_dropped_while_climbing_input_is_active(self) -> None:
        sender = FakeSender()
        climbing = threading.Event()
        climbing.set()
        arbiter = self._arbiter(
            sender, threading.Event(), climbing=climbing
        )
        self.assertTrue(arbiter.request_jump())
        # The executor must consume the jump without tapping Alt (movement
        # owns it during climb) and return to idle.
        self.assertTrue(wait_until(lambda: arbiter.is_idle()))
        self.assertEqual(sender.taps, [])

    def test_blocked_tap_drains_without_a_motion_window(self) -> None:
        sender = FakeSender(ok=False)
        stop = threading.Event()
        # The retry window must outlast one retry delay (0.15s): at 0.15s
        # exactly, "was the tap retried?" was decided by sub-millisecond
        # timing, so this test failed on roughly half of all loaded runs.
        arbiter = self._arbiter(
            sender, stop, jump=10.0, buff=10.0, grace=0.0,
            delivery_retry=0.5,
        )
        completed: list[bool] = []
        self.assertTrue(arbiter.request_buff("home", completed.append))
        # A refused tap never applies its 10s motion window...
        self.assertFalse(arbiter.is_idle())
        # ... but the buff is retried (its timer will not ask again for
        # minutes) and only drained once the bounded window is over.
        self.assertTrue(
            wait_until(lambda: len(sender.taps) > 1, timeout=3.0),
            f"the refused buff was not retried: {sender.taps}",
        )
        # The completion callback runs on the arbiter's own thread just after
        # the queue is drained, so poll for the callback itself: is_idle() can
        # already be true while the notification has not fired yet.
        self.assertTrue(
            wait_until(lambda: completed == [False], timeout=3.0),
            f"the buff completion was never reported: completed={completed}",
        )
        self.assertTrue(wait_until(lambda: arbiter.is_idle(), timeout=3.0))
        self.assertEqual(set(sender.taps), {"home"})

    def test_buff_lost_to_a_focus_dip_is_retried_and_delivered(self) -> None:
        # The tap fails twice (focus stolen) and then succeeds: the buff must
        # still fire instead of being lost for its whole refresh interval.
        sender = FakeSender(failures=2)
        stop = threading.Event()
        arbiter = self._arbiter(sender, stop, grace=0.0, delivery_retry=2.0)
        completed: list[bool] = []
        self.assertTrue(arbiter.request_buff("home", completed.append))
        self.assertTrue(wait_until(lambda: completed == [True], timeout=3.0))
        self.assertEqual(len(sender.taps), 3)
        self.assertTrue(wait_until(lambda: arbiter.is_idle(), timeout=2.0))

    def test_failed_jump_is_not_kept_queued(self) -> None:
        # A jump is stale by the time focus returns: it is retried only through
        # the input layer's own attempts, then drained.
        sender = FakeSender(ok=False)
        stop = threading.Event()
        arbiter = self._arbiter(sender, stop, grace=0.0)
        self.assertTrue(arbiter.request_jump())
        self.assertTrue(wait_until(lambda: arbiter.is_idle(), timeout=2.0))
        self.assertEqual(sender.taps, ["alt"])

    def test_a_buff_waiting_for_the_safe_stage_says_so(self) -> None:
        # The gate shuts whenever the character is not making a walk decision;
        # a buff that was due minutes ago must not wait in total silence.
        sender = FakeSender()
        stop = threading.Event()
        allowed = [False]
        arbiter = self._arbiter(sender, stop, grace=0.0)
        arbiter.gate_wait_warn_seconds = 0.05
        arbiter.set_motion_gate_callback(lambda: allowed[0])
        with self.assertLogs("motion_arbiter", level="WARNING") as captured:
            self.assertTrue(arbiter.request_buff("home"))
            self.assertTrue(wait_until(
                lambda: any("waiting" in line for line in captured.output),
                timeout=2.0,
            ))
        self.assertEqual(sender.taps, [])
        allowed[0] = True
        self.assertTrue(
            wait_until(lambda: sender.taps == ["home"], timeout=2.0)
        )

    def test_refusal_reason_is_reported_for_the_caller_logs(self) -> None:
        sender = FakeSender()
        stop = threading.Event()
        arbiter = self._arbiter(sender, stop)
        arbiter.set_motion_gate_callback(lambda: False)
        self.assertFalse(arbiter.request_jump())
        self.assertIn("walkable stage", arbiter.last_refusal())
        stop.set()
        self.assertFalse(arbiter.request_micro_step())
        self.assertIn("automation inactive", arbiter.last_refusal())

    def test_requests_after_stop_are_rejected(self) -> None:
        sender = FakeSender()
        stop = threading.Event()
        arbiter = MotionArbiter(
            sender, stop,
            jump_motion_seconds=0.01, buff_motion_seconds=0.01,
        )
        stop.set()
        self.assertFalse(arbiter.request_jump())
        self.assertFalse(arbiter.request_buff("home"))
        self.assertTrue(arbiter.is_idle())

    def test_micro_step_uses_movement_callback(self) -> None:
        sender = FakeSender()
        calls = []
        arbiter = self._arbiter(sender, threading.Event(), grace=0.0)
        arbiter.set_micro_step_callback(lambda: calls.append("step") or True)
        self.assertTrue(arbiter.request_micro_step())
        self.assertTrue(wait_until(lambda: arbiter.is_idle()))
        self.assertEqual(calls, ["step"])
        self.assertEqual(sender.taps, [])
        self.assertNotIn(MICRO_STEP, arbiter._queued)

    def test_attack_reservation_blocks_micro_step_until_attack_finishes(self) -> None:
        sender = FakeSender()
        stop = threading.Event()
        calls = []
        arbiter = MotionArbiter(
            sender, stop, attack_grace_seconds=0.0,
            micro_step_motion_seconds=0.0,
        )
        arbiter.set_micro_step_callback(lambda: calls.append("step") or True)
        self.assertTrue(arbiter.try_begin_attack())
        self.assertTrue(arbiter.request_micro_step())
        arbiter.start()
        self.addCleanup(stop.set)
        time.sleep(0.05)
        # The queued callback must not begin in the attack key window.
        self.assertEqual(calls, [])
        self.assertFalse(arbiter.is_idle())
        arbiter.finish_attack(True)
        self.assertTrue(wait_until(lambda: calls == ["step"]))
        self.assertTrue(wait_until(arbiter.is_idle))

    def test_requests_are_rejected_when_patrol_is_not_active(self) -> None:
        sender = FakeSender()
        stop = threading.Event()
        active = threading.Event()
        arbiter = MotionArbiter(
            sender, stop, automation_active_event=active,
        )
        self.assertFalse(arbiter.request_jump())
        active.set()
        self.assertTrue(arbiter.request_jump())
        active.clear()
        # An already queued input is removed rather than sent after Stop.
        arbiter.start()
        self.addCleanup(stop.set)
        self.assertTrue(wait_until(arbiter.is_idle))
        self.assertEqual(sender.taps, [])

    def test_buff_waits_for_safe_horizontal_stage_before_executing(self) -> None:
        sender = FakeSender()
        stop = threading.Event()
        allowed = [False]
        arbiter = MotionArbiter(sender, stop)
        arbiter.set_motion_gate_callback(lambda: allowed[0])
        self.assertFalse(arbiter.request_jump())
        # Buffs register at timer expiry but stay pending until movement is in
        # patrol/rope horizontal travel.  They must not be dropped just
        # because the timer expired during a climb or landing transition.
        self.assertTrue(arbiter.request_buff("home"))
        self.assertFalse(arbiter.request_micro_step())
        allowed[0] = True
        self.assertTrue(arbiter.request_jump())

    def test_buff_completion_callback_runs_after_motion_window(self) -> None:
        sender = FakeSender()
        stop = threading.Event()
        allowed = [False]
        completed = []
        arbiter = self._arbiter(sender, stop, buff=0.06, grace=0.0)
        arbiter.set_motion_gate_callback(lambda: allowed[0])
        self.assertTrue(arbiter.request_buff("home", completed.append))
        time.sleep(0.03)
        self.assertEqual(sender.taps, [])
        self.assertEqual(completed, [])
        allowed[0] = True
        self.assertTrue(wait_until(lambda: sender.taps == ["home"]))
        # Completion is intentionally later than the key tap: the next
        # countdown begins only after the action window has finished.
        self.assertTrue(wait_until(lambda: completed == [True]))


if __name__ == "__main__":
    unittest.main()
