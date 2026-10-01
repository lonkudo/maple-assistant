"""Issue a signed offline MapleAssistant license from an operator private key."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from licensing import LICENSE_FORMAT, activation_code, machine_binding_from_request  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--expires", help="UTC ISO timestamp, e.g. 2027-01-01T00:00:00Z")
    parser.add_argument("--permanent", action="store_true")
    parser.add_argument("--license-id", default="")
    parser.add_argument("--output", type=Path, required=True)
    binding_group = parser.add_mutually_exclusive_group(required=True)
    binding_group.add_argument("--device-request", help="MADR1 device request from the customer app")
    binding_group.add_argument("--portable", action="store_true", help="Operator-only license with no device binding")
    args = parser.parse_args()
    if args.permanent == bool(args.expires):
        raise SystemExit("Choose exactly one of --permanent or --expires.")
    if args.expires:
        try:
            expiry = datetime.fromisoformat(args.expires.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                raise ValueError
        except ValueError:
            raise SystemExit("--expires must include a UTC offset, for example ...Z")
    private = serialization.load_pem_private_key(args.private_key.read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise SystemExit("The supplied private key is not Ed25519.")
    payload = {
        "format": LICENSE_FORMAT,
        "license_id": args.license_id.strip() or secrets.token_urlsafe(10),
        "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "edition": "normal",
        "permanent": bool(args.permanent),
        "expires_at": None if args.permanent else args.expires,
        "machine_binding": binding,
    }
    signed = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    document = {
        "payload": payload,
        "signature": base64.urlsafe_b64encode(private.sign(signed)).decode("ascii").rstrip("="),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=True, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(activation_code(document))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
    binding = None if args.portable else machine_binding_from_request(args.device_request)
