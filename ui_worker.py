"""Independent Tk debug dashboard fed by the capture frame bus."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
import queue
import threading
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
from PIL import Image, ImageTk

from marker_detector import DiamondSizeTracker, detect_yellow_diamond
from map_identity import MapIdentityStore
from map_structure_tracker import MapStructureTracker
from minimap_detector import Box, MinimapDetection, MinimapDetector
from patrol_control import CoordinateLayout, PatrolController


LOG = logging.getLogger(__name__)


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
    """Return Tk states for the separate Start and Stop patrol buttons."""

    return (
        "normal" if can_start and not running else "disabled",
        "normal" if running else "disabled",
    )


def layer_display_order(layer_names: list[str]) -> tuple[str, ...]:
    """Display the highest/newest layer above the lower layers."""

    return tuple(reversed(layer_names))


def rope_unavailable_hint() -> str:
    return "Add a layer to enable Rope recording."


def record_button_is_locked(saved_endpoint: Any, explicitly_unlocked: bool) -> bool:
    """Saved endpoints lock automatically unless the user explicitly unlocks."""

    return saved_endpoint is not None and not explicitly_unlocked


class UiWorker(threading.Thread):
    """Own the independent UI loop; Tk requires ``run`` on Python's main thread."""

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
        on_patrol_start: Optional[Callable[[], None]] = None,
        on_patrol_stop: Optional[Callable[[], None]] = None,
        on_capture_now: Optional[Callable[[], Any]] = None,
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
        self.on_patrol_start = on_patrol_start
        self.on_patrol_stop = on_patrol_stop
        self.on_capture_now = on_capture_now
        self.log_queue = log_queue
        self.automation_active_event = automation_active_event
        self._yolo_process: Any = None
        self.last_snapshot: Optional[DebugSnapshot] = None
        self._root: Any = None
        self._photo_minimap: Any = None
        self._photo_map_name: Any = None
        self._record_buttons: dict[tuple[str, str], Any] = {}
        self._rope_tooltips: dict[str, HoverTooltip] = {}
        self._layer_labels: dict[str, Any] = {}
        self._layer_row_names: tuple[str, ...] = ()
        # Only explicit unlocks need UI state. Locking itself is derived from
        # the controller's saved endpoint, so dynamically created rows behave
        # identically to rows present at startup.
        self._unlocked_points: set[tuple[str, str]] = set()

    def run(self) -> None:
        try:
            import tkinter as tk
            from tkinter import ttk
            self._ttk = ttk

            root = tk.Tk()
            self._root = root
            root.title("Maple Assistant Debug UI")
            screen_width = root.winfo_screenwidth()
            # Default tall layout on the left/secondary monitor; the position
            # is negative so the window opens on the monitor left of primary.
            window_height = 1600
            root.geometry(f"700x{window_height}-1000-500")
            root.minsize(520, 560)
            root.protocol("WM_DELETE_WINDOW", root.destroy)
            root.attributes("-topmost", True)
            root.after(1500, lambda: root.attributes("-topmost", False))

            container = ttk.Frame(root, padding=12)
            container.pack(fill="both", expand=True)
            title = ttk.Label(container, text="Maple Assistant", font=("Segoe UI", 16, "bold"))
            title.pack(anchor="w")
            ttk.Label(container, text="OpenCV minimap detection · patrol controls").pack(anchor="w")

            controls = ttk.LabelFrame(container, text="Layer calibration and patrol", padding=10)
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
                action_row, text="Start Patrol", command=self._start_patrol
            )
            self._start_patrol_button.pack(side="left", padx=(0, 8))
            self._stop_patrol_button = ttk.Button(
                action_row, text="Stop Patrol", command=self._stop_patrol
            )
            self._stop_patrol_button.pack(side="left", padx=(0, 8))
            self._add_layer_button = ttk.Button(
                action_row, text="Add Layer", command=self._add_layer_above
            )
            self._add_layer_button.pack(side="left", padx=(0, 8))
            self._reset_recording_button = ttk.Button(
                action_row, text="Reset Recording", command=self._reset_recording
            )
            self._reset_recording_button.pack(side="left")
            self._layer_rows_frame = ttk.Frame(controls)
            self._layer_rows_frame.pack(fill="x")
            self._control_status = ttk.Label(
                controls,
                text="Record Left, Rope, and Right; then add the layer above.",
            )
            self._control_status.pack(anchor="w", pady=(8, 0))
            self._automation_status_label = ttk.Label(controls)
            self._automation_status_label.pack(anchor="w", pady=(5, 0))
            self._refresh_patrol_controls()

            info = ttk.LabelFrame(container, text="Detection", padding=10)
            info.pack(fill="x", pady=(0, 8))
            self._info_label = ttk.Label(info, text="Waiting for first frame…", justify="left")
            self._info_label.pack(anchor="w")

            yolo_panel = ttk.LabelFrame(container, text="YOLO detection (maplestory-worlds-automation)", padding=10)
            yolo_panel.pack(fill="x", pady=(0, 8))
            yolo_row = ttk.Frame(yolo_panel)
            yolo_row.pack(fill="x")
            ttk.Label(yolo_row, text="Threshold:").pack(side="left", padx=(0, 6))
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
                yolo_row, text="Run", command=self._yolo_start
            )
            self._yolo_run_button.pack(side="left", padx=(0, 8))
            self._yolo_stop_button = ttk.Button(
                yolo_row, text="Stop", command=self._yolo_stop, state="disabled"
            )
            self._yolo_stop_button.pack(side="left", padx=(0, 8))
            # Show-detection toggle: grey/inactive by default; only when
            # activated does Run open the visible detection window.
            self._yolo_show_var = tk.BooleanVar(value=False)
            self._yolo_show_button = ttk.Checkbutton(
                yolo_row,
                text="Show Detection",
                variable=self._yolo_show_var,
                command=self._yolo_sync_show_button,
            )
            self._yolo_show_button.pack(side="left")
            self._yolo_show_button.configure(style="Off.TCheckbutton")
            # Save configuration: persist the current YOLO panel values so
            # they are restored next launch (no need to re-tune every time).
            self._yolo_save_button = ttk.Button(
                yolo_row, text="Save Config", command=self._yolo_save_config
            )
            self._yolo_save_button.pack(side="left", padx=(8, 0))
            # Auto-attack row: toggle + attack key, on its own line so it is
            # always visible.  Grey/inactive by default; when activated the
            # YOLO process faces the target and presses the attack key.
            attack_row = ttk.Frame(yolo_panel)
            attack_row.pack(fill="x", pady=(6, 0))
            self._yolo_attack_var = tk.BooleanVar(value=False)
            self._yolo_attack_button = ttk.Checkbutton(
                attack_row,
                text="Auto Attack",
                variable=self._yolo_attack_var,
                command=self._yolo_sync_show_button,
            )
            self._yolo_attack_button.pack(side="left")
            self._yolo_attack_button.configure(style="Off.TCheckbutton")
            ttk.Label(attack_row, text="Attack Key:").pack(
                side="left", padx=(10, 4)
            )
            self._yolo_attack_key_var = tk.StringVar(value="ctrl")
            attack_key_entry = ttk.Entry(
                attack_row, textvariable=self._yolo_attack_key_var, width=8
            )
            attack_key_entry.pack(side="left", padx=(0, 8))
            attack_key_entry.bind(
                "<KeyRelease>", self._yolo_on_threshold_change
            )
            # Attack range: horizontal slider (progress-bar style) that sets the
            # width of the attack range line drawn on the detection window.
            range_row = ttk.Frame(yolo_panel)
            range_row.pack(fill="x", pady=(6, 0))
            ttk.Label(range_row, text="Attack Range:").pack(
                side="left", padx=(0, 6)
            )
            self._yolo_attack_range_var = tk.IntVar(value=800)
            self._yolo_attack_range_slider = ttk.Scale(
                range_row,
                from_=200,
                to=2000,
                orient="horizontal",
                variable=self._yolo_attack_range_var,
                command=self._yolo_on_range_change,
            )
            self._yolo_attack_range_slider.pack(side="left", fill="x",
                                                expand=True, padx=(0, 8))
            self._yolo_attack_range_label = ttk.Label(
                range_row, text="800 px", width=8
            )
            self._yolo_attack_range_label.pack(side="left")
            # Detection zone size: width and height sliders (progress-bar
            # style) that scale the detection area as a fraction of the frame.
            zone_row = ttk.Frame(yolo_panel)
            zone_row.pack(fill="x", pady=(4, 0))
            ttk.Label(zone_row, text="Zone Width:").pack(side="left", padx=(0, 6))
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
            ttk.Label(zone_row2, text="Zone Height:").pack(side="left", padx=(0, 6))
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
            ttk.Label(zone_row3, text="Zone Shift Y:").pack(side="left", padx=(0, 6))
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
            self._yolo_status = ttk.Label(
                yolo_panel, text="YOLO detection stopped.", justify="left"
            )
            self._yolo_status.pack(anchor="w", pady=(6, 0))
            # Restore previously saved YOLO panel settings (threshold, ranges).
            self._yolo_load_settings()

            ttk.Label(container, text="Detected minimap").pack(anchor="w")
            self._minimap_label = ttk.Label(container)
            self._minimap_label.pack(anchor="w", pady=(4, 10))
            ttk.Label(container, text="Map-name region").pack(anchor="w")
            self._map_name_label = ttk.Label(container)
            self._map_name_label.pack(anchor="w", pady=(4, 0))

            debug_frame = ttk.LabelFrame(container, text="Debug log", padding=6)
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
        root.after(self.refresh_ms, self._poll)

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
            text = "Automation: ACTIVE — game window selected"
        elif patrol_running:
            text = "Automation: PAUSED — select the game window to resume"
        else:
            text = "Automation: stopped"
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
        if line_count > 250:
            self._log_text.delete("1.0", f"{line_count - 250}.0")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

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
        self._photo_minimap = ImageTk.PhotoImage(minimap)
        self._photo_map_name = ImageTk.PhotoImage(name)
        self._minimap_label.configure(image=self._photo_minimap)
        self._map_name_label.configure(image=self._photo_map_name)

    def _record_endpoint(self, boundary: str) -> None:
        if self.patrol_controller is None:
            self._control_status.configure(text="Patrol controller is unavailable.")
            return
        snapshot = self.last_snapshot
        if self.on_capture_now is not None:
            self._control_status.configure(text="Capturing current position…")
            if self._root is not None:
                self._root.update_idletasks()
            try:
                fresh_frame = self.on_capture_now()
                snapshot = build_debug_snapshot(
                    fresh_frame,
                    self.detector,
                    self.configured_map_name,
                    self.diamond_size_tracker,
                    self.structure_tracker,
                )
                self.last_snapshot = snapshot
                self._render(snapshot)
            except Exception as exc:
                LOG.exception("immediate recording capture failed")
                self._control_status.configure(
                    text=f"Cannot record: immediate capture failed: {exc}"
                )
                return
        if snapshot is None or snapshot.player_x is None or snapshot.player_y is None:
            self._control_status.configure(
                text="Cannot record: yellow diamond is not detected in the latest frame."
            )
            return
        try:
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
            self._control_status.configure(text=f"Cannot record: {exc}")
            return
        labels = {
            "left_most_pos": "Left-most",
            "rope_pos": "Rope",
            "right_most_pos": "Right-most",
        }
        label = labels[boundary]
        self._unlocked_points.discard((recorded.layer, boundary))
        LOG.info("record locked: layer=%s point=%s x=%.6f y=%.6f frame=%s",
                 recorded.layer, boundary, recorded.x, recorded.y, snapshot.sequence)
        self._control_status.configure(
            text=(f"Recorded {recorded.layer} {label}: "
                  f"x={recorded.x:.6f}, y={recorded.y:.6f}")
        )
        self._refresh_patrol_controls()

    def _yolo_settings_path(self) -> Path:
        """JSON file holding the YOLO panel settings."""

        return Path(__file__).resolve().parent / "yolo_detection_settings.json"

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
                self._yolo_attack_range_var.set(int(data["attack_range"]))
            if "zone_width" in data:
                self._yolo_zone_w_var.set(int(data["zone_width"]))
            if "zone_height" in data:
                self._yolo_zone_h_var.set(int(data["zone_height"]))
            if "zone_shift_y" in data:
                self._yolo_zone_shift_y_var.set(int(data["zone_shift_y"]))
            if "show_detection" in data:
                self._yolo_show_var.set(bool(data["show_detection"]))
            if "auto_attack" in data:
                if hasattr(self, "_yolo_attack_var"):
                    self._yolo_attack_var.set(bool(data["auto_attack"]))
            if "attack_key" in data:
                if hasattr(self, "_yolo_attack_key_var"):
                    self._yolo_attack_key_var.set(str(data["attack_key"]))
        except (KeyError, TypeError, ValueError):
            LOG.warning("ignored malformed yolo settings", exc_info=True)
            return
        # Refresh the slider labels to match the loaded values.
        self._yolo_on_range_change()
        self._yolo_on_zone_change()
        self._yolo_sync_show_button()
        LOG.info("yolo settings loaded from %s", self._yolo_settings_path())

    def _yolo_save_settings(self) -> None:
        """Persist current YOLO panel values to the local JSON file."""

        data = {
            "threshold": float(self._yolo_threshold_var.get()),
            "attack_range": int(self._yolo_attack_range_var.get()),
            "zone_width": int(self._yolo_zone_w_var.get()),
            "zone_height": int(self._yolo_zone_h_var.get()),
            "zone_shift_y": int(self._yolo_zone_shift_y_var.get()),
            "show_detection": bool(self._yolo_show_var.get()),
            "auto_attack": bool(
                self._yolo_attack_var.get()
                if hasattr(self, "_yolo_attack_var") else False
            ),
            "attack_key": (
                self._yolo_attack_key_var.get().strip()
                if hasattr(self, "_yolo_attack_key_var") else "ctrl"
            ),
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
        value = float(self._yolo_threshold_var.get())
        self._yolo_threshold_label.configure(text=f"{value:.2f}")

    def _yolo_on_range_change(self, _value: str = "") -> None:
        """Update the attack-range label as the slider moves."""

        if not hasattr(self, "_yolo_attack_range_label"):
            return
        value = int(self._yolo_attack_range_var.get())
        self._yolo_attack_range_label.configure(text=f"{value} px")

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

        if self._yolo_process is not None and self._yolo_process.poll() is None:
            self._yolo_status.configure(text="YOLO detection is already running.")
            return
        threshold = 0.4
        try:
            threshold = float(self._yolo_threshold_var.get())
        except (ValueError, TypeError):
            self._yolo_threshold_var.set(0.4)
            threshold = 0.4
        yolo_root = Path(__file__).resolve().parent / "yolo-detection"
        python = yolo_root / "venv313" / "Scripts" / "python.exe"
        script = yolo_root / "live_view.py"
        if not python.is_file() or not script.is_file():
            self._yolo_status.configure(
                text=f"YOLO project not found at {yolo_root} — check paths."
            )
            return
        import subprocess

        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):  # Windows: no console window
            creationflags = subprocess.CREATE_NO_WINDOW
        cmd = [str(python), str(script), "--threshold", f"{threshold}"]
        # Always publish YOLO rope state: the patrol worker uses it to gate
        # the inner-gap jump on the real screen gap.
        cmd.extend(["--rope-state", str(
            Path(__file__).resolve().parent / "work" / "rope_state.json"
        )])
        if not self._yolo_show_var.get():
            cmd.append("--no-show")
        if hasattr(self, "_yolo_attack_var") and self._yolo_attack_var.get():
            cmd.append("--attack")
            attack_key = "ctrl"
            if hasattr(self, "_yolo_attack_key_var"):
                attack_key = (self._yolo_attack_key_var.get().strip()
                              or "ctrl")
            cmd.extend(["--attack-key", attack_key])
            cmd.extend(["--attack-log",
                        str(yolo_root / "attack.log")])
            # Share the attack state file with the patrol worker so patrol
            # movement pauses while a target is active (attack priority).
            cmd.extend(["--attack-state", str(
                Path(__file__).resolve().parent / "work" / "attack_state.json"
            )])
        attack_range = int(self._yolo_attack_range_var.get())
        cmd.extend(["--attack-range", f"{attack_range}"])
        zone_w = max(0.1, min(1.0, int(self._yolo_zone_w_var.get()) / 100.0))
        zone_h = max(0.1, min(1.0, int(self._yolo_zone_h_var.get()) / 100.0))
        cmd.extend(["--zone-width", f"{zone_w:.2f}",
                    "--zone-height", f"{zone_h:.2f}"])
        shift_y = max(-0.5, min(0.5, int(self._yolo_zone_shift_y_var.get()) / 100.0))
        cmd.extend(["--zone-shift-y", f"{shift_y:.2f}"])
        self._yolo_process = subprocess.Popen(
            cmd,
            cwd=str(yolo_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self._yolo_run_button.configure(state="disabled")
        self._yolo_stop_button.configure(state="normal")
        mode = "visible window" if self._yolo_show_var.get() else "headless"
        attack = ("auto-attack ON" if hasattr(self, "_yolo_attack_var")
                  and self._yolo_attack_var.get() else "attack OFF")
        self._yolo_status.configure(
            text=f"YOLO detection running ({mode}, {attack}, "
                 f"threshold {threshold:.2f}). Press Stop to terminate."
        )
        LOG.info("yolo detection started threshold=%.2f show=%s pid=%s",
                 threshold, self._yolo_show_var.get(), self._yolo_process.pid)

    def _yolo_save_config(self) -> None:
        """Persist the current YOLO panel values and confirm on screen."""

        self._yolo_save_settings()
        if hasattr(self, "_yolo_status"):
            self._yolo_status.configure(
                text="Configuration saved - it will be restored next launch."
            )

    def _yolo_stop(self) -> None:
        """Terminate the YOLO detection subprocess."""

        proc = self._yolo_process
        if proc is None or proc.poll() is not None:
            self._yolo_process = None
            self._yolo_run_button.configure(state="normal")
            self._yolo_stop_button.configure(state="disabled")
            self._yolo_status.configure(text="YOLO detection stopped.")
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
        self._yolo_run_button.configure(state="normal")
        self._yolo_stop_button.configure(state="disabled")
        self._yolo_status.configure(text="YOLO detection stopped.")
        LOG.info("yolo detection stopped")

    def _yolo_sync_show_button(self) -> None:
        """Toggle the grey/inactive look based on the checked state."""

        if not hasattr(self, "_yolo_show_button"):
            return
        if self._yolo_show_var.get():
            self._yolo_show_button.configure(style="TCheckbutton")
        else:
            self._yolo_show_button.configure(style="Off.TCheckbutton")
        if hasattr(self, "_yolo_attack_button"):
            if self._yolo_attack_var.get():
                self._yolo_attack_button.configure(style="TCheckbutton")
            else:
                self._yolo_attack_button.configure(style="Off.TCheckbutton")
        running = (self._yolo_process is not None
                   and self._yolo_process.poll() is None)
        if running:
            self._yolo_status.configure(
                text="Stop detection before changing Show Detection; "
                     "restart Run to apply."
            )

    def _record_or_unlock(self, layer_name: str, boundary: str) -> None:
        """Use one embedded button to unlock, then record and relock."""

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
            self._unlocked_points.add(key)
            self._control_status.configure(
                text=(f"Unlocked {layer_name} {boundary}. Click the same Record "
                      "button again to save the current position.")
            )
            self._refresh_patrol_controls()
            return
        self._unlocked_points.discard(key)
        self._record_endpoint(boundary)

    def _start_patrol(self) -> None:
        if self.patrol_controller is None:
            self._control_status.configure(text="Patrol controller is unavailable.")
            return
        if self.patrol_controller.is_enabled():
            return
        if not self.patrol_controller.can_start():
            self._control_status.configure(
                text=("Cannot start: record adaptive Left/Right on every layer "
                      "and Rope on every layer except the final layer.")
            )
            return
        self._control_status.configure(text="Selecting game window…")
        if self._root is not None:
            self._root.update_idletasks()
        if self.on_patrol_start is not None:
            try:
                self.on_patrol_start()
            except OSError as exc:
                self._control_status.configure(
                    text=f"Cannot start: game window selection failed: {exc}"
                )
                return
        self.patrol_controller.set_enabled(True)
        self._refresh_patrol_controls()
        self._control_status.configure(text="Patrol started.")

    def _stop_patrol(self) -> None:
        if self.patrol_controller is None:
            return
        self.patrol_controller.set_enabled(False)
        if self.on_patrol_stop is not None:
            self.on_patrol_stop()
        self._refresh_patrol_controls()
        self._control_status.configure(text="Patrol stopped.")

    def _add_layer_above(self) -> None:
        if self.patrol_controller is None:
            self._control_status.configure(text="Patrol controller is unavailable.")
            return
        try:
            layer_name = self.patrol_controller.add_layer_above()
        except (OSError, ValueError) as exc:
            self._control_status.configure(text=f"Cannot add layer: {exc}")
            return
        self._control_status.configure(
            text=(f"Selected {layer_name}. Move there manually and record "
                  "Left, Rope, and Right. Patrol is paused.")
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
            if getattr(self, "map_identity_store", None) is not None:
                self.map_identity_store.remove(self.configured_map_name)
        except OSError as exc:
            self._control_status.configure(text=f"Cannot reset recording: {exc}")
            return
        self._unlocked_points.clear()
        self._layer_row_names = ()
        self._refresh_patrol_controls()
        self._control_status.configure(
            text="Recording reset. Layer 1 is empty; patrol is stopped."
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
        self._ensure_layer_rows(tuple(layer_names))
        button_labels = {
            "left_most_pos": "Left-most",
            "rope_pos": "Rope",
            "right_most_pos": "Right-most",
        }
        final_name = self.patrol_controller.final_layer_name()
        for layer_name in layer_names:
            suffix = "  ← selected" if layer_name == selected else ""
            if layer_name == final_name:
                suffix += "  (final)"
            self._layer_labels[layer_name].configure(text=f"{layer_name}{suffix}")
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
                        f"x={recorded.x:.6f} y={recorded.y:.6f}"
                    )
                else:
                    text = (
                        "Rope unavailable (final)"
                        if final_rope else f"Record {button_label}"
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
            ("left_most_pos", "Left-most"),
            ("rope_pos", "Rope"),
            ("right_most_pos", "Right-most"),
        )
        for layer_name in layer_names:
            row = ttk.Frame(self._layer_rows_frame)
            row.pack(fill="x", pady=3)
            label = ttk.Label(row, width=18)
            label.pack(side="left", padx=(0, 6))
            self._layer_labels[layer_name] = label
            for point_name, point_label in point_labels:
                button = ttk.Button(
                    row,
                    text=f"Record {point_label}",
                    command=lambda layer=layer_name, point=point_name: (
                        self._record_or_unlock(layer, point)
                    ),
                )
                button.pack(side="left", fill="x", expand=True, padx=(0, 5))
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
    "tooltip_cursor_top_right_position",
]
