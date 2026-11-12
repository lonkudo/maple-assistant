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
#
# 117.50.223.113 was reduced to four service ports on the operator's word:
# 8001, 8002, 8003 and the 8888 dispatcher.  8888 speaks the same protocol as the
# three workers (handshake ``product_key`` -> quota/config, then ``{"type":"frame"}``
# with the ROI JPEG as base64 in ``image``), so it is an ordinary pool member.
#
# Every port on that host was then measured (10-20 WebSocket upgrades each, plus a
# keyless capacity probe) and the verdict drives these two lists:
#
#   WS, answers ``101`` on every attempt: 8001, 8003, 8007, 8009, 8010, 8013
#   WS, answers ``101`` about 70 % of attempts, an HTTP sync service owns the
#   rest of the connections (double bind): 8888
#   HTTP sync only (``GET /v1/health`` -> ``service: roi-track-http, mode: sync``,
#   WebSocket upgrade always ``404``): 8002, 8011, 8012
#   Other services on the host, not lie endpoints: 8004 ``MXDPPLicense`` (CT card
#   authorization), 8005 (403), 8006 ``Werkzeug`` (the Flask activation API),
#   8008 closed.
#
# 8007, 8009, 8010 and 8013 are outside the reduced set but still serve the WS
# protocol with 30 free slots each, so they stay in the pool: the operator asked
# for every usable port to be preflighted.  A port that never upgrades must not sit
# in the WS list - it would waste a preflight candidate on every pass - which is why
# 8002/8011/8012 live in the HTTP list below instead.
WS_ENDPOINTS: tuple[tuple[str, int], ...] = (
    # ("222.215.232.230", 48001),
    # ("222.215.232.230", 48002),
    # ("222.215.232.230", 48003),
    # ("222.215.232.230", 48007),
    # ("222.215.232.230", 48009),
    # ("222.215.232.230", 48011),
    # ("222.215.232.230", 48013),
    # ("222.215.232.230", 48015),
    # ("117.50.223.113", 8002),      # HTTP sync only
    # ("117.50.223.113", 8011),      # HTTP sync only
    # ("117.50.223.113", 8012),      # HTTP sync only
    # ("117.50.223.113", 8004),      # CT card licence server, not a lie endpoint
    # ("117.50.223.113", 8005),      # 403
    # ("117.50.223.113", 8006),      # Flask activation API
    # ("117.50.223.113", 8008),      # closed
    ("117.50.223.113", 8001),
    ("117.50.223.113", 8003),
    ("117.50.223.113", 8007),
    ("117.50.223.113", 8009),
    ("117.50.223.113", 8010),
    ("117.50.223.113", 8013),
    ("117.50.223.113", 8888),
)

# HTTP sync instances of the same deployment (``/v1/session``, ``/v1/frame``,
# ``/v1/close``, ``http_slots_left`` 10, 45 s session TTL).  8004-8006 used to be
# listed here; they are not part of this deployment any more - 8004 is the CT card
# authorization port, which ``local_lie_service.AUTH_SERVER_PORT`` configures
# independently, and 8006 is the activation API.
HTTP_ENDPOINTS: tuple[tuple[str, int], ...] = (
    ("117.50.223.113", 8002),
    ("117.50.223.113", 8011),
    ("117.50.223.113", 8012),
    # ("117.50.223.113", 8004),
    # ("117.50.223.113", 8005),
    # ("117.50.223.113", 8006),
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
    lines: list[str] = []
    # The HTTP block can legitimately be empty (this deployment has no HTTP sync
    # transport any more); then only the WS line is written instead of a line with
    # an empty address list, which a vendor reader would reject or misparse.
    if HTTP_ENDPOINTS:
        host = HTTP_ENDPOINTS[0][0]
        address = ",".join(str(port) for port in sorted({
            port for _host, port in HTTP_ENDPOINTS
        }))
        lines.append(f"{host}:{address}")
    if WS_ENDPOINTS:
        ws_host = WS_ENDPOINTS[0][0]
        ws_address = ",".join(str(port) for port in sorted({
            port for _host, port in WS_ENDPOINTS
        }))
        lines.append(f"{ws_host}:{ws_address} //ws")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


__all__ = [
    "HTTP_ENDPOINTS",
    "WS_ENDPOINTS",
    "endpoint_hosts",
    "write_vendor_endpoint_file",
]
