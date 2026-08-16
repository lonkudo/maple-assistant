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
    parser.add_argument("--no-show", action="store_true",
                        help="run without the preview window (headless)")
    parser.add_argument("--attack-range", type=int, default=800,
                        help="attack range width in pixels, centered on screen "
                             "(default 800)")
    parser.add_argument("--attack-line-height", type=float, default=0.72,
                        help="vertical position of the attack range line as a "
                             "fraction of frame height (default 0.72)")
    parser.add_argument("--zone-width", type=float, default=None,
                        help="detection zone width fraction 0.1-1.0 "
                             "(default: config.yaml center_zone)")
    parser.add_argument("--zone-height", type=float, default=None,
                        help="detection zone height fraction 0.1-1.0 "
                             "(default: config.yaml center_zone)")
    return parser.parse_args()


def draw_attack_range(
    img: np.ndarray, attack_range: int, line_height: float
) -> np.ndarray:
    """Draw the attack range line centered on the screen.

    A horizontal cyan line spans ``attack_range`` pixels centered on the
    frame, with end ticks and a label.  Used to suggest how far the bot can
    attack in the width direction.
    """

    height, width = img.shape[:2]
    center_x = width // 2
    y = int(height * max(0.05, min(0.95, line_height)))
    half = max(10, int(attack_range // 2))
    left = max(0, center_x - half)
    right = min(width - 1, center_x + half)
    color = (255, 255, 0)  # cyan
    # Main range line.
    cv2.line(img, (left, y), (right, y), color, 3)
    # End ticks.
    tick = max(8, int(height * 0.02))
    cv2.line(img, (left, y - tick), (left, y + tick), color, 2)
    cv2.line(img, (right, y - tick), (right, y + tick), color, 2)
    # Center marker (player position reference).
    cv2.line(img, (center_x, y - tick // 2), (center_x, y + tick // 2),
             (0, 255, 255), 2)
    # Label.
    cv2.putText(img, f"ATTACK RANGE: {attack_range}px",
                (center_x - half, y - tick - 8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    return img


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
    # Override the detection zone size from CLI if provided.
    zone = bot.config.config.get("detection_behavior", {}).get("center_zone")
    if zone is None:
        zone = {}
        bot.config.config.setdefault("detection_behavior", {})["center_zone"] = zone
    zone["enabled"] = True
    if args.zone_width is not None:
        zone["width_fraction"] = max(0.1, min(1.0, args.zone_width))
    if args.zone_height is not None:
        zone["height_fraction"] = max(0.1, min(1.0, args.zone_height))
    interval = max(0.05, 1.0 / max(1.0, args.fps))
    region = bot.monitor
    print(f"live view: region={region} threshold={conf} fps={args.fps} "
          f"show={not args.no_show} zone="
          f"{zone.get('width_fraction')}x{zone.get('height_fraction')} "
          f"(ESC to quit)")

    with mss.MSS() as sct:
        while True:
            t0 = time.time()
            shot = sct.grab(region)
            img = cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            dets = bot.detect_objects(img)
            preview = bot._draw_detections(img.copy(), dets)
            preview = draw_attack_range(
                preview, args.attack_range, args.attack_line_height
            )
            cv2.putText(
                preview,
                f"THRESHOLD: {conf:.2f} | MOBS: {len(dets)} | bright={gray.mean():.0f}",
                (10, 70), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 3,
            )
            for d in dets:
                print(f"MOB conf={d.confidence:.2f} box={d.bbox}")
            if not args.no_show:
                cv2.imshow("LIVE mob detection - ESC to exit", preview)
                key = cv2.waitKey(1) & 0xFF
                if key == 27:
                    break
            elapsed = time.time() - t0
            if elapsed < interval:
                time.sleep(interval - elapsed)

    if not args.no_show:
        cv2.destroyAllWindows()
    print("live view closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
