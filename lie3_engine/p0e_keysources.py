r"""P0e — is there another (embedded / fallback) key source?

``resolve_model_key(None)`` is the vendor's documented dev fallback path, and
``ct_release`` may carry a compiled-in key. If neither decrypts the shipped
files either, the conclusion is final: the *card/product* does not match the key
these models were packaged with, and route A needs a fresh activation (or the
vendor's answer), not a code change on our side.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent


def pad(t: str) -> str:
    return t + "=" * (-len(t) % 4)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    home = pathlib.Path(os.environ["LIE3_HOME"]).resolve()
    sys.path.insert(0, str(HERE / "shims"))
    sys.path.append(str(home / "_internal"))

    state = json.loads((home / "data/license/ct_state.json").read_text(encoding="utf-8-sig"))
    payload = json.loads(base64.urlsafe_b64decode(pad(state["ticket"].strip().split(".")[0])))

    import licensing.model_crypto as mc
    import ct_release

    print("=== A. 所有可能的密钥来源 ===")
    keys: dict[str, bytes] = {}
    k_ticket = mc.resolve_model_key(payload)
    if k_ticket:
        keys["ticket payload"] = bytes(k_ticket)
    k_none = mc.resolve_model_key(None)
    print("  resolve_model_key(None) ->", None if k_none is None else f"{len(k_none)}B {bytes(k_none[:8]).hex()}...")
    if k_none:
        keys["resolve_model_key(None)"] = bytes(k_none)
        print("    与票据密钥相同:", bytes(k_none) == bytes(k_ticket))
    env = os.environ.get("MXDPP_MODEL_KEY", "").strip()
    print("  MXDPP_MODEL_KEY 环境变量:", "已设置" if env else "未设置")
    if env:
        keys["env MXDPP_MODEL_KEY"] = mc.key_from_b64(env)

    print("\n=== B. ct_release 暴露了什么 ===")
    names = [n for n in dir(ct_release) if not n.startswith("_")]
    print("  ", names)
    for n in names:
        low = n.lower()
        if any(w in low for w in ("key", "model", "product", "build", "pack")):
            try:
                v = getattr(ct_release, n)
                v = v() if callable(v) else v
                print(f"   {n} = {str(v)[:90]}")
            except Exception as exc:
                print(f"   {n} -> {type(exc).__name__}: {str(exc)[:60]}")

    print("\n=== C. 逐个密钥试解（文件层 + v2 层） ===")
    for name in ("lie3_pivot", "lie3b"):
        enc = home / "_internal" / "lun_lies" / f"{name}.onnx.enc"
        raw = enc.read_bytes()
        print(f"  --- {name}")
        for kname, key in keys.items():
            try:
                out = mc.decrypt_bytes_v2(raw, key, model_name=name)
                print(f"    {kname:26s} -> ✅ {len(out)} 字节")
            except Exception as exc:
                print(f"    {kname:26s} -> {type(exc).__name__}: {str(exc)[:50]}")
        try:
            print(f"    verify_enc_file_binding   -> {mc.verify_enc_file_binding(enc, payload)}")
        except Exception as exc:
            print(f"    verify_enc_file_binding   -> {type(exc).__name__}: {str(exc)[:50]}")

    print("\n=== D. 票据与包的产品标识 ===")
    print("  票据 product_id :", payload.get("product_id"))
    print("  票据 product_name:", payload.get("product_name"))
    bp = json.loads((home / "data/build_profile.json").read_text(encoding="utf-8-sig"))
    print("  包   product_id :", bp.get("product_id"), "| variant:", bp.get("variant"))
    print("  包   client_version:", bp.get("client_version"), "| 票据:", payload.get("client_version"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
