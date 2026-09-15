"""A local mimic of the RoiTrack backend (stdlib only): WS + HTTP, protocol 2.7.0.

It behaves like the real service on the parts we depend on:

* RFC 6455 upgrade, text and binary frames, ping/pong, close
* ``probe`` (§4.3): no key, no billing, publishes ``load.slots_left``
* ``handshake`` (§4.2): checks the key against the accepted keys, answers ``handshake_ack``
  with ``auth``, ``quota_left``, ``mode``, ``frame_standard`` and a ``config`` block
* ``frame`` (§5.1 base64 and §5.2 RTF1 binary): decodes the JPEG, **finds the target in it**
  and answers ``frame_result`` with ``x``/``y`` in the 372x248 ROI *and* ``x_main``/``y_main``
  in lie3main 745x496 - so a client can be checked against a known truth
* ``round_end``/``reset`` (§8), ``frame_id`` sequence checks, per-round billing (1 per ``infer``
  round), ``max_frame_bytes`` and ``image_too_large``
* HTTP: ``GET /v1/health``, ``POST /v1/track/session``, ``POST .../frame``, ``DELETE``

It is a test double, not a reimplementation: no ONNX model is run, the "target" is the brightest
blob in the ROI (which is what the real tracker follows in the lie popup).
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import cv2
import numpy as np

from autolie_api.ws_frames import (
    OP_BINARY,
    OP_CLOSE,
    OP_TEXT,
    encode_frame,
    read_frame,
    read_http_headers,
    server_handshake_response,
)

LOG = logging.getLogger("api_auto_lie")
SERVER_VERSION = "2.7.0"
ROI_W, ROI_H = 372, 248
MAIN_W, MAIN_H = 745, 496
MAX_FRAME_BYTES = 53760


def detect_bright_blob(bgr: np.ndarray, *, min_area: int = 12) -> Optional[tuple[float, float, int]]:
    """The brightest blob in an image -> (x, y, area) or None.

    The lie pass follows a white/glowing target, so "brightest blob" is the stand-in the mimic
    uses for the model's detection - and it gives the client an independent truth to compare its
    reverse mapping against.
    """

    if bgr is None or bgr.size == 0:
        return None
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
    peak = int(gray.max())
    if peak < 60:
        return None
    threshold = max(60, int(peak * 0.85))
    mask = (gray >= threshold).astype(np.uint8)
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    best_area, best_centroid = 0, None
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        if area >= min_area and area > best_area:
            best_area, best_centroid = area, centroids[index]
    if best_centroid is None:
        return None
    return float(best_centroid[0]), float(best_centroid[1]), best_area


@dataclass
class MockStats:
    connections: int = 0
    probes: int = 0
    handshakes: int = 0
    denied: int = 0
    frames: int = 0
    frames_binary: int = 0
    rounds: int = 0
    quota_charged: float = 0.0
    errors: list[str] = field(default_factory=list)


class MockRoiTrackServer:
    """A threaded mimic service. ``port=0`` picks a free port (read it back from .port)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, *,
                 accept_keys: Optional[set[str]] = None, quota: float = 100.0,
                 round_sec: float = 60.0, processing_ms: float = 25.0,
                 slots_left: int = 29, lag_frames: int = 0,
                 drop_after_frames: int = 0) -> None:
        self.host = host
        self.port = int(port)
        self.accept_keys = set(accept_keys) if accept_keys else None      # None = any key
        self.quota = float(quota)
        self.round_sec = float(round_sec)
        self.processing_ms = float(processing_ms)
        self.slots_left = int(slots_left)
        self.lag_frames = int(lag_frames)
        # >0: close the connection after answering that many frames, to exercise a client that
        # must survive a server-side reset (measured against the real service: WinError 10054)
        self.drop_after_frames = int(drop_after_frames)
        self.stats = MockStats()
        self.errors: list[dict] = []
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._connections: list[socket.socket] = []

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "MockRoiTrackServer":
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self.port = self._server.getsockname()[1]
        self._server.listen(8)
        self._running.set()
        self._thread = threading.Thread(target=self._accept_loop, name="mock-roitrack",
                                        daemon=True)
        self._thread.start()
        LOG.info("mimic backend listening on ws://%s:%d", self.host, self.port)
        return self

    def stop(self) -> None:
        self._running.clear()
        for connection in list(self._connections):
            try:
                connection.close()
            except OSError:
                pass
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "MockRoiTrackServer":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    # ------------------------------------------------------------------ connections
    def _accept_loop(self) -> None:
        assert self._server is not None
        while self._running.is_set():
            try:
                connection, _address = self._server.accept()
            except OSError:
                break
            self._connections.append(connection)
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        self.stats.connections += 1
        try:
            connection.settimeout(30.0)
            _line, headers = read_http_headers(connection)
            response = server_handshake_response(headers)
            if response is None:
                connection.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            connection.sendall(response)
            state = _ConnectionState()
            while self._running.is_set():
                opcode, payload = read_frame(connection)
                if opcode == OP_CLOSE:
                    break
                if opcode == OP_TEXT:
                    self._handle_text(connection, state,
                                      json.loads(payload.decode("utf-8")))
                elif opcode == OP_BINARY:
                    self._handle_rtf1(connection, state, payload)
                else:
                    self.stats.errors.append(f"unexpected opcode {opcode}")
        except (ConnectionError, OSError, ValueError) as exc:
            LOG.info("mimic backend: connection ended (%s)", exc)
        finally:
            try:
                connection.close()
            except OSError:
                pass
            if connection in self._connections:
                self._connections.remove(connection)

    # ------------------------------------------------------------------ protocol
    def _ack(self, message: dict, *, auth: bool, config: bool = False) -> dict:
        payload = {
            "type": "handshake_ack", "status": "ok", "server_version": SERVER_VERSION,
            "session_id": str(int(time.time() * 1000) % 10 ** 9), "auth": auth,
            "quota_left": int(self.quota), "round_active": False, "billing": "first_frame",
            "mode": message.get("mode", "infer"),
            "frame_standard": int(message.get("frame_standard", 7)),
            "image_transport": "base64",
        }
        if config:
            payload["config"] = {
                "roi_size": [ROI_W, ROI_H], "jpeg_quality": 90,
                "coord_space": "roi_top_left", "max_frame_bytes": MAX_FRAME_BYTES,
                "max_frame_b64_chars": 71680, "round_sec": self.round_sec,
                "consume_per_round_infer": 1, "consume_per_round_track": 0.5,
            }
        return payload

    def _handle_text(self, connection: socket.socket, state: "_ConnectionState",
                     message: dict) -> None:
        kind = str(message.get("type") or "")
        if kind == "probe":
            self.stats.probes += 1
            reply = self._ack(message, auth=False)
            reply.update({
                "mode": "probe", "billing": "none", "server_time": time.time(),
                "load": {"slots_left": self.slots_left,
                         "last_frame_ms": round(self.processing_ms, 1)},
                "config": {"roi_size": [ROI_W, ROI_H], "consume_per_round": 1.0,
                           "consume_per_round_track": 0.5, "round_sec": self.round_sec},
            })
            self._send(connection, reply, text=True)
            return
        if kind == "handshake":
            key = str(message.get("product_key") or "")
            if self.accept_keys is not None and key not in self.accept_keys:
                self.stats.denied += 1
                self._send(connection, {"type": "auth_denied", "success": False,
                                        "message": "product_key not accepted"}, text=True)
                return
            self.stats.handshakes += 1
            state.mode = str(message.get("mode") or "infer")
            state.frame_standard = int(message.get("frame_standard", 7))
            self._send(connection, self._ack(message, auth=True, config=True), text=True)
            return
        if kind == "round_end":
            state.frame_id_expected = 1
            state.round_open = False
            self.stats.rounds += 1
            self._send(connection, {"type": "round_ended", "success": True}, text=True)
            return
        if kind == "reset":
            state.frame_id_expected = 1
            state.round_open = False
            return
        if kind == "frame":
            image = None
            if "image" in message and message["image"]:
                import base64

                try:
                    raw = base64.b64decode(message["image"], validate=True)
                except Exception as exc:
                    self._error(connection, f"bad_frame_b64: {exc}", message)
                    return
                self.stats.frames += 1
                image = self._decode(connection, raw, message)
                if image is None:
                    return
            else:
                state.pending_text_frame = message          # §5.3 double packet
                state.pending_binary = True
                return
            self._answer_frame(connection, state, message, image)
            return
        self._error(connection, f"unknown type {kind!r}", message)

    def _handle_rtf1(self, connection: socket.socket, state: "_ConnectionState",
                     payload: bytes) -> None:
        if state.pending_binary and state.pending_text_frame is not None:
            # §5.3 binary double packet: JSON text first, then the raw JPEG
            message = state.pending_text_frame
            state.pending_text_frame, state.pending_binary = None, False
            self.stats.frames += 1
            image = self._decode(connection, payload, message)
            if image is None:
                return
            self._answer_frame(connection, state, message, image)
            return
        if payload[:4] != b"RTF1":
            self.stats.errors.append("bad_rtf1")
            self._send(connection, {"type": "error", "error": "bad_rtf1"}, text=True)
            return
        import struct

        frame_id, json_len, jpeg_len = struct.unpack("<III", payload[4:16])
        if len(payload) != 16 + json_len + jpeg_len:
            self.stats.errors.append("bad_rtf1_length")
            self._send(connection, {"type": "error", "error": "bad_rtf1"}, text=True)
            return
        message = json.loads(payload[16:16 + json_len].decode("utf-8"))
        jpeg = payload[16 + json_len:]
        self.stats.frames += 1
        self.stats.frames_binary += 1
        image = self._decode(connection, jpeg, message)
        if image is None:
            return
        self._answer_frame(connection, state, message, image, frame_id=frame_id)

    def _decode(self, connection: socket.socket, raw: bytes, message: dict):
        if len(raw) > MAX_FRAME_BYTES:
            self._error(connection, "image_too_large", message)
            return None
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            self._error(connection, "bad_image", message)
            return None
        return image

    def _answer_frame(self, connection: socket.socket, state: "_ConnectionState",
                      message: dict, image: np.ndarray, *,
                      frame_id: Optional[int] = None) -> None:
        frame_id = int(frame_id if frame_id is not None else message.get("frame_id") or 0)
        if frame_id <= 0:
            self._error(connection, "missing_frame_id", message)
            return
        if frame_id < state.frame_id_expected:
            self._error(connection, "frame_id_stale", message)
            return
        if frame_id > state.frame_id_expected:
            self._error(connection, "frame_id_gap", message)
            return
        state.frame_id_expected += 1
        if not state.round_open:
            state.round_open = True
            state.frames_in_round = 0
            state.round_started = time.time()
            cost = 1.0 if state.mode != "track" else 0.5
            self.quota -= cost
            self.stats.quota_charged += cost
        state.frames_in_round += 1
        state.frames_answered_total += 1
        # mimic a lagging pipeline: every Nth frame answers "keep the previous point"
        if self.lag_frames > 1 and state.frames_in_round % self.lag_frames == 0:
            self._send(connection, {
                "type": "frame_result", "frame_id": frame_id, "success": True,
                "mode": state.mode, "x": state.last_x, "y": state.last_y,
                "x_main": state.last_x_main, "y_main": state.last_y_main,
                "main_wh": [MAIN_W, MAIN_H], "decision": "hold_prev",
                "onnx_count": None, "parsed_count": 0, "api_ok": False,
                "api_skip_reason": "mimic_lag", "timing_ms": self.processing_ms,
                "frame_standard": state.frame_standard, "queue_depth": 0,
                "frames_in_round": state.frames_in_round,
                "quota_left": int(max(0, self.quota)), "round_active": True,
                "seconds_left": round(max(0.0, self.round_sec
                                          - (time.time() - state.round_started)), 1),
            }, text=True)
            return

        if self.drop_after_frames and state.frames_answered_total >= self.drop_after_frames:
            self.stats.errors.append("simulated_reset")
            LOG.info("mimic backend: simulating a connection reset after %d frames",
                     state.frames_answered_total)
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
            return
        blob = detect_bright_blob(image)
        if blob is None:
            x_slot, y_slot, area = ROI_W / 2.0, ROI_H / 2.0, 0
            api_ok = False
        else:
            x_slot, y_slot, area = blob
            api_ok = True
        x_main = x_slot * MAIN_W / float(ROI_W)
        y_main = y_slot * MAIN_H / float(ROI_H)
        state.last_x, state.last_y = x_slot, y_slot
        state.last_x_main, state.last_y_main = x_main, y_main
        if self.processing_ms > 0:
            time.sleep(self.processing_ms / 1000.0)
        self._send(connection, {
            "type": "frame_result", "frame_id": frame_id, "success": True,
            "mode": state.mode, "x": round(x_slot, 2), "y": round(y_slot, 2),
            "x_main": round(x_main, 2), "y_main": round(y_main, 2),
            "main_wh": [MAIN_W, MAIN_H], "decision": "track",
            "onnx_count": 1 if api_ok else 0, "parsed_count": int(area),
            "api_ok": api_ok, "half_mode": True, "timing_ms": self.processing_ms,
            "frame_standard": state.frame_standard, "queue_depth": 0,
            "frames_in_round": state.frames_in_round,
            "quota_left": int(max(0, self.quota)),
            "seconds_left": round(max(0.0, self.round_sec
                                      - (time.time() - state.round_started)), 1),
            "round_active": True,
        }, text=True)

    def _error(self, connection: socket.socket, code: str, message: dict) -> None:
        self.stats.errors.append(code)
        self.errors.append({"error": code, "frame_id": message.get("frame_id")})
        reply: dict[str, Any] = {"type": "error", "error": code}
        if message.get("frame_id"):
            reply["frame_id"] = message["frame_id"]
        self._send(connection, reply, text=True)

    def _send(self, connection: socket.socket, message: dict, *, text: bool) -> None:
        payload = json.dumps(message).encode("utf-8")
        connection.sendall(encode_frame(OP_TEXT if text else OP_BINARY, payload))


@dataclass
class _ConnectionState:
    mode: str = "infer"
    frame_standard: int = 7
    frame_id_expected: int = 1
    frames_answered_total: int = 0
    round_open: bool = False
    round_started: float = 0.0
    frames_in_round: int = 0
    frames_since_start: int = 0
    hold_frames_left: int = 0
    last_x: float = ROI_W / 2.0
    last_y: float = ROI_H / 2.0
    last_x_main: float = MAIN_W / 2.0
    last_y_main: float = MAIN_H / 2.0
    pending_binary: bool = False
    pending_text_frame: Optional[dict] = None


__all__ = ["MockRoiTrackServer", "MockStats", "detect_bright_blob"]
