#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Live continuous mob detection (10 FPS default).
Draws mob boxes + center zone + stamp; ESC to exit.

Usage:
    python live_view.py [--threshold 0.4] [--fps 10]
"""

import sys
import io
import time
import ctypes
import argparse
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

INTERVAL = 0.1  # 10 fps


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live YOLO mob detection view")
    parser.add_argument("--threshold", type=float, default=None,
                        help="confidence threshold override (default: config.yaml)")
    parser.add_argument("--fps", type=float, default=10.0,
                        help="frames per second (default 10)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bot = OptimizedMapleBot()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    conf = (args.threshold if args.threshold is not None
            else float(bot.config.get("model.confidence_threshold", 0.45)))
    bot.model.conf = conf
    interval = max(0.05, 1.0 / max(1.0, args.fps))
    region = bot.monitor
    print(f"live view: region={region} threshold={conf} fps={args.fps} (ESC to quit)")

    with mss.MSS() as sct:
        while True:
            t0 = time.time()
            shot = sct.grab(region)
            img = cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            dets = bot.detect_objects(img)
            preview = bot._draw_detections(img.copy(), dets)
            cv2.putText(
                preview,
                f"THRESHOLD: {conf:.2f} | MOBS: {len(dets)} | bright={gray.mean():.0f}",
                (10, 70), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 3,
            )
            for d in dets:
                print(f"MOB conf={d.confidence:.2f} box={d.bbox}")
            cv2.imshow("LIVE mob detection - ESC to exit", preview)
            key = cv2.waitKey(1) & 0xFF
            if key == 27:
                break
            elapsed = time.time() - t0
            if elapsed < interval:
                time.sleep(interval - elapsed)

    cv2.destroyAllWindows()
    print("live view closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
