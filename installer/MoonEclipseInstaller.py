"""Standalone MoonEclipse installer and updater.

The release archive is published to a netdisk share; this window downloads that
one archive, unpacks it, and installs only the files that differ from the
installed copy.  There is no activation server in the flow: the share link is the
only source, and the unpacked tree is its own manifest.

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
import threading
import time
import uuid
import zipfile
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from licensing import ACTIVATION_SERVER_ENDPOINT, machine_fingerprint_hash  # noqa: E402
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

# Share parsing (share link -> CDN direct URL -> parallel Range download).  This
# is the only payload path: without it there is nothing to install.
try:  # noqa: WPS229 - the installer must start even without the helper
    from lanzou_source import download as lanzou_download
    from lanzou_source import resolve as lanzou_resolve
except Exception:  # pragma: no cover - reported when a mirror is actually used
    lanzou_download = None  # type: ignore[assignment]
    lanzou_resolve = None  # type: ignore[assignment]


INSTALL_MARKER = ".mooneclipse-install.json"
POINTER_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MoonEclipseInstaller"
POINTER_FILE = POINTER_DIR / "last-install.json"
CONFIG_DIRECTORY = "助手配置"
# A protected package carries this executable and its own Python runtime, so it
# needs neither a .venv nor 安装.bat.
PROTECTED_EXECUTABLE = "MoonEclipse.exe"
MAX_DOWNLOAD_WORKERS = 8
# Share links the operator can drop beside the installer, so the fast path works
# without a server-side manifest change: {"shares": ["https://www.ilanzou.com/s/..."]}
MIRRORS_FILE = "installer_mirrors.json"
# Built-in share, so a fresh download works with no configuration file.  Update
# this URL per release, or override it with installer_mirrors.json
# ({"shares": ["https://..."]}) next to the installer.
DEFAULT_SHARE_MIRRORS: tuple[str, ...] = (
    # VIP 蓝奏云 classic share.  蓝奏 links do not expire like a 邮件中转站 does;
    # keep installer_mirrors.json for an ad-hoc source swap.
    "https://wwblt.lanzoup.com/iiLxV4esmo5i",
)
# Folder created on the Desktop for a first install.
DEFAULT_FOLDER_NAME = "MoonEclipse"
# The bar is the download and nothing else: it fills from real socket bytes, and
# the phases after it (extraction, verification, the move) report their real
# counts in the status text without moving it.  A bar that slid on during
# verification claimed progress that no byte stream was making.
PROGRESS_UNITS = 1000
PROGRESS_DOWNLOAD_SHARE = 1.0
PROGRESS_LABELS = {"解压": "正在解压安装包", "校验": "正在校验文件", "安装": "正在安装文件"}
# A mirror slower than this is abandoned when another source is still available
# (0.1 MB/s is 16 minutes for the 93.6 MB archive); the last source is never cut.
MIRROR_MIN_RATE_BPS = 1536 * 1024


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
        if (root / INSTALL_MARKER).is_file():
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


def _share_sources(manifest: dict[str, Any]) -> list[str]:
    """Share links the installer may fetch the whole package from.

    Three places are honoured, most authoritative first:

    1. ``manifest["mirrors"]``          - what the pinned origin issued;
    2. ``manifest["bundle"]["mirrors"]`` - the same list beside the bundle entry;
    3. ``installer_mirrors.json``        - ``{"shares": [...]}`` dropped next to
       the installer (or the app root), so an operator can point the installer
       at a fresh share without a server change.

    Only ``http(s)`` share URLs are returned; the pinned origin's per-file
    SHA-256 values remain the only authority on what gets installed.
    """

    found: list[str] = []

    def add(values: Any) -> None:
        if not isinstance(values, (list, tuple)):
            return
        for value in values:
            if isinstance(value, str):
                text = value.strip()
                if text.startswith("http://") or text.startswith("https://"):
                    if text not in found:
                        found.append(text)

    add(manifest.get("mirrors"))
    bundle = manifest.get("bundle")
    if isinstance(bundle, dict):
        add(bundle.get("mirrors"))
    # The build's own share comes after anything the server issued, so a server
    # update can retarget the installer without a new build.
    add(DEFAULT_SHARE_MIRRORS)
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
        logging.info("installer: %d share source(s) available: %s",
                     len(found), ", ".join(found))
    return found


def _archive_version(archive: Path) -> str:
    """The ``VERSION`` member of a release archive, or "" when absent."""

    try:
        with zipfile.ZipFile(archive) as bundle:
            for name in ("VERSION", "app/VERSION"):
                if name in bundle.namelist():
                    return bundle.read(name).decode("ascii", "replace").strip()
    except (OSError, zipfile.BadZipFile):
        return ""
    return ""


def _activate(code: str) -> tuple[str, str]:
    """Validate this copy against the activation server.

    Returns ``(session_token, catalogue_version)``.  Authentication only: the
    installer never downloads payload from that server, so the catalogue version
    is informational (it is logged, not enforced).
    """

    payload = {
        "activation_code": "".join(code.upper().split()),
        "fingerprint": machine_fingerprint_hash(),
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
    if not token:
        raise RuntimeError("授权失败：服务器未返回会话。")
    manifest = answer.get("manifest")
    version = ""
    if isinstance(manifest, dict):
        version = str(manifest.get("version") or "")
    return token, version


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
    *,
    on_bytes: Any,
    on_total: Any = None,
    on_stage: Any = None,
    on_files: Any = None,
    skip_version: str = "",
) -> tuple[list[tuple[dict[str, Any], Path]], str, bool]:
    """Download the release archive into ``update_dir`` and unpack it into the root.

    Returns ``(entries, version, skipped)``.  The archive is unpacked straight
    over the selected folder - duplicate files are replaced, nothing is nested -
    and the entries describe what was written (path, size, SHA-256).
    """

    if lanzou_download is None or lanzou_resolve is None:
        raise RuntimeError("安装包解析模块不可用，无法下载。")
    shares = _share_sources({})
    if not shares:
        raise RuntimeError("没有可用的分享链接；请在 installer_mirrors.json 中配置。")

    meter = {"bytes": 0}
    meter_lock = threading.Lock()

    def phase_reset(total: int) -> None:
        with meter_lock:
            meter["bytes"] = 0
        if on_total is not None:
            on_total(max(0, int(total)))
        on_bytes(0)

    def phase_progress(count: int) -> None:
        with meter_lock:
            meter["bytes"] += max(0, int(count))
            position = meter["bytes"]
        on_bytes(position)

    def stage(text_value: str) -> None:
        if on_stage is not None:
            on_stage(text_value)

    def files(phase: str, done: int, total: int) -> None:
        if on_files is not None:
            on_files(phase, done, total)

    archive = update_dir / "package.zip"
    problems: list[str] = []
    for index, mirror in enumerate(shares):
        try:
            share = lanzou_resolve(mirror)
            name_version = _version_from_name(share.name)
            if skip_version and name_version and (
                _version_tuple(name_version) == _version_tuple(skip_version)
            ):
                stage(f"已是最新版本（{skip_version}），无需下载。")
                return [], name_version, True
            phase_reset(int(getattr(share, "size", 0) or 0))
            stage("正在下载更新安装包…")
            lanzou_download(
                share, archive, parts=8, progress=phase_progress,
                min_rate_bps=MIRROR_MIN_RATE_BPS if index + 1 < len(shares) else 0,
            )
            break
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            logging.warning("installer: share %s unusable: %s", mirror, exc)
            problems.append(str(exc))
            stage(f"安装包获取失败（{str(exc)[:60]}）")
    else:
        raise RuntimeError("无法从分享链接获取安装包：" + ("；".join(problems) or "没有可用来源"))

    version = _archive_version(archive)
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


def _load_local_manifest(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / INSTALL_MARKER).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


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
        ttk.Label(outer, text="授权码").grid(row=2, column=0, sticky="w", pady=(16, 0))
        self.code = ttk.Entry(outer, width=48, show="*")
        self.code.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        ttk.Label(outer, text="安装位置").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.install_root = tk.StringVar(value=str(default_root))
        ttk.Entry(outer, textvariable=self.install_root, width=42).grid(row=3, column=1, sticky="ew", padx=(8, 6), pady=(10, 0))
        ttk.Button(outer, text="选择…", command=self._choose_root).grid(row=3, column=2, pady=(10, 0))

        # #f0f0f0 matches the window face, so the glyph has no white square
        # behind it.  The canvas keeps focus off itself to avoid a border.
        self.eclipse = tk.Canvas(
            outer, width=52, height=52, highlightthickness=0,
            background="#f0f0f0", borderwidth=0,
        )
        self.eclipse.grid(row=4, column=0, pady=(18, 0), sticky="w")
        self.progress = ttk.Progressbar(outer, mode="determinate", length=340)
        self.progress.grid(row=4, column=1, columnspan=2, pady=(18, 0), sticky="ew")
        self.status = ttk.Label(outer, text="准备就绪")
        self.status.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.install = ttk.Button(outer, text="开始安装 / 更新", command=self._start)
        self.install.grid(row=6, column=2, sticky="e", pady=(16, 0))
        self._paint_eclipse(0.0)
        self.root.after(50, self._drain_events)

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
        code = self.code.get().strip()
        if not code:
            messagebox.showwarning("缺少授权码", "请输入授权码。", parent=self.root)
            return
        root = Path(self.install_root.get()).expanduser()
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
            _token, catalogue_version = _activate(code)
            if catalogue_version:
                logging.info(
                    "installer: activation ok, catalogue version %s", catalogue_version
                )
            root = root.resolve()
            root.mkdir(parents=True, exist_ok=True)
            local = _load_local_manifest(root)
            installed_version = str(local.get("version") or "")
            old = {
                str(item.get("path")): item
                for item in local.get("files", [])
                if isinstance(item, dict)
            }

            self.events.put(("plan", (1, 1)))
            update_dir = root / "update"
            update_dir.mkdir(parents=True, exist_ok=True)

            recorded = [
                (str(item.get("path")), str(item.get("sha256") or ""))
                for item in local.get("files", [])
                if isinstance(item, dict)
            ]

            broken: set[str] = set()

            def intact(entry: tuple[str, str]) -> bool:
                path, digest = entry
                target = root / _safe_path(path)
                if not digest or not target.is_file() or _sha256(target) != digest:
                    with broken_lock:
                        broken.add(path)
                    return False
                return True

            broken_lock = threading.Lock()
            complete = False
            if recorded:
                self.events.put(("stage", "正在检查已安装文件…"))
                workers = max(1, min(MAX_DOWNLOAD_WORKERS, len(recorded)))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    complete = all(pool.map(intact, recorded))

            entries, version, skipped = _fetch_package(
                update_dir, root,
                on_bytes=lambda position: self.events.put(("bytes", int(position))),
                on_total=lambda size: self.events.put(("scale", int(size))),
                on_stage=lambda line: self.events.put(("stage", str(line))),
                on_files=lambda phase, done, total:
                    self.events.put(("files", (str(phase), int(done), int(total)))),
                skip_version=installed_version if complete else "",
            )
            if skipped:
                shutil.rmtree(update_dir / "update", ignore_errors=True)
                self.events.put(("done", (root, False, 0, version or installed_version)))
                return

            if (installed_version and version
                    and _version_tuple(version) < _version_tuple(installed_version)):
                raise RuntimeError(
                    f"安装包版本 {version} 低于已安装的 {installed_version}，已停止。"
                    f"请检查分享链接是否为最新版本。"
                )

            current_paths = {str(item["path"]) for item, _ in entries}
            changed = [
                item for item, _ in entries
                if (str(old.get(str(item["path"]), {}).get("sha256")) != item["sha256"]
                    or str(item["path"]) in broken)
            ]
            removals = [path for path in old if path not in current_paths]
            self.events.put(("stage", f"正在完成安装（{len(changed)} 个文件）…"))

            # A file the previous version shipped and this one does not is
            # removed; user folders (助手配置, update) are untouched.
            for old_path in removals:
                candidate = root / _safe_path(old_path)
                if candidate.is_file():
                    candidate.unlink()

            (root / CONFIG_DIRECTORY).mkdir(parents=True, exist_ok=True)
            install_state = {
                "format": "moon-eclipse-install/v2",
                "install_id": local.get("install_id") or uuid.uuid4().hex,
                "layout": "root",
                "version": version,
                "files": [item for item, _ in entries],
            }
            temporary = root / (INSTALL_MARKER + ".tmp")
            temporary.write_text(
                json.dumps(install_state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(root / INSTALL_MARKER)

            # Version 1.2.284 and older installs kept everything under app/;
            # that tree is now duplicated at the root, so drop it.
            if str(local.get("app_directory") or "") == "app":
                legacy = root / "app"
                if legacy.is_dir():
                    shutil.rmtree(legacy, ignore_errors=True)
                    logging.info("installer: removed the legacy app/ folder")

            _save_pointer(root)
            try:
                update_dir.rmdir()
            except OSError:
                pass
            first_setup = _start_first_install_setup(root)
            self.events.put(("done", (root, first_setup, len(changed), version)))
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
                    root, first_setup, changed, version = value
                    if changed == 0:
                        self.status.configure(text=f"已是最新版本：{version}")
                        messagebox.showinfo(
                            "已是最新",
                            f"当前安装已经是 {version}，无需更新。",
                            parent=self.root,
                        )
                    else:
                        self.status.configure(text=f"安装完成：{root}（{version}）")
                        message = (
                            "程序文件已就绪。首次环境安装窗口已打开；完成后可双击安装目录里的“启动助手.bat”启动。"
                            if first_setup
                            else f"更新完成（{version}，{changed} 个文件）。可双击安装目录里的“启动助手.bat”启动。"
                        )
                        messagebox.showinfo("完成", message, parent=self.root)
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
