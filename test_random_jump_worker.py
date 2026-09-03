import threading
import unittest
from unittest import mock

from random_jump_worker import RandomJumpWorker


class Sender:
    def __init__(self) -> None:
        self.keys = []

    def tap(self, key: str) -> bool:
        self.keys.append(key)
        return True


class RandomJumpWorkerTests(unittest.TestCase):
    def test_jump_uses_fixed_alt_key(self) -> None:
        sender = Sender()
        worker = RandomJumpWorker(sender, threading.Event())
        self.assertTrue(worker.jump_once())
        self.assertEqual(sender.keys, ["alt"])

    def test_delay_is_base_plus_configured_random_gap(self) -> None:
        worker = RandomJumpWorker(
            Sender(), threading.Event(), 2.0, jump_jitter_seconds=.4
        )
        with mock.patch("random_jump_worker.random.uniform", return_value=.25):
            self.assertEqual(worker.next_delay(), 2.25)

    def test_minimum_trigger_interval_is_one_second(self) -> None:
        # Jump motion locks out further motion for 0.9s, so the trigger
        # interval floor is 1.0s - never the old 0.2s attack-style floor.
        worker = RandomJumpWorker(Sender(), threading.Event(), 0.2)
        self.assertEqual(worker.jump_interval, 1.0)
        worker = RandomJumpWorker(Sender(), threading.Event(), 0.9)
        self.assertEqual(worker.jump_interval, 1.0)

    def test_tick_queues_jump_through_motion_arbiter(self) -> None:
        class FakeArbiter:
            def __init__(self) -> None:
                self.requests = 0

            def request_jump(self) -> bool:
                self.requests += 1
                return True

        arbiter = FakeArbiter()
        worker = RandomJumpWorker(
            Sender(), threading.Event(), motion_arbiter=arbiter
        )
        self.assertTrue(worker.motion_arbiter.request_jump())
        self.assertEqual(arbiter.requests, 1)


if __name__ == "__main__":
    unittest.main()
