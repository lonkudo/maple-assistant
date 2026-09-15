# -*- coding: utf-8 -*-
"""Tests for the decoupled lie video tooling.

The folder -> mp4 composition (how the operator's 测试api test clips are produced) and the frame
loader it uses.  The in-game replay through the local Cutie pass was removed with that pass.
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
        # JPG, the project format: image_io.frame_files lists nothing else
        Image.new("RGB", size, colour).save(
            folder / ("frame_%06d.jpg" % index), quality=95
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
                             ["frame_000001.jpg", "frame_000002.jpg",
                              "frame_000003.jpg"])
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


class NoLocalEngineTests(unittest.TestCase):
    """The module must not pull the removed Cutie pass back in."""

    def test_the_replay_half_is_gone(self):
        source = Path(tools.__file__).read_text(encoding="utf-8")
        for gone in ("import auto_lie_worker", "from auto_lie_worker", "simulate_in_game_run",
                     "RecordingAim", "lie_feed_box", "lie_demo_player"):
            self.assertNotIn(gone, source, gone)
        for kept in ("compose_frames_to_video", "load_frames", "VideoSink"):
            self.assertIn(kept, source, kept)


if __name__ == "__main__":
    unittest.main()
