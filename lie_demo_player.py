# -*- coding: utf-8 -*-
"""测试测谎 demo player.

Plays a recorded lie-event video (``detect_video/try_detect.mp4`` by
default) through the real ROI tracker window so the operator can watch the
white target being tracked and the mouse following it.  It reuses the
RoiVideoPlayer engine from target_tracker with a demo-specific seed rule:
try_detect.mp4 shows a transient white popup near the centre during the
first ~50 frames, and neither generic white-area selector reliably picks it
(the frame also contains large static white UI regions).

Run as a separate process from the assistant UI:

    .venv\\Scripts\\python.exe lie_demo_player.py [video_path]
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_VIDEO_DEFAULT = _ROOT / "detect_video" / "try_detect.mp4"

# The player imports target_tracker modules; make them importable even
# when the tracker package is not pip-installed into this venv.
for _path in (str(_ROOT / "target_tracker"), str(_ROOT / "target_tracker" / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import tkinter as tk  # noqa: E402
from tkinter import messagebox  # noqa: E402


def select_demo_target_mask(frame: np.ndarray) -> np.ndarray:
    """Pick the compact white popup that the demo should track.

    Rule: HSV white (low saturation, high value), morphological close,
    then the largest component whose size and span look like a popup, not
    a full-width UI bar or a translucent overlay.  Falls back to the
    package's plain initial selector so the demo never silently dies.
    """

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    white = np.asarray(
        (hsv[:, :, 1] <= 60) & (hsv[:, :, 2] >= 235), dtype=np.uint8
    )
    white = cv2.morphologyEx(
        white,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
    )
    height, width = white.shape
    count, labels, stats, _centers = cv2.connectedComponentsWithStats(
        white, connectivity=8
    )
    min_side = min(width, height)
    candidates: list[tuple[float, int]] = []
    for label in range(1, count):
        x, y, cw, ch, area = (int(value) for value in stats[label])
        if area < 2000 or area > width * height * 0.05:
            continue
        if min(cw, ch) < 30:
            continue
        if max(cw, ch) > min_side * 0.35:
            continue
        aspect = cw / max(1, ch)
        if aspect < 0.3 or aspect > 3.3:
            continue
        center = _centers[label]
        distance = float(np.hypot(
            center[0] - width / 2, center[1] - height / 2
        ))
        # Largest popup-like blob, biased slightly toward the frame centre.
        candidates.append((area - distance * 2.0, label))
    if not candidates:
        from realtime_fade_tracker.initial import select_initial_white_mask

        return select_initial_white_mask(frame, avoid_countdown=True)
    selected = max(candidates)[1]
    return np.asarray(labels == selected, dtype=np.uint8)


def start_embedded_demo(
    parent: tk.Misc,
    models: tuple[object, object],
    video: Path = _VIDEO_DEFAULT,
    *,
    device_label: str = "CUDA",
):
    """Open the demo as a child window using the assistant's warm models.

    The old button spawned a second interpreter, whose private model cache
    had to reload Cutie.  This entry point is called by the main Tk UI and
    uses a ``Toplevel`` plus the models warmed by ``AutoLieWorker`` at startup.
    """

    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(f"demo video not found: {video}")
    capture = cv2.VideoCapture(str(video))
    ok, first_frame = capture.read()
    capture.release()
    if not ok or first_frame is None:
        raise RuntimeError(f"could not read demo video: {video}")
    mask = select_demo_target_mask(first_frame)
    if int(mask.sum()) <= 0:
        raise RuntimeError("no demo target found in first frame")

    from roi_video_tracker import RoiVideoPlayer

    root = tk.Toplevel(parent)
    root.geometry("1100x760")
    return RoiVideoPlayer(
        root, video, first_frame, mask, models=models,
        device_label=device_label,
        device=device_label.lower(),
    )


def main() -> int:
    import torch  # noqa: E402

    print("lie_demo start | python", sys.version.split()[0],
          "| torch", torch.__version__,
          "| cuda", torch.cuda.is_available(), flush=True)
    video = Path(sys.argv[1]) if len(sys.argv) > 1 else _VIDEO_DEFAULT
    if not video.is_file():
        print(f"demo video not found: {video}")
        return 2
    capture = cv2.VideoCapture(str(video))
    ok, first_frame = capture.read()
    capture.release()
    if not ok or first_frame is None:
        print(f"could not read first frame: {video}")
        return 2
    try:
        mask = select_demo_target_mask(first_frame)
    except Exception as exc:
        print(f"no white target found in first frame: {exc}")
        return 2

    from roi_video_tracker import RoiVideoPlayer

    root = tk.Tk()
    root.geometry("1100x760")
    player = RoiVideoPlayer(root, video, first_frame, mask)
    root.protocol("WM_DELETE_WINDOW", player.close)
    print(
        "demo started: seed mask pixels=%d target=%s"
        % (int(mask.sum()), video.name),
        flush=True,
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        raise SystemExit(1)
