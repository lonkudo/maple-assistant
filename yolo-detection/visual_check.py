#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Visual detection check for the YOLO model (MapleStory Worlds automation).

Captures the game window (mss with PrintWindow fallback for fullscreen games),
runs YOLO detection (mob-only + center zone), draws the result, saves a PNG,
and opens an OpenCV window.  Press any key / close to exit.
"""

import sys
import io
import time
from pathlib import Path

# pythonw.exe (no console) must not touch sys.stdout; print is a no-op then.
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

OUTPUT = "detection_visual.png"


def find_game_window() -> dict:
    """Locate the MapleStory window rect via pygetwindow (fallback: full screen)."""
    try:
        import pygetwindow as gw

        for w in gw.getAllWindows():
            if w.title and ("冒险" in w.title or "懷舊" in w.title
                            or "MapleStory" in w.title or "Maple" in w.title
                            or "怀旧" in w.title):
                if w.width > 200 and w.height > 200:
                    return {
                        "left": int(w.left),
                        "top": int(w.top),
                        "width": int(w.width),
                        "height": int(w.height),
                        "title": w.title,
                    }
    except Exception as exc:  # pragma: no cover
        print(f"pygetwindow lookup failed: {exc}")
    return {"left": 0, "top": 0, "width": 1920, "height": 1080, "title": "fullscreen"}


def capture_window(window: dict) -> np.ndarray:
    """Capture the game window: mss first, PrintWindow as fallback.

    Exclusive-fullscreen / hardware-accelerated games often return a black
    frame via BitBlt (mss); PrintWindow with PW_RENDERFULLCONTENT grabs the
    window's own rendered content instead.
    """
    left, top = int(window["left"]), int(window["top"])
    width, height = int(window["width"]), int(window["height"])

    with mss.MSS() as sct:
        shot = sct.grab({"left": left, "top": top,
                         "width": width, "height": height})
        img = cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if gray.mean() > 25.0:  # mss got real content
        return img

    # mss returned a black frame -> try PrintWindow.
    print("mss returned a dark frame; trying PrintWindow fallback...")
    try:
        import win32gui
        import win32ui
        import win32con

        hwnd = win32gui.FindWindow(None, window["title"])
        if not hwnd:
            return img
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        flags = win32con.PW_RENDERFULLCONTENT
        try:
            win32gui.PrintWindow(hwnd, save_dc.GetSafeHdc(), flags)
        except Exception:
            win32gui.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)
        raw = bitmap.GetBitmapBits(True)
        img_pw = np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 4))
        img_pw = cv2.cvtColor(img_pw, cv2.COLOR_BGRA2BGR)
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
        gray2 = cv2.cvtColor(img_pw, cv2.COLOR_BGR2GRAY)
        if gray2.mean() > gray.mean():
            print(f"PrintWindow capture mean brightness: {gray2.mean():.0f}")
            return img_pw
        return img
    except Exception as exc:  # pragma: no cover
        print(f"PrintWindow fallback failed: {exc}")
        return img


def main() -> int:
    window = find_game_window()
    print(f"Game window: {window}")

    bot = OptimizedMapleBot()
    bot.monitor = {
        "left": int(window["left"]),
        "top": int(window["top"]),
        "width": int(window["width"]),
        "height": int(window["height"]),
    }
    conf = float(bot.config.get("model.confidence_threshold", 0.6))
    bot.model.conf = conf
    print(f"Confidence threshold: {conf}")

    img = capture_window(window)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    print(f"capture brightness mean={gray.mean():.0f} "
          f"black%={100*(gray<20).mean():.0f}")

    t0 = time.time()
    detections = bot.detect_objects(img)
    dt_ms = (time.time() - t0) * 1000
    print(f"Detection time: {dt_ms:.0f}ms | found {len(detections)} mobs")

    for d in detections:
        print(f"  mob conf={d.confidence:.2f} box={d.bbox}")

    preview = bot._draw_detections(img.copy(), detections)
    cv2.imwrite(OUTPUT, preview)
    print(f"Saved visual result: {OUTPUT}")

    # Show the window briefly and auto-close so no stale windows pile up.
    print("Showing result window for 5 seconds (auto-closes)...")
    cv2.imshow("MapleStory Mob Detection - auto-closes in 5s", preview)
    cv2.waitKey(5000)
    cv2.destroyAllWindows()
    print("Window closed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
