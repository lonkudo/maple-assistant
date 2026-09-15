# -*- coding: utf-8 -*-
"""The tracking test must actually drive the cursor.

A controller created while another one already owns the cursor exclusively
(an auto-lie pass, a second viewer window) starts DISABLED by design - see
``MouseAimController.__init__``, which honours ``_register()``'s answer.  The
tracking test therefore tracked the target perfectly while the mouse never
moved at all: it now claims the cursor (and enables itself) for its lifetime.

These tests use registry fakes only: no threads and no real cursor input.
"""

import sys
import unittest
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).resolve().parent
for _path in (str(ROOT / "target_tracker"), str(ROOT / "target_tracker" / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from mouse_aim_controller import (  # noqa: E402
    MouseAimController,
    _register,
    _unregister,
    claim_cursor,
    release_cursor,
    suspend_others_except,
    widget_image_region,
)


class _FakeLabel:
    """A Tk Label stand-in: geometry plus Tk's image anchor behaviour.

    Tk draws a Label's image centred in the label, and clips whatever does not
    fit - it does not start at the label's top-left corner.  Measured on the
    test window: a 1366x768 video inside a 1084x744 label is drawn from
    (label_x - 141, label_y - 12).
    """

    def __init__(self, x, y, width, height, mapped=True):
        self._x, self._y = x, y
        self._width, self._height = width, height
        self._mapped = mapped

    def winfo_ismapped(self):
        return self._mapped

    def winfo_rootx(self):
        return self._x

    def winfo_rooty(self):
        return self._y

    def winfo_width(self):
        return self._width

    def winfo_height(self):
        return self._height


class ImageRegionTests(unittest.TestCase):
    def test_image_smaller_than_label_is_centred(self):
        label = _FakeLabel(100, 50, 800, 600)
        self.assertEqual(widget_image_region(label, 640, 480), (180, 110, 820, 590))

    def test_oversized_image_keeps_the_negative_centring_offset(self):
        # The measured case: the cursor used to be sent 141px right / 12px below
        # the drawn picture because that offset was clamped to 0.
        label = _FakeLabel(136, 159, 1084, 744)
        region = widget_image_region(label, 1366, 768)
        self.assertIsNotNone(region)
        left, top, right, bottom = region
        self.assertEqual((left, top), (136 - 141, 159 - 12))
        self.assertEqual((right - left, bottom - top), (1366, 768))

    def test_a_point_maps_onto_the_pixels_it_is_drawn_at(self):
        # Widget maths and the drawn picture must agree, which is what the
        # operator sees as "the cursor is on the crosshair".
        label = _FakeLabel(136, 159, 1084, 744)
        left, top, right, bottom = widget_image_region(label, 1366, 768)
        for x, y in ((300, 200), (0, 0), (1365, 767)):
            drawn_x = label.winfo_rootx() + (label.winfo_width() - 1366) // 2 + x
            drawn_y = label.winfo_rooty() + (label.winfo_height() - 768) // 2 + y
            mapped_x = left + (x / 1366) * (right - left)
            mapped_y = top + (y / 768) * (bottom - top)
            self.assertAlmostEqual(mapped_x, drawn_x, places=6)
            self.assertAlmostEqual(mapped_y, drawn_y, places=6)

    def test_unmapped_label_has_no_region(self):
        self.assertIsNone(widget_image_region(_FakeLabel(0, 0, 800, 600, mapped=False), 640, 480))

    def test_degenerate_sizes_have_no_region(self):
        label = _FakeLabel(0, 0, 800, 600)
        self.assertIsNone(widget_image_region(label, 0, 480))
        self.assertIsNone(widget_image_region(_FakeLabel(0, 0, 1, 1), 640, 480))


class _FakeController:
    """Stand-in for a live controller, registered like the real one."""

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = bool(enabled)
        self._suppressed_by_exclusive = False
        # A controller outside the registry is invisible to
        # suspend_others_except, so the fake joins it exactly like a real one.
        self.start_disabled = bool(_register(self))
        if self.start_disabled:
            # Exactly what MouseAimController.__init__ does with that answer.
            self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool, *, silent: bool = False) -> None:
        self._enabled = bool(enabled)

    def close(self) -> None:
        _unregister(self)


class RegionClampTests(unittest.TestCase):
    """A point on the tracked area's border must not park the cursor ON the edge.

    The operator's report: "the aim is still trapped at edge".  A tracker point that
    saturates on the crop's own border maps onto the region's border pixel, so the
    cursor came to rest exactly on the drawn edge of the area the pass feeds.
    """

    def test_a_border_point_lands_just_inside_the_region(self) -> None:
        controller = MouseAimController.__new__(MouseAimController)
        controller._use_sendinput = False
        controller._lock = Lock()
        controller._region = (100, 50, 800, 500)
        # The crop's own border maps to the region's border pixel...
        self.assertEqual(controller._clamp_to_screen(799.0, 499.0), (797.0, 497.0))
        self.assertEqual(controller._clamp_to_screen(100.0, 50.0), (102.0, 52.0))
        # ... and an ordinary interior point is untouched
        self.assertEqual(controller._clamp_to_screen(400.0, 300.0), (400.0, 300.0))

    def test_a_region_smaller_than_the_margin_still_clamps(self) -> None:
        controller = MouseAimController.__new__(MouseAimController)
        controller._use_sendinput = False
        controller._lock = Lock()
        controller._region = (10, 10, 13, 13)
        self.assertEqual(controller._clamp_to_screen(99.0, 99.0), (12.0, 12.0))


class CursorOwnershipTests(unittest.TestCase):
    def _owner(self) -> _FakeController:
        # The current exclusive owner is a RUNNING driver (that is what
        # suspend_others_except pauses), so it starts enabled.
        owner = _FakeController(enabled=True)
        self.addCleanup(owner.close)
        suspend_others_except(owner)
        return owner

    def test_controller_created_while_another_owns_the_cursor_is_disabled(self):
        self._owner()
        late = _FakeController()
        self.addCleanup(late.close)
        self.assertTrue(late.start_disabled, "the trap this fix removes")
        self.assertFalse(late.enabled, "created-disabled means no mouse movement")

    def test_claim_cursor_never_steals_the_cursor_from_a_running_sequence(self):
        # The automatic pass confines the cursor to the area it feeds; a viewer
        # window would drive it over its whole video, i.e. outside that area and onto
        # its border - the operator's "the mouse and the aim go outside the feeding
        # area" report.  The pass therefore keeps the cursor while it runs, and the
        # window opened meanwhile waits for it to finish.
        owner = self._owner()
        controller = _FakeController()
        self.addCleanup(controller.close)
        self.assertFalse(controller.enabled)

        suspended = claim_cursor(controller)
        self.assertEqual(suspended, [])
        self.assertFalse(controller.enabled, "the pass keeps the cursor")
        self.assertTrue(owner.enabled, "the pass must not be paused by a viewer")

    def test_claim_cursor_enables_the_window_when_nothing_owns_the_cursor(self):
        # ... and a window opened while no sequence runs drives the mouse as before.
        owner = self._owner()
        controller = _FakeController()
        self.addCleanup(controller.close)
        owner.close()                      # the sequence ended and released it
        claim_cursor(controller)
        self.assertTrue(controller.enabled, "the test window must drive the mouse")

    def test_claim_cursor_with_a_free_cursor_just_enables_it(self):
        controller = _FakeController()
        self.addCleanup(controller.close)
        claim_cursor(controller)
        self.assertTrue(controller.enabled)


if __name__ == "__main__":
    unittest.main()
