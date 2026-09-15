"""The RoiTrack endpoints, read from the vendor's own ``autolie_api/ip_port.txt``.

The file is reference material and is never edited (see README.md); this only parses it:

    117.50.223.113:8004-8006            <- HTTP sync (§10)
    117.50.223.113:8001-8003 //ws       <- WebSocket (§4)

So a deployment can move by editing that file, and code, tests and tools all see the same list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("api_auto_lie")

ROOT = Path(__file__).resolve().parent.parent
ENDPOINT_FILE = Path(__file__).resolve().parent / "ip_port.txt"
DEFAULT_HOST = "117.50.223.113"
DEFAULT_WS_PORTS = (8001, 8002, 8003)
DEFAULT_HTTP_PORTS = (8004, 8005, 8006)


@dataclass(frozen=True)
class Endpoint:
    """One service instance: host, port and which protocol it speaks."""

    host: str
    port: int
    transport: str = "ws"                     # "ws" or "http"

    @property
    def url(self) -> str:
        return f"{self.transport}://{self.host}:{self.port}"

    def describe(self) -> str:
        return self.url


def _ports(text: str) -> list[int]:
    ports: list[int] = []
    for chunk in text.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            start, end = chunk.split("-", 1)
            try:
                ports.extend(range(int(start), int(end) + 1))
            except ValueError:
                continue
        elif chunk.isdigit():
            ports.append(int(chunk))
    return ports


def load_endpoints(path: Optional[Path] = None) -> tuple[Endpoint, ...]:
    """Every endpoint of the deployment, WS first then HTTP.

    ``path=None`` reads the vendor file; missing or unparsable content falls back to the known
    defaults so a drill can still run.
    """

    target = Path(path) if path is not None else ENDPOINT_FILE
    found: list[Endpoint] = []
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        LOG.warning("endpoint list %s is missing; using the built-in defaults", target)
        text = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        transport = "ws" if "//ws" in line.replace(" ", "") else "http"
        address = line.split("//")[0].strip()
        if ":" not in address:
            continue
        host, ports_text = address.rsplit(":", 1)
        for port in _ports(ports_text):
            found.append(Endpoint(host=host, port=port, transport=transport))
    if not found:
        found = ([Endpoint(DEFAULT_HOST, port, "ws") for port in DEFAULT_WS_PORTS]
                 + [Endpoint(DEFAULT_HOST, port, "http") for port in DEFAULT_HTTP_PORTS])
    # deduplicate, keep the file's order
    seen: set[tuple[str, int, str]] = set()
    unique: list[Endpoint] = []
    for endpoint in found:
        key = (endpoint.host, endpoint.port, endpoint.transport)
        if key not in seen:
            seen.add(key)
            unique.append(endpoint)
    return tuple(unique)


def ws_endpoints(path: Optional[Path] = None) -> tuple[Endpoint, ...]:
    return tuple(item for item in load_endpoints(path) if item.transport == "ws")


def http_endpoints(path: Optional[Path] = None) -> tuple[Endpoint, ...]:
    return tuple(item for item in load_endpoints(path) if item.transport == "http")


__all__ = ["Endpoint", "ENDPOINT_FILE", "http_endpoints", "load_endpoints", "ws_endpoints"]
