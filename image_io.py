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
import shutil
import tempfile
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


def read_image(
    path: Path, flags: int = cv2.IMREAD_COLOR,
) -> Optional[np.ndarray]:
    """Read an image file through Python's Unicode-aware filesystem API.

    ``cv2.imread`` uses the C file API, which cannot open a path containing
    non-ASCII characters on Windows: it returns ``None`` without raising.  An
    install or extraction folder like ``D:\\辅助\\`` (and the test fixture
    ``D:\\蛇夫G\\``) therefore made every bundled reference image "missing" -
    the reconnector then logged ``no page signature is available`` and clicked
    preset coordinates blind, because ``login_page_target.jpg`` happened to be
    read by a hand-written Unicode-safe branch while the five ``page_*_target``
    crops were not.  This helper is the one read path every reference uses.
    """

    target = Path(path)
    try:
        encoded = np.fromfile(str(target), dtype=np.uint8)
    except (OSError, ValueError):
        LOG.warning("could not read image bytes: %s", target, exc_info=True)
        return None
    if not encoded.size:
        return None
    try:
        image = cv2.imdecode(encoded, flags)
    except cv2.error:
        LOG.warning("could not decode image: %s", target, exc_info=True)
        return None
    return image if image is not None and image.size else None


def _path_is_ascii(path: Path) -> bool:
    try:
        str(Path(path).resolve()).encode("ascii")
    except (UnicodeEncodeError, OSError):
        return False
    return True


class VideoCapture:
    """``cv2.VideoCapture`` that also opens a video under a non-ASCII folder.

    OpenCV's capture opens the path through the C file API, so a Chinese install
    folder (``D:\\辅助\\screenshots\\...mp4``) fails with ``isOpened() == False``.
    A non-ASCII source is copied to a temporary ASCII path first; the copy is
    removed when the capture is released.
    """

    def __init__(self, source: Any) -> None:
        self._path = Path(source)
        self._temp_dir: Optional[tempfile.TemporaryDirectory] = None
        open_path = self._path
        if not _path_is_ascii(self._path):
            self._temp_dir = tempfile.TemporaryDirectory(prefix="me-video-")
            copy = Path(self._temp_dir.name) / ("source" + self._path.suffix)
            shutil.copyfile(self._path, copy)
            open_path = copy
            LOG.info("video capture: copied %s to an ASCII path for OpenCV",
                     self._path.name)
        self._capture = cv2.VideoCapture(str(open_path))

    # Delegate the small surface the project uses.
    def isOpened(self) -> bool:  # noqa: N802 - OpenCV spelling
        return bool(self._capture.isOpened())

    def read(self):
        return self._capture.read()

    def get(self, prop: int) -> float:
        return float(self._capture.get(prop))

    def release(self) -> None:
        try:
            self._capture.release()
        finally:
            if self._temp_dir is not None:
                self._temp_dir.cleanup()
                self._temp_dir = None

    def __enter__(self) -> "VideoCapture":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


class VideoWriter:
    """``cv2.VideoWriter`` that also writes into a non-ASCII folder.

    The writer is created on a temporary ASCII path and moved onto ``target``
    when it is released, so the mp4 lands in ``screenshots\\…`` even when the
    install folder is Chinese.
    """

    def __init__(
        self, target: Any, fourcc: str, fps: float, size: tuple[int, int],
    ) -> None:
        self._target = Path(target)
        self._temp_dir: Optional[tempfile.TemporaryDirectory] = None
        self._target.parent.mkdir(parents=True, exist_ok=True)
        write_path = self._target
        if not _path_is_ascii(self._target):
            self._temp_dir = tempfile.TemporaryDirectory(prefix="me-video-")
            write_path = Path(self._temp_dir.name) / ("output" + self._target.suffix)
        self._writer = cv2.VideoWriter(
            str(write_path), cv2.VideoWriter_fourcc(*fourcc), fps, size,
        )

    def isOpened(self) -> bool:  # noqa: N802 - OpenCV spelling
        return bool(self._writer.isOpened())

    def write(self, frame: Any) -> None:
        self._writer.write(frame)

    def release(self) -> None:
        self._writer.release()
        if self._temp_dir is None:
            return
        try:
            produced = next(iter(Path(self._temp_dir.name).glob("output.*")), None)
            if produced is not None and produced.is_file():
                shutil.move(str(produced), str(self._target))
        finally:
            self._temp_dir.cleanup()
            self._temp_dir = None

    def __enter__(self) -> "VideoWriter":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


def to_bgr(image: Any) -> Optional[np.ndarray]:
    """Any supported image -> BGR numpy array (None when the type is unknown).

    A PIL image carries **RGB** pixels while cv2 speaks BGR, so a PIL frame must be
    reordered here.  It used to be handed to the ndarray branch unchanged, which
    wrote every screenshot and every recorded lie frame with red and blue swapped
    (field report: "the screenshot shifted the game color").  A numpy array is
    already BGR by convention and is passed through untouched.
    """

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
    if not isinstance(array, np.ndarray):
        return None
    if getattr(image, "mode", None) is None:
        # Not a PIL image: keep the old "arrays are BGR" contract.
        return to_bgr(array)
    if array.ndim == 2:
        return cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
    if array.ndim == 3 and array.shape[2] == 4:
        return cv2.cvtColor(array, cv2.COLOR_RGBA2BGR)
    if array.ndim == 3 and array.shape[2] == 3:
        return cv2.cvtColor(array, cv2.COLOR_RGB2BGR)
    return None


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
    "VideoCapture",
    "VideoWriter",
    "frame_files",
    "matches_frame_name",
    "read_image",
    "save_frame",
    "save_reference",
    "save_screenshot",
    "screenshot_name",
    "to_bgr",
]
