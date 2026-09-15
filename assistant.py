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

from minimap_detector import hud_scale_for


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
# The game HUD is FIXED PIXEL above a ~1366px client width: only the
# playfield viewport scales with the window resolution, so HUD regions must
# be absolute pixels, not normalized fractions of the client.  Measured on
# the real client: the top-left region holds the MAP NAME (a ~64px-high
# strip) and BELOW it the real minimap (~380px wide).  The search region
# starts at y=50 (14px of tolerance below the 64px strip) so the map-name
# strip can never be mistaken for the minimap border while small measurement
# errors still fit; the minimap itself varies a little between maps.
#
# BELOW 1366px the game scales the whole HUD down (at 1024x768 everything
# measures ~0.75x: minimap 250x127 -> 187x95, status 370x57 -> 276x33), so
# every fixed-pixel region is scaled per frame by ``hud_scale_for``.
MINIMAP_REGION_TOP = 50
MINIMAP_FALLBACK_REGION = (0, MINIMAP_REGION_TOP, 400, 320)
# HP/MP/EXP bars: with the updated game UI the info bar measures 425x32 px
# at the BOTTOM MIDDLE of the window on the 1080x768 preset, and its centre
# sits 29 px right of the client centre there (confirmed against a live
# client-rect screenshot).  Inside the capture the three bars sit SIDE BY
# SIDE in one vertical band: HP red left, MP blue middle, EXP yellow right.
# The box is anchored to the window bottom and tracks the info bar
# horizontally, so it follows the window size.  The HUD is FIXED PIXEL
# at/above the 1366px reference width - 1366x768 and 1920x1080 share the same
# preset, so they get the identical box - and the whole HUD scales down by
# width below it, so the 1080x768 preset measures 425x32 again (see
# hud_scale_for).  Reference box (1366x768 / 1920x1080): 425 * 1366/1080 =
# 538 px wide, 32 * 1366/1080 = 40 px tall, centre 37 px right of the client
# centre.
STATUS_CAPTURE_WIDTH = 538
STATUS_CAPTURE_HEIGHT = 40
STATUS_CAPTURE_CENTER_OFFSET = 37  # info-bar centre, ref px right of centre
SINGLE_INSTANCE_MUTEX_NAME = "Local\\MapleAssistant.Singleton.v1"
# Status-bar widths are FIXED PIXEL values measured on the real client:
# with the updated UI every bar (HP/MP/EXP) is ~130 px wide at the 1080x768
# preset, i.e. 164 px inside the 538px reference capture; minimum meaningful
# run ~5px.  The fractions stay relative to the reference capture: the
# capture box is scaled by ``hud_scale_for`` and the bars scale by the same
# factor, so the ratios hold at any resolution.
FULL_BAR_CLIENT_FRACTIONS = {"hp": 164.0, "mp": 164.0, "exp": 164.0}
MIN_BAR_CLIENT_FRACTION = 5.0


def status_capture_pixel_box(client_size: tuple[int, int]) -> Box:
    """Return the bottom-anchored, horizontally centered status box.

    The box is the reference status capture (538x40 at the 1366px HUD
    reference; 425x32 on the 1080x768 preset) scaled by ``hud_scale_for``
    (fixed pixel at/above 1366px client width - so 1366x768 and 1920x1080
    return the same box - and shrunk proportionally below that), with its
    centre offset onto the info bar (the new UI draws it right of the client
    centre).
    """

    width, height = client_size
    scale = hud_scale_for(width)
    box_width = max(1, round(STATUS_CAPTURE_WIDTH * scale))
    box_height = max(1, round(STATUS_CAPTURE_HEIGHT * scale))
    center = width // 2 + round(STATUS_CAPTURE_CENTER_OFFSET * scale)
    left = max(0, center - box_width // 2)
    top = max(0, height - box_height)
    return (left, top, left + box_width, top + box_height)


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


def _show_already_running_notice() -> None:
    """Explain a duplicate launch when the hidden launcher has no console."""

    if not hasattr(ctypes, "WinDLL"):
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MessageBoxW(
            None,
            "todo_helper 已经在运行。\n\n请在任务栏中找到现有窗口；如需重启，请先关闭它。",
            "todo_helper",
            0x00000040,  # MB_ICONINFORMATION
        )
    except Exception:
        # A duplicate launch must never crash merely because Windows cannot
        # show the explanatory dialog (for example in a non-interactive test).
        pass


def _start_live_input(
    key_sender: object,
    automation_active_event: threading.Event,
    before_enable: Optional[Callable[[], None]] = None,
    capture_preparing_event: Optional[threading.Event] = None,
    veto_event: Optional[threading.Event] = None,
    veto_reason: str = "",
) -> bool:
    """Focus game, capture/calibrate, then arm keyboard-producing workers.

    Returns False when nothing could be armed (``veto_event`` was set, if one is
    given, or the window/calibration step failed).  The veto is checked before
    calibration and again immediately before arming, because calibration runs on
    Tk's thread for a couple of seconds while the veto can be raised.
    """

    logging.info("START PATROL: selecting game window")
    if key_sender.select_window() is False:
        raise OSError("game window selection returned failure")
    if not key_sender.is_game_foreground():
        raise OSError("game window did not become foreground")
    logging.info("START PATROL: game window verified foreground")
    veto_text = veto_reason or "patrol input is owned elsewhere"
    if veto_event is not None and veto_event.is_set():
        logging.warning(
            "START PATROL vetoed before calibration (%s); input stays disarmed",
            veto_text,
        )
        return False
    if capture_preparing_event is not None:
        # Stable minimap samples must begin only after foreground verification,
        # but before keyboard input is armed. This temporary capture-only gate
        # prevents UI-overlaid pre-focus frames from entering calibration.
        capture_preparing_event.set()
        logging.info("START PATROL: capture-only calibration enabled")
    try:
        if before_enable is not None:
            before_enable()
        if veto_event is not None and veto_event.is_set():
            logging.warning(
                "START PATROL vetoed after calibration (%s); input stays disarmed",
                veto_text,
            )
            return False
        key_sender.enable_input()
        automation_active_event.set()
    finally:
        if capture_preparing_event is not None:
            capture_preparing_event.clear()
    logging.info("START PATROL: automation input armed")
    return True


def _stop_live_input(
    key_sender: object,
    automation_active_event: threading.Event,
    *,
    refocus_before_release: bool = False,
) -> None:
    automation_active_event.clear()
    # Stopping patrol is a safety/lifecycle action. A failed best-effort
    # key-up must be recorded, but it must never tear down the dashboard.
    try:
        key_sender.disable_input(refocus_before_release=refocus_before_release)
    except Exception:
        logging.exception("INPUT RESET failed while stopping patrol")


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


def _clear_previous_log_files() -> None:
    """Startup: wipe every log file from previous runs.

    Runs after the single-instance check so a second instance never clears
    a live session's logs.  error.log, work/assistant.log, yolo logs, demo
    console logs and target_tracker logs all start fresh each launch, which
    keeps any pasted diagnostic output attributable to the current run.
    """

    root = Path(__file__).resolve().parent
    skipped_dirs = {
        ".venv", ".venv-win", "release", ".git",
        "recording-assets", "detect_video", "sound",
    }
    bases = [root]
    try:
        for child in root.iterdir():
            if child.is_dir() and child.name not in skipped_dirs:
                bases.append(child)
    except OSError:
        pass
    removed = 0
    for base in bases:
        try:
            for path in base.rglob("*.log"):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        except OSError:
            pass
    print(f"[startup] cleared {removed} previous log file(s)")


def main() -> int:
    singleton_handle = _acquire_single_instance_mutex()
    if singleton_handle is None:
        _show_already_running_notice()
        return 0
    args = parse_args()
    _clear_previous_log_files()
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(_compact_log_formatter())
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        handlers=[console_handler],
    )
    # error.log 只记录错误：文件接收 ERROR 及以上（含 LOG.exception 与
    # 线程 excepthook 的 logging.critical）。日常 INFO 事件只进运行日志面板
    # 内存与开发控制台，不再全部落盘。
    error_log_handler = RotatingFileHandler(
        Path(__file__).with_name("error.log"),
        maxBytes=2_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    error_log_handler.setLevel(logging.ERROR)
    error_log_handler.setFormatter(_compact_log_formatter())
    logging.getLogger().addHandler(error_log_handler)

    # INFO 追踪单独落盘 work/assistant.log（error.log 保持只含错误）：
    # auto-lie:/lie-detect: 等日常事件仍可离线排查（UI 运行日志只是内存副本）。
    trace_log_path = Path(__file__).resolve().parent / "work" / "assistant.log"
    try:
        trace_log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    trace_log_handler = RotatingFileHandler(
        trace_log_path,
        maxBytes=2_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    trace_log_handler.setLevel(logging.INFO)
    trace_log_handler.setFormatter(_compact_log_formatter())
    logging.getLogger().addHandler(trace_log_handler)

    def _log_uncaught_thread_error(args: threading.ExceptHookArgs) -> None:
        logging.critical(
            "uncaught exception in worker %s",
            getattr(args.thread, "name", "unknown"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _log_uncaught_thread_error

    # Imports are delayed so `--help` works even before dependencies are installed.
    import numpy as np
    from capture_worker import CaptureWorker, FrameBus
    from character_worker import CharacterWorker
    from movement_worker import MovementWorker, _layer_y_band, detect_layer_by_y
    from status_worker import (
        BarStatusDetector,
        StatusConfig,
        StatusWorker,
        WindowKeySender,
    )
    from attack_worker import AttackWorker
    from random_jump_worker import RandomJumpWorker
    from small_step_worker import SmallStepWorker
    from stair_jump_worker import StairJumpWorker
    from hotkey_worker import HotkeyWorker
    from quick_pickup_worker import QuickPickupWorker
    from reconnect_worker import ReconnectWorker
    from trade_worker import TradeWorker
    from motion_arbiter import MotionArbiter
    # TEMPORARILY DISABLED: scheduled shutdown is hidden from the UI.
    # from shutdown_worker import ShutdownWorker
    from countdown_worker import CountdownWorker
    from lie_detector_worker import LieDetectorWorker
    from lie_screenshot_recorder import LieScreenshotRecorder
    from screen_blinker import ScreenBlinker
    from telegram_notifier import TelegramNotifier
    from config_store import get_config_store
    from focus_worker import FocusWorker
    from minimap_detector import (
        MinimapDetector,
        choose_stable_minimap_index,
        hud_scale_for,
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
    # A confirmed stair hop briefly excludes conflicting action input, but it
    # is not a rope climb/return.  Keep its lifetime independent so a parked
    # no-route patrol (action=wait) can still attack and use 小碎步.
    stair_jump_active = threading.Event()
    action_motion_active = _AnyEvent(climbing_active, stair_jump_active)
    dropping_active = threading.Event()
    # Capture cadence event the API lie pass raises for its ~30 fps bursts.
    lie_active = threading.Event()
    moving_active = threading.Event()
    pickup_active = threading.Event()
    automation_active = threading.Event()
    game_focused = threading.Event()
    patrol_preparing = threading.Event()
    trade_capture_active = threading.Event()
    # Briefly owns Left/Right endpoint reversals so fixed attacks cannot
    # swallow the newly pressed opposite direction.
    direction_transition_active = threading.Event()
    movement_frames: queue.Queue = queue.Queue(maxsize=1)
    status_frames: queue.Queue = queue.Queue(maxsize=1)
    ui_frames: queue.Queue = queue.Queue(maxsize=1)
    character_frames: queue.Queue = queue.Queue(maxsize=1)
    lie_detector_frames: queue.Queue = queue.Queue(maxsize=1)
    trade_frames: queue.Queue = queue.Queue(maxsize=1)
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
        / "map-structure-reference.jpg"
    )
    structure_tracker = MapStructureTracker(
        structure_reference, tracking_size=OPENCV_ANALYSIS_SIZE[0]
    )
    map_identity_store = MapIdentityStore(
        configuration_root / "recording-assets" / "map-names"
    )

    def stop_patrol_after_focus_loss() -> None:
        patrol_controller.set_enabled(False)

    def stop_patrol_for_disconnect() -> None:
        """Immediately disarm patrol input after a confirmed disconnect."""

        if not patrol_controller.is_enabled():
            return
        patrol_controller.set_enabled(False)
        _stop_live_input(key_sender, automation_active)
        logging.warning("PATROL STOPPED: disconnect alert triggered")

    rope_profile = map_profile["rope"]
    minimap_region = MINIMAP_FALLBACK_REGION
    status_defaults = StatusConfig()
    # The HUD is fixed pixel: the status capture is bottom-anchored and the
    # detector's expected bar lengths are FIXED PIXEL values (measured on the
    # real client inside the 357px-wide capture), not fractions of the
    # current client width.
    status_capture_width = STATUS_CAPTURE_WIDTH
    status_detector = BarStatusDetector(replace(
        status_defaults,
        status_roi=(0.0, 0.0, 1.0, 1.0),
        full_bar_width_fractions={
            name: width / status_capture_width
            for name, width in FULL_BAR_CLIENT_FRACTIONS.items()
        },
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
        # Capture the FULL client window.  The status capture box is computed
        # per capture from the current client size: bottom-anchored, slightly
        # left of the horizontal center (the HUD is fixed pixel, only the
        # viewport scales).
        status_capture_box_provider=status_capture_pixel_box,
        status_capture_interval=args.status_interval,
        capture_enabled_event=_AnyEvent(
            game_focused, patrol_preparing, trade_capture_active
        ),
        fast_capture_event=dropping_active,
        fast_interval=0.10,
        # Lie pass: ~30 fps while a pass is running, so a pass is fed at the same
        # rate the game shows.  Nothing raises it today (the local pass is gone);
        # the API pass keeps the wiring for its bursts.
        lie_capture_event=lie_active,
        lie_interval=1.0 / 30.0,
        # ==== ADDED pass debug flag into capture worker ====
        debug_draw_regions=args.debug_capture_regions,
        debug_minimap_fallback=MINIMAP_FALLBACK_REGION, # <------ ADD THIS LINE
    )

    def prepare_map_session(*, stationary_reanchor: bool = True) -> None:
        """Verify the recorded map name and re-anchor transient world Y.

        ``stationary_reanchor`` is False for an AUTOMATIC patrol resume: 站桩攻击
        then keeps the standing position that the user's own manual Start Patrol
        recorded instead of re-recording the current (possibly displaced) marker.

        The old ``abort_event`` hook (a lie takeover abandoning this session) is
        gone with the local lie pass.
        """

        stationary_attack = bool(
            getattr(movement_worker, "stationary_attack_enabled", False)
        )

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
                # Accept an OpenCV contour OR the fixed HUD region when the
                # yellow marker is found inside the analysis box; the fixed
                # region is marker-verified geometry on the fixed-pixel HUD.
                marker_rgb = np.asarray(
                    candidate_frame.image.crop(
                        candidate_detection.analysis_box
                    ).convert("RGB")
                )
                if detect_yellow_diamond(marker_rgb) is not None:
                    if candidate_detection.source == "fallback":
                        candidate_detection = replace(
                            candidate_detection,
                            source="fixed-region",
                            confidence=1.0,
                        )
                        probes[-1] = (candidate_frame, candidate_detection)
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
        if stationary_attack:
            # Stationary Attack keeps one *temporary* anchor per standing spot.
            # A MANUAL Start Patrol (按钮 or Ctrl+`) records the character's
            # current position; an AUTOMATIC resume (the auto-lie pass calls
            # this with ``stationary_reanchor=False``) never re-records it, so
            # a knock-down during a pause cannot move the spot.  Recorded map
            # layers, map identity and world-Y anchors are never read here;
            # those recordings stay untouched for normal patrol mode.
            stationary_anchor = getattr(
                movement_worker, "prepare_stationary_attack_anchor", None
            )
            if not callable(stationary_anchor):
                raise OSError(
                    "stationary attack is not supported by this movement worker"
                )
            marker = None
            if stationary_reanchor:
                analysis_rgb = np.asarray(
                    fresh_frame.image.crop(detection.analysis_box).convert("RGB")
                )
                marker = detect_yellow_diamond(analysis_rgb)
            if not stationary_anchor(marker, allow_reanchor=stationary_reanchor):
                raise OSError(
                    "yellow character marker was not detected for stationary attack"
                )
            logging.info(
                "STATIONARY ATTACK startup: %s; recorded layers were not checked",
                "temporary current-position anchor saved"
                if stationary_reanchor
                else "automatic resume kept the recorded standing position",
            )
            return
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
        # The status region is bottom-anchored, slightly left of center (HUD
        # is fixed pixel), computed from this frame's client size.
        status_box = status_capture_pixel_box(fresh_frame.image.size)
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
        if not snapshot.route_order:
            # Nothing recorded: stand-still + attack mode. Skip floor/world-Y
            # setup because there is no patrol route to select.
            logging.info(
                "MAP SESSION no patrol route recorded; standing still + attack"
            )
            return
        layer_bands = []
        for layer_name in snapshot.route_order:
            layer = snapshot.layers.get(layer_name, {})
            if not isinstance(layer, dict):
                continue
            band = _layer_y_band(
                layer, float(layer.get("y_tolerance", 0.020000))
            )
            if band is not None:
                layer_bands.append((layer_name, band))
                logging.info(
                    "LAYER BAND: %s y=(%.6f, %.6f)",
                    layer_name, band[0], band[1],
                )
        screen_blinker.show_layer_bands(
            fresh_frame.window_rect,
            fresh_frame.image.size,
            detection.analysis_box,
            layer_bands,
            wait_until_hidden=True,
        )
        # The overlay is deliberately gone before input is armed. Publish one
        # clean post-overlay frame for screen-capture-based machines so the
        # movement worker cannot consume a colour-tinted minimap.
        try:
            capture_worker.capture_now(timeout=2.0)
        except TimeoutError:
            logging.warning(
                "LAYER BAND OVERLAY: clean post-overlay capture timed out"
            )
        detected_name = (
            detect_layer_by_y(marker.y, snapshot.layers)
            if marker is not None else None
        )
        if marker is None:
            raise OSError(
                "yellow character marker was not detected during patrol startup"
            )
        if detected_name is None:
            raise OSError(
                f"character marker Y={marker.y:.6f} does not match any "
                "recorded layer; record the current map layers again"
            )
        anchor_name = str(detected_name)
        anchor_layer = snapshot.layers.get(anchor_name, {})
        anchor_world_y = anchor_layer.get("layer_world_y")
        if anchor_world_y is None:
            raise OSError(
                f"{anchor_name or 'first layer'} has no recorded world Y; "
                "record this map once"
            )
        structure_tracker.start_session(float(anchor_world_y))
        movement_worker.prepare_patrol_start(anchor_name)
        logging.info(
            "MAP SESSION detected %s from marker_y=%s; re-anchoring "
            "world_y=%.6f",
            anchor_name,
            f"{marker.y:.6f}" if marker is not None else "unknown",
            float(anchor_world_y),
        )

    def start_patrol_input() -> bool:
        """Manual Start Patrol (按钮 / Ctrl+`): focus, calibrate, arm input.

        """

        armed = _start_live_input(
            key_sender, automation_active,
            prepare_map_session,
            patrol_preparing,
        )
        return armed

    attack_workers = []
    # Jump/buff motion keys are executed one at a time by the motion arbiter
    # (0.9s jump window / 0.6s buff window).  Fixed attack defers while the
    # arbiter is busy, so a jump or buff tap never lands inside action
    # motion and gets swallowed by the game.
    motion_arbiter = MotionArbiter(
        key_sender,
        stop_event,
        climbing_active_event=action_motion_active,
        automation_active_event=automation_active,
    )
    # The fixed-rate attack worker always exists so the UI can toggle it
    # live (Fixed Attack panel).  Without --enable-attack it starts disabled
    # and only waits; the panel flips ``enabled`` when the mode is selected.
    attack_worker = AttackWorker(
        key_sender,
        stop_event,
        args.attack_interval,
        climbing_active_event=action_motion_active,
        automation_active_event=automation_active,
        motion_arbiter=motion_arbiter,
        direction_transition_event=direction_transition_active,
    )
    attack_worker.enabled = bool(args.enable_attack)
    attack_workers.append(attack_worker)
    random_jump_worker = RandomJumpWorker(
        key_sender,
        stop_event,
        climbing_active_event=action_motion_active,
        automation_active_event=automation_active,
        motion_arbiter=motion_arbiter,
    )
    # TEMPORARILY DISABLED together with its hidden UI controls.
    # shutdown_worker = ShutdownWorker(...)
    shutdown_worker = None
    hotkey_actions: "queue.Queue[str]" = queue.Queue(maxsize=32)
    # Global hotkeys are UI actions: they are only useful (and only safe) when
    # the interactive UI drains the action queue.  A headless/--no-ui instance
    # (a dry-run smoke test, or a leftover process) must NOT claim the Ctrl
    # chords, otherwise it registers them OS-wide and silently swallows them
    # with nothing to consume the queue - exactly how a stale --no-ui run made
    # Ctrl+` appear dead.
    hotkey_worker = None if args.no_ui else HotkeyWorker(
        stop_event, hotkey_actions
    )
    quick_pickup_results: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=8)
    quick_pickup_worker = QuickPickupWorker(
        key_sender,
        stop_event,
        quick_pickup_results,
        patrol_running=patrol_controller.is_enabled,
    )

    # 自动重连: armed from the Additional Functions panel; the 掉线 event is its trigger and
    # the login page's base colour (screenshots/login_page_target.jpg) its confirmation
    # (see reconnect_worker.py).
    reconnect_results: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=16)
    reconnect_worker = ReconnectWorker(
        key_sender,
        stop_event,
        reconnect_results,
        window_title=args.window_title,
        dry_run=args.dry_run,
    )

    # 测试api (附加功能 panel): pick a video, play it at the API's 5 fps and upload each frame's ROI
    # to the RoiTrack backend (see api_lie_video.py).  A new worker per press, because a thread can
    # only be started once.  api_lie_test.py (the older single-frame screen drill) has no panel
    # button any more; it stays as an offline tool for work/ scripts and the test suite.
    api_test_results: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=256)

    def make_api_test_video_worker(*, video, results, display, seconds=30.0, key="",
                                   fps=5.0):
        """The 测试api drill on a chosen video: play it at 5 fps and aim inside the picture only.

        A new worker per press (a thread cannot restart).  ``display`` carries the frames the UI
        thread must draw; the worker reads back ``worker.image_rect`` (the picture rectangle) to
        confine the mouse to the video area.  The key is not a panel setting any more: it ships
        with the application (LIE_PRODUCT_KEY -> autolie_api/key_secret.txt), and with no key at all
        the local mimic is used, so the button is always safe to press.
        """

        from api_lie_video import VideoDrillWorker

        return VideoDrillWorker(
            video=video,
            results=results,
            display=display,
            stop_event=stop_event,
            key=key,
            seconds=seconds,
            fps=fps,
            aim_enabled=True,
            window_title=args.window_title,
        )

    def _on_disconnect_event() -> None:
        """掉线: the recorder grabs its diagnostic frames, then 自动重连 checks/login."""

        try:
            screenshot_recorder.on_disconnect()
        except Exception:
            logging.warning("disconnect screenshot recorder failed", exc_info=True)
        try:
            reconnect_worker.notify_disconnect()
        except Exception:
            logging.warning("auto reconnect notify failed", exc_info=True)

    def save_recording_minimap_calibration(snapshot: object) -> None:
        """Publish recording's verified border for independent patrol use."""

        detection = getattr(snapshot, "detection")
        # When OpenCV could not close a border contour, the marker-verified
        # fixed HUD region (map-name strip above the measured minimap area)
        # is the calibration: the marker was found inside it, so its
        # absolute-pixel geometry is correct for the fixed-pixel HUD.
        if detection.source == "fallback":
            detection = replace(
                detection, source="fixed-region", confidence=1.0
            )
        client_size = getattr(snapshot, "client_size")
        value = minimap_calibration_to_dict(detection, client_size)
        config_store.write_section("minimap_calibration", value)
        minimap_detector.seed_geometry(detection, client_size)
        logging.info(
            "MINIMAP recording border saved source=%s | box=%s | client=%dx%d",
            detection.source, detection.window_box,
            client_size[0], client_size[1],
        )
    screen_blinker = ScreenBlinker(stop_event, enabled=False)
    telegram_notifier = TelegramNotifier(stop_event)
    # 独立的多事件截图录制器（诊断用，与测谎流程解耦）：测谎 / 掉线 / 循环
    # 事件触发后连续抓取完整游戏窗口约 20 秒，按帧存入 screenshots/<kind>_*/。
    screenshot_recorder = LieScreenshotRecorder(args.window_title)
    countdown_worker = CountdownWorker(
        stop_event,
        sound_path=Path(__file__).resolve().parent / "sound" / "dingdong.mp3",
        enabled=False,
        interval_hours=1.0,
        flash_callback=screen_blinker.request_blink,
        alert_callback=telegram_notifier.notify,
        event_callback=screenshot_recorder.on_countdown,
    )
    lie_detector_worker = LieDetectorWorker(
        lie_detector_frames,
        stop_event,
        enabled=False,
        scan_interval=1.0,
        sound_path=Path(__file__).resolve().parent / "sound" / "dingdong.mp3",
        flash_callback=screen_blinker.request_blink,
        alert_callback=telegram_notifier.notify,
    )
    lie_detector_worker.add_lie_seen_callback(
        screenshot_recorder.on_lie_seen
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
        motion_arbiter=motion_arbiter,
    )
    trade_worker = TradeWorker(
        trade_frames,
        stop_event,
        trade_capture_active,
        key_sender,
        args.window_title,
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
            climb_lateral_cycles_reset=int(
                calibration.get("climb_lateral_cycles_reset", 3)
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
            direction_transition_event=direction_transition_active,
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
            # Landing reconciliation (world-Y settle + re-anchor to the true
            # layer after a knock-down) and the world-Y drift watchdog.
            fall_settle_min_frames=int(
                calibration.get("fall_settle_min_frames", 3)
            ),
            fall_settle_epsilon=float(
                calibration.get("fall_settle_epsilon", 0.15)
            ),
            fall_settle_max_seconds=float(
                calibration.get("fall_settle_max_seconds", 1.2)
            ),
            world_drift_check_interval_seconds=float(
                calibration.get("world_drift_check_interval_seconds", 2.0)
            ),
            world_drift_reanchor_threshold=float(
                calibration.get("world_drift_reanchor_threshold", 0.35)
            ),
            rescue_cycle_limit=int(calibration.get("rescue_cycle_limit", 3)),
            rescue_probe_attack_block_seconds=float(
                calibration.get("rescue_probe_attack_block_seconds", 2.0)
            ),
            rescue_probe_hold_seconds=float(
                calibration.get("rescue_probe_hold_seconds", 0.7)
            ),
            rescue_probe_settle_seconds=float(
                calibration.get("rescue_probe_settle_seconds", 0.2)
            ),
            rescue_probe_move_threshold=float(
                calibration.get("rescue_probe_move_threshold", 0.006)
            ),
            drop_chord_hold_seconds=float(
                calibration.get("drop_chord_hold_seconds", 0.10)
            ),
            drop_retry_seconds=float(calibration.get("drop_retry_seconds", 1.5)),
            minimap_detector=minimap_detector,
            patrol_controller=patrol_controller,
            diamond_size_tracker=movement_diamond_tracker,
            structure_tracker=structure_tracker,
            automation_active_event=automation_active,
            motion_arbiter=motion_arbiter,
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
                calibration.get("stair_jump_stall_frames", 7)
            ),
            patrol_start_grace_seconds=float(
                calibration.get("patrol_start_grace_seconds", 3.0)
            ),
            stair_jump_attempts_max=int(
                calibration.get("stair_jump_attempts_max", 1)
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
    motion_arbiter.set_micro_step_callback(movement_worker.perform_micro_step)
    stair_jump_worker = StairJumpWorker(
        stop_event,
        automation_active_event=automation_active,
        action_active_event=stair_jump_active,
        motion_arbiter=motion_arbiter,
        execute_callback=movement_worker.perform_stair_jump,
    )
    movement_worker.set_stair_jump_worker(stair_jump_worker)
    motion_arbiter.set_buff_callback(movement_worker.perform_arbiter_buff)
    motion_arbiter.set_motion_gate_callback(
        movement_worker.motion_arbiter_motion_allowed
    )
    small_step_worker = SmallStepWorker(
        stop_event,
        automation_active_event=automation_active,
        climbing_active_event=action_motion_active,
        motion_arbiter=motion_arbiter,
    )
    character_worker = CharacterWorker(
        character_frames,
        character_positions,
        stop_event,
        minimap_region_provider=lambda: getattr(
            movement_worker, "_last_minimap_region", None
        ),
        alert_sound_path=(
            Path(__file__).resolve().parent / "sound" / "dingdong.mp3"
        ),
        flash_callback=screen_blinker.request_blink,
        alert_callback=telegram_notifier.notify,
        on_disconnect=stop_patrol_for_disconnect,
        disconnect_event_callback=_on_disconnect_event,
    )
    focus_worker = FocusWorker(
        key_sender,
        stop_event,
        automation_active,
        game_focused,
        on_focus_lost=stop_patrol_after_focus_loss,
        # A lie pass disarms input but is fed by the same capture: keep the
        # foreground gate honest for the whole pass.
        lie_pass_event=lie_active,
    )
    core_workers = [
        capture_worker,
        character_worker,
        movement_worker,
        status_worker,
        motion_arbiter,
        *attack_workers,
        random_jump_worker,
        small_step_worker,
        stair_jump_worker,
        *([hotkey_worker] if hotkey_worker is not None else []),
        quick_pickup_worker,
        reconnect_worker,
        trade_worker,
        screen_blinker,
        countdown_worker,
        lie_detector_worker,
        telegram_notifier,
        focus_worker,
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
            random_jump_worker=random_jump_worker,
            small_step_worker=small_step_worker,
            hotkey_queue=hotkey_actions,
            hotkey_worker=hotkey_worker,
            quick_pickup_worker=quick_pickup_worker,
            quick_pickup_results=quick_pickup_results,
            reconnect_worker=reconnect_worker,
            reconnect_results=reconnect_results,
            api_test_video_factory=make_api_test_video_worker,
            api_test_results=api_test_results,
            trade_worker=trade_worker,
            movement_worker=movement_worker,
            character_worker=character_worker,
            shutdown_worker=shutdown_worker,
            countdown_worker=countdown_worker,
            lie_detector_worker=lie_detector_worker,
            screen_blinker=screen_blinker,
            telegram_notifier=telegram_notifier,
            on_patrol_start=start_patrol_input,
            on_patrol_stop=lambda: _stop_live_input(
                key_sender, automation_active, refocus_before_release=True,
            ),
            on_capture_now=lambda: _capture_focused_game_frame(
                key_sender, capture_worker.capture_now
            ),
            on_recording_verified=save_recording_minimap_calibration,
            log_queue=ui_log_handler.messages if ui_log_handler is not None else None,
            ui_log_handler=ui_log_handler,
            user_config_path=str(config_store.user_path),
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
        logging.getLogger().removeHandler(error_log_handler)
        error_log_handler.close()
        logging.getLogger().removeHandler(trace_log_handler)
        trace_log_handler.close()
        _release_single_instance_mutex(singleton_handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
