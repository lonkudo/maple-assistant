"""One policy for screenshots: **JPG everywhere**.

Measured on a real 1366x768 lie frame (``detect_video/lie_20260910_155856/frame_000027.png``):

    PNG default     1033.1 KB   write 28.75 ms
    PNG level1       914.2 KB   write 46.05 ms
    JPG q90          379.2 KB   write  4.55 ms      <- 2.7x smaller, 6.3x faster
    JPG q95          546.7 KB   write  5.81 ms

So the project writes JPG only: 2.7x smaller and six times faster to write, which matters on
the capture thread.  Quality is :data:`SCREENSHOT_QUALITY` (90) for dumps and
:data:`RECORDING_FRAME_QUALITY` (95) for frames that are composed into a video or later matched
against - a reference image needs the extra margin because JPEG is lossy.

PNG is no longer read either: :func:`frame_files` lists ``.jpg`` / ``.jpeg`` only.  The old PNG
assets were converted once with ``work/convert_png_to_jpg.py`` (429 MB -> 185 MB); a PNG folder
from before that conversion is simply no longer found.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional, Sequence

import cv2
import numpy as np

LOG = logging.getLogger("image-io")

# What the assistant writes.
SCREENSHOT_SUFFIX = ".jpg"
SCREENSHOT_QUALITY = 90
# Frames composed into a diagnostic video, and every reference image that is matched against.
RECORDING_FRAME_QUALITY = 95
REFERENCE_QUALITY = 95
# JPG only.
SUPPORTED_SUFFIXES: tuple[str, ...] = (".jpg", ".jpeg")


def screenshot_name(stem: str) -> str:
    """``"frame_000001"`` -> ``"frame_000001.jpg"``; an existing suffix is replaced."""

    return f"{Path(stem).stem}{SCREENSHOT_SUFFIX}"


def save_screenshot(
    target: Path,
    image: Any,
    *,
    quality: int = SCREENSHOT_QUALITY,
) -> Optional[Path]:
    """Write one screenshot as JPG -> the path, or None when it could not be written.

    ``image`` may be a numpy BGR array or a PIL image (the capture worker hands over PIL frames,
    the OpenCV paths hand over arrays).  A ``.png`` in ``target`` is replaced by ``.jpg``: this
    project is JPG only.
    """

    path = Path(target).with_suffix(SCREENSHOT_SUFFIX)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        LOG.warning("cannot create %s for a screenshot", path.parent, exc_info=True)
    array = to_bgr(image)
    if array is None:
        LOG.warning("screenshot %s not written: unsupported image type %s",
                    path, type(image).__name__)
        return None
    try:
        ok, buffer = cv2.imencode(".jpg", array, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            raise RuntimeError("imencode failed")
        # Write through Python's file layer: cv2.imwrite returns False (silently) when the
        # path holds non-ASCII characters, which an install folder like D:\蛇夫G does.
        path.write_bytes(buffer.tobytes())
    except Exception:
        LOG.warning("could not save screenshot %s", path, exc_info=True)
        return None
    return path


def save_frame(
    folder: Path,
    stem: str,
    image: Any,
    *,
    quality: int = RECORDING_FRAME_QUALITY,
) -> Optional[Path]:
    """Write ``folder/<stem>.jpg`` -> the path, or None when it failed."""

    return save_screenshot(Path(folder) / screenshot_name(stem), image, quality=quality)


def save_reference(path: Path, image: Any, *,
                   quality: int = REFERENCE_QUALITY) -> Optional[Path]:
    """Write an image that will later be MATCHED against, at the reference quality."""

    return save_screenshot(path, image, quality=quality)


def to_bgr(image: Any) -> Optional[np.ndarray]:
    """Any supported image -> BGR numpy array (None when the type is unknown)."""

    if isinstance(image, np.ndarray):
        if image.ndim == 2:
            return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        if image.ndim == 3 and image.shape[2] == 3:
            return image
        if image.ndim == 3 and image.shape[2] == 4:
            return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        return None
    try:                                    # PIL image
        array = np.asarray(image)
    except Exception:
        return None
    return to_bgr(array) if isinstance(array, np.ndarray) else None


def frame_files(folder: Path, prefixes: Sequence[str] = ("frame_", "frame-")) -> list[Path]:
    """Every JPG frame file in ``folder``, sorted.

    Replaces the ``glob("frame_*.png")`` calls of the pre-JPG releases.  PNG is not listed any
    more: the project is JPG only, and the old PNG assets were converted once with
    ``work/convert_png_to_jpg.py``.
    """

    path = Path(folder)
    if not path.is_dir():
        return []
    found = [
        item for item in path.iterdir()
        if item.is_file()
        and item.suffix.lower() in SUPPORTED_SUFFIXES
        and any(item.name.startswith(prefix) for prefix in prefixes)
    ]
    return sorted(found)


def matches_frame_name(name: str, prefixes: Sequence[str] = ("frame_", "frame-")) -> bool:
    """Whether ``name`` looks like a JPG frame file."""

    lowered = name.lower()
    return (any(lowered.startswith(prefix) for prefix in prefixes)
            and Path(lowered).suffix in SUPPORTED_SUFFIXES)


__all__ = [
    "RECORDING_FRAME_QUALITY",
    "REFERENCE_QUALITY",
    "SCREENSHOT_QUALITY",
    "SCREENSHOT_SUFFIX",
    "SUPPORTED_SUFFIXES",
    "frame_files",
    "matches_frame_name",
    "save_frame",
    "save_reference",
    "save_screenshot",
    "screenshot_name",
    "to_bgr",
]
