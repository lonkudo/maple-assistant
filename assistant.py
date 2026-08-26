"""Integrates capture, movement, and status workers."""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import replace
import json
import logging
from logging.handlers import RotatingFileHandler
import queue
import signal
import threading
import time
from pathlib import Path
from typing import Callable, Optional


class CompactThreadFormatter(logging.Formatter):
    """Display worker thread names without the redundant ``-worker``."""

    def format(self, record: logging.LogRecord) -> str:
        record.compact_thread_name = record.threadName.removesuffix("-worker")
        return super().format(record)


def _compact_log_formatter() -> logging.Formatter:
    return CompactThreadFormatter(
        "%(asctime)s %(compact_thread_name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


OPENCV_ANALYSIS_SIZE = (200, 200)
MINIMAP_FALLBACK_REGION = (0, 0, 0.2, 0.24)
STATUS_CAPTURE_REGION = (0.36, 0.96, 0.53, 1)
SINGLE_INSTANCE_MUTEX_NAME = "Local\\MapleAssistant.Singleton.v1"
# Status-bar fractions are calibrated to the CLIENT WIDTH so they hold at any
# resolution (the game HUD scales with the client).  Measured: a full HP fill
# is about 131px on a 2560-wide client -> 131/2560 ~= 0.0512.
FULL_BAR_CLIENT_FRACTION = 0.0512
MIN_BAR_CLIENT_FRACTION = 0.0020


def _acquire_single_instance_mutex(
    mutex_name: str = SINGLE_INSTANCE_MUTEX_NAME,
) -> int | None:
    """Own the per-session assistant mutex, or return None for a duplicate."""

    if not hasattr(ctypes, "WinDLL"):
        return 1
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (
        ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR
    )
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    error_code = ctypes.get_last_error()
    if not handle:
        # A normal process cannot open a mutex created by an elevated copy.
        # Treat access denied as proof that the singleton already exists.
        if error_code == 5:
            return None
        raise OSError(error_code, "could not create Maple Assistant singleton mutex")
    if error_code == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


def _release_single_instance_mutex(handle: int | None) -> None:
    if not handle or handle == 1 or not hasattr(ctypes, "WinDLL"):
        return
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(wintypes.HANDLE(handle))


def _start_live_input(
    key_sender: object,
    automation_active_event: threading.Event,
    before_enable: Optional[Callable[[], None]] = None,
) -> None:
    """Select the game first, then arm all keyboard-producing workers."""

    logging.info("START PATROL: selecting game window")
    if key_sender.select_window() is False:
        raise OSError("game window selection returned failure")
    if not key_sender.is_game_foreground():
        raise OSError("game window did not become foreground")
    logging.info("START PATROL: game window verified foreground")
    if before_enable is not None:
        before_enable()
    key_sender.enable_input()
    automation_active_event.set()
    logging.info("START PATROL: automation input armed")


def _stop_live_input(
    key_sender: object,
    automation_active_event: threading.Event,
) -> None:
    automation_active_event.clear()
    key_sender.disable_input()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Modular MapleStory screen assistant")
    parser.add_argument("--window-title", default="冒险岛怀旧服")
    parser.add_argument("--interval", type=float, default=0.25,
                        help="seconds between minimap captures (default: 0.25)")
    parser.add_argument("--status-interval", type=float, default=1.0,
                        help="seconds between HP/MP captures (default: 1.0)")
    parser.add_argument("--attack-interval", type=float, default=2.0,
                        help="seconds between Ctrl attacks (default: 2)")
    parser.add_argument("--enable-attack", action="store_true",
                        help="enable the independent Ctrl attack worker (off by default)")
    parser.add_argument("--pickup-interval", type=float, default=0.2,
                        help="pickup Z hold length in seconds while walking "
                             "(default: 0.2; 0 disables pickup)")
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
        "--recording-configuration", type=Path,
        default=Path(__file__).with_name("recording-configuration.json"),
        help="shared recorded layers, endpoints, ropes, and route order",
    )
    parser.add_argument("--log-level", default="INFO")
    # ==== ADDED ==== debug flag for drawing capture region rectangles
    parser.add_argument("--debug-capture-regions", action="store_true",
                        help="draw rectangles on captured frame showing all ROI capture regions for debugging")
    return parser.parse_args()


def main() -> int:
    singleton_handle = _acquire_single_instance_mutex()
    if singleton_handle is None:
        return 0
    args = parse_args()
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(_compact_log_formatter())
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        handlers=[console_handler],
    )
    log_path = Path(__file__).with_name("work") / "assistant.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_log_handler = RotatingFileHandler(
        log_path,
        maxBytes=1_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    file_log_handler.setFormatter(_compact_log_formatter())
    logging.getLogger().addHandler(file_log_handler)

    # Imports are delayed so `--help` works even before dependencies are installed.
    import numpy as np
    from capture_worker import CaptureWorker, FrameBus
    from character_worker import CharacterWorker
    from movement_worker import MovementWorker, detect_layer_by_y
    from status_worker import (
        BarStatusDetector,
        StatusConfig,
        StatusWorker,
        WindowKeySender,
    )
    from attack_worker import AttackWorker
    from shutdown_worker import ShutdownWorker
    from focus_worker import FocusWorker
    from minimap_detector import MinimapDetector
    from marker_detector import DiamondSizeTracker, detect_yellow_diamond
    from map_identity import MapIdentityStore
    from map_structure_tracker import MapStructureTracker
    from patrol_control import CoordinateLayout, PatrolController
    from ui_worker import UiLogHandler, UiWorker

    stop_event = threading.Event()
    climb_attack_lock = threading.Lock()
    climbing_active = threading.Event()
    dropping_active = threading.Event()
    moving_active = threading.Event()
    pickup_active = threading.Event()
    automation_active = threading.Event()
    game_focused = threading.Event()
    movement_frames: queue.Queue = queue.Queue(maxsize=1)
    status_frames: queue.Queue = queue.Queue(maxsize=1)
    ui_frames: queue.Queue = queue.Queue(maxsize=1)
    character_frames: queue.Queue = queue.Queue(maxsize=1)
    character_positions: queue.Queue = queue.Queue(maxsize=1)
    subscribers = [movement_frames, status_frames, character_frames]
    if not args.no_ui:
        subscribers.append(ui_frames)
    bus = FrameBus(subscribers)
    ui_log_handler = None
    if not args.no_ui:
        ui_log_handler = UiLogHandler(capacity=300)
        ui_log_handler.setFormatter(_compact_log_formatter())
        logging.getLogger().addHandler(ui_log_handler)
    # The dashboard and frame analysis start without stealing focus. Keyboard
    # input is armed only after the user explicitly clicks Start Patrol.
    key_sender = WindowKeySender(
        args.window_title,
        dry_run=args.dry_run,
        input_enabled=False,
        # Alt is the game's JUMP key - never send it during foreground
        # selection or the character jumps every time patrol starts.
        alt_transition=False,
    )
    calibration = json.loads(args.rope_calibration.read_text(encoding="utf-8"))
    map_profile = json.loads(
        args.recording_configuration.read_text(encoding="utf-8")
    )
    patrol_controller = PatrolController(args.recording_configuration, map_profile)
    structure_reference = (
        args.recording_configuration.parent
        / "recording-assets"
        / "map-structure-reference.png"
    )
    structure_tracker = MapStructureTracker(
        structure_reference, tracking_size=OPENCV_ANALYSIS_SIZE[0]
    )
    map_identity_store = MapIdentityStore(
        args.recording_configuration.parent / "recording-assets" / "map-names"
    )

    def stop_patrol_after_focus_loss() -> None:
        patrol_controller.set_enabled(False)

    rope_profile = map_profile["rope"]
    minimap_region = MINIMAP_FALLBACK_REGION
    status_defaults = StatusConfig()
    status_capture_width = STATUS_CAPTURE_REGION[2] - STATUS_CAPTURE_REGION[0]
    status_detector = BarStatusDetector(replace(
        status_defaults,
        status_roi=(0.0, 0.0, 1.0, 1.0),
        # Client-width-relative fractions: the detector's expected bar length
        # becomes fraction * client_width, valid at any resolution.
        full_bar_width_fraction=(
            FULL_BAR_CLIENT_FRACTION / status_capture_width
        ),
        min_bar_width_fraction=(
            MIN_BAR_CLIENT_FRACTION / status_capture_width
        ),
    ))
    minimap_detector = MinimapDetector(
        fallback_region=minimap_region,
        dedicated_crop=True,
        opencv_size=OPENCV_ANALYSIS_SIZE,
    )
    movement_diamond_tracker = DiamondSizeTracker()
    ui_diamond_tracker = DiamondSizeTracker()
    logging.info("map=%s patrol=%s route=%s", map_profile["map_name"],
                 map_profile.get("patrol_enabled", False),
                 " -> ".join(map_profile.get("route_order", [])))

    capture_worker = CaptureWorker(
        args.window_title,
        args.interval,
        bus,
        stop_event,
        args.debug_dir,
        # Capture the FULL client window (no fixed-size top-left crop) so all
        # normalized analysis regions map to the client at any resolution.
        status_capture_region=STATUS_CAPTURE_REGION,
        status_capture_interval=args.status_interval,
        capture_enabled_event=game_focused,
        fast_capture_event=dropping_active,
        fast_interval=0.10,
        # ==== ADDED pass debug flag into capture worker ====
        debug_draw_regions=args.debug_capture_regions,
        debug_minimap_fallback=MINIMAP_FALLBACK_REGION, # <------ ADD THIS LINE
    )

    def prepare_map_session() -> None:
        """Verify the recorded map name and re-anchor transient world Y."""

        # Read the live profile, not the startup snapshot: a reset or map
        # re-identification updates the shared file while the app runs.
        configured_name = patrol_controller.map_name()
        fresh_frame = capture_worker.capture_now()
        detection = minimap_detector.detect(fresh_frame.image)
        title_image = fresh_frame.image.crop(detection.map_name_box)
        if configured_name and map_identity_store.has_reference(configured_name):
            matched, score = map_identity_store.matches(configured_name, title_image)
            if not matched:
                raise OSError(
                    f"current minimap name does not match recorded map "
                    f"{configured_name!r} (visual match {score:.2f})"
                )
            logging.info(
                "MAP NAME matched recorded profile %s confidence=%.3f",
                configured_name,
                score,
            )
        elif configured_name:
            logging.info(
                "MAP NAME profile %s will be recorded with the next position",
                configured_name,
            )

        # Detect the floor on the fresh frame BEFORE setting the transient
        # world-Y origin. Anchoring unconditionally to the configured patrol
        # start made a character standing on layer1 look confidently like
        # layer2; every later world-Y check then reinforced that wrong state.
        analysis_rgb = np.asarray(
            fresh_frame.image.crop(detection.analysis_box).convert("RGB")
        )
        marker = detect_yellow_diamond(analysis_rgb)
        layout = None
        if marker is not None:
            analysis_left, analysis_top, analysis_right, analysis_bottom = (
                detection.analysis_box
            )
            canvas_left, canvas_top, canvas_right, canvas_bottom = (
                detection.canvas_box
            )
            marker_width, marker_height = marker.pixel_size
            layout = CoordinateLayout(
                analysis_width=analysis_right - analysis_left,
                analysis_height=analysis_bottom - analysis_top,
                canvas_left=canvas_left - analysis_left,
                canvas_top=canvas_top - analysis_top,
                canvas_width=canvas_right - canvas_left,
                canvas_height=canvas_bottom - canvas_top,
                diamond_width=marker_width,
                diamond_height=marker_height,
            )
        snapshot = patrol_controller.snapshot(layout)
        detected_name = (
            detect_layer_by_y(marker.y, snapshot.layers)
            if marker is not None else None
        )
        anchor_name = str(detected_name or patrol_controller.first_layer() or (
            snapshot.route_order[0] if snapshot.route_order else ""
        ))
        anchor_layer = snapshot.layers.get(anchor_name, {})
        anchor_world_y = anchor_layer.get("layer_world_y")
        if not snapshot.route_order:
            # Nothing recorded: stand-still + attack mode.  Skip the world-Y
            # re-anchor - there is no patrol route to anchor.
            logging.info(
                "MAP SESSION no patrol route recorded; standing still + attack"
            )
            return
        if anchor_world_y is None:
            raise OSError(
                f"{anchor_name or 'first layer'} has no recorded world Y; "
                "record this map once"
            )
        structure_tracker.start_session(float(anchor_world_y))
        logging.info(
            "MAP SESSION detected %s from marker_y=%s; re-anchoring "
            "world_y=%.6f",
            anchor_name,
            f"{marker.y:.6f}" if marker is not None else "unknown",
            float(anchor_world_y),
        )
    attack_workers = []
    # The fixed-rate attack worker always exists so the UI can toggle it
    # live (Fixed Attack panel).  Without --enable-attack it starts disabled
    # and only waits; the panel flips ``enabled`` when the mode is selected.
    attack_worker = AttackWorker(
        key_sender,
        stop_event,
        args.attack_interval,
        climbing_active_event=climbing_active,
        automation_active_event=automation_active,
    )
    attack_worker.enabled = bool(args.enable_attack)
    attack_workers.append(attack_worker)
    shutdown_worker = ShutdownWorker(
        key_sender,
        stop_event,
        enabled=False,
        hours=3.0,
    )
    # 拾取 (Z) 已并入移动线程：仅在三个移动阶段与方向键同按同放。
    status_worker = StatusWorker(
        status_frames,
        key_sender,
        stop_event,
        detector=status_detector,
        automation_active_event=automation_active,
        potion_retry_attempts=int(
            calibration.get("potion_retry_attempts", 3)
        ),
        potion_retry_delay_seconds=float(
            calibration.get("potion_retry_delay_seconds", 0.05)
        ),
        status_state_path=str(
            Path(__file__).with_name("work") / "status_state.json"
        ),
    )
    movement_worker = MovementWorker(
            movement_frames,
            key_sender,
            stop_event,
            character_positions=character_positions,
            minimap_region=minimap_region,
            fixed_target_x=float(rope_profile["x"]),
            horizontal_tolerance=float(calibration["horizontal_tolerance"]),
            horizontal_tolerance_diamonds=calibration.get(
                "horizontal_tolerance_diamonds"
            ),
            climb_up_hold_seconds=float(calibration["climb_up_hold_seconds"]),
            movement_hold_seconds=float(calibration.get("movement_hold_seconds", 2.0)),
            minimum_final_hold_seconds=float(calibration.get("minimum_final_hold_seconds", 0.08)),
            minimum_movement_hold_seconds=float(
                calibration.get("minimum_movement_hold_seconds", 0.30)
            ),
            estimated_minimap_speed=float(calibration.get("estimated_minimap_speed", 0.11)),
            final_calculation_distance=float(calibration.get("final_calculation_distance", 0.04)),
            final_calculation_diamonds=calibration.get("final_calculation_diamonds"),
            estimated_final_speed=float(calibration.get("estimated_final_speed", 0.205)),
            final_move_safety_gain=float(calibration.get("final_move_safety_gain", 0.95)),
            aligned_frames_required=int(calibration.get("aligned_frames_required", 2)),
            climb_layer_confirm_frames=int(
                calibration.get("climb_layer_confirm_frames", 3)
            ),
            climb_layer_confirm_seconds=float(
                calibration.get("climb_layer_confirm_seconds", 0.3)
            ),
            climb_arrival_world_tolerance=float(
                calibration.get("climb_arrival_world_tolerance", 0.20)
            ),
            climb_nudge_seconds=float(calibration.get("climb_nudge_seconds", 0.10)),
            climb_y_change_required=float(calibration.get("climb_y_change_required", 0.015)),
            climb_world_y_change_required=float(
                calibration.get("climb_world_y_change_required", 0.75)
            ),
            climb_world_y_stall_change_required=float(
                calibration.get("climb_world_y_stall_change_required", 0.15)
            ),
            climb_world_y_stall_frames=int(
                calibration.get("climb_world_y_stall_frames", 2)
            ),
            climb_failed_shift_right_seconds=float(
                calibration.get("climb_failed_shift_right_seconds", 0.01)
            ),
            climb_attempt_interval_seconds=float(
                calibration.get("climb_attempt_interval_seconds", 1.0)
            ),
            climb_failed_cycles_reset=int(
                calibration.get("climb_failed_cycles_reset", 3)
            ),
            patrol_cycles_per_layer=int(
                calibration.get("patrol_cycles_per_layer", 2)
            ),
            near_rope_seconds=float(calibration.get("near_rope_seconds", 0.5)),
            near_rope_range=float(rope_profile["near_range"]),
            near_rope_inner_range=float(
                rope_profile.get("inner_range", rope_profile["near_range"])
            ),
            under_rope_tolerance=float(
                rope_profile.get("under_rope_tolerance", 0.008)
            ),
            near_rope_diamonds=calibration.get("near_rope_diamonds"),
            climb_attack_lock=climb_attack_lock,
            climbing_active_event=climbing_active,
            dropping_active_event=dropping_active,
            important_positions=map_profile.get("layers", {}),
            route_order=map_profile.get("route_order", []),
            patrol_enabled=map_profile.get("patrol_enabled", False),
            climbing_enabled=map_profile.get("climbing_enabled", True),
            final_layer_action=map_profile.get("final_layer_action", "wait"),
            first_layer=map_profile.get("first_layer"),
            # Contiguous patrol floor range (UI-selected): only these floors
            # are patrolled and the character returns to the range when it
            # falls outside it (layer1 is no longer implicitly the start).
            patrol_start_layer=map_profile.get("patrol_start_layer"),
            patrol_end_layer=map_profile.get("patrol_end_layer"),
            # Falling recovery knobs (see rope_calibration.json).
            fall_detect_frames=int(calibration.get("fall_detect_frames", 3)),
            fall_marker_y_gain=float(calibration.get("fall_marker_y_gain", 0.015)),
            drop_chord_hold_seconds=float(
                calibration.get("drop_chord_hold_seconds", 0.10)
            ),
            drop_retry_seconds=float(calibration.get("drop_retry_seconds", 1.5)),
            minimap_detector=minimap_detector,
            patrol_controller=patrol_controller,
            diamond_size_tracker=movement_diamond_tracker,
            structure_tracker=structure_tracker,
            automation_active_event=automation_active,
            moving_active_event=moving_active,
            pickup_active_event=pickup_active,
            attack_state_path=str(
                Path(__file__).with_name("work") / "attack_state.json"
            ),
            attack_block_max_seconds=float(
                calibration.get("attack_block_max_seconds", 4.0)
            ),
            rope_state_path=str(
                Path(__file__).with_name("work") / "rope_state.json"
            ),
            patrol_state_path=str(
                Path(__file__).with_name("work") / "patrol_state.json"
            ),
            rope_jump_px=float(
                map_profile.get("rope", {}).get("jump_px", 140)
            ),
            on_rope_px=float(
                map_profile.get("rope", {}).get("on_rope_px", 50)
            ),
            under_rope_px=float(
                map_profile.get("rope", {}).get("under_rope_px", 10)
            ),
            rope_approach_creep_seconds=float(
                calibration.get("rope_approach_creep_seconds", 0.25)
            ),
            rope_tiny_step_min_seconds=float(
                calibration.get("rope_tiny_step_min_seconds", 0.05)
            ),
            rope_tiny_step_max_seconds=float(
                calibration.get("rope_tiny_step_max_seconds", 0.15)
            ),
            stair_jump_enabled=bool(calibration.get("stair_jump_enabled", True)),
            stair_jump_stall_diamonds=float(
                calibration.get("stair_jump_stall_diamonds", 0.25)
            ),
            stair_jump_stall_frames=int(
                calibration.get("stair_jump_stall_frames", 3)
            ),
            patrol_start_grace_seconds=float(
                calibration.get("patrol_start_grace_seconds", 3.0)
            ),
            stair_jump_attempts_max=int(
                calibration.get("stair_jump_attempts_max", 3)
            ),
            stair_jump_grace_seconds=float(
                calibration.get("stair_jump_grace_seconds", 0.8)
            ),
            stair_jump_alt_hold_seconds=float(
                calibration.get("stair_jump_alt_hold_seconds", 0.06)
            ),
            stair_jump_lead_seconds=float(
                calibration.get("stair_jump_lead_seconds", 0.15)
            ),
            stair_jump_climb_arrival_grace_seconds=float(
                calibration.get("stair_jump_climb_arrival_grace_seconds", 2.0)
            ),
            other_player_check_interval_seconds=float(
                calibration.get("other_player_check_interval_seconds", 0.0)
            ),
            rescue_check_interval_seconds=float(
                calibration.get("rescue_check_interval_seconds", 300.0)
            ),
            rescue_stuck_frames=int(
                calibration.get("rescue_stuck_frames", 20)
            ),
    )
    character_worker = CharacterWorker(
        character_frames,
        character_positions,
        stop_event,
        minimap_region_provider=lambda: getattr(
            movement_worker, "_last_minimap_region", None
        ),
    )
    core_workers = [
        capture_worker,
        character_worker,
        movement_worker,
        status_worker,
        *attack_workers,
        shutdown_worker,
        FocusWorker(
            key_sender,
            stop_event,
            automation_active,
            game_focused,
            on_focus_lost=stop_patrol_after_focus_loss,
        ),
    ]
    ui_worker = None if args.no_ui else (
        UiWorker(
            ui_frames,
            stop_event,
            minimap_detector,
            configured_map_name=str(map_profile.get("map_name", "")),
            refresh_ms=args.ui_refresh_ms,
            patrol_controller=patrol_controller,
            diamond_size_tracker=ui_diamond_tracker,
            structure_tracker=structure_tracker,
            map_identity_store=map_identity_store,
            status_worker=status_worker,
            attack_worker=attack_worker,
            movement_worker=movement_worker,
            shutdown_worker=shutdown_worker,
            on_patrol_start=lambda: _start_live_input(
                key_sender, automation_active, prepare_map_session
            ),
            on_patrol_stop=lambda: _stop_live_input(
                key_sender, automation_active
            ),
            on_capture_now=capture_worker.capture_now,
            log_queue=ui_log_handler.messages if ui_log_handler is not None else None,
            automation_active_event=automation_active,
        )
    )

    def request_stop(*_unused: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    for worker in core_workers:
        worker.start()

    def monitor_core_workers() -> None:
        while not stop_event.wait(0.5):
            if any(not worker.is_alive() for worker in core_workers):
                logging.error("a core worker stopped unexpectedly")
                stop_event.set()
                return

    supervisor = threading.Thread(
        target=monitor_core_workers,
        name="supervisor-worker",
        daemon=True,
    )
    supervisor.start()

    logging.info(
        "assistant running (%s); click Start Patrol to enable input; Ctrl+C stops",
        "DRY‑RUN" if args.dry_run else "LIVE INPUT DISARMED",
    )
    try:
        if ui_worker is not None:
            logging.info("opening Maple Assistant Debug UI")
            # Tk must run on Python's main thread on Windows. All automation
            # work remains in its own independent workers.
            ui_worker.run()
            if not stop_event.is_set():
                logging.info("debug UI closed; stopping assistant safely")
                stop_event.set()
        else:
            stop_event.wait()
    finally:
        _stop_live_input(key_sender, automation_active)
        stop_event.set()
        for worker in core_workers:
            worker.join(timeout=5)
        supervisor.join(timeout=1)
        if ui_log_handler is not None:
            logging.getLogger().removeHandler(ui_log_handler)
        logging.getLogger().removeHandler(file_log_handler)
        file_log_handler.close()
        _release_single_instance_mutex(singleton_handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
