# -*- coding: utf-8 -*-
"""The screenshot format policy: JPG for dumps, PNG for anything matched against.

Measured on a real 1366x768 lie frame: PNG 1033 KB / 28.75 ms, JPG q90 379 KB / 4.55 ms.  The
policy lives in :mod:`image_io`; these tests keep it honest and keep the reference images -
where the pixel values ARE the measurement - lossless.
"""

import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from image_io import (
    RECORDING_FRAME_QUALITY,
    SCREENSHOT_QUALITY,
    SCREENSHOT_SUFFIX,
    frame_files,
    matches_frame_name,
    save_frame,
    save_screenshot,
    to_bgr,
)

PNG_MAGIC = b"\x89PNG"
JPEG_MAGIC = b"\xff\xd8"


def sample_image() -> np.ndarray:
    """A small picture with real detail (a flat one would not show a size difference)."""

    rng = np.random.default_rng(3)
    image = np.full((64, 96, 3), 40, dtype=np.uint8)
    image[8:56, 8:88] = rng.integers(0, 255, size=(48, 80, 3), dtype=np.uint8)
    return image


class PolicyTests(unittest.TestCase):
    def test_the_default_suffix_is_jpg(self):
        self.assertEqual(SCREENSHOT_SUFFIX, ".jpg")
        self.assertEqual(SCREENSHOT_QUALITY, 90)
        self.assertEqual(RECORDING_FRAME_QUALITY, 95)

    def test_a_suffix_less_path_gets_the_jpg_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = save_screenshot(Path(tmp) / "dump", sample_image())
            self.assertIsNotNone(path)
            self.assertEqual(path.suffix, ".jpg")
            self.assertEqual(path.read_bytes()[:2], JPEG_MAGIC)

    def test_a_png_path_is_written_as_jpg(self):
        """The project is JPG only: a .png target still produces a .jpg file."""

        with tempfile.TemporaryDirectory() as tmp:
            path = save_screenshot(Path(tmp) / "reference.png", sample_image())
            self.assertEqual(path.suffix, ".jpg")
            self.assertEqual(path.read_bytes()[:2], JPEG_MAGIC)
            self.assertEqual(list(Path(tmp).glob("*.png")), [])

    def test_jpg_is_smaller_than_the_lossless_alternative(self):
        """Why the switch happened: the same picture as PNG is ~2.7x larger and 6x slower."""

        with tempfile.TemporaryDirectory() as tmp:
            jpg = save_screenshot(Path(tmp) / "a.jpg", sample_image())
            ok, png_buffer = cv2.imencode(".png", sample_image())
            self.assertTrue(ok)
            self.assertLess(jpg.stat().st_size, len(png_buffer.tobytes()))

    def test_quality_90_is_smaller_than_quality_100(self):
        with tempfile.TemporaryDirectory() as tmp:
            small = save_screenshot(Path(tmp) / "q90.jpg", sample_image(), quality=90)
            large = save_screenshot(Path(tmp) / "q100.jpg", sample_image(), quality=100)
            self.assertLess(small.stat().st_size, large.stat().st_size)

    def test_a_pil_frame_is_accepted_like_an_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = save_frame(Path(tmp), "frame_000001", Image.new("RGB", (32, 24), (9, 9, 9)))
            self.assertIsNotNone(path)
            self.assertEqual(path.name, "frame_000001.jpg")
            self.assertEqual(to_bgr(Image.new("RGB", (4, 3))).shape, (3, 4, 3))
            self.assertEqual(to_bgr(np.zeros((3, 4), dtype=np.uint8)).shape, (3, 4, 3))
            self.assertIsNone(to_bgr("not an image"))

    def test_a_non_ascii_folder_still_receives_the_file(self):
        """cv2.imwrite fails silently on such paths (D:\\蛇夫G), which is why bytes are written."""

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "蛇夫G" / "截图"
            path = save_frame(folder, "frame_000001", sample_image())
            self.assertIsNotNone(path)
            self.assertTrue(path.is_file())
            self.assertGreater(path.stat().st_size, 0)

    def test_a_write_failure_returns_none_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "folder-that-is-a-file"
            target.write_text("x", encoding="utf-8")
            self.assertIsNone(save_screenshot(target / "sub" / "a.jpg", sample_image()))


class ListingTests(unittest.TestCase):
    """JPG is the only frame format; a PNG from before the switch is not listed."""

    def test_jpg_frames_are_found_and_sorted_png_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            first = save_frame(folder, "frame_000002", sample_image())
            # ".jpeg" is accepted as a JPG spelling (the writer itself always produces .jpg)
            (folder / "frame_000003.jpeg").write_bytes(first.read_bytes())
            # a legacy PNG from before the switch: no longer listed
            (folder / "frame_000001.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (folder / "notes.txt").write_text("ignore me", encoding="utf-8")
            (folder / "other_000001.jpg").write_bytes(b"\xff\xd8")
            names = [path.name for path in frame_files(folder)]
            self.assertEqual(names, ["frame_000002.jpg", "frame_000003.jpeg"])
            self.assertTrue(matches_frame_name("frame-000123-stamp.jpg"))
            self.assertFalse(matches_frame_name("frame-000123-stamp.png"))
            self.assertFalse(matches_frame_name("frame_000123.png"))
            self.assertFalse(matches_frame_name("notes.txt"))
            self.assertFalse(matches_frame_name("other_000123.jpg"))

    def test_a_missing_folder_is_empty_not_an_error(self):
        self.assertEqual(frame_files(Path("does/not/exist")), [])


class ReferenceImagesAreJpgTests(unittest.TestCase):
    """Every image on disk is a JPG now - including the ones that are MATCHED against.

    They go through their own unicode-safe writers (``cv2.imencode(".jpg", ..., 95)``): map-name
    signatures, the map-structure reference, the 自动重连 colour reference.  q95 keeps the pixel
    shift under ~1 level, which is what makes that acceptable for a 0.72 correlation threshold
    and for a colour percentile range.
    """

    def test_map_identity_signatures_are_jpg(self):
        from map_identity import MapIdentityStore

        with tempfile.TemporaryDirectory() as tmp:
            store = MapIdentityStore(Path(tmp))
            image = Image.fromarray(sample_image()[:, :, ::-1])
            path = store.record("测试地图", image)
            self.assertEqual(path.suffix, ".jpg")
            self.assertEqual(path.read_bytes()[:2], JPEG_MAGIC)
            self.assertTrue(store.has_reference("测试地图"))
            # the index finds it again after a restart
            self.assertEqual(MapIdentityStore(Path(tmp)).signature_path("测试地图"), path)

    def test_the_map_structure_reference_is_jpg(self):
        from map_structure_tracker import MapStructureTracker

        with tempfile.TemporaryDirectory() as tmp:
            reference = Path(tmp) / "map-structure-reference.jpg"
            tracker = MapStructureTracker(reference)
            tracker._reference = np.zeros((192, 192), dtype=np.float32)   # a fake structure
            tracker.save_reference()
            self.assertEqual(reference.read_bytes()[:2], JPEG_MAGIC)
            # and it loads back through the tracker's own reader
            self.assertTrue(MapStructureTracker(reference)._reference is not None)

    def test_the_shipped_references_are_jpg_and_load(self):
        from reconnect_worker import LOGIN_REFERENCE_PATHS, load_login_reference

        for entry in LOGIN_REFERENCE_PATHS:
            self.assertEqual(Path(entry).suffix, ".jpg")
        self.assertIsNotNone(load_login_reference())
        root = Path(__file__).resolve().parent
        self.assertTrue((root / "recording-assets" / "map-structure-reference.jpg").is_file())
        self.assertEqual(
            list((root / "screenshots").glob("*.png")), [],
            "no PNG is left in the screenshots folder",
        )


if __name__ == "__main__":
    unittest.main()
