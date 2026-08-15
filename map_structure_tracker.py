"""OpenCV minimap-structure tracking for scroll-compensated player Y."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import threading
from typing import Optional

import cv2
import numpy as np
from PIL import Image

from marker_detector import MarkerDetection
from minimap_detector import MinimapDetection


LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class MapTrackingResult:
    sequence: int
    screen_y: Optional[float]
    local_y_diamonds: Optional[float]
    scroll_y_diamonds: float
    world_y_diamonds: Optional[float]
    vertical_shift_pixels: float
    confidence: float
    mode: str


class MapStructureTracker:
    """Track static minimap translation against a persisted map reference."""

    def __init__(
        self,
        reference_path: Optional[Path] = None,
        *,
        tracking_size: int = 192,
        minimum_response: float = 0.12,
        maximum_shift_fraction: float = 0.46,
    ) -> None:
        self.reference_path = Path(reference_path) if reference_path else None
        self.tracking_size = max(96, int(tracking_size))
        self.minimum_response = float(minimum_response)
        self.maximum_shift_fraction = float(maximum_shift_fraction)
        self._lock = threading.RLock()
        self._reference: Optional[np.ndarray] = None
        self._previous: Optional[np.ndarray] = None
        self._previous_offset = 0.0
        self._world_bias = 0.0
        self._pending_anchor_world_y: Optional[float] = None
        self._last_sequence = -1
        self._last_result: Optional[MapTrackingResult] = None
        self._window = cv2.createHanningWindow(
            (self.tracking_size, self.tracking_size), cv2.CV_32F
        )
        self._load_reference()

    def _load_reference(self) -> None:
        if self.reference_path is None or not self.reference_path.is_file():
            return
        image = cv2.imread(str(self.reference_path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            LOG.warning("could not load minimap structure reference %s", self.reference_path)
            return
        self._reference = cv2.resize(
            image, (self.tracking_size, self.tracking_size),
            interpolation=cv2.INTER_AREA,
        ).astype(np.float32) / 255.0 - 0.5
        LOG.info("loaded minimap structure reference %s", self.reference_path)

    @staticmethod
    def _crop_rgb(image: Image.Image, box: tuple[int, int, int, int]) -> np.ndarray:
        return np.asarray(image.crop(box).convert("RGB"), dtype=np.uint8)

    def _structure_image(self, rgb: np.ndarray) -> np.ndarray:
        resized = cv2.resize(
            rgb, (self.tracking_size, self.tracking_size),
            interpolation=cv2.INTER_AREA,
        )
        gray = cv2.cvtColor(resized, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
        hsv = cv2.cvtColor(resized, cv2.COLOR_RGB2HSV)
        # Player/monster/status colors animate. Static platforms and map lines
        # are retained while highly saturated moving sprites are suppressed.
        dynamic = (hsv[:, :, 1] > 150) & (hsv[:, :, 2] > 125)
        smooth = cv2.GaussianBlur(gray, (0, 0), 2.2)
        structure = gray - smooth
        structure[dynamic] = 0.0
        structure = cv2.GaussianBlur(structure, (3, 3), 0.6)
        return structure.astype(np.float32)

    def analyze(
        self,
        frame: object,
        detection: MinimapDetection,
        marker: Optional[MarkerDetection],
    ) -> MapTrackingResult:
        sequence = int(getattr(frame, "sequence", self._last_sequence + 1))
        with self._lock:
            if sequence == self._last_sequence and self._last_result is not None:
                return self._last_result

            canvas_rgb = self._crop_rgb(frame.image, detection.canvas_box)
            structure = self._structure_image(canvas_rgb)
            if self._reference is None:
                self._reference = structure.copy()
                mode = "initial-reference"
                response = 0.50
                shift_y = 0.0
                offset = 0.0
            else:
                (shift_x, shift_y), response = cv2.phaseCorrelate(
                    self._reference, structure, self._window
                )
                accepted = (
                    np.isfinite(shift_x) and np.isfinite(shift_y)
                    and abs(shift_x) <= self.tracking_size * self.maximum_shift_fraction
                    and abs(shift_y) <= self.tracking_size * self.maximum_shift_fraction
                    and response >= self.minimum_response
                )
                mode = "reference"
                if not accepted and self._previous is not None:
                    (incremental_x, incremental_y), incremental_response = cv2.phaseCorrelate(
                        self._previous, structure, self._window
                    )
                    accepted = (
                        np.isfinite(incremental_x) and np.isfinite(incremental_y)
                        and abs(incremental_x) <= self.tracking_size * self.maximum_shift_fraction
                        and abs(incremental_y) <= self.tracking_size * self.maximum_shift_fraction
                        and incremental_response >= self.minimum_response
                    )
                    if accepted:
                        shift_y = incremental_y
                        response = incremental_response
                        mode = "incremental"
                if not accepted:
                    shift_y = 0.0
                    offset = self._previous_offset
                    mode = "uncertain"
                    response = max(0.0, float(response))
                else:
                    marker_height_normalized = self._marker_height_normalized(
                        marker, detection
                    )
                    delta_diamonds = -float(shift_y) / marker_height_normalized
                    offset = (
                        self._previous_offset + delta_diamonds
                        if mode == "incremental" else delta_diamonds
                    )

            local_y = self._local_marker_y_diamonds(marker, detection)
            if local_y is not None and self._pending_anchor_world_y is not None:
                self._world_bias = (
                    self._pending_anchor_world_y - (local_y + float(offset))
                )
                self._pending_anchor_world_y = None
                mode = f"session-anchor/{mode}"
                LOG.info(
                    "MAP SESSION anchored world Y at %.6f",
                    local_y + float(offset) + self._world_bias,
                )
            scroll_y = float(offset) + self._world_bias
            world_y = local_y + scroll_y if local_y is not None else None
            result = MapTrackingResult(
                sequence=sequence,
                screen_y=marker.y if marker is not None else None,
                local_y_diamonds=local_y,
                scroll_y_diamonds=scroll_y,
                world_y_diamonds=world_y,
                vertical_shift_pixels=float(shift_y),
                confidence=max(0.0, min(1.0, float(response))),
                mode=mode,
            )
            self._previous = structure
            self._previous_offset = float(offset)
            self._last_sequence = sequence
            self._last_result = result
            return result

    def start_session(self, anchor_world_y: float) -> None:
        """Start fresh translation tracking anchored to a recorded map layer.

        A new game/map session can render the same repeating platforms at a
        different minimap scroll origin.  The saved patrol coordinates remain
        valid; only the transient world-Y origin must be re-established.
        """

        with self._lock:
            self._reference = None
            self._previous = None
            self._previous_offset = 0.0
            self._world_bias = 0.0
            self._pending_anchor_world_y = float(anchor_world_y)
            self._last_sequence = -1
            self._last_result = None

    def _marker_height_normalized(
        self,
        marker: Optional[MarkerDetection],
        detection: MinimapDetection,
    ) -> float:
        canvas_height = max(1, detection.canvas_box[3] - detection.canvas_box[1])
        marker_height = marker.pixel_size[1] if marker is not None else 6
        return max(1.0, marker_height / canvas_height * self.tracking_size)

    @staticmethod
    def _local_marker_y_diamonds(
        marker: Optional[MarkerDetection],
        detection: MinimapDetection,
    ) -> Optional[float]:
        if marker is None:
            return None
        analysis_top = detection.analysis_box[1]
        analysis_height = max(1, detection.analysis_box[3] - analysis_top)
        canvas_top = detection.canvas_box[1] - analysis_top
        canvas_height = max(1, detection.canvas_box[3] - detection.canvas_box[1])
        marker_y = marker.y * analysis_height
        marker_height = max(1, marker.pixel_size[1])
        return (marker_y - canvas_top - canvas_height / 2.0) / marker_height

    def save_reference(self) -> None:
        with self._lock:
            if self.reference_path is None or self._reference is None:
                return
            self.reference_path.parent.mkdir(parents=True, exist_ok=True)
            image = np.clip((self._reference + 0.5) * 255.0, 0, 255).astype(np.uint8)
            if not cv2.imwrite(str(self.reference_path), image):
                raise OSError(f"could not save minimap reference {self.reference_path}")

    def reset(self, *, delete_reference: bool = False) -> None:
        with self._lock:
            self._reference = None
            self._previous = None
            self._previous_offset = 0.0
            self._world_bias = 0.0
            self._pending_anchor_world_y = None
            self._last_sequence = -1
            self._last_result = None
            if delete_reference and self.reference_path is not None:
                self.reference_path.unlink(missing_ok=True)


__all__ = ["MapStructureTracker", "MapTrackingResult"]
