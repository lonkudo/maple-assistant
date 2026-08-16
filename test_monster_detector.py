"""Tests for the independent monster detection worker."""

from __future__ import annotations

import queue
import threading
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from monster_detector import (
    DEFAULT_MONSTER_ZONE,
    MonsterConfig,
    MonsterDetection,
    MonsterDetector,
    MonsterWorker,
)


class _Frame:
    def __init__(self, image: Image.Image, sequence: int = 0) -> None:
        self.image = image
        self.sequence = sequence


def _make_scene(
    width: int = 300,
    height: int = 300,
    *,
    blobs: list[tuple[int, int, int, int]] = None,
    background: tuple[int, int, int] = (40, 60, 90),
) -> Image.Image:
    """Build an RGB frame with optional colored rectangles (x, y, w, h)."""

    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    for (x, y, w, h) in (blobs or []):
        draw.rectangle((x, y, x + w, y + h), fill=(220, 40, 40))
    return image


class MonsterDetectorTests(unittest.TestCase):
    def test_detects_multiple_monsters_in_zone(self) -> None:
        # Search zone is the middle third vertically: y in [100, 200).
        scene = _make_scene(blobs=[
            (20, 110, 30, 40),   # monster 1 (in zone)
            (90, 130, 25, 35),   # monster 2 (in zone)
            (200, 105, 40, 45),  # monster 3 (in zone)
        ])
        detector = MonsterDetector(MonsterConfig(
            search_zone=DEFAULT_MONSTER_ZONE,
            hsv_lower=(0, 80, 80),
            hsv_upper=(10, 255, 255),
        ))
        detections = detector.detect(scene)
        self.assertEqual(len(detections), 3)
        for detection in detections:
            self.assertIsInstance(detection, MonsterDetection)
            left, top, right, bottom = detection.pixel_box
            self.assertGreaterEqual(left, 0)
            self.assertGreaterEqual(top, 99)  # within zone
            self.assertLess(bottom, 201)
            self.assertGreater(right, left)
            self.assertGreater(bottom, top)
            self.assertGreater(detection.confidence, 0.0)

    def test_blobs_outside_zone_are_ignored(self) -> None:
        scene = _make_scene(blobs=[
            (20, 10, 30, 40),    # top third -> ignored
            (20, 240, 30, 40),   # bottom third -> ignored
            (20, 110, 30, 40),   # middle third -> detected
        ])
        detector = MonsterDetector(MonsterConfig(
            search_zone=DEFAULT_MONSTER_ZONE,
            hsv_lower=(0, 80, 80),
            hsv_upper=(10, 255, 255),
        ))
        detections = detector.detect(scene)
        self.assertEqual(len(detections), 1)
        _, top, _, bottom = detections[0].pixel_box
        self.assertGreaterEqual(top, 100)
        self.assertLess(bottom, 201)

    def test_no_blobs_returns_empty(self) -> None:
        scene = _make_scene(blobs=[])
        detector = MonsterDetector(MonsterConfig(
            search_zone=DEFAULT_MONSTER_ZONE,
            hsv_lower=(0, 80, 80),
            hsv_upper=(10, 255, 255),
        ))
        self.assertEqual(detector.detect(scene), [])

    def test_wrong_color_returns_empty(self) -> None:
        # Scene blobs are red; detector tuned for blue -> nothing found.
        scene = _make_scene(blobs=[(20, 110, 30, 40)])
        detector = MonsterDetector(MonsterConfig(
            search_zone=DEFAULT_MONSTER_ZONE,
            hsv_lower=(100, 120, 120),
            hsv_upper=(130, 255, 255),
        ))
        self.assertEqual(detector.detect(scene), [])

    def test_too_small_blob_is_filtered(self) -> None:
        scene = _make_scene(blobs=[(20, 110, 2, 2)])
        detector = MonsterDetector(MonsterConfig(
            search_zone=DEFAULT_MONSTER_ZONE,
            hsv_lower=(0, 80, 80),
            hsv_upper=(10, 255, 255),
            min_span_fraction=0.02,
        ))
        self.assertEqual(detector.detect(scene), [])

    def test_coverage_reports_band_fraction(self) -> None:
        # A scene mostly filled with the band color -> high coverage.
        scene = _make_scene(blobs=[(20, 110, 200, 100)])  # big red block in zone
        detector = MonsterDetector(MonsterConfig(
            search_zone=DEFAULT_MONSTER_ZONE,
            hsv_lower=(0, 80, 80),
            hsv_upper=(10, 255, 255),
        ))
        _detections, coverage = detector.detect_with_coverage(scene)
        self.assertGreater(coverage, 0.05)

    def test_coverage_zero_when_nothing_matches(self) -> None:
        scene = _make_scene(blobs=[])
        detector = MonsterDetector(MonsterConfig(
            search_zone=DEFAULT_MONSTER_ZONE,
            hsv_lower=(0, 80, 80),
            hsv_upper=(10, 255, 255),
        ))
        _detections, coverage = detector.detect_with_coverage(scene)
        self.assertEqual(coverage, 0.0)

    def test_two_bands_detect_both_monster_colors(self) -> None:
        # Scene with orange and blue monsters in the zone; neutral background
        # so it matches neither band.
        image = Image.new("RGB", (300, 300), (70, 70, 70))
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 110, 60, 150), fill=(240, 120, 30))    # orange
        draw.rectangle((120, 110, 160, 150), fill=(60, 90, 230))   # blue
        draw.rectangle((200, 110, 240, 150), fill=(240, 120, 30))  # orange
        detector = MonsterDetector(configs=[
            MonsterConfig(search_zone=DEFAULT_MONSTER_ZONE,
                          hsv_lower=(0, 100, 80), hsv_upper=(30, 255, 255)),
            MonsterConfig(search_zone=DEFAULT_MONSTER_ZONE,
                          hsv_lower=(100, 100, 80), hsv_upper=(130, 255, 255)),
        ])
        detections = detector.detect(image)
        self.assertEqual(len(detections), 3)

    def test_set_config_replaces_band(self) -> None:
        detector = MonsterDetector()
        self.assertEqual(len(detector.configs), 1)
        detector.add_config(MonsterConfig())
        self.assertEqual(len(detector.configs), 2)
        detector.set_config(1, MonsterConfig(hsv_lower=(1, 2, 3)))
        self.assertEqual(detector.configs[1].hsv_lower, (1, 2, 3))
        with self.assertRaises(IndexError):
            detector.set_config(5, MonsterConfig())

    def test_disabled_band_never_matches(self) -> None:
        image = _make_scene(blobs=[(20, 110, 30, 40)])
        detector = MonsterDetector(MonsterConfig(
            search_zone=DEFAULT_MONSTER_ZONE,
            hsv_lower=(0, 80, 80),
            hsv_upper=(10, 255, 255),
            enabled=False,
        ))
        detections, coverage = detector.detect_with_coverage(image)
        self.assertEqual(detections, [])
        self.assertEqual(coverage, 0.0)

    def test_histogram_method_detects_multicolored_monster(self) -> None:
        # A monster with two colors (orange body + dark spots) on neutral bg.
        image = Image.new("RGB", (300, 300), (70, 70, 70))
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 110, 60, 150), fill=(240, 120, 30))
        draw.rectangle((28, 118, 36, 126), fill=(60, 40, 20))
        draw.rectangle((44, 118, 52, 126), fill=(60, 40, 20))
        draw.rectangle((120, 110, 160, 150), fill=(240, 120, 30))
        draw.rectangle((128, 118, 136, 126), fill=(60, 40, 20))
        template = image.crop((20, 110, 60, 150))  # tight monster crop
        detector = MonsterDetector(MonsterConfig(
            method="histogram",
            search_zone=DEFAULT_MONSTER_ZONE,
            hsv_lower=(0, 80, 80),
            hsv_upper=(30, 255, 255),
            template_image=template,
        ))
        detections = detector.detect(image)
        self.assertGreaterEqual(len(detections), 1)

    def test_template_method_finds_sprite(self) -> None:
        # Sprite placed twice in the zone (same orientation).
        sprite = Image.new("RGB", (40, 40), (240, 120, 30))
        draw = ImageDraw.Draw(sprite)
        draw.rectangle((10, 10, 30, 30), fill=(30, 30, 30))
        image = Image.new("RGB", (300, 300), (70, 70, 70))
        image.paste(sprite, (25, 115))
        image.paste(sprite, (140, 120))
        detector = MonsterDetector(MonsterConfig(
            method="template",
            search_zone=DEFAULT_MONSTER_ZONE,
            hsv_lower=(0, 80, 80),
            hsv_upper=(30, 255, 255),
            template_image=sprite,
        ))
        detections = detector.detect(image)
        self.assertEqual(len(detections), 2)

    def test_template_method_finds_flipped_sprite(self) -> None:
        # Sprite in the zone is mirrored (monster facing the other way).
        sprite = Image.new("RGB", (40, 40), (240, 120, 30))
        draw = ImageDraw.Draw(sprite)
        draw.rectangle((10, 10, 20, 30), fill=(30, 30, 30))  # off-center detail
        image = Image.new("RGB", (300, 300), (70, 70, 70))
        image.paste(sprite.transpose(Image.FLIP_LEFT_RIGHT), (25, 115))
        detector = MonsterDetector(MonsterConfig(
            method="template",
            search_zone=DEFAULT_MONSTER_ZONE,
            hsv_lower=(0, 80, 80),
            hsv_upper=(30, 255, 255),
            template_image=sprite,
        ))
        detections = detector.detect(image)
        self.assertEqual(len(detections), 1)

    def test_motion_detects_moving_blob(self) -> None:
        image_a = _make_scene(blobs=[(20, 110, 30, 40)])
        image_b = _make_scene(blobs=[(60, 110, 30, 40)])  # moved right
        detector = MonsterDetector(MonsterConfig(
            method="motion", search_zone=DEFAULT_MONSTER_ZONE,
        ))
        # First frame only stores the reference.
        self.assertEqual(detector.detect(image_a), [])
        detections = detector.detect(image_b)
        self.assertGreaterEqual(len(detections), 1)

    def test_motion_static_scene_is_empty(self) -> None:
        scene = _make_scene(blobs=[(20, 110, 30, 40)])
        detector = MonsterDetector(MonsterConfig(
            method="motion", search_zone=DEFAULT_MONSTER_ZONE,
        ))
        detector.detect(scene)  # reference
        self.assertEqual(detector.detect(scene), [])

    def test_silhouette_finds_closed_object_any_map(self) -> None:
        # Map-agnostic: two very different "maps" (backgrounds), one closed
        # sprite-like object each.  Only the closed object is found.
        for background in [(70, 70, 70), (40, 60, 90), (120, 90, 40)]:
            image = Image.new("RGB", (300, 300), background)
            draw = ImageDraw.Draw(image)
            # A closed "monster" blob: filled ellipse with a dark outline.
            draw.ellipse((40, 110, 100, 170), fill=(220, 120, 40))
            draw.ellipse((40, 110, 100, 170), outline=(20, 20, 20), width=3)
            detector = MonsterDetector(MonsterConfig(
                method="silhouette", search_zone=DEFAULT_MONSTER_ZONE,
            ))
            detections = detector.detect(image)
            self.assertGreaterEqual(
                len(detections), 1,
                f"silhouette found nothing on background {background}",
            )

    def test_silhouette_ignores_open_background_lines(self) -> None:
        # Only open lines (no closed shape) -> nothing detected.
        image = Image.new("RGB", (300, 300), (70, 70, 70))
        draw = ImageDraw.Draw(image)
        for x in range(0, 300, 30):
            draw.line((x, 110, x + 15, 160), fill=(200, 200, 200), width=2)
        detector = MonsterDetector(MonsterConfig(
            method="silhouette", search_zone=DEFAULT_MONSTER_ZONE,
        ))
        self.assertEqual(detector.detect(image), [])

    def test_motion_camera_scroll_rejected_by_max_area(self) -> None:
        # Everything shifts (player walking): one giant diff blob > max area.
        width, height = 300, 300
        image_a = Image.new("RGB", (width, height), (40, 60, 90))
        draw_a = ImageDraw.Draw(image_a)
        for x in range(0, width, 40):
            draw_a.rectangle((x, 110, x + 10, 130), fill=(200, 200, 200))
        image_b = image_a.copy()
        draw_b = ImageDraw.Draw(image_b)
        for x in range(0, width, 40):
            draw_b.rectangle((x + 6, 110, x + 16, 130), fill=(200, 200, 200))
        detector = MonsterDetector(MonsterConfig(
            method="motion", search_zone=DEFAULT_MONSTER_ZONE,
            max_area_fraction=0.05,
        ))
        detector.detect(image_a)
        # Many small blobs instead of one giant one still pass individually;
        # a single large scroll blob is filtered.  Assert no single blob
        # larger than the max area is reported.
        detections = detector.detect(image_b)
        for detection in detections:
            width_px = detection.pixel_box[2] - detection.pixel_box[0]
            height_px = detection.pixel_box[3] - detection.pixel_box[1]
            self.assertLess(width_px * height_px, 300 * 100 * 0.10)

    def test_red_hue_wraparound_detects_both_sides(self) -> None:
        # A scene with pure red (hue ~0) and magenta-leaning red (hue ~178).
        image = Image.new("RGB", (300, 300), (40, 60, 90))
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 110, 60, 150), fill=(230, 50, 50))     # hue ~0
        draw.rectangle((120, 110, 160, 150), fill=(225, 45, 55))   # hue ~178
        detector = MonsterDetector(MonsterConfig(
            search_zone=DEFAULT_MONSTER_ZONE,
            hsv_lower=(170, 80, 80),   # wraps: 170..179 + 0..10
            hsv_upper=(10, 255, 255),
        ))
        detections = detector.detect(image)
        self.assertEqual(len(detections), 2)

    def test_normalized_box_matches_pixel_box(self) -> None:
        scene = _make_scene(width=200, height=300, blobs=[(40, 110, 20, 30)])
        detector = MonsterDetector(MonsterConfig(
            search_zone=DEFAULT_MONSTER_ZONE,
            hsv_lower=(0, 80, 80),
            hsv_upper=(10, 255, 255),
        ))
        detections = detector.detect(scene)
        self.assertEqual(len(detections), 1)
        detection = detections[0]
        left, top, right, bottom = detection.pixel_box
        norm_left, norm_top, norm_right, norm_bottom = detection.box
        self.assertAlmostEqual(norm_left, left / 200, places=4)
        self.assertAlmostEqual(norm_top, top / 300, places=4)
        self.assertAlmostEqual(norm_right, right / 200, places=4)
        self.assertAlmostEqual(norm_bottom, bottom / 300, places=4)


class MonsterWorkerTests(unittest.TestCase):
    def _worker(self, debug_dir: Path) -> tuple[MonsterWorker, queue.Queue,
                                                threading.Event]:
        frames: queue.Queue = queue.Queue(maxsize=1)
        stop = threading.Event()
        worker = MonsterWorker(
            frames,
            stop,
            detector=MonsterDetector(MonsterConfig(
                search_zone=DEFAULT_MONSTER_ZONE,
                hsv_lower=(0, 80, 80),
                hsv_upper=(10, 255, 255),
            )),
            debug_dir=debug_dir,
        )
        return worker, frames, stop

    def test_worker_publishes_latest_detections(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            worker, frames, stop = self._worker(Path(directory))
            worker.start()
            try:
                scene = _make_scene(blobs=[(20, 110, 30, 40), (90, 130, 25, 35)])
                frames.put(_Frame(scene, sequence=7))
                deadline = threading.Event()
                for _ in range(50):
                    if worker.latest_frame_sequence == 7:
                        break
                    deadline.wait(0.05)
                self.assertEqual(worker.latest_frame_sequence, 7)
                self.assertEqual(len(worker.latest), 2)
            finally:
                stop.set()
                worker.join(timeout=2)

    def test_worker_analyzes_even_when_automation_inactive(self) -> None:
        # Monster analysis is passive (no keyboard input): frames are
        # processed regardless of the patrol/automation state.
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            frames: queue.Queue = queue.Queue(maxsize=1)
            stop = threading.Event()
            automation = threading.Event()  # not set -> must still analyze
            worker = MonsterWorker(
                frames, stop,
                detector=MonsterDetector(MonsterConfig(
                    search_zone=DEFAULT_MONSTER_ZONE,
                    hsv_lower=(0, 80, 80),
                    hsv_upper=(10, 255, 255),
                )),
                automation_active_event=automation,
            )
            worker.start()
            try:
                frames.put(_Frame(_make_scene(blobs=[(20, 110, 30, 40)]), sequence=3))
                for _ in range(20):
                    if worker.latest_frame_sequence == 3:
                        break
                    threading.Event().wait(0.05)
                self.assertEqual(worker.latest_frame_sequence, 3)
                self.assertEqual(len(worker.latest), 1)
            finally:
                stop.set()
                worker.join(timeout=2)

    def test_worker_throttles_to_interval(self) -> None:
        import tempfile
        import time

        with tempfile.TemporaryDirectory() as directory:
            frames: queue.Queue = queue.Queue(maxsize=1)
            stop = threading.Event()
            worker = MonsterWorker(
                frames, stop,
                detector=MonsterDetector(MonsterConfig(
                    search_zone=DEFAULT_MONSTER_ZONE,
                    hsv_lower=(0, 80, 80),
                    hsv_upper=(10, 255, 255),
                )),
                interval=0.4,
            )
            worker.start()
            try:
                scene = _make_scene(blobs=[(20, 110, 30, 40)])
                # Push several frames quickly; only ~1 per 0.4s is analyzed.
                for seq in range(1, 6):
                    frames.put(_Frame(scene, sequence=seq))
                    time.sleep(0.03)
                self.assertLessEqual(worker.latest_frame_sequence, 2)
                # Wait past the interval: a later frame gets analyzed.
                for _ in range(60):
                    if worker.latest_frame_sequence >= 3:
                        break
                    threading.Event().wait(0.05)
                self.assertGreaterEqual(worker.latest_frame_sequence, 1)
            finally:
                stop.set()
                worker.join(timeout=2)

    def test_worker_writes_pink_overlay_when_debug_dir_set(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            debug_dir = Path(directory)
            worker, frames, stop = self._worker(debug_dir)
            worker.start()
            try:
                scene = _make_scene(blobs=[(20, 110, 30, 40)])
                frames.put(_Frame(scene, sequence=1))
                for _ in range(50):
                    if list(debug_dir.glob("monster-*.png")):
                        break
                    threading.Event().wait(0.05)
                overlays = list(debug_dir.glob("monster-*.png"))
                self.assertEqual(len(overlays), 1)
                overlay = Image.open(overlays[0]).convert("RGB")
                self.assertEqual(overlay.size, scene.size)
                # Pink zone outline exists somewhere in the overlay.
                pixels = overlay.load()
                pink_found = any(
                    pixels[x, y] == (255, 105, 180)
                    for x in range(0, overlay.width, 3)
                    for y in range(0, overlay.height, 3)
                )
                self.assertTrue(pink_found)
            finally:
                stop.set()
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
