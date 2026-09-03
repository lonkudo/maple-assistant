import threading
import time
import unittest

from motion_arbiter import MotionArbiter, JUMP


class FakeSender:
    """Records taps; returns ``ok`` from tap() to simulate input gating."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.taps: list[str] = []

    def tap(self, key: str) -> bool:
        self.taps.append(key)
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
        climbing: threading.Event = None,
    ) -> MotionArbiter:
        arbiter = MotionArbiter(
            sender, stop,
            climbing_active_event=climbing,
            jump_motion_seconds=jump,
            buff_motion_seconds=buff,
            attack_grace_seconds=grace,
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
        arbiter = self._arbiter(
            sender, stop, jump=10.0, buff=10.0, grace=0.0
        )
        started = time.monotonic()
        self.assertTrue(arbiter.request_buff("home"))
        # The tap was attempted but refused; no 10s window may be applied.
        self.assertTrue(wait_until(lambda: arbiter.is_idle(), timeout=1.0))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(sender.taps, ["home"])

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


if __name__ == "__main__":
    unittest.main()
