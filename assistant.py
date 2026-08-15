"""Integrates capture, movement, and status workers."""

from __future__ import annotations

import argparse
import json
import logging
import queue
import signal
import threading
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Modular MapleStory screen assistant")
    parser.add_argument("--window-title", default="冒险岛怀旧服")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--attack-interval", type=float, default=2.0,
                        help="seconds between Ctrl attacks (default: 2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="analyze/log only; default sends keyboard events")
    parser.add_argument("--debug-dir", type=Path)
    parser.add_argument("--no-ui", action="store_true",
                        help="disable the independent OpenCV debug UI worker")
    parser.add_argument("--ui-refresh-ms", type=int, default=100,
                        help="debug UI queue polling interval (default: 100 ms)")
    parser.add_argument("--rope-calibration", type=Path,
                        default=Path(__file__).with_name("rope_calibration.json"))
    parser.add_argument(
        "--map-profile", type=Path,
        default=Path(__file__).with_name("map_profiles") / "shooter_training_ground_1.json",
        help="map-specific layers, endpoints, rope, and route order",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(threadName)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Imports are delayed so `--help` works even before dependencies are installed.
    from capture_worker import CaptureWorker, FrameBus
    from movement_worker import MovementWorker
    from status_worker import StatusWorker, WindowKeySender
    from attack_worker import AttackWorker
    from minimap_detector import MinimapDetector
    from ui_worker import UiWorker

    stop_event = threading.Event()
    climb_attack_lock = threading.Lock()
    climbing_active = threading.Event()
    movement_frames: queue.Queue = queue.Queue(maxsize=1)
    status_frames: queue.Queue = queue.Queue(maxsize=1)
    ui_frames: queue.Queue = queue.Queue(maxsize=1)
    subscribers = [movement_frames, status_frames]
    if not args.no_ui:
        subscribers.append(ui_frames)
    bus = FrameBus(subscribers)
    key_sender = WindowKeySender(args.window_title, dry_run=args.dry_run)
    if not args.dry_run:
        try:
            key_sender.select_window()
        except OSError as exc:
            logging.error("cannot start live input: %s", exc)
            return 2
    calibration = json.loads(args.rope_calibration.read_text(encoding="utf-8"))
    map_profile = json.loads(args.map_profile.read_text(encoding="utf-8"))
    rope_profile = map_profile["rope"]
    minimap_detector = MinimapDetector(
        fallback_region=tuple(map_profile["minimap_region"])
    )
    logging.info("map=%s patrol=%s route=%s", map_profile["map_name"],
                 map_profile.get("patrol_enabled", False),
                 " -> ".join(map_profile.get("route_order", [])))

    core_workers = [
        CaptureWorker(args.window_title, args.interval, bus, stop_event, args.debug_dir),
        MovementWorker(
            movement_frames,
            key_sender,
            stop_event,
            minimap_region=tuple(map_profile["minimap_region"]),
            fixed_target_x=float(rope_profile["x"]),
            horizontal_tolerance=float(calibration["horizontal_tolerance"]),
            climb_up_hold_seconds=float(calibration["climb_up_hold_seconds"]),
            movement_hold_seconds=float(calibration.get("movement_hold_seconds", 2.0)),
            minimum_final_hold_seconds=float(calibration.get("minimum_final_hold_seconds", 0.08)),
            minimum_movement_hold_seconds=float(
                calibration.get("minimum_movement_hold_seconds", 0.30)
            ),
            estimated_minimap_speed=float(calibration.get("estimated_minimap_speed", 0.11)),
            final_calculation_distance=float(calibration.get("final_calculation_distance", 0.04)),
            estimated_final_speed=float(calibration.get("estimated_final_speed", 0.205)),
            final_move_safety_gain=float(calibration.get("final_move_safety_gain", 0.95)),
            aligned_frames_required=int(calibration.get("aligned_frames_required", 2)),
            climb_nudge_seconds=float(calibration.get("climb_nudge_seconds", 0.10)),
            climb_y_change_required=float(calibration.get("climb_y_change_required", 0.015)),
            climb_failed_shift_right_seconds=float(
                calibration.get("climb_failed_shift_right_seconds", 0.01)
            ),
            near_rope_seconds=float(calibration.get("near_rope_seconds", 0.5)),
            near_rope_range=float(rope_profile["near_range"]),
            climb_attack_lock=climb_attack_lock,
            climbing_active_event=climbing_active,
            important_positions=map_profile.get("layers", {}),
            route_order=map_profile.get("route_order", []),
            patrol_enabled=map_profile.get("patrol_enabled", False),
            final_layer_action=map_profile.get("final_layer_action", "wait"),
            first_layer=map_profile.get("first_layer"),
            drop_chord_hold_seconds=float(
                calibration.get("drop_chord_hold_seconds", 0.10)
            ),
            drop_retry_seconds=float(calibration.get("drop_retry_seconds", 1.0)),
            minimap_detector=minimap_detector,
        ),
        StatusWorker(status_frames, key_sender, stop_event),
        AttackWorker(key_sender, stop_event, args.attack_interval,
                     climbing_active_event=climbing_active),
    ]
    optional_workers = [] if args.no_ui else [
        UiWorker(
            ui_frames,
            stop_event,
            minimap_detector,
            configured_map_name=str(map_profile.get("map_name", "")),
            refresh_ms=args.ui_refresh_ms,
        )
    ]
    workers = core_workers + optional_workers

    def request_stop(*_unused: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    for worker in workers:
        worker.start()

    logging.info("assistant running (%s); Ctrl+C stops",
                 "DRY-RUN" if args.dry_run else "LIVE")
    try:
        while not stop_event.wait(0.5):
            if any(not worker.is_alive() for worker in core_workers):
                logging.error("a core worker stopped unexpectedly")
                stop_event.set()
    finally:
        stop_event.set()
        for worker in workers:
            worker.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
