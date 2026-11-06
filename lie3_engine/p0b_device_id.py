r"""P0b — which input reproduces the ticket's device_id?

The same install folder yields a *different* device_id in our process than it did
inside the vendor's frozen exe, so the derivation has a contextual input. This
probe walks the plausible ones (working directory, environment, interpreter path)
and reports which one reproduces the ticket's value.

`licensing.device_id` exposes no `app_paths` attribute, so its inputs come from
`os` / `platform` / `uuid` / `hashlib` / `Path` — i.e. cwd, hostname, machine GUID
and the persisted instance UUID.

Run::
    $env:LIE3_HOME = "<licensed install>"
    .\venv-lie3\Scripts\python.exe lie3_engine\p0b_device_id.py
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
TARGET: str | None = None


def pad(text: str) -> str:
    return text + "=" * (-len(text) % 4)


def ticket_device_id(home: pathlib.Path) -> str | None:
    state_path = home / "data" / "license" / "ct_state.json"
    if not state_path.is_file():
        return None
    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    ticket = (state.get("ticket") or "").strip()
    if ticket:
        payload = json.loads(base64.urlsafe_b64decode(pad(ticket.split(".")[0])))
        return payload.get("device_id")
    return state.get("device_id")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    home = pathlib.Path(os.environ["LIE3_HOME"]).resolve()
    target = ticket_device_id(home)
    print("目标 device_id (票据) :", target)
    print("LIE3_HOME            :", home)

    sys.path.insert(0, str(HERE / "shims"))
    sys.path.append(str(home / "_internal"))
    import licensing.device_id as dev

    print("\n--- 无副作用查询 ---")
    for fn_name in ("get_or_create_instance_uuid", "get_machine_guid_for_license",
                    "get_legacy_machine_device_id"):
        fn = getattr(dev, fn_name, None)
        if fn is None:
            print(f"  {fn_name}: 不存在")
            continue
        try:
            print(f"  {fn_name}() = {fn()}")
        except Exception as exc:
            print(f"  {fn_name}() 失败: {type(exc).__name__}: {str(exc)[:100]}")

    print("\n--- device_id vs 工作目录 ---")
    cwds = {
        "当前(工作区)": os.getcwd(),
        "LIE3_HOME": str(home),
        "LIE3_HOME/_internal": str(home / "_internal"),
        "LIE3_HOME/data": str(home / "data"),
        "LIE3_HOME/data/license": str(home / "data" / "license"),
        "C:\\": "C:\\",
    }
    start = os.getcwd()
    hits: list[str] = []
    for label, path in cwds.items():
        try:
            os.chdir(path)
        except Exception as exc:
            print(f"  {label:24s} chdir 失败: {exc}")
            continue
        try:
            value = dev.get_device_id()
        except Exception as exc:
            print(f"  {label:24s} 失败: {type(exc).__name__}: {str(exc)[:80]}")
            continue
        mark = "  <<< 命中!" if value == target else ""
        print(f"  {label:24s} {value}{mark}")
        if value == target:
            hits.append(label)
    os.chdir(start)

    print("\n--- 命中结果 ---")
    print("  " + (", ".join(hits) if hits else "没有任何工作目录命中"))
    return 0 if hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
