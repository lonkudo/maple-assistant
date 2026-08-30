import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from countdown_worker import CountdownWorker


class CountdownWorkerTests(unittest.TestCase):
    def test_expiry_plays_sound_and_resets_full_interval(self) -> None:
        stop = threading.Event()
        played = []
        sound = Path("sound/beep.mp3")
        with mock.patch("countdown_worker.SECONDS_PER_HOUR", 1.0):
            worker = CountdownWorker(
                stop,
                sound_path=sound,
                enabled=True,
                interval_hours=.08,
                poll_interval=.01,
                play_sound=played.append,
            )
            worker.start()
            deadline = time.monotonic() + 1.0
            while not played and time.monotonic() < deadline:
                time.sleep(.01)
            enabled, interval, remaining = worker.snapshot()
            stop.set()
            worker._wake_event.set()
            worker.join(1.0)
        self.assertEqual(played, [sound])
        self.assertTrue(enabled)
        self.assertAlmostEqual(interval, .08, places=2)
        self.assertGreater(remaining, .02)

    def test_remaining_can_be_dragged_within_interval(self) -> None:
        stop = threading.Event()
        with mock.patch("countdown_worker.SECONDS_PER_HOUR", 100.0):
            worker = CountdownWorker(
                stop, sound_path=Path("sound/beep.mp3"),
                enabled=True, interval_hours=1.0,
            )
            worker.set_remaining_seconds(20.0)
            enabled, interval, remaining = worker.snapshot()
            self.assertTrue(enabled)
            self.assertEqual(interval, 100.0)
            self.assertAlmostEqual(remaining, 20.0, delta=.1)
            worker.set_remaining_seconds(200.0)
            self.assertAlmostEqual(worker.snapshot()[2], 100.0, delta=.1)
            worker.set_remaining_seconds(-5.0)
            self.assertAlmostEqual(worker.snapshot()[2], 0.0, delta=.1)

    def test_interval_change_resets_enabled_timer(self) -> None:
        stop = threading.Event()
        with mock.patch("countdown_worker.SECONDS_PER_HOUR", 100.0):
            worker = CountdownWorker(
                stop, sound_path=Path("sound/beep.mp3"),
                enabled=True, interval_hours=1.0,
            )
            worker.set_remaining_seconds(20.0)
            worker.set_interval_hours(2.0)
            enabled, interval, remaining = worker.snapshot()
        self.assertTrue(enabled)
        self.assertEqual(interval, 200.0)
        self.assertAlmostEqual(remaining, 200.0, delta=.1)

    def test_disabled_timer_does_not_accept_remaining_deadline(self) -> None:
        worker = CountdownWorker(
            threading.Event(), sound_path=Path("sound/beep.mp3"),
            enabled=False, interval_hours=1.0,
        )
        worker.set_remaining_seconds(20.0)
        enabled, interval, remaining = worker.snapshot()
        self.assertFalse(enabled)
        self.assertEqual(remaining, interval)

    def test_expiry_requests_visual_alert_with_the_beep(self) -> None:
        flashes = []
        worker = CountdownWorker(
            threading.Event(), sound_path=Path("sound/beep.mp3"), enabled=True,
            play_sound=lambda _path: None, flash_callback=lambda: flashes.append(True),
        )
        worker._fire_and_reset()
        self.assertEqual(flashes, [True])

    def test_expiry_requests_message_alert_with_the_beep(self) -> None:
        alerts = []
        worker = CountdownWorker(
            threading.Event(), sound_path=Path("sound/beep.mp3"), enabled=True,
            play_sound=lambda _path: None, alert_callback=alerts.append,
        )
        worker._fire_and_reset()
        self.assertEqual(alerts, ["倒计时提醒"])


if __name__ == "__main__":
    unittest.main()
