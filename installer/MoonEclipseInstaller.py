"""Standalone MoonEclipse installer and updater.

The release archive is published to a netdisk share; this window downloads that
one archive and unpacks the whole of it into the selected folder, keeping the
user's ``助手配置``.  There is no activation server and no install manifest in the
flow: the share link is the only source, and it carries every file the install
needs.

It is intentionally not imported by the running game helper.  The install root is
remembered in a small LocalAppData pointer, never in the Windows Registry.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from licensing import (  # noqa: E402
    ACTIVATION_SERVER_ENDPOINT,
    activation_code,
    device_identity_for_request,
    machine_fingerprint_hash,
)
from pinned_tls import PinnedTlsError, PinnedTlsResponseError, post_json  # noqa: E402


HERE = Path(__file__).resolve().parent
SOURCE_ROOT = HERE.parent
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

# The activation-server pin lives beside the source files or the executable.  In
# a standalone installer Nuitka places it beside the executable; nothing is read
# from the Windows Registry.
_exe_root = Path(sys.argv[0]).resolve().parent
PIN_ROOT = next(
    (candidate for candidate in (HERE, _exe_root, SOURCE_ROOT)
     if (candidate / "activation_server_pin.json").is_file()),
    SOURCE_ROOT,
)
# Where an operator can drop installer_mirrors.json: beside the executable
# first, then the source tree.
CONFIG_ROOTS = (_exe_root, HERE)

# Share parsing and the shared update transport.  The download path lives in
# the application's ``update_core`` module so the installer and the desktop's
# update icon cannot drift apart; this file only owns the window and the install.
try:  # noqa: WPS229 - the installer must start even without the helper
    from update_core import (
        ReleaseInfo, UpdateCoreError, archive_version, download_package,
        parse_release,
    )
except Exception:  # pragma: no cover - reported when a download is attempted
    ReleaseInfo = None  # type: ignore[assignment]
    UpdateCoreError = RuntimeError  # type: ignore[assignment,misc]
    archive_version = None  # type: ignore[assignment]
    download_package = None  # type: ignore[assignment]
    parse_release = None  # type: ignore[assignment]


POINTER_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MoonEclipseInstaller"
POINTER_FILE = POINTER_DIR / "last-install.json"
CONFIG_DIRECTORY = "助手配置"
# A protected package carries this executable and its own Python runtime, so it
# needs neither a .venv nor 安装.bat.
PROTECTED_EXECUTABLE = "MoonEclipse.exe"
MAX_DOWNLOAD_WORKERS = 8
# Share links the operator can drop beside the installer, so a manual install
# works even when the activation server cannot be reached:
# {"shares": ["<分享链接>"]}.  There is no built-in link: the newest one is served
# by the activation server (see ``_activate``).
MIRRORS_FILE = "installer_mirrors.json"
# Folder created on the Desktop for a first install.
DEFAULT_FOLDER_NAME = "MoonEclipse"
# The bar is the download and nothing else: it fills from real socket bytes, and
# the phases after it (extraction, verification, the move) report their real
# counts in the status text without moving it.  A bar that slid on during
# verification claimed progress that no byte stream was making.
PROGRESS_UNITS = 1000
PROGRESS_DOWNLOAD_SHARE = 1.0
PROGRESS_LABELS = {"解压": "正在解压安装包", "校验": "正在校验文件", "安装": "正在安装文件"}


def _desktop_candidates() -> list[Path]:
    """Every place the user's Desktop plausibly lives, most authoritative first."""

    home = Path.home()
    found: list[Path] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            try:
                found.append(Path(os.path.expandvars(value.strip())))
            except (OSError, ValueError):
                pass

    # 1) The value the shell itself uses: %USERPROFILE%\Desktop normally,
    #    %OneDrive%\Desktop when redirected, 桌面 on a localized system.
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            add(winreg.QueryValueEx(key, "Desktop")[0])
    except (ImportError, OSError, ValueError):
        pass

    # 2) The known-folder API, for a machine whose registry value is missing.
    try:
        import ctypes
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
        folder_id = _GUID(
            0xB4BFCC3A, 0xDB2C, 0x424C,
            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41),
        )
        buffer = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        if shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(buffer)
        ) == 0:
            resolved = str(buffer.value or "")
            ctypes.windll.ole32.CoTaskMemFree(buffer)
            add(resolved)
    except Exception:  # noqa: BLE001 - a failed lookup just falls through
        pass

    # 3) Conventional names, including the localized folder of a redirected or
    #    renamed Desktop.
    add(str(home / "Desktop"))
    add(str(home / "桌面"))
    try:
        for child in home.iterdir():
            if child.is_dir() and child.name.strip().lower() in {"desktop", "桌面"}:
                add(str(child))
    except OSError:
        pass
    return found


def _desktop_directory() -> Path:
    """Return the user's real Desktop folder.

    ``Path.home() / "Desktop"`` is wrong often enough to matter: OneDrive
    redirects the folder to ``.../OneDrive/Desktop`` and a localized Windows
    names it 桌面.  Returning the profile root when both lookups failed used to
    install the whole app into ``C:\\Users\\<user>\\MoonEclipse``.
    """

    for candidate in _desktop_candidates():
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    # Nothing resolved: use the conventional path; the install creates it.
    return Path.home() / "Desktop"


def _configure_logging() -> Path:
    """Log to a file beside the install pointer and keep it small."""

    directory = POINTER_FILE.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return POINTER_FILE
    target = directory / "installer.log"
    try:
        if target.is_file() and target.stat().st_size > 2 * 1024 * 1024:
            target.replace(directory / "installer.old.log")
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(target, encoding="utf-8")],
        force=True,
    )
    logging.info("installer: start, executable %s", sys.argv[0])
    return target


def _pointer_root() -> Path | None:
    try:
        payload = json.loads(POINTER_FILE.read_text(encoding="utf-8"))
        root = Path(str(payload.get("install_root", ""))).resolve()
        if (root / PROTECTED_EXECUTABLE).is_file() or (root / "VERSION").is_file():
            return root
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def _save_pointer(root: Path) -> None:
    POINTER_DIR.mkdir(parents=True, exist_ok=True)
    temporary = POINTER_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"install_root": str(root), "updated_at": int(time.time())}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(POINTER_FILE)


def _safe_path(value: Any) -> Path:
    relative = PurePosixPath(str(value or "").replace("\\", "/"))
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe manifest path")
    return Path(*relative.parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(512 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _share_sources(manifest: dict[str, Any], release: Any = None) -> list[str]:
    """Share links the installer may fetch the whole package from.

    The newest 更新链接 record comes from the activation server, so a release no
    longer needs a new installer build.  Honoured, most authoritative first:

    1. the served ``release`` record (``link``, then ``extra_links``);
    2. ``manifest["mirrors"]`` / ``manifest["bundle"]["mirrors"]`` - a legacy
       server-side publish, still accepted when the operator uses one;
    3. ``installer_mirrors.json`` - ``{"shares": [...]}`` dropped next to the
       installer (or the app root), a manual escape hatch when the server is
       unreachable.

    There is deliberately no built-in link: a hardcoded address is one release
    stale the moment it ships.  Only ``http(s)`` URLs are accepted.
    """

    found: list[str] = []

    def add(values: Any) -> None:
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple)):
            return
        for value in values:
            if isinstance(value, str):
                text = value.strip()
                if text.startswith("http://") or text.startswith("https://"):
                    if text not in found:
                        found.append(text)

    released = getattr(release, "links", None)
    if callable(released):
        add(list(released()))
    else:
        add(release)
    add(manifest.get("mirrors"))
    bundle = manifest.get("bundle")
    if isinstance(bundle, dict):
        add(bundle.get("mirrors"))
    for folder in CONFIG_ROOTS:
        candidate = folder / MIRRORS_FILE
        try:
            if not candidate.is_file():
                continue
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            add(data.get("shares"))
    if found:
        # Host names only: a share URL is a capability, not a log line.
        logging.info(
            "installer: %d share source(s) available: %s",
            len(found), ", ".join(_link_host(item) for item in found),
        )
    return found


def _link_host(link: str) -> str:
    try:
        from urllib.parse import urlsplit

        return urlsplit(str(link)).hostname or ""
    except ValueError:
        return ""


def _archive_version(archive: Path) -> str:
    """The ``VERSION`` member of a release archive, or "" when absent."""

    return archive_version(archive)


def _profile_search_dirs(root: Path) -> list[Path]:
    """Every folder a licensed profile may live in, the correct one first.

    The CORRECT place is the selected root's ``助手配置``: the launcher sets
    ``MOONECLIPSE_CONFIG_DIR=<selected>\\助手配置`` and the app reads its
    entitlement from there.  Older installations are searched too, because the
    operator's activated copies were created by both of them:

    * ``<selected>\\app\\助手配置`` - packages up to v1.2.284 kept everything in
      ``app\\``;
    * ``<parent of selected>\\助手配置`` - a ZIP-extracted package (e.g.
      ``Desktop\\MoonEclipse-release-1.2.274(2)``) has no ``助手配置`` of its own,
      so the app's ``package_root.parent / 助手配置`` fallback made it read and
      activate into ``Desktop\\助手配置``.

    A profile found in either legacy place is copied into the selected root so
    the installed app - and the installer's own window - see one location.
    """

    dirs: list[Path] = [root / "助手配置"]
    candidates = [root / "app" / "助手配置", root.parent / "助手配置"]
    for directory in candidates:
        if directory in dirs:
            continue
        if directory.is_dir():
            dirs.append(directory)
    return dirs


def _profile_target(root: Path) -> Path:
    """Where the app expects its licensed profile document: the selected root."""

    return _profile_search_dirs(root)[0] / "auth.json"


def _relocate_profile_document(source: Path, root: Path) -> None:
    """Copy a profile document into the selected root's ``助手配置``.

    The profile keeps working whichever legacy folder it was found in, but the
    installed app must find it in the selected root.  An existing target
    document is never overwritten.

    The old copy is removed only when it sits inside the selected folder's own
    ``app\\`` tree (that tree is deleted by the install anyway).  A copy found
    one level up (``<parent>\\助手配置``) is shared with the operator's other
    installations on the Desktop and is therefore left alone.
    """

    target = _profile_target(root)
    try:
        if target.resolve() == source.resolve():
            return
    except OSError:
        return
    if target.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    except OSError:
        logging.warning(
            "installer: could not copy %s to %s", source, target, exc_info=True
        )
        return
    if not target.is_file():
        logging.warning(
            "installer: the copied profile is missing at %s; keeping %s",
            target, source,
        )
        return
    try:
        inside_legacy_tree = (root / "app") in source.resolve().parents
    except OSError:
        inside_legacy_tree = False
    if inside_legacy_tree:
        source.unlink(missing_ok=True)
    logging.info(
        "installer: copied the activation profile %s -> %s", source, target
    )


def _legacy_app_dir(root: Path) -> Path | None:
    """The older layout's app folder (``<selected>\\app``) when it holds an install.

    Packages up to v1.2.284 kept every file under ``<selected>\\app``.  Selecting
    such a folder must be recognised as an EXISTING installation, not a new
    location.
    """

    app_dir = root / "app"
    if not app_dir.is_dir():
        return None
    if (
        (app_dir / CONFIG_DIRECTORY).is_dir()
        or (app_dir / PROTECTED_EXECUTABLE).is_file()
        or (app_dir / "VERSION").is_file()
    ):
        return app_dir
    return None


def _migrate_legacy_install(root: Path) -> None:
    """Adopt an old installation's profile into the selected root.

    The selected root is the folder the operator picks now, and the app reads its
    configuration and entitlement from ``<selected>\\助手配置``
    (``MOONECLIPSE_CONFIG_DIR``).  Older installations kept it elsewhere, so
    every file of a legacy ``助手配置`` (``auth.json``, ``user_config.json``,
    saved maps) is copied into the root's ``助手配置``; an existing file is never
    overwritten.  The legacy folder may be ``<selected>\\app\\助手配置`` or, for
    a ZIP-extracted package like ``Desktop\\MoonEclipse-release-1.2.274(2)``, the
    shared ``<parent>\\助手配置`` the old app fell back to.

    Nothing is shared away: a copy found one level up is never deleted (the
    operator's other installations use it), only an in-folder ``app\\`` copy is
    left for the install step's cleanup.
    """

    root.mkdir(parents=True, exist_ok=True)
    target = root / CONFIG_DIRECTORY
    for source in [root / "app" / CONFIG_DIRECTORY, root.parent / CONFIG_DIRECTORY]:
        if not source.is_dir():
            continue
        if source.resolve() == target.resolve():
            continue
        copied = 0
        for item in source.rglob("*"):
            if not item.is_file():
                continue
            destination = target / item.relative_to(source)
            if destination.exists():
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)
                copied += 1
            except OSError:
                logging.warning(
                    "installer: could not migrate %s to %s", item, destination,
                    exc_info=True,
                )
        if copied:
            logging.info(
                "installer: migrated %d configuration file(s) %s -> %s",
                copied, source, target,
            )
    # The activation is NOT in 助手配置: a pre-1.2.285 layout keeps auth.json,
    # cim_identity.dat and license_settings.json at the app root, and this
    # installer deletes that app\ tree below.  Adopt them first, or an update
    # silently ends in "未授权" and the client has to activate again.
    _adopt_legacy_root_authorization(root)


# Authorization documents a pre-root-layout install keeps at the app root.
LEGACY_ROOT_AUTHORIZATION = (
    "auth.json", "cim_identity.dat", "license_settings.json", "license.json",
)


def _adopt_legacy_root_authorization(root: Path) -> list[str]:
    """Copy authorization documents out of a legacy ``app\\`` root.

    Returns the names that are now present at ``root`` (adopted or already there)
    so the cleanup can prove it is not about to destroy the activation.  An
    existing root document always wins: only a missing one is taken over.
    """

    legacy = root / "app"
    if not legacy.is_dir():
        return []
    adopted: list[str] = []
    for name in LEGACY_ROOT_AUTHORIZATION:
        source = legacy / name
        target = root / name
        if not source.is_file():
            continue
        if target.exists():
            adopted.append(name)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            adopted.append(name)
            logging.info(
                "installer: adopted %s from the legacy app folder before cleanup", name
            )
        except OSError:
            logging.warning(
                "installer: could not adopt %s from %s", name, legacy, exc_info=True
            )
    return adopted


def _legacy_config_missing(root: Path) -> list[str]:
    """Legacy user data that is still absent from the selected root.

    Covers the legacy ``助手配置`` documents AND the authorization files a
    pre-1.2.285 layout keeps at the ``app\\`` root: the caller deletes that tree, and
    destroying an un-adopted ``auth.json`` means the client has to activate again.
    """

    app_dir = _legacy_app_dir(root)
    if app_dir is None:
        return []
    missing: list[str] = []
    source = app_dir / CONFIG_DIRECTORY
    if source.is_dir():
        target = root / CONFIG_DIRECTORY
        missing.extend(
            str(item.relative_to(source))
            for item in source.rglob("*")
            if item.is_file() and not (target / item.relative_to(source)).exists()
        )
    for name in LEGACY_ROOT_AUTHORIZATION:
        legacy_file = app_dir / name
        if legacy_file.is_file() and not (root / name).exists():
            missing.append(name)
    return missing


def _previous_install_profile_code(root: Path) -> str:
    """Activation code already stored in this installation's local profile.

    An existing installation keeps its licensed ``auth.json`` (the signed
    entitlement document) in the selected root's ``助手配置``, so the installer
    can reproduce the activation code from it and update the folder without
    asking the operator again.  Only a NEW folder, which has no profile, needs
    the code typed in.

    Two folders are searched (``_profile_search_dirs``): the CORRECT
    ``<selected>\\助手配置`` and the older build's ``<selected>\\app\\助手配置``.

    The code itself comes from, in order:

    1. ``license_settings.json``'s ``activation_code`` - exactly what was typed;
    2. the stored document's ``_local.activation_code`` - the code the app kept
       from the successful activation (a ``MAL-…`` server code);
    3. only as a last resort a token derived from the signed document.

    Deriving it was the original idea and it is WRONG: the stored code is a
    ``MAL-…`` server code while ``licensing.activation_code`` builds an
    ``MA1-…`` token from the whole document, and the server answered
    "设备未授权" for that ("authed but get 设备未授权", 0:24 field report).  The
    document is still moved to the selected root so the installed app keeps its
    activation.
    """

    dirs = _profile_search_dirs(root)
    for directory in dirs:
        settings = directory / "license_settings.json"
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        stored = "".join(str(data.get("activation_code") or "").split())
        if stored:
            logging.info(
                "installer: using the stored activation code from %s", settings
            )
            return stored

    target = _profile_target(root)
    # The correct location first, then the old build's folder, then the bare
    # names some older runs wrote beside the executable.
    candidates: list[Path] = [target]
    for directory in dirs:
        for name in ("auth.json", "license.json"):
            candidates.append(directory / name)
    for base in (root, root / "app"):
        for name in ("auth.json", "license.json"):
            candidates.append(base / name)
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            key = candidate.resolve()
        except OSError:
            key = candidate
        if key in seen:
            continue
        seen.add(key)
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(document, dict) or "payload" not in document:
            continue
        _relocate_profile_document(candidate, root)
        local = document.get("_local")
        if isinstance(local, dict):
            stored = "".join(str(local.get("activation_code") or "").split())
            if stored:
                logging.info(
                    "installer: using the activation code the app stored in %s",
                    candidate,
                )
                return stored
        derived = activation_code(document)
        if derived:
            logging.warning(
                "installer: %s holds no activation code; falling back to a "
                "derived token the server may refuse", candidate,
            )
            return derived
    return ""


def _is_previous_install(root: Path) -> bool:
    """Whether ``root`` already holds an installation.

    There is no install marker file to look for: every update ships the complete
    package from the netdisk, so a folder is judged only by what it contains.  An
    installation created by unzipping a release ZIP - a folder like
    ``Desktop\\MoonEclipse-release-1.2.274(2)`` - is a perfectly normal activated
    installation.  Recognition therefore uses what an installation actually has:

    * the app executable or a ``VERSION`` file at the selected root;
    * the older layout under ``<selected>\\app`` (exe / config);
    * a real profile - ``auth.json`` or ``user_config.json`` - in any searched
      ``助手配置`` (selected root, ``app\\``, or the parent folder the old
      ZIP-extracted app fell back to).
    """

    if (root / PROTECTED_EXECUTABLE).is_file() or (root / "VERSION").is_file():
        return True
    if _legacy_app_dir(root) is not None:
        return True
    for directory in _profile_search_dirs(root):
        for name in ("auth.json", "license.json", "user_config.json"):
            if (directory / name).is_file():
                return True
    return False


def _activate(code: str) -> tuple[str, Any]:
    """Validate this copy against the activation server; return its release.

    Returns ``(session_token, release)``.  The token is only meaningful when the
    operator still publishes a server-side manifest, so it may be empty: the
    payload is fetched from the 更新链接 record's netdisk share, which is what the
    reply now carries.  ``release`` is a ``ReleaseInfo`` (or ``None`` when the
    server has published nothing).
    """

    # Send the composite AND the per-signal digests behind it: a PC that was
    # restored to an earlier disk state (rolled-back MachineGuid) or is still
    # bringing WMI online reports a different composite for the SAME hardware,
    # and the install must not be refused as "另一台设备" before the app that
    # would re-anchor it is even installed.
    fingerprint, components = device_identity_for_request()
    payload = {
        "activation_code": "".join(code.upper().split()),
        "fingerprint": fingerprint,
        "components": components,
    }
    try:
        answer = post_json(
            ACTIVATION_SERVER_ENDPOINT, "/api/v1/installer/session",
            payload, PIN_ROOT / "activation_server_pin.json",
        )
    except PinnedTlsResponseError as exc:
        raise RuntimeError(f"授权失败：{getattr(exc, 'message', str(exc))}") from exc
    except PinnedTlsError as exc:
        raise RuntimeError(f"授权失败：无法连接到授权服务器（{exc}）") from exc
    token = str((answer or {}).get("session_token") or "")
    release = parse_release((answer or {}).get("release")) if parse_release else None
    if release is None:
        manifest = (answer or {}).get("manifest")
        if isinstance(manifest, dict) and manifest.get("version"):
            # A legacy publish still names the version; the link then comes from
            # the manifest's mirrors through ``_share_sources``.
            release = ReleaseInfo(version=str(manifest["version"]))
    if token or release is not None:
        logging.info(
            "installer: activation ok, session=%s release=%s",
            "yes" if token else "no",
            getattr(release, "version", "") or "none",
        )
        return token, release
    raise RuntimeError("授权失败：服务器未返回下载信息。")


# The signed entitlement the desktop verifies, and the desktop's own record of
# the code it was activated with (both are the client's own file names).
ENTITLEMENT_FILE = "auth.json"
ENTITLEMENT_LOCAL_KEY = "_local"
LICENSE_SETTINGS_NAME = "license_settings.json"


def _acquire_entitlement(code: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind this PC to the code and return its signed entitlement document.

    ``/api/v1/installer/session`` only proves the code so the download may start;
    it binds nothing and hands out no entitlement, which is why a freshly
    installed copy used to open unlicensed and ask for the same code again.  This
    second, mutating call performs the activation itself - the installer is the
    customer's first contact with the product, so it is also the right place to
    bind.

    Returns ``(document, reply)``; the reply carries the equipment id and expiry
    for the window to show.
    """

    fingerprint, components = device_identity_for_request()
    payload = {
        "activation_code": "".join(code.upper().split()),
        "fingerprint": fingerprint,
        "components": components,
    }
    try:
        answer = post_json(
            ACTIVATION_SERVER_ENDPOINT, "/api/v1/activate",
            payload, PIN_ROOT / "activation_server_pin.json",
        )
    except PinnedTlsResponseError as exc:
        raise RuntimeError(getattr(exc, "message", str(exc))) from exc
    except PinnedTlsError as exc:
        raise RuntimeError(f"无法连接到授权服务器（{exc}）") from exc
    document = (answer or {}).get("license")
    if not isinstance(document, dict) or "payload" not in document:
        raise RuntimeError("授权服务器未返回授权文件。")
    return document, dict(answer or {})


def _write_entitlement(root: Path, document: dict[str, Any]) -> Path:
    """Write the signed entitlement where the installed app reads it.

    Mirrors ``licensing._persist_document`` exactly: ``<root>/auth.json``, the
    desktop's own ``_local`` display state preserved when the file already
    exists, and an atomic replace so a half-written document is never readable.
    """

    target = Path(root) / ENTITLEMENT_FILE
    output = dict(document)
    local: dict[str, Any] = {}
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = None
    if isinstance(existing, dict):
        candidate = existing.get(ENTITLEMENT_LOCAL_KEY, {})
        if isinstance(candidate, dict) and candidate:
            local = candidate
        elif "payload" not in existing:
            # Short-lived flat layout: keep the display fields as ``_local``.
            local = {
                key: value for key, value in existing.items()
                if key not in {"payload", "signature", ENTITLEMENT_LOCAL_KEY}
            }
    if local:
        output[ENTITLEMENT_LOCAL_KEY] = local
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, delete=False,
    ) as handle:
        json.dump(output, handle, ensure_ascii=True, sort_keys=True, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, target)
    return target


def _write_license_settings(
    root: Path, code: str, reply: dict[str, Any],
) -> Path | None:
    """Record the submitted code the way the desktop does.

    A later run of this installer updates an existing folder without asking for
    the code again (``_previous_install_profile_code`` reads exactly this file),
    which only works while the code is stored beside the config.
    """

    target = _profile_search_dirs(root)[0] / LICENSE_SETTINGS_NAME
    data: dict[str, Any] = {}
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    data["activation_code"] = "".join(str(code).upper().split())
    data["last_validation_valid"] = True
    data["last_validation_code"] = "ACTIVATED"
    data["last_validation_message"] = "安装程序已自动授权。"
    data["last_validation_at"] = datetime.now(timezone.utc).isoformat()
    equipment_id = str(reply.get("device", {}).get("equipment_id", "") if isinstance(
        reply.get("device"), dict) else "").strip()
    if len(equipment_id) == 8:
        data["equipment_id"] = equipment_id
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, delete=False,
        ) as handle:
            json.dump(data, handle, ensure_ascii=True, sort_keys=True, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, target)
    except OSError:
        logging.warning("installer: could not store %s", target, exc_info=True)
        return None
    return target


def authorize_installation(root: Path, code: str) -> str:
    """Make a finished installation licensed; return the note for the window.

    Never raises: the customer already has a complete install at this point, and
    a hiccup here must not turn it into a failed installation.  The returned text
    says either "ready" or exactly what to do next.
    """

    try:
        document, reply = _acquire_entitlement(code)
    except Exception as exc:  # noqa: BLE001 - reported to the window as a note
        logging.exception("installer: automatic authorization failed")
        return f"自动授权未完成（{exc}）。请在助手窗口点「激活授权」输入同一授权码。"
    try:
        target = _write_entitlement(root, document)
    except OSError as exc:
        logging.exception("installer: could not write %s", ENTITLEMENT_FILE)
        return f"授权文件写入失败（{exc}）。请在助手窗口点「激活授权」输入同一授权码。"
    _write_license_settings(root, code, reply)
    equipment_id = ""
    device = reply.get("device")
    if isinstance(device, dict):
        equipment_id = str(device.get("equipment_id") or "")
    logging.info(
        "installer: entitlement written to %s (equipment %s)",
        target, equipment_id or "unknown",
    )
    if equipment_id:
        return f"本机已自动授权（设备码 {equipment_id}），启动后无需再次输入授权码。"
    return "本机已自动授权，启动后无需再次输入授权码。"


def _lens_outline(
    centre: float, radius: float, moon_x: float, steps: int = 36,
) -> list[float]:
    """Outline of two equal circles' overlap, as flat xy pairs for a canvas.

    Empty when the circles do not overlap (the moon has not reached the sun), the
    full circle when they coincide.  This is the only black ever drawn, so the
    moon cannot appear over the empty background.
    """

    distance = abs(moon_x - centre)
    if distance >= 2.0 * radius:
        return []
    if distance == 0.0:
        return [
            coordinate
            for index in range(steps * 2)
            for coordinate in (
                centre + radius * math.cos(math.pi * index / steps),
                centre + radius * math.sin(math.pi * index / steps),
            )
        ]
    spread = math.acos(distance / (2.0 * radius))
    to_moon = math.pi if moon_x < centre else 0.0
    points: list[float] = []
    # Arc of the sun facing the moon, then the matching arc of the moon.
    for index in range(steps + 1):
        angle = to_moon - spread + (2.0 * spread) * index / steps
        points.extend((
            centre + radius * math.cos(angle),
            centre + radius * math.sin(angle),
        ))
    for index in range(steps + 1):
        angle = to_moon + math.pi - spread + (2.0 * spread) * index / steps
        points.extend((
            moon_x + radius * math.cos(angle),
            centre + radius * math.sin(angle),
        ))
    return points


def _version_from_name(name: str) -> str:
    """``MoonEclipse-release-1.2.284.zip`` -> ``1.2.284``."""

    import re

    found = re.search(r"(\d+\.\d+\.\d+)", str(name or ""))
    return found.group(1) if found else ""


def _version_tuple(value: str) -> tuple[int, ...]:
    """``"1.2.284"`` -> ``(1, 2, 284)`` for the downgrade guard."""

    parts = [int(chunk) for chunk in str(value or "").replace("-", ".").split(".") if chunk.isdigit()]
    return tuple(parts[:3]) or (0,)


def _fetch_package(
    update_dir: Path,
    target_root: Path,
    release: Any = None,
    *,
    on_bytes: Any,
    on_total: Any = None,
    on_stage: Any = None,
    on_files: Any = None,
) -> tuple[list[tuple[dict[str, Any], Path]], str, bool]:
    """Download the release archive into ``update_dir`` and unpack it into the root.

    Returns ``(entries, version, skipped)``.  The archive is unpacked straight
    over the selected folder - duplicate files are replaced, nothing is nested -
    and the entries describe what was written (path, size, SHA-256).  ``release``
    is the record the activation server served; its link is the only automatic
    source of the package.
    """

    if download_package is None:
        raise RuntimeError("安装包下载模块不可用，无法下载。")
    if release is None:
        release = ReleaseInfo(version="")
    shares = _share_sources({}, release)
    if not shares:
        raise RuntimeError(
            "无法从服务器获取最新下载地址；"
            "请确认网络与授权状态，或在 installer_mirrors.json 中手动配置。"
        )
    # The shared transport owns the mirrors loop, the progress callbacks and the
    # version check; this window only reports them.
    location = ReleaseInfo(
        version=getattr(release, "version", "") or "",
        info=getattr(release, "info", "") or "",
        link=shares[0],
        extra_links=tuple(shares[1:]),
    )

    def stage(text_value: str) -> None:
        if on_stage is not None:
            on_stage(text_value)

    def files(phase: str, done: int, total: int) -> None:
        if on_files is not None:
            on_files(phase, done, total)

    try:
        archive = download_package(
            location, update_dir,
            on_bytes=on_bytes, on_total=on_total, on_stage=on_stage,
            filename="package.zip",
        )
    except UpdateCoreError as exc:
        raise RuntimeError(str(exc)) from exc

    version = _archive_version(archive)
    if not version:
        raise RuntimeError("安装包里没有 VERSION 文件，无法确认版本。")
    if location.version and _version_tuple(location.version) > _version_tuple(version):
        raise RuntimeError(
            f"下载到的安装包是 {version}，低于服务器公布的 {location.version}。"
        )
    stage("正在解压安装包…")
    _extract_bundle(
        archive, target_root,
        on_entry=lambda done, total: files("解压", done, total),
    )

    stage("正在校验文件…")
    with zipfile.ZipFile(archive) as bundle:
        members = [
            info.filename for info in bundle.infolist()
            if not info.is_dir()
            # User configuration is not managed by the installer: it is never
            # overwritten, never verified, never removed.
            and _safe_path(info.filename).parts[0] != CONFIG_DIRECTORY
        ]
    if not members:
        raise RuntimeError("安装包里没有文件。")
    counted = {"done": 0}
    counted_lock = threading.Lock()

    def describe(name: str) -> tuple[dict[str, Any], Path]:
        relative = _safe_path(name)
        target = target_root / relative
        item = {
            "path": relative.as_posix(),
            "size": target.stat().st_size,
            "sha256": _sha256(target),
        }
        with counted_lock:
            counted["done"] += 1
            current = counted["done"]
        if current % 32 == 0 or current == len(members):
            files("校验", current, len(members))
        return item, relative

    workers = max(1, min(MAX_DOWNLOAD_WORKERS, len(members)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        entries = list(pool.map(describe, members))
    archive.unlink(missing_ok=True)
    return entries, version, False


def _extract_bundle(
    archive: Path, target_root: Path, on_entry: Any = None,
    preserve: tuple[str, ...] = (CONFIG_DIRECTORY,),
) -> None:
    """Extract a version archive, refusing any path that escapes the root.

    ``preserve`` names top-level folders that are user data: their files are
    written only when missing, so shipping default configuration in the package
    cannot wipe a user's own.
    """

    root = target_root.resolve()
    with zipfile.ZipFile(archive) as bundle:
        members = [info for info in bundle.infolist() if not info.is_dir()]
        for index, info in enumerate(members, start=1):
            try:
                relative = _safe_path(info.filename)
            except ValueError:
                raise RuntimeError(f"安装包内容无效：{info.filename}") from None
            destination = (root / relative).resolve()
            if root not in destination.parents:
                raise RuntimeError(f"安装包内容无效：{info.filename}")
            if preserve and relative.parts and relative.parts[0] in preserve and destination.exists():
                if on_entry is not None and (index % 32 == 0 or index == len(members)):
                    on_entry(index, len(members))
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
            if on_entry is not None and (index % 32 == 0 or index == len(members)):
                on_entry(index, len(members))


def _installed_version(root: Path) -> str:
    """The version of the copy in ``root``, from the app's own VERSION file.

    The installer keeps no manifest any more: the netdisk package carries every
    file, so the only thing worth reading from an existing folder is its version
    (the downgrade guard).
    """

    try:
        return (root / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _start_first_install_setup(app_root: Path) -> bool:
    """Start the existing dependency setup only when this is a fresh install.

    Updates never recreate or replace ``.venv``.  A new installation keeps the
    familiar elevated setup window because it may need to install Python.

    A protected package ships the compiled executable and a bundled runtime, so
    it has no ``安装.bat`` and needs no ``.venv`` at all; requiring one there
    turned every fresh protected install into 安装包缺少首次安装程序。
    """

    if (app_root / PROTECTED_EXECUTABLE).is_file():
        return False
    if (app_root / ".venv" / "Scripts" / "pythonw.exe").is_file():
        return False
    setup = app_root / "安装.bat"
    if not setup.is_file():
        raise RuntimeError("安装包缺少首次安装程序。")
    subprocess.Popen(
        ["cmd.exe", "/c", str(setup)],
        cwd=str(app_root),
        shell=False,
    )
    return True


class InstallerWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("MoonEclipse 安装与更新")
        self.root.resizable(False, False)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.running = False
        self.progress_value = 0.0
        self._total_bytes = 1
        self._stage_text = "准备就绪"
        self._phase_started: float | None = None
        # An existing install stays where it is; a first install defaults to
        # Desktop\MoonEclipse.
        default_root = _pointer_root() or (_desktop_directory() / DEFAULT_FOLDER_NAME)

        outer = ttk.Frame(self.root, padding=18)
        outer.grid(sticky="nsew")
        ttk.Label(outer, text="MoonEclipse", font=("Microsoft YaHei UI", 18, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(outer, text="安装位置").grid(row=2, column=0, sticky="w", pady=(16, 0))
        self.install_root = tk.StringVar(value=str(default_root))
        ttk.Entry(outer, textvariable=self.install_root, width=42).grid(row=2, column=1, sticky="ew", padx=(8, 6), pady=(16, 0))
        ttk.Button(outer, text="选择…", command=self._choose_root).grid(row=2, column=2, pady=(16, 0))

        # #f0f0f0 matches the window face, so the glyph has no white square
        # behind it.  The canvas keeps focus off itself to avoid a border.
        self.eclipse = tk.Canvas(
            outer, width=52, height=52, highlightthickness=0,
            background="#f0f0f0", borderwidth=0,
        )
        self.eclipse.grid(row=3, column=0, pady=(18, 0), sticky="w")
        self.progress = ttk.Progressbar(outer, mode="determinate", length=340)
        self.progress.grid(row=3, column=1, columnspan=2, pady=(18, 0), sticky="ew")
        self.status = ttk.Label(outer, text="准备就绪")
        self.status.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.install = ttk.Button(outer, text="开始安装 / 更新", command=self._start)
        self.install.grid(row=5, column=2, sticky="e", pady=(16, 0))
        self._paint_eclipse(0.0)
        self.root.after(50, self._drain_events)

    def _ask_activation_code(self) -> str:
        """Ask for the activation code, for a NEW installation folder only.

        An installation that already holds its licensed profile never reaches
        this dialog: its code is taken from ``auth.json`` (see
        ``_previous_install_profile_code``), so updating an existing folder never
        asks the operator to retype it.
        """

        answer = {"code": ""}
        dialog = tk.Toplevel(self.root)
        dialog.title("首次安装需要授权码")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=16)
        frame.grid(sticky="nsew")
        ttk.Label(
            frame,
            text="这是一个新的安装位置。\n请输入授权码以完成首次安装：",
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        value = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=value, width=52)
        entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        entry.focus_set()

        def confirm(_event: object = None) -> None:
            answer["code"] = value.get().strip()
            dialog.destroy()

        def cancel() -> None:
            answer["code"] = ""
            dialog.destroy()

        ttk.Button(frame, text="取消", command=cancel).grid(
            row=2, column=0, sticky="w", pady=(14, 0)
        )
        ttk.Button(frame, text="确定", command=confirm).grid(
            row=2, column=1, sticky="e", pady=(14, 0)
        )
        entry.bind("<Return>", confirm)
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.grab_set()
        self.root.wait_window(dialog)
        return answer["code"]

    def _choose_root(self) -> None:
        chosen = filedialog.askdirectory(parent=self.root, title="选择 MoonEclipse 安装位置", initialdir=self.install_root.get() or str(Path.home()))
        if chosen:
            self.install_root.set(chosen)

    def _paint_eclipse(self, fraction: float) -> None:
        """The moon travels in from outside and covers the sun.

        Two wrong shapes to avoid: a disc that grows out of the sun's centre (an
        iris, not an eclipse) and a disc that keeps sliding after it has covered
        the sun (a transit).  The moon here is the sun's own size, starts fully
        outside the visible zone, and stops exactly on the sun: at 1.0 the visible
        result is a single black circle.
        """

        self.eclipse.delete("all")
        fraction = max(0.0, min(1.0, fraction))
        centre, radius = 26.0, 20.0
        self.eclipse.create_oval(
            centre - radius, centre - radius, centre + radius, centre + radius,
            fill="#f4d35e", outline="",
        )
        # The moon starts exactly touching the sun's left edge (nothing visible)
        # and ends exactly on it; only the overlap with the sun is painted.
        moon_x = centre - 2.0 * radius * (1.0 - fraction)
        outline = _lens_outline(centre, radius, moon_x)
        if outline:
            self.eclipse.create_polygon(
                outline, fill="#202b3c", outline="",
            )

    def _start(self) -> None:
        if self.running:
            return
        root = Path(self.install_root.get()).expanduser()
        # An old-layout installation (everything under app\) is adopted into the
        # selected root first: its marker makes the folder an existing install
        # and its 助手配置 (user_config.json, auth.json, saved maps) is copied to
        # the root before anything else touches the folder.
        try:
            _migrate_legacy_install(root)
        except Exception:  # noqa: BLE001 - a migration failure must not block
            logging.exception("installer: legacy install migration failed")
        # An existing installation carries its licensed profile: update it with
        # the stored code and never ask again.  Only a NEW folder (no install
        # marker, no profile) prompts for the activation code.
        code = ""
        if _is_previous_install(root):
            code = _previous_install_profile_code(root)
            if not code:
                logging.warning(
                    "installer: %s looks installed but holds no usable profile; "
                    "asking for the activation code", root,
                )
        if not code:
            code = self._ask_activation_code()
            if not code:
                return
        self.running = True
        self.install.state(["disabled"])
        self.status.configure(text="正在验证授权码…")
        threading.Thread(
            target=self._install_worker, args=(code, root),
            name="installer-download", daemon=True,
        ).start()

    def _install_worker(self, code: str, root: Path) -> None:
        try:
            # Authentication first: an invalid code stops before any download.
            # The reply also carries the newest 更新链接 record, which is where
            # this installer learns the download address at all.
            _token, release = _activate(code)
            root = root.resolve()
            root.mkdir(parents=True, exist_ok=True)
            # An older layout keeps 助手配置 elsewhere (inside app\, or the
            # shared parent folder a ZIP-extracted app fell back to).  Adopt it
            # into the selected root before anything else.
            _migrate_legacy_install(root)
            installed_version = _installed_version(root)

            self.events.put(("plan", (1, 1)))
            update_dir = root / "update"
            update_dir.mkdir(parents=True, exist_ok=True)

            # The netdisk package is complete: download it and extract it over
            # the folder.  No local manifest, no per-file diff - user data
            # (助手配置) is preserved by _extract_bundle.
            entries, version, _skipped = _fetch_package(
                update_dir, root, release,
                on_bytes=lambda position: self.events.put(("bytes", int(position))),
                on_total=lambda size: self.events.put(("scale", int(size))),
                on_stage=lambda line: self.events.put(("stage", str(line))),
                on_files=lambda phase, done, total:
                    self.events.put(("files", (str(phase), int(done), int(total)))),
            )

            if (installed_version and version
                    and _version_tuple(version) < _version_tuple(installed_version)):
                raise RuntimeError(
                    f"安装包版本 {version} 低于已安装的 {installed_version}，已停止："
                    f"请确认下载地址是否为最新版本。"
                )

            installed = len(entries)
            self.events.put(("stage", f"正在完成安装（{installed} 个文件）…"))

            (root / CONFIG_DIRECTORY).mkdir(parents=True, exist_ok=True)

            # Version 1.2.284 and older installs kept everything under app/;
            # that tree is duplicated at the root now, so drop it - but only
            # after the migration above put every legacy config file at the
            # root.  A file that could not be migrated keeps the old tree
            # alive instead of being destroyed with it.
            missing = _legacy_config_missing(root)
            legacy = root / "app"
            if missing:
                logging.warning(
                    "installer: keeping the legacy app/ folder; %d "
                    "configuration file(s) are not migrated yet: %s",
                    len(missing), ", ".join(missing[:6]),
                )
            elif legacy.is_dir():
                shutil.rmtree(legacy, ignore_errors=True)
                logging.info("installer: removed the legacy app/ folder")

            _save_pointer(root)
            try:
                update_dir.rmdir()
            except OSError:
                pass
            # Authorize the finished installation: the customer typed the code
            # once, at the start, so the client must open already licensed
            # instead of asking for the same code again.
            self.events.put(("stage", "正在写入授权信息…"))
            authorization_note = authorize_installation(root, code)
            first_setup = _start_first_install_setup(root)
            self.events.put(
                ("done", (root, first_setup, installed, version, authorization_note))
            )
        except Exception as exc:  # noqa: BLE001 - reported in the window
            logging.exception("installer: install failed")
            self.events.put(("error", str(exc)))

    def _set_progress(self, fraction: float) -> None:
        fraction = max(0.0, min(1.0, fraction))
        self.progress_value = fraction
        self.progress.configure(maximum=PROGRESS_UNITS, value=fraction * PROGRESS_UNITS)
        self._paint_eclipse(fraction)

    def _drain_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "plan":
                    count, total = value
                    self._total_bytes = max(1, int(total))
                    self._stage_text = (
                        "正在下载更新安装包…" if count <= 1
                        else f"正在下载 {count} 个已更新文件…"
                    )
                    self._phase_started = None
                    self._set_progress(0.0)
                    self.status.configure(text=self._stage_text)
                elif kind == "scale":
                    # The parsed share reports its own size: restart the bar on
                    # that instead of the plan's planned-bytes estimate.
                    self._total_bytes = max(1, int(value))
                    self._phase_started = None
                    self._set_progress(0.0)
                elif kind == "bytes":
                    position = max(0, int(value))
                    total = max(1, int(self._total_bytes))
                    now = time.monotonic()
                    if position == 0 or self._phase_started is None:
                        self._phase_started = now
                        self._set_progress(0.0)
                        self.status.configure(text=self._stage_text)
                        continue
                    # Real bytes: what the sockets actually delivered, with the
                    # bar reserving the last fifth of the scale for the install.
                    self._set_progress(
                        PROGRESS_DOWNLOAD_SHARE * min(1.0, position / total)
                    )
                    elapsed = max(1e-6, now - self._phase_started)
                    self.status.configure(text=(
                        f"{self._stage_text} "
                        f"{position / 1048576:.1f}/{total / 1048576:.1f} MB"
                        f" · {position / 1048576 / elapsed:.1f} MB/s"
                    ))
                elif kind == "stage":
                    self._stage_text = str(value)
                    self.status.configure(text=self._stage_text)
                elif kind == "files":
                    # Text only: the bar belongs to the byte stream, and these
                    # phases have none, so they must not move it.
                    phase, done, total = value
                    total = max(1, int(total))
                    label = PROGRESS_LABELS.get(str(phase), "正在处理文件")
                    self.status.configure(text=f"{label} {done}/{total}…")
                elif kind == "done":
                    self.running = False
                    self.install.state(["!disabled"])
                    self._set_progress(1.0)
                    root, first_setup, changed, version, authorization_note = value
                    if changed == 0:
                        self.status.configure(text=f"已是最新版本：{version}")
                        messagebox.showinfo(
                            "已是最新",
                            f"当前安装已经是 {version}，无需更新。\n\n{authorization_note}",
                            parent=self.root,
                        )
                    else:
                        self.status.configure(text=f"安装完成：{root}（{version}）")
                        message = (
                            "程序文件已就绪。首次环境安装窗口已打开；完成后可双击安装目录里的“启动助手.bat”启动。"
                            if first_setup
                            else f"更新完成（{version}，{changed} 个文件）。可双击安装目录里的“启动助手.bat”启动。"
                        )
                        messagebox.showinfo(
                            "完成", f"{message}\n\n{authorization_note}", parent=self.root,
                        )
                elif kind == "error":
                    self.running = False
                    self.install.state(["!disabled"])
                    self.status.configure(text="安装失败")
                    messagebox.showerror("安装失败", str(value), parent=self.root)
        except queue.Empty:
            pass
        self.root.after(50, self._drain_events)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    _configure_logging()
    InstallerWindow().run()
