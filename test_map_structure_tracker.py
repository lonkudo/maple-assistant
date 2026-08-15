from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch

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

    def test_new_session_reanchors_without_rerecording_positions(self):
        rng = np.random.default_rng(14)
        canvas = rng.integers(0, 140, (160, 160, 3), dtype=np.uint8)
        frame = self.frame(0, canvas)
        detection = self.detection()
        marker = self.marker()
        tracker = MapStructureTracker(tracking_size=128, minimum_response=.05)
        tracker.analyze(frame, detection, marker)

        tracker.start_session(anchor_world_y=-3.25)
        anchored = tracker.analyze(frame, detection, marker)

        self.assertAlmostEqual(anchored.world_y_diamonds, -3.25, places=6)
        self.assertIn("session-anchor", anchored.mode)

    def test_high_confidence_incremental_wins_over_repeating_reference_alias(self):
        rng = np.random.default_rng(21)
        canvas = rng.integers(0, 140, (160, 160, 3), dtype=np.uint8)
        tracker = MapStructureTracker(tracking_size=128, minimum_response=.05)
        tracker.analyze(self.frame(0, canvas), self.detection(), self.marker())

        # Absolute matching locks onto a repeated platform 30 px away with a
        # weak response. Frame-to-frame tracking sees the real smooth 3 px
        # movement with high confidence and must preserve continuity.
        with patch(
            "map_structure_tracker.cv2.phaseCorrelate",
            side_effect=[((0.0, 30.0), .15), ((0.0, 3.0), .95)],
        ):
            result = tracker.analyze(
                self.frame(1, canvas), self.detection(), self.marker()
            )

        self.assertEqual(result.mode, "incremental")
        self.assertGreater(result.confidence, .9)
        self.assertLess(abs(result.scroll_y_diamonds), 1.0)

    def test_world_reanchor_preserves_tracking_state(self):
        rng = np.random.default_rng(30)
        canvas = rng.integers(0, 140, (160, 160, 3), dtype=np.uint8)
        tracker = MapStructureTracker(tracking_size=128, minimum_response=.05)
        first = tracker.analyze(
            self.frame(0, canvas), self.detection(), self.marker()
        )

        tracker.reanchor_world_y(-7.5)
        anchored = tracker.analyze(
            self.frame(0, canvas), self.detection(), self.marker()
        )

        self.assertNotAlmostEqual(first.world_y_diamonds, -7.5)
        self.assertAlmostEqual(anchored.world_y_diamonds, -7.5, places=6)
        self.assertIn("world-reanchor", anchored.mode)


if __name__ == "__main__":
    unittest.main()
