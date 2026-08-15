import threading
import time
import unittest

from focus_worker import FocusWorker


class FakeSender:
    def __init__(self) -> None:
        self.enabled = True
        self.focused = True
        self.release_calls = 0
        self.disable_calls = 0

    def input_is_enabled(self) -> bool:
        return self.enabled

    def is_game_foreground(self) -> bool:
        return self.focused

    def release_all_keys(self) -> None:
        self.release_calls += 1

    def disable_input(self) -> None:
        self.disable_calls += 1
        self.enabled = False


class FocusWorkerTests(unittest.TestCase):
    def test_focus_loss_stops_and_does_not_automatically_resume(self) -> None:
        sender = FakeSender()
        stop = threading.Event()
        active = threading.Event()
        game_focused = threading.Event()
        focus_lost = threading.Event()
        worker = FocusWorker(
            sender, stop, active, game_focused, poll_interval=0.01,
            on_focus_lost=focus_lost.set,
        )
        worker.start()
        try:
            self.assertTrue(active.wait(0.5))
            self.assertTrue(game_focused.is_set())
            sender.focused = False
            deadline = time.monotonic() + 0.5
            while active.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(active.is_set())
            self.assertFalse(game_focused.is_set())
            self.assertGreater(sender.release_calls, 0)
            self.assertTrue(focus_lost.wait(0.5))
            self.assertEqual(sender.disable_calls, 1)

            sender.focused = True
            time.sleep(0.05)
            self.assertFalse(active.is_set())
        finally:
            stop.set()
            worker.join(0.5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(active.is_set())
        self.assertFalse(game_focused.is_set())


if __name__ == "__main__":
    unittest.main()
