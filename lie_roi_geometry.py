"""Resolve the live lie-playfield crop without changing vendor protocol code.

The game draws the actual answer area with a thin red ``#db231b`` rectangle.
The old width-only geometry is still useful while the second window is
animating in, but a captured border is the authoritative area on both the
1366x768 and 1080x768 presets.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from autolie_api.intergration import lie_roi_box


Box = tuple[int, int, int, int]

# ``#db231b`` in the displayed RGB frame, expressed for OpenCV BGR arrays.
_PLAYFIELD_BORDER_BGR = np.asarray((27, 35, 219), dtype=np.int16)
_BORDER_CHANNEL_TOLERANCE = 55


def _groups(indices: np.ndarray) -> list[tuple[int, int]]:
    """Return inclusive runs from sorted one-dimensional pixel positions."""

    if indices.size == 0:
        return []
    runs: list[tuple[int, int]] = []
    start = previous = int(indices[0])
    for value in indices[1:]:
        current = int(value)
        if current != previous + 1:
            runs.append((start, previous))
            start = current
        previous = current
    runs.append((start, previous))
    return runs


def _nearest_edge(runs: list[tuple[int, int]], expected: int) -> Optional[tuple[int, int]]:
    if not runs:
        return None
    return min(runs, key=lambda run: abs(((run[0] + run[1]) // 2) - expected))


def red_playfield_box(client_bgr: np.ndarray, fallback: Box) -> Optional[Box]:
    """Measure the native red playfield frame, or return ``None`` if absent.

    The search is deliberately constrained around the predicted ROI.  A game
    scene may also contain red pixels, but cannot satisfy both long horizontal
    and long vertical frame-line requirements in that central region.
    """

    if client_bgr is None or client_bgr.ndim != 3 or client_bgr.shape[2] < 3:
        return None
    height, width = client_bgr.shape[:2]
    left, top, expected_width, expected_height = (int(value) for value in fallback)
    pad_x = max(24, int(round(expected_width * 0.10)))
    pad_y = max(24, int(round(expected_height * 0.12)))
    crop_left = max(0, left - pad_x)
    crop_top = max(0, top - pad_y)
    crop_right = min(width, left + expected_width + pad_x)
    crop_bottom = min(height, top + expected_height + pad_y)
    if crop_right - crop_left < 40 or crop_bottom - crop_top < 40:
        return None

    crop = client_bgr[crop_top:crop_bottom, crop_left:crop_right, :3]
    difference = np.max(
        np.abs(crop.astype(np.int16) - _PLAYFIELD_BORDER_BGR), axis=2
    )
    red = difference <= _BORDER_CHANNEL_TOLERANCE
    # One nearly unbroken line is required on every side.  This makes the
    # measurement insensitive to JPEG softness while rejecting ordinary red
    # sprites and damage text.
    row_minimum = max(24, int(round(expected_width * 0.48)))
    column_minimum = max(24, int(round(expected_height * 0.48)))
    horizontal = _groups(np.flatnonzero(red.sum(axis=1) >= row_minimum))
    vertical = _groups(np.flatnonzero(red.sum(axis=0) >= column_minimum))
    top_run = _nearest_edge(horizontal, top - crop_top)
    bottom_run = _nearest_edge(horizontal, top + expected_height - 1 - crop_top)
    left_run = _nearest_edge(vertical, left - crop_left)
    right_run = _nearest_edge(vertical, left + expected_width - 1 - crop_left)
    if not all((top_run, bottom_run, left_run, right_run)):
        return None

    measured_left = crop_left + left_run[0]
    measured_right = crop_left + right_run[1]
    measured_top = crop_top + top_run[0]
    measured_bottom = crop_top + bottom_run[1]
    measured_width = measured_right - measured_left + 1
    measured_height = measured_bottom - measured_top + 1
    if (
        measured_width < expected_width * 0.75
        or measured_width > expected_width * 1.25
        or measured_height < expected_height * 0.75
        or measured_height > expected_height * 1.25
    ):
        return None
    return measured_left, measured_top, measured_width, measured_height


def resolve_lie_roi_box(client_bgr: np.ndarray) -> tuple[Box, str]:
    """Return the measured native ROI when visible, otherwise safe fallback."""

    height, width = client_bgr.shape[:2]
    fallback = lie_roi_box(int(width), int(height))
    measured = red_playfield_box(client_bgr, fallback)
    return (measured, "red-border") if measured is not None else (fallback, "preset")


__all__ = ["red_playfield_box", "resolve_lie_roi_box"]
