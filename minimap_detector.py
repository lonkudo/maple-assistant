"""OpenCV-based dynamic minimap localization and map-name extraction.

This module has no worker/thread or UI dependencies.  Both movement and the
debug UI can reuse the same detector without becoming coupled to one another.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import threading
import time
from typing import Optional, Protocol, Sequence

import cv2
import numpy as np
from PIL import Image


Box = tuple[int, int, int, int]
NormalizedBox = tuple[float, float, float, float]


def _clamp_box(box: Box, width: int, height: int) -> Box:
    left, top, right, bottom = box
    left = max(0, min(width - 1, int(left)))
    top = max(0, min(height - 1, int(top)))
    right = max(left + 1, min(width, int(right)))
    bottom = max(top + 1, min(height, int(bottom)))
    return left, top, right, bottom


def box_to_normalized(box: Box, image_size: tuple[int, int]) -> NormalizedBox:
    width, height = image_size
    left, top, right, bottom = box
    return left / width, top / height, right / width, bottom / height


def _scale_box(
    box: Box,
    source_size: tuple[int, int],
    target_size: tuple[int, int],
) -> Box:
    """Map an OpenCV working box back into source-image pixels."""

    source_width, source_height = source_size
    target_width, target_height = target_size
    scale_x = target_width / source_width
    scale_y = target_height / source_height
    left, top, right, bottom = box
    return _clamp_box(
        (
            round(left * scale_x),
            round(top * scale_y),
            round(right * scale_x),
            round(bottom * scale_y),
        ),
        target_width,
        target_height,
    )


@dataclass(frozen=True)
class MinimapDetection:
    """All boxes use full client-image pixel coordinates."""

    window_box: Box
    analysis_box: Box
    canvas_box: Box
    map_name_box: Box
    confidence: float
    source: str
    map_name: Optional[str] = None

    @property
    def window_size(self) -> tuple[int, int]:
        left, top, right, bottom = self.window_box
        return right - left, bottom - top

    def normalized_analysis_box(self, image_size: tuple[int, int]) -> NormalizedBox:
        return box_to_normalized(self.analysis_box, image_size)


class MapNameReader(Protocol):
    """Replaceable OCR/template adapter for the extracted map-name image."""

    def read(self, image: Image.Image) -> Optional[str]: ...


class MinimapDetector:
    """Locate the resizable top-left minimap using rectangular edge contours."""

    def __init__(
        self,
        fallback_region: NormalizedBox = (0.0, 0.0, 0.22, 0.27),
        map_name_reader: Optional[MapNameReader] = None,
        dedicated_crop: bool = False,
        opencv_size: Optional[tuple[int, int]] = None,
        transient_hold_seconds: float = 1.0,
        box_history: int = 5,
        box_jump_ratio: float = 0.25,
    ) -> None:
        self.fallback_region = fallback_region
        self.map_name_reader = map_name_reader
        self.dedicated_crop = bool(dedicated_crop)
        self.opencv_size = (
            (max(96, int(opencv_size[0])), max(96, int(opencv_size[1])))
            if opencv_size is not None else None
        )
        self.transient_hold_seconds = max(0.0, float(transient_hold_seconds))
        # Median-smoothing of the detected minimap boxes.  A minimap frame can
        # flip between near-identical contour boxes every frame; each flip
        # shifts the coordinate frame (marker AND projected targets), which
        # stalls the rope approach / boundary turns even though the character
        # is moving.  Mirroring ``DiamondSizeTracker``: small jitter is
        # median-smoothed over ``box_history`` frames. Large jumps are held
        # for the active map session; reset_geometry permits a different map
        # to establish its own border at the next explicit patrol start.
        self.box_history_len = max(2, int(box_history))
        self.box_jump_ratio = max(0.05, min(0.8, float(box_jump_ratio)))
        self._box_history: deque[tuple[Box, Box, Box]] = deque(
            maxlen=self.box_history_len
        )
        # A partially detected outer border can make the minimap look much
        # shorter for one or two frames.  That crop excludes the lower map
        # canvas (and therefore the yellow marker), which must not be treated
        # as a disconnect.  Keep a candidate here until the shrink is proven
        # consistent across several frames.
        self._pending_shrunken_boxes: Optional[tuple[Box, Box, Box]] = None
        self._pending_shrunken_count = 0
        self._last_good: Optional[MinimapDetection] = None
        self._last_good_image_size: Optional[tuple[int, int]] = None
        self._last_good_at = float("-inf")
        self._state_lock = threading.Lock()

    def reset_geometry(self) -> None:
        """Forget the previous map's minimap frame before a patrol starts.

        Minimap size is map/HUD-specific, so a newly selected map must be
        allowed to establish a fresh coordinate frame.  During an already
        running patrol, ``_stabilize_boxes`` still protects that frame from a
        false cropped contour.
        """

        with self._state_lock:
            self._box_history.clear()
            self._pending_shrunken_boxes = None
            self._pending_shrunken_count = 0
            self._last_good = None
            self._last_good_image_size = None
            self._last_good_at = float("-inf")

    def seed_geometry(
        self, detection: MinimapDetection, image_size: tuple[int, int]
    ) -> None:
        """Lock a verified minimap border as this map session's baseline."""

        if detection.source != "opencv":
            raise ValueError("minimap geometry must come from an OpenCV border")
        boxes = (
            detection.window_box,
            detection.analysis_box,
            detection.canvas_box,
        )
        with self._state_lock:
            self._box_history.clear()
            self._box_history.append(boxes)
            self._pending_shrunken_boxes = None
            self._pending_shrunken_count = 0
            self._last_good = detection
            self._last_good_image_size = image_size
            self._last_good_at = time.monotonic()

    def retained_geometry(
        self, image_size: tuple[int, int]
    ) -> Optional[MinimapDetection]:
        """Return the verified border retained for this client size."""

        with self._state_lock:
            if (self._last_good is None
                    or self._last_good_image_size != image_size):
                return None
            return replace(self._last_good, source="opencv-held")

    def _held_or_fallback(self, image: Image.Image) -> MinimapDetection:
        now = time.monotonic()
        with self._state_lock:
            if (self._last_good is not None
                    and self._last_good_image_size == image.size):
                age = max(0.0, now - self._last_good_at)
                return replace(
                    self._last_good,
                    confidence=max(
                        0.25,
                        self._last_good.confidence
                        * (0.85 if age <= self.transient_hold_seconds else 0.60),
                    ),
                    source="opencv-held",
                )
        return self._fallback_detection(image)

    def _remember_good(
        self, detection: MinimapDetection, image_size: tuple[int, int]
    ) -> MinimapDetection:
        with self._state_lock:
            self._last_good = detection
            self._last_good_image_size = image_size
            self._last_good_at = time.monotonic()
        return detection

    def _analysis_crop(self, image: Image.Image) -> tuple[Image.Image, Optional[Box]]:
        """Return the image OpenCV analyzes plus its offset inside the frame.

        On a full-client capture the minimap is a small part of the frame; a
        whole-frame resize would shrink it below the minimum contour size and
        detection would always fall back.  Analyze the fallback region (where
        the minimap lives) at the working resolution instead, and remember the
        crop origin so boxes can be mapped back to full-frame coordinates.
        """

        if not self.dedicated_crop or self.opencv_size is None:
            return image, None
        width, height = image.size
        left, top, right, bottom = self.fallback_region
        crop_box = _clamp_box(
            (round(left * width), round(top * height),
             round(right * width), round(bottom * height)),
            width,
            height,
        )
        if image.size[0] <= self.opencv_size[0] * 2 \
                and image.size[1] <= self.opencv_size[1] * 2:
            # The capture is already a tight minimap crop; search it whole.
            return image, None
        return image.crop(crop_box), crop_box

    @staticmethod
    def _coordinate_median(
        history: "deque[tuple[Box, Box, Box]]", order: int
    ) -> Box:
        """Per-coordinate median of one box slot across the history."""
        values = [entry[order] for entry in history]
        return tuple(
            sorted(coordinate)[len(values) // 2]
            for coordinate in zip(*values)
        )

    def _stabilize_boxes(
        self, window: Box, analysis: Box, canvas: Box
    ) -> tuple[Box, Box, Box]:
        """Median-smooth the detected boxes across frames.

        A minimap frame can flip between near-identical contour boxes every
        frame (the border merges with a child rectangle, an overlay adds a
        candidate, etc.).  Each flip shifts the whole normalized coordinate
        frame: the player marker AND the projected patrol/rope targets both
        jump, so the rope approach gap never closes and the character stalls
        ("MOVE TO ROPE right" forever).  Mirroring ``DiamondSizeTracker``:
        small frame-to-frame jitter is median-smoothed; a large jump is held
        because it is usually a contour of the bounded search region rather
        than a real minimap resize. A new map calls reset_geometry first.
        """

        with self._state_lock:
            if self._box_history:
                median = self._coordinate_median(self._box_history, 0)
                median_width = max(1, median[2] - median[0])
                median_height = max(1, median[3] - median[1])
                window_width = max(1, window[2] - window[0])
                window_height = max(1, window[3] - window[1])
                # A sudden height/width collapse is normally a contour that
                # captured the title panel or only the upper part of the
                # minimap. Do not let a false crop or enclosing search-region
                # contour remove or shift the marker coordinate system.
                severely_changed = (
                    window_width < median_width * 0.65
                    or window_height < median_height * 0.65
                    or window_width > median_width * 1.55
                    or window_height > median_height * 1.55
                )
                if severely_changed:
                    pending = self._pending_shrunken_boxes
                    same_pending = pending is not None and max(
                        abs(window[0] - pending[0][0]),
                        abs(window[1] - pending[0][1]),
                        abs(window[2] - pending[0][2]),
                        abs(window[3] - pending[0][3]),
                    ) <= 6
                    if same_pending:
                        self._pending_shrunken_count += 1
                    else:
                        self._pending_shrunken_boxes = (window, analysis, canvas)
                        self._pending_shrunken_count = 1
                    # A running client does not legitimately resize only the
                    # minimap frame by 35%+ while the game window itself is
                    # unchanged.  In particular, the much larger candidate
                    # can be the whole top-left SEARCH CROP, which is only a
                    # performance boundary and must never become coordinate
                    # geometry.  Keep the detected minimap border instead.
                    # Keep the known-good geometry until a fresh assistant
                    # session starts; a user who intentionally changes HUD
                    # scale can simply restart before recording/patrolling.
                    return (
                        self._coordinate_median(self._box_history, 0),
                        self._coordinate_median(self._box_history, 1),
                        self._coordinate_median(self._box_history, 2),
                    )
                else:
                    self._pending_shrunken_boxes = None
                    self._pending_shrunken_count = 0
                deviation = max(
                    abs(window[0] - median[0]),
                    abs(window[1] - median[1]),
                    abs(window[2] - median[2]),
                    abs(window[3] - median[3]),
                )
                span = max(1, median[2] - median[0], median[3] - median[1])
                if deviation / span > self.box_jump_ratio:
                    self._box_history.clear()
            self._box_history.append((window, analysis, canvas))
            if len(self._box_history) < 2:
                return window, analysis, canvas
            return (
                self._coordinate_median(self._box_history, 0),
                self._coordinate_median(self._box_history, 1),
                self._coordinate_median(self._box_history, 2),
            )

    def detect(self, image: Image.Image) -> MinimapDetection:
        original_size = image.size
        cv_image, crop_box = self._analysis_crop(image)
        if self.opencv_size is not None and cv_image.size != self.opencv_size:
            cv_image = cv_image.resize(self.opencv_size, Image.Resampling.BILINEAR)
        rgb = np.asarray(cv_image.convert("RGB"))
        height, width = rgb.shape[:2]
        search_width = (
            width if self.dedicated_crop else max(1, int(round(width * 0.40)))
        )
        search_height = (
            height if self.dedicated_crop else max(1, int(round(height * 0.48)))
        )
        gray = cv2.cvtColor(rgb[:search_height, :search_width], cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 45, 140)
        contours, _hierarchy = cv2.findContours(
            edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
        )

        candidates: list[tuple[float, Box, float]] = []
        rectangles: list[tuple[Box, float]] = []
        for contour in contours:
            x, y, candidate_width, candidate_height = cv2.boundingRect(contour)
            minimum_side = 40 if self.dedicated_crop else 90
            if candidate_width < max(minimum_side, int(width * 0.055)):
                continue
            if candidate_height < max(minimum_side, int(height * 0.075)):
                continue
            max_width_ratio = 0.98 if self.dedicated_crop else 0.40
            max_height_ratio = 0.98 if self.dedicated_crop else 0.45
            if (candidate_width > width * max_width_ratio
                    or candidate_height > height * max_height_ratio):
                continue
            rectangle_area = float(candidate_width * candidate_height)
            rectangularity = abs(float(cv2.contourArea(contour))) / rectangle_area
            if rectangularity < 0.72:
                continue
            aspect = candidate_width / candidate_height
            # Expanded minimaps can become much wider without changing height.
            if not 0.45 <= aspect <= 4.50:
                continue
            candidate_box = (x, y, x + candidate_width, y + candidate_height)
            rectangles.append((candidate_box, rectangularity))
            if (self.dedicated_crop
                    and candidate_width >= width * 0.90
                    and candidate_height >= height * 0.85):
                # This is the search crop (or an edge clipped by that crop),
                # not a measured minimap border.  Inferring the border from
                # the crop made search-region size alter marker/world Y.
                continue
            max_left_ratio = 0.20 if self.dedicated_crop else 0.06
            max_top_ratio = 0.20 if self.dedicated_crop else 0.08
            if x > width * max_left_ratio or y > height * max_top_ratio:
                continue
            # Prefer the outer, highly rectangular minimap frame over its map
            # canvas and title-panel child rectangles.
            area_ratio = rectangle_area / float(width * height)
            score = rectangularity * 0.65 + min(area_ratio / 0.03, 1.0) * 0.35
            candidates.append((score, candidate_box, rectangularity))

        if not candidates:
            return self._held_or_fallback(image)

        _score, window_box, rectangularity = max(candidates, key=lambda item: item[0])
        left, top, right, bottom = window_box
        minimap_width, minimap_height = right - left, bottom - top

        # Preserve the old calibrated analysis geometry relative to the detected
        # minimap frame: at 195x256 this produces approximately (0,80,205,256).
        analysis_box = _clamp_box(
            (
                left,
                top + round(minimap_height * 0.3125),
                left + round(minimap_width * 1.0513),
                bottom,
            ),
            width,
            height,
        )
        inner_candidates: list[Box] = []
        for candidate_box, _candidate_rectangularity in rectangles:
            inner_left, inner_top, inner_right, inner_bottom = candidate_box
            inner_width = inner_right - inner_left
            inner_height = inner_bottom - inner_top
            if candidate_box == window_box:
                continue
            if (inner_left >= left and inner_right <= right + 2
                    and inner_top >= top + minimap_height * 0.25
                    and inner_bottom <= bottom + 2
                    and inner_width >= minimap_width * 0.60
                    and inner_height >= minimap_height * 0.35):
                inner_candidates.append(candidate_box)
        canvas_box = (
            max(inner_candidates, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))
            if inner_candidates else analysis_box
        )
        map_name_box = _clamp_box(
            (
                left + round(minimap_width * 0.14),
                top + round(minimap_height * 0.105),
                left + round(minimap_width * 0.98),
                top + round(minimap_height * 0.32),
            ),
            width,
            height,
        )
        working_size = (width, height)

        def to_original(box: Box) -> Box:
            if crop_box is None:
                return _scale_box(box, working_size, original_size)
            crop_width = crop_box[2] - crop_box[0]
            crop_height = crop_box[3] - crop_box[1]
            scaled = _scale_box(box, working_size, (crop_width, crop_height))
            left, top, right, bottom = scaled
            return _clamp_box(
                (left + crop_box[0], top + crop_box[1],
                 right + crop_box[0], bottom + crop_box[1]),
                original_size[0],
                original_size[1],
            )

        window_box = to_original(window_box)
        analysis_box = to_original(analysis_box)
        canvas_box = to_original(canvas_box)
        # Stabilize the coordinate frame: contour flips between near-identical
        # minimap boxes would shift the player marker AND the projected
        # patrol/rope targets every frame and stall the rope approach.  The
        # map-name crop stays raw (OCR tolerates +-1-2 px wobble).
        window_box, analysis_box, canvas_box = self._stabilize_boxes(
            window_box, analysis_box, canvas_box
        )
        map_name_box = to_original(map_name_box)
        map_name = self._read_map_name(image, map_name_box)
        confidence = float(np.clip(0.55 + rectangularity * 0.45, 0.0, 1.0))
        return self._remember_good(MinimapDetection(
            window_box=window_box,
            analysis_box=analysis_box,
            canvas_box=canvas_box,
            map_name_box=map_name_box,
            confidence=confidence,
            source="opencv",
            map_name=map_name,
        ), original_size)

    def _read_map_name(self, image: Image.Image, box: Box) -> Optional[str]:
        if self.map_name_reader is None:
            return None
        value = self.map_name_reader.read(image.crop(box))
        return value.strip() if value and value.strip() else None

    def _fallback_detection(self, image: Image.Image) -> MinimapDetection:
        width, height = image.size
        left, top, right, bottom = self.fallback_region
        analysis_box = _clamp_box(
            (round(left * width), round(top * height),
             round(right * width), round(bottom * height)),
            width,
            height,
        )
        # The fallback region is the historical analysis crop, not the entire
        # minimap frame. It remains safe for movement if OpenCV cannot localize.
        # The map-name title sits in a strip at the top of the minimap; keep
        # the identity crop to that static strip instead of the whole region so
        # recorded signatures never include the scrolling map canvas below it.
        region_width = analysis_box[2] - analysis_box[0]
        region_height = analysis_box[3] - analysis_box[1]
        map_name_box = _clamp_box(
            (
                analysis_box[0] + round(region_width * 0.14),
                analysis_box[1] + round(region_height * 0.105),
                analysis_box[0] + round(region_width * 0.98),
                analysis_box[1] + round(region_height * 0.32),
            ),
            width,
            height,
        )
        return MinimapDetection(
            window_box=analysis_box,
            analysis_box=analysis_box,
            canvas_box=analysis_box,
            map_name_box=map_name_box,
            confidence=0.0,
            source="fallback",
        )


def choose_stable_minimap_index(
    detections: Sequence[MinimapDetection],
    *,
    minimum_repeats: int = 2,
    marker_verified_indices: Sequence[int] = (),
) -> int:
    """Choose a repeated or marker-verified OpenCV minimap border.

    Repetition remains preferred. A single real OpenCV border is also safe
    when its analysis region independently contains the yellow character
    diamond. Raw fallback/search-region geometry is never accepted.
    """

    clusters: list[list[int]] = []
    for index, detection in enumerate(detections):
        if detection.source != "opencv" or detection.confidence < 0.80:
            continue
        width, height = detection.window_size
        for cluster in clusters:
            exemplar = detections[cluster[0]]
            exemplar_width, exemplar_height = exemplar.window_size
            if (abs(width - exemplar_width) <= max(6, exemplar_width * 0.08)
                    and abs(height - exemplar_height)
                    <= max(6, exemplar_height * 0.08)):
                cluster.append(index)
                break
        else:
            clusters.append([index])
    repeated = [cluster for cluster in clusters if len(cluster) >= minimum_repeats]
    if not repeated:
        verified = [
            index for index in marker_verified_indices
            if (0 <= index < len(detections)
                and detections[index].source == "opencv")
        ]
        if verified:
            return max(
                verified,
                key=lambda index: detections[index].confidence,
            )
        raise OSError("could not establish a stable detected minimap border")
    # Most repeats wins.  If both the true border and a larger enclosing
    # rectangle repeat equally, the smaller measured border is the minimap;
    # the larger one is commonly the bounded top-left search area.
    chosen = min(
        repeated,
        key=lambda cluster: (
            -len(cluster),
            detections[cluster[len(cluster) // 2]].window_size[0]
            * detections[cluster[len(cluster) // 2]].window_size[1],
        ),
    )
    return chosen[len(chosen) // 2]


__all__ = [
    "Box",
    "MapNameReader",
    "MinimapDetection",
    "MinimapDetector",
    "NormalizedBox",
    "box_to_normalized",
    "choose_stable_minimap_index",
]
