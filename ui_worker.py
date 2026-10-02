"""Independent Tk debug dashboard fed by the capture frame bus."""

from __future__ import annotations

import collections
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import logging
import queue
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
    LicenseStatus, activate as activate_license, activate_via_server,
    revoke_license, validate_via_server, verify_license,
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
from update_manager import (
    UpdateError, export_user_config, import_user_config,
    find_newer_desktop_update,
    schedule_hidden_restart, schedule_package_update,
)
from runtime_paths import application_root, package_format
from auto_lie_secret import get_server_secret, has_server_secret


LOG = logging.getLogger(__name__)
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
TYPING_HOTKEY_PREFIXES = ("quick_message:", "quick_pickup:")

# The auto-lie result button has separate measured points for the two fixed
# game presets.  Wider clients follow the 1366 reference by width, including
# 1980x1020: (round(800 * 1980 / 1366), round(472 * 1980 / 1366)) = (1160, 684).
_AUTO_LIE_CONFIRM_1080_SIZE = (1080, 768)
_AUTO_LIE_CONFIRM_1080_POINT = (630, 428)
_AUTO_LIE_CONFIRM_REFERENCE_WIDTH = 1366
_AUTO_LIE_CONFIRM_REFERENCE_POINT = (800, 472)
# Completing a lie round can leave the API/game transition owning focus for a
# moment.  A click during that handoff lands on the desktop even when the point
# is correct, so the confirmation is deliberately gated on a settled game
# foreground check.
_AUTO_LIE_CONFIRM_FOCUS_SETTLE_SECONDS = 0.20
_AUTO_LIE_CONFIRM_FOCUS_ATTEMPTS = 3


def _auto_lie_confirm_client_point(
    client_width: int, client_height: int,
) -> tuple[int, int]:
    """Return the measured post-lie confirmation point in game-client pixels."""

    width = max(1, int(client_width))
    height = max(1, int(client_height))
    if (width, height) == _AUTO_LIE_CONFIRM_1080_SIZE:
        return _AUTO_LIE_CONFIRM_1080_POINT
    scale = width / float(_AUTO_LIE_CONFIRM_REFERENCE_WIDTH)
    return (
        int(round(_AUTO_LIE_CONFIRM_REFERENCE_POINT[0] * scale)),
        int(round(_AUTO_LIE_CONFIRM_REFERENCE_POINT[1] * scale)),
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
# A repeating debug-UI poll failure logs a full traceback this often; the ones in between are DEBUG.
POLL_FAILURE_LOG_SECONDS = 10.0
# A lie event waiting for an already-running pass is dropped after this long, so a stuck pass can
# never keep the pending flag set (and this method retrying) for the rest of the session.
AUTO_LIE_PENDING_MAX_SECONDS = 90.0

# The debug UI uses two columns (controls + debug/YOLO); the initial
# window (including the in-window caption bar when active) is compact by
# default while remaining user-resizable.
# The height stays user-resizable (only the minimum is enforced).
_INITIAL_WINDOW_WIDTH = 1076
_INITIAL_WINDOW_HEIGHT = 560
_LEFT_COLUMN_WIDTH = 500
_RIGHT_COLUMN_WIDTH = 540
_LAYER_AXIS_WIDTH = 430
_LAYER_AXIS_HEIGHT = 38
_LAYER_AXIS_LEFT = 8
_LAYER_AXIS_RIGHT = _LAYER_AXIS_WIDTH - 8
_LAYER_AXIS_Y = 28
# Both columns are FIXED at 500px (500 + 500 + 12px gap + 24px padding).
# The default height is deliberately compact; users can still enlarge it and
# their saved window size is never overwritten.

# Height of the custom in-window caption bar.  Tk cannot add widgets into
# the native OS caption, so the assistant window removes the native caption
# (keeping the OS resize borders and taskbar entry) and draws its own title
# row: app title on the left, then ？/－/□/× on the right at the same level.
_CAPTION_HEIGHT = 34

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
    min_width: int = 980,
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
_GEOMETRY_FORMAT = 6


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


class HoverTooltip:
    """Small cursor-adjacent tooltip that also works on disabled ttk buttons."""

    def __init__(self, widget: Any, text: str, delay_ms: int = 250) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = max(0, int(delay_ms))
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
            wraplength=360,
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
    keeps the latest ``history`` formatted lines (default 600) for the
    "copy running log" action.  Oldest lines are dropped like garbage once
    the history is full; nothing is written to disk here (the ordinary file
    handler owns the on-disk log).
    """

    def __init__(self, capacity: int = 300, history: int = 600) -> None:
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
# is retained in memory (UiLogHandler history, default 600 lines) and is
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
    """Display the saved marker, or the edit hint when it is empty."""

    return str(name).strip() or "修改名称"


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


def quick_message_preview(message: str, max_units: int = 36) -> str:
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
    _FIXED_RANDOM_GAP_STEP = 0.1
    _FIXED_RANDOM_GAP_MAX = 30.0
    _FIXED_ATTACK_INTERVAL_MAX = 30.0
    _OPTIONAL_MOTION_INTERVAL_MAX = 60.0
    # 捡东西 (the stand-still pickup circuit) is deliberately configured in
    # MINUTES: the operator reads and sets that row in "m", except values
    # below one minute are shown as seconds.  Its trigger range is 10s..30m.
    # The configuration and movement worker keep seconds-scale keys.
    _STATIONARY_PICKUP_INTERVAL_MIN_MINUTES = 10.0 / 60.0
    _STATIONARY_PICKUP_INTERVAL_MAX_MINUTES = 30.0
    _STATIONARY_PICKUP_INTERVAL_DEFAULT_MINUTES = 15.0
    _ATTACK_TIMING_SLIDER_LENGTH = 112
    # A DOUBLE click on a 随机 −/+ button moves the gap by this much in one go
    # (five seconds instead of 0.1), while a single click keeps the precise fine
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
        random_jump_worker: Any = None,
        small_step_worker: Any = None,
        hotkey_queue: Optional["queue.Queue[str]"] = None,
        hotkey_worker: Any = None,
        quick_pickup_worker: Any = None,
        quick_pickup_results: Optional["queue.Queue[tuple[str, str]]"] = None,
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
        on_patrol_start: Optional[Callable[[], None]] = None,
        on_patrol_restart: Optional[Callable[[], None]] = None,
        on_patrol_stop: Optional[Callable[[], None]] = None,
        on_capture_now: Optional[Callable[[], Any]] = None,
        on_recording_verified: Optional[Callable[[DebugSnapshot], None]] = None,
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
        # Independent Alt timer. It shares only the attack worker's runtime
        # gating events and exposes no configurable key binding.
        self.random_jump_worker = random_jump_worker
        # Timed directional micro-step; actual keys are owned by movement.
        self.small_step_worker = small_step_worker
        self.hotkey_queue = hotkey_queue
        # Physical hotkey hook whose bindings are temporarily disabled while
        # patrol runs (only the patrol-toggle chord stays live).
        self.hotkey_worker = hotkey_worker
        # Manual-only rapid Z pickup has its own worker/result channel so it
        # cannot arm the normal patrol automation workers.
        self.quick_pickup_worker = quick_pickup_worker
        self.quick_pickup_results = quick_pickup_results
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
        self._api_auto_lie_events: "queue.Queue[object]" = queue.Queue(maxsize=16)
        self._api_auto_lie_pending = False
        self._api_auto_lie_session_armed = False
        self._api_auto_lie_pending_since = 0.0
        self._api_auto_lie_wait_logged = False
        # Debounce state: whether the window currently on screen has already been handled, when
        # the square was last seen to be gone, and when the last pass started.
        self._api_auto_lie_event_active = False
        self._api_auto_lie_clear_since = 0.0
        self._api_auto_lie_last_pass_started = 0.0
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
        self._telegram_bot_token = ""
        self._telegram_chat_id = ""
        self._machine_name_press_job: Any = None
        self._machine_name_hold_fired = False
        self._machine_name_entry: Any = None
        self._quick_messages: list[str] = []
        self._quick_message_press_job: Any = None
        self._quick_message_hold_fired = False
        self._quick_message_last_click_at = float("-inf")
        self._quick_message_last_click_index: Optional[int] = None
        self._quick_delete_press_job: Any = None
        self._quick_delete_hold_fired = False
        self._quick_edit_entry: Any = None
        self.on_patrol_start = on_patrol_start
        self.on_patrol_restart = on_patrol_restart
        self.on_patrol_stop = on_patrol_stop
        self.on_capture_now = on_capture_now
        self.on_recording_verified = on_recording_verified
        self.log_queue = log_queue
        self.ui_log_handler = ui_log_handler
        self.user_config_path = user_config_path
        self.automation_active_event = automation_active_event
        self.lie_watch_armed_event = lie_watch_armed_event
        self.disconnect_watch_armed_event = disconnect_watch_armed_event
        self.channel_update_events = channel_update_events
        self.other_player_stop_events = other_player_stop_events
        # License verification is an independent boundary: it never changes
        # patrol/trade worker internals, it only decides whether UI actions may
        # enter those workflows.
        local_license_status = verify_license()
        # A local signature proves the document was issued by us, but does not
        # prove the online entitlement is still live.  Start locked and let
        # the immediate pinned heartbeat unlock the dashboard only after the
        # activation server accepts this device/license pair.
        self._license_status: LicenseStatus = (
            LicenseStatus(
                False, "checking", "正在验证在线授权…",
                local_license_status.license_id, local_license_status.edition,
                local_license_status.expires_at,
            )
            if local_license_status.valid else local_license_status
        )
        # Keep the UI honest while the immediate pinned validation is in
        # flight.  A locally signed document is useful context (expiry), but
        # it is not an active online entitlement until the
        # server has accepted it for this session.
        self._license_online_validation_pending = bool(local_license_status.valid)
        self._license_session_locked = True
        self._memory_usage_text = "内存：读取中"
        self._license_heartbeat_results: "queue.Queue[tuple[int, LicenseStatus]]" = (
            queue.Queue()
        )
        self._license_heartbeat_started = False
        self._license_heartbeat_generation = 0
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
        self._yolo_process: Any = None
        self.last_snapshot: Optional[DebugSnapshot] = None
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
        # Ctrl+` can be reported twice while Windows changes focus.  A second
        # request queued during a start must not immediately undo that start
        # after calibration completes.
        self._hotkey_toggle_ignore_until = 0.0

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
            if not self._lie_accounting_started:
                self._lie_accounting_worker.start()
                self._lie_accounting_started = True
            app_version = version_label()
            root.title(f"TodoHelper {app_version}")
            screen_width = root.winfo_screenwidth()
            screen_height = root.winfo_screenheight()
            # 调试窗口不抢前台：不设置 -topmost，游戏在爬绳/挂绳时保持焦点，
            # 不会因调试窗口抢到前台而松开按键、角色跳离绳索。窗口位置与大小
            # 按上次保存的几何恢复（可移动、可调整），默认不再固定左上角。
            # The size constants are LOGICAL pixels: on scaled displays
            # (125%/150%) Windows bitmap-scales the DPI-unaware window, so a
            # 1036x680 logical window physically renders 1295x850 / 1554x1020
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
            # whole-window (the default 1036 width includes the caption row).
            caption_installed = self._install_custom_caption(root, tk)
            root.geometry(clamped)
            root.minsize(
                _INITIAL_WINDOW_WIDTH,
                500 + (_CAPTION_HEIGHT if caption_installed else 0),
            )
            root.protocol("WM_DELETE_WINDOW", self._on_debug_window_close)
            self._schedule_window_geometry_save(root)
            # While the OS runs its own move/resize loop Windows draws the
            # frame outline; freeze client repaints until the shape is
            # confirmed, then repaint once (avoids relayout/repaint lag on
            # every resize step).  Also frees WM_SIZE handling for the
            # drag-move path below.
            self._install_resize_burst_guard(root)

            if caption_installed:
                self._build_caption_bar(root, tk, app_version)
            else:
                # Fallback when the custom caption cannot be installed: keep
                # the native caption and float a small ? at the top-right of
                # the content so the help dialog stays reachable.
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
            self._license_label.pack(side="left", fill="x", expand=True)
            self._license_label.bind("<Button-1>", self._copy_equipment_id)
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
            style.configure("Locked.TButton", foreground="#777777")
            style.map("Locked.TButton", foreground=[("!disabled", "#777777")])
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
                justify="left",
                wraplength=440,
                width=54,
            )
            self._control_status.pack(anchor="w", fill="x")
            automation_status_slot = ttk.Frame(controls, height=21)
            automation_status_slot.pack(fill="x", pady=(5, 0))
            automation_status_slot.pack_propagate(False)
            self._automation_status_label = ttk.Label(
                automation_status_slot, justify="left", wraplength=440, width=54
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
            fixed_key_row = ttk.Frame(fixed_panel)
            self._fixed_key_row = fixed_key_row
            fixed_key_row.pack(fill="x", pady=(4, 0))
            # 跳打 is a 站桩攻击 child option. Keep it directly before 按键
            # on this same row so the mode's attack control is one unit.
            self._stationary_jump_enabled_var = tk.BooleanVar(value=False)
            stationary_jump_button = ttk.Checkbutton(
                fixed_key_row, text="跳打",
                variable=self._stationary_jump_enabled_var,
                command=self._fixed_on_change,
            )
            self._stationary_jump_button = stationary_jump_button
            # This slot always exists.  Toggling attack mode only changes
            # enabled state, never the row's geometry.
            stationary_jump_button.pack(side="left", padx=(0, 4))
            fixed_key_label = ttk.Label(fixed_key_row, text="攻击键:")
            self._fixed_key_label = fixed_key_label
            fixed_key_label.pack(
                side="left", padx=(0, 4)
            )
            self._fixed_attack_key_var = tk.StringVar(value="ctrl")
            fixed_key_button = ttk.Button(
                fixed_key_row, text=self._fixed_attack_key_var.get(),
                width=5, style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    fixed_key_button, self._fixed_attack_key_var,
                    "_fixed_attack_key_previous",
                    lambda: self._fixed_on_change(),
                ),
            )
            fixed_key_button.pack(side="left", padx=(0, 4))
            self._fixed_key_button = fixed_key_button
            self._attach_bind_hint(fixed_key_button)
            # The interval/range controls sit in their own sub-frame so the
            # 跳跃攻击 mode can hide them and leave only the 按键 row.
            fixed_interval_group = ttk.Frame(fixed_key_row)
            fixed_interval_group.pack(side="right")
            ttk.Label(fixed_interval_group, text="每").pack(side="left")
            # A deliberately short slider keeps the full attack row inside
            # the compact left column without an artificial empty gap.
            self._fixed_interval_var = tk.DoubleVar(value=3.0)
            fixed_interval_slider = ttk.Scale(
                fixed_interval_group, from_=0.2,
                to=self._FIXED_ATTACK_INTERVAL_MAX, orient="horizontal",
                # 0.2..10.0 in 0.1s steps needs at least 98 usable pixels;
                # the former 82px track physically could not land on every
                # value even with snap rounding.
                variable=self._fixed_interval_var,
                length=self._ATTACK_TIMING_SLIDER_LENGTH,
                command=self._fixed_on_change,
            )
            fixed_interval_slider.pack(side="left", padx=(0, 2))
            self._fixed_interval_label = ttk.Label(
                fixed_interval_group, text="3.0s", width=5
            )
            self._fixed_interval_label.pack(side="left", padx=(0, 2))
            self._fixed_interval_range_label = ttk.Label(
                fixed_interval_group, text="(3.0s, 3.1s)", width=14,
                anchor="w",
            )
            self._fixed_interval_range_label.pack(side="left")
            self._fixed_interval_group = fixed_interval_group
            fixed_random_group = ttk.Frame(fixed_key_row)
            fixed_random_group.pack(side="right", padx=(0, 4))
            self._fixed_random_gap_var = tk.DoubleVar(value=0.1)
            fixed_gap_minus = ttk.Button(fixed_random_group, text="−", width=2)
            self._bind_repeat_step_button(
                fixed_gap_minus, lambda: self._fixed_adjust_random_gap(-0.1),
                current=self._fixed_random_gap_seconds,
                coarse=lambda anchor: self._set_fixed_random_gap(
                    (self._fixed_random_gap_seconds() if anchor is None else anchor)
                    - self._RANDOM_GAP_COARSE_STEP
                ),
            )
            fixed_gap_minus.pack(side="left", padx=(0, 1))
            self._fixed_random_gap_label = ttk.Label(
                fixed_random_group, text="0.1s", width=5, anchor="center"
            )
            self._fixed_random_gap_label.pack(side="left")
            fixed_gap_plus = ttk.Button(fixed_random_group, text="+", width=2)
            self._bind_repeat_step_button(
                fixed_gap_plus, lambda: self._fixed_adjust_random_gap(0.1),
                current=self._fixed_random_gap_seconds,
                coarse=lambda anchor: self._set_fixed_random_gap(
                    (self._fixed_random_gap_seconds() if anchor is None else anchor)
                    + self._RANDOM_GAP_COARSE_STEP
                ),
            )
            fixed_gap_plus.pack(side="left", padx=(1, 0))

            # 组合攻击 only configures the independent attack scheduler;
            # movement, patrol, and input arbitration keep their own roles.
            combo_row = ttk.Frame(fixed_panel)
            combo_row.pack(fill="x", pady=(2, 0))
            self._combo_attack_enabled_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                combo_row, text="组合攻击", width=7,
                variable=self._combo_attack_enabled_var,
                command=self._fixed_on_change,
            ).pack(side="left", padx=(0, 4))
            self._combo_attack_count_vars = []
            self._combo_attack_count_entries = []
            self._combo_attack_key_vars = []
            self._combo_attack_key_buttons = []
            for index in range(3):
                slot = ttk.Frame(combo_row)
                slot.pack(side="left", padx=(0, 5))
                minimum_var = tk.IntVar(value=1)
                maximum_var = tk.IntVar(value=1)
                self._combo_attack_count_vars.append((minimum_var, maximum_var))
                # Plain entries deliberately avoid Spinbox's up/down arrows:
                # the counts are typed ranges, then clamped on focus-out.
                minimum = ttk.Entry(
                    slot, width=3, justify="center", textvariable=minimum_var,
                )
                minimum.pack(side="left")
                minimum.bind("<FocusOut>", self._fixed_on_change, add="+")
                self._combo_attack_count_entries.append(minimum)
                ttk.Label(slot, text="-").pack(side="left")
                maximum = ttk.Entry(
                    slot, width=3, justify="center", textvariable=maximum_var,
                )
                maximum.pack(side="left")
                maximum.bind("<FocusOut>", self._fixed_on_change, add="+")
                self._combo_attack_count_entries.append(maximum)
                ttk.Label(slot, text="次").pack(side="left", padx=(1, 2))
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
            combo_help = ttk.Label(combo_row, text="?", width=2, anchor="e")
            combo_help.pack(side="left", padx=(0, 2))
            combo_help_tooltip = HoverTooltip(combo_help, self._combo_attack_hint())
            combo_help_tooltip.set_enabled(True)
            self._combo_attack_help_tooltip = combo_help_tooltip
            self._bind_key_tooltips.append(combo_help_tooltip)
            # Labels and empty panel space do not claim focus in Tk.  Commit
            # a typed count when any other UI target is clicked, just like the
            # reconnect-channel field does.
            root.bind_all(
                "<Button-1>", self._combo_attack_commit_on_outside_click,
                add="+",
            )

            # Facing is a stand-still recovery option, not part of 小碎步.
            # Left and right are mutually exclusive, and the selected side
            # is applied after an X correction returns to the temporary zone.
            stationary_facing_row = ttk.Frame(fixed_panel)
            self._stationary_facing_row = stationary_facing_row
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
            stationary_facing_row.pack(fill="x", pady=(2, 0))

            pickup_row = ttk.Frame(fixed_panel)
            pickup_row.pack(fill="x", pady=(2, 0))
            self._stationary_pickup_enabled_var = tk.BooleanVar(value=False)
            pickup_enabled = ttk.Checkbutton(
                pickup_row, text="捡东西", width=5,
                variable=self._stationary_pickup_enabled_var,
                command=self._fixed_on_change,
            )
            pickup_enabled.pack(side="left", padx=(0, 4))
            pickup_interval_group = ttk.Frame(pickup_row)
            pickup_interval_group.pack(side="right")
            ttk.Label(pickup_interval_group, text="每").pack(side="left")
            self._stationary_pickup_interval_var = tk.DoubleVar(
                value=self._STATIONARY_PICKUP_INTERVAL_DEFAULT_MINUTES
            )
            pickup_slider = ttk.Scale(
                pickup_interval_group,
                from_=self._STATIONARY_PICKUP_INTERVAL_MIN_MINUTES,
                to=self._STATIONARY_PICKUP_INTERVAL_MAX_MINUTES,
                orient="horizontal", variable=self._stationary_pickup_interval_var,
                length=self._ATTACK_TIMING_SLIDER_LENGTH,
                command=self._fixed_on_change,
            )
            pickup_slider.pack(side="left", padx=(0, 2))
            self._stationary_pickup_interval_label = ttk.Label(
                pickup_interval_group, text="15.0m", width=5
            )
            self._stationary_pickup_interval_label.pack(side="left", padx=(0, 2))
            self._stationary_pickup_interval_range_label = ttk.Label(
                pickup_interval_group, text="(15.0m, 15.0m)", width=14, anchor="w"
            )
            self._stationary_pickup_interval_range_label.pack(side="left")
            pickup_random_group = ttk.Frame(pickup_row)
            pickup_random_group.pack(side="right", padx=(0, 4))
            self._stationary_pickup_gap_var = tk.DoubleVar(value=0.1)
            pickup_gap_minus = ttk.Button(pickup_random_group, text="−", width=2)
            self._bind_repeat_step_button(
                pickup_gap_minus, lambda: self._stationary_pickup_adjust_gap(-0.1),
                current=self._stationary_pickup_gap_minutes,
                coarse=lambda anchor: self._set_stationary_pickup_gap(
                    (self._stationary_pickup_gap_minutes() if anchor is None else anchor)
                    - self._RANDOM_GAP_COARSE_STEP
                ),
            )
            pickup_gap_minus.pack(side="left", padx=(0, 1))
            self._stationary_pickup_gap_label = ttk.Label(
                pickup_random_group, text="0.1m", width=5, anchor="center"
            )
            self._stationary_pickup_gap_label.pack(side="left")
            pickup_gap_plus = ttk.Button(pickup_random_group, text="+", width=2)
            self._bind_repeat_step_button(
                pickup_gap_plus, lambda: self._stationary_pickup_adjust_gap(0.1),
                current=self._stationary_pickup_gap_minutes,
                coarse=lambda anchor: self._set_stationary_pickup_gap(
                    (self._stationary_pickup_gap_minutes() if anchor is None else anchor)
                    + self._RANDOM_GAP_COARSE_STEP
                ),
            )
            pickup_gap_plus.pack(side="left", padx=(1, 0))
            self._stationary_pickup_controls = [
                pickup_enabled, pickup_slider, pickup_gap_minus, pickup_gap_plus,
            ]

            step_row = ttk.Frame(fixed_panel)
            step_row.pack(fill="x", pady=(4, 0))
            self._small_step_row = step_row
            self._small_step_enabled_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                step_row, text="小碎步", width=5,
                variable=self._small_step_enabled_var,
                command=self._fixed_on_change,
            ).pack(side="left", padx=(0, 4))
            step_interval_group = ttk.Frame(step_row)
            step_interval_group.pack(side="right")
            ttk.Label(step_interval_group, text="每").pack(side="left")
            self._small_step_interval_var = tk.DoubleVar(value=5.0)
            ttk.Scale(
                step_interval_group, from_=3.0,
                to=self._OPTIONAL_MOTION_INTERVAL_MAX,
                orient="horizontal",
                variable=self._small_step_interval_var,
                length=self._ATTACK_TIMING_SLIDER_LENGTH,
                command=self._fixed_on_change,
            ).pack(side="left", padx=(0, 2))
            self._small_step_interval_label = ttk.Label(
                step_interval_group, text="5.0s", width=5
            )
            self._small_step_interval_label.pack(side="left", padx=(0, 2))
            self._small_step_interval_range_label = ttk.Label(
                step_interval_group, text="(5.0s, 5.1s)", width=14,
                anchor="w",
            )
            self._small_step_interval_range_label.pack(side="left")
            step_random_group = ttk.Frame(step_row)
            step_random_group.pack(side="right", padx=(0, 4))
            self._small_step_gap_var = tk.DoubleVar(value=0.1)
            step_gap_minus = ttk.Button(step_random_group, text="−", width=2)
            self._bind_repeat_step_button(
                step_gap_minus, lambda: self._small_step_adjust_gap(-0.1),
                current=self._small_step_gap_seconds,
                coarse=lambda anchor: self._set_small_step_gap(
                    (self._small_step_gap_seconds() if anchor is None else anchor)
                    - self._RANDOM_GAP_COARSE_STEP
                ),
            )
            step_gap_minus.pack(side="left", padx=(0, 1))
            self._small_step_gap_label = ttk.Label(
                step_random_group, text="0.1s", width=5, anchor="center"
            )
            self._small_step_gap_label.pack(side="left")
            step_gap_plus = ttk.Button(step_random_group, text="+", width=2)
            self._bind_repeat_step_button(
                step_gap_plus, lambda: self._small_step_adjust_gap(0.1),
                current=self._small_step_gap_seconds,
                coarse=lambda anchor: self._set_small_step_gap(
                    (self._small_step_gap_seconds() if anchor is None else anchor)
                    + self._RANDOM_GAP_COARSE_STEP
                ),
            )
            step_gap_plus.pack(side="left", padx=(1, 0))

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
            drug_panel.pack(fill="x", pady=(0, 8))
            hp_row = ttk.Frame(drug_panel)
            hp_row.pack(fill="x")
            self._hp_use_var = tk.BooleanVar(value=True)
            hp_use_button = ttk.Checkbutton(
                hp_row, text="HP", variable=self._hp_use_var,
                command=self._drug_on_change,
            )
            hp_use_button.pack(side="left")
            ttk.Label(hp_row, text="按键:").pack(side="left", padx=(8, 4))
            self._hp_key_var = tk.StringVar(value="delete")
            hp_key_button = ttk.Button(
                hp_row, text=self._hp_key_var.get(), width=14,
                style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    hp_key_button, self._hp_key_var, "_hp_key_previous",
                    lambda: self._drug_on_change(),
                ),
            )
            hp_key_button.pack(side="left", padx=(0, 10))
            self._hp_key_button = hp_key_button
            self._attach_bind_hint(hp_key_button)
            ttk.Label(hp_row, text="HP 低于以下时喝药:").pack(side="left")
            self._hp_threshold_var = tk.IntVar(value=50)
            # Fixed 500px column: the slider is shortened (length=60) so the
            # row's total width stays inside the 500px column instead of
            # pushing it wider.
            hp_threshold_slider = ttk.Scale(
                hp_row, from_=5, to=95, orient="horizontal",
                variable=self._hp_threshold_var,
                command=self._drug_on_change,
                length=60,
            )
            hp_threshold_slider.pack(side="left", fill="x",
                                     expand=True, padx=(8, 8))
            self._hp_threshold_label = ttk.Label(hp_row, text="50%", width=6)
            self._hp_threshold_label.pack(side="left")
            mp_row = ttk.Frame(drug_panel)
            mp_row.pack(fill="x", pady=(6, 0))
            self._mp_use_var = tk.BooleanVar(value=True)
            mp_use_button = ttk.Checkbutton(
                mp_row, text="MP", variable=self._mp_use_var,
                command=self._drug_on_change,
            )
            mp_use_button.pack(side="left")
            ttk.Label(mp_row, text="按键:").pack(side="left", padx=(8, 4))
            self._mp_key_var = tk.StringVar(value="end")
            mp_key_button = ttk.Button(
                mp_row, text=self._mp_key_var.get(), width=14,
                style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    mp_key_button, self._mp_key_var, "_mp_key_previous",
                    lambda: self._drug_on_change(),
                ),
            )
            mp_key_button.pack(side="left", padx=(0, 10))
            self._mp_key_button = mp_key_button
            self._attach_bind_hint(mp_key_button)
            ttk.Label(mp_row, text="MP 低于以下时喝药:").pack(side="left")
            self._mp_threshold_var = tk.IntVar(value=30)
            mp_threshold_slider = ttk.Scale(
                mp_row, from_=5, to=95, orient="horizontal",
                variable=self._mp_threshold_var,
                command=self._drug_on_change,
                length=60,
            )
            mp_threshold_slider.pack(side="left", fill="x",
                                     expand=True, padx=(8, 8))
            self._mp_threshold_label = ttk.Label(mp_row, text="30%", width=6)
            self._mp_threshold_label.pack(side="left")
            # Periodic buff rows: a bound key tapped on a timer.  Each row has
            # its own 5..600 second slider (default 600 seconds) that decides
            # when the key is triggered.  Unlike HP/MP these are time-based,
            # not bar-percent based.
            buff1_row = ttk.Frame(drug_panel)
            buff1_row.pack(fill="x", pady=(6, 0))
            self._buff1_use_var = tk.BooleanVar(value=False)
            buff1_use_button = ttk.Checkbutton(
                buff1_row, text="饲料", variable=self._buff1_use_var,
                command=self._drug_on_change,
            )
            buff1_use_button.pack(side="left")
            ttk.Label(buff1_row, text="按键:").pack(side="left", padx=(8, 4))
            self._buff1_key_var = tk.StringVar(value="home")
            buff1_key_button = ttk.Button(
                buff1_row, text=self._buff1_key_var.get(), width=14,
                style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    buff1_key_button, self._buff1_key_var,
                    "_buff1_key_previous",
                    lambda: self._drug_on_change(),
                ),
            )
            buff1_key_button.pack(side="left", padx=(0, 10))
            self._buff1_key_button = buff1_key_button
            self._attach_bind_hint(buff1_key_button)
            ttk.Label(buff1_row, text="每").pack(side="left")
            # Buff refresh period in seconds (default 600): horizontal slider
            # in the same progress-bar style as the other panels.
            self._buff1_interval_var = tk.DoubleVar(value=600.0)
            buff1_interval_slider = ttk.Scale(
                buff1_row, from_=5.0, to=600.0, orient="horizontal",
                variable=self._buff1_interval_var,
                command=self._drug_on_change,
                length=60,
            )
            buff1_interval_slider.pack(side="left", fill="x",
                                       expand=True, padx=(8, 8))
            self._buff1_interval_label = ttk.Label(
                buff1_row, text="600s", width=8
            )
            self._buff1_interval_label.pack(side="left")
            buff2_row = ttk.Frame(drug_panel)
            buff2_row.pack(fill="x", pady=(6, 0))
            self._buff2_use_var = tk.BooleanVar(value=False)
            buff2_use_button = ttk.Checkbutton(
                buff2_row, text="增益 1", variable=self._buff2_use_var,
                command=self._drug_on_change,
            )
            buff2_use_button.pack(side="left")
            ttk.Label(buff2_row, text="按键:").pack(side="left", padx=(8, 4))
            self._buff2_key_var = tk.StringVar(value="insert")
            buff2_key_button = ttk.Button(
                buff2_row, text=self._buff2_key_var.get(), width=14,
                style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    buff2_key_button, self._buff2_key_var,
                    "_buff2_key_previous",
                    lambda: self._drug_on_change(),
                ),
            )
            buff2_key_button.pack(side="left", padx=(0, 10))
            self._buff2_key_button = buff2_key_button
            self._attach_bind_hint(buff2_key_button)
            ttk.Label(buff2_row, text="每").pack(side="left")
            self._buff2_interval_var = tk.DoubleVar(value=600.0)
            buff2_interval_slider = ttk.Scale(
                buff2_row, from_=5.0, to=600.0, orient="horizontal",
                variable=self._buff2_interval_var,
                command=self._drug_on_change,
                length=60,
            )
            buff2_interval_slider.pack(side="left", fill="x",
                                       expand=True, padx=(8, 8))
            self._buff2_interval_label = ttk.Label(
                buff2_row, text="600s", width=8
            )
            self._buff2_interval_label.pack(side="left")
            buff3_row = ttk.Frame(drug_panel)
            buff3_row.pack(fill="x", pady=(6, 0))
            self._buff3_use_var = tk.BooleanVar(value=False)
            buff3_use_button = ttk.Checkbutton(
                buff3_row, text="增益 2", variable=self._buff3_use_var,
                command=self._drug_on_change,
            )
            buff3_use_button.pack(side="left")
            ttk.Label(buff3_row, text="按键:").pack(side="left", padx=(8, 4))
            self._buff3_key_var = tk.StringVar(value="pageup")
            buff3_key_button = ttk.Button(
                buff3_row, text=self._buff3_key_var.get(), width=14,
                style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    buff3_key_button, self._buff3_key_var,
                    "_buff3_key_previous",
                    lambda: self._drug_on_change(),
                ),
            )
            buff3_key_button.pack(side="left", padx=(0, 10))
            self._buff3_key_button = buff3_key_button
            self._attach_bind_hint(buff3_key_button)
            ttk.Label(buff3_row, text="每").pack(side="left")
            self._buff3_interval_var = tk.DoubleVar(value=600.0)
            buff3_interval_slider = ttk.Scale(
                buff3_row, from_=5.0, to=600.0, orient="horizontal",
                variable=self._buff3_interval_var,
                command=self._drug_on_change,
                length=60,
            )
            buff3_interval_slider.pack(side="left", fill="x",
                                       expand=True, padx=(8, 8))
            self._buff3_interval_label = ttk.Label(
                buff3_row, text="600s", width=8
            )
            self._buff3_interval_label.pack(side="left")
            # Restore previously saved drug settings and apply them live.
            self._drug_load_settings()

            # Persistent clipboard shortcuts. Short click copies, double-click
            # sends to game chat, and long press edits. The adjacent delete
            # icon also requires a 1s long press.
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
                quick_header, text="单击复制；双击发送；长按 1 秒修改/删除。",
                justify="left", wraplength=300,
            )
            self._quick_message_status.pack(side="left", padx=(8, 0))
            self._quick_messages_frame = ttk.Frame(quick_panel)
            self._quick_messages_frame.pack(fill="x", pady=(6, 0))
            self._render_quick_messages()

            # Additional Functions panel: optional extras, each gated by its
            # own checkbox.  First one: scheduled shutdown - after X hours
            # the game gets Alt+F4, the worker verifies the window is gone,
            # then every worker is stopped.
            extra_panel = ttk.LabelFrame(
                col2, text="附加功能", padding=(8, 6)
            )
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

            # 循环 的 间隔/剩余 progress bars sit on the SAME line as the
            # 掉线/测谎/循环 checkboxes (column 1 is 500px wide to fit).
            ttk.Label(alarm_row, text="间隔").pack(side="left", padx=(4, 0))
            self._countdown_interval_var = tk.DoubleVar(value=1.0)
            self._countdown_interval_slider = ttk.Scale(
                alarm_row, from_=0.1, to=6.0, orient="horizontal",
                # The usable ttk trough is shorter than its requested length
                # because the thumb occupies part of it.  60px therefore
                # cannot represent all 59 tenth-hour steps (for example 3.0h
                # can be skipped).  80px keeps the control compact while
                # retaining every 0.1h selection through 6.0h.
                length=80,
                variable=self._countdown_interval_var,
                command=self._countdown_on_change,
            )
            self._countdown_interval_slider.pack(
                side="left", padx=(1, 1)
            )
            self._countdown_interval_label = ttk.Label(
                alarm_row, text="1.0h", width=4
            )
            self._countdown_interval_label.pack(side="left")

            ttk.Label(alarm_row, text="剩余").pack(side="left", padx=(2, 0))
            self._countdown_remaining_var = tk.DoubleVar(value=3600.0)
            self._countdown_remaining_slider = ttk.Scale(
                alarm_row, from_=0.0, to=3600.0,
                orient="horizontal",
                length=85,
                variable=self._countdown_remaining_var,
                command=self._countdown_remaining_on_drag,
            )
            self._countdown_remaining_slider.pack(
                side="left", padx=(1, 1)
            )
            self._countdown_dragging = False
            self._countdown_remaining_slider.bind(
                "<ButtonPress-1>", self._countdown_drag_start
            )
            self._countdown_remaining_slider.bind(
                "<ButtonRelease-1>", self._countdown_drag_end
            )
            # Clock notation is compact while still keeping every unit clear.
            self._countdown_remaining_label = ttk.Label(
                alarm_row, text="1:00:00", width=8
            )
            self._countdown_remaining_label.pack(side="left")
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
            ttk.Checkbutton(
                reminder_row,
                text="声音",
                variable=self._sound_alert_var,
                command=self._shutdown_on_change,
            ).pack(side="left", padx=(4, 8))

            self._screen_blink_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                reminder_row,
                text="闪烁",
                variable=self._screen_blink_var,
                command=self._shutdown_on_change,
            ).pack(side="left", padx=(0, 8))

            self._telegram_enabled_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                reminder_row,
                text="消息",
                variable=self._telegram_enabled_var,
                command=self._shutdown_on_change,
            ).pack(side="left")
            telegram_row = ttk.Frame(extra_panel)
            telegram_row.pack(fill="x", pady=(4, 0))
            self._telegram_machine_row = telegram_row
            ttk.Label(telegram_row, text="设备名称").pack(side="left")
            self._telegram_machine_var = tk.StringVar(value="")
            self._telegram_machine_button = ttk.Button(
                telegram_row, text="修改名称", width=14
            )
            self._telegram_machine_button.pack(side="left", padx=(4, 8))
            self._telegram_machine_button.bind(
                "<ButtonPress-1>", self._machine_name_press
            )
            self._telegram_machine_button.bind(
                "<ButtonRelease-1>", self._machine_name_release
            )
            self._telegram_token_button = ttk.Button(
                telegram_row, text="修改BOT token",
                command=self._telegram_change_token,
            )
            self._telegram_token_button.pack(side="left")
            self._telegram_status = ttk.Label(
                extra_panel,
                text="消息提醒: 未启用；BOT token 仅保存在本机用户配置。",
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
            AUTO_LIE_LOG.info(
                "自动过测谎: selection loaded as %s",
                "ON" if saved_api_auto_lie else "OFF",
            )
            # Keep restart, lie, and reconnect as separate selections.  自动重开
            # only hands a newly launched client to 自动重连; it must never
            # silently alter the reconnect selection itself.
            reconnect_row = ttk.Frame(extra_panel)
            reconnect_row.pack(fill="x", pady=(4, 0))
            self._api_auto_lie_var = tk.BooleanVar(value=saved_api_auto_lie)
            self._api_auto_lie_check = ttk.Checkbutton(
                reconnect_row,
                text="自动过测谎",
                variable=self._api_auto_lie_var,
                command=self._api_auto_lie_on_change,
            )
            self._api_auto_lie_check.pack(side="left")
            self._reconnect_var = tk.BooleanVar(value=saved_reconnect_enabled)
            self._reconnect_check = ttk.Checkbutton(
                reconnect_row,
                text="自动重连",
                variable=self._reconnect_var,
                command=self._reconnect_on_change,
            )
            self._reconnect_check.pack(side="left")
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
            # Only an integer 1-60 may be typed (validated again on every change).
            channel_check = self._register_validator(
                reconnect_row, self._validate_reconnect_channel
            )
            self._reconnect_channel_var = tk.StringVar(value=str(saved_channel))
            self._reconnect_channel_box = ttk.Spinbox(
                reconnect_row,
                from_=CHANNEL_MIN,
                to=CHANNEL_MAX,
                width=4,
                textvariable=self._reconnect_channel_var,
                validate="key",
                validatecommand=channel_check,
                command=self._reconnect_on_change,
            )
            self._reconnect_channel_box.pack(side="left", padx=(4, 0))
            self._reconnect_channel_box.bind(
                "<FocusOut>", lambda _event: self._reconnect_on_change()
            )
            self._reconnect_message_var = tk.StringVar(value="")
            self._reconnect_message_button = ttk.Button(
                reconnect_row, text="重连消息",
                command=lambda: self._edit_workflow_message(
                    "reconnect", "重连消息", self._reconnect_message_var
                ),
                takefocus=False,
            )
            self._reconnect_message_button.pack(side="left", padx=(6, 0))
            # The API drill is intentionally exposed again while the RTF1
            # binary transport is being field-tested.  It opens the existing
            # video picker and uses the same connection/handshake path as an
            # automatic lie pass, but confines cursor motion to the video.
            self._api_test_button = ttk.Button(
                reconnect_row,
                text="测试API",
                command=self._api_test_clicked,
                takefocus=False,
            )
            self._api_test_button.pack(side="left", padx=(6, 0))
            restart_row = ttk.Frame(extra_panel)
            restart_row.pack(fill="x", pady=(4, 0))
            self._auto_restart_var = tk.BooleanVar(value=False)
            self._auto_restart_check = ttk.Checkbutton(
                restart_row,
                text="自动重开",
                variable=self._auto_restart_var,
                command=self._shutdown_on_change,
            )
            self._auto_restart_check.pack(side="left")
            self._restart_offline_message_var = tk.StringVar(value="")
            self._restart_offline_message_button = ttk.Button(
                restart_row, text="重开消息",
                command=lambda: self._edit_workflow_message(
                    "restart_offline", "重开消息", self._restart_offline_message_var
                ),
                takefocus=False,
            )
            self._restart_offline_message_button.pack(side="left", padx=(6, 0))
            # Clicking a plain label/panel does not necessarily give Tk a
            # different focus owner, so Spinbox <FocusOut> alone never fires.
            # Watch root clicks as well and explicitly commit/blur this one
            # field when the click lands outside it.
            root.bind_all(
                "<Button-1>", self._reconnect_channel_commit_on_outside_click,
                add="+",
            )
            # Retain invisible status sinks for workflow callbacks.  The
            # Additional Functions panel intentionally shows controls only;
            # progress and failures are reported in the running log instead.
            self._reconnect_status = ttk.Label(
                extra_panel,
                text="",
                justify="left",
                wraplength=440,
            )
            # The temporary reconnect tester remains absent.  测试API above
            # is a real operator-facing drill for the RTF1 upload transport.
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
            ttk.Checkbutton(
                player_row,
                text="检测到其他玩家自动切换频道",
                variable=self._player_check_var,
                command=self._shutdown_on_change,
            ).pack(side="left")
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
            ttk.Label(player_row, text="换线等待").pack(side="left", padx=(6, 0))
            self._player_channel_wait_var = tk.StringVar(value="5")
            player_wait_check = self._register_validator(
                player_row, self._validate_player_channel_wait
            )
            self._player_channel_wait_box = ttk.Spinbox(
                player_row, from_=1, to=20, width=3,
                textvariable=self._player_channel_wait_var,
                validate="key", validatecommand=player_wait_check,
                command=self._shutdown_on_change,
            )
            self._player_channel_wait_box.pack(side="left", padx=(3, 0))
            self._player_channel_wait_box.bind(
                "<FocusOut>", lambda _event: self._shutdown_on_change()
            )
            ttk.Label(player_row, text="分钟").pack(side="left", padx=(2, 0))

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
            # shown (the latest few lines); the full 600-line in-memory
            # history stays available through the archive button - no disk
            # I/O per line.  Import/export have text buttons so their purpose
            # remains clear without relying on an unlabeled icon.
            log_panel = ttk.LabelFrame(col2, text="运行日志", padding=(6, 4))
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
            self._import_config_button = ttk.Button(
                log_actions, text="导入配置",
                command=self._import_user_config,
                takefocus=False,
            )
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
            self._copy_server_log_button.pack(side="left", padx=(0, 3))
            self._log_auto_lie_photo = _make_log_icon("auto_lie", root)
            self._copy_auto_lie_log_button = ttk.Button(
                log_actions,
                image=self._log_auto_lie_photo,
                command=self._copy_auto_lie_log,
                takefocus=False,
            )
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
                width = max(root.winfo_width(), required_width)
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

            self._start_license_heartbeat()
            root.after(0, self._poll)
            root.mainloop()
        except Exception:
            LOG.exception("debug UI stopped unexpectedly")
        finally:
            self._yolo_stop()
            self._root = None
            LOG.info("UI worker stopped")

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
            bar, text=f"TodoHelper {app_version}", bg="#f0f0f0",
            anchor="w",
        )
        title.pack(side="left", padx=(10, 0))
        self._caption_title = title

        def _button(text: str, command: Any) -> Any:
            button = tk.Button(
                bar, text=text, command=command, relief="flat",
                bd=0, bg="#f0f0f0", activebackground="#dcdcdc",
                width=3, cursor="hand2",
            )
            button.pack(side="right", fill="y")
            return button

        self._caption_close_button = _button("×", self._on_debug_window_close)
        self._caption_max_button = _button("□", self._caption_toggle_maximize)
        self._caption_min_button = _button("－", self._caption_minimize)
        self._help_button = _button("?", None)
        self._update_button = _button("↻", self._check_desktop_update)
        # Hover-triggered help: show on mouse-enter, disappear on mouse-leave
        # (the popup is floating and never takes keyboard focus).
        self._help_button.bind("<Enter>", self._help_hover_show)
        self._help_button.bind("<Leave>", self._help_hover_leave)

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
        self._stop_patrol()
        root = getattr(self, "_root", None)
        if root is not None:
            root.after(250, self._on_debug_window_close)

    def _check_desktop_update(self) -> None:
        """Find and apply the highest newer nearby/Desktop release on request."""

        install_root = application_root(__file__)
        current = read_version(install_root / "VERSION")
        LOG.info(
            "更新：正在桌面、当前目录及上级目录查找比 v%s 更新的 TodoHelper 安装包。",
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
        """Persist the reminder deadline and UI geometry, then destroy."""
        root = self._root
        if root is not None:
            self._save_countdown_resume_state()
            try:
                _save_window_geometry(self._geometry_for_save(root))
            except Exception:
                LOG.debug("could not save debug UI geometry on close",
                          exc_info=True)
            root.destroy()

    def _geometry_for_save(self, root: Any) -> str:
        """Geometry for persistence (whole window, caption included).

        The custom caption bar is part of the Tk client, so the raw
        winfo geometry already describes the whole window; no adjustment is
        needed.
        """

        return root.winfo_geometry()

    def _schedule_window_geometry_save(self, root: Any, delay_ms: int = 3000) -> None:
        """Periodically persist the debug UI geometry while it is open.

        A periodic save (rather than only on close) also keeps the position
        when the assistant is restarted hard (kill/restart scripts), so the
        window never snaps back to a fixed default corner.
        """

        def _tick() -> None:
            if self._root is not root:
                return
            try:
                _save_window_geometry(self._geometry_for_save(root))
            except Exception:
                LOG.debug("could not save debug UI geometry", exc_info=True)
            try:
                root.after(delay_ms, _tick)
            except Exception:
                pass

        try:
            root.after(delay_ms, _tick)
        except Exception:
            pass

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
        if self.stop_event.is_set():
            root.destroy()
            return
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
            try:
                if not self.stop_event.is_set() and root.winfo_exists():
                    root.after(self.refresh_ms, self._poll)
            except Exception:
                LOG.debug("could not re-arm the debug UI poll", exc_info=True)

    def _poll_body(self, root: Any) -> None:
        """One tick's work, without the re-arm (see :meth:`_poll`)."""

        # Online entitlement is a state gate, not visual work.  A resize burst
        # may defer expensive frame rendering and log insertion, but it must
        # never defer the heartbeat result: otherwise a successful immediate
        # validation can leave the visible header stuck at “正在验证授权” until
        # some unrelated UI change ends the freeze state.
        self._drain_license_heartbeat_results()
        self._drain_lie_accounting_results()
        self._drain_channel_update_events()
        self._drain_other_player_stop_events()

        # While a move/resize modal loop is running the client is frozen
        # (repaint suppressed); skip the heavy snapshot render + log insert
        # so the resize stays light, but keep the poll cadence alive.
        frozen = bool(
            getattr(self, "_resize_freeze_state", {}).get("active", False)
        )
        if frozen:
            return
        latest = None
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
        if latest is not None:
            try:
                self.last_snapshot = build_debug_snapshot(
                    latest,
                    self.detector,
                    self.configured_map_name,
                    self.diamond_size_tracker,
                    self.structure_tracker,
                )
                self._render(self.last_snapshot)
            except Exception:
                LOG.exception("could not update debug UI")
        # Each periodic task is guarded on its own.  The whole body used to
        # share ONE try/except, so an exception thrown by any single step (log
        # drain, status refresh, patrol sync, YOLO exit poll, a result drain)
        # skipped every step after it - including the hotkey dispatch - for the
        # rest of the session, while buttons, workers and the log kept working:
        # the operator's "the hotkey binding is lost" report.  A failing step
        # now costs only itself.
        for step in (
            self._drain_logs,
            self._refresh_automation_status,
            self._sync_patrol_ui_state,
            self._refresh_shutdown_status,
            self._refresh_countdown_status,
            self._refresh_telegram_status,
            self._poll_yolo_exit,
            self._drain_hotkey_actions,
            self._drain_api_test_results,
            self._service_api_auto_lie,
            self._drain_api_auto_lie_results,
        ):
            try:
                step()
            except Exception:
                LOG.exception("periodic UI step %s failed; the tick continues",
                              getattr(step, "__name__", step))

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
        if (self._patrol_stop_pending
                and self._patrol_stop_cleanup_done.is_set()):
            self._patrol_stop_pending = False
            self._patrol_stop_cleanup_done.clear()
            self._refresh_patrol_controls()
            try:
                self._control_status.configure(text="巡逻已停止。")
            except Exception:
                LOG.debug("could not display completed patrol stop", exc_info=True)
            LOG.info("STOP PATROL: background input cleanup completed")
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
        """Refresh and return the signed-license gate without raising."""

        if getattr(self, "_license_session_locked", False):
            return False
        local_status = verify_license()
        previous = getattr(self, "_license_status", None)
        # Local signature checks intentionally know nothing about the online
        # device record.  Preserve the heartbeat's equipment ID and usage
        # data when they refer to the same signed license, otherwise a normal
        # UI gate would erase the value immediately after it was received.
        if (
            local_status.valid
            and isinstance(previous, LicenseStatus)
            and previous.valid
            and previous.license_id == local_status.license_id
        ):
            local_status = replace(
                local_status,
                equipment_id=previous.equipment_id,
                auto_lie_allowed=previous.auto_lie_allowed,
                remaining_auto_lie_count=previous.remaining_auto_lie_count,
                lie_detect_total=previous.lie_detect_total,
                lie_detect_success_total=previous.lie_detect_success_total,
                lie_detect_failed_total=previous.lie_detect_failed_total,
            )
        self._license_status = local_status
        return bool(self._license_status.valid)

    def _start_license_heartbeat(self) -> None:
        """Start the immediate + three-hour online entitlement heartbeat."""

        if getattr(self, "_license_heartbeat_started", False):
            return
        self._license_heartbeat_started = True

        def heartbeat_loop() -> None:
            while not self.stop_event.is_set():
                generation = self._license_heartbeat_generation
                status = validate_via_server()
                try:
                    self._license_heartbeat_results.put_nowait((generation, status))
                except Exception:
                    LOG.warning("license heartbeat result could not be queued", exc_info=True)
                # The first iteration intentionally happens immediately.
                # Subsequent online checks are every three hours, as agreed.
                if self.stop_event.wait(3 * 60 * 60):
                    return

        threading.Thread(
            target=heartbeat_loop,
            name="license-heartbeat",
            daemon=True,
        ).start()

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
        if generation != self._license_heartbeat_generation:
            return
        self._license_status = status
        self._license_online_validation_pending = False
        if status.valid:
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
            return
        LOG.warning("license heartbeat failed: %s", status.code)
        self._lock_licensed_functions()
        if hasattr(self, "_control_status"):
            hint = (status.message if status.code in {"server", "device_not_ready"}
                    else f"在线授权验证失败：{status.message}")
            self._control_status.configure(
                text=hint
            )

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
            device_text = (
                f"设备：{status.equipment_id}（左键复制）"
                if status.equipment_id else "设备：等待服务器返回"
            )
            if getattr(self, "_license_online_validation_pending", False):
                expiry = "永久" if status.expires_at is None else self._format_license_expiry(
                    status.expires_at
                )
                label.configure(
                    text=(
                        f"正在验证在线授权 · {device_text} · 到期：{expiry} · {memory}"
                    ),
                    foreground="#9a6700",
                )
            elif status.valid:
                expiry = "永久" if status.expires_at is None else self._format_license_expiry(
                    status.expires_at
                )
                auto_lie = ""
                if not status.auto_lie_allowed:
                    auto_lie = " · 自动测谎已停用"
                # 测谎 statistics follow the always-visible authorization hint:
                # the server counts every accounted lie event per device and
                # already returns the three totals in the activation, heartbeat
                # and lie-accounting response (licensing._with_server_device),
                # so the operator reads them where the account state is shown
                # instead of in a panel of its own.  Wording matches the
                # operator console's 测谎：总 / 成功 / 失败 column.
                lie_stats = (
                    f" · 测谎：总 {status.lie_detect_total}"
                    f" / 成功 {status.lie_detect_success_total}"
                    f" / 失败 {status.lie_detect_failed_total}"
                )
                lie_quota = (
                    f" · 剩余 {status.remaining_auto_lie_count}"
                    if status.remaining_auto_lie_count is not None else ""
                )
                label.configure(
                    text=(f"验证成功 · {device_text} · 到期：{expiry} · "
                          f"{memory}{lie_stats}{lie_quota}{auto_lie}"),
                    foreground="#17803d",
                )
            else:
                if status.code in {"server", "device_not_ready"}:
                    text = f"{status.message} · {device_text} · {memory}"
                else:
                    text = f"未授权 / 已过期：{status.message} · {device_text} · {memory}"
                label.configure(text=text, foreground="#202020")
        button = getattr(self, "_license_button", None)
        if button is not None:
            button.configure(text="更换授权" if status.valid else "激活授权")

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
            if hasattr(self, "_control_status"):
                self._control_status.configure(text="设备码已复制。")
            LOG.info("equipment ID copied to clipboard")
        except Exception:
            LOG.debug("equipment ID copy failed", exc_info=True)

    def _apply_auto_lie_entitlement(self, status: LicenseStatus) -> None:
        """Disable future auto-lie passes after a server accounting refusal.

        A live pass is deliberately left alone; accounting happens after a
        completed event, so only subsequent windows are disarmed.
        """

        if bool(getattr(status, "auto_lie_allowed", True)):
            return
        if hasattr(self, "_api_auto_lie_var"):
            self._api_auto_lie_var.set(False)
        self._api_auto_lie_session_armed = False
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
            return local.strftime("%Y-%m-%d %H-%M (GMT+8)")
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
        """Return the user-owned activation-attempt settings section."""

        return config_section_file("license")

    def _save_license_attempt(
        self, activation_code: str, status: Optional[LicenseStatus] = None
    ) -> None:
        """Persist the submitted code regardless of whether validation succeeds.

        ``license.json`` is the signed authorization document.  This
        user-config entry records the last code the operator chose to submit
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
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            LOG.warning("could not save activation attempt to user_config", exc_info=True)

    def _lock_licensed_functions(self) -> None:
        """Immediately stop and lock every licensed automation surface."""

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
                pickup_setter = getattr(mover, "set_stationary_pickup_schedule", None)
                if callable(pickup_setter):
                    pickup_setter(False, 0.0, 0.0)
            except Exception:
                LOG.debug("license lock could disable stationary actions", exc_info=True)
        for variable_name in (
            "_stationary_jump_enabled_var", "_stationary_pickup_enabled_var",
            "_small_step_enabled_var",
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
        # Supersede an initial/previous heartbeat that may still be awaiting a
        # response.  The explicit activation result is the new session state.
        self._license_heartbeat_generation += 1
        self._license_online_validation_pending = False
        # Save the new code before contacting a local or online validator.  A
        # rejected value must not silently leave the previous submitted code
        # in user_config.json.
        self._save_license_attempt(code)
        if str(code).strip().upper().startswith("MAL-"):
            self._license_status = activate_via_server(code)
        else:
            self._license_status = activate_license(code)
        self._save_license_attempt(code, self._license_status)
        if self._license_status.valid:
            self._license_session_locked = False
            self._refresh_license_ui()
            LOG.info("license activated id=%s", self._license_status.license_id)
            self._set_license_visual_lock(False)
            self._shutdown_load_settings()
            self._apply_auto_lie_entitlement(self._license_status)
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

        Never raises: the queue is drained item by item, and the result reports
        that run before it are guarded too, so a malformed item or a failing
        result drain cannot leave a queued chord unexecuted.
        """

        for drain in (self._drain_quick_pickup_results,
                      self._drain_reconnect_results):
            try:
                drain()
            except Exception:
                LOG.exception("periodic result drain failed; hotkeys continue")
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
            if not self._license_allowed():
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
                elif action == "quick_pickup:toggle":
                    root = getattr(self, "_root", None)
                    if (getattr(self, "_native_dialog_open", False)
                            or (root is not None and root.grab_current() is not None)):
                        # A dialog of this app is open in front (for example the
                        # 测试测谎 video picker).  Ctrl+Z there is the dialog's own
                        # key, not a request to select the game window and start
                        # tapping Z.
                        self._control_status.configure(
                            text="快速拾取：对话框打开时忽略（请先关闭对话窗口）。"
                        )
                        continue
                    sender = getattr(self.quick_pickup_worker, "key_sender", None)
                    probe = getattr(sender, "game_window_present", None)
                    if callable(probe):
                        try:
                            present = bool(probe())
                        except Exception:
                            present = True
                        if not present:
                            # The operator is not in the game (testing a 测试测谎
                            # video, for example): there is nothing to pick up, and
                            # hunting for the game window here only produced an
                            # error in the log.  Say so once, move no window, send
                            # no key.
                            LOG.info(
                                "quick pickup ignored: no game window on screen "
                                "(testing a video?)"
                            )
                            self._control_status.configure(
                                text="快速拾取：没有找到游戏窗口（正在测试视频？）。"
                            )
                            self._play_action_sound(False)
                            continue
                    patrol_running = bool(
                        self.patrol_controller is not None
                        and self.patrol_controller.is_enabled()
                    )
                    worker = self.quick_pickup_worker
                    if patrol_running or worker is None or not worker.request_toggle():
                        self._control_status.configure(
                            text="快速拾取失败：请先停止巡逻。"
                        )
                        self._play_action_sound(False)
                    else:
                        self._control_status.configure(text="快速拾取：正在切换…")
                elif action.startswith("record:"):
                    boundary = action.partition(":")[2]
                    self._play_action_sound(self._record_endpoint(boundary))
                elif action.startswith("record_jump_point:"):
                    direction = action.partition(":")[2]
                    self._play_action_sound(self._record_jump_point(direction))
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
                        if (self.patrol_controller is not None
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

    def _drain_quick_pickup_results(self) -> None:
        """Apply manual-pickup worker results on Tk's owning thread."""

        results = self.quick_pickup_results
        if results is None:
            return
        while True:
            try:
                state, detail = results.get_nowait()
            except queue.Empty:
                return
            try:
                if state == "started":
                    self._control_status.configure(
                        text="快速拾取已开启；再次按 Ctrl+Z 关闭。"
                    )
                    self._play_action_sound(True)
                elif state == "failed":
                    suffix = f"：{detail}" if detail else "。"
                    self._control_status.configure(text=f"快速拾取失败{suffix}")
                    self._play_action_sound(False)
                elif state == "stopped":
                    suffix = f"（{detail}）" if detail else ""
                    self._control_status.configure(text=f"快速拾取已关闭{suffix}。")
            finally:
                try:
                    results.task_done()
                except (AttributeError, ValueError):
                    pass

    def _validate_reconnect_channel(self, proposed: str) -> bool:
        """Tk validator: the 自动重连 channel box accepts only 1-60 (or an empty edit)."""

        text = str(proposed).strip()
        if text == "":
            return True                      # mid-edit: the value is checked on change
        if not text.isdigit():
            return False
        return CHANNEL_MIN <= int(text) <= CHANNEL_MAX

    @staticmethod
    def _validate_player_channel_wait(proposed: str) -> bool:
        """Tk validator for the 1–20 minute other-player switch delay."""

        text = str(proposed).strip()
        return text == "" or (text.isdigit() and 1 <= int(text) <= 20)

    def _reconnect_channel_commit_on_outside_click(self, event: Any) -> None:
        """Commit the channel when a click leaves its Spinbox without focus.

        Labels, panels, and canvas-like areas generally do not claim keyboard
        focus in Tk.  Without this explicit blur, clicking those areas left a
        partially edited channel in the Spinbox and never wrote it to the
        user configuration.
        """

        box = getattr(self, "_reconnect_channel_box", None)
        root = getattr(self, "_root", None)
        if box is None or root is None:
            return
        try:
            if event.widget is box or root.focus_get() is not box:
                return
            # The click is outside the field.  Release the text selection and
            # move focus to the toplevel so the native <FocusOut> semantics
            # are also preserved for other Tk bindings.
            box.selection_clear()
            root.focus_set()
            self._reconnect_on_change()
        except Exception:
            LOG.debug("auto reconnect channel outside-click commit failed", exc_info=True)

    def _reconnect_on_change(self) -> None:
        """Apply the 自动重连 selection (enable + world + channel) to its worker."""

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
            if hasattr(self, "_reconnect_channel_var"):
                self._reconnect_channel_var.set(str(channel))
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
        """Which backend 测试api will use, with the key's *source* (never the key itself).

        The key is never stored in the package.  It arrives only in a validated
        heartbeat response and remains in process memory until app exit.
        """

        if not has_server_secret():
            return "等待在线授权下发测谎密钥"
        return "真实后端（密钥来源：授权服务器，仅运行内存）"

    def _api_test_status_text(self) -> str:
        video = getattr(self, "api_test_video", "")
        chosen = f"上次视频：{Path(video).name}；" if video else ""
        return (f"测试api: {self._api_test_backend_text()}；{chosen}"
                f"按 测试api 选择视频，{API_TEST_VIDEO_SECONDS:.0f} 秒 @ 5 fps，"
                "开始上传前先等第二个窗口（鼠标只在视频画面内）。")

    def _api_test_clicked(self) -> None:
        """测试api: pick a video file and run the drill on it (5 fps, mouse inside the picture)."""

        factory = getattr(self, "api_test_video_factory", None)
        if not callable(factory):
            self._set_api_test_status("测试api: 本机助手未启用视频测试工作线程。")
            return
        if self._api_test_busy():
            return
        video = self._ask_api_test_video()
        if video is None:
            return
        seconds = self._api_test_video_seconds()
        self._save_api_test_settings()
        window_factory = getattr(self, "api_test_window_factory", None)
        if window_factory is None:
            try:
                from api_lie_video import VideoDrillWindow as window_factory
            except Exception:
                LOG.exception("api test: the video window could not be imported")
                self._set_api_test_status("测试api: 无法载入视频窗口（详见运行日志）。")
                return
        try:
            window = window_factory(
                self._root, f"测试api — {video.name}",
                on_close=self._api_test_window_closed,
                on_toggle_mouse=self._api_test_toggle_mouse,
                on_toggle_pause=self._api_test_toggle_pause,
            )
        except Exception:
            LOG.exception("api test: the video window could not be created")
            self._set_api_test_status("测试api: 无法创建视频窗口（详见运行日志）。")
            return
        display: "queue.Queue[Any]" = queue.Queue(maxsize=2)
        key = get_server_secret()
        if not key:
            try:
                window.close()
            except Exception:
                pass
            self._set_api_test_status("测试api: 等待在线授权下发测谎密钥。")
            return
        try:
            worker = factory(video=video, results=self.api_test_results, display=display,
                             seconds=seconds, key=key)
        except Exception:
            LOG.exception("api test: the video worker could not be created")
            try:
                window.close()
            except Exception:
                LOG.debug("video window close failed", exc_info=True)
            self._set_api_test_status("测试api: 工作线程创建失败（详见运行日志）。")
            return
        self.api_test_worker = worker
        self.api_test_window = window
        self.api_test_video = str(video)
        self._api_test_display = display
        worker.image_rect = window.image_region()
        self._api_test_set_buttons("disabled")
        LOG.info("api test (video): %s for %.0fs at 5 fps (key=%s)", video.name, seconds,
                 "set" if key else "none -> mimic")
        worker.start()
        self._set_api_test_status(
            f"测试api: 正在播放 {video.name} - {seconds:.0f} 秒 @ 5 fps"
            f"（{self._api_test_backend_text()}，鼠标只在视频画面内；Esc 或关闭窗口即停止）……")
        self._api_test_keep_focus()

    def _api_test_busy(self) -> bool:
        """True when a drill is already running (the button says so instead of starting another)."""

        worker = getattr(self, "api_test_worker", None)
        if worker is not None and getattr(worker, "running", False):
            self._set_api_test_status("测试api: 已有一次测试在运行，请等它结束。")
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
                title="选择要测试的视频（测试api 会按 5 fps 播放并上传）",
                initialdir=start,
                filetypes=[("视频", "*.mp4 *.avi *.mkv *.mov *.wmv *.flv *.m4v"),
                           ("所有文件", "*.*")],
            )
        except Exception as exc:
            LOG.exception("api test: the video picker failed")
            self._set_api_test_status(f"测试api: 无法打开视频选择窗口 - {exc}")
            return None
        text = str(chosen or "").strip()
        if not text:
            self._set_api_test_status("测试api: 已取消（没有选择视频）。")
            return None
        video = Path(text)
        if not video.is_file():
            self._set_api_test_status(f"测试api: 找不到视频文件 {video.name}。")
            return None
        self._api_test_video_dir = str(video.parent)
        return video

    def _api_test_set_buttons(self, state: str) -> None:
        button = getattr(self, "_api_test_button", None)
        if button is None:
            return
        try:
            button.configure(state=state)
        except Exception:
            LOG.debug("api test button state could not be set", exc_info=True)

    def _api_test_window_closed(self) -> None:
        """The operator closed the video window: stop the drill."""

        worker = getattr(self, "api_test_worker", None)
        if worker is not None:
            try:
                worker.request_stop()
            except Exception:
                LOG.debug("video drill stop request failed", exc_info=True)
            self._set_api_test_status("测试api: 视频窗口已关闭 - 正在停止……")
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
            f"测试api: 鼠标跟随已{'开启' if enabled else '关闭'}"
            f"（{'只在视频画面内移动' if enabled else '不再移动鼠标'}）。")

    def _api_test_toggle_pause(self, paused: bool) -> None:
        worker = getattr(self, "api_test_worker", None)
        if worker is None or not hasattr(worker, "set_paused"):
            return
        try:
            worker.set_paused(bool(paused))
        except Exception:
            LOG.debug("video drill pause toggle failed", exc_info=True)
        self._set_api_test_status(f"测试api: 已{'暂停' if paused else '继续'}。")

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
        if hasattr(self, "_api_test_status"):
            try:
                self._api_test_status.configure(text=text)
            except Exception:
                LOG.debug("api test status could not be updated", exc_info=True)

    def _drain_api_test_results(self) -> None:
        """Show the 测试api drill's progress on Tk's owning thread."""

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
                if state == "frame":
                    self._set_api_test_status(f"测试api 帧 {detail}")
                elif state == "backend":
                    self._set_api_test_status(f"测试api 后端: {detail}")
                elif state == "capture":
                    self._set_api_test_status(f"测试api 已保存标注帧: {detail}")
                elif state == "done":
                    self._set_api_test_status(f"测试api 完成: {detail}")
                    self._play_action_sound(True)
                    # A completed manual API drill uses the same upstream
                    # service as an in-game pass.  Count it immediately so
                    # the authorization bar reflects the server response.
                    self._enqueue_auto_lie_accounting("api-test")
                    self._api_test_idle()
                elif state == "stopped":
                    self._set_api_test_status(f"测试api 已停止: {detail}")
                    self._api_test_idle()
                elif state == "failed":
                    self._set_api_test_status(f"测试api 失败: {detail}")
                    self._play_action_sound(False)
                    self._api_test_idle()
                else:
                    suffix = f": {detail}" if detail else ""
                    self._set_api_test_status(f"测试api - {state}{suffix}")
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

    def _api_auto_lie_on_change(self) -> None:
        """Arm or disarm the automatic api pass, and persist it immediately."""

        if not self._license_allowed():
            if hasattr(self, "_api_auto_lie_var"):
                self._api_auto_lie_var.set(False)
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
            if not armed:
                self._api_test_status.configure(text="自动过测谎: 未启用。")
            elif not self._lie_detection_armed():
                # 自动过测谎 is driven by the lie detector: without 测谎 no lie window can be seen, so the
                # pass would never start.  Say it here instead of leaving the operator with silence.
                self._api_test_status.configure(
                    text="自动过测谎: 已启用，但「测谎」未勾选 - 检测不到测谎窗口，不会自动过测谎。"
                )
            else:
                self._api_test_status.configure(
                    text="自动过测谎: 已启用 - 未开始巡逻时也会自动抓取窗口检测并接管。"
                )
        if armed and not self._lie_detection_armed():
            AUTO_LIE_LOG.warning(
                "自动过测谎 is armed but 测谎 (lie detection) is OFF: no lie window can be detected, so "
                "the automatic pass can never start - tick 测谎 as well"
            )
        AUTO_LIE_LOG.info("自动过测谎 %s", "armed" if armed else "disarmed")

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

        try:
            self._api_auto_lie_events.put_nowait(match)
        except queue.Full:
            AUTO_LIE_LOG.warning("自动过测谎: event queue is full; newest detector event was dropped")

    def _handle_lie_event_for_api(self, match: object = None) -> None:
        """Handle one queued lie event on the UI thread.

        This owns all Tk variable/widget access.  The detector must only
        enqueue an event, otherwise a cross-thread BooleanVar read can make a
        legitimate lie window fail before the API worker is created.
        """

        if not self._license_allowed():
            AUTO_LIE_LOG.info("自动过测谎: ignored because license is not valid")
            return
        if not has_server_secret():
            AUTO_LIE_LOG.warning("自动过测谎: server credential is unavailable; pass was not started")
            if hasattr(self, "_api_test_status"):
                self._api_test_status.configure(text="自动过测谎: 等待在线授权下发测谎密钥。")
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
        now = time.monotonic()
        if match is None:
            # The square is gone: the window is over and the next bbox is a new event.
            self._api_auto_lie_clear_since = now
            self._api_auto_lie_event_active = False
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
        self._api_auto_lie_pending_since = now
        self._api_auto_lie_wait_logged = False
        AUTO_LIE_LOG.warning(
            "自动过测谎: a new lie window armed a pass (bbox %s, previous pass %.0fs ago)",
            match, since_last,
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
                    match = events.get_nowait()
                except queue.Empty:
                    break
                self._handle_lie_event_for_api(match)
                try:
                    events.task_done()
                except ValueError:
                    pass

        # A pass that WE started for a lie window pauses the patrol (the lie test freezes the
        # character, so the movement worker otherwise counts it as stuck and fires its self-rescue
        # mid-test - the operator's v1.0.26 log: "SELF-RESCUE: character stationary on patrol route
        # for 20 frames" twice inside one lie window, restarting the patrol and walking the character
        # while the api pass was supposed to have the machine).  Resume it as soon as the pass is done.
        if getattr(self, "_api_auto_lie_patrol_paused", False) \
                and not self._worker_is_running(self._api_lie_pass_worker):
            if not self._api_auto_lie_post_confirm_scheduled:
                self._begin_auto_lie_post_confirm()
            if self._api_auto_lie_post_confirm_complete:
                self._resume_patrol_after_api_pass()
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
        self._api_auto_lie_pending = False
        try:
            worker = factory()
        except Exception as exc:
            AUTO_LIE_LOG.warning("自动过测谎 could not start", exc_info=True)
            if hasattr(self, "_api_test_status"):
                self._api_test_status.configure(text=f"自动过测谎: 启动失败（{exc}）")
            return
        self._pause_patrol_for_api_pass()
        self._api_lie_pass_worker = worker
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
        AUTO_LIE_LOG.info("自动过测谎: pass %d started for a lie window", self._api_auto_lie_runs)
        if hasattr(self, "_api_test_status"):
            self._api_test_status.configure(
                text=f"自动过测谎: 测谎窗口出现，正在处理（第 {self._api_auto_lie_runs} 次）…"
            )

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
        controller = getattr(self, "patrol_controller", None)
        if controller is not None and controller.is_enabled():
            try:
                controller.set_enabled(False)
                self._api_auto_lie_patrol_paused = True
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
            client_x, client_y = _auto_lie_confirm_client_point(width, height)
            origin_x, origin_y = win32gui.ClientToScreen(hwnd, (0, 0))
            screen_x = int(origin_x) + client_x
            screen_y = int(origin_y) + client_y
            clicked = click_screen(screen_x, screen_y, keep_focus=hwnd)
            if clicked:
                AUTO_LIE_LOG.info(
                    "自动过测谎: clicked confirmation at client=(%d,%d) "
                    "screen=(%d,%d) client_size=%dx%d",
                    client_x, client_y, screen_x, screen_y, width, height,
                )
            return bool(clicked)
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
        latest = None
        while True:
            try:
                latest = results.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break
        if not (isinstance(latest, tuple) and len(latest) == 2):
            if latest is not None:
                AUTO_LIE_LOG.warning("自动过测谎 result ignored: unexpected item %r", latest)
            return
        state, detail = latest
        text = f"{state}: {detail}" if detail else str(state)
        # Keep the independent auto_lie.log useful after the handoff too:
        # progress/results originate in the worker queue, rather than all
        # being emitted by the worker's general-purpose logger.
        AUTO_LIE_LOG.info("自动过测谎: worker result %s", text)
        if hasattr(self, "_api_test_status"):
            self._api_test_status.configure(text=f"自动过测谎: {text}")
        # One automatic pass produces exactly one durable accounting event.
        if state not in {"done", "failed"}:
            return
        worker_id = id(worker)
        if self._accounted_auto_lie_worker_id == worker_id:
            return
        self._accounted_auto_lie_worker_id = worker_id
        self._enqueue_auto_lie_accounting("automatic")

    def _enqueue_auto_lie_accounting(self, source: str) -> None:
        """Durably report one completed API use as success without delay.

        Both the automatic game pass and the manual video drill use this
        isolated post-pass path.  It never blocks their live WebSocket work.
        """

        try:
            event_id = self._lie_accounting_worker.enqueue("success")
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
                    # 自动重开 belongs to the game/layer lifecycle, not to
                    # the Additional Functions control area.  Keep its live
                    # progress beside the patrol calibration status.
                    if hasattr(self, "_control_status"):
                        self._control_status.configure(text=f"自动重开：{detail}")
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
        if hasattr(self, "_reconnect_channel_var"):
            self._reconnect_channel_var.set(str(channel))
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
            "其他玩家自动换线 stopped (%s); patrol and the automation were cleared and "
            "the 检测到其他玩家自动切换频道 field is now %s",
            reason,
            "cleared" if was_selected else "already off",
        )
        if hasattr(self, "_control_status"):
            try:
                self._control_status.configure(
                    text=f"检测到其他玩家自动换线 已停止（{reason}），巡逻已停止，开关已取消"
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
        self._automation_status_label.configure(text=text)

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
        # 600-line in-memory history stays available through the copy action.
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

    def _copy_running_log(self) -> None:
        """Archive button: copy the in-memory running log to the clipboard."""

        text = ""
        if self.ui_log_handler is not None:
            text = self.ui_log_handler.history_text()
        if not text.strip():
            text = "(暂无运行日志)"
        self._copy_to_clipboard(text)
        LOG.info("运行日志已复制到剪贴板（%d 行）", text.count("\n") + 1)

    def _copy_server_client_log(self) -> None:
        """Linked-nodes button: copy the safe server-client interaction log."""

        path = Path(__file__).resolve().parent / "server_client.log"
        try:
            raw = path.read_bytes()
        except OSError:
            text = ""
        else:
            text = raw.decode("utf-8", errors="replace")
        if not text.strip():
            text = "(server_client.log 为空或不存在)"
        self._copy_to_clipboard(text)
        LOG.info("server_client.log 已复制到剪贴板（%d 行）", text.count("\n") + 1)

    def _copy_auto_lie_log(self) -> None:
        """Copy the dedicated automatic lie-detector/API trace for pasting."""

        path = application_root(__file__) / "auto_lie.log"
        try:
            raw = path.read_bytes()
        except OSError:
            text = ""
        else:
            text = raw.decode("utf-8", errors="replace")
        if not text.strip():
            text = "(自动过测谎日志为空或不存在)"
        self._copy_to_clipboard(text)
        LOG.info("自动过测谎日志已复制到剪贴板（%d 行）", text.count("\n") + 1)

    def _import_user_config(self) -> None:
        """Choose a saved configuration, load it, then restart this instance.

        The imported file is only read at startup, and the running instance
        would otherwise write its own in-memory settings back over it while
        still patrolling the old route.  A successful import therefore stops
        and restarts the assistant through the same hidden helper used by the
        updater.
        """

        if not self.user_config_path:
            LOG.warning("%s失败：当前用户配置路径不可用", LOG_CONFIG_IMPORT)
            return
        try:
            from tkinter import filedialog

            source = filedialog.askopenfilename(
                parent=self._root,
                title="导入用户配置",
                initialfile="新配置.json",
                filetypes=(("用户配置", "新配置.json"), ("JSON 文件", "*.json")),
            )
        except Exception:
            LOG.exception("%s失败：无法打开文件选择窗口", LOG_CONFIG_IMPORT)
            return
        if not source:
            LOG.info("%s已取消", LOG_CONFIG_IMPORT)
            return
        try:
            import_user_config(Path(source), Path(self.user_config_path))
        except UpdateError as exc:
            LOG.warning("%s失败：%s", LOG_CONFIG_IMPORT, exc)
            return
        if not self._schedule_restart():
            LOG.warning("%s已载入，但自动重启未能安排；请手动重新启动。", LOG_CONFIG_IMPORT)
            return
        LOG.info("%s成功：已载入 %s；正在重启助手以应用新配置。", LOG_CONFIG_IMPORT, source)
        self._close_for_restart()

    def _export_user_config(self) -> None:
        """Export the live configuration to ``桌面\\助手配置\\新配置.json``."""

        if not self.user_config_path:
            LOG.warning("%s失败：当前用户配置路径不可用", LOG_CONFIG_EXPORT)
            return
        try:
            target = export_user_config(Path(self.user_config_path))
        except UpdateError as exc:
            LOG.warning("%s失败：%s", LOG_CONFIG_EXPORT, exc)
            return
        LOG.info("%s成功：已导出到桌面 %s", LOG_CONFIG_EXPORT, target)

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
        """Record a locked left- or right-moving jump trigger on this layer."""
        direction = str(direction).casefold()
        if direction not in ("left", "right"):
            LOG.warning("ignored jump-point record request with direction=%r", direction)
            return False
        label = "左跳" if direction == "left" else "右跳"
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
            recorded = self.patrol_controller.record_jump_point(
                snapshot.player_x, snapshot.player_y, direction=direction,
                layout=snapshot.coordinate_layout,
            )
        except (OSError, TypeError, ValueError) as exc:
            self._control_status.configure(text=f"无法录制{label}: {exc}")
            return False
        self._control_status.configure(
            text=f"已插入 {recorded.layer} {label}: x={recorded.x:.6f}, y={recorded.y:.6f}"
        )
        LOG.info("%s jump point locked: layer=%s x=%.6f y=%.6f",
                 direction, recorded.layer, recorded.x, recorded.y)
        self._refresh_patrol_controls()
        return True

    def _delete_recorded_axis_point(
        self, layer_name: str, point_kind: str, jump_index: Optional[int] = None
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
        try:
            if point_kind == "jump_point":
                point = self.patrol_controller.snapshot().layers.get(layer_name, {}).get(
                    "jump_points", []
                )
                direction = (
                    str(point[int(jump_index)].get("direction", "")).casefold()
                    if isinstance(point, list) and jump_index is not None
                    and 0 <= int(jump_index) < len(point) and isinstance(point[int(jump_index)], dict)
                    else ""
                )
                removed = self.patrol_controller.delete_jump_point(
                    layer_name, int(jump_index) if jump_index is not None else -1
                )
                label = "左跳" if direction == "left" else "右跳" if direction == "right" else "旧跳点"
            else:
                removed = self.patrol_controller.clear_endpoint(layer_name, point_kind)
                self._unlocked_points.discard((layer_name, point_kind))
                label = {
                    "left_most_pos": "最左",
                    "rope_pos": "绳索",
                    "right_most_pos": "最右",
                }.get(point_kind, "录制点")
        except (OSError, ValueError) as exc:
            LOG.warning("axis point delete failed", exc_info=True)
            self._control_status.configure(text=f"无法删除录制点: {exc}")
            return
        if removed:
            band_note = ""
            if point_kind != "jump_point":
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
            LOG.info("layer axis point deleted layer=%s kind=%s index=%s",
                     layer_name, point_kind, jump_index)
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
        )
        final_name = self.patrol_controller.final_layer_name()
        for label, kind, command in entries:
            already_recorded = (
                kind != "jump_point"
                and self.patrol_controller.endpoint(layer_name, kind) is not None
            )
            unavailable_rope = kind == "rope_pos" and layer_name == final_name
            state = "disabled" if already_recorded or unavailable_rope else "normal"
            menu.add_command(label=label, command=command, state=state)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    @staticmethod
    def _layer_axis_point_text(label: str, point: Any) -> str:
        return f"{label} ({float(point['x']):.4f}, {float(point['y']):.4f})"

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

    def _draw_layer_axis(self, layer_name: str, layer: Any) -> None:
        """Draw the layer axis with the same marker grammar as the blinker."""

        canvas = self._layer_axis_canvases.get(layer_name)
        if canvas is None:
            return
        canvas.delete("all")
        left, right, axis_y = _LAYER_AXIS_LEFT, _LAYER_AXIS_RIGHT, _LAYER_AXIS_Y
        canvas.create_line(left, axis_y, right, axis_y, fill="#6b6b6b", width=1)
        if not isinstance(layer, dict):
            return
        entries: list[tuple[str, Any, str, Optional[int]]] = []
        for kind, label in (
            ("left_most_pos", "最左"),
            ("rope_pos", "绳索"),
            ("right_most_pos", "最右"),
        ):
            point = layer.get(kind)
            if isinstance(point, dict) and "x" in point and "y" in point:
                entries.append((kind, point, label, None))
        jump_points = layer.get("jump_points", [])
        if isinstance(jump_points, list):
            for index, point in enumerate(jump_points):
                if isinstance(point, dict) and "x" in point and "y" in point:
                    direction = str(point.get("direction", "")).casefold()
                    label = "左跳" if direction == "left" else "右跳" if direction == "right" else "旧跳点"
                    entries.append(("jump_point", point, label, index))
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
        }
        for kind, point, label, index in entries:
            try:
                x_value = max(0.0, min(1.0, float(point["x"])))
            except (TypeError, ValueError, KeyError):
                continue
            x = left + round((right - left) * x_value)
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
            else:
                direction = str(point.get("direction", "")).casefold()
                if direction == "left":
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
                lambda _event, layer_name=layer_name, kind=kind, index=index:
                self._delete_recorded_axis_point(layer_name, kind, index),
            )
        # The temporary 站桩 anchor is not recording data and therefore has
        # no delete/click action.  When its current location successfully
        # matches a usable route layer, paint a quiet X on that layer's axis.
        mover = getattr(self, "movement_worker", None)
        marker_getter = getattr(mover, "stationary_route_anchor_marker", None)
        marker = marker_getter() if callable(marker_getter) else None
        if marker is not None and marker[0] == layer_name:
            try:
                x_value = max(0.0, min(1.0, float(marker[1].x)))
                x = left + round((right - left) * x_value)
                facing = str(marker[2] if len(marker) > 2 else "right").casefold()
                marker_text = {
                    "left": "<",
                    "right": ">",
                    "both": "<>",
                }.get(facing, ">")
                canvas.create_text(
                    x, axis_y - 10, text=marker_text,
                    fill="#cc2222", font=("TkDefaultFont", 9, "bold"),
                )
            except (AttributeError, TypeError, ValueError):
                LOG.debug("stationary route axis marker unavailable", exc_info=True)

    def _yolo_settings_path(self) -> Path:
        """JSON file holding the YOLO panel settings."""

        return config_section_file("yolo_detection")

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

    @staticmethod
    def _fixed_settings_path() -> Path:
        """JSON file holding the Fixed Attack panel settings."""

        return config_section_file("fixed_attack")

    def _fixed_collect_data(self) -> dict:
        """Current Fixed Attack panel values as a settings dict."""

        return {
            "attack_mode": str(self._attack_mode_var.get()),
            "interval_seconds": round(
                max(0.2, min(
                    self._FIXED_ATTACK_INTERVAL_MAX,
                    float(self._fixed_interval_var.get()),
                )), 1
            ),
            "random_gap_seconds": self._fixed_random_gap_seconds(),
            "attack_key": self._fixed_attack_key_var.get().strip(),
            "combo_attack_enabled": bool(
                self._combo_attack_enabled_var.get()
                if hasattr(self, "_combo_attack_enabled_var") else False
            ),
            "combo_attack_slots": self._combo_attack_slots_data(),
            "stationary_jump_enabled": bool(
                getattr(self, "_stationary_jump_enabled_var", None).get()
                if hasattr(self, "_stationary_jump_enabled_var") else False
            ),
            "small_step_enabled": bool(
                getattr(self, "_small_step_enabled_var", None).get()
                if hasattr(self, "_small_step_enabled_var") else False
            ),
            "small_step_interval_seconds": round(
                max(3.0, float(
                    getattr(self, "_small_step_interval_var", None).get()
                )) if hasattr(self, "_small_step_interval_var") else 5.0,
                1,
            ),
            "small_step_gap_seconds": self._small_step_gap_seconds(),
            "stationary_facing_direction": str(
                getattr(self, "_stationary_facing_direction_var", None).get()
                if hasattr(self, "_stationary_facing_direction_var") else "right"
            ),
            "stationary_pickup_enabled": bool(
                getattr(self, "_stationary_pickup_enabled_var", None).get()
                if hasattr(self, "_stationary_pickup_enabled_var") else False
            ),
            "stationary_pickup_interval_seconds": round(
                self._stationary_pickup_interval_minutes() * 60.0, 1
            ),
            # UI values for 捡东西 are minutes.  Keep the seconds value for
            # the worker/config compatibility, alongside the explicit unit.
            "stationary_pickup_gap_minutes": self._stationary_pickup_gap_minutes(),
            "stationary_pickup_gap_seconds": round(
                self._stationary_pickup_gap_minutes() * 60.0, 1
            ),
        }

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
            minimum = max(0, min(999, minimum))
            maximum = max(0, min(999, maximum))
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

    def _combo_attack_commit_on_outside_click(self, event: Any) -> None:
        """Blur, normalize, and save a typed combo count on outside clicks."""

        root = getattr(self, "_root", None)
        entries = tuple(getattr(self, "_combo_attack_count_entries", ()))
        if root is None or not entries:
            return
        try:
            focused = root.focus_get()
            # Clicking a different entry transfers focus normally.  Its own
            # FocusOut binding persists the previous value once.
            if focused not in entries or event.widget is focused:
                return
            focused.selection_clear()
            root.focus_set()
            self._fixed_on_change()
        except Exception:
            LOG.debug("combo attack outside-click commit failed", exc_info=True)

    def _fixed_random_gap_seconds(self) -> float:
        """Return the clamped, one-decimal random-gap setting."""

        var = getattr(self, "_fixed_random_gap_var", None)
        raw = 0.1 if var is None else float(var.get())
        return round(max(0.0, min(self._FIXED_RANDOM_GAP_MAX, raw)), 1)

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
            max(0.0, min(self._FIXED_RANDOM_GAP_MAX, float(base))), 1
        ))
        self._fixed_on_change()

    def _fixed_adjust_random_gap(self, delta: float) -> None:
        """Adjust the random delay ceiling.

        ``delta``'s sign picks the direction and its magnitude the step, so the
        fine button passes ±0.1 while the double click passes ±5.
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
        var = getattr(self, "_small_step_gap_var", None)
        raw = 0.1 if var is None else float(var.get())
        return round(max(0.0, min(self._FIXED_RANDOM_GAP_MAX, raw)), 1)

    def _set_small_step_gap(self, value: Optional[float]) -> None:
        """Apply an absolute small-step delay ceiling, clamped and rounded."""

        base = self._small_step_gap_seconds() if value is None else value
        self._small_step_gap_var.set(round(
            max(0.0, min(self._FIXED_RANDOM_GAP_MAX, float(base))), 1
        ))
        self._fixed_on_change()

    def _small_step_adjust_gap(self, delta: float) -> None:
        step = abs(delta) or self._FIXED_RANDOM_GAP_STEP
        self._set_small_step_gap(
            self._small_step_gap_seconds() + (step if delta > 0 else -step)
        )

    def _stationary_pickup_interval_minutes(self) -> float:
        """Return the 捡东西 trigger interval in MINUTES (this row's unit)."""

        var = getattr(self, "_stationary_pickup_interval_var", None)
        raw = (self._STATIONARY_PICKUP_INTERVAL_DEFAULT_MINUTES
               if var is None else float(var.get()))
        return round(min(
            self._STATIONARY_PICKUP_INTERVAL_MAX_MINUTES,
            max(self._STATIONARY_PICKUP_INTERVAL_MIN_MINUTES, raw),
        ), 3)

    @staticmethod
    def _format_stationary_pickup_interval(minutes: float) -> str:
        """Show the short pickup interval range precisely in seconds."""

        seconds = max(0.0, float(minutes) * 60.0)
        if seconds < 60.0:
            return f"{seconds:.0f}s"
        return f"{float(minutes):.1f}m"

    def _stationary_pickup_gap_minutes(self) -> float:
        """Return the 捡东西 random interval in MINUTES (this row's unit)."""

        var = getattr(self, "_stationary_pickup_gap_var", None)
        raw = 0.1 if var is None else float(var.get())
        return round(max(0.0, min(self._FIXED_RANDOM_GAP_MAX, raw)), 1)

    def _set_stationary_pickup_gap(self, value: Optional[float]) -> None:
        base = self._stationary_pickup_gap_minutes() if value is None else value
        self._stationary_pickup_gap_var.set(round(
            max(0.0, min(self._FIXED_RANDOM_GAP_MAX, float(base))), 1
        ))
        self._fixed_on_change()

    def _stationary_pickup_adjust_gap(self, delta: float) -> None:
        step = abs(delta) or self._FIXED_RANDOM_GAP_STEP
        self._set_stationary_pickup_gap(
            self._stationary_pickup_gap_minutes() + (step if delta > 0 else -step)
        )

    def _hotkey_adjust_fixed_interval(self, delta: float) -> bool:
        """Apply one held Ctrl+[ / Ctrl+] fixed-attack interval step."""

        if (not hasattr(self, "_fixed_interval_var")
                or self._root is None):
            return False
        current = float(self._fixed_interval_var.get())
        value = round(max(
            0.2, min(self._FIXED_ATTACK_INTERVAL_MAX, current + float(delta))
        ), 1)
        if value == round(current, 1):
            return False
        self._fixed_interval_var.set(value)
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
        stationary_jump_button = getattr(self, "_stationary_jump_button", None)
        # This method runs again after several ordinary UI refreshes.  It must
        # never reopen stationary controls that the authorization lock closed.
        stationary_enabled = mode == "stationary" and self._license_allowed()
        for widget in [
            stationary_jump_button,
            *getattr(self, "_stationary_facing_controls", []),
            *getattr(self, "_stationary_pickup_controls", []),
        ]:
            if widget is None:
                continue
            try:
                widget.state(["!disabled" if stationary_enabled else "disabled"])
            except Exception:
                widget.configure(state="normal" if stationary_enabled else "disabled")

    def _fixed_on_change(self, _value: str = "") -> None:
        """Update labels, persist, and apply the fixed-attack settings live."""

        if not hasattr(self, "_fixed_interval_label"):
            return
        mode = str(self._attack_mode_var.get())
        if (not self._YOLO_MONSTER_DETECTION_ENABLED
                and mode not in ("fixed", "stationary")):
            mode = "fixed"
            self._attack_mode_var.set("fixed")
        # Layout is intentionally not refreshed here.  Sliders and long-hold
        # random-gap buttons call this method repeatedly; packing/unpacking
        # their rows on every 0.1 s tick made the visible line flicker or
        # disappear.  Only an actual attack-mode change needs a layout pass.
        # ttk.Scale is pixel-continuous: on this compact 82px track, an exact
        # 3.0s position may have no physical pixel at all.  Quantize every
        # drag to the persisted 0.1s unit so whole values (3.0, 4.0, …) are
        # reachable and the displayed value is exactly what will be used.
        raw_interval = max(
            0.2, min(self._FIXED_ATTACK_INTERVAL_MAX,
                     float(self._fixed_interval_var.get()))
        )
        interval = round(raw_interval, 1)
        if abs(raw_interval - interval) > 1e-9:
            self._fixed_interval_var.set(interval)
        self._fixed_interval_label.configure(text=f"{interval:.1f}s")
        random_gap = self._fixed_random_gap_seconds()
        if hasattr(self, "_fixed_random_gap_label"):
            self._fixed_random_gap_label.configure(text=f"{random_gap:.1f}s")
        if hasattr(self, "_fixed_interval_range_label"):
            self._fixed_interval_range_label.configure(
                text=f"({interval:.1f}s, {interval + random_gap:.1f}s)"
            )
        if hasattr(self, "_small_step_interval_var"):
            # 小碎步 is a longer optional motion; allow up to 60 seconds.
            step_interval = min(
                self._OPTIONAL_MOTION_INTERVAL_MAX,
                max(3.0, float(self._small_step_interval_var.get())),
            )
            self._small_step_interval_var.set(step_interval)
            self._small_step_interval_label.configure(
                text=f"{step_interval:.1f}s"
            )
            step_gap = self._small_step_gap_seconds()
            if hasattr(self, "_small_step_gap_label"):
                self._small_step_gap_label.configure(
                    text=f"{step_gap:.1f}s"
                )
            if hasattr(self, "_small_step_interval_range_label"):
                self._small_step_interval_range_label.configure(
                    text=f"({step_interval:.1f}s, {step_interval + step_gap:.1f}s)"
                )
        if hasattr(self, "_stationary_pickup_interval_var"):
            pickup_interval = self._stationary_pickup_interval_minutes()
            self._stationary_pickup_interval_var.set(pickup_interval)
            self._stationary_pickup_interval_label.configure(
                text=self._format_stationary_pickup_interval(pickup_interval)
            )
            pickup_gap = self._stationary_pickup_gap_minutes()
            self._stationary_pickup_gap_label.configure(text=f"{pickup_gap:.1f}m")
            self._stationary_pickup_interval_range_label.configure(
                text=(
                    f"({self._format_stationary_pickup_interval(pickup_interval)}, "
                    f"{self._format_stationary_pickup_interval(pickup_interval + pickup_gap)})"
                )
            )
        if hasattr(self, "_fixed_key_button"):
            self._fixed_key_button.configure(
                text=self._fixed_attack_key_var.get()
            )
        for button, var in zip(
            getattr(self, "_combo_attack_key_buttons", ()),
            getattr(self, "_combo_attack_key_vars", ()),
        ):
            button.configure(text=var.get())
        combo_tooltip = getattr(self, "_combo_attack_help_tooltip", None)
        if combo_tooltip is not None:
            combo_tooltip.text = self._combo_attack_hint()
        data = self._fixed_collect_data()
        self._fixed_save_settings(data)
        self._fixed_apply_to_worker(data)
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
            return
        mode = str(data.get("attack_mode", "fixed"))
        if (not self._YOLO_MONSTER_DETECTION_ENABLED
                and mode not in ("fixed", "stationary")):
            mode = "fixed"
        worker.jump_attack = bool(
            mode == "stationary"
            and data.get("stationary_jump_enabled", False)
        )
        worker.enabled = bool(mode in ("fixed", "stationary"))
        worker.attack_interval = max(
            0.2, float(data.get("interval_seconds", 3.0))
        )
        worker.attack_jitter_seconds = max(
            0.0, float(data.get("random_gap_seconds", 0.0))
        )
        key = str(data.get("attack_key", "ctrl")).strip()
        if not worker.set_key(key):
            LOG.warning("fixed attack key %r unsupported; keeping %r",
                        key, worker.attack_key)
        worker.set_combo_attack(
            bool(data.get("combo_attack_enabled", False)),
            data.get("combo_attack_slots", ()),
        )
        mover = getattr(self, "movement_worker", None)
        if mover is not None:
            mover.small_step_attack_key = worker.attack_key
            stationary_setter = getattr(
                mover, "set_stationary_attack_enabled", None
            )
            if callable(stationary_setter):
                stationary_setter(mode == "stationary")
            # Automatic stuck stair-jumps are intentionally disabled.  The
            # dedicated jump executor remains solely for recorded jump points.
            mover.stair_jump_enabled = False
        step_worker = getattr(self, "small_step_worker", None)
        if step_worker is not None:
            # 小碎步 is available for fixed and stand-still attack. The latter
            # uses it chiefly to finish facing the monster side.
            step_worker.enabled = bool(
                data.get("small_step_enabled", False)
            ) and mode in ("fixed", "stationary")
            step_worker.step_interval = max(
                3.0,
                float(data.get("small_step_interval_seconds", 5.0)),
            )
            step_worker.step_jitter_seconds = max(
                0.0, float(data.get("small_step_gap_seconds", 0.0))
            )
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
                # The worker and the configuration keep seconds; the 捡东西 row
                # is the only place the interval is read as minutes.
                pickup_setter(
                    bool(data.get("stationary_pickup_enabled", False))
                    and mode == "stationary",
                    float(data.get("stationary_pickup_interval_seconds", 900.0)),
                    float(data.get("stationary_pickup_gap_seconds", 0.0)),
                )

    def _fixed_refresh_grey(self) -> None:
        """Grey the YOLO panel + update status lines for the active mode."""

        mode = str(self._attack_mode_var.get())
        fixed_mode = (
            not self._YOLO_MONSTER_DETECTION_ENABLED
            or mode in ("fixed", "stationary")
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
                interval = float(self._fixed_interval_var.get())
                if mode == "stationary":
                    self._fixed_status.configure(
                        text=(f"站桩攻击已启用 - 按键 "
                              f"{self._fixed_attack_key_var.get()}；"
                              f"开始运行时记录临时位置；"
                              f"只有正确配置楼层，才能寻路回去；"
                              f"{'先跳跃、0.3s 后攻击；' if self._stationary_jump_enabled_var.get() else ''}每 "
                              f"{interval:.1f}s。")
                    )
                else:
                    self._fixed_status.configure(
                        text=(f"巡逻攻击已启用 - 按键 "
                              f"{self._fixed_attack_key_var.get()}；每 "
                              f"{interval:.1f}s。")
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
                    if hasattr(self, "_stationary_jump_enabled_var"):
                        self._stationary_jump_enabled_var.set(True)
                elif mode in ("yolo", "fixed", "stationary"):
                    self._attack_mode_var.set(mode)
            if not self._YOLO_MONSTER_DETECTION_ENABLED:
                if self._attack_mode_var.get() not in ("fixed", "stationary"):
                    self._attack_mode_var.set("fixed")
            if "interval_seconds" in data:
                self._fixed_interval_var.set(
                    float(data["interval_seconds"])
                )
            if ("random_gap_seconds" in data
                    and hasattr(self, "_fixed_random_gap_var")):
                self._fixed_random_gap_var.set(
                    float(data["random_gap_seconds"])
                )
            if "attack_key" in data:
                key = str(data["attack_key"]).strip()
                if key in BINDABLE_KEYS:
                    self._fixed_attack_key_var.set(key)
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
                    minimum = max(0, min(999, minimum))
                    maximum = max(0, min(999, maximum))
                    if minimum > maximum:
                        minimum, maximum = maximum, minimum
                    if index < len(getattr(self, "_combo_attack_count_vars", ())):
                        self._combo_attack_count_vars[index][0].set(minimum)
                        self._combo_attack_count_vars[index][1].set(maximum)
                    raw_key = str(slot.get("key", "-")).strip().casefold()
                    key = raw_key if raw_key in BINDABLE_KEYS else "-"
                    if index < len(getattr(self, "_combo_attack_key_vars", ())):
                        self._combo_attack_key_vars[index].set(key)
            if ("stationary_jump_enabled" in data
                    and hasattr(self, "_stationary_jump_enabled_var")):
                self._stationary_jump_enabled_var.set(bool(
                    data["stationary_jump_enabled"]
                ))
            if ("small_step_enabled" in data
                    and hasattr(self, "_small_step_enabled_var")):
                self._small_step_enabled_var.set(bool(
                    data["small_step_enabled"]
                ))
            if ("small_step_interval_seconds" in data
                    and hasattr(self, "_small_step_interval_var")):
                self._small_step_interval_var.set(max(
                    3.0, float(data["small_step_interval_seconds"])
                ))
            if ("small_step_gap_seconds" in data
                    and hasattr(self, "_small_step_gap_var")):
                self._small_step_gap_var.set(float(
                    data["small_step_gap_seconds"]
                ))
            if ("stationary_pickup_enabled" in data
                    and hasattr(self, "_stationary_pickup_enabled_var")):
                self._stationary_pickup_enabled_var.set(bool(
                    data["stationary_pickup_enabled"]
                ))
            if ("stationary_pickup_interval_seconds" in data
                    and hasattr(self, "_stationary_pickup_interval_var")):
                # Stored in seconds; this row shows and clamps minutes.
                self._stationary_pickup_interval_var.set(min(
                    self._STATIONARY_PICKUP_INTERVAL_MAX_MINUTES,
                    max(self._STATIONARY_PICKUP_INTERVAL_MIN_MINUTES,
                        float(data["stationary_pickup_interval_seconds"]) / 60.0),
                ))
            if hasattr(self, "_stationary_pickup_gap_var"):
                if "stationary_pickup_gap_minutes" in data:
                    self._stationary_pickup_gap_var.set(float(
                        data["stationary_pickup_gap_minutes"]
                    ))
                elif "stationary_pickup_gap_seconds" in data:
                    # Older configs stored this particular UI value in
                    # seconds.  Convert once when loading them.
                    self._stationary_pickup_gap_var.set(
                        float(data["stationary_pickup_gap_seconds"]) / 60.0
                    )
            if hasattr(self, "_stationary_facing_direction_var"):
                direction = str(data.get("stationary_facing_direction", ""))
                if direction not in ("left", "right", "both"):
                    # v1.0.75 had an opt-in right-facing checkbox.  The
                    # selector replaces it and defaults to right.
                    direction = "right"
                self._stationary_facing_direction_var.set(direction)
        except (KeyError, TypeError, ValueError):
            LOG.warning("ignored malformed fixed attack settings",
                        exc_info=True)
            return
        self._fixed_on_change()
        LOG.info("fixed attack settings loaded from %s",
                 self._fixed_settings_path())

    @staticmethod
    def _drug_settings_path() -> Path:
        return config_section_file("drug")

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
        if hasattr(self, "_buff1_key_button"):
            self._buff1_key_button.configure(text=self._buff1_key_var.get())
        if hasattr(self, "_buff2_key_button"):
            self._buff2_key_button.configure(text=self._buff2_key_var.get())
        if hasattr(self, "_buff3_key_button"):
            self._buff3_key_button.configure(text=self._buff3_key_var.get())
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
            "buff_interval_unit": "seconds",
            "buff1_interval": int(round(float(self._buff1_interval_var.get()))),
            "buff2_interval": int(round(float(self._buff2_interval_var.get()))),
            "buff3_interval": int(round(float(self._buff3_interval_var.get()))),
            "buff1_enabled": bool(self._buff1_use_var.get()),
            "buff2_enabled": bool(self._buff2_use_var.get()),
            "buff3_enabled": bool(self._buff3_use_var.get()),
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
            # Older configuration files stored minutes.  The explicit unit
            # tag makes the one-time conversion unambiguous, including 5s.
            legacy_minutes = data.get("buff_interval_unit") != "seconds"
            for name, variable in (
                ("buff1_interval", self._buff1_interval_var),
                ("buff2_interval", self._buff2_interval_var),
                ("buff3_interval", self._buff3_interval_var),
            ):
                if name not in data:
                    continue
                interval = float(data[name])
                if legacy_minutes:
                    interval *= 60.0
                variable.set(min(600.0, max(5.0, interval)))
            if "buff1_enabled" in data:
                self._buff1_use_var.set(bool(data["buff1_enabled"]))
            if "buff2_enabled" in data:
                self._buff2_use_var.set(bool(data["buff2_enabled"]))
            if "buff3_enabled" in data:
                self._buff3_use_var.set(bool(data["buff3_enabled"]))
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

    @staticmethod
    def _shutdown_settings_path() -> Path:
        """JSON file holding the Additional Functions panel settings."""

        return config_section_file("additional_functions")

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
        if hasattr(self, "_countdown_enabled_var"):
            data["countdown_enabled"] = bool(
                self._countdown_enabled_var.get()
            )
            data["countdown_interval_hours"] = round(
                float(self._countdown_interval_var.get()), 1
            )
        if hasattr(self, "_api_auto_lie_var"):
            # 自动过测谎 is part of user_config, like the reconnect selection.
            data["auto_lie_api_enabled"] = bool(self._api_auto_lie_var.get())
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
            try:
                wait_minutes = int(self._player_channel_wait_var.get())
            except (TypeError, ValueError):
                wait_minutes = 5
            data["player_channel_wait_minutes"] = max(1, min(20, wait_minutes))
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
        self._shutdown_save_settings(data)
        self._shutdown_apply_to_worker(data)
        self._shutdown_refresh_grey()

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
                    wait_minutes=data.get("player_channel_wait_minutes", 5.0),
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

    def _machine_name_press(self, _event: Any = None) -> None:
        """Arm the 1s edit gesture; a short click intentionally does nothing."""

        self._machine_name_hold_fired = False
        if self._root is not None:
            self._machine_name_press_job = self._root.after(
                1000, self._machine_name_begin_edit
            )

    def _machine_name_release(self, _event: Any = None) -> None:
        if self._root is not None and self._machine_name_press_job is not None:
            try:
                self._root.after_cancel(self._machine_name_press_job)
            except Exception:
                pass
        self._machine_name_press_job = None
        self._machine_name_hold_fired = False

    def _machine_name_begin_edit(self) -> None:
        self._machine_name_press_job = None
        self._machine_name_hold_fired = True
        if self._machine_name_entry is not None:
            return
        button = getattr(self, "_telegram_machine_button", None)
        row = getattr(self, "_telegram_machine_row", None)
        token_button = getattr(self, "_telegram_token_button", None)
        if button is None or row is None:
            return
        button.pack_forget()
        entry = self._ttk.Entry(
            row, textvariable=self._telegram_machine_var, width=14
        )
        self._machine_name_entry = entry
        entry.pack(side="left", padx=(4, 8), before=token_button)
        entry.bind("<FocusOut>", self._machine_name_finish_edit)
        entry.bind("<Return>", self._machine_name_finish_edit)
        entry.focus_set()
        entry.selection_range(0, "end")

    def _machine_name_finish_edit(self, _event: Any = None) -> None:
        entry = self._machine_name_entry
        if entry is None:
            return
        self._machine_name_entry = None
        try:
            entry.destroy()
        except Exception:
            pass
        name = self._telegram_machine_var.get().strip()
        self._telegram_machine_var.set(name)
        self._telegram_machine_button.configure(
            text=machine_name_button_text(name)
        )
        self._telegram_machine_button.pack(
            side="left", padx=(4, 8), before=self._telegram_token_button
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
                entry = self._ttk.Entry(row, width=34)
                entry.insert(0, message)
                entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
                self._quick_edit_entry = entry
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
                # ttk.Button/Entry controls in this compact panel can be
                # elided by certain Windows themes.  A single ttk.Label is
                # always rendered and still supports the complete click,
                # double-click, and long-press interaction through bindings.
                preview = quick_message_preview(message)
                message_label = self._ttk.Label(
                    row, text=f"Ctrl+{key_number}   {preview}", anchor="w"
                )
                message_label.pack(
                    side="left", fill="x", expand=True, padx=(0, 4)
                )
                if preview != message:
                    tooltip = HoverTooltip(message_label, message)
                    tooltip.set_enabled(True)
                    self._quick_message_tooltips.append(tooltip)
                message_label.bind(
                    "<ButtonPress-1>",
                    lambda event, i=index: self._quick_message_press(i),
                )
                message_label.bind(
                    "<ButtonRelease-1>",
                    lambda event, i=index: self._quick_message_release(i),
                )
            delete_button = self._ttk.Label(row, text="×", width=3, anchor="center")
            delete_button.pack(side="left")
            delete_button.bind(
                "<ButtonPress-1>",
                lambda event, i=index: self._quick_delete_press(i),
            )
            delete_button.bind(
                "<ButtonRelease-1>",
                lambda event, i=index: self._quick_delete_release(i),
            )

    def _quick_messages_save(self) -> None:
        self._shutdown_save_settings(self._shutdown_collect_data())

    def _quick_message_add(self) -> None:
        if len(self._quick_messages) >= 20:
            self._quick_message_status.configure(text="快捷消息最多 20 条。")
            return
        self._quick_messages.append("新快捷消息")
        index = len(self._quick_messages) - 1
        self._quick_messages_save()
        self._render_quick_messages(index)
        self._refit_window_to_content()

    def _quick_message_press(self, index: int) -> None:
        self._quick_message_hold_fired = False
        if self._root is not None:
            self._quick_message_press_job = self._root.after(
                1000, lambda: self._quick_message_begin_edit(index)
            )

    def _quick_message_release(self, index: int) -> None:
        if self._root is not None and self._quick_message_press_job is not None:
            try:
                self._root.after_cancel(self._quick_message_press_job)
            except Exception:
                pass
        self._quick_message_press_job = None
        if self._quick_message_hold_fired:
            self._quick_message_hold_fired = False
            return
        if not (0 <= index < len(self._quick_messages)):
            return
        now = time.monotonic()
        last_at = getattr(
            self, "_quick_message_last_click_at", float("-inf")
        )
        last_index = getattr(self, "_quick_message_last_click_index", None)
        if last_index == index and now - last_at <= 0.60:
            self._quick_message_last_click_at = float("-inf")
            self._quick_message_last_click_index = None
            self._quick_message_double_click(index)
            return
        self._quick_message_last_click_at = now
        self._quick_message_last_click_index = index
        self._copy_quick_message(index)

    def _copy_quick_message(self, index: int) -> bool:
        if not (0 <= index < len(self._quick_messages)):
            return False
        message = self._quick_messages[index]
        try:
            self._root.clipboard_clear()
            self._root.clipboard_append(message)
            self._root.update_idletasks()
            self._quick_message_status.configure(text=f"已复制：{message}")
            return True
        except Exception as exc:
            self._quick_message_status.configure(text=f"复制失败：{exc}")
            return False

    def _quick_message_double_click(self, index: int) -> str:
        """Copy and explicitly send the selected message to game chat."""

        if self._root is not None and self._quick_message_press_job is not None:
            try:
                self._root.after_cancel(self._quick_message_press_job)
            except Exception:
                pass
        self._quick_message_press_job = None
        self._send_quick_message(index)
        return "break"

    def _send_quick_message(self, index: int) -> bool:
        """Send one live list item; deletion naturally shifts later keys."""

        if not self._copy_quick_message(index):
            return False
        sender = getattr(getattr(self, "status_worker", None), "key_sender", None)
        send = getattr(sender, "send_clipboard_message", None)
        if send is None:
            self._quick_message_status.configure(text="发送失败：游戏输入未接入。")
            return False
        try:
            if send() is False:
                raise OSError("无法聚焦游戏窗口")
            self._quick_message_status.configure(
                text=f"已发送：{self._quick_messages[index]}"
            )
            return True
        except Exception as exc:
            self._quick_message_status.configure(text=f"发送失败：{exc}")
            return False

    def _quick_message_begin_edit(self, index: int) -> None:
        self._quick_message_press_job = None
        self._quick_message_hold_fired = True
        self._quick_message_last_click_at = float("-inf")
        self._quick_message_last_click_index = None
        if 0 <= index < len(self._quick_messages):
            self._render_quick_messages(index)

    def _quick_message_finish_edit(self, index: int, entry: Any) -> None:
        if entry is not self._quick_edit_entry:
            return
        self._quick_edit_entry = None
        try:
            text = entry.get().strip()
        except Exception:
            text = ""
        if 0 <= index < len(self._quick_messages) and text:
            self._quick_messages[index] = text[:500]
            self._quick_messages_save()
        self._render_quick_messages()
        self._refit_window_to_content()

    def _quick_delete_press(self, index: int) -> None:
        self._quick_delete_hold_fired = False
        if self._root is not None:
            self._quick_delete_press_job = self._root.after(
                1000, lambda: self._quick_delete(index)
            )

    def _quick_delete_release(self, _index: int) -> None:
        if self._root is not None and self._quick_delete_press_job is not None:
            try:
                self._root.after_cancel(self._quick_delete_press_job)
            except Exception:
                pass
        self._quick_delete_press_job = None
        self._quick_delete_hold_fired = False

    def _quick_delete(self, index: int) -> None:
        self._quick_delete_press_job = None
        self._quick_delete_hold_fired = True
        if 0 <= index < len(self._quick_messages):
            deleted = self._quick_messages.pop(index)
            self._quick_messages_save()
            self._render_quick_messages()
            self._quick_message_status.configure(text=f"已删除：{deleted}")
            self._refit_window_to_content()

    def _freeze_column_widths(self) -> Optional[tuple[int, int]]:
        """Freeze both grid tracks after their complete initial layout.

        Both columns have explicit, invariant pixel widths. Text in the patrol
        stack must never participate in a later grid-width negotiation.
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
            # The grid itself is non-propagating. The two constants are the
            # complete width lock without clipping vertical panel growth.
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
        """Ask for a token, then let the notifier validate it asynchronously."""

        try:
            from tkinter import simpledialog

            token = simpledialog.askstring(
                "修改BOT token",
                "粘贴 Telegram BOT token。\n"
                "请先在 Telegram 给这个 BOT 发送一条消息，系统会自动识别聊天。",
                parent=self._root,
                show="*",
            )
        except Exception as exc:
            self._telegram_status.configure(
                text=f"消息提醒: 无法打开 token 输入框 - {exc}"
            )
            return
        if token is None:
            return
        self._telegram_bot_token = token.strip()
        self._telegram_chat_id = ""
        self._telegram_status.configure(text="消息提醒: 正在验证 BOT 配置...")
        self._shutdown_on_change()

    def _refresh_telegram_status(self) -> None:
        """Show notifier health and persist an auto-discovered chat ID."""

        if not hasattr(self, "_telegram_status"):
            return
        notifier = getattr(self, "telegram_notifier", None)
        if notifier is None:
            self._telegram_status.configure(text="消息提醒: 工作线程未接入。")
            return
        try:
            snapshot = notifier.snapshot()
            self._telegram_status.configure(text=str(snapshot["status"]))
            discovered = str(snapshot.get("chat_id", "")).strip()
            if discovered and discovered != self._telegram_chat_id:
                self._telegram_chat_id = discovered
                self._shutdown_save_settings(self._shutdown_collect_data())
        except Exception as exc:
            # Status display itself must be non-fatal too.
            self._telegram_status.configure(
                text=f"消息提醒: 状态读取失败 - {exc}"
            )

    # Human-readable key labels for the help dialog (mirrors hotkey.json
    # KEY_VK spellings used by HotkeyWorker).
    _HELP_KEY_LABELS = {
        "left": "←", "right": "→", "up": "↑", "down": "↓",
        "home": "Home", "insert": "Insert", "delete": "Delete",
        "bracketleft": "[", "bracketright": "]", "grave": "`",
        "d": "D", "f": "F",
        "0": "0", "1": "1", "2": "2", "3": "3", "4": "4",
        "5": "5", "6": "6", "7": "7", "8": "8", "9": "9",
    }

    _HELP_ACTION_LABELS = {
        "record:left_most_pos": "录制当前图层的 最左 巡逻点",
        "record:rope_pos": "录制当前图层的 绳索 点",
        "record:right_most_pos": "录制当前图层的 最右 巡逻点",
        "record_jump_point:left": "插入当前图层的 左跳 点 (Ctrl+D)",
        "record_jump_point:right": "插入当前图层的 右跳 点 (Ctrl+F)",
        "select_next_layer": "选择下一个录制图层 (Ctrl+↓)",
        "select_next_patrol_start": "选择下一个巡逻起始楼层 (Ctrl+Home)",
        "add_highest_layer": "添加最高楼层",
        "delete_highest_layer": "删除最高楼层",
        "toggle_patrol": "开始 / 停止巡逻 (Ctrl+`)",
        "adjust_fixed_attack_interval:-0.1": "缩短巡逻攻击间隔 0.1 秒",
        "adjust_fixed_attack_interval:+0.1": "加长巡逻攻击间隔 0.1 秒",
        "quick_pickup:toggle": "开启 / 关闭快速拾取（仅停止巡逻时）",
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

    def _help_cancel_jobs(self) -> None:
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

        enabled = bool(data.get("enabled", True))
        state_text = "已启用（配置文件 hotkey.json：enabled=true）" if enabled \
            else "已停用（配置文件 hotkey.json：enabled=false）"
        header_text = (
            f"快捷键总开关: {state_text}\n"
            "巡逻运行时，除 Ctrl+` (开始/停止巡逻) 和 Ctrl+[ / Ctrl+] "
            "(固定攻击间隔) 外，其余快捷键都会临时停用，停止巡逻后恢复。\n"
            "按快捷键后会自动松开 Ctrl，避免游戏里的 Ctrl 攻击持续触发"
            "（配置项 release_ctrl_after_chord=true，默认开启）。\n"
            "日文/中文/韩文输入法开启时会吞掉 Ctrl 组合键；"
            "请把输入法切到「英数/半角英数」或关闭。\n"
            "修改快捷键配置文件后需重启程序生效。"
        )
        footer_text = (
            "启用/停用：配置文件的 enabled 控制总开关；"
            "巡逻中除 Ctrl+` 与攻击间隔外的快捷键会暂时停用；"
            "ignore_injected=true 表示只响应真实物理按键；"
            "delivery=hook 表示使用低级键盘钩子（默认，输入法开启时仍可用），"
            "delivery=native 表示使用系统热键。\n"
            "移开鼠标即自动关闭本提示。"
        )

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
        tk.Label(
            content, text=header_text, justify="left",
            background="#fffbd6", foreground="#202020",
            wraplength=430, anchor="w",
        ).pack(fill="x", pady=(0, 4))

        if key_lines:
            table = tk.Frame(content, bg="#fffbd6")
            table.pack(fill="x", pady=(2, 4))
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

        tk.Label(
            content, text=footer_text, justify="left",
            background="#fffbd6", foreground="#202020",
            wraplength=430, anchor="w",
        ).pack(fill="x", pady=(4, 0))

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
        self._shutdown_status.configure(text=text)

    @staticmethod
    def _format_countdown_seconds(seconds: float) -> str:
        seconds = max(0, int(round(seconds)))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    def _countdown_on_change(self, _value: str = "") -> None:
        """Persist/apply the repeating reminder interval and enabled state."""

        if not hasattr(self, "_countdown_interval_label"):
            return
        raw_hours = max(0.1, min(6.0, float(self._countdown_interval_var.get())))
        # Convert through an integer tenth-hour index so stored/displayed
        # values are exact selectable grid points rather than slider floats.
        hours = (int(round((raw_hours - 0.1) * 10.0)) + 1) / 10.0
        if abs(raw_hours - hours) > 1e-9:
            self._countdown_interval_var.set(hours)
        self._countdown_interval_label.configure(text=f"{hours:.1f}h")
        interval_seconds = hours * 3600.0
        self._countdown_remaining_slider.configure(to=interval_seconds)
        self._countdown_remaining_var.set(interval_seconds)
        self._countdown_remaining_label.configure(
            text=self._format_countdown_seconds(interval_seconds)
        )
        data = self._shutdown_collect_data()
        self._shutdown_save_settings(data)
        self._countdown_apply_to_worker(data)
        self._countdown_refresh_grey()

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
            return

        # The UI's hour slider is the supported interval source. Restoring
        # the recorded interval first guarantees the remaining slider does
        # not clamp the durable deadline after a configuration change.
        hours = max(0.1, min(6.0, interval / 3600.0))
        interval = hours * 3600.0
        remaining = min(remaining, interval)
        self._countdown_interval_var.set(hours)
        self._countdown_enabled_var.set(True)
        data = self._shutdown_collect_data()
        self._shutdown_save_settings(data)
        self._countdown_apply_to_worker(data)
        worker = getattr(self, "countdown_worker", None)
        if worker is not None:
            worker.set_remaining_seconds(remaining)
        self._countdown_remaining_slider.configure(to=interval)
        self._countdown_remaining_var.set(remaining)
        self._countdown_remaining_label.configure(
            text=self._format_countdown_seconds(remaining)
        )
        self._countdown_refresh_grey()
        LOG.info("countdown restored from timer.json remaining=%.1fs", remaining)

    def _countdown_apply_to_worker(self, data: dict) -> None:
        worker = getattr(self, "countdown_worker", None)
        if worker is None:
            self._countdown_status.configure(
                text="循环警报: 工作线程未接入 (无界面模式)。"
            )
            return
        hours = max(
            0.1, min(6.0, float(data.get("countdown_interval_hours", 1.0)))
        )
        worker.set_interval_hours(hours)
        worker.set_enabled(bool(data.get("countdown_enabled", False)))
        if worker.enabled:
            self._countdown_status.configure(
                text=f"循环警报已启动: 每 {hours:.1f} 小时触发已选提醒。"
            )
        else:
            self._countdown_status.configure(text="循环警报: 未启用。")

    def _countdown_refresh_grey(self) -> None:
        enabled = bool(self._countdown_enabled_var.get())
        state = "!disabled" if enabled else "disabled"
        for widget in (
            self._countdown_interval_slider,
            self._countdown_interval_label,
            self._countdown_remaining_slider,
            self._countdown_remaining_label,
        ):
            try:
                widget.state([state])
            except Exception:
                try:
                    widget.configure(
                        state="normal" if enabled else "disabled"
                    )
                except Exception:
                    pass

    def _countdown_drag_start(self, _event: Any = None) -> None:
        self._countdown_dragging = True

    def _countdown_drag_end(self, _event: Any = None) -> None:
        self._countdown_remaining_on_drag(
            str(self._countdown_remaining_var.get())
        )
        self._countdown_dragging = False

    def _countdown_remaining_on_drag(self, value: str) -> None:
        """Move the live deadline as the user drags the remaining-time bar."""

        if (
            not bool(self._countdown_enabled_var.get())
            or not self._countdown_dragging
        ):
            return
        # A remaining-time drag operates in whole minutes.  The worker still
        # counts seconds internally; only manual selection is quantized.
        remaining = max(0.0, round(float(value) / 60.0) * 60.0)
        if abs(float(self._countdown_remaining_var.get()) - remaining) > 1e-9:
            self._countdown_remaining_var.set(remaining)
        self._countdown_remaining_label.configure(
            text=self._format_countdown_seconds(remaining)
        )
        worker = getattr(self, "countdown_worker", None)
        if worker is not None:
            worker.set_remaining_seconds(remaining)

    def _refresh_countdown_status(self) -> None:
        """Keep the draggable bar synchronized unless it is being dragged."""

        if not hasattr(self, "_countdown_status"):
            return
        worker = getattr(self, "countdown_worker", None)
        if worker is None:
            return
        enabled, interval, remaining = worker.snapshot()
        if not enabled:
            return
        self._countdown_remaining_slider.configure(to=interval)
        if not self._countdown_dragging:
            self._countdown_remaining_var.set(remaining)
            self._countdown_remaining_label.configure(
                text=self._format_countdown_seconds(remaining)
            )
        self._countdown_status.configure(
            text=("循环警报: 剩余 "
                  f"{self._format_countdown_seconds(remaining)} / "
                  f"间隔 {interval / 3600.0:.1f}h；到时提醒并自动重置。")
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
                self._reconnect_channel_var.set(str(saved_channel))
            if hasattr(self, "_auto_restart_var"):
                self._auto_restart_var.set(bool(data.get("auto_restart_enabled", False)))
            if hasattr(self, "_restart_offline_message_var"):
                self._restart_offline_message_var.set(
                    str(data.get("restart_offline_message", "")).strip()[:500]
                )
                self._refresh_workflow_message_button(
                    "restart_offline", "重开消息", self._restart_offline_message_var
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
                    wait_minutes = int(data.get("player_channel_wait_minutes", 5))
                except (TypeError, ValueError):
                    wait_minutes = 5
                self._player_channel_wait_var.set(str(max(1, min(20, wait_minutes))))
            if hasattr(self, "_api_test_status"):
                # 测试api has no panel settings; the line only states the backend it will reach.
                self._set_api_test_status(self._api_test_status_text())
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
                self._telegram_machine_button.configure(
                    text=machine_name_button_text(
                        self._telegram_machine_var.get()
                    )
                )
            if hasattr(self, "_quick_messages"):
                self._quick_messages = normalize_quick_messages(
                    data.get("quick_messages", [])
                )
                self._render_quick_messages()
            if hasattr(self, "_countdown_enabled_var"):
                # Like scheduled shutdown, do not silently start a timer on
                # application launch. Preserve only its configured time gap.
                self._countdown_enabled_var.set(False)
                if "countdown_interval_hours" in data:
                    self._countdown_interval_var.set(max(
                        0.1, min(6.0, float(data["countdown_interval_hours"]))
                    ))
        except (KeyError, TypeError, ValueError):
            LOG.warning("ignored malformed additional functions settings",
                        exc_info=True)
            return
        self._shutdown_on_change()
        if hasattr(self, "_countdown_enabled_var"):
            self._countdown_on_change()
            self._restore_countdown_resume_state()
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
        if not stationary_mode and not self.patrol_controller.can_start():
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
        if str(getattr(self, "_attack_mode_var", None).get()
               if hasattr(self, "_attack_mode_var") else "fixed") == "stationary":
            self._control_status.configure(
                text="运行已开始。只有正确配置楼层，才能寻路回去。"
            )
        else:
            self._control_status.configure(text="运行已开始。")
        LOG.info(LOG_RUN_START + "。")
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
            self._control_status.configure(text="正在停止巡逻并释放按键…")
        except Exception:
            LOG.exception("Stop Patrol immediate UI refresh failed")

        def release_input_in_background() -> None:
            try:
                LOG.info("STOP PATROL: background input cleanup starting")
                if self.on_patrol_stop is not None:
                    self.on_patrol_stop()
            except Exception:
                # A foreground/key-release problem must be recorded, but Stop
                # Patrol must leave the dashboard usable for manual recovery.
                LOG.exception("Stop Patrol input cleanup failed")
            finally:
                self._patrol_stop_cleanup_done.set()

        threading.Thread(
            target=release_input_in_background,
            name="patrol-stop-cleanup",
            daemon=True,
        ).start()
        # Stopping patrol also stops the YOLO attack subprocess: in stand-still
        # mode the character stands and the YOLO executor attacks, so without
        # this the character would keep attacking after Stop Patrol.
        try:
            self._yolo_stop()
        except Exception:
            # A detector subprocess/UI cleanup failure must never take down
            # the dashboard after patrol input has already been disarmed.
            LOG.exception("Stop Patrol detector cleanup failed")
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

    def _stationary_attack_selected(self) -> bool:
        """Whether Start Patrol must use the route-independent stand-still mode.

        The UI value is included as well as the live worker state.  This keeps
        the Start button usable during the tiny interval between selecting
        ``站桩攻击`` and the setting callback applying it to the worker.
        """

        mode_var = getattr(self, "_attack_mode_var", None)
        if mode_var is not None:
            try:
                if str(mode_var.get()) == "stationary":
                    return True
            except Exception:
                pass
        return bool(getattr(
            getattr(self, "movement_worker", None),
            "stationary_attack_enabled", False,
        ))

    def _add_layer_above(self) -> bool:
        if self.patrol_controller is None:
            self._control_status.configure(text="巡逻控制器不可用。")
            return False
        try:
            layer_name = self.patrol_controller.add_layer_above()
        except (OSError, ValueError) as exc:
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
            controller.reset_recording()
        except Exception as exc:
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
            or self.patrol_controller.can_start()
        )
        selected = self.patrol_controller.selected_layer()
        snapshot = self.patrol_controller.snapshot()
        route = snapshot.route_order
        layer_names = list(route)
        layer_names.extend(name for name in snapshot.layers if name not in layer_names)
        layer_names = list(layer_display_order(layer_names))
        self._update_patrol_range_combos(layer_names)
        self._ensure_layer_rows(tuple(layer_names))
        if hasattr(self, "_selected_layer_var"):
            self._selected_layer_var.set(selected)
        for layer_name in layer_names:
            self._layer_labels[layer_name].configure(
                text=self._patrol_display_name(layer_name)
            )
            self._draw_layer_axis(
                layer_name, snapshot.layers.get(layer_name, {})
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
        if not self._license_allowed():
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
            removed = self.patrol_controller.remove_highest_layer()
            selected = self.patrol_controller.selected_layer()
        except (OSError, ValueError) as exc:
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
            self.patrol_controller.set_patrol_range(next_start, end)
            # Ctrl+Home changes the patrol's lower bound, so make the same
            # layer visibly selected for recording as well.  Otherwise the
            # radio circle could stay on a different layer and make the hotkey
            # appear to have done nothing.
            self.patrol_controller.select_layer(next_start)
        except (OSError, ValueError) as exc:
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
            self.patrol_controller.set_patrol_range(start, end)
        except ValueError as exc:
            self._control_status.configure(text=f"无法选择巡逻楼层: {exc}")
            self._refresh_patrol_controls()
            return
        self._control_status.configure(
            text=f"巡逻楼层: {start_display} → {end_display}（支持连续范围，可单选一层）"
        )
        self._refresh_patrol_controls()

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
            # This is deliberately a fixed width, sized for the widest normal
            # display name (for example, 楼层10).  A 4-character Tk width clips
            # CJK glyphs on some Windows font/DPI combinations and lets the
            # axis canvas visually cover the final digit.
            label = ttk.Label(row, width=6)
            # Keep the axis marker canvas a further fixed three pixels away;
            # no row is resized when modes or recordings change.
            label.pack(side="left", anchor="center", padx=(0, 10))
            self._layer_labels[layer_name] = label
            canvas = self._tk.Canvas(
                row,
                width=_LAYER_AXIS_WIDTH,
                height=_LAYER_AXIS_HEIGHT,
                highlightthickness=0,
                background="#f0f0f0",
                cursor="hand2",
            )
            canvas.pack(side="left", fill="x", expand=True)
            # Recording choices are intentionally right-click only.  Left
            # clicks remain free for normal UI selection and marker double-
            # click deletion, never opening an add-recording menu.
            canvas.bind(
                "<Button-3>",
                lambda event, layer=layer_name: self._layer_axis_menu(event, layer),
            )
            self._layer_axis_canvases[layer_name] = canvas


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
