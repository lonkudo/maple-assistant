"""Periodic, window-scoped screen capture for the assistant.

The worker owns no game logic.  It captures the target window and publishes a
single timestamped :class:`CapturedFrame` to every interested consumer.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Tuple

from PIL import Image


WindowRect = Tuple[int, int, int, int]
CaptureFunction = Callable[[str], tuple[Image.Image, WindowRect]]


class WindowCaptureError(RuntimeError):
    """Raised when the requested window cannot be captured."""


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    """One immutable publication describing a captured game frame.

    The PIL image itself should be treated as read-only by subscribers.
    ``captured_monotonic`` is suitable for elapsed-time calculations while
    ``captured_at`` is an absolute UTC timestamp suitable for logs/files.
    """

    sequence: int
    captured_at: datetime
    captured_monotonic: float
    image: Image.Image
    window_rect: WindowRect


class FrameBus:
    """Fan out frames without allowing a slow analyzer to build a backlog.

    Each subscriber queue contains at most its newest frame.  ``latest`` and
    ``wait_for_new`` are also available for consumers that do not need queues.
    """

    def __init__(self, subscribers: Iterable[queue.Queue[CapturedFrame]] = ()) -> None:
        self._subscribers = tuple(subscribers)
        self._condition = threading.Condition()
        self._latest: Optional[CapturedFrame] = None

    @property
    def latest(self) -> Optional[CapturedFrame]:
        with self._condition:
            return self._latest

    def publish(self, frame: CapturedFrame) -> None:
        with self._condition:
            self._latest = frame
            self._condition.notify_all()

        for subscriber in self._subscribers:
            # Always discard stale work, including for an unbounded queue.
            while True:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    break
            # A consumer cannot make a queue fuller, but another producer could.
            # The retry protects this small API even if publish is called outside
            # the capture worker.
            while True:
                try:
                    subscriber.put_nowait(frame)
                    break
                except queue.Full:
                    try:
                        subscriber.get_nowait()
                    except queue.Empty:
                        pass

    def wait_for_new(
        self, after_sequence: int = -1, timeout: Optional[float] = None
    ) -> Optional[CapturedFrame]:
        """Wait until a frame newer than ``after_sequence`` is published."""

        with self._condition:
            ready = self._condition.wait_for(
                lambda: self._latest is not None
                and self._latest.sequence > after_sequence,
                timeout,
            )
            return self._latest if ready else None


def capture_window(window_title: str) -> tuple[Image.Image, WindowRect]:
    """Capture the visible client area of a Windows window as an RGB image."""

    if not window_title.strip():
        raise ValueError("window_title must not be empty")

    try:
        import win32con
        import win32gui
        import win32ui
    except ImportError as exc:  # pragma: no cover - exercised only off Windows
        raise WindowCaptureError(
            "Windows capture requires pywin32 (pip install pywin32)"
        ) from exc

    hwnd = win32gui.FindWindow(None, window_title)
    if not hwnd:
        raise WindowCaptureError(f"window not found: {window_title!r}")
    if win32gui.IsIconic(hwnd):
        raise WindowCaptureError(f"window is minimized: {window_title!r}")

    client_left, client_top, client_right, client_bottom = win32gui.GetClientRect(hwnd)
    screen_left, screen_top = win32gui.ClientToScreen(hwnd, (client_left, client_top))
    screen_right, screen_bottom = win32gui.ClientToScreen(
        hwnd, (client_right, client_bottom)
    )
    width = screen_right - screen_left
    height = screen_bottom - screen_top
    if width <= 0 or height <= 0:
        raise WindowCaptureError(f"window has an empty client area: {window_title!r}")

    # GetDC(hwnd) has its origin at the client area's upper-left. GetWindowDC
    # would include borders/title bar and offset the pixels from window_rect.
    window_dc = win32gui.GetDC(hwnd)
    source_dc = win32ui.CreateDCFromHandle(window_dc)
    memory_dc = source_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(source_dc, width, height)
        memory_dc.SelectObject(bitmap)
        memory_dc.BitBlt(
            (0, 0),
            (width, height),
            source_dc,
            (0, 0),
            win32con.SRCCOPY,
        )
        raw_bgra = bitmap.GetBitmapBits(True)
        image = Image.frombuffer(
            "RGB", (width, height), raw_bgra, "raw", "BGRX", 0, 1
        ).copy()
    except Exception as exc:
        raise WindowCaptureError(f"capture failed for {window_title!r}: {exc}") from exc
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        memory_dc.DeleteDC()
        source_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, window_dc)

    return image, (screen_left, screen_top, screen_right, screen_bottom)


class CaptureWorker(threading.Thread):
    """Capture immediately, then at a fixed interval until ``stop_event``."""

    def __init__(
        self,
        window_title: str,
        interval: float,
        bus: FrameBus,
        stop_event: threading.Event,
        debug_dir: Optional[Path] = None,
        capture_fn: Optional[CaptureFunction] = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be greater than zero")
        super().__init__(name="screen-capture", daemon=False)
        self.window_title = window_title
        self.interval = float(interval)
        self.bus = bus
        self.stop_event = stop_event
        self.debug_dir = Path(debug_dir) if debug_dir is not None else None
        self.capture_fn = capture_fn or capture_window
        self.log = logging.getLogger(__name__)
        self._last_debug_path: Optional[Path] = None

    def run(self) -> None:
        if self.debug_dir is not None:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            self._remove_stale_debug_frames()

        sequence = 0
        next_capture = time.monotonic()
        while not self.stop_event.is_set():
            delay = max(0.0, next_capture - time.monotonic())
            if self.stop_event.wait(delay):
                break

            captured_monotonic = time.monotonic()
            try:
                image, window_rect = self.capture_fn(self.window_title)
                frame = CapturedFrame(
                    sequence=sequence,
                    captured_at=datetime.now(timezone.utc),
                    captured_monotonic=captured_monotonic,
                    image=image,
                    window_rect=window_rect,
                )
                self.bus.publish(frame)
                if self.debug_dir is not None:
                    self._save_debug_frame(frame)
                sequence += 1
            except Exception:
                # A temporarily obscured/minimized/restarting game should not
                # silently kill all three workers. The orchestrator can still
                # stop this thread immediately through stop_event.
                self.log.exception("could not capture game window")

            next_capture += self.interval
            now = time.monotonic()
            if next_capture <= now:
                # Skip missed ticks instead of emitting a burst of stale frames.
                missed = int((now - next_capture) // self.interval) + 1
                next_capture += missed * self.interval

        # The current screenshot is useful only while the assistant is running.
        self._remove_last_debug_frame()

    def _save_debug_frame(self, frame: CapturedFrame) -> None:
        assert self.debug_dir is not None
        stamp = frame.captured_at.strftime("%Y%m%dT%H%M%S.%fZ")
        path = self.debug_dir / f"frame-{frame.sequence:06d}-{stamp}.png"
        previous = self._last_debug_path
        self._last_debug_path = None
        if previous is not None and previous != path:
            try:
                previous.unlink(missing_ok=True)
            except OSError:
                self.log.warning("could not remove used debug frame %s", previous,
                                 exc_info=True)
        try:
            frame.image.save(path, format="PNG")
            self._last_debug_path = path
        except Exception:
            self.log.exception("could not save debug frame %s", path)

    def _remove_last_debug_frame(self) -> None:
        path = self._last_debug_path
        self._last_debug_path = None
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            self.log.warning("could not remove final debug frame %s", path,
                             exc_info=True)

    def _remove_stale_debug_frames(self) -> None:
        """Remove only screenshots created by an earlier interrupted run."""

        assert self.debug_dir is not None
        for path in self.debug_dir.glob("frame-*.png"):
            try:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            except OSError:
                self.log.warning("could not remove stale debug frame %s", path,
                                 exc_info=True)
