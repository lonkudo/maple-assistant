# -*- coding: utf-8 -*-
"""测试api on a video: the order of connect / wait / feed, and what ends a run.

All against the local mimic backend, so nothing is billed.  What is pinned here is the workflow the
operator asked for:

* the connection is opened **as soon as the drill starts** (the lie event), not after the wait, so
  handshake and session are ready before there is anything to send;
* feeding begins only after ``AWAIT_SECOND_WINDOW_SEC`` (the second window), and the wait can never
  reach the service's 10s no-frame limit (2.7.0 §4.1);
* the whole process is the 时长 (30s by default), the wait included;
* when the clip ends, the run ends - round_end is sent and everything is closed.
"""

import json
import queue
import sys
import threading
import unittest
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api_lie_video import (  # noqa: E402
    AWAIT_SECOND_WINDOW_SEC,
    DEFAULT_FPS,
    DEFAULT_SECONDS,
    IDLE_NO_FRAME_SEC,
    IDLE_SAFE_SECONDS,
    VideoDrillWorker,
)


def make_clip(path: Path, *, frames: int = 60, fps: float = 10.0) -> Path:
    """A small clip with a moving bright blob, so the mimic has something to find."""

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (1366, 768))
    for index in range(frames):
        frame = np.full((768, 1366, 3), 40, dtype=np.uint8)
        x = 400 + (index * 7) % 500
        frame[300:330, x:x + 30] = 245
        writer.write(frame)
    writer.release()
    return path


def run_drill(tmp: Path, seconds: float = 6.0, await_seconds: float = AWAIT_SECOND_WINDOW_SEC):
    """Run one drill on a short clip against the mimic; return (worker, summary, log text)."""

    clip = make_clip(tmp / "clip.mp4", frames=90)
    results: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=4096)
    display: "queue.Queue[tuple]" = queue.Queue(maxsize=2)
    worker = VideoDrillWorker(
        video=clip, results=results, display=display, stop_event=threading.Event(),
        key="", seconds=seconds, fps=DEFAULT_FPS, use_mimic=True, aim_enabled=False,
        await_seconds=await_seconds,
    )
    worker.start()
    worker.join(timeout=120.0)
    folder = worker.log.folder
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    return worker, summary, (folder / "connection.log").read_text(encoding="utf-8")


class OrderTests(unittest.TestCase):

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_connection_is_opened_before_the_wait_not_after(self):
        _, summary, log = run_drill(self.tmp, seconds=8.0)
        lines = [line for line in log.splitlines() if line.strip()]
        backend_at = next(i for i, line in enumerate(lines) if "backend:" in line)
        feeding_at = next(i for i, line in enumerate(lines) if "feeding starts" in line)
        self.assertLess(backend_at, feeding_at,
                        "handshake must happen at the lie event, not after the wait")
        self.assertIn("feeding starts", log)
        self.assertIn(f"after_await_seconds={summary['await_seconds']}", log)

    def test_feeding_starts_after_the_second_window_wait(self):
        _, summary, _ = run_drill(self.tmp, seconds=8.0)
        self.assertEqual(summary["await_seconds"], AWAIT_SECOND_WINDOW_SEC)
        # 8s at 5 fps = 40 ticks: 15 spent waiting, the rest fed
        self.assertEqual(summary["total_ticks"], 40)
        self.assertEqual(summary["skipped_before_feeding"], int(AWAIT_SECOND_WINDOW_SEC * DEFAULT_FPS))
        self.assertEqual(summary["ticks"], 40 - summary["skipped_before_feeding"])
        self.assertEqual(summary["sent"], summary["ticks"])
        self.assertEqual(summary["answered"], summary["ticks"])
        self.assertEqual(summary["misses"], 0)
        # nothing is billed for the wait: the round starts with the first fed frame
        self.assertLessEqual(summary["feeding_seconds"], summary["whole_process_seconds"])

    def test_the_whole_process_is_the_configured_length(self):
        _, summary, _ = run_drill(self.tmp, seconds=8.0)
        self.assertLess(summary["whole_process_seconds"], 8.0 + 3.0)
        self.assertAlmostEqual(summary["ticks"] / DEFAULT_FPS + summary["await_seconds"],
                               8.0, delta=0.5)
        self.assertEqual(DEFAULT_SECONDS, 30.0)

    def test_a_clip_shorter_than_the_run_ends_the_run_and_closes(self):
        # 90 frames at 10 fps: 9s of clip -> 18s of playback at 5 fps, less than the 20s asked for
        _, summary, log = run_drill(self.tmp, seconds=20.0)
        self.assertEqual(summary["ended_because"], "video ended")
        self.assertEqual(summary["outcome"], "done")
        self.assertIn("round_end sent", log)
        self.assertLess(summary["whole_process_seconds"], 20.0)

    def test_a_long_wait_is_clamped_below_the_idle_limit(self):
        # a wait longer than the service's no-frame limit would get the session kicked (2.7.0 §4.1)
        _, summary, log = run_drill(self.tmp, seconds=10.0, await_seconds=IDLE_NO_FRAME_SEC + 6.0)
        self.assertLess(summary["skipped_before_feeding"], IDLE_NO_FRAME_SEC * DEFAULT_FPS)
        self.assertLessEqual(summary["skipped_before_feeding"], IDLE_SAFE_SECONDS * DEFAULT_FPS)
        self.assertIn("await clamped", log)
        self.assertGreater(summary["sent"], 0)
