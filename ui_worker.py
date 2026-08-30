"""Independent Tk debug dashboard fed by the capture frame bus."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
from PIL import Image, ImageTk

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
from versioning import version_label


LOG = logging.getLogger(__name__)

_INITIAL_WINDOW_WIDTH = 1200
_INITIAL_WINDOW_HEIGHT = 900


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


def _load_window_geometry(default_geometry: str) -> str:
    """Return the saved debug UI geometry, or ``default_geometry`` when the
    settings file is missing/corrupt.  Never raises."""
    try:
        data = json.loads(
            _window_geometry_settings_path().read_text(encoding="utf-8")
        )
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
            json.dumps({"geometry": geometry}, indent=2) + "\n",
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
    """Feed formatted logs to Tk without allowing an unbounded backlog."""

    def __init__(self, capacity: int = 300) -> None:
        super().__init__()
        self.messages: "queue.Queue[str]" = queue.Queue(maxsize=max(20, capacity))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
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
    marker = detect_yellow_diamond(np.asarray(analysis_image.convert("RGB")))
    analysis_left, analysis_top, analysis_right, analysis_bottom = detection.analysis_box
    canvas_left, canvas_top, canvas_right, canvas_bottom = detection.canvas_box
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

    Only the game-usable hotkeys are bindable (``BINDABLE_KEYS``: shift /
    ctrl / alt / space / delete / end / pageup / pagedown / home / insert
    and the 1-9 number row).  Escape cancels key capture and restores the
    previous binding; every other key is ignored.
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
        "a", "space", "ctrl", "shift", "delete", "end",
        "home", "insert", "pageup", "pagedown",
    ]
    available = " / ".join(name for name in ordered if name in BINDABLE_KEYS)
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

    return f"x={float(x):.4f} y={float(y):.4f}"


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
    _FIXED_RANDOM_GAP_STEP = 0.1
    _FIXED_RANDOM_GAP_MAX = 5.0

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
        movement_worker: Any = None,
        character_worker: Any = None,
        shutdown_worker: Any = None,
        countdown_worker: Any = None,
        lie_detector_worker: Any = None,
        screen_blinker: Any = None,
        telegram_notifier: Any = None,
        on_patrol_start: Optional[Callable[[], None]] = None,
        on_patrol_stop: Optional[Callable[[], None]] = None,
        on_capture_now: Optional[Callable[[], Any]] = None,
        on_recording_verified: Optional[Callable[[DebugSnapshot], None]] = None,
        log_queue: Optional["queue.Queue[str]"] = None,
        automation_active_event: Optional[threading.Event] = None,
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
        # capture bus. It never creates screenshot files.
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
        self.on_patrol_stop = on_patrol_stop
        self.on_capture_now = on_capture_now
        self.on_recording_verified = on_recording_verified
        self.log_queue = log_queue
        self.automation_active_event = automation_active_event
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
        self._layer_labels: dict[str, Any] = {}
        self._layer_row_names: tuple[str, ...] = ()
        # Only explicit unlocks need UI state. Locking itself is derived from
        # the controller's saved endpoint, so dynamically created rows behave
        # identically to rows present at startup.
        self._unlocked_points: set[tuple[str, str]] = set()
        self._record_press_job: Any = None
        self._record_hold_fired = False

    def run(self) -> None:
        try:
            import tkinter as tk
            from tkinter import ttk
            self._ttk = ttk

            root = tk.Tk()
            self._root = root
            app_version = version_label()
            root.title(f"Maple 助手 ({app_version}) 调试界面")
            screen_width = root.winfo_screenwidth()
            screen_height = root.winfo_screenheight()
            # 调试窗口不抢前台：不设置 -topmost，游戏在爬绳/挂绳时保持焦点，
            # 不会因调试窗口抢到前台而松开按键、角色跳离绳索。窗口位置与大小
            # 按上次保存的几何恢复（可移动、可调整），默认不再固定左上角。
            restored = _load_window_geometry(
                f"{_INITIAL_WINDOW_WIDTH}x{_INITIAL_WINDOW_HEIGHT}+40+40"
            )
            root.geometry(_clamp_window_geometry(
                restored, screen_width, screen_height,
                max_height=_INITIAL_WINDOW_HEIGHT,
            ))
            root.minsize(980, 560)
            root.protocol("WM_DELETE_WINDOW", self._on_debug_window_close)
            self._schedule_window_geometry_save(root)

            container = ttk.Frame(root, padding=12)
            container.pack(fill="both", expand=True)
            title = ttk.Label(
                container, text=f"Maple 助手 ({app_version})",
                font=("Segoe UI", 16, "bold"),
            )
            title.pack(anchor="w")
            ttk.Label(container, text="OpenCV 小地图检测 · 巡逻控制").pack(anchor="w")

            columns = ttk.Frame(container)
            columns.pack(fill="both", expand=True, pady=(8, 0))
            # The controls column used to receive half the window even though
            # its content needs less room. A 2:3 grid makes it 40% of the
            # available width (20% narrower than the former 50% share) and
            # gives the debug/YOLO column the recovered space.
            columns.columnconfigure(0, weight=2)
            columns.columnconfigure(1, weight=3)
            columns.rowconfigure(0, weight=1)
            col1 = ttk.Frame(columns)
            col1.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
            col2 = ttk.Frame(columns)
            col2.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

            controls = ttk.LabelFrame(col1, text="图层校准与巡逻", padding=10)
            controls.pack(fill="x", pady=(12, 8))
            style = ttk.Style(root)
            style.configure("Locked.TButton", foreground="#777777")
            style.map("Locked.TButton", foreground=[("!disabled", "#777777")])
            # Show Detection toggle: grey (inactive) until checked.
            style.configure("Off.TCheckbutton", foreground="#999999")
            style.map(
                "Off.TCheckbutton",
                foreground=[("selected", "#000000"), ("!selected", "#999999")],
            )
            action_row = ttk.Frame(controls)
            action_row.pack(fill="x", pady=(0, 8))
            self._start_patrol_button = ttk.Button(
                action_row, text="开始巡逻", command=self._start_patrol
            )
            self._start_patrol_button.pack(side="left", padx=(0, 8))
            self._stop_patrol_button = ttk.Button(
                action_row, text="停止巡逻", command=self._stop_patrol
            )
            self._stop_patrol_button.pack(side="left", padx=(0, 8))
            self._add_layer_button = ttk.Button(
                action_row, text="添加楼层", command=self._add_layer_above
            )
            self._add_layer_button.pack(side="left", padx=(0, 8))
            self._reset_recording_button = ttk.Button(
                action_row, text="重置录制", command=self._reset_recording
            )
            self._reset_recording_button.pack(side="left")
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
            self._control_status = ttk.Label(
                controls,
                text="先录制 最左、绳索、最右，然后添加上方图层。",
            )
            self._control_status.pack(anchor="w", pady=(8, 0))
            self._automation_status_label = ttk.Label(controls)
            self._automation_status_label.pack(anchor="w", pady=(5, 0))
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
                yolo_panel, text="YOLO 检测已停止。", justify="left"
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
                col1, text="攻击模式", padding=10
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
                mode_row, text="固定攻击", value="fixed",
                variable=self._attack_mode_var,
                command=self._fixed_on_mode_change,
            ).pack(side="left")
            fixed_key_row = ttk.Frame(fixed_panel)
            fixed_key_row.pack(fill="x", pady=(6, 0))
            ttk.Label(fixed_key_row, text="攻击按键:").pack(
                side="left", padx=(0, 4)
            )
            self._fixed_attack_key_var = tk.StringVar(value="ctrl")
            fixed_key_button = ttk.Button(
                fixed_key_row, text=self._fixed_attack_key_var.get(),
                width=10, style="Locked.TButton",
                command=lambda: self._bind_capture_begin(
                    fixed_key_button, self._fixed_attack_key_var,
                    "_fixed_attack_key_previous",
                    lambda: self._fixed_on_change(),
                ),
            )
            fixed_key_button.pack(side="left", padx=(0, 10))
            self._fixed_key_button = fixed_key_button
            self._attach_bind_hint(fixed_key_button)
            ttk.Label(fixed_key_row, text="每").pack(side="left")
            # Fixed attack period: horizontal slider (progress-bar style),
            # 0.5-10 s, default 3 s.
            self._fixed_interval_var = tk.DoubleVar(value=3.0)
            fixed_interval_slider = ttk.Scale(
                fixed_key_row, from_=0.5, to=10.0, orient="horizontal",
                length=105,
                variable=self._fixed_interval_var,
                command=self._fixed_on_change,
            )
            fixed_interval_slider.pack(side="left", padx=(0, 6))
            self._fixed_interval_label = ttk.Label(
                fixed_key_row, text="3.0s", width=5
            )
            self._fixed_interval_label.pack(side="left", padx=(0, 6))
            ttk.Label(fixed_key_row, text="随机间差:").pack(side="left")
            self._fixed_random_gap_var = tk.DoubleVar(value=0.1)
            ttk.Button(
                fixed_key_row, text="−", width=2,
                command=lambda: self._fixed_adjust_random_gap(-0.1),
            ).pack(side="left", padx=(3, 2))
            self._fixed_random_gap_label = ttk.Label(
                fixed_key_row, text="0.1s", width=5, anchor="center"
            )
            self._fixed_random_gap_label.pack(side="left")
            ttk.Button(
                fixed_key_row, text="+", width=2,
                command=lambda: self._fixed_adjust_random_gap(0.1),
            ).pack(side="left", padx=(2, 0))
            self._fixed_range_label = ttk.Label(
                fixed_panel,
                text="随机攻击间隔: (3.0s, 3.1s)  基础=3.0s + 随机=0.0~0.1s",
                justify="left",
            )
            self._fixed_range_label.pack(anchor="w", pady=(6, 0))
            self._fixed_status = ttk.Label(
                fixed_panel, text="固定攻击未启用。", justify="left"
            )
            self._fixed_load_settings()

            # Drug (HP/MP potion) panel: key binds + percent trigger sliders.
            # The StatusWorker taps the bound key when the bar ratio drops
            # below the chosen percent (debounced by frames + cooldown).
            drug_panel = ttk.LabelFrame(
                col2, text="药品 (HP/MP 药水)", padding=10
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
            hp_threshold_slider = ttk.Scale(
                hp_row, from_=5, to=95, orient="horizontal",
                variable=self._hp_threshold_var,
                command=self._drug_on_change,
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
            )
            mp_threshold_slider.pack(side="left", fill="x",
                                     expand=True, padx=(8, 8))
            self._mp_threshold_label = ttk.Label(mp_row, text="30%", width=6)
            self._mp_threshold_label.pack(side="left")
            # Periodic buff rows: a bound key tapped on a timer.  Each row has
            # its own "every N minutes" slider (default 10 min) that decides
            # when the key is triggered.  Unlike HP/MP these are time-based,
            # not bar-percent based.
            buff1_row = ttk.Frame(drug_panel)
            buff1_row.pack(fill="x", pady=(6, 0))
            self._buff1_use_var = tk.BooleanVar(value=False)
            buff1_use_button = ttk.Checkbutton(
                buff1_row, text="增益 1", variable=self._buff1_use_var,
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
            # Buff refresh period in minutes (default 10): horizontal slider
            # in the same progress-bar style as the other panels.
            self._buff1_interval_var = tk.DoubleVar(value=10.0)
            buff1_interval_slider = ttk.Scale(
                buff1_row, from_=0.5, to=30.0, orient="horizontal",
                variable=self._buff1_interval_var,
                command=self._drug_on_change,
            )
            buff1_interval_slider.pack(side="left", fill="x",
                                       expand=True, padx=(8, 8))
            self._buff1_interval_label = ttk.Label(
                buff1_row, text="10.0min", width=8
            )
            self._buff1_interval_label.pack(side="left")
            buff2_row = ttk.Frame(drug_panel)
            buff2_row.pack(fill="x", pady=(6, 0))
            self._buff2_use_var = tk.BooleanVar(value=False)
            buff2_use_button = ttk.Checkbutton(
                buff2_row, text="增益 2", variable=self._buff2_use_var,
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
            self._buff2_interval_var = tk.DoubleVar(value=10.0)
            buff2_interval_slider = ttk.Scale(
                buff2_row, from_=0.5, to=30.0, orient="horizontal",
                variable=self._buff2_interval_var,
                command=self._drug_on_change,
            )
            buff2_interval_slider.pack(side="left", fill="x",
                                       expand=True, padx=(8, 8))
            self._buff2_interval_label = ttk.Label(
                buff2_row, text="10.0min", width=8
            )
            self._buff2_interval_label.pack(side="left")
            self._drug_status = ttk.Label(
                drug_panel, text="药品面板就绪。", justify="left"
            )
            self._drug_status.pack(anchor="w", pady=(6, 0))
            # Restore previously saved drug settings and apply them live.
            self._drug_load_settings()

            # Persistent clipboard shortcuts. Short click copies, double-click
            # sends to game chat, and long press edits. The adjacent delete
            # icon also requires a 1s long press.
            quick_panel = ttk.LabelFrame(col2, text="快捷消息", padding=10)
            quick_panel.pack(fill="x", pady=(0, 8))
            quick_header = ttk.Frame(quick_panel)
            quick_header.pack(fill="x")
            ttk.Button(
                quick_header, text="添加快捷消息",
                command=self._quick_message_add,
            ).pack(side="left")
            self._quick_message_status = ttk.Label(
                quick_header, text="单击复制；双击发送；长按 1 秒修改/删除。"
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
                col1, text="附加功能", padding=10
            )
            extra_panel.pack(fill="x", pady=(0, 8))
            shutdown_row = ttk.Frame(extra_panel)
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
            )
            self._shutdown_status.pack(anchor="w", pady=(6, 0))

            alarm_row = ttk.Frame(extra_panel)
            alarm_row.pack(fill="x", pady=(8, 0))
            ttk.Label(alarm_row, text="警报:").pack(side="left")

            self._disconnect_alert_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                alarm_row,
                text="掉线警报",
                variable=self._disconnect_alert_var,
                command=self._shutdown_on_change,
            ).pack(side="left", padx=(4, 8))

            self._lie_alert_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                alarm_row,
                text="测谎警报",
                variable=self._lie_alert_var,
                command=self._shutdown_on_change,
            ).pack(side="left", padx=(0, 8))

            self._countdown_enabled_var = tk.BooleanVar(value=False)
            self._countdown_check = ttk.Checkbutton(
                alarm_row, text="循环警报",
                variable=self._countdown_enabled_var,
                command=self._countdown_on_change,
            )
            self._countdown_check.pack(side="left")

            countdown_row = ttk.Frame(extra_panel)
            countdown_row.pack(fill="x", pady=(4, 0))
            ttk.Label(countdown_row, text="间隔").pack(side="left")
            self._countdown_interval_var = tk.DoubleVar(value=1.0)
            self._countdown_interval_slider = ttk.Scale(
                countdown_row, from_=0.1, to=12.0, orient="horizontal",
                length=70,
                variable=self._countdown_interval_var,
                command=self._countdown_on_change,
            )
            self._countdown_interval_slider.pack(
                side="left", padx=(4, 4)
            )
            self._countdown_interval_label = ttk.Label(
                countdown_row, text="1.0h", width=6
            )
            self._countdown_interval_label.pack(side="left")

            ttk.Label(countdown_row, text="剩余").pack(
                side="left", padx=(6, 0)
            )
            self._countdown_remaining_var = tk.DoubleVar(value=3600.0)
            self._countdown_remaining_slider = ttk.Scale(
                countdown_row, from_=0.0, to=3600.0,
                orient="horizontal",
                length=70,
                variable=self._countdown_remaining_var,
                command=self._countdown_remaining_on_drag,
            )
            self._countdown_remaining_slider.pack(
                side="left", padx=(4, 4)
            )
            self._countdown_dragging = False
            self._countdown_remaining_slider.bind(
                "<ButtonPress-1>", self._countdown_drag_start
            )
            self._countdown_remaining_slider.bind(
                "<ButtonRelease-1>", self._countdown_drag_end
            )
            self._countdown_remaining_label = ttk.Label(
                countdown_row, text="1h 00m", width=9
            )
            self._countdown_remaining_label.pack(side="left")
            self._countdown_status = ttk.Label(
                extra_panel,
                text="循环警报: 未启用。",
                justify="left",
            )

            reminder_row = ttk.Frame(extra_panel)
            reminder_row.pack(fill="x", pady=(4, 0))
            ttk.Label(reminder_row, text="提醒:").pack(side="left")
            self._sound_alert_var = tk.BooleanVar(value=True)
            ttk.Checkbutton(
                reminder_row,
                text="声音提醒",
                variable=self._sound_alert_var,
                command=self._shutdown_on_change,
            ).pack(side="left", padx=(4, 8))

            self._screen_blink_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                reminder_row,
                text="闪烁提醒",
                variable=self._screen_blink_var,
                command=self._shutdown_on_change,
            ).pack(side="left", padx=(0, 8))

            self._telegram_enabled_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                reminder_row,
                text="消息提醒",
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
                wraplength=620,
            )
            self._telegram_status.pack(anchor="w", pady=(4, 0))

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

            debug_frame = ttk.LabelFrame(col2, text="调试日志", padding=6)
            debug_frame.pack(fill="both", expand=True, pady=(10, 0))
            self._log_text = tk.Text(
                debug_frame,
                height=10,
                wrap="none",
                state="disabled",
                font=("Consolas", 9),
            )
            log_scroll = ttk.Scrollbar(
                debug_frame, orient="vertical", command=self._log_text.yview
            )
            self._log_text.configure(yscrollcommand=log_scroll.set)
            self._log_text.pack(side="left", fill="both", expand=True)
            log_scroll.pack(side="right", fill="y")

            root.after(0, self._poll)
            root.mainloop()
        except Exception:
            LOG.exception("debug UI stopped unexpectedly")
        finally:
            self._yolo_stop()
            self._root = None
            LOG.info("UI worker stopped")

    def _on_debug_window_close(self) -> None:
        """Close handler: remember the window geometry, then destroy."""
        root = self._root
        if root is not None:
            try:
                _save_window_geometry(root.winfo_geometry())
            except Exception:
                LOG.debug("could not save debug UI geometry on close", exc_info=True)
            root.destroy()

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
                _save_window_geometry(root.winfo_geometry())
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
        root = self._root
        if root is None:
            return
        if self.stop_event.is_set():
            root.destroy()
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
        self._drain_logs()
        self._refresh_automation_status()
        self._refresh_shutdown_status()
        self._refresh_countdown_status()
        self._refresh_telegram_status()
        self._poll_yolo_exit()
        root.after(self.refresh_ms, self._poll)

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
        if self.log_queue is None or not hasattr(self, "_log_text"):
            return
        messages: list[str] = []
        while True:
            try:
                messages.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if not messages:
            return
        self._log_text.configure(state="normal")
        self._log_text.insert("end", "\n".join(messages) + "\n")
        line_count = int(self._log_text.index("end-1c").split(".")[0])
        if line_count > 300:
            self._log_text.delete("1.0", f"{line_count - 300}.0")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _trim_log_lines(self, limit: int) -> None:
        """Keep only the latest ``limit`` lines in the debug log widget."""
        if not hasattr(self, "_log_text"):
            return
        try:
            self._log_text.configure(state="normal")
            line_count = int(self._log_text.index("end-1c").split(".")[0])
            if line_count > limit:
                self._log_text.delete("1.0", f"{line_count - limit}.0")
            self._log_text.configure(state="disabled")
        except Exception:
            pass

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
        minimap = snapshot.minimap_preview.copy()
        minimap.thumbnail((360, 260), Image.Resampling.NEAREST)
        name = snapshot.map_name_preview.copy()
        name.thumbnail((360, 90), Image.Resampling.NEAREST)
        if self._SHOW_MINIMAP_PREVIEW and hasattr(self, "_minimap_label"):
            self._photo_minimap = ImageTk.PhotoImage(minimap)
            self._photo_map_name = ImageTk.PhotoImage(name)
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

    def _record_endpoint(self, boundary: str) -> None:
        if self.patrol_controller is None:
            self._control_status.configure(text="巡逻控制器不可用。")
            return
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
            return
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
            return
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
        except (OSError, ValueError) as exc:
            LOG.warning("record rejected: layer=%s point=%s error=%s",
                        self.patrol_controller.selected_layer(), boundary, exc)
            self._control_status.configure(text=f"无法录制: {exc}")
            return
        labels = {
            "left_most_pos": "最左",
            "rope_pos": "绳索",
            "right_most_pos": "最右",
        }
        label = labels[boundary]
        self._unlocked_points.discard((recorded.layer, boundary))
        LOG.info("record locked: layer=%s point=%s x=%.6f y=%.6f frame=%s",
                 recorded.layer, boundary, recorded.x, recorded.y, snapshot.sequence)
        self._control_status.configure(
            text=(f"已录制 {recorded.layer} {label}: "
                  f"x={recorded.x:.6f}, y={recorded.y:.6f}")
        )
        self._refresh_patrol_controls()

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
                float(self._fixed_interval_var.get()), 1
            ),
            "random_gap_seconds": self._fixed_random_gap_seconds(),
            "attack_key": self._fixed_attack_key_var.get().strip(),
        }

    def _fixed_random_gap_seconds(self) -> float:
        """Return the clamped, one-decimal random-gap setting."""

        var = getattr(self, "_fixed_random_gap_var", None)
        raw = 0.1 if var is None else float(var.get())
        return round(max(0.0, min(self._FIXED_RANDOM_GAP_MAX, raw)), 1)

    def _fixed_adjust_random_gap(self, delta: float) -> None:
        """Adjust the random delay ceiling using fixed 0.1-second steps."""

        current = self._fixed_random_gap_seconds()
        value = round(
            max(0.0, min(
                self._FIXED_RANDOM_GAP_MAX,
                current + (self._FIXED_RANDOM_GAP_STEP if delta > 0
                           else -self._FIXED_RANDOM_GAP_STEP),
            )),
            1,
        )
        self._fixed_random_gap_var.set(value)
        self._fixed_on_change()

    def _fixed_on_change(self, _value: str = "") -> None:
        """Update labels, persist, and apply the fixed-attack settings live."""

        if not hasattr(self, "_fixed_interval_label"):
            return
        if (not self._YOLO_MONSTER_DETECTION_ENABLED
                and self._attack_mode_var.get() != "fixed"):
            self._attack_mode_var.set("fixed")
        interval = float(self._fixed_interval_var.get())
        random_gap = self._fixed_random_gap_seconds()
        if hasattr(self, "_fixed_random_gap_var"):
            self._fixed_random_gap_var.set(random_gap)
        self._fixed_interval_label.configure(text=f"{interval:.1f}s")
        if hasattr(self, "_fixed_random_gap_label"):
            self._fixed_random_gap_label.configure(text=f"{random_gap:.1f}s")
        if hasattr(self, "_fixed_range_label"):
            self._fixed_range_label.configure(
                text=(f"随机攻击间隔: ({interval:.1f}s, "
                      f"{interval + random_gap:.1f}s)  "
                      f"基础={interval:.1f}s + 随机=0.0~{random_gap:.1f}s")
            )
        if hasattr(self, "_fixed_key_button"):
            self._fixed_key_button.configure(
                text=self._fixed_attack_key_var.get()
            )
        data = self._fixed_collect_data()
        self._fixed_save_settings(data)
        self._fixed_apply_to_worker(data)
        self._fixed_refresh_grey()

    def _fixed_on_mode_change(self) -> None:
        """Attack mode radio changed: same as any other change."""

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
                text="固定攻击: 工作线程未接入 (无界面模式)。"
            )
            return
        mode = str(data.get("attack_mode", "fixed"))
        if not self._YOLO_MONSTER_DETECTION_ENABLED:
            mode = "fixed"
        worker.enabled = bool(mode == "fixed")
        worker.attack_interval = max(
            0.25, float(data.get("interval_seconds", 3.0))
        )
        worker.attack_jitter_seconds = max(
            0.0, float(data.get("random_gap_seconds", 0.1))
        )
        key = str(data.get("attack_key", "ctrl")).strip()
        if not worker.set_key(key):
            LOG.warning("fixed attack key %r unsupported; keeping %r",
                        key, worker.attack_key)

    def _fixed_refresh_grey(self) -> None:
        """Grey the YOLO panel + update status lines for the active mode."""

        fixed_mode = (
            not self._YOLO_MONSTER_DETECTION_ENABLED
            or str(self._attack_mode_var.get()) == "fixed"
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
                random_gap = self._fixed_random_gap_seconds()
                self._fixed_status.configure(
                    text=(f"固定攻击已启用 - 按键 "
                          f"{self._fixed_attack_key_var.get()}；"
                          f"基础 {interval:.1f}s；随机间差 {random_gap:.1f}s；"
                          f"范围 ({interval:.1f}s, "
                          f"{interval + random_gap:.1f}s)。"
                          "YOLO 怪物检测暂时停用。")
                )
            else:
                self._fixed_status.configure(
                    text="固定攻击未启用 - 使用 YOLO 检测模式。"
                )
        if hasattr(self, "_yolo_status"):
            if fixed_mode:
                self._yolo_status.configure(
                    text=("暂时停用 - 当前模型识别率不足；"
                          "恢复方法见 README.md。")
                )
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
                if mode in ("yolo", "fixed"):
                    self._attack_mode_var.set(mode)
            if not self._YOLO_MONSTER_DETECTION_ENABLED:
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
        self._key_capture_target = (button, var, previous_attr, on_change)
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
        button, var, previous_attr, on_change = target
        key = keysym_to_scan_key(str(getattr(event, "keysym", "")))
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
                text=f"{self._buff1_interval_var.get():.1f}min"
            )
        if hasattr(self, "_buff2_interval_label"):
            self._buff2_interval_label.configure(
                text=f"{self._buff2_interval_var.get():.1f}min"
            )
        if hasattr(self, "_buff1_key_button"):
            self._buff1_key_button.configure(text=self._buff1_key_var.get())
        if hasattr(self, "_buff2_key_button"):
            self._buff2_key_button.configure(text=self._buff2_key_var.get())
        data = {
            "hp_key": self._hp_key_var.get().strip(),
            "mp_key": self._mp_key_var.get().strip(),
            "hp_threshold": hp_percent,
            "mp_threshold": mp_percent,
            "hp_enabled": bool(self._hp_use_var.get()),
            "mp_enabled": bool(self._mp_use_var.get()),
            "buff1_key": self._buff1_key_var.get().strip(),
            "buff2_key": self._buff2_key_var.get().strip(),
            "buff1_interval": round(float(self._buff1_interval_var.get()), 1),
            "buff2_interval": round(float(self._buff2_interval_var.get()), 1),
            "buff1_enabled": bool(self._buff1_use_var.get()),
            "buff2_enabled": bool(self._buff2_use_var.get()),
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
            if "buff1_interval" in data:
                self._buff1_interval_var.set(float(data["buff1_interval"]))
            if "buff2_interval" in data:
                self._buff2_interval_var.set(float(data["buff2_interval"]))
            if "buff1_enabled" in data:
                self._buff1_use_var.set(bool(data["buff1_enabled"]))
            if "buff2_enabled" in data:
                self._buff2_use_var.set(bool(data["buff2_enabled"]))
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
        """Apply the drug settings to the StatusWorker's detector config."""

        worker = getattr(self, "status_worker", None)
        if worker is None:
            self._drug_status.configure(
                text="药品: 状态工作线程未接入 (无界面模式)。"
            )
            return
        try:
            config = apply_drug_settings(worker.detector.config, data)
            worker.detector.config = config
            self._drug_status.configure(
                text=(
                    f"药品: HP<{data['hp_threshold']}% 按键={data['hp_key']} "
                    f"({'开' if data['hp_enabled'] else '关'}) | "
                    f"MP<{data['mp_threshold']}% 按键={data['mp_key']} "
                    f"({'开' if data['mp_enabled'] else '关'})\n"
                    f"增益1: 按键={data['buff1_key']} 每 "
                    f"{data['buff1_interval']}分钟 "
                    f"({'开' if data['buff1_enabled'] else '关'}) | "
                    f"增益2: 按键={data['buff2_key']} 每 "
                    f"{data['buff2_interval']}分钟 "
                    f"({'开' if data['buff2_enabled'] else '关'})"
                )
            )
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
        return data

    def _shutdown_on_change(self, _value: str = "") -> None:
        """Update labels, persist, and apply the shutdown settings live."""

        if not hasattr(self, "_shutdown_hours_label"):
            return
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

        worker = getattr(self, "shutdown_worker", None)
        if worker is None:
            self._shutdown_status.configure(
                text="定时关闭: 工作线程未接入 (无界面模式)。"
            )
            return
        worker.enabled = bool(data.get("shutdown_enabled", False))
        worker.set_hours(float(data.get("shutdown_hours", 3.0)))
        # Other-player auto channel switch -> movement worker (live).
        mover = getattr(self, "movement_worker", None)
        if mover is not None:
            setter = getattr(mover, "set_other_player_check", None)
            if setter is not None:
                setter(bool(data.get("player_check_enabled", False)))
        character = getattr(self, "character_worker", None)
        if character is not None:
            setter = getattr(character, "set_disconnect_alert", None)
            if setter is not None:
                setter(bool(data.get("disconnect_alert_enabled", False)))
        lie_detector = getattr(self, "lie_detector_worker", None)
        if lie_detector is not None:
            setter = getattr(lie_detector, "set_enabled", None)
            if setter is not None:
                setter(bool(data.get("lie_alert_enabled", False)))
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
        if worker.enabled:
            self._shutdown_status.configure(
                text=f"定时关闭已启动: 游戏将在 "
                     f"{float(data.get('shutdown_hours', 3.0)):.1f}小时后关闭 "
                     f"(Alt+F4 后停止所有工作线程)。"
            )
        else:
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
        # Clear the active-entry identity before destroying widgets so a
        # destruction-induced FocusOut cannot recursively save/render.
        self._quick_edit_entry = None
        for child in frame.winfo_children():
            child.destroy()
        for index, message in enumerate(self._quick_messages):
            row = self._ttk.Frame(frame)
            row.pack(fill="x", pady=2)
            if edit_index == index:
                entry = self._ttk.Entry(row, width=42)
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
                button = self._ttk.Button(row, text=message)
                button.pack(side="left", fill="x", expand=True, padx=(0, 4))
                button.bind(
                    "<ButtonPress-1>",
                    lambda event, i=index: self._quick_message_press(i),
                )
                button.bind(
                    "<ButtonRelease-1>",
                    lambda event, i=index: self._quick_message_release(i),
                )
            delete_button = self._ttk.Button(row, text="×", width=3)
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
        if not self._copy_quick_message(index):
            return "break"
        sender = getattr(getattr(self, "status_worker", None), "key_sender", None)
        send = getattr(sender, "send_clipboard_message", None)
        if send is None:
            self._quick_message_status.configure(text="发送失败：游戏输入未接入。")
            return "break"
        try:
            if send() is False:
                raise OSError("无法聚焦游戏窗口")
            self._quick_message_status.configure(
                text=f"已发送：{self._quick_messages[index]}"
            )
        except Exception as exc:
            self._quick_message_status.configure(text=f"发送失败：{exc}")
        return "break"

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
        if hours:
            return f"{hours}h {minutes:02d}m {seconds:02d}s"
        return f"{minutes}m {seconds:02d}s"

    def _countdown_on_change(self, _value: str = "") -> None:
        """Persist/apply the repeating reminder interval and enabled state."""

        if not hasattr(self, "_countdown_interval_label"):
            return
        hours = max(0.1, float(self._countdown_interval_var.get()))
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

    def _countdown_apply_to_worker(self, data: dict) -> None:
        worker = getattr(self, "countdown_worker", None)
        if worker is None:
            self._countdown_status.configure(
                text="循环警报: 工作线程未接入 (无界面模式)。"
            )
            return
        hours = float(data.get("countdown_interval_hours", 1.0))
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

        if not bool(self._countdown_enabled_var.get()):
            return
        remaining = max(0.0, float(value))
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
            if hasattr(self, "_sound_alert_var"):
                self._sound_alert_var.set(bool(
                    data.get("sound_alert_enabled", True)
                ))
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
                    self._countdown_interval_var.set(float(
                        data["countdown_interval_hours"]
                    ))
        except (KeyError, TypeError, ValueError):
            LOG.warning("ignored malformed additional functions settings",
                        exc_info=True)
            return
        self._shutdown_on_change()
        if hasattr(self, "_countdown_enabled_var"):
            self._countdown_on_change()
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
            self._yolo_status.configure(
                text="YOLO 怪物检测暂时停用；恢复方法见 README.md。"
            )
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
            self._yolo_run_button.configure(state="normal")
            self._yolo_stop_button.configure(state="disabled")
            self._yolo_status.configure(text="YOLO 检测已停止。")
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
        self._yolo_run_button.configure(state="normal")
        self._yolo_stop_button.configure(state="disabled")
        self._yolo_status.configure(text="YOLO 检测已停止。")
        LOG.info("yolo detection stopped")

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
        key = (layer_name, boundary)
        saved_endpoint = self.patrol_controller.endpoint(layer_name, boundary)
        if not record_button_is_locked(saved_endpoint, key in self._unlocked_points):
            # 未锁定（空点/已解锁）：短按释放时已经处理录制。
            return
        self._unlocked_points.add(key)
        cleared = self.patrol_controller.clear_endpoint(layer_name, boundary)
        self._control_status.configure(
            text=(f"已解锁并清除 {layer_name} {boundary} 的录制"
                  + ("。" if cleared else "（无数据）。")
                  + " 现在短按即可录制当前位置。")
        )
        self._refresh_patrol_controls()

    def _start_patrol(self) -> None:
        if self.patrol_controller is None:
            self._control_status.configure(text="巡逻控制器不可用。")
            return
        if self.patrol_controller.is_enabled():
            return
        if not self.patrol_controller.can_start():
            self._control_status.configure(
                text=("无法开始: 每层至少录制一个巡逻点 (最左 / 绳索 / 最右)。"
                      "不录制任何点时将原地站立只进行攻击。")
            )
            return
        self._control_status.configure(text="正在选择游戏窗口…")
        if self._root is not None:
            self._root.update_idletasks()
        if self.on_patrol_start is not None:
            try:
                self.on_patrol_start()
            except OSError as exc:
                self._control_status.configure(
                    text=f"无法开始: 游戏窗口选择失败: {exc}"
                )
                return
        self.patrol_controller.set_enabled(True)
        # 攻击模式为「YOLO 检测」时，开始巡逻自动启动 YOLO 检测
        # （已在运行则跳过；缺依赖/模型会在状态栏给出提示）。
        if (getattr(self, "_attack_mode_var", None) is not None
                and self._attack_mode_var.get() == "yolo"):
            self._yolo_start()
        self._refresh_patrol_controls()
        self._control_status.configure(text="巡逻已开始。")

    def _stop_patrol(self) -> None:
        if self.patrol_controller is None:
            return
        self.patrol_controller.set_enabled(False)
        if self.on_patrol_stop is not None:
            self.on_patrol_stop()
        # Stopping patrol also stops the YOLO attack subprocess: in stand-still
        # mode the character stands and the YOLO executor attacks, so without
        # this the character would keep attacking after Stop Patrol.
        self._yolo_stop()
        # 每次停止巡逻：调试日志只保留最新 100 条。
        self._trim_log_lines(100)
        self._refresh_patrol_controls()
        self._control_status.configure(text="巡逻已停止。")

    def _add_layer_above(self) -> None:
        if self.patrol_controller is None:
            self._control_status.configure(text="巡逻控制器不可用。")
            return
        try:
            layer_name = self.patrol_controller.add_layer_above()
        except (OSError, ValueError) as exc:
            self._control_status.configure(text=f"无法添加楼层: {exc}")
            return
        self._control_status.configure(
            text=(f"已选择 {layer_name}。请手动移动到该层并录制任意巡逻点 "
                  "(最左 / 绳索 / 最右)。巡逻已暂停。")
        )
        self._refresh_patrol_controls()

    def _reset_recording(self) -> None:
        if self.patrol_controller is None:
            return
        # Reset is deliberately immediate: no confirmation dialog or hint.
        # Stop and release live input before mutating the recording.
        self.patrol_controller.set_enabled(False)
        if self.on_patrol_stop is not None:
            self.on_patrol_stop()
        try:
            self.patrol_controller.reset_recording()
            # A reset starts a fresh recording for the current map; adopt the
            # map name now on disk (it may have been edited or re-identified
            # since the UI started) so identity checks use the current name.
            self.configured_map_name = self.patrol_controller.map_name()
            if getattr(self, "structure_tracker", None) is not None:
                self.structure_tracker.reset(delete_reference=True)
            reset_geometry = getattr(
                getattr(self, "detector", None), "reset_geometry", None
            )
            if callable(reset_geometry):
                reset_geometry()
            if getattr(self, "map_identity_store", None) is not None:
                self.map_identity_store.remove(self.configured_map_name)
        except OSError as exc:
            self._control_status.configure(text=f"无法重置录制: {exc}")
            return
        self._unlocked_points.clear()
        self._layer_row_names = ()
        self._refresh_patrol_controls()
        self._control_status.configure(
            text="录制已重置。图层1为空；巡逻已停止。"
        )

    def _refresh_patrol_controls(self) -> None:
        if self.patrol_controller is None:
            self._start_patrol_button.configure(state="disabled")
            self._stop_patrol_button.configure(state="disabled")
            self._add_layer_button.configure(state="disabled")
            self._reset_recording_button.configure(state="disabled")
            return
        running = self.patrol_controller.is_enabled()
        can_start = self.patrol_controller.can_start()
        selected = self.patrol_controller.selected_layer()
        snapshot = self.patrol_controller.snapshot()
        route = snapshot.route_order
        layer_names = list(route)
        layer_names.extend(name for name in snapshot.layers if name not in layer_names)
        layer_names = list(layer_display_order(layer_names))
        self._update_patrol_range_combos(layer_names)
        self._ensure_layer_rows(tuple(layer_names))
        button_labels = {
            "left_most_pos": "最左",
            "rope_pos": "绳索",
            "right_most_pos": "最右",
        }
        final_name = self.patrol_controller.final_layer_name()
        for layer_name in layer_names:
            # Keep this label compact so all three recording buttons retain
            # enough width in the intentionally narrower controls column.
            # The patrol range comboboxes carry selection/top-floor details.
            self._layer_labels[layer_name].configure(
                text=self._patrol_display_name(layer_name)
            )
            for point, button_label in button_labels.items():
                final_rope = point == "rope_pos" and layer_name == final_name
                recorded = self.patrol_controller.endpoint(layer_name, point)
                key = (layer_name, point)
                locked = not final_rope and record_button_is_locked(
                    recorded, key in self._unlocked_points
                )
                if locked and recorded is not None:
                    text = (
                        f"🔒 {button_label}\n"
                        f"{recorded_coordinate_text(recorded.x, recorded.y)}"
                    )
                else:
                    text = (
                        "绳索不可用 (最顶层)"
                        if final_rope else f"录制 {button_label}"
                    )
                self._record_buttons[(layer_name, point)].configure(
                    text=text,
                    state="disabled" if final_rope else "normal",
                    style="Locked.TButton" if locked else "TButton",
                )
                if point == "rope_pos":
                    self._rope_tooltips[layer_name].set_enabled(final_rope)
        start_state, stop_state = patrol_button_states(running, can_start)
        self._start_patrol_button.configure(state=start_state)
        self._stop_patrol_button.configure(state=stop_state)
        self._add_layer_button.configure(state="normal")
        self._reset_recording_button.configure(state="normal")

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
        for combo in (self._patrol_start_combo, self._patrol_end_combo):
            combo.configure(values=display_values)
        start, end = self.patrol_controller.patrol_range()
        self._patrol_start_var.set(self._patrol_display_name(start))
        self._patrol_end_var.set(self._patrol_display_name(end))

    def _patrol_display_name(self, layer_name: str) -> str:
        """UI display for a floor name (``layer2`` -> ``楼层2``)."""
        match = re.search(r"(\d+)$", layer_name)
        return f"楼层{match.group(1)}" if match else layer_name

    def _patrol_name_from_display(self, display: str) -> str:
        """Reverse of ``_patrol_display_name`` (``楼层2`` -> ``layer2``)."""
        match = re.search(r"(\d+)$", display)
        return f"layer{match.group(1)}" if match else display

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
        self._layer_row_names = layer_names
        ttk = self._ttk
        point_labels = (
            ("left_most_pos", "最左"),
            ("rope_pos", "绳索"),
            ("right_most_pos", "最右"),
        )
        for layer_name in layer_names:
            row = ttk.Frame(self._layer_rows_frame)
            row.pack(fill="x", pady=3)
            # ``layer1`` previously reserved 18 text columns, leaving a large
            # blank strip and clipping the action buttons. ``楼层N`` fits in
            # seven columns, including room for multi-digit floor numbers.
            label = ttk.Label(row, width=7)
            label.pack(side="left", padx=(0, 2))
            self._layer_labels[layer_name] = label
            for point_name, point_label in point_labels:
                button = ttk.Button(
                    row,
                    text=f"录制 {point_label}",
                )
                button.pack(side="left", fill="x", expand=True, padx=(0, 5))
                # 长按 1 秒 = 解锁并清除该点录制；短按 = 录制（仅对空点/
                # 已解锁点生效，已录制的点短按无效）。
                button.bind(
                    "<ButtonPress-1>",
                    lambda event, layer=layer_name, point=point_name: (
                        self._record_button_press(layer, point)
                    ),
                )
                button.bind(
                    "<ButtonRelease-1>",
                    lambda event, layer=layer_name, point=point_name: (
                        self._record_button_release(layer, point)
                    ),
                )
                self._record_buttons[(layer_name, point_name)] = button
                if point_name == "rope_pos":
                    self._rope_tooltips[layer_name] = HoverTooltip(
                        button, rope_unavailable_hint()
                    )


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
