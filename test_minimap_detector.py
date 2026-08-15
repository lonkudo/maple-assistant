from __future__ import annotations

from datetime import datetime, timezone
import time
import unittest

from PIL import Image, ImageDraw

from capture_worker import CapturedFrame
from marker_detector import DiamondSizeTracker, detect_yellow_diamond
from minimap_detector import MinimapDetector
from ui_worker import build_debug_snapshot


def synthetic_game_frame() -> Image.Image:
    image = Image.new("RGB", (800, 600), "black")
    draw = ImageDraw.Draw(image)
    # Resizable minimap outer frame and its inner map canvas.
    draw.rectangle((2, 2, 162, 222), outline=(230, 230, 230), width=3)
    draw.rectangle(
        (8, 78, 156, 210), fill=(35, 45, 55),
        outline=(190, 190, 190), width=2,
    )
    draw.rectangle((26, 25, 156, 70), fill=(145, 180, 195))
    return image


class FakeMapNameReader:
    def read(self, _image: Image.Image) -> str:
        return "射手训练场 I"


class MinimapDetectorTests(unittest.TestCase):
    def test_opencv_detects_resizable_outer_minimap_frame(self) -> None:
        detection = MinimapDetector().detect(synthetic_game_frame())
        self.assertEqual(detection.source, "opencv")
        self.assertGreater(detection.confidence, .8)
        self.assertAlmostEqual(detection.window_box[0], 2, delta=3)
        self.assertAlmostEqual(detection.window_box[1], 2, delta=3)
        self.assertAlmostEqual(detection.window_size[0], 161, delta=5)
        self.assertAlmostEqual(detection.window_size[1], 221, delta=5)
        canvas_width = detection.canvas_box[2] - detection.canvas_box[0]
        canvas_height = detection.canvas_box[3] - detection.canvas_box[1]
        self.assertAlmostEqual(canvas_width, 149, delta=5)
        self.assertAlmostEqual(canvas_height, 133, delta=5)

    def test_diamond_detector_measures_different_zoom_sizes(self) -> None:
        import numpy as np

        def marker(radius: int):
            image = np.zeros((160, 260, 3), dtype=np.uint8)
            for y in range(-radius, radius + 1):
                for x in range(-radius, radius + 1):
                    if abs(x) + abs(y) <= radius:
                        image[80 + y, 130 + x] = (255, 255, 136)
            return detect_yellow_diamond(image)

        small = marker(3)
        large = marker(7)
        self.assertIsNotNone(small)
        self.assertIsNotNone(large)
        self.assertGreater(large.pixel_size[0], small.pixel_size[0])
        self.assertGreater(large.pixel_size[1], small.pixel_size[1])

    def test_diamond_size_tracker_smooths_animation_and_detects_zoom(self) -> None:
        tracker = DiamondSizeTracker()
        self.assertEqual(tracker.stabilize((7, 7)), (7, 7))
        self.assertEqual(tracker.stabilize((8, 7)), (8, 7))
        self.assertEqual(tracker.stabilize((7, 8)), (7, 7))
        # A real size jump clears old history instead of slowly averaging it.
        self.assertEqual(tracker.stabilize((15, 15)), (15, 15))

    def test_blank_frame_uses_normalized_fallback(self) -> None:
        detection = MinimapDetector((0, .1, .2, .3)).detect(
            Image.new("RGB", (1000, 500), "black")
        )
        self.assertEqual(detection.source, "fallback")
        self.assertEqual(detection.analysis_box, (0, 50, 200, 150))

    def test_opencv_accepts_expanded_wide_minimap(self) -> None:
        image = Image.new("RGB", (1000, 700), "black")
        draw = ImageDraw.Draw(image)
        draw.rectangle((2, 2, 382, 222), outline=(230, 230, 230), width=3)
        draw.rectangle(
            (8, 78, 376, 210), fill=(35, 45, 55),
            outline=(190, 190, 190), width=2,
        )
        detection = MinimapDetector().detect(image)
        self.assertEqual(detection.source, "opencv")
        self.assertGreater(detection.window_size[0], 370)
        self.assertGreater(detection.canvas_box[2] - detection.canvas_box[0], 360)

    def test_dedicated_crop_reconstructs_outer_frame_from_map_canvas(self) -> None:
        image = Image.new("RGB", (320, 320), "black")
        draw = ImageDraw.Draw(image)
        # The real outer border touches the crop edge and may not be a closed
        # contour, while the inner canvas remains a strong rectangle.
        draw.rectangle(
            (6, 90, 155, 240), fill=(35, 45, 55),
            outline=(190, 190, 190), width=2,
        )
        detection = MinimapDetector(dedicated_crop=True).detect(image)
        self.assertEqual(detection.source, "opencv")
        self.assertAlmostEqual(detection.window_box[0], 2, delta=3)
        self.assertEqual(detection.window_box[1], 0)
        self.assertAlmostEqual(detection.window_box[2], 160, delta=5)
        self.assertAlmostEqual(detection.window_box[3], 257, delta=5)

    def test_opencv_working_image_is_170_square_and_boxes_scale_back(self) -> None:
        image = Image.new("RGB", (320, 320), "black")
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            (6, 90, 155, 240), fill=(35, 45, 55),
            outline=(190, 190, 190), width=3,
        )
        detector = MinimapDetector(
            dedicated_crop=True, opencv_size=(170, 170)
        )

        detection = detector.detect(image)

        self.assertEqual(detector.opencv_size, (170, 170))
        self.assertEqual(detection.source, "opencv")
        self.assertGreater(detection.window_box[2], 140)
        self.assertGreater(detection.window_box[3], 220)

    def test_map_name_reader_is_replaceable_adapter(self) -> None:
        detection = MinimapDetector(
            map_name_reader=FakeMapNameReader()
        ).detect(synthetic_game_frame())
        self.assertEqual(detection.map_name, "射手训练场 I")

    def test_ui_snapshot_is_built_without_starting_tk(self) -> None:
        image = synthetic_game_frame()
        frame = CapturedFrame(
            7, datetime.now(timezone.utc), time.monotonic(), image,
            (0, 0, image.width, image.height),
        )
        snapshot = build_debug_snapshot(
            frame, MinimapDetector(), configured_map_name="test map"
        )
        self.assertEqual(snapshot.sequence, 7)
        self.assertEqual(snapshot.client_size, (800, 600))
        self.assertEqual(snapshot.configured_map_name, "test map")
        self.assertEqual(snapshot.minimap_preview.size,
                         snapshot.detection.window_size)


if __name__ == "__main__":
    unittest.main()
