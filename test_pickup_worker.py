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

    def test_z_is_burst_pattern_hold_then_repeat(self):
        sender = FakeSender()
        stop = threading.Event()
        automation = threading.Event()
        automation.set()
        moving = threading.Event()
        worker = PickupWorker(sender, stop, 0.1,
                              automation_active_event=automation,
                              moving_active_event=moving,
                              pickup_gap_seconds=0.05,
                              poll_seconds=0.01)
        worker.start()
        moving.set()
        time.sleep(0.35)
        # Burst pattern: press Z, hold ~0.1s, release, then repeat quickly -
        # several down/up cycles while the route-walk flag stays set.
        downs = [e for e in sender.events if e == ("down", "z")]
        ups = [e for e in sender.events if e == ("up", "z")]
        self.assertGreaterEqual(len(downs), 2)
        self.assertGreaterEqual(len(ups), 1)
        # Z is never held across the whole window: an up sits between downs.
        first_down = sender.events.index(("down", "z"))
        first_up = sender.events.index(("up", "z"))
        self.assertGreater(first_up, first_down)
        stop.set()
        worker.join(1)

    def test_z_released_immediately_when_moving_stops_mid_hold(self):
        sender = FakeSender()
        stop = threading.Event()
        automation = threading.Event()
        automation.set()
        moving = threading.Event()
        worker = PickupWorker(sender, stop, 1.0,
                              automation_active_event=automation,
                              moving_active_event=moving,
                              poll_seconds=0.01)
        worker.start()
        moving.set()
        time.sleep(0.05)
        self.assertIn(("down", "z"), sender.events)
        # Phase ends mid-hold (e.g. the climb starts): Z must release
        # immediately, not wait for the 1s hold to elapse.
        moving.clear()
        time.sleep(0.05)
        self.assertIn(("up", "z"), sender.events)
        self.assertNotIn("z", sender.held)
        # And it must NOT re-press while blocked.
        time.sleep(0.15)
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

    def test_pickup_active_event_tracks_z_hold(self):
        sender = FakeSender()
        stop = threading.Event()
        automation = threading.Event()
        automation.set()
        moving = threading.Event()
        pickup_active = threading.Event()
        worker = PickupWorker(sender, stop, 0.1,
                              automation_active_event=automation,
                              moving_active_event=moving,
                              pickup_active_event=pickup_active,
                              poll_seconds=0.01)
        worker.start()
        # Event clears while Z is not held.
        self.assertFalse(pickup_active.is_set())
        moving.set()
        time.sleep(0.05)
        # Event set while Z is physically held (movement worker waits on it).
        self.assertTrue(pickup_active.is_set())
        moving.clear()
        time.sleep(0.05)
        self.assertFalse(pickup_active.is_set())
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
