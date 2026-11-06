r"""P0 probe — the single feasibility question this whole route depends on.

Can a plain CPython 3.12 process

  1. import the vendor's *compiled* licensing modules (``licensing/*.pyd``) with only
     shims for the modules that live inside its frozen exe, and
  2. decrypt ``lie3_pivot.onnx.enc`` using the model key taken from the local ticket
     file (the "simulated key receiver" the operator asked for)?

If (1) or (2) fails there is no route A and the plan must change.

Run::

    $env:LIE3_HOME = "<licensed install>"
    .\venv-lie3\Scripts\python.exe lie3_engine\p0_probe.py

Path policy: our own ``shims`` go first, the vendor's ``_internal`` goes **last**
so its partial, disk-side packages can never shadow a complete one (its
``Crypto`` holds only compiled ``.pyd`` files, so PyCryptodome must come from our
own environment).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import sys
import traceback

HERE = pathlib.Path(__file__).resolve().parent

EXPECTED = {"lie3_pivot": 12154231, "lie3b": 12154238}


def pad(text: str) -> str:
    return text + "=" * (-len(text) % 4)


def banner(title: str) -> None:
    print(f"\n{'=' * 12} {title} {'=' * 12}")


def public_names(module) -> list[str]:
    return sorted(n for n in dir(module) if not n.startswith("_"))


def main() -> int:
    try:  # the Windows console defaults to GBK; keep Chinese/emoji from crashing us
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    home_raw = (os.environ.get("LIE3_HOME") or "").strip()
    if not home_raw:
        print("LIE3_HOME 未设置")
        return 2
    home = pathlib.Path(home_raw).resolve()

    banner("0. 环境")
    print("python        :", sys.version.split()[0])
    print("sys.executable:", sys.executable)
    print("cwd           :", os.getcwd())
    print("LIE3_HOME     :", home, "| 存在:", home.is_dir())

    sys.path.insert(0, str(HERE / "shims"))
    sys.path.append(str(home / "_internal"))
    os.environ.setdefault("MAPLE_LOCAL_ONNX", "1")

    import Crypto

    print("Crypto        :", getattr(Crypto, "__version__", "?"), "<-", Crypto.__file__)

    # ---------------------------------------------------------------- ticket
    banner("1. 模拟密钥接收（桩：从票据文件读 model_key）")
    state_path = home / "data" / "license" / "ct_state.json"
    if not state_path.is_file():
        print("找不到", state_path)
        return 2
    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    ticket = (state.get("ticket") or "").strip()
    print("ticket 长度   :", len(ticket))
    if not ticket:
        print("票据为空 -> 桩无法取到密钥（需要先激活）")
        return 3
    payload_bytes = base64.urlsafe_b64decode(pad(ticket.split(".")[0]))
    payload = json.loads(payload_bytes)
    model_key = str(payload.get("model_key") or "")
    print("model_key 长度:", len(model_key))
    print("票据 device_id:", payload.get("device_id"))
    print("票据 exp      :", payload.get("exp"))
    print("文件 device_id:", state.get("device_id"))

    # ---------------------------------------------------------------- imports
    banner("2. 借用他的编译模块（CPython 3.12 + shims）")
    mods: dict[str, object] = {}
    for name in ("licensing.ticket", "licensing.device_id", "licensing.model_crypto", "ct_release"):
        try:
            mods[name] = __import__(name, fromlist=["*"])
            print(f"  OK    {name}")
        except Exception as exc:
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    if "licensing.model_crypto" not in mods:
        print("\n=> 无法借用解密模块，P0 失败")
        traceback.print_exc()
        return 4

    # ---------------------------------------------------------------- surface
    banner("3. 他的接口与实例指纹")
    for name in ("licensing.device_id", "licensing.model_crypto"):
        print(f"  {name}: {public_names(mods[name])}")

    dev = mods["licensing.device_id"]
    computed = None
    for attr in ("get_device_id", "device_id", "stable_instance_id", "client_instance_id"):
        fn = getattr(dev, attr, None)
        if fn is None:
            continue
        try:
            value = fn() if callable(fn) else fn
            print(f"  {attr}() = {value}")
            if attr == "get_device_id":
                computed = value
        except Exception as exc:
            print(f"  {attr}() 失败: {type(exc).__name__}: {exc}")
    print("  与票据一致   :", computed == payload.get("device_id"))

    mc = mods["licensing.model_crypto"]
    resolve_key = getattr(mc, "resolve_model_key", None)
    key_from_his = None
    if resolve_key is not None:
        for arg_name, arg in (("payload", payload), ("None", None)):
            try:
                key_from_his = resolve_key(arg)
                if key_from_his is not None:
                    print(f"  resolve_model_key({arg_name}) -> {type(key_from_his).__name__} "
                          f"len={len(key_from_his)} 前8字节={bytes(key_from_his[:8]).hex()}")
                    break
                print(f"  resolve_model_key({arg_name}) -> None")
            except Exception as exc:
                print(f"  resolve_model_key({arg_name}) 失败: {type(exc).__name__}: {str(exc)[:120]}")

    # ---------------------------------------------------------------- decrypt
    banner("4. 解密")
    decrypt = getattr(mc, "decrypt_file_to_bytes", None)
    if decrypt is None:
        print("decrypt_file_to_bytes 不存在")
        return 5
    print("  doc:", (decrypt.__doc__ or "").strip()[:100])

    candidates: dict[str, bytes] = {"utf-8 bytes": model_key.encode()}
    for name, fn in (
        ("base64url", lambda: base64.urlsafe_b64decode(pad(model_key))),
        ("hex", lambda: bytes.fromhex(model_key)),
    ):
        try:
            candidates[name] = fn()
        except Exception:
            pass
    if key_from_his is not None:
        candidates["his resolve_model_key"] = bytes(key_from_his)

    successes: list[str] = []
    failures: list[str] = []
    for model, expected in EXPECTED.items():
        enc = home / "_internal" / "lun_lies" / f"{model}.onnx.enc"
        banner(f"4.{model}  ({enc.stat().st_size} 字节, 期望明文 {expected})")
        done = False
        for cand_name, key_bytes in candidates.items():
            if done:
                break
            for label, kwargs in (
                ("ticket_payload=payload", {"ticket_payload": payload}),
                ("ticket_payload=None", {"ticket_payload": None}),
                ("无该参数", {}),
            ):
                tag = f"{cand_name} / {label}"
                try:
                    out = decrypt(enc, key_bytes, **kwargs)
                except Exception as exc:
                    msg = f"{type(exc).__name__}: {str(exc)[:110]}"
                    failures.append(f"{model}: {msg}  <- {tag}")
                    continue
                size = len(out) if out is not None else 0
                if size == expected:
                    head = bytes(out[:16])
                    print(f"  成功  {tag}  -> {size} 字节（与厂商日志一致）")
                    print(f"        onnx 前 16 字节: {head.hex()}")
                    print(f"        含 'ir_version': {b'ir_version' in bytes(out[:4096])}")
                    successes.append(f"{model}: OK {size} 字节  <- {tag}")
                    done = True
                    break
                failures.append(f"{model}: 返回 {size} 字节（期望 {expected}）  <- {tag}")
        if not done:
            print("  全部候选失败")

    banner("5. 汇总")
    print("成功:")
    for line in successes or ["  （无）"]:
        print("  " + line)
    print("失败明细（去重）:")
    seen = set()
    for line in failures:
        if line in seen:
            continue
        seen.add(line)
        print("  " + line)
    return 0 if successes else 6


if __name__ == "__main__":
    raise SystemExit(main())
