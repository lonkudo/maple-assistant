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
from typing import Any, Callable, Iterable, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

from image_io import frame_files, save_screenshot, screenshot_name
from minimap_detector import hud_scale_for

LOG = logging.getLogger(__name__)
# A repeating capture failure logs a full traceback this often; the ones in between are DEBUG.
CAPTURE_FAILURE_LOG_SECONDS = 10.0


WindowRect = Tuple[int, int, int, int]
Box = Tuple[int, int, int, int]
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
    pixel_region: Optional[Box] = None,
) -> tuple[Image.Image, WindowRect]:
    """Capture the visible client area of a Windows window as an RGB image.

    ``crop_region`` is a normalized fraction of the client (legacy).
    ``pixel_region`` (left, top, right, bottom) is in ABSOLUTE client pixels;
    negative/zero y values are measured from the BOTTOM edge (the HUD is
    fixed pixel, so the HP/MP bars stay anchored to the bottom of the window
    when it is resized).  ``pixel_region`` wins when both are provided.
    """

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

    if pixel_region is not None:
        source_x, source_top, source_right, source_bottom = pixel_region
        if source_top < 0:
            source_top = client_height + source_top
        if source_bottom <= 0:
            source_bottom = client_height + source_bottom
        source_x = max(0, min(client_width, int(source_x)))
        source_right = max(source_x + 1, min(client_width, int(source_right)))
        source_top = max(0, min(client_height, int(source_top)))
        source_bottom = max(source_top + 1, min(client_height, int(source_bottom)))
        # ``height`` below reads ``source_y``; keep both names in sync.
        source_y = source_top
    elif crop_pixel_size is not None:
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

    def _grab(source: Callable[[], tuple[Any, int, int]]) -> Image.Image:
        """One BitBlt attempt from ``source()``; releases EVERYTHING it took, without raising.

        ``source()`` returns ``(source_dc, dc_handle, dc_hwnd)``: the DC handle and its owner window
        are what ``ReleaseDC`` needs, and both are released here even when the bitmap could not be
        created at all.  The old code took ``GetDC(hwnd)`` outside the try block and called
        ``DeleteObject(bitmap.GetHandle())`` unconditionally, so a failed attempt leaked a DC AND
        turned the real error into ``pywintypes.error: (0, 'DeleteObject', ...)``.
        """

        source_dc = None
        memory_dc = None
        bitmap = None
        dc_handle = 0
        dc_hwnd = 0
        try:
            source_dc, dc_handle, dc_hwnd = source()
            memory_dc = source_dc.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
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
            return Image.frombuffer(
                "RGB", (width, height), raw_bgra, "raw", "BGRX", 0, 1
            ).copy()
        finally:
            try:
                handle = bitmap.GetHandle() if bitmap is not None else 0
            except Exception:
                LOG.debug("capture: bitmap handle could not be read", exc_info=True)
                handle = 0
            if handle:
                try:
                    win32gui.DeleteObject(handle)
                except Exception:
                    LOG.debug("capture: bitmap could not be released", exc_info=True)
            for dc, label in ((memory_dc, "memory DC"), (source_dc, "source DC")):
                if dc is None:
                    continue
                try:
                    dc.DeleteDC()
                except Exception:
                    LOG.debug("capture: %s could not be released", label, exc_info=True)
            if dc_handle:
                try:
                    win32gui.ReleaseDC(dc_hwnd, dc_handle)
                except Exception:
                    LOG.debug("capture: window DC could not be released", exc_info=True)

    # GetDC(hwnd) has its origin at the client area's upper-left. GetWindowDC
    # would include borders/title bar and offset the pixels from window_rect.
    def _window_source() -> tuple[Any, int, int]:
        handle = win32gui.GetDC(hwnd)
        return win32ui.CreateDCFromHandle(handle), handle, hwnd

    def _screen_source() -> tuple[Any, int, int]:
        # Desktop DC: the BitBlt source point must be in SCREEN pixels then.
        handle = win32gui.GetDC(0)
        return win32ui.CreateDCFromHandle(handle), handle, 0

    errors: list[str] = []
    # Attempt 1 and a single retry: GDI/DC exhaustion is transient far more often than it is fatal.
    for attempt in (1, 2):
        try:
            image = _grab(_window_source)
        except Exception as exc:
            errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            LOG.debug("capture: attempt %d for %r failed", attempt, window_title,
                      exc_info=True)
            if attempt == 1:
                time.sleep(0.02)
            continue
        return image, (
            screen_left + source_x,
            screen_top + source_y,
            screen_left + source_right,
            screen_top + source_bottom,
        )

    # Last resort: read the same client rectangle off the desktop DC.  Only while the game window
    # is foreground - otherwise the frame would show whatever covers it (our own panel).
    try:
        foreground = int(win32gui.GetForegroundWindow() or 0)
    except Exception:
        foreground = 0
    if foreground == hwnd:
        screen_x = screen_left + source_x
        screen_y = screen_top + source_y
        source_x, source_y = screen_x, screen_y
        try:
            image = _grab(_screen_source)
        except Exception as exc:
            errors.append(f"screen fallback: {type(exc).__name__}: {exc}")
        else:
            LOG.warning(
                "capture: window DC failed for %r, used the desktop DC for the same client "
                "rectangle (%s)",
                window_title, "; ".join(errors),
            )
            return image, (
                screen_left + screen_x,
                screen_top + screen_y,
                screen_left + source_right,
                screen_top + source_bottom,
            )
    else:
        errors.append("screen fallback skipped: the game window is not foreground")

    raise WindowCaptureError(
        f"capture failed for {window_title!r}: " + "; ".join(errors)
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
        status_capture_pixel_region: Optional[Box] = None,
        status_capture_box_provider: Optional[Callable[[tuple[int, int]], Box]] = None,
        status_capture_interval: Optional[float] = None,
        capture_enabled_event: Optional[threading.Event] = None,
        fast_capture_event: Optional[threading.Event] = None,
        fast_interval: float = 0.10,
        # Lie-pass ultra-fast cadence: while its event is set the capture runs at
        # this interval, so a lie pass is fed at ~30 fps (the removed local pass
        # used it; the API pass raises the same event for its bursts).
        lie_capture_event: Optional[threading.Event] = None,
        lie_interval: float = 1.0 / 30.0,
        # === ADDED DEBUG FLAG ===
        debug_draw_regions: bool = False,
        # Reference-size ABSOLUTE client pixels of the minimap search ROI
        # (scaled per frame by ``hud_scale_for``; NOT normalized).
        debug_minimap_fallback: Optional[Box] = None,
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
        self.status_capture_pixel_region = status_capture_pixel_region
        self.status_capture_box_provider = status_capture_box_provider
        self.status_capture_interval = (
            max(0.05, float(status_capture_interval))
            if status_capture_interval is not None else None
        )
        self.capture_enabled_event = capture_enabled_event
        self.fast_capture_event = fast_capture_event
        self.fast_interval = max(0.02, min(float(fast_interval), self.interval))
        self.lie_capture_event = lie_capture_event
        self.lie_interval = max(0.02, min(float(lie_interval), self.interval))
        self._uses_default_capture = capture_fn is None
        self.log = logging.getLogger(__name__)
        self._last_debug_path: Optional[Path] = None
        self._capture_requested = threading.Event()
        # === store debug flag ===
        self.debug_draw_regions = debug_draw_regions
        self.debug_minimap_fallback = debug_minimap_fallback

    def active_interval(self) -> float:
        # The lie-pass cadence has the highest priority: a pass
        # needs ~30 fps while it drives the cursor.
        if (self.lie_capture_event is not None
                and self.lie_capture_event.is_set()):
            return self.lie_interval
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
        self._capture_failures = 0
        self._last_capture_failure_log = float("-inf")
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
                    if ((self.status_capture_region is not None
                         or self.status_capture_pixel_region is not None
                         or self.status_capture_box_provider is not None)
                            and (self.status_capture_interval is None
                                 or captured_monotonic >= next_status_capture)):
                        # A box provider computes the status box from the
                        # CURRENT client size (bottom-anchored fixed-pixel
                        # HUD); otherwise use the static pixel/normalized
                        # region.
                        pixel_box = self.status_capture_pixel_region
                        if (self.status_capture_box_provider is not None
                                and image is not None):
                            pixel_box = self.status_capture_box_provider(
                                image.size
                            )
                        # The status worker consumes a crop from THIS full-client frame.  A second
                        # window capture would make status and minimap observe different moments and
                        # violate the one shared 5 fps capture workflow.
                        if pixel_box is None:
                            left, top, right, bottom = self.status_capture_region or (0, 0, 1, 1)
                            width, height = image.size
                            pixel_box = (
                                round(left * width), round(top * height),
                                round(right * width), round(bottom * height),
                            )
                        status_image = image.crop(pixel_box)
                        if self.status_capture_interval is not None:
                            next_status_capture = (
                                captured_monotonic + self.status_capture_interval
                            )
                else:
                    image, window_rect = self.capture_fn(self.window_title)
                    status_image = None

                # ========== ADDED: draw debug rectangles ONLY on copied debug image ==========
                drawn_debug_image: Optional[Image.Image] = None
                if self.debug_draw_regions:
                    # critical: make copy, NEVER draw on original image used for processing
                    drawn_debug_image = image.copy()
                    draw = ImageDraw.Draw(drawn_debug_image)
                    w, h = drawn_debug_image.size

                    # Red: main capture_region
                    cr_x1, cr_y1, cr_x2, cr_y2 = self.capture_region
                    draw.rectangle(
                        (int(cr_x1 * w), int(cr_y1 * h), int(cr_x2 * w), int(cr_y2 * h)),
                        outline=(255, 0, 0), width=2
                    )
                    # Blue: status capture region
                    if self.status_capture_region is not None:
                        sr_x1, sr_y1, sr_x2, sr_y2 = self.status_capture_region
                        draw.rectangle(
                            (int(sr_x1 * w), int(sr_y1 * h), int(sr_x2 * w), int(sr_y2 * h)),
                            outline=(0, 120, 255), width=2
                        )
                    elif (self.status_capture_pixel_region is not None
                          or self.status_capture_box_provider is not None):
                        pixel_box = self.status_capture_pixel_region
                        if (self.status_capture_box_provider is not None
                                and image is not None):
                            pixel_box = self.status_capture_box_provider(
                                image.size
                            )
                        if pixel_box is not None:
                            draw.rectangle(pixel_box, outline=(0, 120, 255), width=2)
                    # Green: minimap fallback search ROI (reference-size
                    # absolute client pixels, scaled by the HUD scale factor)
                    if self.debug_minimap_fallback is not None:
                        x1, y1, x2, y2 = self.debug_minimap_fallback
                        hud_scale = hud_scale_for(w)
                        draw.rectangle(
                            (int(x1 * hud_scale), int(y1 * hud_scale),
                             int(x2 * hud_scale), int(y2 * hud_scale)),
                            outline=(0, 255, 0),
                            width=2
                        )
                # Do NOT draw anything on variable `image` here, original image stays clean for opencv

                frame = CapturedFrame(
                    sequence=sequence,
                    captured_at=datetime.now(timezone.utc),
                    captured_monotonic=captured_monotonic,
                    image=image,
                    window_rect=window_rect,
                    status_image=status_image,
                )
                self.bus.publish(frame)

                # save debug frame: if overlay is enabled, swap image for saving only
                if self.debug_dir is not None:
                    if drawn_debug_image is not None:
                        # create temporary frame copy for saving, replace image with drawn overlay
                        debug_save_frame = CapturedFrame(
                            sequence=frame.sequence,
                            captured_at=frame.captured_at,
                            captured_monotonic=frame.captured_monotonic,
                            image=drawn_debug_image,
                            window_rect=frame.window_rect,
                            status_image=frame.status_image,
                        )
                        self._save_debug_frame(debug_save_frame)
                    else:
                        self._save_debug_frame(frame)

                sequence += 1
            except Exception as exc:
                # A temporarily obscured/minimized/restarting game should not
                # silently kill all three workers. The orchestrator can still
                # stop this thread immediately through stop_event.
                # Rate-limited: a persistent failure (GDI exhaustion, a locked session) otherwise
                # writes a full traceback on every tick and buries everything else in the log.
                now_fail = time.monotonic()
                self._capture_failures += 1
                if (now_fail - self._last_capture_failure_log
                        >= CAPTURE_FAILURE_LOG_SECONDS):
                    suppressed = self._capture_failures - 1
                    self._last_capture_failure_log = now_fail
                    self.log.exception(
                        "could not capture game window (failure #%d%s)",
                        self._capture_failures,
                        f", {suppressed} identical since the last report" if suppressed else "",
                    )
                else:
                    self.log.debug("could not capture game window: %s", exc)

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
        path = self.debug_dir / screenshot_name(f"frame-{frame.sequence:06d}-{stamp}")
        previous = self._last_debug_path
        self._last_debug_path = None
        if previous is not None and previous != path:
            try:
                previous.unlink(missing_ok=True)
            except OSError:
                self.log.warning("could not remove used debug frame %s", previous,
                                 exc_info=True)
        # JPG, not PNG: measured 4.55 ms vs 28.75 ms per 1366x768 frame and 379 KB vs
        # 1033 KB (see image_io).  Nothing matches against a debug dump.
        saved = save_screenshot(path, frame.image)
        if saved is not None:
            self._last_debug_path = saved

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
        """Remove screenshots left by an earlier interrupted run.

        JPG is the project format, and a PNG dump from a release before the switch is
        swept up too so it does not linger forever.
        """

        assert self.debug_dir is not None
        stale = list(frame_files(self.debug_dir))
        stale.extend(self.debug_dir.glob("frame-*.png"))
        for path in stale:
            try:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            except OSError:
                self.log.warning("could not remove stale debug frame %s", path,
                                 exc_info=True)


# One shared game-capture cadence: 5 fps, whether patrol is running or the parked watch is active.
LIE_WATCH_INTERVAL_SECONDS = 0.2


@dataclass(frozen=True)
class WatchFeed:
    """One consumer the parked watch feeds, plus the settings that arm and bound it."""

    queue: "queue.Queue[CapturedFrame]"
    label: str
    # The selection that arms this feed (None = always armed while the watch runs).
    armed_event: Optional[threading.Event] = None
    # The capture cadence this consumer's logic was calibrated for.  The disconnect detector's threshold
    # is a FRAME COUNT (40 frames at the normal 0.25s capture = 10s), so feeding it at half the normal
    # rate would silently double the detection time - each feed carries its own interval instead.
    interval: float = LIE_WATCH_INTERVAL_SECONDS
    # Some consumers may only judge a frame when the game window really is in front.  A capture of the
    # game window while the assistant's own panel covers it is not the game: the character worker would
    # count that as "the marker is missing" and fire a false 掉线 alert (and a reconnect that clicks and
    # types into the game).  The lie detector needs no such guard - only its exact HUD square can match.
    requires_game_foreground: bool = False


class ParkedWatchCapture(threading.Thread):
    """Keep the event-driven detectors fed while the shared capture is parked.

    The operator's requirement: 自动重连 and 自动过测谎 are NOT patrol workflows - "even if the patrol is
    not started they should normally work".  Both hang off the shared capture, and that capture is
    deliberately idle before Start Patrol (``game_focused`` needs armed keyboard input).  So while parked
    the lie detector starved (no lie window was ever caught) and the disconnect detector - which lives in
    the character worker and is fed from the same bus - never saw a frame (a 掉线 was never noticed, so
    the reconnect could not run either).

    This thread grabs the game window on its own and publishes ONLY into the queues it was given
    (``WatchFeed``), each behind its own arming setting, so the movement / attack / status workers stay
    exactly as idle as they are while parked.  It stands down whenever the shared capture runs
    (``patrol_capture_event`` is the same gate the capture uses), so there is never a double capture.

    It never foregrounds the game: while parked the operator may be using another window, and stealing
    focus to look for a lie square or a 掉线 prompt would be far worse than a missed scan.
    """

    def __init__(
        self,
        window_title: str,
        feeds: "Sequence[WatchFeed]",
        stop_event: threading.Event,
        *,
        patrol_capture_event: Optional[threading.Event] = None,
        foreground_check: Optional[Callable[[], bool]] = None,
        capture_fn: Optional[CaptureFunction] = None,
    ) -> None:
        super().__init__(name="parked-watch-capture", daemon=True)
        self.window_title = window_title
        self.feeds = tuple(feeds)
        self.stop_event = stop_event
        self.patrol_capture_event = patrol_capture_event
        self.foreground_check = foreground_check
        self.capture_fn = capture_fn or (lambda title: capture_window(title))
        self._sequence = 0
        self._last_state: Optional[str] = None

    # ------------------------------------------------------------------ state
    def _shared_capture_running(self) -> bool:
        return bool(
            self.patrol_capture_event is not None
            and self.patrol_capture_event.is_set()
        )

    def _game_foreground(self) -> bool:
        if self.foreground_check is None:
            return True
        try:
            return bool(self.foreground_check())
        except Exception:
            return False

    def _armed_feeds(self) -> list[WatchFeed]:
        return [
            feed for feed in self.feeds
            if feed.armed_event is None or feed.armed_event.is_set()
        ]

    def _active_feeds(self) -> list[WatchFeed]:
        return [
            feed for feed in self._armed_feeds()
            if not feed.requires_game_foreground or self._game_foreground()
        ]

    def _idle_reason(self) -> str:
        armed = self._armed_feeds()
        if not armed:
            return "no selection is armed (" + ", ".join(f.label for f in self.feeds) + ")"
        if self._shared_capture_running():
            return "the shared patrol capture is running"
        waiting = [f.label for f in armed if f.requires_game_foreground]
        return ("waiting for the game window to come to the foreground ("
                + ", ".join(waiting) + ")")

    def _active_interval(self, active: "Sequence[WatchFeed]") -> float:
        if not active:
            return LIE_WATCH_INTERVAL_SECONDS
        return max(0.05, min(float(feed.interval) for feed in active))

    def _publish_latest(self, target: "queue.Queue[CapturedFrame]",
                        frame: CapturedFrame) -> None:
        """Keep only the newest frame, exactly like :class:`FrameBus` does."""

        while True:
            try:
                target.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break
        try:
            target.put_nowait(frame)
        except queue.Full:
            pass

    # --------------------------------------------------------------------- run
    def run(self) -> None:
        LOG.info(
            "parked watch started (window=%s, feeds=%s); it feeds them only while the patrol capture "
            "is parked",
            self.window_title, ", ".join(feed.label for feed in self.feeds),
        )
        next_capture = time.monotonic() + LIE_WATCH_INTERVAL_SECONDS
        while not self.stop_event.is_set():
            active = self._active_feeds()
            state = "watching" if active else f"idle: {self._idle_reason()}"
            interval = self._active_interval(active)
            if state != self._last_state:
                self._last_state = state
                if active:
                    LOG.warning(
                        "PARKED WATCH: the patrol capture is parked - grabbing %s every %.2fs for %s "
                        "(the patrol does not have to be started)",
                        self.window_title, interval,
                        ", ".join(feed.label for feed in active),
                    )
                else:
                    LOG.info("PARKED WATCH: %s", state)
                next_capture = time.monotonic() + interval
            if not active:
                next_capture = time.monotonic() + interval
                if self.stop_event.wait(0.2):
                    break
                continue
            delay = next_capture - time.monotonic()
            if delay > 0.0:
                if self.stop_event.wait(min(delay, 0.2)):
                    break
                continue
            next_capture = time.monotonic() + interval
            try:
                image, window_rect = self.capture_fn(self.window_title)
                if image is None:
                    continue
                self._sequence += 1
                frame = CapturedFrame(
                    sequence=self._sequence,
                    captured_at=datetime.now(timezone.utc),
                    captured_monotonic=time.monotonic(),
                    image=image,
                    window_rect=window_rect,
                )
                for feed in active:
                    self._publish_latest(feed.queue, frame)
            except Exception:
                # A minimised/restarting/hidden game must not spam, and a failure here must never kill
                # this thread: it is supervised like every other core worker.
                LOG.debug("parked watch: capture/publish failed", exc_info=True)
        LOG.info("parked watch stopped")
