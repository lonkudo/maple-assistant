"""TodoHelper's live RoiTrack deployment route.

The bundled ``autolie_api`` folder is vendor reference material and keeps its
original endpoint list. The application selects its live service here so an
endpoint migration never changes vendor files or the licensing backend.

Both live deployments are listed, six WebSocket service instances in total:
the three on the original host and the three the vendor's own list names
(``autolie_api/ip_port.txt``).  A pass starts on a randomly chosen instance and
moves to the next one when a handshake fails, so a single dead or overloaded
service cannot take automatic lie handling down.
"""

from __future__ import annotations

import secrets
from typing import Optional, Sequence

# Every live WebSocket service instance, grouped by host.
AUTO_LIE_WS_ENDPOINTS: tuple[tuple[str, int], ...] = (
    ("221.10.121.69", 48001),
    ("221.10.121.69", 48002),
    ("221.10.121.69", 48003),
    ("117.50.223.113", 8001),
    ("117.50.223.113", 8002),
    ("117.50.223.113", 8003),
)


def host_ports(host: str) -> tuple[int, ...]:
    """Every WebSocket port registered for *host*, in list order."""

    wanted = str(host).strip()
    return tuple(
        port for endpoint_host, port in AUTO_LIE_WS_ENDPOINTS
        if endpoint_host == wanted
    )


def describe_ws_endpoint(host: str, port: int) -> str:
    """Return a log-safe live WebSocket address."""

    return f"ws://{host}:{int(port)}"


def select_auto_lie_ws_endpoints() -> tuple[tuple[str, int], ...]:
    """All six service instances in a random starting order.

    The choice is deliberately random rather than a fingerprint modulo: the
    modulo spread known devices over one host's three workers, so adding a
    second host (or losing one) left the assignment lopsided, and a device
    whose worker was down had no alternative to fall back to.  The caller walks
    this list, keeps the first instance that answers, and uses the next entry
    whenever a handshake fails.
    """

    endpoints = list(AUTO_LIE_WS_ENDPOINTS)
    # Fisher-Yates with the system entropy source: uniform over all six orders.
    for index in range(len(endpoints) - 1, 0, -1):
        swap = secrets.randbelow(index + 1)
        endpoints[index], endpoints[swap] = endpoints[swap], endpoints[index]
    return tuple(endpoints)


def ordered_candidates(
    cached: Optional[tuple[str, int]],
    candidates: Sequence[tuple[str, int]],
) -> tuple[tuple[str, int], ...]:
    """The instances a pass should try, in order.

    The endpoint the startup probe already answered on comes first, then every
    remaining instance of the same random order with no duplicates.  With no
    usable cached endpoint, or an empty candidate list, the whole deployment is
    returned in a fresh random order, so a pass always has something to try.
    """

    ordered: list[tuple[str, int]] = []
    if cached is not None:
        cached_host = str(cached[0] or "").strip()
        try:
            cached_port = int(cached[1])
        except (TypeError, ValueError):
            cached_port = 0
        if cached_host and cached_port > 0:
            ordered.append((cached_host, cached_port))
    for host, port in candidates:
        entry = (str(host), int(port))
        if entry not in ordered:
            ordered.append(entry)
    return tuple(ordered) if ordered else select_auto_lie_ws_endpoints()


__all__ = [
    "AUTO_LIE_WS_ENDPOINTS",
    "describe_ws_endpoint",
    "host_ports",
    "ordered_candidates",
    "select_auto_lie_ws_endpoints",
]
