from __future__ import annotations

from datetime import datetime, timezone
import time
import unittest

from PIL import Image, ImageDraw

from capture_worker import CapturedFrame
from marker_detector import DiamondSizeTracker, detect_yellow_diamond
from minimap_detector import (
    MinimapDetection,
    MinimapDetector,
    choose_stable_minimap_index,
    minimap_calibration_from_dict,
    minimap_calibration_to_dict,
)
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
    def test_recorded_calibration_scales_to_current_client_size(self):
        detection = MinimapDetection(
            (2, 3, 102, 153), (2, 50, 108, 153),
            (8, 55, 98, 148), (16, 12, 96, 45),
            .94, "opencv",
        )
        saved = minimap_calibration_to_dict(detection, (800, 600))

        restored = minimap_calibration_from_dict(saved, (1600, 1200))

        self.assertIsNotNone(restored)
        self.assertEqual(restored.window_box, (4, 6, 204, 306))
        self.assertEqual(restored.analysis_box, (4, 100, 216, 306))
        self.assertEqual(restored.source, "opencv-recording")

    def test_invalid_recorded_calibration_is_rejected(self):
        self.assertIsNone(minimap_calibration_from_dict({}, (800, 600)))
        self.assertIsNone(minimap_calibration_from_dict({
            "schema": 1,
            "window_box": [0, 0, 1, 1],
        }, (800, 600)))

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

    def test_diamond_detector_accepts_heavy_minimap_zoom(self) -> None:
        """A zoomed diamond inside a fixed panel must still be found.

        The minimap ZOOM can change while the panel size stays the same, so
        the yellow player diamond grows within the analysis box.  The real
        marker is small (~6-7 px at normal zoom on a 130-170 px box); the
        detector must accept a bounded zoom range (roughly 3-4x) but reject
        oversized yellow blobs that are never the marker.
        """

        import numpy as np

        def marker(radius: int, box: tuple[int, int] = (171, 167)):
            height, width = box
            image = np.zeros((height, width, 3), dtype=np.uint8)
            center_y, center_x = height // 2, width // 2
            for y in range(-radius, radius + 1):
                for x in range(-radius, radius + 1):
                    if abs(x) + abs(y) <= radius:
                        image[center_y + y, center_x + x] = (255, 255, 136)
            return detect_yellow_diamond(image)

        # Normal marker (~7px, radius 3) and a zoomed one (radius 12 -> 25px
        # span, well inside the ~30px cap for a 167px box).
        normal = marker(3)
        zoomed = marker(12)
        self.assertIsNotNone(normal)
        self.assertIsNotNone(zoomed)
        self.assertGreater(zoomed.pixel_size[0], normal.pixel_size[0])

        # A 49px blob (radius 24) is far beyond the marker's zoom range and
        # must be rejected, not mistaken for the player diamond.
        self.assertIsNone(marker(24))

        # A long yellow platform decoration must still be rejected.
        height, width = 171, 167
        image = np.zeros((height, width, 3), dtype=np.uint8)
        image[80, 10:150] = (255, 255, 136)
        self.assertIsNone(detect_yellow_diamond(image))

    def test_red_diamond_detector_counts_other_players(self) -> None:
        import numpy as np
        from marker_detector import detect_red_diamonds

        def diamond(image, cy, cx, radius, color):
            for y in range(-radius, radius + 1):
                for x in range(-radius, radius + 1):
                    if abs(x) + abs(y) <= radius:
                        image[cy + y, cx + x] = color

        image = np.zeros((160, 260, 3), dtype=np.uint8)
        # Two red player diamonds (#e30000 center).
        diamond(image, 50, 60, 4, (227, 0, 0))
        diamond(image, 110, 200, 3, (227, 0, 0))
        self.assertEqual(len(detect_red_diamonds(image)), 2)

    def test_red_diamond_detector_ignores_yellow_and_decorations(self) -> None:
        import numpy as np
        from marker_detector import detect_red_diamonds

        image = np.zeros((160, 260, 3), dtype=np.uint8)
        # The yellow player diamond and a long orange-ish decoration must not
        # count as other players.
        for y in range(-4, 5):
            for x in range(-4, 5):
                if abs(x) + abs(y) <= 4:
                    image[60 + y, 100 + x] = (255, 255, 136)
        image[30, 20:80] = (230, 120, 40)  # long orange strip
        self.assertEqual(detect_red_diamonds(image), [])

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

    def test_dedicated_crop_does_not_infer_border_from_search_crop(self) -> None:
        image = Image.new("RGB", (320, 320), "black")
        draw = ImageDraw.Draw(image)
        # The real outer border touches the crop edge and may not be a closed
        # contour, while the inner canvas remains a strong rectangle.
        draw.rectangle(
            (6, 90, 155, 240), fill=(35, 45, 55),
            outline=(190, 190, 190), width=2,
        )
        detection = MinimapDetector(dedicated_crop=True).detect(image)
        self.assertEqual(detection.source, "fallback")

    def test_opencv_working_image_is_180_square_and_boxes_scale_back(self) -> None:
        image = Image.new("RGB", (320, 320), "black")
        draw = ImageDraw.Draw(image)
        draw.rectangle((2, 2, 162, 172), outline=(230, 230, 230), width=3)
        draw.rectangle(
            (6, 60, 155, 160), fill=(35, 45, 55),
            outline=(190, 190, 190), width=3,
        )
        detector = MinimapDetector(
            dedicated_crop=True, opencv_size=(180, 180)
        )

        detection = detector.detect(image)

        self.assertEqual(detector.opencv_size, (180, 180))
        self.assertEqual(detection.source, "opencv")
        self.assertGreater(detection.window_box[2], 140)
        self.assertGreater(detection.window_box[3], 150)

    def test_transient_contour_miss_keeps_last_good_minimap_geometry(self) -> None:
        detector = MinimapDetector(
            dedicated_crop=True,
            opencv_size=(180, 180),
            transient_hold_seconds=1.0,
        )
        good = Image.new("RGB", (320, 320), "black")
        draw = ImageDraw.Draw(good)
        draw.rectangle((2, 2, 162, 172), outline=(230, 230, 230), width=3)
        draw.rectangle(
            (6, 60, 155, 160), fill=(35, 45, 55),
            outline=(190, 190, 190), width=3,
        )
        blank = Image.new("RGB", good.size, "black")

        detected = detector.detect(good)
        held = detector.detect(blank)

        self.assertEqual(detected.source, "opencv")
        self.assertEqual(held.source, "opencv-held")
        self.assertEqual(held.window_box, detected.window_box)
        self.assertEqual(held.analysis_box, detected.analysis_box)
        self.assertEqual(held.canvas_box, detected.canvas_box)

    def test_verified_geometry_can_be_retained_for_same_size_restart(self) -> None:
        detector = MinimapDetector()
        detection = detector.detect(synthetic_game_frame())
        detector.seed_geometry(detection, (800, 600))

        retained = detector.retained_geometry((800, 600))

        self.assertIsNotNone(retained)
        self.assertEqual(retained.source, "opencv-held")
        self.assertEqual(retained.window_box, detection.window_box)
        self.assertIsNone(detector.retained_geometry((1024, 768)))

    def test_box_smoothing_stabilizes_frame_repeats_and_rejects_resizes(self) -> None:
        # Live logs show the minimap frame flipping between near-identical
        # contour boxes every frame; each flip shifts the player marker AND
        # the projected targets, stalling the rope approach.  The median
        # filter must keep the coordinate frame stable across the flip
        # variants, while a genuine resize jump is adopted immediately.
        detector = MinimapDetector()
        repeats = [
            ((0, 0, 95, 135), (0, 20, 95, 135), (2, 40, 92, 130)),
            ((2, 0, 92, 140), (2, 20, 92, 140), (2, 40, 90, 132)),
            ((0, 0, 96, 137), (0, 20, 96, 137), (3, 40, 93, 131)),
            ((2, 0, 92, 140), (2, 20, 92, 140), (2, 40, 90, 132)),
            ((3, 0, 92, 140), (3, 20, 92, 140), (2, 40, 90, 130)),
        ]
        outputs = [detector._stabilize_boxes(w, a, c) for w, a, c in repeats]
        self.assertEqual(outputs[0], repeats[0])  # first frame passes raw
        for box in outputs[1:]:
            width = box[0][2] - box[0][0]
            height = box[0][3] - box[0][1]
            self.assertLessEqual(abs(width - 90), 6)   # 90..96
            self.assertLessEqual(abs(height - 137), 4)  # 135..140
        # A large change during the session is rejected; a new map/session
        # uses reset_geometry before establishing its own border.
        resize = ((50, 10, 300, 260), (50, 30, 300, 260), (55, 50, 295, 250))
        jumped = detector._stabilize_boxes(*resize)
        self.assertNotEqual(jumped[0], resize[0])
        detector.reset_geometry()
        self.assertEqual(detector._stabilize_boxes(*resize)[0], resize[0])

    def test_startup_prefers_repeated_smaller_minimap_border(self) -> None:
        def found(width: int, height: int) -> MinimapDetection:
            box = (0, 0, width, height)
            return MinimapDetection(
                box, box, box, box, .98, "opencv"
            )

        detections = [
            found(238, 207), found(96, 135), found(238, 207),
            found(95, 134), found(97, 135),
        ]
        chosen = detections[choose_stable_minimap_index(detections)]
        self.assertLess(chosen.window_size[0], 100)

    def test_startup_rejects_unstable_or_fallback_only_geometry(self) -> None:
        fallback = MinimapDetection(
            (0, 0, 238, 207), (0, 0, 238, 207), (0, 0, 238, 207),
            (0, 0, 10, 10), 0.0, "fallback",
        )
        with self.assertRaises(OSError):
            choose_stable_minimap_index([fallback, fallback, fallback])

    def test_startup_accepts_single_opencv_border_verified_by_marker(self):
        fallback = MinimapDetection(
            (0, 0, 238, 207), (0, 0, 238, 207), (0, 0, 238, 207),
            (0, 0, 10, 10), 0.0, "fallback",
        )
        measured = MinimapDetection(
            (0, 0, 96, 135), (0, 40, 101, 135), (2, 45, 94, 132),
            (10, 8, 90, 42), .55, "opencv",
        )
        self.assertEqual(
            choose_stable_minimap_index(
                [fallback, measured, fallback],
                marker_verified_indices=[1],
            ),
            1,
        )
        with self.assertRaises(OSError):
            choose_stable_minimap_index(
                [fallback, fallback], marker_verified_indices=[0]
            )

    def test_box_smoothing_rejects_transient_minimap_height_collapse(self) -> None:
        """A clipped contour must not crop the player marker out of the map."""

        detector = MinimapDetector()
        stable = ((0, 0, 239, 184), (0, 58, 239, 184), (2, 78, 237, 178))
        collapsed = ((0, 0, 239, 69), (0, 22, 239, 69), (2, 31, 237, 65))
        detector._stabilize_boxes(*stable)

        # The failure seen in the live log was a one/two-frame crop from 184
        # to 69 pixels high.  Retain the known-good geometry during it.
        self.assertEqual(detector._stabilize_boxes(*collapsed), stable)
        self.assertEqual(detector._stabilize_boxes(*collapsed), stable)
        self.assertEqual(detector._stabilize_boxes(*stable), stable)

        # The current session must not adopt a persistent cropped contour:
        # it would remap recorded adaptive layer coordinates and make a
        # correctly recorded layer look like another floor.  A deliberate HUD
        # resize is picked up cleanly after restarting the assistant.
        detector._stabilize_boxes(*collapsed)
        detector._stabilize_boxes(*collapsed)
        self.assertEqual(detector._stabilize_boxes(*collapsed), stable)

        # Starting patrol on another map deliberately resets the geometry;
        # its differently sized minimap then becomes the new valid baseline.
        detector.reset_geometry()
        self.assertEqual(detector._stabilize_boxes(*collapsed), collapsed)

    def test_large_client_border_detects_with_aspect_preserving_fit(self) -> None:
        """A big client must not fall back because the crop is squashed.

        The debug snapshot is 1707x1067: the 22% x 27% search crop is
        375x288.  The old exact-square resize to a fixed 200x200 working
        image distorted that crop and thinned the minimap border until Canny
        could no longer close its rectangle, so every frame fell back and
        patrol/recording could never verify a border.  The detector now fits
        the crop inside its analysis box preserving aspect ratio.
        """

        image = Image.new("RGB", (1707, 1067), "black")
        draw = ImageDraw.Draw(image)
        # Same geometry as the real snapshot: outer border at the top-left
        # corner, inner map canvas, and a lighter title strip.
        draw.rectangle((0, 0, 158, 248), outline=(230, 230, 230), width=3)
        draw.rectangle(
            (4, 84, 154, 238), fill=(35, 45, 55),
            outline=(190, 190, 190), width=2,
        )
        draw.rectangle((22, 26, 152, 70), fill=(145, 180, 195))

        detector = MinimapDetector(
            dedicated_crop=True, opencv_size=(400, 400)
        )
        detection = detector.detect(image)

        self.assertEqual(detection.source, "opencv")
        self.assertGreater(detection.confidence, 0.8)
        self.assertLess(abs(detection.window_size[0] - 158), 12)
        self.assertLess(abs(detection.window_size[1] - 248), 12)

    def test_partial_title_strip_never_becomes_baseline(self) -> None:
        """A strip sharing the top-left corner must not win over the border.

        The live log flickered between box=(0,0,239,68) (the title strip) and
        box=(0,0,239,184) (the full border).  A strip that becomes the median
        excludes the yellow marker from its analysis box, so recording fails
        with "failed to detect yellow diamond" even though the full border is
        visible.  When both contours appear in the same frame the outer frame
        must win; a persistent strip must never replace a full baseline.
        """

        image = Image.new("RGB", (1707, 1067), "black")
        draw = ImageDraw.Draw(image)
        # Solid bright minimap panel (outer contour closes), dark canvas
        # hollowing the lower part, and a bright title strip with its own
        # closed contour: the detector sees both a 229x62-ish strip and the
        # full 235x180 frame, exactly like the live flicker frames.
        draw.rectangle((0, 0, 239, 184), fill=(150, 160, 175),
                       outline=(235, 235, 235), width=3)
        draw.rectangle((6, 84, 233, 178), fill=(35, 45, 55),
                       outline=(200, 200, 200), width=2)
        draw.rectangle((6, 6, 233, 66), fill=(145, 180, 195),
                       outline=(235, 235, 235), width=2)

        detector = MinimapDetector(
            dedicated_crop=True, opencv_size=(400, 400)
        )
        detection = detector.detect(image)

        self.assertEqual(detection.source, "opencv")
        # The full border (tall) wins, not the 68-px strip.
        self.assertGreater(detection.window_size[1], 120)

    def test_strip_poisoned_baseline_recovers_to_full_border(self) -> None:
        """A first-frame strip must not block recovery for the session.

        Regression: after reset_geometry (recording reset / refocus), the very
        first frame can detect the partial title strip.  The old stabilizer
        returned the median forever and never appended the severely-changed
        full border, so the marker stayed outside the analysis box and every
        record click failed.  The grow direction must be adopted once the
        same larger box repeats for a full history window.
        """

        strip = ((0, 0, 239, 68), (0, 21, 239, 68), (2, 25, 237, 64))
        full = ((0, 0, 239, 184), (0, 57, 239, 184), (2, 78, 237, 178))
        detector = MinimapDetector(
            dedicated_crop=True, opencv_size=(400, 400)
        )
        detector.reset_geometry()
        self.assertEqual(
            detector._stabilize_boxes(*strip)[0][3] - 0, 68
        )

        recovered = None
        for _frame in range(detector.box_history_len + 3):
            recovered = detector._stabilize_boxes(*full)
        self.assertEqual(recovered[0], (0, 0, 239, 184))
        # And the marker (y=107) is inside the recovered analysis box.
        self.assertLessEqual(recovered[1][1], 107)
        self.assertGreaterEqual(recovered[1][3], 107)

    def test_persistent_strip_cannot_shrink_established_full_baseline(self) -> None:
        """A repeated same-width strip must never shrink a full baseline."""

        strip = ((0, 0, 239, 68), (0, 21, 239, 68), (2, 25, 237, 64))
        full = ((0, 0, 239, 184), (0, 57, 239, 184), (2, 78, 237, 178))
        detector = MinimapDetector(
            dedicated_crop=True, opencv_size=(400, 400)
        )
        detector.reset_geometry()
        detector._stabilize_boxes(*full)
        detector._stabilize_boxes(*full)
        detector._stabilize_boxes(*full)

        for _frame in range(detector.box_history_len + 3):
            kept = detector._stabilize_boxes(*strip)
        self.assertEqual(kept[0], (0, 0, 239, 184))

    def test_legitimate_resize_is_adopted_after_consistent_window(self) -> None:
        """A real minimap size change (both dimensions) is adopted.

        The strip rejection must not freeze a session when the minimap
        legitimately changes size (window resize, HUD scale, map switch):
        once the SAME new box repeats for a full history window it becomes
        the new baseline, in either direction.
        """

        full = ((0, 0, 239, 184), (0, 57, 239, 184), (2, 78, 237, 178))
        shrunk = ((0, 0, 130, 100), (0, 31, 130, 100), (2, 42, 128, 96))
        detector = MinimapDetector(
            dedicated_crop=True, opencv_size=(400, 400)
        )
        detector.reset_geometry()
        for _frame in range(3):
            detector._stabilize_boxes(*full)

        adopted = None
        for _frame in range(detector.box_history_len + 3):
            adopted = detector._stabilize_boxes(*shrunk)
        self.assertEqual(adopted[0], (0, 0, 130, 100))

        # And the reverse: a grow back to the larger border is adopted too.
        detector2 = MinimapDetector(
            dedicated_crop=True, opencv_size=(400, 400)
        )
        detector2.reset_geometry()
        for _frame in range(3):
            detector2._stabilize_boxes(*shrunk)
        for _frame in range(detector2.box_history_len + 3):
            adopted = detector2._stabilize_boxes(*full)
        self.assertEqual(adopted[0], (0, 0, 239, 184))

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
