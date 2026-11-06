r"""P0c — is the MAC failure a *usage* problem on our side, or the wrong key?

Control experiment: round-trip a known blob through the vendor's own
``encrypt_bytes_v2`` / ``decrypt_bytes_v2``. If our key handling round-trips but
the shipped ``.onnx.enc`` still fails, then the ticket's ``model_key`` is not the
key those files were packaged with — which the vendor's own error text names
explicitly ("授权票据中的 model_key 与打包加密密钥不一致").

Run::
    $env:LIE3_HOME = "<licensed install>"
    .\venv-lie3\Scripts\python.exe lie3_engine\p0c_roundtrip.py
"""
from __future__ import annotations

import base64
import inspect
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent


def pad(text: str) -> str:
    return text + "=" * (-len(text) % 4)


def show(label: str, fn, *args, **kwargs) -> object:
    try:
        out = fn(*args, **kwargs)
        text = repr(out)
        print(f"  {label:52s} -> {text[:90]}")
        return out
    except Exception as exc:
        print(f"  {label:52s} -> {type(exc).__name__}: {str(exc)[:80]}")
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
    payload_bytes = base64.urlsafe_b64decode(pad(ticket.split(".")[0]))
    payload = json.loads(payload_bytes)
    model_key_b64 = str(payload["model_key"])

    import licensing.model_crypto as mc

    print("=== A. 文件属性 ===")
    enc = home / "_internal/lun_lies/lie3_pivot.onnx.enc"
    enc2 = home / "_internal/lun_lies/lie3b.onnx.enc"
    for path in (enc, enc2):
        print(f"  {path.name}")
        show("    encrypted_format_version", mc.encrypted_format_version, path)
        show("    look_like_encrypted", mc.look_like_encrypted, path)
        show("    model_name_for_enc", mc.model_name_for_enc, path)
        show("    plain_onnx_path_for_enc", mc.plain_onnx_path_for_enc, path)
    print("  MAGIC_V1 =", getattr(mc, "MAGIC_V1", None))
    print("  MAGIC_V2 =", getattr(mc, "MAGIC_V2", None))
    print("  FORMAT_V2 =", getattr(mc, "FORMAT_V2", None))
    for name in ("decrypt_bytes_v2", "decrypt_bytes", "verify_enc_file_binding", "key_from_b64"):
        fn = getattr(mc, name, None)
        if fn is None:
            continue
        try:
            sig = str(inspect.signature(fn))
        except Exception:
            sig = "(签名不可见)"
        doc = (fn.__doc__ or "").strip().splitlines()
        print(f"  {name}{sig}")
        if doc:
            print(f"      {doc[0][:100]}")

    print("\n=== B. 密钥来源对比 ===")
    key_his = mc.resolve_model_key(payload)
    key_b64 = mc.key_from_b64(model_key_b64)
    print("  resolve_model_key(payload) len:", None if key_his is None else len(key_his))
    print("  key_from_b64(model_key)    len:", None if key_b64 is None else len(key_b64))
    if key_his and key_b64:
        print("  两者相同:", bytes(key_his) == bytes(key_b64))
    print("  model_key 原文前12:", model_key_b64[:12])

    print("\n=== C. 往返对照（他的 encrypt_bytes_v2 -> decrypt_bytes_v2） ===")
    probe_plaintext = b"LIE3-P0-ROUNDTRIP-" + os.urandom(16)
    for key_name, key in (("resolve_model_key", key_his), ("key_from_b64", key_b64)):
        if key is None:
            continue
        blob = show(f"{key_name}: encrypt_bytes_v2", mc.encrypt_bytes_v2, probe_plaintext, key)
        if blob is None:
            show(f"{key_name}: encrypt_bytes", mc.encrypt_bytes, probe_plaintext, key)
            continue
        back = show(f"{key_name}: decrypt_bytes_v2", mc.decrypt_bytes_v2, blob, key)
        print(f"    -> 往返{'成功 ✅' if back == probe_plaintext else '失败 ❌'}")

    print("\n=== D. 用不同 ticket_payload 形态解真实文件 ===")
    variants = {
        "dict": payload,
        "payload_bytes": payload_bytes,
        "ticket_str": ticket,
        "None": None,
    }
    for key_name, key in (("resolve_model_key", key_his), ("key_from_b64", key_b64)):
        if key is None:
            continue
        for vname, v in variants.items():
            try:
                out = mc.decrypt_file_to_bytes(enc, key, ticket_payload=v)
                print(f"  {key_name:20s} ticket_payload={vname:14s} -> {len(out)} 字节")
            except Exception as exc:
                print(f"  {key_name:20s} ticket_payload={vname:14s} -> {type(exc).__name__}: {str(exc)[:60]}")

    print("\n=== E. 结论判据 ===")
    print("  C 成功 + D 全失败  => 密钥用法正确，但票据里的 model_key 不是这些文件的打包密钥")
    print("  C 也失败           => 我们的密钥/参数用法不对，需继续排查")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
