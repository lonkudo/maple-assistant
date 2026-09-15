"""Building the backend connection: probe, choose a port, handshake - and log every step.

The protocol has **no HTTPS/auth pre-step** (2.7.0):

* §1: WS default ``ws://<host>:8001``; HTTP default ``http://<host>:8002`` (= WS port + 1).
  The client sends ``product_key`` in the handshake and must **not** send an authorisation-server
  address - the service does the key check itself.
* §4.1: after the TCP connect you have **10 s** to send ``handshake`` or ``probe``, or you are
  dropped; after the formal handshake, 10 s without valid frames drops you too.
* §4.3: ``probe`` is *server selection, no authentication*; it answers ``load.slots_left`` and
  ``load.last_frame_ms``, cannot send frames, and must be followed by the formal handshake.
* §4.2: the formal handshake (with the key) is what "asks the service": ``handshake_ack`` carries
  ``auth``, ``quota_left``, ``mode``, ``frame_standard`` and the server ``config``.

So the order is: **TCP -> WS upgrade -> probe (which port) -> fresh connection -> handshake
(auth + quota + config) -> frames**.  The HTTP block (8004-8006) is a *parallel* transport (§10)
with its own session and frame loop; its ``GET /v1/health`` is used here only as a diagnostic line
in the connection log - it never gates the WebSocket path, and ``POST /v1/track/session`` is not
called because that would open a second, HTTP session (which bills on its first frame).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from autolie_api.endpoints import http_endpoints, ws_endpoints
from autolie_api.key_store import load_product_key, mask_key
from autolie_api.logs import RunLog

LOG = logging.getLogger("api_auto_lie")
PROBE_TIMEOUT_SEC = 4.0
HEALTH_TIMEOUT_SEC = 3.0
TEST_KEY = "LIE-LOCAL-TEST"
MIMIC_NOTE = "本地模拟后端"
# The local mimic lags every 7th frame (a "hold_prev" answer), like the real pipeline does.
MIMIC_LAG_FRAMES = 7
# Test-only knob: with LIE_MIMIC_DROP_AFTER=40 the mimic closes the connection after answering 40
# frames, which is how the real service behaved mid-round (WinError 10054). Used by
# work/video_drill_check.py to prove the drill survives a reset - no quota is spent either way.
MIMIC_DROP_AFTER_ENV = "LIE_MIMIC_DROP_AFTER"


class BackendError(RuntimeError):
    """No usable backend: the reason is already logged and is meant for the panel."""


@dataclass
class ProbeResult:
    endpoint: str
    port: int
    host: str = ""
    ok: bool = False
    rtt_ms: float = 0.0
    slots_left: Optional[int] = None
    last_frame_ms: Optional[float] = None
    server_version: str = ""
    error: str = ""

    def describe(self) -> str:
        if not self.ok:
            return f"{self.endpoint} FAILED ({self.error})"
        return (f"{self.endpoint} {self.rtt_ms:.0f} ms slots_left={self.slots_left} "
                f"last_frame_ms={self.last_frame_ms} v{self.server_version}")


@dataclass
class BackendSession:
    """A live connection plus everything the logs and the panel need to describe it."""

    client: Any
    note: str
    key_source: str
    endpoint: str = ""
    slots_left: Optional[int] = None
    quota_left: Optional[int] = None
    mimic: Any = None
    probes: tuple[ProbeResult, ...] = ()

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            LOG.debug("api: client close failed", exc_info=True)
        if self.mimic is not None:
            try:
                self.mimic.stop()
            except Exception:
                LOG.debug("api: mimic stop failed", exc_info=True)


def _health_check(log: Optional[RunLog] = None) -> Optional[dict]:
    """Diagnostic only: ``GET /v1/health`` on the HTTP ports (never gates the WS path)."""

    import json
    from urllib import request as urlrequest

    from autolie_api.intergration import no_proxy_opener

    for endpoint in http_endpoints():
        started = time.perf_counter()
        url = f"http://{endpoint.host}:{endpoint.port}/v1/health"
        try:
            with no_proxy_opener().open(url, timeout=HEALTH_TIMEOUT_SEC) as response:
                body = response.read().decode("utf-8", "replace")
            elapsed = (time.perf_counter() - started) * 1000.0
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {"raw": body.strip()}
            if log is not None:
                log.connection("HTTP health (diagnostic, does not gate the WS path)",
                               url=url, rtt_ms=round(elapsed), body=payload)
            return payload
        except Exception as exc:
            if log is not None:
                log.connection("HTTP health failed (diagnostic only)", url=url, error=str(exc))
    return None


def probe_ws_endpoints(host: str = "", ports: Sequence[int] = (),
                       *, log: Optional[RunLog] = None,
                       timeout: float = PROBE_TIMEOUT_SEC) -> list[ProbeResult]:
    """§4.3 probe on every WS endpoint -> rtt + slots_left (no key, no billing)."""

    from autolie_api.endpoints import Endpoint
    from autolie_api.ws_client import RoiTrackWsClient

    if host:
        # an explicit host/ports wins: probe exactly what the caller asked for (a test may point
        # at a dead port on purpose), never silently fall back to the file's endpoints.
        wanted = [int(port) for port in ports] or [8001]
        targets = [Endpoint(host=host, port=port, transport="ws") for port in wanted]
    else:
        targets = list(ws_endpoints())
        if ports:
            targets = [item for item in targets if item.port in {int(p) for p in ports}]
    if not targets:
        raise BackendError("端点列表为空（autolie_api/ip_port.txt）")

    results: list[ProbeResult] = []
    for endpoint in targets:
        started = time.perf_counter()
        client = RoiTrackWsClient(endpoint.host, endpoint.port, timeout=timeout)
        result = ProbeResult(endpoint=endpoint.url, port=endpoint.port, host=endpoint.host)
        try:
            client.connect()
            probe = client.probe(client_info="maple_assistant_probe")
            result.rtt_ms = (time.perf_counter() - started) * 1000.0
            result.ok = True
            load = probe.get("load") if isinstance(probe.get("load"), dict) else {}
            slots = load.get("slots_left")
            result.slots_left = int(slots) if isinstance(slots, (int, float)) else None
            last = load.get("last_frame_ms")
            result.last_frame_ms = float(last) if isinstance(last, (int, float)) else None
            result.server_version = str(probe.get("server_version") or "")
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            client.close()
        results.append(result)
        if log is not None:
            log.connection("probe", endpoint=endpoint.url, ok=result.ok,
                           rtt_ms=round(result.rtt_ms), slots_left=result.slots_left,
                           last_frame_ms=result.last_frame_ms,
                           server_version=result.server_version or None,
                           error=result.error or None)
        LOG.info("api: probe %s", result.describe())
    return results


def choose_endpoint(results: Sequence[ProbeResult]) -> Optional[ProbeResult]:
    """Most free slots first, then lowest RTT - so several machines spread over the ports."""

    usable = [item for item in results if item.ok]
    if not usable:
        return None
    return sorted(usable, key=lambda item: (-(item.slots_left or 0), item.rtt_ms))[0]


def open_backend(*, key: str = "", host: str = "", ports: Sequence[int] = (),
                 transport: str = "base64", frame_standard: float = 5.0,
                 use_mimic: bool = False, health: bool = True,
                 log: Optional[RunLog] = None, client_info: str = "maple_assistant") -> BackendSession:
    """Build the connection: mimic when no key resolves, otherwise probe -> handshake.

    Raises :class:`BackendError` when the service cannot be reached, with a message meant for the
    panel; everything attempted is already in ``log``.
    """

    from autolie_api.ws_client import RoiTrackWsClient

    image_transport = ("bgr_jpeg90_packed" if str(transport).lower() == "rtf1"
                       else "bgr_jpeg90_base64")
    explicit_key, source = ("", "无") if use_mimic else load_product_key(key)

    if not explicit_key:
        from autolie_api.mock_backend import MockRoiTrackServer

        try:
            drop_after = int(os.environ.get(MIMIC_DROP_AFTER_ENV) or 0)
        except ValueError:
            drop_after = 0
        server = MockRoiTrackServer(port=0, accept_keys={TEST_KEY},
                                    lag_frames=MIMIC_LAG_FRAMES,
                                    drop_after_frames=drop_after).start()
        client = RoiTrackWsClient(server.host, server.port, timeout=5.0)
        try:
            client.connect()
            client.probe(client_info=client_info)
            ack = client.handshake(TEST_KEY, frame_standard=frame_standard,
                                   image_transport=image_transport, client_info=client_info)
        except Exception as exc:
            server.stop()
            raise BackendError(f"本地模拟后端启动失败：{exc}") from exc
        if log is not None:
            log.connection("backend: local mimic", url=server.url, reason="no key resolved")
        return BackendSession(client=client, note=f"{MIMIC_NOTE} {server.url}（未找到密钥）",
                              key_source="无", endpoint=server.url, mimic=server,
                              quota_left=ack.get("quota_left"))

    if log is not None:
        log.connection("key resolved", key=mask_key(explicit_key), source=source)
    LOG.info("api: key %s from %s", mask_key(explicit_key), source)

    results = probe_ws_endpoints(host, ports, log=log)
    if health:
        _health_check(log)
    best = choose_endpoint(results)
    if best is None:
        tried = ", ".join(f"{item.endpoint}({item.error})" for item in results)
        raise BackendError(f"所有端口都没有响应（{tried or 'no endpoints'}）")
    if log is not None:
        log.connection("chosen endpoint", endpoint=best.endpoint, rtt_ms=round(best.rtt_ms),
                       slots_left=best.slots_left,
                       reason="most free slots, then lowest RTT")

    client = RoiTrackWsClient(best.host or ws_endpoints()[0].host, best.port, timeout=5.0)
    try:
        started = time.perf_counter()
        client.connect()
        ack = client.handshake(explicit_key, frame_standard=frame_standard,
                               image_transport=image_transport, client_info=client_info)
        connect_ms = (time.perf_counter() - started) * 1000.0
    except Exception as exc:
        client.close()
        if log is not None:
            log.connection("handshake failed", endpoint=best.endpoint, error=str(exc))
        raise BackendError(f"握手失败（密钥来自 {source}）：{exc}") from exc

    if log is not None:
        log.connection("handshake_ack", auth=ack.get("auth"), mode=ack.get("mode"),
                       frame_standard=ack.get("frame_standard"),
                       quota_left=ack.get("quota_left"),
                       connect_ms=round(connect_ms),
                       config=ack.get("config"))
    return BackendSession(
        client=client,
        note=(f"真实后端 {best.endpoint} 密钥来自{source} "
              f"(quota_left={ack.get('quota_left')}, slots_left={best.slots_left})"),
        key_source=source, endpoint=best.endpoint, slots_left=best.slots_left,
        quota_left=ack.get("quota_left"), probes=tuple(results),
    )


__all__ = ["BackendError", "BackendSession", "ProbeResult", "choose_endpoint", "open_backend",
           "probe_ws_endpoints"]
