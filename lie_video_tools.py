# -*- coding: utf-8 -*-
"""Lie-event video tooling: turn a recorded lie event into a video.

Standalone and decoupled from the assistant exactly like ``lie_screenshot_recorder.py``: no UI, no
workers, no capture, no game input, no model.  ``compose_frames_to_video`` takes a recorder run
folder (``frame_000001.jpg`` ...) and writes one mp4 next to the frames - that is how the operator's
test clips for 测试api are produced.

The in-game replay through the local Cutie pass is gone with that pass: lie detection is now done by
the remote RoiTrack service (see api_lie_video.py).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np
from PIL import Image

from image_io import frame_files

LOG = logging.getLogger("lie-video-tools")

# Frames are written as JPG since image_io introduced the format policy; PNG recordings from
# earlier releases must keep loading, so the listing helper accepts both.
FRAME_GLOB = "frame_*"
DEFAULT_FPS = 10.0
# OpenCV's own H.264 encoder needs openh264 which most installs lack, so the
# real encoder is ffmpeg when it is on the machine; otherwise mp4v (playable
# by OpenCV/ffmpeg-based players, but not by every desktop player).
VIDEO_CODECS = ("avc1", "mp4v")
FFMPEG_ENV = "LIE_VIDEO_FFMPEG"


# --------------------------------------------------------------- frame input


def load_frames(
    source: Any, pattern: str = FRAME_GLOB
) -> tuple[list[Image.Image], list[Path]]:
    """Load a recording as ``(RGB images, source paths)``.

    ``source`` may be a folder of ``frame_*.jpg`` screenshots or a video file (the project is
    JPG only, see image_io).
    """

    path = Path(source)
    if path.is_dir():
        files = frame_files(path) if pattern == FRAME_GLOB else sorted(path.glob(pattern))
        if not files:
            raise FileNotFoundError(f"no {pattern} frames in {path}")
        return [Image.open(item).convert("RGB") for item in files], files
    if path.is_file():
        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            raise FileNotFoundError(f"cannot open video: {path}")
        images: list[Image.Image] = []
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                images.append(
                    Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                )
        finally:
            capture.release()
        if not images:
            raise FileNotFoundError(f"video has no frames: {path}")
        return images, [path]
    raise FileNotFoundError(f"recording not found: {path}")


def find_ffmpeg() -> Optional[str]:
    """Return an ffmpeg executable for real H.264 encoding, or None."""

    import os
    import shutil

    configured = os.environ.get(FFMPEG_ENV, "").strip()
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\ffmpeg\ffmpeg.exe"):
        if Path(candidate).is_file():
            return candidate
    return None


class VideoSink:
    """Write BGR frames to an mp4 as real H.264 (ffmpeg) or mp4v (OpenCV).

    Every desktop player handles H.264; OpenCV's mp4v output is a fallback for
    machines without ffmpeg.  Usage: ``sink.write(frame_bgr)`` / ``sink.close()``.
    """

    def __init__(self, path: Any, fps: float, size: tuple[int, int]) -> None:
        self.path = Path(path)
        self.fps = float(fps)
        self.size = (int(size[0]), int(size[1]))
        self.codec = ""
        self._process = None
        self._writer = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg = find_ffmpeg()
        if ffmpeg is not None:
            import subprocess

            width, height = self.size
            command = [
                ffmpeg, "-y", "-loglevel", "error",
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{width}x{height}", "-r", f"{self.fps:.4f}", "-i", "-",
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                str(self.path),
            ]
            try:
                self._process = subprocess.Popen(
                    command, stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                )
                self.codec = "h264 (ffmpeg)"
            except Exception:
                LOG.warning("ffmpeg unavailable; falling back to mp4v", exc_info=True)
                self._process = None
        if self._process is None:
            for fourcc in VIDEO_CODECS:
                writer = cv2.VideoWriter(
                    str(self.path), cv2.VideoWriter_fourcc(*fourcc),
                    self.fps, self.size,
                )
                if writer.isOpened():
                    self._writer = writer
                    self.codec = fourcc
                    break
                writer.release()
            if self._writer is None:
                raise RuntimeError(f"cannot open video writer for {self.path}")
        LOG.info("video sink %s (%s)", self.path, self.codec)

    def write(self, frame: Any) -> None:
        if self._process is not None:
            self._process.stdin.write(np.ascontiguousarray(frame).tobytes())
        elif self._writer is not None:
            self._writer.write(frame)

    def close(self) -> None:
        if self._process is not None:
            try:
                self._process.stdin.close()
            except Exception:
                pass
            stderr = b""
            try:
                stderr = self._process.stderr.read() or b""
            except Exception:
                pass
            code = self._process.wait()
            if code != 0:
                LOG.warning(
                    "ffmpeg exited %s for %s: %s",
                    code, self.path, stderr.decode("utf-8", "replace").strip(),
                )
            self._process = None
        if self._writer is not None:
            self._writer.release()
            self._writer = None


def compose_frames_to_video(
    source: Any,
    output_path: Any = None,
    *,
    fps: float = DEFAULT_FPS,
    pattern: str = FRAME_GLOB,
) -> Path:
    """Write a recording's frames into an mp4 and return its path.

    Defaults to ``<folder>/<folder-name>.mp4`` when ``output_path`` is None.
    """

    folder = Path(source)
    target = Path(output_path) if output_path is not None else (
        folder / f"{folder.name}.mp4" if folder.is_dir()
        else folder.with_suffix(".mp4")
    )
    images, _paths = load_frames(source, pattern)
    width, height = images[0].size
    sink = VideoSink(target, fps, (width, height))
    try:
        for image in images:
            if image.size != (width, height):
                image = image.resize((width, height))
            sink.write(cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR))
    finally:
        sink.close()
    LOG.info(
        "composed %d frames -> %s (%dx%d @ %.1f fps)",
        len(images), target, width, height, fps,
    )
    return target
