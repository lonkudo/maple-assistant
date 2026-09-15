"""Yellow minimap-diamond detection with center and size measurements."""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque
import statistics
import threading
from typing import Optional

import numpy as np


# Colour family of the PLAYER's own diamond, measured from the operator's
# captures (a 286x300 minimap crop and a full 1080x768 client frame):
#   player  : core (255,255,0) hue 60 sat 1.00, (255,255,136) hue 60 sat 0.47,
#             body (255,204,0) hue 48 sat 1.00, edge (214,200,0) hue 56 sat 1.00
#   teammate: core (221,153,17) hue 40 sat 0.92, blends (238,221,187) hue 40
#             sat 0.21, (238,221,170) hue 45 sat 0.29, (221,153,34) hue 38,
#             (204,153,34) hue 42 - plus the washed-out (238,238,170) whose hue
#             is 60 (the player's!) but whose saturation is only 0.29.
# Two conditions are therefore required.  The hue floor rejects the teammate's
# saturated orange pixels; the saturation floor rejects its pale blends, which
# can reach the player's hue but never the player's saturation.  Every measured
# player shade passes both with margin.
#
# Field bug this prevents: a recording locked onto a teammate's diamond (its
# highlight pixels passed the old "bright with little blue" test), so every
# recorded point came back as the same static minimap pixel while the character
# moved.
YELLOW_MIN_HUE_DEGREES = 46.0
YELLOW_MIN_SATURATION = 0.30
# Inside every accepted pixel blue is the smallest channel, so the hue test only
# has to cover the yellow-to-orange sector, where hue >= 46 degrees is the
# (green - blue) >= 46/60 * (max - blue) ratio below.
YELLOW_MIN_HUE_RATIO = YELLOW_MIN_HUE_DEGREES / 60.0
# Channel floors for the marker's bright core and for its wider body.
#
# These were widened after operator feedback that "openCV keeps missing
# detection for 3 frames": the old core floor green >= 198 rejected the
# marker's own measured edge pixel (211,197,7), which removed the pixel that
# completed the solid 3x3 core and made the whole marker vanish for a few
# frames at a time.  The widened floors keep every measured teammate and
# map-art colour out (a teammate's green never exceeds 153; map art stays
# below red 200) while admitting the marker's darker edges.
YELLOW_CORE_RED_FLOOR = 200
YELLOW_CORE_GREEN_FLOOR = 190
YELLOW_CORE_BLUE_CEILING = 185
YELLOW_BODY_RED_FLOOR = 180
YELLOW_BODY_GREEN_FLOOR = 155
YELLOW_BODY_BLUE_CEILING = 192
# The player's marker is a solid block, not a spray of pixels: the operator's
# client draws a diamond whose centre is a FULLY FILLED 3x3 core - all nine
# pixels in the yellow range (measured grid from a live 1080x768 frame: row
# widths 1,4,5,6,3,2 of core pixels, giving a solid 3x3 at cols 28-30 of rows
# 25-27).  Map decoration, thin yellow runs, anti-aliased flecks and other
# players' markers never contain such a block, so a 3x3 erosion of the colour
# mask is required before a candidate may be considered at all.  Only raise or
# lower this span if the game renders the marker differently.
YELLOW_MIN_SOLID_CORE_SPAN = 3
# Smallest accepted candidate span: a solid core block plus its adjacent
# anti-aliased pixels is a legitimate marker, anything thinner is not.
YELLOW_MIN_MARKER_SPAN = YELLOW_MIN_SOLID_CORE_SPAN


def _solid_core_mask(mask: np.ndarray, span: int) -> np.ndarray:
    """True where a whole ``span`` x ``span`` block of ``mask`` is set."""

    window = max(1, int(span))
    radius = window // 2
    padded = np.pad(mask.astype(np.uint8), radius, mode="constant")
    solid = np.ones(mask.shape, dtype=bool)
    for offset_y in range(window):
        for offset_x in range(window):
            solid &= (
                padded[
                    offset_y:offset_y + mask.shape[0],
                    offset_x:offset_x + mask.shape[1],
                ] > 0
            )
    return solid


def _yellow_family_mask(red, green, blue, *, min_red, min_green, max_blue):
    """Bright pixels of the player's own (yellow) diamond colour family."""

    value = np.maximum(red, green)
    span = value - blue
    return (
        (red >= min_red)
        & (green >= min_green)
        & (blue <= max_blue)
        & (red >= green * 0.90)
        & (green >= blue * 1.25)
        & (green - blue >= YELLOW_MIN_HUE_RATIO * span)
        & (span >= YELLOW_MIN_SATURATION * value)
    )


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
    # Accept the full measured yellow range of the player diamond - bright
    # golden core (255,255,136) down to the dark olive edge (214,200,0) -
    # plus anti-aliased shades between them.  Long orange/brown platform
    # decorations stay excluded by requiring a strong green channel (not
    # orange) and a weak blue channel (not white), plus the shape and
    # compactness checks below.
    # Seed only from the bright core of the player diamond.  Some maps use
    # ochre/yellow-brown terrain tiles that satisfy a broad yellow test; they
    # are static and made every recording land on the same false coordinate.
    # The true marker retains a compact, bright yellow core even when its
    # anti-aliased outer pixels are darker.  Terrain can contain an isolated
    # bright fleck, but not a marker-sized core; that measured size gate is
    # what keeps this detector tied to the diamond rather than map art.
    yellow = _yellow_family_mask(
        red, green, blue,
        min_red=YELLOW_CORE_RED_FLOOR,
        min_green=YELLOW_CORE_GREEN_FLOOR,
        max_blue=YELLOW_CORE_BLUE_CEILING,
    )
    # The wider body mask (used only to include the marker's anti-aliased outer
    # pixels in its measured box) keeps the same colour-family test.
    yellow_body = _yellow_family_mask(
        red, green, blue,
        min_red=YELLOW_BODY_RED_FLOOR,
        min_green=YELLOW_BODY_GREEN_FLOOR,
        max_blue=YELLOW_BODY_BLUE_CEILING,
    )
    # A candidate must contain a fully filled yellow block (the marker's own
    # solid core); everything thin or sparse is map art or another marker.
    solid_core = _solid_core_mask(yellow, YELLOW_MIN_SOLID_CORE_SPAN)
    height, width = yellow.shape
    candidates: list[tuple[float, MarkerDetection]] = []
    # The reliable player core is the solid 2x2 block the operator's client
    # draws.  The old preference for a 6-7 px component was too large for the
    # current minimap rendering: a valid small marker lost to map decoration or
    # was rejected after minor anti-aliasing.  The body may still expand around
    # this core at other zooms, but candidate scoring begins from it.
    min_dimension = min(width, height)
    expected_span = 3
    max_span = max(20, int(round(min_dimension * 0.18)))
    max_pixels = max(320, max_span * max_span)
    body_components = _components(yellow_body, min_pixels=3)
    # A solid 2x2 core is four bright pixels; require at least that many, which
    # keeps the detector resilient to one anti-aliased/dim pixel without
    # admitting lone flecks.
    for component in _components(yellow, min_pixels=4):
        ys, xs = component[:, 0], component[:, 1]
        if not solid_core[ys, xs].any():
            # No fully filled yellow block inside this component: it is map
            # decoration, an anti-aliased run, or another player's marker -
            # never the player's own diamond.
            continue
        strict_left, strict_top = int(xs.min()), int(ys.min())
        strict_right, strict_bottom = int(xs.max()) + 1, int(ys.max()) + 1
        span_x, span_y = strict_right - strict_left, strict_bottom - strict_top
        count = len(component)
        if (
            count > max_pixels
            or span_x > max_span
            or span_y > max_span
            or span_x < YELLOW_MIN_MARKER_SPAN
            or span_y < YELLOW_MIN_MARKER_SPAN
        ):
            continue
        aspect = span_x / max(1, span_y)
        compact = count / max(1, span_x * span_y)
        # The player diamond is a near-square rhombus.  A wider-than-tall
        # blob like 15x7 is the diamond merged with a thin yellow map-art
        # run: it used to be accepted (aspect < 2.2), which recorded an
        # oversized "diamond" and dragged the point's centre sideways.  A
        # diamond-like aspect keeps the marker's own compact core, so both
        # the measured size and the centre stay on the diamond.
        if 0.60 <= aspect <= 1.55 and compact >= 0.20 and span_x >= 2 and span_y >= 2:
            shape_score = max(0.0, 1.0 - abs(aspect - 1.0) / 2.0)
            # Prefer the size near the expected marker span; tolerate zoom
            # changes up to roughly 3-4x before the candidate scores zero.
            span = max(span_x, span_y)
            size_score = max(
                0.0, 1.0 - abs(span - expected_span) / max(1.0, expected_span * 3)
            )
            score = 0.50 * compact + 0.30 * shape_score + 0.20 * size_score
            # Size/centre come from the diamond's own compact core.  The
            # broader body is only used to include the anti-aliased outer
            # pixels, and only when it is itself a near-square diamond of
            # about the same size (never a merged map-art run).
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
                body_aspect = body_width / max(1, body_height)
                overlaps_seed = not (
                    body_box[2] <= strict_left or body_box[0] >= strict_right
                    or body_box[3] <= strict_top or body_box[1] >= strict_bottom
                )
                if (overlaps_seed and body_width <= max_span
                        and body_height <= max_span
                        and 0.60 <= body_aspect <= 1.55
                        and body_width <= span_x * 1.6
                        and body_height <= span_y * 1.6):
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
