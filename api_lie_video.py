"""测试api (video): play a video, send it to the real backend at 5 fps, aim and draw on it.

The drill the 测试api button runs:

    pick a video -> open a window showing it (1366x768 preset, focused) -> for every 200 ms:
        read the frame -> crop the ROI (310,118,745,496) -> 372x248 -> jpeg90 -> base64
        -> WebSocket -> frame_result -> map the answer into our pixels
        -> move the real cursor to it (inside the window's picture only)
        -> draw the ROI, the answer and the aim on the frame -> show it -> log the frame
    ... until the run length is reached, then round_end + summary + logs.

Why the mapping is exact even though the picture may be shown smaller than 1:1: the window hands
the drill the **screen rectangle the picture actually occupies** (`widget_image_region`), and
`RoiGeometry` maps client pixels onto it linearly - so `to_screen()` is the real cursor position and
`MouseAimController(1366, 768)` with that same rectangle moves the cursor onto the same pixel.

Everything goes into one run folder (autolie_api.logs.RunLog): connection.log, detection.jsonl,
summary.json, frames/.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import cv2
import numpy as np

from autolie_api.connect import BackendError, open_backend
from autolie_api.intergration import (
    JPEG_QUALITY,
    build_slot_frame,
    check_caps,
    lie_roi_box,
    parse_server_point,
)
from autolie_api.logs import RunLog
from autolie_api.mock_backend import detect_bright_blob

LOG = logging.getLogger("api_auto_lie")
ROOT = Path(__file__).resolve().parent


def ensure_aim_path() -> None:
    """Put the bundled ``target_tracker`` folder on sys.path for mouse_aim_controller.

    The app already does this for the local lie pass, but the video drill must work when it is
    driven on its own too (a standalone harness): without the path the aim import raises, the
    drill silently runs without moving the mouse, and the cursor reads come back None.
    """

    import sys

    tracker = ROOT / "target_tracker"
    if tracker.is_dir() and str(tracker) not in sys.path:
        sys.path.insert(0, str(tracker))

DEFAULT_FPS = 5.0
DEFAULT_SECONDS = 30.0
# AWAIT_SECOND_WINDOW: after the lie popup is triggered the *real* lie window (the second window)
# needs a moment before its content is on screen.  Frames sent before that carry the previous
# screen (measured on the operator's clip: the first 16 frames were one frozen image and the
# service answered abandon_frame_hold for every one of them; its real content only appeared ~3.2 s
# in), so feeding starts only after this wait - and the session is opened only then, because
# 2.7.0 §4.1 kicks a session that stays 10 s without a valid frame.
AWAIT_SECOND_WINDOW_SEC = 3.0
# The service's own limit (measured handshake_ack: idle_no_frame_sec = 10); we stay a step under it.
IDLE_NO_FRAME_SEC = 10.0
IDLE_SAFE_SECONDS = IDLE_NO_FRAME_SEC - 2.0
ROI_CLIENT = (1366, 768)
ANNOTATED_EVERY_SECONDS = 2.0
CLIP = "api_lie_video"


# --------------------------------------------------------------------------------- window
class VideoDrillWindow:
    """The Tk window that shows the video.  All methods must run on the UI thread."""

    def __init__(self, root: Any, title: str, *, on_close: Optional[Callable[[], None]] = None,
                 on_toggle_mouse: Optional[Callable[[bool], None]] = None,
                 on_toggle_pause: Optional[Callable[[bool], None]] = None) -> None:
        import tkinter as tk
        from PIL import Image, ImageTk

        self._tk = tk
        self._Image = Image
        self._ImageTk = ImageTk
        self.top = tk.Toplevel(root)
        self.top.title(title)
        self.top.configure(bg="#171717")
        self.top.protocol("WM_DELETE_WINDOW", lambda: self._request_close(on_close))
        self.label = tk.Label(self.top, bg="#171717", fg="white",
                              text="正在打开视频……", font=("Microsoft YaHei UI", 13))
        self.label.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        controls = tk.Frame(self.top, bg="#242424")
        controls.pack(fill=tk.X)
        self.pause_button = tk.Button(controls, text="暂停（空格）", width=14,
                                      command=self._toggle_pause)
        self.pause_button.pack(side=tk.LEFT, padx=6, pady=6)
        self.mouse_button = tk.Button(controls, text="鼠标跟随：开（F8）", width=20,
                                      command=self._toggle_mouse)
        self.mouse_button.pack(side=tk.LEFT, padx=4, pady=6)
        tk.Button(controls, text="退出（Esc）", width=12,
                  command=lambda: self._request_close(on_close)).pack(side=tk.LEFT, padx=4)
        self.status = tk.StringVar(value="准备中……")
        tk.Label(controls, textvariable=self.status, anchor="w", bg="#242424", fg="white",
                 justify="left").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        self._photo = None
        self._image_size = (0, 0)
        self._on_toggle_mouse = on_toggle_mouse
        self._on_toggle_pause = on_toggle_pause
        self._paused = False
        self._mouse_on = True
        self.top.bind("<Escape>", lambda _event: self._request_close(on_close))
        self.top.bind("q", lambda _event: self._request_close(on_close))
        self.top.bind("<space>", lambda _event: self._toggle_pause())
        self.top.bind("m", lambda _event: self._toggle_mouse())
        self.focus()

    # ---- lifecycle
    def focus(self) -> None:
        """Bring the window forward and give it focus, so mouse input belongs to it."""

        try:
            self.top.deiconify()
            self.top.lift()
            self.top.attributes("-topmost", True)
            self.top.focus_force()
        except Exception:
            LOG.debug("video window focus failed", exc_info=True)

    def is_focused(self) -> bool:
        """True while this window owns the keyboard focus inside our application."""

        try:
            return self.top.focus_displayof() is not None
        except Exception:
            return False

    def _request_close(self, callback: Optional[Callable[[], None]]) -> None:
        try:
            self.top.attributes("-topmost", False)
        except Exception:
            pass
        if callback is not None:
            callback()
        self.close()

    def close(self) -> None:
        try:
            self.top.destroy()
        except Exception:
            LOG.debug("video window already closed", exc_info=True)

    @property
    def is_open(self) -> bool:
        try:
            return bool(self.top.winfo_exists())
        except Exception:
            return False

    # ---- display
    def update_frame(self, frame_bgr: np.ndarray, hud: str = "") -> None:
        """Show one frame; the picture is centred by Tk, which image_region() accounts for."""

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        image = self._Image.fromarray(rgb)
        self._image_size = image.size
        self._photo = self._ImageTk.PhotoImage(image)
        self.label.configure(image=self._photo, text="")
        if hud:
            self.status.set(hud)

    def image_region(self) -> Optional[tuple[int, int, int, int]]:
        """The screen rectangle the picture occupies (None while it is not displayed)."""

        from mouse_aim_controller import widget_image_region

        width, height = self._image_size
        if width <= 0 or height <= 0:
            return None
        return widget_image_region(self.label, width, height)

    def set_status(self, text: str) -> None:
        try:
            self.status.set(text)
        except Exception:
            LOG.debug("video window status update failed", exc_info=True)

    def set_mouse_enabled(self, enabled: bool) -> None:
        self._mouse_on = bool(enabled)
        try:
            self.mouse_button.configure(
                text=f"鼠标跟随：{'开' if self._mouse_on else '关'}（F8）")
        except Exception:
            LOG.debug("mouse button update failed", exc_info=True)

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        try:
            self.pause_button.configure(text="继续（空格）" if self._paused else "暂停（空格）")
        except Exception:
            LOG.debug("pause button update failed", exc_info=True)

    # ---- buttons
    def _toggle_pause(self) -> None:
        self.set_paused(not self._paused)
        if self._on_toggle_pause is not None:
            self._on_toggle_pause(self._paused)

    def _toggle_mouse(self) -> None:
        self.set_mouse_enabled(not self._mouse_on)
        if self._on_toggle_mouse is not None:
            self._on_toggle_mouse(self._mouse_on)


# --------------------------------------------------------------------------------- worker
class VideoDrillWorker(threading.Thread):
    """Play a video to the backend at 5 fps and aim on it.  One drill = one thread."""

    def __init__(
        self,
        *,
        video: Path,
        results: "queue.Queue[tuple[str, str]]",
        display: "queue.Queue[tuple[np.ndarray, str]]",
        stop_event: threading.Event,
        key: str = "",
        seconds: float = DEFAULT_SECONDS,
        fps: float = DEFAULT_FPS,
        use_mimic: bool = False,
        loop_video: bool = False,          # the operator's rule: if the video ends, it ends
        aim_enabled: bool = True,
        await_seconds: float = AWAIT_SECOND_WINDOW_SEC,
        log_folder: Optional[Path] = None,
        window_title: str = "",
    ) -> None:
        super().__init__(name="api-lie-video", daemon=True)
        self.video = Path(video)
        self.results = results
        self.display = display
        self.stop_event = stop_event
        self.key = str(key or "")
        self.seconds = max(1.0, float(seconds))
        self.fps = max(1.0, float(fps))
        self.use_mimic = bool(use_mimic)
        self.loop_video = bool(loop_video)
        self.aim_enabled = bool(aim_enabled)
        self.await_seconds = max(0.0, float(await_seconds))
        self.window_title = str(window_title)
        self.log_folder = Path(log_folder) if log_folder is not None else None

        self.stats: dict[str, Any] = {}
        self.log: Optional[RunLog] = None
        # set by the UI on every displayed frame: the picture's rectangle on screen
        self.image_rect: Optional[tuple[int, int, int, int]] = None
        self.paused = threading.Event()
        self.mouse_enabled = threading.Event()
        self.mouse_enabled.set()
        self._aim: Any = None
        self._suspended: list = []
        self.restore_cursor = True
        self._cursor_origin: Optional[tuple[float, float]] = None
        self._stop_request = threading.Event()
        self._lock = threading.Lock()
        self._running = False

    # ---- control
    def request_stop(self) -> None:
        self._stop_request.set()

    def set_paused(self, paused: bool) -> None:
        if paused:
            self.paused.set()
        else:
            self.paused.clear()

    def set_mouse_enabled(self, enabled: bool) -> None:
        if enabled:
            self.mouse_enabled.set()
        else:
            self.mouse_enabled.clear()
        if self._aim is not None:
            try:
                self._aim.set_enabled(bool(enabled))
            except Exception:
                LOG.debug("aim enable failed", exc_info=True)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def _report(self, state: str, detail: str = "") -> None:
        if state == "failed":
            LOG.error("api video drill failed: %s", detail or "(no detail)")
        else:
            LOG.info("api video drill %s%s", state, f": {detail}" if detail else "")
        try:
            self.results.put_nowait((state, detail))
        except queue.Full:
            LOG.warning("api video drill result dropped: %s", state)

    def _show(self, frame: np.ndarray, hud: str) -> None:
        try:
            while True:
                self.display.get_nowait()          # keep only the newest frame
        except queue.Empty:
            pass
        try:
            self.display.put_nowait((frame, hud))
        except queue.Full:
            pass

    # ---- the aim
    def _start_aim(self) -> None:
        if not self.aim_enabled:
            return
        ensure_aim_path()
        try:
            from mouse_aim_controller import MouseAimController, claim_cursor

            self._aim = MouseAimController(ROI_CLIENT[0], ROI_CLIENT[1],
                                           dead_band_px=6.0, confidence_threshold=0.0,
                                           target_stale_seconds=1.0)
            self._suspended = claim_cursor(self._aim)
            self._aim.set_enabled(True, silent=True)
            self._report("aim", "鼠标跟随已开启（F8 关闭，鼠标只会在视频画面内移动）")
        except Exception:
            LOG.exception("api video drill: could not start the aim controller")
            self._aim = None

    def _stop_aim(self) -> None:
        if self._aim is None:
            return
        try:
            self._aim.close()
        except Exception:
            LOG.debug("aim close failed", exc_info=True)
        ensure_aim_path()
        try:
            from mouse_aim_controller import release_cursor

            release_cursor(self._suspended)
        except Exception:
            LOG.debug("cursor release failed", exc_info=True)
        self._aim = None

    @staticmethod
    def _usable_rect(rect: Optional[tuple[int, int, int, int]]) -> bool:
        """A picture rectangle we are willing to aim into.

        On the first ticks the Tk label may not be laid out yet and reports something like
        (-368, -255, 998, 513); aiming there flings the cursor to the wrong place (measured: one
        frame with a 753 px error).  A plausible rect is at least 400x300 and starts at x >= 0.
        """

        if rect is None:
            return False
        left, top, right, bottom = (int(value) for value in rect)
        return (right - left) >= 400 and (bottom - top) >= 300 and left >= 0 and top >= 0

    def _push_aim(self, client_x: float, client_y: float) -> None:
        if self._aim is None or not self.mouse_enabled.is_set():
            return
        rect = self.image_rect
        if not self._usable_rect(rect):
            return
        try:
            self._aim.set_region(*rect)
            self._aim.push_target(client_x, client_y, 1.0, "api")
        except Exception:
            LOG.debug("aim push failed", exc_info=True)

    @staticmethod
    def _cursor() -> Optional[tuple[float, float]]:
        """The real cursor position, through the aim module's own reader."""

        ensure_aim_path()
        try:
            from mouse_aim_controller import read_cursor

            return read_cursor()
        except Exception:
            LOG.debug("cursor read failed", exc_info=True)
            return None

    # ---- drawing
    @staticmethod
    def _annotate(frame: np.ndarray, geometry, point, frame_id: int, hud: str) -> np.ndarray:
        canvas = frame.copy()
        left, top, width, height = geometry.box
        cv2.rectangle(canvas, (left, top), (left + width, top + height), (255, 255, 0), 2)
        if point is not None and point.x is not None and point.y is not None:
            cx, cy = point.to_client(geometry)
            cv2.drawMarker(canvas, (int(round(cx)), int(round(cy))), (0, 0, 255),
                           cv2.MARKER_CROSS, 30, 2)
            cv2.circle(canvas, (int(round(cx)), int(round(cy))), 14, (0, 0, 255), 1)
            aim = point.to_crop(geometry)
            ax, ay = int(round(left + aim[0])), int(round(top + aim[1]))
            cv2.circle(canvas, (ax, ay), 4, (0, 255, 0), -1)
        for index, line in enumerate(hud.split("\n")):
            origin = (left, max(20, top - 12) + index * 20)
            cv2.putText(canvas, line, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 4,
                        cv2.LINE_AA)
            cv2.putText(canvas, line, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1,
                        cv2.LINE_AA)
        return canvas

    # ---- the run
    def _await_ticks(self, total_ticks: int) -> int:
        """How many ticks to play without feeding, i.e. the AWAIT_SECOND_WINDOW wait.

        The session is already open while we wait, and 2.7.0 §4.1 kicks a session that stays
        ``idle_no_frame_sec`` (10 s) without a valid frame - so a wait that big would get us
        disconnected before the first frame.  Such a value is clamped to just under the limit and
        the clamp is recorded in the run log instead of failing silently.
        """

        wanted = max(0, int(round(self.await_seconds * self.fps)))
        if wanted >= total_ticks:
            wanted = max(0, total_ticks - 1)          # always leave at least one frame to feed
        limit = int(IDLE_SAFE_SECONDS * self.fps)
        if wanted > limit:
            if self.log is not None:
                self.log.connection("await clamped to stay inside the idle rule",
                                    asked_seconds=self.await_seconds,
                                    used_seconds=round(limit / self.fps, 1),
                                    idle_no_frame_sec=IDLE_NO_FRAME_SEC)
            wanted = limit
        return wanted

    def run(self) -> None:
        with self._lock:
            if self._running:
                self._report("failed", "测试已在运行")
                return
            self._running = True
        self._stop_request.clear()
        capture = cv2.VideoCapture(str(self.video))
        client = None
        mimic = None
        self.log = RunLog(name=CLIP, folder=self.log_folder)
        log = self.log
        log.connection("video drill start", video=str(self.video), fps=self.fps,
                       seconds=self.seconds, loop_video=self.loop_video,
                       window=self.window_title or "(tk window)")
        try:
            if not capture.isOpened():
                log.connection("video could not be opened")
                self._report("failed", f"无法打开视频 {self.video.name}")
                return
            video_fps = capture.get(cv2.CAP_PROP_FPS) or 0.0
            total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            log.connection("video info", fps=round(video_fps, 3), frames=total_frames,
                           duration_sec=(round(total_frames / video_fps, 1)
                                         if video_fps else None))
            self._report("video", f"{self.video.name} · {total_frames} 帧 @ {video_fps:.1f} fps")
            interval = 1.0 / self.fps
            # The whole drill is the 时长 (30s by default) and the AWAIT_SECOND_WINDOW wait is part
            # of it: the lie event has just fired, we connect at once, wait for the second window,
            # then feed until the run is over (or until the video ends, whichever comes first).
            total_ticks = max(1, int(round(self.seconds * self.fps)))
            clip_capped = False
            if not self.loop_video and total_frames > 0:
                # one source frame per tick: 30s * 5fps frames, or fewer when the clip is shorter
                capped = min(total_ticks, max(1, int(total_frames)))
                clip_capped = capped < total_ticks
                total_ticks = capped
            await_ticks = self._await_ticks(total_ticks)
            feed_ticks = max(1, total_ticks - await_ticks)

            # Connect FIRST, while the second window is still coming up, so the handshake and the
            # session are ready the moment there is something to send.  2.7.0 §4.1 gives a session
            # 10s without a valid frame, so the wait is clamped below that (see _await_ticks).
            try:
                client, note, mimic = self._open(log)
            except BackendError as exc:
                log.connection("backend unavailable", error=str(exc))
                log.summary({"outcome": "failed", "error": str(exc)})
                self._report("failed", str(exc))
                return
            self._report("backend", note)
            if self.aim_enabled:
                self._cursor_origin = self._cursor()
            self._start_aim()

            plan = (f"连接已建立，先等 {self.await_seconds:.1f} 秒（第二个窗口）再上传 "
                    f"{feed_ticks} 帧 @ {self.fps:.0f} fps（{feed_ticks / self.fps:.0f} 秒）"
                    if await_ticks else
                    f"连接已建立，{feed_ticks} 帧 @ {self.fps:.0f} fps（{self.seconds:.0f} 秒）")
            self._report("start", f"{plan}，ROI={lie_roi_box(*ROI_CLIENT)}")
            sent = answered = holds = misses = 0
            last_quota = None
            loops = 0
            deltas: list[float] = []
            truth_deltas: list[float] = []
            timings: list[float] = []
            started = time.perf_counter()
            next_tick = started
            last_annotated = 0.0
            consecutive_misses = 0
            previous_screen: Optional[tuple[float, float]] = None
            round_frame_id = 0          # 2.7.0 §3: restarts at 1 for every round
            rounds = 1
            reconnects = 0
            ended_reason = "run finished"
            for tick in range(1, total_ticks + 1):
                if self._stop_request.is_set() or self.stop_event.is_set():
                    ended_reason = "stopped by the operator"
                    self._report("stopped", f"第 {tick} 帧前收到停止请求")
                    break
                next_tick += interval
                delay = next_tick - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                while self.paused.is_set() and not self._stop_request.is_set():
                    time.sleep(0.05)
                ok, frame = capture.read()
                if not ok:
                    if not self.loop_video:
                        ended_reason = "video ended"
                        self._report("video-end", f"视频已结束（第 {tick} 帧），不循环")
                        break
                    loops += 1
                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    log.connection("video looped", loop=loops, at_frame=tick)
                    ok, frame = capture.read()
                    if not ok:
                        ended_reason = "video ended"
                        self._report("video-end", "视频无法继续读取")
                        break
                if frame.shape[1] != ROI_CLIENT[0] or frame.shape[0] != ROI_CLIENT[1]:
                    frame = cv2.resize(frame, ROI_CLIENT, interpolation=cv2.INTER_AREA)
                client_bgr = frame
                if tick <= await_ticks:
                    # AWAIT_SECOND_WINDOW: the second window is still coming up, so nothing is built
                    # and nothing is billed yet - but the session is already open and waiting, so
                    # feeding starts the instant the window is there.
                    left = max(0.0, self.await_seconds - (tick - 1) * interval)
                    hud = f"已连接 · 等待第二个窗口 {left:.1f}s（暂不上传）"
                    self._show(client_bgr, hud)
                    continue
                if client is None:
                    # a reconnect that could not be rebuilt left us without a session: open one now
                    try:
                        client, note, mimic = self._open(log)
                    except BackendError as exc:
                        log.connection("backend unavailable", error=str(exc))
                        log.summary({"outcome": "failed", "error": str(exc)})
                        self._report("failed", str(exc))
                        break
                    self._report("backend", note)
                    if self.aim_enabled and self._cursor_origin is None:
                        self._cursor_origin = self._cursor()
                    self._start_aim()
                frame_id = tick - await_ticks
                if frame_id == 1:
                    log.connection("feeding starts", after_await_seconds=self.await_seconds,
                                   at_tick=tick, feed_ticks=feed_ticks,
                                   since_start=round(time.perf_counter() - started, 2))
                round_frame_id += 1
                built = build_slot_frame(client_bgr, frame_id=round_frame_id,
                                         interval_sec=interval, window_rect=self.image_rect,
                                         quality=JPEG_QUALITY)
                caps_ok, caps_reason = check_caps(built)
                if not caps_ok:
                    # keep §3 intact: a skipped frame must not leave a hole in the sequence
                    round_frame_id -= 1
                    misses += 1
                    log.detection({"frame_id": frame_id, "caps_ok": False,
                                   "caps_reason": caps_reason})
                    self._report("frame", f"{frame_id}: 超过协议上限（{caps_reason}）")
                    continue
                cursor_before = self._cursor()
                settled_delta = None
                if cursor_before is not None and previous_screen is not None:
                    settled_delta = (((cursor_before[0] - previous_screen[0]) ** 2
                                      + (cursor_before[1] - previous_screen[1]) ** 2) ** 0.5)
                    deltas.append(settled_delta)   # how well the cursor reached the last answer
                try:
                    client.send_frame_base64(built.jpeg, frame_interval_sec=interval,
                                             frame_id=round_frame_id)
                except (ConnectionError, ConnectionResetError, BrokenPipeError, OSError) as exc:
                    # the service reset the connection mid-round (WinError 10054 in the field):
                    # log it, open a new session and keep going instead of dying
                    log.connection("connection lost while sending", frame_id=frame_id,
                                   round_frame_id=round_frame_id, error=str(exc))
                    outcome = self._reconnect(log, client, mimic, str(exc))
                    if outcome is None:
                        break
                    client, mimic, note = outcome
                    reconnects += 1
                    rounds += 1
                    round_frame_id = 0
                    log.connection("reconnected", rounds=rounds, note=note)
                    self._report("reconnect", f"已重连（第 {rounds} 轮）：{note}")
                    continue
                sent += 1
                try:
                    answer = client.wait_result(round_frame_id, timeout=max(1.5, interval * 3))
                except (ConnectionError, ConnectionResetError, BrokenPipeError, OSError) as exc:
                    log.connection("connection lost while reading", frame_id=frame_id,
                                   error=str(exc))
                    outcome = self._reconnect(log, client, mimic, str(exc))
                    if outcome is None:
                        break
                    client, mimic, note = outcome
                    reconnects += 1
                    rounds += 1
                    round_frame_id = 0
                    log.connection("reconnected", rounds=rounds, note=note)
                    self._report("reconnect", f"已重连（第 {rounds} 轮）：{note}")
                    continue
                if answer is None:
                    misses += 1
                    consecutive_misses += 1
                    log.detection({"frame_id": frame_id, "jpeg_bytes": built.byte_size,
                                   "answered": False})
                    self._show(self._annotate(client_bgr, built.geometry, None, frame_id,
                                              f"frame {frame_id}  NO ANSWER"),
                               f"帧 {frame_id}：无回包")
                    if consecutive_misses >= 3:
                        log.connection("stopping after 3 consecutive misses")
                        self._report("failed", "连续 3 帧没有回包，已停止")
                        break
                    continue
                consecutive_misses = 0
                answered += 1
                point = parse_server_point(answer)
                if point.quota_left is not None:
                    last_quota = point.quota_left
                if point.is_hold():
                    holds += 1
                client_xy = point.to_client(built.geometry)
                screen_xy = point.to_screen(built.geometry)
                if point.timing_ms is not None:
                    timings.append(float(point.timing_ms))
                self._push_aim(client_xy[0], client_xy[1])
                previous_screen = screen_xy
                cursor_after = self._cursor()
                delta = None
                if cursor_after is not None and screen_xy is not None:
                    delta = ((cursor_after[0] - screen_xy[0]) ** 2
                             + (cursor_after[1] - screen_xy[1]) ** 2) ** 0.5
                box_left, box_top, box_w, box_h = built.geometry.box
                roi_crop = client_bgr[box_top:box_top + box_h, box_left:box_left + box_w]
                blob = detect_bright_blob(roi_crop)      # local diagnostic, not our answer
                blob_slot = None
                if blob is not None:
                    # the crop is 745x496 (lie3main); the answer is in the 372x248 slot
                    blob_slot = (blob[0] / max(1e-6, built.geometry.scale_x),
                                 blob[1] / max(1e-6, built.geometry.scale_y))
                truth = None
                if blob_slot is not None and point.x is not None:
                    truth = (((point.x - blob_slot[0]) ** 2
                              + (point.y - blob_slot[1]) ** 2) ** 0.5)
                    truth_deltas.append(truth)
                hud = (f"frame {frame_id}  fid={point.frame_id}  {point.decision}\n"
                       f"slot ({point.x:.1f},{point.y:.1f})  client ({client_xy[0]:.0f},"
                       f"{client_xy[1]:.0f})  screen ({screen_xy[0]:.0f},{screen_xy[1]:.0f})\n"
                       f"{point.timing_ms or 0:.0f} ms  quota {point.quota_left}  "
                       f"cursor {('%.0fpx' % settled_delta) if settled_delta is not None else '-'}")
                annotated = self._annotate(client_bgr, built.geometry, point, frame_id, hud)
                self._show(annotated, f"帧 {frame_id}/{feed_ticks} · {point.decision} · "
                                      f"{point.timing_ms or 0:.0f} ms · quota {point.quota_left} · "
                                      f"光标偏差 "
                                      f"{('%.0fpx' % settled_delta) if settled_delta is not None else '-'}")
                log.detection({
                    "frame_id": frame_id, "jpeg_bytes": built.byte_size,
                    "base64_chars": built.base64_chars, "answered": True,
                    "x": point.x, "y": point.y, "x_main": point.x_main, "y_main": point.y_main,
                    "decision": point.decision, "api_ok": point.api_ok,
                    "api_skip_reason": point.api_skip_reason, "timing_ms": point.timing_ms,
                    "quota_left": point.quota_left, "seconds_left": point.seconds_left,
                    "round_active": point.round_active, "mode": point.mode,
                    "spaces_agree": point.cross_check_spaces(),
                    "within_slot": point.within_slot(),
                    "client_x": round(client_xy[0], 1), "client_y": round(client_xy[1], 1),
                    "screen_x": round(screen_xy[0], 1), "screen_y": round(screen_xy[1], 1),
                    "cursor_before": cursor_before, "cursor_after": cursor_after,
                    "answer_to_cursor_px": round(delta, 1) if delta is not None else None,
                    "settled_cursor_px": (round(settled_delta, 1)
                                          if settled_delta is not None else None),
                    "bright_blob_slot": ([round(blob_slot[0], 1), round(blob_slot[1], 1)]
                                         if blob_slot else None),
                    "bright_blob_crop": ([round(blob[0], 1), round(blob[1], 1)]
                                         if blob else None),
                    "blob_vs_answer_px": round(truth, 1) if truth is not None else None,
                    "image_rect": list(self.image_rect) if self.image_rect else None,
                })
                line = (f"{frame_id}: {built.byte_size / 1024:.0f}KB "
                        f"slot=({point.x:.0f},{point.y:.0f}) "
                        f"client=({client_xy[0]:.0f},{client_xy[1]:.0f}) "
                        f"{point.decision}")
                if point.is_hold():
                    line += " hold"
                if point.timing_ms:
                    line += f" {point.timing_ms:.0f}ms"
                if point.quota_left is not None:
                    line += f" quota={point.quota_left}"
                self._report("frame", line)
                now = time.perf_counter()
                if now - last_annotated >= ANNOTATED_EVERY_SECONDS or frame_id == feed_ticks:
                    last_annotated = now
                    path = log.save_frame(annotated, frame_id)
                    if path is not None:
                        self._report("capture", str(path))

            elapsed = time.perf_counter() - started
            if ended_reason == "run finished" and clip_capped:
                # the clip held fewer frames than the run asked for: it ended, so we ended
                ended_reason = "video ended"
                log.connection("video ended before the run did", ticks=total_ticks,
                               feed_ticks=feed_ticks)
            try:
                ended = client.end_round()
                log.connection("round_end sent",
                               ack=ended if ended else "no ack (server closed the socket)")
            except Exception as exc:
                log.connection("round_end failed", error=str(exc))
            def mean(values: list[float]) -> Optional[float]:
                return round(sum(values) / len(values), 1) if values else None

            summary = {
                "outcome": "done",
                "ended_because": ended_reason,
                "video": str(self.video),
                "run_seconds": round(elapsed, 2),
                "whole_process_seconds": round(elapsed, 2),
                "ticks": feed_ticks,
                "total_ticks": total_ticks,
                "await_seconds": self.await_seconds,
                "skipped_before_feeding": await_ticks,
                "feeding_seconds": round(feed_ticks / self.fps, 1),
                "sent": sent, "answered": answered, "holds": holds, "misses": misses,
                "video_loops": loops,
                "rounds": rounds, "reconnects": reconnects,
                "throughput_fps": round(sent / elapsed, 2) if elapsed else None,
                "quota_left": last_quota,
                "server_timing_ms": {"mean": mean(timings),
                                     "max": (round(max(timings), 1) if timings else None)},
                "cursor_vs_answer_px": {"mean": mean(deltas), "max": (round(max(deltas), 1)
                                                                     if deltas else None)},
                "blob_vs_answer_px": {"mean": mean(truth_deltas),
                                      "max": (round(max(truth_deltas), 1)
                                              if truth_deltas else None)},
                "aim_enabled": self.aim_enabled and self._aim is not None,
            }
            log.summary(summary)
            self._report("logs", str(log.folder))
            self._report("done", f"sent={sent} answered={answered} holds={holds} "
                                 f"rounds={rounds} reconnects={reconnects} "
                                 f"misses={misses} loops={loops} "
                                 f"server_mean={summary['server_timing_ms']['mean']}ms "
                                 f"cursor_delta={summary['cursor_vs_answer_px']['mean']}px")
        except Exception as exc:
            LOG.exception("api video drill crashed")
            try:
                log.connection("crash", error=f"{type(exc).__name__}: {exc}")
                log.summary({"outcome": "crashed", "error": str(exc)})
            finally:
                pass
            self._report("failed", f"{type(exc).__name__}: {exc}")
        finally:
            if self.restore_cursor and self._cursor_origin is not None and self._aim is not None:
                try:
                    self._aim.set_region(0, 0, 4000, 4000)
                    self._aim.push_target(*self._cursor_origin)
                    time.sleep(0.45)          # let the controller walk the cursor back
                    log.connection("cursor restored", to=list(self._cursor_origin))
                except Exception:
                    LOG.debug("cursor restore failed", exc_info=True)
            self._stop_aim()
            if client is not None:
                try:
                    client.close()
                except Exception:
                    LOG.debug("client close failed", exc_info=True)
            if mimic is not None:
                try:
                    mimic.stop()
                except Exception:
                    LOG.debug("mimic stop failed", exc_info=True)
            capture.release()
            log.close()
            with self._lock:
                self._running = False

    def _reconnect(self, log: RunLog, client, mimic, reason: str):
        """Rebuild the session after the service dropped it.

        The real service does reset a connection mid-round, so the drill has to survive it. Returns
        ``(client, mimic, note)`` on success, or ``None`` when a new session could not be built (the
        caller then stops with a message in the panel and in ``connection.log``).
        """

        self._report("reconnect", f"连接中断（{reason}）→ 正在重连")
        try:
            client.close()
        except Exception:
            LOG.debug("old client close failed", exc_info=True)
        if mimic is not None:
            try:
                mimic.stop()
            except Exception:
                LOG.debug("old mimic stop failed", exc_info=True)
        try:
            new_client, note, new_mimic = self._open(log)
        except BackendError as exc:
            log.connection("reconnect failed", error=str(exc))
            self._report("failed", f"重连失败：{exc}")
            return None
        return new_client, new_mimic, note

    def _open(self, log: RunLog):
        session = open_backend(key=self.key, transport="base64", frame_standard=self.fps,
                              use_mimic=self.use_mimic, log=log,
                              client_info="maple_assistant_video")
        return session.client, session.note, session.mimic


__all__ = ["DEFAULT_FPS", "DEFAULT_SECONDS", "ROI_CLIENT", "VideoDrillWindow",
           "VideoDrillWorker", "ensure_aim_path"]
