"""Minimal stand-in for the LIE3 release's ``app_paths`` module.

That module lives inside the vendor's frozen exe, so it does not exist on disk.
Every path here is rooted at ``LIE3_HOME`` — the *licensed* install directory —
because the vendor derives its device identity from the install path. Resolving
anything else would make ``device_id`` mismatch and the model would refuse to
decrypt.
"""
from __future__ import annotations

import json
import os
import pathlib
from typing import Any


def lie3_home() -> pathlib.Path:
    """The vendor install root (read-only borrow target)."""
    raw = (os.environ.get("LIE3_HOME") or "").strip()
    if not raw:
        raise RuntimeError("LIE3_HOME 未设置：必须指向 LIE3 的已授权安装目录")
    return pathlib.Path(raw).resolve()


# --- roots -----------------------------------------------------------------
def project_root() -> pathlib.Path:
    return lie3_home()


def app_dir() -> pathlib.Path:
    return lie3_home()


def static_data_dir() -> pathlib.Path:
    return lie3_home()


def bundled_data_dir() -> pathlib.Path:
    return lie3_home()


def resource_root() -> pathlib.Path:
    return lie3_home()


# --- model / runtime -------------------------------------------------------
def local_onnx_models_dir() -> pathlib.Path:
    return lie3_home() / "_internal" / "lun_lies"


def local_onnx_runtime_dir() -> pathlib.Path:
    return lie3_home() / "_internal"


def models_dir() -> pathlib.Path:
    return local_onnx_models_dir()


# --- licence / config ------------------------------------------------------
def license_dir() -> pathlib.Path:
    return lie3_home() / "data" / "license"


def data_dir() -> pathlib.Path:
    return lie3_home() / "data"


def load_login_config() -> dict[str, Any]:
    path = lie3_home() / "ct_login_config.json"
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass
    return {}


def resolve_api_base_url() -> str:
    return str(load_login_config().get("license_server_url") or "")


def is_packaged_client() -> bool:
    """We are a plain script, not the vendor's frozen exe."""
    return False


__all__ = [
    "lie3_home", "project_root", "app_dir", "static_data_dir", "bundled_data_dir",
    "resource_root", "local_onnx_models_dir", "local_onnx_runtime_dir", "models_dir",
    "license_dir", "data_dir", "load_login_config", "resolve_api_base_url",
    "is_packaged_client",
]
