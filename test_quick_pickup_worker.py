import queue
import threading
import time
import unittest

from quick_pickup_worker import QuickPickupWorker


class FakeSender:
    def __init__(self, *, selectable=True, foreground=True, sends=True):
        self.selectable = selectable
        self.foreground = foreground
        self.sends = sends
        self.sent = []

    def select_window(self):
        return self.selectable

    def is_game_foreground(self):
        return self.foreground

    def send_direct_keys(self, *keys):
        self.sent.extend(keys)
        return self.sends


class QuickPickupWorkerTests(unittest.TestCase):
    def _worker(self, sender, patrol_running=lambda: False):
        stop = threading.Event()
        results = queue.Queue()
        worker = QuickPickupWorker(
            sender, stop, results, patrol_running=patrol_running,
            interval_seconds=0.01,
        )
        return worker, stop, results

    def test_toggle_starts_and_sends_z_without_patrol_input_gate(self):
        sender = FakeSender()
        worker, stop, results = self._worker(sender)
        worker.start()
        self.assertTrue(worker.request_toggle())
        time.sleep(0.22)
        self.assertTrue(worker.is_active())
        self.assertEqual(results.get_nowait()[0], "started")
        self.assertIn("z", sender.sent)
        stop.set()
        worker.join(1)

    def test_patrol_running_rejects_start(self):
        sender = FakeSender()
        worker, stop, results = self._worker(sender, patrol_running=lambda: True)
        worker.start()
        self.addCleanup(worker.join, 1)
        self.addCleanup(stop.set)
        self.assertTrue(worker.request_toggle())
        # Wait for the worker's verdict instead of trusting one fixed sleep: a
        # busy machine (other suites' threads) could miss a 60 ms window and
        # turn this into a flaky failure.
        deadline = time.monotonic() + 2.0
        outcome = None
        while time.monotonic() < deadline:
            try:
                outcome = results.get_nowait()
                break
            except queue.Empty:
                time.sleep(0.005)
        self.assertIsNotNone(outcome, "the worker never reported a result")
        state, detail = outcome
        self.assertEqual(state, "failed")
        self.assertIn("patrol", detail)
        self.assertFalse(worker.is_active())
        self.assertEqual(sender.sent, [])
        worker.join(1)

    def test_failed_z_send_stops_worker(self):
        sender = FakeSender(sends=False)
        worker, stop, results = self._worker(sender)
        worker.start()
        self.assertTrue(worker.request_toggle())
        time.sleep(0.22)
        self.assertEqual(results.get_nowait()[0], "started")
        self.assertEqual(results.get_nowait()[0], "failed")
        self.assertFalse(worker.is_active())
        stop.set()
        worker.join(1)


if __name__ == "__main__":
    unittest.main()
