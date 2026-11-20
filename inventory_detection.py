"""OpenCV inventory-tab gate.

The gate deliberately recognises the stable Chinese labels in the item window,
rather than its large illustrated body.  A game scene can contain a similar
coloured panel; four independently matched labels make that a much stronger
signal that the item window is genuinely open.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import cv2
import numpy as np


ITEM_TAB_LABELS: Mapping[str, str] = {
    "inventory": "道具栏",
    "equipment": "装备",
    "consume": "消耗",
    "setup": "设置",
    "other": "其他",
    "special": "特殊",
    "decorate": "装饰",
}
ITEM_TAB_MINIMUM_MATCHES = 4
# Edge matching makes the red selected tab and grey unselected tabs equivalent.
ITEM_TAB_MATCH_THRESHOLD = 0.62
ITEM_TAB_TEMPLATE_SCALES = (0.85, 0.925, 1.0, 1.075, 1.15)
_TEMPLATE_FILE = Path(__file__).with_name("recording-assets") / "item_tab_templates.npz"
_TEMPLATES: dict[str, np.ndarray] | None = None


@dataclass(frozen=True)
class ItemDetection:
    """The result of one frame's item-window label check."""

    active: bool
    matched_labels: tuple[str, ...]
    scores: Mapping[str, float]

    @property
    def matched_count(self) -> int:
        return len(self.matched_labels)


def _edge_image(frame: Any) -> np.ndarray:
    """Convert a capture frame (BGR/RGB/greyscale/RGBA) to Canny edges."""

    image = np.asarray(frame)
    if image.ndim == 2:
        grey = image
    elif image.ndim == 3 and image.shape[2] in (3, 4):
        # Canny needs luminance only; RGB/BGR ordering has no meaningful
        # bearing on the inventory tab glyph edges.
        grey = cv2.cvtColor(image[:, :, :3], cv2.COLOR_BGR2GRAY)
    else:
        raise ValueError("item detection needs a greyscale, RGB, BGR, or RGBA frame")
    return cv2.Canny(grey, 80, 180)


def _load_templates() -> dict[str, np.ndarray]:
    global _TEMPLATES
    if _TEMPLATES is not None:
        return _TEMPLATES
    if not _TEMPLATE_FILE.is_file():
        raise FileNotFoundError(f"item-tab templates are missing: {_TEMPLATE_FILE}")
    with np.load(_TEMPLATE_FILE) as stored:
        templates = {
            key: np.asarray(stored[key], dtype=np.uint8)
            for key in ITEM_TAB_LABELS
            if key in stored
        }
    missing = set(ITEM_TAB_LABELS).difference(templates)
    if missing:
        raise ValueError(f"item-tab templates are incomplete: {', '.join(sorted(missing))}")
    _TEMPLATES = templates
    return templates


def _best_template_score(
    frame_edges: np.ndarray, template: np.ndarray,
    scales: tuple[float, ...],
) -> float:
    """Best normalized edge-template score across practical client scales."""

    best = -1.0
    frame_height, frame_width = frame_edges.shape[:2]
    for scale in scales:
        width = max(1, round(template.shape[1] * scale))
        height = max(1, round(template.shape[0] * scale))
        if width > frame_width or height > frame_height:
            continue
        candidate = (
            template if (width, height) == (template.shape[1], template.shape[0])
            else cv2.resize(template, (width, height), interpolation=cv2.INTER_NEAREST)
        )
        response = cv2.matchTemplate(frame_edges, candidate, cv2.TM_CCOEFF_NORMED)
        _minimum, maximum, _min_location, _max_location = cv2.minMaxLoc(response)
        best = max(best, float(maximum))
    return best


def detect_item_window(
    frame: Any, *,
    threshold: float = ITEM_TAB_MATCH_THRESHOLD,
    minimum_matches: int = ITEM_TAB_MINIMUM_MATCHES,
    scales: tuple[float, ...] = ITEM_TAB_TEMPLATE_SCALES,
) -> ItemDetection:
    """Return whether the item-window label gate passes for ``frame``.

    Each of 道具栏 / 装备 / 消耗 / 设置 / 其他 / 特殊 / 装饰 receives an
    independent OpenCV edge-template score.  The gate passes only after at
    least four labels clear ``threshold``.  No gameplay action is performed by
    this module; callers decide what an active item window should block.
    """

    if minimum_matches < 1:
        raise ValueError("minimum_matches must be at least one")
    if not scales or any(scale <= 0.0 for scale in scales):
        raise ValueError("scales must contain positive values")
    edges = _edge_image(frame)
    templates = _load_templates()
    scores = {
        key: _best_template_score(edges, template, scales)
        for key, template in templates.items()
    }
    matched = tuple(
        ITEM_TAB_LABELS[key] for key in ITEM_TAB_LABELS
        if scores[key] >= threshold
    )
    return ItemDetection(
        active=len(matched) >= minimum_matches,
        matched_labels=matched,
        scores=scores,
    )


def item_tab_gate_passes(frame: Any) -> bool:
    """Small convenience entry point for a caller that only needs the gate."""

    return detect_item_window(frame).active


__all__ = [
    "ITEM_TAB_LABELS",
    "ITEM_TAB_MATCH_THRESHOLD",
    "ITEM_TAB_MINIMUM_MATCHES",
    "ItemDetection",
    "detect_item_window",
    "item_tab_gate_passes",
]
