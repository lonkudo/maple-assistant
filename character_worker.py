"""Character-position detection worker.

Detects the yellow player diamond on EVERY dispatched frame and publishes
the normalised position (x, y, confidence) to a position queue.  Layer
detection and the movement worker consume this single dispatched source of
truth instead of each worker re-detecting the marker on its own cadence, so
the character position is followed every frame even while movement input is
paused / suppressed (focus dips, stale climb input) - the freeze the old
internal cadence caused on layer1 is gone.

The worker never gates on focus or movement state: it only looks at frames.
"""

from __future__ import annotations

import logging
from pathlib import Path
import queue
import threading
from threading import Thread
from typing import Any, Callable, Optional

import numpy as np

from marker_detector import detect_yellow_diamond
from countdown_worker import play_mp3

LOG = logging.getLogger(__name__)

# Fallback minimap region when the movement worker has not produced its
# stabilised box yet (normalised left/top/right/bottom).
DEFAULT_MINIMAP_REGION = (0.0, 0.0, 0.22, 0.27)


class CharacterPosition:
    """One dispatched marker reading (normalised minimap coordinates)."""

    __slots__ = (
        "x", "y", "confidence", "marker_pixel_size", "frame_sequence",
        "minimap_region",
    )

    def __init__(
        self,
        x: Optional[float],
        y: Optional[float],
        confidence: float,
        marker_pixel_size: Optional[tuple[int, int]] = None,
        frame_sequence: Optional[int] = None,
        minimap_region: Optional[tuple[float, float, float, float]] = None,
    ) -> None:
        self.x = x
        self.y = y
        self.confidence = confidence
        self.marker_pixel_size = marker_pixel_size
        self.frame_sequence = frame_sequence
        self.minimap_region = minimap_region


def _crop_minimap(image: Any, region: tuple[float, float, float, float]) -> np.ndarray:
    width, height = image.size
    box = (
        max(0, min(width, int(region[0] * width))),
        max(0, min(height, int(region[1] * height))),
        max(0, min(width, int(region[2] * width))),
        max(0, min(height, int(region[3] * height))),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return np.zeros((1, 1, 3), dtype=np.uint8)
    return np.asarray(image.crop(box).convert("RGB"), dtype=np.uint8)


class CharacterWorker(Thread):
    """Per-frame yellow-diamond detector dispatching CharacterPosition."""

    def __init__(
        self,
        frame_queue: "queue.Queue[Any]",
        position_queue: "queue.Queue[CharacterPosition]",
        stop_event: Any,
        minimap_region_provider: Optional[Callable[[], tuple[float, float, float, float]]] = None,
        disconnect_alert_enabled: bool = False,
        disconnect_alert_misses: int = 3,
        alert_sound_path: Optional[Path] = None,
        play_alert_sound: Optional[Callable[[Path], None]] = None,
        flash_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(name="character-worker", daemon=True)
        self.frame_queue = frame_queue
        self.position_queue = position_queue
        self.stop_event = stop_event
        self._region_provider = minimap_region_provider
        self.minimap_region = DEFAULT_MINIMAP_REGION
        self._last_frame: Any = None
        self._disconnect_alert_lock = threading.Lock()
        self._disconnect_alert_enabled = bool(disconnect_alert_enabled)
        self._disconnect_alert_misses = max(1, int(disconnect_alert_misses))
        self._disconnect_missing_frames = 0
        self._disconnect_alerted = False
        self._alert_sound_path = Path(
            alert_sound_path
            if alert_sound_path is not None
            else Path(__file__).resolve().parent / "sound" / "beep.mp3"
        )
        self._play_alert_sound = play_alert_sound or play_mp3
        self._flash_callback = flash_callback

    def set_disconnect_alert(self, enabled: bool) -> None:
        """Enable/disable the missing-yellow-marker alarm live from the UI."""

        with self._disconnect_alert_lock:
            self._disconnect_alert_enabled = bool(enabled)
            self._disconnect_missing_frames = 0
            self._disconnect_alerted = False
        LOG.info("disconnect alert %s", "enabled" if enabled else "disabled")

    @property
    def disconnect_alert_enabled(self) -> bool:
        with self._disconnect_alert_lock:
            return self._disconnect_alert_enabled

    def _play_disconnect_alert(self) -> None:
        if self._flash_callback is not None:
            try:
                self._flash_callback()
            except Exception:
                LOG.warning("disconnect alert screen blink failed", exc_info=True)
        try:
            self._play_alert_sound(self._alert_sound_path)
        except Exception:
            LOG.warning("disconnect alert sound failed", exc_info=True)

    def _update_disconnect_alert(self, detected: bool) -> None:
        """Consume the existing marker result; never runs another detector."""

        should_alert = False
        with self._disconnect_alert_lock:
            if not self._disconnect_alert_enabled:
                self._disconnect_missing_frames = 0
                self._disconnect_alerted = False
                return
            if detected:
                self._disconnect_missing_frames = 0
                self._disconnect_alerted = False
                return
            self._disconnect_missing_frames += 1
            if (self._disconnect_missing_frames >= self._disconnect_alert_misses
                    and not self._disconnect_alerted):
                self._disconnect_alerted = True
                should_alert = True
        if should_alert:
            LOG.warning(
                "DISCONNECT ALERT: yellow character marker missing for %d "
                "consecutive frames; playing beep",
                self._disconnect_missing_frames,
            )
            # MCI playback waits until the MP3 ends. Keep marker detection at
            # full cadence by moving only audio playback to a tiny daemon.
            threading.Thread(
                target=self._play_disconnect_alert,
                name="disconnect-alert-sound",
                daemon=True,
            ).start()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                frame = self.frame_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                if self._region_provider is not None:
                    region = self._region_provider()
                    if region is not None:
                        self.minimap_region = region
                image = frame.image
                rgb = _crop_minimap(image, self.minimap_region)
                detection = detect_yellow_diamond(rgb)
                # The disconnect alarm consumes this exact result. There is
                # deliberately no second crop or yellow-marker detection.
                self._update_disconnect_alert(detection is not None)
                if detection is not None:
                    position = CharacterPosition(
                        getattr(detection, "x", None),
                        getattr(detection, "y", None),
                        float(getattr(detection, "confidence", 0.0)),
                        getattr(detection, "marker_pixel_size", None),
                        getattr(frame, "sequence", None),
                        tuple(self.minimap_region),
                    )
                else:
                    position = CharacterPosition(
                        None, None, 0.0,
                        frame_sequence=getattr(frame, "sequence", None),
                        minimap_region=tuple(self.minimap_region),
                    )
                try:
                    self.position_queue.put_nowait(position)
                except queue.Full:
                    try:
                        self.position_queue.get_nowait()
                    except queue.Empty:
                        pass
                    self.position_queue.put_nowait(position)
                self._last_frame = frame
            except Exception:
                LOG.exception("character detection failed on a frame")
