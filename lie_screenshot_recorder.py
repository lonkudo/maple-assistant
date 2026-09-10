# -*- coding: utf-8 -*-
"""Lie-event screenshot recorder (decoupled diagnostic module).

Purpose
-------
Records the WHOLE game client for 30 seconds every time one of the
assistant's alert events fires - currently the lie detection (测谎), the
patrol disconnect (掉线) and the circular countdown (循环).  The captured
frames are used to confirm real in-game geometry (detection window size,
white target appearance, what the game looked like at the moment).

Decoupling
----------
- Owns nothing: no UI, no workers, no game input.  It only listens to
  event triggers and, while recording, repeatedly captures the game window.
- It does NOT import auto_lie_worker, lie_detector_worker, ui_worker,
  countdown_worker or character_worker.  The only shared dependency is
  ``capture_worker.capture_window`` (the same capture primitive the
  assistant uses), imported lazily.
- Wiring is done once in assistant.py::

      recorder = LieScreenshotRecorder(window_title)
      lie_detector_worker.add_lie_seen_callback(recorder.on_lie_seen)
      countdown_worker = CountdownWorker(..., event_callback=recorder.on_countdown)
      character_worker = CharacterWorker(..., disconnect_event_callback=recorder.on_disconnect)

Output
------
    screenshots/<kind>_YYYYmmdd_HHMMSS/frame_%06d.png   (kind: lie|offline|countdown)
    screenshots/<kind>_YYYYmmdd_HHMMSS/<run>.mp4       (composed when it ends)

When a run finishes, its frames are turned into an mp4 by
``lie_video_tools.compose_frames_to_video`` (imported lazily, so this module
stays decoupled).  The png frames are deleted afterwards - only once the
video is written and verified - unless ``keep_frames`` is set; a compose
problem always keeps the frames.

The recorder is single-flight: a new event while one recording is running
is ignored.  Recording always stops after ``duration_seconds`` and the flag
is released even when the capture loop crashes.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger(__name__)

# Root output folder next to this file (writable in a normal install).
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "screenshots"
# How long one recording session lasts after the lie event.
RECORD_SECONDS = 30.0
# Small sleep between captures (capture itself takes ~10-30ms, so this lands
# near the capture speed - a dense per-frame timeline of the event).
MIN_INTERVAL_SECONDS = 0.02
# Hard safety cap so a pathological loop cannot fill the disk.
MAX_FRAMES = 1500
ENABLED = True
# Turn the finished run into an mp4 next to the frames.
COMPOSE_VIDEO = True
# Delete the png frames once that video has been written and verified.
KEEP_FRAMES = False


class LieScreenshotRecorder:
    """Record the full game window for a while after each lie event."""

    def __init__(
        self,
        window_title: str,
        *,
        output_dir: Any = None,
        duration_seconds: float = RECORD_SECONDS,
        min_interval: float = MIN_INTERVAL_SECONDS,
        max_frames: int = MAX_FRAMES,
        enabled: bool = ENABLED,
        compose_video: bool = COMPOSE_VIDEO,
        keep_frames: bool = KEEP_FRAMES,
        capture_fn: Any = None,
    ) -> None:
        if not window_title.strip():
            raise ValueError("window_title must not be empty")
        self.window_title = window_title
        self.output_dir = Path(
            output_dir if output_dir is not None else DEFAULT_OUTPUT_DIR
        )
        self.duration_seconds = float(duration_seconds)
        self.min_interval = float(min_interval)
        self.max_frames = int(max_frames)
        self.enabled = bool(enabled)
        self.compose_video = bool(compose_video)
        self.keep_frames = bool(keep_frames)
        # Injectable capture callable -> (PIL image, rect); defaults to the
        # assistant's capture_window.  Tests inject a fake so no live game
        # window is needed.
        self._capture_fn = capture_fn
        self._lock = threading.Lock()
        self._recording = False

    # ------------------------------------------------------------- trigger

    def on_lie_seen(
        self,
        match: Optional[tuple[int, int, int, int]],
        frame: Any = None,
    ) -> None:
        """LieDetectorWorker listener: start recording on a NEW event.

        ``match=None`` (square cleared) is ignored - recording runs a fixed
        duration from the first sighting regardless of the clear signal.
        """

        if match is not None:
            self._start("lie")

    def on_disconnect(self) -> None:
        """CharacterWorker listener: 掉线 patrol-disconnect alert fired."""

        self._start("offline")

    def on_countdown(self) -> None:
        """CountdownWorker listener: 循环 circular alert reached zero."""

        self._start("countdown")

    def _start(self, kind: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._recording:
                LOG.info(
                    "lie-shot: %s event while recording active; ignored",
                    kind,
                )
                return
            self._recording = True
        LOG.info(
            "lie-shot: %s event -> recording full window for %.1fs",
            kind,
            self.duration_seconds,
        )
        thread = threading.Thread(
            target=self._record_loop,
            name="lie-screenshot-recorder",
            daemon=True,
            kwargs={"kind": kind},
        )
        thread.start()

    # ------------------------------------------------------------- capture

    def _record_loop(self, kind: str = "lie") -> None:
        started = time.monotonic()
        deadline = started + self.duration_seconds
        run_dir: Optional[Path] = None
        saved = 0
        try:
            try:
                run_dir = self._prepare_run_dir(kind)
            except OSError as exc:
                LOG.error("lie-shot: cannot create output folder: %s", exc)
                return
            if self._capture_fn is not None:
                capture = self._capture_fn
            else:
                # Imported lazily so this module stays light and decoupled;
                # the helper is the exact one CaptureWorker uses.
                from capture_worker import capture_window  # noqa: PLC0415

                def capture() -> Any:
                    return capture_window(self.window_title)

            errors = 0
            while True:
                now = time.monotonic()
                if now >= deadline or saved >= self.max_frames:
                    break
                try:
                    image, _rect = capture()
                except Exception as exc:  # window hidden/minimized etc.
                    errors += 1
                    if errors >= 20:
                        LOG.warning(
                            "lie-shot: capture failed %d times in a row "
                            "(%s); stopping early",
                            errors,
                            exc,
                        )
                        break
                    time.sleep(0.1)
                    continue
                errors = 0
                path = run_dir / ("frame_%06d.png" % (saved + 1))
                try:
                    image.save(path, "PNG")
                except Exception as exc:  # pragma: no cover - IO issues
                    LOG.warning("lie-shot: saving %s failed: %s", path, exc)
                saved += 1
                elapsed = time.monotonic() - now
                if elapsed < self.min_interval:
                    time.sleep(self.min_interval - elapsed)
        except Exception:
            LOG.exception("lie-shot: recorder crashed")
        finally:
            elapsed = time.monotonic() - started
            if run_dir is not None:
                LOG.info(
                    "lie-shot: %s recording finished after %.1fs "
                    "(%d frames -> %s)",
                    kind,
                    elapsed,
                    saved,
                    run_dir,
                )
                if self.compose_video and saved > 0:
                    # Best effort and never raising; runs before the flag is
                    # released so a new event cannot overlap the compose.
                    self._compose_run_video(run_dir, saved, elapsed)
            # Always release the single-flight flag - even on an unexpected
            # crash - so a later lie event can start a fresh recording.
            with self._lock:
                self._recording = False

    # ------------------------------------------------------------- video

    def _compose_run_video(
        self, run_dir: Path, saved: int, elapsed: float
    ) -> None:
        """Turn a finished run into an mp4 (decoupled, best effort).

        ``lie_video_tools`` is imported lazily so this module stays light.
        Frames are only removed after the video exists and is verified.
        """

        try:
            from lie_video_tools import compose_frames_to_video  # noqa: PLC0415
        except Exception as exc:  # tool not shipped / import problem
            LOG.warning(
                "lie-shot: video tooling unavailable (%s); frames kept", exc
            )
            return
        fps = max(1.0, round(saved / elapsed, 2)) if elapsed > 0 else 10.0
        video_path = run_dir / (run_dir.name + ".mp4")
        try:
            video = compose_frames_to_video(run_dir, video_path, fps=fps)
        except Exception:
            LOG.exception("lie-shot: composing %s failed; frames kept", run_dir)
            return
        if not self._video_is_complete(video, saved):
            LOG.warning(
                "lie-shot: %s looks incomplete; frames kept", video
            )
            return
        LOG.info(
            "lie-shot: composed %d frames -> %s (%.1f fps)",
            saved,
            video,
            fps,
        )
        if self.keep_frames:
            return
        removed = 0
        for frame_path in run_dir.glob("frame_*.png"):
            try:
                frame_path.unlink()
                removed += 1
            except OSError:
                pass
        LOG.info(
            "lie-shot: removed %d png frames (video kept); set keep_frames "
            "to keep them",
            removed,
        )

    @staticmethod
    def _video_is_complete(video: Path, expected: int) -> bool:
        """True when ``video`` holds at least ``expected`` frames."""

        import cv2  # noqa: PLC0415

        try:
            if not video.is_file() or video.stat().st_size <= 0:
                return False
            capture = cv2.VideoCapture(str(video))
            try:
                return int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) >= expected
            finally:
                capture.release()
        except Exception:
            LOG.warning("lie-shot: verifying %s failed", video, exc_info=True)
            return False

    def _prepare_run_dir(self, kind: str = "lie") -> Path:
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        run_dir = self.output_dir / ("%s_%s" % (kind, stamp))
        suffix = 2
        while run_dir.exists():
            run_dir = self.output_dir / ("%s_%s_%d" % (kind, stamp, suffix))
            suffix += 1
        # parents=True also creates the root screenshots/ folder when it
        # does not exist yet.
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir
