"""Signed offline licensing boundary for MapleAssistant.

The client deliberately has *only* an Ed25519 public key. License issuance
lives in ``operator_tools/create_license.py`` and needs the operator's private
key, which must never be copied into a release package.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Optional
from pinned_tls import PinnedTlsError, PinnedTlsResponseError, post_json
from auto_lie_secret import clear_server_secret, set_server_secret
from runtime_paths import application_root


SERVER_LOG = logging.getLogger("server-client")
# Automatic lie handling keeps its own log file.  Everything that belongs to
# that feature - the credential delivered by an accepted reply, the event
# accounting, and the connection trace of the accounting request - is written
# here instead of into the licensing connection log, so the two diagnoses do
# not share a file.
AUTO_LIE_LOG = logging.getLogger("auto-lie")

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
# ``auth.json`` is the single local authorization document.  It supersedes
# ``license.json`` so a normal file-overwrite update can leave one clearly
# named private file untouched.
LICENSE_FILE = "auth.json"
LEGACY_LICENSE_FILE = "license.json"
# The last *server-accepted* fingerprint is local runtime identity, not a
# shareable recording.  Keep it beside auth.json at the application root.
CIM_IDENTITY_FILE = "cim_identity.dat"
AUTH_LOCAL_STATE_KEY = "_local"
ACTIVATION_SERVER_ENDPOINT = "https://211.149.169.194:8443"
ACTIVATION_SERVER_PIN_FILE = "activation_server_pin.json"
# The signed legacy document keeps an edition field for wire compatibility,
# but MoonEclipse now has one product edition.  Existing normal/NP documents
# remain valid; neither value changes available features.
_LEGACY_EDITION_VALUES = {"normal", "np"}
# A complete hardware fingerprint is stable while the assistant process is
# alive.  Match the normal heartbeat cadence so background auto-lie accounting
# never causes an unnecessary CIM refresh between heartbeats.
_FINGERPRINT_COMPLETE_CACHE_SECONDS = 3 * 60 * 60.0
# Do not hold an incomplete startup read for three hours: Windows may simply
# still be bringing CIM/WMI online, so the scheduled failure retry or a manual
# refresh must get another chance soon.
_FINGERPRINT_INCOMPLETE_CACHE_SECONDS = 300.0
# Windows may occasionally return an empty CIM value even though the firmware
# provider is otherwise healthy.  One short second attempt filters that
# transient state without turning every heartbeat into a retry loop.
_CIM_RETRY_DELAY_SECONDS = 0.20
_fingerprint_cache_lock = threading.Lock()
_fingerprint_cache_at = 0.0
_fingerprint_cache: dict[str, str] = {}
# A fingerprint is stable for the life of one assistant process.  Keep the
# most recent *complete* one separately: a transient WMI/CIM outage must not
# replace it with only MachineGuid and make later background work appear to be
# an unready device.
_fingerprint_last_good_cache: dict[str, str] = {}


def _dpapi_protect(payload: bytes) -> bytes:
    """Seal bytes to the current Windows user with DPAPI.

    A copied record cannot be decrypted on another PC/user profile.  The
    implementation uses Crypt32 directly so the protected Nuitka package does
    not depend on pywin32's optional ``win32crypt`` module.
    """

    if sys.platform != "win32":
        raise OSError("DPAPI is only available on Windows")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    source_buffer = ctypes.create_string_buffer(payload)
    source = DATA_BLOB(len(payload), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    protected = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = (
        ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
        wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    )
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if not crypt32.CryptProtectData(
        ctypes.byref(source), "MoonEclipse CIM identity v1", None,
        None, None, 0, ctypes.byref(protected),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(protected.pbData, protected.cbData)
    finally:
        if protected.pbData:
            kernel32.LocalFree(ctypes.cast(protected.pbData, wintypes.HLOCAL))


def _dpapi_unprotect(payload: bytes) -> bytes:
    """Open one current-user DPAPI record, rejecting foreign/corrupt data."""

    if sys.platform != "win32":
        raise OSError("DPAPI is only available on Windows")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    source_buffer = ctypes.create_string_buffer(payload)
    source = DATA_BLOB(len(payload), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    opened = DATA_BLOB()
    description = wintypes.LPWSTR()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = (
        ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB), wintypes.LPVOID, wintypes.LPVOID,
        wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    )
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), ctypes.byref(description), None,
        None, None, 0, ctypes.byref(opened),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(opened.pbData, opened.cbData)
    finally:
        if description:
            kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))
        if opened.pbData:
            kernel32.LocalFree(ctypes.cast(opened.pbData, wintypes.HLOCAL))


def _save_sealed_fingerprint(fingerprint: str, license_id: str, root: Optional[Path] = None) -> None:
    """Atomically store one server-accepted fingerprint outside exports."""

    if (not isinstance(fingerprint, str) or len(fingerprint) != 64
            or not isinstance(license_id, str) or not license_id):
        return
    try:
        record = json.dumps({
            "format": "moon-eclipse-cim-identity/v1",
            "fingerprint": fingerprint,
            "license_id": license_id,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sealed = _dpapi_protect(record)
        target = cim_identity_path(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False) as handle:
            handle.write(sealed)
            temporary = Path(handle.name)
        os.replace(temporary, target)
        SERVER_LOG.info("CIM last-good fingerprint sealed with DPAPI")
    except (OSError, ValueError, TypeError):
        SERVER_LOG.warning("could not save DPAPI-sealed CIM fingerprint", exc_info=True)


def _sealed_fingerprint_for_license(
    license_id: str, root: Optional[Path] = None,
) -> Optional[str]:
    """Return the DPAPI-protected last accepted fingerprint for this license.

    This is deliberately private: it is only a short boot bridge while CIM is
    still starting.  The next successful online heartbeat always replaces it
    with a freshly read live fingerprint.
    """

    try:
        sealed = cim_identity_path(root).read_bytes()
        record = json.loads(_dpapi_unprotect(sealed).decode("utf-8"))
        valid = bool(
            isinstance(record, dict)
            and record.get("format") == "moon-eclipse-cim-identity/v1"
            and record.get("license_id") == license_id
            and isinstance(record.get("fingerprint"), str)
            and len(record["fingerprint"]) == 64
        )
        if valid:
            return str(record["fingerprint"])
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        pass
    return None


def has_sealed_fingerprint(license_id: str, root: Optional[Path] = None) -> bool:
    """Whether this Windows profile holds a valid sealed identity for a license."""

    return _sealed_fingerprint_for_license(license_id, root) is not None


@dataclass(frozen=True)
class LicenseStatus:
    """The non-throwing result consumed by the UI and automation gate."""

    valid: bool
    code: str
    message: str
    license_id: str = ""
    edition: str = ""
    expires_at: Optional[str] = None
    equipment_id: str = ""
    auto_lie_allowed: bool = True
    remaining_auto_lie_count: Optional[int] = None
    # ``True`` when the server reports an unmetered balance (it sends a null
    # count).  The header then shows 无限 and the quota gate must not disarm the
    # automatic lie pass.
    remaining_auto_lie_unlimited: bool = False
    # The current activation code's explicit mode.  Older servers omit this;
    # the legacy unlimited marker remains the compatibility fallback.
    auto_lie_unlimited_mode: bool = False
    # Unlimited-mode takeovers since the operator's last settlement.
    auto_lie_unsettled_count: int = 0
    lie_detect_total: int = 0
    lie_detect_success_total: int = 0
    lie_detect_failed_total: int = 0
    # Per-period counters the server adds next to the lifetime totals: the header
    # shows today's 总/成功/失败 and yesterday's.  Each entry is
    # ``{"total": int, "success": int, "failed": int}``.
    lie_detect_today: dict[str, int] = field(default_factory=dict)
    lie_detect_yesterday: dict[str, int] = field(default_factory=dict)
    # The newest release the server knows about, as ``{"version": "1.2.292"}``.
    # ``None`` means the server said nothing (older server, or nothing published),
    # which is deliberately different from an empty version string.
    server_release: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class ActivationTransferPreview:
    """One server-authorized, expiring activation-replacement preview."""

    ok: bool
    code: str
    message: str
    transfer_token: str = ""
    bound_elsewhere: bool = False
    bound_equipment_id: str = ""
    expires_in_seconds: int = 0
    device_status: LicenseStatus | None = None


class DeviceNotReadyError(ValueError):
    """Stable device signals are not yet available after Windows startup."""


def runtime_root() -> Path:
    """Return the installed package directory for source and Nuitka builds."""

    # ``__file__`` is correct from source, but a frozen build can resolve it
    # inside the one-file/standalone runtime rather than beside the launcher.
    # The latter is where the installer keeps ``助手配置``.  Use the same
    # executable-aware root policy as the rest of the installed application.
    return application_root(__file__)


def _recording_config_root(root: Optional[Path] = None) -> Path:
    """Return the shareable recording folder, never the auth/runtime root."""

    if root is not None:
        return Path(root)
    configured = os.environ.get("MOONECLIPSE_CONFIG_DIR")
    if configured:
        return Path(configured)
    package_root = runtime_root()
    # Keep entitlement and user_config together even when an update is
    # launched directly from the overlaid ``app`` folder rather than through
    # the installer-created launcher that sets MOONECLIPSE_CONFIG_DIR.
    for candidate in (package_root / "助手配置", package_root.parent / "助手配置"):
        if candidate.is_dir():
            return candidate
    return package_root


def license_path(root: Optional[Path] = None) -> Path:
    """Authorization belongs to the install root, outside shareable configs."""

    return Path(root) / LICENSE_FILE if root is not None else runtime_root() / LICENSE_FILE


def _legacy_license_path(root: Optional[Path] = None) -> Path:
    return Path(root) / LEGACY_LICENSE_FILE if root is not None else runtime_root() / LEGACY_LICENSE_FILE


def cim_identity_path(root: Optional[Path] = None) -> Path:
    """Path of the DPAPI-sealed, non-exported last-good fingerprint record."""

    return Path(root) / CIM_IDENTITY_FILE if root is not None else runtime_root() / CIM_IDENTITY_FILE


def _legacy_config_auth_path(root: Optional[Path] = None) -> Path:
    """Return the pre-v1.2.287 auth location for one-time migration only."""

    return _recording_config_root(root) / LICENSE_FILE


def _signed_document_at(path: Path) -> Optional[dict[str, Any]]:
    """Read a candidate entitlement only when it has the signed shape."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    if not {"payload", "signature"}.issubset(document):
        return None
    return document


def _migrate_displaced_auth_document(root: Optional[Path] = None) -> None:
    """Move old ``助手配置\\auth.json`` into the private application root.

    Earlier builds mixed authorization with exportable recordings.  The move
    is one-way: the old file is removed only after an atomic root replacement
    succeeds, so an interrupted migration never loses authorization.
    """

    target = license_path(root)
    legacy_copy = _legacy_config_auth_path(root)
    # ``_legacy_config_auth_path`` falls back to the package root when the install
    # has no ``助手配置`` folder, so it can name the SAME file as ``target``.  The
    # duplicate-removal branch below then unlinked the authoritative document
    # itself: every verification (startup, heartbeat, and therefore the update
    # icon's record fetch) left the installation unlicensed.  Never delete when
    # the two paths are the same file, and treat "cannot compare" as "same".
    try:
        same_file = legacy_copy.resolve() == target.resolve()
    except OSError:
        same_file = True
    if _signed_document_at(target) is not None:
        # A previous overlay update can leave a duplicate signed document in
        # the shareable folder.  The root document is already authoritative,
        # so remove that duplicate rather than continuing to export private
        # entitlement material with recordings.
        if not same_file and _signed_document_at(legacy_copy) is not None:
            try:
                legacy_copy.unlink()
                SERVER_LOG.info("duplicate authorization removed from 助手配置")
            except OSError:
                SERVER_LOG.warning(
                    "could not remove duplicate authorization from 助手配置",
                    exc_info=True,
                )
        return
    app_side = _legacy_config_auth_path(root)
    try:
        if app_side.resolve() == target.resolve():
            return
    except OSError:
        return
    document = _signed_document_at(app_side)
    if document is None:
        return
    try:
        # Preserve display metadata already written to the new private root.
        local: dict[str, Any] = {}
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
            candidate = existing.get(AUTH_LOCAL_STATE_KEY, {}) if isinstance(existing, dict) else {}
            if isinstance(candidate, dict):
                local = candidate
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        output = dict(document)
        if local:
            output[AUTH_LOCAL_STATE_KEY] = local
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, delete=False,
        ) as handle:
            json.dump(output, handle, ensure_ascii=True, sort_keys=True, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, target)
        app_side.unlink(missing_ok=True)
        SERVER_LOG.info("authorization migrated from 助手配置 to the application root")
    except OSError:
        SERVER_LOG.warning("authorization migration from app-side auth.json failed", exc_info=True)


def _migrate_legacy_license_document(root: Optional[Path] = None) -> None:
    """Move a pre-auth.json entitlement once, retaining local display state.

    A prior build may already have created an unsigned auth.json containing
    only activation-code/memo display state.  Merge that ``_local`` object
    into the signed legacy document, then atomically replace auth.json and
    remove the old license.json.  This is local migration only; releases do
    not ship either file.
    """

    legacy = _legacy_license_path(root)
    if not legacy.is_file():
        return
    target = license_path(root)
    try:
        legacy_document = json.loads(legacy.read_text(encoding="utf-8"))
        if not isinstance(legacy_document, dict):
            return
        existing = {}
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            existing = {}
        # v1.2.269 briefly used a flat auth.json for this local-only state.
        # Accept both shapes while moving the signed entitlement into auth.json.
        # Never copy an already-signed document over the legacy entitlement.
        if isinstance(existing, dict) and {
            "payload", "signature"
        }.issubset(existing):
            return
        local = existing.get(AUTH_LOCAL_STATE_KEY, {}) if isinstance(existing, dict) else {}
        if not isinstance(local, dict) and isinstance(existing, dict):
            local = {}
        if isinstance(existing, dict) and not local:
            local = {
                key: value
                for key, value in existing.items()
                if key not in {"payload", "signature", AUTH_LOCAL_STATE_KEY}
            }
        if isinstance(local, dict) and local:
            legacy_document[AUTH_LOCAL_STATE_KEY] = local
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, delete=False
        ) as handle:
            json.dump(legacy_document, handle, ensure_ascii=True, sort_keys=True, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, target)
        legacy.unlink(missing_ok=True)
        SERVER_LOG.info("authorization migrated from license.json to auth.json")
    except OSError:
        SERVER_LOG.warning("authorization migration to auth.json failed", exc_info=True)


def revoke_license(root: Optional[Path] = None) -> bool:
    """Remove the locally saved entitlement after a rejected replacement code.

    An activation attempt is authoritative: if the submitted code cannot be
    validated, an older entitlement must not make the next launch appear
    authorized.  A fresh valid activation recreates this file atomically.
    """

    target = license_path(root)
    try:
        target.unlink(missing_ok=True)
        _legacy_license_path(root).unlink(missing_ok=True)
        return True
    except OSError:
        return False


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


def _usable_device_value(value: str) -> str:
    """Normalize one hardware signal, returning empty when Windows has no usable value."""

    normalized = " ".join(str(value or "").strip().upper().split())
    if normalized in {"", "NONE", "UNKNOWN", "TO BE FILLED BY O.E.M."}:
        return ""
    return normalized


def _cim_value_with_one_retry(class_name: str, property_name: str, signal_name: str) -> str:
    """Read one CIM signal again once when its first bounded lookup is unavailable."""

    value = _cim_value(class_name, property_name)
    if _usable_device_value(value):
        return value
    # This runs only in the background activation/heartbeat path.  Do not log
    # raw serial data, but retain enough information to distinguish a local
    # provider hiccup from an activation-server refusal.
    SERVER_LOG.info("CIM %s signal unavailable; retrying once", signal_name)
    time.sleep(_CIM_RETRY_DELAY_SECONDS)
    retry = _cim_value(class_name, property_name)
    if _usable_device_value(retry):
        SERVER_LOG.info("CIM %s signal recovered on retry", signal_name)
    else:
        SERVER_LOG.warning("CIM %s signal remained unavailable after retry", signal_name)
    return retry


def machine_fingerprint_components(*, force_refresh: bool = False) -> dict[str, str]:
    """Return hashed hardware signals suitable for a signed device binding."""

    global _fingerprint_cache_at, _fingerprint_cache, _fingerprint_last_good_cache
    with _fingerprint_cache_lock:
        cache_lifetime = (
            _FINGERPRINT_COMPLETE_CACHE_SECONDS
            if len(_fingerprint_cache) >= 2
            else _FINGERPRINT_INCOMPLETE_CACHE_SECONDS
        )
        if (not force_refresh
                and time.monotonic() - _fingerprint_cache_at < cache_lifetime):
            return dict(_fingerprint_cache)
    raw = {
        "windows": _machine_guid(),
        "firmware": _cim_value_with_one_retry(
            "Win32_ComputerSystemProduct", "UUID", "firmware UUID",
        ),
        "board": _cim_value_with_one_retry(
            "Win32_BaseBoard", "SerialNumber", "motherboard serial",
        ),
    }
    components: dict[str, str] = {}
    for name, value in raw.items():
        normalized = _usable_device_value(value)
        if normalized:
            components[name] = _component_digest(name, normalized)
    with _fingerprint_cache_lock:
        if len(components) >= 2:
            _fingerprint_last_good_cache = dict(components)
            _fingerprint_cache = dict(components)
        elif len(_fingerprint_last_good_cache) >= 2:
            # The device was already authenticated with this fingerprint in
            # this process.  Do not turn a short CIM outage during asynchronous
            # lie accounting into a false "device not ready" verdict.
            SERVER_LOG.warning(
                "CIM refresh incomplete; retaining the last complete device fingerprint"
            )
            _fingerprint_cache = dict(_fingerprint_last_good_cache)
        else:
            _fingerprint_cache = dict(components)
        _fingerprint_cache_at = time.monotonic()
        return dict(_fingerprint_cache)


def device_request_code() -> str:
    """Create a portable, non-sensitive request code for one customer device."""

    components = machine_fingerprint_components()
    if len(components) < 2:
        raise DeviceNotReadyError("设备未就绪，稍后重试")
    document = {"format": "maple-assistant-device-request/v1", "components": components}
    raw = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return DEVICE_REQUEST_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def device_identity_for_request(
    *, force_refresh: bool = False,
) -> tuple[str, dict[str, str]]:
    """The composite fingerprint together with the digests that produced it.

    Every licensing request sends both halves.  The composite answers "is this
    the same reading?", and the digests let the server answer "is this the same
    EQUIPMENT?" when the composite legitimately changed - a system restore moves
    ``MachineGuid`` back, and a PC that is still bringing WMI online reports only
    two of the three signals.  They are read once here so the two halves of one
    request can never disagree.
    """

    components = machine_fingerprint_components(force_refresh=force_refresh)
    if len(components) < 2:
        # MachineGuid is normally ready first, while CIM/WMI firmware and
        # motherboard providers may still be starting just after a reboot.
        # Do not misreport this as a server or activation-code failure.
        raise DeviceNotReadyError("设备未就绪，稍后重试")
    material = json.dumps(components, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(
        f"MapleAssistant-online-fingerprint/v1\\0{material}".encode("ascii")
    ).hexdigest()
    return fingerprint, dict(components)


def machine_fingerprint_hash(*, force_refresh: bool = False) -> str:
    """Return the opaque, stable fingerprint sent to the fixed activation server."""

    fingerprint, _components = device_identity_for_request(force_refresh=force_refresh)
    return fingerprint


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


def _machine_binding_status(
    binding: Any, license_id: str = "", root: Optional[Path] = None,
) -> Optional[LicenseStatus]:
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
    if len(current) < minimum:
        if license_id and has_sealed_fingerprint(license_id, root):
            return None
        # A boot-time CIM outage is not proof that this is a different PC.
        # Preserve that distinction so the heartbeat worker can run its fixed
        # readiness retry schedule instead of falsely rejecting the license.
        return LicenseStatus(False, "device_not_ready", "设备未就绪，稍后重试")
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


def _document_fingerprint_status(
    payload: dict[str, Any], license_id: str, edition: str,
    root: Optional[Path],
) -> Optional[LicenseStatus]:
    """Exact composite check for a document that carries no per-signal binding.

    Kept as its own step because a document issued by an older server has only
    the composite to stand on, while a newer one ships ``machine_binding`` and is
    judged by ``_machine_binding_status`` instead.
    """

    try:
        try:
            fingerprint = machine_fingerprint_hash()
        except DeviceNotReadyError:
            # CIM can be unavailable during the first seconds of Windows
            # startup.  A DPAPI record is usable only on this same user
            # profile and only for this same signed license, so it can
            # keep local verification alive until the scheduled live
            # heartbeat obtains a fresh CIM reading.
            fingerprint = _sealed_fingerprint_for_license(license_id, root)
            if fingerprint is None:
                raise
        if payload.get("fingerprint_hash") != fingerprint:
            return LicenseStatus(False, "machine", "此授权码仅适用于另一台设备。", license_id, edition)
    except DeviceNotReadyError as exc:
        return LicenseStatus(False, "device_not_ready", str(exc), license_id, edition)
    except ValueError as exc:
        return LicenseStatus(False, "machine", str(exc), license_id, edition)
    return None


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
    document_format = payload.get("format")
    if document_format not in {LICENSE_FORMAT, "maple-assistant-license/v2"}:
        return LicenseStatus(False, "format", "授权文件版本不受支持。")
    license_id = str(payload.get("license_id", "")).strip()
    edition = str(payload.get("edition", "")).strip().casefold()
    if not license_id or edition not in _LEGACY_EDITION_VALUES:
        return LicenseStatus(False, "fields", "授权文件内容不完整。")
    if document_format == "maple-assistant-license/v2":
        binding = payload.get("machine_binding")
        if binding is None:
            # Legacy document: the single composite is the only device claim it
            # makes, so equality is all that can be checked here.
            status = _document_fingerprint_status(payload, license_id, edition, root)
            if status is not None:
                return status
        else:
            # The server recorded the digests behind the composite, so the same
            # equipment may legitimately report a different composite (registry
            # rollback, or one signal still unavailable at boot).  The digest
            # rule replaces the equality test instead of being added to it -
            # otherwise a restored PC would still be rejected by the stale hash.
            machine_status = _machine_binding_status(binding, license_id, root)
            if machine_status is not None:
                return LicenseStatus(
                    False, machine_status.code, machine_status.message, license_id, edition
                )
    else:
        machine_status = _machine_binding_status(
            payload.get("machine_binding"), license_id, root,
        )
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


def _legacy_app_root(root: Optional[Path] = None) -> Optional[Path]:
    """The ``app\\`` folder of a pre-1.2.285 layout, when it sits under this root."""

    base = Path(root) if root is not None else runtime_root()
    legacy = base / "app"
    try:
        if legacy.is_dir() and legacy.resolve() != base.resolve():
            return legacy
    except OSError:
        return None
    return None


def _adopt_legacy_app_authorization(root: Optional[Path] = None) -> None:
    """Take over authorization documents an old ``app\\`` layout left behind.

    Installs up to 1.2.284 kept everything in ``app\\``, so the activation lived at
    ``app\\auth.json`` (plus ``cim_identity.dat`` and ``license_settings.json``).
    The installer now unpacks at the selected root and drops that tree, and an
    overlay update starts the app from the new root - either way the old folder is
    no longer read, and the client had to activate again (field report: "why i
    updated then previous auth info is lost").

    Nothing is overwritten: a document is adopted only when this root has none (a
    signed ``auth.json`` is authoritative and never replaced by a legacy copy).
    """

    legacy = _legacy_app_root(root)
    if legacy is None:
        return
    base = Path(root) if root is not None else runtime_root()
    for name in (LICENSE_FILE, CIM_IDENTITY_FILE, "license_settings.json"):
        source = legacy / name
        target = base / name
        try:
            if not source.is_file():
                continue
            if name == LICENSE_FILE:
                if _signed_document_at(target) is not None:
                    # This root already holds the authoritative entitlement.
                    continue
            elif target.exists():
                continue
            payload = source.read_bytes()
            if name == LICENSE_FILE:
                payload = _merge_local_state(payload, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "wb", dir=target.parent, delete=False,
            ) as handle:
                handle.write(payload)
                temporary = Path(handle.name)
            os.replace(temporary, target)
            SERVER_LOG.info(
                "authorization file %s adopted from the legacy app folder", name
            )
        except OSError:
            SERVER_LOG.warning(
                "could not adopt %s from %s", name, legacy, exc_info=True
            )


def _merge_local_state(payload: bytes, target: Path) -> bytes:
    """Carry over the display metadata (``_local``) of an unsigned local document."""

    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return payload
    if not isinstance(existing, dict):
        return payload
    local = existing.get(AUTH_LOCAL_STATE_KEY)
    if not isinstance(local, dict) or not local:
        return payload
    try:
        document = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return payload
    if not isinstance(document, dict):
        return payload
    document[AUTH_LOCAL_STATE_KEY] = local
    return json.dumps(document, ensure_ascii=True, sort_keys=True, indent=2).encode("utf-8") + b"\n"


def verify_license(root: Optional[Path] = None) -> LicenseStatus:
    """Verify the installed license without throwing into the application."""

    # An older ``app\\`` layout keeps the activation one level down; adopt it before
    # anything decides this install is unlicensed.
    _adopt_legacy_app_authorization(root)
    _migrate_legacy_license_document(root)
    # A same-folder legacy license is the direct predecessor and therefore
    # takes precedence over an app-side fallback left by an overlay build.
    _migrate_displaced_auth_document(root)
    try:
        document = json.loads(license_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LicenseStatus(False, "missing", "未授权：请激活授权码。")
    except (OSError, ValueError):
        return LicenseStatus(False, "unreadable", "授权文件无法读取。")
    # An interrupted older upgrade may have left only local UI metadata in
    # auth.json.  It is not a corrupt license and should simply invite a new
    # activation if no legacy license.json remains to migrate.
    if not isinstance(document, dict) or "payload" not in document:
        return LicenseStatus(False, "missing", "未授权：请激活授权码。")
    status = _parse_document(document, root)
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


def activate(code: str, root: Optional[Path] = None) -> LicenseStatus:
    """Validate an offline activation token and atomically persist it."""

    try:
        document = document_from_activation(code)
    except ValueError as exc:
        return LicenseStatus(False, "activation", str(exc))
    status = _parse_document(document, root)
    if not status.valid:
        return status
    return _persist_document(document, status, root)


def _persist_document(document: dict[str, Any], status: LicenseStatus, root: Optional[Path]) -> LicenseStatus:
    """Persist an already verified signed entitlement as the client's JSON file."""

    target = license_path(root)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # A fresh activation replaces only the signed entitlement.  Preserve
        # the local UI state stored alongside it in auth.json.
        local: dict[str, Any] = {}
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
            candidate = existing.get(AUTH_LOCAL_STATE_KEY, {}) if isinstance(existing, dict) else {}
            if isinstance(existing, dict) and not isinstance(candidate, dict):
                candidate = {}
            # Preserve the short-lived flat auth.json layout too.  This lets a
            # user update directly from that release without losing memo/UI
            # display state during the first successful activation.
            if isinstance(existing, dict) and not candidate and "payload" not in existing:
                candidate = {
                    key: value for key, value in existing.items()
                    if key not in {"payload", "signature", AUTH_LOCAL_STATE_KEY}
                }
            if isinstance(candidate, dict):
                local = candidate
        except (OSError, ValueError):
            pass
        output = dict(document)
        if local:
            output[AUTH_LOCAL_STATE_KEY] = local
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
            json.dump(output, handle, ensure_ascii=True, sort_keys=True, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, target)
    except OSError:
        return LicenseStatus(False, "write", "授权保存失败。")
    return status


def _with_server_device(
    status: LicenseStatus, answer: Any,
) -> LicenseStatus:
    """Attach device state and retain the server key in memory only.

    Only an explicit ``auto_lie`` object may change the in-memory credential: an
    empty ``secret_key`` is the server saying this device has no passes left,
    and a non-empty one is the credential itself.

    A reply that does not mention the credential at all leaves it untouched.
    Treating that silence as "no credential" used to disarm 自动过测谎 until the
    next three-hourly heartbeat, twice over: once for the post-pass accounting
    reply (an older server never sends the field there), and once for every
    activation/heartbeat reply of a server that omits it.  A device with a valid
    license and passes left must never lose its credential because a reply
    happened not to carry the field.
    """

    device = answer.get("device") if isinstance(answer, dict) else None
    usage = answer.get("autolie_fingerprint_usage") if isinstance(answer, dict) else None
    auto_lie = answer.get("auto_lie") if isinstance(answer, dict) else None
    # The tracking/coordinate-cloud key ("tx_key") may arrive either inside the
    # ``auto_lie`` object or as its own top-level field, depending on the server
    # generation.  Both names are the same credential the local 测谎 service
    # authenticates its WS handshake with, so either one is used.
    tx_key = ""
    if isinstance(answer, dict):
        tx_key = str(answer.get("tx_key") or "").strip()
    if not tx_key and isinstance(auto_lie, dict):
        tx_key = str(auto_lie.get("tx_key") or "").strip()
    if isinstance(auto_lie, dict):
        secret = str(auto_lie.get("secret_key") or "").strip()
        set_server_secret(secret)
        if secret:
            AUTO_LIE_LOG.info("auto-lie credential refreshed from server reply")
            # 本地测谎 uses the same coordinate-cloud key: hand the server's answer
            # to the local service so it is not limited to the key baked into its
            # package (the operator's rule: "the local service should also report
            # and get key from response (activation server)").
            try:
                import local_lie_service

                local_lie_service.sync_product_key(tx_key or secret)
            except Exception:  # noqa: BLE001 - a key refresh must never break a reply
                AUTO_LIE_LOG.debug(
                    "the local lie service key could not be refreshed", exc_info=True
                )
        else:
            AUTO_LIE_LOG.warning(
                "auto-lie credential withheld by the server (empty auto_lie.secret_key)"
            )
    else:
        # Silence is not a verdict (see the docstring): the credential stays.
        AUTO_LIE_LOG.debug("auto-lie credential left untouched (this reply carries none)")
        if tx_key:
            # A reply that names only the tracking key still owes the local service
            # its credential; the auto-lie block itself stays untouched.
            try:
                import local_lie_service

                local_lie_service.sync_product_key(tx_key)
            except Exception:  # noqa: BLE001 - never break a licence reply
                AUTO_LIE_LOG.debug(
                    "the local lie service tx_key could not be written", exc_info=True
                )
    # 本地测谎: the activation code's CT card travels in the device document, so the
    # desktop starts the local service with the ONLINE card instead of asking the
    # operator (the 激活测谎 button was removed for that reason).
    card_source = device if isinstance(device, dict) else None
    if isinstance(card_source, dict) and card_source.get("ct_card"):
        try:
            import local_lie_service

            local_lie_service.apply_online_card(str(card_source["ct_card"]))
        except Exception:  # noqa: BLE001 - never break a licence reply
            AUTO_LIE_LOG.debug(
                "the local lie service card could not be applied", exc_info=True
            )
    elif isinstance(card_source, dict):
        # The reply IS the device document, so this is authoritative: the code
        # bound to this device has no CT card, and 本地测谎 therefore stays off
        # (field log 12:31:46: "本地测谎 selected, but the licence server has not
        # delivered a CT card yet" followed by 测试api failing on 8765).  Say it
        # once, with the fix in the message, instead of only in the panel hint.
        try:
            import local_lie_service

            # Authoritative: the reply without a card means this code has no
            # 本地测谎 access, so the desktop greys that selection (it is not a
            # "not arrived yet" guess - the device document was just delivered).
            local_lie_service.note_card_absent()
            if local_lie_service.hint_state() == "no_card":
                import logging as _logging

                _logging.getLogger("local-lie-service").warning(
                    "the licence reply carries no CT card - set ct_card on this "
                    "device's activation code (manager site / admin / "
                    "code_action ct_card); 本地测谎 stays off until then"
                )
        except Exception:  # noqa: BLE001 - diagnostics only
            pass
    elif not isinstance(device, dict) and isinstance(usage, dict):
        # A usage-only reply (the lie-event accounting on
        # /api/v1/local-lie-events) is NOT a licence document: it says nothing
        # about the CT card, so it must never change the 本地测谎 decision.  Only a
        # reply that carries the device document is authoritative, and the server
        # always emits ``ct_card`` in it - empty when this code has no card.
        pass
    if not isinstance(device, dict) and not isinstance(usage, dict):
        return status
    # The full device document remains the compatibility response.  The
    # smaller usage object is emitted on every lie-event reply so the desktop
    # can refresh the header immediately without depending on its shape.
    server_state = dict(device) if isinstance(device, dict) else {}
    if isinstance(usage, dict):
        server_state.update(usage)
    equipment_id = str(server_state.get("equipment_id", status.equipment_id)).strip()
    # A numeric balance and the old unlimited marker must never coexist in the
    # desktop state.  In particular, an activation can replace the initial
    # empty placeholder with a finite grant; treating a stale ``true`` flag as
    # authoritative made that grant render as "无限" until the next restart.
    has_remaining = "remaining_auto_lie_count" in server_state
    reported_remaining = server_state.get("remaining_auto_lie_count")
    if isinstance(reported_remaining, int) and not isinstance(reported_remaining, bool):
        remaining = reported_remaining
    elif has_remaining and reported_remaining is None:
        remaining = None
    else:
        remaining = status.remaining_auto_lie_count

    def _counter(name: str, current: int) -> int:
        value = server_state.get(name, current)
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return current

    def _triple(name: str, current: dict[str, int]) -> dict[str, int]:
        """Read one additive ``{"total", "success", "failed"}`` usage object.

        An older server omits it, in which case the previous value (usually an
        empty object, i.e. unknown) is kept rather than inventing zeros.
        """

        value = server_state.get(name)
        if not isinstance(value, dict):
            return current
        answer: dict[str, int] = {}
        for key in ("total", "success", "failed"):
            try:
                answer[key] = max(0, int(value.get(key, 0) or 0))
            except (TypeError, ValueError):
                answer[key] = 0
        return answer

    # A null count is the server's "unmetered" answer.  Only the server's own
    # explicit flag is trusted: an older server never sends it, and inferring
    # "unlimited" from a missing count would silently disarm the quota gate.
    unlimited = bool(
        has_remaining
        and reported_remaining is None
        and server_state.get("remaining_auto_lie_unlimited") is True
    )
    unlimited_mode = bool(server_state.get("auto_lie_unlimited_mode", unlimited))

    return replace(
        status,
        equipment_id=equipment_id if len(equipment_id) == 8 else status.equipment_id,
        auto_lie_allowed=bool(server_state.get("auto_lie_allowed", status.auto_lie_allowed)),
        remaining_auto_lie_count=remaining,
        remaining_auto_lie_unlimited=bool(unlimited),
        auto_lie_unlimited_mode=unlimited_mode,
        auto_lie_unsettled_count=_counter(
            "auto_lie_unsettled_count", status.auto_lie_unsettled_count
        ),
        lie_detect_total=_counter("lie_detect_total", status.lie_detect_total),
        lie_detect_success_total=_counter("lie_detect_success_total", status.lie_detect_success_total),
        lie_detect_failed_total=_counter("lie_detect_failed_total", status.lie_detect_failed_total),
        lie_detect_today=_triple("lie_detect_today", status.lie_detect_today),
        lie_detect_yesterday=_triple(
            "lie_detect_yesterday", status.lie_detect_yesterday
        ),
    )


def activate_via_server(code: str, root: Optional[Path] = None) -> LicenseStatus:
    """Activate against the built-in, certificate-pinned licensing endpoint.

    The credential is not dropped before the request, for the same reason the
    heartbeat no longer drops it: a transient failure would otherwise destroy a
    still-valid credential.  A new entitlement must not inherit the previous
    code's credential, so it is dropped when the activation is refused, fails,
    or succeeds without an explicit ``auto_lie`` in the reply.
    """

    try:
        SERVER_LOG.info("activation requested")
        fingerprint, components = device_identity_for_request()
        answer = post_json(
            ACTIVATION_SERVER_ENDPOINT,
            "/api/v1/activate",
            {
                "activation_code": "".join(str(code).upper().split()),
                "fingerprint": fingerprint,
                "components": components,
            },
            runtime_root() / ACTIVATION_SERVER_PIN_FILE,
        )
        document = answer["license"]
    except PinnedTlsResponseError as exc:
        SERVER_LOG.warning(
            "activation rejected by server status=%s code=%s", exc.status, exc.code
        )
        clear_server_secret()
        # Rejected activation replies still carry the safe public device
        # summary.  Keep it: a known device must not lose its equipment ID
        # merely because the submitted replacement code was rejected.
        return _with_server_device(
            LicenseStatus(False, f"server:{exc.code}", exc.message),
            exc.data,
        )
    except DeviceNotReadyError:
        SERVER_LOG.info("activation postponed: device fingerprint is not ready")
        clear_server_secret()
        return LicenseStatus(False, "device_not_ready", "设备未就绪，稍后重试")
    except (ValueError, KeyError, OSError, PinnedTlsError, json.JSONDecodeError) as exc:
        SERVER_LOG.warning("activation failed category=%s", type(exc).__name__)
        clear_server_secret()
        return LicenseStatus(False, "server", "激活验证失败")
    status = _parse_document(document, root)
    if not status.valid:
        SERVER_LOG.warning("activation response rejected category=%s", status.code)
        clear_server_secret()
        return status
    persisted = _with_server_device(_persist_document(document, status, root), answer)
    if persisted.valid:
        _save_sealed_fingerprint(fingerprint, persisted.license_id, root)
        SERVER_LOG.info("activation accepted license_id=%s", persisted.license_id)
        if not isinstance(answer.get("auto_lie"), dict):
            # This entitlement replaces the previous code's, so it must not
            # inherit that code's credential.
            clear_server_secret()
            AUTO_LIE_LOG.warning(
                "auto-lie credential dropped: the activated reply carried none"
            )
    else:
        SERVER_LOG.warning("activation could not be saved category=%s", persisted.code)
        clear_server_secret()
    return persisted


def preview_activation_transfer_via_server(code: str, root: Optional[Path] = None) -> ActivationTransferPreview:
    """Ask the server whether a code would replace another device binding.

    This is deliberately read-only as far as the activation binding is
    concerned.  Its token is mandatory for the later mutating request.
    """

    clean_code = "".join(str(code or "").upper().split())
    try:
        SERVER_LOG.info("activation transfer preview requested")
        fingerprint, components = device_identity_for_request()
        answer = post_json(
            ACTIVATION_SERVER_ENDPOINT,
            "/api/v1/activation-transfer/preview",
            {
                "activation_code": clean_code,
                "fingerprint": fingerprint,
                "components": components,
            },
            runtime_root() / ACTIVATION_SERVER_PIN_FILE,
        )
        status = _with_server_device(verify_license(root), answer)
        return ActivationTransferPreview(
            True, str(answer.get("code", "TRANSFER_PREVIEW")),
            str(answer.get("message", "授权码可激活。")),
            transfer_token=str(answer.get("transfer_token", "")),
            bound_elsewhere=bool(answer.get("bound_elsewhere", False)),
            bound_equipment_id=str(answer.get("bound_equipment_id", "")),
            expires_in_seconds=int(answer.get("expires_in_seconds", 0) or 0),
            device_status=status,
        )
    except PinnedTlsResponseError as exc:
        return ActivationTransferPreview(False, f"server:{exc.code}", exc.message)
    except DeviceNotReadyError:
        return ActivationTransferPreview(False, "device_not_ready", "设备未就绪，稍后重试")
    except (ValueError, KeyError, OSError, PinnedTlsError, json.JSONDecodeError) as exc:
        SERVER_LOG.warning("activation transfer preview failed category=%s", type(exc).__name__)
        return ActivationTransferPreview(False, "server", "激活验证失败")


def confirm_activation_transfer_via_server(token: str, root: Optional[Path] = None) -> LicenseStatus:
    """Consume a preview token and persist the newly-issued entitlement."""

    try:
        SERVER_LOG.info("activation transfer confirmation requested")
        fingerprint, components = device_identity_for_request()
        answer = post_json(
            ACTIVATION_SERVER_ENDPOINT,
            "/api/v1/activation-transfer/confirm",
            {
                "transfer_token": str(token),
                "fingerprint": fingerprint,
                "components": components,
            },
            runtime_root() / ACTIVATION_SERVER_PIN_FILE,
        )
        document = answer["license"]
    except PinnedTlsResponseError as exc:
        SERVER_LOG.warning("activation transfer rejected status=%s code=%s", exc.status, exc.code)
        return _with_server_device(LicenseStatus(False, f"server:{exc.code}", exc.message), exc.data)
    except DeviceNotReadyError:
        return LicenseStatus(False, "device_not_ready", "设备未就绪，稍后重试")
    except (ValueError, KeyError, OSError, PinnedTlsError, json.JSONDecodeError) as exc:
        SERVER_LOG.warning("activation transfer failed category=%s", type(exc).__name__)
        return LicenseStatus(False, "server", "激活验证失败")
    parsed = _parse_document(document, root)
    if not parsed.valid:
        return parsed
    persisted = _with_server_device(_persist_document(document, parsed, root), answer)
    if persisted.valid:
        _save_sealed_fingerprint(fingerprint, persisted.license_id, root)
    return persisted


def validate_via_server(
    root: Optional[Path] = None, *, force_cim_refresh: bool = False,
) -> LicenseStatus:
    """Revalidate the saved entitlement through the pinned activation server.

    This is deliberately separate from local signature verification.  A
    signed document proves authenticity, while the online heartbeat proves the
    activation service is currently reachable and has not revoked the bound
    license.

    The credential is NOT dropped before the request.  Clearing it up front
    meant that for the whole round trip a licensed device with passes left had
    no credential at all, and a lie window landing in that gap produced
    "测谎窗口出现时本机没有测谎密钥" even though the entitlement was fine.  It is
    dropped on the outcome instead: any failed heartbeat clears it, and so does
    a verdict that the license is gone or the passes are used up.
    """

    status = _heartbeat_via_server(root, force_cim_refresh=force_cim_refresh)
    if not status.valid and status.code != "device_not_ready":
        # A failed heartbeat clears the credential, and so does a verdict about
        # the entitlement: revoked, expired, or no lie passes left.  A
        # *successful* heartbeat never clears it - the reply either refreshes
        # the credential or leaves the held one in place.
        clear_server_secret()
        AUTO_LIE_LOG.warning(
            "auto-lie credential dropped: heartbeat outcome %s", status.code
        )
    return status


def set_device_memo_via_server(memo: str, root: Optional[Path] = None) -> LicenseStatus:
    """Save the locally chosen equipment note through the pinned service."""

    local = verify_license(root)
    if not local.valid or not local.license_id:
        return LicenseStatus(False, "server", "激活验证失败")
    try:
        SERVER_LOG.info("device memo update requested")
        answer = post_json(
            ACTIVATION_SERVER_ENDPOINT,
            "/api/v1/device-memo",
            {
                "fingerprint": machine_fingerprint_hash(),
                "license_id": local.license_id,
                "memo": memo,
            },
            runtime_root() / ACTIVATION_SERVER_PIN_FILE,
        )
        return _with_server_device(local, answer)
    except PinnedTlsResponseError as exc:
        SERVER_LOG.warning("device memo update rejected status=%s code=%s", exc.status, exc.code)
        return _with_server_device(
            replace(local, valid=False, code=f"server:{exc.code}", message=exc.message),
            exc.data,
        )
    except DeviceNotReadyError:
        return LicenseStatus(False, "device_not_ready", "设备未就绪，稍后重试")
    except (ValueError, KeyError, OSError, PinnedTlsError, json.JSONDecodeError) as exc:
        SERVER_LOG.warning("device memo update failed category=%s", type(exc).__name__)
        return replace(local, code="server", message="激活验证失败")


def unbind_device_via_server(
    activation_code: str, root: Optional[Path] = None,
) -> LicenseStatus:
    """Request a customer-authorized device unbind through pinned TLS.

    Unlike a heartbeat this deliberately does not require a currently valid
    local license document: an expired count-mode code may still be moved to a
    replacement device.  The raw code authenticates the request and is sent
    only over the pinned connection; it is never written to the log.
    """

    code = "".join(str(activation_code or "").upper().split())
    if not code:
        return LicenseStatus(False, "unbind_missing_code", "本机没有可用的授权码，无法解绑。")
    try:
        SERVER_LOG.info("device self-unbind requested")
        fingerprint, components = device_identity_for_request()
        answer = post_json(
            ACTIVATION_SERVER_ENDPOINT,
            "/api/v1/device-unbind",
            {
                "activation_code": code,
                "fingerprint": fingerprint,
                "components": components,
            },
            runtime_root() / ACTIVATION_SERVER_PIN_FILE,
        )
        if answer.get("ok") is not True:
            return LicenseStatus(False, str(answer.get("code", "server")), str(
                answer.get("message", "解绑失败，请稍后重试。")
            ))
        return LicenseStatus(True, "unbound", str(
            answer.get("message", "解绑成功，授权码已复制。请在新设备激活授权。")
        ))
    except PinnedTlsResponseError as exc:
        SERVER_LOG.warning(
            "device self-unbind rejected status=%s code=%s", exc.status, exc.code
        )
        return LicenseStatus(False, f"server:{exc.code}", exc.message)
    except DeviceNotReadyError:
        return LicenseStatus(False, "device_not_ready", "设备未就绪，稍后重试")
    except (ValueError, KeyError, OSError, PinnedTlsError, json.JSONDecodeError) as exc:
        SERVER_LOG.warning("device self-unbind failed category=%s", type(exc).__name__)
        return LicenseStatus(False, "server", "解绑请求失败，请检查网络后重试。")


def _heartbeat_via_server(
    root: Optional[Path] = None, *, force_cim_refresh: bool = False,
) -> LicenseStatus:
    """The heartbeat request itself; the public wrapper applies the verdict rule."""

    local = verify_license(root)
    if not local.valid:
        return local
    if not local.license_id:
        return LicenseStatus(False, "server", "授权文件缺少在线验证信息。")
    try:
        SERVER_LOG.info("license heartbeat requested")
        fingerprint, components = device_identity_for_request(
            force_refresh=force_cim_refresh
        )
        answer = post_json(
            ACTIVATION_SERVER_ENDPOINT,
            "/api/v1/validate",
            {
                "fingerprint": fingerprint,
                "license_id": local.license_id,
                "components": components,
            },
            runtime_root() / ACTIVATION_SERVER_PIN_FILE,
        )
        document = answer["license"]
    except PinnedTlsResponseError as exc:
        SERVER_LOG.warning(
            "license heartbeat rejected status=%s code=%s", exc.status, exc.code
        )
        # An expired/revoked heartbeat is still an authenticated server
        # response about this device, so retain its equipment identity for
        # the header instead of rendering it as unknown.
        return _with_server_device(
            LicenseStatus(False, f"server:{exc.code}", exc.message),
            exc.data,
        )
    except DeviceNotReadyError:
        SERVER_LOG.info("license heartbeat postponed: device fingerprint is not ready")
        return LicenseStatus(False, "device_not_ready", "设备未就绪，稍后重试")
    except (ValueError, KeyError, OSError, PinnedTlsError, json.JSONDecodeError) as exc:
        SERVER_LOG.warning("license heartbeat failed category=%s", type(exc).__name__)
        return LicenseStatus(False, "server", "激活验证失败")
    status = _parse_document(document, root)
    if not status.valid:
        SERVER_LOG.warning("license heartbeat response rejected category=%s", status.code)
        return status
    persisted = _with_server_device(_persist_document(document, status, root), answer)
    # The heartbeat only announces the version; the notes and the link are
    # fetched by ``update_core.fetch_release_info`` when a client cares.  Keep
    # the raw dict so a newer server can add fields without another client
    # change, but never let it influence the entitlement verdict.
    release = answer.get("release") if isinstance(answer, dict) else None
    if isinstance(release, dict) and release.get("version"):
        persisted = replace(persisted, server_release=release)
    if persisted.valid:
        _save_sealed_fingerprint(fingerprint, persisted.license_id, root)
        SERVER_LOG.info("license heartbeat accepted license_id=%s", persisted.license_id)
    return persisted


def report_lie_event_via_server(
    event_id: str, outcome: str, occurred_at: str,
    root: Optional[Path] = None,
) -> LicenseStatus:
    """Send completed-event accounting after the live auto-lie pass is over.

    The caller deliberately schedules this later.  This function never takes
    part in the live WebSocket/cursor path.
    """

    local = verify_license(root)
    if not local.valid or not local.license_id:
        return local
    try:
        AUTO_LIE_LOG.info("lie accounting requested event=%s", str(event_id)[:8])
        answer = post_json(
            ACTIVATION_SERVER_ENDPOINT,
            "/api/v1/lie-events",
            {
                "fingerprint": machine_fingerprint_hash(),
                "license_id": local.license_id,
                "event_id": str(event_id),
                "outcome": str(outcome),
                "occurred_at": str(occurred_at),
            },
            runtime_root() / ACTIVATION_SERVER_PIN_FILE,
            # The accounting request is part of automatic lie handling, so its
            # connection trace stays with the rest of that feature.
            logger=AUTO_LIE_LOG,
        )
        status = _with_server_device(local, answer)
        AUTO_LIE_LOG.info("lie accounting accepted event=%s", str(event_id)[:8])
        return status
    except PinnedTlsResponseError as exc:
        status = _with_server_device(
            local, getattr(exc, "data", {}),
        )
        if exc.code in {"AUTO_LIE_QUOTA_BANNED", "FINGERPRINT_BANNED"}:
            return replace(
                status, valid=False, code="server:auto_lie_banned",
                message="自动过测谎已被服务器停用。", auto_lie_allowed=False,
            )
        return replace(status, code=f"server:{exc.code}", message=exc.message)
    except DeviceNotReadyError:
        return LicenseStatus(False, "device_not_ready", "设备未就绪，稍后重试")
    except (ValueError, OSError, PinnedTlsError, json.JSONDecodeError) as exc:
        AUTO_LIE_LOG.warning("lie accounting failed category=%s", type(exc).__name__)
        return replace(local, code="server", message="激活验证失败")


def verify_document(document: Any, root: Optional[Path] = None) -> LicenseStatus:
    """Check a signed entitlement document without persisting anything.

    Every desktop activation path persists only what ``_parse_document`` has
    already accepted.  The installer writes the same ``auth.json`` directly from
    a server reply, so it needs the same gate: an unverifiable signature, an
    expired payload, or a document bound to another machine must never be
    written and then presented to the customer as a licensed installation.
    """

    return _parse_document(document, root)


def remember_boot_identity(
    fingerprint: str, license_id: str, root: Optional[Path] = None,
) -> bool:
    """Seal this PC's server-accepted fingerprint for the desktop's boot bridge.

    ``auth.json`` alone proves the entitlement but says nothing about *which*
    equipment it belongs to; the DPAPI-sealed ``cim_identity.dat`` record does,
    and it is what lets an already-authorized client start while CIM/WMI is
    still waking up after a reboot (``_machine_binding_status`` and
    ``_document_fingerprint_status`` both fall back to it).  Activation normally
    writes it; the installer activates without ever running the desktop, so
    without this the first start after an install has no bridge and stays
    session-locked until a live heartbeat succeeds.

    Returns ``True`` only when the written record is readable again by this
    Windows profile, so a caller can report a real seal rather than an attempt.
    """

    _save_sealed_fingerprint(fingerprint, license_id, root)
    return bool(fingerprint) and has_sealed_fingerprint(license_id, root)


__all__ = [
    "ACTIVATION_PREFIX", "LICENSE_FORMAT",
    "DEVICE_REQUEST_PREFIX", "DeviceNotReadyError", "LicenseStatus",
    "ActivationTransferPreview", "activate", "activation_code",
    "activate_via_server", "preview_activation_transfer_via_server", "confirm_activation_transfer_via_server", "device_request_code", "document_from_activation", "machine_binding_from_request",
    "device_identity_for_request", "machine_fingerprint_components", "machine_fingerprint_hash",
    "has_sealed_fingerprint", "remember_boot_identity", "verify_document",
    "license_path", "public_key_path", "revoke_license", "runtime_root", "verify_license",
    "report_lie_event_via_server", "set_device_memo_via_server", "unbind_device_via_server", "validate_via_server",
]
