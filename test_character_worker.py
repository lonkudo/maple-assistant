import queue
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from character_worker import CharacterWorker


class CharacterWorkerDisconnectAlertTests(unittest.TestCase):
    def make_worker(self, callback, *, enabled=True, misses=3):
        return CharacterWorker(
            queue.Queue(), queue.Queue(), threading.Event(),
            disconnect_alert_enabled=enabled,
            disconnect_alert_misses=misses,
            alert_sound_path=Path("sound/beep.mp3"),
            play_alert_sound=callback,
        )

    def test_alerts_once_after_confirmed_loss_and_rearms_on_detection(self):
        played = []
        played_event = threading.Event()

        def play(path):
            played.append(path)
            played_event.set()

        worker = self.make_worker(play, misses=3)
        worker._update_disconnect_alert(False)
        worker._update_disconnect_alert(False)
        self.assertFalse(played_event.wait(.05))
        worker._update_disconnect_alert(False)
        self.assertTrue(played_event.wait(.5))
        self.assertEqual(played, [Path("sound/beep.mp3")])

        # A sustained loss beeps only once. Seeing the marker again re-arms
        # the next independently confirmed loss episode.
        worker._update_disconnect_alert(False)
        worker._update_disconnect_alert(False)
        self.assertEqual(len(played), 1)
        worker._update_disconnect_alert(True)
        played_event.clear()
        for _ in range(3):
            worker._update_disconnect_alert(False)
        self.assertTrue(played_event.wait(.5))
        self.assertEqual(len(played), 2)

    def test_disabled_alert_never_plays(self):
        played = []
        worker = self.make_worker(played.append, enabled=False, misses=1)
        for _ in range(5):
            worker._update_disconnect_alert(False)
        self.assertEqual(played, [])
        worker.set_disconnect_alert(True)
        worker._update_disconnect_alert(False)
        deadline = threading.Event()
        self.assertTrue(deadline.wait(.05) is False)
        # The callback thread is short; polling the list avoids timing races.
        for _ in range(20):
            if played:
                break
            threading.Event().wait(.01)
        self.assertEqual(played, [Path("sound/beep.mp3")])

    def test_disconnect_alert_requests_visual_alert_with_the_beep(self):
        flashed = threading.Event()
        worker = CharacterWorker(
            queue.Queue(), queue.Queue(), threading.Event(),
            disconnect_alert_enabled=True, disconnect_alert_misses=1,
            play_alert_sound=lambda _path: None, flash_callback=flashed.set,
        )
        worker._update_disconnect_alert(False)
        self.assertTrue(flashed.wait(.5))

    def test_disconnect_alert_requests_message_alert_with_the_beep(self):
        alerted = threading.Event()
        events = []

        def notify(event_type):
            events.append(event_type)
            alerted.set()

        worker = CharacterWorker(
            queue.Queue(), queue.Queue(), threading.Event(),
            disconnect_alert_enabled=True, disconnect_alert_misses=1,
            play_alert_sound=lambda _path: None, alert_callback=notify,
        )
        worker._update_disconnect_alert(False)
        self.assertTrue(alerted.wait(.5))
        self.assertEqual(events, ["掉线警报"])

    def test_run_reuses_the_single_marker_detection_for_alert(self):
        frames = queue.Queue()
        positions = queue.Queue()
        stop = threading.Event()
        alerted = threading.Event()

        def play(_path):
            alerted.set()
            stop.set()

        worker = CharacterWorker(
            frames, positions, stop,
            disconnect_alert_enabled=True,
            disconnect_alert_misses=1,
            alert_sound_path=Path("sound/beep.mp3"),
            play_alert_sound=play,
        )
        frames.put(SimpleNamespace(
            image=Image.new("RGB", (200, 200), "black"), sequence=7
        ))
        with mock.patch("character_worker.detect_yellow_diamond",
                        return_value=None) as detector:
            worker.start()
            self.assertTrue(alerted.wait(1.0))
            worker.join(1.0)
        detector.assert_called_once()
        position = positions.get_nowait()
        self.assertIsNone(position.x)
        self.assertEqual(position.frame_sequence, 7)


if __name__ == "__main__":
    unittest.main()
