import threading
import time
import unittest

from attack_worker import AttackWorker


class FakeSender:
    def __init__(self):
        self.events = []

    def key_down(self, key):
        self.events.append(("down", key))
        return True

    def key_up(self, key):
        self.events.append(("up", key))
        return True


class AttackWorkerTests(unittest.TestCase):
    def test_attack_once_is_only_ctrl_down_up(self):
        sender = FakeSender()
        worker = AttackWorker(sender, threading.Event())
        self.assertTrue(worker.attack_once())
        self.assertEqual(sender.events, [("down", "ctrl"), ("up", "ctrl")])

    def test_timer_runs_without_frames(self):
        sender = FakeSender()
        stop = threading.Event()
        worker = AttackWorker(sender, stop, .25, initial_offset=.25)
        worker.start()
        time.sleep(.30)
        stop.set()
        worker.join(1)
        self.assertEqual(sender.events[:2], [("down", "ctrl"), ("up", "ctrl")])

    def test_climbing_blocks_attack_then_attack_resumes(self):
        sender = FakeSender()
        stop = threading.Event()
        climbing = threading.Event()
        climbing.set()
        worker = AttackWorker(sender, stop, .25,
                              climbing_active_event=climbing,
                              initial_offset=.25)
        worker.start()
        time.sleep(.30)
        self.assertEqual(sender.events, [])
        climbing.clear()
        time.sleep(.30)
        stop.set()
        worker.join(1)
        self.assertGreaterEqual(len(sender.events), 2)
        self.assertEqual(sender.events[:2], [("down", "ctrl"), ("up", "ctrl")])

    def test_default_attack_clock_is_half_interval_offset(self):
        worker = AttackWorker(FakeSender(), threading.Event(), 3.0)
        self.assertEqual(worker.initial_offset, 1.5)


if __name__ == "__main__":
    unittest.main()
