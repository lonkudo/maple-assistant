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
from typing import Optional

try:
    if sys.stdout is not None and hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, str(Path(__file__).resolve().parent))
# The assistant root (parent of yolo-detection) holds shared coordination
# modules like combat_coordination.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
import numpy as np
import mss

from auto import OptimizedMapleBot, Detection

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
    parser.add_argument("--attack", action="store_true",
                        help="enable auto-attack: face the target (left/right) "
                             "then press Ctrl while the game is focused")
    parser.add_argument("--attack-key", default="ctrl",
                        help="key pressed to attack: ctrl/alt/left/right "
                             "(default ctrl)")
    parser.add_argument("--attack-cooldown", type=float, default=0.6,
                        help="minimum seconds between attacks (default 0.6)")
    parser.add_argument("--attack-log", default=None,
                        help="path to append attack diagnostics (default: "
                             "attack.log next to this script)")
    parser.add_argument("--attack-state", default=None,
                        help="path to write attack state JSON for the patrol "
                             "worker (attack priority signal)")
    parser.add_argument("--window-title", default="\u5192\u9669\u5c9b\u6000\u65e7\u670d",
                        help="game window title used for the foreground check "
                             "(default: \u5192\u9669\u5c9b\u6000\u65e7\u670d)")
    parser.add_argument("--attack-line-height", type=float, default=0.72,
                        help="vertical position of the attack range line as a "
                             "fraction of frame height (default 0.72)")
    parser.add_argument("--zone-width", type=float, default=None,
                        help="detection zone width fraction 0.1-1.0 "
                             "(default: config.yaml center_zone)")
    parser.add_argument("--zone-height", type=float, default=None,
                        help="detection zone height fraction 0.1-1.0 "
                             "(default: config.yaml center_zone)")
    parser.add_argument("--zone-shift-y", type=float, default=None,
                        help="vertical shift of the zone center as a fraction "
                             "of frame height, -0.5..0.5 (positive = down)")
    parser.add_argument("--rope-state", default=None,
                        help="path to write YOLO rope state JSON for the patrol "
                             "worker (gates the inner-gap jump on the real "
                             "screen gap)")
    return parser.parse_args()


def draw_attack_range(
    img: np.ndarray,
    attack_range: int,
    line_height: float,
    character: Optional[Detection] = None,
) -> np.ndarray:
    """Draw the attack range line anchored to the character position.

    When ``character`` is provided the range line is centered on the
    character's center (so it follows the player as the camera moves).
    Without a character the line falls back to the screen center at
    ``line_height`` (fraction of frame height).
    """

    height, width = img.shape[:2]
    color = (255, 255, 0)  # cyan
    if character is not None:
        center_x = int(character.center[0])
        y = int(character.center[1])
    else:
        center_x = width // 2
        y = int(height * max(0.05, min(0.95, line_height)))
    half = max(10, int(attack_range // 2))
    left = max(0, center_x - half)
    right = min(width - 1, center_x + half)
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
            else float(bot.config.get(
                "detection_behavior.confidence_threshold",
                bot.config.get("model.confidence_threshold", 0.45))))
    # The UI threshold must drive BOTH the model's prediction filter and
    # auto.py's own confidence gate (detect_objects/detect_character read
    # self.confidence_threshold, not model.conf).
    bot.confidence_threshold = conf
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
    if args.zone_shift_y is not None:
        zone["shift_y"] = max(-0.5, min(0.5, args.zone_shift_y))
    interval = max(0.05, 1.0 / max(1.0, args.fps))
    region = bot.monitor

    executor = None
    attack_state = None
    rope_state = None
    if args.attack:
        from attack_executor import AttackExecutor

        log_path = args.attack_log
        if log_path is None:
            log_path = str(Path(__file__).resolve().parent / "attack.log")
        executor = AttackExecutor(
            args.window_title,
            attack_key=args.attack_key,
            cooldown=args.attack_cooldown,
            log_path=log_path,
        )
        if args.attack_state:
            from combat_coordination import AttackStateFile

            attack_state = AttackStateFile(args.attack_state)
        if executor.select_window():
            print(f"auto-attack enabled: game window focused "
                  f"(title={args.window_title!r})")
        else:
            print(f"auto-attack enabled, but game window NOT focused "
                  f"(title={args.window_title!r}) - attack will try to "
                  "refocus every few seconds")
    if args.rope_state:
        from combat_coordination import RopeStateFile

        rope_state = RopeStateFile(args.rope_state)
        print(f"rope state publishing to {args.rope_state}")
    print(f"live view: region={region} threshold={conf} fps={args.fps} "
          f"show={not args.no_show} zone="
          f"{zone.get('width_fraction')}x{zone.get('height_fraction')} "
          f"shift_y={zone.get('shift_y', 0.0)} "
          f"attack={'ON' if executor else 'OFF'} (ESC to quit)")

    with mss.MSS() as sct:
        while True:
            t0 = time.time()
            try:
                shot = sct.grab(region)
                img = cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)
            except Exception as exc:
                # mss can transiently fail (e.g. BitBlt while the game is in
                # exclusive fullscreen).  One bad grab must not kill the
                # whole detection process - log and retry next tick.
                print(f"capture error: {exc} - retrying", flush=True)
                time.sleep(max(0.05, interval))
                continue
            try:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                dets = bot.detect_objects(img)
                character = bot.detect_character(img)
                target = bot.attack_decision(
                    dets, character, attack_range=args.attack_range
                )
                # Preview: character/environment/mob detections, each drawn
                # with its own color by _draw_detections (item/npc/ui
                # excluded).  The attack decision above still uses the
                # mob-only tracked detections.
                preview = bot._draw_detections(
                    img.copy(), bot.detect_objects(img, include_all=True)
                )
                if character is not None:
                    # Draw the player box in bright green with a label.
                    x1, y1, x2, y2 = character.bbox
                    cv2.rectangle(preview, (x1, y1), (x2, y2), (0, 255, 0), 3)
                    cv2.putText(
                        preview,
                        f"PLAYER conf={character.confidence:.2f}",
                        (x1, y2 + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                        (0, 255, 0), 2,
                    )
                if target is not None:
                    # Highlight the chosen attack target in red outline.
                    x1, y1, x2, y2 = target.bbox
                    cv2.rectangle(preview, (x1 - 6, y1 - 6), (x2 + 6, y2 + 6),
                                  (0, 0, 255), 2)
                    # Auto-attack: face the monster and press Ctrl.  Only
                    # fires when the game window is focused and the cooldown
                    # elapsed.
                    if executor is not None and character is not None:
                        if executor.attack(character, target):
                            print(f"ATTACK pressed ctrl, facing="
                                  f"{executor._facing}")
                # Publish the attack state for the patrol worker: active only
                # while a target is selected (attack priority over patrol).
                if attack_state is not None:
                    if target is not None and character is not None:
                        attack_state.write(True, target.center)
                    else:
                        attack_state.write(False)
                # Publish YOLO rope state so the patrol worker can gate the
                # inner-gap jump on the real screen gap.
                if rope_state is not None:
                    rope = bot.detect_rope(img)
                    if rope is not None:
                        rope_state.write(
                            True,
                            rope_x=float(rope.center[0]),
                            rope_y=float(rope.center[1]),
                            char_x=(float(character.center[0])
                                    if character is not None else None),
                            char_y=(float(character.center[1])
                                    if character is not None else None),
                        )
                    else:
                        rope_state.write(False)
                preview = draw_attack_range(
                    preview, args.attack_range, args.attack_line_height,
                    character=character,
                )
                cv2.putText(
                    preview,
                    f"THRESHOLD: {conf:.2f} | MOBS: {len(dets)} | "
                    f"TARGET: {'YES' if target else 'NO'} | "
                    f"PLAYER: {'YES' if character else 'NO'} | "
                    f"bright={gray.mean():.0f}",
                    (10, 70), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 3,
                )
                for d in dets:
                    print(f"MOB conf={d.confidence:.2f} box={d.bbox}")
                if character is not None:
                    print(f"PLAYER conf={character.confidence:.2f} "
                          f"center={character.center}")
                if target is not None:
                    print(f"TARGET SELECTED conf={target.confidence:.2f} "
                          f"box={target.bbox}")
                if not args.no_show:
                    cv2.imshow("LIVE mob detection - ESC to exit", preview)
                    key = cv2.waitKey(1) & 0xFF
                    if key == 27:
                        break
            except Exception as exc:
                # A single bad frame (model hiccup, draw error) must not kill
                # the process; log it and keep going next tick.
                print(f"frame error: {exc} - continuing", flush=True)
            elapsed = time.time() - t0
            if elapsed < interval:
                time.sleep(interval - elapsed)

    if not args.no_show:
        cv2.destroyAllWindows()
    print("live view closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
