"""OpenCV-based dynamic minimap localization and map-name extraction.

This module has no worker/thread or UI dependencies.  Both movement and the
debug UI can reuse the same detector without becoming coupled to one another.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import threading
import time
from typing import Optional, Protocol

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
        fallback_region: NormalizedBox = (0.0, 0.075, 0.12, 0.24),
        map_name_reader: Optional[MapNameReader] = None,
        dedicated_crop: bool = False,
        opencv_size: Optional[tuple[int, int]] = None,
        transient_hold_seconds: float = 1.0,
    ) -> None:
        self.fallback_region = fallback_region
        self.map_name_reader = map_name_reader
        self.dedicated_crop = bool(dedicated_crop)
        self.opencv_size = (
            (max(96, int(opencv_size[0])), max(96, int(opencv_size[1])))
            if opencv_size is not None else None
        )
        self.transient_hold_seconds = max(0.0, float(transient_hold_seconds))
        self._last_good: Optional[MinimapDetection] = None
        self._last_good_image_size: Optional[tuple[int, int]] = None
        self._last_good_at = float("-inf")
        self._state_lock = threading.Lock()

    def _held_or_fallback(self, image: Image.Image) -> MinimapDetection:
        now = time.monotonic()
        with self._state_lock:
            if (self._last_good is not None
                    and self._last_good_image_size == image.size
                    and now - self._last_good_at <= self.transient_hold_seconds):
                return replace(
                    self._last_good,
                    confidence=max(0.0, self._last_good.confidence * 0.85),
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

    def detect(self, image: Image.Image) -> MinimapDetection:
        original_size = image.size
        cv_image = image
        if self.opencv_size is not None and image.size != self.opencv_size:
            cv_image = image.resize(self.opencv_size, Image.Resampling.BILINEAR)
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
                    and x <= width * 0.10
                    and height * 0.18 <= y <= height * 0.42
                    and candidate_width >= width * 0.35
                    and candidate_height >= height * 0.30):
                # In the tight top-left capture, the outer minimap border can
                # merge with the crop edge and no longer form a closed contour.
                # Its large rectangular map canvas remains reliable; reconstruct
                # the outer frame from that canvas and the known top anchoring.
                inferred_box = _clamp_box(
                    (
                        x - 4,
                        0,
                        x + candidate_width + 4,
                        y + candidate_height + 16,
                    ),
                    width,
                    height,
                )
                inferred_area = (
                    (inferred_box[2] - inferred_box[0])
                    * (inferred_box[3] - inferred_box[1])
                )
                inferred_score = 0.60 + min(
                    inferred_area / float(width * height), 0.30
                )
                candidates.append((inferred_score, inferred_box, rectangularity))
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
        window_box = _scale_box(window_box, working_size, original_size)
        analysis_box = _scale_box(analysis_box, working_size, original_size)
        canvas_box = _scale_box(canvas_box, working_size, original_size)
        map_name_box = _scale_box(map_name_box, working_size, original_size)
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
        return MinimapDetection(
            window_box=analysis_box,
            analysis_box=analysis_box,
            canvas_box=analysis_box,
            map_name_box=analysis_box,
            confidence=0.0,
            source="fallback",
        )


__all__ = [
    "Box",
    "MapNameReader",
    "MinimapDetection",
    "MinimapDetector",
    "NormalizedBox",
    "box_to_normalized",
]
