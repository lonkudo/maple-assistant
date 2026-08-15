from __future__ import annotations

from datetime import datetime, timezone
import time
import unittest

from PIL import Image, ImageDraw

from capture_worker import CapturedFrame
from minimap_detector import MinimapDetector
from ui_worker import build_debug_snapshot


def synthetic_game_frame() -> Image.Image:
    image = Image.new("RGB", (800, 600), "black")
    draw = ImageDraw.Draw(image)
    # Resizable minimap outer frame and its inner map canvas.
    draw.rectangle((2, 2, 162, 222), outline=(230, 230, 230), width=3)
    draw.rectangle((8, 78, 156, 210), outline=(190, 190, 190), width=2)
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

    def test_blank_frame_uses_normalized_fallback(self) -> None:
        detection = MinimapDetector((0, .1, .2, .3)).detect(
            Image.new("RGB", (1000, 500), "black")
        )
        self.assertEqual(detection.source, "fallback")
        self.assertEqual(detection.analysis_box, (0, 50, 200, 150))

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
