"""测试api: the live API drill behind the panel button.

What one press does, at 5 fps for a few seconds:

1. brings the game window forward (so the capture is the game, not the desktop),
2. screenshots the whole client,
3. converts it with the real converter - crop the precise ROI (310,118,745,496), resize to
   372x248, JPEG quality 90, base64 (``autolie_api.intergration``),
4. connects with our WebSocket client (``autolie_api.ws_client``) and sends the frame,
5. reads the ``frame_result`` and maps the answer back to crop / client / screen pixels,
6. writes an annotated frame (ROI box + the answered position) so the operator can look at it,
7. reports every step to the panel and, at the end, a summary plus ``round_end``.

Backend choice: with a ``product_key`` configured it talks to the real service (probing the
ports from ``autolie_api/ip_port.txt``); with no key it starts the local mimic
(``autolie_api.mock_backend``) so the whole chain can be exercised without a key and without a
network.  The panel always says which one is in use.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import cv2

from autolie_api.connect import BackendError, open_backend
from autolie_api.intergration import (
    JPEG_QUALITY,
    ServerPoint,
    build_slot_frame,
    check_caps,
    lie_roi_box,
    parse_server_point,
)
from autolie_api.logs import RunLog
from image_io import save_screenshot

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent

DEFAULT_FPS = 5.0
DEFAULT_SECONDS = 5.0
# AWAIT_SECOND_WINDOW, shared with the video drill (``api_lie_video.AWAIT_SECOND_WINDOW_SEC``): the lie
# event fires the moment the window's HUD square is on screen, but the window's real content needs a
# moment before it is there - measured on the operator's clip, the first 16 frames were one frozen image
# and the service answered ``abandon_frame_hold`` for every one of them, its content only appearing
# ~3.2 s in.  The AUTOMATIC pass therefore runs the drill's workflow: connect first, wait this long
# WITHOUT building or sending a frame, then start the picture push.  0.0 keeps the old immediate-feed
# behaviour for the offline single-shot drill.
DEFAULT_AWAIT_SECONDS = 0.0
# How often an annotated frame is written while the drill runs (so it does not fill the disk).
CAPTURE_EVERY_SECONDS = 1.0
TEST_KEY = "LIE-LOCAL-TEST"
MIMIC_NOTE = "本地模拟后端"


@dataclass
class TestStats:
    frames: int = 0
    results: int = 0
    holds: int = 0
    failures: int = 0
    bytes_sent: int = 0
    deltas_px: list[float] = field(default_factory=list)
    last_screen: Optional[tuple[float, float]] = None
    quota_left: Optional[int] = None

    def describe(self) -> str:
        average = (sum(self.deltas_px) / len(self.deltas_px)) if self.deltas_px else 0.0
        return (f"frames={self.frames} results={self.results} holds={self.holds} "
                f"failures={self.failures} sent={self.bytes_sent / 1024:.0f} KB "
                f"quota_left={self.quota_left} "
                f"answer_to_screen_delta={average:.1f}px")


class ApiLieTestWorker(threading.Thread):
    """One drill per press; the panel drains :attr:`results` for progress text."""

    def __init__(
        self,
        *,
        results: "queue.Queue[tuple[str, str]]",
        stop_event: threading.Event,
        window_title: str = "",
        key: str = "",
        host: str = "",
        ports: Sequence[int] = (),
        transport: str = "base64",
        fps: float = DEFAULT_FPS,
        duration: float = DEFAULT_SECONDS,
        # Ticks to run WITHOUT feeding, i.e. the AWAIT_SECOND_WINDOW wait for the real lie window (see
        # DEFAULT_AWAIT_SECONDS).  It is part of ``duration``, exactly like the drill's 时长.
        await_seconds: float = DEFAULT_AWAIT_SECONDS,
        key_sender: Any = None,
        capture_fn: Optional[Callable[[], Any]] = None,
        client_size_fn: Optional[Callable[[], Optional[tuple[int, int]]]] = None,
        out_dir: Optional[Path] = None,
        use_mimic: bool = False,
        aim_enabled: bool = False,
        aim_overlay: Optional[Callable[[int, int], None]] = None,
        on_capture_start: Optional[Callable[[], None]] = None,
        on_capture_stop: Optional[Callable[[], None]] = None,
        backend_opener: Optional[Callable[[RunLog], tuple[Any, str, Any]]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(name="api-lie-test", daemon=True)
        self.results = results
        self.stop_event = stop_event
        self.window_title = str(window_title)
        self.key = str(key or "").strip()
        self.host = str(host or "").strip()
        self.ports = tuple(int(port) for port in ports)
        self.transport = str(transport or "base64").lower()
        self.fps = max(1.0, float(fps))
        self.duration = max(1.0, float(duration))
        self.await_seconds = max(0.0, float(await_seconds))
        self.key_sender = key_sender
        self._capture_fn = capture_fn
        self._client_size_fn = client_size_fn
        self._out_dir = Path(out_dir) if out_dir is not None else ROOT / "work" / "api_test"
        # True = always the local mimic (tests, offline drills): never resolve a real key.
        self.use_mimic = bool(use_mimic)
        # EXECUTE the answer with the mouse.  The vendor protocol doc: "鼠标/执行层一般用 x / y
        # （372×248 协议 ROI）" - the returned point is meant to be driven into the client, which is what
        # the video drill does with MouseAimController.  The automatic pass used to only MEASURE the
        # answer (it logged screen=(x, y) and nothing else), so it never took control of the lie test -
        # the operator's report through v1.0.32: "autolie_api failed again, it doesn't take control".
        # Off by default so a test or an offline drill never grabs the real cursor; the assistant turns
        # it on for the automatic pass.
        self.aim_enabled = bool(aim_enabled)
        # Draw the aim while the pass runs (the video drill draws it in its own window; here it is an
        # overlay crosshair on the game at the answered SCREEN point).
        self.aim_overlay = aim_overlay
        self._on_capture_start = on_capture_start
        self._on_capture_stop = on_capture_stop
        # The automatic pass supplies a UI-start probe cache here.  Keeping
        # this optional preserves the standalone drill's normal probe path.
        self._backend_opener = backend_opener
        self._aim: Any = None
        self._aim_suspended: Any = None
        self._sleep = sleep
        self._stop_request = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        self.stats = TestStats()
        self.backend_note = ""
        self.key_source = ""
        self.log_folder = None

    # ------------------------------------------------------------------ control
    def request_stop(self) -> None:
        self._stop_request.set()

    def set_key(self, key: str) -> None:
        """The panel's 密钥 button: an empty key means "use the local mimic"."""

        self.key = str(key or "").strip()

    def set_seconds(self, seconds: float) -> None:
        self.duration = max(1.0, min(60.0, float(seconds)))

    def _await_ticks(self, total_ticks: int, log: Optional[RunLog] = None) -> int:
        """How many ticks to run WITHOUT feeding: the AWAIT_SECOND_WINDOW wait.

        Identical rule to the video drill (``api_lie_video.VideoDrillWorker._await_ticks``): the session
        is already open while we wait, and 2.7.0 §4.1 kicks a session that stays ``idle_no_frame_sec``
        (10 s) without a valid frame, so a wait that big is clamped to just under that limit and the
        clamp is recorded in the run log instead of failing silently.
        """

        wanted = max(0, int(round(self.await_seconds * self.fps)))
        if wanted >= total_ticks:
            wanted = max(0, total_ticks - 1)          # always leave at least one frame to feed
        try:
            from api_lie_video import IDLE_NO_FRAME_SEC, IDLE_SAFE_SECONDS
        except Exception:                             # pragma: no cover - constants only
            return wanted
        limit = int(IDLE_SAFE_SECONDS * self.fps)
        if wanted > limit:
            if log is not None:
                log.connection("await clamped to stay inside the idle rule",
                               asked_seconds=self.await_seconds,
                               used_seconds=round(limit / self.fps, 1),
                               idle_no_frame_sec=IDLE_NO_FRAME_SEC)
            wanted = limit
        return wanted

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def _report(self, state: str, detail: str = "") -> None:
        if state == "failed":
            LOG.error("api test failed: %s", detail or "(no detail)")
        else:
            LOG.info("api test %s%s", state, f": {detail}" if detail else "")
        try:
            self.results.put_nowait((state, detail))
        except queue.Full:
            LOG.warning("api test result dropped: %s", state)

    # ------------------------------------------------------------------ plumbing
    def _capture(self):
        """One screenshot of the whole game window -> (BGR, screen rect)."""

        if self._capture_fn is not None:
            frame, rect = self._capture_fn()
            return frame, rect
        try:
            from capture_worker import capture_window

            image, rect = capture_window(self.window_title)
        except Exception:
            LOG.warning("api test: capture failed", exc_info=True)
            return None, (0, 0, 0, 0)
        import numpy as np

        return np.asarray(image)[:, :, ::-1].copy(), rect

    def _prepare_window(self) -> bool:
        sender = self.key_sender
        if sender is None:
            return True
        try:
            selected = sender.select_window()
        except Exception:
            LOG.exception("api test: window selection failed")
            self._report("failed", "选中游戏窗口时出错（详见 error.log）")
            return False
        if selected is False:
            self._report("failed", f"找不到游戏窗口 {self.window_title or '(title)'}")
            return False
        return True

    # ------------------------------------------------------------------ the mouse execution
    def _start_aim(self, image_width: int, image_height: int) -> None:
        """Own the cursor for this pass, so the API's answer is EXECUTED in the client."""

        if not self.aim_enabled or self._aim is not None:
            return
        try:
            from api_lie_video import aim_module, ensure_aim_path

            ensure_aim_path()
            _aim = aim_module()
            self._aim = _aim.MouseAimController(int(image_width), int(image_height),
                                                dead_band_px=6.0, confidence_threshold=0.0,
                                                target_stale_seconds=1.0)
            self._aim_suspended = _aim.claim_cursor(self._aim)
            self._aim.set_enabled(True, silent=True)
            LOG.warning("api test: 鼠标执行已开启 - the API's answer is driven into the client "
                        "(captured image %dx%d)", image_width, image_height)
            self._report("aim", "鼠标执行已开启：按 API 返回的坐标移动鼠标")
        except Exception:
            LOG.exception("api test: the mouse aim could not be started")
            self._aim = None

    def _push_aim(self, rect, image_width: int, image_height: int,
                  client_x: float, client_y: float) -> None:
        """Move the cursor to one answer (client pixels -> the client rectangle on screen)."""

        if not self.aim_enabled:
            return
        if self._aim is None:
            self._start_aim(image_width, image_height)
        if self._aim is None:
            return
        try:
            if rect is not None and len(rect) == 4:
                self._aim.set_region(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
            self._aim.push_target(float(client_x), float(client_y), 1.0, "api")
        except Exception:
            LOG.debug("api test: the aim push failed", exc_info=True)

    def _stop_aim(self) -> None:
        if self._aim is None:
            return
        try:
            self._aim.close()
        except Exception:
            LOG.debug("api test: aim close failed", exc_info=True)
        try:
            from api_lie_video import aim_module, ensure_aim_path

            ensure_aim_path()
            aim_module().release_cursor(self._aim_suspended)
        except Exception:
            LOG.debug("api test: cursor release failed", exc_info=True)
        self._aim = None
        self._aim_suspended = None

    def _open_backend(self, log=None):
        """Build the connection through the shared step (probe -> choose port -> handshake).

        Returns ``(client, note, mimic)``; raises :class:`BackendError` with a panel-ready
        message.  Everything attempted lands in the connection log.
        """

        if self._backend_opener is not None:
            return self._backend_opener(log)
        session = open_backend(
            key=self.key,
            host=self.host,
            ports=self.ports,
            transport=self.transport,
            frame_standard=self.fps,
            use_mimic=self.use_mimic,
            log=log,
            client_info="maple_assistant_test",
        )
        self.key_source = session.key_source
        return session.client, session.note, session.mimic

    # ------------------------------------------------------------------ the drill
    def run(self) -> None:
        with self._lock:
            if self._running:
                self._report("failed", "测试已在运行")
                return
            self._running = True
        self._stop_request.clear()
        if self._on_capture_start is not None:
            try:
                self._on_capture_start()
            except Exception:
                LOG.warning("api test: could not enable shared capture", exc_info=True)
        client = None
        mimic = None
        log = RunLog(name="apitest_drill")
        self.log_folder = log.folder
        # This clock is the lie-event clock, not the time at which connection
        # setup happened to finish.  It lets the network setup run inside the
        # visual settle period instead of adding its duration after it.
        pass_started = time.perf_counter()
        try:
            self.stats = TestStats()
            if not self._prepare_window():
                return
            self._report("backend", "正在连接后端…")
            log.connection("drill start", window=self.window_title or "(none)",
                           fps=self.fps, seconds=self.duration, transport=self.transport,
                           await_seconds=self.await_seconds)
            # Connect FIRST, exactly like the video drill: the handshake and the session are ready while
            # the lie window is still coming up, so the picture push starts the instant its content is
            # there.  The wait is clamped below the service's idle_no_frame_sec (see _await_ticks).
            try:
                client, note, mimic = self._open_backend(log)
            except BackendError as exc:
                log.connection("backend unavailable", error=str(exc))
                log.summary({"outcome": "failed", "error": str(exc)})
                log.close()
                self._report("failed", str(exc))
                return
            self.backend_note = str(note)
            self._report("backend", f"{note}")

            # Open the socket before capturing the first image.  The shared
            # capture is already enabled, so this leaves the full visual
            # settle period available for endpoint connection/handshake.
            image, rect = self._capture()
            if image is None:
                self._report("failed", "无法截取游戏窗口")
                return
            box = lie_roi_box(image.shape[1], image.shape[0])

            interval = 1.0 / self.fps
            active_seconds = max(interval, self.duration - self.await_seconds)
            feed_frames = max(1, int(round(active_seconds * self.fps)))
            settle_deadline = pass_started + self.await_seconds
            remaining_settle = settle_deadline - time.perf_counter()
            if remaining_settle > 0:
                plan = (f"连接已建立，等待第二个窗口剩余 {remaining_settle:.1f} 秒后立即上传 "
                        f"{feed_frames} 帧 @ {self.fps:.0f} fps（{active_seconds:.0f} 秒）")
            else:
                late = -remaining_settle
                plan = (f"连接在第二个窗口等待期后才完成（晚 {late:.1f} 秒）；现在立即上传 "
                        f"{feed_frames} 帧 @ {self.fps:.0f} fps")
            self._report("start", f"{plan}，ROI={box}")
            # The cursor is claimed as soon as the session exists (the drill does the same), so the pass
            # owns the mouse before the window is even readable.
            if self.aim_enabled and self._aim is None:
                self._start_aim(image.shape[1], image.shape[0])
            if remaining_settle > 0:
                self._sleep(remaining_settle)
            started = time.perf_counter()
            # The first upload is due now.  Later frames use the normal 5-fps
            # cadence; do not add an unnecessary first 200 ms tick.
            next_at = started - interval
            last_capture_saved = 0.0
            frame_id = 0
            for tick in range(1, feed_frames + 1):
                if self._stop_request.is_set() or self.stop_event.is_set():
                    self._report("stopped", f"第 {tick} 帧前收到停止请求")
                    break
                next_at += interval
                delay = next_at - time.perf_counter()
                if delay > 0:
                    self._sleep(delay)
                frame_id += 1
                image, rect = self._capture()
                if image is None:
                    self.stats.failures += 1
                    self._report("failed", "截取游戏窗口失败")
                    break
                frame = build_slot_frame(image, box, frame_id=frame_id,
                                         interval_sec=interval, window_rect=rect,
                                         quality=JPEG_QUALITY)
                ok, reason = check_caps(frame)
                if not ok:
                    self.stats.failures += 1
                    self._report("frame", f"{frame_id}: 超过协议上限（{reason}）")
                    continue
                self.stats.frames += 1
                self.stats.bytes_sent += frame.byte_size
                if self.transport == "rtf1":
                    client.send_frame_rtf1(frame_id, frame.jpeg, frame_interval_sec=interval)
                else:
                    client.send_frame_base64(frame.jpeg, frame_interval_sec=interval,
                                             frame_id=frame_id)
                answer = client.wait_result(frame_id, timeout=max(2.0, interval * 4))
                if answer is None:
                    self.stats.failures += 1
                    self._report("frame", f"{frame_id}: 没有回包（超时）")
                    continue
                point = parse_server_point(answer)
                self.stats.results += 1
                if point.quota_left is not None:
                    self.stats.quota_left = point.quota_left
                log.detection({
                    "frame_id": frame_id,
                    "jpeg_bytes": frame.byte_size,
                    "base64_chars": frame.base64_chars,
                    "x": point.x, "y": point.y,
                    "x_main": point.x_main, "y_main": point.y_main,
                    "decision": point.decision,
                    "api_ok": point.api_ok,
                    "api_skip_reason": point.api_skip_reason,
                    "timing_ms": point.timing_ms,
                    "quota_left": point.quota_left,
                    "seconds_left": point.seconds_left,
                    "round_active": point.round_active,
                    "spaces_agree": point.cross_check_spaces(),
                    "within_slot": point.within_slot(),
                })
                line = f"{frame_id}: {frame.byte_size / 1024:.0f}KB"
                if point.is_hold():
                    self.stats.holds += 1
                    line += " hold"
                if point.x is not None and point.y is not None:
                    client_xy = point.to_client(frame.geometry)
                    screen_xy = point.to_screen(frame.geometry)
                    # EXECUTE the answer: the cursor is driven to the point the API returned (the vendor
                    # doc: the mouse/execution layer uses x/y in the 372x248 protocol ROI).  Without
                    # this the pass only measured the answer and never took control of the lie test.
                    self._push_aim(rect, image.shape[1], image.shape[0],
                                   client_xy[0], client_xy[1])
                    # ... and DRAW it: an overlay crosshair on the game where the pass is aiming, the
                    # live equivalent of what the 测试api video drill shows in its window.
                    if self.aim_overlay is not None:
                        try:
                            self.aim_overlay(int(round(screen_xy[0])), int(round(screen_xy[1])))
                        except Exception:
                            LOG.debug("api test: the aim overlay failed", exc_info=True)
                    self.stats.last_screen = screen_xy
                    self.stats.deltas_px.append(
                        ((client_xy[0] - (box[0] + point.x * frame.geometry.scale_x)) ** 2
                         + (client_xy[1] - (box[1] + point.y * frame.geometry.scale_y)) ** 2)
                        ** 0.5)
                    line += (f" slot=({point.x:.0f},{point.y:.0f}) "
                             f"client=({client_xy[0]:.0f},{client_xy[1]:.0f}) "
                             f"screen=({screen_xy[0]:.0f},{screen_xy[1]:.0f})")
                line += f" {point.decision}"
                if point.timing_ms is not None:
                    line += f" {point.timing_ms:.0f}ms"
                if point.quota_left is not None:
                    line += f" quota={point.quota_left}"
                self._report("frame", line)
                now = time.perf_counter()
                if now - last_capture_saved >= CAPTURE_EVERY_SECONDS or frame_id == feed_frames:
                    last_capture_saved = now
                    path = self._save_annotated(image, frame.geometry, point, frame_id,
                                                log)
                    if path is not None:
                        self._report("capture", str(path))

            try:
                ended = client.end_round()
                LOG.info("api test: round_end -> %s", ended)
            except Exception:
                LOG.debug("api test: round_end failed", exc_info=True)
            log.summary({"outcome": "done", "backend": self.backend_note,
                         "await_seconds": self.await_seconds,
                         "skipped_before_feeding": await_ticks,
                         "stats": self.stats.describe(),
                         "quota_left": self.stats.quota_left,
                         "last_screen": self.stats.last_screen})
            log.close()
            self._report("logs", str(log.folder))
            self._report("done", self.stats.describe())
        except Exception as exc:
            LOG.exception("api test crashed")
            try:
                log.connection("crash", error=f"{type(exc).__name__}: {exc}")
                log.summary({"outcome": "crashed", "error": str(exc)})
            finally:
                log.close()
            self._report("failed", f"{type(exc).__name__}: {exc}")
        finally:
            try:
                log.close()
            except Exception:
                LOG.debug("api test: log close failed", exc_info=True)
            self._stop_aim()
            try:
                if client is not None:
                    client.close()
            except Exception:
                LOG.debug("api test: close failed", exc_info=True)
            try:
                if mimic is not None:
                    mimic.stop()
            except Exception:
                LOG.debug("api test: mimic stop failed", exc_info=True)
            if self._on_capture_stop is not None:
                try:
                    self._on_capture_stop()
                except Exception:
                    LOG.warning("api test: could not release shared capture", exc_info=True)
            with self._lock:
                self._running = False

    def _save_annotated(self, image, geometry, point: ServerPoint, frame_id: int,
                        log=None) -> Optional[Path]:
        """The frame with the ROI box and the answered position drawn on it.

        Written into the run folder (``frames/``) when a log is given, so one drill is one
        self-contained folder.
        """

        canvas = image.copy()
        left, top, width, height = geometry.box
        cv2.rectangle(canvas, (left, top), (left + width, top + height), (0, 255, 255), 2)
        if point is not None and point.x is not None and point.y is not None:
            client_xy = point.to_client(geometry)
            cv2.drawMarker(canvas, (int(round(client_xy[0])), int(round(client_xy[1]))),
                           (0, 0, 255), cv2.MARKER_CROSS, 26, 2)
        label = f"api test frame {frame_id}"
        if point is not None and point.x is not None:
            label += f"  slot({point.x:.0f},{point.y:.0f}) {point.decision}"
        cv2.putText(canvas, label, (left, max(24, top - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(canvas, label, (left, max(24, top - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (0, 255, 255), 2, cv2.LINE_AA)
        if log is not None:
            return log.save_frame(canvas, frame_id)
        return save_screenshot(self._out_dir / f"api_test_{frame_id:04d}",
                               canvas, quality=95)


__all__ = ["ApiLieTestWorker", "TestStats", "DEFAULT_FPS", "DEFAULT_SECONDS"]
