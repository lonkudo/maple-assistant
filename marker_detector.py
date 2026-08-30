"""Yellow minimap-diamond detection with center and size measurements."""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque
import statistics
import threading
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class MarkerDetection:
    x: float
    y: float
    confidence: float
    pixel_box: tuple[int, int, int, int]

    @property
    def pixel_size(self) -> tuple[int, int]:
        left, top, right, bottom = self.pixel_box
        return right - left, bottom - top


class DiamondSizeTracker:
    """Smooth animation noise while reacting immediately to genuine zoom."""

    def __init__(self, history: int = 7, zoom_change_ratio: float = 0.35) -> None:
        self._sizes: deque[tuple[int, int]] = deque(maxlen=max(3, history))
        self.zoom_change_ratio = float(zoom_change_ratio)
        self._lock = threading.Lock()

    def stabilize(self, size: tuple[int, int]) -> tuple[int, int]:
        width, height = max(1, int(size[0])), max(1, int(size[1]))
        with self._lock:
            if self._sizes:
                median_width = statistics.median(value[0] for value in self._sizes)
                median_height = statistics.median(value[1] for value in self._sizes)
                ratio = max(
                    abs(width - median_width) / max(1.0, median_width),
                    abs(height - median_height) / max(1.0, median_height),
                )
                if ratio > self.zoom_change_ratio:
                    self._sizes.clear()
            self._sizes.append((width, height))
            return (
                max(1, round(statistics.median(value[0] for value in self._sizes))),
                max(1, round(statistics.median(value[1] for value in self._sizes))),
            )


def _components(mask: np.ndarray, min_pixels: int = 2) -> list[np.ndarray]:
    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    result: list[np.ndarray] = []
    for start_y, start_x in np.argwhere(mask):
        if seen[start_y, start_x]:
            continue
        stack = [(int(start_y), int(start_x))]
        seen[start_y, start_x] = True
        points: list[tuple[int, int]] = []
        while stack:
            y, x = stack.pop()
            points.append((y, x))
            for ny in range(max(0, y - 1), min(height, y + 2)):
                for nx in range(max(0, x - 1), min(width, x + 2)):
                    if mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        if len(points) >= min_pixels:
            result.append(np.asarray(points, dtype=np.int32))
    return result


def detect_yellow_diamond(minimap_rgb: np.ndarray) -> Optional[MarkerDetection]:
    """Return normalized center, confidence, and colored-pixel bounding box."""

    rgb = minimap_rgb.astype(np.int16)
    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    # Accept both the golden snapshot color (255,255,136) and the pure/bright
    # yellow the live client renders (255,255,0 .. 255,240,80): the marker is
    # a small saturated yellow diamond, whatever its exact shade.  Long
    # orange/brown platform decorations stay excluded by requiring a strong
    # green channel (not orange) and a weak blue channel (not white), plus
    # the shape/compactness checks below.
    yellow = (
        (red >= 200)
        & (green >= 185)
        & (blue <= 180)
        & (red >= green * 0.92)
        & (green >= blue * 1.4)
    )
    yellow_body = (red >= 200) & (green >= 185) & (blue <= 180)
    height, width = yellow.shape
    candidates: list[tuple[float, MarkerDetection]] = []
    # The player diamond is SMALL: ~6-7 px at normal zoom on a 130-170 px
    # analysis box (~4-5% of the box's min dimension).  The minimap ZOOM can
    # change while the panel stays fixed, so the diamond may grow, but only
    # within a bounded range - a 48 px blob is never the marker.  The cap is
    # a generous ~18% of the min dimension (roughly 3-4x the normal diamond)
    # and the score below prefers candidates near the expected size, so the
    # right-size diamond wins while oversized yellow regions are rejected.
    min_dimension = min(width, height)
    expected_span = max(4, int(round(min_dimension * 0.05)))
    max_span = max(20, int(round(min_dimension * 0.18)))
    max_pixels = max(320, max_span * max_span)
    body_components = _components(yellow_body, min_pixels=3)
    for component in _components(yellow, min_pixels=3):
        ys, xs = component[:, 0], component[:, 1]
        strict_left, strict_top = int(xs.min()), int(ys.min())
        strict_right, strict_bottom = int(xs.max()) + 1, int(ys.max()) + 1
        span_x, span_y = strict_right - strict_left, strict_bottom - strict_top
        count = len(component)
        if count > max_pixels or span_x > max_span or span_y > max_span:
            continue
        aspect = span_x / max(1, span_y)
        compact = count / max(1, span_x * span_y)
        if 0.45 <= aspect <= 2.2 and compact >= 0.20 and span_x >= 2 and span_y >= 2:
            shape_score = max(0.0, 1.0 - abs(aspect - 1.0) / 2.0)
            # Prefer the size near the expected marker span; tolerate zoom
            # changes up to roughly 3-4x before the candidate scores zero.
            span = max(span_x, span_y)
            size_score = max(
                0.0, 1.0 - abs(span - expected_span) / max(1.0, expected_span * 3)
            )
            score = 0.50 * compact + 0.30 * shape_score + 0.20 * size_score
            measured_box = (strict_left, strict_top, strict_right, strict_bottom)
            for body in body_components:
                body_ys, body_xs = body[:, 0], body[:, 1]
                if not yellow[body_ys, body_xs].any():
                    continue
                body_box = (
                    int(body_xs.min()), int(body_ys.min()),
                    int(body_xs.max()) + 1, int(body_ys.max()) + 1,
                )
                body_width = body_box[2] - body_box[0]
                body_height = body_box[3] - body_box[1]
                overlaps_seed = not (
                    body_box[2] <= strict_left or body_box[0] >= strict_right
                    or body_box[3] <= strict_top or body_box[1] >= strict_bottom
                )
                if overlaps_seed and body_width <= max_span and body_height <= max_span:
                    measured_box = body_box
                    break
            detection = MarkerDetection(
                x=float(xs.mean()) / width,
                y=float(ys.mean()) / height,
                confidence=min(1.0, float(score)),
                pixel_box=measured_box,
            )
            candidates.append((score, detection))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def detect_red_diamonds(minimap_rgb: np.ndarray) -> list[MarkerDetection]:
    """Return ALL red diamond markers (other players) on the minimap.

    Other players render as red diamonds with a center color of #e30000
    (227, 0, 0).  The yellow player diamond is never matched (yellow has
    high green/blue; red requires both near zero).  Each connected red
    component within diamond-like size/aspect limits becomes one detection;
    unlike the yellow marker there is no "best one" - every player counts.
    """

    rgb = minimap_rgb.astype(np.int16)
    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    # #e30000 center with tolerance; green/blue must stay near zero so
    # yellow/orange platform decorations are never mistaken for players.
    red_center = (
        (np.abs(red - 227) <= 25)
        & (green <= 60)
        & (blue <= 60)
    )
    red_body = (red >= 190) & (green <= 70) & (blue <= 70)
    height, width = red_center.shape
    min_dimension = min(width, height)
    expected_span = max(4, int(round(min_dimension * 0.05)))
    max_span = max(20, int(round(min_dimension * 0.18)))
    max_pixels = max(320, max_span * max_span)
    body_components = _components(red_body, min_pixels=3)
    detections: list[MarkerDetection] = []
    for component in _components(red_center, min_pixels=3):
        ys, xs = component[:, 0], component[:, 1]
        strict_left, strict_top = int(xs.min()), int(ys.min())
        strict_right, strict_bottom = int(xs.max()) + 1, int(ys.max()) + 1
        span_x, span_y = strict_right - strict_left, strict_bottom - strict_top
        count = len(component)
        if count > max_pixels or span_x > max_span or span_y > max_span:
            continue
        aspect = span_x / max(1, span_y)
        compact = count / max(1, span_x * span_y)
        if 0.45 <= aspect <= 2.2 and compact >= 0.20 and span_x >= 2 and span_y >= 2:
            shape_score = max(0.0, 1.0 - abs(aspect - 1.0) / 2.0)
            span = max(span_x, span_y)
            size_score = max(
                0.0, 1.0 - abs(span - expected_span) / max(1.0, expected_span * 3)
            )
            score = 0.50 * compact + 0.30 * shape_score + 0.20 * size_score
            measured_box = (strict_left, strict_top, strict_right, strict_bottom)
            for body in body_components:
                body_ys, body_xs = body[:, 0], body[:, 1]
                if not red_center[body_ys, body_xs].any():
                    continue
                body_box = (
                    int(body_xs.min()), int(body_ys.min()),
                    int(body_xs.max()) + 1, int(body_ys.max()) + 1,
                )
                body_width = body_box[2] - body_box[0]
                body_height = body_box[3] - body_box[1]
                overlaps_seed = not (
                    body_box[2] <= strict_left or body_box[0] >= strict_right
                    or body_box[3] <= strict_top or body_box[1] >= strict_bottom
                )
                if overlaps_seed and body_width <= max_span and body_height <= max_span:
                    measured_box = body_box
                    break
            detections.append(MarkerDetection(
                x=float(xs.mean()) / width,
                y=float(ys.mean()) / height,
                confidence=min(1.0, float(score)),
                pixel_box=measured_box,
            ))
    return detections


__all__ = ["DiamondSizeTracker", "MarkerDetection", "detect_yellow_diamond", "detect_red_diamonds"]
