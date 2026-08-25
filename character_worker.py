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
import queue
from threading import Thread
from typing import Any, Callable, Optional

import numpy as np

from marker_detector import detect_yellow_diamond

LOG = logging.getLogger(__name__)

# Fallback minimap region when the movement worker has not produced its
# stabilised box yet (normalised left/top/right/bottom).
DEFAULT_MINIMAP_REGION = (0.0, 0.075, 0.12, 0.24)


class CharacterPosition:
    """One dispatched marker reading (normalised minimap coordinates)."""

    __slots__ = ("x", "y", "confidence", "marker_pixel_size")

    def __init__(
        self,
        x: Optional[float],
        y: Optional[float],
        confidence: float,
        marker_pixel_size: Optional[tuple[int, int]] = None,
    ) -> None:
        self.x = x
        self.y = y
        self.confidence = confidence
        self.marker_pixel_size = marker_pixel_size


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
    ) -> None:
        super().__init__(name="character-worker", daemon=True)
        self.frame_queue = frame_queue
        self.position_queue = position_queue
        self.stop_event = stop_event
        self._region_provider = minimap_region_provider
        self.minimap_region = DEFAULT_MINIMAP_REGION
        self._last_frame: Any = None

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
                if detection is not None:
                    position = CharacterPosition(
                        getattr(detection, "x", None),
                        getattr(detection, "y", None),
                        float(getattr(detection, "confidence", 0.0)),
                        getattr(detection, "marker_pixel_size", None),
                    )
                else:
                    position = CharacterPosition(None, None, 0.0)
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