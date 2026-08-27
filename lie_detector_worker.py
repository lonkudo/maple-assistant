"""One-second, full-client lie-event detector using in-memory frames only.

At the 1075x768 reference resolution a lie event contains a 40x40 block whose
pixels are all exactly white.  The target width and height scale independently
with the current captured client dimensions.  Frames are consumed from the
existing full-window FrameBus; this worker never captures or writes an image,
so there are no used screenshot files to clean up.
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

from countdown_worker import play_mp3


LOG = logging.getLogger(__name__)
REFERENCE_WINDOW_SIZE = (1075, 768)
REFERENCE_WHITE_SQUARE_SIZE = (40, 40)


def scaled_white_square_size(width: int, height: int) -> tuple[int, int]:
    """Scale the 40x40 reference target to the current client dimensions."""

    reference_width, reference_height = REFERENCE_WINDOW_SIZE
    square_width, square_height = REFERENCE_WHITE_SQUARE_SIZE
    return (
        max(1, int(round(square_width * width / reference_width))),
        max(1, int(round(square_height * height / reference_height))),
    )


def detect_pure_white_square(
    image: Any,
) -> Optional[tuple[int, int, int, int]]:
    """Return one scaled all-white rectangle as ``x, y, width, height``.

    OpenCV erosion asks whether the full scaled kernel fits inside the exact
    white-pixel mask. It searches the full client in one native operation and
    avoids a slow Python sliding-window loop.
    """

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        return None
    height, width = rgb.shape[:2]
    target_width, target_height = scaled_white_square_size(width, height)
    if target_width > width or target_height > height:
        return None
    white = np.all(rgb[:, :, :3] == 255, axis=2).astype(np.uint8) * 255
    kernel = np.ones((target_height, target_width), dtype=np.uint8)
    matches = cv2.erode(
        white,
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
    ) -> None:
        super().__init__(name="lie-detector-worker", daemon=True)
        self.frame_queue = frame_queue
        self.stop_event = stop_event
        self.scan_interval = max(0.05, float(scan_interval))
        self.sound_path = Path(
            sound_path
            if sound_path is not None
            else Path(__file__).resolve().parent / "sound" / "beep.mp3"
        )
        self._play_alert_sound = play_alert_sound or play_mp3
        self._lock = threading.Lock()
        self._enabled = bool(enabled)
        self._next_scan_at = (
            time.monotonic() + self.scan_interval if enabled else None
        )
        self._alerted_for_current_event = False

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
        try:
            self._play_alert_sound(self.sound_path)
        except Exception:
            LOG.warning("lie detector alert sound failed", exc_info=True)

    def _update_alert(self, match: Optional[tuple[int, int, int, int]]) -> None:
        should_alert = False
        with self._lock:
            if not self._enabled:
                return
            if match is None:
                self._alerted_for_current_event = False
            elif not self._alerted_for_current_event:
                self._alerted_for_current_event = True
                should_alert = True
        if should_alert:
            LOG.warning(
                "LIE DETECTOR ALERT: pure-white block detected at "
                "x=%d y=%d size=%dx%d; playing beep",
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
                match = detect_pure_white_square(frame.image)
                self._update_alert(match)
            except Exception:
                LOG.exception("lie detector failed on a frame")
        LOG.info("lie detector worker stopped")


__all__ = [
    "LieDetectorWorker",
    "detect_pure_white_square",
    "scaled_white_square_size",
]
