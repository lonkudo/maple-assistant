# -*- coding: utf-8 -*-
"""Targeted tests for the decoupled lie-event screenshot recorder."""

import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path

from PIL import Image

from image_io import frame_files
from lie_screenshot_recorder import LieScreenshotRecorder


def _all_frames(root: Path) -> list[Path]:
    """Every frame file of every run folder, PNG (older releases) and JPG alike."""

    return [path for run in sorted(Path(root).glob("*_*")) if run.is_dir()
            for path in frame_files(run)]


def _fake_capture(image=None, failures=0):
    """Capture callable: returns a tiny image, optionally failing N times."""

    failures_left = [failures]
    sample = image if image is not None else Image.new("RGB", (64, 48), (200, 200, 200))

    def capture():
        if failures_left[0] > 0:
            failures_left[0] -= 1
            raise RuntimeError("window not ready")
        return sample, (0, 0, 64, 48)

    return capture


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class LieScreenshotRecorderTests(unittest.TestCase):
    def _make(self, tmp, **kwargs):
        defaults = dict(
            window_title="test-window",
            output_dir=Path(tmp) / "a" / "b" / "screenshots",  # non-existent
            duration_seconds=0.15,
            min_interval=0.0,
            max_frames=8,
            keep_frames=True,  # frame assertions below; video tests opt out
            compose_video=False,  # video tests opt in explicitly
            capture_fn=_fake_capture(),
        )
        defaults.update(kwargs)
        return LieScreenshotRecorder(**defaults)

    def test_creates_missing_folder_and_saves_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = self._make(tmp)
            recorder.on_lie_seen((10, 10, 40, 40), frame=None)
            ok = _wait_until(lambda: not recorder._recording)
            self.assertTrue(ok, "recording should finish")
            runs = list(recorder.output_dir.glob("lie_*"))
            self.assertEqual(len(runs), 1, "one run folder expected")
            frames = frame_files(runs[0])
            self.assertGreaterEqual(len(frames), 1, "frames must be saved")
            self.assertLessEqual(len(frames), 8, "max_frames cap respected")
            self.assertEqual(frames[0].suffix, ".jpg", "frames are JPG since image_io")
            self.assertEqual(frames[0].read_bytes()[:2], b"\xff\xd8", "a real JPEG")

    def test_single_flight_ignores_second_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = self._make(tmp)
            recorder.on_lie_seen((10, 10, 40, 40), frame=None)
            recorder.on_lie_seen((50, 50, 40, 40), frame=None)  # mid-run
            ok = _wait_until(lambda: not recorder._recording)
            self.assertTrue(ok)
            runs = list(recorder.output_dir.glob("lie_*"))
            self.assertEqual(len(runs), 1, "no second run while active")

    def test_disabled_recorder_does_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = self._make(tmp, enabled=False)
            recorder.on_lie_seen((10, 10, 40, 40), frame=None)
            time.sleep(0.05)
            self.assertFalse(recorder._recording)
            self.assertFalse(recorder.output_dir.exists())

    def test_clear_event_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = self._make(tmp)
            recorder.on_lie_seen(None, frame=None)
            time.sleep(0.05)
            self.assertFalse(recorder._recording)
            self.assertFalse(recorder.output_dir.exists())

    def test_releases_flag_after_transient_capture_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = self._make(
                tmp,
                duration_seconds=1.0,  # > 3x0.1s failure sleeps
                capture_fn=_fake_capture(failures=3),
            )
            recorder.on_lie_seen((10, 10, 40, 40), frame=None)
            ok = _wait_until(lambda: not recorder._recording)
            self.assertTrue(ok, "flag must be released after failures")
            self.assertGreaterEqual(len(_all_frames(recorder.output_dir)), 1)

    def test_flag_released_after_fatal_capture_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = self._make(
                tmp,
                duration_seconds=5.0,  # long, but 20 straight failures stop it
                capture_fn=_fake_capture(failures=9999),
            )
            recorder.on_lie_seen((10, 10, 40, 40), frame=None)
            ok = _wait_until(lambda: not recorder._recording, timeout=8.0)
            self.assertTrue(ok, "must stop early and release the flag")
            self.assertEqual(len(_all_frames(recorder.output_dir)), 0)

    def test_same_second_runs_get_unique_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = self._make(tmp)
            first = recorder._prepare_run_dir("lie")
            second = recorder._prepare_run_dir("lie")  # same second, exists
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())

    def test_offline_and_countdown_triggers_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            for kind, trigger in (
                ("offline", lambda r: r.on_disconnect()),
                ("countdown", lambda r: r.on_countdown()),
            ):
                recorder = self._make(tmp)
                trigger(recorder)
                ok = _wait_until(lambda: not recorder._recording)
                self.assertTrue(ok, "%s recording should finish" % kind)
                runs = list(recorder.output_dir.glob("%s_*" % kind))
                self.assertEqual(len(runs), 1, "%s run folder expected" % kind)
                frames = frame_files(runs[0])
                self.assertGreaterEqual(len(frames), 1)
                self.assertEqual(frames[0].suffix, ".jpg",
                                 "recording frames are JPG since image_io")

    def test_finished_run_is_composed_into_a_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = self._make(tmp, keep_frames=True, compose_video=True)
            recorder.on_lie_seen((10, 10, 40, 40), frame=None)
            self.assertTrue(_wait_until(lambda: not recorder._recording))
            found = _wait_until(
                lambda: list(recorder.output_dir.glob("lie_*/*.mp4")),
                timeout=10.0,
            )
            self.assertTrue(found, "run video should be composed")
            video = list(recorder.output_dir.glob("lie_*/*.mp4"))[0]
            self.assertGreater(video.stat().st_size, 0)
            frames = frame_files(video.parent)
            self.assertGreaterEqual(len(frames), 1, "keep_frames keeps the frames")

    def test_frames_are_removed_once_the_video_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = self._make(
                tmp, keep_frames=False, compose_video=True
            )
            recorder.on_lie_seen((10, 10, 40, 40), frame=None)
            self.assertTrue(_wait_until(lambda: not recorder._recording))
            # The single-flight flag is only released after the compose, so
            # by now the video exists and the frame files are gone.
            videos = list(recorder.output_dir.glob("lie_*/*.mp4"))
            frames = _all_frames(recorder.output_dir)
            self.assertTrue(videos, "video expected")
            self.assertEqual(frames, [])

    def test_frames_stay_when_composing_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = self._make(tmp, keep_frames=False, compose_video=False)
            recorder.on_lie_seen((10, 10, 40, 40), frame=None)
            self.assertTrue(_wait_until(lambda: not recorder._recording))
            time.sleep(0.2)
            self.assertFalse(list(recorder.output_dir.glob("lie_*/*.mp4")))
            frames = _all_frames(recorder.output_dir)
            self.assertGreaterEqual(len(frames), 1, "no video -> frames must stay")

    def test_single_flight_shared_across_event_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = self._make(tmp)
            recorder.on_lie_seen((10, 10, 40, 40), frame=None)
            recorder.on_countdown()  # while the lie recording is active
            ok = _wait_until(lambda: not recorder._recording)
            self.assertTrue(ok)
            runs = list(recorder.output_dir.glob("*_*"))
            self.assertEqual(len(runs), 1, "one shared run while active")


if __name__ == "__main__":
    unittest.main()
