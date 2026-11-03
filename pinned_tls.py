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
import threading
from typing import BinaryIO, Callable, Mapping, Optional
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


def post_json(
    endpoint: str, path: str, payload: dict, pin_path: Path,
    timeout: float = 10.0, logger: Optional[logging.Logger] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> dict:
    """Pin before transmitting the request body; never silently fall back.

    ``logger`` selects the destination of the connection trace.  Licensing
    questions belong in the ``server-client`` log, while automatic lie handling
    has its own file: the lie-event accounting request is part of that feature,
    so its connection lines must not land in the licensing connection log.
    """

    log = logger if logger is not None else SERVER_LOG
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
        log.info("secure request connecting endpoint=%s path=%s", safe_endpoint, path)
        connection.connect()
        certificate = x509.load_der_x509_certificate(connection.sock.getpeercert(binary_form=True))
        spki = certificate.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        actual = base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")
        if not hmac.compare_digest(expected, actual):
            log.warning("secure request rejected: certificate pin mismatch endpoint=%s", safe_endpoint)
            raise PinnedTlsError("服务器身份校验失败，连接已拒绝。")
        log.info("secure request certificate pin verified endpoint=%s", safe_endpoint)
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        target = (parts.path.rstrip("/") + "/" + path.lstrip("/"))
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        connection.request("POST", target, body=body, headers=request_headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        if response.status >= 400:
            log.warning("secure request rejected by server status=%s endpoint=%s", response.status, safe_endpoint)
            raise PinnedTlsResponseError(
                response.status,
                str(data.get("code", "SERVER_REJECTED")),
                str(data.get("message", "授权服务器拒绝请求。")),
                data,
            )
        log.info("secure request completed status=%s endpoint=%s", response.status, safe_endpoint)
        return data
    except PinnedTlsError:
        # This is an intentional, already-classified refusal (for example a
        # pin mismatch or a 4xx/5xx response), not a transport failure.
        raise
    except (OSError, ssl.SSLError, json.JSONDecodeError) as exc:
        log.warning("secure request failed category=%s endpoint=%s", type(exc).__name__, safe_endpoint)
        raise PinnedTlsError("无法建立安全授权连接。") from exc
    finally:
        connection.close()


def download_binary(
    endpoint: str, path: str, pin_path: Path, authorization: str,
    output: BinaryIO, progress: Optional[Callable[[int], None]] = None,
    timeout: float = 30.0,
) -> int:
    """Stream one protected installer artifact after the normal TLS pin check."""

    parts = urlsplit(endpoint)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise PinnedTlsError("授权服务器必须使用 HTTPS。")
    expected = load_pin(pin_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(parts.hostname, parts.port or 443, context=context, timeout=timeout)
    try:
        connection.connect()
        certificate = x509.load_der_x509_certificate(connection.sock.getpeercert(binary_form=True))
        spki = certificate.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        actual = base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")
        if not hmac.compare_digest(expected, actual):
            raise PinnedTlsError("服务器身份校验失败，连接已拒绝。")
        target = parts.path.rstrip("/") + "/" + path.lstrip("/")
        connection.request("GET", target, headers={"Authorization": f"Bearer {authorization}"})
        response = connection.getresponse()
        if response.status >= 400:
            try:
                data = json.loads(response.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                data = {}
            raise PinnedTlsResponseError(response.status, str(data.get("code", "DOWNLOAD_FAILED")), str(data.get("message", "下载失败。")), data)
        total = 0
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            output.write(chunk)
            total += len(chunk)
            if progress is not None:
                progress(len(chunk))
        return total
    except PinnedTlsError:
        raise
    except (OSError, ssl.SSLError) as exc:
        raise PinnedTlsError("无法下载更新文件。") from exc
    finally:
        connection.close()


_thread_state = threading.local()


def _pooled_connection(host: str, port: int, timeout: float) -> http.client.HTTPSConnection:
    """Reuse one connection per thread instead of a handshake per file.

    A release is over a thousand files, and every fresh connection paid a full
    TLS handshake (measured at ~96 ms each, ~100 s per release).  The server
    answers HTTP/1.1 keep-alive, so hold the connection for the next file.
    """

    existing = getattr(_thread_state, "connection", None)
    if existing is not None and getattr(_thread_state, "key", None) == (host, port):
        return existing
    _drop_pooled_connection()
    context = ssl.create_default_context()
    connection = http.client.HTTPSConnection(host, port, context=context, timeout=timeout)
    connection.connect()
    _thread_state.connection = connection
    _thread_state.key = (host, port)
    return connection


def _drop_pooled_connection() -> None:
    connection = getattr(_thread_state, "connection", None)
    if connection is not None:
        try:
            connection.close()
        except OSError:
            pass
    _thread_state.connection = None
    _thread_state.key = None


def download_url(
    url: str, output: BinaryIO, progress: Optional[Callable[[int], None]] = None,
    timeout: float = 30.0,
) -> int:
    """Stream one release file from an absolute HTTPS URL without the server pin.

    Only CDN copies are fetched this way.  Integrity does not depend on this
    connection: the installer verifies every file against the SHA-256 in the
    manifest that the *pinned* origin issued.  Transport still uses the system
    CA store, so this is weaker than the pin but not unauthenticated, and a CDN
    certificate can never be used to change what a client installs.
    """

    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise PinnedTlsError("发布文件地址无效。")
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    host, port = parts.hostname, parts.port or 443
    total = 0
    for attempt in (1, 2):
        try:
            connection = _pooled_connection(host, port, timeout)
            connection.request("GET", target)
            response = connection.getresponse()
            if response.status >= 400:
                data: dict = {}
                try:
                    data = json.loads(response.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
                raise PinnedTlsResponseError(
                    response.status, "DOWNLOAD_FAILED", "发布文件下载失败。", data
                )
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                total += len(chunk)
                if progress is not None:
                    progress(len(chunk))
            return total
        except PinnedTlsResponseError:
            raise
        except (OSError, ssl.SSLError) as exc:
            # A pooled connection the peer already closed fails before any byte
            # arrives; reconnect once for that case only, because a retry after
            # partial output would corrupt the staged file.
            _drop_pooled_connection()
            if attempt == 2 or total:
                raise PinnedTlsError("无法下载发布文件。") from exc
    raise PinnedTlsError("无法下载发布文件。")


def upload_binary(
    endpoint: str, path: str, pin_path: Path, authorization: str,
    source: BinaryIO, size: int, timeout: float = 120.0,
) -> int:
    """PUT one release artifact through Nginx after the normal pin check."""

    parts = urlsplit(endpoint)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise PinnedTlsError("授权服务器必须使用 HTTPS。")
    expected = load_pin(pin_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(
        parts.hostname, parts.port or 443, context=context, timeout=timeout
    )
    try:
        connection.connect()
        certificate = x509.load_der_x509_certificate(
            connection.sock.getpeercert(binary_form=True)
        )
        spki = certificate.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        actual = base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")
        if not hmac.compare_digest(expected, actual):
            raise PinnedTlsError("服务器身份校验失败，连接已拒绝。")
        target = parts.path.rstrip("/") + "/" + path.lstrip("/")
        connection.putrequest("PUT", target)
        connection.putheader("Authorization", f"Bearer {authorization}")
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Content-Length", str(max(0, int(size))))
        connection.endheaders()
        sent = 0
        while True:
            chunk = source.read(256 * 1024)
            if not chunk:
                break
            connection.send(chunk)
            sent += len(chunk)
        response = connection.getresponse()
        raw = response.read()
        if response.status >= 400:
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                data = {}
            raise PinnedTlsResponseError(
                response.status, str(data.get("code", "UPLOAD_FAILED")),
                str(data.get("message", "上传失败。")), data,
            )
        if sent != int(size):
            raise PinnedTlsError("发布文件读取不完整。")
        return sent
    except PinnedTlsError:
        raise
    except (OSError, ssl.SSLError) as exc:
        raise PinnedTlsError("无法上传发布文件。") from exc
    finally:
        connection.close()
