"""Persist and compare visual minimap-title signatures by configured map name."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
from typing import Optional

import cv2
import numpy as np
from PIL import Image


def _write_image_unicode_safe(path: Path, image: np.ndarray) -> None:
    """Save an image through Python's Unicode-safe file I/O.

    ``cv2.imwrite`` uses the C file API, which fails silently (returns False)
    on Windows when the path contains non-ASCII characters (for example an
    installation folder with Chinese characters like ``D:\u86c7\u592bG``).
    Encoding with ``cv2.imencode`` and writing the bytes with
    ``Path.write_bytes`` keeps the path in Python's Unicode-aware layer.
    """

    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise OSError(f"could not encode image for {path}")
    path.write_bytes(encoded.tobytes())


def _read_image_unicode_safe(path: Path, flags: int) -> Optional[np.ndarray]:
    """Load an image through Python's Unicode-safe file I/O (see above)."""

    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


class MapIdentityStore:
    """Map a human-readable map name to a normalized title-image signature."""

    def __init__(self, root: Path, *, match_threshold: float = 0.72) -> None:
        self.root = Path(root)
        self.match_threshold = float(match_threshold)
        self.index_path = self.root / "map-name-index.json"
        self._lock = threading.RLock()

    @staticmethod
    def _filename(map_name: str) -> str:
        digest = hashlib.sha256(map_name.strip().encode("utf-8")).hexdigest()[:20]
        return f"map-name-{digest}.png"

    @staticmethod
    def _normalize(image: Image.Image) -> np.ndarray:
        rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        gray = cv2.resize(gray, (256, 64), interpolation=cv2.INTER_AREA)
        gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 4)).apply(gray)
        edges = cv2.Canny(gray, 35, 120)
        return edges if np.count_nonzero(edges) >= 20 else gray

    def signature_path(self, map_name: str) -> Path:
        return self.root / self._filename(map_name)

    def has_reference(self, map_name: str) -> bool:
        return bool(map_name.strip()) and self.signature_path(map_name).is_file()

    def record(self, map_name: str, image: Image.Image) -> Path:
        name = map_name.strip()
        if not name:
            raise ValueError("map name is empty")
        signature = self._normalize(image)
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.signature_path(name)
            _write_image_unicode_safe(path, signature)
            index = {}
            if self.index_path.is_file():
                try:
                    index = json.loads(self.index_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    index = {}
            index[name] = path.name
            temporary = self.index_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(index, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.index_path)
            return path

    def similarity(self, map_name: str, image: Image.Image) -> float:
        path = self.signature_path(map_name)
        reference = _read_image_unicode_safe(path, cv2.IMREAD_GRAYSCALE)
        if reference is None:
            return 0.0
        current = self._normalize(image)
        if current.shape != reference.shape:
            reference = cv2.resize(reference, (current.shape[1], current.shape[0]))
        score = cv2.matchTemplate(
            reference.astype(np.float32),
            current.astype(np.float32),
            cv2.TM_CCOEFF_NORMED,
        )[0, 0]
        return max(0.0, min(1.0, float(score))) if np.isfinite(score) else 0.0

    def matches(self, map_name: str, image: Image.Image) -> tuple[bool, float]:
        score = self.similarity(map_name, image)
        return score >= self.match_threshold, score

    def clear(self) -> None:
        with self._lock:
            if not self.root.is_dir():
                return
            for path in self.root.glob("map-name-*.png"):
                path.unlink(missing_ok=True)
            self.index_path.unlink(missing_ok=True)

    def remove(self, map_name: str) -> None:
        """Remove only one map identity, preserving profiles for other maps."""

        name = map_name.strip()
        if not name:
            return
        with self._lock:
            self.signature_path(name).unlink(missing_ok=True)
            if not self.index_path.is_file():
                return
            try:
                index = json.loads(self.index_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return
            index.pop(name, None)
            temporary = self.index_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(index, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.index_path)


__all__ = ["MapIdentityStore"]
