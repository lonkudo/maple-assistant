"""One-second, full-client lie-event detector using in-memory frames only.

A lie event is a HUD square of the exact color #c9ced0 (201, 206, 208), so
it follows the HUD scale factor: measured 60x60 at a 1075px-wide client;
at/above the 1366px HUD reference width (1920x1080, 1366x768) it is
60 * 1366/1075 ~= 76x76.  Frames are consumed from the existing
full-window FrameBus; this worker never captures or writes an image, so
there are no used screenshot files to clean up.
"""

from __future__ import annotations

import logging
from pathlib import Path
import queue
import threading
import time
from typing import Any, Callable, Optional

import cv2
import numpy as np

from countdown_worker import play_mp3, run_sound_async
from minimap_detector import hud_scale_for


LOG = logging.getLogger(__name__)
# The lie square is a HUD element: color #c9ced0, measured 60x60 at a
# 1075px-wide client, so the HUD-reference (>=1366px) size is
# round(60 * 1366/1075) = 76x76.
LIE_SQUARE_COLOR = (201, 206, 208)  # #c9ced0
# Small per-channel tolerance (0-255) so rendering/antialiasing shifts of
# the exact color still match.
LIE_COLOR_TOLERANCE = 10
REFERENCE_LIE_SQUARE_SIZE = (76, 76)


def scaled_lie_square_size(width: int, height: int) -> tuple[int, int]:
    """Scale the 76x76 HUD reference target to the current client.

    The lie square is a HUD element: fixed pixel at/above the 1366px
    reference width and scaled down with the HUD below it (60x60 at a
    1075px-wide client).  ``height`` is kept in the signature for call-site
    compatibility; the HUD scale is driven by the client width.
    """

    scale = hud_scale_for(width)
    square_width, square_height = REFERENCE_LIE_SQUARE_SIZE
    return (
        max(1, int(round(square_width * scale))),
        max(1, int(round(square_height * scale))),
    )


def detect_lie_square(
    image: Any,
) -> Optional[tuple[int, int, int, int]]:
    """Return one scaled #c9ced0 rectangle as ``x, y, width, height``.

    OpenCV erosion asks whether the full scaled kernel fits inside the
    #c9ced0 mask (each channel within ``LIE_COLOR_TOLERANCE``). It searches
    the full client in one native operation and avoids a slow Python
    sliding-window loop.
    """

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        return None
    height, width = rgb.shape[:2]
    target_width, target_height = scaled_lie_square_size(width, height)
    if target_width > width or target_height > height:
        return None
    color = np.asarray(LIE_SQUARE_COLOR, dtype=np.uint8)
    tolerance = int(LIE_COLOR_TOLERANCE)
    lower = np.asarray(
        [max(0, int(channel) - tolerance) for channel in color], dtype=np.uint8
    )
    upper = np.asarray(
        [min(255, int(channel) + tolerance) for channel in color], dtype=np.uint8
    )
    mask = cv2.inRange(rgb[:, :, :3], lower, upper)
    kernel = np.ones((target_height, target_width), dtype=np.uint8)
    matches = cv2.erode(
        mask,
        kernel,
        borderType=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    locations = np.argwhere(matches == 255)
    if locations.size == 0:
        return None
    center_y, center_x = (int(value) for value in locations[0])
    left = max(0, min(width - target_width, center_x - target_width // 2))
    top = max(0, min(height - target_height, center_y - target_height // 2))
    return left, top, target_width, target_height


class LieDetectorWorker(threading.Thread):
    """Sample the shared full-client frame every second."""

    def __init__(
        self,
        frame_queue: "queue.Queue[Any]",
        stop_event: threading.Event,
        *,
        enabled: bool = False,
        scan_interval: float = 1.0,
        sound_path: Optional[Path] = None,
        play_alert_sound: Optional[Callable[[Path], None]] = None,
        flash_callback: Optional[Callable[[], None]] = None,
        alert_callback: Optional[Callable[[str], None]] = None,
        lie_seen_callback: Optional[
            Callable[[Optional[tuple[int, int, int, int]], Any], None]
        ] = None,
    ) -> None:
        super().__init__(name="lie-detector-worker", daemon=True)
        self.frame_queue = frame_queue
        self.stop_event = stop_event
        self.scan_interval = max(0.05, float(scan_interval))
        self.sound_path = Path(
            sound_path
            if sound_path is not None
            else Path(__file__).resolve().parent / "sound" / "dingdong.mp3"
        )
        self._play_alert_sound = play_alert_sound or play_mp3
        self._flash_callback = flash_callback
        self._alert_callback = alert_callback
        # Optional consumers for the raw square event (e.g. the auto
        # lie-pass GPU tracker and the diagnostic screenshot recorder): each
        # receives (match, frame) on a NEW event and (None, frame) when a
        # previously alerted square clears.  set_lie_seen_callback replaces
        # the whole list; add_lie_seen_callback appends one listener.
        self._lie_seen_callbacks: list = []
        if lie_seen_callback is not None:
            self._lie_seen_callbacks.append(lie_seen_callback)
        self._lock = threading.Lock()
        self._enabled = bool(enabled)
        self._sound_enabled = True
        self._next_scan_at = (
            time.monotonic() + self.scan_interval if enabled else None
        )
        self._alerted_for_current_event = False
        # Rate limit for the "almost #c9ced0" diagnostic (see _log_near_miss).
        self._last_near_miss_log = 0.0

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Apply the checkbox live; a newly enabled scan starts after 1s."""

        now = time.monotonic()
        with self._lock:
            self._enabled = bool(enabled)
            self._next_scan_at = (
                now + self.scan_interval if self._enabled else None
            )
            self._alerted_for_current_event = False
        LOG.info("lie detector alert %s", "enabled" if enabled else "disabled")

    def set_sound_enabled(self, enabled: bool) -> None:
        """Enable/disable only audio; visual/message callbacks still fire."""

        with self._lock:
            self._sound_enabled = bool(enabled)

    def set_lie_seen_callback(
        self,
        callback: Optional[
            Callable[[Optional[tuple[int, int, int, int]], Any], None]
        ],
    ) -> None:
        """Replace all raw square-event consumers with a single one (or none)."""

        with self._lock:
            self._lie_seen_callbacks = (
                [callback] if callback is not None else []
            )

    def add_lie_seen_callback(
        self,
        callback: Callable[[Optional[tuple[int, int, int, int]], Any], None],
    ) -> None:
        """Append one more raw square-event consumer (e.g. a recorder)."""

        with self._lock:
            self._lie_seen_callbacks.append(callback)

    def _take_due_scan(self, now: float) -> bool:
        with self._lock:
            if not self._enabled:
                return False
            if self._next_scan_at is None:
                self._next_scan_at = now + self.scan_interval
                return False
            if now + 1e-9 < self._next_scan_at:
                return False
            self._next_scan_at = now + self.scan_interval
            return True

    def _play_alert(self) -> None:
        if self._flash_callback is not None:
            try:
                self._flash_callback()
            except Exception:
                LOG.warning("lie detector screen blink failed", exc_info=True)
        if self._alert_callback is not None:
            try:
                self._alert_callback("测谎警报")
            except Exception:
                LOG.warning("lie detector message callback failed", exc_info=True)
        with self._lock:
            sound_enabled = self._sound_enabled
        if sound_enabled:
            try:
                # Never play inline: a wedged audio device would freeze this
                # detector worker along with the rest of its loop.
                run_sound_async(
                    self._play_alert_sound,
                    self.sound_path,
                    name="lie-alert-sound",
                )
            except Exception:
                LOG.warning("lie detector alert sound failed", exc_info=True)

    def _update_alert(
        self,
        match: Optional[tuple[int, int, int, int]],
        frame: Any = None,
    ) -> None:
        should_alert = False
        with self._lock:
            if not self._enabled:
                return
            if match is None:
                self._alerted_for_current_event = False
            elif not self._alerted_for_current_event:
                self._alerted_for_current_event = True
                should_alert = True
            callbacks = list(self._lie_seen_callbacks)
        # Every listener runs isolated: one broken consumer (or a slow
        # recorder) must never break the alert loop or other listeners.
        for callback in callbacks:
            try:
                # New event -> (match, frame); cleared -> (None, frame).
                callback(match, frame)
            except Exception:
                LOG.warning(
                    "lie-detect: a lie-seen callback failed", exc_info=True
                )
        if callbacks and match is not None:
            LOG.info(
                "lie-detect: square event forwarded to %d callback(s) "
                "bbox=%s",
                len(callbacks),
                match,
            )
        if should_alert:
            LOG.warning(
                "LIE DETECTOR ALERT: #c9ced0 block detected at "
                "x=%d y=%d size=%dx%d; triggering reminders",
                *match,
            )
            threading.Thread(
                target=self._play_alert,
                name="lie-detector-alert-sound",
                daemon=True,
            ).start()

    def run(self) -> None:
        LOG.info("lie detector worker started interval=%.1fs",
                 self.scan_interval)
        while not self.stop_event.is_set():
            try:
                frame = self.frame_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                now = time.monotonic()
                if not self._take_due_scan(now):
                    continue
                # Work directly on the shared in-memory full-client image.
                # No screenshot file is created, retained, or deleted.
                match = detect_lie_square(frame.image)
                if match is None:
                    self._log_near_miss(frame.image, now)
                self._update_alert(match, frame)
            except Exception:
                LOG.exception("lie detector failed on a frame")
        LOG.info("lie detector worker stopped")

    def _log_near_miss(self, image: Any, now: float) -> None:
        """Diagnostic: describe an almost-#c9ced0 square, at most every 5s.

        A lie test that never rings leaves no evidence at all - the operator
        sees nothing happen.  This probes with a relaxed colour tolerance and a
        half-size kernel; when that matches, the log states the square's real
        size and colour, which is exactly what is needed to calibrate
        ``LIE_SQUARE_COLOR`` / ``REFERENCE_LIE_SQUARE_SIZE`` for that client.
        It never alerts and never triggers anything.
        """

        if now - self._last_near_miss_log < 5.0:
            return
        self._last_near_miss_log = now
        try:
            rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
            width = int(rgb.shape[1])
            target_width, target_height = scaled_lie_square_size(
                width, int(rgb.shape[0])
            )
            relaxed_width = max(4, target_width // 2)
            relaxed_height = max(4, target_height // 2)
            colour = np.asarray(LIE_SQUARE_COLOR, dtype=np.int16)
            tolerance = int(LIE_COLOR_TOLERANCE) * 3
            lower = np.asarray(
                [max(0, int(c) - tolerance) for c in colour], dtype=np.uint8
            )
            upper = np.asarray(
                [min(255, int(c) + tolerance) for c in colour], dtype=np.uint8
            )
            mask = cv2.inRange(rgb[:, :, :3], lower, upper)
            kernel = np.ones((relaxed_height, relaxed_width), dtype=np.uint8)
            matches = cv2.erode(
                mask, kernel, borderType=cv2.BORDER_CONSTANT, borderValue=0
            )
            locations = np.argwhere(matches == 255)
            if locations.size == 0:
                return
            center_y, center_x = (int(v) for v in locations[0])
            sample = rgb[center_y, center_x]
            LOG.info(
                "lie-detect: near miss - a %dx%d uniform block near "
                "RGB%s sits at (%d,%d); the exact rule wants %dx%d of RGB%s "
                "(+-%d).  Nothing was triggered.",
                relaxed_width, relaxed_height, tuple(int(v) for v in sample),
                center_x, center_y, target_width, target_height,
                tuple(int(v) for v in LIE_SQUARE_COLOR),
                int(LIE_COLOR_TOLERANCE),
            )
        except Exception:
            LOG.debug("lie-detect: near-miss probe failed", exc_info=True)


__all__ = [
    "LieDetectorWorker",
    "detect_lie_square",
    "scaled_lie_square_size",
]
