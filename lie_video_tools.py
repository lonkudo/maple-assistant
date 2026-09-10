# -*- coding: utf-8 -*-
"""Lie-event video tooling: screenshots -> video, and an in-game replay test.

Two standalone utilities, both decoupled from the assistant exactly like
``lie_screenshot_recorder.py``: no UI, no workers, no capture, no game input.
The only shared dependency is the lie-pass engine itself, imported lazily
inside the replay function.

1. ``compose_frames_to_video`` turns a LieScreenshotRecorder run folder
   (``frame_000001.png`` ...) into an mp4 written next to the frames.
2. ``simulate_in_game_run`` replays that recording through the REAL lie-pass
   engine (``auto_lie_worker``) the way the live game would - same bell, same
   attention window, same Cutie tracker, same aim mapping - but with the mouse
   controller replaced by a recorder, so nothing moves on screen.  It writes
   BOTH outputs: an annotated mp4 and a per-frame text log.

Each screenshot set keeps exactly ONE video: the composed ``<run>.mp4`` written
by the recorder next to its frames.  The replay/test artefacts (annotated video
+ text log) are scratch output and default to ``work/lie_video_tests/`` so a
recording folder is never littered with extra mp4s.

CLI::

    .venv\\Scripts\\python.exe lie_video_tools.py <folder-or-video> [--fps 10]
        [--device cpu|cuda] [--fast] [--no-bell-clear] [--no-inset]

``--fast`` replays without the real-time pacing (frames are fed as fast as
they process); the default paces frames at ``--fps`` so the engine's timing
windows behave as they do in the game.  In both modes the engine's clock is
aligned to the recording timeline before every step, so the takeover delay,
the staleness checks and the 45 s cap are evaluated exactly as in a live run.
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import cv2
import numpy as np
from PIL import Image

LOG = logging.getLogger("lie-video-tools")

FRAME_GLOB = "frame_*.png"
DEFAULT_FPS = 10.0
# OpenCV's own H.264 encoder needs openh264 which most installs lack, so the
# real encoder is ffmpeg when it is on the machine; otherwise mp4v (playable
# by OpenCV/ffmpeg-based players, but not by every desktop player).
VIDEO_CODECS = ("avc1", "mp4v")
FFMPEG_ENV = "LIE_VIDEO_FFMPEG"
# Scratch tree for replay/test artefacts (relative to this module's folder).
DEFAULT_TEST_SUBDIR = Path("work") / "lie_video_tests"
# The detector scans the #c9ced0 bell once a second in the live assistant.
DETECTOR_SCAN_SECONDS = 1.0


# --------------------------------------------------------------- frame input


def load_frames(
    source: Any, pattern: str = FRAME_GLOB
) -> tuple[list[Image.Image], list[Path]]:
    """Load a recording as ``(RGB images, source paths)``.

    ``source`` may be a folder of ``frame_*.png`` screenshots or a video file.
    """

    path = Path(source)
    if path.is_dir():
        files = sorted(path.glob(pattern))
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


def default_test_paths(source: Any) -> tuple[Path, Path]:
    """Return ``(video, text)`` for a replay test of ``source`` (scratch tree)."""

    path = Path(source)
    stem = path.name if path.is_dir() else path.stem
    root = Path(__file__).resolve().parent / DEFAULT_TEST_SUBDIR
    return root / f"{stem}_tracked.mp4", root / f"{stem}_tracked.txt"


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


# --------------------------------------------------------------- replay tools


class RecordingAim:
    """Stand-in for ``mouse_aim_controller.MouseAimController``.

    Accepts the exact calls the engine makes, records them, and never touches
    the real cursor.  ``map_to_screen`` mirrors the production mapping so the
    log can show where the mouse *would* have gone.
    """

    def __init__(self, video_width: int, video_height: int) -> None:
        self.video_width = int(video_width)
        self.video_height = int(video_height)
        self.region: Optional[tuple[int, int, int, int]] = None
        self.last: Optional[tuple[float, float, float, str]] = None
        self.samples: list[tuple[float, float, float, str]] = []
        self.enabled = True
        self.closed = False

    def set_region(self, left: int, top: int, right: int, bottom: int) -> None:
        if right <= left or bottom <= top:
            return
        self.region = (int(left), int(top), int(right), int(bottom))

    def clear_region(self) -> None:
        self.region = None

    def push_target(
        self, x: float, y: float, confidence: float = 1.0, state: str = ""
    ) -> None:
        self.last = (float(x), float(y), float(confidence), str(state))
        self.samples.append(self.last)

    def map_to_screen(self, x: float, y: float) -> Optional[tuple[float, float]]:
        if self.region is None:
            return None
        left, top, right, bottom = self.region
        return (
            left + (float(x) / self.video_width) * (right - left),
            top + (float(y) / self.video_height) * (bottom - top),
        )

    def set_enabled(self, enabled: bool, *, silent: bool = False) -> None:
        self.enabled = bool(enabled)

    def close(self) -> None:
        self.closed = True


def _bell_match(image: Image.Image) -> Optional[tuple[int, int, int, int]]:
    """Production #c9ced0 bell rule, imported lazily."""

    from lie_detector_worker import detect_lie_square

    try:
        return detect_lie_square(image)
    except Exception:
        return None


def _annotate_frame(
    image_bgr: np.ndarray,
    *,
    window_box: Optional[tuple[int, int, int, int]],
    candidate_box: Optional[tuple[int, int, int, int]],
    seed_box: Optional[tuple[int, int, int, int]],
    aim_point: Optional[tuple[float, float]],
    header: str,
    inset: bool,
) -> np.ndarray:
    """Draw the pipeline state on one frame (all boxes in client pixels)."""

    out = image_bgr.copy()
    if window_box is not None:
        left, top, width, height = window_box
        cv2.rectangle(out, (left, top), (left + width - 1, top + height - 1),
                      (0, 140, 255), 3)
        cv2.putText(out, f"lie window {width}x{height} (tracker crop)",
                    (max(2, left), max(20, top - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 140, 255), 2, cv2.LINE_AA)
    if candidate_box is not None:
        x, y, box_w, box_h = candidate_box
        cv2.rectangle(out, (x, y), (x + box_w - 1, y + box_h - 1),
                      (0, 255, 255), 2)
    if seed_box is not None:
        x, y, box_w, box_h = seed_box
        cv2.rectangle(out, (x, y), (x + box_w - 1, y + box_h - 1),
                      (0, 255, 0), 3)
        cv2.putText(out, f"seed {box_w}x{box_h}",
                    (x, max(20, y - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 255, 0), 2, cv2.LINE_AA)
    if aim_point is not None:
        px, py = int(round(aim_point[0])), int(round(aim_point[1]))
        cv2.drawMarker(out, (px, py), (255, 0, 0), cv2.MARKER_CROSS, 26, 3)
        cv2.circle(out, (px, py), 14, (255, 0, 0), 2)
    cv2.rectangle(out, (0, 0), (out.shape[1], 28), (0, 0, 0), -1)
    cv2.putText(out, header, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                (255, 255, 255), 2, cv2.LINE_AA)
    if inset and window_box is not None:
        left, top, width, height = window_box
        crop = image_bgr[top:top + height, left:left + width]
        if crop.size:
            inset_width = max(120, out.shape[1] // 4)
            scale = inset_width / float(crop.shape[1])
            inset_image = cv2.resize(
                crop, (inset_width, max(1, int(round(crop.shape[0] * scale))))
            )
            ih, iw = inset_image.shape[:2]
            oy = out.shape[0] - ih - 8
            out[oy:oy + ih, 8:8 + iw] = inset_image
            cv2.rectangle(out, (8, oy), (8 + iw - 1, oy + ih - 1),
                          (0, 140, 255), 2)
    return out


def simulate_in_game_run(
    source: Any,
    *,
    output_video: Any = None,
    output_text: Any = None,
    fps: float = DEFAULT_FPS,
    device: Optional[str] = None,
    realtime: bool = True,
    bell_clear: bool = True,
    inset: bool = True,
    pattern: str = FRAME_GLOB,
) -> dict:
    """Replay a recorded lie event through the real engine.

    Returns a summary dict (bell frame, seed time/box, end reason, tracking
    statistics).  Writes ``output_video`` (annotated mp4) and ``output_text``
    (per-frame log); both default into the scratch tree
    (:func:`default_test_paths`) so the recording folder only ever holds the
    one composed video.
    """

    # Lazy, decoupled imports: the tool stays standalone without the engine.
    from auto_lie_worker import (
        AutoLieWorker,
        _LieSequenceEngine,
        _TARGET_TRACKER_DIR,
    )

    # The engine puts the bundled tracker on sys.path in its own __init__;
    # the replay needs it one step earlier to swap the mouse controller.
    for path in (str(_TARGET_TRACKER_DIR), str(_TARGET_TRACKER_DIR / "src")):
        if path not in sys.path:
            sys.path.insert(0, path)
    import mouse_aim_controller

    images, sources = load_frames(source, pattern)
    folder = Path(source)
    default_video, default_text = default_test_paths(source)
    video_path = Path(output_video) if output_video is not None else default_video
    text_path = Path(output_text) if output_text is not None else default_text
    video_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.parent.mkdir(parents=True, exist_ok=True)

    width, height = images[0].size
    interval = 1.0 / float(fps) if fps > 0 else 0.0
    frames = [
        SimpleNamespace(image=image, sequence=index,
                        window_rect=(0, 0, width, height))
        for index, image in enumerate(images, start=1)
    ]

    # The mouse must not move during a replay: swap the real controller for
    # the recorder before the engine imports it.
    aim_sink: dict[str, RecordingAim] = {}

    def _recording_aim(video_width, video_height, *args, **kwargs):
        sink = RecordingAim(video_width, video_height)
        aim_sink["aim"] = sink
        return sink

    real_aim = mouse_aim_controller.MouseAimController
    real_suspend = getattr(mouse_aim_controller, "suspend_others_except", None)
    mouse_aim_controller.MouseAimController = _recording_aim
    mouse_aim_controller.suspend_others_except = lambda keep: []

    try:
        writer = VideoSink(video_path, fps, (width, height))
    except Exception:
        mouse_aim_controller.MouseAimController = real_aim
        raise

    rows: list[str] = []
    summary: dict[str, Any] = {
        "source": str(source),
        "frames": len(frames),
        "fps": float(fps),
        "video": str(video_path),
        "text": str(text_path),
        "bell_frame": None,
        "bell_match": None,
        "seed_frame": None,
        "seed_time": None,
        "seed_crop_box": None,
        "seed_client_box": None,
        "end_frame": None,
        "end_time": None,
        "end_reason": None,
        "tracked_frames": 0,
        "mean_confidence": None,
    }
    worker: Any = None
    engine: Any = None
    seeded: dict[str, Any] = {}
    started = time.monotonic()
    status = "no bell detected"

    try:
        # Locate the bell exactly like the live detector would (1 s cadence,
        # sample every frame here so a short slice is never missed).
        bell_index = None
        for index, frame in enumerate(frames):
            if _bell_match(frame.image) is not None:
                bell_index = index
                break
        if bell_index is not None:
            summary["bell_frame"] = frames[bell_index].sequence
            summary["bell_match"] = list(_bell_match(frames[bell_index].image))
            status = "replayed"
            worker = AutoLieWorker(queue.Queue(), threading.Event(), enabled=True)
            with worker._lock:
                worker._active = True
            engine = _LieSequenceEngine(
                worker, tuple(summary["bell_match"]), frames[bell_index]
            )
            original_start = engine._start_tracking

            def _spy_start(frame, bgr, box, _original=original_start):
                left, top = engine._window_box[0], engine._window_box[1]
                seeded["crop_box"] = tuple(box)
                seeded["client_box"] = (
                    int(box[0]) + left, int(box[1]) + top, int(box[2]), int(box[3]),
                )
                return _original(frame, bgr, box)

            engine._start_tracking = _spy_start
            pending = frames[bell_index + 1:]
            bell_sequence = frames[bell_index].sequence
            next_scan = DETECTOR_SCAN_SECONDS
            last_timeline = 0.0
            for frame in pending:
                slot = time.monotonic()
                if engine._ended:
                    break
                worker.frames.put(frame)
                # Replay timeline (recording time, independent of pacing) and
                # align the engine's clock with it so its timing windows match
                # a live run even when replaying as fast as possible.
                header_time = (frame.sequence - bell_sequence) * interval
                last_timeline = header_time
                engine._started_at = time.monotonic() - header_time
                engine.step_once()
                aim = aim_sink.get("aim")
                # Emulate the live detector's 1 s scan of the #c9ced0 bell:
                # the real worker forwards nothing to the engine while the
                # slice is visible and a (None, frame) clear once it is gone.
                if bell_clear and header_time >= next_scan:
                    next_scan += DETECTOR_SCAN_SECONDS
                    if _bell_match(frame.image) is None:
                        worker.on_lie_seen(None, frame)
                if engine._phase == "tracking" and seeded.get("client_box"):
                    if summary["seed_frame"] is None:
                        summary["seed_frame"] = frame.sequence
                        summary["seed_time"] = header_time
                        summary["seed_crop_box"] = list(seeded["crop_box"])
                        summary["seed_client_box"] = list(seeded["client_box"])
                aim_last = aim.last if aim is not None else None
                if aim_last is not None:
                    summary["tracked_frames"] += 1
                window = engine._window_box
                candidate = engine._candidate_box
                candidate_client = None if candidate is None else (
                    candidate[0] + window[0], candidate[1] + window[1],
                    candidate[2], candidate[3],
                )
                # The tracker point is in crop pixels; the aim mapping turns
                # it into the screen point the real cursor would get.
                aim_crop = None if aim_last is None else (aim_last[0], aim_last[1])
                aim_client = None if aim_crop is None else (
                    aim_crop[0] + window[0], aim_crop[1] + window[1]
                )
                aim_screen = None
                if aim is not None and aim_crop is not None and aim.region:
                    aim_screen = tuple(
                        round(value, 1) for value in aim.map_to_screen(*aim_crop)
                    )
                rows.append(
                    "t=+%.3fs seq=%06d phase=%-12s window=%s target=%s "
                    "track=%s conf=%s state=%s aim_screen=%s region=%s" % (
                        header_time, frame.sequence, engine._phase, window,
                        candidate_client,
                        None if aim_crop is None
                        else (round(aim_crop[0], 1), round(aim_crop[1], 1)),
                        "-" if aim_last is None else "%.3f" % aim_last[2],
                        "-" if aim_last is None else aim_last[3],
                        aim_screen,
                        None if aim is None else aim.region,
                    )
                )
                annotated = _annotate_frame(
                    cv2.cvtColor(np.asarray(frame.image), cv2.COLOR_RGB2BGR),
                    window_box=window,
                    candidate_box=candidate_client,
                    seed_box=seeded.get("client_box"),
                    aim_point=aim_client,
                    header=(
                        "t=+%.1fs seq=%d phase=%s conf=%s" % (
                            header_time, frame.sequence, engine._phase,
                            "-" if aim_last is None else "%.2f" % aim_last[2],
                        )
                    ),
                    inset=inset,
                )
                writer.write(annotated)
                if realtime and interval:
                    delay = interval - (time.monotonic() - slot)
                    if delay > 0:
                        time.sleep(delay)
            summary["end_frame"] = engine.__dict__.get("_last_sequence")
            summary["end_time"] = last_timeline
            summary["end_reason"] = engine._end_reason or (
                "replay finished (target gone / still tracking)"
            )
            if engine._tracker is not None and engine._phase == "tracking":
                try:
                    latest = engine._tracker.latest()
                    summary["last_state"] = str(getattr(latest, "state", "?"))
                except Exception:
                    pass
            confidences = [
                sample[2] for sample in (aim.samples if aim is not None else [])
            ]
            if confidences:
                summary["mean_confidence"] = sum(confidences) / len(confidences)
            engine.dispose()
    finally:
        writer.close()
        mouse_aim_controller.MouseAimController = real_aim
        if real_suspend is not None:
            mouse_aim_controller.suspend_others_except = real_suspend
        elif hasattr(mouse_aim_controller, "suspend_others_except"):
            del mouse_aim_controller.suspend_others_except

    header_lines = [
        "# lie_video_tools in-game replay",
        f"# source: {sources[0] if len(sources) == 1 else folder}",
        f"# frames: {len(frames)}  fps: {fps}  realtime: {realtime}  bell_clear: {bell_clear}",
        "# bell: %s frame=%s match=%s" % (
            status, summary["bell_frame"], summary["bell_match"]),
        "# phase | window/detected popup (client px) | target box (client px) |",
        "# tracker point (crop px) + confidence/state | aim mapped to screen |",
    ]
    footer = [
        "# summary",
        "#   seeded : frame=%s t=%s box(crop)=%s box(client)=%s" % (
            summary["seed_frame"],
            None if summary["seed_time"] is None else "+%.3fs" % summary["seed_time"],
            summary["seed_crop_box"], summary["seed_client_box"],
        ),
        "#   tracking samples=%d mean_confidence=%s" % (
            summary["tracked_frames"],
            None if summary["mean_confidence"] is None
            else "%.3f" % summary["mean_confidence"],
        ),
        "#   end    : %s (last seq=%s, t=%s)" % (
            summary["end_reason"], summary["end_frame"],
            None if summary["end_time"] is None else "+%.3fs" % summary["end_time"],
        ),
        "#   outputs: %s | %s" % (video_path, text_path),
    ]
    text_path.write_text(
        "\n".join(header_lines + rows + footer) + "\n", encoding="utf-8"
    )
    LOG.info("replay %s -> %s", status, summary["end_reason"])
    return summary


# --------------------------------------------------------------------- CLI


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compose lie-event screenshots into a video and replay it "
                    "through the real lie-pass engine.")
    parser.add_argument("source", help="recording folder (frame_*.png) or video")
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS,
                        help=f"frame rate of the recording (default {DEFAULT_FPS})")
    parser.add_argument("--device", choices=("cpu", "cuda"), default=None,
                        help="force the tracker device (default: auto)")
    parser.add_argument("--fast", action="store_true",
                        help="do not pace the replay in real time")
    parser.add_argument("--no-bell-clear", action="store_true",
                        help="ignore the live #c9ced0 clear callbacks")
    parser.add_argument("--no-inset", action="store_true",
                        help="do not draw the crop inset in the output video")
    parser.add_argument("--skip-compose", action="store_true",
                        help="only run the replay")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    video = None
    if not args.skip_compose:
        video = compose_frames_to_video(args.source, fps=args.fps)
        print(f"composed video: {video}")

    summary = simulate_in_game_run(
        args.source,
        fps=args.fps,
        device=args.device,
        realtime=not args.fast,
        bell_clear=not args.no_bell_clear,
        inset=not args.no_inset,
    )
    print("replay summary:")
    for key in (
        "bell_frame", "bell_match", "seed_frame", "seed_time",
        "seed_crop_box", "seed_client_box", "tracked_frames",
        "mean_confidence", "end_reason", "end_time", "video", "text",
    ):
        print(f"  {key}: {summary.get(key)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
