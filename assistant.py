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
# The minimap border needs enough working pixels for Canny to close its
# rectangle.  A fixed 200x200 square squash of a large-client crop (375x288
# at 1707x1067) thinned the border until detection always fell back.  The
# detector now fits the crop inside this box preserving aspect ratio, and the
# larger box keeps the border resolvable on large clients.
MINIMAP_ANALYSIS_SIZE = (400, 400)
# The game HUD is FIXED PIXEL: only the playfield viewport scales with the
# window resolution, so HUD regions must be absolute pixels, not normalized
# fractions of the client.  The minimap is anchored top-left; the largest
# observed border is ~240x250, so a 400x400 absolute search region contains
# it at any supported window size while keeping the OpenCV working scale at
# ~1.0 (a huge crop would be downscaled and thin the border).
MINIMAP_FALLBACK_REGION = (0, 0, 400, 400)
# HP/MP bars sit at the BOTTOM of the window: x is fixed from the left edge,
# y is measured from the BOTTOM edge (0.36/0.96/0.53/1 of 2560x1600 gives a
# 435x64 box whose top is 64px above the bottom).  Negative y = from bottom.
STATUS_CAPTURE_REGION = (922, -64, 1357, 0)
SINGLE_INSTANCE_MUTEX_NAME = "Local\\MapleAssistant.Singleton.v1"
# Status-bar widths are FIXED PIXEL values calibrated at the same 2560x1600
# reference (full bar ~192px = 0.075 of 2560; min ~5px = 0.002 of 2560).
# They no longer scale with the client width.
FULL_BAR_CLIENT_FRACTION = 192.0
MIN_BAR_CLIENT_FRACTION = 5.0


class _AnyEvent:
    """Read-only event view that is set when any source event is set."""

    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


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
    capture_preparing_event: Optional[threading.Event] = None,
) -> None:
    """Focus game, capture/calibrate, then arm keyboard-producing workers."""

    logging.info("START PATROL: selecting game window")
    if key_sender.select_window() is False:
        raise OSError("game window selection returned failure")
    if not key_sender.is_game_foreground():
        raise OSError("game window did not become foreground")
    logging.info("START PATROL: game window verified foreground")
    if capture_preparing_event is not None:
        # Stable minimap samples must begin only after foreground verification,
        # but before keyboard input is armed. This temporary capture-only gate
        # prevents UI-overlaid pre-focus frames from entering calibration.
        capture_preparing_event.set()
        logging.info("START PATROL: capture-only calibration enabled")
    try:
        if before_enable is not None:
            before_enable()
        key_sender.enable_input()
        automation_active_event.set()
    finally:
        if capture_preparing_event is not None:
            capture_preparing_event.clear()
    logging.info("START PATROL: automation input armed")


def _stop_live_input(
    key_sender: object,
    automation_active_event: threading.Event,
) -> None:
    automation_active_event.clear()
    key_sender.disable_input()


def _capture_focused_game_frame(
    key_sender: object,
    capture_now: Callable[[], object],
) -> object:
    """Focus the game before a one-off recording capture.

    Recording is available while patrol capture is idle. Focusing first keeps
    the Tk window and its controls out of the game image on machines whose
    capture backend includes overlapping desktop windows.
    """

    logging.info("RECORD POSITION: selecting game window before capture")
    if key_sender.select_window() is False:
        raise OSError("game window selection returned failure")
    if not key_sender.is_game_foreground():
        raise OSError("game window did not become foreground")
    # Allow DWM/compositor state to settle after the foreground transition.
    time.sleep(0.08)
    return capture_now()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Modular MapleStory screen assistant")
    parser.add_argument("--window-title", default="冒险岛怀旧服")
    parser.add_argument("--interval", type=float, default=0.25,
                        help="seconds between minimap captures (default: 0.25)")
    parser.add_argument("--status-interval", type=float, default=0.25,
                        help="seconds between HP/MP captures (default: 0.25)")
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
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).with_name("user_config.json"),
                        help="persistent UI/user configuration file")
    parser.add_argument("--rope-calibration", type=Path, default=None,
                        help="legacy one-run rope calibration override")
    parser.add_argument(
        "--recording-configuration", type=Path,
        default=None,
        help="legacy one-run recorded-route override",
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
    from countdown_worker import CountdownWorker
    from lie_detector_worker import LieDetectorWorker
    from screen_blinker import ScreenBlinker
    from telegram_notifier import TelegramNotifier
    from config_store import get_config_store
    from focus_worker import FocusWorker
    from minimap_detector import (
        MinimapDetector,
        choose_stable_minimap_index,
        minimap_calibration_from_dict,
        minimap_calibration_to_dict,
    )
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
    patrol_preparing = threading.Event()
    movement_frames: queue.Queue = queue.Queue(maxsize=1)
    status_frames: queue.Queue = queue.Queue(maxsize=1)
    ui_frames: queue.Queue = queue.Queue(maxsize=1)
    character_frames: queue.Queue = queue.Queue(maxsize=1)
    lie_detector_frames: queue.Queue = queue.Queue(maxsize=1)
    character_positions: queue.Queue = queue.Queue(maxsize=1)
    subscribers = [
        movement_frames, status_frames, character_frames, lie_detector_frames,
    ]
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
    config_store = get_config_store(args.config)
    calibration = (
        json.loads(args.rope_calibration.read_text(encoding="utf-8"))
        if args.rope_calibration is not None
        else config_store.read_section("rope_calibration")
    )
    map_profile = (
        json.loads(args.recording_configuration.read_text(encoding="utf-8"))
        if args.recording_configuration is not None
        else config_store.read_section("recording")
    )
    profile_path = args.recording_configuration or args.config
    patrol_controller = PatrolController(
        profile_path, map_profile,
        config_store=None if args.recording_configuration is not None
        else config_store,
    )
    configuration_root = profile_path.parent
    structure_reference = (
        configuration_root
        / "recording-assets"
        / "map-structure-reference.png"
    )
    structure_tracker = MapStructureTracker(
        structure_reference, tracking_size=OPENCV_ANALYSIS_SIZE[0]
    )
    map_identity_store = MapIdentityStore(
        configuration_root / "recording-assets" / "map-names"
    )

    def stop_patrol_after_focus_loss() -> None:
        patrol_controller.set_enabled(False)

    rope_profile = map_profile["rope"]
    minimap_region = MINIMAP_FALLBACK_REGION
    status_defaults = StatusConfig()
    status_capture_width = STATUS_CAPTURE_REGION[2] - STATUS_CAPTURE_REGION[0]
    # The HUD is fixed pixel: the status capture is bottom-anchored and the
    # detector's expected bar lengths are FIXED PIXEL values (calibrated at
    # the 2560x1600 reference), not fractions of the current client width.
    status_detector = BarStatusDetector(replace(
        status_defaults,
        status_roi=(0.0, 0.0, 1.0, 1.0),
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
        opencv_size=MINIMAP_ANALYSIS_SIZE,
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
        # Capture the FULL client window.  The status capture region is an
        # ABSOLUTE pixel box anchored to the bottom of the window (the HUD is
        # fixed pixel, only the viewport scales), so it stays correct at any
        # window size.
        status_capture_pixel_region=STATUS_CAPTURE_REGION,
        status_capture_interval=args.status_interval,
        capture_enabled_event=_AnyEvent(game_focused, patrol_preparing),
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
        # A new map can have a different minimap/HUD size. Probe a replacement
        # without first deleting the current verified border: Stop -> Start on
        # the same map must remain restartable even if one contour pass misses.
        logging.info("MINIMAP calibrating border for patrol/map session")
        # The top-left region only bounds OpenCV work; it is not minimap
        # geometry.  Probe independent fresh frames so one false contour
        # cannot seed that search crop as the coordinate frame.
        latest_frame = bus.latest
        try:
            # Get a current game image for map/layer verification. Geometry no
            # longer depends on this capture producing repeatable contours.
            fresh_frame = capture_worker.capture_now(timeout=5.0)
        except TimeoutError:
            if latest_frame is None:
                raise
            fresh_frame = latest_frame
            logging.warning(
                "MINIMAP startup capture timed out; using latest frame "
                "sequence=%d with saved recording border",
                fresh_frame.sequence,
            )

        saved_detection = minimap_calibration_from_dict(
            config_store.read_section("minimap_calibration"),
            fresh_frame.image.size,
        )
        probes = [(fresh_frame, saved_detection)] if saved_detection else []
        if saved_detection is not None:
            # Recording owns border discovery. Patrol only consumes the saved,
            # normalized result, so start is independent of contour stability.
            detection = saved_detection
            minimap_detector.seed_geometry(detection, fresh_frame.image.size)
            calibration_source = "recording-saved"
        else:
            # Compatibility for profiles recorded by older releases. Discover
            # once, but require an actual OpenCV border containing the marker;
            # the next recording persists it and bypasses this path thereafter.
            probe_frames = [fresh_frame]
            after_sequence = fresh_frame.sequence
            for _sample in range(4):
                candidate_frame = bus.wait_for_new(after_sequence, 0.40)
                if candidate_frame is None:
                    break
                probe_frames.append(candidate_frame)
                after_sequence = candidate_frame.sequence
            probes = []
            marker_verified_indices = []
            for candidate_frame in probe_frames:
                probe = MinimapDetector(
                    fallback_region=minimap_region,
                    dedicated_crop=True,
                    opencv_size=MINIMAP_ANALYSIS_SIZE,
                )
                candidate_detection = probe.detect(candidate_frame.image)
                probes.append((candidate_frame, candidate_detection))
                if candidate_detection.source == "opencv":
                    marker_rgb = np.asarray(
                        candidate_frame.image.crop(
                            candidate_detection.analysis_box
                        ).convert("RGB")
                    )
                    if detect_yellow_diamond(marker_rgb) is not None:
                        marker_verified_indices.append(len(probes) - 1)
            chosen_index = choose_stable_minimap_index(
                [candidate for _frame, candidate in probes],
                minimum_repeats=2 if len(probes) >= 2 else 1,
                marker_verified_indices=marker_verified_indices,
            )
            fresh_frame, detection = probes[chosen_index]
            minimap_detector.seed_geometry(detection, fresh_frame.image.size)
            calibration_source = "legacy-detected"
        logging.info(
            "MINIMAP startup border verified source=%s captures=%d | box=%s "
            "| size=%dx%d | confidence=%.3f",
            calibration_source,
            len(probes),
            detection.window_box,
            detection.window_size[0],
            detection.window_size[1],
            detection.confidence,
        )
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

        image_width, image_height = fresh_frame.image.size
        # The status region is absolute bottom-anchored pixels (HUD is fixed
        # pixel), so resolve the negative-from-bottom y against this frame.
        status_left, status_top, status_right, status_bottom = STATUS_CAPTURE_REGION
        if status_top < 0:
            status_top = image_height + status_top
        if status_bottom <= 0:
            status_bottom = image_height + status_bottom
        status_box = (
            round(status_left), round(status_top),
            round(status_right), round(status_bottom),
        )
        # The colours make the startup check easy to read: green is the
        # detected minimap frame, yellow is the marker/patrol analysis area,
        # and blue is the HP/MP status capture area.
        screen_blinker.show_detection_regions(
            fresh_frame.window_rect,
            fresh_frame.image.size,
            (
                ("minimap", detection.window_box, 0x0000FF00),
                ("marker/patrol", detection.analysis_box, 0x0000FFFF),
                ("hp/mp", status_box, 0x00FF0000),
            ),
        )
        logging.info(
            "DETECTION OVERLAY: flashing minimap (green), marker/patrol "
            "(yellow), and HP/MP (blue) regions"
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

    def save_recording_minimap_calibration(snapshot: object) -> None:
        """Publish recording's verified border for independent patrol use."""

        detection = getattr(snapshot, "detection")
        client_size = getattr(snapshot, "client_size")
        value = minimap_calibration_to_dict(detection, client_size)
        config_store.write_section("minimap_calibration", value)
        minimap_detector.seed_geometry(detection, client_size)
        logging.info(
            "MINIMAP recording border saved | box=%s | client=%dx%d",
            detection.window_box, client_size[0], client_size[1],
        )
    screen_blinker = ScreenBlinker(stop_event, enabled=False)
    telegram_notifier = TelegramNotifier(stop_event)
    countdown_worker = CountdownWorker(
        stop_event,
        sound_path=Path(__file__).resolve().parent / "sound" / "beep.mp3",
        enabled=False,
        interval_hours=1.0,
        flash_callback=screen_blinker.request_blink,
        alert_callback=telegram_notifier.notify,
    )
    lie_detector_worker = LieDetectorWorker(
        lie_detector_frames,
        stop_event,
        enabled=False,
        scan_interval=1.0,
        sound_path=Path(__file__).resolve().parent / "sound" / "beep.mp3",
        flash_callback=screen_blinker.request_blink,
        alert_callback=telegram_notifier.notify,
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
                calibration.get("stair_jump_stall_frames", 10)
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
        alert_sound_path=(
            Path(__file__).resolve().parent / "sound" / "beep.mp3"
        ),
        flash_callback=screen_blinker.request_blink,
        alert_callback=telegram_notifier.notify,
    )
    core_workers = [
        capture_worker,
        character_worker,
        movement_worker,
        status_worker,
        *attack_workers,
        shutdown_worker,
        screen_blinker,
        countdown_worker,
        lie_detector_worker,
        telegram_notifier,
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
            character_worker=character_worker,
            shutdown_worker=shutdown_worker,
            countdown_worker=countdown_worker,
            lie_detector_worker=lie_detector_worker,
            screen_blinker=screen_blinker,
            telegram_notifier=telegram_notifier,
            on_patrol_start=lambda: _start_live_input(
                key_sender, automation_active, prepare_map_session,
                patrol_preparing,
            ),
            on_patrol_stop=lambda: _stop_live_input(
                key_sender, automation_active
            ),
            on_capture_now=lambda: _capture_focused_game_frame(
                key_sender, capture_worker.capture_now
            ),
            on_recording_verified=save_recording_minimap_calibration,
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
