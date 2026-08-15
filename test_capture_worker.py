from __future__ import annotations

import queue
import threading
import time
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from capture_worker import CaptureWorker, CapturedFrame, FrameBus


class FrameBusTests(unittest.TestCase):
    def frame(self, sequence: int) -> CapturedFrame:
        from datetime import datetime, timezone

        return CapturedFrame(
            sequence,
            datetime.now(timezone.utc),
            time.monotonic(),
            Image.new("RGB", (2, 2)),
            (0, 0, 2, 2),
        )

    def test_publish_replaces_stale_queued_frame(self) -> None:
        subscriber = queue.Queue()
        bus = FrameBus([subscriber])
        bus.publish(self.frame(1))
        bus.publish(self.frame(2))
        self.assertEqual(subscriber.get_nowait().sequence, 2)
        self.assertEqual(bus.latest.sequence, 2)

    def test_wait_for_new_times_out_without_newer_frame(self) -> None:
        bus = FrameBus()
        bus.publish(self.frame(3))
        self.assertIsNone(bus.wait_for_new(after_sequence=3, timeout=0.01))


class CaptureWorkerTests(unittest.TestCase):
    def test_worker_publishes_and_stops_cleanly(self) -> None:
        calls = 0

        def fake_capture(_title: str):
            nonlocal calls
            calls += 1
            return Image.new("RGB", (4, 3)), (10, 20, 14, 23)

        stop = threading.Event()
        bus = FrameBus()
        worker = CaptureWorker("game", 0.02, bus, stop, capture_fn=fake_capture)
        worker.start()
        second = bus.wait_for_new(after_sequence=0, timeout=0.5)
        stop.set()
        worker.join(timeout=0.5)

        self.assertFalse(worker.is_alive())
        self.assertIsNotNone(second)
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(second.window_rect, (10, 20, 14, 23))
        self.assertEqual(second.image.size, (4, 3))

    def test_transient_capture_failure_does_not_kill_worker(self) -> None:
        attempts = 0

        def flaky_capture(_title: str):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary")
            return Image.new("RGB", (1, 1)), (0, 0, 1, 1)

        stop = threading.Event()
        bus = FrameBus()
        worker = CaptureWorker("game", 0.01, bus, stop, capture_fn=flaky_capture)
        worker.start()
        frame = bus.wait_for_new(timeout=0.5)
        stop.set()
        worker.join(timeout=0.5)

        self.assertIsNotNone(frame)
        self.assertEqual(frame.sequence, 0)
        self.assertFalse(worker.is_alive())

    def test_debug_dir_keeps_only_current_frame_and_cleans_on_stop(self) -> None:
        def fake_capture(_title: str):
            return Image.new("RGB", (4, 3)), (0, 0, 4, 3)

        with tempfile.TemporaryDirectory() as directory:
            debug_dir = Path(directory)
            (debug_dir / "frame-999999-stale.png").write_bytes(b"stale")
            stop = threading.Event()
            bus = FrameBus()
            worker = CaptureWorker(
                "game", 0.02, bus, stop,
                debug_dir=debug_dir, capture_fn=fake_capture,
            )
            try:
                worker.start()
                self.assertIsNotNone(bus.wait_for_new(after_sequence=0, timeout=.5))
                time.sleep(.03)
                self.assertLessEqual(len(list(debug_dir.glob("frame-*.png"))), 1)
            finally:
                stop.set()
                worker.join(timeout=.5)
            self.assertEqual(list(debug_dir.glob("frame-*.png")), [])


if __name__ == "__main__":
    unittest.main()
