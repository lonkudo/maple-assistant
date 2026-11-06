r"""P0f — is the missing decryption input the *device identity*?

Facts established by P0c/P0e:

* our key handling is provably correct (v2 round-trip succeeds), yet
* the shipped files fail with ``MAC check failed`` even with a fresh, valid ticket
  whose ``model_key`` is identical to the expired one, and
* the vendor's own oracle ``verify_enc_file_binding`` returns False.

The vendor's client decrypted these same bytes successfully at 10:17:16 — in a
process whose ``device_id`` matched its ticket. Our process computes a different
one, because ``licensing.device_id`` resolves its root relative to its own package
directory, and outside the frozen exe that root is not the install root.

``licensing/device_id.pyd`` documents the supported switch for a non-frozen
context: ``MXDPP_DEVICE_ID`` (honoured in dev mode, ignored in release builds).
This probe checks whether presenting the install's *true* identity — read from the
ticket itself, which the vendor's server issued for this install — makes the model
decrypt.

Run twice, without and with the override.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
EXPECTED = {"lie3_pivot": 12154231, "lie3b": 12154238}


def pad(t: str) -> str:
    return t + "=" * (-len(t) % 4)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    home = pathlib.Path(os.environ["LIE3_HOME"]).resolve()
    state = json.loads((home / "data/license/ct_state.json").read_text(encoding="utf-8-sig"))
    ticket = (state.get("ticket") or "").strip()
    payload = json.loads(base64.urlsafe_b64decode(pad(ticket.split(".")[0]))) if ticket else {}
    ticket_device = payload.get("device_id")

    print("MXDPP_DEVICE_ID 环境变量:", os.environ.get("MXDPP_DEVICE_ID") or "(未设置)")
    print("票据 device_id          :", ticket_device)

    sys.path.insert(0, str(HERE / "shims"))
    sys.path.append(str(home / "_internal"))

    import licensing.device_id as dev
    import licensing.model_crypto as mc

    computed = dev.get_device_id()
    print("本进程算出的 device_id    :", computed)
    print("与票据一致               :", computed == ticket_device)

    key = mc.resolve_model_key(payload) if payload else None
    print("密钥                      :", "无" if key is None else f"{len(key)}B")

    print("\n--- 预言机 verify_enc_file_binding ---")
    for name in EXPECTED:
        enc = home / "_internal" / "lun_lies" / f"{name}.onnx.enc"
        try:
            print(f"  {name}: {mc.verify_enc_file_binding(enc, payload)}")
        except Exception as exc:
            print(f"  {name}: {type(exc).__name__}: {str(exc)[:70]}")

    print("\n--- 直接解密 ---")
    ok = 0
    for name, expected in EXPECTED.items():
        enc = home / "_internal" / "lun_lies" / f"{name}.onnx.enc"
        if key is None:
            print(f"  {name}: 无密钥")
            continue
        try:
            out = mc.decrypt_file_to_bytes(enc, bytes(key), ticket_payload=payload)
            size = len(out)
            flag = "OK" if size == expected else f"尺寸不符(期望{expected})"
            print(f"  {name}: {size} 字节  {flag}")
            if size == expected:
                print(f"        onnx 前16字节: {bytes(out[:16]).hex()}  含 ir_version: {b'ir_version' in bytes(out[:4096])}")
                ok += 1
        except Exception as exc:
            print(f"  {name}: {type(exc).__name__}: {str(exc)[:70]}")
    return 0 if ok == len(EXPECTED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
