r"""P0d — ask the vendor's own oracle + a correct v2 round-trip.

``verify_enc_file_binding(path, ticket_payload) -> bool`` is the vendor's own
"can this ticket decrypt this file?" probe, so it settles the question the MAC
failures left open. A correct ``encrypt_bytes_v2`` / ``decrypt_bytes_v2``
round-trip (the format the shipped files actually use) is the control that
separates "our usage is wrong" from "the ticket key is not the packaging key".

Run::
    $env:LIE3_HOME = "<licensed install>"
    .\venv-lie3\Scripts\python.exe lie3_engine\p0d_oracle.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
OTHER_HOME = pathlib.Path(os.environ.get("LIE3_HOME_OTHER", "")) if os.environ.get("LIE3_HOME_OTHER") else None


def pad(text: str) -> str:
    return text + "=" * (-len(text) % 4)


def call(label: str, fn, *args, **kwargs):
    try:
        out = fn(*args, **kwargs)
        shown = repr(out)
        print(f"  {label:56s} -> {shown[:100]}")
        return out
    except Exception as exc:
        print(f"  {label:56s} -> {type(exc).__name__}: {str(exc)[:90]}")
        return None


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    home = pathlib.Path(os.environ["LIE3_HOME"]).resolve()
    sys.path.insert(0, str(HERE / "shims"))
    sys.path.append(str(home / "_internal"))

    state = json.loads((home / "data/license/ct_state.json").read_text(encoding="utf-8-sig"))
    ticket = state["ticket"].strip()
    payload = json.loads(base64.urlsafe_b64decode(pad(ticket.split(".")[0])))

    import licensing.model_crypto as mc

    key = mc.resolve_model_key(payload)
    print("model_key -> 32B:", bytes(key[:8]).hex(), "...")

    print("\n=== A. 文件指纹（两份副本是否同一把打包密钥） ===")
    targets = [("本机 LIE3_HOME", home)]
    if OTHER_HOME:
        targets.append(("另一副本", OTHER_HOME))
    digests: dict[str, dict[str, str]] = {}
    for label, root in targets:
        digests[label] = {}
        for name in ("lie3_pivot", "lie3b"):
            p = root / "_internal" / "lun_lies" / f"{name}.onnx.enc"
            if not p.is_file():
                continue
            h = hashlib.sha256(p.read_bytes()).hexdigest()[:20]
            digests[label][name] = h
            import datetime
            ts = datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M:%S")
            print(f"  {label:16s} {name:12s} sha256[:20]={h}  mtime={ts}  size={p.stat().st_size}")
    if len(digests) == 2:
        labels = list(digests)
        for name in ("lie3_pivot", "lie3b"):
            same = digests[labels[0]].get(name) == digests[labels[1]].get(name)
            print(f"  两份副本 {name} 是否同文件: {same}")

    print("\n=== B. 他的预言机 verify_enc_file_binding ===")
    for name in ("lie3_pivot", "lie3b"):
        enc = home / "_internal" / "lun_lies" / f"{name}.onnx.enc"
        call(f"{name} + 票据 dict", mc.verify_enc_file_binding, enc, payload)
        call(f"{name} + None", mc.verify_enc_file_binding, enc, None)

    print("\n=== C. v2 往返对照（正确签名） ===")
    probe = b"LIE3-P0-ROUNDTRIP-" + os.urandom(16)
    for model_name in ("lie3_pivot", "lie3b"):
        blob = call(f"encrypt_bytes_v2(model_name={model_name})",
                    mc.encrypt_bytes_v2, probe, key, model_name=model_name)
        if blob is None:
            continue
        back = call(f"decrypt_bytes_v2(model_name={model_name})",
                    mc.decrypt_bytes_v2, blob, key, model_name=model_name)
        print(f"    -> 往返{'成功' if back == probe else '失败'}")

    print("\n=== D. 绕过文件层，直接 v2 解真实字节 ===")
    for name in ("lie3_pivot", "lie3b"):
        enc = home / "_internal" / "lun_lies" / f"{name}.onnx.enc"
        raw = enc.read_bytes()
        for model_name in (name, "lie3_pivot"):
            out = call(f"{name}: decrypt_bytes_v2(raw_bytes, model_name={model_name})",
                       mc.decrypt_bytes_v2, raw, key, model_name=model_name)
            if out is not None:
                print(f"    -> {len(out)} 字节")

    print("\n=== E. 结论 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
