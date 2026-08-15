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
NormalizedBox = Tuple[float, float, float, float]
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
    status_image: Optional[Image.Image] = None


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


def remap_normalized_box(box: NormalizedBox, crop: NormalizedBox) -> NormalizedBox:
    """Map a full-client normalized box into normalized cropped-frame units."""

    crop_left, crop_top, crop_right, crop_bottom = crop
    crop_width = crop_right - crop_left
    crop_height = crop_bottom - crop_top
    if crop_width <= 0 or crop_height <= 0:
        raise ValueError("capture crop must have positive width and height")
    left, top, right, bottom = box
    return (
        (left - crop_left) / crop_width,
        (top - crop_top) / crop_height,
        (right - crop_left) / crop_width,
        (bottom - crop_top) / crop_height,
    )


def capture_window(
    window_title: str,
    crop_region: NormalizedBox = (0.0, 0.0, 1.0, 1.0),
    crop_pixel_size: Optional[tuple[int, int]] = None,
) -> tuple[Image.Image, WindowRect]:
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
    client_width = screen_right - screen_left
    client_height = screen_bottom - screen_top
    if client_width <= 0 or client_height <= 0:
        raise WindowCaptureError(f"window has an empty client area: {window_title!r}")

    if crop_pixel_size is not None:
        pixel_width, pixel_height = map(int, crop_pixel_size)
        if pixel_width <= 0 or pixel_height <= 0:
            raise ValueError("pixel capture crop must have positive dimensions")
        source_x = 0
        source_y = 0
        source_right = min(client_width, pixel_width)
        source_bottom = min(client_height, pixel_height)
    else:
        crop_left, crop_top, crop_right, crop_bottom = crop_region
        if not (0.0 <= crop_left < crop_right <= 1.0
                and 0.0 <= crop_top < crop_bottom <= 1.0):
            raise ValueError(f"invalid normalized capture crop: {crop_region!r}")
        source_x = round(crop_left * client_width)
        source_y = round(crop_top * client_height)
        source_right = round(crop_right * client_width)
        source_bottom = round(crop_bottom * client_height)
    width = max(1, source_right - source_x)
    height = max(1, source_bottom - source_y)

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
            (source_x, source_y),
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

    return image, (
        screen_left + source_x,
        screen_top + source_y,
        screen_left + source_right,
        screen_top + source_bottom,
    )


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
        capture_region: NormalizedBox = (0.0, 0.0, 1.0, 1.0),
        capture_pixel_size: Optional[tuple[int, int]] = None,
        status_capture_region: Optional[NormalizedBox] = None,
        status_capture_interval: Optional[float] = None,
        capture_enabled_event: Optional[threading.Event] = None,
        fast_capture_event: Optional[threading.Event] = None,
        fast_interval: float = 0.10,
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
        self.capture_region = capture_region
        self.capture_pixel_size = capture_pixel_size
        self.status_capture_region = status_capture_region
        self.status_capture_interval = (
            max(0.05, float(status_capture_interval))
            if status_capture_interval is not None else None
        )
        self.capture_enabled_event = capture_enabled_event
        self.fast_capture_event = fast_capture_event
        self.fast_interval = max(0.02, min(float(fast_interval), self.interval))
        self._uses_default_capture = capture_fn is None
        self.log = logging.getLogger(__name__)
        self._last_debug_path: Optional[Path] = None
        self._capture_requested = threading.Event()

    def active_interval(self) -> float:
        if self.fast_capture_event is not None and self.fast_capture_event.is_set():
            return self.fast_interval
        return self.interval

    def capture_now(self, timeout: float = 2.0) -> CapturedFrame:
        """Request a capture begun after this call and wait for its frame."""

        requested_at = time.monotonic()
        latest = self.bus.latest
        after_sequence = latest.sequence if latest is not None else -1
        deadline = requested_at + max(0.1, float(timeout))
        self._capture_requested.set()
        while not self.stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            frame = self.bus.wait_for_new(after_sequence, remaining)
            if frame is None:
                break
            if frame.captured_monotonic >= requested_at:
                return frame
            # A scheduled capture already in progress when the button was
            # clicked is stale for recording. Wait for the requested one.
            after_sequence = frame.sequence
        raise TimeoutError("immediate game capture timed out")

    def run(self) -> None:
        if self.debug_dir is not None:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            self._remove_stale_debug_frames()

        sequence = 0
        next_capture = time.monotonic()
        next_status_capture = 0.0
        while not self.stop_event.is_set():
            capture_interval = self.active_interval()
            now = time.monotonic()
            if next_capture - now > capture_interval:
                # A fast-capture action just began. Do not wait out the prior
                # normal patrol deadline before switching cadence.
                next_capture = now
            forced = self._capture_requested.is_set()
            enabled = (
                self.capture_enabled_event is None
                or self.capture_enabled_event.is_set()
            )
            if not enabled and not forced:
                next_capture = time.monotonic()
                self._capture_requested.wait(0.05)
                if self.stop_event.is_set():
                    break
                continue
            delay = max(0.0, next_capture - time.monotonic())
            if delay > 0.0 and not forced:
                self._capture_requested.wait(min(delay, 0.05))
                if self.stop_event.is_set():
                    break
                continue
            forced = self._capture_requested.is_set()
            enabled = (
                self.capture_enabled_event is None
                or self.capture_enabled_event.is_set()
            )
            if not enabled and not forced:
                continue
            if forced:
                # Clear before starting so a second request arriving during
                # capture remains set and receives another newer frame.
                self._capture_requested.clear()

            captured_monotonic = time.monotonic()
            try:
                if self._uses_default_capture:
                    image, window_rect = capture_window(
                        self.window_title,
                        self.capture_region,
                        self.capture_pixel_size,
                    )
                    status_image = None
                    if (self.status_capture_region is not None
                            and (self.status_capture_interval is None
                                 or captured_monotonic >= next_status_capture)):
                        status_image, _status_rect = capture_window(
                            self.window_title, self.status_capture_region
                        )
                        if self.status_capture_interval is not None:
                            next_status_capture = (
                                captured_monotonic + self.status_capture_interval
                            )
                else:
                    image, window_rect = self.capture_fn(self.window_title)
                    status_image = None
                frame = CapturedFrame(
                    sequence=sequence,
                    captured_at=datetime.now(timezone.utc),
                    captured_monotonic=captured_monotonic,
                    image=image,
                    window_rect=window_rect,
                    status_image=status_image,
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

            next_capture += capture_interval
            now = time.monotonic()
            if next_capture <= now:
                # Skip missed ticks instead of emitting a burst of stale frames.
                missed = int((now - next_capture) // capture_interval) + 1
                next_capture += missed * capture_interval

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
