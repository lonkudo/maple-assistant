from datetime import datetime, timezone
import tempfile
import unittest

import cv2
import numpy as np
from PIL import Image

from capture_worker import CapturedFrame
from map_structure_tracker import MapStructureTracker
from marker_detector import MarkerDetection
from minimap_detector import MinimapDetection


class MapStructureTrackerTests(unittest.TestCase):
    @staticmethod
    def frame(sequence, canvas):
        return CapturedFrame(
            sequence, datetime.now(timezone.utc), float(sequence),
            Image.fromarray(canvas, "RGB"), (0, 0, canvas.shape[1], canvas.shape[0]),
        )

    @staticmethod
    def detection(size=160):
        return MinimapDetection(
            (0, 0, size, size), (0, 0, size, size), (0, 0, size, size),
            (0, 0, 1, 1), 1.0, "test",
        )

    @staticmethod
    def marker(y=.5):
        return MarkerDetection(.5, y, 1.0, (77, int(y * 160) - 3, 83, int(y * 160) + 3))

    def test_static_map_keeps_world_y_stable(self):
        rng = np.random.default_rng(4)
        canvas = rng.integers(0, 150, (160, 160, 3), dtype=np.uint8)
        tracker = MapStructureTracker(tracking_size=128, minimum_response=.05)
        first = tracker.analyze(self.frame(0, canvas), self.detection(), self.marker())
        second = tracker.analyze(self.frame(1, canvas), self.detection(), self.marker())
        self.assertAlmostEqual(first.world_y_diamonds, second.world_y_diamonds, places=2)
        self.assertGreater(second.confidence, .5)

    def test_map_shift_compensates_centered_diamond(self):
        rng = np.random.default_rng(8)
        canvas = rng.integers(0, 140, (160, 160, 3), dtype=np.uint8)
        shifted = cv2.warpAffine(
            canvas, np.float32([[1, 0, 0], [0, 1, -12]]), (160, 160),
            borderMode=cv2.BORDER_CONSTANT,
        )
        tracker = MapStructureTracker(tracking_size=160, minimum_response=.05)
        first = tracker.analyze(self.frame(0, canvas), self.detection(), self.marker())
        second = tracker.analyze(self.frame(1, shifted), self.detection(), self.marker())
        self.assertGreater(second.scroll_y_diamonds, 1.0)
        self.assertGreater(second.world_y_diamonds, first.world_y_diamonds + 1.0)

    def test_reference_survives_restart(self):
        rng = np.random.default_rng(12)
        canvas = rng.integers(0, 140, (160, 160, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            path = __import__("pathlib").Path(directory) / "reference.png"
            tracker = MapStructureTracker(path, tracking_size=128, minimum_response=.05)
            tracker.analyze(self.frame(0, canvas), self.detection(), self.marker())
            tracker.save_reference()
            restored = MapStructureTracker(path, tracking_size=128, minimum_response=.05)
            result = restored.analyze(self.frame(1, canvas), self.detection(), self.marker())
            self.assertEqual(result.mode, "reference")
            self.assertAlmostEqual(result.scroll_y_diamonds, 0.0, places=2)


if __name__ == "__main__":
    unittest.main()
