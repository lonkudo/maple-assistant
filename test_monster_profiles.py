"""Tests for monster profile storage and HSV derivation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from monster_profiles import MonsterProfileStore, derive_hsv_bounds


class DeriveHsvBoundsTests(unittest.TestCase):
    def test_red_image_band_covers_hue_zero(self) -> None:
        image = Image.new("RGB", (60, 60), (230, 50, 50))
        lower, upper = derive_hsv_bounds(image)
        # Pure red sits at hue 0; a normal or wrapped band must reach it.
        covers_zero = lower[0] <= 0 <= upper[0] or (
            lower[0] > upper[0] and (0 >= lower[0] or 0 <= upper[0])
        )
        self.assertTrue(covers_zero, f"band {lower}..{upper} misses hue 0")
        self.assertGreaterEqual(lower[1], 40)
        self.assertGreaterEqual(lower[2], 40)

    def test_blue_image_band_is_around_blue_hue(self) -> None:
        image = Image.new("RGB", (60, 60), (40, 80, 220))
        lower, upper = derive_hsv_bounds(image)
        # Blue hue is around 120 (OpenCV units 60..135 depending on shade).
        self.assertLessEqual(lower[0], 130)
        self.assertGreaterEqual(upper[0], 70)

    def test_red_wraparound_band_covers_both_sides(self) -> None:
        # Left half pure red (hue ~0), right half magenta-leaning (hue ~178).
        image = Image.new("RGB", (120, 60), (230, 50, 50))
        draw = ImageDraw.Draw(image)
        draw.rectangle((60, 0, 119, 59), fill=(225, 45, 55))
        lower, upper = derive_hsv_bounds(image)
        hue_span = (upper[0] - lower[0]) % 180
        # Either a wide band or a wrapped band must cover both reds.
        self.assertTrue(
            hue_span >= 90 or lower[0] > upper[0],
            f"band {lower}..{upper} does not cover red wraparound",
        )

    def test_dark_image_falls_back_to_permissive_mask(self) -> None:
        image = Image.new("RGB", (40, 40), (8, 8, 10))
        lower, upper = derive_hsv_bounds(image)
        self.assertEqual(lower, (0, 80, 80))
        self.assertEqual(upper, (179, 255, 255))


class MonsterProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.store = MonsterProfileStore(self.root / "monsters")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_save_load_roundtrip(self) -> None:
        image = Image.new("RGB", (50, 50), (230, 50, 50))
        name, bounds = self.store.save("red slime", image)
        self.assertEqual(name, "red-slime")
        self.assertEqual(self.store.names(), ["red-slime"])
        loaded_image, loaded_bounds = self.store.load("red-slime")
        self.assertIsNotNone(loaded_image)
        self.assertEqual(loaded_bounds, bounds)
        self.assertEqual(loaded_image.size, (50, 50))

    def test_names_are_sorted_and_skip_broken_entries(self) -> None:
        self.store.save("bee", Image.new("RGB", (10, 10), (200, 160, 40)))
        self.store.save("ant", Image.new("RGB", (10, 10), (60, 40, 30)))
        # A PNG without its sidecar JSON is a broken entry.
        (self.root / "monsters" / "broken.png").write_bytes(b"x")
        self.assertEqual(self.store.names(), ["ant", "bee"])

    def test_sanitized_name(self) -> None:
        image = Image.new("RGB", (10, 10), (200, 160, 40))
        name, _bounds = self.store.save("  snail!!  ", image)
        self.assertEqual(name, "snail")
        self.assertIn("snail", self.store.names())

    def test_delete_removes_profile(self) -> None:
        self.store.save("bee", Image.new("RGB", (10, 10), (200, 160, 40)))
        self.assertTrue(self.store.delete("bee"))
        self.assertEqual(self.store.names(), [])
        self.assertFalse(self.store.delete("bee"))

    def test_hsv_bounds_persisted_in_sidecar(self) -> None:
        image = Image.new("RGB", (40, 40), (40, 80, 220))
        _name, bounds = self.store.save("blue", image)
        meta = json.loads(
            (self.root / "monsters" / "blue.json").read_text(encoding="utf-8")
        )
        self.assertEqual(tuple(meta["hsv_lower"]), bounds[0])
        self.assertEqual(tuple(meta["hsv_upper"]), bounds[1])


if __name__ == "__main__":
    unittest.main()
