"""Integrates capture, movement, and status workers."""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import replace
import json
import logging
import math
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
            "MapleAssistant 已经在运行。\n\n请在任务栏中找到现有窗口；如需重启，请先关闭它。",
            "MapleAssistant",
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
    parser.add_argument("--interval", type=float, default=0.20,
                        help="seconds between shared game captures (default: 0.20 / 5 fps)")
    parser.add_argument("--status-interval", type=float, default=0.20,
                        help="seconds between status analysis samples (default: 0.20 / 5 fps)")
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
        # The launcher replaces an existing copy before starting a fresh one.
        # A short process-exit race can still reach this guard; exit silently
        # instead of showing a misleading "close the old instance" prompt.
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
    from capture_worker import (
        LIE_WATCH_INTERVAL_SECONDS,
        CaptureWorker,
        FrameBus,
        ParkedWatchCapture,
        WatchFeed,
    )
    from character_worker import CharacterWorker
    from movement_worker import (
        MovementWorker,
        _coherent_observed_world_points,
        _layer_world_y_band,
        _layer_y_band,
        detect_layer_by_y,
    )
    from status_worker import (
        BarStatusDetector,
        StatusConfig,
        StatusWorker,
        WindowKeySender,
    )
    from attack_worker import AttackWorker
    from small_step_worker import SmallStepWorker
    from stair_jump_worker import StairJumpWorker
    from hotkey_worker import HotkeyWorker
    from quick_pickup_worker import QuickPickupWorker
    from reconnect_worker import ReconnectWorker
    from trade_worker import TradeWorker
    from workflow_cancel_worker import WorkflowCancelWorker
    from motion_arbiter import MotionArbiter
    # TEMPORARILY DISABLED: scheduled shutdown is hidden from the UI.
    # from shutdown_worker import ShutdownWorker
    from countdown_worker import CountdownWorker
    from lie_detector_worker import LieDetectorWorker
    from lie_screenshot_recorder import LieScreenshotRecorder
    from screen_blinker import ScreenBlinker
    from telegram_notifier import TelegramNotifier
    from versioning import version_label
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
    # The API lie pass joins the normal shared 5 fps capture stream while it owns the cursor.
    lie_active = threading.Event()
    moving_active = threading.Event()
    pickup_active = threading.Event()
    automation_active = threading.Event()
    # Set while the 自动重连 owns the machine: the focus gate must not re-arm the automation then
    # (otherwise attack/jump start on the login page - the character is not in game yet).
    reconnect_active = threading.Event()
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
        # Ctrl+Q waits for the trade dialog's small presence area to change.
        # Its worker arms the shared capture only during that workflow, but it
        # must still be a FrameBus subscriber or it will never receive a frame.
        trade_frames,
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

    # Select a WebSocket endpoint once while the dashboard is opening.  A
    # probe does not authenticate or bill (protocol 2.7.0 §4.3), so the lie
    # event can later connect straight to the cached endpoint instead of
    # serially probing every port.  Do NOT pre-handshake here: the protocol
    # drops an idle authenticated connection after 10 seconds without frames.
    auto_lie_endpoint_lock = threading.Lock()
    auto_lie_endpoint: dict[str, object] = {"value": None, "error": ""}

    def warm_auto_lie_endpoint() -> None:
        try:
            from autolie_api.connect import choose_endpoint, probe_ws_endpoints

            best = choose_endpoint(probe_ws_endpoints())
            if best is None:
                raise RuntimeError("no WebSocket endpoint answered the startup probe")
            with auto_lie_endpoint_lock:
                auto_lie_endpoint["value"] = best
                auto_lie_endpoint["error"] = ""
            logging.info(
                "AUTO LIE preflight ready: cached WebSocket endpoint %s (%.0f ms)",
                best.endpoint, best.rtt_ms,
            )
        except Exception as exc:
            with auto_lie_endpoint_lock:
                auto_lie_endpoint["error"] = f"{type(exc).__name__}: {exc}"
            logging.warning("AUTO LIE preflight failed; event will retry discovery: %s", exc)

    threading.Thread(
        target=warm_auto_lie_endpoint,
        name="auto-lie-preflight",
        daemon=True,
    ).start()

    def open_prepared_auto_lie_backend(log):
        """Open one fresh formal session against the UI-start cached endpoint.

        The HTTP health check is intentionally excluded: it is diagnostic-only
        in protocol 2.7.0 and must never delay a live lie event.
        """

        from autolie_api.connect import open_backend
        from autolie_api.endpoints import ws_endpoints
        from autolie_api.key_store import load_product_key, mask_key
        from autolie_api.ws_client import RoiTrackWsClient

        key, source = load_product_key("")
        if not key:
            # The local mimic is created directly by the normal helper and
            # has no remote probe/health path to avoid.
            session = open_backend(
                key="", transport="base64", frame_standard=5.0,
                health=False, client_info="maple_assistant_auto_lie",
            )
            return session.client, session.note, session.mimic
        with auto_lie_endpoint_lock:
            endpoint = auto_lie_endpoint.get("value")
            preflight_error = str(auto_lie_endpoint.get("error") or "")
        if endpoint is None:
            choices = ws_endpoints()
            if not choices:
                raise RuntimeError("no configured WebSocket endpoint")
            endpoint = choices[0]
            logging.warning(
                "AUTO LIE preflight is unavailable (%s); trying configured endpoint %s directly",
                preflight_error or "still running", endpoint.url,
            )
        endpoint_url = str(getattr(endpoint, "endpoint", getattr(endpoint, "url", "")))
        client = RoiTrackWsClient(endpoint.host, endpoint.port, timeout=5.0)
        try:
            started = time.perf_counter()
            client.connect()
            ack = client.handshake(
                key, frame_standard=5, image_transport="bgr_jpeg90_base64",
                client_info="maple_assistant_auto_lie",
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
        except Exception as exc:
            client.close()
            if log is not None:
                log.connection(
                    "cached endpoint handshake failed; falling back to discovery",
                    endpoint=endpoint_url, error=str(exc),
                )
            # A stale startup probe must not make the live pass unavailable.
            # This fallback still omits the HTTP health check.
            session = open_backend(
                key=key, transport="base64", frame_standard=5.0,
                health=False, client_info="maple_assistant_auto_lie",
            )
            return session.client, session.note, session.mimic
        if log is not None:
            log.connection(
                "cached endpoint handshake_ack", endpoint=endpoint_url,
                auth=ack.get("auth"), quota_left=ack.get("quota_left"),
                connect_ms=round(elapsed_ms),
            )
        note = (
            f"真实后端 {endpoint_url} 密钥来自{source} "
            f"(quota_left={ack.get('quota_left')}；UI 启动预探测)"
        )
        logging.info(
            "AUTO LIE ready: direct handshake %s in %.0f ms (key %s)",
            endpoint_url, elapsed_ms, mask_key(key),
        )
        return client, note, None

    calibration = (
        json.loads(args.rope_calibration.read_text(encoding="utf-8"))
        if args.rope_calibration is not None
        else config_store.read_section("rope_calibration")
    )

    def five_fps_frames(value: object, minimum: int = 1) -> int:
        """Preserve a 4-fps frame-based delay after moving the shared stream to 5 fps."""

        return max(int(minimum), int(math.ceil(float(value) * 1.25)))
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
        # Focus loss is also a real patrol stop. Merely clearing the route
        # flag left attack/movement workers armed until some later UI action,
        # which could preserve a held direction after the game returned.
        disarm_patrol_runtime()

    def stop_patrol_for_disconnect() -> None:
        """Immediately disarm patrol input after a confirmed disconnect.

        Nothing is remembered here: whether the patrol comes back after the reconnect is decided by
        the operator's own patrol INTENT in the UI (开始巡逻 / 停止巡逻).  This function only stops.
        """

        if not patrol_controller.is_enabled():
            return
        patrol_controller.set_enabled(False)
        disarm_patrol_runtime()
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

    # The gate the shared capture runs on.  It is ALSO what tells the parked 测谎 watch to stand down: if
    # this is set, the detector is already being fed by the shared capture and a second one is waste.
    shared_capture_gate = _AnyEvent(
        game_focused, patrol_preparing, trade_capture_active, lie_active
    )
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
        capture_enabled_event=shared_capture_gate,
        fast_capture_event=dropping_active,
        # Every consumer stays on the same 5 fps source, including falls and auto-lie.
        fast_interval=float(args.interval),
        lie_capture_event=lie_active,
        lie_interval=float(args.interval),
        # ==== ADDED pass debug flag into capture worker ====
        debug_draw_regions=args.debug_capture_regions,
        debug_minimap_fallback=MINIMAP_FALLBACK_REGION, # <------ ADD THIS LINE
    )

    def prepare_map_session(*, stationary_reanchor: bool = True, show_overlays: bool = True,
                            show_startup_marker: bool = False,
                            require_layer: bool = True) -> None:
        """Verify the recorded map name and re-anchor transient world Y.

        ``stationary_reanchor`` is False for an AUTOMATIC patrol resume: 站桩攻击
        then keeps the standing position that the user's own manual Start Patrol
        recorded instead of re-recording the current (possibly displaced) marker.

        ``show_overlays`` is False for the AUTOMATIC restart after an auto-reconnect: the detection
        and layer-band overlays are a calibration aid for the operator pressing 开始巡逻, and the
        band overlay waits until it is hidden - a reconnect must not block on that.

        ``require_layer`` is False for the same automatic restart: the character may be OFF the patrol
        route after a reconnect, and then the patrol still has to start (from the route's base layer)
        so the movement worker's out-of-route return logic can bring it back.

        ``show_startup_marker`` draws only the manual-start geometry and a
        short marker crosshair.  It deliberately does not draw layer bands or
        wait for a clean overlay-free capture, so the patrol does not freeze.

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
            # A manual Start Patrol in 站桩攻击 must anchor to the marker at
            # that exact moment.  ``bus.latest`` can be a parked/startup frame
            # and would retain the first temporary position for the whole
            # assistant session, so this one-shot capture is authoritative.
            fresh_frame = capture_worker.capture_now(timeout=5.0)
        except TimeoutError:
            if latest_frame is None:
                raise
            if (time.monotonic() - latest_frame.captured_at) > 1.0:
                raise OSError(
                    "could not capture a current minimap frame for stationary attack"
                )
            fresh_frame = latest_frame
            logging.warning(
                "MINIMAP startup capture timed out; using a recent frame "
                "sequence=%d with saved recording border",
                fresh_frame.sequence,
            )

        saved_calibration = config_store.read_section("minimap_calibration")
        saved_detection = minimap_calibration_from_dict(
            saved_calibration,
            fresh_frame.image.size,
        )
        if stationary_attack and saved_detection is not None:
            # 站桩攻击 has no route recording to preserve, so its temporary
            # anchor must never inherit a previous map's persisted minimap
            # rectangle.  A stale 67px box followed by the live 86px box
            # changes normalized X and immediately creates a false recovery
            # walk.  Probe the actual current minimap before anchoring.
            logging.info(
                "STATIONARY ATTACK: ignoring saved minimap calibration "
                "and probing the current map border before recording anchor"
            )
            saved_detection = None
        if saved_calibration and saved_detection is None:
            logging.warning(
                "MINIMAP saved calibration ignored: it is not a measured minimap border; "
                "a fresh OpenCV border is required"
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
                # A marker confirms that a measured contour contains the
                # player.  The broad fallback is only a search area and is
                # never promoted to minimap geometry.
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
        # Keep the stationary-start visual diagnosis identical to a normal
        # patrol start: minimap, marker-analysis and HP/MP regions all show
        # their actual current capture geometry.
        status_box = status_capture_pixel_box(fresh_frame.image.size)
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
                # Match normal manual recording: use fresh verified captures,
                # not the initial startup frame alone.  A one-frame OpenCV
                # flicker at Start Patrol previously became the stationary
                # anchor and immediately caused a correction walk.
                marker_samples = []
                for sample_index in range(3):
                    candidate_frame = fresh_frame
                    if sample_index:
                        try:
                            candidate_frame = capture_worker.capture_now(timeout=2.0)
                        except TimeoutError:
                            logging.warning(
                                "STATIONARY ATTACK anchor sample %d timed out",
                                sample_index + 1,
                            )
                            continue
                    candidate_rgb = np.asarray(
                        candidate_frame.image.crop(detection.analysis_box).convert("RGB")
                    )
                    candidate_marker = detect_yellow_diamond(candidate_rgb)
                    if candidate_marker is not None:
                        marker_samples.append((candidate_frame, candidate_marker))
                if marker_samples:
                    # Coordinate flicker is normally a one-frame outlier.
                    # Median X/Y preserves the normal recording coordinate
                    # system while rejecting that outlier.
                    marker = marker_samples[len(marker_samples) // 2][1]
                    marker_x = float(np.median([item[1].x for item in marker_samples]))
                    marker_y = float(np.median([item[1].y for item in marker_samples]))
                    marker = replace(marker, x=marker_x, y=marker_y)
                    fresh_frame = marker_samples[-1][0]
            if not stationary_anchor(marker, allow_reanchor=stationary_reanchor):
                raise OSError(
                    "yellow character marker was not detected for stationary attack"
                )
            # Manual 站桩攻击 starts deliberately request show_overlays=False
            # to avoid the normal patrol's long calibration pause, but still
            # need the full visual geometry check requested by the operator.
            if show_overlays or stationary_reanchor:
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
                    "STATIONARY ATTACK DETECTION OVERLAY: minimap (green), "
                    "marker/patrol (yellow), HP/MP (blue), fixed point crosshair"
                )
            # Show the temporary 站桩攻击 anchor once, directly on its minimap
            # marker.  This is diagnostic only: the click-through crosshair
            # never participates in patrol movement or the fixed-position
            # calculation, and therefore cannot shift the saved anchor.
            if stationary_reanchor and marker is not None:
                try:
                    left, top, right, bottom = fresh_frame.window_rect
                    image_width, image_height = fresh_frame.image.size
                    analysis_left, analysis_top, analysis_right, analysis_bottom = (
                        detection.analysis_box
                    )
                    scale_x = (right - left) / max(1, image_width)
                    scale_y = (bottom - top) / max(1, image_height)
                    screen_blinker.show_aim_marker(
                        round(left + (analysis_left + marker.x * (analysis_right - analysis_left)) * scale_x),
                        round(top + (analysis_top + marker.y * (analysis_bottom - analysis_top)) * scale_y),
                        ttl_seconds=2.5,
                        size=11,
                    )
                    logging.info(
                        "STATIONARY ATTACK marker: fixed position crosshair drawn x=%.6f y=%.6f",
                        marker.x, marker.y,
                    )
                except Exception:
                    logging.warning("STATIONARY ATTACK marker overlay failed", exc_info=True)
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
        if show_overlays or show_startup_marker:
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
        if show_startup_marker and marker is not None:
            try:
                left, top, right, bottom = fresh_frame.window_rect
                analysis_left, analysis_top, analysis_right, analysis_bottom = (
                    detection.analysis_box
                )
                image_width, image_height = fresh_frame.image.size
                scale_x = (right - left) / max(1, image_width)
                scale_y = (bottom - top) / max(1, image_height)
                screen_blinker.show_aim_marker(
                    round(left + (analysis_left + marker.x * (analysis_right - analysis_left)) * scale_x),
                    round(top + (analysis_top + marker.y * (analysis_bottom - analysis_top)) * scale_y),
                    ttl_seconds=2.5,
                    size=11,
                )
                logging.info(
                    "PATROL ATTACK marker: startup crosshair drawn x=%.6f y=%.6f",
                    marker.x, marker.y,
                )
            except Exception:
                logging.warning("PATROL ATTACK marker overlay failed", exc_info=True)
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
        jump_point_overlay: list[tuple[float, float]] = []
        for layer_name in snapshot.route_order:
            layer = snapshot.layers.get(layer_name, {})
            if not isinstance(layer, dict):
                continue
            for point in layer.get("jump_points", []):
                if not isinstance(point, dict):
                    continue
                try:
                    jump_point_overlay.append((
                        float(point["x"]), float(point["y"])
                    ))
                except (KeyError, TypeError, ValueError):
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
        # The world-Y bands are what decides a floor when the marker reading is ambiguous, so a recording
        # whose floors disagree in world Y is a silent trap: his 13:16 log had the character standing on
        # layer1 (marker_y 0.676829 inside layer1's band) and the worker answering layer2 from the world
        # signal, because a freshly recorded floor carried the PREVIOUS floor's world origin.  Print every
        # band and say it out loud when one floor's band reaches another floor's anchor.
        world_anchors: dict[str, float] = {}
        world_bands: dict[str, tuple[float, float]] = {}
        for layer_name in snapshot.route_order:
            layer = snapshot.layers.get(layer_name, {})
            if not isinstance(layer, dict) or "layer_world_y" not in layer:
                continue
            anchor = float(layer["layer_world_y"])
            world_anchors[layer_name] = anchor
            band = _layer_world_y_band(
                layer, float(layer.get("world_y_tolerance", 0.75))
            )
            if band is None:
                continue
            world_bands[layer_name] = band
            logging.info(
                "LAYER WORLD BAND: %s world=(%.6f, %.6f) anchor=%.6f",
                layer_name, band[0], band[1], anchor,
            )
        for layer_name, (low, high) in world_bands.items():
            for other_name, other_anchor in world_anchors.items():
                if other_name == layer_name:
                    continue
                if low - 1e-9 <= other_anchor <= high + 1e-9:
                    logging.warning(
                        "LAYER WORLD BAND OVERLAP: %s's world band (%.6f, %.6f) contains %s's anchor "
                        "%.6f - the recorded world Y values of these floors disagree, so the world "
                        "signal cannot separate them; re-record %s or %s",
                        layer_name, low, high, other_name, other_anchor,
                        layer_name, other_name,
                    )
        # A recorded floor outside route_order cannot be patrolled, but it still takes part in the marker
        # match: one overlapping band there is enough to make a good marker reading look "ambiguous" and
        # hand the floor decision to the world signal.  Name them.
        route_names = set(snapshot.route_order)
        for layer_name, layer in snapshot.layers.items():
            if layer_name in route_names or not isinstance(layer, dict):
                continue
            band = _layer_y_band(
                layer, float(layer.get("y_tolerance", 0.020000))
            )
            logging.warning(
                "LAYER EXTRA RECORDED FLOOR: %s y=%s is not in route_order=%s; it cannot be patrolled, "
                "but a marker Y inside its band makes the reading ambiguous (this is how a stale or "
                "duplicate recording steers the patrol onto another floor)",
                layer_name,
                (f"({band[0]:.6f}, {band[1]:.6f})" if band is not None else "n/a"),
                list(snapshot.route_order),
            )
        # Sanity of the RECORDING itself: a point saved with a poor marker reading, or points of one
        # floor saved in different minimap canvas frames, is what makes a floor's band wide and lets a
        # marker on the floor below match it (his 13:37 report: "the layer2 should be a line but it is a
        # band").  Both are named here so the floor can simply be re-recorded.
        try:
            raw_layers = patrol_controller.snapshot().layers
        except Exception:
            raw_layers = {}
        for layer_name, layer in raw_layers.items():
            if not isinstance(layer, dict):
                continue
            frames: dict[tuple[float, float], list[str]] = {}
            heights: list[float] = []
            weak: list[tuple[str, float, float]] = []
            for point_name in ("left_most_pos", "rope_pos", "right_most_pos"):
                point = layer.get(point_name)
                coordinate = point.get("coordinate_v2") if isinstance(point, dict) else None
                if not isinstance(coordinate, dict):
                    continue
                recorded = coordinate.get("recorded_layout")
                if isinstance(recorded, dict):
                    try:
                        frame = (round(float(recorded["canvas_top"]), 1),
                                 round(float(recorded["canvas_height"]), 1))
                    except (KeyError, TypeError, ValueError):
                        frame = None
                    if frame is not None:
                        frames.setdefault(frame, []).append(point_name)
                if isinstance(point.get("y"), (int, float)):
                    heights.append(float(point["y"]))
                confidence = point.get("tracking_confidence")
                if isinstance(confidence, (int, float)) and isinstance(point.get("y"), (int, float)):
                    weak.append((point_name, float(confidence), float(point["y"])))
            if len(frames) > 1:
                logging.warning(
                    "LAYER RECORDING: %s's points were saved in %d different minimap canvas frames (%s) - "
                    "the saved heights cannot be re-projected against each other, so a floor can end up "
                    "drawn as a band (or matching another floor's marker) instead of a line; re-record "
                    "this floor",
                    layer_name, len(frames),
                    "; ".join(
                        f"{'/'.join(names)} canvas_top={frame[0]:.0f} height={frame[1]:.0f}"
                        for frame, names in frames.items()
                    ),
                )
            if len(heights) >= 2 and (max(heights) - min(heights)) >= 0.05:
                # The floor's own recorded heights disagree by >= 4 px of the minimap: either a genuine
                # stair/bench platform, or a point saved while the marker reading was weak.  The weak
                # reading is named so a bogus point can be re-recorded instead of guessed at.
                for point_name, confidence, y in weak:
                    if confidence >= 0.30:
                        continue
                    logging.warning(
                        "LAYER RECORDING: %s.%s sits %.1f px away from this floor's other points and was "
                        "saved with a weak marker reading (tracking_confidence=%.3f) - re-record it if "
                        "this floor is not a ramp",
                        layer_name, point_name, abs(y - min(heights)) * 82.0, confidence,
                    )
            # The canonical world anchor and the points' own observed world Y must agree: the world-Y band
            # is built from the observed values while the tracker and the re-anchors use the canonical one,
            # so a disagreement makes the world signal meaningless for this floor.  His profile has exactly
            # that (layer1 canonical 2.416667, its points observed ~1.04), and the raw tracker - which
            # swings while falling - then landed inside layer1's band mid-air and the planned descent was
            # declared arrived one floor too early.
            canonical = layer.get("layer_world_y")
            observed = [
                float(point[1])
                for point in _coherent_observed_world_points(layer)
            ]
            if isinstance(canonical, (int, float)) and observed:
                mean_observed = sum(observed) / len(observed)
                if abs(float(canonical) - mean_observed) >= 0.5:
                    logging.warning(
                        "LAYER RECORDING: %s's canonical world Y is %.6f but its points were recorded at "
                        "%.6f (%.3f apart) - the world signal cannot separate the floors for this layer; "
                        "re-record it",
                        layer_name, float(canonical), mean_observed,
                        abs(float(canonical) - mean_observed),
                    )
        if show_overlays:
            screen_blinker.show_jump_points(
                fresh_frame.window_rect,
                fresh_frame.image.size,
                detection.analysis_box,
                jump_point_overlay,
            )
            screen_blinker.show_layer_bands(
                fresh_frame.window_rect,
                fresh_frame.image.size,
                detection.analysis_box,
                layer_bands,
                # The screen-capture backend can include these translucent
                # bands.  Do not arm movement until they are gone; otherwise
                # the first live marker frames may be obscured and the patrol
                # appears not to have started.
                wait_until_hidden=True,
            )
            # The overlay can be captured by desktop-based backends, so only
            # when it was actually shown do we wait for it and publish a clean
            # post-overlay frame. Manual patrol starts deliberately skip this
            # optional visual step: it used to make a successful Start Patrol
            # stand still for several seconds after its success sound.
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
            if require_layer:
                raise OSError(
                    f"character marker Y={marker.y:.6f} does not match any "
                    "recorded layer; record the current map layers again"
                )
            # The automatic restart after a reconnect (the operator: "you should first detect current
            # layer, if it is out of patrol route then check how to go back"): the character is not on
            # any recorded layer band, so the patrol starts from the route's BASE layer and the
            # movement worker's out-of-route return logic (Alt+Down when the world Y is too high,
            # climb/return when it is lower) brings it back.  Marker Y and the bands are logged above,
            # so the decision is readable from the log.
            fallback = next(iter(snapshot.route_order), None)
            if fallback is None:
                raise OSError("the patrol route has no layer to start from")
            logging.warning(
                "MAP SESSION: marker Y=%.6f is OUTSIDE every recorded layer band (the character is "
                "off the patrol route) - starting the patrol from the base layer %s so the "
                "out-of-route return logic can bring it back",
                marker.y,
                fallback,
            )
            detected_name = fallback
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

        # Layer-band drawing is diagnostic only. Do not make a live patrol
        # wait for it: the overlay's timed display and clean recapture caused
        # a visible freeze immediately after the start confirmation.
        # Start a new movement generation before arming input.  Delayed work
        # from a stopped patrol remains cancelled until this exact point.
        movement_worker.arm_patrol_input()
        armed = _start_live_input(
            key_sender, automation_active,
            lambda: prepare_map_session(
                show_overlays=False, show_startup_marker=True,
            ),
            patrol_preparing,
        )
        return armed

    def restart_patrol_after_reconnect() -> bool:
        """Start Patrol again after a reconnect - no manual start needed.

        The operator's reports through v1.0.30: "it don't start patrol, it didn't go into detect layer
        and check if back to patrol route logic ... what i want is that i don't need to manually
        restart patrol".

        Called by the UI (on Tk's thread) only when the operator's patrol INTENT is set, so this does
        the work without asking again: the same preparation as a manual Start Patrol, with two
        differences - the overlays are skipped, and a layer that is NOT on the patrol route does not
        refuse the start (the patrol then begins from the route's base layer and the movement worker's
        out-of-route return logic walks the character back: Alt+Down when the world Y is too high, the
        return/climb logic when it is lower).
        """

        def prepare() -> None:
            try:
                prepare_map_session(show_overlays=False, require_layer=False)
            except OSError as exc:
                # The map session could not be prepared at all (no minimap, no marker, wrong map):
                # still arm the patrol, so the workers can recover instead of staying dead.
                logging.warning("RECONNECT PATROL RESTART: the map session could not be prepared "
                                "(%s) - starting the patrol anyway", exc)

        try:
            movement_worker.arm_patrol_input()
            armed = _start_live_input(key_sender, automation_active, prepare, patrol_preparing)
        except OSError as exc:
            logging.warning("RECONNECT PATROL RESTART refused at the window/calibration step: %s", exc)
            return False
        if armed:
            patrol_controller.set_enabled(True)
            logging.warning("RECONNECT PATROL RESTART: patrol resumed automatically after the "
                            "reconnect (layer detection ran; off-route characters are walked back by "
                            "the out-of-route return logic)")
        return bool(armed)

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
    def release_hotkey_ctrl(action: str) -> None:
        """End the GAME's Ctrl state after a recognized Ctrl chord.

        The operator presses Ctrl+<key>; Windows consumes the second key, but
        the game has already received the Ctrl key-down - and because the
        operator keeps Ctrl held to finish the chord, the game's Ctrl-bound
        attack keeps firing ("the attack is triggered infinitely").  One forced
        Ctrl key-up ends it.

        It is injected into the global input stream on purpose: only that
        reaches the game.  Because this same injection also clears the modifier
        state Windows matches ``RegisterHotKey`` chords against, chord
        detection runs through the worker's own low-level hook, which tracks the
        PHYSICAL modifiers and filters the assistant's stamped events - see
        ``HotkeyWorker.delivery`` (defaults to "hook" while this cleanup is
        enabled).
        """

        try:
            key_sender.force_key_up("ctrl", reason=f"hotkey chord: {action}")
        except Exception:
            logging.warning("hotkey Ctrl cleanup failed for %s", action, exc_info=True)

    def keep_hotkey_ctrl_released() -> None:
        """Re-assert the Ctrl release while the operator still holds Ctrl.

        One injected key-up is undone by the keyboard repeat of the physically
        held key within tens of milliseconds, which is why the game resumed
        attacking right after Ctrl+1 even though the cleanup had run.  The
        worker calls this (silently) until Ctrl is let go.

        It stands down while the assistant is sending a Ctrl chord of its own -
        a quick message is Enter + Ctrl+V + Enter on the game window, and a
        Ctrl key-up landing inside it would paste nothing.
        """

        guard = getattr(key_sender, "ctrl_chord_in_flight", None)
        if callable(guard) and guard():
            return
        key_sender.force_key_up(
            "ctrl", reason="Ctrl still held after a hotkey chord", quiet=True,
        )

    hotkey_worker = None if args.no_ui else HotkeyWorker(
        stop_event, hotkey_actions,
        on_chord=release_hotkey_ctrl,
        keep_ctrl_released=keep_hotkey_ctrl_released,
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
        # The reconnect stands the automation down while it runs: arming live input for its own keys
        # otherwise wakes the attack worker too (measured 19:07: `attack repetition: a` every second
        # through the whole sequence).
        automation_event=automation_active,
        reconnect_active_event=reconnect_active,
    )

    # 测试api (附加功能 panel): pick a video, play it at the API's 5 fps and upload each frame's ROI
    # to the RoiTrack backend (see api_lie_video.py).  A new worker per press, because a thread can
    # only be started once.  api_lie_test.py (the older single-frame screen drill) has no panel
    # button any more; it stays as an offline tool for work/ scripts and the test suite.
    api_test_results: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=256)
    # The service expects one short live round: after the separate 3-second
    # settle, send frames for no more than 13 seconds, then round_end and
    # close the WebSocket.
    AUTO_LIE_TRACK_SECONDS = 13.0

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

    # 自动过测谎 (附加功能 panel): a selection that runs the api pass by itself when the game's lie
    # window appears.  It reuses the shipped single-frame drill (`api_lie_test`), which captures the
    # game window, uploads the ROI to the RoiTrack backend and aims at the answer - the same workflow as
    # 测试api, triggered by the lie event instead of the button.  One worker per event, because a thread
    # cannot restart.
    api_auto_lie_results: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=64)
    auto_lie_capture_sequence = -1

    def capture_shared_auto_lie_frame():
        """Wait for the next shared 5 fps frame; auto-lie never captures the game itself."""

        nonlocal auto_lie_capture_sequence
        frame = bus.wait_for_new(auto_lie_capture_sequence, timeout=1.0)
        if frame is None:
            return None, (0, 0, 0, 0)
        auto_lie_capture_sequence = frame.sequence
        image = np.asarray(frame.image.convert("RGB"))[:, :, ::-1].copy()
        return image, frame.window_rect

    def make_api_auto_lie_worker():
        """One automatic pass for one lie window - the SAME workflow as 测试api (the video drill)."""

        from api_lie_test import ApiLieTestWorker
        # The drill's measured AWAIT_SECOND_WINDOW wait, so the automatic pass runs the drill's workflow
        # in the live game: the lie window's HUD square is detected the moment the window opens, its real
        # content needs ~3 s (his clip: the first 16 frames were one frozen image and the service answered
        # abandon_frame_hold for every one of them), so we connect FIRST, wait that long WITHOUT sending a
        # single frame, and only then start the picture push.  The wait is part of the pass length, exactly
        # like the drill's 时长.
        from api_lie_video import AWAIT_SECOND_WINDOW_SEC

        logging.info("自动过测谎: starting a pass for the lie window")
        return ApiLieTestWorker(
            results=api_auto_lie_results,
            stop_event=stop_event,
            window_title=args.window_title,
            key="",
            key_sender=key_sender,
            fps=5.0,
            # The worker sends its explicit round_end and closes after this
            # 13-second active tracking window.
            duration=AWAIT_SECOND_WINDOW_SEC + AUTO_LIE_TRACK_SECONDS,
            await_seconds=AWAIT_SECOND_WINDOW_SEC,
            # The answer must be EXECUTED, not just measured: the pass drives the cursor to the point
            # the API returns (the vendor doc: "鼠标/执行层一般用 x / y（372×248 协议 ROI）"), exactly like
            # the 测试api video drill.  Without it the lie window was never answered.
            aim_enabled=True,
            # ... and it is DRAWN: a click-through crosshair overlay on the game at the answered point,
            # the live equivalent of the crosshair the video drill shows in its own window.
            aim_overlay=screen_blinker.show_aim_marker,
            capture_fn=capture_shared_auto_lie_frame,
            on_capture_start=lie_active.set,
            on_capture_stop=lie_active.clear,
            backend_opener=open_prepared_auto_lie_backend,
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
        scan_interval=0.20,
        sound_path=Path(__file__).resolve().parent / "sound" / "dingdong.mp3",
        flash_callback=screen_blinker.request_blink,
        alert_callback=telegram_notifier.notify,
    )
    lie_detector_worker.add_lie_seen_callback(
        screenshot_recorder.on_lie_seen
    )
    # 自动过测谎 and 自动重连 are EVENT workflows, not patrol workflows: while the patrol capture is parked
    # this watch keeps their detectors fed, so a lie window or a 掉线 still fires with no Start Patrol.
    # Each feed is armed by its own panel selection (测谎 / 掉线).
    lie_detection_armed = threading.Event()
    disconnect_watch_armed = threading.Event()
    parked_watch = ParkedWatchCapture(
        args.window_title,
        (
            WatchFeed(
                lie_detector_frames,
                "测谎 (lie detector)",
                armed_event=lie_detection_armed,
                interval=LIE_WATCH_INTERVAL_SECONDS,
            ),
            WatchFeed(
                character_frames,
                "掉线/自动重连 (disconnect detection)",
                armed_event=disconnect_watch_armed,
                # The marker gate is a normal-cadence frame count (25 samples,
                # about 5 s at 5 FPS).  The separate login-page gate runs at
                # 1 FPS only after those samples are missing.
                interval=float(args.interval),
                # A frame of the game window while the assistant's own panel covers it is not the game:
                # counting it would fire a false 掉线 alert and a reconnect that clicks the game.
                requires_game_foreground=True,
            ),
        ),
        stop_event,
        patrol_capture_event=shared_capture_gate,
        foreground_check=key_sender.is_game_foreground,
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
            aligned_frames_required=five_fps_frames(
                calibration.get("aligned_frames_required", 2), 2
            ),
            climb_layer_confirm_frames=five_fps_frames(
                calibration.get("climb_layer_confirm_frames", 3), 2
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
            climb_world_y_stall_frames=five_fps_frames(
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
            fall_detect_frames=five_fps_frames(calibration.get("fall_detect_frames", 3), 2),
            fall_marker_y_gain=float(calibration.get("fall_marker_y_gain", 0.015)),
            # Landing reconciliation (world-Y settle + re-anchor to the true
            # layer after a knock-down) and the world-Y drift watchdog.
            fall_settle_min_frames=five_fps_frames(
                calibration.get("fall_settle_min_frames", 3), 2
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
            # 自动重连: the falling edge of this event makes patrol re-check the route as its very
            # first act after the reconnect gives the input back.
            reconnect_active_event=reconnect_active,
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
            # Automatic stair jumps were removed from the attack panel. The
            # jump executor is retained only for explicit recorded jump points.
            stair_jump_enabled=False,
            stair_jump_stall_diamonds=float(
                calibration.get("stair_jump_stall_diamonds", 0.25)
            ),
            stair_jump_stall_frames=five_fps_frames(
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
            rescue_stuck_frames=five_fps_frames(
                calibration.get("rescue_stuck_frames", 20), 5
            ),
    )
    motion_arbiter.set_micro_step_callback(movement_worker.perform_micro_step)
    motion_arbiter.set_facing_callback(movement_worker.perform_stationary_facing)
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

    def disarm_patrol_runtime(*, refocus_before_release: bool = False) -> None:
        """Cancel every patrol-owned action, then perform one key scrub.

        This is intentionally idempotent: Stop Patrol, focus loss, and a
        disconnect can race, and none may leave a delayed arbiter/movement
        action alive for a later patrol toggle.
        """

        automation_active.clear()
        movement_worker.disarm_patrol_input()
        motion_arbiter.cancel_pending("patrol lifecycle stopped")
        stair_jump_worker.cancel_pending("patrol lifecycle stopped")
        _stop_live_input(
            key_sender,
            automation_active,
            refocus_before_release=refocus_before_release,
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
        # A disconnect needs 25 normal capture observations.  The capture worker may temporarily
        # accelerate for other workflows, but those extra frames must not shorten this confirmation.
        disconnect_alert_sample_seconds=float(args.interval),
        alert_sound_path=(
            Path(__file__).resolve().parent / "sound" / "dingdong.mp3"
        ),
        flash_callback=screen_blinker.request_blink,
        alert_callback=telegram_notifier.notify,
        on_disconnect=stop_patrol_for_disconnect,
        disconnect_event_callback=_on_disconnect_event,
        # A missing marker alone is not an offline event: space zones can
        # hide the minimap.  Reconnect starts only after this same frame also
        # proves that the login page is visible.
        disconnect_login_page_check=reconnect_worker.login_page_visible_in_frame,
        # No disconnect alert while 自动重连 is running: its login screens have no yellow marker.
        reconnect_active_event=reconnect_active,
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
        reconnect_active_event=reconnect_active,
    )
    core_workers = [
        capture_worker,
        character_worker,
        movement_worker,
        status_worker,
        motion_arbiter,
        *attack_workers,
        small_step_worker,
        stair_jump_worker,
        *([hotkey_worker] if hotkey_worker is not None else []),
        quick_pickup_worker,
        reconnect_worker,
        trade_worker,
        screen_blinker,
        countdown_worker,
        lie_detector_worker,
        parked_watch,
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
            small_step_worker=small_step_worker,
            hotkey_queue=hotkey_actions,
            hotkey_worker=hotkey_worker,
            quick_pickup_worker=quick_pickup_worker,
            quick_pickup_results=quick_pickup_results,
            reconnect_worker=reconnect_worker,
            reconnect_results=reconnect_results,
            api_test_video_factory=make_api_test_video_worker,
            api_test_results=api_test_results,
            # 自动过测谎: the api pass that runs by itself when a lie window appears.
            api_auto_lie_factory=make_api_auto_lie_worker,
            trade_worker=trade_worker,
            movement_worker=movement_worker,
            character_worker=character_worker,
            shutdown_worker=shutdown_worker,
            countdown_worker=countdown_worker,
            lie_detector_worker=lie_detector_worker,
            screen_blinker=screen_blinker,
            telegram_notifier=telegram_notifier,
            on_patrol_start=start_patrol_input,
            # 自动重连 brought the character back into the game: prepare the map session again (layer
            # detection) and resume the patrol without the operator pressing 开始巡逻.
            on_patrol_restart=restart_patrol_after_reconnect,
            on_patrol_stop=lambda: disarm_patrol_runtime(
                refocus_before_release=True,
            ),
            on_capture_now=lambda: _capture_focused_game_frame(
                key_sender, capture_worker.capture_now
            ),
            on_recording_verified=save_recording_minimap_calibration,
            log_queue=ui_log_handler.messages if ui_log_handler is not None else None,
            ui_log_handler=ui_log_handler,
            user_config_path=str(config_store.user_path),
            automation_active_event=automation_active,
            # 测谎 armed: the parked watch may grab the game window so a lie window is still caught
            # (and 自动过测谎 can take over) without Start Patrol.
            lie_watch_armed_event=lie_detection_armed,
            # 掉线 armed: the same watch feeds the character worker, so a disconnect is noticed (and
            # 自动重连 can run) without Start Patrol.
            disconnect_watch_armed_event=disconnect_watch_armed,
        )
    )

    if ui_worker is not None:
        # 自动过测谎: the lie detector's own event drives the automatic api pass (the callback
        # only raises a flag; the panel's Tk thread services it, so nothing Tk is touched
        # from the detector's thread).
        lie_detector_worker.add_lie_seen_callback(
            lambda match, _frame: ui_worker.on_lie_event_for_api(match)
        )

    def auto_lie_is_active() -> bool:
        return bool(ui_worker is not None and ui_worker.auto_lie_pass_active())

    def cancel_auto_lie() -> bool:
        return bool(ui_worker is not None and ui_worker.request_cancel_auto_lie_pass())

    workflow_cancel_worker = WorkflowCancelWorker(
        stop_event,
        (
            ("auto reconnect", reconnect_worker.is_active, reconnect_worker.request_cancel),
            ("automatic lie pass", auto_lie_is_active, cancel_auto_lie),
            ("Ctrl+Q/Ctrl+W trade", trade_worker.is_trade_active, trade_worker.request_cancel),
        ),
    )
    core_workers.append(workflow_cancel_worker)

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
        "MapleAssistant %s starting from %s",
        version_label(),
        Path(__file__).resolve().parent,
    )
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
