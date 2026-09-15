"""Minimal RFC 6455 WebSocket framing (stdlib only).

The project has no websocket dependency, and the RoiTrack service only needs a small part of
the protocol: a client handshake, masked client frames, unmasked server frames, text and binary
payloads, ping/pong and close.  This module holds exactly that, so both our client
(:mod:`autolie_api.ws_client`) and the local mimic backend (:mod:`autolie_api.mock_backend`)
speak the same framing instead of two hand-rolled copies.

Not implemented on purpose: extensions, fragmentation of outgoing messages, permessage-deflate.
The service sends single frames (one JSON result or one RTF1 packet per message), and so do we.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
from typing import Optional, Tuple

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def accept_key(client_key: str) -> str:
    """The server's ``Sec-WebSocket-Accept`` for a client's ``Sec-WebSocket-Key``."""

    digest = hashlib.sha1((client_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def client_handshake_request(host: str, port: int, path: str = "/") -> bytes:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    return (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")


def server_handshake_response(headers: dict) -> Optional[bytes]:
    """The 101 response for parsed request headers, or None when the upgrade is invalid."""

    key = headers.get("sec-websocket-key")
    if not key or "websocket" not in headers.get("upgrade", "").lower():
        return None
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n"
    ).encode("ascii")


def encode_frame(opcode: int, payload: bytes, *, mask: bool = False) -> bytes:
    """One complete frame; ``mask=True`` for client->server frames (RFC requires it)."""

    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < (1 << 16):
        header.append(mask_bit | 126)
        header += struct.pack(">H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack(">Q", length)
    if not mask:
        return bytes(header) + payload
    key = os.urandom(4)
    masked = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + key + masked


def recv_exactly(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("connection closed")
        data += chunk
    return data


def read_frame(sock: socket.socket) -> Tuple[int, bytes]:
    """Read one complete frame -> (opcode, payload); handles interleaved pings."""

    while True:
        first, second = recv_exactly(sock, 2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", recv_exactly(sock, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", recv_exactly(sock, 8))[0]
        masked = bool(second & 0x80)
        key = recv_exactly(sock, 4) if masked else b""
        payload = recv_exactly(sock, length) if length else b""
        if masked:
            payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
        if opcode == OP_PING:
            sock.sendall(encode_frame(OP_PONG, payload))
            continue
        if opcode == OP_PONG:
            continue
        return opcode, payload


def read_http_headers(sock: socket.socket) -> Tuple[str, dict]:
    """Read a request line + headers (until the blank line) -> (request line, headers)."""

    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("connection closed during the handshake")
        buffer += chunk
    head = buffer.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    return lines[0], headers
