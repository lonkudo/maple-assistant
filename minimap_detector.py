"""OpenCV-based dynamic minimap localization and map-name extraction.

This module has no worker/thread or UI dependencies.  Both movement and the
debug UI can reuse the same detector without becoming coupled to one another.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import logging
import threading
import time
from typing import Any, Mapping, Optional, Protocol, Sequence

import cv2
import numpy as np
from PIL import Image

LOG = logging.getLogger(__name__)


Box = tuple[int, int, int, int]
NormalizedBox = tuple[float, float, float, float]

# The game HUD is FIXED PIXEL above a ~1366px client width: measured on the
# real client the minimap and the status bars keep the same absolute size at
# 1920x1080 and 1366x768.  BELOW that width the game scales the whole HUD
# down (at 1024x768 everything measures ~0.75x: minimap 250x127 -> 187x95,
# status 538x40 -> 403x30).  All fixed-pixel HUD regions must be scaled by
# this factor before use.
HUD_REFERENCE_WIDTH = 1366


def hud_scale_for(client_width: int) -> float:
    """Return the HUD scale factor for a client of ``client_width`` px."""

    return min(1.0, float(client_width) / HUD_REFERENCE_WIDTH)


def minimap_search_box(
    fallback_region: NormalizedBox, client_width: int, *, include_header: bool = True
) -> tuple[float, float, float, float]:
    """Return the broad client-pixel minimap search box.

    The minimap window begins at the client top edge.  The measured minimap
    geometry remains dynamic; this is only its sufficiently broad search
    region, expressed in the same captured-pixel coordinate space.
    """

    scale = hud_scale_for(client_width)
    left, top, right, bottom = fallback_region
    if include_header:
        return (left * scale, 0.0, right * scale, max(bottom, 400) * scale)
    return (left * scale, top * scale, right * scale, bottom * scale)


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
    # True only when ``analysis_box``/``canvas_box`` are a MEASURED inner canvas
    # (or a previously measured one reused).  It is display-only: the yellow
    # marker/patrol overlay is not painted for a region that could not be
    # measured, so a diagnostic rectangle can never claim the whole minimap
    # window as the map canvas.  Nothing about input or auto-lie reads it.
    canvas_measured: bool = False

    @property
    def window_size(self) -> tuple[int, int]:
        left, top, right, bottom = self.window_box
        return right - left, bottom - top

    def normalized_analysis_box(self, image_size: tuple[int, int]) -> NormalizedBox:
        return box_to_normalized(self.analysis_box, image_size)


def is_verified_border(detection: MinimapDetection) -> bool:
    """True when a detection can seed/calibrate minimap geometry.

    Only a measured OpenCV contour is a border.  The fallback rectangle is a
    deliberately generous *search* area, not minimap geometry: accepting it
    just because it contains the player marker makes the startup overlay much
    larger than the minimap and poisons all normalized marker coordinates.
    """

    return detection.source.startswith("opencv")


# The minimap window is a panel: the map canvas sits inside it, below a header
# strip (map name / buttons) and above a thin footer.  Those are NOT a fixed
# height and NOT a fixed offset from the window's bottom - the game does not
# rescale this HUD uniformly (measured canvas: ~262x152 at 1366, ~89x118 at
# 1080x768, and a 195x255 window whose canvas is 182x148 on the older client) -
# so the canvas is measured from the captured pixels themselves.  The inner
# border is drawn as a rectangle: its contour is the measurement, and these
# bounds only reject a candidate that cannot be a canvas of this window.
INNER_CANVAS_MIN_WIDTH_SHARE = 0.50
INNER_CANVAS_MIN_HEIGHT_SHARE = 0.15
INNER_CANVAS_MIN_AREA_SHARE = 0.20
# The window's own border is 1-3 px thick; a canvas candidate must be strictly
# inside it, so the measurement can never return the window itself.
INNER_CANVAS_EDGE_MARGIN = 1
# Adopting a newly measured canvas that only jitters by a few pixels would shift
# every normalized marker coordinate with it.  Keep the remembered one instead.
INNER_CANVAS_JITTER_TOLERANCE = 3


def _box_text(box: Box) -> str:
    """``(left, top, right, bottom)`` for a log line."""

    return "(%d, %d, %d, %d)" % tuple(int(value) for value in box)


def _is_fallback_search_geometry(box: Box, image_size: tuple[int, int]) -> bool:
    """Return whether a saved box is the old broad fallback search region.

    Earlier versions could serialize ``(0, 50, 400, 320)`` (HUD-scaled) as a
    calibration after merely finding a yellow marker inside it.  Keep valid
    old OpenCV recordings, but discard that known-invalid geometry so a new
    patrol cannot reuse it forever.
    """

    width, _height = image_size
    scale = hud_scale_for(width)
    expected = tuple(round(value) for value in minimap_search_box(
        (0, 50, 400, 320), width
    ))
    tolerance = max(3, round(4 * scale))
    return (
        box[0] <= tolerance
        and abs(box[1] - expected[1]) <= tolerance
        and box[2] >= expected[2] - tolerance
        and box[3] >= expected[3] - tolerance
    )


def minimap_calibration_to_dict(
    detection: MinimapDetection,
    image_size: tuple[int, int],
) -> dict[str, Any]:
    """Serialize a recording-verified border in ABSOLUTE pixels.

    The game HUD is fixed pixel: only the playfield viewport scales with the
    window, so the minimap border occupies the same pixels at any resolution.
    Storing absolute boxes (instead of client-normalized fractions) makes the
    saved calibration valid regardless of the current window size.
    """

    if not is_verified_border(detection):
        raise ValueError("只有已确认的小地图边框才能校准：请先让小地图完整可见再录制。")
    return {
        "schema": 2,
        "border_source": detection.source,
        "recorded_client_size": [int(image_size[0]), int(image_size[1])],
        "window_box": list(detection.window_box),
        "analysis_box": list(detection.analysis_box),
        "canvas_box": list(detection.canvas_box),
        "map_name_box": list(detection.map_name_box),
        "confidence": float(detection.confidence),
    }


def minimap_calibration_from_dict(
    value: Mapping[str, Any],
    image_size: tuple[int, int],
) -> Optional[MinimapDetection]:
    """Return a saved border in ABSOLUTE pixels, clamped to the client.

    Schema 2 stores absolute pixel boxes (see ``minimap_calibration_to_dict``)
    and is applied unchanged because the minimap is fixed pixel.  Schema 1
    stored client-normalized fractions from an older release and is rescaled
    for compatibility; a fresh recording overwrites it with schema 2.
    """

    width, height = image_size
    if width < 1 or height < 1:
        return None
    schema = value.get("schema")
    if schema not in (1, 2):
        return None
    normalized = schema == 1

    def load_box(name: str) -> Box:
        raw = value.get(name)
        if not isinstance(raw, (list, tuple)) or len(raw) != 4:
            raise ValueError(name)
        numbers = tuple(float(item) for item in raw)
        if not all(np.isfinite(item) for item in numbers):
            raise ValueError(name)
        if normalized:
            if not all(-0.01 <= item <= 1.01 for item in numbers):
                raise ValueError(name)
            numbers = (
                numbers[0] * width, numbers[1] * height,
                numbers[2] * width, numbers[3] * height,
            )
        if numbers[2] <= numbers[0] or numbers[3] <= numbers[1]:
            raise ValueError(name)
        return _clamp_box(
            (
                round(numbers[0]), round(numbers[1]),
                round(numbers[2]), round(numbers[3]),
            ),
            width,
            height,
        )

    try:
        border_source = value.get("border_source")
        if border_source is not None and not str(border_source).startswith("opencv"):
            return None
        detection = MinimapDetection(
            window_box=load_box("window_box"),
            analysis_box=load_box("analysis_box"),
            canvas_box=load_box("canvas_box"),
            map_name_box=load_box("map_name_box"),
            confidence=float(value.get("confidence", 1.0)),
            source="opencv-recording",
        )
    except (TypeError, ValueError, OverflowError):
        return None
    window_width, window_height = detection.window_size
    if window_width < 20 or window_height < 20:
        return None
    if _is_fallback_search_geometry(detection.window_box, image_size):
        return None
    return detection


class MapNameReader(Protocol):
    """Replaceable OCR/template adapter for the extracted map-name image."""

    def read(self, image: Image.Image) -> Optional[str]: ...


class MinimapDetector:
    """Locate the resizable top-left minimap using rectangular edge contours."""

    def __init__(
        self,
        # default measured on the real client: the map-name strip occupies
        # the top ~64px and the minimap starts below it; the search region
        # starts at y=50 for 14px of tolerance.
        fallback_region: NormalizedBox = (0, 50, 400, 320),
        map_name_reader: Optional[MapNameReader] = None,
        dedicated_crop: bool = False,
        opencv_size: Optional[tuple[int, int]] = None,
        transient_hold_seconds: float = 1.0,
        box_history: int = 5,
        box_jump_ratio: float = 0.25,
        include_header: bool = True,
    ) -> None:
        # The game HUD is fixed pixel above ~1366px client width: the minimap
        # occupies the same absolute pixels at any window resolution in that
        # range.  ``fallback_region`` is therefore expressed in ABSOLUTE
        # client pixels at the HUD reference size; below 1366px the game
        # scales the whole HUD down, so every use of the region scales it by
        # ``hud_scale_for`` of the CURRENT frame's client width.
        self.fallback_region = fallback_region
        self.map_name_reader = map_name_reader
        self.dedicated_crop = bool(dedicated_crop)
        self.include_header = bool(include_header)
        self.opencv_size = (
            (max(96, int(opencv_size[0])), max(96, int(opencv_size[1])))
            if opencv_size is not None else None
        )
        # Full-client captures are far larger than the minimap; images at or
        # below this size are treated as already-cropped minimap captures and
        # searched whole.  Kept independent of ``opencv_size`` so raising the
        # analysis resolution never reclassifies a normal client frame.
        self._tight_crop_size = (480, 480)
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
        # Hysteresis anchor: the returned geometry holds the first
        # established box while the raw contour alternates between two
        # near-identical boxes (border vs canvas edge).  A different box is
        # adopted only after the SAME box repeats for a full history window.
        self._stable_boxes: Optional[tuple[Box, Box, Box]] = None
        self._stable_candidate: Optional[tuple[Box, Box, Box]] = None
        self._stable_candidate_count = 0
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
        # The canvas measured from the captured border earlier in this map
        # session.  It is what a frame with an unreadable inner border reuses,
        # so the map coordinate frame never falls back to the whole window.
        self._measured_canvas: Optional[Box] = None
        self._measured_canvas_size: Optional[tuple[int, int]] = None
        self._last_canvas_log: Optional[str] = None
        self._state_lock = threading.Lock()
        # The outer minimap window is useful for finding the widget, but its
        # header is not map coordinate space.  Maintain a separate detector
        # that starts below that header and dynamically measures the canvas.
        self._canvas_detector: Optional[MinimapDetector] = None
        if self.include_header and self.dedicated_crop:
            self._canvas_detector = MinimapDetector(
                fallback_region=fallback_region,
                map_name_reader=None,
                dedicated_crop=True,
                opencv_size=self.opencv_size,
                transient_hold_seconds=transient_hold_seconds,
                box_history=box_history,
                box_jump_ratio=box_jump_ratio,
                include_header=False,
            )

    def _scaled_fallback_region(self, client_width: int) -> tuple[float, float, float, float]:
        """Scale the reference fallback region to the current client width."""

        return minimap_search_box(
            self.fallback_region, client_width,
            include_header=self.include_header,
        )

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
            self._stable_boxes = None
            self._stable_candidate = None
            self._stable_candidate_count = 0
            self._last_good = None
            self._last_good_image_size = None
            self._last_good_at = float("-inf")
            self._measured_canvas = None
            self._measured_canvas_size = None
            self._last_canvas_log = None
        if self._canvas_detector is not None:
            self._canvas_detector.reset_geometry()

    def seed_geometry(
        self, detection: MinimapDetection, image_size: tuple[int, int]
    ) -> None:
        """Lock a verified minimap border as this map session's baseline."""

        if not is_verified_border(detection):
            raise ValueError("小地图坐标必须来自已确认的边框：本次画面边框未通过校验。")
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

    def _measure_inner_canvas(
        self, image: Image.Image, window_box: Box
    ) -> Optional[Box]:
        """Measure the map canvas from the captured pixels inside the window.

        The canvas is the map panel's own inner rectangle: the largest closed
        border contour that is strictly inside the minimap window and covers a
        substantial part of it.  Its top and bottom come from that border, so
        the header and footer are excluded by measurement, never by a fixed
        height, a fixed bottom offset or a proportion of the window.

        Returns ``None`` when the frame shows no usable inner border; the
        caller then reuses the last verified canvas rather than inventing one.
        """

        window_box = _clamp_box(window_box, *image.size)
        left, top, right, bottom = window_box
        width, height = right - left, bottom - top
        if width < 16 or height < 16:
            return None
        try:
            gray = cv2.cvtColor(
                np.asarray(image.crop(window_box).convert("RGB")),
                cv2.COLOR_RGB2GRAY,
            )
        except (ValueError, OSError):
            return None
        margin = INNER_CANVAS_EDGE_MARGIN
        # The plain threshold is the faithful measurement, so a NESTED canvas
        # rectangle it finds wins immediately.  A fainter border and a border
        # broken by map artwork are consulted otherwise, because closing the
        # edge map can merge the canvas with the artwork and widen the box.
        passes = (
            (cv2.Canny(gray, 45, 140), None),
            (cv2.Canny(gray, 30, 90), None),
            (cv2.Canny(gray, 45, 140), "close"),
        )
        loose: Optional[Box] = None
        for edges, mode in passes:
            if mode == "close":
                edges = cv2.morphologyEx(
                    edges, cv2.MORPH_CLOSE,
                    cv2.getStructuringElement(cv2.MORPH_RECT, (3, 1)),
                )
                edges = cv2.morphologyEx(
                    edges, cv2.MORPH_CLOSE,
                    cv2.getStructuringElement(cv2.MORPH_RECT, (1, 3)),
                )
            nested, candidate = self._canvas_candidate_from_edges(
                edges, width, height, margin
            )
            if nested and candidate is not None:
                return (
                    left + candidate[0],
                    top + candidate[1],
                    left + candidate[2],
                    top + candidate[3],
                )
            if candidate is not None and (
                loose is None
                or self._box_area(candidate) > self._box_area(loose)
            ):
                loose = candidate
        if loose is None:
            return None
        return (
            left + loose[0],
            top + loose[1],
            left + loose[2],
            top + loose[3],
        )

    @staticmethod
    def _box_area(box: Box) -> int:
        return max(0, box[2] - box[0]) * max(0, box[3] - box[1])

    @staticmethod
    def _canvas_candidate_from_edges(
        edges: np.ndarray, width: int, height: int, margin: int
    ) -> tuple[bool, Optional[Box]]:
        """Best canvas rectangle in one edge map: ``(nested, box)``.

        The map canvas is the rectangle nested DIRECTLY inside the window's own
        border, so when that border is in the edge map only first-level
        rectangles are considered: anything nested deeper is map artwork or a
        panel drawn inside the canvas, and adopting one would cut the character
        marker out of the analysis region.  A frame whose edge map has no window
        border can only offer a looser answer, which the caller accepts only
        when no pass produced a nested canvas.
        """

        contours, hierarchy = cv2.findContours(
            edges, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE
        )
        if hierarchy is None:
            return False, None
        parents = hierarchy[0][:, 3]

        def rect_of(contour: np.ndarray) -> Box:
            x, y, contour_width, contour_height = cv2.boundingRect(contour)
            return (x, y, x + contour_width, y + contour_height)

        def depth_of(index: int) -> int:
            value = 0
            parent = int(parents[index])
            while parent != -1:
                index, parent = parent, int(parents[parent])
                value += 1
            return value

        boxes = [rect_of(contour) for contour in contours]
        depths = [depth_of(index) for index in range(len(contours))]
        has_window_border = any(
            depths[index] == 0
            and boxes[index][0] <= margin
            and boxes[index][1] <= margin
            and boxes[index][2] >= width - margin
            and boxes[index][3] >= height - margin
            for index in range(len(contours))
        )
        best: Optional[tuple[int, Box]] = None
        for index, box in enumerate(boxes):
            if has_window_border and depths[index] != 1:
                continue
            x, y, right, bottom = box
            candidate_width, candidate_height = right - x, bottom - y
            if candidate_width < width * INNER_CANVAS_MIN_WIDTH_SHARE:
                continue
            if candidate_height < height * INNER_CANVAS_MIN_HEIGHT_SHARE:
                continue
            if (x < margin or y < margin
                    or right > width - margin or bottom > height - margin):
                # This is the window's own border (or the search region), not
                # an inner canvas: the canvas is inset on every side.
                continue
            share = (candidate_width * candidate_height) / float(width * height)
            if share < INNER_CANVAS_MIN_AREA_SHARE:
                continue
            if best is None or share > best[0]:
                best = (share, box)
        if best is None:
            return has_window_border, None
        return has_window_border, best[1]

    def _verified_canvas(self, image_size: tuple[int, int]) -> Optional[Box]:
        """The canvas measured earlier in this map session, if there is one."""

        with self._state_lock:
            if (self._measured_canvas is None
                    or self._measured_canvas_size != image_size):
                return None
            return self._measured_canvas

    def _remember_canvas(self, box: Box, image_size: tuple[int, int]) -> Box:
        """Remember a measured canvas, keeping the frame stable while it jitters."""

        with self._state_lock:
            remembered = self._measured_canvas
            if (remembered is not None
                    and self._measured_canvas_size == image_size
                    and max(
                        abs(box[index] - remembered[index]) for index in range(4)
                    ) <= INNER_CANVAS_JITTER_TOLERANCE):
                return remembered
            self._measured_canvas = box
            self._measured_canvas_size = image_size
            return box

    def _note_canvas(self, message: str) -> None:
        """Log the canvas decision once per distinct outcome, not per frame."""

        if self._last_canvas_log == message:
            return
        self._last_canvas_log = message
        LOG.info("INNER CANVAS: %s", message)

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
        left, top, right, bottom = self._scaled_fallback_region(width)
        crop_box = _clamp_box(
            (round(left), round(top), round(right), round(bottom)),
            width,
            height,
        )
        if image.size[0] <= self._tight_crop_size[0] \
                and image.size[1] <= self._tight_crop_size[1]:
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
                    # A severe one-frame change is usually a contour artifact.
                    # Hold the known-good geometry while a candidate is
                    # unconfirmed.  But the minimap can also legitimately
                    # resize mid-session (window resize, HUD scale, map
                    # switch): once the SAME new box repeats for a full
                    # history window it is normal detection and must be
                    # adopted.
                    #
                    # The one exception is a PARTIAL TITLE STRIP: it keeps the
                    # full border's width (e.g. 239x68 vs the full 239x184)
                    # and only collapses in height, whatever the current zoom
                    # or minimap size.  A genuine resize changes width AND
                    # height together, so a same-width height collapse is
                    # always a strip and is never adopted, no matter how many
                    # frames repeat it (its analysis box excludes the yellow
                    # marker and remaps recorded layer coordinates).
                    same_width = abs(window_width - median_width) <= max(
                        3, round(median_width * 0.05)
                    )
                    height_collapsed = window_height < median_height * 0.65
                    if same_width and height_collapsed:
                        self._pending_shrunken_boxes = None
                        self._pending_shrunken_count = 0
                        return (
                            self._coordinate_median(self._box_history, 0),
                            self._coordinate_median(self._box_history, 1),
                            self._coordinate_median(self._box_history, 2),
                        )
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
                    if self._pending_shrunken_count >= self.box_history_len:
                        # Same box repeated for a full history window: a real
                        # resize or recovery from a poisoned first frame, not
                        # a one-off contour flicker.  Adopt it as the new
                        # session baseline.
                        adopted = self._pending_shrunken_boxes
                        self._box_history.clear()
                        self._box_history.append(adopted)
                        self._pending_shrunken_boxes = None
                        self._pending_shrunken_count = 0
                        self._stable_boxes = adopted
                        self._stable_candidate = None
                        self._stable_candidate_count = 0
                        return adopted
                    # Transient severe change: keep the known-good geometry.
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
            median_boxes = (
                self._coordinate_median(self._box_history, 0),
                self._coordinate_median(self._box_history, 1),
                self._coordinate_median(self._box_history, 2),
            )
            return self._hold_stable_boxes(
                window, analysis, canvas, median_boxes
            )

    @staticmethod
    def _boxes_close(first: Box, second: Box, tolerance: int = 6) -> bool:
        """True when two window boxes differ by at most ``tolerance`` px."""

        return max(
            abs(first[0] - second[0]), abs(first[1] - second[1]),
            abs(first[2] - second[2]), abs(first[3] - second[3]),
        ) <= tolerance

    def _hold_stable_boxes(
        self,
        window: Box,
        analysis: Box,
        canvas: Box,
        median_boxes: tuple[Box, Box, Box],
    ) -> tuple[Box, Box, Box]:
        """Hold one box while the raw contour alternates between two
        near-identical boxes (e.g. the border vs the canvas edge - the live
        client flips 87x70 / 80x70 every frame).

        The per-coordinate median cannot smooth an A/B/A/B alternation: the
        median itself flips with the history window, shifting the whole
        normalized coordinate frame (marker AND projected targets) every few
        frames, which makes the character look like it is moving (stall
        recovery never fires) while it is actually stuck.  A different box is
        adopted only after the SAME box repeats for a full history window -
        a genuine resize, HUD scale change, or map switch.
        """

        if self._stable_boxes is None:
            self._stable_boxes = median_boxes
            return median_boxes
        stable = self._stable_boxes
        if self._boxes_close(window, stable[0]):
            # Same geometry as the anchor (or its frame-to-frame jitter):
            # hold it and forget any in-progress candidate.
            self._stable_candidate = None
            self._stable_candidate_count = 0
            return stable
        candidate = self._stable_candidate
        if candidate is not None and self._boxes_close(window, candidate[0]):
            self._stable_candidate_count += 1
        else:
            self._stable_candidate = (window, analysis, canvas)
            self._stable_candidate_count = 1
        if self._stable_candidate_count >= self.box_history_len:
            self._stable_boxes = self._stable_candidate
            self._stable_candidate = None
            self._stable_candidate_count = 0
        return self._stable_boxes

    def detect(self, image: Image.Image) -> MinimapDetection:
        original_size = image.size
        cv_image, crop_box = self._analysis_crop(image)
        if self.opencv_size is not None and cv_image.size != self.opencv_size:
            # Fit inside the analysis box preserving aspect ratio.  The old
            # exact-square squash distorted a non-square crop (e.g. 375x288 ->
            # 200x200) and thinned the minimap border until Canny could no
            # longer close its rectangle, so large clients always fell back.
            # Never upscale: a crop smaller than the box is already fine.
            working_width, working_height = cv_image.size
            target_width, target_height = self.opencv_size
            scale = min(
                1.0,
                target_width / working_width,
                target_height / working_height,
            )
            cv_image = cv_image.resize(
                (
                    max(1, round(working_width * scale)),
                    max(1, round(working_height * scale)),
                ),
                Image.Resampling.BILINEAR,
            )
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

        def is_origin_minimap_border(
            x: int, y: int, candidate_width: int, candidate_height: int
        ) -> bool:
            """Accept a substantial top-left frame despite Canny area noise."""

            return (
                self.include_header
                and self.dedicated_crop
                and x <= 3 and y <= 3
                and candidate_width >= minimum_side
                and candidate_height >= minimum_side
            )
        # The minimap is a large fraction of the search region (measured
        # ~380x250 in a 400x280 box).  Tiny contours - a UI button, the map
        # name strip, a minimap child rectangle - are NOT the minimap border:
        # the live client produced a 40x40 box whose analysis area excluded
        # the yellow marker, failing every recording.  Require a meaningful
        # minimum side relative to the search region.
        region_min = max(1, min(width, height))
        minimum_side = (
            max(40, round(region_min * 0.25)) if self.dedicated_crop else 90
        )
        for contour in contours:
            x, y, candidate_width, candidate_height = cv2.boundingRect(contour)
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
            origin_minimap_border = is_origin_minimap_border(
                x, y, candidate_width, candidate_height
            )
            if rectangularity < 0.72 and not origin_minimap_border:
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
            # canvas and title-panel child rectangles.  The area cap must not
            # saturate for both the full border and its title strip (e.g.
            # 0.41 vs 0.15 of the crop): a saturated tie was broken by
            # contour order, making detection flicker between the strip and
            # the full border every frame.  The larger outer frame is the
            # minimap geometry and must win deterministically.
            area_ratio = rectangle_area / float(width * height)
            score = rectangularity * 0.65 + min(area_ratio / 0.20, 1.0) * 0.35
            if origin_minimap_border:
                # The full minimap frame encloses an inner canvas with a
                # cleaner Canny contour.  Prefer the full coordinate frame.
                score += 0.50
            candidates.append((score, candidate_box, rectangularity))

        if not candidates:
            return self._held_or_fallback(image)

        # A partially detected outer border shares the real frame's top-left
        # corner but is strictly shorter (the title-panel strip, e.g. 239x68
        # vs the full 239x184).  When both appear in the same frame the strip
        # must never become the minimap geometry: its analysis box excludes
        # the yellow player marker, so recording/patrol would fail even
        # though the full border was visible.  Drop any candidate strictly
        # contained in another candidate anchored at the same corner.
        outer: list[tuple[float, Box, float]] = []
        for score, box, rectangularity in candidates:
            left, top, right, bottom = box
            contained = any(
                abs(other[1][0] - left) <= 1
                and abs(other[1][1] - top) <= 1
                and other[1][2] >= right - 1
                and other[1][3] > bottom + 1
                for other in candidates
            )
            if not contained:
                outer.append((score, box, rectangularity))
        if not outer:
            return self._held_or_fallback(image)
        candidates = outer

        _score, window_box, rectangularity = max(candidates, key=lambda item: item[0])
        left, top, right, bottom = window_box
        minimap_width, minimap_height = right - left, bottom - top

        # The analysis area is the whole detected minimap frame.  The old
        # 0.3125 top offset (and 1.0513 right extension) came from a layout
        # where the map-name strip sat INSIDE the minimap's top; now the
        # map name is a separate strip ABOVE the minimap, so the marker /
        # patrol analysis box must match the window box exactly (the green
        # and yellow rectangles in the startup overlay must coincide).
        analysis_box = window_box
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
        # Detectors without the separate canvas pass (a tight minimap crop, or
        # the canvas pass itself) use the detected border as both regions, which
        # is the historical behaviour and is measured geometry.
        canvas_measured = True
        if self._canvas_detector is not None:
            canvas_detection = self._canvas_detector.detect(image)
            canvas_measured = False
            if canvas_detection.source.startswith("opencv"):
                # This box begins below the title/header strip and ends at
                # the actual map-panel border.  It is the only coordinate
                # frame used for marker, patrol, rope, and layer logic.
                analysis_box = canvas_detection.window_box
                canvas_box = canvas_detection.canvas_box
                canvas_measured = True
            elif canvas_box != window_box:
                # The outer pass sometimes already found an inner contour;
                # retain that measured candidate rather than enlarging the
                # analysis area back to the complete minimap window.
                analysis_box = canvas_box
                canvas_measured = True
            else:
                # Neither pass produced a closed inner border on this frame.
                # Measure it from the captured pixels; when even that fails,
                # reuse the canvas measured earlier in this session.  The
                # analysis region is NEVER enlarged back to the whole minimap
                # window just because the inner border is hard to read.
                measured = self._measure_inner_canvas(image, window_box)
                if measured is not None:
                    analysis_box = self._remember_canvas(measured, original_size)
                    canvas_box = analysis_box
                    canvas_measured = True
                    self._note_canvas("measured " + _box_text(analysis_box))
                else:
                    remembered = self._verified_canvas(original_size)
                    if remembered is not None:
                        analysis_box = remembered
                        canvas_box = remembered
                        canvas_measured = True
                        self._note_canvas(
                            "reused " + _box_text(remembered)
                            + " (this frame's inner border is not readable)"
                        )
                    else:
                        # Nothing has ever been measured for this client size:
                        # keep the historical analysis region and mark the
                        # canvas unmeasured, so the diagnostic yellow rectangle
                        # is simply not drawn instead of covering the window.
                        canvas_measured = False
                        self._note_canvas(
                            "not measurable and none remembered; the analysis "
                            "region stays the detected minimap window and no "
                            "yellow marker region is drawn"
                        )
            if not canvas_measured:
                analysis_box = window_box
                canvas_box = window_box
        # The MAP NAME is a fixed strip ABOVE the minimap (measured ~64px
        # tall): its crop must read that strip from the ORIGINAL image, not
        # the search crop (which starts below it at y=50).  The name crop is
        # the band between the image top and the detected border's top,
        # widened to the border's horizontal span.
        map_name_box = _clamp_box(
            (window_box[0], 0, window_box[2], max(1, window_box[1])),
            original_size[0],
            original_size[1],
        )
        if map_name_box[3] - map_name_box[1] < 4:
            # The minimap touches the window top (no map-name strip above);
            # fall back to the top band of the minimap itself.
            band_top = window_box[1]
            band_bottom = min(
                original_size[1],
                band_top + max(4, (window_box[3] - window_box[1]) // 4),
            )
            map_name_box = _clamp_box(
                (window_box[0], band_top, window_box[2], band_bottom),
                original_size[0],
                original_size[1],
            )
        map_name = self._read_map_name(image, map_name_box)
        confidence = float(np.clip(0.55 + rectangularity * 0.45, 0.0, 1.0))
        if is_origin_minimap_border(
            window_box[0], window_box[1],
            window_box[2] - window_box[0], window_box[3] - window_box[1],
        ):
            confidence = max(confidence, 0.85)
        return self._remember_good(MinimapDetection(
            window_box=window_box,
            analysis_box=analysis_box,
            canvas_box=canvas_box,
            map_name_box=map_name_box,
            confidence=confidence,
            source="opencv",
            map_name=map_name,
            canvas_measured=canvas_measured,
        ), original_size)

    def _read_map_name(self, image: Image.Image, box: Box) -> Optional[str]:
        if self.map_name_reader is None:
            return None
        value = self.map_name_reader.read(image.crop(box))
        return value.strip() if value and value.strip() else None

    def _fallback_detection(self, image: Image.Image) -> MinimapDetection:
        width, height = image.size
        left, top, right, bottom = self._scaled_fallback_region(width)
        analysis_box = _clamp_box(
            (round(left), round(top), round(right), round(bottom)),
            width,
            height,
        )
        # The fallback region starts BELOW the map-name strip (the search
        # region top is the map-name bottom, with 4px tolerance).  The
        # map-name title sits in the strip ABOVE the minimap
        # (y=0..analysis_top); keep the identity crop to that static strip
        # so recorded signatures never include the scrolling map canvas
        # below it.
        map_name_box = _clamp_box(
            (
                analysis_box[0],
                0,
                analysis_box[2],
                analysis_box[1],
            ),
            width,
            height,
        )
        if map_name_box[3] - map_name_box[1] < 4:
            # The minimap touches the window top (no map-name strip above);
            # fall back to the top band of the region itself.
            region_height = analysis_box[3] - analysis_box[1]
            map_name_box = _clamp_box(
                (
                    analysis_box[0],
                    analysis_box[1],
                    analysis_box[2],
                    min(height, analysis_box[1] + max(4, region_height // 4)),
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
            # The fallback region is a broad SEARCH area, not the minimap and
            # not a canvas: nothing here was measured from a border.
            canvas_measured=False,
        )


def choose_stable_minimap_index(
    detections: Sequence[MinimapDetection],
    *,
    minimum_repeats: int = 2,
    marker_verified_indices: Sequence[int] = (),
) -> int:
    """Choose a repeated or marker-verified minimap border.

    Repetition remains preferred. A single verified border is also safe when
    its analysis region independently contains the yellow character diamond:
    either a real OpenCV contour, or the fixed HUD region (map-name strip
    above the measured minimap area) that the caller confirmed contains the
    marker.  Raw unverified fallback/search-region geometry is never
    accepted.
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
                and is_verified_border(detections[index]))
        ]
        if verified:
            return max(
                verified,
                key=lambda index: detections[index].confidence,
            )
        raise OSError("无法稳定检测到小地图边框：请让小地图完整可见（不要被界面遮挡）后重试。")
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
    "minimap_calibration_from_dict",
    "minimap_calibration_to_dict",
]
