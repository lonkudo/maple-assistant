import threading
import time
import unittest
from unittest import mock

from shutdown_worker import ShutdownWorker


class FakeSender:
    """dry_run=False sender; game window state is a settable flag."""

    def __init__(self, window_open: bool = True):
        self.dry_run = False
        self.events = []
        self.window_open = window_open
        self.selected = 0

    def select_window(self) -> bool:
        self.selected += 1
        return True

    def key_down(self, key):
        self.events.append(("down", key))
        return True

    def key_up(self, key):
        self.events.append(("up", key))
        return True

    def _find_target_window(self) -> int:
        if not self.window_open:
            raise OSError("window gone")
        return 12345


class ShutdownWorkerTests(unittest.TestCase):
    def test_disabled_worker_never_fires(self):
        sender = FakeSender()
        stop = threading.Event()
        worker = ShutdownWorker(sender, stop, enabled=False, hours=0.05,
                                poll_interval=0.1)
        worker.start()
        time.sleep(0.35)
        self.assertFalse(stop.is_set())
        self.assertEqual(sender.events, [])
        stop.set()
        worker.join(1)

    def test_deadline_presses_alt_f4_chord_then_stops_everything(self):
        with mock.patch("shutdown_worker.SECONDS_PER_HOUR", 1.0):
            sender = FakeSender(window_open=False)  # Alt+F4 closes the game
            stop = threading.Event()
            worker = ShutdownWorker(sender, stop, enabled=True, hours=0.05,
                                    poll_interval=0.1, chord_hold=0.01,
                                    close_grace=0.1)
            worker.start()
            time.sleep(0.35)
            worker.join(1)
        # Alt+F4 sent as a chord: alt down, f4 down, f4 up, alt up.
        self.assertEqual(sender.events, [
            ("down", "alt"), ("down", "f4"),
            ("up", "f4"), ("up", "alt"),
        ])
        self.assertTrue(sender.selected >= 1)  # game foregrounded first
        self.assertTrue(stop.is_set())  # every worker told to stop

    def test_game_not_closed_retries_then_arms_again_without_stopping(self):
        with mock.patch("shutdown_worker.SECONDS_PER_HOUR", 1.0):
            sender = FakeSender(window_open=True)  # Alt+F4 does not close it
            stop = threading.Event()
            worker = ShutdownWorker(sender, stop, enabled=True, hours=0.05,
                                    poll_interval=0.1, chord_hold=0.01,
                                    close_grace=0.1, max_attempts=2)
            worker.start()
            time.sleep(0.9)
            worker.enabled = False  # stop the retry loop
            worker.join(1)
        # Retried (>= max_attempts chords), gave up and re-armed - it never
        # stops the workers while the game stays open.  (Disabling the
        # feature afterwards clears the deadline, so only the chord count
        # and the stop flag are asserted.)
        chords = [e for e in sender.events if e == ("down", "f4")]
        self.assertGreaterEqual(len(chords), 2)
        self.assertFalse(stop.is_set())

    def test_dry_run_sends_no_keys_and_treats_game_as_closed(self):
        with mock.patch("shutdown_worker.SECONDS_PER_HOUR", 1.0):
            sender = FakeSender(window_open=True)
            sender.dry_run = True
            stop = threading.Event()
            worker = ShutdownWorker(sender, stop, enabled=True, hours=0.05,
                                    poll_interval=0.1, close_grace=0.1)
            worker.start()
            time.sleep(0.35)
            worker.join(1)
        self.assertEqual(sender.events, [])
        self.assertTrue(stop.is_set())

    def test_set_hours_rearms_deadline(self):
        sender = FakeSender()
        worker = ShutdownWorker(sender, threading.Event(), enabled=True,
                                hours=3.0)
        worker._deadline = time.monotonic() + 100.0
        worker.set_hours(5.0)
        self.assertEqual(worker.hours, 5.0)
        self.assertIsNone(worker._deadline)


if __name__ == "__main__":
    unittest.main()
