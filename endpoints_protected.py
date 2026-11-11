"""The live RoiTrack deployment table, kept in ONE compiled module.

This module exists so the plaintext endpoints are not part of an end-user package:

* the release builders compile it with Nuitka (``--module``) into
  ``endpoints_protected.pyd`` and ship that instead of this source file;
* ``autolie_api/ip_port.txt`` (the vendor's own plaintext list) is no longer
  packaged either - ``write_vendor_endpoint_file()`` can regenerate it inside a
  runtime folder when a vendor code path insists on reading a file.

Nothing here imports the application, so it compiles standalone and stays usable
from both the source tree (dev) and a packaged .pyd.
"""

from __future__ import annotations

from pathlib import Path

# Every live WebSocket service instance, grouped by host.
# 222.215.232.230 (48001, 48002, 48003, 48007, 48009, 48011, 48013, 48015) was
# retired on the operator's word; only 117.50.223.113 stays live.  Keeping the old
# rows commented here (rather than deleting them) records that the client must not
# be pointed at that host again without a decision - the list is what a pass walks
# in random order, so a stale row would silently send passes there.
WS_ENDPOINTS: tuple[tuple[str, int], ...] = (
    # ("222.215.232.230", 48001),
    # ("222.215.232.230", 48002),
    # ("222.215.232.230", 48003),
    # ("222.215.232.230", 48007),
    # ("222.215.232.230", 48009),
    # ("222.215.232.230", 48011),
    # ("222.215.232.230", 48013),
    # ("222.215.232.230", 48015),
    ("117.50.223.113", 8001),
    ("117.50.223.113", 8002),
    ("117.50.223.113", 8003),
    ("117.50.223.113", 8007),
    ("117.50.223.113", 8009),
    ("117.50.223.113", 8011),
)

# HTTP sync instances of the same deployment.
HTTP_ENDPOINTS: tuple[tuple[str, int], ...] = (
    ("117.50.223.113", 8004),
    ("117.50.223.113", 8005),
    ("117.50.223.113", 8006),
)


def endpoint_hosts() -> tuple[str, ...]:
    """Unique hosts, in deployment order."""

    hosts: list[str] = []
    for host, _port in WS_ENDPOINTS + HTTP_ENDPOINTS:
        if host not in hosts:
            hosts.append(host)
    return tuple(hosts)


def write_vendor_endpoint_file(target: Path | str) -> Path:
    """Write the vendor ``ip_port.txt`` shape into *target* (runtime only).

    The file is never packaged; it exists only long enough for a vendor code path
    that insists on reading one.  Returns the path written.
    """

    path = Path(target)
    address = ",".join(str(port) for port in sorted({
        port for _host, port in HTTP_ENDPOINTS
    }))
    ws_address = ",".join(str(port) for port in sorted({
        port for _host, port in WS_ENDPOINTS
    }))
    host = HTTP_ENDPOINTS[0][0] if HTTP_ENDPOINTS else WS_ENDPOINTS[0][0]
    ws_host = WS_ENDPOINTS[0][0] if WS_ENDPOINTS else host
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{host}:{address}\n{ws_host}:{ws_address} //ws\n", encoding="utf-8"
    )
    return path


__all__ = [
    "HTTP_ENDPOINTS",
    "WS_ENDPOINTS",
    "endpoint_hosts",
    "write_vendor_endpoint_file",
]
