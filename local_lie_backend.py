"""本地测谎 backend: the CT partner API on 127.0.0.1:8765.

测谎 (lie detection) has two backends:

* **云端测谎** - the vendor ROI-track service over WebSocket (``autolie_api``),
  billed per round through the licence heartbeat's WS key.
* **本地测谎** - the CT partner API this repository ships as
  ``CT_Lie3_KR_partner_api_DML``.  It listens on ``http://127.0.0.1:8765`` and
  does the same work locally: ONNX detection of the lie3main picture plus local
  tw_jingdian tracking, answering with the point to click.  It bills its own
  CT_IN card and WS quota, so the app needs no online 测谎密钥 in this mode.

The drills (``api_lie_test.ApiLieTestWorker`` / ``api_lie_video.VideoDrillWorker``)
speak one small client protocol, so this module only has to present the partner
API as that protocol:

    client.send_frame_rtf1(frame_id, jpeg, *, frame_interval_sec)
    client.send_frame_base64(jpeg, *, frame_interval_sec, frame_id)
    client.poll_results(*, timeout=0.0) -> list[dict]
    client.end_round(*, wait=True, timeout=2.0) -> dict | None
    client.close()
    client._pending: dict[int, dict]        # the drills pop released answers

Answers are the vendor's ``frame_result`` shape (``autolie_api.intergration
.parse_server_point`` reads it), i.e. ``x``/``y`` are the protocol/slot ROI
pixels the geometry maps back onto the screen.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

LOG = logging.getLogger(__name__)

# The packaged partner API binds loopback 8765 (see 同行对接使用说明.txt).
LOCAL_LIE_HOST = "127.0.0.1"
LOCAL_LIE_PORT = 8765
# Startup handshake and licence validation happen before 8765 listens, so a
# health probe needs room for a cold service without stalling a lie event: the
# auto-lie pass only ever asks a service that answered a probe.
HEALTH_TIMEOUT_SEC = 3.0
FRAME_TIMEOUT_SEC = 15.0
SESSION_TIMEOUT_SEC = 20.0
# The partner API's own frame standard is 3..7 and the drills feed at 5 fps.
DEFAULT_FRAME_STANDARD = 5
DEFAULT_INFER = "dml"


class LocalLieError(RuntimeError):
    """The local service is unusable; the message is meant for the panel."""


def _opener():
    """Direct opener: the loopback service must never go through a proxy."""

    return urlrequest.build_opener(urlrequest.ProxyHandler({}))


def _json_request(url: str, *, payload: Optional[dict] = None,
                  timeout: float = HEALTH_TIMEOUT_SEC) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urlrequest.Request(
        url, data=data, method="GET" if payload is None else "POST",
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    with _opener().open(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    try:
        parsed = json.loads(body)
    except ValueError as exc:
        raise LocalLieError(f"本地测谎服务返回了非 JSON 响应：{body[:120]}") from exc
    if not isinstance(parsed, dict):
        raise LocalLieError(f"本地测谎服务返回了意外的响应：{body[:120]}")
    return parsed


def base_url(host: str = LOCAL_LIE_HOST, port: int = LOCAL_LIE_PORT) -> str:
    return f"http://{host}:{int(port)}"


def probe_local_lie_service(
    host: str = LOCAL_LIE_HOST, port: int = LOCAL_LIE_PORT,
    *, timeout: float = HEALTH_TIMEOUT_SEC,
) -> tuple[bool, str]:
    """``(ok, note)`` for ``GET /v1/health`` - never raises."""

    url = base_url(host, port) + "/v1/health"
    try:
        payload = _json_request(url, timeout=timeout)
    except (urlerror.URLError, OSError, LocalLieError) as exc:
        return False, f"{base_url(host, port)} 未就绪：{exc}"
    if not payload.get("ok"):
        return False, f"{base_url(host, port)} 健康检查未通过：{payload}"
    licence = payload.get("partner_license_startup") or {}
    handshake = payload.get("ws_startup_handshake") or {}
    parts = [f"{base_url(host, port)} 就绪"]
    if licence and not licence.get("ok"):
        parts.append("月卡未通过")
    if licence.get("license_expire"):
        parts.append(f"月卡 {licence['license_expire']}")
    if handshake.get("ok"):
        parts.append(f"次数 {handshake.get('quota_left')}")
    if payload.get("local_infer_available") is False:
        parts.append("本地模型不可用")
    return True, "，".join(parts)


def open_local_lie_backend(
    log: Any = None, *, host: str = LOCAL_LIE_HOST, port: int = LOCAL_LIE_PORT,
    frame_standard: float = DEFAULT_FRAME_STANDARD, infer: str = DEFAULT_INFER,
) -> Any:
    """Build a session for the drills; raises :class:`LocalLieError` otherwise.

    Returns the vendor ``BackendSession`` (client + note + the fields the logs
    and panel read), so both drills and the auto-lie pass can treat the local
    service exactly like the cloud one.
    """

    from autolie_api.connect import BackendSession

    client = LocalLieClient(
        host=host, port=port, frame_standard=frame_standard, infer=infer,
        log=log,
    )
    note = client.open()
    return BackendSession(
        client=client, note=note, key_source="本地测谎（127.0.0.1:8765）",
        endpoint=base_url(host, port), quota_left=client.quota_left,
    )


class LocalLieClient:
    """The CT partner API presented as the drills' client protocol."""

    def __init__(
        self, *, host: str = LOCAL_LIE_HOST, port: int = LOCAL_LIE_PORT,
        frame_standard: float = DEFAULT_FRAME_STANDARD, infer: str = DEFAULT_INFER,
        log: Any = None,
    ) -> None:
        self.host = str(host or LOCAL_LIE_HOST)
        self.port = int(port or LOCAL_LIE_PORT)
        self.frame_standard = int(max(3, min(7, round(float(frame_standard or DEFAULT_FRAME_STANDARD)))))
        self.infer = "cpu" if str(infer).casefold() == "cpu" else DEFAULT_INFER
        self.log = log
        self.session_id: str = ""
        self.quota_left: Optional[int] = None
        # The drills release their own answers from this cache (they pop the
        # frame_id they handled), so it must exist and stay an ordinary dict.
        self._pending: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._closed = False
        # The last answer that carried a point; a later detection-less frame
        # repeats it as the protocol's ``hold_prev`` instead of publishing an
        # answer without coordinates.
        self._last_point: Optional[dict] = None
        # Counts frames the service could not resolve, so the "no target" hint is
        # logged once per run instead of on every frame.
        self._no_point_frames = 0

    # ------------------------------------------------------------------ session
    def open(self) -> str:
        """Health probe + ``POST /v1/session``; returns the panel note."""

        ok, note = probe_local_lie_service(self.host, self.port)
        if not ok:
            raise LocalLieError(note)
        payload = _json_request(
            base_url(self.host, self.port) + "/v1/session",
            payload={"infer": self.infer, "frame_standard": self.frame_standard},
            timeout=SESSION_TIMEOUT_SEC,
        )
        if not payload.get("ok"):
            raise LocalLieError(
                "本地测谎服务无法开 session："
                + str(payload.get("error") or payload)
            )
        self.session_id = str(payload.get("session_id") or "")
        if not self.session_id:
            raise LocalLieError("本地测谎服务没有返回 session_id。")
        quota = payload.get("quota_left")
        if isinstance(quota, (int, float)):
            self.quota_left = int(quota)
        self._log(
            "connection", "本地测谎 session 已建立", session_id=self.session_id,
            infer=payload.get("infer"), frame_standard=payload.get("frame_standard"),
            quota_left=self.quota_left,
        )
        return note

    def _log(self, channel: str, message: str, **fields: Any) -> None:
        logger = getattr(self.log, channel, None)
        if callable(logger):
            try:
                logger(message, **fields)
            except Exception:  # noqa: BLE001 - logging must never break a frame
                LOG.debug("local lie log failed", exc_info=True)
        LOG.info("%s %s", message, fields)

    # -------------------------------------------------------------------- frames
    def send_frame_rtf1(self, frame_id: int, jpeg: bytes, *,
                        frame_interval_sec: float = 0.2) -> None:
        self.send_frame_base64(jpeg, frame_interval_sec=frame_interval_sec,
                               frame_id=frame_id)

    def send_frame_base64(self, jpeg: bytes, *, frame_interval_sec: float = 0.2,
                          frame_id: int = 1) -> None:
        """POST the JPEG; the answer is cached under ``frame_id``.

        The partner API answers synchronously, so one request is one answer.
        A transport failure is raised as ``ConnectionError`` like the cloud
        client, which is what makes the drills rebuild the session.
        """

        if self._closed or not self.session_id:
            raise ConnectionError("本地测谎 session 已关闭")
        url = (
            base_url(self.host, self.port)
            + f"/v1/frame?session_id={self.session_id}"
        )
        request = urlrequest.Request(
            url, data=jpeg, method="POST",
            headers={"Content-Type": "image/jpeg"},
        )
        try:
            with _opener().open(request, timeout=FRAME_TIMEOUT_SEC) as response:
                body = response.read().decode("utf-8", "replace")
        except (urlerror.URLError, OSError) as exc:
            raise ConnectionError(f"本地测谎送帧失败：{exc}") from exc
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise ConnectionError(f"本地测谎返回了非 JSON 响应：{body[:120]}") from exc
        if not isinstance(payload, dict):
            raise ConnectionError(f"本地测谎返回了意外的响应：{body[:120]}")
        answer = self._translate(payload, int(frame_id))
        if answer is None:
            # The service detected nothing in this frame.  The cloud protocol
            # expresses that as "hold the previous point"; with no previous point
            # there is nothing to send at all, so the drill keeps feeding instead
            # of crashing on an answer without coordinates.
            return
        with self._lock:
            self._pending[int(frame_id)] = answer
        quota = answer.get("quota_left")
        if isinstance(quota, int):
            self.quota_left = quota

    def _translate(self, payload: dict, frame_id: int) -> Optional[dict]:
        """Partner API answer -> the vendor ``frame_result`` keys.

        The partner API says ``ok``; the vendor result says ``success``.  The
        point itself is already protocol-ROI pixels (``coord_space:
        roi_top_left``, ``roi_size`` 372x248), which is what the drill geometry
        expects, so ``x``/``y`` pass through unchanged.

        A frame the service could not resolve (``ok: false`` with null
        coordinates - normal while its local tracker has nothing to lock on)
        must NEVER reach the drill without a point: ``ServerPoint.to_client()``
        raises ``ValueError: frame_result carries no x/y`` and kills the whole
        pass ("the local service was stopped on both runs", field log 21:35).
        It becomes the protocol's ``hold_prev`` - the last point is repeated -
        and when there is no last point yet, ``None`` means "no answer yet".
        """

        ok = bool(payload.get("ok"))
        x = payload.get("x")
        y = payload.get("y")
        if x is None or y is None:
            if not ok and self._no_point_frames == 0:
                self._no_point_frames = 1
                self._log(
                    "detection",
                    "本地测谎: 这一帧没有识别到目标（服务需要 lie3main 大图）；"
                    "继续送帧，收到坐标前不会给出答案",
                    frame_id=frame_id,
                    error=payload.get("error"),
                )
            if self._last_point is None:
                return None
            hold = dict(self._last_point)
            hold.update({
                "frame_id": frame_id,
                "success": True,
                "decision": "hold_prev",
                "timing_ms": payload.get("ms"),
                "quota_left": payload.get("quota_left", hold.get("quota_left")),
                "raw": payload,
            })
            return hold
        self._no_point_frames = 0
        answer: dict[str, Any] = {
            "frame_id": frame_id,
            "success": ok,
            "decision": str(payload.get("decision") or "track"),
            "x": x,
            "y": y,
            "x_main": payload.get("x_main"),
            "y_main": payload.get("y_main"),
            "quota_left": payload.get("quota_left"),
            "timing_ms": payload.get("ms"),
            "mode": str(payload.get("track") or ""),
            "source": "local_8765",
            "raw": payload,
        }
        self._last_point = dict(answer)
        return answer

    def poll_results(self, *, timeout: float = 0.0) -> list[dict]:
        """Answers ready since the last poll (oldest first)."""

        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._lock:
                ready = [self._pending[key] for key in sorted(self._pending)]
            if ready or time.monotonic() >= deadline:
                return ready
            time.sleep(0.01)

    # --------------------------------------------------------------------- round
    def end_round(self, *, wait: bool = True,
                  timeout: float = 2.0) -> Optional[dict]:
        """``POST /v1/close`` - the partner API ends the session itself."""

        if not self.session_id:
            return None
        try:
            payload = _json_request(
                base_url(self.host, self.port) + "/v1/close",
                payload={"session_id": self.session_id},
                timeout=max(1.0, float(timeout)),
            )
        except (urlerror.URLError, OSError, LocalLieError) as exc:
            LOG.debug("local lie close failed: %s", exc)
            return None
        self._log("connection", "本地测谎 session 已关闭",
                  session_id=self.session_id, ok=payload.get("ok"))
        return payload

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.end_round(wait=False, timeout=2.0)
        finally:
            with self._lock:
                self._pending.clear()


__all__ = [
    "DEFAULT_FRAME_STANDARD", "DEFAULT_INFER", "LOCAL_LIE_HOST", "LOCAL_LIE_PORT",
    "LocalLieClient", "LocalLieError", "base_url", "open_local_lie_backend",
    "probe_local_lie_service",
]
