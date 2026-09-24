"""Signed offline licensing boundary for MapleAssistant.

The client deliberately has *only* an Ed25519 public key. License issuance
lives in ``operator_tools/create_license.py`` and needs the operator's private
key, which must never be copied into a release package.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    _CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # Existing environments may predate the licensing update.
    class InvalidSignature(Exception):
        pass

    Ed25519PublicKey = None
    _CRYPTOGRAPHY_AVAILABLE = False


LICENSE_FORMAT = "maple-assistant-license/v1"
ACTIVATION_PREFIX = "MA1-"
DEVICE_REQUEST_PREFIX = "MADR1-"
PUBLIC_KEY_FILE = "license_public_key.json"
LICENSE_FILE = "license.json"
EDITION_NORMAL = "normal"
EDITION_NP = "np"
_FINGERPRINT_CACHE_SECONDS = 300.0
_fingerprint_cache_lock = threading.Lock()
_fingerprint_cache_at = 0.0
_fingerprint_cache: dict[str, str] = {}


@dataclass(frozen=True)
class LicenseStatus:
    """The non-throwing result consumed by the UI and automation gate."""

    valid: bool
    code: str
    message: str
    license_id: str = ""
    edition: str = ""
    expires_at: Optional[str] = None


def runtime_root() -> Path:
    """Return the installed package directory for source and Nuitka builds."""

    return Path(__file__).resolve().parent


def license_path(root: Optional[Path] = None) -> Path:
    return Path(root or runtime_root()) / LICENSE_FILE


def public_key_path(root: Optional[Path] = None) -> Path:
    return Path(root or runtime_root()) / PUBLIC_KEY_FILE


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _component_digest(name: str, value: str) -> str:
    """Hash one normalized device signal; raw serial data never leaves the PC."""

    material = f"MapleAssistant-device-v1\0{name}\0{value}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _machine_guid() -> str:
    if sys.platform != "win32":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            return str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip()
    except (OSError, ImportError):
        return ""


def _cim_value(class_name: str, property_name: str) -> str:
    """Read one stable Windows firmware signal, with a short bounded call."""

    if sys.platform != "win32":
        return ""
    command = (
        f"(Get-CimInstance -ClassName {class_name} -ErrorAction Stop).{property_name}"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=4, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.stdout.strip().splitlines()[0].strip() if result.returncode == 0 and result.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def machine_fingerprint_components() -> dict[str, str]:
    """Return hashed hardware signals suitable for a signed device binding."""

    global _fingerprint_cache_at, _fingerprint_cache
    with _fingerprint_cache_lock:
        if time.monotonic() - _fingerprint_cache_at < _FINGERPRINT_CACHE_SECONDS:
            return dict(_fingerprint_cache)
    raw = {
        "windows": _machine_guid(),
        "firmware": _cim_value("Win32_ComputerSystemProduct", "UUID"),
        "board": _cim_value("Win32_BaseBoard", "SerialNumber"),
    }
    components: dict[str, str] = {}
    for name, value in raw.items():
        normalized = " ".join(value.strip().upper().split())
        if normalized and normalized not in {"NONE", "UNKNOWN", "TO BE FILLED BY O.E.M."}:
            components[name] = _component_digest(name, normalized)
    with _fingerprint_cache_lock:
        _fingerprint_cache = dict(components)
        _fingerprint_cache_at = time.monotonic()
    return components


def device_request_code() -> str:
    """Create a portable, non-sensitive request code for one customer device."""

    components = machine_fingerprint_components()
    if len(components) < 2:
        raise ValueError("无法读取足够的设备信息，请以管理员身份重新启动助手。")
    document = {"format": "maple-assistant-device-request/v1", "components": components}
    raw = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return DEVICE_REQUEST_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def machine_binding_from_request(code: str) -> dict[str, Any]:
    """Validate an operator-supplied device request for inclusion in a license."""

    value = "".join(str(code).strip().split())
    if not value.startswith(DEVICE_REQUEST_PREFIX):
        raise ValueError("设备码格式错误。")
    try:
        document = json.loads(_b64decode(value[len(DEVICE_REQUEST_PREFIX):]).decode("utf-8"))
        components = document["components"]
    except (KeyError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("设备码无法解析。") from exc
    if document.get("format") != "maple-assistant-device-request/v1" or not isinstance(components, dict):
        raise ValueError("设备码版本不受支持。")
    cleaned = {
        str(name): str(digest) for name, digest in components.items()
        if str(name) in {"windows", "firmware", "board"}
        and len(str(digest)) == 64
    }
    if len(cleaned) < 2:
        raise ValueError("设备码缺少足够的设备信息。")
    return {"version": 1, "minimum_matches": 2, "components": cleaned}


def _machine_binding_status(binding: Any) -> Optional[LicenseStatus]:
    """Return an invalid status only when a signed device binding mismatches."""

    if binding is None:
        return None
    if not isinstance(binding, dict):
        return LicenseStatus(False, "machine", "授权设备绑定格式错误。")
    expected = binding.get("components")
    if not isinstance(expected, dict):
        return LicenseStatus(False, "machine", "授权设备绑定格式错误。")
    minimum = int(binding.get("minimum_matches", 2))
    current = machine_fingerprint_components()
    matches = sum(
        1 for name, digest in expected.items()
        if current.get(str(name)) == str(digest)
    )
    if matches < minimum:
        return LicenseStatus(False, "machine", "此授权码仅适用于另一台设备。")
    return None


def _b64decode(value: str) -> bytes:
    text = str(value).strip()
    return base64.urlsafe_b64decode((text + "=" * (-len(text) % 4)).encode("ascii"))


def _load_public_key(root: Optional[Path] = None) -> Any:
    if not _CRYPTOGRAPHY_AVAILABLE:
        raise ValueError("授权组件未安装；请双击 安装.bat 更新环境。")
    try:
        data = json.loads(public_key_path(root).read_text(encoding="utf-8"))
        if data.get("algorithm") != "Ed25519":
            raise ValueError("unsupported key algorithm")
        raw = _b64decode(str(data["public_key"]))
        if len(raw) != 32:
            raise ValueError("invalid Ed25519 public key length")
        return Ed25519PublicKey.from_public_bytes(raw)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("授权公钥不可用") from exc


def _parse_document(document: Any, root: Optional[Path] = None) -> LicenseStatus:
    if not isinstance(document, dict):
        return LicenseStatus(False, "malformed", "授权文件格式错误。")
    payload = document.get("payload")
    signature = document.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, str):
        return LicenseStatus(False, "malformed", "授权文件缺少签名信息。")
    try:
        _load_public_key(root).verify(_b64decode(signature), _canonical_payload(payload))
    except InvalidSignature:
        return LicenseStatus(False, "signature", "授权签名无效。")
    except ValueError as exc:
        return LicenseStatus(False, "key", str(exc))
    if payload.get("format") != LICENSE_FORMAT:
        return LicenseStatus(False, "format", "授权文件版本不受支持。")
    license_id = str(payload.get("license_id", "")).strip()
    edition = str(payload.get("edition", "")).strip().casefold()
    if not license_id or edition not in (EDITION_NORMAL, EDITION_NP):
        return LicenseStatus(False, "fields", "授权文件内容不完整。")
    machine_status = _machine_binding_status(payload.get("machine_binding"))
    if machine_status is not None:
        return LicenseStatus(
            False, machine_status.code, machine_status.message, license_id, edition
        )
    permanent = bool(payload.get("permanent", False))
    expires_at = payload.get("expires_at")
    if not permanent:
        if not isinstance(expires_at, str):
            return LicenseStatus(False, "expiry", "授权文件缺少到期时间。")
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except ValueError:
            return LicenseStatus(False, "expiry", "授权到期时间格式错误。")
        if datetime.now(timezone.utc) >= expiry.astimezone(timezone.utc):
            return LicenseStatus(False, "expired", "授权已过期。", license_id, edition, expires_at)
    return LicenseStatus(
        True, "valid", "授权有效。" if permanent else "授权有效（尚未到期）。",
        license_id, edition, None if permanent else str(expires_at),
    )


def verify_license(root: Optional[Path] = None, *, edition: str = EDITION_NORMAL) -> LicenseStatus:
    """Verify the installed license without throwing into the application."""

    try:
        document = json.loads(license_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LicenseStatus(False, "missing", "未授权：请激活授权码。")
    except (OSError, ValueError):
        return LicenseStatus(False, "unreadable", "授权文件无法读取。")
    status = _parse_document(document, root)
    if status.valid and status.edition != edition.casefold():
        return LicenseStatus(False, "edition", "此授权不适用于当前版本。", status.license_id, status.edition)
    return status


def activation_code(document: dict[str, Any]) -> str:
    """Return a portable offline activation token for a signed document."""

    raw = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return ACTIVATION_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def document_from_activation(code: str) -> dict[str, Any]:
    value = "".join(str(code).strip().split())
    if not value.startswith(ACTIVATION_PREFIX):
        raise ValueError("授权码格式错误。")
    try:
        document = json.loads(_b64decode(value[len(ACTIVATION_PREFIX):]).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("授权码无法解析。") from exc
    if not isinstance(document, dict):
        raise ValueError("授权码内容错误。")
    return document


def activate(code: str, root: Optional[Path] = None, *, edition: str = EDITION_NORMAL) -> LicenseStatus:
    """Validate an offline activation token and atomically persist it."""

    try:
        document = document_from_activation(code)
    except ValueError as exc:
        return LicenseStatus(False, "activation", str(exc))
    status = _parse_document(document, root)
    if status.valid and status.edition != edition.casefold():
        status = LicenseStatus(False, "edition", "此授权不适用于当前版本。", status.license_id, status.edition)
    if not status.valid:
        return status
    return _persist_document(document, status, root)


def _persist_document(document: dict[str, Any], status: LicenseStatus, root: Optional[Path]) -> LicenseStatus:
    """Persist an already verified signed entitlement as the client's JSON file."""

    target = license_path(root)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
            json.dump(document, handle, ensure_ascii=True, sort_keys=True, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, target)
    except OSError:
        return LicenseStatus(False, "write", "授权保存失败。")
    return status


def activate_via_server(
    code: str, endpoint: str, root: Optional[Path] = None,
    *, edition: str = EDITION_NORMAL,
) -> LicenseStatus:
    """Activate a short server-issued code and store its signed response locally.

    The protocol is deliberately tiny so the local emulator and a later HTTPS
    service can share it: the client submits its hashed device request and
    receives only a signed license document.
    """

    try:
        request_payload = json.dumps({
            "code": "".join(str(code).split()),
            "device_request": device_request_code(),
        }).encode("utf-8")
        url = endpoint.rstrip("/") + "/v1/activate"
        request = Request(url, data=request_payload, method="POST", headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            answer = json.loads(response.read().decode("utf-8"))
        document = answer["license"]
    except (ValueError, KeyError, OSError, URLError, json.JSONDecodeError) as exc:
        return LicenseStatus(False, "server", f"授权服务器不可用：{exc}")
    status = _parse_document(document, root)
    if status.valid and status.edition != edition.casefold():
        status = LicenseStatus(False, "edition", "此授权不适用于当前版本。", status.license_id, status.edition)
    return _persist_document(document, status, root) if status.valid else status


__all__ = [
    "ACTIVATION_PREFIX", "EDITION_NORMAL", "EDITION_NP", "LICENSE_FORMAT",
    "DEVICE_REQUEST_PREFIX", "LicenseStatus", "activate", "activation_code",
    "activate_via_server", "device_request_code", "document_from_activation", "machine_binding_from_request",
    "license_path", "public_key_path", "runtime_root", "verify_license",
]
