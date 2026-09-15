# -*- coding: utf-8 -*-
"""Step 1 of the API lie pass: screenshot -> protocol frame payload -> answer mapped back.

The geometry under test is the operator's measurement: at a 1366x768 client the precise lie ROI
is (310, 118) with a 745x496 box, i.e. the popup preset (299, 85, 767, 598) inset by 11/11/33/69.
The protocol ROI is 372x248, so one slot pixel is 2.0027 x 2.0 client pixels - and the server's
own lie3main space (745x496) is the crop itself, 1:1.
"""

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402

from autolie_api.intergration import (  # noqa: E402
    ASPECT_TOLERANCE,
    COORD_SPACE_MAIN,
    JPEG_QUALITY,
    LIE3MAIN_SIZE,
    MAX_FRAME_B64_CHARS,
    MAX_FRAME_BYTES,
    SLOT_H,
    SLOT_W,
    RoiGeometry,
    build_slot_frame,
    check_caps,
    crop_region,
    fit_slot,
    lie_roi_box,
    parse_server_point,
    resize_to_slot,
)

CLIENT = (1366, 768)
BOX = (310, 118, 745, 496)
# a client that sits at (528, 255) on the desktop, as in the field log
WINDOW_RECT = (528, 255, 528 + CLIENT[0], 255 + CLIENT[1])


def client_frame() -> np.ndarray:
    """A synthetic 1366x768 game client."""

    frame = np.full((CLIENT[1], CLIENT[0], 3), 32, dtype=np.uint8)
    frame[100:700, 200:1200] = (60, 70, 90)
    return frame


class RoiBoxTests(unittest.TestCase):
    """The measured crop box, and how it follows the game-UI preset."""

    def test_the_reference_client_gives_the_measured_box(self):
        self.assertEqual(lie_roi_box(1366, 768), (310, 118, 745, 496))
        self.assertEqual(lie_roi_box(1920, 1080), (587, 274, 745, 496),
                         "the popup preset is the same size, only its centre moves")
        self.assertEqual(lie_roi_box(*CLIENT)[2:], LIE3MAIN_SIZE,
                         "the crop IS the server's lie3main space")

    def test_the_box_keeps_the_protocol_aspect(self):
        for size in ((1366, 768), (1920, 1080), (1080, 768), (1280, 720)):
            _left, _top, width, height = lie_roi_box(*size)
            aspect = width / height
            self.assertLess(abs(aspect - SLOT_W / SLOT_H), ASPECT_TOLERANCE,
                            f"{size} crop aspect {aspect:.4f} would distort the ROI")


class CropTests(unittest.TestCase):
    def test_the_crop_is_exactly_the_box(self):
        frame = client_frame()
        frame[400, 700] = (255, 255, 255)
        crop, box = crop_region(frame)
        self.assertEqual(box, BOX)
        self.assertEqual(crop.shape[:2], (496, 745))
        self.assertTrue((crop == frame[118:614, 310:1055]).all(),
                        "the crop must be the ROI itself, not a shifted window")
        self.assertTrue((crop[400 - 118, 700 - 310] == (255, 255, 255)).all())

    def test_an_explicit_box_is_used_and_clamped(self):
        frame = client_frame()
        _, box = crop_region(frame, (0, 0, 400, 300))
        self.assertEqual(box, (0, 0, 400, 300))
        _, clamped = crop_region(frame, (1300, 700, 400, 300))
        self.assertEqual(clamped, (1300, 700, 66, 68))

    def test_an_empty_screenshot_is_refused(self):
        with self.assertRaises(ValueError):
            crop_region(np.zeros((0, 0, 3), dtype=np.uint8))


class SlotEncodingTests(unittest.TestCase):
    """372x248, 3 channels, JPEG quality 90, inside the protocol caps."""

    def setUp(self):
        self.frame = build_slot_frame(client_frame(), frame_id=3, interval_sec=0.2)

    def test_the_slot_has_the_protocol_shape_and_a_real_jpeg(self):
        decoded = cv2.imdecode(np.frombuffer(self.frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape, (SLOT_H, SLOT_W, 3))
        self.assertEqual(self.frame.jpeg[:2], b"\xff\xd8")
        self.assertEqual(self.frame.jpeg[-2:], b"\xff\xd9")

    def test_the_base64_is_the_same_bytes(self):
        import base64

        self.assertEqual(base64.b64decode(self.frame.base64_text), self.frame.jpeg)
        self.assertEqual(self.frame.base64_chars, 4 * -(-len(self.frame.jpeg) // 3))

    def test_the_payload_is_the_270_base64_frame_message(self):
        payload = self.frame.payload()
        self.assertEqual(payload["type"], "frame")
        self.assertEqual(payload["frame_id"], 3)
        self.assertEqual(payload["image"], self.frame.base64_text)
        self.assertAlmostEqual(payload["frame_interval_sec"], 0.2)

    def test_the_sent_frame_is_saved_as_frame_id_jpg(self):
        """2.7.0 §3: frame_id starts at 1 per round and increases by one - the dump is named
        after it, and holds the exact bytes that went to the server."""

        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            first = build_slot_frame(client_frame(), frame_id=1)
            second = build_slot_frame(client_frame(), frame_id=2)
            paths = [first.save(Path(folder)), second.save(Path(folder))]
            self.assertEqual([p.name for p in paths], ["1.jpg", "2.jpg"])
            for path, frame in zip(paths, (first, second)):
                self.assertEqual(path.read_bytes(), frame.jpeg)
                self.assertEqual(path.read_bytes()[:2], b"\xff\xd8")
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()),
                             ["1.jpg", "2.jpg"])

    def test_quality_90_stays_inside_the_caps_and_is_the_sweet_spot(self):
        ok, reason = check_caps(self.frame)
        self.assertTrue(ok, reason)
        self.assertLess(self.frame.byte_size, MAX_FRAME_BYTES)
        self.assertLess(self.frame.base64_chars, MAX_FRAME_B64_CHARS)
        # the same slot at quality 100 (OpenCV's default) does NOT fit - that is why the
        # protocol says 90
        noisy = np.random.default_rng(7).integers(
            0, 256, size=(SLOT_H, SLOT_W, 3), dtype=np.uint8)
        from autolie_api.intergration import encode_slot_jpeg

        best = encode_slot_jpeg(noisy, 100)
        good = encode_slot_jpeg(noisy, JPEG_QUALITY)
        self.assertGreater(len(best), len(good))

    def test_a_gray_or_wrongly_sized_slot_is_refused(self):
        from autolie_api.intergration import encode_slot_jpeg

        with self.assertRaises(ValueError):
            encode_slot_jpeg(np.zeros((SLOT_H, SLOT_W), dtype=np.uint8))
        with self.assertRaises(ValueError):
            encode_slot_jpeg(np.zeros((SLOT_H, SLOT_W - 1, 3), dtype=np.uint8))


class MappingTests(unittest.TestCase):
    """server (372x248) -> crop -> client -> screen, and back."""

    def setUp(self):
        self.frame = build_slot_frame(client_frame(), window_rect=WINDOW_RECT)
        self.geometry = self.frame.geometry

    def test_the_scales_are_the_measured_ones(self):
        self.assertAlmostEqual(self.geometry.scale_x, 745 / 372, places=6)
        self.assertAlmostEqual(self.geometry.scale_y, 2.0, places=6)
        self.assertAlmostEqual(self.geometry.main_scale_x, 1.0, places=6)

    def test_a_slot_point_lands_on_the_expected_screen_pixel(self):
        self.assertEqual(self.geometry.slot_to_crop_point(0, 0), (0.0, 0.0))
        self.assertEqual(self.geometry.slot_to_screen_point(0, 0), (838.0, 373.0))
        centre = self.geometry.slot_to_screen_point(SLOT_W / 2, SLOT_H / 2)
        self.assertAlmostEqual(centre[0], 528 + 310 + 745 / 2, places=3)
        self.assertAlmostEqual(centre[1], 255 + 118 + 496 / 2, places=3)

    def test_the_marker_round_trips_through_slot_and_back(self):
        marker = (700, 400)                              # a client pixel
        slot_x, slot_y = self.geometry.crop_to_slot_point(marker[0] - BOX[0],
                                                          marker[1] - BOX[1])
        self.assertAlmostEqual(slot_x, (700 - 310) / (745 / 372), places=3)
        back = self.geometry.slot_to_client_point(slot_x, slot_y)
        self.assertAlmostEqual(back[0], marker[0], delta=1e-6)
        self.assertAlmostEqual(back[1], marker[1], delta=1e-6)
        screen = self.geometry.slot_to_screen_point(slot_x, slot_y)
        again = self.geometry.screen_to_slot_point(*screen)
        self.assertAlmostEqual(again[0], slot_x, places=6)
        self.assertAlmostEqual(again[1], slot_y, places=6)

    def test_one_slot_pixel_is_about_two_client_pixels(self):
        first = self.geometry.slot_to_client_point(100, 100)
        second = self.geometry.slot_to_client_point(101, 101)
        self.assertAlmostEqual(second[0] - first[0], 2.0027, places=3)
        self.assertAlmostEqual(second[1] - first[1], 2.0, places=6)

    def test_the_aim_region_is_the_crops_screen_rectangle(self):
        self.assertEqual(self.geometry.crop_screen_rect(),
                         (528 + 310, 255 + 118, 528 + 310 + 745, 255 + 118 + 496))

    def test_main_space_maps_one_to_one_on_the_measured_crop(self):
        slot = self.geometry.main_to_crop_point(372.5, 248.0)
        self.assertAlmostEqual(slot[0], 372.5, places=6)
        self.assertAlmostEqual(slot[1], 248.0, places=6)

    def test_a_2to1_crop_is_centre_fitted_instead_of_stretched(self):
        wide = np.zeros((300, 600, 3), dtype=np.uint8)
        self.assertEqual(fit_slot(wide), (75, 0, 450, 300))
        tall = np.zeros((600, 300, 3), dtype=np.uint8)
        self.assertEqual(fit_slot(tall), (0, 200, 300, 200))
        # the measured crop is used whole
        self.assertEqual(fit_slot(np.zeros((496, 745, 3), dtype=np.uint8)), (0, 0, 745, 496))

    def test_a_centre_fitted_crop_still_maps_exactly(self):
        geometry = RoiGeometry(box=(0, 0, 600, 300), client_size=(600, 300),
                               window_rect=(0, 0, 600, 300),
                               content_box=(75, 0, 450, 300))
        self.assertEqual(geometry.slot_to_crop_point(0, 0), (75.0, 0.0))
        self.assertAlmostEqual(geometry.slot_to_crop_point(SLOT_W, SLOT_H)[0], 525.0)

    def test_resize_is_the_protocol_size(self):
        self.assertEqual(resize_to_slot(np.zeros((496, 745, 3), dtype=np.uint8)).shape,
                         (SLOT_H, SLOT_W, 3))


class ServerPointTests(unittest.TestCase):
    """The JSON the server answers with."""

    RESULTS = {
        "type": "frame_result", "frame_id": 7, "success": True, "mode": "infer",
        "x": 186.0, "y": 124.0, "x_main": 372.0, "y_main": 248.0,
        "main_wh": [745, 496], "decision": "track", "onnx_count": 14, "parsed_count": 12,
        "api_ok": True, "half_mode": True, "timing_ms": 25.3, "frame_standard": 7,
        "queue_depth": 0, "frames_in_round": 1, "quota_left": 98, "seconds_left": 55.2,
        "round_active": True,
    }

    def test_a_result_is_parsed_and_mapped_to_the_screen(self):
        point = parse_server_point(self.RESULTS)
        self.assertEqual(point.frame_id, 7)
        self.assertTrue(point.success)
        self.assertEqual(point.slot_xy(), (186.0, 124.0))
        self.assertEqual(point.decision, "track")
        self.assertAlmostEqual(point.timing_ms, 25.3)
        self.assertEqual(point.quota_left, 98)
        self.assertFalse(point.is_hold())
        self.assertTrue(point.within_slot())
        geometry = RoiGeometry(box=BOX, client_size=CLIENT, window_rect=WINDOW_RECT)
        crop = point.to_crop(geometry)
        self.assertAlmostEqual(crop[0], 186.0 * 745 / 372, places=3)
        screen = point.to_screen(geometry)
        self.assertAlmostEqual(screen[0], 528 + 310 + crop[0], places=3)

    def test_hold_and_pending_decisions_are_recognised(self):
        for decision in ("hold_prev", "abandon_frame_hold", "freeze_search_only_static"):
            payload = dict(self.RESULTS, decision=decision)
            self.assertTrue(parse_server_point(payload).is_hold(), decision)
        self.assertTrue(parse_server_point(dict(self.RESULTS, success=False)).is_hold())

    def test_a_result_without_a_point_raises_only_when_asked(self):
        point = parse_server_point({"frame_id": 2, "success": False, "error": "round_inactive"})
        self.assertEqual(point.error, "round_inactive")
        self.assertFalse(point.within_slot())
        with self.assertRaises(ValueError):
            point.slot_xy()

    def test_the_two_coordinate_spaces_are_cross_checked(self):
        self.assertTrue(parse_server_point(self.RESULTS).cross_check_spaces())
        inconsistent = dict(self.RESULTS, x_main=900.0)
        self.assertFalse(parse_server_point(inconsistent).cross_check_spaces())
        # a result in only one space cannot be cross-checked, and that is not an error
        self.assertTrue(parse_server_point(
            {"frame_id": 1, "success": True, "x": 1.0, "y": 2.0}).cross_check_spaces())

    def test_main_space_can_be_requested_explicitly(self):
        point = parse_server_point(self.RESULTS)
        geometry = RoiGeometry(box=BOX, client_size=CLIENT, window_rect=WINDOW_RECT)
        self.assertEqual(point.to_crop(geometry, space=COORD_SPACE_MAIN), (372.0, 248.0))


class CapsTests(unittest.TestCase):
    def test_an_over_sized_frame_reports_why(self):
        from autolie_api.intergration import SlotFrame

        geometry = RoiGeometry(box=BOX, client_size=CLIENT, window_rect=WINDOW_RECT)
        frame = SlotFrame(jpeg=b"x" * (MAX_FRAME_BYTES + 1), base64_text="",
                          geometry=geometry, frame_id=1, interval_sec=0.2, encode_ms=0.0)
        ok, reason = check_caps(frame)
        self.assertFalse(ok)
        self.assertIn("max_frame_bytes", reason)


if __name__ == "__main__":
    unittest.main()
