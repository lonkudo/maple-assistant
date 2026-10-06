"""Independent visual outcome detection for an automatic lie-test pass.

The WebSocket worker only streams frames.  This monitor decides whether the
game itself has reached a terminal success or the black-room failure state.
Keeping those responsibilities separate means a remote socket close cannot
invent a failed game result.
"""
from __future__ import annotations

import cv2
import numpy as np


# The success dialog contains a plain 40x40 cyan-blue field. It is not an
# image-template feature: client rendering can make the blue a little darker
# or lighter, while the hue/saturation character stays stable. Requiring 90%
# coverage makes text, icons, sky, and ordinary HUD fragments insufficient.
SUCCESS_BOX_SIZE = 40
SUCCESS_BLUE_HSV_LOWER = (94, 115, 120)
SUCCESS_BLUE_HSV_UPPER = (110, 255, 255)
SUCCESS_THRESHOLD = 0.90
SUCCESS_CONSECUTIVE_FRAMES = 2


class LieSuccessBoxDetector:
    """Find the success dialog's uniform blue 40x40 box by colour coverage."""

    @property
    def size(self) -> tuple[int, int]:
        return SUCCESS_BOX_SIZE, SUCCESS_BOX_SIZE

    @staticmethod
    def _to_rgb(image: object) -> np.ndarray | None:
        if hasattr(image, "convert"):
            rgb = np.asarray(image.convert("RGB"))
        else:
            rgb = np.asarray(image)
        if rgb.ndim != 3 or rgb.shape[2] < 3:
            return None
        return rgb[:, :, :3]

    def score(self, image: object) -> float:
        """Return the highest matching-pixel fraction in a 40x40 dialog box."""

        rgb = self._to_rgb(image)
        if rgb is None:
            return 0.0
        height, width = rgb.shape[:2]
        if width < SUCCESS_BOX_SIZE or height < SUCCESS_BOX_SIZE:
            return 0.0

        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        # Success dialogs are centered in the playable area. The region
        # deliberately excludes the top HUD, where unrelated cyan widgets
        # otherwise form a full 40x40 colour patch.
        left = int(width * 0.15)
        right = int(width * 0.85)
        top = int(height * 0.25)
        bottom = int(height * 0.88)
        region = hsv[top:bottom, left:right]
        if region.shape[0] < SUCCESS_BOX_SIZE or region.shape[1] < SUCCESS_BOX_SIZE:
            region = hsv

        mask = cv2.inRange(
            region,
            np.asarray(SUCCESS_BLUE_HSV_LOWER, dtype=np.uint8),
            np.asarray(SUCCESS_BLUE_HSV_UPPER, dtype=np.uint8),
        )
        coverage = cv2.boxFilter(
            (mask > 0).astype(np.float32),
            ddepth=-1,
            ksize=(SUCCESS_BOX_SIZE, SUCCESS_BOX_SIZE),
            normalize=True,
        )
        return float(cv2.minMaxLoc(coverage)[1])


__all__ = [
    "LieSuccessBoxDetector",
    "SUCCESS_BOX_SIZE",
    "SUCCESS_CONSECUTIVE_FRAMES",
    "SUCCESS_THRESHOLD",
]
