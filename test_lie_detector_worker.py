import queue
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from lie_detector_worker import (
    LieDetectorWorker,
    detect_lie_square,
    scaled_lie_square_size,
)


LIE_COLOR = (201, 206, 208)  # #c9ced0


class LieDetectorImageTests(unittest.TestCase):
    def test_reference_width_detects_exact_76_pixel_lie_square(self):
        # At/above the 1366px HUD reference width the lie square is
        # 60 * 1366/1075 ~= 76x76.
        image = Image.new("RGB", (1366, 768), "black")
        for y in range(200, 276):
            for x in range(300, 376):
                image.putpixel((x, y), LIE_COLOR)
        match = detect_lie_square(image)
        self.assertIsNotNone(match)
        self.assertEqual(match[2:], (76, 76))

    def test_target_scales_with_hud_reference_width(self):
        # Fixed pixel at/above 1366px; 60x60 at the measured 1075px client.
        self.assertEqual(scaled_lie_square_size(1920, 1080), (76, 76))
        self.assertEqual(scaled_lie_square_size(1366, 768), (76, 76))
        self.assertEqual(scaled_lie_square_size(1075, 768), (60, 60))
        self.assertEqual(scaled_lie_square_size(1024, 768), (57, 57))
        image = Image.new("RGB", (1075, 768), "black")
        for y in range(50, 110):
            for x in range(80, 140):
                image.putpixel((x, y), LIE_COLOR)
        match = detect_lie_square(image)
        self.assertIsNotNone(match)
        self.assertEqual(match[2:], (60, 60))

    def test_color_shifted_within_tolerance_still_matches(self):
        # +5 per channel on every pixel (within LIE_COLOR_TOLERANCE=10).
        shifted = tuple(channel + 5 for channel in LIE_COLOR)
        image = Image.new("RGB", (1075, 768), "black")
        for y in range(200, 260):
            for x in range(300, 360):
                image.putpixel((x, y), shifted)
        match = detect_lie_square(image)
        self.assertIsNotNone(match)
        self.assertEqual(match[2:], (60, 60))

    def test_color_outside_tolerance_breaks_match(self):
        image = Image.new("RGB", (1075, 768), "black")
        for y in range(200, 260):
            for x in range(300, 360):
                image.putpixel((x, y), LIE_COLOR)
        # 51 below the red channel: far outside the +-10 tolerance.
        image.putpixel((320, 220), (150, 206, 208))
        self.assertIsNone(detect_lie_square(image))


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
        self.assertEqual(events, ["测谎警报"])

    def test_sound_can_be_disabled_without_suppressing_visual_alert(self):
        played = []
        flashed = threading.Event()
        worker = LieDetectorWorker(
            queue.Queue(), threading.Event(), enabled=True,
            play_alert_sound=played.append, flash_callback=flashed.set,
        )
        worker.set_sound_enabled(False)
        worker._update_alert((10, 20, 40, 40))
        self.assertTrue(flashed.wait(.5))
        self.assertEqual(played, [])

    def test_worker_analyzes_shared_frame_without_writing_a_file(self):
        frames = queue.Queue()
        stop = threading.Event()
        alerted = threading.Event()
        image = Image.new("RGB", (1075, 768), "black")
        for y in range(100, 160):
            for x in range(100, 160):
                image.putpixel((x, y), LIE_COLOR)
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
