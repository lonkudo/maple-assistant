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
        self.assertTrue(worker.request_toggle())
        time.sleep(0.06)
        self.assertFalse(worker.is_active())
        state, detail = results.get_nowait()
        self.assertEqual(state, "failed")
        self.assertIn("patrol", detail)
        self.assertEqual(sender.sent, [])
        stop.set()
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
