#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Threshold sweep for the YOLO mob detector.

Sweeps 0.20 -> 0.60 (step 0.05), each: fresh capture, detection, mob boxes,
threshold + count stamped on the image, shown 10 seconds (window forced to
foreground / moved to the secondary monitor so it is visible next to the
fullscreen game), and saved as threshold_sweep_<t>.png.
"""

import sys
import io
import time
import ctypes
from pathlib import Path

try:
    if sys.stdout is not None and hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2
import numpy as np
import mss

from auto import OptimizedMapleBot

THRESHOLDS = [round(0.20 + 0.05 * i, 2) for i in range(9)]  # 0.20..0.60
SHOW_SECONDS = 10


def physical_game_region() -> dict:
    """Return the game's PHYSICAL pixel region (DPI-aware).

    The game reports logical 1707x1067 at 150%% DPI = physical 2560x1600.
    mss captures in physical pixels, so we must use the physical rect.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI
    except Exception:
        pass
    import pygetwindow as gw

    for w in gw.getAllWindows():
        if w.title and ("冒险" in w.title or "懷舊" in w.title
                        or "MapleStory" in w.title or "Maple" in w.title
                        or "怀旧" in w.title):
            if w.width > 200 and w.height > 200:
                return {"left": int(w.left), "top": int(w.top),
                        "width": int(w.width), "height": int(w.height),
                        "title": w.title}
    # Fallback: physical size of the primary monitor.
    with mss.MSS() as sct:
        m = sct.monitors[1]
        return {"left": int(m["left"]), "top": int(m["top"]),
                "width": int(m["width"]), "height": int(m["height"]),
                "title": "primary"}


def capture(region: dict) -> np.ndarray:
    with mss.MSS() as sct:
        shot = sct.grab(region)
        return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)


def show_visible(title: str, image: np.ndarray, seconds: int) -> None:
    """Show the window, force it above the fullscreen game, keep it there."""
    cv2.imshow(title, image)
    cv2.waitKey(1)
    try:
        import win32gui
        import win32con

        hwnd = win32gui.FindWindow(None, title)
        if hwnd:
            # Move to the left/secondary monitor area and raise it.
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, -1900, -600,
                                  900, 600, win32con.SWP_SHOWWINDOW)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.2)
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, -1900, -600,
                                  900, 600, win32con.SWP_SHOWWINDOW)
    except Exception as exc:
        print(f"(window placement skipped: {exc})")
    cv2.waitKey(seconds * 1000)
    cv2.destroyAllWindows()


def main() -> int:
    bot = OptimizedMapleBot()
    region = physical_game_region()
    bot.monitor = dict(region)
    print("capturing physical region:", region)

    for threshold in THRESHOLDS:
        bot.model.conf = threshold
        img = capture(region)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        detections = bot.detect_objects(img)
        mob_count = len(detections)

        preview = bot._draw_detections(img.copy(), detections)
        cv2.putText(preview, f"THRESHOLD: {threshold:.2f}  |  MOBS: {mob_count}",
                    (10, 70), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 255, 255), 3)
        cv2.putText(preview, f"bright={gray.mean():.0f}",
                    (10, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

        out = f"threshold_sweep_{threshold:.2f}.png"
        cv2.imwrite(out, preview)
        print(f"[{threshold:.2f}] mobs={mob_count} bright={gray.mean():.0f} "
              f"saved={out}")

        show_visible(
            f"threshold {threshold:.2f} - mobs {mob_count} (auto-closes)",
            preview, SHOW_SECONDS,
        )

    print("sweep done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
