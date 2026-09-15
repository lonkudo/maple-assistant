import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ctypes

import countdown_worker
from countdown_worker import CountdownWorker, play_mp3, run_sound_async


class _FakeWinmm:
    """Records MCI commands and reports a controllable playback mode."""

    def __init__(self, polls_before_stop: int = 1, open_error: int = 0):
        self.commands: list[str] = []
        self.polls = 0
        self.polls_before_stop = polls_before_stop
        self.open_error = open_error
        self.mode = "playing"

    def mciSendStringW(self, command, buffer, size, handle):
        self.commands.append(command)
        if self.open_error and command.startswith("open "):
            return self.open_error
        if command.startswith("status "):
            self.polls += 1
            if (self.polls_before_stop is not None
                    and self.polls > self.polls_before_stop):
                self.mode = "stopped"
            if buffer is not None:
                buffer.value = self.mode
        return 0


class PlayMp3Tests(unittest.TestCase):
    def setUp(self) -> None:
        countdown_worker._mci_cooldown_until = 0.0

    def tearDown(self) -> None:
        countdown_worker._mci_cooldown_until = 0.0

    def _sound(self) -> Path:
        path = Path(__file__).with_name("work") / "ma_play_mp3_test.mp3"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"ID3\x00")
        return path

    def test_play_never_waits_and_always_closes_the_alias(self) -> None:
        """MCI ``wait`` blocks forever when the sound never finishes."""

        fake = _FakeWinmm(polls_before_stop=1)
        with mock.patch.object(ctypes, "windll", SimpleNamespace(winmm=fake)):
            play_mp3(self._sound())
        joined = " | ".join(fake.commands)
        self.assertNotIn(" wait", joined)
        self.assertTrue(fake.commands[0].startswith("open "))
        self.assertTrue(fake.commands[1].startswith("play "))
        self.assertTrue(fake.commands[-1].startswith("close "))

    def test_play_gives_up_after_the_budget_and_closes(self) -> None:
        """A clip that never reports a stop must not hang the thread."""

        fake = _FakeWinmm(polls_before_stop=None)
        with mock.patch.object(ctypes, "windll", SimpleNamespace(winmm=fake)):
            with self.assertLogs("countdown_worker", level="WARNING") as logs:
                play_mp3(self._sound(), max_seconds=0.05)
        self.assertIn("exceeded", "\n".join(logs.output))
        self.assertTrue(fake.commands[-1].startswith("close "))

    def test_play_stops_immediately_when_the_stop_event_is_set(self) -> None:
        fake = _FakeWinmm(polls_before_stop=None)
        stop_event = threading.Event()
        stop_event.set()
        with mock.patch.object(ctypes, "windll", SimpleNamespace(winmm=fake)):
            started = time.monotonic()
            play_mp3(self._sound(), stop_event=stop_event)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(fake.commands[-1].startswith("close "))

    def test_play_skips_a_missing_file_without_touching_mci(self) -> None:
        fake = _FakeWinmm()
        with mock.patch.object(ctypes, "windll", SimpleNamespace(winmm=fake)):
            play_mp3(Path(__file__).with_name("work") / "ma_missing.mp3")
        self.assertEqual(fake.commands, [])

    def test_open_failure_still_raises_no_error(self) -> None:
        fake = _FakeWinmm(open_error=258)
        with mock.patch.object(ctypes, "windll", SimpleNamespace(winmm=fake)):
            with self.assertLogs("countdown_worker", level="WARNING"):
                play_mp3(self._sound())
        self.assertEqual(len(fake.commands), 1)
        self.assertTrue(fake.commands[0].startswith("open "))


    def test_play_skips_while_the_device_is_in_cooldown(self) -> None:
        """One wedged clip must not pull every later sound into the hang."""

        fake = _FakeWinmm(polls_before_stop=None)
        with mock.patch.object(ctypes, "windll", SimpleNamespace(winmm=fake)):
            play_mp3(self._sound(), max_seconds=0.05)
            before = len(fake.commands)
            with self.assertLogs("countdown_worker", level="WARNING") as logs:
                play_mp3(self._sound())
        self.assertIn("audio device is unavailable", "\n".join(logs.output))
        self.assertEqual(len(fake.commands), before)

    def test_run_sound_async_plays_on_a_daemon_thread(self) -> None:
        played: list[Path] = []
        done = threading.Event()

        def play(path: Path) -> None:
            played.append(path)
            done.set()

        run_sound_async(play, Path("sound/dingdong.mp3"), name="t-sound")
        self.assertTrue(done.wait(1.0))
        self.assertEqual(played, [Path("sound/dingdong.mp3")])


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
