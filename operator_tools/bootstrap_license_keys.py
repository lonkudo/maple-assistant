"""Create an operator-only Ed25519 key pair for MapleAssistant licensing.

Never place the resulting private PEM in the app folder or a release ZIP.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-output", type=Path, required=True)
    parser.add_argument("--public-output", type=Path, required=True)
    args = parser.parse_args()
    if args.private_output.exists() or args.public_output.exists():
        raise SystemExit("Refusing to overwrite an existing key file.")
    private = Ed25519PrivateKey.generate()
    args.private_output.parent.mkdir(parents=True, exist_ok=True)
    args.public_output.parent.mkdir(parents=True, exist_ok=True)
    args.private_output.write_bytes(private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    args.public_output.write_text(json.dumps({
        "algorithm": "Ed25519",
        "public_key": base64.urlsafe_b64encode(private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )).decode("ascii").rstrip("="),
    }, indent=2) + "\n", encoding="utf-8")
    print(f"private key created at {args.private_output}")
    print(f"public key created at {args.public_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
