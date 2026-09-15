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
        key_sender: Any = None,
        capture_fn: Optional[Callable[[], Any]] = None,
        client_size_fn: Optional[Callable[[], Optional[tuple[int, int]]]] = None,
        out_dir: Optional[Path] = None,
        use_mimic: bool = False,
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
        self.key_sender = key_sender
        self._capture_fn = capture_fn
        self._client_size_fn = client_size_fn
        self._out_dir = Path(out_dir) if out_dir is not None else ROOT / "work" / "api_test"
        # True = always the local mimic (tests, offline drills): never resolve a real key.
        self.use_mimic = bool(use_mimic)
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

    def _open_backend(self, log=None):
        """Build the connection through the shared step (probe -> choose port -> handshake).

        Returns ``(client, note, mimic)``; raises :class:`BackendError` with a panel-ready
        message.  Everything attempted lands in the connection log.
        """

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
        client = None
        mimic = None
        log = RunLog(name="apitest_drill")
        self.log_folder = log.folder
        try:
            self.stats = TestStats()
            if not self._prepare_window():
                return
            image, rect = self._capture()
            if image is None:
                self._report("failed", "无法截取游戏窗口")
                return
            box = lie_roi_box(image.shape[1], image.shape[0])
            self._report("backend", "正在连接后端…")
            log.connection("drill start", window=self.window_title or "(none)",
                           fps=self.fps, seconds=self.duration, transport=self.transport)
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

            interval = 1.0 / self.fps
            total = max(1, int(round(self.duration * self.fps)))
            self._report("start", f"{total} 帧 @ {self.fps:.0f} fps，ROI={box}")
            started = time.perf_counter()
            next_at = started
            last_capture_saved = 0.0
            for frame_id in range(1, total + 1):
                if self._stop_request.is_set() or self.stop_event.is_set():
                    self._report("stopped", f"第 {frame_id} 帧前收到停止请求")
                    break
                next_at += interval
                delay = next_at - time.perf_counter()
                if delay > 0:
                    self._sleep(delay)
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
                if now - last_capture_saved >= CAPTURE_EVERY_SECONDS or frame_id == total:
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
