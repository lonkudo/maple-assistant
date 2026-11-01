"""Publish a separately-built MoonEclipse package to the activation server."""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import secrets
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from licensing import ACTIVATION_SERVER_ENDPOINT
from pinned_tls import PinnedTlsError, load_pin


EXCLUDED_PARTS = {".git", ".venv", "__pycache__", "work", "release", "助手配置", "data", "recordings", "updates", "logs"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(512 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_for(source: Path, version: str) -> tuple[dict, list[Path]]:
    files: list[Path] = []
    rows: list[dict] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or any(part in EXCLUDED_PARTS for part in path.relative_to(source).parts):
            continue
        relative = path.relative_to(source).as_posix()
        rows.append({"path": relative, "sha256": sha256(path), "size": path.stat().st_size, "file_version": version})
        files.append(path)
    if not rows:
        raise ValueError("release folder contains no publishable files")
    return {"format": "moon-eclipse-release-manifest/v1", "version": version, "files": rows}, files


def multipart_publish(endpoint: str, token: str, manifest: dict, files: list[Path]) -> dict:
    # Reuse the already-pinned desktop transport rules through a one-shot
    # standard library multipart body.  The command intentionally takes its
    # publisher credential only from an environment variable.
    import http.client
    import ssl
    from urllib.parse import urlsplit
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    import base64
    import hmac
    parts = urlsplit(endpoint)
    expected = load_pin(ROOT / "activation_server_pin.json")
    boundary = "----MoonEclipse" + uuid.uuid4().hex
    chunks: list[bytes] = []
    def add_field(name: str, value: bytes, filename: str | None = None) -> None:
        chunks.append((f"--{boundary}\r\n").encode())
        if filename is None:
            chunks.append((f'Content-Disposition: form-data; name="{name}"\r\n\r\n').encode())
        else:
            chunks.append((f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
        chunks.append(value)
        chunks.append(b"\r\n")
    add_field("manifest", json.dumps(manifest, separators=(",", ":")).encode())
    for index, path in enumerate(files):
        add_field(f"file_{index}", path.read_bytes(), path.name)
    chunks.append((f"--{boundary}--\r\n").encode())
    body = b"".join(chunks)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(parts.hostname, parts.port or 443, context=context, timeout=120)
    try:
        connection.connect()
        certificate = x509.load_der_x509_certificate(connection.sock.getpeercert(binary_form=True))
        actual = base64.b64encode(hashlib.sha256(certificate.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).digest()).decode()
        if not hmac.compare_digest(expected, actual):
            raise PinnedTlsError("server certificate pin mismatch")
        connection.request("POST", parts.path.rstrip("/") + "/api/v1/operator/releases/publish", body=body, headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))})
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        if response.status >= 400:
            raise RuntimeError(payload.get("message", "publish rejected"))
        return payload
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish MoonEclipse installer files")
    parser.add_argument("--source", type=Path, required=True, help="finished package directory, not repository root")
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    token = os.environ.get("MOONECLIPSE_RELEASE_PUBLISH_TOKEN", "").strip()
    if not token:
        raise SystemExit("MOONECLIPSE_RELEASE_PUBLISH_TOKEN is required")
    source = args.source.resolve()
    if not source.is_dir():
        raise SystemExit("--source must be a package directory")
    manifest, files = manifest_for(source, args.version.strip())
    result = multipart_publish(ACTIVATION_SERVER_ENDPOINT, token, manifest, files)
    print(json.dumps({"version": result.get("version"), "files": result.get("files")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
