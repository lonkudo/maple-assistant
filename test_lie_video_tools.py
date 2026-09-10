# -*- coding: utf-8 -*-
"""Tests for the decoupled lie video tooling.

No GPU, no game window, no mouse: the replay test uses a blank recording, so
it exercises the wiring (bell search, writer, text log, recorder aim) without
touching Cutie.  The real tracking path is validated on the operator machine
with the recorded lie-event folder (see lie_video_tools CLI).
"""

import tempfile
import unittest
from pathlib import Path

import cv2
from PIL import Image

import lie_video_tools as tools


def _write_frames(folder: Path, count: int = 5, size=(64, 48), colour=(20, 20, 20)) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(1, count + 1):
        Image.new("RGB", size, colour).save(
            folder / ("frame_%06d.png" % index)
        )
    return folder


class ComposeTests(unittest.TestCase):
    def test_frames_become_a_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = _write_frames(Path(tmp) / "lie_test", count=5)
            video = tools.compose_frames_to_video(folder, fps=5.0)
            self.assertTrue(video.is_file())
            self.assertEqual(video.parent, folder)
            capture = cv2.VideoCapture(str(video))
            frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = capture.get(cv2.CAP_PROP_FPS)
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            capture.release()
            self.assertEqual(frames, 5)
            self.assertAlmostEqual(fps, 5.0, delta=0.1)
            self.assertEqual(width, 64)

    def test_explicit_output_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = _write_frames(Path(tmp) / "lie_test", count=2)
            target = Path(tmp) / "elsewhere.mp4"
            self.assertEqual(
                tools.compose_frames_to_video(folder, target), target
            )
            self.assertTrue(target.is_file())

    def test_empty_folder_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                tools.compose_frames_to_video(Path(tmp) / "empty")


class LoadFramesTests(unittest.TestCase):
    def test_folder_frames_load_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = _write_frames(Path(tmp) / "lie_test", count=3)
            images, paths = tools.load_frames(folder)
            self.assertEqual(len(images), 3)
            self.assertEqual([p.name for p in paths],
                             ["frame_000001.png", "frame_000002.png",
                              "frame_000003.png"])
            self.assertEqual(images[0].size, (64, 48))

    def test_composed_video_reloads_frame_for_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = _write_frames(Path(tmp) / "lie_test", count=4)
            video = tools.compose_frames_to_video(folder, fps=4.0)
            images, _paths = tools.load_frames(video)
            self.assertEqual(len(images), 4)

    def test_missing_source_raises(self):
        with self.assertRaises(FileNotFoundError):
            tools.load_frames("does-not-exist-anywhere")


class RecordingAimTests(unittest.TestCase):
    def test_maps_crop_point_onto_the_region(self):
        aim = tools.RecordingAim(100, 50)
        aim.set_region(300, 100, 500, 200)
        self.assertEqual(aim.map_to_screen(0, 0), (300.0, 100.0))
        self.assertEqual(aim.map_to_screen(100, 50), (500.0, 200.0))
        self.assertEqual(aim.map_to_screen(50, 25), (400.0, 150.0))

    def test_without_region_nothing_is_mapped(self):
        aim = tools.RecordingAim(100, 50)
        self.assertIsNone(aim.map_to_screen(10, 10))
        aim.push_target(1, 2, 0.5, "CUTIE")
        self.assertEqual(aim.last, (1.0, 2.0, 0.5, "CUTIE"))
        self.assertEqual(len(aim.samples), 1)

    def test_degenerate_region_is_ignored(self):
        aim = tools.RecordingAim(10, 10)
        aim.set_region(5, 5, 5, 5)
        self.assertIsNone(aim.region)


class ReplayWiringTests(unittest.TestCase):
    def test_blank_recording_reports_no_bell(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = _write_frames(Path(tmp) / "lie_blank", count=3)
            summary = tools.simulate_in_game_run(
                folder,
                fps=50.0,
                realtime=False,
                output_video=folder / "out.mp4",
                output_text=folder / "out.txt",
            )
            self.assertIsNone(summary["bell_frame"])
            self.assertIsNone(summary["seed_frame"])
            self.assertEqual(summary["tracked_frames"], 0)
            self.assertEqual(summary["frames"], 3)
            text = (folder / "out.txt").read_text(encoding="utf-8")
            self.assertIn("no bell detected", text)
            self.assertIn("# summary", text)


if __name__ == "__main__":
    unittest.main()
