# -*- coding: utf-8 -*-
"""Marker colour-family tests: the player's yellow diamond vs other markers.

Field bug this file locks down: a recording locked onto a TEAMMATE's diamond.
Its saturated core was rejected by the old gate, but its anti-aliased/highlight
pixels - and the washed-out (238,238,170), whose hue equals the player's own
hue 60 - passed it, so every recorded point came back as the same static
minimap pixel (x=0.800000 y=0.300000 in the field log) while the character
moved.

Every colour below is measured from the operator's captures:
  screenshots/1.png              (286x300 minimap crop with a teammate)
  screenshots/frame_000010.png   (full 1080x768 client frame)
"""

import unittest

import numpy as np

from marker_detector import (
    YELLOW_BODY_BLUE_CEILING,
    YELLOW_BODY_GREEN_FLOOR,
    YELLOW_BODY_RED_FLOOR,
    YELLOW_CORE_BLUE_CEILING,
    YELLOW_CORE_GREEN_FLOOR,
    YELLOW_CORE_RED_FLOOR,
    YELLOW_MIN_SOLID_CORE_SPAN,
    _solid_core_mask,
    _yellow_family_mask,
    detect_yellow_diamond,
)

# Player's own diamond (both captures + previously documented shades).
PLAYER_CORE_MEASURED = (255, 255, 0)        # hue 60, saturation 1.00
PLAYER_CORE_DOCUMENTED = (255, 255, 136)    # hue 60, saturation 0.47
PLAYER_BODY_MEASURED = (255, 204, 0)        # hue 48, saturation 1.00
PLAYER_EDGE_DOCUMENTED = (214, 200, 0)      # hue 56, saturation 1.00
PLAYER_EDGE_MEASURED = (211, 197, 7)        # single anti-aliased core pixel

# Teammate's diamond (orange family) and its blends.
TEAMMATE_CORE_MEASURED = (221, 153, 17)     # hue 40, saturation 0.92
TEAMMATE_BLENDS = (
    (238, 221, 187),                        # hue 40, saturation 0.21
    (238, 221, 170),                        # hue 45, saturation 0.29
    (238, 238, 170),                        # hue 60 (!), saturation 0.29
    (221, 153, 34),                         # hue 38
    (204, 153, 34),                         # hue 42
    (255, 143, 0),                          # the #ff8f00 core itself
)

# Pale minimap decoration from the same frame: warm but never a marker.
MAP_ART_PALE = (
    (204, 187, 170), (187, 170, 153), (200, 191, 191), (200, 200, 200),
)


def _canvas(width=80, height=70, colour=(0, 0, 0)):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :] = colour
    return frame


def _diamond(frame, cx, cy, colour, radius=2):
    """Draw a compact diamond (the marker's own shape)."""

    for y in range(-radius, radius + 1):
        for x in range(-radius, radius + 1):
            if abs(x) + abs(y) <= radius:
                frame[cy + y, cx + x] = colour


def _family(frame, *, core=True):
    rgb = frame.astype(np.int16)
    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    if core:
        return _yellow_family_mask(
            red, green, blue,
            min_red=YELLOW_CORE_RED_FLOOR,
            min_green=YELLOW_CORE_GREEN_FLOOR,
            max_blue=YELLOW_CORE_BLUE_CEILING,
        )
    return _yellow_family_mask(
        red, green, blue,
        min_red=YELLOW_BODY_RED_FLOOR,
        min_green=YELLOW_BODY_GREEN_FLOOR,
        max_blue=YELLOW_BODY_BLUE_CEILING,
    )


class PlayerDiamondColourTests(unittest.TestCase):
    def test_every_measured_player_core_colour_is_detected(self):
        for colour in (
            PLAYER_CORE_MEASURED, PLAYER_CORE_DOCUMENTED,
            PLAYER_BODY_MEASURED, PLAYER_EDGE_DOCUMENTED,
        ):
            with self.subTest(colour=colour):
                frame = _canvas()
                _diamond(frame, 40, 35, colour)
                marker = detect_yellow_diamond(frame)
                self.assertIsNotNone(marker, colour)
                self.assertAlmostEqual(marker.x, 0.5, delta=0.02)
                self.assertAlmostEqual(marker.y, 0.5, delta=0.02)

    def test_measured_player_edge_pixel_joins_the_core_family(self):
        # (211,197,7) is one anti-aliased edge pixel of the real marker.  The
        # old green floor (198) rejected it, which removed the pixel that
        # completed the solid 3x3 core and made the whole marker disappear for
        # three frames at a time ("openCV keeps missing detection").  The
        # widened floors admit it - and only it: every teammate shade stays out.
        frame = _canvas()
        frame[10, 10] = PLAYER_EDGE_MEASURED
        self.assertTrue(bool(_family(frame)[10, 10]))
        self.assertTrue(bool(_family(frame, core=False)[10, 10]))

    def test_widened_floors_still_reject_the_near_miss_colours(self):
        # Each of these is one small step away from an accepted player shade
        # and must stay out of the family.
        for colour in (
            (221, 153, 17),    # teammate core: green 153 < 190
            (238, 221, 170),   # teammate blend: hue 45 and saturation 0.29
            (238, 238, 170),   # hue 60 (player!) but saturation 0.29
            (204, 153, 34),
            (204, 187, 170),
        ):
            with self.subTest(colour=colour):
                frame = _canvas()
                frame[5, 5] = colour
                self.assertFalse(bool(_family(frame)[5, 5]), colour)
                self.assertFalse(bool(_family(frame, core=False)[5, 5]), colour)


class TeammateDiamondColourTests(unittest.TestCase):
    def test_every_measured_teammate_colour_is_rejected(self):
        for colour in (TEAMMATE_CORE_MEASURED,) + TEAMMATE_BLENDS:
            with self.subTest(colour=colour):
                frame = _canvas()
                _diamond(frame, 64, 21, colour, radius=3)
                self.assertIsNone(detect_yellow_diamond(frame), colour)
                # ... and it must not join the player's measured box either.
                self.assertFalse(bool(_family(frame, core=False).any()), colour)

    def test_pale_map_art_is_never_a_marker(self):
        for colour in MAP_ART_PALE:
            with self.subTest(colour=colour):
                frame = _canvas()
                _diamond(frame, 30, 30, colour, radius=4)
                self.assertIsNone(detect_yellow_diamond(frame), colour)

    def test_teammate_next_to_the_player_never_wins(self):
        # The reported scene: the teammate stands at a fixed minimap pixel
        # (64, 21) of an 80x70 analysis box while the character is elsewhere.
        frame = _canvas()
        _diamond(frame, 64, 21, TEAMMATE_CORE_MEASURED, radius=3)
        _diamond(frame, 64, 21, TEAMMATE_BLENDS[1], radius=2)
        _diamond(frame, 20, 50, PLAYER_CORE_MEASURED)
        marker = detect_yellow_diamond(frame)
        self.assertIsNotNone(marker)
        self.assertAlmostEqual(marker.x, 20 / 80.0, delta=0.02)
        self.assertAlmostEqual(marker.y, 50 / 70.0, delta=0.02)

    def test_teammate_only_scene_yields_no_marker_at_all(self):
        # Without a player marker the detector must report nothing, so the
        # recording is rejected instead of saving the teammate's position.
        frame = _canvas()
        _diamond(frame, 64, 21, TEAMMATE_CORE_MEASURED, radius=3)
        for index, colour in enumerate(TEAMMATE_BLENDS):
            _diamond(frame, 10 + index * 8, 40, colour)
        self.assertIsNone(detect_yellow_diamond(frame))


class SolidCoreShapeTests(unittest.TestCase):
    """The marker is a solid block: all 9 pixels of its 3x3 core are yellow."""

    # Verbatim pixel grid measured in the operator's live 1080x768 frame
    # (minimap box (7,56)-(87,126)): a 6x6 rotated diamond whose centre is a
    # fully filled 3x3 block.  C = core colour family, b = body family only,
    # . = minimap background inside the marker's bounding box.
    MEASURED_GRID = (
        "....bC",
        ".CCCC.",
        "CCCCCb",
        "CCCCCC",
        ".CCCb.",
        "..CC..",
    )

    @classmethod
    def _measured_marker(cls, frame, left=27, top=24):
        core_shades = [PLAYER_CORE_MEASURED] * 12 + [PLAYER_CORE_DOCUMENTED] * 4
        core_index = 0
        for row, line in enumerate(cls.MEASURED_GRID):
            for col, token in enumerate(line):
                if token == "C":
                    shade = core_shades[core_index % len(core_shades)]
                    core_index += 1
                    frame[top + row, left + col] = shade
                elif token == "b":
                    frame[top + row, left + col] = PLAYER_EDGE_MEASURED
                else:
                    frame[top + row, left + col] = (35, 35, 35)
        return frame

    def test_measured_live_frame_marker_is_detected(self):
        frame = _canvas()
        frame[:, :] = (120, 120, 120)
        marker = detect_yellow_diamond(self._measured_marker(frame))
        self.assertIsNotNone(marker)
        # The detector reported x=0.366667 y=0.379592 box=(27,24,33,30) on the
        # real frame; the replayed grid must land on the same spot.
        self.assertAlmostEqual(marker.x, 0.3667, delta=0.02)
        self.assertAlmostEqual(marker.y, 0.3796, delta=0.02)
        self.assertEqual(marker.pixel_size, (6, 6))

    def test_measured_frame_has_solid_core_margin(self):
        # Robustness margin of the widening: the replayed live marker must
        # offer more than one 3x3 window that satisfies the core family, so a
        # frame with one degraded edge pixel still yields a marker instead of
        # three consecutive misses.
        frame = _canvas()
        frame[:, :] = (120, 120, 120)
        marker_frame = self._measured_marker(frame)
        solid = _solid_core_mask(_family(marker_frame), YELLOW_MIN_SOLID_CORE_SPAN)
        self.assertGreaterEqual(int(solid.sum()), 2)

    def test_sparse_rhombus_is_no_longer_a_marker(self):
        # A 3x3 rhombus (5 pixels) is what an anti-aliased map icon or a
        # downscaled marker looks like: it has no fully filled 3x3 core.
        frame = _canvas()
        _diamond(frame, 40, 35, PLAYER_CORE_MEASURED, radius=1)
        self.assertIsNone(detect_yellow_diamond(frame))

    def test_thin_yellow_run_is_no_longer_a_marker(self):
        frame = _canvas()
        frame[30, 10:60] = PLAYER_CORE_MEASURED
        frame[31, 10:60] = PLAYER_CORE_MEASURED
        self.assertIsNone(detect_yellow_diamond(frame))

    def test_solid_block_without_a_full_core_is_not_a_marker(self):
        # A 3x3 block missing one corner pixel cannot fill a 3x3 core.
        frame = _canvas()
        frame[34:37, 39:42] = PLAYER_CORE_MEASURED
        frame[34, 39] = (0, 0, 0)
        # The remaining L-shape still has a 2x2, but no 3x3: rejected only if
        # the hole breaks every 3x3 window, so check the exact outcome.
        marker = detect_yellow_diamond(frame)
        self.assertIsNone(marker)

    def test_solid_three_by_three_block_is_a_marker(self):
        frame = _canvas()
        frame[34:37, 39:42] = PLAYER_BODY_MEASURED
        marker = detect_yellow_diamond(frame)
        self.assertIsNotNone(marker)
        self.assertEqual(marker.pixel_size, (3, 3))


class RealFrameSceneTests(unittest.TestCase):
    """Replays the blobs measured inside the real 1080x768 client frame."""

    @staticmethod
    def _scene():
        frame = _canvas()
        # Player's own 6x6 diamond (core + documented inner shade + one edge
        # pixel), exactly as measured at box=(27,24,33,30) of the minimap.
        _diamond(frame, 30, 27, PLAYER_CORE_MEASURED, radius=2)
        for x, y in ((28, 24), (29, 24), (31, 25), (32, 26)):
            frame[y, x] = PLAYER_CORE_DOCUMENTED
        frame[30, 26] = PLAYER_EDGE_MEASURED
        # Pale decoration rows from the same frame.
        frame[12, 39:55] = (204, 187, 170)
        frame[38, 14:29] = (187, 170, 153)
        frame[25:36, 0] = (200, 191, 191)
        frame[50, 64:67] = (187, 170, 170)
        return frame

    def test_detector_picks_the_real_player_diamond(self):
        marker = detect_yellow_diamond(self._scene())
        self.assertIsNotNone(marker)
        self.assertAlmostEqual(marker.x, 30 / 80.0, delta=0.03)
        self.assertAlmostEqual(marker.y, 27 / 70.0, delta=0.03)
        self.assertGreaterEqual(min(marker.pixel_size), 4)

    def test_teammate_over_the_same_scene_still_loses(self):
        frame = self._scene()
        # Add the teammate's blob on top of the measured frame.
        frame[3:12, 61:90] = (221, 153, 17)
        frame[4:11, 62:89] = (238, 221, 170)
        marker = detect_yellow_diamond(frame)
        self.assertIsNotNone(marker)
        self.assertAlmostEqual(marker.x, 30 / 80.0, delta=0.03)


if __name__ == "__main__":
    unittest.main()
