"""Integrates capture, movement, and status workers."""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import replace
import json
import logging
import math
import os
import queue
import shutil
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from config_store import get_config_store
from minimap_detector import hud_scale_for
from runtime_paths import application_root
from auto_lie_secret import CredentialRedactionFilter, get_server_secret
from async_log_writer import AsyncBatchLogWriter
from crash_reporter import record_exception
from versioning import version_label


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
# Keep the original mutex identity after the product rename.  An in-place
# update must not let an already-running TodoHelper instance and its
# MoonEclipse replacement automate the same game window simultaneously.
SINGLE_INSTANCE_MUTEX_NAME = "Local\\TodoHelper.Singleton.v1"
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
        raise OSError(error_code, "could not create MoonEclipse singleton mutex")
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


def _stuck_non_daemon_threads() -> list[threading.Thread]:
    """Non-daemon threads still alive once the shutdown joins are done."""

    current = threading.current_thread()
    return [
        thread for thread in threading.enumerate()
        if thread is not current and not thread.daemon and thread.is_alive()
    ]


def _force_exit_if_a_thread_is_stuck() -> None:
    """Leave the process even when a non-daemon worker refuses to stop.

    ``capture_worker`` is deliberately non-daemon, so a normal shutdown waits for
    the game-window capture to finish.  When that capture is stuck inside a Win32
    call (hung or minimised game window) the interpreter waits for it forever: the
    dashboard is gone, every worker was told to stop, the operator saw the app
    close - yet ``MoonEclipse.exe`` stays in Task Manager holding the
    single-instance mutex, so the next launch silently exits and reminds of
    nothing.  The operator reads that as "it was not killed".  Everything is
    flushed by the time this runs (log writers closed, mutex released), so
    forcing the exit can only drop a thread that already refused to stop.
    """

    stuck = _stuck_non_daemon_threads()
    if not stuck:
        return
    logging.getLogger(__name__).warning(
        "shutdown: %d non-daemon thread(s) still alive after the joins (%s); "
        "forcing the process to exit so a stuck capture cannot keep the app alive",
        len(stuck),
        ", ".join(thread.name for thread in stuck),
    )
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - a console-less launch has no streams
        pass
    os._exit(0)


def _show_already_running_notice() -> None:
    """Explain a duplicate launch when the hidden launcher has no console."""

    if not hasattr(ctypes, "WinDLL"):
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MessageBoxW(
            None,
            "MoonEclipse 已经在运行。\n\n请在任务栏中找到现有窗口；如需重启，请先关闭它。",
            "MoonEclipse",
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
        raise OSError("游戏窗口选择失败")
    if not key_sender.is_game_foreground():
        raise OSError("游戏窗口未能切换到前台")
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
        if key_sender.enable_input() is False:
            logging.warning(
                "START PATROL deferred: keyboard cleanup is still busy; "
                "the UI remains responsive and input stays disarmed"
            )
            return False
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
        raise OSError("游戏窗口选择失败")
    if not key_sender.is_game_foreground():
        raise OSError("游戏窗口未能切换到前台")
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
                        default=None,
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


def _recording_log_header(
    version: str, config_path: Path, recording: object,
) -> str:
    """Header placed at the top of assistant.log: recording first, then the log.

    A shared log must be interpretable on its own, and the recording (layers,
    endpoints, ropes, jump points) is what every movement line refers to.
    """

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        body = json.dumps(recording, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        body = repr(recording)
    return (
        "# ===== MoonEclipse log =====\n"
        f"# started  {stamp}\n"
        f"# version  {version}\n"
        f"# exe      {application_root(__file__)}\n"
        f"# config   {config_path}\n"
        "# ===== recording (user_config.json :: recording) =====\n"
        f"{body}\n"
        "# ===== log =====\n"
    )


def _clear_previous_log_files() -> None:
    """Startup: wipe routine logs from previous runs.

    Runs after the single-instance check so a second instance never clears
    a live session's logs.  work/assistant.log, yolo logs, demo console logs,
    the dedicated auto_lie.log and target_tracker logs all start fresh each launch, which
    keeps any pasted diagnostic output attributable to the current run.  The
    exception-only crash report is deliberately preserved: it is the evidence
    available after the dashboard has already disappeared.

    The update/launch reports are preserved for the same reason, and one of them
    is decisive: ``update-helper.log`` belongs to ``todohelper_update.ps1``,
    which runs BETWEEN two instances and always ends by starting the new one.
    Deleting it here destroyed the only record of an install that had just
    failed - the field report "the update was NOT installed ... path does not
    exist" arrived with no log to read, because the restarted assistant had
    already wiped it.  ``assistant-launch-*.log`` / ``error.log`` are written by
    ``startup_probe.py`` when the app fails to come up at all and have the same
    "read after the fact" role.
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
            # "*.log" covers the live files; "*.log.*" covers the rotated
            # chunks a previous build left behind (assistant.log.1 ...).  The
            # runtime log no longer rotates, so a leftover chunk would only be
            # mistaken for part of the current run.
            for pattern in ("*.log", "*.log.[0-9]*"):
                for path in base.rglob(pattern):
                    if path.name.casefold() in {
                        "crash_report.log", "ui_hang_trace.log",
                        # Update/launch evidence that is only ever read AFTER
                        # this process started (see the docstring).
                        "update-helper.log",
                        "assistant-launch-status.log",
                        "assistant-launch-error.log",
                    }:
                        continue
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        pass
        except OSError:
            pass
    print(f"[startup] cleared {removed} previous log file(s)")


def _migrate_recording_assets_out_of_config(
    configuration_root: Path, package_root: Path,
) -> Path:
    """Keep generated map references out of the shareable config folder.

    ``助手配置`` is intentionally exportable JSON only.  Older releases wrote
    image references and map-name caches below it, so move those files once to
    the private root-level runtime data tree.  Each move is per-file and
    replace-based: a stopped migration leaves either the old or new complete
    file, never a partially-written reference.
    """

    source = configuration_root / "recording-assets"
    target = package_root / "data" / "recording-assets"
    if not source.is_dir():
        return target
    try:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
        for directory in sorted(
            (entry for entry in source.rglob("*") if entry.is_dir()),
            key=lambda entry: len(entry.parts), reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            source.rmdir()
        except OSError:
            pass
        logging.info("recording assets migrated from 助手配置 to root data folder")
    except OSError:
        logging.warning("recording-assets migration deferred; preserving old files", exc_info=True)
        return source
    return target


def main() -> int:
    singleton_handle = _acquire_single_instance_mutex()
    if singleton_handle is None:
        # The launcher replaces an existing copy before starting a fresh one.
        # A short process-exit race can still reach this guard; exit silently
        # instead of showing a misleading "close the old instance" prompt.
        return 0
    args = parse_args()
    _clear_previous_log_files()
    # 本地测谎: bring the local service up with the app when this build ships it
    # and it is not already listening.  Backgrounded - licence validation and the
    # WS handshake take seconds and must never delay the UI.  Shutdown stops it
    # again, but only when THIS app started it.
    try:
        import local_lie_service

        # Pre-start is owned by the 本地测谎 selection (supervisor_set_active): the
        # UI restores the saved backend at startup and starts/stops the service on
        # every change, so launching with 本地测谎 selected starts it immediately
        # and switching to 关闭/云端测谎 stops it again.
        local_lie_service.stop_supervisor()
    except Exception:  # noqa: BLE001 - a missing service folder is not fatal
        logging.getLogger(__name__).debug(
            "local lie service could not be scheduled", exc_info=True
        )
    # The recording is needed for the log header, so the config store is
    # created before the log writers own the file.
    config_store = get_config_store(args.config)
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, args.log_level.upper(), logging.INFO))
    # INFO and ERROR tracking are persisted in work/assistant.log; the UI
    # running log remains an in-memory convenience view.
    # Keep the writer and the UI's copy-file action on the exact same
    # installed root.  ``__file__`` may be an internal Nuitka module path,
    # whereas application_root() always resolves to the EXE folder.
    log_root = application_root(__file__)
    trace_log_path = log_root / "work" / "assistant.log"
    try:
        trace_log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    # The recording goes on top: the writer appends below it, so the file can be
    # attached as one self-contained document.
    try:
        _header = _recording_log_header(
            version_label(),
            config_store.user_path,
            config_store.read_section("recording"),
        )
        trace_log_path.write_text(_header, encoding="utf-8")
    except OSError:
        pass
    def start_patrol_log_document(reason: str) -> None:
        """Restart the log at a patrol start: the file holds this run only.

        The operator attaches ``work\\assistant.log`` as the evidence for the
        run he is looking at, so the file must describe *that* run - not an
        ever-growing archive and not a size-capped cut of it.  The version and
        the current recording lead the new document, and everything logged
        from here on follows them.
        """

        try:
            recording = config_store.read_section("recording")
        except Exception:
            recording = {}
        try:
            text_body = json.dumps(
                recording, ensure_ascii=False, indent=2, sort_keys=True
            )
        except (TypeError, ValueError):
            text_body = ""
        header = (
            "# ===== MoonEclipse log =====\n"
            f"# started  {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"# version  {version_label()}\n"
            f"# exe      {application_root(__file__)}\n"
            f"# config   {config_store.user_path}\n"
            f"# ===== recording snapshot ({reason} "
            f"{time.strftime('%H:%M:%S')}) =====\n"
            f"{text_body}\n"
            "# ===== end recording snapshot =====\n"
            "# ===== log =====\n"
        )
        trace_log_writer.start_new_run(header)

    trace_log_writer = AsyncBatchLogWriter(
        trace_log_path,
        # One file, one run: the runtime log is restarted at every Start Patrol
        # (see start_patrol_log_document) and never rotates, so the attached
        # file always holds exactly the run that is happening now.  The old
        # 2 MB rotation silently cut a run in the middle and kept only its
        # newest part.
        max_bytes=0,
        backup_count=0,
        formatter=_compact_log_formatter(),
        mirror=sys.stderr,
    )
    # The credential is process-memory only and must never reach a log file -
    # masked or not.  One filter enforces that on every file handler: the
    # licensing lines only ever name the *outcome* ("refreshed", "withheld"),
    # so the state stays fully diagnosable without the value.
    credential_filter = CredentialRedactionFilter()
    trace_log_handler = trace_log_writer.handler
    trace_log_handler.setLevel(logging.INFO)
    trace_log_handler.addFilter(credential_filter)
    root_logger.addHandler(trace_log_handler)
    trace_log_writer.start()

    # The licensing/server boundary gets its own compact, safe-to-share trace.
    # It deliberately records no activation code, fingerprint, token, or secret.
    server_log_writer = AsyncBatchLogWriter(
        log_root / "server_client.log",
        max_bytes=500_000,
        backup_count=2,
        formatter=_compact_log_formatter(),
    )
    server_log_handler = server_log_writer.handler
    server_log_handler.setLevel(logging.INFO)
    server_logger = logging.getLogger("server-client")
    server_logger.setLevel(logging.INFO)
    server_log_handler.addFilter(credential_filter)
    server_logger.addHandler(server_log_handler)
    server_log_writer.start()

    # Automatic lie handling needs a shareable diagnosis file independent of
    # the noisy patrol log.  It contains the detector-to-pass handoff, the
    # prepared WebSocket connection, every worker outcome and all API-worker
    # progress/errors; it deliberately does not add keys, activation codes or
    # raw frames.  These named loggers do not propagate to the root logger:
    # automatic lie activity belongs in auto_lie.log, not assistant.log.
    auto_lie_log_writer = AsyncBatchLogWriter(
        log_root / "auto_lie.log",
        max_bytes=1_000_000,
        backup_count=2,
        formatter=_compact_log_formatter(),
    )
    auto_lie_log_handler = auto_lie_log_writer.handler
    auto_lie_log_handler.setLevel(logging.INFO)
    auto_lie_log_handler.addFilter(credential_filter)
    auto_lie_logger = logging.getLogger("auto-lie")
    for auto_lie_log_name in (
        "auto-lie",
        "api_auto_lie",
        "api_lie_test",
        "autolie_api",
    ):
        scoped_logger = logging.getLogger(auto_lie_log_name)
        scoped_logger.setLevel(logging.INFO)
        scoped_logger.addHandler(auto_lie_log_handler)
        scoped_logger.propagate = False
    auto_lie_log_writer.start()

    # UI poll diagnostics are intentionally verbose: every sub-process writes
    # an exact BEGIN/END boundary so a multi-second Tk stall can be assigned to
    # one phase even before that phase returns.  Keep this stream out of both
    # assistant.log and the Tk log history, and batch its writes independently.
    ui_poll_log_writer = AsyncBatchLogWriter(
        log_root / "ui_poll.log",
        max_bytes=8_000_000,
        backup_count=2,
        capacity=20_000,
        batch_records=500,
        batch_seconds=0.10,
        formatter=_compact_log_formatter(),
    )
    ui_poll_log_handler = ui_poll_log_writer.handler
    ui_poll_log_handler.setLevel(logging.INFO)
    ui_poll_logger = logging.getLogger("ui-poll")
    ui_poll_logger.setLevel(logging.INFO)
    ui_poll_logger.addHandler(ui_poll_log_handler)
    ui_poll_logger.propagate = False
    ui_poll_log_writer.start()

    def _log_uncaught_thread_error(args: threading.ExceptHookArgs) -> None:
        record_exception(
            f"uncaught worker exception: {getattr(args.thread, 'name', 'unknown')}",
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
        )
        logging.critical(
            "uncaught exception in worker %s",
            getattr(args.thread, "name", "unknown"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _log_uncaught_thread_error

    previous_sys_excepthook = sys.excepthook

    def _log_uncaught_main_error(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: object,
    ) -> None:
        record_exception(
            "uncaught main-thread exception", exc_type, exc_value, exc_traceback,
        )
        previous_sys_excepthook(exc_type, exc_value, exc_traceback)

    sys.excepthook = _log_uncaught_main_error

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
    from buff_scheduler import BuffScheduler
    from random_rest_worker import RandomRestWorker
    from attack_worker import AttackWorker
    from attack_slot import AttackSlot
    from atomic_motion import AtomicMotionRunner
    from small_step_worker import SmallStepWorker
    from stair_jump_worker import StairJumpWorker
    from hotkey_worker import HotkeyWorker
    from reconnect_worker import CHANNEL_DEFAULT, ReconnectWorker
    from auto_restart_worker import AutoRestartWorker
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
    from dingtalk_notifier import DingTalkNotifier
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
    from patrol_control import CoordinateLayout, PatrolController, _layer_y_band
    from ui_worker import UiLogHandler, UiWorker

    stop_event = threading.Event()
    climb_attack_lock = threading.Lock()
    climbing_active = threading.Event()
    # A confirmed stair hop briefly excludes conflicting action input, but it
    # is not a rope climb/return.  Keep its lifetime independent so a parked
    # no-route patrol (action=wait) can still attack and use 小碎步.
    stair_jump_active = threading.Event()
    # Stand-still anchor recovery temporarily takes attack priority while the
    # character walks back into its small fixed-position zone.
    stationary_recovery_active = threading.Event()
    # Lets the fixed attack cadence wake immediately when stand-still return
    # reaches its small adjustment zone, rather than losing one full interval.
    stationary_attack_resume = threading.Event()
    action_motion_active = _AnyEvent(
        climbing_active, stair_jump_active, stationary_recovery_active
    )
    dropping_active = threading.Event()
    # The API lie pass joins the normal shared 5 fps capture stream while it owns the cursor.
    lie_active = threading.Event()
    moving_active = threading.Event()
    pickup_active = threading.Event()
    automation_active = threading.Event()
    # Set while the 自动重连 owns the machine: the focus gate must not re-arm the automation then
    # (otherwise attack/jump start on the login page - the character is not in game yet).
    reconnect_active = threading.Event()
    # Raised from the high-memory restart trigger until its fresh game client
    # has been handed to 自动重连.  It is separate from reconnect_active so
    # the latter can remain the login worker's own lifecycle flag.
    auto_restart_active = threading.Event()
    reconnect_or_restart_active = _AnyEvent(reconnect_active, auto_restart_active)
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
    # Passive diagnostic subscriber.  It never causes a capture; it receives
    # the same published frame only while LieScreenshotRecorder is active.
    recorder_frames: queue.Queue = queue.Queue(maxsize=1)
    trade_frames: queue.Queue = queue.Queue(maxsize=1)
    character_positions: queue.Queue = queue.Queue(maxsize=1)
    subscribers = [
        movement_frames, status_frames, character_frames, lie_detector_frames,
        recorder_frames,
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
        ui_log_handler.addFilter(credential_filter)
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
    additional_settings = config_store.read_section("additional_functions")
    channel_update_events: "queue.Queue[int]" = queue.Queue(maxsize=16)
    # A stopped 有人换线 workflow asks the UI to clear its own
    # selection, so the feature cannot start a new round by itself.
    other_player_stop_events: "queue.Queue[str]" = queue.Queue(maxsize=8)

    # Select a WebSocket endpoint once while the dashboard is opening.  A
    # probe does not authenticate or bill (protocol 2.7.0 §4.3), so the lie
    # event can later connect straight to the cached endpoint instead of
    # serially probing every port.  Do NOT pre-handshake here: the protocol
    # drops an idle authenticated connection after 10 seconds without frames.
    auto_lie_endpoint_lock = threading.Lock()
    auto_lie_endpoint: dict[str, object] = {
        "value": None, "error": "", "candidates": (),
    }
    # A dead service port must not consume the lie window.  The next candidate
    # can be tried quickly; a healthy local/remote service has historically
    # completed its WebSocket upgrade and formal handshake far below this.
    AUTO_LIE_HANDSHAKE_TIMEOUT_SECONDS = 0.5

    def warm_auto_lie_endpoint() -> None:
        """Probe six random ports and cache the least-loaded quick reply.

        A probe is only a capacity observation, never a reservation.  For the
        first 300 ms of each six-port concurrent batch, collect every healthy
        result with a positive ``slots_left`` and select the largest value.
        If that short observation window has no usable answer, retain the old
        latency-first rule: use the first later reply that has a free slot.
        The full randomized order remains cached for handshake fallbacks.
        """

        try:
            from concurrent.futures import (
                ThreadPoolExecutor,
                TimeoutError as FuturesTimeoutError,
                as_completed,
            )

            from auto_lie_backend import select_auto_lie_ws_endpoints
            from autolie_api.connect import probe_ws_endpoints

            candidates = select_auto_lie_ws_endpoints()
            best = None
            tried: list[str] = []
            full: list[str] = []

            def probe_one(host: str, port: int):
                """Return one safe probe result without failing sibling probes."""

                try:
                    results = probe_ws_endpoints(host, (int(port),))
                    return next((item for item in results if item.ok), None)
                except Exception as exc:
                    auto_lie_logger.debug(
                        "AUTO LIE probe %s:%d raised %s: %s",
                        host, port, type(exc).__name__, exc,
                    )
                    return None

            for offset in range(0, len(candidates), 6):
                batch = candidates[offset:offset + 6]
                executor = ThreadPoolExecutor(
                    max_workers=len(batch),
                    thread_name_prefix="auto-lie-probe",
                )
                futures = {
                    executor.submit(probe_one, host, int(port)): (host, int(port))
                    for host, port in batch
                }
                chosen_without_waiting = False
                quick_available: list[tuple[object, int]] = []
                sampled_futures: set[object] = set()
                try:
                    # Let six probes race for a small, fixed capacity sample.
                    # `as_completed` preserves actual arrival order for the
                    # fallback path below, unlike inspecting an unordered set
                    # of completed futures.
                    quick_deadline = time.monotonic() + 0.300
                    try:
                        for future in as_completed(
                            futures,
                            timeout=max(0.0, quick_deadline - time.monotonic()),
                        ):
                            sampled_futures.add(future)
                            host, port = futures[future]
                            result = future.result()
                            tried.append(f"{host}:{port}")
                            slots_left = getattr(result, "slots_left", None)
                            if (result is not None
                                    and isinstance(slots_left, (int, float))
                                    and int(slots_left) > 0):
                                quick_available.append((result, len(tried)))
                            else:
                                full.append(f"{host}:{port}")
                    except FuturesTimeoutError:
                        # The bounded sample is complete.  Do not wait for
                        # slow ports merely to compare capacity.
                        pass

                    if quick_available:
                        # Higher reported free capacity wins.  Arrival order
                        # resolves an equal-capacity tie, keeping selection
                        # deterministic within this batch.
                        best, _arrival = max(
                            quick_available,
                            key=lambda item: (
                                int(getattr(item[0], "slots_left", 0)),
                                -item[1],
                            ),
                        )
                        chosen_without_waiting = True
                    else:
                        # No usable capacity sample appeared in 300 ms.  Keep
                        # the previous behavior exactly: accept the first
                        # later positive-slot response and immediately proceed.
                        remaining_futures = [
                            future for future in futures
                            if future not in sampled_futures
                        ]
                        for future in as_completed(remaining_futures):
                            host, port = futures[future]
                            result = future.result()
                            tried.append(f"{host}:{port}")
                            slots_left = getattr(result, "slots_left", None)
                            if (result is not None
                                    and isinstance(slots_left, (int, float))
                                    and int(slots_left) > 0):
                                best = result
                                chosen_without_waiting = True
                                break
                            full.append(f"{host}:{port}")
                finally:
                    # The selected endpoint is just a probe observation, so
                    # slow sibling probes must never delay the later formal
                    # handshake.  A full/unreachable batch is allowed to
                    # finish so its next batch starts with a clean state.
                    if chosen_without_waiting:
                        for sibling in futures:
                            sibling.cancel()
                    executor.shutdown(wait=not chosen_without_waiting)
                if best is not None:
                    break
            if best is None:
                raise RuntimeError(
                    "no WebSocket endpoint reported a remaining slot ("
                    + (", ".join(full or tried) or "no endpoints") + ")"
                )
            with auto_lie_endpoint_lock:
                auto_lie_endpoint["value"] = best
                auto_lie_endpoint["error"] = ""
                auto_lie_endpoint["candidates"] = candidates
            auto_lie_logger.info(
                "AUTO LIE preflight ready: cached WebSocket endpoint %s "
                "(slots_left=%s, %.0f ms; six-port capacity sample, %d of %d candidate(s) replied)",
                best.endpoint, best.slots_left, best.rtt_ms, len(tried), len(candidates),
            )
        except Exception as exc:
            with auto_lie_endpoint_lock:
                auto_lie_endpoint["error"] = f"{type(exc).__name__}: {exc}"
            auto_lie_logger.warning("AUTO LIE preflight failed; event will retry discovery: %s", exc)

    threading.Thread(
        target=warm_auto_lie_endpoint,
        name="auto-lie-preflight",
        daemon=True,
    ).start()

    def open_prepared_auto_lie_backend(log):
        """Open one fresh formal session, trying each service instance in turn.

        The HTTP health check is intentionally excluded: it is diagnostic-only
        in protocol 2.7.0 and must never delay a live lie event.  The startup
        probe's endpoint is used first; a handshake failure moves on to the next
        instance of the same random order rather than failing the pass.
        """

        from auto_lie_backend import (
            describe_ws_endpoint,
            ordered_candidates,
            select_auto_lie_ws_endpoints,
        )
        from autolie_api.key_store import mask_key
        from autolie_api.ws_client import RoiTrackWsClient
        from auto_lie_secret import get_server_secret

        key = get_server_secret()
        source = "授权服务器（仅运行内存）"
        if not key:
            raise RuntimeError("auto-lie credential is unavailable; wait for validated heartbeat")
        with auto_lie_endpoint_lock:
            endpoint = auto_lie_endpoint.get("value")
            preflight_error = str(auto_lie_endpoint.get("error") or "")
            candidates = list(auto_lie_endpoint.get("candidates") or ())
        if not candidates:
            candidates = list(select_auto_lie_ws_endpoints())
        cached: "tuple[str, int] | None" = None
        if endpoint is not None:
            cached_host = str(getattr(endpoint, "host", "") or "")
            cached_port = int(getattr(endpoint, "port", 0) or 0)
            if cached_host and cached_port:
                cached = (cached_host, cached_port)
        ordered = ordered_candidates(cached, candidates)
        if endpoint is None:
            auto_lie_logger.warning(
                "AUTO LIE preflight is unavailable (%s); trying %d endpoint(s) directly",
                preflight_error or "still running", len(ordered),
            )

        failures: list[str] = []
        for index, (host, port) in enumerate(ordered):
            endpoint_url = describe_ws_endpoint(host, port)
            client = RoiTrackWsClient(
                host, port, timeout=AUTO_LIE_HANDSHAKE_TIMEOUT_SECONDS,
            )
            try:
                started = time.perf_counter()
                client.connect()
                ack = client.handshake(
                    # RTF1 §5.2: one binary WebSocket packet carries compact JSON
                    # metadata plus the JPEG bytes.  Do not base64-expand the JPEG.
                    key, frame_standard=5, image_transport="bgr_jpeg90_packed",
                    client_info="maple_assistant_auto_lie",
                )
                elapsed_ms = (time.perf_counter() - started) * 1000.0
            except Exception as exc:
                client.close()
                failures.append(f"{endpoint_url} ({type(exc).__name__}: {exc})")
                if log is not None:
                    log.connection(
                        "endpoint handshake failed; trying the next one",
                        endpoint=endpoint_url, error=str(exc),
                    )
                auto_lie_logger.warning(
                    "AUTO LIE endpoint %s handshake failed; trying the next one: %s",
                    endpoint_url, exc,
                )
                continue
            if log is not None:
                log.connection(
                    "endpoint handshake_ack", endpoint=endpoint_url,
                    auth=ack.get("auth"), quota_left=ack.get("quota_left"),
                    connect_ms=round(elapsed_ms),
                )
            origin = "UI 启动预探测" if index == 0 and endpoint is not None else "备选端点"
            note = (
                f"真实后端 {endpoint_url} 密钥来自{source} "
                f"(quota_left={ack.get('quota_left')}；{origin})"
            )
            auto_lie_logger.info(
                "AUTO LIE ready: handshake %s in %.0f ms (key %s, %s)",
                endpoint_url, elapsed_ms, mask_key(key), origin,
            )
            return client, note, None
        raise RuntimeError(
            "所有测谎服务端点握手均失败：" + ("；".join(failures) or "no endpoints")
        )

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
    profile_path = args.recording_configuration or config_store.user_path
    patrol_controller = PatrolController(
        profile_path, map_profile,
        config_store=None if args.recording_configuration is not None
        else config_store,
    )
    configuration_root = profile_path.parent
    recording_assets_root = _migrate_recording_assets_out_of_config(
        configuration_root, application_root(__file__),
    )
    structure_reference = (
        recording_assets_root
        / "map-structure-reference.jpg"
    )
    structure_tracker = MapStructureTracker(
        structure_reference, tracking_size=OPENCV_ANALYSIS_SIZE[0]
    )
    map_identity_store = MapIdentityStore(
        recording_assets_root / "map-names"
    )

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

    def reconnect_restart_start(
        snapshot: Any, detected_name: str, above_route: bool,
    ) -> tuple[str, bool, bool]:
        """Where an automatic reconnect restart begins, and whether it still drops.

        The operator's rule is "on auto reconnect we shall start from layer1", so a
        restart starts at the recording's own first layer even when the marker sits
        elsewhere.  ``above_route`` is returned UNCHANGED: when the respawn is above the
        route the restart must still open with the drop-to-route descent (Alt+Down twice,
        then the edge walk).  Dropping that flag made the worker start horizontal patrol
        while the character was far above the route, which looked like "randomly walking,
        never trying Alt+Down" (local log 22:10:18).
        """

        start_layer = str(getattr(snapshot, "first_layer", "") or "")
        layers = getattr(snapshot, "layers", {}) or {}
        if start_layer and start_layer in layers:
            return start_layer, bool(above_route), True
        return str(detected_name), bool(above_route), False

    def prepare_map_session(*, stationary_reanchor: bool = True, show_overlays: bool = True,
                            show_startup_marker: bool = False,
                            require_layer: bool = True,
                            reconnect_restart: bool = False,
                            draw_bands: bool = True) -> None:
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
        short marker crosshair.  It deliberately does not draw layer bands.

        ``draw_bands`` draws the layer-Y bands for a manual start.  They render
        on their own thread (``wait_until_hidden=False``) for their 2.2 s and
        never block arming the patrol, so calibration feedback costs nothing at
        start-up.  The automatic resumes pass False: nobody is watching them and
        an overlay window during automatic recovery is only risk.

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
                    "无法为站桩攻击捕获当前小地图画面"
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
                    "当前移动模块不支持站桩攻击"
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
                    "站桩攻击启动时未检测到黄色角色标记"
                )
            # Stand-still attack automatically reuses only an existing,
            # correctly configured route.  The temporary standing point is
            # never added to the recording.  A non-matching anchor simply
            # stays ordinary stand-still attack for this start.
            configure_stationary_return = getattr(
                movement_worker, "configure_stationary_return_route", None
            )
            if callable(configure_stationary_return):
                if configure_stationary_return():
                    logging.info(
                        "STATIONARY ATTACK startup: temporary point selected "
                        "its patrol layer; route return is armed"
                    )
                else:
                    route_error = getattr(
                        movement_worker, "stationary_route_validation_error", None
                    )
                    detail = route_error() if callable(route_error) else ""
                    pickup_enabled = bool(getattr(
                        movement_worker, "stationary_pickup_enabled", False
                    ))
                    if detail and pickup_enabled:
                        # This is a normal configuration refusal. UiWorker
                        # catches OSError and shows the text; nothing should
                        # escape the callback or crash the dashboard.
                        raise OSError(
                            "已启用定时巡逻：请检查巡逻路线配置（"
                            + detail + "）"
                        )
                    logging.warning(
                        "STATIONARY ATTACK startup: no usable route helper; "
                        "ordinary stand-still attack continues%s",
                        f" ({detail})" if detail else "",
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
            # Show the temporary 站桩攻击 anchor on the same shared marker
            # canvas as patrol points.  It is diagnostic only and never
            # participates in the fixed-position calculation.
            if stationary_reanchor and marker is not None:
                try:
                    screen_blinker.show_patrol_points(
                        fresh_frame.window_rect,
                        fresh_frame.image.size,
                        detection.analysis_box,
                        (("fixed_anchor", marker.x, marker.y),),
                    )
                    logging.info(
                        "STATIONARY ATTACK marker: shared fixed-position crosshair drawn x=%.6f y=%.6f",
                        marker.x, marker.y,
                    )
                except Exception:
                    logging.warning("STATIONARY ATTACK marker overlay failed", exc_info=True)
            if stationary_reanchor:
                # A manual stand-still start deliberately has no route
                # handoff: the just-recorded temporary point is the entire
                # starting state.  Automatic resumes are different.  They
                # must preserve this point, then inspect the *current*
                # marker against the recorded route so the movement worker
                # can either drop down or climb/walk back to it.
                logging.info(
                    "STATIONARY ATTACK startup: temporary current-position "
                    "anchor saved; recorded layers were not checked"
                )
                return
            logging.info(
                "STATIONARY ATTACK automatic resume: preserved temporary "
                "standing point; checking the current floor for route return"
            )
        title_image = fresh_frame.image.crop(detection.map_name_box)
        if configured_name and map_identity_store.has_reference(configured_name):
            matched, score = map_identity_store.matches(configured_name, title_image)
            if not matched:
                raise OSError(
                    f"当前地图名称与已录制的地图不匹配 "
                    f"{configured_name}（画面相似度 {score:.2f}）；"
                    "请确认当前地图，或在「重置录制」后重新录制。"
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
            regions = [("minimap", detection.window_box, 0x0000FF00)]
            if detection.canvas_measured:
                regions.append(("marker/patrol", detection.analysis_box, 0x0000FFFF))
            else:
                # The yellow rectangle is the map canvas.  When no border could
                # be measured and none was remembered, drawing it over the whole
                # detected/search area would claim the minimap window as the
                # canvas, so it is simply left out.
                logging.warning(
                    "DETECTION OVERLAY: the map canvas is neither measurable in "
                    "this frame nor remembered for this client size (%s); only "
                    "the detected minimap frame is drawn, not a yellow "
                    "marker/patrol region",
                    detection.analysis_box,
                )
            regions.append(("hp/mp", status_box, 0x00FF0000))
            screen_blinker.show_detection_regions(
                fresh_frame.window_rect,
                fresh_frame.image.size,
                tuple(regions),
            )
            logging.info(
                "DETECTION OVERLAY: flashing %s regions",
                ", ".join(name for name, _box, _colour in regions),
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
            if show_startup_marker and marker is not None:
                screen_blinker.show_patrol_points(
                    fresh_frame.window_rect, fresh_frame.image.size,
                    detection.analysis_box,
                    (("fixed_anchor", marker.x, marker.y),),
                )
            return
        layer_bands = []
        patrol_point_overlay: list[tuple[str, float, float]] = []
        if show_startup_marker and marker is not None:
            patrol_point_overlay.append(("fixed_anchor", marker.x, marker.y))
        for layer_name in snapshot.route_order:
            layer = snapshot.layers.get(layer_name, {})
            if not isinstance(layer, dict):
                continue
            # A layer may legitimately carry a single supporter (rope only, or
            # left-most/right-most only).  Its band is then the operator's
            # one-sided [y, y + small_gap] with small_gap >= one marker row, so
            # say which shape produced the band that is printed below.
            single_supporter = (
                sum(
                    1
                    for point_name in ("left_most_pos", "rope_pos", "right_most_pos")
                    if isinstance(layer.get(point_name), dict)
                )
                <= 1
            )
            for point_name, marker_kind in (
                ("left_most_pos", "left_endpoint"),
                ("rope_pos", "rope"),
                ("right_most_pos", "right_endpoint"),
            ):
                point = layer.get(point_name)
                if not isinstance(point, dict):
                    continue
                try:
                    patrol_point_overlay.append((
                        marker_kind, float(point["x"]), float(point["y"])
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
            for point in layer.get("jump_points", []):
                if not isinstance(point, dict):
                    continue
                try:
                    direction = str(point.get("direction", "")).casefold()
                    marker_kind = {
                        "left": "jump_left", "right": "jump_right", "up": "jump_up",
                    }.get(direction, "jump_right")
                    patrol_point_overlay.append((
                        marker_kind, float(point["x"]), float(point["y"])
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
            band = _layer_y_band(
                layer, float(layer.get("y_tolerance", 0.020000))
            )
            if band is not None:
                layer_bands.append((layer_name, band))
                logging.info(
                    "LAYER BAND: %s y=(%.6f, %.6f) width=%.6f%s",
                    layer_name, band[0], band[1], band[1] - band[0],
                    " (single supporter: [y, y + small_gap])"
                    if single_supporter else "",
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
        if show_overlays or show_startup_marker:
            screen_blinker.show_patrol_points(
                fresh_frame.window_rect,
                fresh_frame.image.size,
                detection.analysis_box,
                patrol_point_overlay,
            )
        if draw_bands or show_overlays:
            # Concurrent on purpose: ``wait_until_hidden=False`` renders the
            # bands on their own thread for their 2.2 s and returns at once, so
            # the patrol arms immediately.  The old blocking variant waited for
            # the overlay and for a clean recapture, which is why band drawing
            # had been switched off for manual starts.
            screen_blinker.show_layer_bands(
                fresh_frame.window_rect,
                fresh_frame.image.size,
                detection.analysis_box,
                layer_bands,
                wait_until_hidden=False,
            )
        if marker is None:
            raise OSError(
                "巡逻启动时未检测到黄色角色标记"
            )
        # Flat recordings have one band per layer.  Split recordings have a
        # separate band in each X part, so startup must first pick the part
        # containing the yellow marker; consulting the outer legacy layer
        # dict here is what produced "Y does not belong to any layer" even
        # when the point had just been recorded in a split part.
        split_points = tuple(getattr(snapshot, "split_points", ()) or ())
        startup_part_index = 0
        anchor_layer_override: Optional[dict[str, Any]] = None
        if split_points:
            startup_part_index = sum(
                1 for split in split_points
                if float(marker.x) >= float(split.get("x", 1.0))
            )
            split_candidates: list[tuple[float, str, dict[str, Any]]] = []
            for name in snapshot.route_order:
                layer = snapshot.layers.get(name, {})
                parts = layer.get("part_points") if isinstance(layer, dict) else None
                part = parts.get(str(startup_part_index)) if isinstance(parts, dict) else None
                if not isinstance(part, dict):
                    continue
                band = _layer_y_band(
                    part, float(part.get("y_tolerance", 0.020000))
                )
                if band is None or not (band[0] - 1e-9 <= marker.y <= band[1] + 1e-9):
                    continue
                reference_y = float(part.get("layer_y", (band[0] + band[1]) / 2.0))
                split_candidates.append((abs(marker.y - reference_y), name, part))
            if split_candidates:
                _distance, detected_name, anchor_layer_override = min(split_candidates)
            else:
                detected_name = None
        else:
            detected_name = detect_layer_by_y(marker.y, snapshot.layers)
        startup_above_route = False
        if detected_name is None:
            # A player can start on an upper platform that has no separately
            # recorded layer.  If its marker is visibly above the configured
            # patrol range, do not refuse Start Patrol: arm the normal
            # drop-to-route state until it enters the recorded range.
            route_candidates = [
                name for name in snapshot.route_order
                if name in snapshot.layers
                and (not snapshot.patrol_start_layer
                     or int("".join(filter(str.isdigit, name)) or 0)
                     >= int("".join(filter(str.isdigit, snapshot.patrol_start_layer)) or 0))
                and (not snapshot.patrol_end_layer
                     or int("".join(filter(str.isdigit, name)) or 0)
                     <= int("".join(filter(str.isdigit, snapshot.patrol_end_layer)) or 0))
            ]
            top_route = max(
                route_candidates,
                key=lambda name: int("".join(filter(str.isdigit, name)) or 0),
                default=None,
            )
            top_layer = snapshot.layers.get(top_route, {}) if top_route is not None else {}
            if split_points and isinstance(top_layer, dict):
                parts = top_layer.get("part_points")
                top_layer = parts.get(str(startup_part_index), {}) if isinstance(parts, dict) else {}
            top_band = (_layer_y_band(
                top_layer, float(top_layer.get("y_tolerance", 0.020000)),
            ) if isinstance(top_layer, dict) else None)
            if top_band is not None and marker.y < top_band[0]:
                startup_above_route = True
                detected_name = top_route
                if split_points and isinstance(top_layer, dict):
                    anchor_layer_override = top_layer
                logging.warning(
                    "MAP SESSION: marker Y=%.6f is above the patrol route "
                    "(top %s begins at %.6f); dropping into the route",
                    marker.y, top_route, top_band[0],
                )
        if detected_name is None:
            if require_layer:
                raise OSError(
                    f"角色当前 Y={marker.y:.6f} 不属于任何已录制楼层；"
                    "请在本图重新录制各楼层。"
                )
            # The automatic restart after a reconnect (the operator: "you should first detect current
            # layer, if it is out of patrol route then check how to go back"): the character is not on
            # any recorded layer band, so the patrol starts from the route's BASE layer and the
            # movement worker's out-of-route return logic (Alt+Down when the world Y is too high,
            # climb/return when it is lower) brings it back.  Marker Y and the bands are logged above,
            # so the decision is readable from the log.
            fallback = next(iter(snapshot.route_order), None)
            if fallback is None:
                raise OSError("巡逻路线没有可用的起始楼层（请先录制楼层与最左/最右点）")
            logging.warning(
                "MAP SESSION: marker Y=%.6f is OUTSIDE every recorded layer band (the character is "
                "off the patrol route) - starting the patrol from the base layer %s so the "
                "out-of-route return logic can bring it back",
                marker.y,
                fallback,
            )
            detected_name = fallback
        anchor_name = str(detected_name)
        if reconnect_restart:
            # Operator's rule (2026-11-09): "on auto reconnect we shall start from
            # layer1".  A reconnect respawns the character anywhere (his log: marker
            # Y=0.380208 while the patrol route begins at 0.493292), so the restart
            # begins at the recording's own first layer instead of at the marker's
            # anchor layer.
            #
            # ``startup_above_route`` is deliberately KEPT: when the marker is above the
            # route the restart must still open with the drop-to-route descent (Alt+Down,
            # then the edge walk).  Clearing it here made the worker start horizontal
            # patrol immediately - the 22:10:18 local log shows "PATROL START: detected
            # layer1 in patrol range; starting at left cycle 1/1" while the marker was
            # 0.3246 away from layer1, i.e. the character walked and never dropped.
            start_layer, startup_above_route, used_first_layer = reconnect_restart_start(
                snapshot, anchor_name, startup_above_route
            )
            if used_first_layer:
                if start_layer != anchor_name:
                    logging.warning(
                        "MAP SESSION: reconnect restart starts the patrol from %s "
                        "(recording first layer; the marker detected %s)",
                        start_layer, anchor_name,
                    )
                anchor_name = start_layer
                anchor_layer_override = None
            else:
                logging.warning(
                    "MAP SESSION: reconnect restart could not use the recording's "
                    "first layer (%r); starting from %s",
                    start_layer, anchor_name,
                )
        if split_points and anchor_layer_override is None:
            split_layer = snapshot.layers.get(anchor_name, {})
            parts = split_layer.get("part_points") if isinstance(split_layer, dict) else None
            part = parts.get(str(startup_part_index)) if isinstance(parts, dict) else None
            if isinstance(part, dict):
                anchor_layer_override = part
        anchor_layer = anchor_layer_override or snapshot.layers.get(anchor_name, {})
        anchor_world_y = anchor_layer.get("layer_world_y")
        if anchor_world_y is None:
            raise OSError(
                f"{anchor_name or '第一层'} 没有录制世界 Y；请在本图重新录制一次"
            )
        structure_tracker.start_session(float(anchor_world_y))
        select_split_part = getattr(movement_worker, "select_split_start_part", None)
        if split_points and callable(select_split_part):
            select_split_part(startup_part_index)
            logging.info(
                "MAP SESSION split start: marker X=%.6f selected part %d/%d",
                marker.x, startup_part_index + 1, len(split_points) + 1,
            )
        movement_worker.prepare_patrol_start(
            anchor_name,
            above_route=startup_above_route,
            reconnect_restart=reconnect_restart,
        )
        logging.info(
            "MAP SESSION detected %s from marker_y=%s%s; re-anchoring "
            "world_y=%.6f",
            anchor_name,
            f"{marker.y:.6f}" if marker is not None else "unknown",
            " (above patrol route)" if startup_above_route else "",
            float(anchor_world_y),
        )

    def start_patrol_input() -> bool:
        """Manual Start Patrol (按钮 / Ctrl+`): focus, calibrate, arm input.

        """

        # The attached log is the evidence for ONE run: restart the file here
        # (version + recording first) so it holds exactly the run being
        # started, instead of a growing multi-run archive or a size-capped cut.
        start_patrol_log_document("start patrol")
        # Layer-band drawing is diagnostic only. Do not make a live patrol
        # wait for it: the overlay's timed display and clean recapture caused
        # a visible freeze immediately after the start confirmation.
        # Start a new movement generation before arming input.  Delayed work
        # from a stopped patrol remains cancelled until this exact point.
        movement_worker.arm_patrol_input()
        if bool(getattr(movement_worker, "dead_flower_mode", False)):
            # 死花 does not use the normal patrol route, but its private rope
            # loop still needs a stable minimap crop and a real yellow marker.
            # Skipping calibration here left the detector alternating between
            # the header and the actual minimap, so the flower worker never
            # received a player position.  ``require_layer=False`` keeps this
            # detection-only: it will not reject a marker on the rope and the
            # movement worker still bypasses every normal patrol decision.
            logging.info(
                "DEAD FLOWER startup: calibrating minimap/marker only; "
                "normal patrol route remains disabled"
            )
            return _start_live_input(
                key_sender, automation_active,
                lambda: prepare_map_session(
                    show_overlays=False,
                    show_startup_marker=False,
                    require_layer=False,
                ),
                patrol_preparing,
            )
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
                # This is an automatic resume, never a new manual standing
                # point.  Preserve the temporary stationary anchor and hand
                # the freshly detected marker to the normal drop/return
                # startup logic.
                prepare_map_session(
                    stationary_reanchor=False,
                    show_overlays=False,
                    draw_bands=False,
                    require_layer=False,
                    reconnect_restart=True,
                )
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
        # The attack-animation window every buff/skill tap must wait out: the
        # client silently drops a key pressed while the animation is still
        # running. 1.2s is the operator's measured preflight-clear time; a map
        # profile may override it without a code change.
        attack_grace_seconds=float(
            calibration.get("attack_grace_seconds", 1.2)
        ),
    )
    # Short multi-key combat sequences have their own atomic lane.  They are
    # admitted by the existing arbiter lease/token but do not become FIFO
    # movement motions and therefore leave a patrol Left/Right hold intact.
    atomic_motion_runner = AtomicMotionRunner(
        key_sender,
        stop_event,
        automation_active_event=automation_active,
    )
    # The shared keyboard sender is the final authority for every worker's
    # key-down.  During a charge it permits only the charge key itself and
    # normal patrol Left/Right, so a direct buff/rope/pickup send cannot race
    # the long hold.
    charge_gate = getattr(key_sender, "set_charge_gate", None)
    if callable(charge_gate):
        charge_gate(lambda: (
            atomic_motion_runner.charge_active()
            or motion_arbiter.charge_active()
        ))
    # The fixed-rate attack worker always exists so the UI can toggle it
    # live (Fixed Attack panel).  Without --enable-attack it starts disabled
    # and only waits; the panel flips ``enabled`` when the mode is selected.
    attack_slot = AttackSlot()
    attack_worker = AttackWorker(
        key_sender,
        stop_event,
        args.attack_interval,
        climbing_active_event=action_motion_active,
        automation_active_event=automation_active,
        motion_arbiter=motion_arbiter,
        direction_transition_event=direction_transition_active,
        attack_resume_event=stationary_attack_resume,
        attack_slot=attack_slot,
        atomic_motion_runner=atomic_motion_runner,
    )
    attack_worker.enabled = bool(args.enable_attack)
    attack_workers.append(attack_worker)
    # TEMPORARILY DISABLED together with its hidden UI controls.
    # shutdown_worker = ShutdownWorker(...)
    shutdown_worker = None
    hotkey_actions: "queue.Queue[str]" = queue.Queue(maxsize=32)
    # Ctrl+` is an emergency stop path.  Its hook callback runs independently
    # of Tk, so it can disarm automation even while the dashboard is occupied
    # with a diagnostic repaint.  Tk consumes this marker later to update its
    # buttons and text; it never repeats the physical key cleanup.
    hotkey_stop_prearmed = threading.Event()
    hotkey_stop_cleanup_done = threading.Event()
    hotkey_stop_prearm_lock = threading.Lock()

    def consume_hotkey_stop_prearmed() -> bool:
        with hotkey_stop_prearm_lock:
            if not hotkey_stop_prearmed.is_set():
                return False
            hotkey_stop_prearmed.clear()
            return True

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

        if action == "toggle_patrol":
            # The stop gate must be first.  ``force_key_up`` below can wait
            # for an in-flight key transaction, which previously made Ctrl+`
            # look unresponsive even though the hook had already recognized
            # it.  This block performs no Tk or physical-input work.
            with hotkey_stop_prearm_lock:
                if not hotkey_stop_prearmed.is_set() and patrol_controller.is_enabled():
                    hotkey_stop_prearmed.set()
                    hotkey_stop_cleanup_done.clear()
                    automation_active.clear()
                    patrol_controller.set_enabled(False)
                    try:
                        movement_worker.disarm_patrol_input()
                        motion_arbiter.cancel_pending("hotkey patrol stop")
                        stair_jump_worker.cancel_pending("hotkey patrol stop")
                    except Exception:
                        logging.exception("STOP PATROL: hotkey worker disarm failed")

                    def release_hotkey_input() -> None:
                        try:
                            # The game can lose foreground between the bot's
                            # last key-down and this hotkey callback (most
                            # visibly while a laggy client briefly hands focus
                            # to its Ghost surface).  The old no-refocus path
                            # then only forgot the local ledger and sent *no*
                            # game-side key-up, leaving that held direction in
                            # MapleStory.  This remains a background cleanup:
                            # its one bounded refocus attempt cannot delay the
                            # immediate stop gate or Tk handoff.
                            _stop_live_input(
                                key_sender, automation_active,
                                refocus_before_release=True,
                            )
                        finally:
                            hotkey_stop_cleanup_done.set()

                    threading.Thread(
                        target=release_hotkey_input,
                        name="hotkey-patrol-stop-cleanup",
                        daemon=True,
                    ).start()
                    logging.info(
                        "STOP PATROL: hotkey pre-armed before any key cleanup or Tk dispatch"
                    )

        try:
            # Bounded: this runs on the hotkey HOOK thread.  Waiting for the
            # keyboard ledger here parked chord detection, so every later hotkey
            # appeared dead (field report: no hotkey worked after a Stop Patrol
            # whose ledger cleanup had been deferred).
            key_sender.force_key_up(
                "ctrl", reason=f"hotkey chord: {action}", lock_timeout=0.05,
            )
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
            # Bounded for the same reason as above: this repeats on the hook
            # thread while Ctrl stays held, so it must never wait for a busy
            # keyboard ledger.
            lock_timeout=0.05,
        )

    hotkey_worker = None if args.no_ui else HotkeyWorker(
        stop_event, hotkey_actions,
        on_chord=release_hotkey_ctrl,
        keep_ctrl_released=keep_hotkey_ctrl_released,
    )
    # 自动重连: armed from the Additional Functions panel; the 掉线 event is its trigger and
    # the login page's base colour (screenshots/login_page_target.jpg) its confirmation
    # (see reconnect_worker.py).
    reconnect_results: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=16)

    def reconnect_post_login_marker_ready() -> bool:
        """Probe one fresh shared frame for the yellow character marker.

        Reconnect owns only its channel/login sequence.  This callback keeps
        the post-login readiness gate in the application's established minimap
        coordinate system and deliberately creates an isolated detector, so a
        failed loading-frame probe cannot disturb live patrol calibration.
        """

        try:
            frame = capture_worker.capture_now(timeout=1.0)
            saved_detection = minimap_calibration_from_dict(
                config_store.read_section("minimap_calibration"),
                frame.image.size,
            )
            detection = saved_detection
            if detection is None:
                detector = MinimapDetector(
                    fallback_region=minimap_region,
                    dedicated_crop=True,
                    opencv_size=MINIMAP_ANALYSIS_SIZE,
                )
                detection = detector.detect(frame.image)
            analysis_rgb = np.asarray(
                frame.image.crop(detection.analysis_box).convert("RGB")
            )
            return detect_yellow_diamond(analysis_rgb) is not None
        except Exception:
            logging.debug(
                "AUTO RECONNECT post-login yellow-marker probe did not produce a usable frame",
                exc_info=True,
            )
            return False

    reconnect_worker = ReconnectWorker(
        key_sender,
        stop_event,
        reconnect_results,
        window_title=args.window_title,
        dry_run=args.dry_run,
        # The bundled login-colour crop is application data.  Pass its
        # concrete path from the entry process instead of asking a worker
        # module to infer its own directory: this is stable for source,
        # normal ZIP and Nuitka releases, regardless of the extraction
        # folder's name or its parent path.
        template_path=(
            application_root(__file__)
            / "recording-assets"
            / "login_page_target.jpg"
        ),
        post_login_marker_ready_fn=reconnect_post_login_marker_ready,
        # The reconnect stands the automation down while it runs: arming live input for its own keys
        # otherwise wakes the attack worker too (measured 19:07: `attack repetition: a` every second
        # through the whole sequence).
        automation_event=automation_active,
        reconnect_active_event=reconnect_active,
        restart_active_event=auto_restart_active,
    )

    def note_player_channel_landed(channel: int) -> None:
        """Persist one completed player-triggered channel route centrally."""

        selected = max(1, min(60, int(channel)))
        reconnect_worker.set_channel(selected)
        settings = config_store.read_section("additional_functions")
        settings["auto_reconnect_channel"] = selected
        config_store.write_section("additional_functions", settings)
        try:
            channel_update_events.put_nowait(selected)
        except queue.Full:
            try:
                channel_update_events.get_nowait()
            except queue.Empty:
                pass
            try:
                channel_update_events.put_nowait(selected)
            except queue.Full:
                pass
        logging.info("player channel route committed current reconnect channel=%d", selected)

    def prepare_patrol_after_other_player_channel() -> bool:
        """Re-anchor the map after a clean player-triggered channel landing.

        A channel can spawn the character above the recorded patrol range.
        Reuse the reconnect preparation path: it reads the fresh marker and
        arms ``drop-to-route`` when the marker is above the top recorded band,
        before MovementWorker is allowed to resume horizontal patrol.
        """

        try:
            prepare_map_session(
                stationary_reanchor=False,
                show_overlays=False,
                draw_bands=False,
                require_layer=False,
                reconnect_restart=True,
            )
            logging.warning(
                "OTHER-PLAYER CHANNEL RESUME: map session refreshed; "
                "above-route spawn will drop before patrol"
            )
            return True
        except OSError as exc:
            logging.warning(
                "OTHER-PLAYER CHANNEL RESUME refused: could not prepare the "
                "fresh map/layer session (%s)", exc,
            )
            return False

    def note_other_player_stop(reason: str) -> None:
        """Ask the UI to clear the 其他玩家自动换线 selection after a manual stop.

        A manual Esc (or the workflow's own stop) must switch the feature OFF:
        leaving the field ticked let the next red diamond start a completely new
        round, with its 求让消息 and its waiting - the operator's "otherwise we
        only go into new round of switching".
        """

        try:
            other_player_stop_events.put_nowait(str(reason or "stopped"))
        except queue.Full:
            try:
                other_player_stop_events.get_nowait()
            except queue.Empty:
                pass
            try:
                other_player_stop_events.put_nowait(str(reason or "stopped"))
            except queue.Full:
                pass
        logging.warning("other-player switch stopped (%s); asking the UI to clear the field", reason)

    # 测试api (附加功能 panel): pick a video, play it at the API's 5 fps and upload each frame's ROI
    # to the RoiTrack backend (see api_lie_video.py).  A new worker per press, because a thread can
    # only be started once.  api_lie_test.py (the older single-frame screen drill) has no panel
    # button any more; it stays as an offline tool for work/ scripts and the test suite.
    api_test_results: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=256)
    # Standalone/video drills still use a bounded frame feed.  The automatic
    # live pass is outcome-controlled: it keeps streaming until the game
    # reports success/failure or the service closes its advertised round.
    AUTO_LIE_TRACK_SECONDS = 15.0
    AUTO_LIE_FEED_DELAY_SECONDS = 3.0

    def _local_lie_opener(frame_standard: float = 5.0):
        """Opener for 本地测谎: the CT partner API on 127.0.0.1:8765.

        The drills catch ``BackendError`` from their opener, so a local-service
        failure is translated into that type instead of escaping the thread.
        """

        def opener(log):
            from autolie_api.connect import BackendError
            from local_lie_backend import LocalLieError, open_local_lie_backend

            try:
                session = open_local_lie_backend(
                    log, frame_standard=frame_standard,
                )
            except LocalLieError as exc:
                raise BackendError(str(exc)) from exc
            except (OSError, ValueError) as exc:
                raise BackendError(f"本地测谎连接失败：{exc}") from exc
            return session.client, session.note, session.mimic

        return opener

    def make_api_test_video_worker(*, video, results, display, seconds=30.0, key="",
                                   fps=5.0, backend="cloud"):
        """The 测试api drill on a chosen video: play it at 5 fps and aim inside the picture only.

        A new worker per press (a thread cannot restart).  ``display`` carries the frames the UI
        thread must draw; the worker reads back ``worker.image_rect`` (the picture rectangle) to
        confine the mouse to the video area.  The key is not a panel setting any more: it ships
        from the validated licensing heartbeat and remains only in this process's memory.

        ``backend="local"`` points the same drill at 127.0.0.1:8765 instead: the
        local service bills its own CT_IN card and quota, so no key is used.
        """

        from api_lie_video import VideoDrillWorker

        from auto_lie_backend import host_ports, select_auto_lie_ws_endpoints

        if str(backend).casefold() == "local":
            return VideoDrillWorker(
                video=video,
                results=results,
                display=display,
                stop_event=stop_event,
                key="",
                seconds=seconds,
                fps=fps,
                transport="rtf1",
                aim_enabled=True,
                window_title=args.window_title,
                backend_opener=_local_lie_opener(fps),
            )
        # One randomly chosen host, with all of that host's ports handed over:
        # the drill's own probe then picks the best instance of that host.
        host, _port = select_auto_lie_ws_endpoints()[0]
        return VideoDrillWorker(
            video=video,
            results=results,
            display=display,
            stop_event=stop_event,
            key=key,
            seconds=seconds,
            fps=fps,
            transport="rtf1",
            host=host,
            ports=host_ports(host),
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

    def make_api_auto_lie_worker(backend="cloud"):
        """One automatic pass for one lie window - the SAME workflow as 测试api (the video drill).

        ``backend="local"`` runs the pass against 本地测谎 (127.0.0.1:8765): the
        frame, the capture, the aim and the outcome handling are identical, only
        the client changes, and no online 测谎密钥 is needed.
        """

        from api_lie_test import ApiLieTestWorker
        # The detector timestamp is attached by the UI before this worker is
        # started.  Connection/handshake therefore consumes part of the
        # three-second visual settle instead of starting a fresh wait.

        local = str(backend).casefold() == "local"
        auto_lie_logger.info(
            "自动过测谎: starting a pass for the lie window (%s)",
            "本地测谎 127.0.0.1:8765" if local else "云端测谎",
        )
        return ApiLieTestWorker(
            results=api_auto_lie_results,
            stop_event=stop_event,
            window_title=args.window_title,
            key="" if local else get_server_secret(),
            key_sender=key_sender,
            fps=5.0,
            transport="rtf1",
            # Kept only for the generic worker API.  outcome_controlled below
            # makes the live pass end on the game's actual result instead.
            duration=AUTO_LIE_FEED_DELAY_SECONDS + AUTO_LIE_TRACK_SECONDS,
            await_seconds=AUTO_LIE_FEED_DELAY_SECONDS,
            outcome_controlled=True,
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
            backend_opener=(_local_lie_opener(5.0) if local
                            else open_prepared_auto_lie_backend),
        )

    def _on_disconnect_event() -> None:
        """掉线: immediately dispatch the reconnect workflow."""

        try:
            accepted = reconnect_worker.notify_disconnect()
            logging.info(
                "auto reconnect: confirmed disconnect dispatch %s (worker_enabled=%s)",
                "queued" if accepted else "rejected",
                reconnect_worker.enabled,
            )
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

    def reload_imported_minimap_calibration() -> None:
        """Apply an imported minimap border to the running shared detector."""

        frame = bus.latest
        if frame is None:
            minimap_detector.reset_geometry()
            logging.warning(
                "MINIMAP imported calibration deferred: no shared frame is available"
            )
            return
        saved = config_store.read_section("minimap_calibration")
        detection = minimap_calibration_from_dict(saved, frame.image.size)
        if detection is None:
            minimap_detector.reset_geometry()
            logging.warning(
                "MINIMAP imported calibration is unavailable; cleared prior geometry"
            )
            return
        minimap_detector.seed_geometry(detection, frame.image.size)
        logging.info(
            "MINIMAP imported calibration applied source=%s | box=%s | client=%dx%d",
            detection.source, detection.window_box,
            frame.image.size[0], frame.image.size[1],
        )
    screen_blinker = ScreenBlinker(stop_event, enabled=False)
    telegram_notifier = TelegramNotifier(stop_event)
    # 钉钉提醒: the operator's group-robot webhook, armed by the 消息 selection.
    # One alert helper fans the key events (掉线 / 测谎 / 循环) out to every
    # enabled channel, so no worker has to know how many channels exist.
    dingtalk_notifier = DingTalkNotifier(stop_event)

    def _message_alert(event_type: str) -> None:
        """Publish one key event to the optional message channels."""

        telegram_notifier.notify(event_type)
        dingtalk_notifier.notify(event_type)

    # Passive diagnostic recorder: it writes only the existing shared frames;
    # it must never create a second GDI/window-capture loop beside auto-lie.
    screenshot_recorder = LieScreenshotRecorder(
        args.window_title, frame_queue=recorder_frames,
    )
    countdown_worker = CountdownWorker(
        stop_event,
        sound_path=Path(__file__).resolve().parent / "sound" / "dingdong.mp3",
        enabled=False,
        interval_hours=1.0,
        flash_callback=screen_blinker.request_blink,
        alert_callback=_message_alert,
        # 循环 screenshot/MP4 recording is commented out again on the operator's
        # word ("comment the 循环 again, screenshots have good color pattern now,
        # the picture is good again"): the temporary restore had only been there to
        # test the recorder's colours, and that is settled.  A countdown therefore
        # just flashes and notifies, exactly as before.
        #
        # The recorder keeps its entry point, so re-enabling is this one line:
        #     event_callback=screenshot_recorder.on_countdown,
        # (writes screenshots/countdown_<stamp>/frame_*.jpg + a composed MP4).
        event_callback=None,
    )
    lie_detector_worker = LieDetectorWorker(
        lie_detector_frames,
        stop_event,
        enabled=False,
        scan_interval=0.20,
        sound_path=Path(__file__).resolve().parent / "sound" / "dingdong.mp3",
        flash_callback=screen_blinker.request_blink,
        alert_callback=_message_alert,
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
            # When shared patrol capture is parked, diagnostics still consume
            # the exact parked-watch frames rather than opening another source.
            WatchFeed(
                recorder_frames,
                "诊断截图",
                armed_event=screenshot_recorder.recording_event,
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
        charge_active_callback=atomic_motion_runner.charge_active,
        charge_interrupt_callback=atomic_motion_runner.interrupt_charge,
    )
    # 增益1..5 have their own schedule: they must not stop because the HP/MP bar
    # read stopped, and a cast must never be judged by an HP/MP delta.  Delivery
    # still goes through MotionArbiter (safe-stage gate, attack-animation window,
    # one key at a time).
    buff_scheduler = BuffScheduler(
        key_sender,
        stop_event,
        config_provider=lambda: status_worker.detector.config,
        automation_active_event=automation_active,
        motion_arbiter=motion_arbiter,
        charge_active_callback=atomic_motion_runner.charge_active,
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
            # Two actual 5-FPS captures (~400 ms) confirm the new layer.
            # Do not rescale this legacy 4-FPS value: that silently turned
            # the requested two-frame confirmation into three frames.
            climb_layer_confirm_frames=2,
            climb_layer_confirm_seconds=float(
                # The two independent arrival frames are the safety gate.
                # Do not add a timed hold after them: at 5 FPS the next route
                # dispatch naturally follows on the next capture.
                0.0
            ),
            climb_arrival_world_tolerance=float(
                calibration.get("climb_arrival_world_tolerance", 0.20)
            ),
            climb_nudge_seconds=float(calibration.get("climb_nudge_seconds", 0.20)),
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
                min(0.5, calibration.get("climb_attempt_interval_seconds", 0.5))
            ),
            climb_failed_cycles_reset=int(
                calibration.get("climb_failed_cycles_reset", 3)
            ),
            climb_lateral_cycles_reset=int(
                calibration.get("climb_lateral_cycles_reset", 3)
            ),
            # Each layer owns its persisted ``patrol_times``.  This remains a
            # compatibility fallback only for incomplete legacy recordings.
            patrol_cycles_per_layer=int(
                calibration.get("patrol_cycles_per_layer", 1)
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
            stationary_recovery_active_event=stationary_recovery_active,
            stationary_attack_resume_event=stationary_attack_resume,
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
            # Three clear downward marker transitions are sufficient to prove
            # a real fall at 5 FPS.  The former stored value of 3 was scaled
            # to 4 for the old 4->5 FPS migration, so a short one-floor fall
            # could settle after exactly three transitions and never enter
            # landing reconciliation.
            fall_detect_frames=five_fps_frames(
                min(2, calibration.get("fall_detect_frames", 2)), 2
            ),
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
            rescue_probe_settle_seconds=float(
                calibration.get("rescue_probe_settle_seconds", 0.2)
            ),
            rescue_probe_move_threshold=float(
                calibration.get("rescue_probe_move_threshold", 0.006)
            ),
            drop_chord_hold_seconds=float(
                calibration.get("drop_chord_hold_seconds", 0.10)
            ),
            drop_retry_seconds=float(calibration.get("drop_retry_seconds", 0.9)),
            # The chord the jump-up point sends: Alt, the game's jump key.
            # ctrl+up / ctrl+end are this app's recording hotkeys and are never
            # sent to the client.
            jump_up_chord=str(calibration.get("jump_up_chord", "alt")),
            minimap_detector=minimap_detector,
            patrol_controller=patrol_controller,
            diamond_size_tracker=movement_diamond_tracker,
            structure_tracker=structure_tracker,
            automation_active_event=automation_active,
            # 自动重连: the falling edge of this event makes patrol re-check the route as its very
            # first act after the reconnect gives the input back.
            reconnect_active_event=reconnect_active,
            motion_arbiter=motion_arbiter,
            atomic_motion_runner=atomic_motion_runner,
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
            other_player_room_code=str(additional_settings.get("player_room_code", "")),
            other_player_wait_seconds=float(
                additional_settings.get(
                    "player_channel_wait_seconds",
                    float(additional_settings.get("player_channel_wait_minutes", 5.0)) * 60.0,
                )
            ),
            current_channel=int(
                additional_settings.get("auto_reconnect_channel", CHANNEL_DEFAULT)
            ),
            on_channel_landed=note_player_channel_landed,
            on_other_player_stop=note_other_player_stop,
            on_other_player_resume=prepare_patrol_after_other_player_channel,
            rescue_check_interval_seconds=float(
                calibration.get("rescue_check_interval_seconds", 300.0)
            ),
            rescue_stuck_frames=five_fps_frames(
                calibration.get("rescue_stuck_frames", 8), 5
            ),
    )
    # 随机休息: a short stand-still every 75-120 minutes.  The panel decides
    # whether it is enabled (and in which attack modes); this worker owns the
    # timing and asks MovementWorker to pause movement AND attack input.
    random_rest_worker = RandomRestWorker(
        stop_event,
        request_rest=movement_worker.request_rest,
        end_rest=movement_worker.cancel_rest,
        automation_active_event=automation_active,
    )
    motion_arbiter.set_micro_step_callback(movement_worker.perform_micro_step)
    movement_worker.set_attack_slot(attack_slot)
    jump_counterattack_hook = getattr(
        atomic_motion_runner, "set_jump_counterattack_executor", None,
    )
    if callable(jump_counterattack_hook):
        jump_counterattack_hook(movement_worker.execute_jump_counterattack_recoil)
    # 跳反打 is a 巡逻模式 + 跳打 feature: the runner asks the movement worker,
    # which owns the mode flags, before every counter attack beat.
    jump_counterattack_gate = getattr(
        atomic_motion_runner, "set_jump_counterattack_gate", None,
    )
    if callable(jump_counterattack_gate):
        jump_counterattack_gate(movement_worker.jump_counterattack_allowed_now)
    # 定时巡逻 stage of 站桩攻击: the attack gap is doubled while that stage runs.
    # MovementWorker owns the stage, so the cadence asks it instead of keeping a
    # second copy of the flag that could drift.
    attack_worker.stationary_patrol_stage_probe = (
        movement_worker.stationary_patrol_stage_active
    )
    # 跑花模式 uses the normal patrol route.  Only its combat cadence is
    # gated, through the movement-owned confirmed-layer snapshot.
    attack_worker.set_layer_attack_gate(
        False, movement_worker.flower_patrol_attack_allowed,
    )
    motion_arbiter.set_facing_callback(movement_worker.perform_stationary_facing)
    # 站桩攻击's final correction onto the 桩: one tiny step, deliberately
    # concurrent with the fixed attack cadence (see
    # MovementWorker.perform_stationary_step).
    motion_arbiter.set_step_callback(
        movement_worker.perform_stationary_step
    )
    motion_arbiter.set_counterattack_callback(
        movement_worker.perform_counterattack
    )
    # Status reads the HP bar while MovementWorker owns the minimap X history
    # and the arbiter-backed reaction.  Wire them only after both workers
    # exist; neither worker imports or controls the other directly.
    status_worker.hp_drop_callback = movement_worker.notify_hp_drop
    # 死花模式 uses 增益 1 (the UI's buff2 row) as its flower-to-flower
    # cadence. BuffScheduler owns its timer/key; MovementWorker owns only the
    # reached-flower gate and the subsequent private rope movement.
    buff_scheduler.buff2_flower_gate_callback = movement_worker.dead_flower_buff2_delivery_allowed
    buff_scheduler.buff2_flower_completed_callback = movement_worker.notify_dead_flower_buff2_delivered
    movement_worker.set_dead_flower_buff2_request(
        buff_scheduler.request_buff2_at_flower_point
    )
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

        started_at = time.monotonic()
        automation_active.clear()
        logging.info("STOP PATROL timing: automation gate cleared (0ms)")
        movement_worker.disarm_patrol_input()
        logging.info(
            "STOP PATROL timing: movement input disarmed after %.0fms",
            (time.monotonic() - started_at) * 1000.0,
        )
        motion_arbiter.cancel_pending("patrol lifecycle stopped")
        logging.info(
            "STOP PATROL timing: motion arbiter cancelled after %.0fms",
            (time.monotonic() - started_at) * 1000.0,
        )
        stair_jump_worker.cancel_pending("patrol lifecycle stopped")
        logging.info(
            "STOP PATROL timing: stair jump cancelled after %.0fms",
            (time.monotonic() - started_at) * 1000.0,
        )
        _stop_live_input(
            key_sender,
            automation_active,
            refocus_before_release=refocus_before_release,
        )
        logging.info(
            "STOP PATROL timing: live input released after %.0fms",
            (time.monotonic() - started_at) * 1000.0,
        )

    # 清理内存 owns only the logout/login lifecycle.  Once the existing game
    # reaches its login page it queues ReconnectWorker's dedicated handoff, rather
    # than reimplementing any world/channel/login steps here.
    auto_restart_worker = AutoRestartWorker(
        stop_event,
        key_sender,
        reconnect_worker,
        restart_active_event=auto_restart_active,
        stop_patrol=stop_patrol_for_disconnect,
        result_queue=reconnect_results,
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
        alert_callback=_message_alert,
        on_disconnect=stop_patrol_for_disconnect,
        disconnect_event_callback=_on_disconnect_event,
        # A missing marker alone is not an offline event: space zones can
        # hide the minimap.  Reconnect starts only after this same frame also
        # proves that the login page is visible.
        disconnect_login_page_check=reconnect_worker.login_page_visible_in_frame,
        # No disconnect alert while 自动重连 is running: its login screens have no yellow marker.
        reconnect_active_event=reconnect_or_restart_active,
    )
    focus_worker = FocusWorker(
        key_sender,
        stop_event,
        automation_active,
        game_focused,
        # A focus loss only pauses: patrol resumes by itself when the game is
        # focused again, and only the operator's Stop Patrol ends a run.
        # A lie pass disarms input but is fed by the same capture: keep the
        # foreground gate honest for the whole pass.
        lie_pass_event=lie_active,
        reconnect_active_event=reconnect_or_restart_active,
    )
    core_workers = [
        capture_worker,
        character_worker,
        movement_worker,
        status_worker,
        buff_scheduler,
        random_rest_worker,
        motion_arbiter,
        *attack_workers,
        small_step_worker,
        stair_jump_worker,
        *([hotkey_worker] if hotkey_worker is not None else []),
        reconnect_worker,
        auto_restart_worker,
        trade_worker,
        screen_blinker,
        countdown_worker,
        lie_detector_worker,
        parked_watch,
        telegram_notifier,
        dingtalk_notifier,
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
            random_rest_worker=random_rest_worker,
            small_step_worker=small_step_worker,
            hotkey_queue=hotkey_actions,
            hotkey_worker=hotkey_worker,
            on_hotkey_stop_prearmed=consume_hotkey_stop_prearmed,
            hotkey_stop_cleanup_done_event=hotkey_stop_cleanup_done,
            reconnect_worker=reconnect_worker,
            auto_restart_worker=auto_restart_worker,
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
            dingtalk_notifier=dingtalk_notifier,
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
            on_imported_minimap_calibration=reload_imported_minimap_calibration,
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
            channel_update_events=channel_update_events,
            other_player_stop_events=other_player_stop_events,
        )
    )

    if ui_worker is not None:
        # 自动过测谎 starts only when the grey lie square and the yellow player
        # marker appear in the SAME shared capture.  This replaces the old
        # timed window-recreation/refocus guard: a white/black login surface
        # has no player marker, while a real live lie scene does.  The probe
        # never captures another frame and uses a private geometry detector,
        # so it cannot disturb patrol's live minimap detector.
        auto_lie_marker_geometry: dict[tuple[int, int], object] = {}
        auto_lie_marker_detector = MinimapDetector(
            fallback_region=minimap_region,
            dedicated_crop=True,
            opencv_size=MINIMAP_ANALYSIS_SIZE,
        )
        auto_lie_marker_miss_logged = False

        def auto_lie_frame_has_marker(frame: object) -> bool:
            try:
                image = getattr(frame, "image", None)
                if image is None:
                    return False
                image_size = image.size
                detection = minimap_detector.retained_geometry(image_size)
                if detection is None:
                    detection = auto_lie_marker_geometry.get(image_size)
                if detection is None:
                    detection = minimap_calibration_from_dict(
                        config_store.read_section("minimap_calibration"), image_size
                    )
                    if detection is not None:
                        auto_lie_marker_geometry[image_size] = detection
                if detection is None:
                    # Automatic lie may be enabled before the first patrol
                    # start on a map. This private fallback runs only for a
                    # grey-box candidate and never alters patrol geometry.
                    detection = auto_lie_marker_detector.detect(image)
                marker_rgb = np.asarray(
                    image.crop(detection.analysis_box).convert("RGB")
                )
                return detect_yellow_diamond(marker_rgb) is not None
            except Exception:
                auto_lie_logger.debug(
                    "自动过测谎: same-frame marker gate could not inspect the candidate",
                    exc_info=True,
                )
                return False

        def forward_auto_lie_candidate(match: object, frame: object) -> None:
            nonlocal auto_lie_marker_miss_logged
            if match is None:
                auto_lie_marker_miss_logged = False
                ui_worker.on_lie_event_for_api(None)
                return
            if auto_lie_frame_has_marker(frame):
                auto_lie_marker_miss_logged = False
                ui_worker.on_lie_event_for_api(match)
                return
            if not auto_lie_marker_miss_logged:
                auto_lie_logger.info(
                    "自动过测谎: grey lie square ignored because the yellow marker "
                    "is absent in the same capture"
                )
                auto_lie_marker_miss_logged = True

        # The callback only raises a flag; the panel's Tk thread services it,
        # so nothing Tk is touched from the detector thread.
        lie_detector_worker.add_lie_seen_callback(
            forward_auto_lie_candidate
        )

    def auto_lie_is_active() -> bool:
        return bool(ui_worker is not None and ui_worker.auto_lie_pass_active())

    def cancel_auto_lie() -> bool:
        return bool(ui_worker is not None and ui_worker.request_cancel_auto_lie_pass())

    def cancel_auto_reconnect() -> bool:
        """Cancel the whole reconnect mission without creating diagnostics."""

        return bool(reconnect_worker.request_cancel())

    workflow_cancel_worker = WorkflowCancelWorker(
        stop_event,
        (
            ("auto reconnect", reconnect_worker.is_active, cancel_auto_reconnect),
            ("auto restart", auto_restart_worker.is_active, auto_restart_worker.request_cancel),
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
        "MoonEclipse %s starting from %s",
        version_label(),
        Path(__file__).resolve().parent,
    )
    logging.info(
        "assistant running (%s); click Start Patrol to enable input; Ctrl+C stops",
        "DRY‑RUN" if args.dry_run else "LIVE INPUT DISARMED",
    )
    try:
        if ui_worker is not None:
            logging.info("opening MoonEclipse Debug UI")
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
        # 本地测谎: stop the service only if this app started it; a service the
        # operator launched by hand keeps running.
        try:
            import local_lie_service

            local_lie_service.stop_supervisor()
            local_lie_service.stop_started_service()
        except Exception:  # noqa: BLE001 - never block the shutdown
            logging.getLogger(__name__).debug(
                "local lie service stop failed", exc_info=True
            )
        if ui_log_handler is not None:
            root_logger.removeHandler(ui_log_handler)
        root_logger.removeHandler(trace_log_handler)
        server_logger.removeHandler(server_log_handler)
        for auto_lie_log_name in (
            "auto-lie", "api_auto_lie", "api_lie_test", "autolie_api",
        ):
            logging.getLogger(auto_lie_log_name).removeHandler(auto_lie_log_handler)
        ui_poll_logger.removeHandler(ui_poll_log_handler)
        # Writers own file close/flush.  They drain only their current bounded
        # record batches and never hold a live gameplay worker at shutdown.
        auto_lie_log_writer.close(drain_timeout=1.0)
        ui_poll_log_writer.close(drain_timeout=1.0)
        server_log_writer.close(drain_timeout=1.0)
        trace_log_writer.close(drain_timeout=1.0)
        _release_single_instance_mutex(singleton_handle)
        # Last step, after every writer is closed: a capture thread wedged inside
        # Win32 would otherwise keep the process (and the mutex) alive after the
        # window is gone.  See ``_force_exit_if_a_thread_is_stuck``.
        _force_exit_if_a_thread_is_stuck()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
