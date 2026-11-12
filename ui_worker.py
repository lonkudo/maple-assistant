"""Independent Tk debug dashboard fed by the capture frame bus."""

from __future__ import annotations

import collections
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import logging
import queue
import random
import re
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageTk

from marker_detector import DiamondSizeTracker, detect_yellow_diamond
from map_identity import MapIdentityStore
from map_structure_tracker import MapStructureTracker
from minimap_detector import (
    Box,
    MinimapDetection,
    MinimapDetector,
    is_verified_border,
)
from patrol_control import CoordinateLayout, PatrolController
from status_worker import apply_drug_settings, BINDABLE_KEYS, WindowKeySender
from config_store import config_section_file
from game_chat import send_game_chat_message
from countdown_worker import play_mp3
from licensing import (
    LicenseStatus,
    preview_activation_transfer_via_server, confirm_activation_transfer_via_server,
    revoke_license, set_device_memo_via_server, unbind_device_via_server,
    has_sealed_fingerprint, validate_via_server, verify_license,
)
from lie_accounting import LieAccountingWorker
from reconnect_worker import (
    CHANNEL_DEFAULT,
    CHANNEL_MAX,
    CHANNEL_MIN,
    WORLD_NAMES,
    click_screen,
    valid_channel,
)
from timer_state import load_timer_state, save_timer_state, timer_state_path
from versioning import read_version, version_label
from update_core import (
    ReleaseInfo, UpdateCoreError, announced_version, apply_staged_update,
    clear_staged_updates, fetch_release_info, is_newer, read_notice,
    stage_update, write_notice,
)
from update_manager import (
    UpdateError, desktop_roots, export_user_config, import_user_config,
    find_newer_desktop_update,
    restore_user_config_bytes,
    schedule_hidden_restart,
    schedule_package_update,
)
from runtime_paths import application_root, package_format
from dingtalk_notifier import (
    format_dingtalk_message, normalize_webhook as normalize_dingtalk_webhook,
    send_in_background as send_dingtalk_in_background,
)
from crash_reporter import record_current_exception, record_exception
from ui_hang_watchdog import UiHangWatchdog
from auto_lie_secret import clear_server_secret, get_server_secret, has_server_secret
from auto_lie_outcome import (
    LieSuccessBoxDetector, SUCCESS_CONSECUTIVE_FRAMES, SUCCESS_THRESHOLD,
)


LOG = logging.getLogger(__name__)
# High-volume UI poll boundary diagnostics have their own asynchronous file.
# They never propagate into assistant.log or the Tk running-log history.
UI_POLL_LOG = logging.getLogger("ui-poll")
# Per-poll timing traces were temporary diagnosis telemetry.  Keep the trace
# hooks available, but leave their high-volume logging disabled in releases.
UI_POLL_TRACE_LOGGING = False
_DEVICE_MEMO_PATTERN = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff _-]{0,24}$")
# Automatic lie workflow diagnostics are deliberately isolated from the
# regular patrol/application log.  The application configures this named
# logger to write only to auto_lie.log.
AUTO_LIE_LOG = logging.getLogger("auto-lie")

# Hotkey actions that TYPE into the game: they need live input armed, so they re-arm it themselves
# when the auto-reconnect (or a failed run) left it off.  Recording/selection chords do not type and
# are deliberately absent.
# Trade actions use ``send_direct_keys`` inside TradeWorker, which is scoped
# to its explicit dialog sequence and works while general patrol input is
# disarmed.  They must not be included here: re-arming general input on
# Ctrl+Q/Ctrl+W can wake unrelated attack workers while patrol is stopped.
TYPING_HOTKEY_PREFIXES = ("quick_message:",)

# The auto-lie result dialog needs both measured confirmation points, twice
# each.  Wider clients follow the 1366 reference by width.  The 1080 preset
# uses separately measured coordinates: its second point keeps the same X as
# its first point and applies the reference's 32-pixel upward Y shift.
_AUTO_LIE_CONFIRM_1080_SIZE = (1080, 768)
_AUTO_LIE_CONFIRM_1080_POINTS = ((630, 428), (630, 396))
_AUTO_LIE_CONFIRM_REFERENCE_WIDTH = 1366
_AUTO_LIE_CONFIRM_REFERENCE_POINTS = ((800, 472), (800, 440))
_AUTO_LIE_CONFIRM_CLICKS_PER_POINT = 2
# Completing a lie round can leave the API/game transition owning focus for a
# moment.  A click during that handoff lands on the desktop even when the point
# is correct, so the confirmation is deliberately gated on a settled game
# foreground check.
_AUTO_LIE_CONFIRM_FOCUS_SETTLE_SECONDS = 0.20
_AUTO_LIE_CONFIRM_FOCUS_ATTEMPTS = 3
# The black-room failure signature is the yellow minimap marker staying gone
# after the prompt click.  A grace period avoids counting a normal transition.
_AUTO_LIE_FAILURE_GRACE_SECONDS = 3.0
_AUTO_LIE_FAILURE_MISSING_FRAMES = 15
_AUTO_LIE_OUTCOME_ARM_SECONDS = 10.0
_AUTO_LIE_OUTCOME_OBSERVE_SECONDS = 40.0


def _auto_lie_confirm_client_points(
    client_width: int, client_height: int,
) -> tuple[tuple[int, int], ...]:
    """Return the measured post-lie confirmation points in client pixels."""

    width = max(1, int(client_width))
    height = max(1, int(client_height))
    if (width, height) == _AUTO_LIE_CONFIRM_1080_SIZE:
        return _AUTO_LIE_CONFIRM_1080_POINTS
    scale = width / float(_AUTO_LIE_CONFIRM_REFERENCE_WIDTH)
    return tuple(
        (int(round(x * scale)), int(round(y * scale)))
        for x, y in _AUTO_LIE_CONFIRM_REFERENCE_POINTS
    )

# 自动过测谎 debouncing.  The lie detector reports a NEW event when it sees the square again after it
# was gone (and None when it clears), but its detection can flicker frame to frame, so the consumer
# must decide what counts as ONE lie window:
#   * one pass per WINDOW: while a pass is running, further events of the same window (the square
#     flickers) are ignored - the first alarm is accepted, the repeats are not,
#   * and after a pass, the next one is never closer than AUTO_LIE_MIN_PASS_GAP_SECONDS.
#
# The "the square must be gone for 3 s before a window counts as new" rule is GONE (v1.0.27): it made
# the automatic pass silently never run when the square flickered back inside those 3 s - the field
# report was "the lie event happened, but the autolie_api is not taking over", with no line in the log
# saying why.  Quota is protected by the minimum gap between passes instead, and every skip is logged.
AUTO_LIE_MIN_PASS_GAP_SECONDS = 10.0
# A lie window that arrives without an in-memory credential is reported once per
# window and then left alone.  It deliberately does NOT request a heartbeat: the
# per-window request this used to make (bounded to 3 attempts, 10 s apart) turned
# a run of lie windows into a run of licensing round trips.  The heartbeat is
# sent only at application startup, on its own three-hour/midnight schedule, and
# from the operator's caption refresh control.
# A repeating debug-UI poll failure logs a full traceback this often; the ones in between are DEBUG.
POLL_FAILURE_LOG_SECONDS = 10.0
# Diagnostic threshold only. Timing is registered in memory first and emitted
# through the asynchronous log writer only when the whole poll is slow.
UI_POLL_SLOW_TOTAL_SECONDS = 0.25
# A lie event waiting for an already-running pass is dropped after this long, so a stuck pass can
# never keep the pending flag set (and this method retrying) for the rest of the session.
AUTO_LIE_PENDING_MAX_SECONDS = 90.0
@dataclass(frozen=True)
class _AutoLieDetectorEvent:
    """One detector match with the monotonic time at which it was observed."""

    match: object
    detected_at: float


# The debug UI uses two columns (controls + debug/YOLO); the initial
# window (including the in-window caption bar when active) is compact by
# default while remaining user-resizable.
# The height stays user-resizable (only the minimum is enforced).
_INITIAL_WINDOW_WIDTH = 980
_INITIAL_WINDOW_HEIGHT = 560
# Every panel in the left stack follows this single compact track.  The
# right-side controls retain their own wider track because their key bindings
# and numeric values need it.
_LEFT_COLUMN_WIDTH = 375
# Direct-value controls replaced the wide right-column slider troughs.  The
# widest visible right row now fits comfortably in this tighter track.
_RIGHT_COLUMN_WIDTH = 393
# The outer 8% at each side never contains a recordable layer element. Crop
# it from the UI axis so the useful middle 84% gets a wider canvas.
# The layer row also contains a radio selector and its label.  296px left the
# row itself requesting more than the 360px column and silently widening every
# left-side panel. The patrol-count value needs 10px more room for its hover
# text, so the axis uses a fixed 206px lane rather than taking that space.
_LAYER_AXIS_WIDTH = 206
_LAYER_AXIS_HEIGHT = 38
_LAYER_AXIS_LEFT = 0
_LAYER_AXIS_RIGHT = _LAYER_AXIS_WIDTH
_LAYER_AXIS_Y = 28
_LAYER_AXIS_VISIBLE_MIN_X = 0.08
_LAYER_AXIS_VISIBLE_MAX_X = 0.92
# The columns are fixed at 380px and 515px respectively.  The compact initial
# geometry leaves room for each panel's controls without retaining unused
# drawing space at the right of the patrol axis.
# The default height is deliberately compact; users can still enlarge it and
# their saved window size is never overwritten.

# Height of the custom in-window caption bar.  Tk cannot add widgets into
# the native OS caption, so the assistant window removes the native caption
# (keeping the OS resize borders and taskbar entry) and draws its own title
# row: app title on the left, then ？/－/□/× on the right at the same level.
_CAPTION_HEIGHT = 34

# The caption ↻ button no longer hunts for a newer package.  It forces one
# entitlement heartbeat now and refreshes the status shown in the UI, then
# stays greyed for this long: a burst of clicks must not hammer the activation
# service (ten failed validations inside 24 hours ban the fingerprint).
LICENSE_REFRESH_COOLDOWN_SECONDS = 60.0
# "Aggressive" means a failure is retried inside the same press instead of
# waiting for the three-hour timer.  Only a transport failure qualifies - the
# heartbeat never reached the server, so nothing was counted against this
# device.  A typed refusal is final in both directions: retrying it would spend
# the ten failures that ban a fingerprint.
LICENSE_REFRESH_RETRY_ATTEMPTS = 3
LICENSE_REFRESH_RETRY_SECONDS = 1.0
# A failed online heartbeat immediately locks new UI actions. It receives one
# scheduled confirmation before running automation is stopped, so a brief
# service outage does not cut off an already-running safe session.
LICENSE_HEARTBEAT_FAILURE_RETRY_SECONDS = 10 * 60
# Windows can launch the helper before CIM/WMI has exposed motherboard and
# firmware signals.  These are deliberate *CIM-only* retries; a server refusal
# is never retried here because that could consume activation-failure quota.
CIM_BOOT_RETRY_SECONDS = (3.0, 30.0, 60.0, 300.0)
# The header stays on "正在验证在线授权…" and the panel stays locked while the
# online check is pending, so a check that never reports back leaves the whole
# application unusable with no way out ("the authorization never finished, the
# application crashed").  After this long the pending state resolves as a
# failure - the ↻ button and the next scheduled heartbeat keep trying in the
# background - so the operator always gets an actionable screen back.
LICENSE_PENDING_WATCHDOG_SECONDS = 75.0
_GMT8 = timezone(timedelta(hours=8))

# --- 更新图标 -----------------------------------------------------------------
# The caption's second action turns green when the heartbeat announces a release
# newer than this build.  The heartbeat only carries the version, so the notes and
# link are fetched once per version and cached on disk (``update_info.json`` next
# to VERSION) - a user who skips the update still sees the notice next start.
UPDATE_ICON_GREY = "#8a8a8a"
UPDATE_ICON_GREEN = "#2e9e4f"
UPDATE_ICON_INK_GREY = "#7d8794"
UPDATE_ICON_INK_GREEN = "#ffffff"
UPDATE_ICON_BUSY = "#2f6fd0"
# Tooltip line budget: enough for the version, a few note lines and the hint.
UPDATE_NOTICE_TOOLTIP_LINES = 8
# --- 自动测谎 backend group ------------------------------------------------------
# The old 自动测谎 checkbox is a three-way group now, because the lie pass has two
# backends: the vendor cloud service (unchanged) and the CT partner API that the
# local package runs on 127.0.0.1:8765.  ``off`` keeps the old unticked state.
AUTOLIE_BACKEND_OFF = "off"
AUTOLIE_BACKEND_CLOUD = "cloud"
AUTOLIE_BACKEND_LOCAL = "local"
# 测试云端测谎 is built and wired (same command as 测试本地测谎, the drill already
# reaches the cloud backend when 云端测谎 is selected) but the operator keeps it
# hidden: every cloud run charges the online 测谎次数, so only 测试本地测谎 - which
# the CT card pays for - is exposed.  Flip this to True to bring the button back;
# nothing else has to change.
API_TEST_CLOUD_BUTTON_VISIBLE = False
# 本地测谎 probe cadence.  The service validates its licence and handshakes the
# coordinate cloud BEFORE it listens on 8765 (~5-20 s cold), so the first probe
# after selecting it usually sees "connection refused" (field log 00:34:38).  The
# probe retries a few times, and the poll re-probes while the panel is not
# connected, so the hint heals itself once the service is up.
LOCAL_LIE_PROBE_ATTEMPTS = 5
LOCAL_LIE_PROBE_INTERVAL_SECONDS = 2.0
LOCAL_LIE_PROBE_RETRY_SECONDS = 10.0
# The app owns the service: if 8765 is still down this long after the previous
# start attempt, start it again (never ask the operator to run the launcher).
LOCAL_LIE_START_RETRY_SECONDS = 45.0


def seconds_until_next_gmt8_midnight_refresh() -> float:
    """Return the delay until the next GMT+8 daily refresh window.

    The server calculates the visible daily auto-lie totals in GMT+8.  A
    normal entitlement heartbeat is intentionally infrequent, so add this
    calendar boundary wake-up rather than leaving a running dashboard showing
    yesterday's figures until its next three-hour check.  A short random
    delay prevents every active desktop from calling the server at 00:00:00.
    """

    now = datetime.now(_GMT8)
    next_midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(
        1.0,
        (next_midnight - now).total_seconds() + random.uniform(0.0, 60.0),
    )


def license_refresh_retryable(status: LicenseStatus) -> bool:
    """True when a failed heartbeat may be retried without risking a ban.

    ``validate_via_server`` reports a typed ``server:<CODE>`` refusal when the
    activation service answered and rejected this device - that answer already
    counted as a failed attempt for the fingerprint, and ten of them inside 24
    hours ban it, so it is never retried.  The generic ``server`` code means no
    usable answer arrived at all (network, timeout, TLS, unparsable body), and
    ``device_not_ready`` means the local fingerprint was not ready so no request
    was sent: both are transport-level and safe to repeat immediately.
    """

    return status.code in {"server", "device_not_ready"}

# 测试api on a video: the operator's measured run length is ~30s at the API's 5 fps, and the panel
# no longer offers a box for it (the 密钥 button is gone too: the product key ships inside the
# application, see autolie_api/key_store.py).
API_TEST_VIDEO_SECONDS = 30.0


def tooltip_cursor_top_right_position(
    pointer_x: int,
    pointer_y: int,
    tooltip_width: int,
    tooltip_height: int,
    monitor_work_area: tuple[int, int, int, int],
) -> tuple[int, int]:
    """Place a tooltip at cursor upper-right on the cursor's own monitor."""

    left, top, right, bottom = monitor_work_area
    x = pointer_x + 14
    x = max(left + 4, min(x, max(left + 4, right - tooltip_width - 4)))
    y = pointer_y - tooltip_height - 10
    y = max(top + 4, min(y, max(top + 4, bottom - tooltip_height - 4)))
    return x, y


def _geometry_with_caption(
    geometry: str, caption_height: int = _CAPTION_HEIGHT
) -> str:
    """Add the custom caption height to a stored content geometry.

    Saved geometries describe the CONTENT area only (the same meaning they
    had before the custom caption existed), so restoring adds the caption
    back on top of the client area.
    """

    parsed = _parse_window_geometry(geometry)
    if parsed is None:
        return geometry
    width, height, x, y = parsed
    return f"{width}x{height + int(caption_height)}{x:+d}{y:+d}"


def _geometry_without_caption(
    geometry: str, caption_height: int = _CAPTION_HEIGHT
) -> str:
    """Strip the custom caption height before persisting content geometry."""

    parsed = _parse_window_geometry(geometry)
    if parsed is None:
        return geometry
    width, height, x, y = parsed
    height = max(height - int(caption_height), 200)
    return f"{width}x{height}{x:+d}{y:+d}"


def _window_geometry_settings_path() -> Any:
    """Persisted debug UI window geometry (position + size)."""
    return config_section_file("ui_window")


def _parse_window_geometry(
    geometry: str,
) -> Optional[tuple[int, int, int, int]]:
    """Parse a Tk geometry string ``WxH+X+Y`` -> (width, height, x, y).

    Returns None for malformed or non-positive input, mirroring the
    strictness of the loader so a corrupt settings file cannot crash the UI.
    """
    match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", str(geometry).strip())
    if not match:
        return None
    width, height, x, y = (int(part) for part in match.groups())
    if width <= 0 or height <= 0:
        return None
    return width, height, x, y


def _clamp_window_geometry(
    geometry: str,
    screen_width: int = 1920,
    screen_height: int = 1080,
    min_width: int = _INITIAL_WINDOW_WIDTH,
    min_height: int = 560,
    max_height: Optional[int] = None,
) -> str:
    """Keep a restored window fully on-screen and at least the minimum size.

    A saved position far off-screen (e.g. after a monitor change) would
    otherwise open the debug UI somewhere invisible; the window must stay
    editable/movable, so the clamp makes sure it can always be grabbed.
    """
    parsed = _parse_window_geometry(geometry)
    if parsed is None:
        return f"{min_width}x{min_height}+40+40"
    width, height, x, y = parsed
    width = max(min_width, min(width, max(min_width, screen_width)))
    height_limit = max(min_height, screen_height)
    if max_height is not None:
        height_limit = min(height_limit, max(min_height, int(max_height)))
    height = max(min_height, min(height, height_limit))
    x = max(0, min(x, max(0, screen_width - width - 8)))
    y = max(0, min(y, max(0, screen_height - height - 40)))
    return f"{width}x{height}+{x}+{y}"


# Format of the persisted window-geometry record.  Bumped whenever the
# meaning of the stored geometry changes (whole-window vs content-only vs
# caption semantics or DPI-aware coordinate space), so machines with a
# stale file from an older release fall back to the current default once
# instead of restoring a mismatched size (e.g. 1275x904 / 1620x1050 /
# 1635x1272 from an older layout, a remembered manual resize, or a
# DPI-aware run that stored physical-pixel sizes).
_GEOMETRY_FORMAT = 17


def _load_window_geometry(default_geometry: str) -> str:
    """Return the saved debug UI geometry, or ``default_geometry`` when the
    settings file is missing/corrupt/stale.  Never raises.

    Only records written by the CURRENT geometry format are honored: older
    files (different caption/size semantics) are ignored once so the window
    opens at the intended initial size on every machine.
    """
    try:
        data = json.loads(
            _window_geometry_settings_path().read_text(encoding="utf-8")
        )
        if int(data.get("format", 0)) != _GEOMETRY_FORMAT:
            return default_geometry
        geometry = data.get("geometry")
        if isinstance(geometry, str) and _parse_window_geometry(geometry):
            return geometry
    except Exception:
        pass
    return default_geometry


def _save_window_geometry(geometry: str) -> None:
    """Persist the debug UI window geometry for the next startup."""
    try:
        _window_geometry_settings_path().write_text(
            json.dumps({"format": _GEOMETRY_FORMAT, "geometry": geometry},
                       indent=2)
            + "\n",
            encoding="utf-8",
        )
    except Exception:
        LOG.debug("could not save debug UI window geometry", exc_info=True)


def monitor_work_area_for_pointer(
    pointer_x: int,
    pointer_y: int,
) -> tuple[int, int, int, int]:
    """Return the Windows work area for the monitor containing the pointer."""

    try:
        import win32api
        import win32con

        monitor = win32api.MonitorFromPoint(
            (pointer_x, pointer_y), win32con.MONITOR_DEFAULTTONEAREST
        )
        return tuple(int(value) for value in win32api.GetMonitorInfo(monitor)["Work"])
    except Exception:
        # Last-resort virtual desktop bounds; Tk reports these in the same
        # coordinate space as winfo_pointerx/y.
        import tkinter as tk

        root = tk._default_root
        if root is not None:
            left = int(root.winfo_vrootx())
            top = int(root.winfo_vrooty())
            return (
                left,
                top,
                left + int(root.winfo_vrootwidth()),
                top + int(root.winfo_vrootheight()),
            )
        return (0, 0, 1920, 1080)


_HOVER_TOOLTIP_MAX_WIDTH = 480


class HoverTooltip:
    """Small cursor-adjacent tooltip that also works on disabled ttk buttons."""

    def __init__(
        self, widget: Any, text: str, delay_ms: int = 250,
        wraplength: int = _HOVER_TOOLTIP_MAX_WIDTH,
    ) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = max(0, int(delay_ms))
        # Every hover hint has one consistent maximum width. Individual
        # callers may request a narrower hint, but never an unbounded one.
        self.wraplength = min(
            _HOVER_TOOLTIP_MAX_WIDTH, max(0, int(wraplength))
        )
        self.enabled = False
        self._after_id: Any = None
        self._window: Any = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if not self.enabled:
            self._hide()

    def _schedule(self, _event: Any = None) -> None:
        self._cancel()
        if self.enabled:
            self._after_id = self.widget.after(self.delay_ms, self._show)

    def _show(self) -> None:
        self._after_id = None
        if not self.enabled or self._window is not None:
            return
        import tkinter as tk

        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        label = tk.Label(
            window,
            text=self.text,
            justify="left",
            background="#fffbd6",
            foreground="#202020",
            relief="solid",
            borderwidth=1,
            padx=7,
            pady=4,
            # Wrap instead of growing the tooltip to the full text length
            # (the bindable-hotkeys hint is long and would otherwise extend
            # off-screen / over the UI).
            wraplength=self.wraplength,
        )
        label.pack()
        window.update_idletasks()
        pointer_x = self.widget.winfo_pointerx()
        pointer_y = self.widget.winfo_pointery()
        x, y = tooltip_cursor_top_right_position(
            pointer_x,
            pointer_y,
            window.winfo_reqwidth(),
            window.winfo_reqheight(),
            monitor_work_area_for_pointer(pointer_x, pointer_y),
        )
        window.wm_geometry(f"+{x}+{y}")
        self._window = window

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _hide(self, _event: Any = None) -> None:
        self._cancel()
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
            self._window = None

    def destroy(self) -> None:
        self._hide()


class UiLogHandler(logging.Handler):
    """Feed formatted logs to Tk without an unbounded backlog or disk I/O.

    The UI panel shows only SIGNIFICANT events - patrol started, patrol
    ended, and ERROR+ (critical bugs) - while a bounded in-memory history
    keeps the latest ``history`` formatted lines (default 500) for the
    "copy running log" action.  Oldest lines are dropped like garbage once
    the history is full; nothing is written to disk here (the ordinary file
    handler owns the on-disk log).
    """

    def __init__(self, capacity: int = 300, history: int = 500) -> None:
        super().__init__()
        self.messages: "queue.Queue[str]" = queue.Queue(maxsize=max(20, capacity))
        self._history: "collections.deque[str]" = collections.deque(
            maxlen=max(50, int(history))
        )
        self._lock = threading.Lock()

    @staticmethod
    def _significant(record: logging.LogRecord, message: str) -> bool:
        if record.levelno >= logging.ERROR:
            # Critical bugs (LOG.error / LOG.exception / CRITICAL).
            return True
        return (
            LOG_RUN_START in message
            or LOG_RUN_STOP in message
            or LOG_CONFIG_IMPORT in message
            or LOG_CONFIG_EXPORT in message
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            with self._lock:
                self._history.append(message)
            if not self._significant(record, message):
                return
            while True:
                try:
                    self.messages.put_nowait(message)
                    return
                except queue.Full:
                    try:
                        self.messages.get_nowait()
                    except queue.Empty:
                        return
        except Exception:
            self.handleError(record)

    def history_text(self) -> str:
        """Latest retained log lines, oldest first (bounded, in memory)."""

        with self._lock:
            return "\n".join(self._history)


# The 运行日志 panel displays only significant events: patrol started /
# patrol ended and ERROR+ records (critical bugs).  The full recent stream
# is retained in memory (UiLogHandler history, default 500 lines) and is
# copied to the clipboard by the archive icon button.
LOG_RUN_START = "巡逻已开始"
LOG_RUN_STOP = "巡逻已停止"
LOG_CONFIG_IMPORT = "配置导入"
LOG_CONFIG_EXPORT = "配置导出"


def _make_log_icon(kind: str, master: Any) -> ImageTk.PhotoImage:
    """16x16 monochrome glyph for the running-log action buttons.

    ``archive`` = a document sheet (copy the running log); ``user`` = a
    person silhouette (copy the user settings); ``server`` = a compact
    linked-node glyph (copy server_client.log); ``auto_lie`` = a target
    frame (copy auto_lie.log). Drawn with PIL so the glyphs render
    identically on every Windows theme/font set.
    """

    size = 16
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    ink = (70, 70, 70, 255)
    paper = (248, 248, 248, 255)
    if kind == "archive":
        # Document sheet with a folded corner and two content lines.
        draw.rectangle((2, 3, 13, 14), fill=paper, outline=ink)
        draw.line((2, 3, 7, 3, 10, 6), fill=ink)  # folded corner hint
        draw.line((5, 8, 12, 8), fill=ink)
        draw.line((5, 11, 12, 11), fill=ink)
    elif kind == "user":
        # Head + shoulders silhouette.
        draw.ellipse((4, 1, 12, 9), outline=ink)
        draw.arc((1, 8, 15, 20), start=180, end=360, fill=ink)
    elif kind == "server":
        draw.rounded_rectangle((1, 5, 7, 11), radius=1, fill=paper, outline=ink)
        draw.rounded_rectangle((9, 2, 15, 8), radius=1, fill=paper, outline=ink)
        draw.line((7, 7, 9, 5), fill=ink)
        draw.point((3, 8), fill=ink)
        draw.point((11, 5), fill=ink)
    elif kind == "auto_lie":
        draw.rectangle((2, 2, 13, 13), outline=ink)
        draw.ellipse((5, 5, 10, 10), outline=ink)
        draw.point((7, 7), fill=ink)
    else:
        raise ValueError(f"unknown log icon kind: {kind!r}")
    # The UI is built on an explicit Tk root.  Letting ImageTk choose the
    # implicit default root can create ``pyimage`` in a different Tcl
    # interpreter, after which ttk rejects it and the whole UI (and hotkey
    # worker) shuts down during startup.
    return ImageTk.PhotoImage(image, master=master)


def _make_teaching_icon(master: Any) -> ImageTk.PhotoImage:
    """Return a clearly legible book glyph for the caption action row."""

    size = 20
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    ink = (45, 70, 95, 255)
    paper = (216, 232, 244, 255)
    # A closed-book silhouette reads reliably at caption-bar scale.  The
    # previous open-page silhouette was visually too close to a chess piece.
    draw.rounded_rectangle((3, 2, 16, 17), radius=1, fill=paper, outline=ink, width=2)
    draw.line((6, 3, 6, 16), fill=ink, width=2)  # book spine
    draw.line((9, 6, 14, 6), fill=ink, width=1)
    draw.line((9, 9, 14, 9), fill=ink, width=1)
    draw.line((9, 12, 13, 12), fill=ink, width=1)
    return ImageTk.PhotoImage(image, master=master)


@dataclass(frozen=True)
class DebugSnapshot:
    sequence: int
    captured_at: datetime
    client_size: tuple[int, int]
    detection: MinimapDetection
    minimap_preview: Image.Image
    map_name_preview: Image.Image
    configured_map_name: str
    player_x: Optional[float]
    player_y: Optional[float]
    marker_confidence: float
    marker_pixel_size: Optional[tuple[int, int]]
    coordinate_layout: Optional[CoordinateLayout]
    scroll_y_diamonds: float = 0.0
    world_y_diamonds: Optional[float] = None
    structure_confidence: float = 0.0
    structure_mode: str = "disabled"


def build_debug_snapshot(
    frame: Any,
    detector: MinimapDetector,
    configured_map_name: str = "",
    diamond_size_tracker: Optional[DiamondSizeTracker] = None,
    structure_tracker: Optional[MapStructureTracker] = None,
) -> DebugSnapshot:
    """Pure frame-to-view-model conversion, independently testable from Tk."""

    detection = detector.detect(frame.image)
    analysis_image = frame.image.crop(detection.analysis_box)
    analysis_left, analysis_top, analysis_right, analysis_bottom = detection.analysis_box
    canvas_left, canvas_top, canvas_right, canvas_bottom = detection.canvas_box
    marker = detect_yellow_diamond(np.asarray(analysis_image.convert("RGB")))
    coordinate_layout = None
    if marker is not None:
        marker_width, marker_height = marker.pixel_size
        if diamond_size_tracker is not None:
            marker_width, marker_height = diamond_size_tracker.stabilize(
                (marker_width, marker_height)
            )
        coordinate_layout = CoordinateLayout(
            analysis_width=analysis_right - analysis_left,
            analysis_height=analysis_bottom - analysis_top,
            canvas_left=canvas_left - analysis_left,
            canvas_top=canvas_top - analysis_top,
            canvas_width=canvas_right - canvas_left,
            canvas_height=canvas_bottom - canvas_top,
            diamond_width=marker_width,
            diamond_height=marker_height,
        )
    tracking = (
        structure_tracker.analyze(frame, detection, marker)
        if structure_tracker is not None else None
    )
    return DebugSnapshot(
        sequence=frame.sequence,
        captured_at=frame.captured_at,
        client_size=frame.image.size,
        detection=detection,
        minimap_preview=frame.image.crop(detection.window_box),
        map_name_preview=frame.image.crop(detection.map_name_box),
        configured_map_name=configured_map_name,
        player_x=marker.x if marker is not None else None,
        player_y=marker.y if marker is not None else None,
        marker_confidence=marker.confidence if marker is not None else 0.0,
        marker_pixel_size=(marker_width, marker_height) if marker is not None else None,
        coordinate_layout=coordinate_layout,
        scroll_y_diamonds=(tracking.scroll_y_diamonds if tracking else 0.0),
        world_y_diamonds=(tracking.world_y_diamonds if tracking else None),
        structure_confidence=(tracking.confidence if tracking else 0.0),
        structure_mode=(tracking.mode if tracking else "disabled"),
    )


class DebugSnapshotWorker(threading.Thread):
    """Build best-effort dashboard snapshots away from Tk's event loop.

    This worker is deliberately display-only.  It owns a private minimap
    detector and does not update the movement/route trackers, so dropping an
    old debug frame can never change a patrol decision.  Both queues hold one
    item: when the dashboard is busy, only the newest capture is worth
    rendering.
    """

    def __init__(self, stop_event: threading.Event, detector: MinimapDetector) -> None:
        super().__init__(name="debug-snapshot", daemon=True)
        self.stop_event = stop_event
        self._requests: "queue.Queue[tuple[Any, str]]" = queue.Queue(maxsize=1)
        self._results: "queue.Queue[DebugSnapshot]" = queue.Queue(maxsize=1)
        # MinimapDetector carries smoothing/history state.  A private copy
        # keeps the diagnostic view from racing movement's live detector.
        self.detector = MinimapDetector(
            fallback_region=detector.fallback_region,
            map_name_reader=detector.map_name_reader,
            dedicated_crop=detector.dedicated_crop,
            opencv_size=detector.opencv_size,
            transient_hold_seconds=detector.transient_hold_seconds,
            box_history=detector.box_history_len,
            box_jump_ratio=detector.box_jump_ratio,
            include_header=detector.include_header,
        )

    @staticmethod
    def _put_latest(target: "queue.Queue[Any]", item: Any) -> None:
        try:
            target.put_nowait(item)
            return
        except queue.Full:
            pass
        try:
            target.get_nowait()
            try:
                target.task_done()
            except (AttributeError, ValueError):
                pass
        except queue.Empty:
            pass
        try:
            target.put_nowait(item)
        except queue.Full:
            # A concurrent worker completion can only make this diagnostic
            # frame stale; the next capture will replace it.
            pass

    def submit(self, frame: Any, configured_map_name: str) -> None:
        self._put_latest(self._requests, (frame, configured_map_name))

    def poll_latest(self) -> Optional[DebugSnapshot]:
        latest: Optional[DebugSnapshot] = None
        while True:
            try:
                latest = self._results.get_nowait()
            except queue.Empty:
                return latest
            try:
                self._results.task_done()
            except (AttributeError, ValueError):
                pass

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                frame, configured_map_name = self._requests.get(timeout=0.20)
            except queue.Empty:
                continue
            try:
                # Collapse requests that arrived while OpenCV processed the
                # preceding image.  Debug output must never form a backlog.
                while True:
                    try:
                        frame, configured_map_name = self._requests.get_nowait()
                    except queue.Empty:
                        break
                snapshot = build_debug_snapshot(
                    frame, self.detector, configured_map_name,
                    # These trackers own route/session state elsewhere.  The
                    # dashboard needs only an independent visual snapshot.
                    diamond_size_tracker=None,
                    structure_tracker=None,
                )
                self._put_latest(self._results, snapshot)
            except Exception:
                LOG.exception("could not build background debug snapshot")
            finally:
                try:
                    self._requests.task_done()
                except (AttributeError, ValueError):
                    pass


def _box_text(box: Box) -> str:
    left, top, right, bottom = box
    return f"x={left}, y={top}, w={right-left}, h={bottom-top}"


def patrol_button_states(running: bool, can_start: bool) -> tuple[str, str]:
    """Return Tk states for the separate Start and Stop patrol buttons.

    Start is enabled only when the patrol can start and is not running; Stop
    is enabled only while the patrol is running (greyed out when stopped).
    """

    return (
        "normal" if can_start and not running else "disabled",
        "normal" if running else "disabled",
    )


def layer_display_order(layer_names: list[str]) -> tuple[str, ...]:
    """Display the highest/newest layer above the lower layers.

    Ordering is by the layer's numeric position (layer1 = bottom, highest
    number = top), NEVER by the order the points happened to be recorded in
    - otherwise a top layer recorded before a lower one would appear below
    it ("layer1 on top of layer2").
    """

    def _layer_number(name: str) -> int:
        match = re.search(r"(\d+)$", name)
        return int(match.group(1)) if match else 0

    return tuple(reversed(sorted(layer_names, key=_layer_number)))


def keysym_to_scan_key(keysym: str) -> Optional[str]:
    """Map a Tk keysym to a bindable scan-code key name, or None.

    Only the game-usable hotkeys listed in ``BINDABLE_KEYS`` are bindable.
    Escape cancels key capture and restores the previous binding; every other
    key is ignored.
    """

    if not keysym:
        return None
    normalized = {
        "Control_L": "ctrl", "Control_R": "ctrl",
        "Alt_L": "alt", "Alt_R": "alt",
        "Shift_L": "shift", "Shift_R": "shift",
        "BackSpace": "backspace", "Caps_Lock": "caps",
        "Prior": "pageup", "Next": "pagedown",
        "Return": "enter",
        "KP_0": "kp_0", "KP_1": "kp_1", "KP_2": "kp_2",
        "KP_3": "kp_3", "KP_4": "kp_4", "KP_5": "kp_5",
        "KP_6": "kp_6", "KP_7": "kp_7", "KP_8": "kp_8",
        "KP_9": "kp_9",
        "KP_Add": "kp_add", "KP_Subtract": "kp_subtract",
        "KP_Multiply": "kp_multiply", "KP_Divide": "kp_divide",
        "KP_Enter": "kp_enter", "KP_Decimal": "kp_decimal",
    }.get(keysym, keysym)
    candidate = normalized.lower()
    if candidate in WindowKeySender._SCAN and candidate in BINDABLE_KEYS:
        return candidate
    return None


def rope_unavailable_hint() -> str:
    return "添加上层后即可录制绳索位置。"


def bindable_keys_hint() -> str:
    """Popout hint listing every currently bindable hotkey.

    Shown when hovering a key-bind button (fixed attack / HP / MP / 增益
    buff keys), mirroring the rope-record hint popout.  The list is derived
    from ``BINDABLE_KEYS`` so it always matches what the capture actually
    accepts.
    """

    ordered = [
        "1", "2", "3", "4", "5", "6", "7", "8", "9",
        "q", "w", "e", "r", "t", "y", "u", "i", "o", "p",
        "a", "s", "d", "f", "g", "h", "j", "k", "l",
        "x", "c", "v", "b", "n", "m", "slash",
        "space", "ctrl", "shift", "delete", "end",
        "home", "insert", "pageup", "pagedown",
    ]
    available = " / ".join(name for name in ordered if name in BINDABLE_KEYS)
    # ``-`` is shown globally as an available binding token.  Combo Attack
    # interprets it as its explicit no-attack slot; other panels keep their
    # existing key-specific validation.
    available += " / -"
    return (
        "可绑定按键：\n"
        f"{available}\n\n"
        "点击按钮后再按目标键即可绑定；\n"
        "按 Esc 或不可绑定的键会恢复原值。\n"
        "方向键 / Alt / Z 是移动、跳跃与拾取键，不可绑定。"
    )


def record_button_is_locked(saved_endpoint: Any, explicitly_unlocked: bool) -> bool:
    """Saved endpoints lock automatically unless the user explicitly unlocks."""

    return saved_endpoint is not None and not explicitly_unlocked


def recorded_coordinate_text(x: float, y: float) -> str:
    """Compact button-only display; stored coordinate precision is unchanged."""

    return f"({float(x):.4f}, {float(y):.4f})"


def machine_name_button_text(name: str) -> str:
    """Keep the compact device-name editor's action label stable."""

    # The actual device name can contain arbitrary operator text.  Keep this
    # narrow settings-row action label fixed instead of exposing it there.
    return "设备名称"


def normalize_quick_messages(value: Any, limit: int = 20) -> list[str]:
    """Keep only bounded, non-empty quick-message strings."""

    if not isinstance(value, list):
        return []
    messages = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = str(item).strip()
        if text:
            messages.append(text[:500])
        if len(messages) >= max(1, int(limit)):
            break
    return messages


def quick_message_preview(message: str, max_units: int = 42) -> str:
    """Fit a quick-message label without wrapping its compact row.

    CJK characters use roughly two Latin-character cells. The complete value
    remains available for its action and in the hover hint when shortened.
    """

    text = str(message)
    used = 0
    chars: list[str] = []
    for char in text:
        width = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + width > max_units:
            return "".join(chars).rstrip() + "..."
        chars.append(char)
        used += width
    return text


class NumericValueRangeControl:
    """A fixed-height, wheel-editable numeric value or min/max range.

    The surrounding setting label deliberately does not belong to this
    component.  That keeps labels on a stable baseline while either number
    grows on hover or selection.  ``unit`` is a display suffix only, so the
    caller can omit it for unitless values.
    """

    _NORMAL_STYLE = "NumericValue.TLabel"
    _HOVER_STYLE = "NumericValueHover.TLabel"
    _ACTIVE_STYLE = "NumericValueActive.TLabel"
    _DISABLED_STYLE = "NumericValueDisabled.TLabel"
    _FIXED_HEIGHT = 26

    def __init__(
        self,
        parent: Any,
        *,
        tk_module: Any,
        ttk_module: Any,
        minimum: float | int,
        maximum: float | int | None = None,
        range_mode: bool = True,
        value_type: type[float] | type[int] = float,
        step: float | int = 1,
        lower_bound: float | int | None = None,
        upper_bound: float | int | None = None,
        unit: str = "",
        value_width: int = 5,
        extra_width_px: int = 0,
        before_change: Optional[Callable[[], None]] = None,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        self.range_mode = bool(range_mode)
        self._tk = tk_module
        self._ttk = ttk_module
        self.value_type = value_type
        self.step = value_type(step)
        self.lower_bound = lower_bound
        self.upper_bound = upper_bound
        self.unit = str(unit)
        self.before_change = before_change
        self.on_change = on_change
        self.selected_side: Optional[str] = None
        self.hover_side: Optional[str] = None
        self.enabled = True
        self._leave_job: Any = None
        variable_type = (tk_module.IntVar if value_type is int
                         else tk_module.DoubleVar)
        self.minimum_var = variable_type(value=minimum)
        self.maximum_var = variable_type(
            value=(maximum if maximum is not None else minimum)
        )
        # Once propagation is disabled to hold a stable height, Tk no longer
        # derives a width from the child labels.  Reserve the exact cell width
        # here as well; otherwise the component collapses to a 1px frame.
        cell_count = 2 if self.range_mode else 1
        # ``ttk.Label(width=N)`` already reserves the glyph width.  The old
        # 9px estimate added a second, visibly empty cell after every range
        # value.  7px leaves room for the 12pt hover value without stretching
        # compact attack rows.
        fixed_width = (
            (cell_count * value_width * 7)
            + (8 if self.range_mode else 0)
            + max(0, int(extra_width_px))
        )
        self.frame = ttk_module.Frame(
            parent, width=fixed_width, height=self._FIXED_HEIGHT,
        )
        self.frame.pack_propagate(False)
        self.min_label = ttk_module.Label(
            self.frame, width=value_width, anchor="center",
            style=self._NORMAL_STYLE,
        )
        self.min_label.pack(side="left", fill="y")
        self.max_label: Optional[Any] = None
        if self.range_mode:
            ttk_module.Label(self.frame, text="~").pack(side="left", fill="y")
            self.max_label = ttk_module.Label(
                self.frame, width=value_width, anchor="center",
                style=self._NORMAL_STYLE,
            )
            self.max_label.pack(side="left", fill="y")
        self._bind_label(self.min_label, "min")
        if self.max_label is not None:
            self._bind_label(self.max_label, "max")
        self.normalize()

    def pack(self, **kwargs: Any) -> None:
        self.frame.pack(**kwargs)

    def get_bounds(self) -> tuple[float | int, float | int]:
        self.normalize()
        return self.minimum_var.get(), self.maximum_var.get()

    def sample(self) -> float | int:
        """Return one random value from the inclusive configured bounds."""

        minimum, maximum = self.get_bounds()
        if self.value_type is int:
            return random.randint(int(minimum), int(maximum))
        return random.uniform(float(minimum), float(maximum))

    def set_bounds(
        self, minimum: float | int, maximum: float | int | None = None,
    ) -> None:
        target_maximum = maximum if maximum is not None else minimum
        # Live countdown refreshes call this often.  Do not redraw an
        # unchanged field: on Windows a label redraw can synthesize Leave and
        # make an otherwise stable hover appear to blink.
        try:
            if (self.minimum_var.get() == minimum
                    and self.maximum_var.get() == target_maximum):
                return
        except Exception:
            pass
        self.minimum_var.set(minimum)
        self.maximum_var.set(target_maximum)
        self.normalize()

    def set_enabled(self, enabled: bool) -> None:
        """Enable/grey only the editable number labels in this component."""

        self.enabled = bool(enabled)
        state = ["!disabled"] if self.enabled else ["disabled"]
        for label in (self.min_label, self.max_label):
            if label is not None:
                label.state(state)
        if not self.enabled:
            self.selected_side = None
            self.hover_side = None
        self._refresh_styles()

    def normalize(self, changed: Optional[str] = None) -> tuple[float | int, float | int]:
        minimum = self._normalize_value(self.minimum_var.get())
        maximum = self._normalize_value(self.maximum_var.get())
        if not self.range_mode:
            maximum = minimum
        elif minimum > maximum:
            if changed == "max":
                minimum = maximum
            else:
                maximum = minimum
        self.minimum_var.set(minimum)
        self.maximum_var.set(maximum)
        self._refresh_text()
        return minimum, maximum

    def _normalize_value(self, value: Any) -> float | int:
        number = self.value_type(value)
        if self.lower_bound is not None:
            number = max(self.value_type(self.lower_bound), number)
        if self.upper_bound is not None:
            number = min(self.value_type(self.upper_bound), number)
        if self.value_type is int:
            return int(number)
        step = float(self.step)
        if step > 0.0:
            number = round(float(number) / step) * step
        return round(float(number), 6)

    def _display(self, value: float | int) -> str:
        if self.value_type is int:
            text = str(int(value))
        else:
            text = f"{float(value):.6f}".rstrip("0").rstrip(".")
        return f"{text}{self.unit}"

    def _refresh_text(self) -> None:
        self.min_label.configure(text=self._display(self.minimum_var.get()))
        if self.max_label is not None:
            self.max_label.configure(text=self._display(self.maximum_var.get()))
        self._refresh_styles()

    def _bind_label(self, label: Any, side: str) -> None:
        label.bind("<Enter>", lambda _event: self._hover(side, True))
        label.bind("<Leave>", lambda _event: self._hover(side, False))
        # Clicks are coarse direct adjustment.  The wheel remains the
        # fine-grained one-step input, so both interactions work on every
        # numeric value without an extra +/- button row.
        label.bind("<Button-1>", lambda _event: self._click_adjust(side, 1))
        label.bind("<Button-3>", lambda _event: self._click_adjust(side, -1))
        label.bind("<MouseWheel>", lambda event: self._wheel(side, event.delta))
        label.bind("<Button-4>", lambda _event: self._wheel(side, 120))
        label.bind("<Button-5>", lambda _event: self._wheel(side, -120))

    def _label_for(self, side: str) -> Any:
        return self.min_label if side == "min" else self.max_label

    def _hover(self, side: str, entered: bool) -> None:
        if entered:
            self._cancel_pending_leave()
            self.hover_side = side
            self._refresh_styles()
            return
        # Resizing a selected value from 10pt to 12pt can make Tk emit a
        # synthetic Leave although the pointer never left this component.
        # Wait for the layout to settle, then clear only if the pointer is
        # genuinely outside the fixed component frame.
        self._cancel_pending_leave()
        self._leave_job = self.frame.after(20, self._clear_after_true_leave)

    def _cancel_pending_leave(self) -> None:
        if self._leave_job is None:
            return
        try:
            self.frame.after_cancel(self._leave_job)
        except Exception:
            pass
        self._leave_job = None

    def _clear_after_true_leave(self) -> None:
        self._leave_job = None
        try:
            pointer_x = int(self.frame.winfo_pointerx())
            pointer_y = int(self.frame.winfo_pointery())
            inside = (
                self.frame.winfo_rootx() <= pointer_x < self.frame.winfo_rootx() + self.frame.winfo_width()
                and self.frame.winfo_rooty() <= pointer_y < self.frame.winfo_rooty() + self.frame.winfo_height()
            )
        except Exception:
            inside = False
        if inside:
            return
        self.selected_side = None
        self.hover_side = None
        self._refresh_styles()

    def _select(self, side: str) -> str:
        self.selected_side = side
        self.hover_side = side
        self._refresh_styles()
        return "break"

    def _click_adjust(self, side: str, direction: int) -> str:
        """Apply one coarse click adjustment: exactly fifty fine steps."""

        label = self._label_for(side)
        try:
            if label is not None and label.instate(["disabled"]):
                return "break"
        except Exception:
            pass
        self._select(side)
        if self.before_change is not None:
            self.before_change()
        variable = self.minimum_var if side == "min" else self.maximum_var
        coarse_delta = self.value_type(self.step * 50)
        variable.set(self.value_type(variable.get() + int(direction) * coarse_delta))
        self.normalize(changed=side)
        if self.on_change is not None:
            self.on_change()
        return "break"

    def _wheel(self, side: str, delta: int) -> str:
        label = self._label_for(side)
        try:
            if label is not None and label.instate(["disabled"]):
                return "break"
        except Exception:
            pass
        if self.selected_side != side:
            self._select(side)
        if self.before_change is not None:
            self.before_change()
        direction = 1 if float(delta) > 0.0 else -1
        variable = self.minimum_var if side == "min" else self.maximum_var
        variable.set(self.value_type(variable.get() + direction * self.step))
        self.normalize(changed=side)
        if self.on_change is not None:
            self.on_change()
        return "break"

    def _refresh_styles(self) -> None:
        for side in ("min", "max"):
            label = self._label_for(side)
            if label is not None:
                label.configure(
                    style=(
                        self._DISABLED_STYLE if not self.enabled
                        else self._ACTIVE_STYLE if self.selected_side == side
                        else self._HOVER_STYLE if self.hover_side == side
                        else self._NORMAL_STYLE
                    )
                )


class UiWorker(threading.Thread):
    """Own the independent UI loop; Tk requires ``run`` on Python's main thread."""

    # Set True to show the detected-minimap and map-name preview images in
    # the UI again (they are hidden by default; the code is kept for future
    # debugging of minimap detection).
    _SHOW_MINIMAP_PREVIEW = False

    # Set True to show the "Detection" info panel (frame stats) again; it is
    # hidden by default, the rendering code is kept for future use.
    _SHOW_DETECTION_INFO = False
    # TEMPORARY: keep every YOLO widget and handler intact but do not pack the
    # panel/radio into the UI. README.md documents the one-flag restoration.
    _SHOW_YOLO_PANEL = False
    # TEMPORARY: the current monster model is not trained reliably enough.
    # Keep the implementation below intact so it can be restored quickly;
    # README.md documents the matching installer change.
    _YOLO_MONSTER_DETECTION_ENABLED = False
    # TEMPORARY: keep scheduled-shutdown code/settings available, but do not
    # expose its controls or start its worker.
    _SHOW_SHUTDOWN_PANEL = False
    _FIXED_RANDOM_GAP_STEP = 0.05
    _FIXED_RANDOM_GAP_MAX = 30.0
    _FIXED_ATTACK_INTERVAL_MAX = 30.0
    _FIXED_ATTACK_INTERVAL_STEP = 0.05
    # Preserve the full 0.20..30.00s configuration range while dedicating
    # most of the visible slider travel to the common 0.20..2.00s interval.
    # A linear 30-second scale cannot expose its 0.05s increments on a
    # compact UI track (596 values would need 596 usable pixels).
    _FIXED_ATTACK_INTERVAL_FINE_MAX = 2.0
    _FIXED_ATTACK_INTERVAL_FINE_TRACK_SHARE = 0.80
    _OPTIONAL_MOTION_INTERVAL_MAX = 60.0
    # 捡东西 (the stand-still pickup circuit) is deliberately configured in
    # MINUTES: the operator reads and sets that row in "m", except values
    # below one minute are shown as seconds.  Its trigger range is 10s..30m.
    # The configuration and movement worker keep seconds-scale keys.
    _STATIONARY_PICKUP_INTERVAL_MIN_MINUTES = 10.0 / 60.0
    _STATIONARY_PICKUP_INTERVAL_MAX_MINUTES = 30.0
    _STATIONARY_PICKUP_INTERVAL_DEFAULT_MINUTES = 15.0
    _DUAL_STATIONARY_SWITCH_MIN_MINUTES = 1.0
    _DUAL_STATIONARY_SWITCH_MAX_MINUTES = 30.0
    _DUAL_STATIONARY_SWITCH_DEFAULT_MINUTES = 15.0
    # One visual length for all of the time sliders in the attack panel.  The
    # former 112px logical track was too short for the 0.05s attack cadence
    # steps and also left an unnecessary empty gap after the key binding.
    _ATTACK_TIMING_SLIDER_LENGTH = 150
    # Potion and buff sliders form their own aligned right-panel column.
    # 50px shorter than the previous compact right-panel track.  All five
    # rows keep this same length and aligned endpoint.
    _DRUG_SLIDER_LENGTH = 76
    _DRUG_SLIDER_VALUE_WIDTH = 8
    # A DOUBLE click on a 随机 −/+ button moves the gap by this much in one go
    # (five seconds instead of 0.05), while a single click keeps the precise fine
    # step and holding keeps repeating it.
    _RANDOM_GAP_COARSE_STEP = 5.0
    # Two presses closer together than this belong to one click sequence, so a
    # double click measures its 5 s from the value the sequence started at and
    # the fine step of the first press cannot leave 5.1 s behind.
    _RANDOM_GAP_CLICK_SEQUENCE_SECONDS = 0.35

    def __init__(
        self,
        frame_queue: "queue.Queue[Any]",
        stop_event: threading.Event,
        detector: MinimapDetector,
        *,
        configured_map_name: str = "",
        refresh_ms: int = 100,
        patrol_controller: Optional[PatrolController] = None,
        diamond_size_tracker: Optional[DiamondSizeTracker] = None,
        structure_tracker: Optional[MapStructureTracker] = None,
        map_identity_store: Optional[MapIdentityStore] = None,
        status_worker: Any = None,
        attack_worker: Any = None,
        random_rest_worker: Any = None,
        random_jump_worker: Any = None,
        small_step_worker: Any = None,
        hotkey_queue: Optional["queue.Queue[str]"] = None,
        hotkey_worker: Any = None,
        on_hotkey_stop_prearmed: Optional[Callable[[], bool]] = None,
        hotkey_stop_cleanup_done_event: Optional[threading.Event] = None,
        reconnect_worker: Any = None,
        auto_restart_worker: Any = None,
        reconnect_results: Optional["queue.Queue[tuple[str, str]]"] = None,
        api_test_video_factory: Optional[Callable[..., Any]] = None,
        api_test_results: Optional["queue.Queue[tuple[str, str]]"] = None,
        # 自动过测谎: builds one api pass for one lie window (the assistant owns the worker classes).
        api_auto_lie_factory: Optional[Callable[..., Any]] = None,
        trade_worker: Any = None,
        movement_worker: Any = None,
        character_worker: Any = None,
        shutdown_worker: Any = None,
        countdown_worker: Any = None,
        lie_detector_worker: Any = None,
        screen_blinker: Any = None,
        telegram_notifier: Any = None,
        dingtalk_notifier: Any = None,
        on_patrol_start: Optional[Callable[[], None]] = None,
        on_patrol_restart: Optional[Callable[[], None]] = None,
        on_patrol_stop: Optional[Callable[[], None]] = None,
        on_capture_now: Optional[Callable[[], Any]] = None,
        on_recording_verified: Optional[Callable[[DebugSnapshot], None]] = None,
        on_imported_minimap_calibration: Optional[Callable[[], None]] = None,
        log_queue: Optional["queue.Queue[str]"] = None,
        ui_log_handler: Any = None,
        user_config_path: Optional[str] = None,
        automation_active_event: Optional[threading.Event] = None,
        # 测谎 armed: the assistant's parked lie watch may grab the game window so a lie window is
        # caught (and 自动过测谎 can take over) even without Start Patrol.
        lie_watch_armed_event: Optional[threading.Event] = None,
        # 掉线 armed: the same parked watch feeds the character worker, so a disconnect is noticed (and
        # 自动重连 can run) even without Start Patrol.
        disconnect_watch_armed_event: Optional[threading.Event] = None,
        channel_update_events: Optional["queue.Queue[int]"] = None,
        # A stopped 其他玩家自动换线 workflow asks for its own field to be cleared.
        other_player_stop_events: Optional["queue.Queue[str]"] = None,
    ) -> None:
        super().__init__(name="ui-worker", daemon=True)
        self.frame_queue = frame_queue
        self.stop_event = stop_event
        self.detector = detector
        self.configured_map_name = configured_map_name
        self.refresh_ms = max(30, int(refresh_ms))
        self.patrol_controller = patrol_controller
        self.diamond_size_tracker = diamond_size_tracker
        self.structure_tracker = structure_tracker
        self.map_identity_store = map_identity_store
        # Status worker whose detector config the Drug panel edits live
        # (potions keys + trigger percents).
        self.status_worker = status_worker
        # Fixed-rate attack worker (AttackWorker) the Fixed Attack panel
        # toggles: enabled flag, interval and attack key are applied live.
        self.attack_worker = attack_worker
        # 随机休息 schedule: the panel only decides whether it is enabled (and in
        # which attack modes); the worker owns the 75-120 min / 3-15 s timing and
        # the movement+attack pause itself.
        self.random_rest_worker = random_rest_worker
        # Independent Alt timer. It shares only the attack worker's runtime
        # gating events and exposes no configurable key binding.
        self.random_jump_worker = random_jump_worker
        # Timed directional micro-step; actual keys are owned by movement.
        self.small_step_worker = small_step_worker
        self.hotkey_queue = hotkey_queue
        # Physical hotkey hook whose bindings are temporarily disabled while
        # patrol runs (only the patrol-toggle chord stays live).
        self.hotkey_worker = hotkey_worker
        # Ctrl+` can be received while Tk is rendering a diagnostic frame.
        # The hotkey worker has already disabled automation in that case; Tk
        # later consumes this marker only to refresh its controls/status.
        self.on_hotkey_stop_prearmed = on_hotkey_stop_prearmed
        self._hotkey_stop_cleanup_done_event = hotkey_stop_cleanup_done_event
        # 自动重连: armed from the Additional Functions panel, triggered by the 掉线
        # event and confirmed by the login-page template check.
        self.reconnect_worker = reconnect_worker
        self.auto_restart_worker = auto_restart_worker
        self.reconnect_results = reconnect_results
        # 测试api: one factory that builds a drill thread per press (a thread cannot restart), plus
        # the queue its progress lines arrive on.  The only drill is the video one: the button picks
        # a video file and plays it in its own focused window, so the mouse stays inside the picture
        # and never touches the game.
        self.api_test_video_factory = api_test_video_factory
        self.api_test_results = api_test_results
        self.api_auto_lie_factory = api_auto_lie_factory
        self.api_test_worker: Any = None
        self.api_test_window: Any = None            # the video window (Tk Toplevel)
        self.api_test_video: str = ""              # the file the current drill plays
        self._api_test_video_dir = ""              # last folder, remembered for the picker
        self._api_test_display: "queue.Queue[Any]" = queue.Queue(maxsize=2)
        self._api_test_keep_focus_at = 0.0
        # The upstream credential is not a panel/config value.  A validated
        # heartbeat places it in the process-only auto_lie_secret store.
        # v0423: 自动过测谎 - the api pass runs by itself when the game's lie window appears.  The lie
        # detector calls `on_lie_event_for_api()` from its own thread, which only raises a flag; the Tk
        # thread services it in `_poll` (nothing Tk is touched off-thread).
        self._api_auto_lie_events: "queue.Queue[object]" = queue.Queue(maxsize=2)
        self._api_auto_lie_event_enqueue_lock = threading.Lock()
        self._api_auto_lie_match_queued = False
        self._api_auto_lie_pending = False
        self._api_auto_lie_session_armed = False
        # The server-issued lie-pass quota: when 剩余 reaches 0 the checkbox is
        # unselected and greyed until a heartbeat reports a positive balance
        # again (an unmetered balance never locks it).
        self._api_auto_lie_quota_locked = False
        self._api_auto_lie_quota_logged = False
        self._api_auto_lie_pending_since = 0.0
        self._api_auto_lie_wait_logged = False
        # A lie window that arrived while the 本地测谎 service was down: logged once
        # per window, and the start request is rate-limited.
        self._api_auto_lie_service_wait_logged = False
        self._local_lie_start_requested_at = 0.0
        # Debounce state: whether the window currently on screen has already been handled, when
        # the square was last seen to be gone, and when the last pass started.
        self._api_auto_lie_event_active = False
        self._api_auto_lie_clear_since = 0.0
        self._api_auto_lie_last_pass_started = 0.0
        # A transport-close timeout is accounted as success by policy, but the
        # old detector square can remain visible for a few capture ticks.  Do
        # not spend another pass on that same stale square; a clear frame arms
        # the next real lie window normally.
        self._api_auto_lie_ignore_until_clear = False
        self._api_auto_lie_change_origin = "unknown"
        # 本地测谎 (127.0.0.1:8765): result of the last health probe and the note
        # the panel shows.  None = not probed yet in this session.
        self._local_lie_ready: Optional[bool] = None
        # 试跑 requested while the local service was cold-starting: the drill is
        # kept pending and started as soon as /v1/health answers, instead of
        # being cancelled with "本地测谎服务还没准备好".
        self._api_test_pending: Optional[tuple[Any, float]] = None
        self._api_test_wait_started_at: float = 0.0
        self._api_test_wait_job: Any = None
        self._local_lie_note: str = ""
        # A key refresh from the activation server restarts the local service,
        # but never underneath a live lie pass.
        try:
            import local_lie_service

            local_lie_service.set_busy_probe(
                lambda: self._worker_is_running(self._api_lie_pass_worker)
            )
        except Exception:  # noqa: BLE001 - the probe is best effort
            LOG.debug("local lie service busy probe could not be installed",
                      exc_info=True)
        # (ok, note) of a finished 激活测谎 run, consumed by the Tk poll.
        self._local_lie_activate_result: Optional[tuple[bool, str]] = None
        # Set by the probe thread, consumed by the Tk poll (widgets are only
        # touched on the Tk thread).
        self._local_lie_report_pending = False
        self._latest_capture_frame: Any = None
        self._auto_lie_outcome_state: Optional[dict[str, Any]] = None
        self._auto_lie_success_box = LieSuccessBoxDetector()
        width, height = self._auto_lie_success_box.size
        AUTO_LIE_LOG.info(
            "自动过测谎: success blue-box detector ready %dx%d coverage=%.2f x%d",
            width, height, SUCCESS_THRESHOLD, SUCCESS_CONSECUTIVE_FRAMES,
        )
        # True while an automatic pass has the patrol stood down (see _pause_patrol_for_api_pass).
        self._api_auto_lie_patrol_paused = False
        self._api_auto_lie_post_confirm_scheduled = False
        self._api_auto_lie_post_confirm_complete = False
        # The operator's patrol INTENT: set by 开始巡逻, cleared by 停止巡逻 (buttons and the Ctrl+`
        # toggle).  A reconnect restarts the patrol only when this is set - the patrol STATE is useless
        # for that decision, because a disconnect (and the focus gate) stop the patrol, and a Start
        # Patrol attempted while the character is still on a login page is refused.
        self._patrol_intent = False
        self._api_lie_pass_worker: Any = None
        self._api_auto_lie_runs = 0
        # Isolated Ctrl+Q/Ctrl+W trade workflow.  It uses the existing game
        # capture worker only while checking for a trader.
        self.trade_worker = trade_worker
        # Movement worker whose jump-rope logic follows the attack mode:
        # Fixed Attack mode runs without YOLO, so the minimap logic must own
        # the rope jump there.
        self.movement_worker = movement_worker
        # Existing per-frame yellow-marker detector; the disconnect alarm
        # consumes its already-computed detection result.
        self.character_worker = character_worker
        # Shutdown worker (ShutdownWorker) the Additional Functions panel
        # arms: enabled flag + hours are applied live.
        self.shutdown_worker = shutdown_worker
        # Independent repeating sound reminder. It owns no game state/input;
        # this reference only exposes its interval/deadline to the UI.
        self.countdown_worker = countdown_worker
        # Five-second white-square detector fed by the existing full-client
        # capture bus. It never creates screenshot files.  The local lie pass it
        # used to feed is gone (lie detection is remote now), so it drives only
        # the 测谎 alarm and the screenshot recorder.
        self.lie_detector_worker = lie_detector_worker
        # Shared visual counterpart to every optional beep alert.
        self.screen_blinker = screen_blinker
        # Optional Telegram delivery runs in its own worker; UI calls only
        # non-blocking configuration/queue methods.
        self.telegram_notifier = telegram_notifier
        # 钉钉提醒: same contract as the Telegram notifier - the UI only writes
        # the webhook / switch and reads a status line, the worker owns the
        # network.  Armed by the same 消息 selection as the other channels.
        self.dingtalk_notifier = dingtalk_notifier
        self._telegram_bot_token = ""
        self._telegram_chat_id = ""
        self._quick_messages: list[str] = []
        self._quick_edit_entry: Any = None
        self._quick_edit_index: Optional[int] = None
        self._quick_edit_blur_job: Any = None
        self.on_patrol_start = on_patrol_start
        self.on_patrol_restart = on_patrol_restart
        self.on_patrol_stop = on_patrol_stop
        self.on_capture_now = on_capture_now
        self.on_recording_verified = on_recording_verified
        self.on_imported_minimap_calibration = on_imported_minimap_calibration
        self.log_queue = log_queue
        self.ui_log_handler = ui_log_handler
        self.user_config_path = user_config_path
        # Ctrl+Z undo is deliberately local-only: it keeps up to ten complete
        # user configuration documents in RAM and never writes a history file.
        # Settings loaders run while the dashboard is being built, so capture
        # begins only after the first completed UI layout.
        self._undo_history: collections.deque[tuple[str, bytes]] = collections.deque(maxlen=10)
        self._undo_recording_enabled = False
        self._undo_restoring = False
        self._undo_last_key = ""
        self._undo_last_at = 0.0
        self._undo_skip_next_change_key = ""
        self.automation_active_event = automation_active_event
        self.lie_watch_armed_event = lie_watch_armed_event
        self.disconnect_watch_armed_event = disconnect_watch_armed_event
        self.channel_update_events = channel_update_events
        self.other_player_stop_events = other_player_stop_events
        # License verification is an independent boundary: it never changes
        # patrol/trade worker internals, it only decides whether UI actions may
        # enter those workflows.
        local_license_status = verify_license()
        saved_equipment_id = self._load_saved_equipment_id()
        if saved_equipment_id:
            local_license_status = replace(
                local_license_status, equipment_id=saved_equipment_id
            )
        # A sealed, server-accepted fingerprint belongs only to this Windows
        # profile and this license.  It lets a normal already-authorized user
        # start while CIM/WMI wakes up after boot; the heartbeat below still
        # retries live CIM and ultimately locks the UI if it never recovers.
        sealed_boot_identity = bool(
            local_license_status.valid
            and local_license_status.license_id
            and has_sealed_fingerprint(local_license_status.license_id)
        )
        self._license_status: LicenseStatus = (
            replace(local_license_status, code="cim_pending", message="设备正在确认…")
            if sealed_boot_identity else (
                LicenseStatus(
                    False, "checking", "正在验证在线授权…",
                    local_license_status.license_id, local_license_status.edition,
                    local_license_status.expires_at, local_license_status.equipment_id,
                )
                if local_license_status.valid else local_license_status
            )
        )
        # Keep the UI honest while the immediate pinned validation is in
        # flight.  A locally signed document is useful context (expiry), but
        # it is not an active online entitlement until the
        # server has accepted it for this session.
        self._license_online_validation_pending = bool(local_license_status.valid)
        self._license_session_locked = not sealed_boot_identity
        self._memory_usage_text = "内存：读取中"
        self._license_heartbeat_results: "queue.Queue[tuple[int, LicenseStatus]]" = (
            queue.Queue()
        )
        self._device_memo_results: "queue.Queue[tuple[str, LicenseStatus]]" = queue.Queue()
        # Self-unbind is a round-trip operation.  The worker owns the pinned
        # request; Tk only owns the resulting prompt and immediate local lock.
        self._device_unbind_results: "queue.Queue[LicenseStatus]" = queue.Queue(maxsize=1)
        self._device_unbind_in_flight = False
        self._self_unbound_this_session = False
        self._license_heartbeat_started = False
        self._license_heartbeat_generation = 0
        self._license_heartbeat_consecutive_failures = 0
        # Manual refresh (the caption ↻ button).  The heartbeat thread sleeps on
        # this event between its three-hour rounds, so setting it validates
        # immediately instead of waiting for the timer; the flag asks that
        # thread for the short retry burst.
        self._license_heartbeat_request = threading.Event()
        self._license_refresh_aggressive = False
        self._license_refresh_cooldown_job: Any = None
        # --- 更新图标 state ---------------------------------------------------
        # ``_release_notice`` is the release the green icon stands for (version,
        # notes, link); ``_release_states`` delivers worker results to Tk, so the
        # heartbeat thread and the download thread never touch widgets directly.
        self._release_notice: Optional[dict[str, Any]] = None
        self._release_states: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=8)
        self._release_check_in_flight = False
        self._release_check_version = ""
        self._release_download_in_flight = False
        self._release_progress_text = ""
        # Completed auto-lie passes are accounted for later, never on the
        # time-sensitive WebSocket/cursor path.  The worker persists its own
        # queue so an app restart cannot silently discard a completed event.
        self._lie_accounting_results: "queue.Queue[LicenseStatus]" = queue.Queue(maxsize=32)
        self._lie_accounting_worker = LieAccountingWorker(
            self.stop_event,
            self._lie_accounting_results,
            application_root() / "lie_accounting_pending.json",
        )
        self._lie_accounting_started = False
        self._accounted_auto_lie_worker_id: Optional[int] = None
        self._auto_lie_marker_verification: Optional[dict[str, Any]] = None
        self._yolo_process: Any = None
        self.last_snapshot: Optional[DebugSnapshot] = None
        self._debug_snapshot_worker = DebugSnapshotWorker(stop_event, detector)
        self._debug_snapshot_worker_started = False
        # A debug snapshot is not part of patrol logic.  In the normal UI both
        # debug views are hidden, so continuously running a second minimap
        # detector at capture rate only steals CPU from Tk and the real
        # workers.  It is enabled briefly for the post-auto-lie marker check
        # and on demand by recording.
        self._next_poll_due_at: Optional[float] = None
        # Each Tk poll records phase begin/end times in RAM.  Normal fast polls
        # stay silent; a slow poll flushes its complete ordered trace through
        # the asynchronous logger so diagnostics cannot create the lag they
        # are trying to measure.
        self._ui_poll_trace_id = 0
        self._ui_poll_trace_started_at = 0.0
        self._ui_poll_trace_steps: list[list[Any]] = []
        self._ui_text_cache: dict[str, str] = {}
        self._countdown_display_state: Optional[tuple[float, str]] = None
        self._patrol_controls_render_key: Any = None
        # A persistent first-panel explanation for a disabled patrol Start
        # button.  Keep it separate from transient start/recording errors so
        # switching back to a valid configuration can restore the normal text.
        self._patrol_start_condition_hint_visible = False
        self._root: Any = None
        self._photo_minimap: Any = None
        self._photo_map_name: Any = None
        self._record_buttons: dict[tuple[str, str], Any] = {}
        self._rope_tooltips: dict[str, HoverTooltip] = {}
        # Key-bind buttons carry the bindable-hotkeys popout hint; the list
        # keeps the tooltips alive for the whole UI lifetime.
        self._bind_key_tooltips: list[HoverTooltip] = []
        self._quick_message_tooltips: list[HoverTooltip] = []
        self._layer_labels: dict[str, Any] = {}
        self._layer_patrol_time_controls: dict[str, NumericValueRangeControl] = {}
        self._layer_row_names: tuple[str, ...] = ()
        # Only explicit unlocks need UI state. Locking itself is derived from
        # the controller's saved endpoint, so dynamically created rows behave
        # identically to rows present at startup.
        self._unlocked_points: set[tuple[str, str]] = set()
        self._record_press_job: Any = None
        self._record_hold_fired = False
        self._attack_hotkey_sound_job: Any = None
        # A hotkey callback can be delivered twice by Windows during a focus
        # transition.  Never stack identical success/failure MP3 feedback.
        self._last_action_sound_at: dict[bool, float] = {}
        # Last patrol running state the UI buttons were rendered for.  Patrol
        # can be stopped OUTSIDE the UI (disconnect alert / sustained focus
        # loss call ``patrol_controller.set_enabled(False)`` directly in
        # assistant.py); the poll sync below refreshes the Start/Stop buttons
        # when that happens instead of leaving them stale.
        self._patrol_ui_running: Optional[bool] = None
        # Debounce for external patrol-state changes (a self-rescue can
        # disable and re-enable the controller within seconds; only a state
        # that holds for two consecutive polls is treated as a real change).
        self._patrol_pending_running: Optional[bool] = None
        # Stop first changes the controller/UI state, then releases the game
        # keys.  Key release may wait on an active worker transaction, so it
        # must never freeze Tk or leave the Stop button visually stale.
        self._patrol_stop_pending = False
        self._patrol_stop_cleanup_done = threading.Event()
        # Monotonic timestamps are diagnostic-only.  They let the running log
        # distinguish a delayed Tk callback from a slow background key scrub.
        self._patrol_stop_started_at: Optional[float] = None
        self._patrol_stop_cleanup_finished_at: Optional[float] = None
        # Ctrl+` can be reported twice while Windows changes focus.  A second
        # request queued during a start must not immediately undo that start
        # after calibration completes.
        self._hotkey_toggle_ignore_until = 0.0

    def _service_prearmed_hotkey_stop(self) -> None:
        """Catch up Tk after the hotkey worker performed an immediate stop."""

        consume_prearmed = self.on_hotkey_stop_prearmed
        if not callable(consume_prearmed):
            return
        try:
            # The handoff crosses from the hotkey hook thread to Tk.  Keep
            # this boundary separate from the visual catch-up: a held hotkey
            # lock and a slow widget redraw have very different fixes.
            with self._trace_ui_poll_step("prearmed-hotkey-stop:consume-flag"):
                prearmed = consume_prearmed()
            if not prearmed:
                return
        except Exception:
            LOG.exception("could not consume pre-armed hotkey stop")
            return
        with self._trace_ui_poll_step("prearmed-hotkey-stop:ui-catch-up"):
            self._finish_prearmed_hotkey_stop()
        # The matching queued Ctrl+` action may arrive in this same poll, or
        # may have been rejected by its ordinary hotkey cooldown.  In either
        # case it must never turn the just-stopped patrol back on.
        with self._trace_ui_poll_step("prearmed-hotkey-stop:ignore-window"):
            self._hotkey_toggle_ignore_until = time.monotonic() + 0.75
        with self._trace_ui_poll_step("prearmed-hotkey-stop:feedback-sound"):
            self._play_action_sound(False)

    def run(self) -> None:
        try:
            import tkinter as tk
            from tkinter import ttk
            self._tk = tk
            self._ttk = ttk

            root = tk.Tk()
            # Never map Tk's default tiny window.  The finished geometry is
            # applied only after every panel has been built and laid out.
            root.withdraw()
            self._root = root
            self._ui_hang_watchdog = UiHangWatchdog(
                application_root() / "work" / "ui_hang_trace.log",
                timeout_seconds=8.0,
            )
            self._ui_hang_watchdog.start()

            # Tk intercepts exceptions raised by individual button/timer
            # callbacks instead of letting them escape ``mainloop``.  Preserve
            # their traceback before Tk prints it, otherwise a crashed
            # dashboard leaves no usable evidence once its copy button is gone.
            original_callback_reporter = root.report_callback_exception

            def report_callback_exception(
                exc_type: type[BaseException],
                exc_value: BaseException,
                exc_traceback: object,
            ) -> None:
                record_exception(
                    "Tk callback exception", exc_type, exc_value, exc_traceback,
                )
                original_callback_reporter(exc_type, exc_value, exc_traceback)

            root.report_callback_exception = report_callback_exception
            if not self._debug_snapshot_worker_started:
                self._debug_snapshot_worker.start()
                self._debug_snapshot_worker_started = True
            if not self._lie_accounting_started:
                self._lie_accounting_worker.start()
                self._lie_accounting_started = True
            app_version = version_label()
            root.title(f"MoonEclipse {app_version}")
            screen_width = root.winfo_screenwidth()
            screen_height = root.winfo_screenheight()
            # 调试窗口不抢前台：不设置 -topmost，游戏在爬绳/挂绳时保持焦点，
            # 不会因调试窗口抢到前台而松开按键、角色跳离绳索。窗口位置与大小
            # 按上次保存的几何恢复（可移动、可调整），默认不再固定左上角。
            # The size constants are LOGICAL pixels: on scaled displays
            # (125%/150%) Windows bitmap-scales the DPI-unaware window, so a
            # 1160x560 logical window physically scales with every other app
            # - the width scales with every other app and the height stays the
            # natural content height (no clipping, no extra space).
            restored = _load_window_geometry(
                f"{_INITIAL_WINDOW_WIDTH}x{_INITIAL_WINDOW_HEIGHT}+40+40"
            )
            clamped = _clamp_window_geometry(
                restored, screen_width, screen_height,
            )

            # Replace the native caption with an in-window one so the help
            # "?" can sit at the same level as the title and the window
            # buttons (Tk cannot add widgets to the OS caption bar).  Native
            # resize borders and the taskbar entry are preserved.  The
            # in-window caption is part of the Tk client, so geometry is
            # whole-window (the default width includes the caption row).
            caption_installed = self._install_custom_caption(root, tk)
            root.geometry(clamped)
            root.minsize(
                _INITIAL_WINDOW_WIDTH,
                500 + (_CAPTION_HEIGHT if caption_installed else 0),
            )
            root.protocol("WM_DELETE_WINDOW", self._on_debug_window_close)
            # While the OS runs its own move/resize loop Windows draws the
            # frame outline; freeze client repaints until the shape is
            # confirmed, then repaint once (avoids relayout/repaint lag on
            # every resize step).  Also frees WM_SIZE handling for the
            # drag-move path below.
            self._install_resize_burst_guard(root)

            if caption_installed:
                self._build_caption_bar(root, tk, app_version)
                # Restore a pending update notice from disk: a user who skipped
                # the update must still see the green icon on the next start.
                self._load_release_notice()
            else:
                # Fallback when the custom caption cannot be installed: keep
                # the native caption and float a small ? at the top-right of
                # the content so the help dialog stays reachable.
                self._teaching_photo = _make_teaching_icon(root)
                self._teaching_button = tk.Button(
                    root,
                    image=self._teaching_photo,
                    width=3,
                    height=1,
                    relief="raised",
                    bd=1,
                    cursor="hand2",
                    command=self._show_teaching_viewer,
                )
                self._teaching_button.place(
                    relx=1.0, x=-37, y=6, anchor="ne"
                )
                teaching_tooltip = HoverTooltip(self._teaching_button, "教学")
                teaching_tooltip.set_enabled(True)
                self._bind_key_tooltips.append(teaching_tooltip)
                self._help_button = tk.Button(
                    root,
                    text="?",
                    font=("Segoe UI", 9, "bold"),
                    width=2,
                    height=1,
                    relief="raised",
                    bd=1,
                    cursor="hand2",
                )
                self._help_button.place(relx=1.0, x=-10, y=6, anchor="ne")
                # Hover-triggered help (same behavior as the caption ?).
                self._help_button.bind("<Enter>", self._help_hover_show)
                self._help_button.bind("<Leave>", self._help_hover_leave)

            # Keep the content flush beneath the custom caption.  The old
            # uniform padding, plus the columns/controls top margins, made a
            # visible white strip above only the left side of the UI.
            container = ttk.Frame(root, padding=(12, 0, 12, 12))
            container.pack(fill="both", expand=True)
            # Content must never dictate the window size: the initial size is
            # fixed by the geometry applied after the UI is built, and the
            # inner panels must not spread the window open on first display.
            container.pack_propagate(False)

            license_bar = ttk.Frame(container)
            license_bar.pack(fill="x", pady=(4, 0))
            self._license_label = ttk.Label(license_bar, anchor="w")
            # Keep the authorization state and its usage counters contiguous.
            # An expanding label here pushed the counters into a detached
            # right-side island with a large blank gap between them.
            self._license_label.pack(side="left")
            self._license_device_label = ttk.Label(license_bar, anchor="w")
            self._license_device_label.pack(side="left")
            self._license_device_label.bind("<Button-1>", self._copy_equipment_id)
            self._license_activation_label = ttk.Label(license_bar, anchor="w")
            self._license_activation_label.pack(side="left")
            self._license_activation_label.bind("<Button-1>", self._copy_activation_code)
            self._license_memo_label = ttk.Label(license_bar, anchor="w")
            self._license_memo_label.pack(side="left")
            self._license_memo_label.bind("<Button-1>", self._edit_device_memo)
            self._license_unbind_label = ttk.Label(license_bar, anchor="w")
            self._license_unbind_label.pack(side="left")
            self._license_unbind_label.bind("<Button-1>", self._request_device_unbind)
            # Usage has its own row. It retains the day-by-day breakdown while
            # making the current finite/unsettled entitlement easy to read.
            self._license_lie_stats = ttk.Frame(container)
            self._license_lie_stats.pack(fill="x", pady=(0, 3))
            self._license_lie_prefix = ttk.Label(self._license_lie_stats, text="")
            self._license_lie_remaining = ttk.Label(
                self._license_lie_stats, text="", foreground="#777777"
            )
            self._license_lie_suffix = ttk.Label(self._license_lie_stats, text="")
            self._license_lie_today_prefix = ttk.Label(self._license_lie_stats, text="")
            self._license_lie_total = ttk.Label(
                self._license_lie_stats, text="", foreground="#777777"
            )
            self._license_lie_success = ttk.Label(
                self._license_lie_stats, text="", foreground="#17803d"
            )
            self._license_lie_failed = ttk.Label(
                self._license_lie_stats, text="", foreground="#b42318"
            )
            self._license_lie_yesterday_prefix = ttk.Label(self._license_lie_stats, text="")
            self._license_lie_yesterday_total = ttk.Label(
                self._license_lie_stats, text="", foreground="#777777"
            )
            self._license_lie_yesterday_success = ttk.Label(
                self._license_lie_stats, text="", foreground="#17803d"
            )
            self._license_lie_yesterday_failed = ttk.Label(
                self._license_lie_stats, text="", foreground="#b42318"
            )
            # Memory belongs with runtime measurements rather than the stable
            # authorization/device identity row above.
            self._license_memory_label = ttk.Label(self._license_lie_stats, anchor="w")
            for stat_label in (
                self._license_lie_prefix,
                self._license_lie_remaining,
                self._license_lie_suffix,
                self._license_lie_today_prefix,
                self._license_lie_total,
                self._license_lie_success,
                self._license_lie_failed,
                self._license_lie_yesterday_prefix,
                self._license_lie_yesterday_total,
                self._license_lie_yesterday_success,
                self._license_lie_yesterday_failed,
                self._license_memory_label,
            ):
                stat_label.pack(side="left")
            self._license_button = ttk.Button(
                license_bar, text="激活授权", command=self._activate_license
            )
            self._license_button.pack(side="right")
            self._refresh_license_ui()

            columns = ttk.Frame(container)
            # A shared small top inset keeps both panel stacks visually clear
            # of the title separator, without the old unequal left-only gap.
            columns.pack(fill="both", expand=True, pady=(8, 0))
            columns.pack_propagate(False)
            columns.grid_propagate(False)
            self._columns_frame = columns
            # Both stacks have the same compact fixed width. The child rows
            # deliberately use short controls and ellipsized quick messages
            # rather than making the left column wider than the right.
            columns.columnconfigure(0, weight=0, minsize=_LEFT_COLUMN_WIDTH)
            # The right stack contains the longest complete control line.
            # Give it the extra 40px it actually needs instead of clipping
            # labels or adding spacing inside individual panels.
            columns.columnconfigure(1, weight=0, minsize=_RIGHT_COLUMN_WIDTH)
            # Keep the two independently-sized stacks pinned to the very top
            # of the available area.  A weighted grid row can leave a small
            # theme-dependent lead-in above a LabelFrame on some systems.
            columns.grid_anchor("nw")
            col1 = ttk.Frame(columns)
            col1.grid(row=0, column=0, sticky="new", padx=(0, 6))
            # The left stack is locked to its widest real row after the
            # complete UI has been measured.  Without this, grid preserves a
            # surplus strip to the right of the patrol canvas and stretches
            # every left LabelFrame to match it.
            col1.configure(width=_LEFT_COLUMN_WIDTH)
            col1.grid_propagate(False)
            self._col1_frame = col1

            col2 = ttk.Frame(columns)
            col2.grid(row=0, column=1, sticky="new", padx=(6, 0))
            # Keep the right stack at one stable width.  Its panel contents
            # must wrap/truncate within it instead of changing grid geometry
            # and causing a full window relayout.
            col2.configure(width=_RIGHT_COLUMN_WIDTH)
            col2.grid_propagate(False)
            self._col2_frame = col2

            controls = ttk.LabelFrame(col1, text="图层校准与巡逻", padding=8)
            controls.pack(fill="x", pady=(0, 8))
            style = ttk.Style(root)
            # Reusable direct numeric-control styles.  Its containing frame
            # has a fixed height, so the larger hover/active value cannot
            # shift neighbouring widgets or flicker its setting label.
            style.configure("NumericValue.TLabel", font=("Segoe UI", 10))
            style.configure("NumericValueHover.TLabel", font=("Segoe UI", 12))
            style.configure(
                "NumericValueActive.TLabel",
                font=("Segoe UI", 12), foreground="#007ad9",
            )
            style.configure(
                "NumericValueDisabled.TLabel",
                font=("Segoe UI", 10), foreground="#a0a0a0",
            )
            style.configure("Locked.TButton", foreground="#777777")
            style.map("Locked.TButton", foreground=[("!disabled", "#777777")])
            # 本地测谎 without a CT card: the radio is greyed, and a disabled
            # Radiobutton alone is easy to miss in this theme (its text keeps the
            # normal colour under some ttk versions), so the grey is explicit and
            # the label says why it is unavailable.
            style.configure("Greyed.TRadiobutton", foreground="#a0a0a0")
            style.map(
                "Greyed.TRadiobutton",
                foreground=[("disabled", "#a0a0a0"), ("!disabled", "#a0a0a0")],
            )
            # Recorded coordinates are the widest left-side cells. A compact
            # record style keeps all three inside the 500px column.
            style.configure("Record.TButton", font=("Segoe UI", 8), padding=(1, 1))
            style.configure(
                "RecordLocked.TButton", font=("Segoe UI", 8), padding=(1, 1),
                foreground="#777777"
            )
            style.map(
                "RecordLocked.TButton", foreground=[("!disabled", "#777777")]
            )
            # Show Detection toggle: grey (inactive) until checked.
            style.configure("Off.TCheckbutton", foreground="#999999")
            style.map(
                "Off.TCheckbutton",
                foreground=[("selected", "#000000"), ("!selected", "#999999")],
            )
            action_row = ttk.Frame(controls)
            action_row.pack(fill="x", pady=(0, 8))
            self._start_patrol_button = ttk.Button(
                action_row, text="开始运行", width=7, command=self._start_patrol
            )
            self._start_patrol_button.pack(side="left", padx=(0, 4))
            self._stop_patrol_button = ttk.Button(
                action_row, text="停止运行", width=7, command=self._stop_patrol
            )
            self._stop_patrol_button.pack(side="left", padx=(0, 4))
            self._add_layer_button = ttk.Button(
                action_row, text="添加楼层", width=7, command=self._add_layer_above
            )
            self._add_layer_button.pack(side="left", padx=(0, 4))
            self._delete_layer_button = ttk.Button(
                action_row, text="删除楼层", width=7, command=self._delete_highest_layer
            )
            self._delete_layer_button.pack(side="left", padx=(0, 4))
            self._reset_recording_button = ttk.Button(
                action_row, text="重置录制", width=7, command=self._reset_recording
            )
            self._reset_recording_button.pack(side="left")
            # One shared radio value makes the currently selected recording
            # layer visible and clickable in every dynamically built row.
            self._selected_layer_var = tk.StringVar(value="")
            # Contiguous patrol floor range: patrol ONLY the selected floors
            # (a single floor is allowed); a fall outside the range makes the
            # character return to it.  layer1 is no longer implicitly the
            # patrol start.
            range_row = ttk.Frame(controls)
            range_row.pack(fill="x", pady=(0, 8))
            ttk.Label(range_row, text="巡逻楼层:").pack(side="left", padx=(0, 4))
            self._patrol_start_var = tk.StringVar()
            self._patrol_start_combo = ttk.Combobox(
                range_row, textvariable=self._patrol_start_var,
                width=8, state="readonly", values=[],
            )
            self._patrol_start_combo.pack(side="left", padx=(0, 2))
            self._patrol_start_combo.bind(
                "<<ComboboxSelected>>", self._patrol_range_changed
            )
            ttk.Label(range_row, text="→").pack(side="left", padx=(0, 2))
            self._patrol_end_var = tk.StringVar()
            self._patrol_end_combo = ttk.Combobox(
                range_row, textvariable=self._patrol_end_var,
                width=8, state="readonly", values=[],
            )
            self._patrol_end_combo.pack(side="left", padx=(0, 2))
            self._patrol_end_combo.bind(
                "<<ComboboxSelected>>", self._patrol_range_changed
            )
            self._layer_rows_frame = ttk.Frame(controls)
            self._layer_rows_frame.pack(fill="x")
            self._layer_axis_canvases: dict[str, Any] = {}
            # ``ttk.Label`` does not implement ``height`` on Tk 8.6 / Python
            # 3.10.  Reserve text space with a fixed-height parent instead;
            # the changing patrol-start status can then never shift the rows
            # below it on older machines.
            control_status_slot = ttk.Frame(controls, height=40)
            control_status_slot.pack(fill="x", pady=(8, 0))
            control_status_slot.pack_propagate(False)
            self._control_status = ttk.Label(
                control_status_slot,
                text="先录制 最左、绳索、最右，然后添加上方图层。",
                justify="left", wraplength=330,
            )
            self._control_status.pack(anchor="w", fill="x")
            automation_status_slot = ttk.Frame(controls, height=21)
            automation_status_slot.pack(fill="x", pady=(5, 0))
            automation_status_slot.pack_propagate(False)
            self._automation_status_label = ttk.Label(
                automation_status_slot, justify="left", wraplength=330,
            )
            self._automation_status_label.pack(anchor="w", fill="x")
            self._refresh_patrol_controls()

            # Detection info panel: hidden by default (kept for future use).
            if self._SHOW_DETECTION_INFO:
                info = ttk.LabelFrame(col1, text="检测", padding=10)
                info.pack(fill="x", pady=(0, 8))
                self._info_label = ttk.Label(
                    info, text="等待第一帧…", justify="left"
                )
                self._info_label.pack(anchor="w")

            yolo_panel = ttk.LabelFrame(col2, text="YOLO 怪物检测", padding=10)
            if self._SHOW_YOLO_PANEL:
                yolo_panel.pack(fill="x", pady=(0, 8))
            # Reference kept so the Fixed Attack panel can grey this whole
            # panel out when the fixed-rate mode is selected.
            self._yolo_panel = yolo_panel
            yolo_row = ttk.Frame(yolo_panel)
            yolo_row.pack(fill="x")
            ttk.Label(yolo_row, text="置信度阈值:").pack(side="left", padx=(0, 6))
            self._yolo_threshold_var = tk.DoubleVar(value=0.4)
            self._yolo_threshold_slider = ttk.Scale(
                yolo_row,
                from_=0.05,
                to=0.95,
                orient="horizontal",
                variable=self._yolo_threshold_var,
                command=self._yolo_on_threshold_change,
            )
            self._yolo_threshold_slider.pack(side="left", fill="x",
                                             expand=True, padx=(0, 8))
            self._yolo_threshold_label = ttk.Label(yolo_row, text="0.40", width=6)
            self._yolo_threshold_label.pack(side="left", padx=(0, 10))
            self._yolo_run_button = ttk.Button(
                yolo_row, text="运行", command=self._yolo_start
            )
            self._yolo_run_button.pack(side="left", padx=(0, 8))
            self._yolo_stop_button = ttk.Button(
                yolo_row, text="停止", command=self._yolo_stop, state="disabled"
            )
            self._yolo_stop_button.pack(side="left", padx=(0, 8))
            # 显示检测画面 / 保存配置 放在独立一行：避免与小窗口/高 DPI 下
            # 的滑条挤在同一行而被挤出面板外看不到。
            show_row = ttk.Frame(yolo_panel)
            show_row.pack(fill="x", pady=(6, 0))
            # Show-detection toggle: grey/inactive by default; only when
            # activated does Run open the visible detection window.
            self._yolo_show_var = tk.BooleanVar(value=False)
            self._yolo_show_button = ttk.Checkbutton(
                show_row,
                text="显示检测画面",
                variable=self._yolo_show_var,
                command=self._yolo_sync_show_button,
            )
            self._yolo_show_button.pack(side="left")
            self._yolo_show_button.configure(style="Off.TCheckbutton")
            # Save configuration: persist the current YOLO panel values so
            # they are restored next launch (no need to re-tune every time).
            self._yolo_save_button = ttk.Button(
                show_row, text="保存配置", command=self._yolo_save_config
            )
            self._yolo_save_button.pack(side="left", padx=(8, 0))
            # Attack range: horizontal slider (progress-bar style).  Value is
            # a PERCENTAGE of the game window width - the real pixels are
            # computed from the actual window size at runtime, so it adapts
            # to any resolution automatically.
            # 自动攻击行为由「攻击模式」面板统一设置（YOLO 检测模式 = 自动攻击）。
            range_row = ttk.Frame(yolo_panel)
            range_row.pack(fill="x", pady=(6, 0))
            ttk.Label(range_row, text="攻击范围:").pack(
                side="left", padx=(0, 6)
            )
            self._yolo_attack_range_var = tk.IntVar(value=30)
            self._yolo_attack_range_slider = ttk.Scale(
                range_row,
                from_=5,
                to=80,
                orient="horizontal",
                variable=self._yolo_attack_range_var,
                command=self._yolo_on_range_change,
            )
            self._yolo_attack_range_slider.pack(side="left", fill="x",
                                                expand=True, padx=(0, 8))
            self._yolo_attack_range_label = ttk.Label(
                range_row, text="30%", width=8
            )
            self._yolo_attack_range_label.pack(side="left")
            # Minimum/maximum mob box size: ONE progress bar controls the
            # minimum as a PERCENTAGE of the game window width; the maximum
            # is 4x the minimum automatically (both are resolution-
            # independent and applied per frame by the detector).
            mob_size_row = ttk.Frame(yolo_panel)
            mob_size_row.pack(fill="x", pady=(4, 0))
            ttk.Label(mob_size_row, text="怪物尺寸范围:").pack(
                side="left", padx=(0, 6)
            )
            self._yolo_min_mob_var = tk.IntVar(value=2)
            self._yolo_min_mob_slider = ttk.Scale(
                mob_size_row,
                from_=1,
                to=15,
                orient="horizontal",
                variable=self._yolo_min_mob_var,
                command=self._yolo_on_min_mob_change,
            )
            self._yolo_min_mob_slider.pack(side="left", fill="x",
                                           expand=True, padx=(0, 8))
            self._yolo_min_mob_label = ttk.Label(
                mob_size_row, text="最小 2% / 最大 8%", width=16
            )
            self._yolo_min_mob_label.pack(side="left")
            # Detection frequency: frames per second, 2-30, middle = 10 fps
            # (the default).  Lower = less GPU load, slower reaction.
            fps_row = ttk.Frame(yolo_panel)
            fps_row.pack(fill="x", pady=(4, 0))
            ttk.Label(fps_row, text="检测帧率:").pack(
                side="left", padx=(0, 6)
            )
            self._yolo_fps_var = tk.IntVar(value=10)
            self._yolo_fps_slider = ttk.Scale(
                fps_row,
                from_=2,
                to=30,
                orient="horizontal",
                variable=self._yolo_fps_var,
                command=self._yolo_on_fps_change,
            )
            self._yolo_fps_slider.pack(side="left", fill="x",
                                       expand=True, padx=(0, 8))
            self._yolo_fps_label = ttk.Label(fps_row, text="10 帧/秒", width=8)
            self._yolo_fps_label.pack(side="left")
            # Detection zone size: width and height sliders (progress-bar
            # style) that scale the detection area as a fraction of the frame.
            zone_row = ttk.Frame(yolo_panel)
            zone_row.pack(fill="x", pady=(4, 0))
            ttk.Label(zone_row, text="检测区宽度:").pack(side="left", padx=(0, 6))
            self._yolo_zone_w_var = tk.IntVar(value=60)
            self._yolo_zone_w_slider = ttk.Scale(
                zone_row, from_=20, to=100, orient="horizontal",
                variable=self._yolo_zone_w_var,
                command=self._yolo_on_zone_change,
            )
            self._yolo_zone_w_slider.pack(side="left", fill="x", expand=True,
                                          padx=(0, 8))
            self._yolo_zone_w_label = ttk.Label(zone_row, text="60%", width=8)
            self._yolo_zone_w_label.pack(side="left")
            zone_row2 = ttk.Frame(yolo_panel)
            zone_row2.pack(fill="x", pady=(4, 0))
            ttk.Label(zone_row2, text="检测区高度:").pack(side="left", padx=(0, 6))
            self._yolo_zone_h_var = tk.IntVar(value=60)
            self._yolo_zone_h_slider = ttk.Scale(
                zone_row2, from_=20, to=100, orient="horizontal",
                variable=self._yolo_zone_h_var,
                command=self._yolo_on_zone_change,
            )
            self._yolo_zone_h_slider.pack(side="left", fill="x", expand=True,
                                          padx=(0, 8))
            self._yolo_zone_h_label = ttk.Label(zone_row2, text="60%", width=8)
            self._yolo_zone_h_label.pack(side="left")
            zone_row3 = ttk.Frame(yolo_panel)
            zone_row3.pack(fill="x", pady=(4, 0))
            ttk.Label(zone_row3, text="检测区垂直偏移:").pack(side="left", padx=(0, 6))
            self._yolo_zone_shift_y_var = tk.IntVar(value=0)
            self._yolo_zone_shift_y_slider = ttk.Scale(
                zone_row3, from_=-50, to=50, orient="horizontal",
                variable=self._yolo_zone_shift_y_var,
                command=self._yolo_on_zone_change,
            )
            self._yolo_zone_shift_y_slider.pack(side="left", fill="x",
                                                expand=True, padx=(0, 8))
            self._yolo_zone_shift_y_label = ttk.Label(zone_row3, text="0%", width=8)
            self._yolo_zone_shift_y_label.pack(side="left")
            # 显示检测画面（可选，默认不显示）；YOLO 依赖由 安装.bat 自动安装。
            self._yolo_status = ttk.Label(
                yolo_panel, text="YOLO 检测已停止。", justify="left",
                wraplength=440,
            )
            self._yolo_status.pack(anchor="w", pady=(6, 0))
            # Restore previously saved YOLO panel settings (threshold, ranges).
            self._yolo_load_settings()

            # Attack-mode panel: choose the attack engine. Either the YOLO
            # detection mode (mob detection + auto attack using the attack
            # key below) or a fixed-rate attack that taps the attack key every
            # N seconds. Selecting the fixed mode greys out the YOLO panel;
            # the fixed worker lives in the assistant process (AttackWorker)
            # and is applied live.
            fixed_panel = ttk.LabelFrame(
                col1, text="攻击模式", padding=(6, 5)
            )
            fixed_panel.pack(fill="x", pady=(0, 8))
            mode_row = ttk.Frame(fixed_panel)
            mode_row.pack(fill="x")
            ttk.Label(mode_row, text="攻击模式:").pack(
                side="left", padx=(0, 8)
            )
            self._attack_mode_var = tk.StringVar(
                value=("yolo" if self._YOLO_MONSTER_DETECTION_ENABLED
                       else "fixed")
            )
            self._dual_stationary_posts: list[dict[str, Any]] = []
            yolo_mode_button = ttk.Radiobutton(
                mode_row, text="YOLO 检测", value="yolo",
                variable=self._attack_mode_var,
                command=self._fixed_on_mode_change,
            )
            if self._SHOW_YOLO_PANEL:
                yolo_mode_button.pack(side="left", padx=(0, 12))
            if not self._YOLO_MONSTER_DETECTION_ENABLED:
                yolo_mode_button.configure(state="disabled")
            ttk.Radiobutton(
                mode_row, text="巡逻攻击", value="fixed",
                variable=self._attack_mode_var,
                command=self._fixed_on_mode_change,
            ).pack(side="left")
            ttk.Radiobutton(
                mode_row, text="站桩攻击", value="stationary",
                variable=self._attack_mode_var,
                command=self._fixed_on_mode_change,
            ).pack(side="left", padx=(0, 8))
            ttk.Radiobutton(
                mode_row, text="双桩模式", value="dual_stationary",
                variable=self._attack_mode_var,
                command=self._fixed_on_mode_change,
            ).pack(side="left", padx=(0, 8))
            ttk.Radiobutton(
                mode_row, text="死花模式", value="dead_flower",
                variable=self._attack_mode_var,
                command=self._fixed_on_mode_change,
            ).pack(side="left")
            ttk.Radiobutton(
                mode_row, text="跑花模式", value="flower_patrol",
                variable=self._attack_mode_var,
                command=self._fixed_on_mode_change,
            ).pack(side="left", padx=(8, 0))
            # 随机休息 is created further down, inside the 延时回桩/跳反打 row: it is
            # a mode-scoped optional modifier just like they are, and its interval
            # is a hover tooltip rather than a permanent label on the panel.
            # These rows configure attacks only.  死花模式 still leaves the
            # mode selector and its 重置花 controls available, but must make
            # every normal attack option visibly unavailable.
            self._dead_flower_attack_rows: list[Any] = []
            fixed_key_row = ttk.Frame(fixed_panel)
            self._fixed_key_row = fixed_key_row
            self._dead_flower_attack_rows.append(fixed_key_row)
            fixed_key_row.pack(fill="x", pady=(4, 0))
            # Attack delivery is a mutually exclusive group.  A literal '-'
            # attack binding is the durable no-attack configuration; it does
            # not need a separate visible radio option.
            self._attack_delivery_var = tk.StringVar(value="stand")
            self._attack_delivery_buttons: list[Any] = []
            for delivery_value, delivery_text in (
                ("stand", "站打"), ("jump", "跳打"),
                ("charge", "蓄力"),
            ):
                button = ttk.Radiobutton(
                    fixed_key_row, text=delivery_text,
                    value=delivery_value,
                    variable=self._attack_delivery_var,
                    command=self._fixed_on_change,
                )
                button.pack(side="left", padx=(0, 1))
                self._attack_delivery_buttons.append(button)
            # Compatibility alias: existing worker/apply paths still derive
            # their active jump flag from the selected delivery value.
            self._jump_attack_enabled_var = tk.BooleanVar(value=False)
            fixed_key_label = ttk.Label(fixed_key_row, text="攻击键:")
            self._fixed_key_label = fixed_key_label
            fixed_key_label.pack(
                side="left", padx=(0, 0)
            )
            self._fixed_attack_key_var = tk.StringVar(value="ctrl")
            fixed_key_button = ttk.Button(
                fixed_key_row, text=self._fixed_attack_key_var.get(),
                width=4, style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    fixed_key_button, self._fixed_attack_key_var,
                    "_fixed_attack_key_previous",
                    lambda: self._fixed_on_change(),
                    allow_null=True,
                ),
            )
            fixed_key_button.pack(side="left", padx=(0, 0))
            self._fixed_key_button = fixed_key_button
            self._attach_bind_hint(fixed_key_button)
            # The cadence is an inclusive min/max range.  The adjacent
            # "间隔" label is intentionally outside the reusable numeric
            # component, so it stays still while the values enlarge.
            ttk.Label(fixed_key_row, text="每").pack(side="left", padx=(0, 0))
            self._fixed_interval_control = NumericValueRangeControl(
                fixed_key_row,
                tk_module=tk,
                ttk_module=ttk,
                minimum=3.0,
                maximum=3.1,
                range_mode=True,
                value_type=float,
                step=self._FIXED_ATTACK_INTERVAL_STEP,
                lower_bound=0.2,
                upper_bound=self._FIXED_ATTACK_INTERVAL_MAX,
                unit="s",
                value_width=5,
                # The cadence can display two full five-character values
                # (for example 0.25s - 7.55s).  Keep their paint inside this
                # frame so it cannot overlap the following 蓄力 label.
                extra_width_px=12,
                before_change=lambda: self._undo_capture_callback("攻击间隔"),
                on_change=self._fixed_on_change,
            )
            self._fixed_min_interval_var = self._fixed_interval_control.minimum_var
            self._fixed_max_interval_var = self._fixed_interval_control.maximum_var
            # Alias retained for older hotkey/settings paths that represent
            # the cadence's lower bound.
            self._fixed_interval_var = self._fixed_min_interval_var
            self._fixed_min_interval_label = self._fixed_interval_control.min_label
            self._fixed_max_interval_label = self._fixed_interval_control.max_label
            self._fixed_interval_group = self._fixed_interval_control.frame
            self._fixed_interval_control.pack(side="left")
            self._charge_hold_label = ttk.Label(fixed_key_row, text="蓄力")
            self._charge_hold_label.pack(side="left", padx=(0, 0))
            self._charge_hold_control = NumericValueRangeControl(
                fixed_key_row,
                tk_module=tk,
                ttk_module=ttk,
                minimum=0.5,
                maximum=0.5,
                range_mode=True,
                value_type=float,
                step=0.1,
                lower_bound=0.1,
                upper_bound=5.0,
                unit="s",
                value_width=4,
                # Two 4-character values plus the separator need this final
                # 8px reserve; column width alone cannot unclip a fixed frame.
                extra_width_px=8,
                before_change=lambda: self._undo_capture_callback("蓄力时间"),
                on_change=self._fixed_on_change,
            )
            self._charge_hold_control.pack(side="left", padx=(0, 0))

            # 被撞反击 is an event path separate from normal cadence/combo
            # attack.  A confirmed HP drop asks the arbiter to step away from
            # the hit and then tap this independently bound key.
            counterattack_row = ttk.Frame(fixed_panel, height=34)
            self._dead_flower_attack_rows.append(counterattack_row)
            self._stationary_action_row = counterattack_row
            counterattack_row.pack(fill="x", pady=(2, 0))
            counterattack_row.pack_propagate(False)
            self._counterattack_enabled_var = tk.BooleanVar(value=False)
            counterattack_button = ttk.Checkbutton(
                counterattack_row, text="被撞反击", width=7,
                variable=self._counterattack_enabled_var,
                command=self._fixed_on_change,
            )
            counterattack_button.pack(side="left", padx=(0, 4))
            counterattack_hint = HoverTooltip(
                counterattack_button,
                "建议在怪物少的地图使用，否则频繁被撞影响正常输出。",
            )
            counterattack_hint.set_enabled(True)
            self._bind_key_tooltips.append(counterattack_hint)
            ttk.Label(counterattack_row, text="攻击键:").pack(
                side="left", padx=(0, 4)
            )
            self._counterattack_key_var = tk.StringVar(value="ctrl")
            counterattack_key_button = ttk.Button(
                counterattack_row, text=self._counterattack_key_var.get(),
                width=5, style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    counterattack_key_button, self._counterattack_key_var,
                    "_counterattack_key_previous", self._fixed_on_change,
                ),
            )
            counterattack_key_button.pack(side="left")
            self._counterattack_key_button = counterattack_key_button
            self._attach_bind_hint(counterattack_key_button)

            # Stationary-only options share one row.  The visible child is
            # selected from the current mode rather than leaving disabled
            # controls and their padding on unrelated modes.
            stationary_options_row = ttk.Frame(fixed_panel)
            self._stationary_options_row = stationary_options_row
            # 双桩的换桩时间 belongs in the shared third action row, next to
            # 被撞反击.  It is packed only while 双桩模式 is selected.
            dual_stationary_row = ttk.Frame(counterattack_row)
            self._dual_stationary_row = dual_stationary_row
            ttk.Label(dual_stationary_row, text="换桩 每").pack(
                side="left", padx=(0, 2)
            )
            self._dual_stationary_switch_control = NumericValueRangeControl(
                dual_stationary_row,
                tk_module=tk, ttk_module=ttk,
                minimum=self._DUAL_STATIONARY_SWITCH_DEFAULT_MINUTES,
                maximum=self._DUAL_STATIONARY_SWITCH_DEFAULT_MINUTES,
                range_mode=True, value_type=float, step=0.1,
                lower_bound=1.0, upper_bound=30.0, unit="m",
                value_width=5,
                # Two values such as 11.1m - 11.1m need their full glyph
                # width inside this fixed component.
                extra_width_px=16,
                before_change=lambda: self._undo_capture_callback("换桩时间"),
                on_change=self._fixed_on_change,
            )
            self._dual_stationary_switch_minutes_var = (
                self._dual_stationary_switch_control.minimum_var
            )
            self._dual_stationary_switch_max_minutes_var = (
                self._dual_stationary_switch_control.maximum_var
            )
            # The group itself is mode-only; its numeric control is always
            # packed inside that group so it has a real width and height once
            # the group becomes visible.
            self._dual_stationary_switch_control.pack(side="left")

            # 组合攻击 only configures the independent attack scheduler;
            # movement, patrol, and input arbitration keep their own roles.
            combo_row = ttk.Frame(fixed_panel)
            self._dead_flower_attack_rows.append(combo_row)
            # 组合攻击 is the second visual row, directly below the delivery
            # choice and cadence row.
            combo_row.pack(fill="x", pady=(2, 0), before=counterattack_row)
            self._combo_attack_enabled_var = tk.BooleanVar(value=False)
            combo_attack_button = ttk.Checkbutton(
                combo_row, text="组合攻击", width=7,
                variable=self._combo_attack_enabled_var,
                command=self._fixed_on_change,
            )
            combo_attack_button.pack(side="left", padx=(0, 4))
            self._combo_attack_count_vars = []
            self._combo_attack_count_controls = []
            self._combo_attack_key_vars = []
            self._combo_attack_key_buttons = []
            for index in range(3):
                slot = ttk.Frame(combo_row)
                slot.pack(side="left", padx=(0, 5))
                count_control = NumericValueRangeControl(
                    slot,
                    tk_module=tk, ttk_module=ttk,
                    minimum=1, maximum=1,
                    range_mode=True, value_type=int, step=1,
                    lower_bound=0, upper_bound=30,
                    # Both integer endpoints (for example 30 - 30) must
                    # remain inside the count component, not overpaint 次.
                    value_width=2, extra_width_px=8,
                    before_change=lambda index=index: self._undo_capture_callback(
                        f"组合攻击{index + 1}"
                    ),
                    on_change=self._fixed_on_change,
                )
                self._combo_attack_count_controls.append(count_control)
                self._combo_attack_count_vars.append(
                    (count_control.minimum_var, count_control.maximum_var)
                )
                count_control.pack(side="left", padx=(0, 2))
                ttk.Label(slot, text="次").pack(side="left", padx=(0, 2))
                key_var = tk.StringVar(value="ctrl")
                self._combo_attack_key_vars.append(key_var)
                key_button = ttk.Button(slot, text="ctrl", width=5,
                                        style="Locked.TButton")
                key_button.configure(
                    command=lambda button=key_button, var=key_var, slot_index=index:
                    self._bind_capture_begin(
                        button, var,
                        f"_combo_attack_key_{slot_index}_previous",
                        lambda: self._fixed_on_change(), allow_null=True,
                    )
                )
                key_button.pack(side="left")
                self._combo_attack_key_buttons.append(key_button)
                tooltip = HoverTooltip(
                    key_button,
                    bindable_keys_hint()
                    + "\n按 - 可设置为空按键（该次不发送攻击）。",
                )
                tooltip.set_enabled(True)
                self._bind_key_tooltips.append(tooltip)
            combo_attack_tooltip = HoverTooltip(
                combo_attack_button, self._combo_attack_hint()
            )
            combo_attack_tooltip.set_enabled(True)
            self._combo_attack_tooltip = combo_attack_tooltip
            self._bind_key_tooltips.append(combo_attack_tooltip)

            # Facing is a stand-still recovery option, not part of 小碎步.
            # Left and right are mutually exclusive, and the selected side
            # is applied after an X correction returns to the temporary zone.
            # 朝向 occupies the same mode-specific third-row slot as 换桩.
            stationary_facing_row = ttk.Frame(counterattack_row)
            self._stationary_facing_row = stationary_facing_row
            self._dead_flower_attack_rows.append(stationary_facing_row)
            ttk.Label(stationary_facing_row, text="朝向").pack(side="left")
            self._stationary_facing_direction_var = tk.StringVar(value="right")
            stationary_facing_controls = []
            facing_left = ttk.Radiobutton(
                stationary_facing_row, text="左", value="left",
                variable=self._stationary_facing_direction_var,
                command=self._fixed_on_change,
            )
            facing_left.pack(side="left", padx=(4, 4))
            stationary_facing_controls.append(facing_left)
            facing_right = ttk.Radiobutton(
                stationary_facing_row, text="右", value="right",
                variable=self._stationary_facing_direction_var,
                command=self._fixed_on_change,
            )
            facing_right.pack(side="left")
            stationary_facing_controls.append(facing_right)
            facing_both = ttk.Radiobutton(
                stationary_facing_row, text="双向", value="both",
                variable=self._stationary_facing_direction_var,
                command=self._fixed_on_change,
            )
            facing_both.pack(side="left", padx=(4, 0))
            stationary_facing_controls.append(facing_both)
            self._stationary_facing_controls = stationary_facing_controls

            # The remaining optional actions share the counterattack third
            # row. There are no alignment spacers: each control begins after
            # the preceding control's real width.
            pickup_row = ttk.Frame(stationary_options_row)
            self._stationary_pickup_row = pickup_row
            self._stationary_pickup_enabled_var = tk.BooleanVar(value=False)
            pickup_enabled = ttk.Checkbutton(
                pickup_row, text="定时巡逻", width=7,
                variable=self._stationary_pickup_enabled_var,
                command=self._fixed_on_change,
            )
            pickup_enabled.pack(side="left", padx=(0, 4))
            self._stationary_pickup_enabled_button = pickup_enabled
            ttk.Label(pickup_row, text="每").pack(side="left", padx=(0, 2))
            self._stationary_pickup_control = NumericValueRangeControl(
                pickup_row,
                tk_module=tk, ttk_module=ttk,
                minimum=self._STATIONARY_PICKUP_INTERVAL_DEFAULT_MINUTES,
                maximum=self._STATIONARY_PICKUP_INTERVAL_DEFAULT_MINUTES,
                range_mode=True, value_type=float, step=0.1,
                lower_bound=1.0, upper_bound=30.0, unit="m",
                value_width=5,
                # Keep the full second value inside the pickup component so
                # it cannot paint underneath the following 小碎步 checkbox.
                extra_width_px=24,
                before_change=lambda: self._undo_capture_callback("定时巡逻间隔"),
                on_change=self._fixed_on_change,
            )
            self._stationary_pickup_interval_var = self._stationary_pickup_control.minimum_var
            self._stationary_pickup_max_minutes_var = self._stationary_pickup_control.maximum_var
            self._stationary_pickup_control.pack(side="left")
            self._stationary_pickup_controls = [
                pickup_enabled,
                self._stationary_pickup_control.min_label,
                self._stationary_pickup_control.max_label,
            ]

            # This is intentionally a separate line: it modifies only the
            # return from the timed-patrol circuit, never a normal stationary
            # correction or a knockback recovery.
            delayed_return_row = ttk.Frame(fixed_panel)
            self._stationary_delayed_return_row = delayed_return_row
            self._stationary_delayed_return_enabled_var = tk.BooleanVar(value=False)
            delayed_return_button = ttk.Checkbutton(
                delayed_return_row,
                text="延时回桩",
                width=7,
                variable=self._stationary_delayed_return_enabled_var,
                command=self._fixed_on_change,
            )
            delayed_return_button.pack(side="left")
            self._stationary_delayed_return_controls = [delayed_return_button]
            delayed_return_hint = HoverTooltip(
                delayed_return_button,
                "等待1~3 秒再回桩点",
            )
            delayed_return_hint.set_enabled(True)
            self._bind_key_tooltips.append(delayed_return_hint)

            self._jump_counterattack_enabled_var = tk.BooleanVar(value=False)
            jump_counterattack_button = ttk.Checkbutton(
                delayed_return_row,
                text="跳反打",
                width=6,
                variable=self._jump_counterattack_enabled_var,
                command=self._fixed_on_change,
            )
            jump_counterattack_button.pack(side="left", padx=(8, 0))
            self._jump_counterattack_button = jump_counterattack_button
            jump_counterattack_hint = HoverTooltip(
                jump_counterattack_button,
                "跳起来的时候反打，适用于移动速度快的怪物比如白狼",
            )
            jump_counterattack_hint.set_enabled(True)
            self._bind_key_tooltips.append(jump_counterattack_hint)

            # 随机休息 shares this row, directly after 跳反打: like it, it is an
            # optional modifier of the active mode (巡逻攻击 / 站桩攻击 /
            # 双桩模式).  The panel greys it out everywhere else instead of hiding
            # it, so the option stays discoverable without changing the panel
            # geometry.  The 75m~120m / 3s~15s schedule is a hover hint, not a
            # permanent label, so the row stays one line wide.
            self._random_rest_var = tk.BooleanVar(value=False)
            self._random_rest_check = ttk.Checkbutton(
                delayed_return_row,
                text="随机休息",
                width=7,
                variable=self._random_rest_var,
                command=self._fixed_on_change,
            )
            self._random_rest_check.pack(side="left", padx=(8, 0))
            random_rest_hint = HoverTooltip(
                self._random_rest_check,
                "每75m~120m 休息3s~15s",
            )
            random_rest_hint.set_enabled(True)
            self._random_rest_hint = random_rest_hint
            self._bind_key_tooltips.append(random_rest_hint)

            step_row = ttk.Frame(stationary_options_row)
            self._small_step_options_row = step_row
            self._small_step_row = step_row
            self._dead_flower_attack_rows.append(step_row)
            self._small_step_enabled_var = tk.BooleanVar(value=False)
            small_step_button = ttk.Checkbutton(
                step_row, text="小碎步", width=5,
                variable=self._small_step_enabled_var,
                command=self._fixed_on_change,
            )
            small_step_button.pack(side="left", padx=(0, 2))
            small_step_hint = HoverTooltip(
                small_step_button,
                "可以用来防止空打（空打指的是站桩连续攻击近一分钟后伤害会消失）。",
            )
            small_step_hint.set_enabled(True)
            self._bind_key_tooltips.append(small_step_hint)
            ttk.Label(step_row, text="每").pack(side="left", padx=(0, 2))
            self._small_step_control = NumericValueRangeControl(
                step_row,
                tk_module=tk, ttk_module=ttk,
                minimum=5.0, maximum=5.0,
                range_mode=True, value_type=float, step=0.1,
                lower_bound=5.0, upper_bound=60.0, unit="s",
                value_width=5,
                # Keep this cadence range compact: the 后摇 field follows in
                # the same row.
                extra_width_px=4,
                before_change=lambda: self._undo_capture_callback("小碎步间隔"),
                on_change=self._fixed_on_change,
            )
            self._small_step_interval_var = self._small_step_control.minimum_var
            self._small_step_max_seconds_var = self._small_step_control.maximum_var
            self._small_step_control.pack(side="left")
            ttk.Label(step_row, text="后摇").pack(side="left", padx=(4, 1))
            self._small_step_post_attack_control = NumericValueRangeControl(
                step_row,
                tk_module=tk, ttk_module=ttk,
                minimum=0.70,
                range_mode=False, value_type=float, step=0.05,
                lower_bound=0.20, upper_bound=5.00, unit="s",
                value_width=5,
                before_change=lambda: self._undo_capture_callback("小碎步后摇"),
                on_change=self._fixed_on_change,
            )
            self._small_step_post_attack_control.pack(side="left")

            self._fixed_status = ttk.Label(
                fixed_panel, text="巡逻攻击未启用。", justify="left",
                wraplength=440,
            )
            self._fixed_load_settings()
            # Keep the status sink for non-UI callers, but do not display a
            # hint in the attack-mode panel.  It consumed vertical space and
            # duplicated information available in the run/status panels.
            # Defensive: after realization, update only stationary control
            # states. It must never pack/unpack rows or relayout column 0.
            root.after(200, self._fixed_refresh_rows)

            # Drug (HP/MP potion) panel: key binds + percent trigger sliders.
            # The StatusWorker taps the bound key when the bar ratio drops
            # below the chosen percent (debounced by frames + cooldown).
            drug_panel = ttk.LabelFrame(
                col2, text="药品 (HP/MP 药水)", padding=(8, 6)
            )
            self._drug_panel = drug_panel
            drug_panel.pack(fill="x", pady=(0, 8))
            # Keep this panel dense: two independent four-row columns rather
            # than one long column that widens the entire right side.
            drug_columns = ttk.Frame(drug_panel)
            drug_columns.pack(anchor="w")
            drug_left = ttk.Frame(drug_columns)
            drug_left.pack(side="left", anchor="n")
            drug_right = ttk.Frame(drug_columns)
            drug_right.pack(side="left", anchor="n", padx=(12, 0))
            hp_row = ttk.Frame(drug_left)
            hp_row.pack(fill="x")
            self._hp_use_var = tk.BooleanVar(value=True)
            hp_use_button = ttk.Checkbutton(
                hp_row, text="HP", width=6, variable=self._hp_use_var,
                command=self._drug_on_change,
            )
            hp_use_button.pack(side="left")
            self._hp_key_var = tk.StringVar(value="delete")
            hp_key_button = ttk.Button(
                hp_row, text=self._hp_key_var.get(), width=10,
                style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    hp_key_button, self._hp_key_var, "_hp_key_previous",
                    lambda: self._drug_on_change(),
                ),
            )
            hp_key_button.pack(side="left", padx=(0, 4))
            self._hp_key_button = hp_key_button
            self._attach_bind_hint(hp_key_button)
            # Every visible value in the right column uses the same direct,
            # wheel-editable numeric component.  No Scale widgets remain to
            # force a wide trough or low-precision pointer drag.
            self._hp_threshold_control = NumericValueRangeControl(
                hp_row, tk_module=tk, ttk_module=ttk,
                minimum=50, range_mode=False, value_type=int, step=1,
                lower_bound=5, upper_bound=95, unit="%", value_width=5,
                before_change=lambda: self._undo_capture_callback("HP阈值"),
                on_change=self._drug_on_change,
            )
            self._hp_threshold_var = self._hp_threshold_control.minimum_var
            self._hp_threshold_label = self._hp_threshold_control.min_label
            ttk.Label(hp_row, text="小于").pack(side="left", padx=(8, 2))
            self._hp_threshold_control.pack(side="left")
            mp_row = ttk.Frame(drug_left)
            mp_row.pack(fill="x", pady=(2, 0))
            self._mp_use_var = tk.BooleanVar(value=True)
            mp_use_button = ttk.Checkbutton(
                mp_row, text="MP", width=6, variable=self._mp_use_var,
                command=self._drug_on_change,
            )
            mp_use_button.pack(side="left")
            self._mp_key_var = tk.StringVar(value="end")
            mp_key_button = ttk.Button(
                mp_row, text=self._mp_key_var.get(), width=10,
                style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    mp_key_button, self._mp_key_var, "_mp_key_previous",
                    lambda: self._drug_on_change(),
                ),
            )
            mp_key_button.pack(side="left", padx=(0, 4))
            self._mp_key_button = mp_key_button
            self._attach_bind_hint(mp_key_button)
            self._mp_threshold_control = NumericValueRangeControl(
                mp_row, tk_module=tk, ttk_module=ttk,
                minimum=30, range_mode=False, value_type=int, step=1,
                lower_bound=5, upper_bound=95, unit="%", value_width=5,
                before_change=lambda: self._undo_capture_callback("MP阈值"),
                on_change=self._drug_on_change,
            )
            self._mp_threshold_var = self._mp_threshold_control.minimum_var
            self._mp_threshold_label = self._mp_threshold_control.min_label
            ttk.Label(mp_row, text="小于").pack(side="left", padx=(8, 2))
            self._mp_threshold_control.pack(side="left")
            # Periodic buff rows: a bound key tapped on a timer.  Each row has
            # its own 5..600 second slider (default 600 seconds) that decides
            # when the key is triggered.  Unlike HP/MP these are time-based,
            # not bar-percent based.
            buff1_row = ttk.Frame(drug_left)
            buff1_row.pack(fill="x", pady=(2, 0))
            self._buff1_use_var = tk.BooleanVar(value=False)
            buff1_use_button = ttk.Checkbutton(
                buff1_row, text="饲料", width=6, variable=self._buff1_use_var,
                command=self._drug_on_change,
            )
            buff1_use_button.pack(side="left")
            self._buff1_key_var = tk.StringVar(value="home")
            buff1_key_button = ttk.Button(
                buff1_row, text=self._buff1_key_var.get(), width=10,
                style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    buff1_key_button, self._buff1_key_var,
                    "_buff1_key_previous",
                    lambda: self._drug_on_change(),
                ),
            )
            buff1_key_button.pack(side="left", padx=(0, 4))
            self._buff1_key_button = buff1_key_button
            self._attach_bind_hint(buff1_key_button)
            self._buff1_interval_control = NumericValueRangeControl(
                buff1_row, tk_module=tk, ttk_module=ttk,
                minimum=600, range_mode=False, value_type=int, step=1,
                lower_bound=5, upper_bound=600, unit="s", value_width=5,
                extra_width_px=12,
                before_change=lambda: self._undo_capture_callback("饲料间隔"),
                on_change=self._drug_on_change,
            )
            self._buff1_interval_var = self._buff1_interval_control.minimum_var
            self._buff1_interval_label = self._buff1_interval_control.min_label
            ttk.Label(buff1_row, text="每隔").pack(side="left", padx=(8, 2))
            self._buff1_interval_control.pack(side="left")
            buff2_row = ttk.Frame(drug_left)
            buff2_row.pack(fill="x", pady=(2, 0))
            self._buff2_use_var = tk.BooleanVar(value=False)
            buff2_use_button = ttk.Checkbutton(
                buff2_row, text="补药", width=6, variable=self._buff2_use_var,
                command=self._drug_on_change,
            )
            buff2_use_button.pack(side="left")
            self._buff2_key_var = tk.StringVar(value="insert")
            buff2_key_button = ttk.Button(
                buff2_row, text=self._buff2_key_var.get(), width=10,
                style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    buff2_key_button, self._buff2_key_var,
                    "_buff2_key_previous",
                    lambda: self._drug_on_change(),
                ),
            )
            buff2_key_button.pack(side="left", padx=(0, 4))
            self._buff2_key_button = buff2_key_button
            self._attach_bind_hint(buff2_key_button)
            self._buff2_interval_control = NumericValueRangeControl(
                buff2_row, tk_module=tk, ttk_module=ttk,
                minimum=600, range_mode=False, value_type=int, step=1,
                lower_bound=5, upper_bound=600, unit="s", value_width=5,
                extra_width_px=12,
                before_change=lambda: self._undo_capture_callback("补药间隔"),
                on_change=self._drug_on_change,
            )
            self._buff2_interval_var = self._buff2_interval_control.minimum_var
            self._buff2_interval_label = self._buff2_interval_control.min_label
            ttk.Label(buff2_row, text="每隔").pack(side="left", padx=(8, 2))
            self._buff2_interval_control.pack(side="left")
            buff3_row = ttk.Frame(drug_right)
            buff3_row.pack(fill="x")
            self._buff3_use_var = tk.BooleanVar(value=False)
            buff3_use_button = ttk.Checkbutton(
                buff3_row, text="增益 1", width=6, variable=self._buff3_use_var,
                command=self._drug_on_change,
            )
            buff3_use_button.pack(side="left")
            self._buff3_key_var = tk.StringVar(value="pageup")
            buff3_key_button = ttk.Button(
                buff3_row, text=self._buff3_key_var.get(), width=10,
                style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    buff3_key_button, self._buff3_key_var,
                    "_buff3_key_previous",
                    lambda: self._drug_on_change(),
                ),
            )
            buff3_key_button.pack(side="left", padx=(0, 4))
            self._buff3_key_button = buff3_key_button
            self._attach_bind_hint(buff3_key_button)
            self._buff3_interval_control = NumericValueRangeControl(
                buff3_row, tk_module=tk, ttk_module=ttk,
                minimum=600, range_mode=False, value_type=int, step=1,
                lower_bound=5, upper_bound=600, unit="s", value_width=5,
                extra_width_px=12,
                before_change=lambda: self._undo_capture_callback("增益1间隔"),
                on_change=self._drug_on_change,
            )
            self._buff3_interval_var = self._buff3_interval_control.minimum_var
            self._buff3_interval_label = self._buff3_interval_control.min_label
            ttk.Label(buff3_row, text="每隔").pack(side="left", padx=(8, 2))
            self._buff3_interval_control.pack(side="left")
            buff4_row = ttk.Frame(drug_right)
            buff4_row.pack(fill="x", pady=(2, 0))
            self._buff4_use_var = tk.BooleanVar(value=False)
            buff4_use_button = ttk.Checkbutton(
                buff4_row, text="增益 2", width=6, variable=self._buff4_use_var,
                command=self._drug_on_change,
            )
            buff4_use_button.pack(side="left")
            self._buff4_key_var = tk.StringVar(value="pagedown")
            buff4_key_button = ttk.Button(
                buff4_row, text=self._buff4_key_var.get(), width=10,
                style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    buff4_key_button, self._buff4_key_var,
                    "_buff4_key_previous",
                    lambda: self._drug_on_change(),
                ),
            )
            buff4_key_button.pack(side="left", padx=(0, 4))
            self._buff4_key_button = buff4_key_button
            self._attach_bind_hint(buff4_key_button)
            self._buff4_interval_control = NumericValueRangeControl(
                buff4_row, tk_module=tk, ttk_module=ttk,
                minimum=600, range_mode=False, value_type=int, step=1,
                lower_bound=5, upper_bound=600, unit="s", value_width=5,
                extra_width_px=12,
                before_change=lambda: self._undo_capture_callback("增益2间隔"),
                on_change=self._drug_on_change,
            )
            self._buff4_interval_var = self._buff4_interval_control.minimum_var
            self._buff4_interval_label = self._buff4_interval_control.min_label
            ttk.Label(buff4_row, text="每隔").pack(side="left", padx=(8, 2))
            self._buff4_interval_control.pack(side="left")
            buff5_row = ttk.Frame(drug_right)
            buff5_row.pack(fill="x", pady=(2, 0))
            self._buff5_use_var = tk.BooleanVar(value=False)
            buff5_use_button = ttk.Checkbutton(
                buff5_row, text="增益 3", width=6, variable=self._buff5_use_var,
                command=self._drug_on_change,
            )
            buff5_use_button.pack(side="left")
            self._buff5_key_var = tk.StringVar(value="-")
            buff5_key_button = ttk.Button(
                buff5_row, text=self._buff5_key_var.get(), width=10,
                style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    buff5_key_button, self._buff5_key_var,
                    "_buff5_key_previous",
                    lambda: self._drug_on_change(),
                ),
            )
            buff5_key_button.pack(side="left", padx=(0, 4))
            self._buff5_key_button = buff5_key_button
            self._attach_bind_hint(buff5_key_button)
            self._buff5_interval_control = NumericValueRangeControl(
                buff5_row, tk_module=tk, ttk_module=ttk,
                minimum=600, range_mode=False, value_type=int, step=1,
                lower_bound=5, upper_bound=600, unit="s", value_width=5,
                extra_width_px=12,
                before_change=lambda: self._undo_capture_callback("增益3间隔"),
                on_change=self._drug_on_change,
            )
            self._buff5_interval_var = self._buff5_interval_control.minimum_var
            self._buff5_interval_label = self._buff5_interval_control.min_label
            ttk.Label(buff5_row, text="每隔").pack(side="left", padx=(8, 2))
            self._buff5_interval_control.pack(side="left")
            buff6_row = ttk.Frame(drug_right)
            buff6_row.pack(fill="x", pady=(2, 0))
            self._buff6_use_var = tk.BooleanVar(value=False)
            buff6_use_button = ttk.Checkbutton(
                buff6_row, text="增益 4", width=6, variable=self._buff6_use_var,
                command=self._drug_on_change,
            )
            buff6_use_button.pack(side="left")
            self._buff6_key_var = tk.StringVar(value="-")
            buff6_key_button = ttk.Button(
                buff6_row, text=self._buff6_key_var.get(), width=10,
                style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    buff6_key_button, self._buff6_key_var,
                    "_buff6_key_previous",
                    lambda: self._drug_on_change(),
                ),
            )
            buff6_key_button.pack(side="left", padx=(0, 4))
            self._buff6_key_button = buff6_key_button
            self._attach_bind_hint(buff6_key_button)
            self._buff6_interval_control = NumericValueRangeControl(
                buff6_row, tk_module=tk, ttk_module=ttk,
                minimum=600, range_mode=False, value_type=int, step=1,
                lower_bound=5, upper_bound=600, unit="s", value_width=5,
                extra_width_px=12,
                before_change=lambda: self._undo_capture_callback("增益4间隔"),
                on_change=self._drug_on_change,
            )
            self._buff6_interval_var = self._buff6_interval_control.minimum_var
            self._buff6_interval_label = self._buff6_interval_control.min_label
            ttk.Label(buff6_row, text="每隔").pack(side="left", padx=(8, 2))
            self._buff6_interval_control.pack(side="left")
            # Restore previously saved drug settings and apply them live.
            self._drug_load_settings()

            # Persistent clipboard shortcuts. Rows are edited directly by a
            # left click; the adjacent × removes the row with one left click.
            # Column 0 sequence: patrol calibration → attack mode → quick
            # messages.  Keeping this utility beside the patrol controls
            # makes the two columns visually balanced.
            quick_panel = ttk.LabelFrame(col1, text="快捷消息", padding=8)
            quick_panel.pack(fill="x", pady=(0, 8))
            quick_header = ttk.Frame(quick_panel)
            quick_header.pack(fill="x")
            ttk.Button(
                quick_header, text="添加快捷消息",
                command=self._quick_message_add,
            ).pack(side="left")
            self._quick_message_status = ttk.Label(
                quick_header, text="左键修改；左键 × 删除；Ctrl+数字发送。",
                justify="left", wraplength=0, anchor="w",
            )
            self._quick_message_status.pack(
                side="left", fill="x", expand=True, padx=(8, 0)
            )
            self._quick_messages_frame = ttk.Frame(quick_panel)
            self._quick_messages_frame.pack(fill="x", pady=(6, 0))
            root.bind_all(
                "<Button-1>", self._quick_message_blur_on_outside_click,
                add="+",
            )
            self._render_quick_messages()

            # Additional Functions panel: optional extras, each gated by its
            # own checkbox.  First one: scheduled shutdown - after X hours
            # the game gets Alt+F4, the worker verifies the window is gone,
            # then every worker is stopped.
            extra_panel = ttk.LabelFrame(
                col2, text="附加功能", padding=(8, 6)
            )
            self._extra_panel = extra_panel
            # This is constructed after the drug rows for code locality, but
            # packed ahead of them: column 1 reads 附加功能 → 药品 → 运行日志.
            extra_panel.pack(fill="x", pady=(0, 8), before=drug_panel)
            shutdown_row = ttk.Frame(extra_panel)
            if self._SHOW_SHUTDOWN_PANEL:
                shutdown_row.pack(fill="x")
            self._shutdown_enabled_var = tk.BooleanVar(value=False)
            shutdown_check = ttk.Checkbutton(
                shutdown_row, text="运行后定时关闭",
                variable=self._shutdown_enabled_var,
                command=self._shutdown_on_change,
            )
            shutdown_check.pack(side="left", padx=(0, 8))
            self._shutdown_check = shutdown_check
            # Countdown length: horizontal slider (progress-bar style),
            # 0.5-12 hours, default 3.
            self._shutdown_hours_var = tk.DoubleVar(value=3.0)
            shutdown_slider = ttk.Scale(
                shutdown_row, from_=0.5, to=12.0, orient="horizontal",
                variable=self._shutdown_hours_var,
                command=self._shutdown_on_change,
            )
            shutdown_slider.pack(side="left", fill="x", expand=True,
                                 padx=(0, 8))
            self._shutdown_slider = shutdown_slider
            self._shutdown_hours_label = ttk.Label(
                shutdown_row, text="3.0h", width=6
            )
            self._shutdown_hours_label.pack(side="left")
            ttk.Label(
                shutdown_row, text="小时后关闭游戏 (Alt+F4) 并停止"
            ).pack(side="left", padx=(8, 0))
            self._shutdown_status = ttk.Label(
                extra_panel,
                text="定时关闭: 未启用 - 游戏继续运行。",
                justify="left",
                wraplength=440,
            )
            if self._SHOW_SHUTDOWN_PANEL:
                self._shutdown_status.pack(anchor="w", pady=(6, 0))

            alarm_row = ttk.Frame(extra_panel)
            alarm_row.pack(
                fill="x", pady=((8 if self._SHOW_SHUTDOWN_PANEL else 0), 0)
            )
            ttk.Label(alarm_row, text="警报:").pack(side="left")

            self._disconnect_alert_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                alarm_row,
                text="掉线",
                variable=self._disconnect_alert_var,
                command=self._shutdown_on_change,
            ).pack(side="left", padx=(4, 6))

            self._lie_alert_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                alarm_row,
                text="测谎",
                variable=self._lie_alert_var,
                command=self._on_lie_alert_change,
            ).pack(side="left", padx=(0, 6))

            self._countdown_enabled_var = tk.BooleanVar(value=False)
            self._countdown_check = ttk.Checkbutton(
                alarm_row, text="循环",
                variable=self._countdown_enabled_var,
                command=self._countdown_on_change,
            )
            self._countdown_check.pack(side="left")
            countdown_hint = HoverTooltip(
                self._countdown_check, "当剩余时间归零的时候进行警报"
            )
            countdown_hint.set_enabled(True)
            self._bind_key_tooltips.append(countdown_hint)

            # 循环 uses the same direct numeric control as the other right
            # column values.  The interval is whole minutes: 1..300m.
            self._countdown_interval_caption = ttk.Label(alarm_row, text="间隔")
            self._countdown_interval_caption.pack(side="left", padx=(2, 0))
            self._countdown_interval_control = NumericValueRangeControl(
                alarm_row, tk_module=tk, ttk_module=ttk,
                minimum=60, range_mode=False, value_type=int, step=1,
                lower_bound=1, upper_bound=300, unit="m", value_width=5,
                extra_width_px=12,
                before_change=lambda: self._undo_capture_callback("倒计时间隔"),
                on_change=self._countdown_on_change,
            )
            self._countdown_interval_var = self._countdown_interval_control.minimum_var
            self._countdown_interval_label = self._countdown_interval_control.min_label
            self._countdown_interval_control.pack(side="left")

            ttk.Label(alarm_row, text="剩余").pack(side="left", padx=(4, 0))
            self._countdown_remaining_var = tk.DoubleVar(value=3600.0)
            # Remaining is deliberately three independently editable values,
            # shown as HH:mm:ss.  Changing one component moves the worker's
            # live deadline; the countdown itself keeps running.
            # All three clock fields reserve the exact same compact two-digit
            # width.  A single physical pixel protects the larger 12pt hover
            # font without adding a third character cell of blank space.
            remaining_clock_width = 2
            remaining_clock_extra_width = 8
            self._countdown_remaining_hour_control = NumericValueRangeControl(
                alarm_row, tk_module=tk, ttk_module=ttk,
                minimum=1, range_mode=False, value_type=int, step=1,
                lower_bound=0, upper_bound=5, unit="",
                value_width=remaining_clock_width,
                extra_width_px=remaining_clock_extra_width,
                before_change=lambda: self._undo_capture_callback("倒计时剩余时间"),
                on_change=self._countdown_remaining_components_changed,
            )
            self._countdown_remaining_minute_control = NumericValueRangeControl(
                alarm_row, tk_module=tk, ttk_module=ttk,
                minimum=0, range_mode=False, value_type=int, step=1,
                lower_bound=0, upper_bound=59, unit="",
                value_width=remaining_clock_width,
                # Two 12pt minute digits need a few more physical pixels
                # than the hour's usual single digit, but not a third blank
                # character cell.
                extra_width_px=10,
                before_change=lambda: self._undo_capture_callback("倒计时剩余时间"),
                on_change=self._countdown_remaining_components_changed,
            )
            self._countdown_remaining_second_control = NumericValueRangeControl(
                alarm_row, tk_module=tk, ttk_module=ttk,
                minimum=0, range_mode=False, value_type=int, step=1,
                lower_bound=0, upper_bound=59, unit="",
                value_width=remaining_clock_width,
                extra_width_px=10,
                before_change=lambda: self._undo_capture_callback("倒计时剩余时间"),
                on_change=self._countdown_remaining_components_changed,
            )
            self._countdown_remaining_hour_control.pack(side="left")
            ttk.Label(alarm_row, text=":").pack(side="left")
            self._countdown_remaining_minute_control.pack(side="left")
            ttk.Label(alarm_row, text=":").pack(side="left")
            self._countdown_remaining_second_control.pack(side="left")
            self._countdown_status = ttk.Label(
                extra_panel,
                text="循环警报: 未启用。",
                justify="left",
                wraplength=440,
            )

            reminder_row = ttk.Frame(extra_panel)
            reminder_row.pack(fill="x", pady=(4, 0))
            ttk.Label(reminder_row, text="提醒:").pack(side="left")
            self._sound_alert_var = tk.BooleanVar(value=True)
            sound_check = ttk.Checkbutton(
                reminder_row,
                text="声音",
                variable=self._sound_alert_var,
                command=self._shutdown_on_change,
            )
            sound_check.pack(side="left", padx=(4, 8))

            self._screen_blink_var = tk.BooleanVar(value=False)
            blink_check = ttk.Checkbutton(
                reminder_row,
                text="闪烁",
                variable=self._screen_blink_var,
                command=self._shutdown_on_change,
            )
            blink_check.pack(side="left", padx=(0, 8))

            self._telegram_enabled_var = tk.BooleanVar(value=False)
            telegram_check = ttk.Checkbutton(
                reminder_row,
                text="消息",
                variable=self._telegram_enabled_var,
                command=self._shutdown_on_change,
            )
            telegram_check.pack(side="left")
            # 钉钉消息: the group-robot webhook, in the same 提醒 row and the
            # same plain button style as the other controls.  The button carries
            # its own state (钉钉消息 / 钉钉已设置); the live status shares the
            # 图层校准与巡逻 hint line instead of owning a row of its own.
            self._dingtalk_webhook = self._load_dingtalk_webhook()
            self._dingtalk_results: queue.Queue[tuple[Any, Any]] = queue.Queue()
            self._dingtalk_note = ""
            self._dingtalk_button = ttk.Button(
                reminder_row,
                text="钉钉消息",
                command=self._dingtalk_change_webhook,
            )
            self._dingtalk_button.pack(side="left", padx=(8, 0))
            # 设备名: the marker every push carries ("MoonEclipse 设备名 掉线 时间…"),
            # so the group can tell two running machines apart.  Same plain
            # button style, and it reports its own state (设备名 / 名称已设置).
            self._dingtalk_name_button = ttk.Button(
                reminder_row,
                text="设备名",
                command=self._edit_dingtalk_name,
            )
            self._dingtalk_name_button.pack(side="left", padx=(8, 0))
            for widget, hint_text in (
                (sound_check, "发出dingdong的声音"),
                (blink_check, "屏幕闪烁红色，适用于没声音的电脑"),
                (telegram_check, "勾选后：掉线、测谎、循环 推送到钉钉群"),
                (self._dingtalk_button, "设置钉钉群机器人的 webhook 链接（点击输入，留空清除）"),
                (self._dingtalk_name_button, "设置推送消息里的设备名称（点击输入，留空清除）"),
            ):
                tooltip = HoverTooltip(widget, hint_text)
                tooltip.set_enabled(True)
                self._bind_key_tooltips.append(tooltip)
            self._refresh_dingtalk_button()
            self._refresh_dingtalk_name_button()
            self._apply_dingtalk_settings()
            self._apply_dingtalk_settings()
            telegram_row = ttk.Frame(extra_panel)
            # 消息推送配置 is intentionally hidden for now.  Keep its widgets
            # instantiated so existing saved settings and notifier callbacks
            # remain valid when this surface is restored.
            self._telegram_machine_row = telegram_row
            telegram_label = ttk.Label(telegram_row, text="消息推送配置")
            telegram_label.pack(side="left")
            self._telegram_machine_var = tk.StringVar(value="")
            self._telegram_machine_button = ttk.Button(
                telegram_row,
                text="设备名称",
                width=14,
                command=self._edit_machine_name,
            )
            self._telegram_machine_button.pack(side="left", padx=(4, 8))
            self._telegram_token_button = ttk.Button(
                telegram_row, text="设置BOT token",
                command=self._telegram_change_token,
            )
            self._telegram_token_button.pack(side="left")
            telegram_setup_hint = (
                "配置bot需要开启网络代理，需要获取telegram bot key，"
                "具体查看右上角教程，学习如何配置"
            )
            for widget in (
                telegram_label,
                self._telegram_machine_button,
                self._telegram_token_button,
            ):
                tooltip = HoverTooltip(widget, telegram_setup_hint)
                tooltip.set_enabled(True)
                self._bind_key_tooltips.append(tooltip)
            self._telegram_status = ttk.Label(
                extra_panel,
                text="",
                justify="left",
                wraplength=440,
            )
            # Keep the status widget as an internal sink for notifier updates,
            # but do not show explanatory hints in the Additional Functions
            # panel.  Operational failures remain in the running log.

            # 自动重连: a selection plus its two settings (which world, which channel).
            # 掉线 (the character detector's event) is only the FIRST sign; the worker
            # confirms it by the login page's BASE COLOUR (screenshots/login_page_target.jpg
            # is the colour reference).  Ticking the box only arms the worker - the drill
            # starts on a 掉线 event or on the temporary 测试重连 button below.
            saved_reconnect_enabled, saved_world, saved_channel = (
                self._load_reconnect_settings()
            )
            saved_api_auto_lie = self._load_api_auto_lie_setting()
            saved_api_auto_lie_backend = self._load_api_auto_lie_backend_setting()
            AUTO_LIE_LOG.info(
                "自动过测谎: selection loaded as %s (backend=%s)",
                "ON" if saved_api_auto_lie else "OFF",
                saved_api_auto_lie_backend,
            )
            # Keep cleanup, lie, and reconnect as separate selections.  清理内存
            # only hands the logged-out client to 自动重连; it must never
            # silently alter the reconnect selection itself.
            # 自动测谎 owns a line of its own (operator: "自动测谎 take a line, 自动重连
            # put to a new line following 自动测谎") - the label, three radios and
            # 测试API do not fit beside the reconnect settings.
            auto_lie_row = ttk.Frame(extra_panel)
            auto_lie_row.pack(fill="x", pady=(4, 0))
            reconnect_row = ttk.Frame(extra_panel)
            reconnect_row.pack(fill="x", pady=(4, 0))
            # 自动测谎 is a grouped selection now, not a checkbox: the lie pass has
            # two backends and the old on/off switch alone could not say which.
            #   关闭      - the feature is off (the old unticked checkbox)
            #   云端测谎  - the vendor ROI-track service (everything as before)
            #   本地测谎  - the CT partner API on 127.0.0.1:8765
            # ``_api_auto_lie_var`` stays the armed boolean every existing code
            # path reads; the radios are its author.
            self._api_auto_lie_var = tk.BooleanVar(value=saved_api_auto_lie)
            self._api_auto_lie_backend_var = tk.StringVar(
                value=("off" if not saved_api_auto_lie else saved_api_auto_lie_backend)
            )
            auto_lie_label = ttk.Label(auto_lie_row, text="自动测谎")
            auto_lie_label.pack(side="left")
            # Patrol sends global key input.  If a temporary game stall leaves
            # the dashboard foreground, Space/Enter must not silently change
            # this security-sensitive selection.  It remains a mouse control.
            self._api_auto_lie_buttons: list[Any] = []
            for value, label in (
                ("off", "关闭"),
                ("cloud", "云端测谎"),
                ("local", "本地测谎"),
            ):
                button = ttk.Radiobutton(
                    auto_lie_row,
                    text=label,
                    value=value,
                    variable=self._api_auto_lie_backend_var,
                    command=self._api_auto_lie_backend_changed,
                    takefocus=False,
                )
                button.bind(
                    "<ButtonPress-1>", self._mark_auto_lie_mouse_change, add="+"
                )
                button.bind(
                    "<space>", self._block_auto_lie_keyboard_toggle, add="+"
                )
                button.bind(
                    "<Return>", self._block_auto_lie_keyboard_toggle, add="+"
                )
                button.pack(side="left")
                self._api_auto_lie_buttons.append(button)
                if value == "off":
                    continue
                hint = HoverTooltip(
                    button,
                    "开启后每测一次扣一次次数；期间自动接管鼠标，你不用操作，"
                    "按 Esc 可以中断（仍然扣费）。想自己操作请先关闭"
                    if value == "cloud" else
                    "由本机测谎服务识别，使用测谎卡计次，不扣在线测谎次数；"
                    "选好后可以先点 测试本地测谎 试跑一次",
                )
                hint.set_enabled(True)
                self._bind_key_tooltips.append(hint)
            # 激活测谎 is gone on purpose: the activation code now carries its
            # ct_card, the licence server returns it in the device document and
            # local_lie_service.apply_online_card() writes it, so there is nothing
            # for the operator to type.
            # The old attribute name is read by the tooltip refresh path and by
            # the tests that assert the selection is a mouse-only control.
            self._api_auto_lie_check = self._api_auto_lie_buttons[1]
            # Pre-start the local service AT LAUNCH when 本地测谎 is the saved
            # backend: it must already be listening long before a lie window can
            # appear (a cold start takes seconds, a pass cannot wait for it).
            if saved_api_auto_lie and saved_api_auto_lie_backend == AUTOLIE_BACKEND_LOCAL:
                try:
                    import local_lie_service

                    local_lie_service.supervisor_set_active(
                        True, application_root(__file__)
                    )
                except Exception:  # noqa: BLE001 - a missing folder is not fatal
                    LOG.debug("本地测谎 pre-start could not be armed", exc_info=True)
            # 测试本地测谎 and 测试云端测谎 live in ONE slot: a drill always runs on
            # whichever backend the group selects, so at most one of the two can be
            # reachable.  The slot frame is frozen at the wider label, so showing or
            # hiding the button never reflows this row (operator: "share the same
            # place, don't make UI expand").  测试云端测谎 is only built, never packed,
            # while API_TEST_CLOUD_BUTTON_VISIBLE is false.
            self._api_test_slot = ttk.Frame(auto_lie_row)
            self._api_test_slot.pack(side="left", padx=(10, 0))
            self._api_test_local_button = ttk.Button(
                self._api_test_slot,
                text="测试本地测谎",
                command=self._api_test_clicked,
                takefocus=False,
            )
            self._api_test_cloud_button = ttk.Button(
                self._api_test_slot,
                text="测试云端测谎",
                command=self._api_test_clicked,
                takefocus=False,
            )
            self._api_test_arm_state = "normal"
            self._api_test_slot.pack_propagate(False)
            try:
                self._api_test_slot.configure(
                    width=max(
                        self._api_test_local_button.winfo_reqwidth(),
                        self._api_test_cloud_button.winfo_reqwidth(),
                    ),
                    height=max(
                        self._api_test_local_button.winfo_reqheight(),
                        self._api_test_cloud_button.winfo_reqheight(),
                    ),
                )
            except Exception:  # noqa: BLE001 - a sizing hint is never fatal
                LOG.debug("api test slot could not be sized", exc_info=True)
            local_test_hint = HoverTooltip(
                self._api_test_local_button,
                "用一段录好的视频试跑一次，看看能不能正常识别；不扣在线测谎次数",
            )
            local_test_hint.set_enabled(True)
            self._bind_key_tooltips.append(local_test_hint)
            cloud_test_hint = HoverTooltip(
                self._api_test_cloud_button,
                "用视频试跑一次，会扣在线测谎次数",
            )
            cloud_test_hint.set_enabled(True)
            self._bind_key_tooltips.append(cloud_test_hint)
            # ``_api_test_button`` stays the name the state helpers and the tests
            # read; both real buttons are driven together by _api_test_set_buttons.
            self._api_test_button = self._api_test_cloud_button
            self._sync_api_test_buttons()
            self._reconnect_var = tk.BooleanVar(value=saved_reconnect_enabled)
            self._reconnect_check = ttk.Checkbutton(
                reconnect_row,
                text="自动重连",
                variable=self._reconnect_var,
                command=self._reconnect_on_change,
            )
            self._reconnect_check.pack(side="left")
            reconnect_hint = HoverTooltip(
                self._reconnect_check, "掉线的时候自动重连游戏"
            )
            reconnect_hint.set_enabled(True)
            self._bind_key_tooltips.append(reconnect_hint)
            # The selection is part of user_config: it is persisted the moment the operator leaves the
            # widget, not only when the whole panel writes its configuration (his request).
            self._reconnect_check.bind(
                "<FocusOut>", lambda _event: self._reconnect_on_change()
            )
            self._reconnect_world_var = tk.StringVar(value=saved_world)
            self._reconnect_world_box = ttk.Combobox(
                reconnect_row,
                textvariable=self._reconnect_world_var,
                values=list(WORLD_NAMES),
                state="readonly",
                width=7,
            )
            self._reconnect_world_box.pack(side="left", padx=(6, 4))
            self._reconnect_world_box.bind(
                "<<ComboboxSelected>>", lambda _event: self._reconnect_on_change()
            )
            # A value changed with the keyboard fires no selection event, so leaving the box must save
            # it as well - this is the operator's "saved when the widget blur" for the world.
            self._reconnect_world_box.bind(
                "<FocusOut>", lambda _event: self._reconnect_on_change()
            )
            ttk.Label(reconnect_row, text="频道").pack(side="left")
            self._reconnect_channel_control = NumericValueRangeControl(
                reconnect_row,
                tk_module=tk,
                ttk_module=ttk,
                minimum=saved_channel,
                range_mode=False,
                value_type=int,
                step=1,
                lower_bound=CHANNEL_MIN,
                upper_bound=CHANNEL_MAX,
                unit="",
                value_width=3,
                before_change=lambda: self._undo_capture_callback("自动重连频道"),
                on_change=self._reconnect_on_change,
            )
            self._reconnect_channel_var = self._reconnect_channel_control.minimum_var
            self._reconnect_channel_control.pack(side="left", padx=(4, 0))
            self._reconnect_message_var = tk.StringVar(value="")
            self._reconnect_message_button = ttk.Button(
                reconnect_row, text="重连消息",
                command=lambda: self._edit_workflow_message(
                    "reconnect", "重连消息", self._reconnect_message_var
                ),
                takefocus=False,
            )
            self._reconnect_message_button.pack(side="left", padx=(6, 0))
            # The video drill remains available to internal callers, but the
            # operator-facing panel keeps this diagnostic control hidden.
            restart_row = ttk.Frame(extra_panel)
            restart_row.pack(fill="x", pady=(4, 0))
            self._auto_restart_var = tk.BooleanVar(value=False)
            self._auto_restart_check = ttk.Checkbutton(
                restart_row,
                text="清理内存",
                variable=self._auto_restart_var,
                command=self._shutdown_on_change,
            )
            restart_hint = HoverTooltip(
                self._auto_restart_check,
                "内存达到98%以后，主动发送下线消息 （未配置不发送），然后重连游戏",
            )
            restart_hint.set_enabled(True)
            self._bind_key_tooltips.append(restart_hint)
            self._restart_offline_message_var = tk.StringVar(value="")
            self._restart_offline_message_button = ttk.Button(
                restart_row, text="下线消息",
                command=lambda: self._edit_workflow_message(
                    "restart_offline", "下线消息", self._restart_offline_message_var
                ),
                takefocus=False,
            )
            self._auto_restart_check.pack(side="left")
            self._restart_offline_message_button.pack(side="left", padx=(6, 0))
            # 定时下机: the same 清理内存 log-off, on a clock instead of on memory.
            # It reuses the 循环 numeric control (NumericValueRangeControl) as
            # three HH : mm : ss fields capped at 12:00:00, and when the time is
            # reached it asks the auto-restart worker for its normal lifecycle
            # (下线消息 -> Esc -> 下线 -> 重连), exactly like the memory trigger.
            self._timed_logout_var = tk.BooleanVar(value=False)
            timed_logout_check = ttk.Checkbutton(
                restart_row,
                text="定时下机",
                variable=self._timed_logout_var,
                command=self._timed_logout_on_change,
            )
            timed_logout_check.pack(side="left", padx=(12, 0))
            timed_clock_width = 2
            timed_clock_extra = (8, 10, 10)
            self._timed_logout_hour_control = NumericValueRangeControl(
                restart_row, tk_module=tk, ttk_module=ttk,
                minimum=0, range_mode=False, value_type=int, step=1,
                lower_bound=0, upper_bound=self.TIMED_LOGOUT_MAX_HOURS, unit="",
                value_width=timed_clock_width,
                extra_width_px=timed_clock_extra[0],
                before_change=lambda: self._undo_capture_callback("定时下机"),
                on_change=self._timed_logout_on_change,
            )
            self._timed_logout_minute_control = NumericValueRangeControl(
                restart_row, tk_module=tk, ttk_module=ttk,
                minimum=0, range_mode=False, value_type=int, step=1,
                lower_bound=0, upper_bound=59, unit="",
                value_width=timed_clock_width,
                extra_width_px=timed_clock_extra[1],
                before_change=lambda: self._undo_capture_callback("定时下机"),
                on_change=self._timed_logout_on_change,
            )
            self._timed_logout_second_control = NumericValueRangeControl(
                restart_row, tk_module=tk, ttk_module=ttk,
                minimum=0, range_mode=False, value_type=int, step=1,
                lower_bound=0, upper_bound=59, unit="",
                value_width=timed_clock_width,
                extra_width_px=timed_clock_extra[2],
                before_change=lambda: self._undo_capture_callback("定时下机"),
                on_change=self._timed_logout_on_change,
            )
            self._timed_logout_hour_control.pack(side="left", padx=(6, 0))
            ttk.Label(restart_row, text=":").pack(side="left")
            self._timed_logout_minute_control.pack(side="left")
            ttk.Label(restart_row, text=":").pack(side="left")
            self._timed_logout_second_control.pack(side="left")
            timed_logout_hint = HoverTooltip(
                timed_logout_check,
                "到时间后下线",
            )
            timed_logout_hint.set_enabled(True)
            self._bind_key_tooltips.append(timed_logout_hint)
            self._timed_logout_deadline: Optional[float] = None
            self._timed_logout_deadline: Optional[float] = None
            self._timed_logout_note = ""
            # Retain invisible status sinks for workflow callbacks.  The
            # Additional Functions panel intentionally shows controls only;
            # progress and failures are reported in the running log instead.
            self._reconnect_status = ttk.Label(
                extra_panel,
                text="",
                justify="left",
                wraplength=440,
            )
            # The invisible status sink is shared by the automatic lie pass and
            # the manual 测试API video drill; details stay in the running log.
            self._api_test_status = ttk.Label(
                extra_panel, text="",
                justify="left", wraplength=440,
            )
            if saved_reconnect_enabled:
                # apply the saved values to the worker once it exists (Tk's own timer:
                # the UiWorker is not a Tk object)
                self._root.after(200, self._reconnect_on_change)

            # Other-player safety net is intentionally the final row. It is
            # a selection (enable/disable), not a manual trigger button.
            player_row = ttk.Frame(extra_panel)
            player_row.pack(fill="x", pady=(4, 0))
            self._player_check_var = tk.BooleanVar(value=False)
            player_check_button = ttk.Checkbutton(
                player_row,
                text="有人换线",
                variable=self._player_check_var,
                command=self._shutdown_on_change,
            )
            player_check_button.pack(side="left")
            self._player_check_hint = HoverTooltip(
                player_check_button,
                "",
                wraplength=_HOVER_TOOLTIP_MAX_WIDTH,
            )
            self._player_check_hint.set_enabled(True)
            self._bind_key_tooltips.append(self._player_check_hint)
            self._player_request_message_var = tk.StringVar(value="")
            self._player_request_message_button = ttk.Button(
                player_row, text="求让消息",
                command=lambda: self._edit_workflow_message(
                    "player_request", "求让消息", self._player_request_message_var
                ),
                takefocus=False,
            )
            self._player_request_message_button.pack(side="left", padx=(6, 0))
            self._player_room_code_var = tk.StringVar(value="")
            self._player_room_code_button = ttk.Button(
                player_row, text="房间码",
                command=self._edit_player_room_code,
                takefocus=False,
            )
            self._player_room_code_button.pack(side="left", padx=(6, 0))
            player_wait_label = ttk.Label(player_row, text="多等")
            player_wait_label.pack(side="left", padx=(6, 0))
            self._player_channel_wait_control = NumericValueRangeControl(
                player_row,
                tk_module=tk,
                ttk_module=ttk,
                minimum=300,
                range_mode=False,
                value_type=int,
                step=1,
                lower_bound=1,
                upper_bound=1200,
                unit="秒",
                value_width=5,
                # ``1200秒`` contains a wide CJK suffix; reserve its final
                # pixels inside the fixed-width numeric component.
                extra_width_px=12,
                before_change=lambda: self._undo_capture_callback("换线等待时间"),
                on_change=self._player_channel_wait_commit,
            )
            self._player_channel_wait_var = self._player_channel_wait_control.minimum_var
            self._player_channel_wait_control.pack(side="left", padx=(3, 0))
            self._player_wait_tooltips = []
            for widget in (
                player_wait_label,
                self._player_channel_wait_control.min_label,
            ):
                tooltip = HoverTooltip(
                    widget, "", wraplength=_HOVER_TOOLTIP_MAX_WIDTH
                )
                tooltip.set_enabled(True)
                self._bind_key_tooltips.append(tooltip)
                self._player_wait_tooltips.append(tooltip)
            self._refresh_player_wait_hint()

            # 自动过测谎 (the local Cutie/YOLO lie pass) is gone: lie detection is done by the
            # remote RoiTrack service through 测试api (see api_lie_video.py), so neither the
            # checkbox, the CPU/CUDA testers nor the demo player exist any more.
            self._shutdown_load_settings()

            # Minimap / map-name preview widgets: built but hidden by default
            # (kept for future use - flip _SHOW_MINIMAP_PREVIEW to show).
            if self._SHOW_MINIMAP_PREVIEW:
                ttk.Label(col1, text="检测到的小地图").pack(anchor="w")
                self._minimap_label = ttk.Label(col1)
                self._minimap_label.pack(anchor="w", pady=(4, 10))
                ttk.Label(col1, text="地图名称区域").pack(anchor="w")
                self._map_name_label = ttk.Label(col1)
                self._map_name_label.pack(anchor="w", pady=(4, 0))

            # 运行日志 panel: a real LabelFrame panel (title + outline) like
            # the other panels.  Messages inside keep the plain hint style
            # of the 图层校准与巡逻 status hints; the report controls sit at
            # the panel's top-left and appear once patrol has ended (report
            # time): the archive button copies the running log, the user
            # button copies user settings.  Only significant events are
            # shown (the latest few lines); the full 500-line in-memory
            # history stays available through the archive button - no disk
            # I/O per line.  Import/export have text buttons so their purpose
            # remains clear without relying on an unlabeled icon.
            log_panel = ttk.LabelFrame(col2, text="运行日志", padding=(6, 4))
            self._log_panel = log_panel
            log_panel.pack(fill="x", pady=(10, 0))
            log_actions = ttk.Frame(log_panel)
            log_actions.pack(fill="x")
            self._log_archive_photo = _make_log_icon("archive", root)
            self._copy_log_button = ttk.Button(
                log_actions,
                image=self._log_archive_photo,
                command=self._copy_running_log,
                takefocus=False,
            )
            running_log_hint = HoverTooltip(self._copy_log_button, "运行日志")
            running_log_hint.set_enabled(True)
            self._bind_key_tooltips.append(running_log_hint)
            self._import_config_button = ttk.Button(
                log_actions, text="导入配置",
                command=self._import_user_config,
                takefocus=False,
            )
            import_config_hint = HoverTooltip(
                self._import_config_button, "导入后会刷新当前助手并停止巡逻"
            )
            import_config_hint.set_enabled(True)
            self._bind_key_tooltips.append(import_config_hint)
            self._export_config_button = ttk.Button(
                log_actions, text="导出配置",
                command=self._export_user_config,
                takefocus=False,
            )
            self._copy_log_button.pack(side="left", padx=(0, 3))
            self._log_server_photo = _make_log_icon("server", root)
            self._copy_server_log_button = ttk.Button(
                log_actions,
                image=self._log_server_photo,
                command=self._copy_server_client_log,
                takefocus=False,
            )
            server_log_hint = HoverTooltip(self._copy_server_log_button, "授权日志")
            server_log_hint.set_enabled(True)
            self._bind_key_tooltips.append(server_log_hint)
            self._copy_server_log_button.pack(side="left", padx=(0, 3))
            self._log_auto_lie_photo = _make_log_icon("auto_lie", root)
            self._copy_auto_lie_log_button = ttk.Button(
                log_actions,
                image=self._log_auto_lie_photo,
                command=self._copy_auto_lie_log,
                takefocus=False,
            )
            auto_lie_log_hint = HoverTooltip(self._copy_auto_lie_log_button, "自动测谎日志")
            auto_lie_log_hint.set_enabled(True)
            self._bind_key_tooltips.append(auto_lie_log_hint)
            self._copy_auto_lie_log_button.pack(side="left", padx=(0, 3))
            self._import_config_button.pack(side="left", padx=(0, 3))
            self._export_config_button.pack(side="left")
            self._log_display_lines: list[str] = []
            self._log_label = ttk.Label(
                log_panel,
                text="",
                justify="left",
                anchor="w",
                wraplength=430,
            )
            self._log_label.pack(fill="x", anchor="w", pady=(3, 0))

            # A missing/expired license never prevents the dashboard from
            # opening.  It simply leaves every product control grey while the
            # activation button above remains available.
            self._set_license_visual_lock(not self._license_allowed())

            # Pin the window to the configured initial geometry AFTER all
            # content has been built: pack propagation from the panels would
            # otherwise let the content spread the window open taller than
            # the requested initial size on the very first display.
            root.update_idletasks()
            locked_widths = self._freeze_column_widths()
            if locked_widths is not None:
                # 12px between the columns plus the container's 24px side
                # inset.  Reserve it before mapping the root so the measured
                # stationary row cannot be clipped by the previous geometry.
                required_width = sum(locked_widths) + 36
                # Recalculate the initial width from the compact column
                # tracks instead of preserving a large geometry saved by a
                # prior slider-heavy release.  The minimum remains the same
                # safe measured width, so no panel is clipped.
                width = max(_INITIAL_WINDOW_WIDTH, required_width)
                root.minsize(
                    max(_INITIAL_WINDOW_WIDTH, required_width),
                    500 + (_CAPTION_HEIGHT if caption_installed else 0),
                )
                clamped = (
                    f"{width}x{root.winfo_height()}+{root.winfo_x()}+{root.winfo_y()}"
                )
            root.geometry(clamped)
            # Fit the first displayed window to the taller column.  A
            # previous release may have saved a manually enlarged height;
            # keeping that stale value created a large empty area below the
            # panels.  This runs while the root is still hidden, so the user
            # sees only the compact finished layout.
            self._refit_window_to_content()
            root.deiconify()
            # A few saved-setting callbacks are deliberately scheduled during
            # startup.  They apply initial values only and must not become
            # fake Ctrl+Z steps.
            root.after(500, self._enable_undo_history)

            # Mapping the dashboard can make Windows give it foreground even
            # though the assistant has not been started.  Return foreground to
            # the selected game once, without arming automation or injecting a
            # key, so the operator's ordinary keyboard input is usable as soon
            # as the helper finishes opening.
            root.after(120, self._return_game_focus_after_startup)

            self._start_license_heartbeat()
            root.after(0, self._poll)
            root.mainloop()
        except Exception:
            record_current_exception("Tk UI worker exception")
            LOG.exception("debug UI stopped unexpectedly")
        finally:
            watchdog = getattr(self, "_ui_hang_watchdog", None)
            if watchdog is not None:
                watchdog.close()
            self._yolo_stop()
            self._root = None
            LOG.info("UI worker stopped")

    def _return_game_focus_after_startup(self) -> None:
        """Give the game back foreground after the dashboard's first map."""

        sender = getattr(getattr(self, "status_worker", None), "key_sender", None)
        select_window = getattr(sender, "select_window", None)
        if not callable(select_window):
            return
        try:
            select_window()
        except Exception:
            # The game may not be running yet.  Startup must remain usable for
            # configuration in that case.
            LOG.debug("startup game-focus restore skipped", exc_info=True)

    def _install_custom_caption(self, root: Any, tk: Any) -> bool:
        """Hide the native caption so an in-window one can host the ? button.

        Only the caption strip (WS_CAPTION) is removed; the native thick
        resize border, min/max flags, system menu and taskbar entry are all
        kept, so the window still resizes, snaps and minimizes like before.
        Returns False (keep the native caption) on any failure.
        """

        try:
            if sys.platform != "win32":
                return False
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            root.update_idletasks()
            hwnd = int(user32.GetParent(root.winfo_id()))
            if not hwnd:
                hwnd = int(root.winfo_id())
            if not hwnd:
                return False
            GWL_STYLE = -16
            WS_CAPTION = 0x00C00000
            style = user32.GetWindowLongW(hwnd, GWL_STYLE)
            if not style:
                return False
            user32.SetWindowLongW(hwnd, GWL_STYLE, style & ~WS_CAPTION)
            # Refresh the frame so the caption disappears immediately.
            SWP_FRAMECHANGED = 0x0020
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOZORDER = 0x0004
            user32.SetWindowPos(
                hwnd, None, 0, 0, 0, 0,
                SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER,
            )
            self._caption_hwnd = hwnd
            return True
        except Exception:
            LOG.warning("custom caption unavailable; using native caption",
                        exc_info=True)
            return False

    def _build_caption_bar(
        self, root: Any, tk: Any, app_version: str
    ) -> None:
        """Draw the in-window caption row: title + ？ － □ × buttons."""

        bar = tk.Frame(root, bg="#f0f0f0", height=_CAPTION_HEIGHT)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self._caption_bar = bar

        # A thin bottom line visually separates the caption row from the
        # application content below it.
        separator = tk.Frame(root, bg="#c8c8c8", height=1)
        separator.pack(fill="x")
        self._caption_separator = separator

        title = tk.Label(
            bar, text=f"MoonEclipse {app_version}", bg="#f0f0f0",
            anchor="w",
        )
        title.pack(side="left", padx=(10, 0))
        self._caption_title = title
        # Restored verbatim after a download progress line (see
        # ``_set_release_progress``), so the caption never keeps a stale status.
        self._caption_title_base = f"MoonEclipse {app_version}"

        def _button(
            text: str, command: Any, image: Any = None, width: int = 3
        ) -> Any:
            button = tk.Button(
                bar, text=text, command=command, relief="flat",
                bd=0, bg="#f0f0f0", activebackground="#dcdcdc",
                width=width, height=1, padx=0, pady=0, cursor="hand2",
            )
            if image is not None:
                button.configure(image=image, compound="center")
            button.pack(side="right", fill="y")
            _bind_caption_hover(button)
            return button

        def _bind_caption_hover(widget: Any) -> None:
            """Give every caption action the book icon's visible hover shade."""

            widget.bind(
                "<Enter>",
                lambda _event: widget.configure(bg="#dcdcdc"),
                add="+",
            )
            widget.bind(
                "<Leave>",
                lambda _event: widget.configure(bg="#f0f0f0"),
                add="+",
            )

        self._caption_close_button = _button("×", self._on_debug_window_close)
        self._caption_max_button = _button("□", self._caption_toggle_maximize)
        self._caption_min_button = _button("－", self._caption_minimize)
        self._help_button = _button("?", None)
        # Use a canvas instead of a PhotoImage inside a one-line Button.
        # Button image bounds are clipped inconsistently by Windows DPI
        # scaling; a canvas gives the teaching book its full 30px slot.
        self._teaching_button = tk.Canvas(
            bar, width=30, height=_CAPTION_HEIGHT,
            bg="#f0f0f0", highlightthickness=0, cursor="hand2",
        )
        self._teaching_button.pack(side="right", fill="y")
        book_ink = "#2d465f"
        self._teaching_button.create_rectangle(
            8, 8, 23, 25, fill="#d8e8f4", outline=book_ink, width=2
        )
        self._teaching_button.create_line(12, 9, 12, 24, fill=book_ink, width=2)
        self._teaching_button.create_line(15, 13, 21, 13, fill=book_ink)
        self._teaching_button.create_line(15, 17, 21, 17, fill=book_ink)
        self._teaching_button.bind(
            "<Button-1>", lambda _event: self._show_teaching_viewer()
        )
        _bind_caption_hover(self._teaching_button)
        self._teaching_button_tooltip = HoverTooltip(
            self._teaching_button, "教学"
        )
        self._teaching_button_tooltip.set_enabled(True)
        # ↻ is a status refresh, not a package hunt: the operator asked for an
        # immediate heartbeat with the activation server so the header's
        # 剩余 / 总 / 成功 / 失败 / 昨日 numbers and the 自动过测谎 gate are current.
        # It greys itself for LICENSE_REFRESH_COOLDOWN_SECONDS afterwards.
        self._update_button = _button("↻", self._request_license_heartbeat)
        # Keep the hint available while the button is greyed by its cooldown:
        # that is exactly when the operator wonders what the ↻ glyph does.
        self._update_button_tooltip = HoverTooltip(
            self._update_button, "刷新测谎次数"
        )
        self._update_button_tooltip.set_enabled(True)
        # The release icon sits beside ↻ and is the only control that changes
        # colour: green means "a newer version was announced by the server".
        # Hover shows the release notes, a click downloads and installs.
        self._release_button = tk.Canvas(
            bar, width=30, height=_CAPTION_HEIGHT,
            bg="#f0f0f0", highlightthickness=0, cursor="hand2",
        )
        self._release_button.bind(
            "<Button-1>", lambda _event: self._start_release_update()
        )
        _bind_caption_hover(self._release_button)
        self._release_button_tooltip = HoverTooltip(
            self._release_button, "已是最新版本"
        )
        self._release_button_tooltip.set_enabled(True)
        # ``side=right`` adds each later child to the left.  Re-pack these two
        # controls as refresh then teaching so their visible order is book,
        # refresh, help, minimize, maximize, close.
        self._teaching_button.pack_forget()
        self._update_button.pack_forget()
        self._update_button.pack(side="right", fill="y")
        self._release_button.pack(side="right", fill="y")
        self._teaching_button.pack(side="right", fill="y")
        self._paint_release_icon()
        # Hover-triggered help: show on mouse-enter, disappear on mouse-leave
        # (the popup is floating and never takes keyboard focus).
        # Keep the caption hover shade registered by ``_button`` while also
        # showing/hiding the binding-table popup.
        self._help_button.bind("<Enter>", self._help_hover_show, add="+")
        self._help_button.bind("<Leave>", self._help_hover_leave, add="+")

        # Dragging any empty part of the bar moves the window (manual
        # geometry drag: after WS_CAPTION is removed the OS caption hit-test
        # is gone, so native HTCAPTION dragging would not move the window).
        # A cheap outline follows the cursor; the real window is only
        # repositioned once on release so Tk is not relaid out + repainted on
        # every mouse move (that made dragging/resizing laggy).
        def _press(event: Any) -> None:
            self._caption_drag_begin(event)

        def _motion(event: Any) -> None:
            self._caption_drag_move(event)

        def _release(_event: Any) -> None:
            self._caption_drag_end()

        for widget in (bar, title):
            widget.bind("<ButtonPress-1>", _press)
            widget.bind("<B1-Motion>", _motion)
            widget.bind("<ButtonRelease-1>", _release)

    @staticmethod
    def _teaching_asset_paths() -> list[Path]:
        """Return packaged teaching images without preloading them.

        ``teach-assets`` is a packaged data folder, so source and installed
        copies resolve the same relative location.
        """

        root = application_root(__file__)
        for folder in (root / "teach-assets",):
            if not folder.is_dir():
                continue
            images = [
                path for path in folder.iterdir()
                if path.is_file() and path.suffix.casefold() in {
                    ".jpg", ".jpeg", ".png", ".bmp", ".webp",
                }
            ]
            if images:
                def natural_key(path: Path) -> list[object]:
                    return [
                        int(piece) if piece.isdigit() else piece.casefold()
                        for piece in re.split(r"(\d+)", path.name)
                    ]
                return sorted(images, key=natural_key)
        return []

    def _show_teaching_viewer(self) -> None:
        """Open a small on-demand image viewer for packaged teaching pages."""

        root = getattr(self, "_root", None)
        if root is None:
            return
        existing = getattr(self, "_teaching_viewer", None)
        try:
            if existing is not None and existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                return
        except Exception:
            pass

        pages = self._teaching_asset_paths()
        if not pages:
            try:
                from tkinter import messagebox
                messagebox.showinfo(
                    "教学", "教学图片缺失，请重新安装助手",
                    parent=root,
                )
            except Exception:
                LOG.warning("teaching images are unavailable")
            return

        import tkinter as tk

        viewer = tk.Toplevel(root)
        viewer.title("MoonEclipse 教学")
        viewer.transient(root)
        viewer.resizable(False, False)
        self._teaching_viewer = viewer
        state = {"index": 0, "photo": None}
        image_label = tk.Label(viewer, background="#f0f0f0")
        image_label.pack(padx=8, pady=(8, 4))
        footer = tk.Frame(viewer)
        footer.pack(fill="x", padx=8, pady=(0, 8))
        previous = tk.Button(footer, text="上一张")
        next_page = tk.Button(footer, text="下一张")
        page_label = tk.Label(footer, anchor="center")
        previous.pack(side="left")
        page_label.pack(side="left", fill="x", expand=True, padx=8)
        next_page.pack(side="right")

        def render(index: int) -> None:
            index = max(0, min(len(pages) - 1, index))
            state["index"] = index
            path = pages[index]
            try:
                with Image.open(path) as source:
                    image = source.convert("RGB")
                # Keep the viewer lightweight: only the visible page is in
                # memory and oversized documentation images are downscaled.
                image.thumbnail((900, 640), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image, master=viewer)
                state["photo"] = photo
                image_label.configure(image=photo, text="")
                page_label.configure(text=f"{index + 1} / {len(pages)}")
                previous.configure(state="normal" if index else "disabled")
                next_page.configure(
                    state="normal" if index < len(pages) - 1 else "disabled"
                )
            except Exception as exc:
                image_label.configure(image="", text=f"无法读取教学图片：{path.name}")
                page_label.configure(text=f"{index + 1} / {len(pages)}")
                LOG.warning("could not open teaching image %s: %s", path, exc)

        previous.configure(command=lambda: render(state["index"] - 1))
        next_page.configure(command=lambda: render(state["index"] + 1))
        viewer.bind("<Left>", lambda _event: render(state["index"] - 1))
        viewer.bind("<Right>", lambda _event: render(state["index"] + 1))
        viewer.bind("<Escape>", lambda _event: viewer.destroy())
        viewer.protocol("WM_DELETE_WINDOW", viewer.destroy)
        render(0)

    def _schedule_restart(self) -> bool:
        """Start the hidden restart helper; False when it could not start.

        Shared by 更新 (a new package was applied) and 导入配置 (the imported
        settings are only read at startup).  The helper waits for this process
        to exit and then relaunches through the package launcher.
        """

        try:
            schedule_hidden_restart(application_root(__file__))
        except UpdateError as exc:
            LOG.warning("自动重启失败：%s", exc)
            return False
        return True

    def _close_for_restart(self) -> None:
        """Stop live input and close this instance for the scheduled restart."""

        # Stop live input before the helper starts a clean elevated instance.
        stopped = self._stop_patrol()
        if not stopped:
            # Headless/minimal dashboards may not have a route controller;
            # there is then no cleanup thread to wait for before copying.
            self._patrol_stop_cleanup_done.set()
        root = getattr(self, "_root", None)
        if root is not None:
            root.after(250, self._on_debug_window_close)

    def _check_desktop_update(self) -> None:
        """Find and apply the highest newer nearby/Desktop release on request.

        The caption ↻ button no longer calls this - it forces a status
        heartbeat instead, because the operator updates packages by hand.  The
        workflow is kept intact so an explicit update action can be wired to it
        again without rewriting anything.
        """

        install_root = application_root(__file__)
        current = read_version(install_root / "VERSION")
        LOG.info(
            "更新：正在桌面、当前目录及上级目录查找比 v%s 更新的 MoonEclipse 安装包。",
            current,
        )
        try:
            package = find_newer_desktop_update(
                current, local_roots=(install_root, install_root.parent),
                package_format=package_format(install_root),
            )
            LOG.info("更新：找到 v%s，来源 %s", package.version, package.path)
            schedule_package_update(package, install_root)
        except UpdateError as exc:
            LOG.warning("更新失败：%s", exc)
            return
        except Exception:
            LOG.exception("更新失败：发生未预期错误")
            return
        LOG.info(
            "更新已安排：v%s 将在当前程序退出后由固定更新程序安装；"
            "将保留 user_config.json，成功后自动删除安装包并重启。",
            package.version,
        )
        self._close_for_restart()

    # ------------------------------------------------------------- 更新图标 ---
    def _install_root(self) -> Path:
        """The folder VERSION and ``update_info.json`` belong to."""

        return application_root(__file__)

    def _local_version(self) -> str:
        return read_version(self._install_root() / "VERSION")

    def _load_release_notice(self) -> None:
        """Restore the green icon from disk at startup.

        ``read_notice`` deletes a record that is no longer newer than this build,
        so an installed update clears its own notice on the next start.
        """

        try:
            notice = read_notice(self._install_root(), self._local_version())
        except Exception:  # noqa: BLE001 - a display detail must not break startup
            LOG.warning("更新提示：读取本地缓存失败", exc_info=True)
            notice = None
        self._release_notice = notice
        self._refresh_release_tooltip()
        self._paint_release_icon()

    def _release_tooltip_text(self) -> str:
        """What hovering the icon says: the notice, or "up to date"."""

        notice = self._release_notice
        if not isinstance(notice, dict) or not notice.get("version"):
            return f"已是最新版本 v{self._local_version()}"
        lines = [f"发现新版本 v{notice['version']}"]
        info_lines = [
            line.strip() for line in str(notice.get("info") or "").splitlines()
            if line.strip()
        ]
        if info_lines:
            lines.extend(info_lines[:UPDATE_NOTICE_TOOLTIP_LINES])
            if len(info_lines) > UPDATE_NOTICE_TOOLTIP_LINES:
                lines.append("…")
        lines.append("点击立即更新")
        return "\n".join(lines)

    def _refresh_release_tooltip(self) -> None:
        tooltip = getattr(self, "_release_button_tooltip", None)
        if tooltip is not None:
            tooltip.text = self._release_tooltip_text()

    def _paint_release_icon(self) -> None:
        """Draw the glyph: grey idle, green update available, blue downloading."""

        canvas = getattr(self, "_release_button", None)
        if canvas is None:
            return
        notice = self._release_notice
        if self._release_download_in_flight:
            ink = UPDATE_ICON_BUSY
        elif isinstance(notice, dict) and notice.get("version"):
            ink = UPDATE_ICON_GREEN
        else:
            ink = UPDATE_ICON_INK_GREY
        canvas.delete("all")
        # A downward arrow into a tray: "there is something to take in".
        canvas.create_line(15, 7, 15, 17, fill=ink, width=2)
        canvas.create_line(15, 18, 10, 12, fill=ink, width=2)
        canvas.create_line(15, 18, 20, 12, fill=ink, width=2)
        canvas.create_line(8, 24, 22, 24, fill=ink, width=2)
        canvas.create_line(8, 24, 8, 21, fill=ink, width=2)
        canvas.create_line(22, 24, 22, 21, fill=ink, width=2)
        if isinstance(notice, dict) and notice.get("version") and not self._release_download_in_flight:
            # The badge makes the state readable even at a glance, not only by
            # comparing two shades of green.
            canvas.create_oval(19, 4, 27, 12, fill=UPDATE_ICON_GREEN, outline="#ffffff")

    def _set_release_progress(self, text: str) -> None:
        """Show download progress in the caption title, restoring it after."""

        self._release_progress_text = text
        title = getattr(self, "_caption_title", None)
        if title is None:
            return
        try:
            base = getattr(self, "_caption_title_base", "MoonEclipse")
            title.configure(text=f"{base} · {text}" if text else base)
        except Exception:  # noqa: BLE001 - a title is cosmetic
            LOG.debug("更新提示：标题更新失败", exc_info=True)

    def _apply_release_notice(self, notice: Optional[dict[str, Any]]) -> None:
        """Adopt one release record (or clear it) and repaint the icon."""

        previous = (self._release_notice or {}).get("version")
        self._release_notice = notice if isinstance(notice, dict) else None
        self._refresh_release_tooltip()
        self._paint_release_icon()
        current = (self._release_notice or {}).get("version")
        if current and current != previous:
            LOG.info("更新提示：发现新版本 v%s（点击标题栏的更新图标安装）", current)
        elif previous and not current:
            LOG.info("更新提示：已是最新版本 v%s", self._local_version())

    def _schedule_release_check(self, payload: Any) -> None:
        """Compare the announced version and fetch its notes when they are new.

        Called from the heartbeat thread, so it must not touch widgets.  The
        heartbeat carries the **version only** - ``announced_version`` accepts
        exactly that, while ``parse_release`` (which needs a download link) is
        reserved for the full record fetched below.  Using the full-record parser
        here silently rejected every heartbeat announcement and the icon never
        turned green.

        The notes are requested once per version: the heartbeat announces the
        same version every three hours and the stored notice already answers it.
        """

        version = announced_version(payload)
        if not version:
            return
        if not is_newer(version, self._local_version()):
            # The server is at or behind this build: any stored notice is stale.
            if self._release_notice is not None:
                self._put_release_state("clear", version)
            return
        known = self._release_notice or {}
        if known.get("version") == version and (known.get("info") or known.get("link")):
            return
        if self._release_check_in_flight and self._release_check_version == version:
            return
        self._release_check_in_flight = True
        self._release_check_version = version
        threading.Thread(
            target=self._release_check_job, args=(version,),
            name="release-check", daemon=True,
        ).start()

    def _release_check_job(self, version: str) -> None:
        """Worker: fetch the full record for an announced version."""

        try:
            info = fetch_release_info()
            full = info if (info is not None and info.version == version) else None
            if full is not None:
                write_notice(self._install_root(), full)
                notice = {
                    "version": full.version, "info": full.info,
                    "link": full.link, "extra_links": list(full.extra_links),
                }
            else:
                # The announcement is real even when the detail request failed;
                # keep the version so the icon is honest, and let the click
                # fetch the link again.  Nothing to store on disk: a notice
                # without a link cannot be acted on after a restart.
                LOG.warning(
                    "更新提示：服务器公布 v%s，但更新说明获取失败；点击时将重新获取",
                    version,
                )
                notice = {"version": version, "info": "", "link": "", "extra_links": []}
            self._put_release_state("notice", notice)
        except Exception:  # noqa: BLE001 - never kill the heartbeat over this
            LOG.warning("更新提示：获取更新说明失败", exc_info=True)
        finally:
            self._release_check_in_flight = False

    def _put_release_state(self, action: str, payload: Any) -> None:
        """Queue one update-icon event for Tk; dropping a cosmetic one is fine.

        Progress callbacks fire per network chunk, so an unbounded queue would
        grow without limit if the UI thread were busy.  A dropped progress line
        only costs one repaint; the terminal events (``notice``, ``staged``,
        ``failed``) are queued on a fresh queue when the small one is full.
        """

        try:
            self._release_states.put_nowait((action, payload))
        except queue.Full:
            if action in {"progress"}:
                return
            try:
                self._release_states.get_nowait()
            except queue.Empty:
                pass
            try:
                self._release_states.put_nowait((action, payload))
            except queue.Full:
                LOG.debug("更新提示：状态队列已满，丢弃 %s", action)

    def _drain_release_states(self) -> None:
        """Apply worker results for the update icon on Tk's thread."""

        while True:
            try:
                action, payload = self._release_states.get_nowait()
            except queue.Empty:
                return
            if action == "notice":
                self._apply_release_notice(dict(payload))
            elif action == "clear":
                try:
                    clear_notice(self._install_root())
                except Exception:  # noqa: BLE001 - the icon state matters more
                    LOG.warning("更新提示：删除本地缓存失败", exc_info=True)
                self._apply_release_notice(None)
            elif action == "progress":
                self._set_release_progress(str(payload))
            elif action == "failed":
                self._release_download_in_flight = False
                self._paint_release_icon()
                self._set_release_progress("")
                LOG.warning("更新失败：%s", payload)
                self._show_release_message("更新失败", str(payload), error=True)
            elif action == "staged":
                self._release_download_in_flight = False
                self._set_release_progress("")
                LOG.info("更新已安排：v%s 将在助手退出后安装并重启。", (self._release_notice or {}).get("version"))
                self._close_for_restart()

    def _start_release_update(self) -> None:
        """The update icon's click: download the announced release and install it."""

        notice = self._release_notice
        local = self._local_version()
        if not isinstance(notice, dict) or not notice.get("version"):
            self._show_release_message(
                "已是最新版本", f"当前运行的是 v{local}，服务器没有更新版本。",
            )
            return
        if self._release_download_in_flight:
            LOG.info("更新：下载已在进行中，忽略本次点击。")
            return
        version = str(notice["version"])
        from tkinter import messagebox

        if not messagebox.askyesno(
            "安装更新",
            f"下载并安装 v{version}？\n\n安装时助手会关闭，装完自动重启，"
            "你的设置都会保留。",
            parent=getattr(self, "_root", None),
        ):
            return
        release = ReleaseInfo(
            version=version,
            info=str(notice.get("info") or ""),
            link=str(notice.get("link") or ""),
            extra_links=tuple(str(item) for item in (notice.get("extra_links") or [])),
        )
        self._release_download_in_flight = True
        self._paint_release_icon()
        self._set_release_progress("正在准备更新…")
        threading.Thread(
            target=self._release_download_job, args=(release,),
            name="release-download", daemon=True,
        ).start()

    def _release_download_job(self, release: ReleaseInfo) -> None:
        """Worker: download, verify, then hand the archive to the restart helper."""

        root = self._install_root()
        meter = {"position": 0}

        if not release.links():
            # The heartbeat only announced a version and the detail request has
            # not produced a link (yet): fetch it now rather than downloading
            # from nowhere.
            self._put_release_state("progress", "正在获取下载链接…")
            fetched = fetch_release_info()
            if fetched is None or fetched.version != release.version:
                self._put_release_state(
                    "failed",
                    f"无法获取 v{release.version} 的下载链接，请稍后重试；"
                    "也可以稍后再点一次更新图标。",
                )
                return
            release = fetched

        def on_total(total: int) -> None:
            meter["position"] = 0
            if total > 0:
                self._put_release_state(
                    "progress", f"下载更新 0.0/{total / 1048576:.1f} MB"
                )

        def on_bytes(position: int) -> None:
            meter["position"] = position
            self._put_release_state(
                "progress", f"下载更新 {position / 1048576:.1f} MB"
            )

        def on_stage(text_value: str) -> None:
            self._put_release_state("progress", str(text_value))

        try:
            archive = stage_update(
                root, release, on_bytes=on_bytes, on_total=on_total, on_stage=on_stage,
            )
        except UpdateCoreError as exc:
            self._put_release_state("failed", str(exc))
            return
        except Exception:  # noqa: BLE001 - report, never crash the UI
            LOG.exception("更新失败：下载安装包时发生未预期错误")
            self._put_release_state("failed", "下载更新时发生未预期错误，请稍后重试。")
            return
        self._put_release_state("progress", "正在校验安装包…")
        try:
            apply_staged_update(root, archive)
        except (UpdateError, UpdateCoreError) as exc:
            # A package this installation cannot apply (wrong release kind,
            # missing VERSION) must be reported HERE: the helper installs after
            # the application exits, so a mismatch discovered there left the
            # operator with nothing running.
            self._put_release_state("failed", str(exc))
            return
        except Exception:  # noqa: BLE001
            LOG.exception("更新失败：安排安装时发生未预期错误")
            self._put_release_state("failed", "无法安排安装，请稍后重试。")
            return
        self._put_release_state("staged", str(archive))

    def _show_release_message(self, title: str, text: str, *, error: bool = False) -> None:
        """One message box for the update icon, never blocking a worker thread."""

        try:
            from tkinter import messagebox

            if error:
                messagebox.showerror(title, text, parent=getattr(self, "_root", None))
            else:
                messagebox.showinfo(title, text, parent=getattr(self, "_root", None))
        except Exception:  # noqa: BLE001 - no window to attach to
            LOG.info("%s：%s", title, text)

    def _caption_drag_begin(self, event: Any) -> None:
        """Start an outline-only caption drag (no live window moves)."""

        root = getattr(self, "_root", None)
        if root is None:
            return
        self._caption_drag_origin = (event.x_root, event.y_root)
        self._caption_origin_geometry = (root.winfo_x(), root.winfo_y())
        self._destroy_drag_outline()
        try:
            import tkinter as tk

            width = max(root.winfo_width(), 100)
            height = max(root.winfo_height(), 100)
            x = root.winfo_rootx()
            y = root.winfo_rooty()
            state = {"window": None, "borders": [], "width": width,
                     "height": height, "x": x, "y": y}
            # Always use four thin native windows.  Even a nominally
            # transparent full-window overlay can be briefly rendered black
            # by some Windows graphics drivers when it is moved or destroyed.
            # These strips never own the inside of the rectangle, so no black
            # box can be painted over the application.
            for _ in range(4):
                border = tk.Toplevel(root)
                border.withdraw()
                border.overrideredirect(True)
                border.attributes("-topmost", True)
                border.configure(bg="#2f6fdf")
                state["borders"].append(border)
            self._drag_outline = state
            self._position_drag_outline(x, y)
        except Exception:
            self._destroy_drag_outline()

    def _position_drag_outline(self, x: int, y: int) -> None:
        """Position the thin native border used while caption-dragging."""

        state = getattr(self, "_drag_outline", None)
        if not isinstance(state, dict):
            return
        state["x"] = x
        state["y"] = y
        width = state["width"]
        height = state["height"]
        # top, bottom, left, right; these are the only opaque pixels in the
        # entire drag operation.
        positions = (
            (width, 1, x, y), (width, 1, x, y + height - 1),
            (1, height, x, y), (1, height, x + width - 1, y),
        )
        for border, (w, h, px, py) in zip(state.get("borders", ()), positions):
            border.geometry(f"{w}x{h}+{px}+{py}")
            border.deiconify()

    def _destroy_drag_outline(self) -> None:
        """Hide first, then dispose of every outline surface."""

        state = getattr(self, "_drag_outline", None)
        self._drag_outline = None
        if not isinstance(state, dict):
            return
        windows = [state.get("window"), *state.get("borders", ())]
        for window in windows:
            if window is None:
                continue
            try:
                window.withdraw()
                window.destroy()
            except Exception:
                pass

    def _caption_drag_move(self, event: Any) -> None:
        """Move only the cheap outline; the real window stays put."""

        origin = getattr(self, "_caption_drag_origin", None)
        if origin is None:
            return
        if not isinstance(getattr(self, "_drag_outline", None), dict):
            return
        dx = event.x_root - origin[0]
        dy = event.y_root - origin[1]
        x, y = self._caption_origin_geometry
        try:
            self._position_drag_outline(x + dx, y + dy)
        except Exception:
            pass

    def _caption_drag_end(self) -> None:
        """Drop the outline and apply the final position exactly once."""

        origin = getattr(self, "_caption_drag_origin", None)
        if origin is None:
            return
        state = getattr(self, "_drag_outline", None)
        final_x = state.get("x") if isinstance(state, dict) else None
        final_y = state.get("y") if isinstance(state, dict) else None
        # Hide the separate compositor surfaces before moving the real UI.
        # The final move is deliberately native: Tk's geometry manager sends
        # a full client relayout for a position-only change on some Windows
        # builds, briefly painting controls black before restoring them.
        self._destroy_drag_outline()
        if final_x is not None and final_y is not None:
            self._move_window_without_tk_relayout(final_x, final_y)
        self._caption_drag_origin = None

    def _move_window_without_tk_relayout(self, x: int, y: int) -> None:
        """Move the top-level through Windows without asking Tk to relayout.

        Tk ``geometry('+x+y')`` is correct functionally, but it can recreate
        the entire client drawing surface after a manual caption drag.  A
        native position-only move retains that surface and lets DWM move the
        completed frame as one image.  The Tk fallback remains for platforms
        where no native caption window exists.
        """

        hwnd = getattr(self, "_caption_hwnd", None)
        if hwnd and sys.platform == "win32":
            try:
                import ctypes

                SWP_NOSIZE = 0x0001
                SWP_NOZORDER = 0x0004
                SWP_NOACTIVATE = 0x0010
                moved = ctypes.windll.user32.SetWindowPos(
                    int(hwnd), None, int(x), int(y), 0, 0,
                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
                )
                if moved:
                    return
                LOG.debug("native caption move failed; using Tk fallback")
            except Exception:
                LOG.debug("native caption move unavailable; using Tk fallback",
                          exc_info=True)
        try:
            self._root.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _caption_drag_start(self, event: Any) -> None:
        """Compatibility hook; drag handling is bound directly on the bar."""

        self._caption_drag_begin(event)

    def _install_resize_burst_guard(self, root: Any) -> None:
        """Pure-Tk guard: pause heavy UI work while the window is resized.

        Earlier attempts suppressed repaints during move/resize with Win32
        (WndProc subclassing, then WM_SETREDRAW) and both crashed or left the
        window unrendered on some machines.  This version uses NO Win32 calls
        at all: it watches <Configure> bursts (drag/resize in progress) and
        only tells the poll loop to skip its expensive snapshot render + log
        insertion while the burst lasts.  Tk keeps its normal repaint
        behavior, so nothing can crash or freeze - the UI simply stops doing
        extra heavy work during the gesture and resumes when it settles.
        """

        state = {
            "active": False,
            "last": 0.0,
            "size": (root.winfo_width(), root.winfo_height()),
        }
        self._resize_freeze_state = state

        def _settle() -> None:
            state["active"] = False

        def _on_configure(event: Any) -> None:
            # <Configure> events bubble up from every child widget through
            # the bindtags chain; only the ROOT window's own size/position
            # changes mean the user is dragging/resizing the window.
            if getattr(event, "widget", None) is not root:
                return
            size = (getattr(event, "width", 0), getattr(event, "height", 0))
            # Moving a window also sends Configure events.  It does not
            # reflow content, so it must never suspend rendering or it can
            # leave stale/black-looking UI at the drop point.
            if state["size"] == size:
                return
            state["size"] = size
            now = time.monotonic()
            if state["active"]:
                state["last"] = now
                self._resize_settle_job = root.after(300, _settle)
                return
            previous = state["last"]
            state["last"] = now
            if previous and (now - previous) < 0.25:
                state["active"] = True
                self._resize_settle_job = root.after(300, _settle)

        self._resize_configure_handler = _on_configure
        root.bind("<Configure>", _on_configure, add="+")

    def _caption_minimize(self) -> None:
        """Minimize to the taskbar (native iconify still works)."""

        root = getattr(self, "_root", None)
        if root is not None:
            try:
                root.iconify()
            except Exception:
                LOG.warning("minimize failed", exc_info=True)

    def _caption_toggle_maximize(self) -> None:
        """Toggle native maximize/restore for the frameless window."""

        root = getattr(self, "_root", None)
        if root is None:
            return
        try:
            if root.state() == "zoomed":
                root.state("normal")
            else:
                root.state("zoomed")
        except Exception:
            LOG.warning("maximize toggle failed", exc_info=True)

    def _on_debug_window_close(self) -> None:
        """Stop every worker, persist the deadline and geometry, then destroy.

        The close owns the shutdown, so it sets the shared ``stop_event`` HERE
        instead of relying on ``assistant.main`` observing that Tk returned.
        Before this, closing the window only saved state and destroyed the root:
        the reminder timer was stopped later, by a hand-off in the main loop, so
        any path that lost that hand-off (a raising settle call, a worker the
        main loop never reached) left an armed 循环 countdown running after the
        window was gone - and every expiry pushed another 循环提醒 to 钉钉 while
        the operator believed the app was closed ("the loop countdown timer was
        not killed when application closed, so the dingtalk message was sent
        repeatedly").  Setting the event first makes the countdown, both message
        notifiers and the capture loop observe the shutdown immediately.
        """

        root = self._root
        if root is not None:
            stop = getattr(self, "stop_event", None)
            if stop is not None:
                stop.set()
            LOG.info(
                "debug UI closing: stop event set, the countdown and the "
                "message channels stop before the window goes away"
            )
            # Closing normally is an interrupted pass outcome.  The queue gets
            # a bounded best-effort transmit chance without delaying gameplay.
            # Guarded: this call used to be the only statement before the save,
            # so one failure here stopped ``destroy`` from ever running - the
            # dashboard stayed open with the countdown still armed.
            try:
                self._lie_accounting_worker.settle_for_shutdown(timeout=0.5)
            except Exception:  # noqa: BLE001 - the close must always complete
                LOG.warning("lie accounting could not settle on close",
                            exc_info=True)
            self._save_countdown_resume_state()
            try:
                _save_window_geometry(self._geometry_for_save(root))
            except Exception:
                LOG.debug("could not save debug UI geometry on close",
                          exc_info=True)
            try:
                root.destroy()
            except Exception:  # noqa: BLE001 - already gone is still closed
                LOG.debug("root was already destroyed on close", exc_info=True)

    def _geometry_for_save(self, root: Any) -> str:
        """Geometry for persistence (whole window, caption included).

        The custom caption bar is part of the Tk client, so the raw
        winfo geometry already describes the whole window; no adjustment is
        needed.
        """

        return root.winfo_geometry()

    def _poll(self) -> None:
        """Run one tick of the periodic UI work and ALWAYS schedule the next one.

        The re-arm used to be the last statement of this method, so a single exception in any step
        (log drain, patrol sync, hotkey dispatch, api drains) silently ended every periodic task for
        the rest of the session: hotkeys stopped doing anything because their actions are dispatched
        here, and no action sound played any more - while widget callbacks (buttons, focus bindings)
        kept working.  That is exactly the operator's "the hotkey binding is lost and the sound is
        gone" report, with the movement/attack workers still logging normally.
        """

        root = self._root
        if root is None:
            return
        watchdog = getattr(self, "_ui_hang_watchdog", None)
        if watchdog is not None:
            watchdog.kick()
        if self.stop_event.is_set():
            root.destroy()
            return
        now = time.monotonic()
        due_at = self._next_poll_due_at
        late_by = 0.0
        trace_id = self._ui_poll_trace_id + 1
        if due_at is not None:
            late_by = now - due_at
            if late_by >= UI_POLL_SLOW_TOTAL_SECONDS:
                LOG.warning(
                    "TK EVENT LOOP GAP: poll_id=%d began %.0fms late; a prior Tk callback "
                    "or CPU starvation delayed hotkey/UI dispatch",
                    trace_id, late_by * 1000.0,
                )
        poll_started = time.perf_counter()
        self._ui_poll_trace_id = trace_id
        self._ui_poll_trace_started_at = poll_started
        self._ui_poll_trace_steps = []
        if UI_POLL_TRACE_LOGGING:
            UI_POLL_LOG.info(
                "POLL BEGIN id=%d scheduled_late=%.3fms",
                trace_id, max(0.0, late_by) * 1000.0,
            )
        try:
            self._poll_body(root)
        except Exception as exc:
            # Rate limited: a persistent failure used to write a full traceback on every tick
            # (about five per second), which buries every other line in the log.
            now = time.monotonic()
            signature = f"{type(exc).__name__}: {exc}"
            logged_at = getattr(self, "_poll_failure_log_at", float("-inf"))
            if (signature != getattr(self, "_poll_failure_signature", "")
                    or now - logged_at >= POLL_FAILURE_LOG_SECONDS):
                self._poll_failure_signature = signature
                self._poll_failure_log_at = now
                self._poll_failure_count = 0
                LOG.exception(
                    "debug UI poll failed; the periodic refresh continues (hotkeys and sounds stay "
                    "alive)"
                )
            else:
                self._poll_failure_count = getattr(self, "_poll_failure_count", 0) + 1
                LOG.debug(
                    "debug UI poll failed again (%d times): %s",
                    self._poll_failure_count, signature,
                )
        finally:
            with self._trace_ui_poll_step("rearm-next-poll"):
                try:
                    if not self.stop_event.is_set() and root.winfo_exists():
                        self._next_poll_due_at = (
                            time.monotonic() + self.refresh_ms / 1000.0
                        )
                        root.after(self.refresh_ms, self._poll)
                except Exception:
                    LOG.debug("could not re-arm the debug UI poll", exc_info=True)
            elapsed = time.perf_counter() - poll_started
            if UI_POLL_TRACE_LOGGING:
                UI_POLL_LOG.info(
                    "POLL END id=%d body=%.3fms scheduled_late=%.3fms steps=%d",
                    trace_id,
                    elapsed * 1000.0,
                    max(0.0, late_by) * 1000.0,
                    len(self._ui_poll_trace_steps),
                )
            if (UI_POLL_TRACE_LOGGING and (
                    elapsed >= UI_POLL_SLOW_TOTAL_SECONDS
                    or late_by >= UI_POLL_SLOW_TOTAL_SECONDS)):
                self._log_slow_ui_poll_trace(trace_id, elapsed, late_by)

    @contextmanager
    def _trace_ui_poll_step(self, phase: str):
        """Record one UI sub-process boundary without doing synchronous I/O."""

        if not UI_POLL_TRACE_LOGGING:
            yield
            return
        started_at = time.perf_counter()
        entry: list[Any] = [str(phase), started_at, None]
        self._ui_poll_trace_steps.append(entry)
        UI_POLL_LOG.info(
            "STEP BEGIN poll=%d phase=%s offset=%.3fms",
            self._ui_poll_trace_id,
            phase,
            (started_at - self._ui_poll_trace_started_at) * 1000.0,
        )
        try:
            yield
        finally:
            ended_at = time.perf_counter()
            entry[2] = ended_at
            UI_POLL_LOG.info(
                "STEP END poll=%d phase=%s offset=%.3fms elapsed=%.3fms",
                self._ui_poll_trace_id,
                phase,
                (ended_at - self._ui_poll_trace_started_at) * 1000.0,
                (ended_at - started_at) * 1000.0,
            )

    def _log_slow_ui_poll_trace(
        self, trace_id: int, elapsed: float, scheduled_late: float,
    ) -> None:
        """Flush the exact begin/end timeline for one materially slow poll."""

        origin = self._ui_poll_trace_started_at
        LOG.warning(
            "UI POLL TRACE BEGIN id=%d body=%.0fms scheduled_late=%.0fms steps=%d",
            trace_id,
            elapsed * 1000.0,
            max(0.0, scheduled_late) * 1000.0,
            len(self._ui_poll_trace_steps),
        )
        for phase, started_at, ended_at in tuple(self._ui_poll_trace_steps):
            effective_end = time.perf_counter() if ended_at is None else ended_at
            LOG.warning(
                "UI POLL TRACE STEP id=%d phase=%s begin=+%.0fms end=+%.0fms elapsed=%.0fms%s",
                trace_id,
                phase,
                (started_at - origin) * 1000.0,
                (effective_end - origin) * 1000.0,
                (effective_end - started_at) * 1000.0,
                " unfinished" if ended_at is None else "",
            )
        LOG.warning("UI POLL TRACE END id=%d", trace_id)

    def _debug_snapshot_is_needed(self) -> bool:
        """Whether a new diagnostic minimap analysis has a live consumer.

        Patrol, status, character, lie-window detection, and auto-lie cursor
        control all have their own shared-frame workers.  They never depend on
        this display snapshot.  The only non-visual consumer is the brief
        marker-return check after a completed auto-lie pass.
        """

        # The auto-lie outcome watcher has two independent evidence paths:
        # the success panel uses the newest raw capture, while black-room
        # failure uses the minimap marker from a DebugSnapshot.  Without this
        # explicit live consumer, ``last_snapshot`` can remain an old
        # pre-pass frame after the transport closes, so a visibly missing
        # marker is never allowed to reach its consecutive-frame threshold
        # and the close-timeout fallback incorrectly records success.
        outcome_watch = self._auto_lie_outcome_state
        outcome_marker_watch_active = bool(
            outcome_watch
            and not outcome_watch.get("terminal")
            and not outcome_watch.get("timed_out")
        )
        return bool(
            self._SHOW_MINIMAP_PREVIEW
            or self._SHOW_DETECTION_INFO
            or self._auto_lie_marker_verification is not None
            or outcome_marker_watch_active
        )

    def _poll_body(self, root: Any) -> None:
        """One tick's work, without the re-arm (see :meth:`_poll`)."""

        # The physical hotkey stop is an emergency path.  Its hook has already
        # disarmed game input, so let Tk show that fact before draining any
        # routine result queue.  In particular, a slow channel/configuration
        # update must never place a multi-second wall between Ctrl+` and the
        # dashboard state change.
        with self._trace_ui_poll_step("prearmed-hotkey-stop"):
            self._service_prearmed_hotkey_stop()
        # Physical hotkeys are an operator safety path.  The listener thread
        # has already recognized the chord and placed its action in a queue;
        # dispatch it before any OpenCV snapshot build, log display update, or
        # periodic widget refresh.  In particular Ctrl+` must not wait behind
        # debug rendering before it can stop game input.
        with self._trace_ui_poll_step("hotkey-actions"):
            try:
                self._drain_hotkey_actions()
            except Exception:
                LOG.exception("urgent hotkey dispatch failed; the UI tick continues")
        # A detected lie window is a real-time event.  Its detector timestamp
        # anchors the upload deadline, so waiting for the debug snapshot or
        # routine panel repaint here can make an otherwise fast WebSocket
        # handshake start several seconds late.  Start/advance the dedicated
        # worker before all nonessential display work.  The worker itself owns
        # handshake, frame pushes, and server I/O on its own thread.
        for step in (
            self._service_api_auto_lie,
            self._drain_api_auto_lie_results,
            self._report_local_lie_probe,
            self._poll_local_lie_service_hint,
        ):
            phase = f"auto-lie:{getattr(step, '__name__', step)}"
            with self._trace_ui_poll_step(phase):
                try:
                    step()
                except Exception:
                    AUTO_LIE_LOG.exception(
                        "自动过测谎 priority service %s failed; the UI tick continues",
                        getattr(step, "__name__", step),
                    )
        # While a move/resize modal loop is running the client is frozen
        # (repaint suppressed); skip the heavy snapshot render + log insert
        # so the resize stays light, but keep the poll cadence alive.
        with self._trace_ui_poll_step("resize-freeze-gate"):
            frozen = bool(
                getattr(self, "_resize_freeze_state", {}).get("active", False)
            )
        if frozen:
            return
        latest = None
        with self._trace_ui_poll_step("frame-queue-drain"):
            while True:
                try:
                    candidate = self.frame_queue.get_nowait()
                except queue.Empty:
                    break
                latest = candidate
                try:
                    self.frame_queue.task_done()
                except (AttributeError, ValueError):
                    pass
        with self._trace_ui_poll_step("debug-snapshot-result"):
            snapshot_to_render: Optional[DebugSnapshot] = (
                self._debug_snapshot_worker.poll_latest()
            )
        with self._trace_ui_poll_step("latest-frame-cache"):
            if latest is not None:
                self._latest_capture_frame = latest
        # The outcome observer needs only the latest captured image; it does
        # not need a freshly rendered debug snapshot.  Keeping it here means a
        # success/failure observation is never held behind OpenCV UI drawing.
        with self._trace_ui_poll_step("auto-lie:outcome-monitor"):
            try:
                self._service_auto_lie_outcome_monitor()
            except Exception:
                AUTO_LIE_LOG.exception(
                    "自动过测谎 priority outcome observation failed; the UI tick continues"
                )
        with self._trace_ui_poll_step("debug-snapshot-submit"):
            if latest is not None and self._debug_snapshot_is_needed():
                # OpenCV minimap detection and image cropping are diagnostic UI
                # work.  Hand them to a latest-only worker so a slow snapshot can
                # never delay the next hotkey, auto-lie event, or patrol control.
                self._debug_snapshot_worker.submit(latest, self.configured_map_name)
        # Each periodic task is guarded on its own.  The whole body used to
        # share ONE try/except, so an exception thrown by any single step (log
        # drain, status refresh, patrol sync, YOLO exit poll, a result drain)
        # skipped every step after it - including the hotkey dispatch - for the
        # rest of the session, while buttons, workers and the log kept working:
        # the operator's "the hotkey binding is lost" report.  A failing step
        # now costs only itself.
        for step in (
            # Result handlers may update many widgets, persist settings, or
            # restart reconnect work.  They stay on Tk because widgets are
            # not thread-safe, but are deliberately after hotkey and auto-lie
            # dispatch: an armed live pass never waits for cosmetic/status
            # work or configuration I/O.
            self._drain_license_heartbeat_results,
            self._drain_release_states,
            self._drain_device_memo_results,
            self._drain_device_unbind_results,
            self._drain_lie_accounting_results,
            self._drain_channel_update_events,
            self._drain_other_player_stop_events,
            self._drain_reconnect_results,
            self._drain_logs,
            self._refresh_automation_status,
            self._sync_patrol_ui_state,
            self._refresh_shutdown_status,
            self._refresh_countdown_status,
            self._refresh_telegram_status,
            self._drain_dingtalk_results,
            self._refresh_panel_hints,
            self._tick_timed_logout,
            self._poll_yolo_exit,
            self._drain_api_test_results,
            self._service_auto_lie_marker_verification,
        ):
            phase = getattr(step, "__name__", str(step))
            with self._trace_ui_poll_step(phase):
                try:
                    step()
                except Exception:
                    LOG.exception("periodic UI step %s failed; the tick continues",
                                  getattr(step, "__name__", step))

        # Tk rendering is non-critical debug output.  Keep it last so a
        # completed snapshot never delays hotkey or patrol-control handling.
        if snapshot_to_render is not None:
            with self._trace_ui_poll_step("debug-render"):
                try:
                    self.last_snapshot = snapshot_to_render
                    self._render(snapshot_to_render)
                except Exception:
                    LOG.exception("could not render completed debug snapshot")

    def _sync_patrol_ui_state(self) -> None:
        """Refresh patrol buttons when patrol stopped outside the UI.

        Disconnect alerts and sustained focus loss stop patrol directly on
        the controller from assistant.py (no UI action runs), so without
        this check the Stop button stayed enabled and Start stayed greyed
        even though patrol had already stopped.  A controller toggle can
        also be TRANSIENT (a self-rescue disables and re-enables patrol
        within seconds), so the new state must hold for two consecutive
        polls before it is treated as a real change and logged.
        """

        if (self.patrol_controller is None
                or not hasattr(self, "_start_patrol_button")):
            return
        hotkey_cleanup_done = bool(
            self._hotkey_stop_cleanup_done_event is not None
            and self._hotkey_stop_cleanup_done_event.is_set()
        )
        if (self._patrol_stop_pending
                and (self._patrol_stop_cleanup_done.is_set() or hotkey_cleanup_done)):
            completed_at = getattr(self, "_patrol_stop_cleanup_finished_at", None)
            started_at = getattr(self, "_patrol_stop_started_at", None)
            now = time.monotonic()
            self._patrol_stop_pending = False
            self._patrol_stop_cleanup_done.clear()
            if hotkey_cleanup_done and self._hotkey_stop_cleanup_done_event is not None:
                self._hotkey_stop_cleanup_done_event.clear()
            self._refresh_patrol_controls()
            try:
                self._control_status.configure(text="巡逻已停止。")
            except Exception:
                LOG.debug("could not display completed patrol stop", exc_info=True)
            cleanup_ms = (
                (completed_at - started_at) * 1000.0
                if isinstance(started_at, float) and isinstance(completed_at, float)
                else None
            )
            ui_notice_ms = (
                (now - completed_at) * 1000.0
                if isinstance(completed_at, float) else None
            )
            total_ms = (
                (now - started_at) * 1000.0
                if isinstance(started_at, float) else None
            )
            LOG.info(
                "STOP PATROL timing: stopped fully; cleanup=%s, UI-notice=%s, total=%s",
                self._format_stop_elapsed(cleanup_ms),
                self._format_stop_elapsed(ui_notice_ms),
                self._format_stop_elapsed(total_ms),
            )
            self._patrol_stop_started_at = None
            self._patrol_stop_cleanup_finished_at = None
        running = bool(self.patrol_controller.is_enabled())
        if running == getattr(self, "_patrol_ui_running", None):
            self._patrol_pending_running = None
            return
        if getattr(self, "_patrol_pending_running", None) != running:
            self._patrol_pending_running = running
            return
        self._patrol_pending_running = None
        was_running = bool(getattr(self, "_patrol_ui_running", False))
        self._refresh_patrol_controls()
        self._patrol_ui_running = running
        if hasattr(self, "_update_log_icon_visibility"):
            self._update_log_icon_visibility()
        if was_running and not running:
            # Patrol ended outside the UI (auto-stop / focus loss / rescue
            # give-up): surface it in the running log.
            LOG.info(LOG_RUN_STOP + "（外部/自动停止）。")
        elif not was_running and running:
            LOG.info(LOG_RUN_START + "。")

    def _license_allowed(self) -> bool:
        """Return the last background-validation verdict without doing I/O.

        This gate is used by routine UI paths such as Start/Stop Patrol and
        control redraws.  It must never re-read the license document, verify
        a signature, or refresh the hardware fingerprint: the latter can
        start CIM/PowerShell calls and held the Tk thread for several seconds
        during an otherwise immediate Ctrl+` stop.  Startup establishes the
        initial state; activation and the heartbeat worker are the only paths
        that refresh it thereafter.
        """

        if getattr(self, "_license_session_locked", False):
            return False
        status = getattr(self, "_license_status", None)
        return bool(isinstance(status, LicenseStatus) and status.valid)

    def _start_license_heartbeat(self) -> None:
        """Start the immediate + three-hour online entitlement heartbeat."""

        if getattr(self, "_license_heartbeat_started", False):
            return
        self._license_heartbeat_started = True
        if getattr(self, "_license_online_validation_pending", False):
            # The header was put on "正在验证在线授权…" while the panel was being
            # built; now that widgets exist the watchdog can guard it.
            self._arm_license_pending()

        def run_round(aggressive: bool) -> Optional[LicenseStatus]:
            """One validation round; ``None`` means shutdown was requested."""

            return self._license_heartbeat_round(aggressive)

        def heartbeat_loop() -> None:
            while not self.stop_event.is_set():
                generation = self._license_heartbeat_generation
                # A manual refresh (the caption ↻) consumes its flag here: the
                # same round is then allowed the short retry burst.
                aggressive = bool(self._license_refresh_aggressive)
                self._license_refresh_aggressive = False
                status = run_round(aggressive)
                if status is None:          # shutdown requested mid-round
                    return
                try:
                    self._license_heartbeat_results.put_nowait((generation, status))
                except Exception:
                    LOG.warning("license heartbeat result could not be queued", exc_info=True)
                # The heartbeat's release field is the server's newest version.
                # Comparing it here (a worker thread, no widgets) starts the one
                # screen the UI still needs: the green caption icon.
                if status.server_release:
                    try:
                        self._schedule_release_check(status.server_release)
                    except Exception:
                        LOG.warning("更新提示：版本检查调度失败", exc_info=True)
                # The first iteration happens immediately. A failed result
                # keeps the dashboard grey and receives one confirmation ten
                # minutes later; an accepted result resumes the normal
                # three-hour cadence. The caption ↻ always cuts this wait.
                regular_wait_seconds = (
                    3 * 60 * 60 if status.valid
                    else LICENSE_HEARTBEAT_FAILURE_RETRY_SECONDS
                )
                # Always validate at the next GMT+8 day boundary so the UI
                # receives fresh today/yesterday counters right after 00:00.
                wait_seconds = min(
                    regular_wait_seconds, seconds_until_next_gmt8_midnight_refresh()
                )
                if self._wait_for_next_heartbeat(wait_seconds):
                    return

        threading.Thread(
            target=heartbeat_loop,
            name="license-heartbeat",
            daemon=True,
        ).start()

    def _license_heartbeat_round(self, aggressive: bool) -> Optional[LicenseStatus]:
        """One online validation round; ``None`` means shutdown was requested.

        Any raise is turned into a failed status instead of escaping: the header
        stays on "正在验证在线授权…" and the panel stays locked while
        ``_license_online_validation_pending`` is set, so a heartbeat thread that
        died silently left the whole application frozen on that yellow text
        (operator: "the authorization never finished, the application crashed").
        A missing local-service folder, a locked configuration file, a CIM provider
        that vanished between two calls, or any unforeseen error must therefore
        still resolve the header.
        """

        try:
            status = validate_via_server()
            # Device-not-ready means the local CIM provider was not ready, not
            # that the server rejected the entitlement.  Retry the live read after
            # 3 s, 30 s, 60 s and 5 min.  ``force`` avoids the normal
            # five-minute incomplete-fingerprint cache.
            if status.code == "device_not_ready":
                for attempt, delay in enumerate(CIM_BOOT_RETRY_SECONDS, start=1):
                    LOG.warning(
                        "CIM unavailable; retry %d/%d in %.0fs",
                        attempt, len(CIM_BOOT_RETRY_SECONDS), delay,
                    )
                    if self.stop_event.wait(delay):
                        return None
                    status = validate_via_server(force_cim_refresh=True)
                    if status.code != "device_not_ready":
                        break
            for _ in range(
                (LICENSE_REFRESH_RETRY_ATTEMPTS - 1) if aggressive else 0
            ):
                if not license_refresh_retryable(status):
                    break
                if self.stop_event.wait(LICENSE_REFRESH_RETRY_SECONDS):
                    return None
                status = validate_via_server()
            if aggressive:
                LOG.info(
                    "手动刷新：心跳完成（%s）",
                    "有效" if status.valid else status.code,
                )
            return status
        except Exception:  # noqa: BLE001 - a dead heartbeat froze the header
            LOG.exception("在线授权验证异常；本次判定为失败并解锁界面")
            return LicenseStatus(
                False, "heartbeat_error", "在线授权验证异常，请稍后点右上角刷新重试。",
            )

    def _wait_for_next_heartbeat(self, seconds: float) -> bool:
        """Sleep until the next scheduled heartbeat.

        Returns True when the thread must end (shutdown).  A pending manual
        refresh ends the wait instead, and records that the next round is an
        aggressive one, so a press is never lost while a heartbeat is in
        flight.
        """

        deadline = time.monotonic() + seconds
        while True:
            if self.stop_event.is_set():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._license_heartbeat_request.wait(min(remaining, 0.5)):
                self._license_heartbeat_request.clear()
                self._license_refresh_aggressive = True
                return False

    def _request_license_heartbeat(self) -> None:
        """Caption ↻: refresh the online status now, then grey out for a minute.

        The operator's rule: the button no longer updates the package, it does
        an aggressive heartbeat with the remote and updates the current status.
        The press therefore requests an immediate validation (the three-hour
        timer is bypassed and a transport failure is retried right away) and
        disables the button for ``LICENSE_REFRESH_COOLDOWN_SECONDS``, which also
        caps how often a click storm can reach the activation server.
        """

        button = getattr(self, "_update_button", None)
        if button is not None:
            try:
                if str(button.cget("state")) == "disabled":
                    LOG.info("手动刷新：冷却中，忽略本次点击。")
                    return
            except Exception:
                LOG.debug("caption refresh button state unreadable", exc_info=True)
        self._license_refresh_aggressive = True
        # The header says "正在验证在线授权…" until the result lands, exactly as
        # the startup heartbeat does.
        self._arm_license_pending()
        self._license_heartbeat_request.set()
        LOG.info("手动刷新：已请求立即向授权服务器发送心跳。")
        self._refresh_license_ui()
        self._begin_license_refresh_cooldown(button)

    def _arm_license_pending(self) -> None:
        """Mark the online check pending and arm its watchdog.

        Every path that shows the yellow "正在验证在线授权…" header goes through
        here, so the indicator can never outlive
        ``LICENSE_PENDING_WATCHDOG_SECONDS`` without a resolution.
        """

        self._license_online_validation_pending = True
        root = getattr(self, "_root", None)
        if root is None:
            return
        previous = getattr(self, "_license_pending_watchdog_job", None)
        if previous is not None:
            try:
                root.after_cancel(previous)
            except Exception:
                LOG.debug("stale licence watchdog could not be cancelled", exc_info=True)
        try:
            self._license_pending_watchdog_job = root.after(
                int(LICENSE_PENDING_WATCHDOG_SECONDS * 1000),
                self._resolve_stale_license_pending,
            )
        except Exception:  # noqa: BLE001 - the retry loop still exists
            LOG.debug("licence watchdog could not be armed", exc_info=True)

    def _resolve_stale_license_pending(self) -> None:
        """Resolve an online check that never reported back.

        Nothing in the heartbeat path may leave the header pending forever: the
        panel is locked while it is, so the operator would see a frozen
        application on "正在验证在线授权…".  The verdict here is deliberately a
        failure - the real state is unknown - and the normal failure policy
        (locked UI plus the ↻ button and the scheduled retry) takes over.
        """

        self._license_pending_watchdog_job = None
        if not getattr(self, "_license_online_validation_pending", False):
            return
        LOG.warning(
            "在线授权验证 %.0fs 内没有结果；结束等待并允许重试",
            LICENSE_PENDING_WATCHDOG_SECONDS,
        )
        self._license_online_validation_pending = False
        try:
            self._license_status = LicenseStatus(
                False, "heartbeat_timeout",
                "在线授权验证超时，请点右上角刷新重试。",
            )
            self._license_heartbeat_consecutive_failures += 1
            self._lock_license_ui_only()
            if hasattr(self, "_control_status"):
                self._control_status.configure(text="在线授权验证超时，请点右上角刷新重试。")
        except Exception:  # noqa: BLE001 - never raise from a watchdog
            LOG.exception("在线授权超时状态无法呈现")
        # Ask again in the background (not a tight loop: the heartbeat's own
        # failure back-off governs how often the server is contacted).
        self._license_heartbeat_request.set()

    def _begin_license_refresh_cooldown(self, button: Any) -> None:
        """Grey the caption refresh button and re-enable it after the cooldown."""

        root = getattr(self, "_root", None)
        if button is not None:
            try:
                button.configure(state="disabled", disabledforeground="#a3a3a3")
            except Exception:
                LOG.debug("caption refresh button could not be greyed", exc_info=True)
        if root is None:
            return
        previous = getattr(self, "_license_refresh_cooldown_job", None)
        if previous is not None:
            try:
                root.after_cancel(previous)
            except Exception:
                LOG.debug("stale refresh cooldown could not be cancelled", exc_info=True)
        self._license_refresh_cooldown_job = root.after(
            int(LICENSE_REFRESH_COOLDOWN_SECONDS * 1000),
            self._end_license_refresh_cooldown,
        )

    def _end_license_refresh_cooldown(self) -> None:
        """Re-enable the caption refresh button once its cooldown elapsed."""

        self._license_refresh_cooldown_job = None
        button = getattr(self, "_update_button", None)
        if button is None:
            return
        try:
            button.configure(state="normal")
        except Exception:
            LOG.debug("caption refresh button could not be re-enabled", exc_info=True)

    def _drain_license_heartbeat_results(self) -> None:
        """Apply online validation results on Tk's owning thread."""

        latest: Optional[tuple[int, LicenseStatus]] = None
        while True:
            try:
                latest = self._license_heartbeat_results.get_nowait()
            except queue.Empty:
                break
        if latest is None:
            return
        generation, status = latest
        # A manual activation may complete while the initial heartbeat is in
        # flight.  Its older result must never undo the explicit success.
        # The pending indicator still stops: a newer state is already in place,
        # and leaving it on would freeze the header on "正在验证在线授权…".
        if generation != self._license_heartbeat_generation:
            self._license_online_validation_pending = False
            return
        # Clear the pending indicator BEFORE the result is rendered.  The header
        # text is driven by this flag, so an exception while applying the verdict
        # (a Tk callback error, a widget that is not built yet, a local-service
        # call that fails) used to leave the application frozen on
        # "正在验证在线授权…" although the answer had already arrived.
        self._license_online_validation_pending = False
        try:
            status = self._preserve_server_identity(status)
        except Exception:  # noqa: BLE001 - never leave the header pending
            LOG.exception("在线授权结果无法应用（身份保留失败），按原样使用")
        self._license_status = status
        if status.valid:
            try:
                self._license_heartbeat_consecutive_failures = 0
                self._remember_equipment_id(status.equipment_id)
                was_locked = self._license_session_locked
                self._license_session_locked = False
                # Loading persisted controls may restore the saved Auto Lie
                # checkbutton.  Do that first, then apply the server entitlement
                # so a quota-disabled device cannot be switched back on by config.
                if was_locked:
                    LOG.info("license heartbeat accepted; automation unlocked")
                    self._shutdown_load_settings()
                self._refresh_license_ui()
                self._set_license_visual_lock(False)
                self._apply_auto_lie_entitlement(status)
                self._apply_lie_quota_gate(status)
                if hasattr(self, "_control_status"):
                    self._control_status.configure(
                        text=("在线授权验证成功，自动功能已解锁。"
                              if status.auto_lie_allowed
                              else "在线授权验证成功，自动过测谎已被服务器停用。")
                    )
                if hasattr(self, "_quick_message_status"):
                    self._quick_message_status.configure(
                        text=("在线授权验证成功，自动功能已解锁。"
                              if status.auto_lie_allowed
                              else "在线授权验证成功，自动过测谎已被服务器停用。")
                    )
                self._refresh_patrol_controls()
            except Exception:  # noqa: BLE001 - unlocked state is already committed
                LOG.exception("在线授权成功但面板解锁失败")
            return
        self._license_heartbeat_consecutive_failures += 1
        LOG.warning(
            "license heartbeat failed: %s (consecutive=%d)", status.code,
            self._license_heartbeat_consecutive_failures,
        )
        # A CIM result reaches this point only after the complete 3s/30s/1m/
        # 5m retry schedule above.  It is therefore a real terminal hardware
        # readiness failure, not a transient startup race.
        if (status.code == "device_not_ready"
                or self._license_heartbeat_consecutive_failures >= 2):
            self._lock_licensed_functions()
        else:
            self._lock_license_ui_only()
        if hasattr(self, "_control_status"):
            hint = (status.message if status.code in {"server", "device_not_ready"}
                    else f"在线授权验证失败：{status.message}")
            self._control_status.configure(
                text=hint
            )

    def _drain_device_memo_results(self) -> None:
        """Show the result of the non-blocking remote memo synchronization."""

        latest: Optional[tuple[str, LicenseStatus]] = None
        while True:
            try:
                latest = self._device_memo_results.get_nowait()
            except queue.Empty:
                break
        if latest is None:
            return
        _memo, status = latest
        if status.valid:
            self._license_status = self._preserve_server_identity(status)
            self._refresh_license_ui()
            if hasattr(self, "_control_status"):
                self._control_status.configure(text="备注已同步到服务器。")
            LOG.info("device memo synchronized")
            return
        if hasattr(self, "_control_status"):
            self._control_status.configure(text=f"备注已保存到本地；服务器同步失败：{status.message}")
        LOG.warning("device memo synchronization rejected: %s", status.code)

    def _drain_device_unbind_results(self) -> None:
        """Complete a customer self-unbind on Tk after its background request."""

        latest: Optional[LicenseStatus] = None
        while True:
            try:
                latest = self._device_unbind_results.get_nowait()
            except queue.Empty:
                break
        if latest is None:
            return
        self._device_unbind_in_flight = False
        root = getattr(self, "_root", None)
        if latest.valid and latest.code == "unbound":
            # Copy first: the newly unbound code is what the customer needs on
            # the replacement computer.  The local signed document is then
            # removed so no future launch can briefly re-enable this PC.
            self._copy_activation_code()
            clear_server_secret()
            self._revoke_saved_license()
            self._self_unbound_this_session = True
            # A heartbeat that began before the detach still describes the old
            # binding.  Its result must not repaint or unlock this now-unbound
            # computer after the server has accepted the detach.
            self._license_heartbeat_generation += 1
            old = self._license_status
            self._license_status = LicenseStatus(
                False, "unbound", "授权已解绑，请在新设备激活授权。",
                equipment_id=old.equipment_id,
            )
            self._license_online_validation_pending = False
            self._lock_licensed_functions()
            if hasattr(self, "_control_status"):
                self._control_status.configure(text="解绑成功，授权码已复制，本机功能已锁定。")
            try:
                from tkinter import messagebox
                if root is not None:
                    messagebox.showinfo(
                        "解绑成功",
                        "解绑成功，授权码已复制。\n\n请在新设备激活授权；本机功能已锁定。",
                        parent=root,
                    )
            except Exception:
                LOG.debug("device self-unbind success dialog could not open", exc_info=True)
            LOG.info("device self-unbind accepted; local automation locked")
            return
        if hasattr(self, "_control_status"):
            self._control_status.configure(text=f"解绑失败：{latest.message}")
        try:
            from tkinter import messagebox
            if root is not None:
                messagebox.showerror("解绑失败", latest.message, parent=root)
        except Exception:
            LOG.debug("device self-unbind failure dialog could not open", exc_info=True)
        LOG.warning("device self-unbind rejected: %s", latest.code)

    def _drain_lie_accounting_results(self) -> None:
        """Apply immediate server accounting after a completed lie pass.

        The pass has already ended by the time an item reaches this queue.
        A quota refusal therefore disables only future work; it never adds
        latency or cancellation to the live cursor stream.
        """

        latest: Optional[LicenseStatus] = None
        while True:
            try:
                latest = self._lie_accounting_results.get_nowait()
            except queue.Empty:
                break
        if latest is None:
            return
        self._license_status = latest
        self._license_online_validation_pending = False
        if latest.valid:
            self._apply_auto_lie_entitlement(latest)
            # Every completed pass is reported to the server and the reply carries
            # the fresh balance, so this is also where a device that just spent
            # its last pass gets disarmed.
            self._apply_lie_quota_gate(latest)
            self._refresh_license_ui()
            LOG.info("auto-lie accounting synchronized")
            return
        LOG.warning("auto-lie accounting rejected: %s", latest.code)
        self._lock_licensed_functions()
        self._refresh_license_ui()
        if hasattr(self, "_control_status"):
            self._control_status.configure(text=latest.message)

    def _refresh_license_ui(self) -> None:
        """Render the non-fatal authorization state in the always-visible UI."""

        status = self._license_status
        label = getattr(self, "_license_label", None)
        if label is not None:
            memory = getattr(self, "_memory_usage_text", "内存：读取中")
            self._render_memory_usage(memory)
            self._render_license_identity(status)
            if getattr(self, "_license_online_validation_pending", False):
                self._render_lie_usage(None)
                expiry = "永久" if status.expires_at is None else self._format_license_expiry(
                    status.expires_at
                )
                label.configure(
                    text=f"正在验证在线授权 · 到期：{expiry}",
                    foreground="#9a6700",
                )
            elif status.valid:
                self._render_lie_usage(status)
                expiry = "永久" if status.expires_at is None else self._format_license_expiry(
                    status.expires_at
                )
                auto_lie = ""
                if not status.auto_lie_allowed:
                    auto_lie = " · 自动测谎已停用"
                label.configure(text=f"验证成功 · 到期：{expiry}{auto_lie}", foreground="#17803d")
            else:
                self._render_lie_usage(status)
                expiry = "永久" if status.expires_at is None else self._format_license_expiry(
                    status.expires_at
                )
                if status.code == "expired":
                    text = f"授权已过期 · 到期：{expiry}"
                else:
                    text = f"未授权：{status.message}"
                label.configure(text=text, foreground="#202020")
        button = getattr(self, "_license_button", None)
        if button is not None:
            button.configure(text="更换授权" if status.valid else "激活授权")

    def _license_display_values(self) -> tuple[str, str]:
        """Read the user-owned activation code and memo without trusting shape."""

        try:
            data = json.loads(self._license_settings_path().read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            return "", ""
        return str(data.get("activation_code", "") or "").strip(), str(data.get("memo", "") or "").strip()

    def _render_license_identity(self, status: LicenseStatus) -> None:
        """Render independently clickable device, code, and local memo fields."""

        device_label = getattr(self, "_license_device_label", None)
        code_label = getattr(self, "_license_activation_label", None)
        memo_label = getattr(self, "_license_memo_label", None)
        unbind_label = getattr(self, "_license_unbind_label", None)
        code, memo = self._license_display_values()
        identity_active = bool(status.valid) and not bool(
            getattr(self, "_self_unbound_this_session", False)
        )
        if device_label is not None:
            device_text = (
                f" · 设备：{status.equipment_id}（左键复制）"
                if status.equipment_id else " · 设备：等待服务器返回"
            )
            device_label.configure(
                text=device_text,
                foreground="#17803d" if identity_active and status.equipment_id else "#202020",
            )
        if code_label is not None:
            masked = code[-5:] if code else "未设置"
            code_label.configure(
                text=f" · 授权码：{masked}（左键复制）",
                foreground="#17803d" if identity_active and code else "#202020",
            )
        if memo_label is not None:
            memo_label.configure(
                text=f" · 备注：{memo or '未设置'}（左键设置）",
                foreground="#17803d" if identity_active and memo else "#202020",
            )
        if unbind_label is not None:
            already_unbound = bool(getattr(self, "_self_unbound_this_session", False))
            unbind_label.configure(
                text=" · 已解绑" if already_unbound else " · 解绑",
                foreground="#777777" if already_unbound else "#b42318",
            )

    def _render_memory_usage(self, text: str) -> None:
        """Render header memory separately so critical memory is visible."""

        label = getattr(self, "_license_memory_label", None)
        if label is None:
            return
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", str(text))
        try:
            critical = match is not None and float(match.group(1)) > 95.0
        except (TypeError, ValueError):
            critical = False
        label.configure(
            text=f" · {text}",
            foreground="#b42318" if critical else "#17803d",
        )

    def _copy_equipment_id(self, _event: Any = None) -> None:
        """Copy the public server-issued device ID from the authorization bar."""

        equipment_id = str(getattr(self._license_status, "equipment_id", "")).strip()
        if len(equipment_id) != 8:
            return
        root = getattr(self, "_root", None)
        if root is None:
            return
        try:
            root.clipboard_clear()
            root.clipboard_append(equipment_id)
            label = getattr(self, "_license_device_label", None)
            if label is not None:
                label.configure(
                    text=str(label.cget("text")).replace("（左键复制）", "（已复制）")
                )
            previous_job = getattr(self, "_equipment_copy_restore_job", None)
            if previous_job is not None:
                try:
                    root.after_cancel(previous_job)
                except Exception:
                    pass
            # Render from the authoritative status again after the brief copy
            # confirmation, so a heartbeat received in the meantime is never
            # replaced with stale caption text.
            self._equipment_copy_restore_job = root.after(
                1500,
                lambda: (setattr(self, "_equipment_copy_restore_job", None),
                         self._refresh_license_ui()),
            )
            if hasattr(self, "_control_status"):
                self._control_status.configure(text="设备码已复制。")
            LOG.info("equipment ID copied to clipboard")
        except Exception:
            LOG.debug("equipment ID copy failed", exc_info=True)

    def _copy_activation_code(self, _event: Any = None) -> None:
        """Copy the full locally stored activation code while keeping it masked."""

        code, _memo = self._license_display_values()
        root = getattr(self, "_root", None)
        if not code or root is None:
            return
        try:
            root.clipboard_clear()
            root.clipboard_append(code)
            label = getattr(self, "_license_activation_label", None)
            if label is not None:
                label.configure(text=str(label.cget("text")).replace("（左键复制）", "（已复制）"))
            previous_job = getattr(self, "_activation_code_copy_restore_job", None)
            if previous_job is not None:
                try:
                    root.after_cancel(previous_job)
                except Exception:
                    pass
            self._activation_code_copy_restore_job = root.after(
                1500,
                lambda: (setattr(self, "_activation_code_copy_restore_job", None), self._refresh_license_ui()),
            )
            if hasattr(self, "_control_status"):
                self._control_status.configure(text="授权码已复制。")
        except Exception:
            LOG.debug("activation code copy failed", exc_info=True)

    def _save_local_device_memo(self, memo: str) -> bool:
        """Persist the safe display note in the local-only auth section."""

        try:
            path = self._license_settings_path()
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            data["memo"] = memo
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return True
        except OSError:
            LOG.warning("could not save device memo to auth.json", exc_info=True)
            return False

    def _edit_device_memo(self, _event: Any = None) -> None:
        """Prompt for a safe memo, save it locally, then synchronize remotely."""

        _code, current = self._license_display_values()
        try:
            from tkinter import simpledialog
            value = simpledialog.askstring(
                "设置备注",
                "备注仅支持中文、英文、数字、空格、下划线和短横线（最多24字符）：",
                initialvalue=current,
                parent=self._root,
            )
        except Exception:
            LOG.exception("device memo dialog could not open")
            return
        if value is None:
            return
        memo = str(value).strip()
        if not _DEVICE_MEMO_PATTERN.fullmatch(memo):
            self._control_status.configure(
                text="备注格式无效：仅支持中文、英文、数字、空格、下划线和短横线（最多24字符）。"
            )
            return
        if not self._save_local_device_memo(memo):
            self._control_status.configure(text="备注未能保存到本地配置。")
            return
        self._refresh_license_ui()
        self._control_status.configure(text="备注已保存到本地，正在同步服务器…")

        def _sync() -> None:
            status = set_device_memo_via_server(memo)
            try:
                self._device_memo_results.put_nowait((memo, status))
            except Exception:
                LOG.warning("device memo result could not be queued", exc_info=True)

        threading.Thread(target=_sync, name="device-memo-sync", daemon=True).start()

    def _request_device_unbind(self, _event: Any = None) -> None:
        """Ask the server to detach this count-mode code from this equipment."""

        if getattr(self, "_self_unbound_this_session", False):
            return
        if getattr(self, "_device_unbind_in_flight", False):
            if hasattr(self, "_control_status"):
                self._control_status.configure(text="正在请求解绑，请稍候。")
            return
        code, _memo = self._license_display_values()
        if not code:
            if hasattr(self, "_control_status"):
                self._control_status.configure(text="本机没有可用的授权码，无法解绑。")
            return
        root = getattr(self, "_root", None)
        try:
            from tkinter import messagebox
            approved = bool(messagebox.askyesno(
                "解绑授权",
                "解绑后授权码会复制到剪贴板，本机所有功能会立即锁定。\n\n"
                "每日最多可解绑2次。是否继续？",
                parent=root,
            ))
        except Exception:
            LOG.exception("device self-unbind confirmation dialog could not open")
            return
        if not approved:
            return
        self._device_unbind_in_flight = True
        if hasattr(self, "_control_status"):
            self._control_status.configure(text="正在请求解绑…")

        def _unbind() -> None:
            status = unbind_device_via_server(code)
            try:
                self._device_unbind_results.put_nowait(status)
            except queue.Full:
                # There can be only one in-flight request.  If an unexpected
                # duplicate result appears, retain the newest verdict.
                try:
                    self._device_unbind_results.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._device_unbind_results.put_nowait(status)
                except queue.Full:
                    LOG.warning("device self-unbind result could not be queued")
            except Exception:
                LOG.warning("device self-unbind result could not be queued", exc_info=True)

        threading.Thread(target=_unbind, name="device-self-unbind", daemon=True).start()

    def _render_lie_usage(self, status: Optional[LicenseStatus]) -> None:
        """Render entitlement plus today/yesterday outcome counters."""

        fields = (
            "_license_lie_prefix", "_license_lie_remaining",
            "_license_lie_suffix", "_license_lie_today_prefix",
            "_license_lie_total", "_license_lie_success", "_license_lie_failed",
            "_license_lie_yesterday_prefix", "_license_lie_yesterday_total",
            "_license_lie_yesterday_success", "_license_lie_yesterday_failed",
        )
        widgets = [getattr(self, name, None) for name in fields]
        if not all(widgets):
            return
        if status is None:
            for widget in widgets:
                widget.configure(text="")
            return

        unlimited = bool(
            status.auto_lie_unlimited_mode
            or status.remaining_auto_lie_unlimited
        )
        def _values(source: object, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
            source = source if isinstance(source, dict) else {}
            parsed: list[int] = []
            for name, default in zip(("total", "success", "failed"), fallback):
                try:
                    parsed.append(max(0, int(source.get(name, default) or 0)))
                except (TypeError, ValueError):
                    parsed.append(default)
            return tuple(parsed)  # type: ignore[return-value]

        lifetime = (
            status.lie_detect_total,
            status.lie_detect_success_total,
            status.lie_detect_failed_total,
        )
        today_total, today_success, today_failed = _values(
            status.lie_detect_today, lifetime
        )
        yesterday_total, yesterday_success, yesterday_failed = _values(
            status.lie_detect_yesterday, (0, 0, 0)
        )
        if unlimited:
            try:
                unsettled = max(0, int(status.auto_lie_unsettled_count))
            except (TypeError, ValueError):
                unsettled = 0
            self._license_lie_prefix.configure(text="测谎 (未清算, ")
            self._license_lie_remaining.configure(text=str(unsettled), foreground="#17803d")
        else:
            remaining = (
                str(status.remaining_auto_lie_count)
                if status.remaining_auto_lie_count is not None else "—"
            )
            self._license_lie_prefix.configure(text="测谎 (剩余, ")
            self._license_lie_remaining.configure(text=remaining, foreground="#777777")
        self._license_lie_suffix.configure(text=")")
        self._license_lie_today_prefix.configure(text=" (今日, ")
        self._license_lie_total.configure(text=str(today_total))
        self._license_lie_success.configure(text=f", {today_success}")
        self._license_lie_failed.configure(text=f", {today_failed})")
        self._license_lie_yesterday_prefix.configure(text=" (昨日, ")
        self._license_lie_yesterday_total.configure(text=str(yesterday_total))
        self._license_lie_yesterday_success.configure(text=f", {yesterday_success}")
        self._license_lie_yesterday_failed.configure(text=f", {yesterday_failed})")

    def _apply_lie_quota_gate(self, status: LicenseStatus) -> None:
        """Disarm and grey 自动过测谎 while the device has no lie passes left.

        The operator's rule: "if 剩余 turns 0 then make the 自动过测谎 unselected
        immediately and grey the selection until a new heartbeat tells that the
        remaining is bigger than 0", and an unlimited balance passes the check
        untouched.  Only the *selection* is touched here - a pass that is already
        running is left alone, exactly like the server-side entitlement refusal.
        """

        unlimited = bool(getattr(status, "remaining_auto_lie_unlimited", False))
        remaining = getattr(status, "remaining_auto_lie_count", None)
        if self._auto_lie_backend_is_local():
            # 本地测谎 is billed by the local service (its own CT_IN card and WS
            # quota), so the online 剩余 count neither arms nor disarms it: a
            # device with 0 cloud passes can still run the local backend.
            return
        if not unlimited and isinstance(remaining, int) and not isinstance(remaining, bool):
            exhausted = remaining <= 0
        else:
            # Unlimited, or a server that does not report a balance at all: the
            # quota gate has nothing to say.
            exhausted = False
        check = getattr(self, "_api_auto_lie_check", None)
        if exhausted:
            if hasattr(self, "_api_auto_lie_var"):
                self._api_auto_lie_var.set(False)
            if hasattr(self, "_api_auto_lie_backend_var"):
                self._api_auto_lie_backend_var.set(AUTOLIE_BACKEND_OFF)
            self._api_auto_lie_session_armed = False
            self._api_auto_lie_quota_locked = True
            # The credential goes with the balance: the server withholds it at
            # the same moment (an empty auto_lie.secret_key), so keeping the RAM
            # copy would just let a window keep asking for a pass it can no
            # longer pay for.  A later positive balance re-arms it from the
            # server's next reply.
            clear_server_secret()
            if check is not None:
                try:
                    check.state(["disabled"])
                except Exception:
                    LOG.debug("could not grey 自动过测谎", exc_info=True)
            if hasattr(self, "_api_test_status"):
                self._api_test_status.configure(
                    text="自动过测谎：剩余次数已用完，等待服务器补充。"
                )
            if not getattr(self, "_api_auto_lie_quota_logged", False):
                self._api_auto_lie_quota_logged = True
                LOG.warning(
                    "auto-lie quota exhausted (剩余 0); 自动过测谎 unselected, greyed "
                    "and its in-memory lie credential dropped until a heartbeat "
                    "reports a positive balance; a lie window now asks for an "
                    "immediate heartbeat instead of a pass"
                )
            return
        if not getattr(self, "_api_auto_lie_quota_locked", False):
            return
        # The balance came back: the field becomes selectable again (the operator
        # ticks it himself, so a paid feature is never enabled behind his back).
        self._api_auto_lie_quota_locked = False
        self._api_auto_lie_quota_logged = False
        if check is not None:
            try:
                check.state(["!disabled"])
            except Exception:
                LOG.debug("could not re-enable 自动过测谎", exc_info=True)
        if hasattr(self, "_api_test_status"):
            self._api_test_status.configure(
                text="自动过测谎：剩余次数已恢复，可重新勾选。"
            )
        LOG.warning(
            "auto-lie quota restored (剩余 %s); 自动过测谎 can be selected again",
            "无限" if unlimited else remaining,
        )

    def _apply_auto_lie_entitlement(self, status: LicenseStatus) -> None:
        """Disarm and grey 自动过测谎 after a server accounting refusal.

        A live pass is deliberately left alone; accounting happens after a
        completed event, so only subsequent windows are disarmed.  The selection
        is greyed as well, exactly like the exhausted-quota gate: a device the
        server has switched off must not look switchable.
        """

        if bool(getattr(status, "auto_lie_allowed", True)):
            return
        if hasattr(self, "_api_auto_lie_var"):
            self._api_auto_lie_var.set(False)
        if hasattr(self, "_api_auto_lie_backend_var"):
            self._api_auto_lie_backend_var.set(AUTOLIE_BACKEND_OFF)
        self._api_auto_lie_session_armed = False
        if getattr(status, "code", "") in {
            "server:auto_lie_banned", "server:AUTO_LIE_QUOTA_BANNED",
            "server:FINGERPRINT_BANNED",
        }:
            self._api_auto_lie_quota_locked = True
            check = getattr(self, "_api_auto_lie_check", None)
            if check is not None:
                try:
                    check.state(["disabled"])
                except Exception:
                    LOG.debug("could not grey 自动过测谎", exc_info=True)
        if hasattr(self, "_api_test_status"):
            self._api_test_status.configure(text="自动过测谎：服务器已停用此设备。")
        LOG.warning("auto-lie disabled by server device status")

    @staticmethod
    def _format_license_expiry(value: str) -> str:
        """Display license expiry in the operator's local GMT+8 format."""

        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            local = parsed.astimezone(timezone(timedelta(hours=8)))
            # The stored expiry remains a timezone-aware UTC instant.  This
            # compact display intentionally hides the redundant year/zone.
            return local.strftime("%m-%d %H-%M")
        except (TypeError, ValueError):
            return str(value)

    def _set_license_visual_lock(self, locked: bool) -> None:
        """Grey every product control while retaining the activation button."""

        content = getattr(self, "_columns_frame", None)
        if content is None:
            return
        control_classes = {
            "Button", "TButton", "Checkbutton", "TCheckbutton",
            "Radiobutton", "TRadiobutton", "Scale", "TScale",
            "Combobox", "TCombobox", "Entry", "TEntry",
        }
        # Only product panels are gated: the title-bar close/minimize/help
        # controls and the separate activation row must remain usable.
        stack = [content]
        while stack:
            widget = stack.pop()
            try:
                stack.extend(widget.winfo_children())
                if widget.winfo_class() not in control_classes:
                    continue
                if locked:
                    try:
                        widget.state(["disabled"])
                    except Exception:
                        widget.configure(state="disabled")
                else:
                    try:
                        widget.state(["!disabled"])
                    except Exception:
                        widget.configure(state="normal")
            except Exception:
                continue
        # Unlocking the product must not undo the lie-pass quota gate: an
        # exhausted device stays greyed after a licence refresh.
        if not locked and getattr(self, "_api_auto_lie_quota_locked", False):
            check = getattr(self, "_api_auto_lie_check", None)
            if check is not None:
                try:
                    check.state(["disabled"])
                except Exception:
                    LOG.debug("could not re-grey 自动过测谎", exc_info=True)
        # The generic authorization unlock enables every product widget.  Put
        # the attack-mode constraint back afterwards, otherwise a saved
        # 巡逻攻击 session exposes station-only controls until mode is changed.
        if not locked:
            self._fixed_refresh_rows()

    def _show_license_refusal(self) -> None:
        status = self._license_status
        if getattr(self, "_license_online_validation_pending", False) or status.code == "checking":
            text = "正在验证在线授权，请稍候。"
        elif status.code in {"server", "device_not_ready"}:
            text = status.message
        else:
            text = f"未授权：{status.message} 请先点击「激活授权」。"
        if hasattr(self, "_control_status"):
            self._control_status.configure(text=text)
        if hasattr(self, "_quick_message_status"):
            self._quick_message_status.configure(text=text)

    def _show_license_status_alert(self) -> None:
        """Confirm a successful authorization in a user-visible alert."""

        root = getattr(self, "_root", None)
        status = self._license_status
        if root is None or not status.valid:
            return
        expiry = "永久" if status.expires_at is None else self._format_license_expiry(
            status.expires_at
        )
        try:
            from tkinter import messagebox
            messagebox.showinfo(
                "授权状态",
                f"授权有效\n到期：{expiry}",
                parent=root,
            )
        except Exception:
            LOG.debug("license status alert could not open", exc_info=True)

    def _show_activation_result_alert(self) -> None:
        """Show the result of this explicit user activation attempt only."""

        root = getattr(self, "_root", None)
        if root is None:
            return
        status = self._license_status
        try:
            from tkinter import messagebox
            if status.valid:
                self._show_license_status_alert()
            else:
                messagebox.showerror(
                    "授权失败",
                    f"授权码未通过验证。\n\n{status.message}",
                    parent=root,
                )
        except Exception:
            LOG.debug("activation result alert could not open", exc_info=True)

    @staticmethod
    def _license_settings_path() -> Path:
        """Return the local-only activation-attempt settings in auth.json."""

        return config_section_file("license")

    @classmethod
    def _load_saved_equipment_id(cls) -> str:
        """Return the last server-issued public device code, if available."""

        try:
            data = json.loads(cls._license_settings_path().read_text(encoding="utf-8"))
            value = str(data.get("equipment_id", "")).strip()
            return value if len(value) == 8 else ""
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return ""

    def _remember_equipment_id(self, equipment_id: object) -> None:
        """Keep the non-secret public device code visible after local expiry."""

        value = str(equipment_id or "").strip()
        if len(value) != 8:
            return
        try:
            path = self._license_settings_path()
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            if data.get("equipment_id") == value:
                return
            data["equipment_id"] = value
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            LOG.debug("could not remember public equipment ID", exc_info=True)

    def _preserve_server_identity(self, status: LicenseStatus) -> LicenseStatus:
        """Keep known public device/usage state across a failed heartbeat."""

        previous = getattr(self, "_license_status", None)
        if not isinstance(previous, LicenseStatus):
            return status
        equipment_id = status.equipment_id or previous.equipment_id
        if not equipment_id:
            equipment_id = self._load_saved_equipment_id()
        if status.valid:
            # A successful heartbeat is authoritative for entitlement and
            # quota values. Retain only a public ID an older server omitted.
            return replace(
                status,
                equipment_id=equipment_id if len(str(equipment_id)) == 8 else "",
            )
        return replace(
            status,
            equipment_id=equipment_id if len(str(equipment_id)) == 8 else "",
            remaining_auto_lie_count=(
                status.remaining_auto_lie_count
                if status.remaining_auto_lie_count is not None
                else previous.remaining_auto_lie_count
            ),
            remaining_auto_lie_unlimited=(
                status.remaining_auto_lie_unlimited
                or previous.remaining_auto_lie_unlimited
            ),
            auto_lie_unlimited_mode=(
                status.auto_lie_unlimited_mode
                or previous.auto_lie_unlimited_mode
            ),
            auto_lie_unsettled_count=max(
                status.auto_lie_unsettled_count,
                previous.auto_lie_unsettled_count,
            ),
            lie_detect_total=max(status.lie_detect_total, previous.lie_detect_total),
            lie_detect_success_total=max(
                status.lie_detect_success_total, previous.lie_detect_success_total
            ),
            lie_detect_failed_total=max(
                status.lie_detect_failed_total, previous.lie_detect_failed_total
            ),
            lie_detect_today=(status.lie_detect_today or previous.lie_detect_today),
            lie_detect_yesterday=(
                status.lie_detect_yesterday or previous.lie_detect_yesterday
            ),
        )

    def _save_license_attempt(
        self, activation_code: str, status: Optional[LicenseStatus] = None
    ) -> None:
        """Persist the submitted code regardless of whether validation succeeds.

        ``license.json`` is the signed authorization document.  This
        auth.json records the last code the operator chose to submit
        and the outcome of that exact validation attempt.
        """

        try:
            path = self._license_settings_path()
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            data["activation_code"] = str(activation_code)
            if status is not None:
                data.update({
                    "last_validation_valid": bool(status.valid),
                    "last_validation_code": str(status.code),
                    "last_validation_message": str(status.message),
                    "last_validation_at": datetime.now(timezone.utc).isoformat(),
                })
                equipment_id = str(status.equipment_id or "").strip()
                if len(equipment_id) == 8:
                    data["equipment_id"] = equipment_id
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            LOG.warning("could not save activation attempt to auth.json", exc_info=True)

    def _lock_license_ui_only(self) -> None:
        """Immediately grey all product controls without interrupting work."""

        self._license_session_locked = True
        self._set_license_visual_lock(True)
        self._refresh_license_ui()

    def _lock_licensed_functions(self) -> None:
        """Stop every automation worker after the retry heartbeat also fails."""

        self._license_session_locked = True
        self._patrol_intent = False
        self._api_auto_lie_pending = False
        self._api_auto_lie_session_armed = False
        for event in (
            getattr(self, "automation_active_event", None),
            getattr(self, "lie_watch_armed_event", None),
            getattr(self, "disconnect_watch_armed_event", None),
        ):
            try:
                if event is not None:
                    event.clear()
            except Exception:
                LOG.debug("license lock could not clear an automation event", exc_info=True)
        for name in ("attack_worker", "random_jump_worker", "small_step_worker"):
            worker = getattr(self, name, None)
            if worker is not None and hasattr(worker, "enabled"):
                try:
                    worker.enabled = False
                except Exception:
                    LOG.debug("license lock could not disable %s", name, exc_info=True)
        mover = getattr(self, "movement_worker", None)
        if mover is not None:
            try:
                stationary_setter = getattr(mover, "set_stationary_attack_enabled", None)
                if callable(stationary_setter):
                    stationary_setter(False)
                dead_flower_setter = getattr(mover, "set_dead_flower_mode", None)
                if callable(dead_flower_setter):
                    dead_flower_setter(False)
                jump_setter = getattr(mover, "set_jump_attack_enabled", None)
                if callable(jump_setter):
                    jump_setter(False)
                pickup_setter = getattr(mover, "set_stationary_pickup_schedule", None)
                if callable(pickup_setter):
                    pickup_setter(False, 0.0, 0.0)
                delayed_return_setter = getattr(
                    mover, "set_stationary_delayed_return_enabled", None,
                )
                if callable(delayed_return_setter):
                    delayed_return_setter(False)
                flower_cycle_setter = getattr(mover, "set_dead_flower_ground_schedule", None)
                if callable(flower_cycle_setter):
                    flower_cycle_setter(False, 0.0, 0.0)
            except Exception:
                LOG.debug("license lock could disable stationary actions", exc_info=True)
        for variable_name in (
            "_jump_attack_enabled_var", "_stationary_pickup_enabled_var",
            "_stationary_delayed_return_enabled_var", "_small_step_enabled_var",
        ):
            variable = getattr(self, variable_name, None)
            try:
                if variable is not None:
                    variable.set(False)
            except Exception:
                LOG.debug("license lock could clear %s", variable_name, exc_info=True)
        reconnect = getattr(self, "reconnect_worker", None)
        if reconnect is not None:
            try:
                reconnect.set_enabled(False)
                reconnect.request_cancel()
            except Exception:
                LOG.debug("license lock could not stop auto reconnect", exc_info=True)
        restart = getattr(self, "auto_restart_worker", None)
        if restart is not None:
            try:
                restart.configure(enabled=False)
                restart.request_cancel()
            except Exception:
                LOG.debug("license lock could not stop auto restart", exc_info=True)
        countdown = getattr(self, "countdown_worker", None)
        if countdown is not None:
            try:
                countdown.set_enabled(False)
            except Exception:
                LOG.debug("license lock could not stop countdown", exc_info=True)
        for worker, method in (
            (getattr(self, "screen_blinker", None), "set_enabled"),
            (getattr(self, "telegram_notifier", None), "set_enabled"),
        ):
            callback = getattr(worker, method, None)
            if callable(callback):
                try:
                    callback(False)
                except Exception:
                    LOG.debug("license lock could disable %s", method, exc_info=True)
        for worker, method in (
            (getattr(self, "trade_worker", None), "request_cancel"),
            (getattr(self, "lie_detector_worker", None), "set_enabled"),
            (getattr(self, "character_worker", None), "set_disconnect_alert"),
            (getattr(self, "movement_worker", None), "set_other_player_check"),
        ):
            callback = getattr(worker, method, None)
            if callable(callback):
                try:
                    callback(False) if method in {
                        "set_enabled", "set_disconnect_alert", "set_other_player_check"
                    } else callback()
                except Exception:
                    LOG.debug("license lock could not stop %s", method, exc_info=True)
        self.request_cancel_auto_lie_pass()
        for variable_name in (
            "_reconnect_var", "_api_auto_lie_var", "_disconnect_alert_var",
            "_lie_alert_var", "_shutdown_enabled_var", "_player_check_var",
        ):
            variable = getattr(self, variable_name, None)
            try:
                if variable is not None:
                    variable.set(False)
            except Exception:
                pass
        controller = getattr(self, "patrol_controller", None)
        was_running = bool(controller is not None and controller.is_enabled())
        if was_running:
            self._stop_patrol()
        self._set_license_visual_lock(True)
        self._refresh_license_ui()

    def _revoke_saved_license(self) -> None:
        """Ensure a rejected activation also remains rejected after restart."""

        if revoke_license():
            LOG.info("invalid activation removed the saved license")
        else:
            LOG.warning("invalid activation could not remove saved license")

    @staticmethod
    def _activation_failure_replaces_license(status: LicenseStatus) -> bool:
        """Only a verified rejected code supersedes an existing entitlement.

        A bad activation code must lock the product permanently.  A network,
        TLS, or server-configuration outage must not erase a valid paid
        entitlement merely because validation could not be reached.
        """

        return status.code == "activation" or status.code.startswith("server:")

    def _activate_license(self) -> None:
        """Accept a customer activation code for the built-in licensing service."""

        try:
            from tkinter import simpledialog
            code = simpledialog.askstring(
                "激活授权", "请输入授权码：", parent=self._root
            )
        except Exception:
            LOG.exception("license activation dialog could not open")
            return
        if not code:
            return
        code = str(code).strip()
        prior_status = self._license_status
        # Every code uses the same two-step transfer protocol.  Previewing is
        # read-only; only the customer-confirmed request can change bindings.
        preview = preview_activation_transfer_via_server(code)
        if not preview.ok or not preview.transfer_token:
            try:
                from tkinter import messagebox
                messagebox.showerror("授权失败", preview.message, parent=self._root)
            except Exception:
                LOG.debug("activation preview refusal alert could not open", exc_info=True)
            return
        try:
            from tkinter import messagebox
            if preview.bound_elsewhere:
                old_id = preview.bound_equipment_id or "未知设备"
                prompt = (
                    f"该授权码当前绑定设备：{old_id}。\n\n"
                    "确认后将解绑该设备并绑定到当前设备。"
                    "原设备将在下一次授权校验时停止运行。\n\n"
                    "确定要更换授权吗？"
                )
            else:
                prompt = "该授权码尚未绑定设备。\n\n确定要激活到当前设备吗？"
            if not messagebox.askyesno("确认授权", prompt, parent=self._root):
                return
        except Exception:
            LOG.exception("activation transfer confirmation dialog could not open")
            return
        # Supersede a pending heartbeat only after the customer has
        # deliberately confirmed the server's preview.
        self._license_heartbeat_generation += 1
        self._license_online_validation_pending = False
        activation_status = confirm_activation_transfer_via_server(preview.transfer_token)
        if not activation_status.valid:
            # A stale/failed transfer must never remove a still-valid local
            # entitlement merely because the server rejected phase two.
            self._license_status = prior_status
            self._refresh_license_ui()
            try:
                from tkinter import messagebox
                messagebox.showerror("授权失败", activation_status.message, parent=self._root)
            except Exception:
                LOG.debug("activation transfer refusal alert could not open", exc_info=True)
            return
        # An explicit replacement attempt must not hide a device ID that was
        # already known for this installation.  Current servers return it on
        # every rejection; this fallback also keeps the header correct while
        # an older server is being upgraded.
        self._license_status = self._preserve_server_identity(activation_status)
        self._save_license_attempt(code, self._license_status)
        if self._license_status.valid:
            # A freshly accepted activation is the only path that may turn a
            # session-local "已解绑" caption back into the clickable "解绑".
            # It is safe even when the replacement binds to this same device.
            self._self_unbound_this_session = False
            self._license_session_locked = False
            self._refresh_license_ui()
            LOG.info("license activated id=%s", self._license_status.license_id)
            self._set_license_visual_lock(False)
            self._shutdown_load_settings()
            self._apply_auto_lie_entitlement(self._license_status)
            self._apply_lie_quota_gate(self._license_status)
            self._control_status.configure(text="授权已保存，自动功能已解锁。")
        else:
            self._refresh_license_ui()
            LOG.warning("license activation failed: %s", self._license_status.code)
            if self._activation_failure_replaces_license(self._license_status):
                # A verified rejected code replaces the prior activation
                # choice.  Remove the old signed document so a restart cannot
                # silently restore authorization from a previous code.
                self._revoke_saved_license()
            self._lock_licensed_functions()
            self._show_license_refusal()
        self._show_activation_result_alert()
        self._refresh_patrol_controls()

    def _drain_hotkey_actions(self) -> None:
        """Run physical hotkey actions safely on Tk's owning thread.

        Never raises: the queue is drained item by item, so a malformed item
        or a failing action cannot leave a queued chord unexecuted.  Routine
        worker-result drains are intentionally serviced later in ``_poll``:
        they may write configuration or refresh multiple widgets and must not
        delay this emergency path or auto-lie dispatch.
        """
        actions = self.hotkey_queue
        if actions is None:
            return
        while True:
            try:
                action = actions.get_nowait()
            except queue.Empty:
                return
            except Exception:
                LOG.debug("hotkey queue read failed", exc_info=True)
                return
            if not isinstance(action, str):
                # A malformed item must never take the whole poll chain (and with it every hotkey
                # action) down.
                LOG.warning("hotkey action ignored: unexpected item %r", action)
                try:
                    actions.task_done()
                except (AttributeError, ValueError):
                    pass
                continue
            prearmed_stop = False
            if action == "toggle_patrol":
                consume_prearmed = self.on_hotkey_stop_prearmed
                if callable(consume_prearmed):
                    try:
                        prearmed_stop = bool(consume_prearmed())
                    except Exception:
                        LOG.exception("could not consume pre-armed hotkey stop")
            # Ctrl+Z restores only this process's in-memory local settings.
            # It sends no game input, so it remains safe and useful even when
            # a heartbeat has temporarily locked licensed automation.
            if action == "undo:last_change":
                self._play_action_sound(self._undo_latest_change())
                try:
                    actions.task_done()
                except (AttributeError, ValueError):
                    pass
                continue
            if not self._license_allowed():
                # Stopping is always permitted.  The hook has already stopped
                # live input; keep the dashboard consistent even if an
                # entitlement result arrived between key-down and this poll.
                if prearmed_stop:
                    self._finish_prearmed_hotkey_stop()
                    try:
                        actions.task_done()
                    except (AttributeError, ValueError):
                        pass
                    continue
                LOG.warning("hotkey %s ignored: license is not valid", action)
                self._show_license_refusal()
                try:
                    actions.task_done()
                except (AttributeError, ValueError):
                    pass
                continue
            LOG.info("hotkey action run: %s", action)
            try:
                # Re-arming input belongs to this action: a failure here must
                # cost this one chord, never the whole drain (which is what
                # left every later hotkey dead for the session).
                if action.startswith(TYPING_HOTKEY_PREFIXES):
                    self._arm_input_for_hotkey(action)
                if action.startswith("quick_message:"):
                    if not self._send_quick_message(
                        int(action.partition(":")[2])
                    ):
                        self._play_action_sound(False)
                elif action.startswith("trade:"):
                    message = (
                        self._quick_messages[0]
                        if getattr(self, "_quick_messages", []) else ""
                    )
                    if action == "trade:invite" and self.trade_worker is not None:
                        result = self.trade_worker.toggle_invite(message)
                        if result == "cancelled":
                            self._quick_message_status.configure(
                                text="交易：已取消。"
                            )
                        elif result == "started":
                            self._quick_message_status.configure(
                                text="交易：正在邀请并等待交易者。"
                            )
                        else:
                            self._quick_message_status.configure(
                                text="交易失败：交易操作正在进行。"
                            )
                    elif self.trade_worker is None or not self.trade_worker.request(
                        action, message
                    ):
                        self._quick_message_status.configure(
                            text="交易失败：交易操作正在进行。"
                        )
                    else:
                        self._quick_message_status.configure(
                            text="交易：正在接受邀请。"
                        )
                elif action.startswith("record:"):
                    boundary = action.partition(":")[2]
                    self._play_action_sound(self._record_endpoint(boundary))
                elif action.startswith("record_jump_point:"):
                    direction = action.partition(":")[2]
                    self._play_action_sound(self._record_jump_point(direction))
                elif action == "record_split_point":
                    self._play_action_sound(self._record_split_point())
                elif action == "record_flower_point":
                    self._play_action_sound(self._record_flower_point())
                elif action == "record_support_point":
                    self._play_action_sound(self._record_support_point())
                elif action.startswith("record_dual_stationary_post:"):
                    slot_name = action.partition(":")[2]
                    slot = {"first": 0, "second": 1}.get(slot_name)
                    if slot is None:
                        LOG.warning("dual stationary hotkey ignored: unknown slot %r", slot_name)
                        self._play_action_sound(False)
                        continue
                    if str(self._attack_mode_var.get()) != "dual_stationary":
                        self._control_status.configure(text="请先选择双桩模式，再按 Ctrl+R / Ctrl+T 录制。")
                        self._play_action_sound(False)
                        continue
                    if slot == 1 and len(self._dual_stationary_posts_data()) < 1:
                        self._control_status.configure(text="请先用 Ctrl+R 录制桩1，再用 Ctrl+T 录制桩2。")
                        self._play_action_sound(False)
                        continue
                    snapshot = self._capture_snapshot_for_recording()
                    if snapshot is None or snapshot.player_x is None or snapshot.player_y is None:
                        self._control_status.configure(text="无法录制双桩点：未检测到黄色角色标记。")
                        self._play_action_sound(False)
                        continue
                    self._dual_stationary_pending_point = {
                        "x": float(snapshot.player_x),
                        "y": float(snapshot.player_y),
                    }
                    arm = getattr(self.hotkey_worker, "arm_dual_stationary_capture", None)
                    if callable(arm):
                        arm(slot)
                        self._control_status.configure(
                            text=(f"桩{slot + 1}：已记录当前位置，请按 "
                                  "D=朝左、F=朝右、G=双向（Esc 取消）。")
                        )
                    else:
                        self._play_action_sound(False)
                elif action.startswith("record_dual_stationary_facing:"):
                    selector = action.partition(":")[2]
                    point = getattr(self, "_dual_stationary_pending_point", None)
                    self._dual_stationary_pending_point = None
                    if selector == "cancel":
                        self._control_status.configure(text="双桩点录制已取消。")
                    else:
                        try:
                            slot_text, facing = selector.split(":", 1)
                            slot = int(slot_text)
                        except (TypeError, ValueError):
                            LOG.warning("dual stationary facing ignored: malformed selector %r", selector)
                            self._play_action_sound(False)
                            continue
                        self._play_action_sound(
                            self._record_dual_stationary_post(point, facing, slot)
                        )
                elif action == "record_skill_point":
                    # Ctrl+E has already armed phase two in HotkeyWorker.
                    # Do not arm again here: a fast selected key can reach
                    # the queue before Tk processes this action, and a second
                    # UI-side arm would leave a stale capture behind.
                    if self._prepare_skill_point_recording():
                        self._control_status.configure(
                            text="技能点：请按一个可绑定为攻击键的按键作为释放键（Esc 取消）。"
                        )
                    else:
                        cancel = getattr(
                            self.hotkey_worker, "cancel_skill_key_capture", None
                        )
                        if callable(cancel):
                            cancel()
                        self._play_action_sound(False)
                elif action.startswith("record_skill_key:"):
                    key = action.partition(":")[2]
                    if key == "cancel":
                        self._control_status.configure(text="技能点录制已取消。")
                    else:
                        self._play_action_sound(self._record_skill_point(key))
                elif action == "select_next_layer":
                    self._play_action_sound(self._select_next_layer())
                elif action == "select_next_patrol_start":
                    self._play_action_sound(self._select_next_patrol_start())
                elif action.startswith("adjust_fixed_attack_interval:"):
                    self._hotkey_adjust_fixed_interval(
                        float(action.partition(":")[2])
                    )
                elif action == "add_highest_layer":
                    self._play_action_sound(self._add_layer_above())
                elif action == "delete_highest_layer":
                    self._play_action_sound(self._delete_highest_layer())
                elif action == "toggle_patrol":
                    now = time.monotonic()
                    if now < self._hotkey_toggle_ignore_until:
                        LOG.info(
                            "hotkey toggle ignored: previous patrol transition "
                            "just completed"
                        )
                    else:
                        if prearmed_stop:
                            self._finish_prearmed_hotkey_stop()
                            self._play_action_sound(False)
                        elif (self.patrol_controller is not None
                                and self.patrol_controller.is_enabled()):
                            self._stop_patrol()
                            self._play_action_sound(False)
                        else:
                            self._play_action_sound(self._start_patrol())
                        # Suppress only a duplicate delivery of this chord,
                        # not an ordinary later Ctrl+` requested by the user.
                        self._hotkey_toggle_ignore_until = time.monotonic() + 0.75
            except Exception:
                LOG.exception("hotkey action failed: %s", action)
                self._play_action_sound(False)
            finally:
                try:
                    actions.task_done()
                except (AttributeError, ValueError):
                    pass

    def _normalized_player_channel_wait_seconds(self) -> int:
        """Read the selectable delay as a durable 1–1200 second value."""

        variable = getattr(self, "_player_channel_wait_var", None)
        try:
            value = int(variable.get()) if variable is not None else 300
        except Exception:
            value = 300
        return max(1, min(1200, value))

    def _refresh_player_wait_hint(self) -> None:
        """Keep every channel-wait hint synchronized to the displayed value."""

        seconds = self._normalized_player_channel_wait_seconds()
        # Deliberately choose the first line break.  Automatic wrapping used
        # to leave the number at the end of a short line, which is hard to
        # read.  The number, unit and action stay on this short first line.
        hint = (
            f"确定换线以后，再等 {seconds}\u00a0秒换线；\n"
            "用来实现组队的前后脚离开；房间码相同且当前处于同一频道，"
            "则随机频道相同。"
        )
        check_hint = getattr(self, "_player_check_hint", None)
        if check_hint is not None:
            check_hint.text = (
                "当有人来时 30~60 秒随机后发送一条求让消息，继续等待至 3 分钟；"
                f"期间玩家离开则终止；如果没有离开，再多等 {seconds} 秒后换线。"
            )
        for tooltip in getattr(self, "_player_wait_tooltips", ()):
            tooltip.text = hint

    def _player_channel_wait_commit(self) -> None:
        """Normalize, save, and apply the wait field after it loses focus."""

        self._undo_capture_panel_change("附加功能设置")
        seconds = self._normalized_player_channel_wait_seconds()
        control = getattr(self, "_player_channel_wait_control", None)
        if control is not None:
            control.set_bounds(seconds)
        else:
            variable = getattr(self, "_player_channel_wait_var", None)
            if variable is not None:
                variable.set(seconds)
        self._refresh_player_wait_hint()
        self._shutdown_on_change()

    def _reconnect_on_change(self) -> None:
        """Apply the 自动重连 selection (enable + world + channel) to its worker."""

        self._undo_capture_panel_change("自动重连设置")

        # Changing or blurring a settings field must not perform a fresh
        # license-file read.  The startup/activation result is the session's
        # authorization gate; a transient file read here used to turn an
        # already-authorized UI into a false 未授权 state while committing the
        # reconnect channel.
        if not bool(getattr(self, "_license_status", None)
                    and self._license_status.valid):
            if hasattr(self, "_reconnect_var"):
                self._reconnect_var.set(False)
            if self.reconnect_worker is not None:
                self.reconnect_worker.set_enabled(False)
            self._show_license_refusal()
            return
        worker = self.reconnect_worker
        enabled = bool(self._reconnect_var.get()) if hasattr(
            self, "_reconnect_var") else False
        # 自动重连 has no independent trigger: 掉线 is the character
        # detector that raises the reconnect event.  Enabling reconnect must
        # therefore force-enable 掉线 immediately, persist it, and arm the
        # parked capture watch before the reconnect worker is enabled.
        if enabled and hasattr(self, "_disconnect_alert_var"):
            if not bool(self._disconnect_alert_var.get()):
                self._disconnect_alert_var.set(True)
                self._shutdown_on_change()
                LOG.info("自动重连 enabled; 掉线 was enabled as its required trigger")
        world = self._reconnect_world_var.get() if hasattr(
            self, "_reconnect_world_var") else WORLD_NAMES[0]
        channel_text = self._reconnect_channel_var.get() if hasattr(
            self, "_reconnect_channel_var") else str(CHANNEL_DEFAULT)
        channel = valid_channel(channel_text)
        if channel is None:
            channel = CHANNEL_DEFAULT
            control = getattr(self, "_reconnect_channel_control", None)
            if control is not None:
                control.set_bounds(channel)
            elif hasattr(self, "_reconnect_channel_var"):
                self._reconnect_channel_var.set(channel)
        if worker is None:
            if hasattr(self, "_reconnect_status"):
                self._reconnect_status.configure(
                    text="自动重连: 本机助手未启用该工作线程。"
                )
            return
        worker.set_world(world)
        worker.set_channel(channel)
        worker.set_enabled(enabled)
        self._save_reconnect_settings(enabled, world, channel)
        # toggling the selection also re-arms the temporary test button
        self._reconnect_test_idle()
        if hasattr(self, "_reconnect_status"):
            if enabled:
                self._reconnect_status.configure(
                    text=f"自动重连: 已启用 - {world} {channel}频道；"
                         f"掉线后确认登录页即自动进入（未开始巡逻也生效）。"
                )
            else:
                self._reconnect_status.configure(text="自动重连: 未启用。")

    def _register_validator(self, widget, callback):
        """A Tk entry validator for ``callback``, registered on ``widget``.

        A validator must be registered on the Tk interpreter, which only the widget
        (or the root) owns - ``self`` is the UiWorker, not a Tk object.  Registering it
        on the worker is what once stopped the whole assistant from starting:
        ``AttributeError: 'UiWorker' object has no attribute 'register'``.
        """

        return (widget.register(callback), "%P")

    def _reconnect_test_clicked(self) -> None:
        """TEMPORARY: run the 自动重连 sequence once, right now, for the operator's test.

        The enable checkbox is deliberately not required (that is the point of the test
        button); the world and channel boxes are applied to the worker first, so the run
        uses what is on screen.
        """

        worker = self.reconnect_worker
        if worker is None:
            if hasattr(self, "_reconnect_status"):
                self._reconnect_status.configure(
                    text="自动重连: 本机助手未启用该工作线程。"
                )
            return
        self._reconnect_on_change()          # push world/channel (+enabled) to the worker
        if not worker.trigger_test():
            if hasattr(self, "_reconnect_status"):
                self._reconnect_status.configure(text="自动重连: 已有一次重连正在执行。")
            return
        LOG.info("auto reconnect: manual test started from the panel")
        if hasattr(self, "_reconnect_test_button"):
            try:
                self._reconnect_test_button.configure(state="disabled")
            except Exception:
                LOG.debug("test button could not be disabled", exc_info=True)
        if hasattr(self, "_reconnect_status"):
            self._reconnect_status.configure(
                text="自动重连: 手动测试已开始（请勿移动鼠标/键盘）。"
            )

    def _set_reconnect_status(self, text: str) -> None:
        if hasattr(self, "_reconnect_status"):
            try:
                self._reconnect_status.configure(text=text)
            except Exception:
                LOG.debug("reconnect status could not be updated", exc_info=True)

    def _reconnect_test_idle(self) -> None:
        """Re-arm the temporary 测试重连 button after a run finished."""

        if hasattr(self, "_reconnect_test_button"):
            try:
                self._reconnect_test_button.configure(state="normal")
            except Exception:
                LOG.debug("test button could not be re-armed", exc_info=True)

    # ------------------------------------------------------------------ 测试api

    def _api_test_backend_text(self) -> str:
        """Client wording for "is the lie service ready", never internal detail.

        The key and its source are diagnostics: they belong in the log, not on a
        line a customer reads.
        """

        if not has_server_secret():
            return "等待测谎授权"
        return "测谎服务已就绪"

    def _api_test_clicked(self) -> None:
        """试跑: pick a video file and run the drill on it (5 fps, mouse inside the picture)."""

        factory = getattr(self, "api_test_video_factory", None)
        if not callable(factory):
            # Client wording only; the technical reason is a log line.
            LOG.warning("api test: the video drill factory is not wired in this build")
            self._set_api_test_status("试跑暂不可用，请重启助手再试")
            return
        if self._api_test_busy():
            return
        video = self._ask_api_test_video()
        if video is None:
            return
        seconds = self._api_test_video_seconds()
        self._save_api_test_settings()
        # 本地测谎: a cold service needs ~7-10 s (licence ticket + WS handshake), so
        # a drill pressed during that window waits for it instead of failing.
        self._api_test_begin_or_wait(video, seconds)

    def _api_test_begin_or_wait(self, video: Any, seconds: float) -> None:
        """Run the drill now, or wait for a starting 本地测谎 service."""

        if self._auto_lie_backend_is_local():
            ready, note = self._ensure_local_lie_service()
            if not ready:
                if self._api_test_pending is None:
                    self._api_test_pending = (video, seconds)
                    self._api_test_wait_started_at = time.monotonic()
                    LOG.info(
                        "试跑: 本地测谎服务还没就绪（%s）；等待其启动后自动开始", note
                    )
                    self._set_api_test_status(
                        "本地测谎服务正在启动，就绪后自动开始试跑…"
                    )
                    self._api_test_schedule_wait_tick(delay_ms=700)
                return
        self._api_test_pending = None
        self._api_test_wait_started_at = 0.0
        self._begin_api_test(video, seconds)

    def _api_test_schedule_wait_tick(self, *, delay_ms: int) -> None:
        job = getattr(self, "_api_test_wait_job", None)
        if job is not None:
            try:
                self._root.after_cancel(job)
            except Exception:
                LOG.debug("api test wait cancel failed", exc_info=True)
        try:
            self._api_test_wait_job = self._root.after(
                int(delay_ms), self._api_test_wait_tick
            )
        except Exception:
            LOG.debug("api test wait could not be scheduled", exc_info=True)
            self._api_test_wait_job = None

    def _api_test_wait_tick(self) -> None:
        """Poll a cold-starting 本地测谎 service, then start the waiting drill."""

        self._api_test_wait_job = None
        pending = getattr(self, "_api_test_pending", None)
        if pending is None:
            return
        started = float(getattr(self, "_api_test_wait_started_at", 0.0) or 0.0)
        if started and time.monotonic() - started > 40.0:
            self._api_test_pending = None
            self._api_test_wait_started_at = 0.0
            self._report_api_test_failure(
                "本地测谎服务启动失败：40 秒内 127.0.0.1:8765 未响应（详见 运行日志）"
            )
            return
        try:
            from local_lie_backend import probe_local_lie_service

            ok, note = probe_local_lie_service(timeout=1.0)
        except Exception as exc:  # noqa: BLE001 - a probe never blocks the retry
            ok, note = False, f"本地测谎探测失败：{exc}"
        if ok:
            self._local_lie_ready = True
            self._local_lie_note = note
            LOG.info("试跑: 本地测谎服务已就绪，开始等待中的试跑")
            video, seconds = pending
            self._api_test_begin_or_wait(video, seconds)
            return
        # Still starting: retry quickly, and keep the panel text honest.
        self._api_test_schedule_wait_tick(delay_ms=700)

    def _begin_api_test(self, video: Any, seconds: float) -> None:
        """Start the drill on *video* (the body of the former _api_test_clicked)."""

        # This method is also reached from the service wait-tick, which never runs
        # the button handler, so every name the body needs is resolved HERE.  The
        # split of _api_test_clicked missed this one and every 试跑 died with
        # "NameError: name 'factory' is not defined".
        factory = getattr(self, "api_test_video_factory", None)
        if not callable(factory):
            LOG.warning("api test: the video drill factory is not wired in this build")
            self._report_api_test_failure("测试视频功能暂不可用（本版本未接入），请重启助手后再试")
            return
        local_backend = self._auto_lie_backend_is_local()
        self._api_test_local_backend = bool(local_backend)
        key = "" if local_backend else get_server_secret()
        window_factory = getattr(self, "api_test_window_factory", None)
        if window_factory is None:
            try:
                from api_lie_video import VideoDrillWindow as window_factory
            except Exception:
                LOG.exception("api test: the video window could not be imported")
                self._set_api_test_status("视频窗口打不开，请重试")
                return
        try:
            window = window_factory(
                self._root, f"测试本地测谎 — {video.name}",
                on_close=self._api_test_window_closed,
                on_toggle_mouse=self._api_test_toggle_mouse,
                on_toggle_pause=self._api_test_toggle_pause,
            )
        except Exception:
            LOG.exception("api test: the video window could not be created")
            self._set_api_test_status("视频窗口打不开，请重试")
            return
        display: "queue.Queue[Any]" = queue.Queue(maxsize=2)
        # 本地测谎: no online 测谎密钥 at all.  The local service on 127.0.0.1:8765
        # bills its own CT_IN card and WS quota, so 测试api can run against it the
        # moment the service answers its health probe.
        local_backend = self._auto_lie_backend_is_local()
        # Which service served this drill decides where its accounting goes.  The
        # result drain runs later in another method, so the choice is recorded
        # here instead of re-reading the radio (the operator may switch the group
        # while a 测试api run is still playing).
        self._api_test_local_backend = bool(local_backend)
        key = "" if local_backend else get_server_secret()
        if not local_backend and not key:
            try:
                window.close()
            except Exception:
                pass
            self._report_api_test_failure("还没拿到测谎授权，请稍等一会再试")
            return
        if local_backend:
            ready, note = self._ensure_local_lie_service()
            if not ready:
                try:
                    window.close()
                except Exception:
                    pass
                AUTO_LIE_LOG.info("自动测谎: 本地服务未就绪，试跑取消（%s）", note)
                self._report_api_test_failure("本地测谎服务还没准备好，请等几秒再试")
                return
        try:
            if local_backend:
                worker = factory(video=video, results=self.api_test_results,
                                 display=display, seconds=seconds, key="",
                                 backend=AUTOLIE_BACKEND_LOCAL)
            else:
                worker = factory(video=video, results=self.api_test_results, display=display,
                                 seconds=seconds, key=key)
        except Exception:
            LOG.exception("api test: the video worker could not be created")
            try:
                window.close()
            except Exception:
                LOG.debug("video window close failed", exc_info=True)
            self._report_api_test_failure("试跑没能启动，请重试；多次失败请重启助手")
            return
        self.api_test_worker = worker
        self.api_test_window = window
        self.api_test_video = str(video)
        self._api_test_display = display
        worker.image_rect = window.image_region()
        self._api_test_set_buttons("disabled")
        LOG.info("api test (video): %s for %.0fs at 5 fps (key=%s, backend=%s)",
                 video.name, seconds, "set" if key else "none -> mimic",
                 self._api_test_backend_text())
        # 本地测谎 statistics: a local drill is a local pass too.  Without this the
        # 本地测谎 panel stayed empty after 测试API (the field report: "全局测谎统计
        # moved but 本地测谎 did not"), because only the automatic pass reported.
        self._local_lie_drill_event = ""
        if local_backend:
            import uuid as _uuid

            self._local_lie_drill_event = str(_uuid.uuid4())
            try:
                import local_lie_service

                local_lie_service.report_local_pass(
                    self._local_lie_drill_event, "pending", detail="测试本地测谎",
                )
            except Exception:  # noqa: BLE001 - statistics never block a drill
                LOG.debug("本地测谎 测试API start report failed", exc_info=True)
        worker.start()
        self._set_api_test_status(
            f"测试本地测谎：正在试跑 {video.name}，约 {seconds:.0f} 秒；"
            "鼠标只在视频画面内，按 Esc 或关闭窗口即停止")
        self._api_test_keep_focus()

    def _api_test_busy(self) -> bool:
        """True when a drill is already running (the button says so instead of starting another)."""

        worker = getattr(self, "api_test_worker", None)
        if worker is not None and getattr(worker, "running", False):
            self._set_api_test_status("试跑已经在进行，请等它结束")
            return True
        return False

    def _ask_api_test_video(self):
        """Ask for the video to test.  Any file the operator likes; the folder is remembered."""

        try:
            from tkinter import filedialog

            start = getattr(self, "_api_test_video_dir", "") or str(
                Path(__file__).resolve().parent / "detect_video")
            chosen = filedialog.askopenfilename(
                parent=self._root,
                title="选择要试跑的视频",
                initialdir=start,
                filetypes=[("视频", "*.mp4 *.avi *.mkv *.mov *.wmv *.flv *.m4v"),
                           ("所有文件", "*.*")],
            )
        except Exception as exc:
            LOG.exception("api test: the video picker failed")
            LOG.warning("api test: picker error was %r", exc)
            self._set_api_test_status("选择视频时出错了，请重试")
            return None
        text = str(chosen or "").strip()
        if not text:
            # Cancelling the picker is a normal action, not a failure to report.
            LOG.debug("api test: the video picker was cancelled")
            return None
        video = Path(text)
        if not video.is_file():
            LOG.info("api test: the chosen video is gone (%s)", video.name)
            self._set_api_test_status("视频文件找不到了，请重新选择")
            return None
        self._api_test_video_dir = str(video.parent)
        return video

    def _api_test_set_buttons(self, state: str) -> None:
        """Arm or disarm every drill button (the two share one slot)."""

        self._api_test_arm_state = state
        seen: set[int] = set()
        for name in ("_api_test_button", "_api_test_local_button",
                     "_api_test_cloud_button"):
            button = getattr(self, name, None)
            if button is None or id(button) in seen:
                continue
            seen.add(id(button))
            try:
                button.configure(state=state)
            except Exception:
                LOG.debug("api test button state could not be set", exc_info=True)
        self._sync_api_test_buttons()

    def _sync_api_test_buttons(self) -> None:
        """Show the drill button the 自动测谎 selection allows, and nothing else.

        本地测谎 exposes 测试本地测谎.  云端测谎 and 关闭 show no drill at all: the slot
        keeps its frozen size, so the row neither grows nor jumps when the button
        comes and goes.  测试云端测谎 stays in the build but is never packed while
        ``API_TEST_CLOUD_BUTTON_VISIBLE`` is false.
        """

        local = getattr(self, "_api_test_local_button", None)
        cloud = getattr(self, "_api_test_cloud_button", None)
        if local is None or cloud is None:
            return
        mode = self._auto_lie_backend_mode()
        show_local = mode == AUTOLIE_BACKEND_LOCAL
        show_cloud = bool(
            API_TEST_CLOUD_BUTTON_VISIBLE and mode == AUTOLIE_BACKEND_CLOUD
        )
        # ``winfo_manager`` says whether pack owns the widget; ``winfo_ismapped``
        # would also be false for a packed button on a hidden tab, and then the
        # hidden one would never be unpacked and both would show up later.
        def managed(button: Any) -> bool:
            return bool(button.winfo_manager())

        for button, wanted in ((local, show_local), (cloud, show_cloud)):
            try:
                if wanted and not managed(button):
                    button.pack(side="left", fill="both", expand=True)
                elif not wanted and managed(button):
                    button.pack_forget()
            except Exception:  # noqa: BLE001 - never break a selection change
                LOG.debug("api test button could not be swapped", exc_info=True)
        armed = str(getattr(self, "_api_test_arm_state", "normal")) == "normal"
        # 本地测谎 without a CT card cannot serve a pass, so its drill is not
        # clickable either - the operator reported the button as still clickable
        # while the radio said 未开通.  A hidden drill is disabled too, so no path
        # can pack and use it behind a missing card.
        local_has_card = self._local_lie_access() is not False
        for button, wanted in ((local, show_local), (cloud, show_cloud)):
            usable = local_has_card if button is local else True
            try:
                button.configure(
                    state=(
                        "normal"
                        if wanted and armed and mode != AUTOLIE_BACKEND_OFF and usable
                        else "disabled"
                    )
                )
            except Exception:
                LOG.debug("api test button could not be re-armed", exc_info=True)

    def _api_test_window_closed(self) -> None:
        """The operator closed the video window: stop the drill."""

        worker = getattr(self, "api_test_worker", None)
        if worker is not None:
            try:
                worker.request_stop()
            except Exception:
                LOG.debug("video drill stop request failed", exc_info=True)
            self._set_api_test_status("视频窗口已关闭，正在停止试跑")
        else:
            self._api_test_idle()

    def _api_test_toggle_mouse(self, enabled: bool) -> None:
        worker = getattr(self, "api_test_worker", None)
        if worker is None or not hasattr(worker, "set_mouse_enabled"):
            return
        try:
            worker.set_mouse_enabled(bool(enabled))
        except Exception:
            LOG.debug("video drill mouse toggle failed", exc_info=True)
        self._set_api_test_status(
            "试跑：鼠标跟随已开启（只在视频画面内移动）" if enabled
            else "试跑：鼠标跟随已关闭")

    def _api_test_toggle_pause(self, paused: bool) -> None:
        worker = getattr(self, "api_test_worker", None)
        if worker is None or not hasattr(worker, "set_paused"):
            return
        try:
            worker.set_paused(bool(paused))
        except Exception:
            LOG.debug("video drill pause toggle failed", exc_info=True)
        self._set_api_test_status("试跑：已暂停" if paused else "试跑：继续播放")

    def _api_test_keep_focus(self) -> None:
        """Keep the video window focused (throttled), so mouse input belongs to the video."""

        window = getattr(self, "api_test_window", None)
        worker = getattr(self, "api_test_worker", None)
        if window is None or worker is None or not getattr(worker, "running", False):
            return
        now = time.monotonic()
        if now - getattr(self, "_api_test_keep_focus_at", 0.0) < 1.0:
            return
        self._api_test_keep_focus_at = now
        try:
            if not window.is_focused():
                window.focus()
        except Exception:
            LOG.debug("video window refocus failed", exc_info=True)

    def _api_test_video_seconds(self) -> float:
        """The video test's run length: the measured ~30s, with no panel setting.

        The 时长 box is gone on purpose: the operator's locked decision is a ~30s run at the API's
        5 fps, and a shorter run only covers the frozen screen before the lie window appears.
        ``api_lie_video.DEFAULT_SECONDS`` is the same number, so the worker never disagrees.
        """

        return API_TEST_VIDEO_SECONDS

    def _set_api_test_status(self, text: str) -> None:
        # The status sink is deliberately NOT packed (the panel shows controls
        # only), so the running log is the channel an operator can read: without
        # this line a failed 试跑 press left nothing behind at all.  The text is
        # client wording; anything technical is logged separately by the caller.
        LOG.info("试跑: %s", text)
        if hasattr(self, "_api_test_status"):
            try:
                self._configure_text_if_changed(
                    self._api_test_status, "api_test_status", text
                )
            except Exception:
                LOG.debug("api test status could not be updated", exc_info=True)

    def _report_api_test_failure(self, text: str) -> None:
        """A 试跑 press could not start: log it, note it, and tell the client.

        The dialog body is client wording plus where the details went, so a
        non-technical operator knows both what happened and who can look into it.
        """

        self._set_api_test_status(text)
        LOG.warning("试跑 could not start: %s", text)
        try:
            import local_lie_service

            local_lie_service.note_failure(text)
        except Exception:  # noqa: BLE001 - the hint is best effort
            LOG.debug("local lie service note failed", exc_info=True)
        try:
            self._refresh_panel_hints()
        except Exception:  # noqa: BLE001
            LOG.debug("panel hint refresh failed", exc_info=True)
        body = f"{text}\n\n详情已记录到运行日志。"
        try:
            from tkinter import messagebox

            messagebox.showerror(
                "测试本地测谎", body, parent=getattr(self, "_root", None)
            )
        except Exception:  # noqa: BLE001 - no window to attach to
            LOG.info("试跑: %s", body)

    def _drain_api_test_results(self) -> None:
        """Show the 试跑 drill's progress on Tk's owning thread.

        Only outcomes reach the client; per-frame and backend chatter stays in the
        running log, where support can read it.
        """

        self._drain_api_test_display()
        results = getattr(self, "api_test_results", None)
        if results is None:
            return
        while True:
            try:
                item = results.get_nowait()
            except queue.Empty:
                return
            except Exception:
                LOG.debug("api test result read failed", exc_info=True)
                return
            if not (isinstance(item, tuple) and len(item) == 2):
                LOG.warning("api test result ignored: unexpected item %r", item)
                try:
                    results.task_done()
                except (AttributeError, ValueError):
                    pass
                continue
            state, detail = item
            try:
                if state in ("frame", "backend", "capture"):
                    LOG.debug("试跑 progress: %s %s", state, detail)
                elif state == "done":
                    self._set_api_test_status("试跑完成")
                    self._play_action_sound(True)
                    # A completed manual API drill counts for exactly one backend.
                    # 云端测谎 charges the server quota, so the cloud accounting
                    # event is queued for a cloud drill only: a 本地测谎 drill is
                    # billed by the CT card and belongs to 本地测谎统计, and
                    # queueing a cloud event here is what made 云端测谎统计 move
                    # while 本地测谎统计 stayed empty.
                    if not getattr(self, "_api_test_local_backend", False):
                        self._enqueue_auto_lie_accounting("api-test")
                    self._report_local_lie_drill("success", detail)
                    self._api_test_idle()
                elif state == "stopped":
                    self._set_api_test_status("已停止试跑")
                    self._report_local_lie_drill("aborted", detail)
                    self._api_test_idle()
                elif state == "failed":
                    self._set_api_test_status("试跑失败")
                    self._play_action_sound(False)
                    self._report_local_lie_drill("failed", detail)
                    self._api_test_idle()
                else:
                    LOG.debug("试跑 progress: %s", state)
            finally:
                try:
                    results.task_done()
                except (AttributeError, ValueError):
                    pass

    def _drain_api_test_display(self) -> None:
        """Draw the newest video frame in the drill window and hand its rect back to the worker.

        The worker only moves the mouse inside ``image_rect``, and only the UI thread knows where
        Tk actually placed the picture, so every displayed frame re-publishes it.
        """

        window = getattr(self, "api_test_window", None)
        worker = getattr(self, "api_test_worker", None)
        if window is None or worker is None:
            return
        frame = hud = None
        while True:
            try:
                frame, hud = self._api_test_display.get_nowait()
            except queue.Empty:
                break
            except Exception:
                LOG.debug("api test display queue error", exc_info=True)
                break
        if frame is None:
            return
        try:
            window.update_frame(frame, hud or "")
        except Exception:
            LOG.debug("video frame could not be shown", exc_info=True)
            return
        try:
            rect = window.image_region()
        except Exception:
            rect = None
        if rect:
            worker.image_rect = rect
        try:
            self._api_test_keep_focus()
        except Exception:
            LOG.debug("api test window could not be kept focused", exc_info=True)

    def _api_test_idle(self) -> None:
        """The drill ended (or was stopped): close the video window and re-arm the buttons."""

        self._api_test_set_buttons("normal")
        window = getattr(self, "api_test_window", None)
        self.api_test_window = None
        if window is not None:
            try:
                window.close()
            except Exception:
                LOG.debug("api test window could not be closed", exc_info=True)
        try:
            while True:
                self._api_test_display.get_nowait()
        except queue.Empty:
            pass
        except Exception:
            LOG.debug("api test display drain failed", exc_info=True)

    def _save_api_test_settings(self) -> None:
        try:
            self._shutdown_save_settings(self._shutdown_collect_data())
        except Exception:
            LOG.warning("api test settings could not be saved", exc_info=True)

    def _load_api_auto_lie_setting(self) -> bool:
        """Whether 自动过测谎 was armed last time (part of user_config, like the reconnect)."""

        try:
            data = json.loads(
                self._shutdown_settings_path().read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return False
        return bool(data.get("auto_lie_api_enabled", False))

    def _load_api_auto_lie_backend_setting(self) -> str:
        """Which 测谎 backend was selected last time: ``cloud`` or ``local``.

        Unknown/absent values mean ``cloud``: that is what every earlier version
        ran, so an existing user_config keeps its exact behaviour.
        """

        try:
            data = json.loads(
                self._shutdown_settings_path().read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return AUTOLIE_BACKEND_CLOUD
        value = str(data.get("auto_lie_backend") or "").casefold()
        return AUTOLIE_BACKEND_LOCAL if value == AUTOLIE_BACKEND_LOCAL else AUTOLIE_BACKEND_CLOUD

    def _auto_lie_backend_mode(self) -> str:
        """The effective selection: ``off`` | ``cloud`` | ``local``."""

        variable = getattr(self, "_api_auto_lie_backend_var", None)
        value = str(variable.get()) if variable is not None else AUTOLIE_BACKEND_OFF
        if value not in (AUTOLIE_BACKEND_CLOUD, AUTOLIE_BACKEND_LOCAL):
            return AUTOLIE_BACKEND_OFF
        armed = getattr(self, "_api_auto_lie_var", None)
        if armed is None or not bool(armed.get()):
            return AUTOLIE_BACKEND_OFF
        return value

    def _auto_lie_backend_is_local(self) -> bool:
        return self._auto_lie_backend_mode() == AUTOLIE_BACKEND_LOCAL

    def _auto_lie_backend_label(self) -> str:
        mode = self._auto_lie_backend_mode()
        if mode == AUTOLIE_BACKEND_LOCAL:
            return "本地测谎"
        if mode == AUTOLIE_BACKEND_CLOUD:
            return "云端测谎"
        return "关闭"

    def _api_auto_lie_backend_changed(self) -> None:
        """A radio in the 自动测谎 group was clicked.

        The group is the author of the armed boolean that every existing code
        path reads, so the two can never disagree: choosing 关闭 disarms, and
        choosing a backend arms it (which also arms 测谎, its prerequisite).
        """

        mode = str(getattr(self, "_api_auto_lie_backend_var", None).get())
        armed = mode in (AUTOLIE_BACKEND_CLOUD, AUTOLIE_BACKEND_LOCAL)
        self._api_auto_lie_var.set(armed)
        self._api_auto_lie_change_origin = "mouse"
        # Pre-start / stop the local service with the selection itself: 本地测谎
        # starts it and keeps it listening; 关闭 and 云端测谎 stop it again (only an
        # instance this app started); reselecting 本地测谎 restarts it.  There is no
        # lazy start at a lie window and nothing for the operator to launch.
        #
        # A pass in flight owns the service: stopping it underneath the pass would
        # abort the very lie window being answered, so the stop is deferred to the
        # pass end (see _service_api_auto_lie).  A start is always allowed.
        wanted = mode == AUTOLIE_BACKEND_LOCAL
        running = self._worker_is_running(getattr(self, "_api_lie_pass_worker", None))
        try:
            import local_lie_service

            if not wanted and running:
                self._local_lie_stop_pending = True
                AUTO_LIE_LOG.info(
                    "自动测谎: 本地测谎 service stop deferred - a pass is running"
                )
            else:
                self._local_lie_stop_pending = False
                local_lie_service.supervisor_set_active(
                    wanted, application_root(__file__)
                )
        except Exception:  # noqa: BLE001 - never break a selection change
            LOG.debug("本地测谎 supervisor switch failed", exc_info=True)
        if mode == AUTOLIE_BACKEND_LOCAL:
            # 本地测谎 has its own connection: report it the moment it is chosen
            # instead of failing on the first lie window.
            self._probe_local_lie_service()
        # The drill button follows the selection: 本地测谎 exposes 测试本地测谎,
        # 云端测谎 exposes 测试云端测谎, 关闭 greys it.
        self._sync_api_test_buttons()
        self._api_auto_lie_on_change()

    def _probe_local_lie_service(self) -> None:
        """Probe 127.0.0.1:8765 in the background and report it on the status line.

        The service validates its licence and handshakes the coordinate cloud
        BEFORE it listens on 8765 (~5-20 s cold), so a single probe fired right
        after the selection can see "connection refused" while the service is
        simply still starting - the field log at 00:34:38 was exactly that.  The
        probe therefore retries for a short while before reporting a failure, and
        ``_poll_local_lie_service_hint`` re-probes when the state is not ready, so
        the panel heals itself once the service is up.
        """

        self._local_lie_probe_at = time.monotonic()

        def job() -> None:
            ok = False
            note = ""
            for attempt in range(LOCAL_LIE_PROBE_ATTEMPTS):
                try:
                    from local_lie_backend import probe_local_lie_service

                    ok, note = probe_local_lie_service()
                except Exception as exc:  # noqa: BLE001 - never break the UI thread
                    ok, note = False, f"本地测谎探测失败：{exc}"
                if ok:
                    break
                if attempt + 1 < LOCAL_LIE_PROBE_ATTEMPTS:
                    time.sleep(LOCAL_LIE_PROBE_INTERVAL_SECONDS)
            self._local_lie_ready = bool(ok)
            self._local_lie_note = note
            # The probe runs off the Tk thread, so it may not touch a widget:
            # _poll picks the flag up and writes the status line there.
            self._local_lie_report_pending = True
            AUTO_LIE_LOG.info("自动过测谎 backend=local: %s", note)

        threading.Thread(target=job, name="local-lie-probe", daemon=True).start()

    def _local_lie_access(self) -> Optional[bool]:
        """Whether this activation code may use 本地测谎, or None while unknown.

        The CT card is the access token: the licence reply carries it in the device
        document, so a code without one has no 本地测谎.  Before the first reply
        nothing is known and the selection must stay usable.
        """

        try:
            import local_lie_service

            if not local_lie_service.card_known():
                return None
            return bool(local_lie_service.has_online_card())
        except Exception:  # noqa: BLE001 - an unavailable module must not lock the UI
            LOG.debug("本地测谎 access could not be decided", exc_info=True)
            return None

    def _sync_local_lie_radio(self) -> None:
        """Grey the 本地测谎 radio when the code has no CT card.

        The radio is greyed **and not clickable** while no CT card is known - the
        operator's rule for this state: a backend the machine cannot serve must not be
        pickable.  Only its LOOK and its `state` change; the group variable is never
        assigned (forcing 关闭 read as "the assistant switched me to 关闭"), and the
        armed boolean is withdrawn so no lie window tries a backend that cannot serve
        it.  A card-bearing reply restores both the normal look and the arming.

        The look is RE-ASSERTED every tick, not only on the transition: something in
        this dashboard re-enables the row's widgets after a settings apply, and a
        one-shot `configure()` therefore left a clickable "本地测谎（未开通）" on
        screen (the operator's report).  A cheap state/text read decides whether the
        write is needed, and a mismatch is logged with the value that was found.
        """

        buttons = list(getattr(self, "_api_auto_lie_buttons", []) or [])
        if len(buttons) < 3:
            return
        access = self._local_lie_access()
        allowed = access is not False
        local = buttons[2]
        wanted_state = "normal" if allowed else self.LOCAL_LIE_GREYED_STATE
        wanted_text = "本地测谎" if allowed else "本地测谎（未开通）"
        wanted_style = "TRadiobutton" if allowed else "Greyed.TRadiobutton"
        found_state = self._radio_state(local)
        found_text = self._radio_text(local)
        transition = allowed != getattr(self, "_local_lie_radio_allowed", None)
        if transition or found_state != wanted_state or found_text != wanted_text:
            self._local_lie_radio_allowed = allowed
            try:
                local.configure(state=wanted_state, text=wanted_text,
                                style=wanted_style)
            except Exception:  # noqa: BLE001 - a radio look is never fatal
                LOG.debug("本地测谎 radio look could not be set", exc_info=True)
            if not allowed and transition:
                AUTO_LIE_LOG.warning(
                    "自动测谎: this activation code carries no CT card - 本地测谎 is "
                    "greyed and cannot be selected (%s); radio state=%s text=%s",
                    str(getattr(self, "_api_auto_lie_backend_var", None).get()),
                    self._radio_state(local),
                    self._radio_text(local),
                )
            elif not transition and not allowed:
                # Something re-enabled the widget after the fact.  Say so: a silent
                # re-assertion would hide the culprit forever.
                AUTO_LIE_LOG.warning(
                    "自动测谎: the greying was lost (%s -> %s, %r -> %r); re-applied",
                    found_state, wanted_state, found_text, wanted_text,
                )
        if not allowed and self._auto_lie_backend_is_local():
            # The radio cannot be clicked away from 本地测谎 either, so the armed
            # boolean is what must not stay on: a lie window would otherwise try a
            # backend the machine cannot serve.
            armed_var = getattr(self, "_api_auto_lie_var", None)
            if armed_var is not None and bool(armed_var.get()):
                try:
                    armed_var.set(False)
                    self._api_auto_lie_change_origin = "no_card"
                    self._set_api_test_status(
                        "自动测谎: 本地测谎未开通，已暂停（测谎卡恢复后自动继续）。"
                    )
                except Exception:  # noqa: BLE001 - never break the UI tick
                    LOG.debug("自动测谎 could not be disarmed without a card",
                              exc_info=True)
        elif (allowed
                and getattr(self, "_api_auto_lie_change_origin", "") == "no_card"
                and str(getattr(self, "_api_auto_lie_backend_var", None).get())
                == AUTOLIE_BACKEND_LOCAL):
            # The card came back while 本地测谎 was still selected: resume the same
            # way a click would, so the operator does not have to re-pick it.
            self._api_auto_lie_change_origin = ""
            AUTO_LIE_LOG.info(
                "自动测谎: the CT card returned - 本地测谎 is selectable and re-armed"
            )
            try:
                self._api_auto_lie_backend_changed()
            except Exception:  # noqa: BLE001 - never break the UI tick
                LOG.debug("本地测谎 could not be re-armed after the card returned",
                          exc_info=True)

    @staticmethod
    def _radio_state(widget) -> str:
        """The widget's ttk state, for the log (a look that did not apply is silent)."""

        try:
            return str(widget.cget("state"))
        except Exception:  # noqa: BLE001 - logging helper
            return "?"

    @staticmethod
    def _radio_text(widget) -> str:
        """The widget's label, for the log."""

        try:
            return str(widget.cget("text"))
        except Exception:  # noqa: BLE001 - logging helper
            return "?"

    def _activate_local_lie_clicked(self) -> None:
        """激活测谎: write a CT_ card for the local service and restart it.

        Only the CT_* card is asked for.  The coordinate-cloud key
        (``lie_product_key``) is the SAME secret the remote 测谎 backend uses, so
        it is taken from the licence heartbeat rather than typed in again.
        The activation itself is the service's startup validation, so the flow
        ends with a restart and a health probe.
        """

        try:
            from tkinter import simpledialog

            card = simpledialog.askstring(
                "激活测谎",
                "请输入 CT_ 测谎卡（CT_IN-xxxx）：\n"
                "坐标云密钥会自动使用在线授权下发的那一个。",
                parent=getattr(self, "_root", None),
            )
        except Exception:  # noqa: BLE001 - no window to attach to
            LOG.exception("激活测谎: the prompt could not be shown")
            return
        if card is None:
            return
        card = str(card).strip()
        if not card.upper().startswith("CT_"):
            self._report_api_test_failure(
                "激活测谎: 激活码无效，请填写 CT_ 开头的测谎卡。"
            )
            return
        product_key = ""
        try:
            product_key = str(get_server_secret() or "")
        except Exception:  # noqa: BLE001 - the key is optional here
            LOG.debug("激活测谎: no licence-heartbeat key available", exc_info=True)
        if hasattr(self, "_api_test_status"):
            self._set_api_test_status("激活测谎: 正在写入卡号并重启本地测谎服务…")
        if hasattr(self, "_local_lie_service_note"):
            self._local_lie_ready = None
        AUTO_LIE_LOG.info(
            "激活测谎: activating the local service (card=%s, ws key=%s)",
            (card[:6] + "****") if len(card) > 10 else "***",
            "set" if product_key else "missing",
        )

        def job() -> None:
            try:
                import local_lie_service

                ok, note = local_lie_service.activate(
                    application_root(__file__), card, product_key,
                )
            except Exception as exc:  # noqa: BLE001 - report, never crash
                ok, note = False, f"激活测谎失败：{exc}"
            self._local_lie_ready = bool(ok)
            self._local_lie_note = note
            self._local_lie_activate_result = (bool(ok), note)
            self._local_lie_report_pending = True

        threading.Thread(target=job, name="local-lie-activate", daemon=True).start()

    def _report_local_lie_probe(self) -> None:
        """Tk tick: show the 本地测谎 probe / 激活测谎 result off-thread work produced."""

        if not getattr(self, "_local_lie_report_pending", False):
            return
        self._local_lie_report_pending = False
        activated = getattr(self, "_local_lie_activate_result", None)
        if activated is not None:
            self._local_lie_activate_result = None
            ok, note = activated
            AUTO_LIE_LOG.info("激活测谎: %s (%s)", "ok" if ok else "failed", note)
            if ok and hasattr(self, "_api_auto_lie_backend_var"):
                # A successful activation selects 本地测谎 - that is what the
                # operator pressed the button for.  Going through the normal
                # change handler also arms 测谎 (the frame source without which
                # no lie window is ever detected) and persists the choice.
                self._api_auto_lie_backend_var.set(AUTOLIE_BACKEND_LOCAL)
                self._api_auto_lie_var.set(True)
                self._api_auto_lie_backend_changed()
            self._refresh_panel_hints()
            try:
                from tkinter import messagebox

                show = messagebox.showinfo if ok else messagebox.showerror
                show(
                    "激活测谎",
                    ("本地测谎已激活：" + note) if ok else str(note),
                    parent=getattr(self, "_root", None),
                )
            except Exception:  # noqa: BLE001 - no window to attach to
                LOG.info("激活测谎: %s", note)
            return
        if not self._auto_lie_backend_is_local():
            return
        note = getattr(self, "_local_lie_note", "") or "127.0.0.1:8765 未就绪"
        text = (
            f"自动测谎: 本地测谎已连接 - {note}"
            if self._local_lie_ready
            else f"自动测谎: 本地测谎未连接 - {note}"
        )
        if hasattr(self, "_api_test_status"):
            self._api_test_status.configure(text=text)

    def _ensure_local_lie_service(
        self, *, timeout: float = 5.0, force_probe: bool = False,
    ) -> tuple[bool, str]:
        """``(ready, note)`` for 本地测谎, probing now when nothing was probed yet.

        ``force_probe`` re-reads ``/v1/health`` even when a cached verdict exists: the
        automatic pass must know whether the service is listening RIGHT NOW, because a
        stale True sends the pass into a dead port and a stale False drops a lie event
        the service could have served.
        """

        if force_probe or self._local_lie_ready is None:
            try:
                from local_lie_backend import probe_local_lie_service

                ok, note = probe_local_lie_service(timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                ok, note = False, f"本地测谎探测失败：{exc}"
            self._local_lie_ready = bool(ok)
            self._local_lie_note = note
        else:
            ok, note = bool(self._local_lie_ready), self._local_lie_note
        return ok, note

    def _api_auto_lie_on_change(self) -> None:
        """Arm or disarm the automatic api pass, and persist it immediately."""

        origin = getattr(self, "_api_auto_lie_change_origin", "unknown")
        self._api_auto_lie_change_origin = "unknown"

        if not self._license_allowed():
            if hasattr(self, "_api_auto_lie_var"):
                self._api_auto_lie_var.set(False)
            if hasattr(self, "_api_auto_lie_backend_var"):
                self._api_auto_lie_backend_var.set(AUTOLIE_BACKEND_OFF)
            self._api_auto_lie_session_armed = False
            self._show_license_refusal()
            return
        armed = bool(self._api_auto_lie_var.get()) if hasattr(
            self, "_api_auto_lie_var") else False
        self._api_auto_lie_session_armed = armed
        # 自动过测谎 has no independent frame source: the normal 测谎 worker
        # is its prerequisite.  Selecting the dependent option must therefore
        # arm its prerequisite immediately, rather than merely displaying a
        # warning which leaves a checked 自动过测谎 box unable to ever start.
        if (armed and hasattr(self, "_lie_alert_var")
                and not bool(self._lie_alert_var.get())):
            self._lie_alert_var.set(True)
            AUTO_LIE_LOG.info("自动过测谎 enabled 测谎 automatically (required prerequisite)")
        try:
            data = self._shutdown_collect_data()
            self._shutdown_save_settings(data)
            self._shutdown_apply_to_worker(data)
        except Exception:
            AUTO_LIE_LOG.warning("自动过测谎 setting could not be saved", exc_info=True)
        if hasattr(self, "_api_test_status"):
            backend_label = self._auto_lie_backend_label()
            if not armed:
                self._api_test_status.configure(text="自动过测谎: 未启用（关闭）。")
            elif self._auto_lie_backend_is_local() and self._local_lie_ready is False:
                self._api_test_status.configure(
                    text="自动测谎: 本地测谎未连接 - "
                         f"{getattr(self, '_local_lie_note', '') or '127.0.0.1:8765 未就绪'}"
                )
            elif not self._lie_detection_armed():
                # 自动过测谎 is driven by the lie detector: without 测谎 no lie window can be seen, so the
                # pass would never start.  Say it here instead of leaving the operator with silence.
                self._api_test_status.configure(
                    text=f"自动测谎: {backend_label}已启用，但「测谎」未勾选 - "
                         "检测不到测谎窗口，不会自动过测谎。"
                )
            else:
                detail = (
                    f"（{getattr(self, '_local_lie_note', '')}）"
                    if self._auto_lie_backend_is_local()
                    and getattr(self, "_local_lie_note", "")
                    else ""
                )
                self._api_test_status.configure(
                    text=f"自动测谎: {backend_label}已启用{detail} - "
                         "未开始巡逻时也会自动抓取窗口检测并接管。"
                )
        if armed and not self._lie_detection_armed():
            AUTO_LIE_LOG.warning(
                "自动过测谎 is armed but 测谎 (lie detection) is OFF: no lie window can be detected, so "
                "the automatic pass can never start - tick 测谎 as well"
            )
        AUTO_LIE_LOG.info(
            "自动过测谎 %s (source=%s)", "armed" if armed else "disarmed", origin
        )

    def _mark_auto_lie_mouse_change(self, _event: object = None) -> None:
        """Tag the following checkbutton command for diagnostics only."""

        self._api_auto_lie_change_origin = "mouse"

    def _block_auto_lie_keyboard_toggle(self, _event: object = None) -> str:
        """Keep global Space/Enter input from changing the auto-lie setting."""

        AUTO_LIE_LOG.warning(
            "自动过测谎: ignored Space/Enter on dashboard; use the mouse checkbox"
        )
        return "break"

    def _lie_detection_armed(self) -> bool:
        """Whether the 测谎 checkbox is ticked (it is what enables the detector)."""

        var = getattr(self, "_lie_alert_var", None)
        try:
            return bool(var.get()) if var is not None else False
        except Exception:
            return False

    def _disconnect_detection_armed(self) -> bool:
        """Whether the 掉线 checkbox is ticked (it is what enables the disconnect detector).

        自动重连 reacts to that alert (``CharacterWorker`` -> ``_on_disconnect_event`` ->
        ``notify_disconnect``), so without 掉线 nothing would ever notice the disconnect and the
        reconnect could never run - not before the patrol and not during it.
        """

        var = getattr(self, "_disconnect_alert_var", None)
        try:
            return bool(var.get()) if var is not None else False
        except Exception:
            return False

    def on_lie_event_for_api(self, match: object = None) -> None:
        """Queue one detector event; safe to call from the detector thread."""

        # The detector can see a matching block on every capture. Coalesce
        # repeated present events until Tk has inspected one; otherwise a
        # loading screen can fill the queue before the UI has one chance to
        # apply the window-recreation guard.
        if match is not None:
            with self._api_auto_lie_event_enqueue_lock:
                if self._api_auto_lie_match_queued:
                    return
                try:
                    self._api_auto_lie_events.put_nowait(
                        _AutoLieDetectorEvent(match=match, detected_at=time.monotonic())
                    )
                    self._api_auto_lie_match_queued = True
                    AUTO_LIE_LOG.info(
                        "自动过测谎 timing: detector event queued for UI dispatch"
                    )
                except queue.Full:
                    return
            return
        try:
            self._api_auto_lie_events.put_nowait(None)
        except queue.Full:
            return

    def _handle_lie_event_for_api(
        self,
        match: object = None,
        *,
        detected_at: Optional[float] = None,
    ) -> None:
        """Handle one queued lie event on the UI thread.

        This owns all Tk variable/widget access.  The detector must only
        enqueue an event, otherwise a cross-thread BooleanVar read can make a
        legitimate lie window fail before the API worker is created.

        ``match is None`` is the detector reporting that the square is *gone*
        (``lie_detector_worker`` forwards ``(None, frame)`` for a cleared
        window): it closes the current window and releases the stale-square
        guard, and it must never arm a pass.  It is handled before every gate
        below, because a clear is lifecycle information rather than an
        authorization action - gating it away on the license, the 自动过测谎
        selection or the credential would leave ``event_active`` /
        ``ignore_until_clear`` stuck after a handled popup and silently disarm
        every later lie window.  Losing this branch is the v1.2.119 field bug:
        the detector's first scan after a restart finds no square, and that
        clear was read as "a new lie window armed a pass (bbox None)".
        """

        if match is None:
            # The square is gone: this window is over, and the next bbox is a
            # new event.  A pass that is merely pending still runs - the alarm
            # was real, the square only flickered.
            self._api_auto_lie_clear_since = time.monotonic()
            self._api_auto_lie_event_active = False
            self._api_auto_lie_ignore_until_clear = False
            return
        if not self._license_allowed():
            AUTO_LIE_LOG.info("自动过测谎: ignored because license is not valid")
            return
        if not hasattr(self, "_api_auto_lie_var"):
            AUTO_LIE_LOG.warning("自动过测谎: lie event ignored - the panel has no 自动过测谎 selection")
            return
        if not self._api_auto_lie_var.get():
            AUTO_LIE_LOG.warning(
                "自动过测谎: lie event ignored - the 自动过测谎 selection is OFF "
                "(tick it to pass automatically)"
            )
            return
        if not getattr(self, "_api_auto_lie_session_armed", False):
            # The detector may see the same popup in several consecutive
            # frames before the user enables this session.  It is expected,
            # not an operator-facing warning.
            AUTO_LIE_LOG.debug("自动过测谎: lie event ignored until enabled during this session")
            return
        # The credential belongs to a pass that can really start, so this check
        # sits AFTER the selection gates.  Checked first, it spent up to three
        # licensing round trips per lie window and printed 本机没有测谎密钥 for a
        # feature the operator had switched off (the field log: "selection
        # loaded as OFF" at 12:17, then the missing-key warning at 12:39 for a
        # setting nobody had enabled).
        #
        # 本地测谎 does NOT need that credential: the local service bills its own
        # CT_IN card, so an account whose cloud key has not arrived (or has none)
        # must still take a lie window over.  Without this branch the local
        # backend silently ignored every lie event - the operator's question
        # "when a lie event happens, can the local service take over?".
        local_backend = self._auto_lie_backend_is_local()
        if not local_backend and not has_server_secret():
            # Deliberately silent and deliberately passive.  A window can arrive
            # before the credential does - every session starts with none, and
            # the heartbeat runs only at startup, on its own schedule, and from
            # the caption refresh - so this is a normal state, not an incident.
            # Nothing is written to the auto-lie log and no heartbeat is
            # requested here: the window is answered by hand.
            return
        now = time.monotonic()
        if getattr(self, "_api_auto_lie_ignore_until_clear", False):
            # A prior server-close timeout was recorded successfully.  Keep
            # ignoring this *same* visual until detector reports it gone,
            # then the following popup can start a fresh pass.
            return
        if getattr(self, "_api_auto_lie_event_active", False):
            return                                   # this window is already being handled
        self._api_auto_lie_event_active = True
        if self._worker_is_running(getattr(self, "_api_lie_pass_worker", None)):
            # ONE pass per window: the pass that is running was started for this very window (the
            # square only flickers, the window is still on screen), so a second pass would spend a
            # second api round for the same test.  The operator's rule: "the first alarm will be
            # accepted, then the autolie_api takes over, the second one will be ignored".
            AUTO_LIE_LOG.warning("自动过测谎: a pass is ALREADY handling this lie window - this event is "
                        "ignored (one pass per window; a pass is running)")
            if hasattr(self, "_api_test_status"):
                self._api_test_status.configure(
                    text="自动过测谎: 正在处理本测谎窗口，重复事件已忽略。"
                )
            return
        since_last = now - getattr(self, "_api_auto_lie_last_pass_started", 0.0)
        if since_last < AUTO_LIE_MIN_PASS_GAP_SECONDS:
            AUTO_LIE_LOG.warning(
                "自动过测谎: a lie window appeared %.0fs after the previous pass started (minimum "
                "%.0fs apart) - NO pass is started for it (the quota is protected this way, not by "
                "silently ignoring windows)", since_last, AUTO_LIE_MIN_PASS_GAP_SECONDS,
            )
            if hasattr(self, "_api_test_status"):
                self._api_test_status.configure(
                    text=f"自动过测谎: 测谎窗口出现，但距上次通行仅 {since_last:.0f}s（最小 "
                         f"{AUTO_LIE_MIN_PASS_GAP_SECONDS:.0f}s），本次不通行。"
                )
            return
        self._api_auto_lie_pending = True
        self._api_auto_lie_pending_since = (
            float(detected_at) if detected_at is not None else now
        )
        self._api_auto_lie_wait_logged = False
        AUTO_LIE_LOG.warning(
            "自动过测谎: a new lie window armed a pass (bbox %s, previous pass %.0fs ago, "
            "detector-to-arm=%.0fms)",
            match, since_last, (now - self._api_auto_lie_pending_since) * 1000.0,
        )

    @staticmethod
    def _worker_is_running(worker: Any) -> bool:
        """Whether a pass worker is busy - ``running`` is a property on some, a method on others.

        ``ApiLieTestWorker.running`` and ``VideoDrillWorker.running`` are ``@property`` bools;
        treating one as a callable raised ``TypeError: 'bool' object is not callable`` on EVERY poll
        tick, and because the pending flag is cleared only after this check it could never clear.
        An unreadable state counts as "busy": a second pass must never be started blindly.
        """

        if worker is None:
            return False
        state = getattr(worker, "running", False)
        if callable(state):
            try:
                return bool(state())
            except Exception:
                AUTO_LIE_LOG.warning("自动过测谎: worker.running() failed", exc_info=True)
                return True
        return bool(state)

    def _service_api_auto_lie(self) -> None:
        """Start the api pass for a pending lie event (Tk thread)."""

        # The detector thread only queues events.  Consume them here before
        # deciding whether a pass is pending, so every selection/license/UI
        # read happens on Tk's owning thread.
        events = getattr(self, "_api_auto_lie_events", None)
        if events is not None:
            while True:
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    break
                if isinstance(event, _AutoLieDetectorEvent):
                    match = event.match
                    detected_at = event.detected_at
                else:
                    # ``None`` clear events and old/test callers remain
                    # compatible with the original queue payload.
                    match = event
                    detected_at = None
                if match is not None:
                    with self._api_auto_lie_event_enqueue_lock:
                        self._api_auto_lie_match_queued = False
                    if detected_at is not None:
                        AUTO_LIE_LOG.info(
                            "自动过测谎 timing: detector-to-UI-dequeue=%.0fms",
                            (time.monotonic() - float(detected_at)) * 1000.0,
                        )
                self._handle_lie_event_for_api(match, detected_at=detected_at)
                try:
                    events.task_done()
                except ValueError:
                    pass

        # A pass that WE started for a lie window pauses the patrol (the lie test freezes the
        # character, so the movement worker otherwise counts it as stuck and fires its self-rescue
        # mid-test - the operator's v1.0.26 log: "SELF-RESCUE: character stationary on patrol route
        # for 20 frames" twice inside one lie window, restarting the patrol and walking the character
        # while the api pass was supposed to have the machine).  Resume it as soon as the pass is done.
        # The pass lifecycle is concluded for every automatic pass, not only for
        # one that paused a patrol: a lie test can run while the character just
        # stands still (nothing to pause), and its dialog still has to be closed
        # by the confirmation click.  ``_resume_patrol_after_api_pass`` is a
        # no-op unless this pass really paused a patrol.
        pass_worker = getattr(self, "_api_lie_pass_worker", None)
        if pass_worker is not None and not self._worker_is_running(pass_worker):
            outcome_state = getattr(self, "_auto_lie_outcome_state", None)
            watching_outcome = bool(
                outcome_state
                and outcome_state.get("worker_id") == id(self._api_lie_pass_worker)
                and not outcome_state.get("terminal")
                and not outcome_state.get("timed_out")
            )
            completed = bool(getattr(
                self._api_lie_pass_worker, "completed_successfully", False
            ))
            terminal_outcome = str(outcome_state.get("terminal") or "") if outcome_state else ""
            transport_waiting_for_visual_outcome = bool(
                getattr(self._api_lie_pass_worker, "transport_closed", False)
                and getattr(self._api_lie_pass_worker, "feed_started", False)
                and not terminal_outcome
            )
            if terminal_outcome == "success":
                # A server-close timeout is deliberately accounted as success
                # without clicking an unverified dialog.  It must still clear
                # the paused-pass lifecycle so the following lie window can
                # be handled.
                if outcome_state and outcome_state.get("skip_confirmation"):
                    self._resume_patrol_after_api_pass()
                elif self._api_auto_lie_post_confirm_complete:
                    self._resume_patrol_after_api_pass()
            elif terminal_outcome == "failed_marker_missing":
                self._api_auto_lie_failed_handled = True
            elif completed and not self._api_auto_lie_post_confirm_scheduled:
                self._begin_auto_lie_post_confirm()
            elif completed and self._api_auto_lie_post_confirm_complete:
                self._resume_patrol_after_api_pass()
            elif watching_outcome or transport_waiting_for_visual_outcome:
                # A server-initiated close is transport state, not a game
                # outcome.  Keep the visual observer alive through its 40 s
                # window so the same success/failure rules still decide the
                # accounting result and whether the confirmation is clicked.
                pass
            elif not completed and not getattr(self, "_api_auto_lie_failed_handled", False):
                # A WebSocket failure does not prove that the lie dialog has
                # completed.  Clicking its fixed confirmation point, or
                # resuming patrol beneath it, would operate the wrong game
                # state.  Leave it for the operator to cancel/retry safely.
                self._api_auto_lie_failed_handled = True
                AUTO_LIE_LOG.warning(
                    "自动过测谎: pass ended before completion; confirmation click and patrol resume are skipped"
                )
                if hasattr(self, "_api_test_status"):
                    self._api_test_status.configure(
                        text="自动过测谎: 后端中断，未点击确认也未恢复运行；请按 Esc 处理。"
                    )
            # A selection change asked for the 本地测谎 service to stop while this
            # pass was still using it.  Now that the pass is over, honour it.
            if getattr(self, "_local_lie_stop_pending", False):
                self._local_lie_stop_pending = False
                if not self._auto_lie_backend_is_local():
                    try:
                        import local_lie_service

                        local_lie_service.supervisor_set_active(
                            False, application_root(__file__)
                        )
                        AUTO_LIE_LOG.info(
                            "自动测谎: 本地测谎 service stopped now that the pass ended"
                        )
                    except Exception:  # noqa: BLE001 - service switching is best effort
                        LOG.debug("本地测谎 deferred stop failed", exc_info=True)
        if not self._api_auto_lie_pending:
            return
        # One pass at a time.  A pending event waits for the running one, but not forever: a worker
        # that never comes back must not keep the flag set and this method retrying every tick.
        if self._worker_is_running(self._api_lie_pass_worker):
            if not getattr(self, "_api_auto_lie_wait_logged", False):
                self._api_auto_lie_wait_logged = True
                AUTO_LIE_LOG.info("自动过测谎: a pass is still running; this lie event waits for it")
            waited = time.monotonic() - getattr(
                self, "_api_auto_lie_pending_since", time.monotonic()
            )
            if waited >= AUTO_LIE_PENDING_MAX_SECONDS:
                self._api_auto_lie_pending = False
                AUTO_LIE_LOG.warning(
                    "自动过测谎: dropping a lie event that waited %.0fs for the running pass",
                    waited,
                )
            return
        factory = getattr(self, "api_auto_lie_factory", None)
        if factory is None:
            self._api_auto_lie_pending = False
            AUTO_LIE_LOG.warning(
                "自动过测谎: this build has no api pass factory - the lie event is dropped"
            )
            if hasattr(self, "_api_test_status"):
                self._api_test_status.configure(
                    text="自动过测谎: 本机助手未启用该工作线程。")
            return
        since_last = time.monotonic() - getattr(
            self, "_api_auto_lie_last_pass_started", 0.0
        )
        if since_last < AUTO_LIE_MIN_PASS_GAP_SECONDS:
            self._api_auto_lie_pending = False
            AUTO_LIE_LOG.info(
                "自动过测谎: pass skipped - the previous pass started %.0fs ago "
                "(minimum %.0fs apart)",
                since_last, AUTO_LIE_MIN_PASS_GAP_SECONDS,
            )
            return
        local_backend = self._auto_lie_backend_is_local()
        if local_backend:
            # Ask the port directly: a cached verdict is not evidence for THIS
            # window (a stale True aims the pass at a dead port).
            ready, note = self._ensure_local_lie_service(force_probe=True)
            if not ready:
                # The service is normally pre-started and supervised, but a real lie
                # window can arrive during a cold start or a restart.  Dropping the
                # event loses that window outright, so keep it pending, ask for a
                # start, and retry on the next UI tick (bounded below like any other
                # waiting event).
                waited = time.monotonic() - getattr(
                    self, "_api_auto_lie_pending_since", time.monotonic()
                )
                if waited >= AUTO_LIE_PENDING_MAX_SECONDS:
                    self._api_auto_lie_pending = False
                    AUTO_LIE_LOG.warning(
                        "自动过测谎 (本地测谎): the service stayed down for %.0fs "
                        "(%s) - dropping this lie window", waited, note,
                    )
                    return
                self._api_auto_lie_pending = True
                requested = self._local_lie_request_start(
                    reason="a lie window is waiting"
                )
                if not getattr(self, "_api_auto_lie_service_wait_logged", False):
                    self._api_auto_lie_service_wait_logged = True
                    AUTO_LIE_LOG.warning(
                        "自动过测谎 (本地测谎): the service is not listening (%s); "
                        "keeping the lie window and retrying%s",
                        note, " (start requested)" if requested else "",
                    )
                if hasattr(self, "_api_test_status"):
                    self._api_test_status.configure(
                        text=f"自动测谎（本地测谎）: 服务启动中，就绪后立即处理 - {note}"
                    )
                return
            self._api_auto_lie_service_wait_logged = False
        self._api_auto_lie_pending = False
        try:
            worker = factory(backend=AUTOLIE_BACKEND_LOCAL) if local_backend else factory()
        except Exception as exc:
            AUTO_LIE_LOG.warning("自动过测谎 could not start", exc_info=True)
            if hasattr(self, "_api_test_status"):
                self._api_test_status.configure(text=f"自动过测谎: 启动失败（{exc}）")
            return
        feed_anchor_setter = getattr(worker, "set_feed_start_from_detection", None)
        if callable(feed_anchor_setter):
            feed_anchor_setter(
                getattr(self, "_api_auto_lie_pending_since", time.monotonic())
            )
        setter = getattr(worker, "set_on_backend_ready", None)
        if callable(setter):
            setter(self._begin_auto_lie_accounting_event)
        pause_started = time.perf_counter()
        self._pause_patrol_for_api_pass()
        pause_elapsed = time.perf_counter() - pause_started
        self._api_lie_pass_worker = worker
        # Recorded here, before the worker runs: this is the backend the pass is
        # actually served by, so its accounting cannot be diverted by a selection
        # change (or by 本地测谎 becoming unavailable) while the pass is in flight.
        self._api_lie_pass_local_backend = bool(local_backend)
        self._api_auto_lie_last_pass_started = time.monotonic()
        self._api_auto_lie_wait_logged = False
        self._api_auto_lie_runs += 1
        try:
            worker.start()
        except Exception as exc:
            AUTO_LIE_LOG.warning("自动过测谎 worker could not be started", exc_info=True)
            if hasattr(self, "_api_test_status"):
                self._api_test_status.configure(text=f"自动过测谎: 启动失败（{exc}）")
            return
        detected_at = getattr(self, "_api_auto_lie_pending_since", None)
        if isinstance(detected_at, (float, int)):
            AUTO_LIE_LOG.info(
                "自动过测谎 timing: detector-to-worker-start=%.0fms, patrol-pause=%.0fms",
                (time.monotonic() - float(detected_at)) * 1000.0,
                pause_elapsed * 1000.0,
            )
        AUTO_LIE_LOG.info("自动过测谎: pass %d started for a lie window", self._api_auto_lie_runs)
        if hasattr(self, "_api_test_status"):
            self._api_test_status.configure(
                text=f"自动过测谎: 测谎窗口出现，正在处理（第 {self._api_auto_lie_runs} 次）…"
            )

    def _local_lie_request_start(self, *, reason: str = "") -> bool:
        """Ask the 本地测谎 service to start, at most once every few seconds.

        The supervisor already pre-starts it whenever 本地测谎 is selected; this is the
        direct request the automatic pass makes when a real lie window finds the port
        closed (a restart that has not finished, or a supervisor that backed off).
        Returns True when a start was requested.
        """

        now = time.monotonic()
        last = getattr(self, "_local_lie_start_requested_at", 0.0)
        if now - last < 3.0:
            return False
        self._local_lie_start_requested_at = now
        root = None
        try:
            from runtime_paths import application_root

            root = application_root(__file__)
        except Exception:  # noqa: BLE001 - the module default is fine
            root = None

        def _start() -> None:
            try:
                import local_lie_service

                ok, note = local_lie_service.ensure_started(root)
                AUTO_LIE_LOG.info(
                    "自动过测谎 (本地测谎): start requested%s -> %s (%s)",
                    f" ({reason})" if reason else "",
                    "running" if ok else "not yet",
                    note,
                )
            except Exception:  # noqa: BLE001 - a start request never breaks a pass
                AUTO_LIE_LOG.debug("本地测谎 start request failed", exc_info=True)

        threading.Thread(
            target=_start, name="local-lie-start-request", daemon=True,
        ).start()
        return True

    def _pause_patrol_for_api_pass(self) -> None:
        """Stand the patrol down while the automatic api pass answers the lie test.

        The lie test freezes the character, and a frozen character on the patrol route is exactly what
        the movement worker's self-rescue reacts to - it restarted the patrol and walked the character
        in the middle of the test.  The patrol is only resumed when it was running before, so a pass
        never starts a patrol the operator did not start.
        """

        self._api_auto_lie_patrol_paused = False
        self._api_auto_lie_post_confirm_scheduled = False
        self._api_auto_lie_post_confirm_complete = False
        self._api_auto_lie_failed_handled = False
        self._auto_lie_outcome_state = None
        controller = getattr(self, "patrol_controller", None)
        if controller is not None and controller.is_enabled():
            try:
                controller.set_enabled(False)
                self._api_auto_lie_patrol_paused = True
                self._refresh_patrol_controls()
                AUTO_LIE_LOG.warning("自动过测谎: patrol paused for the api pass (it resumes when the pass "
                            "finishes)")
            except Exception:
                AUTO_LIE_LOG.warning("自动过测谎: the patrol could not be paused for the pass", exc_info=True)
        event = getattr(self, "automation_active_event", None)
        if event is not None:
            try:
                event.clear()
            except Exception:
                AUTO_LIE_LOG.debug("自动过测谎: the automation switch could not be cleared", exc_info=True)

    def _resume_patrol_after_api_pass(self) -> None:
        """Put the patrol back after the automatic api pass finished."""

        was_paused = getattr(self, "_api_auto_lie_patrol_paused", False)
        self._api_auto_lie_patrol_paused = False
        if not was_paused:
            return
        controller = getattr(self, "patrol_controller", None)
        if controller is not None:
            try:
                controller.set_enabled(True)
                self._refresh_patrol_controls()
            except Exception:
                AUTO_LIE_LOG.warning("自动过测谎: the patrol could not be resumed", exc_info=True)
        event = getattr(self, "automation_active_event", None)
        if event is not None:
            try:
                event.set()
            except Exception:
                AUTO_LIE_LOG.debug("自动过测谎: the automation switch could not be set", exc_info=True)
        AUTO_LIE_LOG.warning("自动过测谎: the api pass is done - the patrol resumes where it was")

    def _auto_lie_success_box_visible(self) -> bool:
        """Whether the newest shared capture really shows the success box.

        A policy success must not become a blind fixed click (the operator's
        rule), but leaving a finished dialog on screen blocks the character.
        This is the evidence that separates the two cases: the 40x40 success
        field is measured in the latest frame, exactly as the outcome monitor
        measures it.
        """

        frame = getattr(self, "_latest_capture_frame", None)
        matcher = getattr(self, "_auto_lie_success_box", None)
        image = getattr(frame, "image", None)
        if image is None or matcher is None:
            return False
        try:
            return float(matcher.score(image)) >= SUCCESS_THRESHOLD
        except Exception:
            AUTO_LIE_LOG.debug("自动过测谎: success box check failed", exc_info=True)
            return False

    def _begin_auto_lie_post_confirm(self) -> None:
        """Click the completed automatic lie dialog's measured confirm point."""

        self._api_auto_lie_post_confirm_scheduled = True
        self._api_auto_lie_post_confirm_complete = False
        sender = getattr(getattr(self, "status_worker", None), "key_sender", None)
        click_confirm = self._click_auto_lie_confirmation
        try:
            if click_confirm(sender) is False:
                AUTO_LIE_LOG.warning("自动过测谎: post-pass confirmation click was refused")
        except Exception:
            AUTO_LIE_LOG.warning("自动过测谎: post-pass confirmation click failed", exc_info=True)
        finally:
            self._api_auto_lie_post_confirm_complete = True
        AUTO_LIE_LOG.info("自动过测谎: pass finished; confirmation click sent; patrol may resume")

    @staticmethod
    def _click_auto_lie_confirmation(sender: Any) -> bool:
        """Focus and verify the game before clicking the auto-lie confirm point."""

        try:
            select_window = getattr(sender, "select_window", None)
            is_foreground = getattr(sender, "is_game_foreground", None)
            if not callable(select_window):
                AUTO_LIE_LOG.warning("自动过测谎: post-pass confirmation click skipped; game window selector unavailable")
                return False
            focused = False
            for attempt in range(1, _AUTO_LIE_CONFIRM_FOCUS_ATTEMPTS + 1):
                if select_window() is False:
                    AUTO_LIE_LOG.warning(
                        "自动过测谎: post-pass confirmation focus attempt %d/%d failed",
                        attempt, _AUTO_LIE_CONFIRM_FOCUS_ATTEMPTS,
                    )
                    continue
                time.sleep(_AUTO_LIE_CONFIRM_FOCUS_SETTLE_SECONDS)
                # A test/dry-run sender may not expose this predicate.  A real
                # sender must prove that the game, rather than the dashboard or
                # the departing takeover window, owns foreground before click.
                if not callable(is_foreground) or is_foreground():
                    focused = True
                    AUTO_LIE_LOG.info(
                        "自动过测谎: game foreground verified before confirmation click "
                        "(attempt %d/%d)",
                        attempt, _AUTO_LIE_CONFIRM_FOCUS_ATTEMPTS,
                    )
                    break
                AUTO_LIE_LOG.warning(
                    "自动过测谎: game did not retain foreground before confirmation click "
                    "(attempt %d/%d)",
                    attempt, _AUTO_LIE_CONFIRM_FOCUS_ATTEMPTS,
                )
            if not focused:
                AUTO_LIE_LOG.warning("自动过测谎: post-pass confirmation click skipped; game window is not foreground")
                return False
            hwnd = int(getattr(sender, "hwnd", 0) or 0)
            if not hwnd:
                AUTO_LIE_LOG.warning("自动过测谎: post-pass confirmation click skipped; game handle unavailable")
                return False
            import win32gui

            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            width, height = int(right - left), int(bottom - top)
            if width <= 0 or height <= 0:
                AUTO_LIE_LOG.warning("自动过测谎: post-pass confirmation click skipped; invalid client size")
                return False
            origin_x, origin_y = win32gui.ClientToScreen(hwnd, (0, 0))
            clicks: list[tuple[int, int, int, int]] = []
            for client_x, client_y in _auto_lie_confirm_client_points(width, height):
                screen_x = int(origin_x) + client_x
                screen_y = int(origin_y) + client_y
                for _ in range(_AUTO_LIE_CONFIRM_CLICKS_PER_POINT):
                    if not click_screen(screen_x, screen_y, keep_focus=hwnd):
                        AUTO_LIE_LOG.warning(
                            "自动过测谎: confirmation click failed at client=(%d,%d) "
                            "screen=(%d,%d)",
                            client_x, client_y, screen_x, screen_y,
                        )
                        return False
                    clicks.append((client_x, client_y, screen_x, screen_y))
            AUTO_LIE_LOG.info(
                "自动过测谎: clicked both confirmation points twice "
                "client=%s client_size=%dx%d",
                [(x, y) for x, y, _screen_x, _screen_y in clicks],
                width, height,
            )
            return True
        except Exception:
            AUTO_LIE_LOG.warning("自动过测谎: post-pass confirmation point could not be clicked", exc_info=True)
            return False

    def _drain_api_auto_lie_results(self) -> None:
        """Report the automatic pass into the shared hint area."""

        worker = self._api_lie_pass_worker
        if worker is None:
            return
        results = getattr(worker, "results", None)
        if results is None:
            return
        received: list[tuple[str, str]] = []
        while True:
            try:
                item = results.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break
            if isinstance(item, tuple) and len(item) == 2:
                received.append((str(item[0]), str(item[1])))
            else:
                AUTO_LIE_LOG.warning("自动过测谎 result ignored: unexpected item %r", item)
        for state, detail in received:
            text = f"{state}: {detail}" if detail else str(state)
            # Keep the independent auto_lie.log useful after the handoff too:
            # progress/results originate in the worker queue, rather than all
            # being emitted by the worker's general-purpose logger.
            AUTO_LIE_LOG.info("自动过测谎: worker result %s", text)
            if hasattr(self, "_api_test_status"):
                self._configure_text_if_changed(
                    self._api_test_status, "api_test_status", f"自动过测谎: {text}"
                )
            if state == "feed_started":
                self._arm_auto_lie_outcome_monitor(worker)
            elif state == "transport_closed":
                state_data = self._auto_lie_outcome_state
                if state_data is not None and state_data.get("worker_id") == id(worker):
                    state_data["transport_closed"] = True
                    AUTO_LIE_LOG.warning(
                        "自动过测谎: 后端先关闭连接；继续按成功图标/小地图缺失规则判断结果"
                    )

    def _arm_auto_lie_outcome_monitor(self, worker: Any) -> None:
        """Begin result observation after the worker's first live frame is due."""

        now = time.monotonic()
        self._auto_lie_outcome_state = {
            "worker_id": id(worker),
            "started_at": now,
            "not_before": now + _AUTO_LIE_OUTCOME_ARM_SECONDS,
            "deadline": now + _AUTO_LIE_OUTCOME_OBSERVE_SECONDS,
            "last_frame_sequence": -1,
            "last_marker_sequence": -1,
            "success_hits": 0,
            "missing": 0,
            "terminal": "",
            "accounted": False,
            "timed_out": False,
            "transport_closed": False,
        }
        AUTO_LIE_LOG.info(
            "自动过测谎: outcome observation armed after %.0fs; success/failure window %.0f-%.0fs",
            _AUTO_LIE_OUTCOME_ARM_SECONDS,
            _AUTO_LIE_OUTCOME_ARM_SECONDS,
            _AUTO_LIE_OUTCOME_OBSERVE_SECONDS,
        )

    def _finish_auto_lie_outcome(
        self,
        state: dict[str, Any],
        outcome: str,
        *,
        click_confirmation: bool = True,
    ) -> None:
        """Commit one visual outcome without waiting on socket/accounting work."""

        if state.get("terminal"):
            return
        state["terminal"] = outcome
        state["skip_confirmation"] = not click_confirmation
        worker = self._api_lie_pass_worker
        if worker is not None and id(worker) == state.get("worker_id"):
            request = getattr(worker, "request_terminal_outcome", None)
            if callable(request):
                try:
                    request(outcome)
                except Exception:
                    AUTO_LIE_LOG.warning("自动过测谎: could not stop stream after %s", outcome,
                                         exc_info=True)
            release_capture = getattr(worker, "release_outcome_capture", None)
            if callable(release_capture):
                release_capture()
        event_id = str(getattr(worker, "accounting_event_id", "") or "")
        if event_id and not state.get("accounted"):
            if self._pass_served_by_local():
                # 本地测谎: report the outcome to the local-service statistics
                # endpoint instead of the cloud accounting queue (the local pass
                # is billed by the CT service, not by 测谎次数).
                try:
                    import local_lie_service

                    local_lie_service.report_local_pass(
                        getattr(self, "_local_lie_event_id", "") or event_id,
                        "success" if outcome == "success" else "failed",
                        frames=int(getattr(self, "_api_auto_lie_runs", 0) or 0),
                        detail=str(outcome),
                    )
                    state["accounted"] = True
                    AUTO_LIE_LOG.info(
                        "自动过测谎: 本地测谎 outcome %s reported event=%s",
                        outcome, event_id[:8],
                    )
                except Exception:  # noqa: BLE001 - statistics never block a pass
                    AUTO_LIE_LOG.debug("本地测谎 outcome report failed", exc_info=True)
            else:
                try:
                    self._lie_accounting_worker.finalize_event(event_id, outcome)
                    state["accounted"] = True
                    AUTO_LIE_LOG.info("自动过测谎: visual %s; final accounting queued event=%s",
                                      outcome, event_id[:8])
                except Exception:
                    AUTO_LIE_LOG.warning("自动过测谎: could not queue visual %s event=%s",
                                         outcome, event_id[:8], exc_info=True)
        if outcome == "success":
            clicked = self._api_auto_lie_post_confirm_scheduled
            if not clicked and click_confirmation:
                self._begin_auto_lie_post_confirm()
                clicked = True
            elif not clicked and self._auto_lie_success_box_visible():
                # A policy success (the server closed and the observation window
                # produced no verdict) used to leave the dialog standing, so the
                # character stayed on the prompt until the operator clicked it.
                # The blind fixed click stays forbidden, so this one is gated on
                # evidence: the success box is on screen in the newest capture,
                # which means the dialog really is up.
                AUTO_LIE_LOG.warning(
                    "自动过测谎: 按规则成功记账；最新画面仍显示成功图标，补点确认关闭窗口"
                )
                self._begin_auto_lie_post_confirm()
                clicked = True
            if hasattr(self, "_api_test_status"):
                self._api_test_status.configure(
                    text=(
                        "自动过测谎: 检测到成功，正在点击确认…"
                        if clicked
                        else "自动过测谎: 后端已关闭且未见失败，按规则成功记账；"
                             "画面中没有成功图标，未点击确认。"
                    )
                )
        else:
            self._api_auto_lie_failed_handled = True
            if hasattr(self, "_api_test_status"):
                self._api_test_status.configure(text="自动过测谎: 小地图标记持续丢失，判定失败。")

    def _service_auto_lie_outcome_monitor(self) -> None:
        """Observe success image and minimap failure independently of the socket."""

        state = self._auto_lie_outcome_state
        if not state or state.get("terminal") or state.get("timed_out"):
            return
        if time.monotonic() < float(state["not_before"]):
            return
        if time.monotonic() >= float(state["deadline"]):
            worker = self._api_lie_pass_worker
            server_closed = bool(
                state.get("transport_closed")
                or (
                    worker is not None
                    and id(worker) == state.get("worker_id")
                    and getattr(worker, "transport_closed", False)
                )
            )
            if server_closed:
                # The server explicitly ended its stream and 40 seconds of
                # independent visual observation found neither the success
                # panel nor the black-room failure.  The agreed policy is to
                # settle this one as success, without an unsafe fixed click.
                self._api_auto_lie_event_active = False
                self._api_auto_lie_ignore_until_clear = True
                AUTO_LIE_LOG.warning(
                    "自动过测谎: 后端已关闭，%.0fs 内未检测到成功或失败；按规则成功记账，"
                    "等待当前窗口消失后才允许下一轮",
                    _AUTO_LIE_OUTCOME_OBSERVE_SECONDS,
                )
                self._finish_auto_lie_outcome(
                    state,
                    "success",
                    click_confirmation=False,
                )
                return
            # A live socket is not enough evidence to keep this pass alive
            # forever.  Bound the ownership window so a silent server cannot
            # retain capture/mouse ownership and block every later lie event.
            # Unlike an explicit server close, this is not accounted as
            # success: leave the durable event pending for the established
            # heartbeat/server settlement policy and keep patrol paused.
            state["timed_out"] = True
            self._api_auto_lie_event_active = False
            self._api_auto_lie_ignore_until_clear = True
            self._api_auto_lie_failed_handled = True
            if worker is not None and id(worker) == state.get("worker_id"):
                request_stop = getattr(worker, "request_stop", None)
                if callable(request_stop):
                    request_stop()
                release_capture = getattr(worker, "release_outcome_capture", None)
                if callable(release_capture):
                    release_capture()
            AUTO_LIE_LOG.warning(
                "自动过测谎: %.0fs 内未检测到成功或失败，已停止数据推送；巡逻保持暂停，"
                "等待当前窗口消失后才允许下一轮",
                _AUTO_LIE_OUTCOME_OBSERVE_SECONDS,
            )
            if hasattr(self, "_api_test_status"):
                self._api_test_status.configure(
                    text="自动过测谎: 结果超时，已停止接管；巡逻保持暂停，请按 Esc 处理。"
                )
            return

        frame = getattr(self, "_latest_capture_frame", None)
        sequence = int(getattr(frame, "sequence", -1))
        if sequence > int(state["last_frame_sequence"]):
            state["last_frame_sequence"] = sequence
            matcher = getattr(self, "_auto_lie_success_box", None)
            if matcher is not None:
                try:
                    score = float(matcher.score(getattr(frame, "image", None)))
                except Exception:
                    score = 0.0
                    AUTO_LIE_LOG.debug("自动过测谎: success blue-box check failed", exc_info=True)
                if score >= SUCCESS_THRESHOLD:
                    state["success_hits"] = int(state["success_hits"]) + 1
                    if int(state["success_hits"]) >= SUCCESS_CONSECUTIVE_FRAMES:
                        AUTO_LIE_LOG.info("自动过测谎: success blue-box confirmed score=%.3f", score)
                        self._finish_auto_lie_outcome(state, "success")
                        return
                else:
                    state["success_hits"] = 0

        snapshot = self.last_snapshot
        snapshot_sequence = int(getattr(snapshot, "sequence", -1))
        if snapshot_sequence <= int(state["last_marker_sequence"]):
            return
        state["last_marker_sequence"] = snapshot_sequence
        if float(getattr(snapshot, "marker_confidence", 0.0)) > 0.0:
            state["missing"] = 0
            return
        state["missing"] = int(state["missing"]) + 1
        if int(state["missing"]) >= _AUTO_LIE_FAILURE_MISSING_FRAMES:
            AUTO_LIE_LOG.warning("自动过测谎: black-room marker loss confirmed during outcome watch")
            self._finish_auto_lie_outcome(state, "failed_marker_missing")

    def _pass_served_by_local(self) -> bool:
        """Which backend served the CURRENT pass, decided when it started.

        Reading the 自动测谎 radio at outcome time was wrong: switching the group
        (or losing the CT card, which greys 本地测谎 and disarms it) while a pass is
        running sent a local pass's outcome to the cloud accounting queue - the
        local row stayed pending for ever and the cloud was charged - or reported a
        cloud pass to the local statistics.
        """

        recorded = getattr(self, "_api_lie_pass_local_backend", None)
        if recorded is None:
            return self._auto_lie_backend_is_local()
        return bool(recorded)

    def _begin_auto_lie_accounting_event(self) -> Optional[str]:
        """Persist the server lifecycle record when the WS handshake is ready."""

        if self._pass_served_by_local():
            # 本地测谎 is billed by the CT service, so the cloud accounting row
            # would be wrong; report the pass to the local-service endpoint
            # instead (statistics only) and keep its event id for the outcome.
            import uuid as _uuid

            event_id = str(_uuid.uuid4())
            self._local_lie_event_id = event_id
            self._local_lie_event_started = time.monotonic()
            try:
                import local_lie_service

                local_lie_service.report_local_pass(event_id, "pending")
            except Exception:  # noqa: BLE001 - statistics never block a pass
                AUTO_LIE_LOG.debug("本地测谎 start report failed", exc_info=True)
            AUTO_LIE_LOG.info(
                "自动过测谎: 本地测谎 pass reported as pending event=%s", event_id[:8]
            )
            return event_id
        try:
            event_id = self._lie_accounting_worker.begin_event()
            AUTO_LIE_LOG.info(
                "自动过测谎: WebSocket ready; start accounting queued event=%s",
                event_id[:8],
            )
            return event_id
        except Exception:
            AUTO_LIE_LOG.warning(
                "自动过测谎: could not persist handshake accounting event",
                exc_info=True,
            )
            return None

    def _arm_auto_lie_marker_verification(self, event_id: str) -> None:
        """Watch fresh minimap reads for the post-takeover black-room state."""

        # Continuous debug snapshots are intentionally off during ordinary
        # play.  Ignore any old on-demand/recording snapshot and require a
        # capture that arrives after this verification is armed.
        latest_frame = getattr(self, "_latest_capture_frame", None)
        latest_sequence = int(getattr(latest_frame, "sequence", -1))
        self._auto_lie_marker_verification = {
            "event_id": str(event_id),
            "not_before": time.monotonic() + _AUTO_LIE_FAILURE_GRACE_SECONDS,
            "last_sequence": latest_sequence,
            "missing": 0,
        }
        AUTO_LIE_LOG.info(
            "自动过测谎: black-room marker verification armed event=%s",
            str(event_id)[:8],
        )

    def _service_auto_lie_marker_verification(self) -> None:
        """Report only persistent post-pass marker loss as a true failure."""

        state = self._auto_lie_marker_verification
        if not state or time.monotonic() < float(state["not_before"]):
            return
        snapshot = self.last_snapshot
        if snapshot is None:
            return
        sequence = int(getattr(snapshot, "sequence", -1))
        if sequence <= int(state["last_sequence"]):
            return
        state["last_sequence"] = sequence
        if float(getattr(snapshot, "marker_confidence", 0.0)) > 0.0:
            self._auto_lie_marker_verification = None
            try:
                self._lie_accounting_worker.finalize_event(
                    str(state["event_id"]), "success",
                )
                AUTO_LIE_LOG.info(
                    "自动过测谎: marker returned; success queued event=%s",
                    str(state["event_id"])[:8],
                )
            except Exception:
                AUTO_LIE_LOG.warning(
                    "自动过测谎: could not queue marker-return success event=%s",
                    str(state["event_id"])[:8], exc_info=True,
                )
            return
        state["missing"] = int(state["missing"]) + 1
        if int(state["missing"]) < _AUTO_LIE_FAILURE_MISSING_FRAMES:
            return
        event_id = str(state["event_id"])
        self._auto_lie_marker_verification = None
        try:
            self._lie_accounting_worker.finalize_event(
                event_id, "failed_marker_missing",
            )
            AUTO_LIE_LOG.warning(
                "自动过测谎: black-room marker loss confirmed; failure queued event=%s",
                event_id[:8],
            )
        except Exception:
            AUTO_LIE_LOG.warning(
                "自动过测谎: could not queue black-room failure event=%s",
                event_id[:8], exc_info=True,
            )

    def _enqueue_auto_lie_accounting(self, source: str) -> None:
        """Durably report one completed API use as success without delay.

        Both the automatic game pass and the manual video drill use this
        isolated post-pass path.  It never blocks their live WebSocket work.
        """

        try:
            event_id = self._lie_accounting_worker.begin_event()
            self._lie_accounting_worker.finalize_event(event_id, "success")
            AUTO_LIE_LOG.info(
                "自动过测谎: %s completed; immediate success accounting event=%s",
                source, event_id[:8],
            )
        except Exception:
            # The completed pass must remain independent from a local
            # persistence problem; a later run can still operate normally.
            AUTO_LIE_LOG.warning("自动过测谎: immediate accounting could not be queued", exc_info=True)

    def auto_lie_pass_active(self) -> bool:
        """Whether an automatic API lie pass may be stopped by Esc."""

        worker = getattr(self, "_api_lie_pass_worker", None)
        try:
            return bool(worker is not None and worker.is_alive())
        except Exception:
            return False

    def request_cancel_auto_lie_pass(self) -> bool:
        """Ask the active automatic lie pass to stop, without touching Tk."""

        worker = getattr(self, "_api_lie_pass_worker", None)
        if worker is None:
            return False
        try:
            if not worker.is_alive():
                return False
            stop = getattr(worker, "request_stop", None)
            if not callable(stop):
                return False
            self._api_auto_lie_pending = False
            stop()
            AUTO_LIE_LOG.warning("自动过测谎: cancellation requested by Esc")
            return True
        except Exception:
            AUTO_LIE_LOG.debug("自动过测谎: Esc cancellation failed", exc_info=True)
            return False

    def _save_reconnect_settings(self, enabled: bool, world: str, channel: int) -> None:
        """Persist 自动重连 with the rest of the panel's settings."""

        try:
            self._shutdown_save_settings(self._shutdown_collect_data())
        except Exception:
            LOG.warning("auto reconnect settings could not be saved", exc_info=True)

    def _load_reconnect_settings(self) -> tuple[bool, str, int]:
        """The saved 自动重连 settings (defaults when there are none)."""

        try:
            data = json.loads(
                self._shutdown_settings_path().read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return False, WORLD_NAMES[0], CHANNEL_DEFAULT
        world = str(data.get("auto_reconnect_world", WORLD_NAMES[0]))
        if world not in WORLD_NAMES:
            world = WORLD_NAMES[0]
        channel = valid_channel(data.get("auto_reconnect_channel", CHANNEL_DEFAULT))
        if channel is None:
            channel = CHANNEL_DEFAULT
        return bool(data.get("auto_reconnect_enabled", False)), world, channel

    def _restart_patrol_after_reconnect(self, detail: str) -> None:
        """Put the patrol back the way 开始巡逻 does, after the reconnect brought the character in.

        ``_start_patrol`` cannot be reused directly: it returns early when the patrol controller is
        already enabled, which is exactly the state a reconnect leaves behind - and the missing part is
        the SESSION preparation (marker -> layer detection -> ``prepare_patrol_start``), not the switch.
        ``on_patrol_restart`` is the dedicated hook for this (the assistant's
        ``restart_patrol_after_reconnect``): it runs the same preparation without the calibration
        overlays and without refusing when the character is off the patrol route.
        """

        if not hasattr(self, "_reconnect_status"):
            return
        # 自动重连 is NOT a patrol workflow: it reconnects because the character dropped, whether or not
        # the operator was patrolling.  It may only put back a patrol that was interrupted, never start
        # one that was never there ("the assistant resumes only a patrol that the disconnect actually
        # interrupted" - see reconnect_worker._finish_automation).  The reliable signal is the operator's
        # own last 开始巡逻/停止巡逻 choice: the disconnect alert and the focus gate stop the patrol by
        # themselves WITHOUT touching this flag, which is exactly why v1.0.31's "character logined but
        # patrol did not start" case still restarts (intent was ON) while a parked assistant stays parked.
        if not bool(getattr(self, "_patrol_intent", False)):
            LOG.warning(
                "auto reconnect: the run succeeded, but the operator never started the patrol (停止巡逻/"
                "未按过) - leaving it stopped; 自动重连 does not depend on the patrol"
            )
            self._reconnect_status.configure(
                text=f"自动重连完成: {detail}，未开始巡逻（之前未按开始巡逻）。"
            )
            return
        self._reconnect_status.configure(
            text=f"自动重连完成: {detail}，正在检测层数并恢复巡逻…"
        )
        # The patrol INTENT is ON, so the patrol is restored whatever the switch reads right now: the
        # disconnect alert and the focus gate stop the patrol by themselves, so any "is it enabled?"
        # test is unreliable and was exactly what left the patrol stopped in v1.0.31.
        LOG.info("auto reconnect: restarting the patrol after the reconnect (the operator's last "
                 "开始巡逻/停止巡逻 choice was: %s)", "开始巡逻" if getattr(
                     self, "_patrol_intent", False) else "停止巡逻/未按过")
        hook = self.on_patrol_restart or self.on_patrol_start
        armed = True
        if hook is not None:
            try:
                armed = hook() is not False
            except OSError as exc:
                LOG.warning("auto reconnect: patrol restart refused at the window/calibration step "
                            "(layer detection): %s", exc)
                armed = False
        if armed and self.patrol_controller is not None:
            self.patrol_controller.set_enabled(True)
            self._refresh_patrol_controls()
        LOG.info("auto reconnect: patrol restart after the run -> armed=%s", armed)
        self._reconnect_status.configure(
            text=(f"自动重连完成: {detail}，已重新检测层数并恢复巡逻" if armed
                  else f"自动重连完成: {detail}，未自动开始巡逻（详见日志）")
        )

    def _drain_reconnect_results(self) -> None:
        """Show the 自动重连 worker's progress on Tk's owning thread."""

        results = self.reconnect_results
        if results is None:
            return
        while True:
            try:
                state, detail = results.get_nowait()
            except queue.Empty:
                return
            try:
                if state == "memory":
                    self._memory_usage_text = f"内存：{detail}"
                    self._refresh_license_ui()
                    continue
                if state == "restart":
                    # 清理内存 belongs to the game/layer lifecycle, not to
                    # the Additional Functions control area.  Keep its live
                    # progress beside the patrol calibration status.
                    if hasattr(self, "_control_status"):
                        self._control_status.configure(text=f"清理内存：{detail}")
                    continue
                if not hasattr(self, "_reconnect_status"):
                    continue
                if state == "failed":
                    self._reconnect_status.configure(
                        text=f"自动重连失败: {detail}。"
                    )
                    self._play_action_sound(False)
                    self._reconnect_test_idle()
                elif state == "done":
                    self._reconnect_status.configure(
                        text=f"自动重连完成: {detail}。"
                    )
                    self._play_action_sound(True)
                    self._reconnect_test_idle()
                    self._send_configured_workflow_message(
                        getattr(self, "_reconnect_message_var", None),
                        "重连消息",
                    )
                elif state == "colour":
                    # the temporary 测试重连 button's login-page colour measurement
                    self._reconnect_status.configure(text=f"登录页颜色检测: {detail}")
                elif state == "patrol-restart":
                    # The reconnect came back with the character in game and asks for the patrol to be
                    # prepared again: re-detecting the layer and re-anchoring the map session is what
                    # 开始巡逻 does, and it is what makes the movement worker resync onto the patrol
                    # route after a reconnect (the operator's v1.0.24 report: "it don't start patrol,
                    # it didn't go into detect layer and check if back to patrol route logic").
                    self._restart_patrol_after_reconnect(detail)
                elif state == "input":
                    self._reconnect_status.configure(text=f"自动重连: {detail}")
                elif state == "capture":
                    self._reconnect_status.configure(
                        text=f"已保存诊断截图: {Path(detail).name}（{Path(detail).parent}）"
                    )
                else:
                    suffix = f": {detail}" if detail else ""
                    self._reconnect_status.configure(
                        text=f"自动重连进行中 - {state}{suffix}"
                    )
            finally:
                try:
                    results.task_done()
                except (AttributeError, ValueError):
                    pass

    def _edit_workflow_message(
        self, kind: str, title: str, variable: Any,
    ) -> None:
        """Edit one event-workflow message without coupling it to quick slots."""

        if not self._license_allowed():
            self._show_license_refusal()
            return
        try:
            from tkinter import simpledialog
            current = str(variable.get() or "")
            value = simpledialog.askstring(
                title, "留空则不发送消息：", initialvalue=current,
                parent=self._root,
            )
        except Exception:
            LOG.warning("%s editor could not open", title, exc_info=True)
            return
        if value is None:
            return
        variable.set(str(value).strip()[:500])
        self._refresh_workflow_message_button(kind, title, variable)
        self._shutdown_on_change()
        LOG.info("%s %s", title, "configured" if variable.get() else "cleared")

    def _edit_player_room_code(self) -> None:
        """Edit the optional deterministic channel-routing code."""

        if not self._license_allowed():
            self._show_license_refusal()
            return
        try:
            from tkinter import simpledialog
            variable = self._player_room_code_var
            value = simpledialog.askstring(
                "房间码", "留空则每次随机换线：",
                initialvalue=str(variable.get() or ""), parent=self._root,
            )
        except Exception:
            LOG.warning("房间码 editor could not open", exc_info=True)
            return
        if value is None:
            return
        variable.set(str(value).strip()[:128])
        button = getattr(self, "_player_room_code_button", None)
        if button is not None:
            button.configure(text="已设置" if variable.get() else "房间码")
        self._shutdown_on_change()
        LOG.info("房间码 %s", "configured" if variable.get() else "cleared")

    def _drain_channel_update_events(self) -> None:
        """Reflect worker-confirmed channel arrivals in Tk without cross-thread access.

        Setting the spinbox variable is not enough: the 自动重连 status line, the
        reconnect worker and the saved configuration are all refreshed by
        ``_reconnect_on_change``, so without it the field an operator watches for
        "which channel am I on" kept showing the old channel after a successful
        landing ("the current channel on UI is not changing with successful
        landing").  The handler is only run while the session is authorized, so a
        transient license read cannot turn the landing into a refusal.
        """

        events = getattr(self, "channel_update_events", None)
        if events is None:
            return
        latest = None
        while True:
            try:
                latest = events.get_nowait()
            except queue.Empty:
                break
        if latest is None:
            return
        channel = valid_channel(latest)
        if channel is None:
            return
        control = getattr(self, "_reconnect_channel_control", None)
        if control is not None:
            control.set_bounds(channel)
        elif hasattr(self, "_reconnect_channel_var"):
            self._reconnect_channel_var.set(channel)
        licensed = bool(
            getattr(self, "_license_status", None)
            and self._license_status.valid
        )
        if licensed:
            try:
                # Applies the channel to the reconnect worker, persists it and
                # refreshes the 自动重连 status line.
                self._reconnect_on_change()
            except Exception:
                LOG.exception("could not apply the landed channel to the UI")
        else:
            LOG.info(
                "player channel route landed on %d; the field shows it, but the "
                "reconnect worker is locked until the license is valid", channel
            )
        LOG.info("player channel route landed on %d; 自动重连频道已同步", channel)

    def _drain_other_player_stop_events(self) -> None:
        """Stop the mission and clear the 其他玩家自动换线 field when its workflow ended.

        Runs on Tk's thread (the worker only posted a reason).  Two things happen,
        both requested by the operator:

        * **Stop Patrol runs too.**  Ending the mission only stopped *patrol*; the
          automation switch stayed armed, so the attacks kept firing and the game
          window kept being brought to the foreground ("esc don't clear patrol
          state, the game window still keeps foregrounding and attack never
          stop").  ``_stop_patrol`` is the operator's own Stop: it clears
          ``automation_active`` immediately and scrubs the keys in the
          background, which is what actually silences the attack worker and the
          focusing that goes with it.
        * **The field is cleared**, so the feature cannot start another round -
          with its 求让消息 and its waiting - on the next red diamond.  The
          settings are saved and applied exactly as if the operator had un-ticked
          it himself.
        """

        events = getattr(self, "other_player_stop_events", None)
        if events is None:
            return
        reason = None
        while True:
            try:
                reason = events.get_nowait()
            except queue.Empty:
                break
        if reason is None:
            return
        # The worker only posts this for a stop the operator caused (manual Esc).
        # Patrol and the automation are cleared exactly like the Stop button, and
        # the dashboard reflects it instead of looking busy.
        try:
            self._stop_patrol()
        except Exception:
            LOG.exception("could not stop patrol after the other-player mission ended")
        if not hasattr(self, "_player_check_var"):
            return
        was_selected = bool(self._player_check_var.get())
        if was_selected:
            self._player_check_var.set(False)
            try:
                # The checkbox's own handler: derive the settings with the box
                # now off, persist them, and push them to the movement worker.
                self._shutdown_on_change()
            except Exception:
                LOG.exception("could not clear the other-player switch field")
        LOG.warning(
            "有人换线 stopped (%s); patrol and the automation were cleared and "
            "the 有人换线 field is now %s",
            reason,
            "cleared" if was_selected else "already off",
        )
        if hasattr(self, "_control_status"):
            try:
                self._control_status.configure(
                    text=f"有人换线 已停止（{reason}），巡逻已停止，开关已取消"
                )
            except Exception:
                LOG.debug("could not show the other-player stop status", exc_info=True)

    def _refresh_workflow_message_button(
        self, kind: str, title: str, variable: Any,
    ) -> None:
        """Show that an event message is configured without exposing its text."""

        button = getattr(self, f"_{kind}_message_button", None)
        if button is None:
            return
        try:
            configured = bool(str(variable.get() or "").strip())
            button.configure(text="已设置" if configured else title)
        except Exception:
            LOG.debug("%s button refresh failed", title, exc_info=True)

    def _send_configured_workflow_message(self, variable: Any, label: str) -> bool:
        """Send a configured workflow message by the normal chat route."""

        try:
            message = str(variable.get() or "") if variable is not None else ""
        except Exception:
            message = ""
        if not message.strip():
            return False
        sender = getattr(getattr(self, "status_worker", None), "key_sender", None)
        sent = send_game_chat_message(sender, message)
        LOG.info("%s %s", label, "sent" if sent else "not sent")
        return sent

    def _arm_input_for_hotkey(self, action: str) -> None:
        """Re-arm live input for message and pickup hotkeys only.

        The auto-reconnect leaves live input OFF when a run fails (it must not type on the login
        page), and a run that succeeds leaves it exactly as it found it - which, before Start Patrol,
        is also OFF. Every ordinary message/pickup key would then be refused. Trade is deliberately
        excluded because its worker uses direct dialog keys and must not wake general attack input.
        Arming here is skipped while the game is not focused, while patrol runs, or while reconnect
        owns the machine.
        """

        sender = getattr(getattr(self, "status_worker", None), "key_sender", None)
        if sender is None:
            return
        is_enabled = getattr(sender, "input_is_enabled", None)
        if not callable(is_enabled) or is_enabled():
            return
        reconnect = getattr(self, "reconnect_worker", None)
        owner = getattr(reconnect, "reconnect_active_event", None)
        if owner is not None and owner.is_set():
            LOG.info("hotkey %s: 自动重连 owns the machine; live input left off", action)
            return
        if (self.patrol_controller is not None
                and self.patrol_controller.is_enabled()):
            LOG.info("hotkey %s: patrol is running; live input state left alone", action)
            return
        foreground = getattr(sender, "is_game_foreground", None)
        if callable(foreground) and not foreground():
            LOG.info(
                "hotkey %s: live input is off and the game window is not foreground; "
                "not re-arming", action,
            )
            return
        enable = getattr(sender, "enable_input", None)
        if not callable(enable):
            return
        try:
            enable()
        except Exception:
            LOG.warning("hotkey %s: live input could not be re-armed", action, exc_info=True)
            return
        LOG.info(
            "hotkey %s: live input re-armed (patrol is stopped) - typing hotkeys work again",
            action,
        )

    def _play_action_sound(self, success: bool) -> None:
        """Play UI action feedback without blocking Tk."""

        now = time.monotonic()
        previous = self._last_action_sound_at.get(success, float("-inf"))
        if now - previous < 1.0:
            LOG.info("action sound suppressed (duplicate): %s",
                     "success" if success else "fail")
            return
        self._last_action_sound_at[success] = now
        name = "success.mp3" if success else "fail.mp3"
        path = Path(__file__).resolve().parent / "sound" / name
        threading.Thread(
            target=play_mp3, args=(path,),
            name=f"action-sound-{'success' if success else 'fail'}",
            daemon=True,
        ).start()

    def _poll_yolo_exit(self) -> None:
        """Detect a YOLO subprocess that died silently (usually missing deps)."""
        proc = self._yolo_process
        if proc is None or proc.poll() is None:
            return
        # 关闭上一次运行留下的日志句柄。
        handle = getattr(self, "_yolo_launch_log", None)
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
            self._yolo_launch_log = None
        # 读取 yolo_launch.log 最后一行，把真实错误显示在界面上。
        detail = ""
        log_path = (Path(__file__).resolve().parent
                    / "yolo-detection" / "yolo_launch.log")
        try:
            lines = [ln.rstrip("\r\n") for ln in
                     log_path.read_text(encoding="utf-8",
                                        errors="replace").splitlines()
                     if ln.strip()]
            if lines:
                last = lines[-1]
                if len(last) > 120:
                    last = last[-120:]
                detail = " 错误: " + last
        except Exception:
            pass
        self._yolo_process = None
        if hasattr(self, "_yolo_run_button"):
            self._yolo_run_button.configure(state="normal")
        if hasattr(self, "_yolo_stop_button"):
            self._yolo_stop_button.configure(state="disabled")
        if hasattr(self, "_yolo_status"):
            self._yolo_status.configure(
                text=f"YOLO 检测进程已退出 (rc={proc.returncode}){detail}。"
                     "详见 yolo-detection\\yolo_launch.log。"
            )
        LOG.warning("yolo detection process exited early (rc=%s)%s",
                    proc.returncode, detail)

    def _refresh_automation_status(self) -> None:
        if not hasattr(self, "_automation_status_label"):
            return
        patrol_running = bool(
            self.patrol_controller is not None
            and self.patrol_controller.is_enabled()
        )
        active = bool(
            self.automation_active_event is not None
            and self.automation_active_event.is_set()
        )
        if active:
            text = "自动化: 运行中 — 已选中游戏窗口"
        elif patrol_running:
            # 不显示“游戏未在前台”的提示。
            text = ""
        else:
            text = "自动化: 已停止"
        self._configure_text_if_changed(
            self._automation_status_label, "automation_status", text
        )

    def _configure_text_if_changed(self, widget: Any, key: str, text: object) -> bool:
        """Update a Tk text field only when its visible value actually changes."""

        value = str(text)
        if self._ui_text_cache.get(key) == value:
            return False
        self._ui_text_cache[key] = value
        widget.configure(text=value)
        return True

    def _drain_logs(self) -> None:
        if self.log_queue is None or not hasattr(self, "_log_label"):
            return
        messages: list[str] = []
        while True:
            try:
                messages.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if not messages:
            return
        # Hint-style display (same look as the plain message hints): only the
        # latest few significant events, one compact line each.  The full
        # 500-line in-memory history stays available through the copy action.
        for message in messages:
            line = " ".join(message.splitlines())
            if len(line) > 220:
                line = line[:220] + "…"
            self._log_display_lines.append(line)
        del self._log_display_lines[:-8]
        self._log_label.configure(text="\n".join(self._log_display_lines))

    def _trim_log_lines(self, limit: int) -> None:
        """Keep only the latest ``limit`` lines in the running-log display."""
        if not hasattr(self, "_log_display_lines"):
            return
        try:
            if len(self._log_display_lines) > limit:
                del self._log_display_lines[:-limit]
            self._log_label.configure(text="\n".join(self._log_display_lines))
        except Exception:
            pass

    def _update_log_icon_visibility(self) -> None:
        """Show the copy icons only after patrol has ended (report time)."""

        if not hasattr(self, "_copy_log_button"):
            return
        running = bool(
            self.patrol_controller is not None
            and self.patrol_controller.is_enabled()
        )
        # Rebuild in the same left-to-right order used when the panel is
        # created.  ``pack_forget`` drops the original side option, which
        # previously made these controls reappear right-aligned after a run.
        buttons = (
            (self._copy_log_button, (0, 3)),
            (getattr(self, "_copy_server_log_button", None), (0, 3)),
            (getattr(self, "_copy_auto_lie_log_button", None), (0, 3)),
            (getattr(self, "_import_config_button", None), (0, 3)),
            (getattr(self, "_export_config_button", None), (0, 0)),
        )
        for button, padding in buttons:
            if button is None:
                continue
            if running:
                button.pack_forget()
            elif not button.winfo_manager():
                button.pack(side="left", padx=padding)

    def _copy_to_clipboard(self, text: str) -> None:
        root = getattr(self, "_root", None)
        if root is None:
            LOG.warning("clipboard copy skipped: UI root not ready")
            return
        try:
            root.clipboard_clear()
            root.clipboard_append(text)
        except Exception:
            LOG.exception("clipboard copy failed")

    def _copy_log_file(self, target: Path, log_type: str) -> None:
        """Copy an existing root log file as a file object, not its path.

        Each live log already has one fixed file maintained by the asynchronous
        logger.  The toolbar therefore must copy that exact file after checking
        it exists; producing a second export under ``助手配置`` made the visible
        path and the actual log source diverge.
        """

        try:
            target = target.resolve(strict=True)
            if not target.is_file():
                raise OSError(f"not a regular file: {target}")
        except OSError:
            LOG.exception("%s复制失败：日志文件不存在", log_type)
            return

        copied_file = False
        try:
            # Use CF_HDROP directly instead of a text clipboard path.  This
            # works in both the source run and the packaged EXE without a
            # pywin32 dependency: chat clients and Explorer receive the
            # actual just-written file.
            import ctypes
            from ctypes import wintypes

            class _DROPFILES(ctypes.Structure):
                _fields_ = [
                    ("pFiles", wintypes.DWORD),
                    ("pt_x", wintypes.LONG),
                    ("pt_y", wintypes.LONG),
                    ("fNC", wintypes.BOOL),
                    ("fWide", wintypes.BOOL),
                ]

            path_text = str(target.resolve()) + "\0\0"
            payload = path_text.encode("utf-16-le")
            payload_size = ctypes.sizeof(_DROPFILES) + len(payload)
            kernel32 = ctypes.windll.kernel32
            user32 = ctypes.windll.user32
            # ctypes otherwise assumes 32-bit ``int`` returns, which can
            # truncate a GlobalAlloc pointer in the 64-bit packaged helper.
            kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
            kernel32.GlobalAlloc.restype = ctypes.c_void_p
            kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
            kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)
            user32.SetClipboardData.argtypes = (wintypes.UINT, ctypes.c_void_p)
            user32.SetClipboardData.restype = ctypes.c_void_p
            handle = kernel32.GlobalAlloc(0x0002, payload_size)  # GMEM_MOVEABLE
            if not handle:
                raise OSError("GlobalAlloc failed for CF_HDROP")
            try:
                pointer = kernel32.GlobalLock(handle)
                if not pointer:
                    raise OSError("GlobalLock failed for CF_HDROP")
                try:
                    drop = _DROPFILES(
                        ctypes.sizeof(_DROPFILES), 0, 0, False, True,
                    )
                    ctypes.memmove(pointer, ctypes.byref(drop), ctypes.sizeof(drop))
                    ctypes.memmove(
                        int(pointer) + ctypes.sizeof(_DROPFILES), payload, len(payload),
                    )
                finally:
                    kernel32.GlobalUnlock(handle)
                if not user32.OpenClipboard(None):
                    raise OSError("OpenClipboard failed")
                try:
                    if not user32.EmptyClipboard():
                        raise OSError("EmptyClipboard failed")
                    # CF_HDROP=15.  On success Windows owns ``handle``.
                    if not user32.SetClipboardData(15, handle):
                        raise OSError("SetClipboardData(CF_HDROP) failed")
                    handle = None
                finally:
                    user32.CloseClipboard()
                copied_file = True
            finally:
                if handle:
                    kernel32.GlobalFree(handle)
        except Exception:
            # Keep a usable fallback only when Windows has another owner for
            # the clipboard.  The file was still written and verified above.
            LOG.warning("%s文件存在，但文件复制失败；改为复制路径：%s", log_type, target, exc_info=True)
            self._copy_to_clipboard(str(target))

        if copied_file:
            LOG.info("%s已导出并复制文件：%s", log_type, target)

    def _copy_running_log(self) -> None:
        """Attach the live log: one document per patrol run, recording on top.

        ``work\\assistant.log`` is restarted at every Start Patrol by
        ``start_patrol_log_document`` and is never rotated, so the file holds
        exactly the run that is happening now: version, the recording, then
        that run's lines.  The toolbar copies that one file for the operator
        to attach.
        """

        self._copy_log_file(
            application_root(__file__) / "work" / "assistant.log", "运行日志",
        )

    def _copy_server_client_log(self) -> None:
        """Export the safe server-client interaction log and copy its file."""

        self._copy_log_file(
            application_root(__file__) / "server_client.log", "授权日志",
        )

    def _copy_auto_lie_log(self) -> None:
        """Export the automatic-lie trace and copy its file."""

        self._copy_log_file(
            application_root(__file__) / "auto_lie.log", "自动测谎日志",
        )

    def _import_user_config(self) -> None:
        """Choose a saved configuration and refresh the running helper."""

        if not self.user_config_path:
            LOG.warning("%s失败：当前用户配置路径不可用", LOG_CONFIG_IMPORT)
            return
        try:
            from tkinter import filedialog

            config_folder = Path(self.user_config_path).parent
            if config_folder.name != "助手配置":
                config_folder = config_folder / "助手配置"
            config_folder.mkdir(parents=True, exist_ok=True)
            source = filedialog.askopenfilename(
                parent=self._root,
                title="导入用户配置",
                initialdir=str(config_folder),
                filetypes=(("JSON 文件", "*.json"), ("所有文件", "*.*")),
            )
        except Exception:
            LOG.exception("%s失败：无法打开文件选择窗口", LOG_CONFIG_IMPORT)
            return
        if not source:
            LOG.info("%s已取消", LOG_CONFIG_IMPORT)
            return
        if bool(getattr(self, "_config_import_in_progress", False)):
            LOG.info("%s已在刷新中", LOG_CONFIG_IMPORT)
            return
        self._config_import_in_progress = True
        self._control_status.configure(text="↻ 正在刷新导入配置…")
        # Stop first so no worker can act on the old route while its shared
        # recording snapshot is being replaced.
        self._stop_patrol()
        destination = Path(self.user_config_path)

        def copy_then_refresh() -> None:
            self._patrol_stop_cleanup_done.wait(timeout=5.0)
            try:
                original_content = destination.read_bytes()
                import_user_config(Path(source), destination)
                error: Optional[Exception] = None
            except Exception as exc:
                original_content = None
                error = exc

            root = getattr(self, "_root", None)
            if root is None:
                return

            def finish() -> None:
                if error is not None:
                    self._config_import_in_progress = False
                    LOG.warning("%s失败：%s", LOG_CONFIG_IMPORT, error)
                    self._control_status.configure(text=f"导入配置失败：{error}")
                    return
                try:
                    self._reload_imported_user_config()
                except Exception as exc:
                    LOG.exception("%s刷新失败", LOG_CONFIG_IMPORT)
                    # The replacement succeeded but its complete live reload
                    # did not. Restore the exact prior document before
                    # accepting another UI change, so disk and workers cannot
                    # remain on two different configurations.
                    if original_content is None:
                        self._config_import_in_progress = False
                        self._control_status.configure(text=f"导入配置失败：{exc}")
                        return

                    def rollback_then_refresh() -> None:
                        try:
                            restore_user_config_bytes(destination, original_content)
                            rollback_error: Optional[Exception] = None
                        except Exception as rollback_exc:
                            rollback_error = rollback_exc

                        def finish_rollback() -> None:
                            self._config_import_in_progress = False
                            if rollback_error is not None:
                                LOG.error(
                                    "%s回滚失败: %s", LOG_CONFIG_IMPORT, rollback_error
                                )
                                self._control_status.configure(
                                    text="导入配置失败，且无法恢复原配置；请不要继续保存并重启助手。"
                                )
                                return
                            try:
                                self._reload_imported_user_config()
                            except Exception:
                                LOG.exception("%s回滚后的刷新失败", LOG_CONFIG_IMPORT)
                                self._control_status.configure(
                                    text="导入配置失败，已恢复原配置；请重启助手后再试。"
                                )
                                return
                            self._control_status.configure(
                                text="导入配置失败，已恢复原配置。"
                            )

                        try:
                            root.after(0, finish_rollback)
                        except Exception:
                            LOG.debug("configuration import rollback callback skipped", exc_info=True)

                    threading.Thread(
                        target=rollback_then_refresh,
                        name="config-import-rollback",
                        daemon=True,
                    ).start()
                    return
                self._config_import_in_progress = False
                self._control_status.configure(text="配置已刷新；巡逻保持停止。")
                LOG.info("%s成功：已刷新 %s，无需重启。", LOG_CONFIG_IMPORT, source)

            try:
                root.after(0, finish)
            except Exception:
                LOG.debug("configuration import refresh callback skipped", exc_info=True)

        threading.Thread(
            target=copy_then_refresh, name="config-import-refresh", daemon=True,
        ).start()

    def _reload_imported_user_config(self) -> None:
        """Redraw user-owned panels and apply their values after an import."""

        reload_minimap = self.on_imported_minimap_calibration
        if reload_minimap is not None:
            reload_minimap()
        controller = self.patrol_controller
        previous_layer = ""
        if controller is not None:
            try:
                previous_layer = str(controller.selected_layer())
            except Exception:
                previous_layer = ""
            controller.reload_recording()
            # ``reload_recording`` selects the TOP layer.  An undo/import must
            # leave the operator on the layer they were calibrating, and the
            # combo below is painted from the controller, so restore it first.
            if previous_layer:
                try:
                    controller.select_layer(previous_layer)
                except (ValueError, KeyError):
                    LOG.info(
                        "reload: layer %s is gone after the reload; "
                        "keeping the controller's own selection",
                        previous_layer,
                    )
            self.configured_map_name = controller.map_name()
        # Each loader also applies its values to its corresponding live worker.
        self._yolo_load_settings()
        self._fixed_load_settings()
        self._drug_load_settings()
        self._shutdown_load_settings()
        self._patrol_controls_render_key = None
        self._refresh_patrol_controls()

    def _undo_capture(self, change_name: str, *, coalesce: bool = False) -> bool:
        """Remember the pre-change user configuration for local Ctrl+Z.

        Numeric controls call their normal panel callback repeatedly while the
        wheel turns.  Adjacent events for the same numeric control within
        750 ms share one pre-change snapshot; buttons and recording operations
        always make a distinct history entry.  The history never leaves
        process memory.
        """

        if (not getattr(self, "_undo_recording_enabled", False)
                or getattr(self, "_undo_restoring", False)
                or not getattr(self, "user_config_path", None)):
            return False
        now = time.monotonic()
        key = str(change_name)
        if (coalesce and key == getattr(self, "_undo_last_key", "")
                and now - getattr(self, "_undo_last_at", 0.0) <= 0.75):
            self._undo_last_at = now
            return False
        try:
            content = Path(self.user_config_path).read_bytes()
        except OSError:
            LOG.warning("undo snapshot skipped: user configuration is unavailable")
            return False
        history = getattr(self, "_undo_history", None)
        if history is None:
            return False
        history.append((key, content))
        self._undo_last_key = key
        self._undo_last_at = now
        LOG.info("undo snapshot saved: %s (%d/10)", key, len(history))
        return True

    def _undo_discard_latest(self, change_name: str) -> None:
        """Discard a pre-change snapshot when its mutation was rejected."""

        history = getattr(self, "_undo_history", None)
        if history and history[-1][0] == str(change_name):
            history.pop()
            self._undo_last_key = ""
            self._undo_last_at = 0.0

    def _enable_undo_history(self) -> None:
        """Enable user-change snapshots after startup callbacks have settled."""

        if not self.stop_event.is_set():
            self._undo_recording_enabled = True

    def _undo_capture_callback(self, change_name: str) -> None:
        """Capture one numeric-control sequence before its value mutates."""

        self._undo_capture(change_name, coalesce=True)
        self._undo_skip_next_change_key = str(change_name)

    def _undo_capture_panel_change(self, change_name: str) -> None:
        """Capture a non-numeric panel action unless its numeric control did."""

        key = str(change_name)
        if getattr(self, "_undo_skip_next_change_key", ""):
            self._undo_skip_next_change_key = ""
            return
        self._undo_capture(key)

    def _undo_latest_change(self) -> bool:
        """Restore the latest in-memory configuration snapshot safely."""

        controller = self.patrol_controller
        if controller is not None and controller.is_enabled():
            self._control_status.configure(text="撤销前请先停止巡逻。")
            return False
        if not getattr(self, "_undo_history", None):
            self._control_status.configure(text="没有可撤销的更改。")
            return False
        if not self.user_config_path:
            self._control_status.configure(text="撤销失败：用户配置不可用。")
            return False
        change_name, content = self._undo_history.pop()
        try:
            self._undo_restoring = True
            restore_user_config_bytes(Path(self.user_config_path), content)
            self._reload_imported_user_config()
        except Exception as exc:
            LOG.exception("undo restore failed: %s", change_name)
            self._undo_history.append((change_name, content))
            self._control_status.configure(text=f"撤销失败：{exc}")
            return False
        finally:
            self._undo_restoring = False
        self._undo_last_key = ""
        self._undo_last_at = 0.0
        self._control_status.configure(
            text=f"已撤销最近更改（{change_name}）；还可撤销 {len(self._undo_history)} 步。"
        )
        LOG.info("undo restored: %s (%d/10 remaining)", change_name, len(self._undo_history))
        return True

    def _export_user_config(self) -> None:
        """Export the live configuration to the selected ``助手配置`` folder."""

        if not self.user_config_path:
            LOG.warning("%s失败：当前用户配置路径不可用", LOG_CONFIG_EXPORT)
            return
        try:
            # The attack panel is a user-owned section too.  Persist the
            # controls visible right now before copying the unified document,
            # including when this is the first export after the assistant
            # started or after importing another configuration.
            self._fixed_save_settings(self._fixed_collect_data())
            target = export_user_config(Path(self.user_config_path))
        except UpdateError as exc:
            LOG.warning("%s失败：%s", LOG_CONFIG_EXPORT, exc)
            return
        LOG.info("%s成功：已导出到 %s", LOG_CONFIG_EXPORT, target)

    def _render(self, snapshot: DebugSnapshot) -> None:
        detection = snapshot.detection
        recognized_name = detection.map_name or "OCR adapter not configured"
        player_text = (
            f"({snapshot.player_x:.6f}, {snapshot.player_y:.6f})"
            if snapshot.player_x is not None and snapshot.player_y is not None
            else "not detected"
        )
        diamond_text = (
            f"{snapshot.marker_pixel_size[0]} × {snapshot.marker_pixel_size[1]} px"
            if snapshot.marker_pixel_size is not None else "not detected"
        )
        if hasattr(self, "_info_label"):
            self._info_label.configure(text=(
                f"Frame: {snapshot.sequence}\n"
                f"Captured: {snapshot.captured_at.astimezone().strftime('%H:%M:%S.%f')[:-3]}\n"
                f"Cropped capture: {snapshot.client_size[0]} × {snapshot.client_size[1]} px\n"
                f"Detector: {detection.source}  confidence={detection.confidence:.3f}\n"
                f"Minimap: {_box_text(detection.window_box)}\n"
                f"Analysis: {_box_text(detection.analysis_box)}\n"
            f"Map canvas: {_box_text(detection.canvas_box)}\n"
            f"Map-name crop: {_box_text(detection.map_name_box)}\n"
            f"Player: {player_text}  confidence={snapshot.marker_confidence:.3f}\n"
            f"Diamond: {diamond_text}\n"
            f"Map scroll Y: {snapshot.scroll_y_diamonds:+.3f} diamonds\n"
            f"World Y: "
            f"{snapshot.world_y_diamonds if snapshot.world_y_diamonds is not None else 'unknown'}"
            f"  structure={snapshot.structure_confidence:.3f} "
            f"({snapshot.structure_mode})\n"
            f"Configured map: {snapshot.configured_map_name or 'unknown'}\n"
            f"Recognized map: {recognized_name}"
        ))
        if self._SHOW_MINIMAP_PREVIEW and hasattr(self, "_minimap_label"):
            # Preview panes are hidden in the compact UI.  Avoid copying and
            # resizing two images on every poll when nobody can see them;
            # this leaves the Tk event loop more time to paint cleanly after
            # a resize or a drag drop.
            minimap = snapshot.minimap_preview.copy()
            minimap.thumbnail((360, 260), Image.Resampling.NEAREST)
            name = snapshot.map_name_preview.copy()
            name.thumbnail((360, 90), Image.Resampling.NEAREST)
            # Always bind preview images to this dashboard's interpreter.
            # A trade overlay may temporarily own another Tk interpreter;
            # implicit PhotoImage roots can then produce "pyimage does not
            # exist" and abort the entire dashboard.
            self._photo_minimap = ImageTk.PhotoImage(
                minimap, master=self._root
            )
            self._photo_map_name = ImageTk.PhotoImage(name, master=self._root)
            self._minimap_label.configure(image=self._photo_minimap)
            self._map_name_label.configure(image=self._photo_map_name)

    def _capture_snapshot_for_recording(self) -> "Optional[DebugSnapshot]":
        """Return a fresh (or latest) debug snapshot with a detected diamond.

        Records capture on demand so the position is current when the button
        is clicked; the fresh snapshot is also rendered so the user sees what
        was recorded.
        """

        snapshot = self.last_snapshot
        if self.on_capture_now is not None:
            self._control_status.configure(text="正在捕获当前位置…")
            if self._root is not None:
                self._root.update_idletasks()
            # Use a FRESH detector probe per recording click, exactly like
            # patrol startup does.  The shared detector's box history can be
            # poisoned by one bad frame (a partial title strip), which would
            # make every one of these samples return the strip and reject the
            # recording even though the full border is visible.  A fresh
            # probe measures this frame independently; a successful record
            # then seeds the shared detector via on_recording_verified.
            probe = MinimapDetector(
                fallback_region=getattr(
                    self.detector, "fallback_region", (0, 0, 400, 400)
                ),
                dedicated_crop=getattr(self.detector, "dedicated_crop", True),
                opencv_size=getattr(self.detector, "opencv_size", (400, 400)),
            )
            try:
                # Patrol capture is deliberately idle while recording. Take a
                # few explicit post-focus samples so a reset does not depend
                # on one transition frame or an unstabilized minimap border.
                for _attempt in range(3):
                    fresh_frame = self.on_capture_now()
                    candidate = build_debug_snapshot(
                        fresh_frame,
                        probe,
                        self.configured_map_name,
                        self.diamond_size_tracker,
                        self.structure_tracker,
                    )
                    snapshot = candidate
                    if (is_verified_border(candidate.detection)
                            and candidate.player_x is not None
                            and candidate.player_y is not None):
                        break
                    time.sleep(0.05)
                self.last_snapshot = snapshot
                self._render(snapshot)
            except Exception as exc:
                LOG.exception("immediate recording capture failed")
                self._control_status.configure(
                    text=f"无法录制: 即时捕获失败: {exc}"
                )
                return None
        return snapshot

    def _record_endpoint(self, boundary: str) -> bool:
        if self.patrol_controller is None:
            self._control_status.configure(text="巡逻控制器不可用。")
            return False
        if self.patrol_controller.is_enabled():
            self._control_status.configure(text="巡逻中无法录制，请先停止巡逻。")
            return False
        if not self.patrol_controller.snapshot().layers:
            self._control_status.configure(
                text="无法录制: 没有楼层，请先点击「添加楼层」。"
            )
            return False
        snapshot = self._capture_snapshot_for_recording()
        if snapshot is None or snapshot.player_x is None or snapshot.player_y is None:
            LOG.warning(
                "RECORD REJECTED: no yellow marker | detection=%s "
                "window=%s analysis=%s client=%s",
                snapshot.detection.source if snapshot is not None else None,
                snapshot.detection.window_box if snapshot is not None else None,
                snapshot.detection.analysis_box if snapshot is not None else None,
                snapshot.client_size if snapshot is not None else None,
            )
            self._control_status.configure(
                text="无法录制: 最新画面中未检测到黄色菱形标记。"
            )
            return False
        if not is_verified_border(snapshot.detection):
            LOG.warning(
                "RECORD REJECTED: border source=%s window=%s client=%s",
                snapshot.detection.source,
                snapshot.detection.window_box,
                snapshot.client_size,
            )
            self._control_status.configure(
                text="无法录制: 未检测到可保存的小地图边框。"
            )
            return False
        try:
            # Border calibration is an independent recording output. Save it
            # before route coordinates so patrol never depends on UI timing.
            undo_saved = self._undo_capture("录制巡逻点")
            if self.on_recording_verified is not None:
                self.on_recording_verified(snapshot)
            if self.map_identity_store is not None and self.configured_map_name:
                self.map_identity_store.record(
                    self.configured_map_name, snapshot.map_name_preview
                )
            if self.structure_tracker is not None:
                self.structure_tracker.save_reference()
            recorded = self.patrol_controller.record_endpoint(
                boundary,
                snapshot.player_x,
                snapshot.player_y,
                layout=snapshot.coordinate_layout,
                world_y=snapshot.world_y_diamonds,
                tracking_confidence=snapshot.structure_confidence,
            )
        except (OSError, TypeError, ValueError) as exc:
            if undo_saved:
                self._undo_discard_latest("录制巡逻点")
            LOG.warning("record rejected: layer=%s point=%s error=%s",
                        self.patrol_controller.selected_layer(), boundary, exc)
            self._control_status.configure(text=f"无法录制: {exc}")
            return False
        labels = {
            "left_most_pos": "最左",
            "rope_pos": "绳索",
            "right_most_pos": "最右",
        }
        label = labels[boundary]
        self._unlocked_points.discard((recorded.layer, boundary))
        LOG.info("record locked: layer=%s point=%s x=%.6f y=%.6f frame=%s "
                 "marker=%s conf=%.2f",
                 recorded.layer, boundary, recorded.x, recorded.y, snapshot.sequence,
                 snapshot.marker_pixel_size, snapshot.marker_confidence)
        self._control_status.configure(
            text=(f"已录制 {recorded.layer} {label}: "
                  f"x={recorded.x:.6f}, y={recorded.y:.6f}")
        )
        self._refresh_patrol_controls()
        return True

    def _record_jump_point(self, direction: str) -> bool:
        """Record a locked directional or upward jump trigger on this layer."""
        direction = str(direction).casefold()
        if direction not in ("left", "right", "up"):
            LOG.warning("ignored jump-point record request with direction=%r", direction)
            return False
        label = {"left": "左跳点", "right": "右跳点", "up": "上跳点"}[direction]
        if self.patrol_controller is None or self.patrol_controller.is_enabled():
            self._control_status.configure(text=f"巡逻中无法录制{label}，请先停止巡逻。")
            return False
        snapshot = self._capture_snapshot_for_recording()
        if snapshot is None or snapshot.player_x is None or snapshot.player_y is None:
            self._control_status.configure(text=f"无法录制{label}: 未检测到黄色菱形标记。")
            return False
        if not is_verified_border(snapshot.detection):
            self._control_status.configure(text=f"无法录制{label}: 未检测到可保存的小地图边框。")
            return False
        try:
            undo_saved = self._undo_capture("录制跳点")
            recorded = self.patrol_controller.record_jump_point(
                snapshot.player_x, snapshot.player_y, direction=direction,
                layout=snapshot.coordinate_layout,
            )
        except (OSError, TypeError, ValueError) as exc:
            if undo_saved:
                self._undo_discard_latest("录制跳点")
            self._control_status.configure(text=f"无法录制{label}: {exc}")
            return False
        self._control_status.configure(
            text=f"已插入 {recorded.layer} {label}: x={recorded.x:.6f}, y={recorded.y:.6f}"
        )
        LOG.info("%s jump point locked: layer=%s x=%.6f y=%.6f",
                 direction, recorded.layer, recorded.x, recorded.y)
        self._refresh_patrol_controls()
        return True

    def _record_split_point(self) -> bool:
        """Record a global vertical split boundary at the current marker."""
        if self.patrol_controller is None or self.patrol_controller.is_enabled():
            self._control_status.configure(text="巡逻中无法添加分区线，请先停止巡逻。")
            return False
        snapshot = self._capture_snapshot_for_recording()
        if snapshot is None or snapshot.player_x is None or snapshot.player_y is None:
            self._control_status.configure(text="无法添加分区线：未检测到黄色菱形标记。")
            return False
        try:
            undo_saved = self._undo_capture("录制分区线")
            point = self.patrol_controller.record_split_point(
                snapshot.player_x, snapshot.player_y, snapshot.coordinate_layout,
            )
        except (OSError, TypeError, ValueError) as exc:
            if undo_saved:
                self._undo_discard_latest("录制分区线")
            self._control_status.configure(text=f"无法添加分区线: {exc}")
            return False
        self._control_status.configure(
            text=f"已添加分区线 X={point.x:.6f}；波浪线贯穿楼层1至最高层。"
        )
        self._refresh_patrol_controls()
        return True

    def _prepare_skill_point_recording(self) -> bool:
        if self.patrol_controller is None or self.patrol_controller.is_enabled():
            self._control_status.configure(text="巡逻中无法录制技能点，请先停止巡逻。")
            return False
        return True

    def _record_skill_point(self, key: str) -> bool:
        snapshot = self._capture_snapshot_for_recording()
        if snapshot is None or snapshot.player_x is None or snapshot.player_y is None:
            self._control_status.configure(text="无法录制技能点：未检测到黄色菱形标记。")
            return False
        try:
            undo_saved = self._undo_capture("录制技能点")
            point = self.patrol_controller.record_skill_point(
                snapshot.player_x, snapshot.player_y, key, snapshot.coordinate_layout,
            )
        except (OSError, TypeError, ValueError) as exc:
            if undo_saved:
                self._undo_discard_latest("录制技能点")
            self._control_status.configure(text=f"无法录制技能点: {exc}")
            return False
        self._control_status.configure(
            text=f"已录制 {point.layer} 技能点 {str(key).upper()}：X={point.x:.6f}。"
        )
        self._refresh_patrol_controls()
        return True

    def _record_flower_point(self) -> bool:
        """Record one dead-flower rope waypoint from the live marker."""

        snapshot = self._capture_snapshot_for_recording()
        if snapshot is None or snapshot.player_x is None or snapshot.player_y is None:
            self._control_status.configure(text="无法录制花点：未检测到黄色菱形标记。")
            return False
        try:
            undo_saved = self._undo_capture("录制花点")
            point = self.patrol_controller.record_flower_point(
                snapshot.player_x, snapshot.player_y, snapshot.coordinate_layout,
            )
        except (OSError, TypeError, ValueError) as exc:
            if undo_saved:
                self._undo_discard_latest("录制花点")
            self._control_status.configure(text=f"无法录制花点: {exc}")
            return False
        self._control_status.configure(
            text=f"已录制 {point.layer} 花点：X={point.x:.6f}，Y={point.y:.6f}。"
        )
        self._refresh_patrol_controls()
        return True

    def _record_support_point(self) -> bool:
        """Record a local layer supporter without adding a route action."""

        if self.patrol_controller is None or self.patrol_controller.is_enabled():
            self._control_status.configure(text="巡逻中无法录制支持点，请先停止巡逻。")
            return False
        snapshot = self._capture_snapshot_for_recording()
        if snapshot is None or snapshot.player_x is None or snapshot.player_y is None:
            self._control_status.configure(text="无法录制支持点：未检测到黄色菱形标记。")
            return False
        if not is_verified_border(snapshot.detection):
            self._control_status.configure(text="无法录制支持点：未检测到可保存的小地图边框。")
            return False
        try:
            undo_saved = self._undo_capture("录制支持点")
            point = self.patrol_controller.record_support_point(
                snapshot.player_x, snapshot.player_y, snapshot.coordinate_layout,
            )
        except (OSError, TypeError, ValueError) as exc:
            if undo_saved:
                self._undo_discard_latest("录制支持点")
            self._control_status.configure(text=f"无法录制支持点: {exc}")
            return False
        self._control_status.configure(
            text=f"已录制 {point.layer} 支持点：X={point.x:.6f}，Y={point.y:.6f}。"
        )
        self._refresh_patrol_controls()
        return True

    def _delete_recorded_axis_point(
        self, layer_name: str, point_kind: str, point_x: float,
        point_index: Optional[int] = None,
    ) -> None:
        """Delete a point chosen by a double-click in the layer X-axis."""

        if self.patrol_controller is None or self.patrol_controller.is_enabled():
            self._control_status.configure(text="巡逻中无法删除录制点，请先停止巡逻。")
            return
        # A canvas redraw does not reliably emit <Leave> for an item removed
        # beneath a stationary pointer.  Close the hover popup *before* the
        # backing recording is deleted so its former coordinate can never
        # remain as a stale floating hint.
        self._hide_layer_axis_hint()
        if point_kind == "dual_stationary_post":
            self._delete_dual_stationary_post(point_index)
            return
        try:
            undo_saved = self._undo_capture("删除录制点")
            removed = self.patrol_controller.delete_axis_point(
                layer_name, point_kind, point_x, point_index,
            )
            if point_kind not in ("jump_point", "skill_point", "flower_point", "support_point"):
                self._unlocked_points.discard((layer_name, point_kind))
            label = {
                "left_most_pos": "最左", "rope_pos": "绳索",
                "right_most_pos": "最右", "jump_point": "跳点",
                "skill_point": "技能点", "flower_point": "花点",
                "support_point": "支持点",
            }.get(point_kind, "录制点")
        except (OSError, ValueError) as exc:
            if undo_saved:
                self._undo_discard_latest("删除录制点")
            LOG.warning("axis point delete failed", exc_info=True)
            self._control_status.configure(text=f"无法删除录制点: {exc}")
            return
        if removed:
            band_note = ""
            if point_kind not in ("jump_point", "skill_point", "support_point"):
                if self.patrol_controller.layer_has_band(layer_name):
                    band_note = " 已重新计算该层图层带。"
                else:
                    band_note = (
                        " 该层已无最左/绳索/最右支持点，"
                        "图层带已清空；开始运行会提示需先录制支持点。"
                    )
            self._control_status.configure(
                text=f"已删除 {self._patrol_display_name(layer_name)} {label}。{band_note}"
            )
            LOG.info("layer axis point deleted layer=%s kind=%s x=%.6f index=%s",
                     layer_name, point_kind, point_x, point_index)
            self._refresh_patrol_controls()
        elif undo_saved:
            self._undo_discard_latest("删除录制点")

    def _delete_dual_stationary_post(self, point_index: Optional[int]) -> None:
        """Remove one persisted 双桩 marker and synchronise the live pair."""

        try:
            index = int(point_index) if point_index is not None else -1
        except (TypeError, ValueError):
            index = -1
        posts = self._dual_stationary_posts_data()
        if not 0 <= index < len(posts):
            self._control_status.configure(text="无法删除双桩点：录制索引无效。")
            return
        undo_saved = self._undo_capture("删除双桩点")
        try:
            removed = posts.pop(index)
            self._dual_stationary_posts = posts
            # Dual posts live in the fixed-attack section, not the map
            # profile.  Use its ordinary persistence path so MovementWorker
            # immediately drops a deleted post from its live anchor pair.
            self._undo_skip_next_change_key = "攻击设置"
            self._fixed_on_change()
        except Exception as exc:
            if undo_saved:
                self._undo_discard_latest("删除双桩点")
            LOG.warning("dual stationary post delete failed", exc_info=True)
            self._control_status.configure(text=f"无法删除双桩点: {exc}")
            return
        self._control_status.configure(
            text=(f"已删除双桩{index + 1}：X={removed['x']:.6f}，Y={removed['y']:.6f}；"
                  f"当前剩余 {len(posts)}/2 桩。")
        )
        LOG.info("dual stationary post deleted index=%d remaining=%d", index, len(posts))
        self._refresh_patrol_controls()

    def _delete_temporary_stationary_anchor(self) -> None:
        """Delete the session-only 站桩 marker selected on the layer canvas."""

        controller = self.patrol_controller
        if controller is None or controller.is_enabled():
            self._control_status.configure(text="巡逻中无法删除站桩位置，请先停止巡逻。")
            return
        mover = getattr(self, "movement_worker", None)
        clear = getattr(mover, "clear_stationary_attack_anchor", None)
        if not callable(clear):
            self._control_status.configure(text="无法删除站桩位置：移动工作线程不可用。")
            return
        try:
            clear("stationary anchor deleted from layer axis")
        except Exception as exc:
            LOG.warning("temporary stationary anchor delete failed", exc_info=True)
            self._control_status.configure(text=f"无法删除站桩位置: {exc}")
            return
        self._control_status.configure(text="已删除临时站桩位置；下次开始运行将重新记录当前位置。")
        LOG.info("temporary stationary anchor deleted from layer axis")
        self._refresh_patrol_controls()

    def _layer_axis_menu(self, event: Any, layer_name: str) -> None:
        """Show point-recording choices for one single-clicked layer axis."""

        if self.patrol_controller is None or self.patrol_controller.is_enabled():
            return
        self._select_recording_layer(layer_name)
        menu = self._tk.Menu(self._root, tearoff=False)
        entries = (
            ("添加最左", "left_most_pos", lambda: self._record_endpoint("left_most_pos")),
            ("添加绳索", "rope_pos", lambda: self._record_endpoint("rope_pos")),
            ("添加最右", "right_most_pos", lambda: self._record_endpoint("right_most_pos")),
            ("添加左跳", "jump_point", lambda: self._record_jump_point("left")),
            ("添加右跳", "jump_point", lambda: self._record_jump_point("right")),
            ("添加上跳", "jump_point", lambda: self._record_jump_point("up")),
            ("添加花点", "flower_point", self._record_flower_point),
            ("添加支持点", "support_point", self._record_support_point),
        )
        final_name = self.patrol_controller.final_layer_name()
        for label, kind, command in entries:
            already_recorded = (
                kind not in ("jump_point", "flower_point", "support_point")
                and self.patrol_controller.endpoint(layer_name, kind) is not None
            )
            unavailable_rope = kind == "rope_pos" and layer_name == final_name
            state = "disabled" if already_recorded or unavailable_rope else "normal"
            menu.add_command(label=label, command=command, state=state)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _set_layer_patrol_times(self, layer_name: str, times: int) -> None:
        try:
            self.patrol_controller.set_layer_patrol_times(layer_name, times)
        except (OSError, TypeError, ValueError) as exc:
            self._control_status.configure(text=f"无法设置巡逻次数: {exc}")
            return
        self._control_status.configure(
            text=f"{self._patrol_display_name(layer_name)} 将巡逻 {times} 次后再进入下一层/分区。"
        )
        self._refresh_patrol_controls()

    def _layer_patrol_times_changed(self, layer_name: str) -> None:
        """Persist the independent 1..20 repeat count for one layer row."""

        control = self._layer_patrol_time_controls.get(layer_name)
        if control is None:
            return
        try:
            times = int(control.get_bounds()[0])
        except (TypeError, ValueError):
            return
        self._set_layer_patrol_times(layer_name, times)

    @staticmethod
    def _layer_axis_point_text(label: str, point: Any) -> str:
        return f"{label} ({float(point['x']):.4f}, {float(point['y']):.4f})"

    @staticmethod
    def _skill_key_axis_label(key: Any) -> str:
        """Return the compact, two-character label for a skill-point key."""

        normalized = str(key or "").strip().casefold()
        labels = {
            "ctrl": "CT", "shift": "SH", "space": "SP",
            "delete": "DL", "end": "ED", "pagedown": "PD",
            "pageup": "PU", "home": "HM", "insert": "IN",
            "slash": "/",
        }
        if normalized in labels:
            return labels[normalized]
        return normalized.upper()[:2] or "?"

    def _hide_layer_axis_hint(self, _canvas: Any = None) -> None:
        """Destroy the out-of-canvas layer-axis coordinate popup safely."""

        # Drawing the hint *inside* the canvas let its rectangle cover a rope
        # or jump arrow at the same X. Tk then emitted Enter/Leave in a tight
        # loop (hint appears -> marker leaves -> hint disappears -> marker
        # re-enters). Keep it in its own non-focus popup instead.
        popup = getattr(self, "_layer_axis_hint_popup", None)
        self._layer_axis_hint_popup = None
        try:
            if popup is not None and bool(popup.winfo_exists()):
                popup.destroy()
        except Exception:
            return

    def _show_layer_axis_hint(self, event: Any, canvas: Any, text: str) -> None:
        """Show a non-overlapping coordinate hint for one layer-axis marker."""

        try:
            if not bool(canvas.winfo_exists()):
                return
            self._hide_layer_axis_hint()
            popup = self._tk.Toplevel(self._root)
            popup.overrideredirect(True)
            try:
                popup.attributes("-topmost", True)
            except Exception:
                pass
            label = self._ttk.Label(
                popup, text=text, padding=(4, 2), relief="solid",
            )
            label.pack()
            popup.update_idletasks()
            # Place it clear of the canvas item beneath the pointer. It does
            # not claim focus and therefore cannot generate a second marker
            # Enter/Leave sequence when rope and jump share an X coordinate.
            width = max(1, int(popup.winfo_reqwidth()))
            popup.geometry(
                f"+{int(event.x_root) - width // 2}+{int(event.y_root) - 28}"
            )
            self._layer_axis_hint_popup = popup
        except Exception:
            self._hide_layer_axis_hint()
            LOG.debug("layer-axis hover hint unavailable", exc_info=True)

    @staticmethod
    def _layer_axis_canvas_x(value: float, left: int, right: int) -> int:
        """Map the visible logical 0.08–0.92 band onto the full UI axis."""

        visible_span = _LAYER_AXIS_VISIBLE_MAX_X - _LAYER_AXIS_VISIBLE_MIN_X
        normalized = (float(value) - _LAYER_AXIS_VISIBLE_MIN_X) / visible_span
        normalized = max(0.0, min(1.0, normalized))
        return left + round((right - left) * normalized)

    def _draw_layer_axis(
        self, layer_name: str, layer: Any, split_points: tuple[dict[str, Any], ...] = (),
    ) -> None:
        """Draw the layer axis with the same marker grammar as the blinker."""

        canvas = self._layer_axis_canvases.get(layer_name)
        if canvas is None:
            return
        canvas.delete("all")
        # The canvas expands to consume the usable part of the left column.
        # Its recorded drawing used a stale fixed right edge, which left a
        # blank tail whenever the application had spare width.  Map the axis
        # and every marker to the actual canvas width instead.
        try:
            rendered_width = int(canvas.winfo_width())
        except Exception:
            rendered_width = _LAYER_AXIS_WIDTH
        left = _LAYER_AXIS_LEFT
        right = max(_LAYER_AXIS_RIGHT, rendered_width)
        axis_y = _LAYER_AXIS_Y
        canvas.create_line(left, axis_y, right, axis_y, fill="#6b6b6b", width=1)
        if not isinstance(layer, dict):
            return
        # A split is global to the map rather than an individual floor.  Draw
        # the same short wave on every layer row: stacked rows therefore form
        # one visibly continuous wave from layer1 through the final layer.
        for split in split_points:
            try:
                split_x = max(0.0, min(1.0, float(split["x"])))
            except (KeyError, TypeError, ValueError):
                continue
            center = self._layer_axis_canvas_x(split_x, left, right)
            wave: list[float] = []
            for y in range(1, _LAYER_AXIS_HEIGHT + 2, 2):
                offset = 3 if ((y // 3) % 2 == 0) else -3
                wave.extend((center + offset, y))
            canvas.create_line(*wave, fill="#8b5a2b", width=2, smooth=True)
        entries: list[tuple[str, Any, str, Optional[int]]] = []
        sources = [layer]
        parts = layer.get("part_points")
        if isinstance(parts, dict):
            sources = [part for part in parts.values() if isinstance(part, dict)]
        for source in sources:
            for kind, label in (
            ("left_most_pos", "最左"),
            ("rope_pos", "绳索"),
            ("right_most_pos", "最右"),
            ):
                point = source.get(kind)
                if isinstance(point, dict) and "x" in point and "y" in point:
                    entries.append((kind, point, label, None))
            jump_points = source.get("jump_points", [])
            if isinstance(jump_points, list):
                for index, point in enumerate(jump_points):
                    if isinstance(point, dict) and "x" in point and "y" in point:
                        direction = str(point.get("direction", "")).casefold()
                        label = {
                            "left": "左跳", "right": "右跳", "up": "上跳点",
                            # A point with no direction predates the
                            # directional recorder; it is still a jump.
                        }.get(direction, "旧跳点")
                        entries.append(("jump_point", point, label, index))
            skill_points = source.get("skill_points", [])
            if isinstance(skill_points, list):
                for index, point in enumerate(skill_points):
                    if isinstance(point, dict) and "x" in point and "y" in point:
                        entries.append(("skill_point", point, f"技能{point.get('key', '?')}", index))
            flower_points = source.get("flower_points", [])
            if isinstance(flower_points, list):
                for index, point in enumerate(flower_points):
                    if isinstance(point, dict) and "x" in point and "y" in point:
                        # The rope normally gives every flower point the same
                        # X.  Preserve the separate recorded Y identities on
                        # the one-dimensional axis with explicit 花1 / 花2
                        # labels instead of making the second marker look
                        # like a replacement for the first one.
                        entries.append(("flower_point", point, f"花{index + 1}", index))
            support_points = source.get("support_points", [])
            if isinstance(support_points, list):
                base_ys = []
                for point_name in ("left_most_pos", "rope_pos", "right_most_pos"):
                    base = source.get(point_name)
                    if isinstance(base, dict) and "y" in base:
                        try:
                            base_ys.append(float(base["y"]))
                        except (TypeError, ValueError):
                            continue
                for index, point in enumerate(support_points):
                    if not isinstance(point, dict) or "x" not in point or "y" not in point:
                        continue
                    try:
                        support_y = float(point["y"])
                    except (TypeError, ValueError):
                        continue
                    # Minimap Y increases downward: a larger value is lower.
                    # Supporters inside an existing floor envelope do not add
                    # a new vertical extreme, so render them as 支.
                    if base_ys and support_y > max(base_ys) + 1e-6:
                        label = "低"
                    elif base_ys and support_y < min(base_ys) - 1e-6:
                        label = "高"
                    else:
                        label = "支"
                    entries.append(("support_point", point, label, index))
        # Dual posts are their own marker type, just like 最左 / 最右 / 绳索.
        # They are not temporary stationary overlays: the two persisted
        # records must remain separately visible on their recorded layer.
        dual_mode_var = getattr(self, "_attack_mode_var", None)
        dual_mode_active = bool(
            dual_mode_var is not None and str(dual_mode_var.get()) == "dual_stationary"
        )
        if dual_mode_active:
            for index, point in enumerate(self._dual_stationary_posts_data()):
                if point.get("layer") == layer_name:
                    entries.append(("dual_stationary_post", point, f"双桩{index + 1}", index))
        def _axis_x(entry: tuple[str, Any, str, Optional[int]]) -> float:
            try:
                return float(entry[1].get("x", 0.0))
            except (AttributeError, TypeError, ValueError):
                # Keep malformed legacy entries visible only as safely as
                # possible; a new recording must never crash the UI because
                # an unrelated saved point has an invalid coordinate.
                return 0.0

        # This is display-only sorting.  The retained source index is used by
        # the double-click delete handler, while JSON remains append-order.
        entries.sort(key=_axis_x)
        colors = {
            "left_most_pos": "#1479d1",
            "right_most_pos": "#1479d1",
            "rope_pos": "#d8a400",
            "jump_point": "#1d9b45",
            "skill_point": "#8a2be2",
            "flower_point": "#d13a8b",
            "support_point": "#4a6f9c",
            "dual_stationary_post": "#cc2222",
        }
        for kind, point, label, index in entries:
            try:
                x_value = max(0.0, min(1.0, float(point["x"])))
            except (TypeError, ValueError, KeyError):
                continue
            if not (_LAYER_AXIS_VISIBLE_MIN_X <= x_value <= _LAYER_AXIS_VISIBLE_MAX_X):
                continue
            x = self._layer_axis_canvas_x(x_value, left, right)
            text = self._layer_axis_point_text(label, point)
            tag = f"axis:{layer_name}:{kind}:{'' if index is None else index}"
            # The axis mirrors the native blinker grammar: endpoint bars, a
            # vertical rope arrow, and thin 45-degree directional jump arrows.
            # One Tk canvas paints the complete layer atomically.
            if kind in ("left_most_pos", "right_most_pos"):
                canvas.create_rectangle(
                    x - 2, axis_y - 12, x + 2, axis_y + 1,
                    fill=colors[kind], outline="", tags=(tag,),
                )
            elif kind == "rope_pos":
                canvas.create_line(x, axis_y - 2, x, axis_y - 15,
                                   x - 6, axis_y - 9, tags=(tag,),
                                   fill=colors[kind], width=1)
                canvas.create_line(x, axis_y - 15, x + 6, axis_y - 9,
                                   tags=(tag,), fill=colors[kind], width=1)
            elif kind == "skill_point":
                # Match the jump arrow's height with a 14px square so skill
                # markers stay legible even when two-character key labels are
                # needed (for example Ctrl -> CT, PageUp -> PU).
                canvas.create_rectangle(
                    x - 7, axis_y - 16, x + 7, axis_y - 2,
                    fill=colors[kind], outline="", tags=(tag,),
                )
                canvas.create_text(
                    x, axis_y - 9,
                    text=self._skill_key_axis_label(point.get("key", "")),
                    fill="white", font=("TkDefaultFont", 10, "bold"), tags=(tag,),
                )
            elif kind == "flower_point":
                # Two flower points may share one rope X.  The later marker
                # intentionally reads 花2 at that same X, while each marker's
                # own hover text still supplies its distinct recorded Y.
                canvas.create_oval(
                    x - 8, axis_y - 16, x + 8, axis_y - 2,
                    fill=colors[kind], outline="", tags=(tag,),
                )
                canvas.create_text(
                    x, axis_y - 9, text=label, fill="white",
                    font=("TkDefaultFont", 8, "bold"), tags=(tag,),
                )
            elif kind == "support_point":
                canvas.create_rectangle(
                    x - 7, axis_y - 16, x + 7, axis_y - 2,
                    fill=colors[kind], outline="", tags=(tag,),
                )
                canvas.create_text(
                    x, axis_y - 9, text=label, fill="white",
                    font=("TkDefaultFont", 9, "bold"), tags=(tag,),
                )
            elif kind == "dual_stationary_post":
                # Same red facing glyph as ordinary 站桩攻击, but as a real
                # recorded axis point so both posts are unique and stable.
                facing = str(point.get("facing", "right")).casefold()
                marker_text = {
                    "left": "<",
                    "right": ">",
                    "both": "<>",
                }.get(facing, ">")
                canvas.create_text(
                    x, axis_y - 10, text=marker_text,
                    fill=colors[kind], font=("TkDefaultFont", 9, "bold"), tags=(tag,),
                )
            else:
                direction = str(point.get("direction", "")).casefold()
                if direction == "up":
                    # Vertical jump-up: the rope marker's up arrow, in the
                    # jump-point colour.  Falling through to the right arrow
                    # drew a 45-degree marker that pointed nowhere near the
                    # recorded action.
                    canvas.create_line(x, axis_y - 2, x, axis_y - 15,
                                       x - 6, axis_y - 9, tags=(tag,),
                                       fill=colors[kind], width=1)
                    canvas.create_line(x, axis_y - 15, x + 6, axis_y - 9,
                                       tags=(tag,), fill=colors[kind], width=1)
                elif direction == "left":
                    canvas.create_line(x + 5, axis_y - 6, x - 4, axis_y - 15,
                                       tags=(tag,), fill=colors[kind], width=1)
                    canvas.create_line(x - 4, axis_y - 15, x, axis_y - 15,
                                       tags=(tag,), fill=colors[kind], width=1)
                    canvas.create_line(x - 4, axis_y - 15, x - 4, axis_y - 11,
                                       tags=(tag,), fill=colors[kind], width=1)
                else:
                    canvas.create_line(x - 5, axis_y - 6, x + 4, axis_y - 15,
                                       tags=(tag,), fill=colors[kind], width=1)
                    canvas.create_line(x + 4, axis_y - 15, x, axis_y - 15,
                                       tags=(tag,), fill=colors[kind], width=1)
                    canvas.create_line(x + 4, axis_y - 15, x + 4, axis_y - 11,
                                       tags=(tag,), fill=colors[kind], width=1)
            canvas.tag_bind(
                tag, "<Enter>",
                lambda event, canvas=canvas, text=text:
                self._show_layer_axis_hint(event, canvas, text),
            )
            canvas.tag_bind(
                tag, "<Leave>",
                lambda _event, canvas=canvas: self._hide_layer_axis_hint(canvas),
            )
            canvas.tag_bind(
                tag, "<Double-Button-1>",
                lambda _event, layer_name=layer_name, kind=kind,
                point_x=x_value, index=index:
                self._delete_recorded_axis_point(
                    layer_name, kind, point_x, index,
                ),
            )
        # The temporary 站桩 anchor is session-only rather than map-recording
        # data, but it is still a visible user point and can be deleted by a
        # double click just like other axis markers.
        mover = getattr(self, "movement_worker", None)
        marker_getter = getattr(mover, "stationary_route_anchor_marker", None)
        marker = marker_getter() if callable(marker_getter) else None
        if (not dual_mode_active
                and marker is not None and marker[0] == layer_name):
            try:
                x_value = max(0.0, min(1.0, float(marker[1].x)))
                if not (_LAYER_AXIS_VISIBLE_MIN_X <= x_value <= _LAYER_AXIS_VISIBLE_MAX_X):
                    return
                x = self._layer_axis_canvas_x(x_value, left, right)
                facing = str(marker[2] if len(marker) > 2 else "right").casefold()
                marker_text = {
                    "left": "<",
                    "right": ">",
                    "both": "<>",
                }.get(facing, ">")
                tag = f"axis:{layer_name}:stationary_anchor"
                canvas.create_text(
                    x, axis_y - 10, text=marker_text,
                    fill="#cc2222", font=("TkDefaultFont", 9, "bold"), tags=(tag,),
                )
                canvas.tag_bind(
                    tag, "<Double-Button-1>",
                    lambda _event: self._delete_temporary_stationary_anchor(),
                )
            except (AttributeError, TypeError, ValueError):
                LOG.debug("stationary route axis marker unavailable", exc_info=True)

    def _yolo_settings_path(self) -> Path:
        """JSON file holding the YOLO panel settings."""

        path = Path(self.user_config_path) if self.user_config_path else None
        return config_section_file("yolo_detection", path)

    def _yolo_load_settings(self) -> None:
        """Restore saved YOLO panel values from the local JSON file."""

        try:
            data = json.loads(
                self._yolo_settings_path().read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return
        try:
            if "threshold" in data:
                self._yolo_threshold_var.set(float(data["threshold"]))
            if "attack_range" in data:
                # 旧版保存的是像素（以 2561px 参考宽校准）：>100 视为旧像素，
                # 自动换算成百分比（像素 ÷ 2561 × 100）。
                value = int(data["attack_range"])
                if value > 100:
                    value = round(value / 2561.0 * 100)
                self._yolo_attack_range_var.set(max(5, min(80, value)))
            if "min_mob_size" in data:
                if hasattr(self, "_yolo_min_mob_var"):
                    value = int(data["min_mob_size"])
                    # 旧版保存的是像素（旧滑块 10-200px，参考宽 2561）；
                    # 新滑块范围 1-15%，>15 必为旧像素值，换算成百分比。
                    if value > 15:
                        value = round(value / 2561.0 * 100)
                    self._yolo_min_mob_var.set(max(1, min(15, value)))
            if "detection_fps" in data:
                if hasattr(self, "_yolo_fps_var"):
                    self._yolo_fps_var.set(int(data["detection_fps"]))
            if "zone_width" in data:
                self._yolo_zone_w_var.set(int(data["zone_width"]))
            if "zone_height" in data:
                self._yolo_zone_h_var.set(int(data["zone_height"]))
            if "zone_shift_y" in data:
                self._yolo_zone_shift_y_var.set(int(data["zone_shift_y"]))
            if "show_detection" in data:
                self._yolo_show_var.set(bool(data["show_detection"]))
        except (KeyError, TypeError, ValueError):
            LOG.warning("ignored malformed yolo settings", exc_info=True)
            return
        # Refresh the slider labels to match the loaded values.
        self._yolo_on_range_change()
        self._yolo_on_min_mob_change()
        self._yolo_on_zone_change()
        self._yolo_sync_show_button()
        LOG.info("yolo settings loaded from %s", self._yolo_settings_path())

    def _fixed_settings_path(self) -> Path:
        """JSON file holding the Fixed Attack panel settings."""

        path = Path(self.user_config_path) if self.user_config_path else None
        return config_section_file("fixed_attack", path)

    def _fixed_collect_data(self) -> dict:
        """Current Fixed Attack panel values as a settings dict."""

        interval_min, interval_max = self._fixed_attack_range()
        return {
            "attack_mode": str(self._attack_mode_var.get()),
            # 随机休息 is a mode-scoped option (巡逻攻击 / 站桩攻击 / 双桩模式); the
            # saved flag is kept even when another mode greys it out, so switching
            # back restores the operator's choice.
            "random_rest_enabled": bool(
                self._random_rest_var.get()
                if hasattr(self, "_random_rest_var") else False
            ),
            "min_interval_seconds": interval_min,
            "max_interval_seconds": interval_max,
            "attack_key": self._fixed_attack_key_var.get().strip(),
            "counterattack_enabled": bool(
                self._counterattack_enabled_var.get()
                if hasattr(self, "_counterattack_enabled_var") else False
            ),
            "counterattack_key": (
                self._counterattack_key_var.get().strip()
                if hasattr(self, "_counterattack_key_var") else "ctrl"
            ),
            "combo_attack_enabled": bool(
                self._combo_attack_enabled_var.get()
                if hasattr(self, "_combo_attack_enabled_var") else False
            ),
            "combo_attack_slots": self._combo_attack_slots_data(),
            "attack_delivery": str(
                self._attack_delivery_var.get()
                if hasattr(self, "_attack_delivery_var") else "stand"
            ),
            "jump_counterattack_enabled": bool(
                getattr(self, "_jump_counterattack_enabled_var", None).get()
                if hasattr(self, "_jump_counterattack_enabled_var") else False
            ),
            "charge_min_seconds": self._charge_hold_range_seconds()[0],
            "charge_max_seconds": self._charge_hold_range_seconds()[1],
            "jump_attack_enabled": bool(
                getattr(self, "_jump_attack_enabled_var", None).get()
                if hasattr(self, "_jump_attack_enabled_var") else False
            ),
            "small_step_enabled": bool(
                getattr(self, "_small_step_enabled_var", None).get()
                if hasattr(self, "_small_step_enabled_var") else False
            ),
            "small_step_min_seconds": self._small_step_range_seconds()[0],
            "small_step_max_seconds": self._small_step_range_seconds()[1],
            "small_step_post_attack_seconds": self._small_step_post_attack_seconds(),
            "stationary_facing_direction": str(
                getattr(self, "_stationary_facing_direction_var", None).get()
                if hasattr(self, "_stationary_facing_direction_var") else "right"
            ),
            "stationary_pickup_enabled": bool(
                getattr(self, "_stationary_pickup_enabled_var", None).get()
                if hasattr(self, "_stationary_pickup_enabled_var") else False
            ),
            "stationary_pickup_min_minutes": self._stationary_pickup_range_minutes()[0],
            "stationary_pickup_max_minutes": self._stationary_pickup_range_minutes()[1],
            "stationary_delayed_return_enabled": bool(
                getattr(self, "_stationary_delayed_return_enabled_var", None).get()
                if hasattr(self, "_stationary_delayed_return_enabled_var") else False
            ),
            "dual_stationary_posts": self._dual_stationary_posts_data(),
            "dual_stationary_switch_min_minutes": self._dual_stationary_switch_range_minutes()[0],
            "dual_stationary_switch_max_minutes": self._dual_stationary_switch_range_minutes()[1],
        }

    def _charge_hold_range_seconds(self) -> tuple[float, float]:
        """Return the inclusive randomized charge-hold range in seconds."""

        control = getattr(self, "_charge_hold_control", None)
        if control is None:
            return 0.5, 0.5
        minimum, maximum = control.get_bounds()
        try:
            minimum, maximum = float(minimum), float(maximum)
        except (TypeError, ValueError, tk.TclError):
            minimum, maximum = 0.5, 0.5
        minimum = max(0.1, min(5.0, round(minimum, 1)))
        maximum = max(minimum, min(5.0, round(maximum, 1)))
        return minimum, maximum

    def _dual_stationary_switch_minutes(self) -> float:
        control = getattr(self, "_dual_stationary_switch_control", None)
        if control is None:
            return self._DUAL_STATIONARY_SWITCH_DEFAULT_MINUTES
        minimum, _maximum = control.get_bounds()
        return float(minimum)

    def _dual_stationary_switch_range_minutes(self) -> tuple[float, float]:
        control = getattr(self, "_dual_stationary_switch_control", None)
        if control is None:
            value = self._DUAL_STATIONARY_SWITCH_DEFAULT_MINUTES
            return value, value
        minimum, maximum = control.get_bounds()
        return float(minimum), float(maximum)

    def _dual_stationary_posts_data(self) -> list[dict[str, Any]]:
        raw = getattr(self, "_dual_stationary_posts", ())
        result: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                x, y = float(item["x"]), float(item["y"])
            except (KeyError, TypeError, ValueError):
                continue
            facing = str(item.get("facing", "right")).casefold()
            if facing not in ("left", "right", "both"):
                facing = "right"
            normalized = {"x": round(x, 6), "y": round(y, 6), "facing": facing}
            # The marker belongs on the layer that was selected when Ctrl+R
            # was pressed.  It is display-only; MovementWorker still chooses
            # anchors solely by the physical X/Y distance.
            recorded_layer = item.get("layer")
            if isinstance(recorded_layer, str) and recorded_layer:
                normalized["layer"] = recorded_layer
            result.append(normalized)
        return result[:2]

    def _refresh_dual_stationary_status(self) -> None:
        """Retained as a harmless record-refresh hook.

        Dual-post summaries were deliberately removed from both panels; the
        recorded markers on the layer canvas remain the visual source.
        """

        return

    def _record_dual_stationary_post(
        self, point: Any, facing: str, slot: Any,
    ) -> bool:
        """Commit a specific dual-stationary post after its D/F/G selector."""

        if str(self._attack_mode_var.get()) != "dual_stationary":
            self._control_status.configure(text="请先选择双桩模式，再录制双桩点。")
            return False
        try:
            x, y = float(point["x"]), float(point["y"])
        except (KeyError, TypeError, ValueError):
            self._control_status.configure(text="无法录制双桩点：黄色角色标记已丢失。")
            return False
        if facing not in ("left", "right", "both"):
            return False
        if slot not in (0, 1):
            self._control_status.configure(text="无法录制双桩点：录制槽位无效。")
            return False
        posts = self._dual_stationary_posts_data()
        self._undo_capture("录制双桩点")
        item = {"x": round(x, 6), "y": round(y, 6), "facing": facing}
        controller = getattr(self, "patrol_controller", None)
        selected_layer = getattr(controller, "selected_layer", None)
        if callable(selected_layer):
            try:
                layer_name = str(selected_layer())
            except Exception:
                layer_name = ""
            if layer_name:
                item["layer"] = layer_name
        if slot == 0:
            if posts:
                posts[0] = item
            else:
                posts.append(item)
        elif len(posts) == 1:
            posts.append(item)
        else:
            posts[1] = item
        self._dual_stationary_posts = posts
        self._refresh_dual_stationary_status()
        # The dual-post save uses the fixed-attack panel persistence path;
        # its snapshot is already the recording snapshot above.
        self._undo_skip_next_change_key = "攻击设置"
        self._fixed_on_change()
        names = {"left": "左", "right": "右", "both": "双向"}
        self._control_status.configure(
            text=(f"已录制桩{slot + 1}：X={x:.6f}，Y={y:.6f}，"
                  f"朝向{names[facing]}（双桩 {len(posts)}/2）。")
        )
        return True

    def _combo_attack_slots_data(self) -> list[dict]:
        """Return the three normalized combo-slot mappings for persistence."""

        result = []
        count_vars = getattr(self, "_combo_attack_count_vars", ())
        key_vars = getattr(self, "_combo_attack_key_vars", ())
        for index in range(3):
            try:
                minimum = int(count_vars[index][0].get())
            except (IndexError, TypeError, ValueError, tk.TclError):
                minimum = 1
            try:
                maximum = int(count_vars[index][1].get())
            except (IndexError, TypeError, ValueError, tk.TclError):
                maximum = minimum
            minimum = max(0, min(30, minimum))
            maximum = max(0, min(30, maximum))
            if minimum > maximum:
                minimum, maximum = maximum, minimum
            if index < len(count_vars):
                count_vars[index][0].set(minimum)
                count_vars[index][1].set(maximum)
            key = "-"
            if index < len(key_vars):
                candidate = str(key_vars[index].get()).strip().casefold()
                key = candidate if candidate in BINDABLE_KEYS else "-"
                key_vars[index].set(key)
            result.append({
                "min_count": minimum,
                "max_count": maximum,
                "key": key,
            })
        return result

    def _combo_attack_hint(self) -> str:
        """Build the live Chinese tooltip for the three combo slots."""

        slots = self._combo_attack_slots_data()
        pieces = []
        for slot in slots:
            key = "不打" if slot["key"] == "-" else slot["key"]
            pieces.append(
                f"{key} {slot['min_count']}-{slot['max_count']}次"
            )
        return "组合攻击: " + ", ".join(pieces) + ", 绑定 - 代表不打"

    def _fixed_random_gap_seconds(self) -> float:
        """Return the clamped random-gap setting in 0.05-second precision."""

        var = getattr(self, "_fixed_random_gap_var", None)
        raw = 0.1 if var is None else float(var.get())
        return round(max(0.0, min(self._FIXED_RANDOM_GAP_MAX, raw)), 2)

    def _bind_repeat_step_button(
        self,
        button: Any,
        callback: Any,
        *,
        coarse: Any = None,
        current: Any = None,
    ) -> None:
        """Give a ± random-gap button click-and-hold acceleration.

        A press applies one precise 0.1 s adjustment immediately.  Keeping
        the mouse down for 0.25 s then repeats every 100 ms, so reaching a
        useful multi-second random range does not require dozens of clicks.
        Release cancels the repeat without a trailing adjustment.

        A DOUBLE click applies one coarse step instead (``coarse``, wired to
        ``_RANDOM_GAP_COARSE_STEP`` = 5 s), measured from the value the click
        sequence started at (``current``), so the fine steps of its own presses
        cannot turn 5 s into 5.1 s.
        """

        jobs = getattr(self, "_repeat_step_jobs", None)
        if jobs is None:
            jobs = {}
            self._repeat_step_jobs = jobs
        anchors = getattr(self, "_repeat_step_anchors", None)
        if anchors is None:
            anchors = {}
            self._repeat_step_anchors = anchors
        key = id(button)

        def stop_repeat(_event: Any = None) -> None:
            job = jobs.pop(key, None)
            if job is not None:
                try:
                    button.after_cancel(job)
                except Exception:
                    pass

        def repeat() -> None:
            # The entry disappears before a scheduled callback can run when
            # the mouse button is released, so it cannot make an extra step.
            if key not in jobs:
                return
            try:
                callback()
                jobs[key] = button.after(100, repeat)
            except Exception:
                jobs.pop(key, None)
                LOG.debug("random-gap hold repeat stopped", exc_info=True)

        def start_repeat(_event: Any = None) -> None:
            stop_repeat()
            now = time.monotonic()
            anchor = anchors.get(key)
            if (anchor is None
                    or now - anchor[0]
                    > self._RANDOM_GAP_CLICK_SEQUENCE_SECONDS):
                anchors[key] = (
                    now,
                    float(current()) if callable(current) else None,
                )
            callback()
            try:
                jobs[key] = button.after(250, repeat)
            except Exception:
                jobs.pop(key, None)
                LOG.debug("random-gap hold repeat unavailable", exc_info=True)

            # This is a setting stepper, not an action button.  Clear the
            # native pressed state after the class binding has run so its
            # visual style remains stable throughout a long hold.  We still
            # allow the normal release handler to run and stop the timer.
            def clear_pressed_style() -> None:
                try:
                    button.state(("!pressed",))
                except Exception:
                    pass
            try:
                button.after_idle(clear_pressed_style)
            except Exception:
                pass

        def coarse_step(_event: Any = None) -> None:
            """Double click: one coarse step from where the sequence started."""

            stop_repeat()
            anchor = anchors.pop(key, None)
            if not callable(coarse):
                return
            try:
                coarse(None if anchor is None else anchor[1])
            except Exception:
                LOG.exception("random-gap coarse step failed")

        button.bind("<ButtonPress-1>", start_repeat)
        button.bind("<ButtonRelease-1>", stop_repeat)
        button.bind("<Double-Button-1>", coarse_step)
        # ttk.Button takes a pointer grab while clicked, so its release event
        # still arrives even if the pointer is moved outside.  Do not cancel
        # on <Leave>: refreshing a packed row can synthesize Leave and used
        # to silently stop the repeat after its first adjustment.

    def _set_fixed_random_gap(self, value: Optional[float]) -> None:
        """Apply an absolute random-delay ceiling, clamped and rounded."""

        base = self._fixed_random_gap_seconds() if value is None else value
        self._fixed_random_gap_var.set(round(
            max(0.0, min(self._FIXED_RANDOM_GAP_MAX, float(base))), 2
        ))
        self._fixed_on_change()

    def _fixed_adjust_random_gap(self, delta: float) -> None:
        """Adjust the random delay ceiling.

        ``delta``'s sign picks the direction and its magnitude the step, so the
        fine button passes ±0.05 while the double click passes ±5.
        """

        step = abs(delta) or self._FIXED_RANDOM_GAP_STEP
        self._set_fixed_random_gap(
            self._fixed_random_gap_seconds() + (step if delta > 0 else -step)
        )

    def _random_jump_gap_seconds(self) -> float:
        """Return the clamped random-jump delay ceiling."""

        var = getattr(self, "_random_jump_gap_var", None)
        raw = 0.1 if var is None else float(var.get())
        return round(max(0.0, min(self._FIXED_RANDOM_GAP_MAX, raw)), 1)

    def _set_random_jump_gap(self, value: Optional[float]) -> None:
        """Apply an absolute random-jump delay ceiling, clamped and rounded."""

        base = self._random_jump_gap_seconds() if value is None else value
        self._random_jump_gap_var.set(round(
            max(0.0, min(self._FIXED_RANDOM_GAP_MAX, float(base))), 1
        ))
        self._fixed_on_change()

    def _random_jump_adjust_gap(self, delta: float) -> None:
        step = abs(delta) or self._FIXED_RANDOM_GAP_STEP
        self._set_random_jump_gap(
            self._random_jump_gap_seconds() + (step if delta > 0 else -step)
        )

    def _small_step_gap_seconds(self) -> float:
        minimum, maximum = self._small_step_range_seconds()
        return round(maximum - minimum, 1)

    def _small_step_range_seconds(self) -> tuple[float, float]:
        control = getattr(self, "_small_step_control", None)
        if control is None:
            return 5.0, 5.0
        minimum, maximum = control.get_bounds()
        return float(minimum), float(maximum)

    def _small_step_post_attack_seconds(self) -> float:
        """Return 小碎步's single post-attack quiet period in seconds."""

        control = getattr(self, "_small_step_post_attack_control", None)
        if control is None:
            return 0.70
        minimum, _maximum = control.get_bounds()
        return float(minimum)

    def _set_small_step_gap(self, value: Optional[float]) -> None:
        """Compatibility helper: express the old gap as a range maximum."""
        base = self._small_step_gap_seconds() if value is None else value
        control = getattr(self, "_small_step_control", None)
        if control is None:
            return
        minimum, _maximum = self._small_step_range_seconds()
        control.set_bounds(minimum, minimum + max(0.0, float(base)))
        self._fixed_on_change()

    def _small_step_adjust_gap(self, delta: float) -> None:
        step = abs(delta) or self._FIXED_RANDOM_GAP_STEP
        self._set_small_step_gap(
            self._small_step_gap_seconds() + (step if delta > 0 else -step)
        )

    def _stationary_pickup_interval_minutes(self) -> float:
        """Return the 捡东西 trigger interval in MINUTES (this row's unit)."""

        minimum, _maximum = self._stationary_pickup_range_minutes()
        return minimum

    def _stationary_pickup_range_minutes(self) -> tuple[float, float]:
        control = getattr(self, "_stationary_pickup_control", None)
        if control is None:
            value = self._STATIONARY_PICKUP_INTERVAL_DEFAULT_MINUTES
            return value, value
        minimum, maximum = control.get_bounds()
        return float(minimum), float(maximum)

    @staticmethod
    def _format_stationary_pickup_interval(minutes: float) -> str:
        """Show the short pickup interval range precisely in seconds."""

        seconds = max(0.0, float(minutes) * 60.0)
        if seconds < 60.0:
            return f"{seconds:.0f}s"
        return f"{float(minutes):.1f}m"

    def _stationary_pickup_gap_minutes(self) -> float:
        """Return the 捡东西 random interval in MINUTES (this row's unit)."""
        minimum, maximum = self._stationary_pickup_range_minutes()
        return round(maximum - minimum, 1)

    def _set_stationary_pickup_gap(self, value: Optional[float]) -> None:
        base = self._stationary_pickup_gap_minutes() if value is None else value
        control = getattr(self, "_stationary_pickup_control", None)
        if control is None:
            return
        minimum, _maximum = self._stationary_pickup_range_minutes()
        control.set_bounds(minimum, minimum + max(0.0, float(base)))
        self._fixed_on_change()

    def _stationary_pickup_adjust_gap(self, delta: float) -> None:
        step = abs(delta) or self._FIXED_RANDOM_GAP_STEP
        self._set_stationary_pickup_gap(
            self._stationary_pickup_gap_minutes() + (step if delta > 0 else -step)
        )

    def _hotkey_adjust_fixed_interval(self, delta: float) -> bool:
        """Shift both endpoints of the attack range for Ctrl+[ / Ctrl+]."""

        if (not hasattr(self, "_fixed_min_interval_var")
                or self._root is None):
            return False
        minimum, maximum = self._fixed_attack_range()
        span = maximum - minimum
        shifted_minimum = round(max(
            0.2, min(self._FIXED_ATTACK_INTERVAL_MAX - span,
                     minimum + float(delta))
        ), 2)
        shifted_maximum = round(shifted_minimum + span, 2)
        if (shifted_minimum == minimum
                and shifted_maximum == maximum):
            return False
        self._fixed_min_interval_var.set(shifted_minimum)
        self._fixed_max_interval_var.set(shifted_maximum)
        self._fixed_on_change()

        previous = self._attack_hotkey_sound_job
        if previous is not None:
            try:
                self._root.after_cancel(previous)
            except Exception:
                pass
        self._attack_hotkey_sound_job = self._root.after(
            2000, self._finish_hotkey_attack_adjustment
        )
        return True

    def _finish_hotkey_attack_adjustment(self) -> None:
        """Play one success tone after the held interval adjustment settles."""

        self._attack_hotkey_sound_job = None
        self._play_action_sound(True)

    def _fixed_refresh_rows(self) -> None:
        """Update stationary-only controls without changing panel geometry."""

        mode = str(getattr(self, "_attack_mode_var", None).get()
                   if hasattr(self, "_attack_mode_var") else "fixed")
        # This method runs again after several ordinary UI refreshes.  It must
        # never reopen stationary controls that the authorization lock closed.
        fixed_attack_enabled = (
            mode in ("fixed", "flower_patrol", "stationary", "dual_stationary") and self._license_allowed()
        )
        stationary_enabled = mode == "stationary" and self._license_allowed()
        dual_stationary_enabled = mode == "dual_stationary" and self._license_allowed()
        flower_cycle_enabled = mode in ("stationary", "dual_stationary", "dead_flower") and self._license_allowed()
        dead_flower_mode = mode == "dead_flower"
        # 随机休息 only exists where the character patrols or stands on a 桩.  It
        # shares a row with 延时回桩/跳反打, so only the checkbox is gated - the
        # row's other controls keep their own rules.
        rest_allowed = (
            mode in ("fixed", "stationary", "dual_stationary")
            and self._license_allowed()
        )
        rest_check = getattr(self, "_random_rest_check", None)
        if rest_check is not None:
            try:
                rest_check.state(["!disabled" if rest_allowed else "disabled"])
            except Exception:
                rest_check.configure(state="normal" if rest_allowed else "disabled")
        rest_worker = getattr(self, "random_rest_worker", None)
        rest_var = getattr(self, "_random_rest_var", None)
        if rest_worker is not None and rest_var is not None:
            rest_setter = getattr(rest_worker, "set_enabled", None)
            if callable(rest_setter):
                rest_setter(bool(rest_var.get()) and rest_allowed)
        pickup_label = getattr(self, "_stationary_pickup_enabled_button", None)
        if pickup_label is not None:
            # The same persisted interval control drives two different route
            # machines. Its meaning follows the active mode without changing
            # the backwards-compatible saved configuration fields.
            pickup_label.configure(text="重置花" if dead_flower_mode else "定时巡逻")
        for row in getattr(self, "_dead_flower_attack_rows", ()):
            if row is not None:
                self._set_panel_state(
                    row,
                    disabled=(dead_flower_mode or not self._license_allowed()),
                )
        # Counterattack, pickup/reset and 小碎步 are intentionally one fixed
        # third row.  Outside 站桩/双桩 it remains visible for orientation but
        # is greyed and cannot alter the live route settings.
        stationary_action_row = getattr(self, "_stationary_action_row", None)
        if stationary_action_row is not None:
            self._set_panel_state(
                stationary_action_row,
                disabled=not (mode in ("stationary", "dual_stationary")
                              and self._license_allowed()),
            )
        for widget in getattr(self, "_attack_delivery_buttons", ()):
            if widget is None:
                continue
            try:
                widget.state(["!disabled" if fixed_attack_enabled else "disabled"])
            except Exception:
                widget.configure(
                    state="normal" if fixed_attack_enabled else "disabled"
                )
        delivery_var = getattr(self, "_attack_delivery_var", None)
        charge_enabled = bool(
            fixed_attack_enabled
            and delivery_var is not None
            and str(delivery_var.get()) == "charge"
        )
        jump_counterattack_enabled = bool(
            fixed_attack_enabled
            and delivery_var is not None
            and str(delivery_var.get()) == "jump"
        )
        jump_counterattack_button = getattr(self, "_jump_counterattack_button", None)
        if jump_counterattack_button is not None:
            try:
                jump_counterattack_button.state([
                    "!disabled" if jump_counterattack_enabled else "disabled"
                ])
            except Exception:
                jump_counterattack_button.configure(
                    state="normal" if jump_counterattack_enabled else "disabled"
                )
        charge_label = getattr(self, "_charge_hold_label", None)
        if charge_label is not None:
            try:
                charge_label.state(["!disabled" if charge_enabled else "disabled"])
            except Exception:
                charge_label.configure(state="normal" if charge_enabled else "disabled")
        charge_control = getattr(self, "_charge_hold_control", None)
        if charge_control is not None:
            charge_control.set_enabled(charge_enabled)
        for widget in getattr(self, "_stationary_facing_controls", []):
            if widget is None:
                continue
            try:
                widget.state(["!disabled" if stationary_enabled else "disabled"])
            except Exception:
                widget.configure(state="normal" if stationary_enabled else "disabled")
        stationary_options = getattr(self, "_stationary_options_row", None)
        stationary_facing_row = getattr(self, "_stationary_facing_row", None)
        pickup_row = getattr(self, "_stationary_pickup_row", None)
        small_step_row = getattr(self, "_small_step_options_row", None)
        delayed_return_row = getattr(self, "_stationary_delayed_return_row", None)
        dual_row = getattr(self, "_dual_stationary_row", None)
        # Keep the fourth options row mounted in every mode.  Dead-flower
        # mode must grey unavailable patrol-attack controls rather than
        # removing/recreating them: Tk's pack relayout can visibly resize or
        # flicker the whole panel during a mode switch.  The per-control state
        # gates above decide which controls can actually be used.
        #
        # One compact, mode-specific third-row slot remains: stationary mode
        # shows 朝向; dual-stationary mode shows 换桩.  That slot has a fixed
        # parent-row height, so changing its child does not change panel size.
        if stationary_options is not None and not stationary_options.winfo_manager():
            stationary_options.pack(fill="x", pady=(2, 0))
        if pickup_row is not None and not pickup_row.winfo_manager():
            pickup_row.pack(side="left")
        if small_step_row is not None and not small_step_row.winfo_manager():
            small_step_row.pack(side="left", padx=(4, 0))
        if delayed_return_row is not None and not delayed_return_row.winfo_manager():
            delayed_return_row.pack(fill="x", pady=(2, 0))
        if stationary_facing_row is not None:
            stationary_facing_row.pack_forget()
        if dual_row is not None:
            dual_row.pack_forget()
        if stationary_enabled and stationary_facing_row is not None:
            stationary_facing_row.pack(side="left", padx=(8, 0))
        elif dual_stationary_enabled and dual_row is not None:
            dual_row.pack(side="left", padx=(8, 0))
        if dual_row is not None:
            self._set_panel_state(dual_row, disabled=not dual_stationary_enabled)
        for widget in getattr(self, "_stationary_pickup_controls", []):
            if widget is None:
                continue
            try:
                widget.state(["!disabled" if flower_cycle_enabled else "disabled"])
            except Exception:
                widget.configure(state="normal" if flower_cycle_enabled else "disabled")
        for widget in getattr(self, "_stationary_delayed_return_controls", []):
            if widget is None:
                continue
            try:
                widget.state([
                    "!disabled" if (mode in ("stationary", "dual_stationary")
                                      and self._license_allowed()) else "disabled"
                ])
            except Exception:
                widget.configure(
                    state=("normal" if (mode in ("stationary", "dual_stationary")
                                      and self._license_allowed()) else "disabled")
                )

    def _fixed_attack_range(self, changed: Optional[str] = None) -> tuple[float, float]:
        """Return the reusable cadence component's ordered range."""

        control = getattr(self, "_fixed_interval_control", None)
        if control is None:
            return 3.0, 3.1
        minimum, maximum = control.normalize(changed=changed)
        return float(minimum), float(maximum)

    def _adjust_fixed_interval_bound(self, side: str, delta: float) -> None:
        control = getattr(self, "_fixed_interval_control", None)
        if control is None:
            return
        variable = control.minimum_var if side == "min" else control.maximum_var
        variable.set(float(variable.get()) + float(delta))
        control.normalize(changed=side)
        self._fixed_on_change()

    def _fixed_on_change(self, _value: str = "") -> None:
        """Update labels, persist, and apply the fixed-attack settings live."""

        if not hasattr(self, "_fixed_min_interval_label"):
            return
        self._undo_capture_panel_change("攻击设置")
        mode = str(self._attack_mode_var.get())
        if (not self._YOLO_MONSTER_DETECTION_ENABLED
                and mode not in ("fixed", "flower_patrol", "stationary", "dual_stationary", "dead_flower")):
            mode = "fixed"
            self._attack_mode_var.set("fixed")
        delivery = str(
            self._attack_delivery_var.get()
            if hasattr(self, "_attack_delivery_var") else "stand"
        )
        if delivery not in ("stand", "jump", "charge"):
            delivery = "stand"
            self._attack_delivery_var.set(delivery)
        if hasattr(self, "_jump_attack_enabled_var"):
            # Existing jump-attack code remains the only implemented special
            # delivery path. The other two choices are UI-only for now.
            self._jump_attack_enabled_var.set(delivery == "jump")
        # Text-value wheels are applied in 0.05s steps.  There is no slider
        # drag or additive random-gap control on this row anymore.
        interval_min, interval_max = self._fixed_attack_range()
        self._charge_hold_range_seconds()
        for control_name in (
            "_small_step_control",
            "_small_step_post_attack_control",
            "_stationary_pickup_control",
            "_dual_stationary_switch_control",
        ):
            control = getattr(self, control_name, None)
            if control is not None:
                control.normalize()
        self._refresh_dual_stationary_status()
        if hasattr(self, "_fixed_key_button"):
            self._fixed_key_button.configure(
                text=self._fixed_attack_key_var.get()
            )
        if hasattr(self, "_counterattack_key_button"):
            self._counterattack_key_button.configure(
                text=self._counterattack_key_var.get()
            )
        for button, var in zip(
            getattr(self, "_combo_attack_key_buttons", ()),
            getattr(self, "_combo_attack_key_vars", ()),
        ):
            button.configure(text=var.get())
        combo_tooltip = getattr(self, "_combo_attack_tooltip", None)
        if combo_tooltip is not None:
            combo_tooltip.text = self._combo_attack_hint()
        data = self._fixed_collect_data()
        self._fixed_save_settings(data)
        self._fixed_apply_to_worker(data)
        # Delivery radio buttons call this method directly, not the mode
        # change handler.  Refresh its dependent charge-only control here.
        self._fixed_refresh_rows()
        self._fixed_refresh_grey()

    def _fixed_on_mode_change(self) -> None:
        """Attack mode radio changed: refresh visibility once, then apply."""

        self._fixed_refresh_rows()
        self._fixed_on_change()

    def _fixed_save_settings(self, data: dict) -> None:
        """Persist the Fixed Attack panel values to the local JSON file."""

        try:
            self._fixed_settings_path().write_text(
                json.dumps(data, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            LOG.warning("could not save fixed attack settings", exc_info=True)

    def _fixed_apply_to_worker(self, data: dict) -> None:
        """Apply the fixed-attack settings to the AttackWorker live."""

        worker = getattr(self, "attack_worker", None)
        if worker is None:
            self._fixed_status.configure(
                text="巡逻攻击: 工作线程未接入 (无界面模式)。"
            )
            return
        # Settings may be refreshed after an authorization failure (for
        # example by a slider callback).  Do not let that repaint reactivate
        # any attack, facing, jump-attack, or pickup behavior.
        if not self._license_allowed():
            worker.enabled = False
            worker.jump_attack = False
            worker.set_combo_attack(False, ())
            step_worker = getattr(self, "small_step_worker", None)
            if step_worker is not None:
                step_worker.enabled = False
            mover = getattr(self, "movement_worker", None)
            if mover is not None:
                stationary_setter = getattr(
                    mover, "set_stationary_attack_enabled", None
                )
                if callable(stationary_setter):
                    stationary_setter(False)
                pickup_setter = getattr(mover, "set_stationary_pickup_schedule", None)
                if callable(pickup_setter):
                    pickup_setter(False, 0.0, 0.0)
                flower_cycle_setter = getattr(mover, "set_dead_flower_ground_schedule", None)
                if callable(flower_cycle_setter):
                    flower_cycle_setter(False, 0.0, 0.0)
                flower_patrol_setter = getattr(mover, "set_flower_patrol_mode", None)
                if callable(flower_patrol_setter):
                    flower_patrol_setter(False)
                counterattack_setter = getattr(mover, "set_counterattack", None)
                if callable(counterattack_setter):
                    counterattack_setter(False, "ctrl")
            return
        mode = str(data.get("attack_mode", "fixed"))
        if (not self._YOLO_MONSTER_DETECTION_ENABLED
                and mode not in ("fixed", "flower_patrol", "stationary", "dual_stationary", "dead_flower")):
            mode = "fixed"
        dead_flower_mode = mode == "dead_flower"
        dual_stationary_mode = mode == "dual_stationary"
        delivery = str(data.get("attack_delivery", "stand")).casefold()
        if delivery not in ("stand", "jump", "charge", "none"):
            delivery = "jump" if bool(data.get(
                "jump_attack_enabled", data.get("stationary_jump_enabled", False),
            )) else "stand"
        jump_attack_enabled = delivery == "jump" and mode in (
            "fixed", "flower_patrol", "stationary", "dual_stationary",
        )
        legacy_charge = float(data.get("charge_hold_seconds", 0.5))
        charge_min_seconds = max(0.1, min(
            5.0, float(data.get("charge_min_seconds", legacy_charge)),
        ))
        charge_max_seconds = max(charge_min_seconds, min(
            5.0, float(data.get("charge_max_seconds", charge_min_seconds)),
        ))
        worker.jump_attack = jump_attack_enabled
        worker.jump_counterattack_enabled = bool(
            data.get("jump_counterattack_enabled", False)
            and jump_attack_enabled
        )
        worker.attack_delivery = delivery
        worker.charge_min_seconds = charge_min_seconds
        worker.charge_max_seconds = charge_max_seconds
        flower_patrol_mode = mode == "flower_patrol"
        worker.enabled = bool(mode in ("fixed", "flower_patrol", "stationary", "dual_stationary"))
        gate_setter = getattr(worker, "set_layer_attack_gate", None)
        if callable(gate_setter):
            gate_setter(flower_patrol_mode)
        # 站桩 shares its beats with the anchor correction and 小碎步, which carry
        # their own attacks; on patrol those are deliberate extras, so only the
        # stand-still cadence keeps the attack-spacing rule (attack_worker).
        worker.stationary_mode = bool(mode in ("stationary", "dual_stationary"))
        # New range fields replace the former base interval + random gap.
        # Accept old local settings for one launch, then the next save writes
        # the explicit min/max pair only.
        legacy_minimum = float(data.get("interval_seconds", 3.0))
        minimum = float(data.get("min_interval_seconds", legacy_minimum))
        maximum = float(data.get(
            "max_interval_seconds",
            legacy_minimum + float(data.get("random_gap_seconds", 0.0)),
        ))
        minimum = max(0.2, min(self._FIXED_ATTACK_INTERVAL_MAX, minimum))
        maximum = max(minimum, min(self._FIXED_ATTACK_INTERVAL_MAX, maximum))
        worker.attack_min_interval = minimum
        worker.attack_max_interval = maximum
        # Compatibility aliases for integrations which still inspect these
        # fields; scheduling itself uses the explicit range above.
        worker.attack_interval = minimum
        worker.attack_jitter_seconds = maximum - minimum
        key = str(data.get("attack_key", "ctrl")).strip()
        if not worker.set_key(key):
            LOG.warning("fixed attack key %r unsupported; keeping %r",
                        key, worker.attack_key)
        worker.set_combo_attack(
            bool(data.get("combo_attack_enabled", False)) and not dead_flower_mode
            and key != "-" and delivery != "none",
            data.get("combo_attack_slots", ()),
        )
        mover = getattr(self, "movement_worker", None)
        if mover is not None:
            dead_flower_setter = getattr(mover, "set_dead_flower_mode", None)
            if callable(dead_flower_setter):
                dead_flower_setter(dead_flower_mode)
            flower_patrol_setter = getattr(mover, "set_flower_patrol_mode", None)
            if callable(flower_patrol_setter):
                flower_patrol_setter(flower_patrol_mode)
            mover.small_step_attack_key = worker.attack_key
            delivery_setter = getattr(mover, "set_attack_delivery", None)
            if callable(delivery_setter):
                delivery_setter(delivery, charge_min_seconds, charge_max_seconds)
            else:
                jump_setter = getattr(mover, "set_jump_attack_enabled", None)
                if callable(jump_setter):
                    jump_setter(jump_attack_enabled)
            jump_counterattack_setter = getattr(
                mover, "set_jump_counterattack_enabled", None,
            )
            if callable(jump_counterattack_setter):
                jump_counterattack_setter(bool(
                    data.get("jump_counterattack_enabled", False)
                    and jump_attack_enabled
                ))
            counterattack_setter = getattr(mover, "set_counterattack", None)
            if callable(counterattack_setter):
                counterattack_setter(
                    bool(data.get("counterattack_enabled", False)) and not dead_flower_mode,
                    str(data.get("counterattack_key", "ctrl")).strip(),
                )
            stationary_setter = getattr(
                mover, "set_stationary_attack_enabled", None
            )
            if callable(stationary_setter):
                stationary_setter(mode in ("stationary", "dual_stationary"))
            dual_setter = getattr(mover, "set_dual_stationary_configuration", None)
            if callable(dual_setter):
                legacy_switch_minutes = float(
                    data.get("dual_stationary_switch_seconds", 900.0)
                ) / 60.0
                dual_switch_min = float(data.get(
                    "dual_stationary_switch_min_minutes", legacy_switch_minutes,
                ))
                dual_switch_max = float(data.get(
                    "dual_stationary_switch_max_minutes", dual_switch_min,
                ))
                dual_setter(
                    dual_stationary_mode,
                    data.get("dual_stationary_posts", ()),
                    dual_switch_min * 60.0,
                    max(0.0, dual_switch_max - dual_switch_min) * 60.0,
                )
            # Automatic stuck stair-jumps are intentionally disabled.  The
            # dedicated jump executor remains solely for recorded jump points.
            mover.stair_jump_enabled = False
        step_worker = getattr(self, "small_step_worker", None)
        if step_worker is not None:
            # 小碎步 is available for fixed and stand-still attack. The latter
            # uses it chiefly to finish facing the monster side.
            step_worker.enabled = bool(
                data.get("small_step_enabled", False)
            ) and mode in ("fixed", "flower_patrol", "stationary", "dual_stationary")
            legacy_step_min = float(data.get("small_step_interval_seconds", 5.0))
            step_min = float(data.get("small_step_min_seconds", legacy_step_min))
            step_max = float(data.get(
                "small_step_max_seconds",
                step_min + float(data.get("small_step_gap_seconds", 0.0)),
            ))
            step_worker.step_interval = max(5.0, step_min)
            step_worker.step_jitter_seconds = max(
                0.0, step_max - step_worker.step_interval
            )
        if mover is not None:
            mover.small_step_post_attack_seconds = max(0.20, min(
                5.00,
                float(data.get("small_step_post_attack_seconds", 0.70)),
            ))
        if mover is not None:
            direction = str(data.get("stationary_facing_direction", "right"))
            setter = getattr(mover, "set_stationary_facing_direction", None)
            if callable(setter):
                setter(direction)
            else:
                mover.stationary_facing_direction = (
                    direction if direction in ("left", "right", "both") else "right"
                )
            pickup_setter = getattr(mover, "set_stationary_pickup_schedule", None)
            if callable(pickup_setter):
                legacy_pickup_min = float(
                    data.get("stationary_pickup_interval_seconds", 900.0)
                ) / 60.0
                pickup_min = float(data.get(
                    "stationary_pickup_min_minutes", legacy_pickup_min,
                ))
                pickup_max = float(data.get(
                    "stationary_pickup_max_minutes",
                    pickup_min + float(data.get(
                        "stationary_pickup_gap_minutes",
                        float(data.get("stationary_pickup_gap_seconds", 0.0)) / 60.0,
                    )),
                ))
                pickup_setter(
                    bool(data.get("stationary_pickup_enabled", False))
                    and mode in ("stationary", "dual_stationary"),
                    pickup_min * 60.0,
                    max(0.0, pickup_max - pickup_min) * 60.0,
                )
            delayed_return_setter = getattr(
                mover, "set_stationary_delayed_return_enabled", None,
            )
            if callable(delayed_return_setter):
                delayed_return_setter(
                    bool(data.get("stationary_delayed_return_enabled", False))
                    and mode in ("stationary", "dual_stationary")
                )
            flower_cycle_setter = getattr(mover, "set_dead_flower_ground_schedule", None)
            if callable(flower_cycle_setter):
                legacy_pickup_min = float(
                    data.get("stationary_pickup_interval_seconds", 900.0)
                ) / 60.0
                pickup_min = float(data.get(
                    "stationary_pickup_min_minutes", legacy_pickup_min,
                ))
                pickup_max = float(data.get("stationary_pickup_max_minutes", pickup_min))
                flower_cycle_setter(
                    bool(data.get("stationary_pickup_enabled", False))
                    and dead_flower_mode,
                    pickup_min * 60.0,
                    max(0.0, pickup_max - pickup_min) * 60.0,
                )

    def _fixed_refresh_grey(self) -> None:
        """Grey the YOLO panel + update status lines for the active mode."""

        mode = str(self._attack_mode_var.get())
        fixed_mode = (
            not self._YOLO_MONSTER_DETECTION_ENABLED
            or mode in ("fixed", "flower_patrol", "stationary", "dual_stationary", "dead_flower")
        )
        if fixed_mode:
            # Only one attack engine at a time: selecting Fixed Attack stops
            # a running YOLO detection subprocess.
            proc = getattr(self, "_yolo_process", None)
            if proc is not None and proc.poll() is None:
                self._yolo_stop()
        # The jump-rope logic follows the mode: YOLO screen gap when YOLO
        # detection is the active engine, minimap logic when the fixed-rate
        # mode (no YOLO subprocess) is selected.
        mover = getattr(self, "movement_worker", None)
        if mover is not None:
            setter = getattr(mover, "set_yolo_detection_active", None)
            if setter is not None:
                setter(not fixed_mode)
        panel = getattr(self, "_yolo_panel", None)
        if panel is not None:
            self._set_panel_state(panel, fixed_mode)
        if hasattr(self, "_fixed_status"):
            if fixed_mode:
                interval_min, interval_max = self._fixed_attack_range()
                interval_text = (
                    f"{interval_min:.2f}".rstrip("0").rstrip(".")
                    + "–"
                    + f"{interval_max:.2f}".rstrip("0").rstrip(".")
                )
                jump_delivery_text = ""
                if self._jump_attack_enabled_var.get():
                    jump_delivery_text = (
                        "跳反打已启用；" if bool(
                            getattr(self, "_jump_counterattack_enabled_var", None).get()
                            if hasattr(self, "_jump_counterattack_enabled_var") else False
                        ) else "跳跃后 100ms 攻击；"
                    )
                if mode == "dead_flower":
                    self._fixed_status.configure(
                        text="死花模式已启用 - 双层花点按 补药 定时上下移动；单层仅 Buff 与 HP/MP。"
                    )
                elif mode == "stationary":
                    self._fixed_status.configure(
                        text=(f"站桩攻击已启用 - 按键 "
                              f"{self._fixed_attack_key_var.get()}；"
                              f"开始运行时记录临时位置；"
                              f"只有正确配置楼层，才能寻路回去；"
                              f"{jump_delivery_text}间隔 "
                              f"{interval_text}s。")
                    )
                elif mode == "dual_stationary":
                    posts = len(self._dual_stationary_posts_data())
                    self._fixed_status.configure(
                        text=(f"双桩模式已启用 - 已录制 {posts}/2 桩；"
                              f"Ctrl+R 后按 D/F/G 录制朝向；每 "
                              f"{self._dual_stationary_switch_minutes():.1f}m 换桩。")
                    )
                elif mode == "flower_patrol":
                    self._fixed_status.configure(
                        text=(f"跑花模式已启用 - 正常巡逻；仅楼层1攻击；按键 "
                              f"{self._fixed_attack_key_var.get()}；"
                              f"间隔 {interval_text}s。")
                    )
                else:
                    self._fixed_status.configure(
                        text=(f"巡逻攻击已启用 - 按键 "
                              f"{self._fixed_attack_key_var.get()}；"
                              f"{jump_delivery_text}间隔 "
                              f"{interval_text}s。")
                    )
            else:
                self._fixed_status.configure(
                    text="巡逻攻击未启用 - 使用 YOLO 检测模式。"
                )
        if hasattr(self, "_yolo_status"):
            if fixed_mode:
                self._yolo_status.configure(text="暂时停用。")
            else:
                self._yolo_status.configure(text="YOLO 检测已停止。")

    def _set_panel_state(self, panel: Any, disabled: bool) -> None:
        """Enable/disable every widget inside *panel* (ttk or tk)."""

        state = "disabled" if disabled else "!disabled"
        for child in panel.winfo_children():
            try:
                child.state([state])
            except Exception:
                try:
                    child.configure(
                        state="disabled" if disabled else "normal"
                    )
                except Exception:
                    pass
            # Most YOLO controls are nested inside row frames. Disabling only
            # the immediate frames does not disable their buttons on Tk, so
            # recurse to make the temporary feature switch effective.
            if callable(getattr(child, "winfo_children", None)):
                self._set_panel_state(child, disabled)

    def _fixed_load_settings(self) -> None:
        """Restore saved Fixed Attack panel values and apply them live."""

        try:
            data = json.loads(
                self._fixed_settings_path().read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            data = {}
        try:
            if "attack_mode" in data:
                mode = str(data["attack_mode"])
                if mode == "jump_attack":
                    # One-time migration from the former dedicated mode.
                    self._attack_mode_var.set("stationary")
                    if hasattr(self, "_jump_attack_enabled_var"):
                        self._jump_attack_enabled_var.set(True)
                elif mode in ("yolo", "fixed", "flower_patrol", "stationary", "dual_stationary", "dead_flower"):
                    self._attack_mode_var.set(mode)
            if not self._YOLO_MONSTER_DETECTION_ENABLED:
                if self._attack_mode_var.get() not in ("fixed", "flower_patrol", "stationary", "dual_stationary", "dead_flower"):
                    self._attack_mode_var.set("fixed")
            # 随机休息: restore the saved choice; the mode rules decide afterwards
            # whether it is actually in effect (``_fixed_refresh_rows``).
            if hasattr(self, "_random_rest_var"):
                self._random_rest_var.set(bool(data.get("random_rest_enabled", False)))
            # Migrate old ``interval + random_gap`` values into the new
            # explicit inclusive interval range.  A legacy 0.90 + 0.10s
            # setting therefore becomes 0.90–1.00s with identical timing.
            legacy_minimum = float(data.get("interval_seconds", 3.0))
            minimum = float(data.get("min_interval_seconds", legacy_minimum))
            maximum = float(data.get(
                "max_interval_seconds",
                legacy_minimum + float(data.get("random_gap_seconds", 0.1)),
            ))
            self._fixed_min_interval_var.set(minimum)
            self._fixed_max_interval_var.set(maximum)
            self._fixed_attack_range()
            if "attack_key" in data:
                key = str(data["attack_key"]).strip()
                if key in BINDABLE_KEYS or key == "-":
                    self._fixed_attack_key_var.set(key)
            if hasattr(self, "_counterattack_enabled_var"):
                self._counterattack_enabled_var.set(bool(
                    data.get("counterattack_enabled", False)
                ))
            if hasattr(self, "_counterattack_key_var"):
                key = str(data.get("counterattack_key", "ctrl")).strip()
                if key in BINDABLE_KEYS and key != "-":
                    self._counterattack_key_var.set(key)
            if hasattr(self, "_combo_attack_enabled_var"):
                self._combo_attack_enabled_var.set(bool(
                    data.get("combo_attack_enabled", False)
                ))
            slots = data.get("combo_attack_slots", ())
            if isinstance(slots, list):
                for index, slot in enumerate(slots[:3]):
                    if not isinstance(slot, dict):
                        continue
                    try:
                        minimum = int(slot.get("min_count", 1))
                        maximum = int(slot.get("max_count", minimum))
                    except (TypeError, ValueError):
                        minimum, maximum = 1, 1
                    minimum = max(0, min(30, minimum))
                    maximum = max(0, min(30, maximum))
                    if minimum > maximum:
                        minimum, maximum = maximum, minimum
                    if index < len(getattr(self, "_combo_attack_count_vars", ())):
                        self._combo_attack_count_vars[index][0].set(minimum)
                        self._combo_attack_count_vars[index][1].set(maximum)
                    raw_key = str(slot.get("key", "-")).strip().casefold()
                    key = raw_key if raw_key in BINDABLE_KEYS else "-"
                    if index < len(getattr(self, "_combo_attack_key_vars", ())):
                        self._combo_attack_key_vars[index].set(key)
            if hasattr(self, "_attack_delivery_var"):
                # Old configurations had only a jump checkbox.  Preserve it
                # as 跳打; new configurations persist the full selection.
                delivery = str(data.get("attack_delivery", "")).casefold()
                if delivery not in ("stand", "jump", "charge"):
                    delivery = (
                        "jump" if bool(data.get(
                            "jump_attack_enabled",
                            data.get("stationary_jump_enabled", False),
                        )) else "stand"
                    )
                self._attack_delivery_var.set(delivery)
            if ("jump_counterattack_enabled" in data
                    and hasattr(self, "_jump_counterattack_enabled_var")):
                self._jump_counterattack_enabled_var.set(bool(
                    data["jump_counterattack_enabled"]
                ))
            charge_control = getattr(self, "_charge_hold_control", None)
            if charge_control is not None:
                try:
                    legacy_charge = float(data.get("charge_hold_seconds", 0.5))
                    charge_min = float(data.get("charge_min_seconds", legacy_charge))
                    charge_max = float(data.get("charge_max_seconds", charge_min))
                except (TypeError, ValueError):
                    charge_min, charge_max = 0.5, 0.5
                charge_control.set_bounds(charge_min, charge_max)
            if ("small_step_enabled" in data
                    and hasattr(self, "_small_step_enabled_var")):
                self._small_step_enabled_var.set(bool(
                    data["small_step_enabled"]
                ))
            small_step_control = getattr(self, "_small_step_control", None)
            if small_step_control is not None:
                legacy_small_min = float(data.get("small_step_interval_seconds", 5.0))
                small_min = float(data.get("small_step_min_seconds", legacy_small_min))
                small_max = float(data.get(
                    "small_step_max_seconds",
                    small_min + float(data.get("small_step_gap_seconds", 0.0)),
                ))
                small_step_control.set_bounds(small_min, small_max)
            small_step_post_attack_control = getattr(
                self, "_small_step_post_attack_control", None,
            )
            if small_step_post_attack_control is not None:
                try:
                    small_step_post_attack_control.set_bounds(float(data.get(
                        "small_step_post_attack_seconds", 0.70,
                    )))
                except (TypeError, ValueError):
                    small_step_post_attack_control.set_bounds(0.70)
            if ("stationary_pickup_enabled" in data
                    and hasattr(self, "_stationary_pickup_enabled_var")):
                self._stationary_pickup_enabled_var.set(bool(
                    data["stationary_pickup_enabled"]
                ))
            if ("stationary_delayed_return_enabled" in data
                    and hasattr(self, "_stationary_delayed_return_enabled_var")):
                self._stationary_delayed_return_enabled_var.set(bool(
                    data["stationary_delayed_return_enabled"]
                ))
            pickup_control = getattr(self, "_stationary_pickup_control", None)
            if pickup_control is not None:
                legacy_pickup_min = float(
                    data.get("stationary_pickup_interval_seconds", 900.0)
                ) / 60.0
                pickup_min = float(data.get(
                    "stationary_pickup_min_minutes", legacy_pickup_min,
                ))
                legacy_pickup_gap = float(data.get(
                    "stationary_pickup_gap_minutes",
                    float(data.get("stationary_pickup_gap_seconds", 0.0)) / 60.0,
                ))
                pickup_max = float(data.get(
                    "stationary_pickup_max_minutes", pickup_min + legacy_pickup_gap,
                ))
                pickup_control.set_bounds(pickup_min, pickup_max)
            if hasattr(self, "_stationary_facing_direction_var"):
                direction = str(data.get("stationary_facing_direction", ""))
                if direction not in ("left", "right", "both"):
                    # v1.0.75 had an opt-in right-facing checkbox.  The
                    # selector replaces it and defaults to right.
                    direction = "right"
                self._stationary_facing_direction_var.set(direction)
            dual_switch_control = getattr(self, "_dual_stationary_switch_control", None)
            if dual_switch_control is not None:
                legacy_switch_min = float(
                    data.get("dual_stationary_switch_seconds", 900.0)
                ) / 60.0
                dual_switch_control.set_bounds(
                    float(data.get("dual_stationary_switch_min_minutes", legacy_switch_min)),
                    float(data.get("dual_stationary_switch_max_minutes", legacy_switch_min)),
                )
            raw_posts = data.get("dual_stationary_posts", ())
            self._dual_stationary_posts = (
                [item for item in raw_posts if isinstance(item, dict)][:2]
                if isinstance(raw_posts, list) else []
            )
        except (KeyError, TypeError, ValueError):
            LOG.warning("ignored malformed fixed attack settings",
                        exc_info=True)
            return
        # Settings load can start directly in 巡逻攻击.  Apply the same
        # stationary-only lock used by a later mode click, otherwise those
        # controls render enabled until the operator switches modes once.
        self._fixed_refresh_rows()
        self._fixed_on_change()
        LOG.info("fixed attack settings loaded from %s",
                 self._fixed_settings_path())

    def _drug_settings_path(self) -> Path:
        path = Path(self.user_config_path) if self.user_config_path else None
        return config_section_file("drug", path)

    def _attach_bind_hint(self, button: Any) -> None:
        """Attach the bindable-hotkeys popout hint to a key-bind button.

        Mirrors the rope-record hint: hovering the button shows which keys
        can actually be bound (``BINDABLE_KEYS``), so the user does not have
        to guess.  The hint is always enabled - the binding buttons are
        always bindable.
        """

        tooltip = HoverTooltip(button, bindable_keys_hint())
        tooltip.set_enabled(True)
        self._bind_key_tooltips.append(tooltip)

    def _bind_capture_begin(
        self, button: Any, var: tk.StringVar, previous_attr: str,
        on_change: Optional[Callable[[], None]] = None,
        *, allow_null: bool = False,
    ) -> None:
        """Unlock a key button: one click arms it for recording.

        The button shows "press a key..."; the NEXT key press records it,
        Escape/unsupported keys restore the previous binding, and the button
        returns to LOCKED mode (grey, shows the key).  A second click while
        armed is ignored.
        """

        if getattr(self, "_key_capturing", False):
            return
        self._key_capturing = True
        self._key_capture_target = (
            button, var, previous_attr, on_change, bool(allow_null),
        )
        setattr(self, previous_attr, var.get())
        button.configure(text="请按一个按键…", style="TButton")
        root = getattr(self, "_root", None)
        if root is not None:
            root.bind("<KeyPress>", self._key_capture_handler)

    def _key_capture_handler(self, event: Any) -> str:
        """Record the pressed key and return the button to locked mode."""

        target = getattr(self, "_key_capture_target", None)
        if target is None:
            return ""
        button, var, previous_attr, on_change, allow_null = target
        keysym = str(getattr(event, "keysym", ""))
        key = keysym_to_scan_key(keysym)
        if allow_null and keysym.casefold() in ("minus", "subtract"):
            key = "-"
        if key is not None:
            var.set(key)
            if on_change is not None:
                on_change()
        else:
            var.set(getattr(self, previous_attr, var.get()))
        button.configure(text=var.get(), style="Locked.TButton")
        self._key_capturing = False
        self._key_capture_target = None
        root = getattr(self, "_root", None)
        if root is not None:
            root.unbind("<KeyPress>")
        return "break"

    def _drug_on_change(self, _event: Any = None) -> None:
        """Update labels, persist, and apply the drug settings live."""

        if not hasattr(self, "_hp_threshold_label"):
            return
        self._undo_capture_panel_change("药品与增益设置")
        for control in (
            getattr(self, "_hp_threshold_control", None),
            getattr(self, "_mp_threshold_control", None),
            getattr(self, "_buff1_interval_control", None),
            getattr(self, "_buff2_interval_control", None),
            getattr(self, "_buff3_interval_control", None),
            getattr(self, "_buff4_interval_control", None),
            getattr(self, "_buff5_interval_control", None),
            getattr(self, "_buff6_interval_control", None),
        ):
            if control is not None:
                control.normalize()
        hp_percent = int(self._hp_threshold_var.get())
        mp_percent = int(self._mp_threshold_var.get())
        self._hp_threshold_label.configure(text=f"{hp_percent}%")
        self._mp_threshold_label.configure(text=f"{mp_percent}%")
        if hasattr(self, "_hp_key_button"):
            self._hp_key_button.configure(text=self._hp_key_var.get())
        if hasattr(self, "_mp_key_button"):
            self._mp_key_button.configure(text=self._mp_key_var.get())
        if hasattr(self, "_buff1_interval_label"):
            self._buff1_interval_label.configure(
                text=f"{int(round(self._buff1_interval_var.get()))}s"
            )
        if hasattr(self, "_buff2_interval_label"):
            self._buff2_interval_label.configure(
                text=f"{int(round(self._buff2_interval_var.get()))}s"
            )
        if hasattr(self, "_buff3_interval_label"):
            self._buff3_interval_label.configure(
                text=f"{int(round(self._buff3_interval_var.get()))}s"
            )
        if hasattr(self, "_buff4_interval_label"):
            self._buff4_interval_label.configure(
                text=f"{int(round(self._buff4_interval_var.get()))}s"
            )
        if hasattr(self, "_buff5_interval_label"):
            self._buff5_interval_label.configure(
                text=f"{int(round(self._buff5_interval_var.get()))}s"
            )
        if hasattr(self, "_buff6_interval_label"):
            self._buff6_interval_label.configure(
                text=f"{int(round(self._buff6_interval_var.get()))}s"
            )
        if hasattr(self, "_buff1_key_button"):
            self._buff1_key_button.configure(text=self._buff1_key_var.get())
        if hasattr(self, "_buff2_key_button"):
            self._buff2_key_button.configure(text=self._buff2_key_var.get())
        if hasattr(self, "_buff3_key_button"):
            self._buff3_key_button.configure(text=self._buff3_key_var.get())
        if hasattr(self, "_buff4_key_button"):
            self._buff4_key_button.configure(text=self._buff4_key_var.get())
        if hasattr(self, "_buff5_key_button"):
            self._buff5_key_button.configure(text=self._buff5_key_var.get())
        if hasattr(self, "_buff6_key_button"):
            self._buff6_key_button.configure(text=self._buff6_key_var.get())
        data = {
            "hp_key": self._hp_key_var.get().strip(),
            "mp_key": self._mp_key_var.get().strip(),
            "hp_threshold": hp_percent,
            "mp_threshold": mp_percent,
            "hp_enabled": bool(self._hp_use_var.get()),
            "mp_enabled": bool(self._mp_use_var.get()),
            "buff1_key": self._buff1_key_var.get().strip(),
            "buff2_key": self._buff2_key_var.get().strip(),
            "buff3_key": self._buff3_key_var.get().strip(),
            "buff4_key": self._buff4_key_var.get().strip(),
            "buff5_key": self._buff5_key_var.get().strip(),
            "buff6_key": self._buff6_key_var.get().strip(),
            "buff_interval_unit": "seconds",
            "buff1_interval": int(round(float(self._buff1_interval_var.get()))),
            "buff2_interval": int(round(float(self._buff2_interval_var.get()))),
            "buff3_interval": int(round(float(self._buff3_interval_var.get()))),
            "buff4_interval": int(round(float(self._buff4_interval_var.get()))),
            "buff5_interval": int(round(float(self._buff5_interval_var.get()))),
            "buff6_interval": int(round(float(self._buff6_interval_var.get()))),
            "buff1_enabled": bool(self._buff1_use_var.get()),
            "buff2_enabled": bool(self._buff2_use_var.get()),
            "buff3_enabled": bool(self._buff3_use_var.get()),
            "buff4_enabled": bool(self._buff4_use_var.get()),
            "buff5_enabled": bool(self._buff5_use_var.get()),
            "buff6_enabled": bool(self._buff6_use_var.get()),
        }
        self._drug_save_settings(data)
        self._drug_apply_to_worker(data)

    def _drug_load_settings(self) -> None:
        """Restore saved drug panel values and apply them live."""

        try:
            data = json.loads(
                self._drug_settings_path().read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            data = {}
        try:
            if "hp_key" in data:
                self._hp_key_var.set(str(data["hp_key"]))
            if "mp_key" in data:
                self._mp_key_var.set(str(data["mp_key"]))
            if "hp_threshold" in data:
                self._hp_threshold_var.set(int(data["hp_threshold"]))
            if "mp_threshold" in data:
                self._mp_threshold_var.set(int(data["mp_threshold"]))
            if "hp_enabled" in data:
                self._hp_use_var.set(bool(data["hp_enabled"]))
            if "mp_enabled" in data:
                self._mp_use_var.set(bool(data["mp_enabled"]))
            if "buff1_key" in data:
                self._buff1_key_var.set(str(data["buff1_key"]))
            if "buff2_key" in data:
                self._buff2_key_var.set(str(data["buff2_key"]))
            if "buff3_key" in data:
                self._buff3_key_var.set(str(data["buff3_key"]))
            if "buff4_key" in data:
                self._buff4_key_var.set(str(data["buff4_key"]))
            if "buff5_key" in data:
                self._buff5_key_var.set(str(data["buff5_key"]))
            if "buff6_key" in data:
                self._buff6_key_var.set(str(data["buff6_key"]))
            # Older configuration files stored minutes.  The explicit unit
            # tag makes the one-time conversion unambiguous, including 5s.
            legacy_minutes = data.get("buff_interval_unit") != "seconds"
            for name, variable in (
                ("buff1_interval", self._buff1_interval_var),
                ("buff2_interval", self._buff2_interval_var),
                ("buff3_interval", self._buff3_interval_var),
                ("buff4_interval", self._buff4_interval_var),
                ("buff5_interval", self._buff5_interval_var),
                ("buff6_interval", self._buff6_interval_var),
            ):
                if name not in data:
                    continue
                interval = float(data[name])
                if legacy_minutes:
                    interval *= 60.0
                variable.set(int(round(min(600.0, max(5.0, interval)))))
            if "buff1_enabled" in data:
                self._buff1_use_var.set(bool(data["buff1_enabled"]))
            if "buff2_enabled" in data:
                self._buff2_use_var.set(bool(data["buff2_enabled"]))
            if "buff3_enabled" in data:
                self._buff3_use_var.set(bool(data["buff3_enabled"]))
            if "buff4_enabled" in data:
                self._buff4_use_var.set(bool(data["buff4_enabled"]))
            if "buff5_enabled" in data:
                self._buff5_use_var.set(bool(data["buff5_enabled"]))
            if "buff6_enabled" in data:
                self._buff6_use_var.set(bool(data["buff6_enabled"]))
        except (KeyError, TypeError, ValueError):
            LOG.warning("ignored malformed drug settings", exc_info=True)
            return
        self._drug_on_change()
        LOG.info("drug settings loaded from %s", self._drug_settings_path())

    def _drug_save_settings(self, data: dict) -> None:
        """Persist the drug panel values to the local JSON file."""

        try:
            self._drug_settings_path().write_text(
                json.dumps(data, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            LOG.warning("could not save drug settings", exc_info=True)

    def _drug_apply_to_worker(self, data: dict) -> None:
        """Apply the drug settings to the StatusWorker's detector config.

        Rows carry their own keys/sliders/checkboxes, so no echo status line
        is kept below the panel (the 药品/增益 summary line was removed).
        """

        worker = getattr(self, "status_worker", None)
        if worker is None:
            return
        try:
            config = apply_drug_settings(worker.detector.config, data)
            worker.detector.config = config
        except Exception as exc:
            LOG.warning("drug settings apply failed: %s", exc)

    def _shutdown_settings_path(self) -> Path:
        """JSON file holding the Additional Functions panel settings."""

        path = Path(self.user_config_path) if self.user_config_path else None
        return config_section_file("additional_functions", path)

    def _shutdown_collect_data(self) -> dict:
        """Current Additional Functions panel values as a settings dict."""

        data = {
            "shutdown_enabled": bool(self._shutdown_enabled_var.get()),
            "shutdown_hours": round(float(self._shutdown_hours_var.get()), 1),
        }
        if hasattr(self, "_player_check_var"):
            data["player_check_enabled"] = bool(self._player_check_var.get())
        if hasattr(self, "_disconnect_alert_var"):
            data["disconnect_alert_enabled"] = bool(
                self._disconnect_alert_var.get()
            )
        if hasattr(self, "_lie_alert_var"):
            data["lie_alert_enabled"] = bool(self._lie_alert_var.get())
        if hasattr(self, "_sound_alert_var"):
            data["sound_alert_enabled"] = bool(self._sound_alert_var.get())
        if hasattr(self, "_screen_blink_var"):
            data["screen_blink_enabled"] = bool(self._screen_blink_var.get())
        if hasattr(self, "_telegram_enabled_var"):
            data["telegram_enabled"] = bool(self._telegram_enabled_var.get())
            data["telegram_bot_token"] = self._telegram_bot_token
            data["telegram_chat_id"] = self._telegram_chat_id
            data["telegram_machine_name"] = self._telegram_machine_var.get().strip()
        if hasattr(self, "_quick_messages"):
            data["quick_messages"] = list(self._quick_messages)
        # 定时下机: the one-shot 清理内存 log-off, with the wait in seconds.
        data["timed_logout_enabled"] = bool(
            self._timed_logout_var.get()
            if hasattr(self, "_timed_logout_var") else False
        )
        data["timed_logout_seconds"] = (
            self._timed_logout_seconds()
            if hasattr(self, "_timed_logout_hour_control") else 0
        )
        # 钉钉提醒: webhook + the 消息 switch are part of 附加功能 configuration.
        data["dingtalk_webhook"] = str(
            getattr(self, "_dingtalk_webhook", "") or ""
        )
        data["dingtalk_enabled"] = bool(
            self._telegram_enabled_var.get()
            if hasattr(self, "_telegram_enabled_var") else False
        )
        if hasattr(self, "_countdown_enabled_var"):
            data["countdown_enabled"] = bool(
                self._countdown_enabled_var.get()
            )
            # New direct UI: whole minutes, 1..300.  Keep the legacy hours
            # key too so an older helper can still read this configuration.
            minutes = int(self._countdown_interval_var.get())
            data["countdown_interval_minutes"] = minutes
            data["countdown_interval_hours"] = round(minutes / 60.0, 4)
        if hasattr(self, "_api_auto_lie_var"):
            # 自动过测谎 is part of user_config, like the reconnect selection.
            data["auto_lie_api_enabled"] = bool(self._api_auto_lie_var.get())
            # Which backend that selection means: 云端测谎 / 本地测谎.  Written
            # even while disarmed, so re-arming restores the operator's choice.
            backend_var = getattr(self, "_api_auto_lie_backend_var", None)
            if backend_var is not None:
                value = str(backend_var.get()).casefold()
                data["auto_lie_backend"] = (
                    AUTOLIE_BACKEND_LOCAL if value == AUTOLIE_BACKEND_LOCAL
                    else AUTOLIE_BACKEND_CLOUD
                )
        if hasattr(self, "_reconnect_var"):
            data["auto_reconnect_enabled"] = bool(self._reconnect_var.get())
            data["auto_reconnect_world"] = self._reconnect_world_var.get()
            data["auto_reconnect_channel"] = int(
                valid_channel(self._reconnect_channel_var.get()) or CHANNEL_DEFAULT
            )
        if hasattr(self, "_auto_restart_var"):
            data["auto_restart_enabled"] = bool(self._auto_restart_var.get())
        if hasattr(self, "_restart_offline_message_var"):
            data["restart_offline_message"] = str(
                self._restart_offline_message_var.get()
            ).strip()[:500]
        if hasattr(self, "_reconnect_message_var"):
            data["reconnect_message"] = str(self._reconnect_message_var.get()).strip()[:500]
        if hasattr(self, "_player_request_message_var"):
            data["player_request_message"] = str(
                self._player_request_message_var.get()
            ).strip()[:500]
        if hasattr(self, "_player_room_code_var"):
            data["player_room_code"] = str(
                self._player_room_code_var.get()
            ).strip()[:128]
        if hasattr(self, "_player_channel_wait_var"):
            data["player_channel_wait_seconds"] = (
                self._normalized_player_channel_wait_seconds()
            )
        # 测试api needs no settings any more: the run length is fixed and the key ships with the app.
        return data

    def _on_lie_alert_change(self) -> None:
        """Turning off lie detection also disarms its dependent automation."""

        lie_armed = self._lie_detection_armed()
        if (not lie_armed and hasattr(self, "_api_auto_lie_var")
                and bool(self._api_auto_lie_var.get())):
            self._api_auto_lie_var.set(False)
            self._api_auto_lie_session_armed = False
            self._api_auto_lie_pending = False
            self.request_cancel_auto_lie_pass()
            AUTO_LIE_LOG.info("测谎 disabled; dependent 自动过测谎 was disabled as well")
        self._shutdown_on_change()

    def _shutdown_on_change(self, _value: str = "") -> None:
        """Update labels, persist, and apply the shutdown settings live."""

        if not hasattr(self, "_shutdown_hours_label"):
            return
        self._undo_capture_panel_change("附加功能设置")
        # The 掉线 checkbox cannot be turned off underneath an active 自动重连
        # selection.  Restore it here as well as in _reconnect_on_change so
        # a direct click on 掉线 cannot leave an apparently armed reconnect
        # with no event source.
        if (hasattr(self, "_reconnect_var")
                and bool(self._reconnect_var.get())
                and hasattr(self, "_disconnect_alert_var")
                and not bool(self._disconnect_alert_var.get())):
            self._disconnect_alert_var.set(True)
            LOG.info("自动重连 is selected; kept 掉线 enabled as its required trigger")
        hours = float(self._shutdown_hours_var.get())
        self._shutdown_hours_label.configure(text=f"{hours:.1f}h")
        data = self._shutdown_collect_data()
        self._refresh_player_wait_hint()
        self._shutdown_save_settings(data)
        self._shutdown_apply_to_worker(data)
        self._shutdown_refresh_grey()

    def _load_dingtalk_webhook(self) -> str:
        """The saved 钉钉 webhook, normalised ("" when unset or unusable)."""

        try:
            data = json.loads(
                self._shutdown_settings_path().read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return ""
        if not isinstance(data, dict):
            return ""
        return normalize_dingtalk_webhook(data.get("dingtalk_webhook", ""))

    def _apply_dingtalk_settings(self) -> None:
        """Hand the webhook / switch / 设备名称 to the 钉钉 worker and repaint.

        The status line is the 图层校准与巡逻 panel's hint, so this is also what
        keeps that hint truthful after every change (saved, cleared, armed).
        """

        notifier = getattr(self, "dingtalk_notifier", None)
        webhook = str(getattr(self, "_dingtalk_webhook", "") or "")
        enabled = False
        variable = getattr(self, "_telegram_enabled_var", None)
        if variable is not None:
            try:
                enabled = bool(variable.get())
            except Exception:  # noqa: BLE001 - Tk variable, display only
                enabled = False
        if notifier is not None:
            configure = getattr(notifier, "configure", None)
            if callable(configure):
                try:
                    configure(webhook, self._dingtalk_machine_name(), enabled)
                except Exception:  # noqa: BLE001 - never break the panel
                    LOG.warning("钉钉提醒: settings could not be applied",
                                exc_info=True)
        self._refresh_dingtalk_button()
        self._refresh_dingtalk_name_button()
        self._refresh_dingtalk_status()

    # The 钉钉 status shares the panel hint line ("在线授权验证成功…") instead of
    # owning a row of its own: the note is appended to whatever that line
    # currently says and re-appended on every poll tick.
    DINGTALK_NOTE_SEP = " · "
    # The operator's short form for a 本地测谎 without a CT card.  Used ONLY when the
    # service note does not already say it, so the panel line never carries the same
    # fact twice (the 1.3.9 build printed the prefix sentence next to a longer copy).
    LOCAL_LIE_GREYED_NOTE = "本地测谎未开通"
    # How a 本地测谎 radio without a CT card is presented: greyed AND not pickable.
    # Kept as one named value because the operator has been explicit about it twice -
    # "grey it rather than switch me to 关闭" and then "still selectable, fix it".
    LOCAL_LIE_GREYED_STATE = "disabled"

    def _dingtalk_note_text(self) -> str:
        """The ONE short 钉钉 state appended to the panel hint.

        How to configure it lives in the panel tooltips, not here: repeating
        "掉线、测谎、循环…" in this line as well made the same sentence appear
        twice on screen.
        """

        transient = str(getattr(self, "_dingtalk_note", "") or "")
        if transient:
            # A test result is shown exactly once; the next tick reports the
            # notifier's own state again.
            self._dingtalk_note = ""
            return transient
        notifier = getattr(self, "dingtalk_notifier", None)
        status_text = getattr(notifier, "status_text", None)
        if callable(status_text):
            try:
                state = str(status_text())
            except Exception:  # noqa: BLE001 - hint only
                state = ""
            if state:
                return f"钉钉消息: {state}"
        if not getattr(self, "_dingtalk_webhook", ""):
            return "钉钉消息: 未设置"
        enabled = False
        variable = getattr(self, "_telegram_enabled_var", None)
        try:
            enabled = bool(variable.get()) if variable is not None else False
        except Exception:  # noqa: BLE001
            enabled = False
        return "钉钉消息: 已开启" if enabled else "钉钉消息: 已设置，未开启"

    def _refresh_panel_hints(self) -> None:
        """Write every 附加功能 status onto the ONE shared hint line.

        Order is fixed (钉钉 first, then 定时下机) so the line never reorders
        itself, and any previous notes are stripped before the new ones are
        appended - no stacked, half-stale sentences.
        """

        label = getattr(self, "_control_status", None)
        if label is None:
            return
        try:
            current = str(label.cget("text") or "")
        except Exception:  # noqa: BLE001 - hint only
            return
        base = current.split(self.DINGTALK_NOTE_SEP)[0].rstrip(" ·;；")
        notes = []
        dingtalk_note = self._dingtalk_note_text()
        if dingtalk_note:
            notes.append(dingtalk_note)
        timed_note = self._timed_logout_remaining_text()
        if timed_note:
            notes.append(timed_note)
        # 本地测谎 service state (started with the app, stopped with it): the
        # operator asked for this hint on the 图层校准与巡逻 panel.
        service_note = self._local_lie_service_note()
        if service_note:
            notes.append(service_note)
        # One sentence only: the service note already says 本地测谎未开通 while
        # 本地测谎 is the supervisor's target, so this only covers the case where the
        # radio is greyed but that note is empty.  It is derived from the ACCESS
        # state (not from the note) because the note is empty as soon as the backend
        # is disarmed - the reason the sentence never painted in 1.3.9.
        if (self._local_lie_access() is False
                and "本地测谎未开通" not in (service_note or "")):
            notes.append(self.LOCAL_LIE_GREYED_NOTE)
        self.last_dingtalk_status = dingtalk_note
        self.last_timed_logout_status = timed_note
        self._local_lie_service_hint_shown = service_note
        text = self.DINGTALK_NOTE_SEP.join([base, *notes]) if base else \
            self.DINGTALK_NOTE_SEP.join(notes)
        try:
            label.configure(text=text)
        except Exception:  # noqa: BLE001 - a hint is cosmetic
            LOG.debug("面板提示: could not be painted", exc_info=True)

    def _local_lie_service_note(self) -> str:
        """The 本地测谎 service sentence for the panel hint, or ""."""

        try:
            import local_lie_service

            return str(local_lie_service.hint_text() or "")
        except Exception:  # noqa: BLE001 - a hint is cosmetic
            LOG.debug("本地测谎 hint unavailable", exc_info=True)
            return ""

    def _poll_local_lie_service_hint(self) -> None:
        """Repaint the 图层校准与巡逻 hint when the service state changes.

        Also re-probes while 本地测谎 is selected and the service is not reachable:
        the app starts it in the background at launch, so the very first probe can
        legitimately fail ("connection refused") and the panel must recover by
        itself once the service is listening.
        """

        # The 本地测谎 radio follows the code's CT card: without one the backend is
        # out of reach, so the selection is greyed (and disarmed) on every tick.
        self._sync_local_lie_radio()
        if getattr(self, "_auto_lie_backend_is_local", None) is not None:
            if (self._auto_lie_backend_is_local()
                    and self._local_lie_ready is False
                    and not getattr(self, "_local_lie_report_pending", False)
                    and time.monotonic() - getattr(self, "_local_lie_probe_at", 0.0)
                    >= LOCAL_LIE_PROBE_RETRY_SECONDS):
                self._probe_local_lie_service()
        note = self._local_lie_service_note()
        # The greyed sentence is derived from the access state, not from `note`, so
        # the repaint gate has to watch that state as well - otherwise a greying
        # radio whose service note stayed "" never repainted the hint at all.
        access = self._local_lie_access()
        if (note == getattr(self, "_local_lie_service_hint_shown", None)
                and access == getattr(self, "_local_lie_access_hint_shown", None)):
            return
        self._local_lie_access_hint_shown = access
        self._refresh_panel_hints()

    def _report_local_lie_drill(self, outcome: str, detail: str = "") -> None:
        """Finish the 本地测谎 statistics row of a 测试API drill.

        测试API is a local pass too: without this the 本地测谎 panel never moved
        (the field report "全局测谎统计 moved but 本地测谎 did not").  The start
        row was opened in ``_api_test_clicked``; this closes the same event.
        """

        event_id = str(getattr(self, "_local_lie_drill_event", "") or "")
        if not event_id:
            return
        self._local_lie_drill_event = ""
        try:
            import local_lie_service

            local_lie_service.report_local_pass(
                event_id, outcome, detail=f"测试本地测谎 {detail}".strip(),
            )
        except Exception:  # noqa: BLE001 - statistics never block a drill
            LOG.debug("本地测谎 测试API report failed", exc_info=True)

    def _refresh_dingtalk_status(self) -> None:
        """Compatibility alias: the 钉钉 state is part of the shared hint now."""

        self._refresh_panel_hints()

    def _refresh_dingtalk_button(self) -> None:
        """The button doubles as the state readout: 钉钉消息 / 钉钉已设置."""

        button = getattr(self, "_dingtalk_button", None)
        if button is None:
            return
        try:
            button.configure(
                text="钉钉已设置" if getattr(self, "_dingtalk_webhook", "")
                else "钉钉消息"
            )
        except Exception:  # noqa: BLE001 - a label is cosmetic
            LOG.debug("钉钉消息: button could not be repainted", exc_info=True)

    def _set_dingtalk_status(self, text: str, *, error: bool = False) -> None:
        """Show one transient note in the shared hint line.

        The note survives until the next poll, which replaces it with the
        notifier's own state, so a test result is visible without becoming a
        permanent fixture.
        """

        del error  # the shared hint line has one colour; the wording carries it
        self._dingtalk_note = f"钉钉消息: {text}"
        self._refresh_dingtalk_status()

    def _dingtalk_change_webhook(self) -> None:
        """Ask for the group-robot webhook and send one test message.

        An empty answer clears the setting (the button returns to 钉钉提醒).
        A stored webhook is verified immediately, because "saved" without a
        delivered message tells the operator nothing.
        """

        from tkinter import simpledialog

        try:
            value = simpledialog.askstring(
                "钉钉提醒",
                "请输入 webhook 链接：",
                initialvalue=getattr(self, "_dingtalk_webhook", ""),
                parent=getattr(self, "_root", None),
            )
        except Exception:  # noqa: BLE001 - no window to attach to
            LOG.exception("钉钉提醒: the webhook dialog could not open")
            return
        if value is None:
            # Cancelled: keep whatever is stored.
            return
        webhook = normalize_dingtalk_webhook(value)
        if str(value).strip() and not webhook:
            self._set_dingtalk_status(
                "链接无效：需要机器人 send?access_token=… 或 "
                "connector.dingtalk.com/webhook/… 形式的完整地址",
                error=True,
            )
            return
        self._dingtalk_webhook = webhook
        # Persist immediately: the webhook belongs to 附加功能 settings, so it
        # travels with user_config.json and survives an update.
        self._shutdown_on_change()
        self._apply_dingtalk_settings()
        if not webhook:
            self._set_dingtalk_status("已清除钉钉提醒")
            LOG.info("钉钉提醒: webhook cleared")
            return
        self._set_dingtalk_status("正在发送测试消息…")
        LOG.info("钉钉提醒: webhook saved, sending one test message")
        send_dingtalk_in_background(
            webhook,
            format_dingtalk_message(self._dingtalk_machine_name(), "测试提醒"),
            self._dingtalk_test_finished,
        )

    # 定时下机 fires the 清理内存 lifecycle once, after a HH:mm:ss wait.
    # 12:00:00 is the operator's maximum; anything longer is clamped.
    TIMED_LOGOUT_MAX_HOURS = 12
    TIMED_LOGOUT_MAX_SECONDS = TIMED_LOGOUT_MAX_HOURS * 3600

    def _timed_logout_components(self) -> tuple[int, int, int]:
        """The three clock fields as (hours, minutes, seconds)."""

        values = []
        for name in ("_timed_logout_hour_control",
                     "_timed_logout_minute_control",
                     "_timed_logout_second_control"):
            control = getattr(self, name, None)
            try:
                values.append(int(control.minimum_var.get()) if control else 0)
            except Exception:  # noqa: BLE001 - Tk variable, display only
                values.append(0)
        return values[0], values[1], values[2]

    def _timed_logout_seconds(self) -> int:
        """Configured wait in seconds, clamped to 12:00:00."""

        hours, minutes, seconds = self._timed_logout_components()
        total = max(0, hours) * 3600 + max(0, min(59, minutes)) * 60 + max(0, min(59, seconds))
        return int(min(total, self.TIMED_LOGOUT_MAX_SECONDS))

    def _set_timed_logout_components(self, seconds: int) -> None:
        """Render ``seconds`` into the HH:mm:ss fields (clamped to the maximum)."""

        total = max(0, min(int(seconds), self.TIMED_LOGOUT_MAX_SECONDS))
        hours, remainder = divmod(total, 3600)
        minutes, seconds_part = divmod(remainder, 60)
        for name, value in (
            ("_timed_logout_hour_control", hours),
            ("_timed_logout_minute_control", minutes),
            ("_timed_logout_second_control", seconds_part),
        ):
            control = getattr(self, name, None)
            if control is not None:
                try:
                    control.set_bounds(value)
                except Exception:  # noqa: BLE001 - widget state only
                    LOG.debug("定时下机: clock field could not be set", exc_info=True)

    def _timed_logout_remaining_text(self) -> str:
        """The 定时下机 note for the shared hint line.

        The countdown itself has NO sentence: the three clock fields are the
        countdown (they tick down like 循环's 剩余 HH:mm:ss), so the hint only
        carries an event ("时间到，正在下线…") or the reason nothing runs.
        """

        transient = str(getattr(self, "_timed_logout_note", "") or "")
        if transient:
            # One-shot messages (fired, failed, unsupported) are shown once.
            self._timed_logout_note = ""
            return f"定时下机: {transient}"
        if getattr(self, "_timed_logout_deadline", None) is not None:
            return ""
        checked = False
        variable = getattr(self, "_timed_logout_var", None)
        try:
            checked = bool(variable.get()) if variable is not None else False
        except Exception:  # noqa: BLE001 - Tk variable, display only
            checked = False
        if checked and self._timed_logout_seconds() <= 0:
            return "定时下机: 未启动，请先设置时间"
        # Unchecked (or fired): say nothing - the hint line stays clean.
        return ""

    def _timed_logout_on_change(self, _value: Any = "") -> None:
        """Arm/disarm the one-shot 定时下机 timer and persist it.

        Arming restarts the wait from the configured HH:mm:ss; unchecking (or
        setting 00:00:00) cancels it.  No worker thread is involved: the shared
        UI poll compares one deadline, so this costs nothing while it waits.
        The countdown itself is the feedback, so no extra sentence is written.
        """

        enabled = False
        variable = getattr(self, "_timed_logout_var", None)
        try:
            enabled = bool(variable.get()) if variable is not None else False
        except Exception:  # noqa: BLE001 - Tk variable, display only
            enabled = False
        seconds = self._timed_logout_seconds()
        if enabled and seconds <= 0:
            self._timed_logout_deadline = None
            LOG.info("定时下机: 已勾选但时间是 00:00:00，未启动")
        elif enabled:
            self._timed_logout_deadline = time.monotonic() + seconds
            hours, minutes, rest = self._timed_logout_components()
            LOG.info(
                "定时下机: 已启动，%02d:%02d:%02d 后下线",
                hours, minutes, rest,
            )
        else:
            self._timed_logout_deadline = None
            LOG.info("定时下机: 已取消")
        self._refresh_panel_hints()
        data = self._shutdown_collect_data()
        self._shutdown_save_settings(data)

    def _set_timed_logout_note(self, text: str) -> None:
        """Queue one short one-shot sentence for the shared hint line."""

        self._timed_logout_note = str(text)
        self._refresh_panel_hints()

    def _tick_timed_logout(self) -> None:
        """Run the readout, then fire 定时下机 once its deadline passes.

        The readout IS the countdown: while armed the three clock fields are
        rewritten with the remaining HH:mm:ss, exactly the way 循环's 剩余
        fields behave, so no separate sentence is needed on screen.
        """

        deadline = getattr(self, "_timed_logout_deadline", None)
        if deadline is not None:
            remaining = max(0, int(round(deadline - time.monotonic())))
            self._set_timed_logout_components(remaining)
        self._refresh_panel_hints()
        if deadline is None or time.monotonic() < deadline:
            return
        self._timed_logout_deadline = None
        variable = getattr(self, "_timed_logout_var", None)
        if variable is not None:
            try:
                variable.set(False)
            except Exception:  # noqa: BLE001 - Tk variable, display only
                LOG.debug("定时下机: could not uncheck the selection", exc_info=True)
        worker = getattr(self, "auto_restart_worker", None)
        LOG.warning("定时下机: 时间到，执行下线")
        # 定时下机 owns this log-off: 自动重连 watches the same 掉线 signal and
        # would reconnect in the middle of it, so its selection is ticked off
        # first (and stays off in the saved configuration).
        self._suppress_auto_reconnect_for_timed_logout()
        data = self._shutdown_collect_data()
        self._shutdown_save_settings(data)
        trigger = getattr(worker, "trigger_logoff", None)
        if not callable(trigger):
            LOG.warning("定时下机: 清理内存工作线程不支持只下线")
            self._set_timed_logout_note("清理内存暂不可用")
            return
        try:
            accepted = bool(trigger())
        except Exception:  # noqa: BLE001 - never kill the poll tick
            LOG.exception("定时下机: 触发下线失败")
            self._set_timed_logout_note("下线失败，请稍后重试")
            return
        if not accepted:
            LOG.info("定时下机: 下线流程已经在运行")
            self._set_timed_logout_note("下线流程已在运行")
            return
        self._set_timed_logout_note("时间到，正在下线…")

    def _suppress_auto_reconnect_for_timed_logout(self) -> bool:
        """Tick 自动重连 off before the 定时下机 log-off runs.

        Returns True when it had been selected.  The change goes through the
        normal handler, so the reconnect worker is disarmed and the setting is
        saved - repainting the checkbox alone would leave a live watcher that
        races the log-off.  No sentence is written: the fired note already says
        what is happening.
        """

        variable = getattr(self, "_reconnect_var", None)
        if variable is None:
            return False
        try:
            if not bool(variable.get()):
                return False
            variable.set(False)
        except Exception:  # noqa: BLE001 - never block the log-off
            LOG.exception("定时下机: 无法取消 自动重连 选择")
            return False
        LOG.warning("定时下机: 已取消 自动重连，避免与下线流程冲突")
        try:
            self._reconnect_on_change()
        except Exception:  # noqa: BLE001 - the log-off must still run
            LOG.exception("定时下机: 应用 自动重连 关闭状态失败")
        return True

    def _apply_timed_logout(self, data: dict) -> None:
        """Restore the saved 定时下机 selection and clock fields."""

        controls = [getattr(self, name, None) for name in (
            "_timed_logout_hour_control", "_timed_logout_minute_control",
            "_timed_logout_second_control",
        )]
        if any(control is None for control in controls):
            return
        try:
            seconds = int(data.get("timed_logout_seconds", 0) or 0)
        except (TypeError, ValueError):
            seconds = 0
        self._set_timed_logout_components(seconds)
        enabled = bool(data.get("timed_logout_enabled", False))
        variable = getattr(self, "_timed_logout_var", None)
        if variable is not None:
            try:
                variable.set(enabled)
            except Exception:  # noqa: BLE001 - Tk variable, display only
                LOG.debug("定时下机: could not restore the selection", exc_info=True)
        if enabled and self._timed_logout_seconds() > 0:
            # A running countdown keeps ITS deadline: every other panel change
            # re-applies this configuration, and re-arming here would push the
            # deadline forward forever (the operator's "倒计时没有开始").
            if getattr(self, "_timed_logout_deadline", None) is None:
                self._timed_logout_deadline = (
                    time.monotonic() + self._timed_logout_seconds()
                )
                hours, minutes, rest = self._timed_logout_components()
                LOG.info("定时下机: 已恢复 %02d:%02d:%02d 的倒计时",
                         hours, minutes, rest)
        else:
            self._timed_logout_deadline = None
        self._refresh_panel_hints()

    def _dingtalk_machine_name(self) -> str:
        """The device name every push carries ("" when the operator cleared it)."""

        variable = getattr(self, "_telegram_machine_var", None)
        try:
            return str(variable.get()) if variable is not None else ""
        except Exception:  # noqa: BLE001 - display-only value
            return ""

    def _refresh_dingtalk_name_button(self) -> None:
        """设备名 while unset, 名称已设置 once a name is stored."""

        button = getattr(self, "_dingtalk_name_button", None)
        if button is None:
            return
        try:
            button.configure(
                text="名称已设置" if self._dingtalk_machine_name().strip()
                else "设备名"
            )
        except Exception:  # noqa: BLE001 - a label is cosmetic
            LOG.debug("钉钉消息: name button could not be repainted", exc_info=True)

    def _edit_dingtalk_name(self) -> None:
        """Ask for the device name shown in every 钉钉 message.

        It reuses the same stored 设备名称 the Telegram alert reads, so one name
        describes this machine on every channel; an empty answer clears it.
        """

        from tkinter import simpledialog

        try:
            name = simpledialog.askstring(
                "设备名",
                "请输入设备名称（会显示在每条提醒里；留空则清除）：",
                initialvalue=self._dingtalk_machine_name(),
                parent=getattr(self, "_root", None),
            )
        except Exception:  # noqa: BLE001 - no window to attach to
            LOG.exception("钉钉消息: the device-name dialog could not open")
            return
        if name is None:
            # Cancelled: keep the stored name.
            return
        variable = getattr(self, "_telegram_machine_var", None)
        if variable is not None:
            try:
                variable.set(str(name).strip()[:100])
            except Exception:  # noqa: BLE001 - Tk variable, display only
                LOG.debug("钉钉消息: device name could not be stored", exc_info=True)
        self._refresh_dingtalk_name_button()
        # Persist with the rest of the 附加功能 settings and push the new name
        # into the notifier so the very next message carries it.
        self._shutdown_on_change()
        self._apply_dingtalk_settings()
        LOG.info("钉钉消息: device name set to %r", self._dingtalk_machine_name())

    def _dingtalk_test_finished(self, status: Any, error: Any) -> None:
        """Worker thread → Tk: queue the outcome, never touch widgets here."""

        results = getattr(self, "_dingtalk_results", None)
        if results is None:
            return
        try:
            results.put_nowait((status, error))
        except queue.Full:
            pass

    def _drain_dingtalk_results(self) -> None:
        """Paint the outcome of a test send on Tk's thread."""
        results = getattr(self, "_dingtalk_results", None)
        if results is None:
            return
        while True:
            try:
                status, error = results.get_nowait()
            except queue.Empty:
                return
            if error:
                self._set_dingtalk_status(f"测试失败：{error}", error=True)
                LOG.warning("钉钉提醒: test message failed - %s", error)
                continue
            self._set_dingtalk_status("测试消息已发送，请在钉钉群查看")
            LOG.info("钉钉提醒: test message delivered (%s)", status or "ok")

    def _shutdown_save_settings(self, data: dict) -> None:
        """Persist the Additional Functions values to the local JSON file."""

        try:
            self._shutdown_settings_path().write_text(
                json.dumps(data, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            LOG.warning("could not save additional functions settings",
                        exc_info=True)

    def _shutdown_apply_to_worker(self, data: dict) -> None:
        """Apply the shutdown settings to the ShutdownWorker live."""

        if not self._license_allowed():
            # A stored configuration must not silently reactivate parked
            # capture, reconnect, or alert-driven automation before a license
            # is entered on a fresh installation.
            data = dict(data)
            data.update({
                "shutdown_enabled": False,
                "player_check_enabled": False,
                "disconnect_alert_enabled": False,
                "lie_alert_enabled": False,
                "auto_lie_api_enabled": False,
                "auto_reconnect_enabled": False,
                "auto_restart_enabled": False,
            })
        worker = getattr(self, "shutdown_worker", None)
        if worker is not None:
            worker.enabled = bool(data.get("shutdown_enabled", False))
            worker.set_hours(float(data.get("shutdown_hours", 3.0)))
        else:
            # Scheduled shutdown is temporarily disabled (no worker is
            # constructed), but the alarm/reminder wiring below is
            # independent and MUST still be applied live.  An early return
            # here silently killed 掉线警报, 测谎警报, and the 声音/闪烁/消息
            # toggles whenever shutdown_worker is None.
            self._shutdown_status.configure(
                text="定时关闭: 工作线程未接入 (无界面模式)。"
            )
        # Other-player auto channel switch -> movement worker (live).
        mover = getattr(self, "movement_worker", None)
        if mover is not None:
            setter = getattr(mover, "set_other_player_check", None)
            if setter is not None:
                setter(bool(data.get("player_check_enabled", False)))
            message_setter = getattr(mover, "set_other_player_request_message", None)
            if message_setter is not None:
                message_setter(data.get("player_request_message", ""))
            routing_setter = getattr(mover, "set_other_player_channel_routing", None)
            if routing_setter is not None:
                routing_setter(
                    room_code=data.get("player_room_code", ""),
                    wait_seconds=data.get("player_channel_wait_seconds", 300.0),
                    current_channel=data.get("auto_reconnect_channel", CHANNEL_DEFAULT),
                )
        character = getattr(self, "character_worker", None)
        if character is not None:
            setter = getattr(character, "set_disconnect_alert", None)
            if setter is not None:
                setter(bool(data.get("disconnect_alert_enabled", False)))
        lie_detector = getattr(self, "lie_detector_worker", None)
        lie_detection_armed = bool(data.get("lie_alert_enabled", False))
        if lie_detector is not None:
            setter = getattr(lie_detector, "set_enabled", None)
            if setter is not None:
                setter(lie_detection_armed)
        # Keep the displayed 自动重连 selection and the worker's actual armed
        # state in the same settings application transaction.  In particular,
        # this path is used after the initial online-license heartbeat
        # succeeds.  Previously it restored a checked Tk variable and armed
        # 掉线, but omitted ReconnectWorker.set_enabled(), leaving a visibly
        # selected 自动重连 that rejected the next confirmed offline event as
        # "disabled".
        reconnect = getattr(self, "reconnect_worker", None)
        reconnect_enabled = bool(data.get("auto_reconnect_enabled", False))
        if reconnect is not None:
            reconnect_world = str(
                data.get("auto_reconnect_world", WORLD_NAMES[0])
            )
            if reconnect_world not in WORLD_NAMES:
                reconnect_world = WORLD_NAMES[0]
            reconnect_channel = valid_channel(
                data.get("auto_reconnect_channel", CHANNEL_DEFAULT)
            ) or CHANNEL_DEFAULT
            reconnect.set_world(reconnect_world)
            reconnect.set_channel(reconnect_channel)
            reconnect.set_enabled(reconnect_enabled)
            LOG.info(
                "auto reconnect settings applied: selected=%s worker_enabled=%s "
                "world=%s channel=%s",
                reconnect_enabled,
                reconnect.enabled,
                reconnect_world,
                reconnect_channel,
            )
        restart = getattr(self, "auto_restart_worker", None)
        if restart is not None:
            try:
                restart.configure(
                    enabled=bool(data.get("auto_restart_enabled", False)),
                    offline_message=data.get("restart_offline_message", ""),
                )
            except Exception:
                LOG.warning("auto restart settings could not be applied", exc_info=True)
        # 测谎 / 掉线 are what feed 自动过测谎 and 自动重连: while they are armed and the shared capture is
        # parked (no Start Patrol), the assistant's parked watch grabs the game window on its own, so a
        # lie window is still detected and a 掉线 is still noticed - both workflows are independent of
        # the patrol.
        disconnect_armed = bool(data.get("disconnect_alert_enabled", False))
        for watch_event, armed in (
            (getattr(self, "lie_watch_armed_event", None), lie_detection_armed),
            (getattr(self, "disconnect_watch_armed_event", None), disconnect_armed),
        ):
            if watch_event is None:
                continue
            try:
                if armed:
                    watch_event.set()
                else:
                    watch_event.clear()
            except Exception:
                LOG.debug("the parked watch could not be armed", exc_info=True)
        sound_enabled = bool(data.get("sound_alert_enabled", True))
        for alert_worker in (
            getattr(self, "countdown_worker", None),
            character,
            lie_detector,
        ):
            setter = getattr(alert_worker, "set_sound_enabled", None)
            if setter is not None:
                setter(sound_enabled)
        blinker = getattr(self, "screen_blinker", None)
        if blinker is not None:
            setter = getattr(blinker, "set_enabled", None)
            if setter is not None:
                setter(bool(data.get("screen_blink_enabled", False)))
        notifier = getattr(self, "telegram_notifier", None)
        if notifier is not None:
            notifier.configure(
                str(data.get("telegram_bot_token", "")),
                str(data.get("telegram_chat_id", "")),
                str(data.get("telegram_machine_name", "")),
            )
            notifier.set_enabled(bool(data.get("telegram_enabled", False)))
        # 钉钉提醒: the webhook and the 消息 switch travel with the same section,
        # so a restored configuration arms the push without touching the panel.
        dingtalk = getattr(self, "dingtalk_notifier", None)
        if dingtalk is not None:
            webhook = normalize_dingtalk_webhook(data.get("dingtalk_webhook", ""))
            if webhook:
                self._dingtalk_webhook = webhook
            configure = getattr(dingtalk, "configure", None)
            if callable(configure):
                try:
                    configure(
                        webhook or getattr(self, "_dingtalk_webhook", ""),
                        str(data.get("telegram_machine_name", "")),
                        bool(data.get("telegram_enabled", False)),
                    )
                except Exception:  # noqa: BLE001 - optional channel
                    LOG.warning("钉钉提醒: restored settings could not be applied",
                                exc_info=True)
        if hasattr(self, "_dingtalk_button"):
            self._refresh_dingtalk_button()
            self._refresh_dingtalk_status()
        # 定时下机: restore the saved clock so a restart keeps the operator's
        # selection instead of silently disarming it.
        self._apply_timed_logout(data)
        if worker is not None and worker.enabled:
            self._shutdown_status.configure(
                text=f"定时关闭已启动: 游戏将在 "
                     f"{float(data.get('shutdown_hours', 3.0)):.1f}小时后关闭 "
                     f"(Alt+F4 后停止所有工作线程)。"
            )
        elif worker is not None:
            self._shutdown_status.configure(
                text="定时关闭: 未启用 - 游戏继续运行。"
            )

    def _shutdown_refresh_grey(self) -> None:
        """Grey the countdown slider when the shutdown feature is off."""

        enabled = bool(self._shutdown_enabled_var.get())
        state = "!disabled" if enabled else "disabled"
        for widget in (self._shutdown_slider, self._shutdown_hours_label):
            try:
                widget.state([state])
            except Exception:
                try:
                    widget.configure(
                        state="normal" if enabled else "disabled"
                    )
                except Exception:
                    pass

    def _edit_machine_name(self) -> None:
        """Set or clear the Telegram device name from an explicit click."""

        try:
            from tkinter import simpledialog

            name = simpledialog.askstring(
                "设备名称",
                "设置消息推送显示的设备名称；留空则清除设置。",
                initialvalue=self._telegram_machine_var.get(),
                parent=self._root,
            )
        except Exception as exc:
            self._configure_text_if_changed(
                self._telegram_status,
                "telegram_status",
                f"消息提醒: 无法打开设备名称输入框 - {exc}",
            )
            return
        if name is None:
            return
        self._telegram_machine_var.set(name.strip()[:100])
        self._configure_text_if_changed(
            self._telegram_machine_button,
            "telegram_machine_button",
            machine_name_button_text(self._telegram_machine_var.get()),
        )
        self._shutdown_on_change()

    def _render_quick_messages(self, edit_index: Optional[int] = None) -> None:
        frame = getattr(self, "_quick_messages_frame", None)
        if frame is None:
            return
        for tooltip in self._quick_message_tooltips:
            tooltip.destroy()
        self._quick_message_tooltips.clear()
        # Clear the active-entry identity before destroying widgets so a
        # destruction-induced FocusOut cannot recursively save/render.
        self._quick_edit_entry = None
        self._quick_edit_index = None
        for child in frame.winfo_children():
            child.destroy()
        for index, message in enumerate(self._quick_messages):
            row = self._ttk.Frame(frame)
            row.pack(fill="x", pady=2)
            # Hotkey.json maps list positions 0..9 to Ctrl+1..Ctrl+0.  Keep
            # that index visible beside every message, including while it is
            # being edited, so a deletion/reorder is immediately obvious.
            key_number = (index + 1) % 10
            if edit_index == index:
                self._ttk.Label(
                    row, text=f"Ctrl+{key_number}", width=6, anchor="w"
                ).pack(side="left", padx=(0, 4))
                # The editor expands into the row's real available width;
                # its requested width must not widen column 0 by itself.
                entry = self._ttk.Entry(row, width=20)
                entry.insert(0, message)
                entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
                self._quick_edit_entry = entry
                self._quick_edit_index = index
                entry.bind(
                    "<FocusOut>",
                    lambda event, i=index, field=entry: (
                        self._quick_message_finish_edit(i, field)
                    ),
                )
                entry.bind(
                    "<Return>",
                    lambda event, i=index, field=entry: (
                        self._quick_message_finish_edit(i, field)
                    ),
                )
                entry.focus_set()
                entry.selection_range(0, "end")
            else:
                # ttk.Label remains visible under every Windows theme and a
                # direct left click opens the inline editor.
                # This is deliberately wider than the old fixed preview.  A
                # row itself owns the available width; truncation only starts
                # near its right-side delete affordance rather than leaving a
                # conspicuous unused strip in the middle of the panel.
                # The compact row still has room after its Ctrl label and ×.
                # Use it before ellipsizing so a normal message does not look
                # artificially short beside a large blank stretch.
                # One additional CJK character (two display units) fills the
                # requested ~15px of remaining quick-message row width.
                preview = quick_message_preview(message, max_units=54)
                message_label = self._ttk.Label(
                    row, text=f"Ctrl+{key_number}   {preview}", anchor="w",
                    # The label fills its row, but its intrinsic request is
                    # kept tiny so a long preview cannot dictate panel width.
                    width=1, wraplength=0,
                )
                message_label.pack(
                    side="left", fill="x", expand=True, padx=(0, 4)
                )
                if preview != message:
                    tooltip = HoverTooltip(message_label, message)
                    tooltip.set_enabled(True)
                    self._quick_message_tooltips.append(tooltip)
                message_label.bind(
                    "<Button-1>",
                    lambda _event, i=index: self._quick_message_begin_edit(i),
                )
            delete_button = self._ttk.Label(row, text="×", width=3, anchor="center")
            delete_button.pack(side="left")
            delete_button.bind(
                "<Button-1>",
                lambda _event, i=index: self._quick_delete(i),
            )

    def _quick_messages_save(self) -> None:
        self._shutdown_save_settings(self._shutdown_collect_data())

    def _quick_message_add(self) -> None:
        if len(self._quick_messages) >= 20:
            self._quick_message_status.configure(text="快捷消息最多 20 条。")
            return
        self._undo_capture("快捷消息")
        self._quick_messages.append("新快捷消息")
        index = len(self._quick_messages) - 1
        self._quick_messages_save()
        self._render_quick_messages(index)
        self._refit_window_to_content()

    def _set_quick_message_status(self, text: str) -> None:
        """Show one compact, non-wrapping quick-message status line."""

        label = getattr(self, "_quick_message_status", None)
        if label is not None:
            # The status row has substantially more usable room than the
            # former 30-cell preview.  Keep it on one line and ellipsize only
            # when its actual message is genuinely long.
            label.configure(text=quick_message_preview(text, max_units=60))

    def _copy_quick_message(self, index: int) -> bool:
        if not (0 <= index < len(self._quick_messages)):
            return False
        message = self._quick_messages[index]
        try:
            self._root.clipboard_clear()
            self._root.clipboard_append(message)
            self._root.update_idletasks()
            self._set_quick_message_status(f"已复制：{message}")
            return True
        except Exception as exc:
            self._set_quick_message_status(f"复制失败：{exc}")
            return False

    def _send_quick_message(self, index: int) -> bool:
        """Send one live list item; deletion naturally shifts later keys."""

        if not self._copy_quick_message(index):
            return False
        sender = getattr(getattr(self, "status_worker", None), "key_sender", None)
        send = getattr(sender, "send_clipboard_message", None)
        if send is None:
            self._set_quick_message_status("发送失败：游戏输入未接入。")
            return False
        try:
            if send() is False:
                raise OSError("无法聚焦游戏窗口")
            self._set_quick_message_status(
                f"已发送：{self._quick_messages[index]}"
            )
            return True
        except Exception as exc:
            self._set_quick_message_status(f"发送失败：{exc}")
            return False

    def _quick_message_begin_edit(self, index: int) -> None:
        if 0 <= index < len(self._quick_messages):
            self._render_quick_messages(index)

    def _quick_message_finish_edit(self, index: int, entry: Any) -> None:
        if entry is not self._quick_edit_entry:
            return
        self._quick_edit_entry = None
        self._quick_edit_index = None
        try:
            text = entry.get().strip()
        except Exception:
            text = ""
        if 0 <= index < len(self._quick_messages) and text:
            self._undo_capture("快捷消息")
            self._quick_messages[index] = text[:500]
            self._quick_messages_save()
        self._render_quick_messages()
        self._refit_window_to_content()

    def _quick_message_blur_on_outside_click(self, event: Any) -> None:
        """Commit inline quick-message edits on any click outside the entry.

        Labels and blank panel space do not take keyboard focus in Tk, so a
        plain ``<FocusOut>`` alone leaves an editor visibly active after such
        clicks.  Deferring the commit until the current mouse dispatch is
        complete preserves the destination click (including edit/delete).
        """

        entry = getattr(self, "_quick_edit_entry", None)
        index = getattr(self, "_quick_edit_index", None)
        if entry is None or index is None or getattr(event, "widget", None) is entry:
            return
        root = getattr(self, "_root", None)
        if root is None:
            return
        job = getattr(self, "_quick_edit_blur_job", None)
        if job is not None:
            try:
                root.after_cancel(job)
            except Exception:
                pass

        def commit_if_still_active() -> None:
            self._quick_edit_blur_job = None
            self._quick_message_finish_edit(int(index), entry)

        self._quick_edit_blur_job = root.after_idle(commit_if_still_active)

    def _quick_delete(self, index: int) -> None:
        if 0 <= index < len(self._quick_messages):
            self._undo_capture("快捷消息")
            deleted = self._quick_messages.pop(index)
            self._quick_messages_save()
            self._render_quick_messages()
            self._set_quick_message_status(f"已删除：{deleted}")
            self._refit_window_to_content()

    def _freeze_column_widths(self) -> Optional[tuple[int, int]]:
        """Freeze both grid tracks after their complete initial layout.

        The compact left column has an explicit invariant width.  The right
        column is measured from its widest actual control row so it never
        clips values or buttons when the window is fitted to the UI.
        """

        columns = getattr(self, "_columns_frame", None)
        col1 = getattr(self, "_col1_frame", None)
        col2 = getattr(self, "_col2_frame", None)
        if columns is None or col1 is None or col2 is None:
            return None
        try:
            left_width = _LEFT_COLUMN_WIDTH
            right_width = _RIGHT_COLUMN_WIDTH
            columns.columnconfigure(0, weight=0, minsize=left_width)
            columns.columnconfigure(1, weight=0, minsize=right_width)
            col1.configure(width=left_width)
            col1.grid_propagate(False)
            col2.configure(width=right_width)
            col2.grid_propagate(False)
            # The grid itself is non-propagating.  These measured/fixed
            # tracks are the complete width lock without clipping vertical
            # panel growth.
            self._locked_column_widths = (left_width, right_width)
            return self._locked_column_widths
        except Exception:
            LOG.debug("column width lock unavailable", exc_info=True)
            return None

    def _refit_window_to_content(self) -> None:
        """Fit the window height exactly to the UI content.

        Called once before the first display and after quick-message rows are
        added/removed/edited, so the window grows when rows are added and
        shrinks back when they are deleted (Tk never shrinks automatically).
        The target height is
        derived from the taller column's REQUIRED height plus the window
        chrome, so the fit is exact - no leftover padding and no growth
        beyond the content.
        """

        root = getattr(self, "_root", None)
        if root is None:
            return
        try:
            if root.state() == "zoomed":
                return
            columns = getattr(self, "_columns_frame", None)
            col1 = getattr(self, "_col1_frame", None)
            col2 = getattr(self, "_col2_frame", None)
            if columns is None or col1 is None or col2 is None:
                return
            root.update_idletasks()
            chrome = root.winfo_height() - columns.winfo_height()
            needed = chrome + max(col1.winfo_reqheight(),
                                  col2.winfo_reqheight())
            try:
                min_height = int(root.minsize()[1]) or 200
            except Exception:
                min_height = 200
            needed = max(needed, min_height)
            current = root.winfo_height()
            if abs(needed - current) < 2:
                return
            width = root.winfo_width()
            x = root.winfo_x()
            y = root.winfo_y()
            root.geometry(f"{width}x{needed}+{x}+{y}")
            root.update_idletasks()
        except Exception:
            LOG.debug("window content refit failed", exc_info=True)

    def _telegram_change_token(self) -> None:
        """Edit the token with the same set/clear semantics as event messages."""

        try:
            from tkinter import simpledialog

            token = simpledialog.askstring(
                "设置BOT token",
                "粘贴 Telegram BOT token；留空则清除设置。",
                parent=self._root,
                show="*",
            )
        except Exception as exc:
            self._configure_text_if_changed(
                self._telegram_status,
                "telegram_status",
                f"消息提醒: 无法打开 token 输入框 - {exc}",
            )
            return
        if token is None:
            return
        self._telegram_bot_token = token.strip()
        self._telegram_chat_id = ""
        self._configure_text_if_changed(
            self._telegram_status,
            "telegram_status",
            "消息提醒: 正在验证 BOT 配置...",
        )
        self._refresh_telegram_token_button()
        self._shutdown_on_change()

    def _refresh_telegram_token_button(self) -> None:
        """Use the same configured-state affordance as 求让消息 / 重连消息."""

        button = getattr(self, "_telegram_token_button", None)
        if button is None:
            return
        try:
            self._configure_text_if_changed(
                button,
                "telegram_token_button",
                "已设置" if self._telegram_bot_token.strip() else "设置BOT token",
            )
        except Exception:
            LOG.debug("Telegram token button refresh failed", exc_info=True)

    def _refresh_telegram_status(self) -> None:
        """Show notifier health and persist an auto-discovered chat ID."""

        if not hasattr(self, "_telegram_status"):
            return
        notifier = getattr(self, "telegram_notifier", None)
        if notifier is None:
            self._configure_text_if_changed(
                self._telegram_status, "telegram_status", "消息提醒: 工作线程未接入。"
            )
            return
        try:
            snapshot = notifier.snapshot()
            self._configure_text_if_changed(
                self._telegram_status, "telegram_status", str(snapshot["status"])
            )
            discovered = str(snapshot.get("chat_id", "")).strip()
            if discovered and discovered != self._telegram_chat_id:
                self._telegram_chat_id = discovered
                self._shutdown_save_settings(self._shutdown_collect_data())
            self._refresh_telegram_token_button()
        except Exception as exc:
            # Status display itself must be non-fatal too.
            self._configure_text_if_changed(
                self._telegram_status,
                "telegram_status",
                f"消息提醒: 状态读取失败 - {exc}",
            )

    # Human-readable key labels for the help dialog (mirrors hotkey.json
    # KEY_VK spellings used by HotkeyWorker).
    _HELP_KEY_LABELS = {
        "left": "←", "right": "→", "up": "↑", "down": "↓",
        "home": "Home", "end": "End", "insert": "Insert", "delete": "Delete",
        "bracketleft": "[", "bracketright": "]", "grave": "`",
        "a": "A", "d": "D", "e": "E", "f": "F", "s": "S", "g": "G",
        "0": "0", "1": "1", "2": "2", "3": "3", "4": "4",
        "5": "5", "6": "6", "7": "7", "8": "8", "9": "9",
    }

    _HELP_ACTION_LABELS = {
        "record:left_most_pos": "录制当前图层的 最左 巡逻点",
        "record:rope_pos": "录制当前图层的 绳索 点",
        "record:right_most_pos": "录制当前图层的 最右 巡逻点",
        "record_jump_point:left": "插入当前图层的 左跳 点 (Ctrl+D)",
        "record_jump_point:right": "插入当前图层的 右跳 点 (Ctrl+F)",
        "record_jump_point:up": "添入当前图层的上跳点 (Ctrl+End)",
        "record_split_point": "在当前位置添加贯穿全部楼层的波浪分区线 (Ctrl+S)",
        "record_flower_point": "录制当前绳索层的花点 (Ctrl+G)",
        "record_support_point": "录制当前图层的局部支持点 (Ctrl+A)",
        "record_dual_stationary_post:first": "录制桩1；随后按 D=朝左、F=朝右、G=双向 (Ctrl+R)",
        "record_dual_stationary_post:second": "录制桩2；随后按 D=朝左、F=朝右、G=双向 (Ctrl+T)",
        "record_skill_point": "录制技能点；随后按任意攻击键可绑定按键 (Ctrl+E)",
        "select_next_layer": "选择下一个录制图层 (Ctrl+↓)",
        "select_next_patrol_start": "选择下一个巡逻起始楼层 (Ctrl+Home)",
        "add_highest_layer": "添加最高楼层",
        "delete_highest_layer": "删除最高楼层",
        "toggle_patrol": "开始 / 停止巡逻 (Ctrl+`)",
        "adjust_fixed_attack_interval:-0.1": "缩短巡逻攻击间隔 0.1 秒",
        "adjust_fixed_attack_interval:+0.1": "加长巡逻攻击间隔 0.1 秒",
        "undo:last_change": "撤销最近一次更改（最多 10 步）",
        "trade:invite": "向鼠标所在角色发起交易",
        "trade:accept": "接受交易邀请",
    }

    def _help_key_label(self, keys: str) -> str:
        """Turn a hotkey.json ``ctrl+grave`` style chord into ``Ctrl+` ``."""

        parts = [part.strip() for part in str(keys).split("+")]
        if not parts:
            return str(keys)
        modifier = "Ctrl+" if parts[0].casefold() == "ctrl" else ""
        key = parts[-1].casefold()
        label = self._HELP_KEY_LABELS.get(key, key.title())
        return f"{modifier}{label}"

    def _help_action_label(self, action: str) -> str:
        """Chinese description for one hotkey action name."""

        if action.startswith("quick_message:"):
            index = int(action.partition(":")[2])
            return f"发送第 {index + 1} 条快捷消息"
        return self._HELP_ACTION_LABELS.get(action, action)

    def _help_hover_show(self, _event: Any = None) -> None:
        """Show the help popup on hover (mouse-enter), debounced briefly."""

        self._help_cancel_jobs()
        root = getattr(self, "_root", None)
        if root is None:
            return
        existing = getattr(self, "_help_popup", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    return
            except Exception:
                pass
        self._help_show_job = root.after(180, self._show_hotkey_help)

    def _help_hover_leave(self, _event: Any = None) -> None:
        """Hide the help popup when the pointer leaves (mouse-leave).

        A short grace period keeps the popup open while the pointer travels
        from the ? button onto the popup itself; entering the popup cancels
        the pending hide (see ``_help_bind_popup_hover``).
        """

        self._help_cancel_jobs()
        root = getattr(self, "_root", None)
        if root is None:
            return
        self._help_hide_job = root.after(220, self._hide_hotkey_help)

    def _help_cancel_jobs(self, _event: Any = None) -> None:
        """Cancel pending show/hide timers for the hover help popup."""

        root = getattr(self, "_root", None)
        if root is None:
            return
        for attr in ("_help_show_job", "_help_hide_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    root.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attr, None)

    def _help_bind_popup_hover(self, popup: Any) -> None:
        """Keep the popup alive while the pointer is over it."""

        popup.bind("<Enter>", self._help_cancel_jobs, add="+")
        popup.bind(
            "<Leave>", lambda _e: self._help_hover_leave(), add="+"
        )

    def _hide_hotkey_help(self) -> None:
        """Destroy the floating help popup if it is still open."""

        self._help_cancel_jobs()
        popup = getattr(self, "_help_popup", None)
        if popup is None:
            return
        try:
            if popup.winfo_exists():
                popup.destroy()
        except Exception:
            pass
        self._help_popup = None

    def _show_hotkey_help(self) -> None:
        """Floating help popup anchored under the caption-row "?" button.

        The popup is a borderless transient window (a floating message,
        like a tooltip) instead of a second framed dialog, so it does not
        steal focus from the game.  Hover-triggered: it appears while the
        pointer is over the "?" (or the popup itself) and disappears on
        mouse-leave; hovering the popup keeps it open, moving away closes it.
        """

        root = getattr(self, "_root", None)
        if root is None:
            return
        existing = getattr(self, "_help_popup", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    return
            except Exception:
                pass
        import tkinter as tk

        data = {}
        try:
            data = json.loads(
                (Path(__file__).resolve().parent / "hotkey.json")
                .read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            pass

        # Same look as the bindable-keys hint on the attack/buff key buttons:
        # one yellow ("#fffbd6") tooltip-style label with a solid border.
        popup = tk.Toplevel(root)
        popup.overrideredirect(True)      # floating message, no window frame
        popup.configure(bg="#fffbd6")
        popup.attributes("-topmost", False)
        self._help_popup = popup

        # Binding list as an aligned two-column table: keys on the left
        # (right-aligned), explanations on the right (left-aligned).  Each
        # column is one multi-line Label so every row lines up.
        bindings = data.get("bindings", [])
        key_lines: list[str] = []
        label_lines: list[str] = []
        quick_messages = []
        for item in bindings:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action", ""))
            # The ten quick-message slots (Ctrl+1..Ctrl+9, Ctrl+0) share one
            # row instead of ten identical rows.
            if action.startswith("quick_message:"):
                quick_messages.append(
                    (str(item.get("keys", "")), action)
                )
                continue
            keys = self._help_key_label(item.get("keys", ""))
            action_label = self._help_action_label(action)
            key_lines.append(keys)
            label_lines.append(action_label)
        if quick_messages:
            keys_label = self._help_key_label(quick_messages[0][0])
            last_keys = self._help_key_label(quick_messages[-1][0])
            if len(quick_messages) > 1 and keys_label != last_keys:
                key_lines.append(f"{keys_label} ~ {last_keys}")
            else:
                key_lines.append(keys_label)
            first_index = int(quick_messages[0][1].partition(":")[2])
            label_lines.append(
                f"发送第 {first_index + 1}~"
                f"{first_index + len(quick_messages)} 条快捷消息"
            )

        content = tk.Frame(
            popup, bg="#fffbd6", padx=7, pady=4, bd=1, relief="solid"
        )
        content.pack(fill="x")
        if key_lines:
            table = tk.Frame(content, bg="#fffbd6")
            table.pack(fill="x")
            tk.Label(
                table, text="按键", background="#fffbd6",
                foreground="#202020", font=("", 9, "bold"),
                anchor="e", width=18,
            ).grid(row=0, column=0, sticky="e", padx=(0, 10))
            tk.Label(
                table, text="功能", background="#fffbd6",
                foreground="#202020", font=("", 9, "bold"),
                anchor="w",
            ).grid(row=0, column=1, sticky="w")
            key_column = tk.Label(
                table, text="\n".join(key_lines), justify="right",
                background="#fffbd6", foreground="#202020",
                anchor="e", width=18,
            )
            key_column.grid(row=1, column=0, sticky="e", padx=(0, 10))
            tk.Label(
                table, text="\n".join(label_lines), justify="left",
                background="#fffbd6", foreground="#202020",
                anchor="w",
            ).grid(row=1, column=1, sticky="w")

        # Two setup requirements follow the binding table: both are conditions
        # the assistant cannot detect, and both were asked for by the operator
        # after field reports where the game simply ignored the jump chord or the
        # minimap geometry did not match the recording.  They stay below the
        # table so the bindings - what the operator opens "?" for - come first.
        if key_lines:
            # A thin rule separates the binding list from the requirements.
            tk.Frame(content, bg="#d8d0a8", height=1).pack(fill="x", pady=(5, 4))
        tk.Label(
            content, text="注意事项", background="#fffbd6",
            foreground="#202020", font=("", 9, "bold"), anchor="w",
        ).pack(fill="x")
        for line in (
            "1. 跳跃键必须绑定 alt。",
            "2. 游戏内分辨率必须设置为 1366x768；"
            "如果窗口太小，请到 Windows「显示设置」把缩放改为 150% 或 125%。",
        ):
            tk.Label(
                content, text=line, justify="left", anchor="w",
                background="#fffbd6", foreground="#202020",
                wraplength=380,
            ).pack(fill="x", pady=(0, 1))

        # Stay open while the pointer is over the popup; hover-out closes it.
        self._help_bind_popup_hover(popup)

        # Position below the caption-row "?" (or top-right as fallback).
        popup.update_idletasks()
        width = popup.winfo_reqwidth()
        height = popup.winfo_reqheight()
        anchor = getattr(self, "_help_button", None)
        x = root.winfo_rootx() + root.winfo_width() - width - 12
        y = root.winfo_rooty() + _CAPTION_HEIGHT + 4
        if anchor is not None:
            try:
                x = anchor.winfo_rootx() - width + anchor.winfo_width()
                y = anchor.winfo_rooty() + anchor.winfo_height() + 2
            except Exception:
                pass
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        x = max(4, min(x, screen_width - width - 8))
        y = max(4, min(y, screen_height - height - 8))
        popup.geometry(f"+{x}+{y}")

    def _refresh_shutdown_status(self) -> None:
        """Live countdown in the status line (called every UI poll tick)."""

        if not hasattr(self, "_shutdown_status"):
            return
        worker = getattr(self, "shutdown_worker", None)
        if worker is None or not worker.enabled:
            return
        deadline = getattr(worker, "_deadline", None)
        if deadline is None:
            return
        remaining = max(0.0, deadline - time.monotonic())
        hours = remaining / 3600.0
        if hours >= 1.0:
            text = f"定时关闭已启动: 游戏将在 {hours:.1f}小时后关闭。"
        else:
            minutes = int(remaining // 60.0)
            seconds = int(remaining % 60.0)
            text = (f"定时关闭已启动: 游戏将在 {minutes}分 {seconds:02d}秒后关闭。")
        self._configure_text_if_changed(self._shutdown_status, "shutdown_status", text)

    @staticmethod
    def _format_countdown_seconds(seconds: float) -> str:
        seconds = max(0, int(round(seconds)))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    def _countdown_on_change(self, _value: str = "") -> None:
        """Persist/apply the repeating reminder interval and enabled state."""

        control = getattr(self, "_countdown_interval_control", None)
        if control is None:
            return
        self._undo_capture_panel_change("倒计时设置")
        minutes, _ = control.normalize()
        minutes = int(minutes)
        interval_seconds = float(minutes * 60)
        self._countdown_remaining_var.set(interval_seconds)
        self._countdown_set_remaining_components(interval_seconds)
        data = self._shutdown_collect_data()
        self._shutdown_save_settings(data)
        self._countdown_apply_to_worker(data)
        self._countdown_refresh_grey()

    def _countdown_interval_seconds(self) -> float:
        control = getattr(self, "_countdown_interval_control", None)
        if control is None:
            return 60.0
        minutes, _ = control.normalize()
        return float(int(minutes) * 60)

    def _countdown_set_remaining_components(self, seconds: float) -> float:
        """Render the live remaining deadline as independent HH:mm:ss fields."""

        seconds = max(0.0, min(float(seconds), self._countdown_interval_seconds()))
        whole = int(round(seconds))
        hours, remainder = divmod(whole, 3600)
        minutes, seconds_part = divmod(remainder, 60)
        for control, value in (
            (getattr(self, "_countdown_remaining_hour_control", None), hours),
            (getattr(self, "_countdown_remaining_minute_control", None), minutes),
            (getattr(self, "_countdown_remaining_second_control", None), seconds_part),
        ):
            if control is not None:
                control.set_bounds(value)
        total = float(whole)
        if hasattr(self, "_countdown_remaining_var"):
            self._countdown_remaining_var.set(total)
        return total

    def _countdown_remaining_components_changed(self) -> None:
        """Apply a manual HH:mm:ss edit without pausing the countdown."""

        if not bool(getattr(self, "_countdown_enabled_var", None).get()):
            return
        self._undo_capture_panel_change("倒计时设置")
        controls = (
            getattr(self, "_countdown_remaining_hour_control", None),
            getattr(self, "_countdown_remaining_minute_control", None),
            getattr(self, "_countdown_remaining_second_control", None),
        )
        if any(control is None for control in controls):
            return
        hours = int(controls[0].minimum_var.get())
        minutes = int(controls[1].minimum_var.get())
        seconds = int(controls[2].minimum_var.get())
        remaining = min(
            float(hours * 3600 + minutes * 60 + seconds),
            self._countdown_interval_seconds(),
        )
        self._countdown_set_remaining_components(remaining)
        worker = getattr(self, "countdown_worker", None)
        if worker is not None:
            worker.set_remaining_seconds(remaining)

    def _save_countdown_resume_state(self) -> None:
        """Save the current reminder's wall-clock deadline on UI close."""

        worker = getattr(self, "countdown_worker", None)
        if worker is None:
            return
        try:
            enabled, interval, remaining = worker.snapshot()
            deadline_at = time.time() + remaining if enabled else None
            save_timer_state(
                timer_state_path(),
                enabled=enabled,
                deadline_at=deadline_at,
                interval_seconds=interval,
            )
            LOG.info(
                "countdown resume state saved enabled=%s deadline_at=%s",
                enabled,
                f"{deadline_at:.3f}" if deadline_at is not None else "none",
            )
        except Exception:
            LOG.warning("could not save countdown resume state", exc_info=True)

    def _restore_countdown_resume_state(self) -> None:
        """Restore a future saved deadline; expired deadlines stay unselected."""

        # ``countdown_enabled`` is the operator's durable selection.  A
        # leftover timer.json from a previously armed session must never
        # override an explicit saved-off 循环 choice at the next launch.
        if not bool(self._countdown_enabled_var.get()):
            return
        state = load_timer_state(timer_state_path())
        if not bool(state.get("enabled", False)):
            return
        try:
            deadline_at = float(state["deadline_at"])
            interval = max(0.01, float(state["interval_seconds"]))
        except (KeyError, TypeError, ValueError):
            LOG.warning("ignored malformed timer.json state")
            return
        remaining = deadline_at - time.time()
        if remaining <= 0.0:
            LOG.info("countdown resume deadline expired; leaving 循环 unselected")
            # Make that sentence true.  ``_countdown_on_change`` runs before
            # this restore and arms the worker from a FULL interval whenever
            # ``countdown_enabled`` was saved, so the early return left 循环
            # running: an app the operator had closed came back armed and kept
            # pushing a 循环提醒 per interval.  An expired saved deadline is not
            # a reason to re-arm, so the selection and the worker are disarmed
            # and that state is persisted.
            self._countdown_enabled_var.set(False)
            data = self._shutdown_collect_data()
            self._shutdown_save_settings(data)
            self._countdown_apply_to_worker(data)
            self._countdown_refresh_grey()
            return

        # Restore against the same 1..300 minute UI range.
        minutes = max(1, min(300, int(round(interval / 60.0))))
        interval = float(minutes * 60)
        remaining = min(remaining, interval)
        self._countdown_interval_control.set_bounds(minutes)
        self._countdown_enabled_var.set(True)
        data = self._shutdown_collect_data()
        self._shutdown_save_settings(data)
        self._countdown_apply_to_worker(data)
        worker = getattr(self, "countdown_worker", None)
        if worker is not None:
            worker.set_remaining_seconds(remaining)
        self._countdown_set_remaining_components(remaining)
        self._countdown_refresh_grey()
        LOG.info("countdown restored from timer.json remaining=%.1fs", remaining)

    def _countdown_apply_to_worker(self, data: dict) -> None:
        worker = getattr(self, "countdown_worker", None)
        if worker is None:
            self._countdown_status.configure(
                text="循环警报: 工作线程未接入 (无界面模式)。"
            )
            return
        minutes = int(data.get(
            "countdown_interval_minutes",
            round(float(data.get("countdown_interval_hours", 1.0)) * 60.0),
        ))
        minutes = max(1, min(300, minutes))
        worker.set_interval_hours(minutes / 60.0)
        worker.set_enabled(bool(data.get("countdown_enabled", False)))
        if worker.enabled:
            self._countdown_status.configure(
                text=f"循环警报已启动: 每 {minutes} 分钟触发已选提醒。"
            )
        else:
            self._countdown_status.configure(text="循环警报: 未启用。")

    def _countdown_refresh_grey(self) -> None:
        enabled = bool(self._countdown_enabled_var.get())
        for control in (
            getattr(self, "_countdown_interval_control", None),
            getattr(self, "_countdown_remaining_hour_control", None),
            getattr(self, "_countdown_remaining_minute_control", None),
            getattr(self, "_countdown_remaining_second_control", None),
        ):
            if control is not None:
                self._set_panel_state(control.frame, disabled=not enabled)

    def _refresh_countdown_status(self) -> None:
        """Keep HH:mm:ss fields synchronized with the live countdown."""

        if not hasattr(self, "_countdown_status"):
            return
        worker = getattr(self, "countdown_worker", None)
        if worker is None:
            return
        enabled, interval, remaining = worker.snapshot()
        if not enabled:
            return
        shown_remaining = self._format_countdown_seconds(remaining)
        display_state = (round(float(interval), 3), shown_remaining)
        if display_state != self._countdown_display_state:
            self._countdown_display_state = display_state
            self._countdown_set_remaining_components(remaining)
        self._configure_text_if_changed(
            self._countdown_status, "countdown_status",
            ("循环警报: 剩余 "
             f"{shown_remaining} / 间隔 {int(round(interval / 60.0))}m；到时提醒并自动重置。")
        )

    def _shutdown_load_settings(self) -> None:
        """Restore saved Additional Functions values and apply them live.

        The scheduled-shutdown CHECKBOX is deliberately NOT restored: a
        saved "enabled" would silently re-arm the countdown on every launch
        and could Alt+F4 the game mid-session (observed: the game closed
        unexpectedly while the user was interacting).  The user must tick it
        explicitly each session; only the hour value is remembered.
        """

        try:
            data = json.loads(
                self._shutdown_settings_path().read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            data = {}
        try:
            self._shutdown_enabled_var.set(False)
            if "shutdown_hours" in data:
                self._shutdown_hours_var.set(float(data["shutdown_hours"]))
            if "player_check_enabled" in data and hasattr(
                self, "_player_check_var"
            ):
                self._player_check_var.set(bool(data["player_check_enabled"]))
            if "disconnect_alert_enabled" in data and hasattr(
                self, "_disconnect_alert_var"
            ):
                self._disconnect_alert_var.set(bool(
                    data["disconnect_alert_enabled"]
                ))
            if "lie_alert_enabled" in data and hasattr(
                self, "_lie_alert_var"
            ):
                self._lie_alert_var.set(bool(data["lie_alert_enabled"]))
            # Restore the dependent selection as an armed session state too.
            # Previously the checkbox could render as selected from config
            # while _api_auto_lie_session_armed stayed False, so every real
            # lie event was silently ignored until the operator toggled it.
            saved_auto_lie = bool(data.get("auto_lie_api_enabled", False))
            if hasattr(self, "_api_auto_lie_var"):
                self._api_auto_lie_var.set(saved_auto_lie)
            # The backend travels with the selection, so a restart keeps 本地测谎
            # selected instead of silently falling back to the cloud pass.
            if hasattr(self, "_api_auto_lie_backend_var"):
                saved_backend = str(data.get("auto_lie_backend") or "").casefold()
                self._api_auto_lie_backend_var.set(
                    saved_backend if saved_backend in (
                        AUTOLIE_BACKEND_LOCAL, AUTOLIE_BACKEND_CLOUD,
                    ) else AUTOLIE_BACKEND_CLOUD
                )
                if saved_auto_lie and self._auto_lie_backend_is_local():
                    self._probe_local_lie_service()
            self._api_auto_lie_session_armed = saved_auto_lie
            if saved_auto_lie and hasattr(self, "_lie_alert_var"):
                self._lie_alert_var.set(True)
            if hasattr(self, "_sound_alert_var"):
                self._sound_alert_var.set(bool(
                    data.get("sound_alert_enabled", True)
                ))
            if hasattr(self, "_reconnect_var"):
                # 自动重连 is a safety feature the operator arms on purpose: restore it
                # together with its world/channel so a restart keeps working.
                saved_enabled = bool(data.get("auto_reconnect_enabled", False))
                saved_world = str(data.get("auto_reconnect_world", WORLD_NAMES[0]))
                if saved_world not in WORLD_NAMES:
                    saved_world = WORLD_NAMES[0]
                saved_channel = valid_channel(
                    data.get("auto_reconnect_channel", CHANNEL_DEFAULT)
                ) or CHANNEL_DEFAULT
                self._reconnect_var.set(saved_enabled)
                if saved_enabled and hasattr(self, "_disconnect_alert_var"):
                    # Persisted reconnect configurations from older versions
                    # may have 掉线 off.  Migrate them live rather than loading
                    # a reconnect checkbox that can never trigger.
                    self._disconnect_alert_var.set(True)
                self._reconnect_world_var.set(saved_world)
                control = getattr(self, "_reconnect_channel_control", None)
                if control is not None:
                    control.set_bounds(saved_channel)
                else:
                    self._reconnect_channel_var.set(saved_channel)
            if hasattr(self, "_auto_restart_var"):
                self._auto_restart_var.set(bool(data.get("auto_restart_enabled", False)))
            if hasattr(self, "_restart_offline_message_var"):
                self._restart_offline_message_var.set(
                    str(data.get("restart_offline_message", "")).strip()[:500]
                )
                self._refresh_workflow_message_button(
                    "restart_offline", "下线消息", self._restart_offline_message_var
                )
            if hasattr(self, "_reconnect_message_var"):
                self._reconnect_message_var.set(
                    str(data.get("reconnect_message", "")).strip()[:500]
                )
                self._refresh_workflow_message_button(
                    "reconnect", "重连消息", self._reconnect_message_var
                )
            if hasattr(self, "_player_request_message_var"):
                self._player_request_message_var.set(
                    str(data.get("player_request_message", "")).strip()[:500]
                )
                self._refresh_workflow_message_button(
                    "player_request", "求让消息", self._player_request_message_var
                )
            if hasattr(self, "_player_room_code_var"):
                self._player_room_code_var.set(
                    str(data.get("player_room_code", "")).strip()[:128]
                )
                button = getattr(self, "_player_room_code_button", None)
                if button is not None:
                    button.configure(
                        text="已设置" if self._player_room_code_var.get() else "房间码"
                    )
            if hasattr(self, "_player_channel_wait_var"):
                try:
                    # Configs from before the seconds UI stored whole minutes.
                    # Migrate them on load rather than turning an old five-minute
                    # preference into a five-second switch.
                    if "player_channel_wait_seconds" in data:
                        wait_seconds = int(data["player_channel_wait_seconds"])
                    else:
                        wait_seconds = int(
                            data.get("player_channel_wait_minutes", 5)
                        ) * 60
                except (TypeError, ValueError):
                    wait_seconds = 300
                normalized_wait = max(1, min(1200, wait_seconds))
                control = getattr(self, "_player_channel_wait_control", None)
                if control is not None:
                    control.set_bounds(normalized_wait)
                else:
                    self._player_channel_wait_var.set(normalized_wait)
                self._refresh_player_wait_hint()
            if "screen_blink_enabled" in data and hasattr(
                    self, "_screen_blink_var"
            ):
                self._screen_blink_var.set(bool(data["screen_blink_enabled"]))
            if hasattr(self, "_telegram_enabled_var"):
                self._telegram_enabled_var.set(bool(
                    data.get("telegram_enabled", False)
                ))
                self._telegram_bot_token = str(
                    data.get("telegram_bot_token", "")
                ).strip()
                self._telegram_chat_id = str(
                    data.get("telegram_chat_id", "")
                ).strip()
                self._telegram_machine_var.set(str(
                    data.get("telegram_machine_name", "")
                ))
                self._configure_text_if_changed(
                    self._telegram_machine_button,
                    "telegram_machine_button",
                    machine_name_button_text(self._telegram_machine_var.get()),
                )
                self._refresh_telegram_token_button()
            if hasattr(self, "_quick_messages"):
                self._quick_messages = normalize_quick_messages(
                    data.get("quick_messages", [])
                )
                self._render_quick_messages()
            if hasattr(self, "_countdown_enabled_var"):
                # 循环 is an explicit saved choice.  Restore it exactly: an
                # unchecked saved setting launches with grey inert sliders;
                # a checked setting launches selected with active sliders and
                # restores/starts its countdown normally.
                self._countdown_enabled_var.set(bool(
                    data.get("countdown_enabled", False)
                ))
                raw_minutes = data.get("countdown_interval_minutes")
                if raw_minutes is None and "countdown_interval_hours" in data:
                    raw_minutes = round(float(data["countdown_interval_hours"]) * 60.0)
                if raw_minutes is not None:
                    self._countdown_interval_control.set_bounds(
                        max(1, min(300, int(raw_minutes)))
                    )
        except (KeyError, TypeError, ValueError):
            LOG.warning("ignored malformed additional functions settings",
                        exc_info=True)
            return
        self._shutdown_on_change()
        if hasattr(self, "_countdown_enabled_var"):
            self._countdown_on_change()
            self._restore_countdown_resume_state()
            # Re-apply after Tk has completed widget geometry/style setup.
            # This uses the same BooleanVar loaded above, not an inferred
            # worker/timer state, so a saved unchecked 循环 remains grey.
            root = getattr(self, "_root", None)
            if root is not None:
                root.after_idle(self._countdown_refresh_grey)
            else:
                self._countdown_refresh_grey()
        LOG.info("additional functions settings loaded from %s",
                 self._shutdown_settings_path())

    def _yolo_save_settings(self) -> None:
        """Persist current YOLO panel values to the local JSON file."""

        data = {
            "threshold": round(float(self._yolo_threshold_var.get()), 2),
            "attack_range": int(self._yolo_attack_range_var.get()),
            "min_mob_size": int(self._yolo_min_mob_var.get())
            if hasattr(self, "_yolo_min_mob_var") else 60,
            "detection_fps": int(self._yolo_fps_var.get())
            if hasattr(self, "_yolo_fps_var") else 10,
            "zone_width": int(self._yolo_zone_w_var.get()),
            "zone_height": int(self._yolo_zone_h_var.get()),
            "zone_shift_y": int(self._yolo_zone_shift_y_var.get()),
            "show_detection": bool(self._yolo_show_var.get()),
        }
        path = self._yolo_settings_path()
        try:
            path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            LOG.info("yolo settings saved to %s", path)
        except OSError:
            LOG.warning("could not save yolo settings to %s", path, exc_info=True)

    def _yolo_on_threshold_change(self, _event: Any = None) -> None:
        """Update the threshold label as the slider moves (no auto-save)."""

        if not hasattr(self, "_yolo_threshold_label"):
            return
        value = round(float(self._yolo_threshold_var.get()), 2)
        self._yolo_threshold_var.set(value)
        self._yolo_threshold_label.configure(text=f"{value:.2f}")

    def _yolo_on_range_change(self, _value: str = "") -> None:
        """Update the attack-range label as the slider moves (percent)."""

        if not hasattr(self, "_yolo_attack_range_label"):
            return
        value = int(self._yolo_attack_range_var.get())
        self._yolo_attack_range_label.configure(text=f"{value}%")

    def _yolo_on_min_mob_change(self, _value: str = "") -> None:
        """Update the mob-size-range label (min %, max = 4x min)."""

        if not hasattr(self, "_yolo_min_mob_label"):
            return
        value = int(self._yolo_min_mob_var.get())
        max_value = min(60, value * 4)
        self._yolo_min_mob_label.configure(
            text=f"最小 {value}% / 最大 {max_value}%"
        )

    def _yolo_on_fps_change(self, _value: str = "") -> None:
        """Update the detection-FPS label as the slider moves."""

        if not hasattr(self, "_yolo_fps_label"):
            return
        value = int(self._yolo_fps_var.get())
        self._yolo_fps_label.configure(text=f"{value} fps")

    def _yolo_on_zone_change(self, _value: str = "") -> None:
        """Update the zone size labels as the sliders move."""

        if not hasattr(self, "_yolo_zone_w_label"):
            return
        w = int(self._yolo_zone_w_var.get())
        h = int(self._yolo_zone_h_var.get())
        self._yolo_zone_w_label.configure(text=f"{w}%")
        self._yolo_zone_h_label.configure(text=f"{h}%")
        if hasattr(self, "_yolo_zone_shift_y_label"):
            shift = int(self._yolo_zone_shift_y_var.get())
            self._yolo_zone_shift_y_label.configure(
                text=f"{shift:+d}%" if shift else "0%"
            )

    def _yolo_start(self) -> None:
        """Launch the YOLO live detection as a subprocess with the UI threshold."""

        if not self._YOLO_MONSTER_DETECTION_ENABLED:
            self._yolo_status.configure(text="暂时停用。")
            LOG.info("yolo detection launch ignored: feature temporarily disabled")
            return

        if self._yolo_process is not None and self._yolo_process.poll() is None:
            self._yolo_status.configure(text="YOLO 检测已在运行中。")
            return
        threshold = 0.4
        try:
            threshold = float(self._yolo_threshold_var.get())
        except (ValueError, TypeError):
            self._yolo_threshold_var.set(0.4)
            threshold = 0.4
        yolo_root = Path(__file__).resolve().parent / "yolo-detection"
        script = yolo_root / "live_view.py"
        if not script.is_file():
            self._yolo_status.configure(
                text=f"缺少 yolo-detection 文件夹: {yolo_root} — "
                     "请确认整个文件夹已完整解压。"
            )
            return
        python = yolo_root / "venv313" / "Scripts" / "python.exe"
        using_main_env = False
        if not python.is_file():
            # 回退：直接使用助手当前的主环境 Python（安装.bat 会把 YOLO
            # 依赖装进 .venv，即 Python 3.10-3.12），不再要求单独的 venv313。
            python = Path(sys.executable)
            using_main_env = True
        import subprocess

        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):  # Windows: no console window
            creationflags = subprocess.CREATE_NO_WINDOW
        # 模型文件检查：界面默认从 yolo-detection\weights\best.pt 加载。
        weights = yolo_root / "weights" / "best.pt"
        if not weights.is_file():
            self._yolo_status.configure(
                text=f"缺少模型文件: {weights} — 请把训练好的模型"
                     "（best.pt）放到 yolo-detection\\weights\\ 目录。"
            )
            return
        # 依赖预检：主环境回退时快速确认 torch/ultralytics/mss/cv2 可用
        # （find_spec 不真正导入，秒级完成）。
        if using_main_env:
            try:
                probe = subprocess.run(
                    [str(python), "-c",
                     "import importlib.util as u;print(all("
                     "u.find_spec(m) is not None for m in "
                     "('torch','ultralytics','mss','cv2')))"],
                    capture_output=True, text=True, timeout=30,
                    creationflags=creationflags,
                )
                deps_ok = (probe.returncode == 0
                           and probe.stdout.strip().endswith("True"))
            except Exception:
                deps_ok = False
            if not deps_ok:
                self._yolo_status.configure(
                    text="缺少 YOLO 依赖（torch/ultralytics/mss/cv2）。"
                         "请重新双击 安装.bat 安装全部依赖。"
                )
                return
        cmd = [str(python), str(script), "--threshold", f"{threshold}"]
        if hasattr(self, "_yolo_fps_var"):
            cmd.extend(["--fps", f"{int(self._yolo_fps_var.get())}"])
        if hasattr(self, "_yolo_min_mob_var"):
            cmd.extend(["--min-mob-size",
                        f"{int(self._yolo_min_mob_var.get())}"])
            # 最大尺寸 = 最小尺寸 × 4（同一条进度条控制）。
            cmd.extend(["--max-mob-size",
                        f"{min(60, int(self._yolo_min_mob_var.get()) * 4)}"])
        # Always publish YOLO rope state: the patrol worker uses it to gate
        # the inner-gap jump on the real screen gap.
        cmd.extend(["--rope-state", str(
            Path(__file__).resolve().parent / "work" / "rope_state.json"
        )])
        if not self._yolo_show_var.get():
            cmd.append("--no-show")
        # 自动攻击行为由「攻击模式」面板统一设置：YOLO 检测模式 = 自动攻击，
        # 攻击按键与固定攻击共用（来自攻击模式面板）。
        attack_key = "ctrl"
        if hasattr(self, "_fixed_attack_key_var"):
            attack_key = (self._fixed_attack_key_var.get().strip() or "ctrl")
        if (self._YOLO_MONSTER_DETECTION_ENABLED
                and getattr(self, "_attack_mode_var", None) is not None
                and self._attack_mode_var.get() == "yolo"):
            cmd.append("--attack")
            cmd.extend(["--attack-key", attack_key])
            cmd.extend(["--attack-log",
                        str(yolo_root / "attack.log")])
            # Share the attack state file with the patrol worker so patrol
            # movement pauses while a target is active (attack priority).
            cmd.extend(["--attack-state", str(
                Path(__file__).resolve().parent / "work" / "attack_state.json"
            )])
            cmd.extend(["--patrol-state", str(
                Path(__file__).resolve().parent / "work" / "patrol_state.json"
            )])
        attack_range = int(self._yolo_attack_range_var.get())
        cmd.extend(["--attack-range", f"{attack_range}"])
        zone_w = max(0.1, min(1.0, int(self._yolo_zone_w_var.get()) / 100.0))
        zone_h = max(0.1, min(1.0, int(self._yolo_zone_h_var.get()) / 100.0))
        cmd.extend(["--zone-width", f"{zone_w:.2f}",
                    "--zone-height", f"{zone_h:.2f}"])
        shift_y = max(-0.5, min(0.5, int(self._yolo_zone_shift_y_var.get()) / 100.0))
        cmd.extend(["--zone-shift-y", f"{shift_y:.2f}"])
        # 把 YOLO 进程的输出（含报错）写入 yolo_launch.log，失败时可排查。
        launch_log = yolo_root / "yolo_launch.log"
        try:
            log_handle = open(launch_log, "wb", buffering=0)
        except Exception:
            log_handle = None
        self._yolo_launch_log = log_handle
        self._yolo_process = subprocess.Popen(
            cmd,
            cwd=str(yolo_root),
            stdout=log_handle,
            stderr=subprocess.STDOUT if log_handle is not None
            else subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self._yolo_run_button.configure(state="disabled")
        self._yolo_stop_button.configure(state="normal")
        mode = "显示画面" if self._yolo_show_var.get() else "无窗口"
        attack = ("自动攻击已开" if (getattr(self, "_attack_mode_var", None)
                                    is not None
                                    and self._attack_mode_var.get() == "yolo")
                  else "检测模式")
        env_hint = "（主环境）" if using_main_env else ""
        self._yolo_status.configure(
            text=f"YOLO 检测运行中 {env_hint}({mode}, {attack}, "
                 f"阈值 {threshold:.2f})。点击停止以结束。"
        )
        LOG.info("yolo detection started threshold=%.2f show=%s pid=%s",
                 threshold, self._yolo_show_var.get(), self._yolo_process.pid)

    def _yolo_save_config(self) -> None:
        """Persist the current YOLO panel values and confirm on screen."""

        self._yolo_save_settings()
        self._yolo_save_threshold_to_config()
        if hasattr(self, "_yolo_status"):
            self._yolo_status.configure(
                text="配置已保存 - 下次启动时自动恢复。"
            )

    def _yolo_save_threshold_to_config(self) -> None:
        """Write the UI threshold into the YOLO detection config.yaml.

        config.yaml's ``detection_behavior.confidence_threshold`` is the
        source of truth the model reads on startup; keep it in sync with the
        slider so the saved threshold survives even without --threshold.

        The update runs in the yolo venv (venv313) because that environment
        has the yaml dependency; the assistant's own Python 3.10 env does
        not (and must not import auto.py, which needs mss).
        """

        try:
            import subprocess

            yolo_root = Path(__file__).resolve().parent / "yolo-detection"
            python = yolo_root / "venv313" / "Scripts" / "python.exe"
            if not python.is_file():
                LOG.warning("venv313 not found; config.yaml threshold not updated")
                return
            threshold = round(float(self._yolo_threshold_var.get()), 2)
            code = (
                "import sys; "
                "sys.path.insert(0, r'%s'); "
                "from auto import ConfigManager; "
                "m = ConfigManager(r'%s'); "
                "m.set('detection_behavior.confidence_threshold', %r); "
                "ok = m.save(); "
                "v = ConfigManager(r'%s').get("
                "'detection_behavior.confidence_threshold'); "
                "print('VERIFY', v); "
                "sys.exit(0 if (ok and abs(float(v) - %r) < 1e-6) else 3)"
            ) % (
                str(yolo_root),
                str(yolo_root / "config.yaml"),
                threshold,
                str(yolo_root / "config.yaml"),
                threshold,
            )
            result = subprocess.run(
                [str(python), "-c", code],
                capture_output=True, text=True, timeout=30,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                ),
            )
            if result.returncode == 0:
                LOG.info("threshold %.3f written + verified in %s",
                         threshold, yolo_root / "config.yaml")
            else:
                LOG.warning("config.yaml threshold update failed "
                            "(rc=%s): %s",
                            result.returncode,
                            (result.stdout + result.stderr).strip())
        except Exception:
            LOG.warning("could not update config.yaml threshold", exc_info=True)

    def _yolo_stop(self) -> None:
        """Terminate the YOLO detection subprocess."""

        proc = self._yolo_process
        if proc is None or proc.poll() is not None:
            self._yolo_process = None
            handle = getattr(self, "_yolo_launch_log", None)
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
                self._yolo_launch_log = None
            self._set_yolo_stopped_ui()
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        except Exception as exc:
            LOG.warning("yolo stop failed: %s", exc)
        self._yolo_process = None
        handle = getattr(self, "_yolo_launch_log", None)
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
            self._yolo_launch_log = None
        self._set_yolo_stopped_ui()
        LOG.info("yolo detection stopped")

    def _set_yolo_stopped_ui(self) -> None:
        """Update the stopped state only while the Tk controls still exist.

        The dashboard close handler destroys widgets before ``run`` enters
        cleanup. Calling ``configure`` on one of those stale buttons raised
        ``TclError`` and converted a normal close into an application crash.
        """

        root = getattr(self, "_root", None)
        if root is None:
            return
        try:
            if not root.winfo_exists():
                return
        except Exception:
            return
        updates = (
            ("_yolo_run_button", {"state": "normal"}),
            ("_yolo_stop_button", {"state": "disabled"}),
            ("_yolo_status", {"text": "YOLO 检测已停止。"}),
        )
        for attribute, options in updates:
            widget = getattr(self, attribute, None)
            if widget is None:
                continue
            try:
                if widget.winfo_exists():
                    widget.configure(**options)
            except Exception:
                # Process cleanup has already completed; stale UI feedback is
                # not important enough to make the assistant exit.
                LOG.debug("could not update closed YOLO UI", exc_info=True)

    def _yolo_sync_show_button(self) -> None:
        """Toggle the grey/inactive look based on the checked state."""

        if not hasattr(self, "_yolo_show_button"):
            return
        if self._yolo_show_var.get():
            self._yolo_show_button.configure(style="TCheckbutton")
        else:
            self._yolo_show_button.configure(style="Off.TCheckbutton")
        running = (self._yolo_process is not None
                   and self._yolo_process.poll() is None)
        if running:
            self._yolo_status.configure(
                text="请先停止检测再修改显示选项；重新运行以生效。"
            )

    def _record_button_press(self, layer_name: str, boundary: str) -> None:
        """Button pressed: arm the 1s long-press timer for unlock/clear."""
        if self.patrol_controller is None:
            return
        self._record_press_job = None
        self._record_hold_fired = False
        if self._root is not None:
            self._record_press_job = self._root.after(
                1000, lambda: self._record_button_hold(layer_name, boundary)
            )

    def _record_button_release(self, layer_name: str, boundary: str) -> None:
        """Button released.

        A SHORT click records an unlocked/empty point.  After a 1s long
        press already unlocked (and cleared) the point, this same release
        must NOT record - the user clicks again to re-record.
        """
        if self._root is not None:
            try:
                self._root.after_cancel(self._record_press_job)
            except Exception:
                pass
            self._record_press_job = None
        if self._record_hold_fired:
            # 长按解锁已在本按下的 1s 定时器里完成：释放不录制。
            self._record_hold_fired = False
            return
        if self.patrol_controller is None:
            return
        try:
            self.patrol_controller.select_layer(layer_name)
        except ValueError as exc:
            self._control_status.configure(text=str(exc))
            return
        if hasattr(self, "_selected_layer_var"):
            self._selected_layer_var.set(layer_name)
        key = (layer_name, boundary)
        saved_endpoint = self.patrol_controller.endpoint(layer_name, boundary)
        if record_button_is_locked(saved_endpoint, key in self._unlocked_points):
            self._control_status.configure(
                text=f"{layer_name} {boundary} 已录制：长按按钮 1 秒解锁并清除。"
            )
            return
        self._unlocked_points.discard(key)
        self._record_endpoint(boundary)

    def _record_button_hold(self, layer_name: str, boundary: str) -> None:
        """1s long press on a locked point: unlock AND clear its recording."""
        self._record_hold_fired = True
        if self.patrol_controller is None:
            return
        try:
            self.patrol_controller.select_layer(layer_name)
        except ValueError as exc:
            self._control_status.configure(text=str(exc))
            return
        if hasattr(self, "_selected_layer_var"):
            self._selected_layer_var.set(layer_name)
        key = (layer_name, boundary)
        saved_endpoint = self.patrol_controller.endpoint(layer_name, boundary)
        if not record_button_is_locked(saved_endpoint, key in self._unlocked_points):
            # 未锁定（空点/已解锁）：短按释放时已经处理录制。
            return
        self._unlocked_points.add(key)
        cleared = self.patrol_controller.clear_endpoint(layer_name, boundary)
        band_note = ""
        if cleared:
            if self.patrol_controller.layer_has_band(layer_name):
                band_note = " 已重新计算该层图层带。"
            else:
                band_note = (
                    " 该层已无最左/绳索/最右支持点，"
                    "图层带已清空；开始运行会提示需先录制支持点。"
                )
        self._control_status.configure(
            text=(f"已解锁并清除 {layer_name} {boundary} 的录制"
                  + ("。" if cleared else "（无数据）。")
                  + band_note
                  + " 现在短按即可录制当前位置。")
        )
        self._refresh_patrol_controls()

    def _start_patrol(self) -> bool:
        # The operator WANTS to patrol: remembered even when this attempt is refused (the attempt
        # usually fails because the character is still on a login page, which is exactly the state a
        # reconnect ends), so a later successful 自动重连 can start the patrol on its own.  The
        # operator's v1.0.30 report: he pressed 开始巡逻 at 18:08:01 ("yellow character marker was not
        # detected during patrol startup"), the reconnect then succeeded - and the patrol stayed
        # stopped because no patrol had ever been RUNNING for the reconnect to resume.
        if not self._license_allowed():
            self._show_license_refusal()
            return False
        if self._patrol_stop_pending:
            LOG.info("START PATROL ignored: previous stop is still releasing keys")
            self._control_status.configure(text="正在停止巡逻并释放按键，请稍候。")
            return False
        self._patrol_intent = True
        if self.patrol_controller is None:
            LOG.warning("START PATROL refused: the patrol controller is unavailable")
            self._control_status.configure(text="巡逻控制器不可用。")
            return False
        if self.patrol_controller.is_enabled():
            return True
        # A radio-button change is normally applied immediately, but a rapid
        # click on 开始巡逻 can reach this callback before Tk has delivered the
        # mode's command callback.  Apply the visible fixed/stationary choice
        # synchronously: otherwise the UI says 站桩攻击 while the movement
        # worker still follows an old route endpoint/temporary anchor.
        self._fixed_on_change()
        stationary_mode = self._stationary_attack_selected()
        dead_flower_mode = self._dead_flower_selected()
        mode_var = getattr(self, "_attack_mode_var", None)
        if (mode_var is not None and str(mode_var.get()) == "dual_stationary"
                and len(self._dual_stationary_posts_data()) != 2):
            self._control_status.configure(
                text="双桩模式开始条件不满足：请用 Ctrl+R 后按 D/F/G 录制两个桩点。"
            )
            return False
        if not (stationary_mode or dead_flower_mode) and not self.patrol_controller.can_start():
            route_errors = self.patrol_controller.validate_patrol_route()
            # Logged, not only shown: this refusal used to leave no trace in the log the operator
            # sends, so "the patrol toggle does nothing" was undiagnosable.
            LOG.warning(
                "START PATROL refused: a floor in the patrol range has no recorded action "
                "point (range=%s, layers=%s)",
                self.patrol_controller.patrol_range(),
                sorted(self.patrol_controller.snapshot_layers()),
            )
            self._control_status.configure(
                text=("无法开始: 巡逻路线的每层都需要最左和最右点。"
                      + (" " + "；".join(route_errors) if route_errors else ""))
            )
            return False
        self._control_status.configure(text="正在选择游戏窗口…")
        if self._root is not None:
            self._root.update_idletasks()
        armed = True
        if self.on_patrol_start is not None:
            try:
                result = self.on_patrol_start()
            except OSError as exc:
                LOG.warning("START PATROL refused: game window/calibration step failed: %s", exc)
                self._control_status.configure(
                    text=f"无法开始: 游戏窗口选择失败: {exc}"
                )
                return False
            except Exception as exc:
                # A setup error must never leave the dashboard stranded on
                # “正在选择游戏窗口…”.  Keep the traceback in the log, but
                # turn every unexpected callback failure into an actionable
                # visible refusal.
                LOG.exception("START PATROL setup failed")
                self._control_status.configure(text=f"无法开始: 启动设置失败: {exc}")
                return False
            # The hook returns False when it deliberately did not arm input (the
            # game window could not be prepared).  Nothing re-arms it later, so
            # this is a failure the operator has to retry, not a deferred start.
            armed = result is not False
        if not armed:
            LOG.warning(
                "START PATROL refused: input was not armed (window not ready, focus lost, or "
                "the calibration step vetoed it)"
            )
            self._control_status.configure(
                text="无法开始: 输入未武装（游戏窗口未就绪或焦点丢失），请重试。"
            )
            LOG.info(LOG_RUN_START + "（未武装输入，未开始）。")
            return False
        self.patrol_controller.set_enabled(True)
        # 攻击模式为「YOLO 检测」时，开始巡逻自动启动 YOLO 检测
        # （已在运行则跳过；缺依赖/模型会在状态栏给出提示）。
        if (getattr(self, "_attack_mode_var", None) is not None
                and self._attack_mode_var.get() == "yolo"):
            self._yolo_start()
        self._refresh_patrol_controls()
        if dead_flower_mode:
            self._control_status.configure(
                text="死花模式已开始：花点按 补药 定时循环；重置花到时才下绳巡逻。"
            )
        elif str(getattr(self, "_attack_mode_var", None).get()
                 if hasattr(self, "_attack_mode_var") else "fixed") == "flower_patrol":
            self._control_status.configure(
                text="跑花模式已开始：正常巡逻；仅在楼层1攻击。"
            )
        elif str(getattr(self, "_attack_mode_var", None).get()
                 if hasattr(self, "_attack_mode_var") else "fixed") == "stationary":
            self._control_status.configure(text="运行已开始。")
        else:
            self._control_status.configure(text="运行已开始。")
        LOG.info(LOG_RUN_START + "。")
        return True

    def _finish_prearmed_hotkey_stop(self) -> bool:
        """Apply the Tk-only half of a Ctrl+` stop already made by the hook.

        The hotkey worker clears the automation gate and starts key release at
        chord time.  This method intentionally sends no keys and never waits:
        it merely catches the dashboard up when Tk next becomes available.
        """

        self._patrol_intent = False
        if self.patrol_controller is None:
            return False
        if self._patrol_stop_pending:
            return True
        self._patrol_stop_pending = True
        self._patrol_stop_started_at = time.monotonic()
        self._patrol_stop_cleanup_finished_at = None
        try:
            with self._trace_ui_poll_step(
                "prearmed-hotkey-stop:status-widget"
            ):
                self._control_status.configure(text="正在停止巡逻并释放按键…")
            with self._trace_ui_poll_step(
                "prearmed-hotkey-stop:patrol-controls"
            ):
                self._refresh_patrol_controls()
        except Exception:
            LOG.debug("could not refresh pre-armed patrol stop", exc_info=True)
        LOG.info("STOP PATROL: UI caught up with immediate hotkey disarm")
        return True

    def _stop_patrol(self) -> bool:
        # The operator does NOT want to patrol any more: a later reconnect must not start one.
        self._patrol_intent = False
        # This is lock-free and must happen in the hotkey/UI callback itself.
        # The slower key scrub runs in the cleanup thread below; until then,
        # leaving this event set lets attack or movement emit another key after
        # the operator has already requested Stop Patrol.
        event = getattr(self, "automation_active_event", None)
        if event is not None:
            event.clear()
        if self.patrol_controller is None:
            return False
        if self._patrol_stop_pending:
            LOG.info("STOP PATROL already releasing keys")
            return True
        stop_started_at = time.monotonic()
        self._patrol_stop_started_at = stop_started_at
        self._patrol_stop_cleanup_finished_at = None
        # This is the main-panel state transition.  It happens before any
        # controller or keyboard work, so the user gets immediate feedback as
        # soon as Tk begins handling the toggle.
        try:
            self._control_status.configure(text="正在停止巡逻并释放按键…")
        except Exception:
            LOG.debug("Stop Patrol could not show immediate stopping status", exc_info=True)
        LOG.info("STOP PATROL timing: request accepted; stopping workers")
        try:
            self.patrol_controller.set_enabled(False)
        except Exception:
            LOG.exception("Stop Patrol controller transition failed")
            return False
        self._patrol_stop_pending = True
        self._patrol_stop_cleanup_done.clear()
        # Render the stopped state before waiting on the keyboard-state lock.
        # A worker may be halfway through a key transaction; waiting for that
        # lock on Tk used to make the whole app look crashed.
        try:
            self._refresh_patrol_controls()
        except Exception:
            LOG.exception("Stop Patrol immediate UI refresh failed")

        def release_input_in_background() -> None:
            cleanup_started_at = time.monotonic()
            try:
                LOG.info(
                    "STOP PATROL timing: background cleanup started after %s",
                    self._format_stop_elapsed(
                        (cleanup_started_at - stop_started_at) * 1000.0
                    ),
                )
                if self.on_patrol_stop is not None:
                    self.on_patrol_stop()
            except Exception:
                # A foreground/key-release problem must be recorded, but Stop
                # Patrol must leave the dashboard usable for manual recovery.
                LOG.exception("Stop Patrol input cleanup failed")
            finally:
                self._patrol_stop_cleanup_finished_at = time.monotonic()
                self._patrol_stop_cleanup_done.set()
                LOG.info(
                    "STOP PATROL timing: background cleanup finished in %s",
                    self._format_stop_elapsed(
                        (self._patrol_stop_cleanup_finished_at - cleanup_started_at) * 1000.0
                    ),
                )

        threading.Thread(
            target=release_input_in_background,
            name="patrol-stop-cleanup",
            daemon=True,
        ).start()
        # YOLO is no longer part of the patrol runtime.  In particular, Stop
        # Patrol must not synchronously wait up to three seconds for a legacy
        # detector process that this interface no longer starts.
        LOG.info(LOG_RUN_STOP + "。")
        try:
            # The controls already reflect the stopped controller. Keep Start
            # disabled until the background key cleanup says it is safe.
            self._refresh_patrol_controls()
        except Exception:
            # The stop operation itself succeeded; a stale/destroyed Tk
            # widget must not turn a safe input stop into an app crash.
            LOG.exception("Stop Patrol UI refresh failed")
        return True

    @staticmethod
    def _format_stop_elapsed(milliseconds: Optional[float]) -> str:
        """Render a bounded timing value for Stop Patrol diagnostics."""

        if milliseconds is None:
            return "n/a"
        return f"{max(0.0, float(milliseconds)):.0f}ms"

    def _stationary_attack_selected(self) -> bool:
        """Whether Start Patrol must use the route-independent stand-still mode.

        The UI value is included as well as the live worker state.  This keeps
        the Start button usable during the tiny interval between selecting
        ``站桩攻击`` and the setting callback applying it to the worker.
        """

        mode_var = getattr(self, "_attack_mode_var", None)
        if mode_var is not None:
            try:
                if str(mode_var.get()) in ("stationary", "dual_stationary"):
                    return True
            except Exception:
                pass
        return bool(getattr(
            getattr(self, "movement_worker", None),
            "stationary_attack_enabled", False,
        ))

    def _dead_flower_selected(self) -> bool:
        """Whether Buff/HP/MP-only mode bypasses route and minimap gates."""

        mode_var = getattr(self, "_attack_mode_var", None)
        if mode_var is not None:
            try:
                if str(mode_var.get()) == "dead_flower":
                    return True
            except Exception:
                pass
        return bool(getattr(
            getattr(self, "movement_worker", None), "dead_flower_mode", False,
        ))

    def _add_layer_above(self) -> bool:
        if self.patrol_controller is None:
            self._control_status.configure(text="巡逻控制器不可用。")
            return False
        try:
            undo_saved = self._undo_capture("添加楼层")
            layer_name = self.patrol_controller.add_layer_above()
        except (OSError, ValueError) as exc:
            if undo_saved:
                self._undo_discard_latest("添加楼层")
            self._control_status.configure(text=f"无法添加楼层: {exc}")
            return False
        self._control_status.configure(
            text=(f"已选择 {layer_name}。请手动移动到该层并录制任意巡逻点 "
                  "(最左 / 绳索 / 最右)。巡逻已暂停。")
        )
        self._refresh_patrol_controls()
        return True

    def _reset_recording(self) -> None:
        controller = self.patrol_controller
        if controller is None:
            try:
                self._control_status.configure(text="巡逻控制器不可用。")
            except Exception:
                pass
            return
        # Resetting the profile while a patrol owns its route/marker state can
        # leave workers holding references to removed layers.  This used to be
        # a visible refusal; retain that safe gate instead of trying to stop,
        # delete, and rebuild everything in one Tk button callback.
        if controller.is_enabled():
            LOG.info("RESET RECORDING ignored: stop patrol first")
            try:
                self._control_status.configure(text="请先停止巡逻，再重置录制。")
            except Exception:
                LOG.debug("could not display reset refusal", exc_info=True)
            return
        if self._patrol_stop_pending:
            LOG.info("RESET RECORDING ignored: patrol key cleanup is active")
            try:
                self._control_status.configure(text="正在停止巡逻并释放按键，请稍候再重置录制。")
            except Exception:
                LOG.debug("could not display reset wait", exc_info=True)
            return

        self._patrol_intent = False
        try:
            undo_saved = self._undo_capture("重置录制")
            controller.reset_recording()
            # 双桩点 are persisted by the fixed-attack section, not by the
            # map recording.  A true reset must clear those anchors as part of
            # the same action, then immediately remove them from the live
            # MovementWorker configuration as well.
            self._dual_stationary_posts = []
            self._undo_skip_next_change_key = "攻击设置"
            self._fixed_on_change()
            clear_stationary = getattr(
                getattr(self, "movement_worker", None),
                "clear_stationary_attack_anchor", None,
            )
            if callable(clear_stationary):
                clear_stationary("recording reset")
        except PermissionError:
            if undo_saved:
                self._undo_discard_latest("重置录制")
            # A configuration copied from an older extracted release can
            # still be owned by that release/Explorer.  Do not expose a raw
            # WinError path; the old recording must be recreated for the
            # current package before it can be exported again.
            LOG.warning("Reset Recording refused: user configuration is inaccessible")
            try:
                self._control_status.configure(
                    text="配置版本与当前助手版本不匹配，请重新录制以后导出新配置。"
                )
            except Exception:
                LOG.debug("could not display reset compatibility refusal", exc_info=True)
            return
        except Exception as exc:
            if undo_saved:
                self._undo_discard_latest("重置录制")
            # The reset must never take down the UI.  A locked config/reference
            # file, malformed on-disk profile, or a transient Windows error is
            # reported as a normal refusal the operator can retry.
            LOG.exception("Reset Recording failed")
            try:
                self._control_status.configure(text=f"无法重置录制: {exc}")
            except Exception:
                LOG.debug("could not display reset failure", exc_info=True)
            return

        # The profile write succeeded.  Everything below is best-effort
        # cleanup: none of these optional references may turn a completed
        # reset into an application crash.
        try:
            # A reset starts a fresh recording for the current map; adopt the
            # map name now on disk (it may have been edited or re-identified
            # since the UI started) so identity checks use the current name.
            self.configured_map_name = controller.map_name()
        except Exception:
            LOG.warning("Reset Recording could not refresh the map name", exc_info=True)
        for label, cleanup in (
            ("structure reference", lambda: getattr(
                self, "structure_tracker", None
            ).reset(delete_reference=True) if getattr(
                self, "structure_tracker", None
            ) is not None else None),
            ("minimap geometry", lambda: (
                getattr(getattr(self, "detector", None), "reset_geometry", lambda: None)()
            )),
            ("map identity", lambda: getattr(
                self, "map_identity_store", None
            ).remove(self.configured_map_name) if getattr(
                self, "map_identity_store", None
            ) is not None else None),
        ):
            try:
                cleanup()
            except Exception:
                LOG.warning("Reset Recording could not clear %s", label, exc_info=True)
        self._unlocked_points.clear()
        self._layer_row_names = ()
        try:
            self._refresh_patrol_controls()
            self._control_status.configure(
                text="录制已重置。图层1为空；巡逻已停止。"
            )
        except Exception:
            LOG.exception("Reset Recording UI rebuild failed")

    def _refresh_patrol_controls(self) -> None:
        if self.patrol_controller is None:
            self._start_patrol_button.configure(state="disabled")
            self._stop_patrol_button.configure(state="disabled")
            self._add_layer_button.configure(state="disabled")
            self._delete_layer_button.configure(state="disabled")
            self._reset_recording_button.configure(state="disabled")
            if hasattr(self, "_update_log_icon_visibility"):
                self._update_log_icon_visibility()
            return
        running = bool(self.patrol_controller.is_enabled())
        self._patrol_ui_running = bool(running)
        if hasattr(self, "_update_log_icon_visibility"):
            self._update_log_icon_visibility()
        # Physical hotkeys follow the same state: while patrol runs every
        # binding except the patrol-toggle chord is temporarily disabled.
        hotkeys = getattr(self, "hotkey_worker", None)
        setter = getattr(hotkeys, "set_patrol_running", None)
        if setter is not None:
            setter(bool(running))
        # 站桩攻击 records only a temporary current-position anchor at Start
        # Patrol. It intentionally has no recorded layers, ropes, bands or
        # route completeness requirement.
        can_start = (
            self._stationary_attack_selected()
            or self._dead_flower_selected()
            or self.patrol_controller.can_start()
        )
        selected = self.patrol_controller.selected_layer()
        snapshot = self.patrol_controller.snapshot()
        route = snapshot.route_order
        layer_names = list(route)
        layer_names.extend(name for name in snapshot.layers if name not in layer_names)
        layer_names = list(layer_display_order(layer_names))
        license_allowed = self._license_allowed()
        attack_mode_var = getattr(self, "_attack_mode_var", None)
        try:
            attack_mode = str(attack_mode_var.get()) if attack_mode_var is not None else ""
        except (AttributeError, tk.TclError):
            attack_mode = ""
        # Ordinary 站桩 uses a session-only anchor rather than a map-recorded
        # point.  It must nevertheless be part of the canvas cache identity:
        # otherwise deleting it would leave the old red marker painted until
        # some unrelated route setting happened to refresh the canvas.
        stationary_marker_key = None
        mover = getattr(self, "movement_worker", None)
        marker_getter = getattr(mover, "stationary_route_anchor_marker", None)
        if callable(marker_getter):
            try:
                marker = marker_getter()
                if marker is not None:
                    stationary_marker_key = (
                        str(marker[0]),
                        round(float(marker[1].x), 6),
                        round(float(marker[1].y), 6),
                        str(marker[2] if len(marker) > 2 else "right"),
                    )
            except (AttributeError, IndexError, TypeError, ValueError):
                LOG.debug("stationary route marker unavailable for canvas cache", exc_info=True)
        # Treat the patrol panel as a tiny declarative view.  This method is
        # called from both explicit actions and the periodic state observer;
        # deleting/recreating every canvas item, combobox value and button
        # state when the model is unchanged makes the whole Tk process feel
        # slow (even native file dialogs share that event loop).
        render_key = (
            running,
            can_start,
            # 双桩模式 and 站桩模式 both satisfy `_stationary_attack_selected`,
            # but paint different axis marker sources.  The precise mode must
            # participate in the cache key or a mode switch can retain the
            # prior canvas and show the wrong facing marker type.
            attack_mode,
            self._stationary_attack_selected(),
            self._dead_flower_selected(),
            tuple(
                (post.get("x"), post.get("y"), post.get("facing"), post.get("layer"))
                for post in self._dual_stationary_posts_data()
            ),
            stationary_marker_key,
            selected,
            tuple(layer_names),
            repr(snapshot.layers),
            bool(self._patrol_stop_pending),
            license_allowed,
        )
        if render_key == self._patrol_controls_render_key:
            return
        self._patrol_controls_render_key = render_key
        self._update_patrol_range_combos(layer_names)
        self._ensure_layer_rows(tuple(layer_names))
        if hasattr(self, "_selected_layer_var"):
            self._selected_layer_var.set(selected)
        for layer_name in layer_names:
            layer = snapshot.layers.get(layer_name, {})
            patrol_times = max(1, min(20, int(layer.get("patrol_times", 1)))) if isinstance(layer, dict) else 1
            self._layer_labels[layer_name].configure(
                text=f"{self._patrol_display_name(layer_name)} 巡逻"
            )
            control = self._layer_patrol_time_controls.get(layer_name)
            if control is not None:
                control.set_bounds(patrol_times)
            self._draw_layer_axis(
                layer_name, layer, snapshot.split_points,
            )
        start_state, stop_state = patrol_button_states(running, can_start)
        if self._patrol_stop_pending:
            start_state = "disabled"
        self._start_patrol_button.configure(state=start_state)
        self._stop_patrol_button.configure(state=stop_state)
        self._add_layer_button.configure(state="normal")
        self._delete_layer_button.configure(
            state="normal" if len(layer_names) > 0 else "disabled"
        )
        self._reset_recording_button.configure(state="normal")
        show_patrol_start_hint = bool(
            license_allowed
            and not running
            and not self._patrol_stop_pending
            and not self._stationary_attack_selected()
            and not self._dead_flower_selected()
            and not can_start
        )
        if show_patrol_start_hint:
            self._control_status.configure(
                text=("巡逻模式开始条件不满足。请检查楼层配置，"
                      "可点击右上书本图标查看教程。")
            )
        elif self._patrol_start_condition_hint_visible:
            self._control_status.configure(
                text="先录制 最左、绳索、最右，然后添加上方图层。"
            )
        self._patrol_start_condition_hint_visible = show_patrol_start_hint
        if not license_allowed:
            # The dashboard remains inspectable, but no capture-driven
            # recording or automation entry point is usable before activation.
            for button in (
                self._start_patrol_button, self._stop_patrol_button,
                self._add_layer_button, self._delete_layer_button,
                self._reset_recording_button,
                *self._record_buttons.values(),
            ):
                button.configure(state="disabled")
            self._patrol_start_combo.configure(state="disabled")
            self._patrol_end_combo.configure(state="disabled")

    def _update_patrol_range_combos(self, display_names: list[str]) -> None:
        """Feed the numeric-ascending floor list into the range comboboxes and
        restore the current range selection from the patrol controller.  The
        comboboxes display ``楼层N`` (not ``layerN``) so the range reads
        ``楼层1 -> 楼层N`` in the UI.
        """
        numeric = sorted(
            display_names,
            key=lambda name: int("".join(filter(str.isdigit, name)) or 0),
        )
        display_values = [self._patrol_display_name(name) for name in numeric]
        start, end = self.patrol_controller.patrol_range()
        selected_values = (
            self._patrol_display_name(start),
            self._patrol_display_name(end),
        )
        # ``StringVar.set`` alone leaves ttk.Combobox's internal current
        # index stale on some Windows/Tk builds.  Set the current index too,
        # so add/delete/Ctrl+Home visibly update both controls immediately.
        for combo, variable, selected in zip(
            (self._patrol_start_combo, self._patrol_end_combo),
            (self._patrol_start_var, self._patrol_end_var),
            selected_values,
        ):
            combo.configure(values=display_values)
            if selected in display_values:
                combo.current(display_values.index(selected))
            else:
                combo.set("")
            variable.set(selected if selected in display_values else "")

    def _patrol_display_name(self, layer_name: str) -> str:
        """UI display for a floor name (``layer2`` -> ``楼层2``)."""
        match = re.search(r"(\d+)$", layer_name)
        return f"楼层{match.group(1)}" if match else layer_name

    def _patrol_name_from_display(self, display: str) -> str:
        """Reverse of ``_patrol_display_name`` (``楼层2`` -> ``layer2``)."""
        match = re.search(r"(\d+)$", display)
        return f"layer{match.group(1)}" if match else display

    def _select_recording_layer(self, layer_name: str) -> bool:
        """Select one recording row without changing the patrol range."""

        if self.patrol_controller is None:
            return False
        try:
            self.patrol_controller.select_layer(layer_name)
        except ValueError as exc:
            self._control_status.configure(text=f"无法选择楼层: {exc}")
            return False
        if hasattr(self, "_selected_layer_var"):
            self._selected_layer_var.set(layer_name)
        self._control_status.configure(
            text=f"已选择 {self._patrol_display_name(layer_name)}。"
        )
        self._refresh_patrol_controls()
        return True

    def _delete_highest_layer(self) -> bool:
        """Delete the numeric top layer, regardless of the selected row."""

        if self.patrol_controller is None:
            self._control_status.configure(text="巡逻控制器不可用。")
            return False
        try:
            undo_saved = self._undo_capture("删除楼层")
            removed = self.patrol_controller.remove_highest_layer()
            selected = self.patrol_controller.selected_layer()
        except (OSError, ValueError) as exc:
            if undo_saved:
                self._undo_discard_latest("删除楼层")
            self._control_status.configure(text=f"无法删除楼层: {exc}")
            return False
        if selected:
            message = (f"已删除 {self._patrol_display_name(removed)}。已选择 "
                       f"{self._patrol_display_name(selected)}；巡逻已暂停。")
        else:
            message = (f"已删除 {self._patrol_display_name(removed)}。"
                       "已无楼层；可直接开始巡逻原地攻击/跳跃。")
        self._control_status.configure(text=message)
        self._refresh_patrol_controls()
        return True

    def _select_next_layer(self) -> bool:
        """Cycle selected recording layer in numeric order, wrapping N -> 1."""

        if self.patrol_controller is None:
            return False
        snapshot = self.patrol_controller.snapshot()
        names = sorted(
            snapshot.layers,
            key=lambda name: int("".join(filter(str.isdigit, name)) or 0),
        )
        if not names:
            return False
        current = self.patrol_controller.selected_layer()
        next_index = (names.index(current) + 1) % len(names) if current in names else 0
        return self._select_recording_layer(names[next_index])

    def _select_next_patrol_start(self) -> bool:
        """Cycle patrol start in numeric order, wrapping top back to layer1."""

        if self.patrol_controller is None:
            return False
        snapshot = self.patrol_controller.snapshot()
        names = sorted(
            snapshot.layers,
            key=lambda name: int("".join(filter(str.isdigit, name)) or 0),
        )
        if not names:
            return False
        start, end = self.patrol_controller.patrol_range()
        next_index = (names.index(start) + 1) % len(names) if start in names else 0
        next_start = names[next_index]
        if (end not in names or int("".join(filter(str.isdigit, end)) or 0)
                < int("".join(filter(str.isdigit, next_start)) or 0)):
            end = next_start
        try:
            undo_saved = self._undo_capture("巡逻范围")
            self.patrol_controller.set_patrol_range(next_start, end)
            # Ctrl+Home changes the patrol's lower bound, so make the same
            # layer visibly selected for recording as well.  Otherwise the
            # radio circle could stay on a different layer and make the hotkey
            # appear to have done nothing.
            self.patrol_controller.select_layer(next_start)
        except (OSError, ValueError) as exc:
            if undo_saved:
                self._undo_discard_latest("巡逻范围")
            self._control_status.configure(text=f"无法设置巡逻起始楼层: {exc}")
            return False
        self._control_status.configure(
            text=(f"巡逻楼层: {self._patrol_display_name(next_start)} → "
                  f"{self._patrol_display_name(end)}。")
        )
        self._refresh_patrol_controls()
        return True

    def _patrol_range_changed(self, _event: Any = None) -> None:
        """Apply the UI-selected contiguous patrol floor range."""
        if self.patrol_controller is None:
            return
        start_display = self._patrol_start_var.get()
        end_display = self._patrol_end_var.get()
        if not start_display or not end_display:
            return
        start = self._patrol_name_from_display(start_display)
        end = self._patrol_name_from_display(end_display)
        # Changing either side of a contiguous range must never leave the
        # other side invalid.  Clamp the opposite control instead of rejecting
        # the click and silently restoring the old selection.
        if int("".join(filter(str.isdigit, start)) or 0) > int(
                "".join(filter(str.isdigit, end)) or 0):
            if getattr(_event, "widget", None) is self._patrol_start_combo:
                end = start
            else:
                start = end
        try:
            undo_saved = self._undo_capture("巡逻范围")
            self.patrol_controller.set_patrol_range(start, end)
        except ValueError as exc:
            if undo_saved:
                self._undo_discard_latest("巡逻范围")
            self._control_status.configure(text=f"无法选择巡逻楼层: {exc}")
            self._refresh_patrol_controls()
            return
        self._control_status.configure(
            text=f"巡逻楼层: {start_display} → {end_display}（支持连续范围，可单选一层）"
        )
        self._refresh_patrol_controls()

    def _schedule_layer_axis_redraw(self) -> None:
        """Redraw axes after Tk assigns their final row width.

        Adding or removing a layer recreates its Canvas.  During that same
        callback it temporarily reports only its requested width; the final
        width arrives in ``<Configure>`` one idle turn later.  This redraw is
        intentionally independent of the normal patrol-controls render key,
        because the recording data did not change merely because geometry did.
        """

        root = getattr(self, "_root", None)
        if root is None or getattr(self, "_layer_axis_redraw_job", None) is not None:
            return
        try:
            self._layer_axis_redraw_job = root.after_idle(
                self._redraw_layer_axes_after_layout
            )
        except Exception:
            self._layer_axis_redraw_job = None

    def _redraw_layer_axes_after_layout(self) -> None:
        """Paint all current axes using their settled canvas dimensions."""

        self._layer_axis_redraw_job = None
        controller = getattr(self, "patrol_controller", None)
        if controller is None:
            return
        try:
            snapshot = controller.snapshot()
            for layer_name in tuple(getattr(self, "_layer_row_names", ())):
                canvas = self._layer_axis_canvases.get(layer_name)
                if canvas is None or not bool(canvas.winfo_exists()):
                    continue
                self._draw_layer_axis(
                    layer_name, snapshot.layers.get(layer_name, {}),
                    snapshot.split_points,
                )
        except Exception:
            LOG.debug("layer-axis post-layout redraw failed", exc_info=True)

    def _ensure_layer_rows(self, layer_names: tuple[str, ...]) -> None:
        if layer_names == self._layer_row_names:
            return
        for tooltip in self._rope_tooltips.values():
            tooltip.destroy()
        for child in self._layer_rows_frame.winfo_children():
            child.destroy()
        self._record_buttons.clear()
        self._rope_tooltips.clear()
        self._layer_labels.clear()
        self._layer_patrol_time_controls.clear()
        self._layer_axis_canvases.clear()
        self._layer_row_names = layer_names
        ttk = self._ttk
        for layer_name in layer_names:
            row = ttk.Frame(self._layer_rows_frame)
            row.pack(fill="x", pady=(1, 2))
            selector = ttk.Radiobutton(
                row,
                variable=self._selected_layer_var,
                value=layer_name,
                command=lambda layer=layer_name: (
                    self._select_recording_layer(layer)
                ),
            )
            selector.pack(side="left", anchor="center", padx=(0, 1))
            # The layer name stays independent of the fixed-height numeric
            # component, so hovering a value never shifts the label or axis.
            label = ttk.Label(row, width=8)
            label.pack(side="left", anchor="center")
            self._layer_labels[layer_name] = label
            repeat_control = NumericValueRangeControl(
                row,
                tk_module=self._tk,
                ttk_module=ttk,
                minimum=1,
                range_mode=False,
                value_type=int,
                step=1,
                lower_bound=1,
                upper_bound=20,
                value_width=3,
                extra_width_px=3,
                before_change=lambda layer=layer_name: self._undo_capture_callback(
                    f"{self._patrol_display_name(layer)}巡逻次数"
                ),
                on_change=lambda layer=layer_name: self._layer_patrol_times_changed(layer),
            )
            repeat_control.pack(side="left", padx=(0, 0))
            ttk.Label(row, text="次").pack(side="left", padx=(0, 1))
            self._layer_patrol_time_controls[layer_name] = repeat_control
            canvas = self._tk.Canvas(
                row,
                width=_LAYER_AXIS_WIDTH,
                height=_LAYER_AXIS_HEIGHT,
                highlightthickness=0,
                background="#f0f0f0",
                cursor="hand2",
            )
            # Restore the expanding canvas so its axis uses the full visible
            # lane. The patrol-count control already owns the recovered 10px,
            # therefore this expanded lane is still 10px shorter than before
            # without leaving a blank area at the right.
            canvas.pack(side="left", fill="x", expand=True)
            # Redraw after Tk assigns the expanded width so the logical
            # 8%–92% range fills the whole visible layer canvas.
            canvas.bind(
                "<Configure>",
                lambda _event: self._schedule_layer_axis_redraw(),
                add="+",
            )
            # Recording choices are intentionally right-click only.  Left
            # clicks remain free for normal UI selection and marker double-
            # click deletion, never opening an add-recording menu.
            canvas.bind(
                "<Button-3>",
                lambda event, layer=layer_name: self._layer_axis_menu(event, layer),
            )
            self._layer_axis_canvases[layer_name] = canvas
        self._schedule_layer_axis_redraw()


__all__ = [
    "DebugSnapshot",
    "UiLogHandler",
    "UiWorker",
    "build_debug_snapshot",
    "layer_display_order",
    "monitor_work_area_for_pointer",
    "patrol_button_states",
    "rope_unavailable_hint",
    "record_button_is_locked",
    "recorded_coordinate_text",
    "tooltip_cursor_top_right_position",
]
