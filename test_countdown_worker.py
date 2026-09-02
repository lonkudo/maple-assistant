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
        sound = Path("sound/dingdong.mp3")
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

    def test_stale_zero_drag_during_fire_does_not_fire_twice(self) -> None:
        # The UI drag-end re-applies remaining=0 while the expiry sound is
        # still playing (the bar still shows 0:00).  That stale re-arm must
        # not make the run loop fire the end event a second time once the
        # playback finishes.
        stop = threading.Event()
        sound_started = threading.Event()
        release_sound = threading.Event()
        fired = []

        def blocking_sound(_path: Path) -> None:
            fired.append(time.monotonic())
            sound_started.set()
            release_sound.wait(2.0)

        with mock.patch("countdown_worker.SECONDS_PER_HOUR", 100.0):
            worker = CountdownWorker(
                stop,
                sound_path=Path("sound/dingdong.mp3"),
                enabled=True,
                interval_hours=1.0,
                poll_interval=.01,
                play_sound=blocking_sound,
            )
            worker.start()
            worker.set_remaining_seconds(0.0)
            self.assertTrue(sound_started.wait(1.0))
            # UI echo: bar still at 0:00 -> drag release re-applies zero
            # while the ding-dong is still playing.
            worker.set_remaining_seconds(0.0)
            worker.set_remaining_seconds(0.0)
            release_sound.set()
            time.sleep(.2)
            enabled, _interval, remaining = worker.snapshot()
            stop.set()
            worker._wake_event.set()
            worker.join(1.0)
        self.assertEqual(len(fired), 1)
        self.assertTrue(enabled)
        self.assertGreater(remaining, .0)

    def test_remaining_can_be_dragged_within_interval(self) -> None:
        stop = threading.Event()
        with mock.patch("countdown_worker.SECONDS_PER_HOUR", 100.0):
            worker = CountdownWorker(
                stop, sound_path=Path("sound/dingdong.mp3"),
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
                stop, sound_path=Path("sound/dingdong.mp3"),
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
            threading.Event(), sound_path=Path("sound/dingdong.mp3"),
            enabled=False, interval_hours=1.0,
        )
        worker.set_remaining_seconds(20.0)
        enabled, interval, remaining = worker.snapshot()
        self.assertFalse(enabled)
        self.assertEqual(remaining, interval)

    def test_expiry_requests_visual_alert_with_the_sound(self) -> None:
        flashes = []
        worker = CountdownWorker(
            threading.Event(), sound_path=Path("sound/dingdong.mp3"), enabled=True,
            play_sound=lambda _path: None, flash_callback=lambda: flashes.append(True),
        )
        worker._fire_and_reset()
        self.assertEqual(flashes, [True])

    def test_expiry_requests_message_alert_with_the_sound(self) -> None:
        alerts = []
        worker = CountdownWorker(
            threading.Event(), sound_path=Path("sound/dingdong.mp3"), enabled=True,
            play_sound=lambda _path: None, alert_callback=alerts.append,
        )
        worker._fire_and_reset()
        self.assertEqual(alerts, ["循环警报"])

    def test_sound_can_be_disabled_without_suppressing_other_reminders(self):
        played = []
        flashes = []
        alerts = []
        worker = CountdownWorker(
            threading.Event(), sound_path=Path("sound/dingdong.mp3"), enabled=True,
            play_sound=played.append,
            flash_callback=lambda: flashes.append(True),
            alert_callback=alerts.append,
        )
        worker.set_sound_enabled(False)
        worker._fire_and_reset()
        self.assertEqual(played, [])
        self.assertEqual(flashes, [True])
        self.assertEqual(alerts, ["循环警报"])


if __name__ == "__main__":
    unittest.main()
