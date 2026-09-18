"""Character worker tests: the disconnect alert counts missing FRAMES (operator's rule: 120).

The threshold is a frame count, so the equivalent time depends on the capture interval (0.25 s by
default, 0.10 s while dropping, 1/30 s during an API lie pass).  ``now`` is injected wherever the
test cares about the elapsed time that the log prints.
"""

import queue
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from character_worker import DISCONNECT_ALERT_FRAMES, CharacterWorker


class CharacterWorkerDisconnectAlertTests(unittest.TestCase):
    def make_worker(self, callback, *, enabled=True, misses=DISCONNECT_ALERT_FRAMES,
                    **kwargs):
        return CharacterWorker(
            queue.Queue(), queue.Queue(), threading.Event(),
            disconnect_alert_enabled=enabled,
            disconnect_alert_misses=misses,
            alert_sound_path=Path("sound/dingdong.mp3"),
            play_alert_sound=callback,
            **kwargs,
        )

    def test_the_default_threshold_is_120_frames(self):
        self.assertEqual(DISCONNECT_ALERT_FRAMES, 120)
        worker = self.make_worker(lambda _path: None)
        self.assertEqual(worker._disconnect_alert_misses, 120)

    def test_alerts_on_the_last_frame_only_and_rearms_on_detection(self):
        played = []
        played_event = threading.Event()

        def play(path):
            played.append(path)
            played_event.set()

        worker = self.make_worker(play, misses=120)
        for frame in range(119):                       # 119 misses is not a disconnect yet
            worker._update_disconnect_alert(False, now=frame * 0.25)
        self.assertEqual(played, [])
        worker._update_disconnect_alert(False, now=119 * 0.25)
        self.assertTrue(played_event.wait(.5))
        self.assertEqual(played, [Path("sound/dingdong.mp3")])

        # A sustained loss plays only once.  Seeing the marker again re-arms the next episode.
        for frame in range(200, 400):
            worker._update_disconnect_alert(False, now=frame * 0.25)
        self.assertEqual(len(played), 1)
        worker._update_disconnect_alert(True, now=200.0)
        played_event.clear()
        for frame in range(120):
            worker._update_disconnect_alert(False, now=300.0 + frame * 0.25)
        self.assertTrue(played_event.wait(.5))
        self.assertEqual(len(played), 2)

    def test_a_single_detected_frame_resets_the_counter(self):
        played = []
        worker = self.make_worker(played.append, misses=120)
        for frame in range(119):
            worker._update_disconnect_alert(False, now=frame * 0.25)
        worker._update_disconnect_alert(True, now=100.0)     # the marker is back
        self.assertEqual(worker._disconnect_missing_frames, 0)
        for frame in range(119):
            worker._update_disconnect_alert(False, now=200.0 + frame * 0.25)
        for _ in range(20):
            threading.Event().wait(.01)
        self.assertEqual(played, [], "the counter must start over after a detection")

    def test_the_log_prints_frames_and_seconds(self):
        worker = self.make_worker(lambda _path: None, misses=2)
        worker._update_disconnect_alert(False, now=0.0)
        with self.assertLogs("character_worker", level="WARNING") as captured:
            worker._update_disconnect_alert(False, now=0.25)
        line = "\n".join(captured.output)
        self.assertIn("2 consecutive frames", line)
        self.assertIn("0.2s", line)
        self.assertIn("threshold 2 frames", line)

    def test_disabled_alert_never_plays(self):
        played = []
        worker = self.make_worker(played.append, enabled=False, misses=1)
        for frame in range(5):
            worker._update_disconnect_alert(False, now=float(frame))
        self.assertEqual(played, [])
        worker.set_disconnect_alert(True)
        worker._update_disconnect_alert(False, now=10.0)
        # The callback thread is short; polling the list avoids timing races.
        for _ in range(20):
            if played:
                break
            threading.Event().wait(.01)
        self.assertEqual(played, [Path("sound/dingdong.mp3")])

    def test_disconnect_alert_requests_visual_alert_with_the_sound(self):
        flashed = threading.Event()
        worker = self.make_worker(
            lambda _path: None, misses=1, flash_callback=flashed.set,
        )
        worker._update_disconnect_alert(False)
        self.assertTrue(flashed.wait(.5))

    def test_disconnect_alert_requests_message_alert_with_the_sound(self):
        alerted = threading.Event()
        events = []

        def notify(event_type):
            events.append(event_type)
            alerted.set()

        worker = self.make_worker(
            lambda _path: None, misses=1, alert_callback=notify,
        )
        worker._update_disconnect_alert(False)
        self.assertTrue(alerted.wait(.5))
        self.assertEqual(events, ["掉线警报"])

    def test_sound_can_be_disabled_without_suppressing_message_alert(self):
        played = []
        events = []
        alerted = threading.Event()

        def notify(event_type):
            events.append(event_type)
            alerted.set()

        worker = self.make_worker(
            played.append, misses=1, alert_callback=notify,
        )
        worker.set_sound_enabled(False)
        worker._update_disconnect_alert(False)
        self.assertTrue(alerted.wait(.5))
        self.assertEqual(played, [])
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
            alert_sound_path=Path("sound/dingdong.mp3"),
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
