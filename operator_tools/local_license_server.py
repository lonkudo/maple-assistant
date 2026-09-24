"""Local SQLite emulator for the future MapleAssistant licensing service.

Run it only on the operator machine.  The database and Ed25519 private key
never belong in the client package.  It mirrors the future HTTPS API:
``POST /v1/activate`` with a short code and a hashed device request returns a
signed license document.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
import sys
from typing import Any
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from licensing import LICENSE_FORMAT, machine_binding_from_request  # noqa: E402


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path)
    database.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            activation_code TEXT PRIMARY KEY,
            license_id TEXT NOT NULL UNIQUE,
            edition TEXT NOT NULL,
            permanent INTEGER NOT NULL,
            expires_at TEXT,
            issued_at TEXT NOT NULL,
            binding_json TEXT,
            license_json TEXT,
            activated_at TEXT,
            revoked INTEGER NOT NULL DEFAULT 0
        )
    """)
    return database


def _new_code() -> str:
    return "MAL-" + secrets.token_urlsafe(8).replace("_", "A").replace("-", "B")


def issue(database_path: Path, edition: str, permanent: bool, expires_at: str | None) -> str:
    if permanent == bool(expires_at):
        raise ValueError("Choose exactly one of permanent or expires_at.")
    code = _new_code()
    issued = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with _connect(database_path) as database:
        database.execute(
            "INSERT INTO licenses (activation_code, license_id, edition, permanent, expires_at, issued_at) VALUES (?, ?, ?, ?, ?, ?)",
            (code, str(uuid.uuid4()), edition, int(permanent), expires_at, issued),
        )
    return code


def _same_device(expected: dict[str, Any], incoming: dict[str, Any]) -> bool:
    expected_components = expected.get("components", {})
    incoming_components = incoming.get("components", {})
    matches = sum(
        1 for key, value in expected_components.items()
        if incoming_components.get(key) == value
    )
    return matches >= int(expected.get("minimum_matches", 2))


def activate(database_path: Path, private_key_path: Path, code: str, request_code: str) -> dict[str, Any]:
    binding = machine_binding_from_request(request_code)
    private = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError("The server private key is not Ed25519.")
    with _connect(database_path) as database:
        row = database.execute(
            "SELECT license_id, edition, permanent, expires_at, issued_at, binding_json, license_json, revoked FROM licenses WHERE activation_code = ?",
            (code,),
        ).fetchone()
        if row is None:
            raise PermissionError("授权码不存在。")
        license_id, edition, permanent, expires_at, issued_at, binding_json, license_json, revoked = row
        if revoked:
            raise PermissionError("授权码已撤销。")
        if binding_json:
            stored_binding = json.loads(binding_json)
            if not _same_device(stored_binding, binding):
                raise PermissionError("授权码已经绑定到另一台设备。")
            return json.loads(license_json)
        payload = {
            "format": LICENSE_FORMAT,
            "license_id": license_id,
            "issued_at": issued_at,
            "edition": edition,
            "permanent": bool(permanent),
            "expires_at": expires_at,
            "machine_binding": binding,
        }
        document = {
            "payload": payload,
            "signature": base64.urlsafe_b64encode(private.sign(_canonical(payload))).decode("ascii").rstrip("="),
        }
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        database.execute(
            "UPDATE licenses SET binding_json = ?, license_json = ?, activated_at = ? WHERE activation_code = ?",
            (json.dumps(binding, sort_keys=True), json.dumps(document, sort_keys=True), now, code),
        )
        return document


def _handler(database_path: Path, private_key_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/activate":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                document = activate(database_path, private_key_path, request["code"], request["device_request"])
                payload = json.dumps({"license": document}).encode("utf-8")
                self.send_response(HTTPStatus.OK)
            except PermissionError as exc:
                payload = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(HTTPStatus.FORBIDDEN)
            except Exception as exc:
                payload = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(HTTPStatus.BAD_REQUEST)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: Any) -> None:
            return
    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    issue_parser = subcommands.add_parser("issue")
    issue_parser.add_argument("--db", type=Path, required=True)
    issue_parser.add_argument("--edition", choices=("normal", "np"), default="normal")
    issue_parser.add_argument("--permanent", action="store_true")
    issue_parser.add_argument("--expires")
    serve_parser = subcommands.add_parser("serve")
    serve_parser.add_argument("--db", type=Path, required=True)
    serve_parser.add_argument("--private-key", type=Path, required=True)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.command == "issue":
        print(issue(args.db, args.edition, args.permanent, args.expires))
        return 0
    server = ThreadingHTTPServer((args.host, args.port), _handler(args.db, args.private_key))
    print(f"Local licensing server listening at http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
