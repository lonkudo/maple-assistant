from __future__ import annotations

import queue
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from image_io import frame_files
from capture_worker import (
    CaptureWorker,
    CapturedFrame,
    FrameBus,
    remap_normalized_box,
)


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

    def test_normalized_box_is_remapped_into_capture_crop(self) -> None:
        mapped = remap_normalized_box(
            (0.34, 0.96, 0.56, 1.0),
            (0.0, 0.0, 0.60, 1.0),
        )
        self.assertAlmostEqual(mapped[0], 0.34 / 0.60)
        self.assertAlmostEqual(mapped[1], 0.96)
        self.assertAlmostEqual(mapped[2], 0.56 / 0.60)
        self.assertAlmostEqual(mapped[3], 1.0)


class CaptureWorkerTests(unittest.TestCase):
    def test_capture_pauses_while_focus_gate_is_clear(self) -> None:
        calls = 0

        def fake_capture(_title: str):
            nonlocal calls
            calls += 1
            return Image.new("RGB", (4, 3)), (0, 0, 4, 3)

        stop = threading.Event()
        focused = threading.Event()
        bus = FrameBus()
        worker = CaptureWorker(
            "game", 0.01, bus, stop,
            debug_draw_regions=True,
            capture_fn=fake_capture,
            capture_enabled_event=focused,
        )
        worker.start()
        try:
            time.sleep(0.05)
            self.assertEqual(calls, 0)
            focused.set()
            self.assertIsNotNone(bus.wait_for_new(timeout=0.5))
            focused.clear()
            time.sleep(0.03)
            paused_calls = calls
            time.sleep(0.08)
            self.assertEqual(calls, paused_calls)
        finally:
            stop.set()
            worker.join(0.5)
        self.assertFalse(worker.is_alive())

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

    def test_capture_now_forces_fresh_frame_while_scheduled_capture_is_paused(self) -> None:
        calls = 0

        def fake_capture(_title: str):
            nonlocal calls
            calls += 1
            return Image.new("RGB", (4, 3), (calls, 0, 0)), (0, 0, 4, 3)

        stop = threading.Event()
        capture_enabled = threading.Event()  # deliberately clear
        bus = FrameBus()
        worker = CaptureWorker(
            "game", 60.0, bus, stop, capture_fn=fake_capture,
            capture_enabled_event=capture_enabled,
        )
        worker.start()
        try:
            self.assertIsNone(bus.latest)
            requested_at = time.monotonic()
            frame = worker.capture_now(timeout=0.5)
            self.assertGreaterEqual(frame.captured_monotonic, requested_at)
            self.assertEqual(calls, 1)
            self.assertEqual(frame.image.getpixel((0, 0)), (1, 0, 0))
        finally:
            stop.set()
            worker.join(0.5)

    def test_status_capture_can_run_slower_than_minimap_capture(self) -> None:
        stop = threading.Event()
        bus = FrameBus()
        worker = CaptureWorker(
            "game", .02, bus, stop,
            capture_fn=lambda _title: (Image.new("RGB", (2, 2)), (0, 0, 2, 2)),
            status_capture_interval=.2,
        )
        self.assertEqual(worker.status_capture_interval, .2)

    def test_drop_action_selects_fast_capture_interval(self) -> None:
        stop = threading.Event()
        fast = threading.Event()
        worker = CaptureWorker(
            "game", .25, FrameBus(), stop,
            capture_fn=lambda _title: (Image.new("RGB", (2, 2)), (0, 0, 2, 2)),
            fast_capture_event=fast,
            fast_interval=.10,
        )
        self.assertEqual(worker.active_interval(), .25)
        fast.set()
        self.assertEqual(worker.active_interval(), .10)

    def test_lie_action_selects_30fps_and_outranks_the_drop_cadence(self) -> None:
        stop = threading.Event()
        fast = threading.Event()
        lie = threading.Event()
        worker = CaptureWorker(
            "game", .25, FrameBus(), stop,
            capture_fn=lambda _title: (Image.new("RGB", (2, 2)), (0, 0, 2, 2)),
            fast_capture_event=fast,
            fast_interval=.10,
            lie_capture_event=lie,
            lie_interval=1.0 / 30.0,
        )
        self.assertEqual(worker.active_interval(), .25)
        fast.set()
        self.assertEqual(worker.active_interval(), .10)
        lie.set()
        self.assertAlmostEqual(worker.active_interval(), 1.0 / 30.0)
        fast.clear()
        self.assertAlmostEqual(worker.active_interval(), 1.0 / 30.0)

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
        # The bus intentionally exposes the newest frame, so a fast worker may
        # publish more valid frames before this thread resumes.
        self.assertGreaterEqual(frame.sequence, 0)
        self.assertGreaterEqual(attempts, 2)
        self.assertFalse(worker.is_alive())

    def test_debug_dir_keeps_only_current_frame_and_cleans_on_stop(self) -> None:
        def fake_capture(_title: str):
            return Image.new("RGB", (4, 3)), (0, 0, 4, 3)

        with tempfile.TemporaryDirectory() as directory:
            debug_dir = Path(directory)
            # a PNG left behind by an earlier release must still be cleaned up
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
                # The worker deletes the previous dump and writes the next one every
                # iteration, so read the bytes the moment a file is there.
                deadline = time.monotonic() + 1.0
                name, payload = "", b""
                while time.monotonic() < deadline and not payload:
                    for candidate in sorted(debug_dir.glob("frame-*.jpg")):
                        try:
                            payload = candidate.read_bytes()
                        except OSError:
                            continue
                        name = candidate.name
                        break
                    if not payload:
                        time.sleep(0.005)
                self.assertTrue(name.endswith(".jpg"), "debug frames are JPG since image_io")
                self.assertEqual(payload[:2], b"\xff\xd8", "a real JPEG")
                self.assertEqual(list(debug_dir.glob("frame-*.png")), [],
                                 "a legacy PNG dump is swept up, not kept")
            finally:
                stop.set()
                worker.join(timeout=.5)
            self.assertEqual(frame_files(debug_dir), [])

    def test_capture_window_pixel_region_resolves_bottom_anchor(self) -> None:
        """Bottom-anchored pixel regions must not crash capture_window.

        Regression: the v0022 status capture used ``pixel_region`` with a
        negative (from-bottom) y, but that branch never assigned
        ``source_y``, so ``height = source_bottom - source_y`` raised
        UnboundLocalError on the live machine and every record click timed
        out with "could not capture game window".
        """

        import sys
        from unittest import mock

        from capture_worker import capture_window

        captured: dict[str, object] = {}

        class FakeGui:
            @staticmethod
            def FindWindow(_class: object, _title: str) -> int:
                return 1

            @staticmethod
            def IsIconic(_hwnd: int) -> bool:
                return False

            @staticmethod
            def GetClientRect(_hwnd: int) -> tuple[int, int, int, int]:
                return 0, 0, 1400, 800

            @staticmethod
            def ClientToScreen(_hwnd: int, point: tuple[int, int]) -> tuple[int, int]:
                return point

            @staticmethod
            def GetDC(_hwnd: int) -> int:
                return 5

            @staticmethod
            def ReleaseDC(_hwnd: int, _dc: int) -> int:
                return 0

            @staticmethod
            def DeleteObject(_handle: int) -> int:
                return 0

        class FakeDc:
            def __init__(self) -> None:
                captured["dc"] = self

            def CreateCompatibleDC(self) -> "FakeDc":
                return FakeDc()

            def SelectObject(self, _bitmap: object) -> int:
                return 0

            def BitBlt(
                self, _dest: tuple[int, int], size: tuple[int, int],
                _source: object, origin: tuple[int, int], _rop: int,
            ) -> None:
                captured["size"] = size
                captured["origin"] = origin

            def DeleteDC(self) -> int:
                return 0

        class FakeBitmap:
            def __init__(self) -> None:
                captured["bitmap"] = self
                self._size = (0, 0)

            def CreateCompatibleBitmap(self, _dc: object, width: int, height: int) -> int:
                self._size = (width, height)
                return 0

            def GetBitmapBits(self, _flags: bool) -> bytes:
                width, height = self._size
                return b"\x00" * (width * height * 4)

            def GetHandle(self) -> int:
                return 7

        class FakeUi:
            @staticmethod
            def CreateDCFromHandle(_dc: int) -> FakeDc:
                return FakeDc()

            @staticmethod
            def CreateBitmap() -> FakeBitmap:
                return FakeBitmap()

        fake_modules = {
            "win32gui": FakeGui,
            "win32ui": FakeUi,
            "win32con": mock.MagicMock(SRCCOPY=0x00CC0020),
        }
        with mock.patch.dict(sys.modules, fake_modules):
            image, rect = capture_window(
                "game", pixel_region=(922, -64, 1357, 0)
            )

        # Bottom-anchored: top = 800 - 64 = 736, bottom = 800.
        self.assertEqual(image.size, (435, 64))
        self.assertEqual(rect[1], 736)
        self.assertEqual(rect[3], 800)
        self.assertEqual(captured["size"], (435, 64))
        self.assertEqual(captured["origin"], (922, 736))


class CaptureFailurePathTests(unittest.TestCase):
    """The 11:29:59 field traceback: the cleanup must not replace the real error.

    ``CreateCompatibleBitmap`` fails (GDI/DC exhaustion) -> ``bitmap.GetHandle()`` is 0 -> the old
    ``finally`` called ``DeleteObject(0)`` and raised ``pywintypes.error: (0, 'DeleteObject', ...)``,
    hiding "CreateCompatibleDC failed" and leaving the DC taken by ``GetDC`` leaked on every retry.
    """

    @staticmethod
    def _modules(*, create_bitmap_ok: bool, calls: dict):
        class FakeError(Exception):
            pass

        class FakeGui:
            @staticmethod
            def FindWindow(_class, _title):
                return 1

            @staticmethod
            def IsIconic(_hwnd):
                return False

            @staticmethod
            def GetClientRect(_hwnd):
                return 0, 0, 200, 100

            @staticmethod
            def ClientToScreen(_hwnd, point):
                return point

            @staticmethod
            def GetDC(hwnd):
                calls.setdefault("getdc", []).append(hwnd)
                return 5 if hwnd else 6

            @staticmethod
            def ReleaseDC(hwnd, _dc):
                calls.setdefault("releasedc", []).append(hwnd)
                return 0

            @staticmethod
            def DeleteObject(handle):
                calls.setdefault("deleteobject", []).append(handle)
                if not handle:
                    raise FakeError("(0, 'DeleteObject', 'No error message is available')")
                return 0

            @staticmethod
            def GetForegroundWindow():
                return 1

        class FakeDc:
            def CreateCompatibleDC(self):
                return FakeDc()

            def SelectObject(self, _bitmap):
                return 0

            def BitBlt(self, *_args):
                return None

            def DeleteDC(self):
                calls["deletedc"] = calls.get("deletedc", 0) + 1
                return 0

        class FakeBitmap:
            def CreateCompatibleBitmap(self, _dc, _width, _height):
                if not create_bitmap_ok:
                    raise FakeError("CreateCompatibleDC failed")
                return 0

            def GetBitmapBits(self, _flags):
                return b"\x00" * (200 * 100 * 4)

            def GetHandle(self):
                return 7 if create_bitmap_ok else 0

        class FakeUi:
            @staticmethod
            def CreateDCFromHandle(_dc):
                return FakeDc()

            @staticmethod
            def CreateBitmap():
                return FakeBitmap()

        return {
            "win32gui": FakeGui,
            "win32ui": FakeUi,
            "win32con": mock.MagicMock(SRCCOPY=0x00CC0020),
        }

    def test_a_failed_bitmap_creation_keeps_the_original_error(self) -> None:
        import sys

        from capture_worker import WindowCaptureError, capture_window

        calls: dict = {}
        with mock.patch.dict(sys.modules, self._modules(create_bitmap_ok=False, calls=calls)):
            with self.assertRaises(WindowCaptureError) as caught:
                capture_window("game")
        message = str(caught.exception)
        self.assertIn("CreateCompatibleDC failed", message, message)
        self.assertNotIn("DeleteObject", message, message)
        # No DeleteObject(0) was attempted for the bitmap that never existed.
        self.assertNotIn(0, calls.get("deleteobject", []))
        # And the DCs were all released: two attempts (window path) + the desktop fallback.
        self.assertEqual(calls.get("getdc"), [1, 1, 0], calls)
        self.assertEqual(calls.get("releasedc"), [1, 1, 0], calls)

    def test_a_successful_capture_releases_every_dc_once(self) -> None:
        import sys

        from capture_worker import capture_window

        calls: dict = {}
        with mock.patch.dict(sys.modules, self._modules(create_bitmap_ok=True, calls=calls)):
            image, _rect = capture_window("game")
        self.assertEqual(image.size, (200, 100))
        self.assertEqual(calls.get("getdc"), [1])
        self.assertEqual(calls.get("releasedc"), [1])
        self.assertEqual(calls.get("deletedc"), 2)
        self.assertEqual(calls.get("deleteobject"), [7])

    def test_the_desktop_fallback_is_refused_while_the_game_is_not_foreground(self) -> None:
        import sys

        from capture_worker import WindowCaptureError, capture_window

        calls: dict = {}
        modules = self._modules(create_bitmap_ok=False, calls=calls)
        modules["win32gui"].GetForegroundWindow = staticmethod(lambda: 99)
        with mock.patch.dict(sys.modules, modules):
            with self.assertRaises(WindowCaptureError) as caught:
                capture_window("game")
        self.assertIn("not foreground", str(caught.exception))
        self.assertEqual(calls.get("getdc"), [1, 1], calls)


if __name__ == "__main__":
    unittest.main()
