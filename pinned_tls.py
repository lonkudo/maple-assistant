"""HTTPS transport that accepts only an explicitly pinned server public key."""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import logging
from pathlib import Path
import ssl
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import serialization


SERVER_LOG = logging.getLogger("server-client")


class PinnedTlsError(ConnectionError):
    pass


class PinnedTlsResponseError(PinnedTlsError):
    """A verified server returned an application-level rejection."""

    def __init__(self, status: int, code: str, message: str, data: dict | None = None) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.data = dict(data or {})


def load_pin(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = str(data["spki_sha256"])
        if data.get("format") != "maple-assistant-tls-pin/v1" or len(base64.b64decode(value, validate=True)) != 32:
            raise ValueError
        return value
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PinnedTlsError("安全连接配置不可用。") from exc


def post_json(endpoint: str, path: str, payload: dict, pin_path: Path, timeout: float = 10.0) -> dict:
    """Pin before transmitting the request body; never silently fall back."""
    parts = urlsplit(endpoint)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise PinnedTlsError("授权服务器必须使用 HTTPS。")
    safe_endpoint = f"{parts.scheme}://{parts.hostname}:{parts.port or 443}{parts.path.rstrip('/')}"
    expected = load_pin(pin_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(parts.hostname, parts.port or 443, context=context, timeout=timeout)
    try:
        SERVER_LOG.info("secure request connecting endpoint=%s path=%s", safe_endpoint, path)
        connection.connect()
        certificate = x509.load_der_x509_certificate(connection.sock.getpeercert(binary_form=True))
        spki = certificate.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        actual = base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")
        if not hmac.compare_digest(expected, actual):
            SERVER_LOG.warning("secure request rejected: certificate pin mismatch endpoint=%s", safe_endpoint)
            raise PinnedTlsError("服务器身份校验失败，连接已拒绝。")
        SERVER_LOG.info("secure request certificate pin verified endpoint=%s", safe_endpoint)
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        target = (parts.path.rstrip("/") + "/" + path.lstrip("/"))
        connection.request("POST", target, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        if response.status >= 400:
            SERVER_LOG.warning("secure request rejected by server status=%s endpoint=%s", response.status, safe_endpoint)
            raise PinnedTlsResponseError(
                response.status,
                str(data.get("code", "SERVER_REJECTED")),
                str(data.get("message", "授权服务器拒绝请求。")),
                data,
            )
        SERVER_LOG.info("secure request completed status=%s endpoint=%s", response.status, safe_endpoint)
        return data
    except PinnedTlsError:
        # This is an intentional, already-classified refusal (for example a
        # pin mismatch or a 4xx/5xx response), not a transport failure.
        raise
    except (OSError, ssl.SSLError, json.JSONDecodeError) as exc:
        SERVER_LOG.warning("secure request failed category=%s endpoint=%s", type(exc).__name__, safe_endpoint)
        raise PinnedTlsError("无法建立安全授权连接。") from exc
    finally:
        connection.close()
