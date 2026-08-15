"""OpenCV-based dynamic minimap localization and map-name extraction.

This module has no worker/thread or UI dependencies.  Both movement and the
debug UI can reuse the same detector without becoming coupled to one another.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class MinimapDetection:
    """All boxes use full client-image pixel coordinates."""

    window_box: Box
    analysis_box: Box
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
    ) -> None:
        self.fallback_region = fallback_region
        self.map_name_reader = map_name_reader

    def detect(self, image: Image.Image) -> MinimapDetection:
        rgb = np.asarray(image.convert("RGB"))
        height, width = rgb.shape[:2]
        search_width = max(1, int(round(width * 0.40)))
        search_height = max(1, int(round(height * 0.48)))
        gray = cv2.cvtColor(rgb[:search_height, :search_width], cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 45, 140)
        contours, _hierarchy = cv2.findContours(
            edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
        )

        candidates: list[tuple[float, Box, float]] = []
        for contour in contours:
            x, y, candidate_width, candidate_height = cv2.boundingRect(contour)
            if candidate_width < max(90, int(width * 0.055)):
                continue
            if candidate_height < max(90, int(height * 0.075)):
                continue
            if candidate_width > width * 0.40 or candidate_height > height * 0.45:
                continue
            if x > width * 0.06 or y > height * 0.08:
                continue
            rectangle_area = float(candidate_width * candidate_height)
            rectangularity = abs(float(cv2.contourArea(contour))) / rectangle_area
            if rectangularity < 0.72:
                continue
            aspect = candidate_width / candidate_height
            if not 0.55 <= aspect <= 1.65:
                continue
            # Prefer the outer, highly rectangular minimap frame over its map
            # canvas and title-panel child rectangles.
            area_ratio = rectangle_area / float(width * height)
            score = rectangularity * 0.65 + min(area_ratio / 0.03, 1.0) * 0.35
            candidates.append((score, (x, y, x + candidate_width, y + candidate_height),
                               rectangularity))

        if not candidates:
            return self._fallback_detection(image)

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
        map_name = self._read_map_name(image, map_name_box)
        confidence = float(np.clip(0.55 + rectangularity * 0.45, 0.0, 1.0))
        return MinimapDetection(
            window_box=window_box,
            analysis_box=analysis_box,
            map_name_box=map_name_box,
            confidence=confidence,
            source="opencv",
            map_name=map_name,
        )

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
