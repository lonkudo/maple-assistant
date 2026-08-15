"""Independent Tk debug dashboard fed by the capture frame bus."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import queue
import threading
from typing import Any, Optional

from PIL import Image, ImageTk

from minimap_detector import Box, MinimapDetection, MinimapDetector


LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class DebugSnapshot:
    sequence: int
    captured_at: datetime
    client_size: tuple[int, int]
    detection: MinimapDetection
    minimap_preview: Image.Image
    map_name_preview: Image.Image
    configured_map_name: str


def build_debug_snapshot(
    frame: Any,
    detector: MinimapDetector,
    configured_map_name: str = "",
) -> DebugSnapshot:
    """Pure frame-to-view-model conversion, independently testable from Tk."""

    detection = detector.detect(frame.image)
    return DebugSnapshot(
        sequence=frame.sequence,
        captured_at=frame.captured_at,
        client_size=frame.image.size,
        detection=detection,
        minimap_preview=frame.image.crop(detection.window_box),
        map_name_preview=frame.image.crop(detection.map_name_box),
        configured_map_name=configured_map_name,
    )


def _box_text(box: Box) -> str:
    left, top, right, bottom = box
    return f"x={left}, y={top}, w={right-left}, h={bottom-top}"


class UiWorker(threading.Thread):
    """Own a Tk window on its own thread; never sends gameplay input."""

    def __init__(
        self,
        frame_queue: "queue.Queue[Any]",
        stop_event: threading.Event,
        detector: MinimapDetector,
        *,
        configured_map_name: str = "",
        refresh_ms: int = 100,
    ) -> None:
        super().__init__(name="ui-worker", daemon=True)
        self.frame_queue = frame_queue
        self.stop_event = stop_event
        self.detector = detector
        self.configured_map_name = configured_map_name
        self.refresh_ms = max(30, int(refresh_ms))
        self.last_snapshot: Optional[DebugSnapshot] = None
        self._root: Any = None
        self._photo_minimap: Any = None
        self._photo_map_name: Any = None

    def run(self) -> None:
        try:
            import tkinter as tk
            from tkinter import ttk

            root = tk.Tk()
            self._root = root
            root.title("Maple Assistant Debug UI")
            root.geometry("620x620")
            root.minsize(520, 500)
            root.protocol("WM_DELETE_WINDOW", root.destroy)

            container = ttk.Frame(root, padding=12)
            container.pack(fill="both", expand=True)
            title = ttk.Label(container, text="Maple Assistant", font=("Segoe UI", 16, "bold"))
            title.pack(anchor="w")
            ttk.Label(container, text="OpenCV minimap detection · read-only UI").pack(anchor="w")

            info = ttk.LabelFrame(container, text="Detection", padding=10)
            info.pack(fill="x", pady=(12, 8))
            self._info_label = ttk.Label(info, text="Waiting for first frame…", justify="left")
            self._info_label.pack(anchor="w")

            ttk.Label(container, text="Detected minimap").pack(anchor="w")
            self._minimap_label = ttk.Label(container)
            self._minimap_label.pack(anchor="w", pady=(4, 10))
            ttk.Label(container, text="Map-name region").pack(anchor="w")
            self._map_name_label = ttk.Label(container)
            self._map_name_label.pack(anchor="w", pady=(4, 0))

            root.after(0, self._poll)
            root.mainloop()
        except Exception:
            LOG.exception("debug UI stopped unexpectedly")
        finally:
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
                    latest, self.detector, self.configured_map_name
                )
                self._render(self.last_snapshot)
            except Exception:
                LOG.exception("could not update debug UI")
        root.after(self.refresh_ms, self._poll)

    def _render(self, snapshot: DebugSnapshot) -> None:
        detection = snapshot.detection
        recognized_name = detection.map_name or "OCR adapter not configured"
        self._info_label.configure(text=(
            f"Frame: {snapshot.sequence}\n"
            f"Captured: {snapshot.captured_at.astimezone().strftime('%H:%M:%S.%f')[:-3]}\n"
            f"Client: {snapshot.client_size[0]} × {snapshot.client_size[1]} px\n"
            f"Detector: {detection.source}  confidence={detection.confidence:.3f}\n"
            f"Minimap: {_box_text(detection.window_box)}\n"
            f"Analysis: {_box_text(detection.analysis_box)}\n"
            f"Map-name crop: {_box_text(detection.map_name_box)}\n"
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


__all__ = ["DebugSnapshot", "UiWorker", "build_debug_snapshot"]
