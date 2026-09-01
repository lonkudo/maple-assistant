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


if __name__ == "__main__":
    unittest.main()
