#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CT LIE3 远程跟踪工作流 — 可运行样本（供同行借鉴）

本文件从 CT 客户端（ct_lie3_capture_core + roitrack + lie3answer714）提炼核心流程，
用最小可运行代码展示：

  1. 状态机：watching → prepared → recording
  2. lie3 模板确认后立刻 probe + 握手（不等 log）
  3. log 连续命中后开录，按固定 FPS 定时截取 lie3main ROI
  4. 每帧：缩放到 372×248 → JPEG90 → 异步发帧（不阻塞截图节拍）
  5. 收包：poll 取「已发送帧号以内、未执行过的最高 frame_id」坐标
  6. pending 时 hold 上一目标，不触发重连

运行示例
--------
# 离线演示（无需服务器，模拟 probe/握手/多路在途 POST）
python CT/samples/ct_lie3_remote_workflow_sample.py --mock

# 连接真实 RoiTrack 服务（需已配置端点与 product_key）
python CT/samples/ct_lie3_remote_workflow_sample.py --real --backend http \\
    --product-key YOUR_LIE_KEY --http-base http://117.50.223.113:8004

python CT/samples/ct_lie3_remote_workflow_sample.py --real --backend ws \\
    --product-key YOUR_LIE_KEY --ws-host 117.50.223.113 --ws-ports 8001,8002,8003

环境变量（与 CT 发布版一致，可选）
  ROI_TRACK_HTTP_BASE / ROI_TRACK_WS_HOST + ROI_TRACK_WS_PORTS
  LIE_PRODUCT_KEY
  ROI_TRACK_HTTP_FRAME_STANDARD=5
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# 与 CT 一致的 half-pivot 画布尺寸
# ---------------------------------------------------------------------------
SLOT_W, SLOT_H = 372, 248
PROBE_TIMEOUT_SEC = 1.0
DEFAULT_RECORD_FPS = 5.0
DEFAULT_WAIT_RESULT_SEC = 1.5

log = logging.getLogger("ct_lie3_sample")


def _ensure_ct_on_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    ct = root / "CT"
    for p in (str(root), str(ct)):
        if p not in sys.path:
            sys.path.insert(0, p)
    return root


# ---------------------------------------------------------------------------
# 状态机（对齐 ct_lie3_capture_core.CaptureState）
# ---------------------------------------------------------------------------
class CaptureState(str, Enum):
    WATCHING = "watching"      # 扫 lie3 模板
    PREPARED = "prepared"      # lie3 已出现，等 log；此阶段完成 probe+握手
    RECORDING = "recording"    # log 达标，定时截 main 并发远程 track


@dataclass
class SampleSettings:
    record_fps: float = DEFAULT_RECORD_FPS
    watch_interval_sec: float = 0.33
    prepare_interval_sec: float = 0.2
    log_confirm_hits: int = 2
    lie3_confirm_hits: int = 2
    log_threshold: float = 0.63
    max_round_frames: int = 12
    output_dir: Path = field(default_factory=lambda: Path("sample_captures"))


# ---------------------------------------------------------------------------
# 异步收包语义（与 roitrack/http_client + ws_client 一致）
# ---------------------------------------------------------------------------
class AsyncResultStore:
    """
    多路在途发帧 + 按 frame_id 收包缓存。

    poll(sent_fid) 规则（CT 现行语义）：
      - 所有回包写入 _results，本地不主动丢包
      - 在 sent_fid 以内、且 frame_id > _last_applied_fid 的结果里取最大 frame_id
      - 例：4、5 都已回且未执行 → 只执行 5
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: Dict[int, Dict[str, Any]] = {}
        self._last_applied_fid: int = 0

    def store(self, resp: Dict[str, Any]) -> None:
        fid = int(resp.get("frame_id") or 0)
        if fid <= 0:
            return
        with self._lock:
            self._results[fid] = dict(resp)

    def poll_applicable(self, sent_fid: int) -> Optional[Dict[str, Any]]:
        sent = int(sent_fid or 0)
        with self._lock:
            best_fid = 0
            best: Optional[Dict[str, Any]] = None
            for fid, res in self._results.items():
                if fid <= self._last_applied_fid:
                    continue
                if sent > 0 and fid > sent:
                    continue
                if fid > best_fid:
                    best_fid = fid
                    best = res
            if best is None:
                return None
            self._last_applied_fid = best_fid
            return dict(best)

    def poll_with_wait(self, sent_fid: int, max_wait_sec: float) -> Optional[Dict[str, Any]]:
        deadline = time.monotonic() + max(0.0, float(max_wait_sec))
        while True:
            res = self.poll_applicable(sent_fid)
            if res is not None:
                return res
            if max_wait_sec <= 0 or time.monotonic() >= deadline:
                return None
            time.sleep(0.008)

    def reset(self) -> None:
        with self._lock:
            self._results.clear()
            self._last_applied_fid = 0


def bgr_to_jpeg90(bgr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes()


def resize_to_slot(main_bgr: np.ndarray) -> Tuple[np.ndarray, float, float]:
    h0, w0 = main_bgr.shape[:2]
    sx = SLOT_W / max(1, w0)
    sy = SLOT_H / max(1, h0)
    if w0 == SLOT_W and h0 == SLOT_H:
        return main_bgr, sx, sy
    slot = cv2.resize(main_bgr, (SLOT_W, SLOT_H), interpolation=cv2.INTER_LINEAR)
    return slot, sx, sy


def slot_to_main_roi(x_slot: float, y_slot: float, sx: float, sy: float) -> Tuple[float, float]:
    return x_slot / sx, y_slot / sy


# ---------------------------------------------------------------------------
# 远程 track 后端抽象
# ---------------------------------------------------------------------------
class RemoteTrackBackend(Protocol):
    def probe_and_handshake(self, *, force_new: bool = False) -> bool: ...
    def end_round(self) -> None: ...
    def submit_frame(self, slot_bgr: np.ndarray, frame_interval_sec: float) -> Tuple[int, bool]: ...
    def wait_result(self, sent_fid: int, frame_interval_sec: float) -> Optional[Dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Mock 后端：模拟 probe 排序 + 异步 RTT + 移动目标坐标
# ---------------------------------------------------------------------------
class MockRemoteBackend:
    def __init__(self, *, lag_frames: int = 1) -> None:
        self._store = AsyncResultStore()
        self._frame_id = 0
        self._ready = False
        self._lag = max(0, int(lag_frames))
        self._target_x = SLOT_W * 0.5
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mock-post")

    def probe_and_handshake(self, *, force_new: bool = False) -> bool:
        if force_new:
            self.end_round()
        # 模拟多端口 probe（并行 1s 超时）
        ports = [8004, 8005, 8006]
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=len(ports)) as pool:
            futs = {pool.submit(self._mock_probe, p): p for p in ports}
            scores: List[Tuple[int, float]] = []
            for fut in as_completed(futs):
                port = futs[fut]
                try:
                    rtt = fut.result()
                    if rtt is not None:
                        scores.append((port, rtt))
                except Exception:
                    pass
        scores.sort(key=lambda x: x[1])
        elapsed = (time.perf_counter() - t0) * 1000
        if not scores:
            log.warning("[mock] 全部端口 probe 失败")
            return False
        best_port, best_rtt = scores[0]
        self._ready = True
        self._store.reset()
        self._frame_id = 0
        log.info(
            "[mock] probe 选中 :%d rtt=%.0fms（探测耗时 %.0fms，共 %d 个可用）",
            best_port,
            best_rtt,
            elapsed,
            len(scores),
        )
        return True

    def _mock_probe(self, port: int) -> Optional[float]:
        time.sleep(random.uniform(0.05, 0.25))
        if random.random() < 0.15:
            return None
        return random.uniform(30, 120)

    def end_round(self) -> None:
        self._ready = False
        self._store.reset()
        self._frame_id = 0

    def submit_frame(self, slot_bgr: np.ndarray, frame_interval_sec: float) -> Tuple[int, bool]:
        if not self._ready:
            return 0, False
        self._frame_id += 1
        fid = self._frame_id
        _ = bgr_to_jpeg90(slot_bgr)
        self._executor.submit(self._complete_frame, fid, frame_interval_sec)
        return fid, True

    def _complete_frame(self, fid: int, frame_interval_sec: float) -> None:
        # 模拟网络 RTT + 服务端 lag（回包 frame_id 可能落后 sent）
        time.sleep(random.uniform(0.08, 0.35))
        recv_fid = max(1, fid - self._lag)
        self._target_x = (self._target_x + 6.0) % (SLOT_W - 20)
        y = SLOT_H * 0.55 + 8 * np.sin(recv_fid * 0.4)
        self._store.store(
            {
                "type": "frame_result",
                "success": True,
                "frame_id": recv_fid,
                "x_main": float(self._target_x),
                "y_main": float(y),
                "decision": "track",
                "timing_ms": {"total": random.randint(40, 120)},
            }
        )

    def wait_result(self, sent_fid: int, frame_interval_sec: float) -> Optional[Dict[str, Any]]:
        wait = min(3.0, max(DEFAULT_WAIT_RESULT_SEC, frame_interval_sec * 3.0))
        return self._store.poll_with_wait(sent_fid, wait)


# ---------------------------------------------------------------------------
# Real 后端：委托 CT/roitrack 模块（与发布版相同实现）
# ---------------------------------------------------------------------------
class RealRemoteBackend:
    def __init__(self, backend: str, product_key: str) -> None:
        _ensure_ct_on_path()
        self._backend = backend.strip().lower()
        self._product_key = product_key.strip()
        if not self._product_key:
            raise ValueError("real 模式需要 --product-key 或环境变量 LIE_PRODUCT_KEY")

    def probe_and_handshake(self, *, force_new: bool = False) -> bool:
        from ct_lie3_backend import lie3_track_start_round_if_enabled

        import os

        os.environ["LIE_PRODUCT_KEY"] = self._product_key
        if self._backend == "http":
            os.environ["LIE3_BACKEND"] = "roi_track_http"
        else:
            os.environ["LIE3_BACKEND"] = "roi_track_ws"
        resp = lie3_track_start_round_if_enabled(force_new=force_new)
        ok = isinstance(resp, dict) and str(resp.get("status") or "").lower() == "ok"
        log.info("[real] 握手 %s: %s", "成功" if ok else "失败", resp)
        return ok

    def end_round(self) -> None:
        from ct_lie3_backend import lie3_track_end_round_if_enabled

        lie3_track_end_round_if_enabled()

    def submit_frame(self, slot_bgr: np.ndarray, frame_interval_sec: float) -> Tuple[int, bool]:
        if self._backend == "http":
            from roitrack.http_client import get_roi_track_http_client

            client = get_roi_track_http_client()
            return client.submit_frame(slot_bgr, frame_interval_sec=frame_interval_sec)
        from roitrack.ws_client import get_roi_track_client

        client = get_roi_track_client()
        return client.submit_frame(slot_bgr, frame_interval_sec=frame_interval_sec)

    def wait_result(self, sent_fid: int, frame_interval_sec: float) -> Optional[Dict[str, Any]]:
        wait = min(3.0, max(DEFAULT_WAIT_RESULT_SEC, frame_interval_sec * 3.0))
        if self._backend == "http":
            from roitrack.http_client import get_roi_track_http_client

            return get_roi_track_http_client().wait_frame_result(sent_fid, max_wait_sec=wait)
        from roitrack.ws_client import get_roi_track_client

        return get_roi_track_client().wait_frame_result(
            sent_fid, timeout_sec=wait, frame_interval_sec=frame_interval_sec
        )


# ---------------------------------------------------------------------------
# 工作流样本：main ROI 定时截图 + 远程 track
# ---------------------------------------------------------------------------
@dataclass
class FrameRecord:
    index: int
    capture_ms: int
    sent_fid: int
    recv_fid: int
    roi_xy: Optional[Tuple[float, float]]
    decision: str
    pending: bool


class CtLie3WorkflowSample:
    """
    精简版 CT 采集循环：
      - 用合成信号代替模板匹配（lie3/log 置信度）
      - recording 阶段按 record_fps 节拍截 main ROI 并远程 track
    """

    def __init__(self, backend: RemoteTrackBackend, settings: SampleSettings) -> None:
        self.backend = backend
        self.settings = settings
        self.state = CaptureState.WATCHING
        self._lie3_streak = 0
        self._log_streak = 0
        self._armed = False
        self._handshake_ok = False
        self._prev_roi: Optional[Tuple[float, float]] = None
        self._frame_idx = 0
        self._records: List[FrameRecord] = []
        self._last_capture_ts = 0.0
        self.settings.output_dir.mkdir(parents=True, exist_ok=True)

    # ----- 合成场景（替代真实模板匹配） -----

    def _synthetic_lie3_conf(self, tick: int) -> float:
        if 8 <= tick <= 14:
            return 0.72 + 0.03 * np.sin(tick * 0.5)
        return 0.25 + random.uniform(-0.05, 0.05)

    def _synthetic_log_conf(self, tick: int) -> float:
        if tick >= 16:
            return 0.68 + 0.02 * np.sin(tick * 0.3)
        return 0.2

    def _synthetic_main_bgr(self, tick: int) -> np.ndarray:
        """模拟 lie3main ROI（例如 400×280），内部画移动亮点。"""
        w, h = 400, 280
        img = np.full((h, w, 3), 32, dtype=np.uint8)
        cx = int((tick * 7) % (w - 40) + 20)
        cy = int(h * 0.55)
        cv2.circle(img, (cx, cy), 10, (0, 220, 255), -1)
        cv2.putText(
            img,
            f"main tick={tick}",
            (8, 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (200, 200, 200),
            1,
            cv2.LINE_AA,
        )
        return img

    # ----- 远程 track 单帧（对齐 lie3answer714 直接采纳路径） -----

    def _track_one_frame(self, main_bgr: np.ndarray, frame_interval_sec: float) -> FrameRecord:
        self._frame_idx += 1
        slot_bgr, sx, sy = resize_to_slot(main_bgr)
        sent_fid, sent_ok = self.backend.submit_frame(slot_bgr, frame_interval_sec)
        recv_fid = 0
        roi: Optional[Tuple[float, float]] = None
        decision = "submit_failed"
        pending = False

        if not sent_ok or sent_fid <= 0:
            pending = True
        else:
            resp = self.backend.wait_result(sent_fid, frame_interval_sec)
            if resp is None or not resp.get("success"):
                pending = True
                decision = "async_pending"
                if self._prev_roi is not None:
                    roi = self._prev_roi
                    decision = "hold_prev"
                else:
                    roi = (main_bgr.shape[1] * 0.5, main_bgr.shape[0] * 0.5)
                    decision = "roi_center"
            else:
                recv_fid = int(resp.get("frame_id") or 0)
                x_slot = float(resp.get("x_main") or 0)
                y_slot = float(resp.get("y_main") or 0)
                roi = slot_to_main_roi(x_slot, y_slot, sx, sy)
                decision = str(resp.get("decision") or "track")
                self._prev_roi = roi

        capture_ms = int((time.perf_counter() - self._last_capture_ts) * 1000) if self._last_capture_ts else 0
        rec = FrameRecord(
            index=self._frame_idx,
            capture_ms=capture_ms,
            sent_fid=sent_fid,
            recv_fid=recv_fid,
            roi_xy=roi,
            decision=decision,
            pending=pending,
        )
        self._records.append(rec)
        return rec

    def _save_main_frame(self, main_bgr: np.ndarray, rec: FrameRecord) -> None:
        name = f"frame_{rec.index:04d}_fid{rec.sent_fid}.jpg"
        path = self.settings.output_dir / name
        cv2.imwrite(str(path), main_bgr)
        meta = {
            "file": name,
            "sent_fid": rec.sent_fid,
            "recv_fid": rec.recv_fid,
            "roi": rec.roi_xy,
            "decision": rec.decision,
            "pending": rec.pending,
        }
        with (self.settings.output_dir / "frames.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")

    def _sleep_until_next_tick(self, interval_sec: float) -> None:
        now = time.perf_counter()
        if self._last_capture_ts > 0:
            remain = interval_sec - (now - self._last_capture_ts)
            if remain > 0:
                time.sleep(remain)
        self._last_capture_ts = time.perf_counter()

    # ----- 主循环 -----

    def run(self) -> None:
        interval = 1.0 / max(0.5, float(self.settings.record_fps))
        tick = 0
        log.info("=== CT LIE3 工作流样本开始 ===")
        log.info("状态: %s", self.state.value)

        while tick < 40:
            tick += 1
            lie3_conf = self._synthetic_lie3_conf(tick)
            log_conf = self._synthetic_log_conf(tick)

            if self.state == CaptureState.WATCHING:
                if lie3_conf >= 0.65:
                    self._lie3_streak += 1
                else:
                    self._lie3_streak = 0
                if self._lie3_streak >= self.settings.lie3_confirm_hits:
                    log.info("[tick %d] lie3 确认 → prepared（置信度 %.3f）", tick, lie3_conf)
                    self.state = CaptureState.PREPARED
                    self._handshake_ok = self.backend.probe_and_handshake(force_new=True)
                    log.info("[prepared] 预握手 %s", "OK" if self._handshake_ok else "FAIL")
                time.sleep(self.settings.watch_interval_sec)
                continue

            if self.state == CaptureState.PREPARED:
                if log_conf >= self.settings.log_threshold:
                    self._log_streak += 1
                else:
                    self._log_streak = 0
                if self._log_streak >= self.settings.log_confirm_hits:
                    if not self._handshake_ok:
                        self._handshake_ok = self.backend.probe_and_handshake(force_new=True)
                    self._armed = self._handshake_ok
                    log.info(
                        "[tick %d] log 确认 → recording（log=%.3f, armed=%s）",
                        tick,
                        log_conf,
                        self._armed,
                    )
                    self.state = CaptureState.RECORDING
                    self._last_capture_ts = 0.0
                time.sleep(self.settings.prepare_interval_sec)
                continue

            if self.state == CaptureState.RECORDING:
                if not self._armed:
                    log.warning("未 armed，跳过发帧")
                    break
                self._sleep_until_next_tick(interval)
                main_bgr = self._synthetic_main_bgr(tick)
                rec = self._track_one_frame(main_bgr, interval)
                self._save_main_frame(main_bgr, rec)
                lag = max(0, rec.sent_fid - rec.recv_fid) if rec.recv_fid else "-"
                roi_s = (
                    f"({rec.roi_xy[0]:.1f},{rec.roi_xy[1]:.1f})" if rec.roi_xy else "none"
                )
                log.info(
                    "帧 %d sent=%s recv=%s lag=%s decision=%s roi=%s pending=%s",
                    rec.index,
                    rec.sent_fid,
                    rec.recv_fid or "-",
                    lag,
                    rec.decision,
                    roi_s,
                    rec.pending,
                )
                if rec.index >= self.settings.max_round_frames:
                    break
                continue

        self.backend.end_round()
        summary = {
            "state_end": self.state.value,
            "frames": len(self._records),
            "output_dir": str(self.settings.output_dir.resolve()),
        }
        path = self.settings.output_dir / "summary.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("=== 结束 === %s", summary)


def build_backend(args: argparse.Namespace) -> RemoteTrackBackend:
    if args.mock:
        return MockRemoteBackend(lag_frames=args.mock_lag)
    return RealRemoteBackend(backend=args.backend, product_key=args.product_key or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="CT LIE3 远程跟踪工作流样本")
    parser.add_argument("--mock", action="store_true", help="离线模拟（默认推荐先看这个）")
    parser.add_argument("--real", action="store_true", help="使用 CT/roitrack 真实客户端")
    parser.add_argument("--backend", choices=("http", "ws"), default="http")
    parser.add_argument("--product-key", default="", help="RoiTrack 技术卡密钥")
    parser.add_argument("--http-base", default="", help="覆盖 HTTP base，如 http://host:8004")
    parser.add_argument("--ws-host", default="", help="覆盖 WS host")
    parser.add_argument("--ws-ports", default="", help="覆盖 WS 端口，逗号分隔")
    parser.add_argument("--fps", type=float, default=DEFAULT_RECORD_FPS)
    parser.add_argument("--frames", type=int, default=12, help="recording 最多帧数")
    parser.add_argument("--mock-lag", type=int, default=1, help="mock 回包落后 sent 的帧数")
    parser.add_argument("--out", type=Path, default=Path("sample_captures"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if not args.mock and not args.real:
        args.mock = True

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    import os

    if args.http_base:
        os.environ["ROI_TRACK_HTTP_BASE"] = args.http_base.rstrip("/")
    if args.ws_host:
        os.environ["ROI_TRACK_WS_HOST"] = args.ws_host
    if args.ws_ports:
        os.environ["ROI_TRACK_WS_PORTS"] = args.ws_ports
    if args.product_key:
        os.environ["LIE_PRODUCT_KEY"] = args.product_key
    os.environ.setdefault("ROI_TRACK_HTTP_FRAME_STANDARD", str(int(args.fps)))

    settings = SampleSettings(
        record_fps=args.fps,
        max_round_frames=max(1, args.frames),
        output_dir=args.out,
    )
    backend = build_backend(args)
    sample = CtLie3WorkflowSample(backend, settings)
    sample.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
