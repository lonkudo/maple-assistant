import logging
import threading
import time
import unittest

from focus_worker import FocusWorker


class FakeSender:
    def __init__(self) -> None:
        self.enabled = True
        self.focused = True
        self.select_ok = True
        self.release_calls = 0
        self.disable_calls = 0
        self.select_calls = 0
        self.focus_calls = 0

    def input_is_enabled(self) -> bool:
        return self.enabled

    def is_game_foreground(self) -> bool:
        self.focus_calls += 1
        return self.focused

    def select_window(self) -> bool:
        self.select_calls += 1
        return self.select_ok

    def release_all_keys(self) -> None:
        self.release_calls += 1

    def disable_input(self) -> None:
        self.disable_calls += 1
        self.enabled = False


class FocusWorkerTests(unittest.TestCase):
    def test_stopped_patrol_does_not_check_or_select_window(self) -> None:
        sender = FakeSender()
        sender.enabled = False
        stop = threading.Event()
        active = threading.Event()
        game_focused = threading.Event()
        worker = FocusWorker(
            sender, stop, active, game_focused, poll_interval=0.01,
        )
        worker.start()
        try:
            time.sleep(0.08)
            self.assertEqual(sender.focus_calls, 0)
            self.assertEqual(sender.select_calls, 0)
            self.assertFalse(active.is_set())
            self.assertFalse(game_focused.is_set())

            sender.enabled = True
            deadline = time.monotonic() + 0.5
            while sender.focus_calls < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(sender.focus_calls, 1)
            self.assertTrue(active.wait(0.5))
        finally:
            stop.set()
            worker.join(0.5)

    def test_transient_focus_dip_resumes_without_stopping(self) -> None:
        sender = FakeSender()
        stop = threading.Event()
        active = threading.Event()
        game_focused = threading.Event()
        worker = FocusWorker(
            sender, stop, active, game_focused, poll_interval=0.01,
            focus_lost_grace_seconds=0.5,
        )
        worker.start()
        try:
            self.assertTrue(active.wait(0.5))
            self.assertTrue(game_focused.is_set())
            # Short dip: the game is away for ~0.05s, well inside the grace.
            sender.focused = False
            deadline = time.monotonic() + 0.5
            while active.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(active.is_set())
            self.assertFalse(game_focused.is_set())
            # Auto-refocus was attempted while the game was away (async thread).
            select_deadline = time.monotonic() + 0.5
            while sender.select_calls < 1 and time.monotonic() < select_deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(sender.select_calls, 1)
            sender.focused = True
            self.assertTrue(active.wait(0.5))
            self.assertTrue(game_focused.is_set())
            self.assertEqual(sender.disable_calls, 0)
        finally:
            stop.set()
            worker.join(0.5)
        self.assertFalse(worker.is_alive())

    def test_sustained_focus_loss_pauses_and_then_resumes(self) -> None:
        # The operator's rule: only a manual Stop Patrol ends a patrol.  A
        # sustained focus loss - refocus cannot win here - must PAUSE, release
        # the keys and resume by itself once the game is focused again.  It must
        # never disarm live input or stop the patrol.
        sender = FakeSender()
        stop = threading.Event()
        active = threading.Event()
        game_focused = threading.Event()
        worker = FocusWorker(
            sender, stop, active, game_focused, poll_interval=0.01,
            focus_lost_grace_seconds=0.05,
        )
        worker.start()
        try:
            self.assertTrue(active.wait(0.5))
            self.assertTrue(game_focused.is_set())
            sender.select_ok = False
            sender.focused = False
            # Automation pauses immediately, and the keys are released.
            deadline = time.monotonic() + 0.3
            while active.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(active.is_set())
            self.assertFalse(game_focused.is_set())
            self.assertGreater(sender.release_calls, 0)
            # Refocus was attempted (async thread) but cannot win.
            select_deadline = time.monotonic() + 0.5
            while sender.select_calls < 1 and time.monotonic() < select_deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(sender.select_calls, 1)
            # Well past the grace window the run is still armed - paused, not
            # stopped - so the game coming back resumes it by itself.
            time.sleep(0.15)
            self.assertTrue(sender.enabled)
            self.assertEqual(sender.disable_calls, 0)
            sender.focused = True
            self.assertTrue(active.wait(0.5))
            self.assertTrue(game_focused.is_set())
        finally:
            stop.set()
            worker.join(0.5)
        self.assertFalse(worker.is_alive())

    def test_pause_is_reported_once_per_unfocused_episode(self) -> None:
        sender = FakeSender()
        stop = threading.Event()
        active = threading.Event()
        game_focused = threading.Event()
        worker = FocusWorker(
            sender, stop, active, game_focused, poll_interval=0.01,
            focus_lost_grace_seconds=0.02,
        )
        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        handler = Capture()
        logger = logging.getLogger("focus_worker")
        previous_level = logger.level
        # The resume line is INFO; without this the root WARNING level drops it.
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        worker.start()
        try:
            self.assertTrue(active.wait(0.5))
            sender.focused = False
            time.sleep(0.25)          # many polls, one episode
            self.assertEqual(
                len([r for r in records if "AUTOMATION PAUSED" in r]), 1
            )
            sender.focused = True
            deadline = time.monotonic() + 0.5
            while not active.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            # The event is set before the resume line is written, so wait for it.
            resume_deadline = time.monotonic() + 0.5
            while (not [r for r in records if "AUTOMATION RESUMED" in r]
                   and time.monotonic() < resume_deadline):
                time.sleep(0.01)
            self.assertEqual(
                len([r for r in records if "AUTOMATION RESUMED" in r]), 1
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
            stop.set()
            worker.join(0.5)

    def test_lie_pass_keeps_the_foreground_gate_open_without_input(self) -> None:
        # A 自动过测谎 pass takes the machine over with input DISARMED, yet the
        # pass is fed by the same capture: the foreground gate must stay open
        # for it (otherwise the capture stops and the pass starves - the field
        # log's "sequence ended (capture stalled) after 2.2s").
        sender = FakeSender()
        sender.enabled = False          # input disarmed by the takeover
        stop = threading.Event()
        active = threading.Event()
        game_focused = threading.Event()
        lie_pass = threading.Event()
        worker = FocusWorker(
            sender, stop, active, game_focused, poll_interval=0.01,
            lie_pass_event=lie_pass,
        )
        worker.start()
        try:
            time.sleep(0.08)
            # Disarmed and no pass: idle, gate closed, nothing probed.
            self.assertFalse(game_focused.is_set())
            self.assertEqual(sender.focus_calls, 0)

            lie_pass.set()
            deadline = time.monotonic() + 0.5
            while not game_focused.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(game_focused.is_set())
            self.assertGreaterEqual(sender.focus_calls, 1)
            # Frames keep flowing, but the keyboard stays blocked: automation
            # must NOT be re-armed by this probe.
            self.assertFalse(active.is_set())

            lie_pass.clear()
            deadline = time.monotonic() + 0.5
            while game_focused.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(game_focused.is_set())
        finally:
            stop.set()
            worker.join(0.5)
        self.assertFalse(game_focused.is_set())

if __name__ == "__main__":
    unittest.main()
