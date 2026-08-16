"""Independent monster detection worker.

The worker consumes the immutable frames published by ``capture_worker`` and
runs a color-mask segmentation over a fixed search zone (the vertical middle
third of the game window, full width).  Detection is deliberately multi-monster:
``cv2.connectedComponentsWithStats`` extracts every color-masked blob that
passes the size/shape filters, so any number of monsters on screen is reported.

The worker owns no keyboard input and no capture.  When ``debug_dir`` is set it
saves a pink-overlaid copy of the analyzed frame: one pink rectangle for the
search zone, one pink rectangle per detected monster.  The overlay is drawn
only on the saved copy; the original frame stays clean for other consumers.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import cv2
import numpy as np
from PIL import Image, ImageDraw

LOG = logging.getLogger(__name__)

NormalizedBox = tuple[float, float, float, float]

# Search zone: the vertical middle third of the game window, full width.
DEFAULT_MONSTER_ZONE: NormalizedBox = (0.0, 1.0 / 3.0, 1.0, 2.0 / 3.0)

# Pink overlay used for both the search zone and each detected monster.
PINK = (255, 105, 180)


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Intersection-over-union of two pixel boxes."""

    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


def _containment(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int]
) -> float:
    """Fraction of the smaller box that lies inside the larger one."""

    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    smaller = min(area_a, area_b)
    if smaller <= 0:
        return 0.0
    return inter / smaller


@dataclass(frozen=True)
class MonsterDetection:
    """One detected monster in normalized full-frame coordinates."""

    box: NormalizedBox  # (left, top, right, bottom), normalized 0..1
    center_x: float
    center_y: float
    confidence: float
    pixel_box: tuple[int, int, int, int]

    @property
    def width(self) -> float:
        return self.box[2] - self.box[0]

    @property
    def height(self) -> float:
        return self.box[3] - self.box[1]


@dataclass(frozen=True)
class MonsterConfig:
    """Detection parameters for one monster profile.

    ``method`` selects the OpenCV strategy:
    - ``color``: HSV band mask (fast, but needs a tight color range)
    - ``histogram``: HSV back-projection of the profile image's full color
      distribution (robust to multi-colored monsters and background noise)
    - ``template``: normalized cross-correlation of the profile image over the
      zone (classic sprite matching; tolerant of flipped/left-right sprites)
    - ``motion``: frame-to-frame differencing; anything that moved is a
      monster.  Needs no reference picture.  Works when the camera is still;
      a moving camera (player walking) shifts the whole world and is rejected
      by the max-size filter.
    - ``silhouette``: static edge/contour extraction.  Finds compact closed
      objects (sprites) by their outline without needing a reference picture
      or motion.  Catches players/NPCs too; best combined with size filters.

    ``hsv_lower``/``hsv_upper`` are OpenCV HSV bounds (H 0..179, S/V 0..255).
    The default range is a permissive "saturated" mask meant to be narrowed to
    the target monster's palette once a screenshot is provided.
    """

    method: str = "color"
    search_zone: NormalizedBox = DEFAULT_MONSTER_ZONE
    hsv_lower: tuple[int, int, int] = (0, 80, 80)
    hsv_upper: tuple[int, int, int] = (179, 255, 255)
    # Disabled bands are empty UI slots: they never match anything.
    enabled: bool = True
    # Histogram back-projection tuning.
    hist_bins: int = 16
    hist_threshold: float = 0.08
    # Template matching tuning.
    template_image: Optional[Image.Image] = None
    template_scales: tuple[float, ...] = (0.6, 0.8, 1.0, 1.2, 1.5)
    match_threshold: float = 0.50
    nms_iou: float = 0.35
    # Motion detection tuning.
    motion_diff_threshold: int = 12
    motion_min_pixels: int = 40
    # Silhouette (static contour) detection tuning.
    sil_edge_low: int = 40
    sil_edge_high: int = 120
    sil_close_span: int = 7
    # Size filters are fractions of the search-zone dimensions so they survive
    # resolution/zoom changes without re-tuning.  The zone is short (1/3 of the
    # frame height), so a monster occupies a large fraction of it.
    min_area_fraction: float = 0.001
    max_area_fraction: float = 0.40
    min_span_fraction: float = 0.03
    max_span_fraction: float = 0.70
    min_aspect: float = 0.3
    max_aspect: float = 3.0
    min_compactness: float = 0.20
    max_monsters: int = 16
    minimum_confidence: float = 0.30


class MonsterDetector:
    """HSV color-mask detector over one or more monster color bands.

    Every configured band (one per uploaded monster profile) is evaluated on
    the same HSV conversion of the search zone; detections from all bands are
    merged into a single list.  Adding a band costs one extra inRange +
    connected-components pass over the zone (linear, ~2-3 ms) while the crop
    and HSV conversion are shared.
    """

    def __init__(
        self,
        config: Optional[MonsterConfig] = None,
        configs: Optional[list[MonsterConfig]] = None,
    ) -> None:
        if configs is not None:
            self.configs = list(configs)
        elif config is not None:
            self.configs = [config]
        else:
            self.configs = [MonsterConfig()]
        if not self.configs:
            self.configs = [MonsterConfig()]
        # Motion detection keeps the previous zone frame for differencing.
        self._previous_zone: Optional[np.ndarray] = None

    @property
    def config(self) -> MonsterConfig:
        """First band; kept for single-band compatibility."""

        return self.configs[0]

    @config.setter
    def config(self, value: MonsterConfig) -> None:
        """Replace all bands with a single one (single-band compatibility)."""

        self.configs = [value]

    def add_config(self, config: MonsterConfig) -> None:
        """Append another monster color band."""

        self.configs.append(config)

    def set_config(self, index: int, config: MonsterConfig) -> None:
        """Replace the band at ``index`` (used by UI slots)."""

        if index < 0 or index >= len(self.configs):
            raise IndexError(f"monster band index {index} out of range")
        self.configs[index] = config

    def detect(self, image: Image.Image) -> list[MonsterDetection]:
        """Return all monsters found inside the configured search zone."""

        return self.detect_with_coverage(image)[0]

    def detect_with_coverage(
        self, image: Image.Image
    ) -> tuple[list[MonsterDetection], float]:
        """Return ``(detections, zone_coverage)`` for the search zone.

        ``zone_coverage`` is the maximum band-match fraction across all bands
        (0..1).  Used to diagnose bands that include background.
        """

        frame_width, frame_height = image.size
        zone_box = self._zone_box(frame_width, frame_height)
        zone_width = zone_box[2] - zone_box[0]
        zone_height = zone_box[3] - zone_box[1]
        if zone_width <= 0 or zone_height <= 0:
            return [], 0.0

        # One crop + one HSV conversion shared by every color/histogram band.
        crop = np.asarray(image.crop(zone_box).convert("RGB"))
        hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)

        detections: list[MonsterDetection] = []
        coverage = 0.0
        max_monsters = self.configs[0].max_monsters
        any_motion = any(
            (config.method or "color").casefold() == "motion"
            for config in self.configs if config.enabled
        )
        for config in self.configs:
            if not config.enabled:
                continue
            method = (config.method or "color").casefold()
            if method == "template":
                found = self._detect_template(
                    image, config, zone_box, zone_width, zone_height,
                    frame_width, frame_height, max_monsters,
                )
                detections.extend(found)
            elif method == "motion":
                # Motion is evaluated once per frame on the shared zone.
                if not any_motion:
                    continue
                mask, motion_ready = self._motion_mask(crop, config)
                if not motion_ready:
                    continue
                coverage = max(coverage, float(mask.sum()) / max(1.0, float(mask.size) * 255.0))
                if not mask.any():
                    continue
                detections.extend(self._detect_band(
                    mask, config, zone_box, zone_width, zone_height,
                    frame_width, frame_height,
                ))
                any_motion = False  # only run once per frame
            elif method == "silhouette":
                # Static contour extraction: map-agnostic, no color/terrain
                # assumptions.  Finds compact closed objects by edge closure.
                mask = self._silhouette_mask(crop, config)
                coverage = max(coverage, float(mask.sum()) / max(1.0, float(mask.size) * 255.0))
                if not mask.any():
                    continue
                detections.extend(self._detect_band(
                    mask, config, zone_box, zone_width, zone_height,
                    frame_width, frame_height,
                ))
            else:
                mask = self._band_mask(hsv, config)
                if method == "histogram":
                    mask = self._histogram_mask(hsv, config, mask)
                coverage = max(coverage, float(mask.sum()) / max(1.0, float(mask.size) * 255.0))
                if not mask.any():
                    continue
                detections.extend(self._detect_band(
                    mask, config, zone_box, zone_width, zone_height,
                    frame_width, frame_height,
                ))
            if len(detections) >= max_monsters:
                break
        detections.sort(key=lambda d: d.confidence, reverse=True)
        return detections[:max_monsters], coverage

    def _motion_mask(
        self, crop_rgb: np.ndarray, config: MonsterConfig
    ) -> tuple[np.ndarray, bool]:
        """Return ``(motion_mask, ready)`` from frame differencing.

        The first call only stores the reference frame and returns ``ready``
        False; subsequent calls diff against it.  A whole-world scroll (player
        walking) produces one giant blob that the max-size filter rejects.
        """

        gray = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2GRAY)
        previous = self._previous_zone
        if previous is None or previous.shape != gray.shape:
            self._previous_zone = gray.copy()
            return np.zeros_like(gray), False
        diff = cv2.absdiff(previous, gray)
        self._previous_zone = gray.copy()
        threshold = max(1, int(config.motion_diff_threshold))
        _ok, mask = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)
        # Merge body parts AND the "ghost" of the previous position: frame
        # differencing highlights both where the monster was and where it now
        # is.  A kernel sized to the expected monster span fuses them into one
        # blob.  The span scales with the zone, so zoom changes still work.
        span = max(5, int(round(
            min(crop_rgb.shape[0], crop_rgb.shape[1]) * 0.06
        )))
        if span % 2 == 0:
            span += 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (span, span))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        # Suppress tiny flicker blobs (cursor, sparkle effects).
        min_pixels = max(4, int(config.motion_min_pixels))
        num, labels, stats, _centroids = cv2.connectedComponentsWithStats(
            mask, connectivity=8
        )
        for label in range(1, num):
            if stats[label][4] < min_pixels:
                mask[labels == label] = 0
        return mask, True

    def _silhouette_mask(
        self, crop_rgb: np.ndarray, config: MonsterConfig
    ) -> np.ndarray:
        """Map-agnostic static object extraction by edge closure.

        No color, terrain, or map assumptions: Canny edges are closed with a
        small kernel so sprite outlines become solid rings, flood-fill turns
        the rings into filled masks, and the outer border fill is removed so
        background is never counted.  Compact closed objects (monsters,
        players, NPCs) survive; open background detail does not.
        """

        gray = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(
            gray,
            max(1, int(config.sil_edge_low)),
            max(1, int(config.sil_edge_high)),
        )
        span = max(3, int(config.sil_close_span))
        if span % 2 == 0:
            span += 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (span, span))
        closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

        # Flood-fill the background from every border pixel: everything that
        # is NOT enclosed by a closed ring gets marked.  Inverting then leaves
        # exactly the enclosed object interiors (plus the rings).
        height, width = closed.shape
        bg = np.zeros((height + 2, width + 2), dtype=np.uint8)
        seeds = []
        for x in range(width):
            if closed[0, x] == 0:
                seeds.append((0, x))
            if closed[height - 1, x] == 0:
                seeds.append((height - 1, x))
        for y in range(height):
            if closed[y, 0] == 0:
                seeds.append((y, 0))
            if closed[y, width - 1] == 0:
                seeds.append((y, width - 1))
        for y, x in seeds:
            cv2.floodFill(closed, bg, (int(x), int(y)), 128,
                          loDiff=0, upDiff=0)
        # 128 = background reachable from border; anything else (0 or 255)
        # is an enclosed object interior or ring.
        interior = np.where(closed == 128, 0, 255).astype(np.uint8)
        # Drop the rings themselves: keep only solid interior pixels.
        interior = cv2.bitwise_and(interior, cv2.bitwise_not(edges))
        # Remove specks that are not part of a closed object.
        num, labels, stats, _centroids = cv2.connectedComponentsWithStats(
            interior, connectivity=8
        )
        result = np.zeros_like(interior)
        for label in range(1, num):
            if stats[label][4] >= max(6, int(config.motion_min_pixels)):
                result[labels == label] = 255
        return result

    @staticmethod
    def _histogram_mask(
        hsv: np.ndarray, config: MonsterConfig, band_mask: np.ndarray
    ) -> np.ndarray:
        """Back-project the profile's full color distribution onto the zone.

        ``band_mask`` already limits the search to the profile's dominant hue
        band; the back-projection then scores pixels by how well their
        (H, S) pair appears in the profile image, which tolerates the many
        shades of a multi-colored monster.
        """

        template = config.template_image
        if template is None:
            return band_mask
        tpl_hsv = cv2.cvtColor(
            np.asarray(template.convert("RGB")), cv2.COLOR_RGB2HSV
        )
        bins = max(2, int(config.hist_bins))
        hist = cv2.calcHist(
            [tpl_hsv], [0, 1], None, [bins, bins], [0, 180, 0, 256]
        )
        total = float(hist.sum())
        if total <= 0:
            return band_mask
        # Normalize to 0..255 so calcBackProject returns back-projection
        # intensities in the usual image range.
        cv2.normalize(hist, hist, 0, 255, cv2.NORM_MINMAX)
        # Keep only hues that are actually present in the profile.
        hist = np.where(hist > 0.05, hist, 0.0)
        back = cv2.calcBackProject(
            [hsv], [0, 1], hist, [0, 180, 0, 256], 1
        )
        threshold = float(np.clip(config.hist_threshold, 0.0, 1.0)) * 255.0
        mask = np.where(back >= threshold, 255, 0).astype(np.uint8)
        # Combine with the hue band so unrelated colors stay excluded.
        mask &= band_mask
        # Close small gaps inside a monster's body.
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        return mask

    def _detect_template(
        self,
        image: Image.Image,
        config: MonsterConfig,
        zone_box: tuple[int, int, int, int],
        zone_width: int,
        zone_height: int,
        frame_width: int,
        frame_height: int,
        max_monsters: int,
    ) -> list[MonsterDetection]:
        """Multi-scale normalized cross-correlation of the profile image.

        The template is searched at several scales and both left/right
        orientations (sprites flip when facing the other way).  The zone is
        downscaled first (matching scales are applied to the small zone), so
        the search stays cheap; boxes are mapped back to full-frame pixels.
        Overlapping matches are merged with non-maximum suppression.
        """

        template = config.template_image
        if template is None:
            return []
        zone_gray_full = cv2.cvtColor(
            np.asarray(image.crop(zone_box).convert("RGB")), cv2.COLOR_RGB2GRAY
        )
        tpl_gray = cv2.cvtColor(
            np.asarray(template.convert("RGB")), cv2.COLOR_RGB2GRAY
        )
        if tpl_gray.size == 0:
            return []

        # Downscale the zone to a fixed analysis width for speed.  Scales and
        # box coordinates are expressed relative to the small zone.
        target_width = min(zone_width, 360)
        scale_factor = target_width / max(1, zone_width)
        zone_gray = cv2.resize(
            zone_gray_full,
            (target_width, max(1, int(round(zone_height * scale_factor)))),
            interpolation=cv2.INTER_AREA,
        )
        small_w, small_h = zone_gray.shape[1], zone_gray.shape[0]

        matches: list[tuple[float, tuple[int, int, int, int]]] = []
        for scale in config.template_scales:
            tpl_w = int(round(tpl_gray.shape[1] * scale_factor * float(scale)))
            tpl_h = int(round(tpl_gray.shape[0] * scale_factor * float(scale)))
            if tpl_w < 4 or tpl_h < 4:
                continue
            if tpl_w >= small_w or tpl_h >= small_h:
                continue
            scaled = cv2.resize(tpl_gray, (tpl_w, tpl_h),
                                interpolation=cv2.INTER_AREA)
            for flipped in (False, True):
                probe = cv2.flip(scaled, 1) if flipped else scaled
                result = cv2.matchTemplate(
                    zone_gray, probe, cv2.TM_CCOEFF_NORMED
                )
                ys, xs = np.where(result >= float(config.match_threshold))
                for y, x in zip(ys, xs):
                    # Map the small-zone box back to full-frame coordinates.
                    sx = int(round(x / scale_factor))
                    sy = int(round(y / scale_factor))
                    sw = int(round(tpl_w / scale_factor))
                    sh = int(round(tpl_h / scale_factor))
                    pixel_box = (
                        zone_box[0] + sx,
                        zone_box[1] + sy,
                        zone_box[0] + sx + sw,
                        zone_box[1] + sy + sh,
                    )
                    matches.append((float(result[y, x]), pixel_box))

        if not matches:
            return []
        matches.sort(key=lambda item: item[0], reverse=True)
        kept: list[tuple[float, tuple[int, int, int, int]]] = []
        for score, box in matches:
            if any(_iou(box, other) > float(config.nms_iou)
                   or _containment(box, other) > 0.6
                   for _s, other in kept):
                continue
            kept.append((score, box))
            if len(kept) >= max_monsters:
                break
        return [
            MonsterDetection(
                box=(
                    box[0] / frame_width,
                    box[1] / frame_height,
                    box[2] / frame_width,
                    box[3] / frame_height,
                ),
                center_x=(box[0] + box[2]) / 2.0 / frame_width,
                center_y=(box[1] + box[3]) / 2.0 / frame_height,
                confidence=min(1.0, max(0.0, score)),
                pixel_box=box,
            )
            for score, box in kept
        ]

    def _zone_box(
        self, frame_width: int, frame_height: int
    ) -> tuple[int, int, int, int]:
        left, top, right, bottom = self.configs[0].search_zone
        return (
            max(0, min(frame_width, int(left * frame_width))),
            max(0, min(frame_height, int(top * frame_height))),
            max(0, min(frame_width, int(right * frame_width))),
            max(0, min(frame_height, int(bottom * frame_height))),
        )

    @staticmethod
    def _band_mask(hsv: np.ndarray, config: MonsterConfig) -> np.ndarray:
        lower = np.asarray(config.hsv_lower, dtype=np.uint8)
        upper = np.asarray(config.hsv_upper, dtype=np.uint8)
        if lower[0] <= upper[0]:
            return cv2.inRange(hsv, lower, upper)
        # Hue wraparound: mask [lower..179] plus [0..upper].
        mask = cv2.inRange(hsv, lower, np.asarray((179, 255, 255), dtype=np.uint8))
        mask |= cv2.inRange(hsv, np.asarray((0, 0, 0), dtype=np.uint8), upper)
        return mask

    @staticmethod
    def _detect_band(
        mask: np.ndarray,
        config: MonsterConfig,
        zone_box: tuple[int, int, int, int],
        zone_width: int,
        zone_height: int,
        frame_width: int,
        frame_height: int,
    ) -> list[MonsterDetection]:
        zone_area = float(zone_width * zone_height)
        min_area = max(1.0, zone_area * config.min_area_fraction)
        max_area = zone_area * config.max_area_fraction
        min_span = min(zone_width, zone_height) * config.min_span_fraction
        max_span = max(zone_width, zone_height) * config.max_span_fraction

        _count, _labels, stats, centroids = cv2.connectedComponentsWithStats(
            mask, connectivity=8
        )
        detections: list[MonsterDetection] = []
        for label in range(1, stats.shape[0]):
            x, y, width, height, area = stats[label]
            if area < min_area or area > max_area:
                continue
            if width < min_span or height < min_span:
                continue
            if width > max_span or height > max_span:
                continue
            aspect = width / max(1.0, float(height))
            if not (config.min_aspect <= aspect <= config.max_aspect):
                continue
            compactness = area / max(1.0, float(width * height))
            if compactness < config.min_compactness:
                continue
            # Confidence blends compactness (solid blob) with a mild size bonus.
            size_score = min(1.0, area / max(1.0, min_area * 40.0))
            confidence = float(np.clip(
                0.7 * compactness + 0.3 * size_score, 0.0, 1.0
            ))
            if confidence < config.minimum_confidence:
                continue

            center_x = float(centroids[label][0]) / zone_width
            center_y = float(centroids[label][1]) / zone_height
            pixel_box = (
                zone_box[0] + int(x),
                zone_box[1] + int(y),
                zone_box[0] + int(x + width),
                zone_box[1] + int(y + height),
            )
            normalized_box = (
                pixel_box[0] / frame_width,
                pixel_box[1] / frame_height,
                pixel_box[2] / frame_width,
                pixel_box[3] / frame_height,
            )
            detections.append(MonsterDetection(
                box=normalized_box,
                center_x=center_x,
                center_y=center_y,
                confidence=confidence,
                pixel_box=pixel_box,
            ))
        return detections


class MonsterWorker(threading.Thread):
    """Consume frames, detect all monsters, and publish the latest set."""

    def __init__(
        self,
        frame_queue: queue.Queue,
        stop_event: threading.Event,
        *,
        detector: Optional[MonsterDetector] = None,
        debug_dir: Optional[Path] = None,
        # Kept for signature compatibility, but monster analysis is passive
        # (no keyboard input), so frames are analyzed even when patrol is off.
        automation_active_event: Optional[threading.Event] = None,
        interval: float = 1.0,
    ) -> None:
        super().__init__(name="monster-worker", daemon=True)
        self.frame_queue = frame_queue
        self.stop_event = stop_event
        self.detector = detector or MonsterDetector()
        self.debug_dir = Path(debug_dir) if debug_dir is not None else None
        self.automation_active_event = automation_active_event
        # Analyze at most one frame every ``interval`` seconds (1 fps by
        # default); detection is a background diagnostic, not a control loop.
        self.interval = max(0.1, float(interval))
        self._latest: list[MonsterDetection] = []
        self._latest_frame_sequence = -1
        self._latest_zone_coverage = 0.0
        self._lock = threading.Lock()
        self._last_debug_path: Optional[Path] = None

    @property
    def latest(self) -> list[MonsterDetection]:
        """Thread-safe snapshot of the most recent detections."""

        with self._lock:
            return list(self._latest)

    @property
    def latest_frame_sequence(self) -> int:
        with self._lock:
            return self._latest_frame_sequence

    @property
    def latest_zone_coverage(self) -> float:
        """Fraction of the search zone matching the current HSV band (0..1).

        A high value (e.g. > 0.10) usually means the band includes background
        colors, which produces noisy detections.
        """

        with self._lock:
            return self._latest_zone_coverage

    def _process_frame(self, frame: object) -> None:
        image = getattr(frame, "image", frame)
        if not isinstance(image, Image.Image):
            LOG.warning("ignored frame without PIL image")
            return
        detections, coverage = self.detector.detect_with_coverage(image)
        sequence = int(getattr(frame, "sequence", -1))
        with self._lock:
            self._latest = detections
            self._latest_frame_sequence = sequence
            self._latest_zone_coverage = coverage
        if detections:
            LOG.info("monsters detected=%d coverage=%.1f%% seq=%s",
                     len(detections), 100.0 * coverage, sequence)
        elif coverage > 0.10:
            LOG.warning(
                "monster band matches %.0f%% of the search zone but no blob "
                "passed the shape filters; band may include background",
                100.0 * coverage,
            )
        if self.debug_dir is not None:
            self._save_debug_frame(image, detections, sequence)

    def _save_debug_frame(
        self,
        image: Image.Image,
        detections: list[MonsterDetection],
        sequence: int,
    ) -> None:
        """Save a pink-overlaid copy of the frame (zone + every monster)."""

        assert self.debug_dir is not None
        overlay = image.copy()
        draw = ImageDraw.Draw(overlay)
        width, height = overlay.size
        left, top, right, bottom = self.config_search_zone()
        draw.rectangle(
            (int(left * width), int(top * height),
             int(right * width), int(bottom * height)),
            outline=PINK, width=2,
        )
        for detection in detections:
            x1, y1, x2, y2 = detection.pixel_box
            draw.rectangle((x1, y1, x2, y2), outline=PINK, width=2)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        path = self.debug_dir / f"monster-{sequence:06d}-{stamp}.png"
        previous = self._last_debug_path
        self._last_debug_path = None
        if previous is not None and previous != path:
            try:
                previous.unlink(missing_ok=True)
            except OSError:
                LOG.warning("could not remove previous monster frame %s",
                            previous, exc_info=True)
        try:
            overlay.save(path, format="PNG")
            self._last_debug_path = path
        except Exception:
            LOG.exception("could not save monster debug frame %s", path)

    def config_search_zone(self) -> NormalizedBox:
        return self.detector.config.search_zone

    def run(self) -> None:
        if self.debug_dir is not None:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            self._remove_stale_debug_frames()
        LOG.info("monster worker started zone=%s interval=%.2fs",
                 tuple(round(v, 3) for v in self.config_search_zone()),
                 self.interval)
        next_analysis = 0.0
        while not self.stop_event.is_set():
            try:
                frame = self.frame_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                now = time.monotonic()
                if now < next_analysis:
                    # Throttle: skip frames until the interval elapses; the
                    # newest frame is still queued for the next analysis.
                    continue
                next_analysis = now + self.interval
                self._process_frame(frame)
            except Exception:
                LOG.exception("monster frame analysis failed")
            finally:
                try:
                    self.frame_queue.task_done()
                except (AttributeError, ValueError):
                    pass
        self._remove_last_debug_frame()
        LOG.info("monster worker stopped")

    def _remove_last_debug_frame(self) -> None:
        path = self._last_debug_path
        self._last_debug_path = None
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            LOG.warning("could not remove final monster frame %s", path,
                        exc_info=True)

    def _remove_stale_debug_frames(self) -> None:
        """Remove monster overlays saved by an earlier interrupted run."""

        assert self.debug_dir is not None
        for path in self.debug_dir.glob("monster-*.png"):
            try:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            except OSError:
                LOG.warning("could not remove stale monster frame %s", path,
                            exc_info=True)


__all__: Sequence[str] = (
    "MonsterConfig",
    "MonsterDetection",
    "MonsterDetector",
    "MonsterWorker",
    "DEFAULT_MONSTER_ZONE",
    "PINK",
)
