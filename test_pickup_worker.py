import threading
import time
import unittest

from pickup_worker import PickupWorker


class FakeSender:
    def __init__(self):
        self.events = []

    def key_down(self, key):
        self.events.append(("down", key))
        return True

    def key_up(self, key):
        self.events.append(("up", key))
        return True


class PickupWorkerTests(unittest.TestCase):
    def test_pickup_once_is_only_z_down_up(self):
        sender = FakeSender()
        worker = PickupWorker(sender, threading.Event())
        self.assertTrue(worker.pickup_once())
        self.assertEqual(sender.events, [("down", "z"), ("up", "z")])
        self.assertEqual(worker.pickup_count, 1)

    def test_timer_runs_without_frames(self):
        sender = FakeSender()
        stop = threading.Event()
        worker = PickupWorker(sender, stop, 0.25, initial_offset=0.25)
        worker.start()
        time.sleep(0.30)
        stop.set()
        worker.join(1)
        self.assertEqual(sender.events[:2], [("down", "z"), ("up", "z")])

    def test_climbing_blocks_pickup_then_pickup_resumes(self):
        sender = FakeSender()
        stop = threading.Event()
        climbing = threading.Event()
        climbing.set()
        worker = PickupWorker(sender, stop, 0.25,
                              climbing_active_event=climbing,
                              initial_offset=0.25)
        worker.start()
        time.sleep(0.30)
        self.assertEqual(sender.events, [])
        climbing.clear()
        time.sleep(0.30)
        stop.set()
        worker.join(1)
        self.assertGreaterEqual(len(sender.events), 2)
        self.assertEqual(sender.events[:2], [("down", "z"), ("up", "z")])

    def test_dropping_blocks_pickup(self):
        sender = FakeSender()
        stop = threading.Event()
        dropping = threading.Event()
        dropping.set()
        worker = PickupWorker(sender, stop, 0.25,
                              dropping_active_event=dropping,
                              initial_offset=0.25)
        worker.start()
        time.sleep(0.30)
        self.assertEqual(sender.events, [])
        stop.set()
        worker.join(1)

    def test_automation_paused_blocks_pickup(self):
        sender = FakeSender()
        stop = threading.Event()
        automation = threading.Event()  # not set: patrol paused
        moving = threading.Event()
        moving.set()
        worker = PickupWorker(sender, stop, 0.25,
                              automation_active_event=automation,
                              moving_active_event=moving,
                              initial_offset=0.25)
        worker.start()
        time.sleep(0.30)
        self.assertEqual(sender.events, [])
        stop.set()
        worker.join(1)

    def test_not_moving_blocks_pickup(self):
        sender = FakeSender()
        stop = threading.Event()
        automation = threading.Event()
        automation.set()
        moving = threading.Event()  # not set: character idle/aligned
        worker = PickupWorker(sender, stop, 0.25,
                              automation_active_event=automation,
                              moving_active_event=moving,
                              initial_offset=0.25)
        worker.start()
        time.sleep(0.30)
        self.assertEqual(sender.events, [])
        stop.set()
        worker.join(1)

    def test_moving_enables_pickup(self):
        sender = FakeSender()
        stop = threading.Event()
        automation = threading.Event()
        automation.set()
        moving = threading.Event()
        moving.set()  # character is walking left/right
        worker = PickupWorker(sender, stop, 0.25,
                              automation_active_event=automation,
                              moving_active_event=moving,
                              initial_offset=0.25)
        worker.start()
        time.sleep(0.30)
        stop.set()
        worker.join(1)
        self.assertGreaterEqual(len(sender.events), 2)
        self.assertEqual(sender.events[:2], [("down", "z"), ("up", "z")])

    def test_default_pickup_clock_is_half_interval_offset(self):
        worker = PickupWorker(FakeSender(), threading.Event(), 1.0)
        self.assertEqual(worker.initial_offset, 0.5)


if __name__ == "__main__":
    unittest.main()
