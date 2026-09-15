"""Our RoiTrack WebSocket client (stdlib only, per 2.7.0 §4-§8).

Only what the service needs: connect + handshake, optional probe, frame upload as base64 JSON
(§5.1) or RTF1 binary (§5.2), JSON ``frame_result`` receive, ``round_end``, close.

    from autolie_api.ws_client import RoiTrackWsClient

    client = RoiTrackWsClient("127.0.0.1", 8001)
    client.connect()
    print(client.probe())
    print(client.handshake("LIE-TEST-KEY", frame_standard=5))
    frame_id, ok = client.send_frame_base64(jpeg_bytes, frame_interval_sec=0.2)
    result = client.wait_result(frame_id, timeout=2.0)
"""

from __future__ import annotations

import json
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from autolie_api.ws_frames import (
    OP_CLOSE,
    OP_TEXT,
    client_handshake_request,
    encode_frame,
    read_frame,
)

LOG = logging.getLogger("api_auto_lie")
HANDSHAKE_TIMEOUT_SEC = 10.0
# 2.7.0 §3: how many frames may be in flight before the server answers frame_buffer_full.
BUFFER_LIMITS = {7: 4, 6: 4, 5: 3, 4: 3, 3: 2}


@dataclass
class WsStats:
    frames_sent: int = 0
    frames_sent_bytes: int = 0
    results_received: int = 0
    errors: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return (f"sent={self.frames_sent} ({self.frames_sent_bytes / 1024:.0f} KB) "
                f"results={self.results_received} errors={len(self.errors)}")


class RoiTrackWsClient:
    """A small, explicit client: one connection, one round, frames in/out."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8001, *,
                 timeout: float = 5.0) -> None:
        self.host = str(host)
        self.port = int(port)
        self.timeout = float(timeout)
        self.sock: Optional[socket.socket] = None
        self.handshake_ack: dict = {}
        self.stats = WsStats()
        self._frame_id = 0
        self._pending: dict[int, dict] = {}

    # ------------------------------------------------------------------ connection
    def connect(self) -> bool:
        """TCP connect + WebSocket upgrade."""

        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self.sock.sendall(client_handshake_request(self.host, self.port))
        head = b""
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT_SEC
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("server closed during the upgrade")
            head += chunk
            if time.monotonic() > deadline:
                raise TimeoutError("upgrade timed out")
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise ConnectionError(f"upgrade refused: {status}")
        LOG.info("api lie ws: connected to ws://%s:%d (%s)", self.host, self.port, status)
        return True

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self.sock.sendall(encode_frame(OP_CLOSE, b""))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None

    # ------------------------------------------------------------------ messages
    def _send_json(self, message: dict) -> None:
        if self.sock is None:
            raise ConnectionError("not connected")
        self.sock.sendall(encode_frame(OP_TEXT, json.dumps(message).encode("utf-8"),
                                       mask=True))

    def _send_binary(self, payload: bytes) -> None:
        if self.sock is None:
            raise ConnectionError("not connected")
        self.sock.sendall(encode_frame(0x2, payload, mask=True))

    def probe(self, client_info: str = "maple_assistant") -> dict:
        """§4.3: pick/verify a server; no key, no billing, no frames."""

        self._send_json({"type": "probe", "client_info": client_info,
                         "client_ts": time.time()})
        return self._next_json()

    def handshake(self, product_key: str, *, mode: str = "infer", frame_standard: int = 5,
                  image_transport: str = "bgr_jpeg90_base64",
                  client_info: str = "maple_assistant") -> dict:
        """§4.2: the real, authenticated handshake (it does not bill)."""

        self._send_json({
            "type": "handshake", "product_key": product_key, "mode": mode,
            "frame_standard": int(frame_standard), "image_transport": image_transport,
            "client_info": client_info,
        })
        ack = self._next_json()
        self.handshake_ack = ack
        if ack.get("type") == "auth_denied":
            raise PermissionError(f"handshake refused: {ack.get('message')}")
        if not ack.get("auth", False):
            raise PermissionError(f"handshake not authenticated: {ack}")
        self._frame_id = 0
        LOG.info("api lie ws: handshake ok, quota_left=%s, config=%s",
                 ack.get("quota_left"), ack.get("config"))
        return ack

    def send_frame_base64(self, jpeg: bytes, *, frame_interval_sec: float,
                          frame_id: Optional[int] = None) -> tuple[int, bool]:
        """§5.1: the base64 JSON frame. Returns (frame_id, sent).

        ``frame_id`` may be given explicitly (a capture loop owns the numbering); otherwise the
        client's own counter advances, which starts at 1 for every round per §3.
        """

        import base64 as _b64

        self._frame_id = int(frame_id) if frame_id else self._frame_id + 1
        self._send_json({
            "type": "frame", "frame_id": self._frame_id,
            "image": _b64.b64encode(jpeg).decode("ascii"),
            "frame_interval_sec": round(float(frame_interval_sec), 4),
        })
        self.stats.frames_sent += 1
        self.stats.frames_sent_bytes += len(jpeg)
        return self._frame_id, True

    def send_frame_rtf1(self, frame_id: int, jpeg: bytes, *,
                        frame_interval_sec: float) -> tuple[int, bool]:
        """§5.2: one binary packet, no base64 (``image_transport: bgr_jpeg90_packed``)."""

        from autolie_api.intergration import pack_rtf1

        meta = {"type": "frame", "frame_id": int(frame_id),
                "frame_interval_sec": round(float(frame_interval_sec), 4)}
        packet = pack_rtf1(frame_id, meta, jpeg)
        self._send_binary(packet)
        self.stats.frames_sent += 1
        self.stats.frames_sent_bytes += len(jpeg)
        return int(frame_id), True

    def end_round(self, *, wait: bool = True, timeout: float = 2.0) -> Optional[dict]:
        """§8: finish the round; the next round's frame_id starts at 1 again.

        A service that closes the socket instead of answering ``round_ended`` is not an error -
        the round is over either way (measured: at the end of a 150-frame round the connection was
        already closed when the ack would have arrived).  The caller gets None and logs it.
        """

        try:
            self._send_json({"type": "round_end"})
        except OSError as exc:
            LOG.info("api lie ws: round_end could not be sent (%s)", exc)
            self._frame_id = 0
            return None
        ack: Optional[dict] = None
        if wait:
            try:
                ack = self._next_json(timeout=timeout)
            except (ConnectionError, TimeoutError, BlockingIOError, OSError) as exc:
                LOG.info("api lie ws: no round_ended ack (%s); the round is over", exc)
        self._frame_id = 0
        return ack

    # ------------------------------------------------------------------ receiving
    def _next_json(self, timeout: Optional[float] = None) -> dict:
        opcode, payload = self._next_frame(timeout)
        if opcode != OP_TEXT:
            raise ValueError(f"expected a text frame, got opcode {opcode}")
        return json.loads(payload.decode("utf-8"))

    def _next_frame(self, timeout: Optional[float] = None) -> tuple[int, bytes]:
        if self.sock is None:
            raise ConnectionError("not connected")
        if timeout is not None:
            # timeout=0 would make the socket non-blocking and raise BlockingIOError instead;
            # a millisecond keeps the "poll without blocking" semantics safe.
            self.sock.settimeout(max(0.001, float(timeout)))
        try:
            opcode, payload = read_frame(self.sock)
        finally:
            if timeout is not None:
                self.sock.settimeout(self.timeout)
        if opcode == OP_CLOSE:
            raise ConnectionError("server closed the connection")
        return opcode, payload

    def poll_results(self, *, timeout: float = 0.0) -> list[dict]:
        """Every ``frame_result`` currently readable (never blocks longer than timeout).

        A read *timeout* means "nothing yet" and returns what was collected.  A **closed or reset
        connection** is not a timeout: it is raised to the caller, which must rebuild the session
        (measured in the field: the service reset the socket mid-round, and swallowing that as "no
        answer" cost two frames and a 1.5 s wait per reset).  ``ConnectionError`` is a subclass of
        ``OSError``, so it has to be excluded explicitly.
        """

        results: list[dict] = []
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and results:
                break
            try:
                message = self._next_json(timeout=max(0.0, remaining))
            except (TimeoutError, BlockingIOError):
                break
            if message.get("type") == "frame_result":
                self.stats.results_received += 1
                self._pending[int(message.get("frame_id") or 0)] = message
                results.append(message)
            elif message.get("error"):
                self.stats.errors.append(str(message["error"]))
        return results

    def wait_result(self, sent_frame_id: int, *, timeout: float = 2.0) -> Optional[dict]:
        """The result for ``sent_frame_id`` (or any later one already received)."""

        if sent_frame_id in self._pending:
            return self._pending.pop(sent_frame_id)
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            self.poll_results(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
            for frame_id in sorted(self._pending):
                if frame_id >= sent_frame_id:
                    return self._pending.pop(frame_id)
        return None
