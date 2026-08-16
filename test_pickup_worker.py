import threading
import time
import unittest

from pickup_worker import PickupWorker


class FakeSender:
    def __init__(self):
        self.events = []
        self.held = set()

    def key_down(self, key):
        self.events.append(("down", key))
        self.held.add(key)
        return True

    def key_up(self, key):
        self.events.append(("up", key))
        self.held.discard(key)
        return True

    def tap(self, key):
        self.events.append(("tap", key))
        return True


class PickupWorkerTests(unittest.TestCase):
    def _worker(self, **kwargs):
        stop = kwargs.pop("stop", None) or threading.Event()
        return PickupWorker(FakeSender(), stop, 0.1, **kwargs)

    def test_holds_z_while_moving_releases_when_stopped(self):
        sender = FakeSender()
        stop = threading.Event()
        automation = threading.Event()
        automation.set()
        moving = threading.Event()
        worker = PickupWorker(sender, stop, 0.1,
                              automation_active_event=automation,
                              moving_active_event=moving,
                              poll_seconds=0.01)
        worker.start()
        time.sleep(0.05)
        # Not moving yet: no keys.
        self.assertEqual(sender.events, [])
        # Movement starts: Z is pressed and held (down only, no up yet).
        moving.set()
        time.sleep(0.05)
        self.assertIn(("down", "z"), sender.events)
        self.assertNotIn(("up", "z"), sender.events)
        self.assertIn("z", sender.held)
        # Movement stops: Z is released.
        moving.clear()
        time.sleep(0.05)
        self.assertIn(("up", "z"), sender.events)
        self.assertNotIn("z", sender.held)
        stop.set()
        worker.join(1)

    def test_no_repeat_taps_while_holding(self):
        sender = FakeSender()
        stop = threading.Event()
        automation = threading.Event()
        automation.set()
        moving = threading.Event()
        worker = PickupWorker(sender, stop, 0.1,
                              automation_active_event=automation,
                              moving_active_event=moving,
                              poll_seconds=0.01)
        worker.start()
        moving.set()
        time.sleep(0.10)
        # Only ONE key-down; holding is continuous, never tapped repeatedly.
        downs = [e for e in sender.events if e == ("down", "z")]
        self.assertEqual(len(downs), 1)
        stop.set()
        worker.join(1)

    def test_climbing_blocks_pickup(self):
        sender = FakeSender()
        stop = threading.Event()
        automation = threading.Event()
        automation.set()
        moving = threading.Event()
        moving.set()
        climbing = threading.Event()
        climbing.set()
        worker = PickupWorker(sender, stop, 0.1,
                              automation_active_event=automation,
                              moving_active_event=moving,
                              climbing_active_event=climbing,
                              poll_seconds=0.01)
        worker.start()
        time.sleep(0.05)
        self.assertEqual(sender.events, [])
        # Climb ends while still moving: Z engages.
        climbing.clear()
        time.sleep(0.05)
        self.assertIn(("down", "z"), sender.events)
        stop.set()
        worker.join(1)

    def test_automation_paused_releases_z(self):
        sender = FakeSender()
        stop = threading.Event()
        automation = threading.Event()
        automation.set()
        moving = threading.Event()
        moving.set()
        worker = PickupWorker(sender, stop, 0.1,
                              automation_active_event=automation,
                              moving_active_event=moving,
                              poll_seconds=0.01)
        worker.start()
        time.sleep(0.05)
        self.assertIn(("down", "z"), sender.events)
        # Patrol paused: Z released even though moving flag still set.
        automation.clear()
        time.sleep(0.05)
        self.assertIn(("up", "z"), sender.events)
        stop.set()
        worker.join(1)

    def test_stop_releases_held_z(self):
        sender = FakeSender()
        stop = threading.Event()
        automation = threading.Event()
        automation.set()
        moving = threading.Event()
        moving.set()
        worker = PickupWorker(sender, stop, 0.1,
                              automation_active_event=automation,
                              moving_active_event=moving,
                              poll_seconds=0.01)
        worker.start()
        time.sleep(0.05)
        self.assertIn(("down", "z"), sender.events)
        stop.set()
        worker.join(1)
        self.assertIn(("up", "z"), sender.events)
        self.assertNotIn("z", sender.held)


if __name__ == "__main__":
    unittest.main()
