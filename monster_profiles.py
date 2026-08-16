"""Monster reference profiles: save uploaded monster images with auto-tuned HSV.

``MonsterProfileStore`` persists each uploaded monster picture under
``recording-assets/monsters/`` as a PNG plus a sidecar JSON holding the HSV
color bounds derived from the image.  The UI dropdown lists the saved names,
and the same HSV bounds are applied to the live :class:`MonsterDetector`.

``derive_hsv_bounds`` samples the saturated pixels of an image and returns a
robust HSV band (hue handled circularly, so red monsters straddling 0/180 are
still covered).  It is a pure function so tests can exercise it without Tk.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image

LOG = logging.getLogger(__name__)

HsvBounds = Tuple[Tuple[int, int, int], Tuple[int, int, int]]

_PROFILE_SUFFIX = ".png"
_META_SUFFIX = ".json"
_UNSAFE_NAME = re.compile(r"[^\w\-. ]+")
_SAFE_NAME = re.compile(r"\s+")


def _sanitize_name(name: str) -> str:
    cleaned = _UNSAFE_NAME.sub("", name).strip()
    cleaned = _SAFE_NAME.sub("-", cleaned)
    return cleaned[:60] or "monster"


def derive_hsv_bounds(image: Image.Image) -> HsvBounds:
    """Return ``(hsv_lower, hsv_upper)`` tuned to the image's dominant color.

    The band is built around the *peak hue cluster* of strongly saturated
    pixels (S >= 120), not global percentiles.  A tight crop of a monster
    therefore yields a narrow, precise band; a picture dominated by
    background produces a band around that background instead, which is
    exactly the signal the user should see.  Hue is handled circularly so red
    monsters straddling 0/180 are covered.
    """

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    if rgb.size == 0:
        return (0, 80, 80), (179, 255, 255)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue = hsv[..., 0].astype(np.int16)
    sat = hsv[..., 1].astype(np.int16)
    val = hsv[..., 2].astype(np.int16)

    strong = (sat >= 120) & (val >= 60)
    if int(strong.sum()) < max(30, rgb.size // 400):
        strong = (sat >= 60) & (val >= 40)
    if not strong.any():
        return (0, 80, 80), (179, 255, 255)

    hues = hue[strong]
    sats = sat[strong]
    vals = val[strong]

    # Hue histogram (0..179) with circular smoothing: the peak bin plus its
    # neighbours finds the dominant color cluster even across the 0/180 seam.
    hist = np.bincount(hues, minlength=180).astype(np.float64)
    smooth = (
        np.roll(hist, 1) + hist + np.roll(hist, -1)
    )
    peak = int(np.argmax(smooth))
    peak_count = int(hist[peak])

    # Half-width grows with how spread the cluster is, capped so a single
    # dominant color stays a tight band.
    spread = int(np.percentile(np.abs((hues - peak + 90) % 180 - 90), 90))
    half_width = int(np.clip(max(6, spread // 2), 6, 30))

    lo_hue = (peak - half_width) % 180
    hi_hue = (peak + half_width) % 180

    def to_hue(deg: int) -> int:
        return int(deg) % 180

    s_low = int(np.clip(np.percentile(sats, 10), 40, 255))
    s_high = int(np.clip(np.percentile(sats, 90), s_low, 255))
    v_low = int(np.clip(np.percentile(vals, 10), 40, 255))
    v_high = int(np.clip(np.percentile(vals, 90), v_low, 255))

    lower = (to_hue(lo_hue), s_low, v_low)
    upper = (to_hue(hi_hue), s_high, v_high)
    # A wrapped band (lower hue > upper hue) is valid input for MonsterDetector.
    return lower, upper


class MonsterProfileStore:
    """Persist monster reference images and their derived HSV bounds."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def names(self) -> list[str]:
        """Return sorted profile names, excluding broken entries."""

        if not self.root.is_dir():
            return []
        names = []
        for image_path in sorted(self.root.glob(f"*{_PROFILE_SUFFIX}")):
            if (image_path.with_suffix(_META_SUFFIX)).is_file():
                names.append(image_path.name[: -len(_PROFILE_SUFFIX)])
        return names

    def image_path(self, name: str) -> Path:
        return self.root / f"{_sanitize_name(name)}{_PROFILE_SUFFIX}"

    def meta_path(self, name: str) -> Path:
        return self.root / f"{_sanitize_name(name)}{_META_SUFFIX}"

    def hsv_bounds(self, name: str) -> Optional[HsvBounds]:
        path = self.meta_path(name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            lower = tuple(int(value) for value in data["hsv_lower"])
            upper = tuple(int(value) for value in data["hsv_upper"])
            if len(lower) == 3 and len(upper) == 3:
                return (lower, upper)
        except (OSError, ValueError, KeyError, TypeError):
            LOG.warning("ignored unreadable monster profile %s", path)
        return None

    def save(self, name: str, image: Image.Image) -> tuple[str, HsvBounds]:
        """Save the image and its derived HSV bounds; returns (name, bounds)."""

        self._ensure()
        bounds = derive_hsv_bounds(image)
        safe_name = _sanitize_name(name)
        image_path = self.image_path(safe_name)
        meta_path = self.meta_path(safe_name)
        image.convert("RGB").save(image_path, format="PNG")
        meta = {
            "name": safe_name,
            "hsv_lower": list(bounds[0]),
            "hsv_upper": list(bounds[1]),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        meta_path.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        LOG.info("saved monster profile %s bounds=%s..%s",
                 safe_name, bounds[0], bounds[1])
        return safe_name, bounds

    def load(self, name: str) -> Optional[tuple[Image.Image, HsvBounds]]:
        """Return ``(image, hsv_bounds)`` for a saved profile, or None."""

        bounds = self.hsv_bounds(name)
        if bounds is None:
            return None
        image_path = self.image_path(name)
        try:
            image = Image.open(image_path).convert("RGB")
        except OSError:
            LOG.warning("could not open monster image %s", image_path)
            return None
        return image, bounds

    def delete(self, name: str) -> bool:
        """Remove a profile; returns True when anything was deleted."""

        removed = False
        for path in (self.image_path(name), self.meta_path(name)):
            try:
                if path.is_file():
                    path.unlink()
                    removed = True
            except OSError:
                LOG.warning("could not remove monster profile file %s", path,
                            exc_info=True)
        return removed


__all__: Sequence[str] = (
    "HsvBounds",
    "MonsterProfileStore",
    "derive_hsv_bounds",
)
