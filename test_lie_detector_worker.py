import queue
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from lie_detector_worker import (
    LieDetectorWorker,
    detect_pure_white_square,
    scaled_white_square_size,
)


class LieDetectorImageTests(unittest.TestCase):
    def test_reference_resolution_detects_exact_40_pixel_white_square(self):
        image = Image.new("RGB", (1075, 768), "black")
        for y in range(200, 240):
            for x in range(300, 340):
                image.putpixel((x, y), (255, 255, 255))
        match = detect_pure_white_square(image)
        self.assertIsNotNone(match)
        self.assertEqual(match[2:], (40, 40))

    def test_target_scales_independently_with_window_resolution(self):
        self.assertEqual(scaled_white_square_size(1075, 768), (40, 40))
        self.assertEqual(scaled_white_square_size(2150, 1536), (80, 80))
        self.assertEqual(scaled_white_square_size(537, 384), (20, 20))
        image = Image.new("RGB", (537, 384), "black")
        for y in range(50, 70):
            for x in range(80, 100):
                image.putpixel((x, y), (255, 255, 255))
        self.assertIsNotNone(detect_pure_white_square(image))

    def test_nonwhite_pixel_breaks_an_exact_size_match(self):
        image = Image.new("RGB", (1075, 768), "black")
        for y in range(200, 240):
            for x in range(300, 340):
                image.putpixel((x, y), (255, 255, 255))
        image.putpixel((320, 220), (254, 255, 255))
        self.assertIsNone(detect_pure_white_square(image))


class LieDetectorWorkerTests(unittest.TestCase):
    def test_scans_only_when_one_second_deadline_is_due(self):
        worker = LieDetectorWorker(
            queue.Queue(), threading.Event(), enabled=False,
            scan_interval=1.0,
        )
        worker.set_enabled(True)
        due = worker._next_scan_at
        self.assertIsNotNone(due)
        self.assertFalse(worker._take_due_scan(due - .01))
        self.assertTrue(worker._take_due_scan(due))
        self.assertAlmostEqual(worker._next_scan_at, due + 1.0, places=3)

    def test_alerts_once_per_visible_event_and_rearms_when_square_clears(self):
        played = []
        played_event = threading.Event()

        def play(path):
            played.append(path)
            played_event.set()

        worker = LieDetectorWorker(
            queue.Queue(), threading.Event(), enabled=True,
            sound_path=Path("sound/beep.mp3"),
            play_alert_sound=play,
        )
        match = (10, 20, 40, 40)
        worker._update_alert(match)
        self.assertTrue(played_event.wait(.5))
        worker._update_alert(match)
        self.assertEqual(len(played), 1)
        worker._update_alert(None)
        played_event.clear()
        worker._update_alert(match)
        self.assertTrue(played_event.wait(.5))
        self.assertEqual(len(played), 2)

    def test_lie_alert_requests_visual_alert_with_the_beep(self):
        flashed = threading.Event()
        worker = LieDetectorWorker(
            queue.Queue(), threading.Event(), enabled=True,
            play_alert_sound=lambda _path: None, flash_callback=flashed.set,
        )
        worker._update_alert((10, 20, 40, 40))
        self.assertTrue(flashed.wait(.5))

    def test_lie_alert_requests_message_alert_with_the_beep(self):
        alerted = threading.Event()
        events = []

        def notify(event_type):
            events.append(event_type)
            alerted.set()

        worker = LieDetectorWorker(
            queue.Queue(), threading.Event(), enabled=True,
            play_alert_sound=lambda _path: None, alert_callback=notify,
        )
        worker._update_alert((10, 20, 40, 40))
        self.assertTrue(alerted.wait(.5))
        self.assertEqual(events, ["测谎报警"])

    def test_worker_analyzes_shared_frame_without_writing_a_file(self):
        frames = queue.Queue()
        stop = threading.Event()
        alerted = threading.Event()
        image = Image.new("RGB", (1075, 768), "black")
        for y in range(100, 140):
            for x in range(100, 140):
                image.putpixel((x, y), (255, 255, 255))
        worker = LieDetectorWorker(
            frames, stop, enabled=True, scan_interval=.05,
            sound_path=Path("sound/beep.mp3"),
            play_alert_sound=lambda _path: alerted.set(),
        )
        worker.start()
        try:
            time.sleep(.06)
            frames.put(SimpleNamespace(image=image))
            self.assertTrue(alerted.wait(1.0))
        finally:
            stop.set()
            worker.join(1.0)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
