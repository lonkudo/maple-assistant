"""Standalone MoonEclipse installer and incremental updater.

It is intentionally not imported by the running game helper.  It downloads
only changed, SHA-256-verified files into a user-chosen root and remembers
that root in a small LocalAppData pointer, never in the Windows Registry.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import logging
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
from urllib.parse import quote

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


HERE = Path(__file__).resolve().parent
SOURCE_ROOT = HERE.parent
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

# In source form the pin lives beside the repository files.  In the standalone
# installer Nuitka places it beside the executable; do not infer this from a
# Registry entry or the user's selected installation folder.
_exe_root = Path(sys.argv[0]).resolve().parent
PIN_ROOT = next(
    (candidate for candidate in (HERE, _exe_root, SOURCE_ROOT)
     if (candidate / "activation_server_pin.json").is_file()),
    SOURCE_ROOT,
)

from licensing import ACTIVATION_SERVER_ENDPOINT, machine_fingerprint_hash  # noqa: E402
from pinned_tls import download_binary, download_url, post_json  # noqa: E402

# 蓝奏优享 share parsing (share link -> CDN direct URL -> parallel Range download).
# Optional: a manifest without ``bundle.mirrors`` behaves exactly as before, and
# every byte still has to match the SHA-256 the pinned origin issued.
try:  # noqa: WPS229 - the installer must start even without the helper
    from lanzou_source import download as lanzou_download
    from lanzou_source import resolve as lanzou_resolve
except Exception:  # pragma: no cover - reported when a mirror is actually used
    lanzou_download = None  # type: ignore[assignment]
    lanzou_resolve = None  # type: ignore[assignment]


INSTALL_MARKER = ".mooneclipse-install.json"
POINTER_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MoonEclipseInstaller"
POINTER_FILE = POINTER_DIR / "last-install.json"
APP_DIRECTORY = "app"
CONFIG_DIRECTORY = "助手配置"
# A protected package carries this executable and its own Python runtime, so it
# needs neither a .venv nor 安装.bat.
PROTECTED_EXECUTABLE = "MoonEclipse.exe"
MAX_DOWNLOAD_WORKERS = 8
# A one-request archive is ~37% of the payload (74 MB vs 199 MB), but it must
# never be used for a small update.  Only plan files this large, and at least
# half the archive, take the bundle path.
BUNDLE_MIN_BYTES = 24 * 1024 * 1024
# A share link is fetched as ONE archive, so it is only worth the extra resolve
# when the manifest would otherwise hand out many per-file requests.  Below this
# the ordinary per-file path is cheaper than a 90 MB whole package.
SHARE_MIN_FILES = 64
# Share links the operator can drop beside the installer, so the fast path works
# without a server-side manifest change: {"shares": ["https://www.ilanzou.com/s/..."]}
MIRRORS_FILE = "installer_mirrors.json"
# Built-in fallback share, so the fast path works on a fresh download with no
# manifest change and no configuration file.  A share holding a different
# version is rejected by its VERSION member before anything is extracted, and
# every file still has to match the manifest's own SHA-256, so a stale entry can
# only cost the fallback - never a wrong install.  Update this URL per release,
# or override it with installer_mirrors.json next to the installer.
DEFAULT_SHARE_MIRRORS: tuple[str, ...] = (
    "https://www.ilanzou.com/s/1DnyKoclO?code=ru3v",
)
# Encoding for the generated .bat launcher; see _write_launcher().
LAUNCHER_ENCODING = "gbk"
# Folder created on the Desktop for a first install.
DEFAULT_FOLDER_NAME = "MoonEclipse"


def _desktop_directory() -> Path:
    """Return the user's real Desktop folder.

    ``Path.home() / "Desktop"`` is wrong often enough to matter: OneDrive
    redirects the folder to ``.../OneDrive/Desktop``, and a localized Windows
    names it 桌面.  Ask the shell for the known folder instead and fall back
    only when that fails.
    """

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
        if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(buffer)) == 0:
            resolved = str(buffer.value or "")
            ctypes.windll.ole32.CoTaskMemFree(buffer)
            if resolved:
                path = Path(resolved)
                if path.is_dir():
                    return path
    except (AttributeError, OSError, ValueError):
        pass
    fallback = Path.home() / "Desktop"
    return fallback if fallback.is_dir() else Path.home()


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


def _bundle_worth_it(bundle: dict[str, Any], planned_bytes: int) -> bool:
    """Use the archive only when it really saves transfer."""

    try:
        size = int(bundle.get("size", 0))
    except (TypeError, ValueError):
        return False
    if not str(bundle.get("name") or "") or size <= 0:
        return False
    return planned_bytes >= max(BUNDLE_MIN_BYTES, size // 2)


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

    add(DEFAULT_SHARE_MIRRORS)
    add(manifest.get("mirrors"))
    bundle = manifest.get("bundle")
    if isinstance(bundle, dict):
        add(bundle.get("mirrors"))
    for folder in (PIN_ROOT, Path(sys.argv[0]).resolve().parent, HERE):
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


def _download_planned_files(
    manifest: dict[str, Any],
    planned: list[tuple[dict[str, Any], Path]],
    staging: Path,
    *,
    token: str,
    on_bytes: Any,
    per_file: bool = True,
) -> int:
    """Fetch every planned file into ``staging``; returns the per-file count used.

    The chain, cheapest-first, with the pinned origin's per-file SHA-256 as the
    only authority on what may be installed:

      1. a share link (one archive over parallel ranges) when the plan is large;
      2. the CDN bundle, when the manifest offers one and it saves bytes;
      3. one request per file for whatever is still missing.

    It is a module-level function so the exact production path can be exercised
    by a test instead of a re-implementation of it.
    """

    digest_cache: dict[str, str] = {}

    def matches(item: dict[str, Any], relative: Path) -> bool:
        # Hash each staged file once: the archive path hashes every planned file
        # while pruning, and the final check would otherwise hash all 1,000+ of
        # them a second time (measured: 11.6 s -> ~7 s for a 1037-file plan).
        key = relative.as_posix()
        if key not in digest_cache:
            staged = staging / relative
            digest_cache[key] = _sha256(staged) if staged.is_file() else ""
        return digest_cache[key] == str(item["sha256"])

    def verify_all(entries: list[tuple[dict[str, Any], Path]]) -> None:
        for item, relative in entries:
            if not matches(item, relative):
                raise RuntimeError(f"文件校验失败：{relative}")

    bundle = manifest.get("bundle")
    shares = _share_sources(manifest)
    want_archive = bool(planned) and (
        len(planned) >= SHARE_MIN_FILES
        or sum(int(item.get("size", 0)) for item, _ in planned) >= BUNDLE_MIN_BYTES
    )
    expected_version = str(manifest.get("version") or "").strip()
    remaining = list(planned)
    fetched_per_file = 0

    # 1) Whole package from a share link.
    if want_archive and shares and lanzou_download is not None:
        archive = staging / "bundle.zip"
        for mirror in shares:
            try:
                share = lanzou_resolve(mirror)
                lanzou_download(share, archive, parts=8, progress=on_bytes)
                version = _archive_version(archive)
                if expected_version and version and version != expected_version:
                    raise RuntimeError(
                        f"share holds version {version}, the manifest wants {expected_version}"
                    )
                _extract_bundle(archive, staging)
                archive.unlink(missing_ok=True)
                satisfied = sum(1 for item, relative in remaining if matches(item, relative))
                logging.info(
                    "installer: whole package %s from the share "
                    "(%d/%d files verified, no per-file download for those)",
                    share.name, satisfied, len(remaining),
                )
                remaining = [
                    (item, relative) for item, relative in remaining
                    if not matches(item, relative)
                ]
                break
            except Exception as exc:  # noqa: BLE001 - try the next source
                logging.warning("installer: share %s unusable: %s", mirror, exc)

    # 2) CDN bundle.
    if remaining and isinstance(bundle, dict) and _bundle_worth_it(
        bundle, sum(int(item.get("size", 0)) for item, _ in remaining)
    ):
        archive = staging / "bundle.zip"
        bundle_url = bundle.get("url")
        if isinstance(bundle_url, str) and bundle_url.startswith("https://"):
            with archive.open("wb") as output:
                download_url(bundle_url, output, on_bytes)
        else:
            with archive.open("wb") as output:
                download_binary(
                    ACTIVATION_SERVER_ENDPOINT, "/api/v1/installer/bundle",
                    PIN_ROOT / "activation_server_pin.json", token, output, on_bytes,
                )
        if (archive.stat().st_size != int(bundle.get("size", 0))
                or _sha256(archive) != str(bundle.get("sha256", ""))):
            raise RuntimeError("安装包整包校验失败。")
        _extract_bundle(archive, staging)
        archive.unlink(missing_ok=True)
        verify_all(remaining)
        remaining = []

    # 3) One request per file, only for what is still missing.
    if remaining and per_file:
        def fetch(item: dict[str, Any], relative: Path) -> None:
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as output:
                cdn_url = item.get("url")
                if isinstance(cdn_url, str) and cdn_url.startswith("https://"):
                    received = download_url(cdn_url, output, on_bytes)
                else:
                    received = download_binary(
                        ACTIVATION_SERVER_ENDPOINT,
                        "/api/v1/installer/files/" + quote(relative.as_posix(), safe="/"),
                        PIN_ROOT / "activation_server_pin.json", token, output, on_bytes,
                    )
            if received != int(item["size"]) or _sha256(target) != str(item["sha256"]):
                raise RuntimeError(f"文件校验失败：{relative}")

        with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as pool:
            futures = [pool.submit(fetch, item, relative) for item, relative in remaining]
            for future in as_completed(futures):
                future.result()
        fetched_per_file = len(remaining)

    verify_all(planned)
    return fetched_per_file


def _extract_bundle(archive: Path, target_root: Path) -> None:
    """Extract a version archive, refusing any path that escapes the root."""

    root = target_root.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            if info.is_dir():
                continue
            try:
                relative = _safe_path(info.filename)
            except ValueError:
                raise RuntimeError(f"安装包内容无效：{info.filename}") from None
            destination = (root / relative).resolve()
            if root not in destination.parents:
                raise RuntimeError(f"安装包内容无效：{info.filename}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)


def _load_local_manifest(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / INSTALL_MARKER).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _write_launcher(root: Path) -> None:
    """Keep user configuration beside the selected root, outside app updates."""

    launcher = root / "启动 MoonEclipse.bat"
    content = "\r\n".join((
        "@echo off",
        "setlocal",
        "set \"MOONECLIPSE_CONFIG_DIR=%~dp0助手配置\"",
        "call \"%~dp0app\\启动助手.bat\"",
        "",
    ))
    # A batch file's own literals are read in the console codepage, so this
    # cannot be written as ascii or utf-8.  Writing it as ascii used to raise
    # UnicodeEncodeError here (after every application file had already been
    # replaced) and turn a completed install into a 安装失败 dialog with no
    # launcher and no first-run setup.  GBK is the codepage cmd.exe and the
    # Windows ANSI file APIs use on the Chinese systems this build targets, so
    # the helper is handed the same 助手配置 folder name the installer created
    # instead of a mojibake path that would become a second configuration tree.
    launcher.write_text(content, encoding=LAUNCHER_ENCODING)


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
        # An existing install stays where it is; a first install defaults to
        # Desktop\MoonEclipse.
        default_root = _pointer_root() or (_desktop_directory() / DEFAULT_FOLDER_NAME)

        outer = ttk.Frame(self.root, padding=18)
        outer.grid(sticky="nsew")
        ttk.Label(outer, text="MoonEclipse", font=("Microsoft YaHei UI", 18, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(outer, text="输入授权码后下载或更新已变更的文件。配置与录制内容不会被覆盖。").grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 14))
        ttk.Label(outer, text="授权码").grid(row=2, column=0, sticky="w")
        self.code = ttk.Entry(outer, width=48, show="*")
        self.code.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        ttk.Label(outer, text="安装位置").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.install_root = tk.StringVar(value=str(default_root))
        ttk.Entry(outer, textvariable=self.install_root, width=42).grid(row=3, column=1, sticky="ew", padx=(8, 6), pady=(10, 0))
        ttk.Button(outer, text="选择…", command=self._choose_root).grid(row=3, column=2, pady=(10, 0))

        self.eclipse = tk.Canvas(outer, width=52, height=52, highlightthickness=0, background="#f6f7f9")
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
        self.eclipse.delete("all")
        self.eclipse.create_oval(6, 6, 46, 46, fill="#f4d35e", outline="")
        # The dark moon crosses the sun exactly with verified byte progress.
        offset = -42 + max(0.0, min(1.0, fraction)) * 52
        self.eclipse.create_oval(6 + offset, 6, 46 + offset, 46, fill="#202b3c", outline="")

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
        threading.Thread(target=self._install_worker, args=(code, root), name="installer-download", daemon=True).start()

    def _install_worker(self, code: str, root: Path) -> None:
        try:
            answer = post_json(
                ACTIVATION_SERVER_ENDPOINT, "/api/v1/installer/session",
                {"activation_code": "".join(code.upper().split()), "fingerprint": machine_fingerprint_hash()},
                PIN_ROOT / "activation_server_pin.json",
            )
            manifest = answer.get("manifest")
            token = str(answer.get("session_token", ""))
            if not isinstance(manifest, dict) or not token:
                raise RuntimeError("服务器返回的安装清单无效。")
            files = manifest.get("files")
            if not isinstance(files, list):
                raise RuntimeError("服务器返回的安装清单无效。")
            root = root.resolve()
            app_root = root / APP_DIRECTORY
            local = _load_local_manifest(root)
            old = {str(item.get("path")): item for item in local.get("files", []) if isinstance(item, dict)}
            planned = []
            for item in files:
                if not isinstance(item, dict):
                    raise RuntimeError("服务器返回的安装清单无效。")
                relative = _safe_path(item.get("path"))
                existing = app_root / relative
                if old.get(str(item.get("path")), {}).get("sha256") == item.get("sha256") and existing.is_file() and _sha256(existing) == item.get("sha256"):
                    continue
                planned.append((item, relative))
            total = sum(int(item.get("size", 0)) for item, _ in planned)
            self.events.put(("plan", (len(planned), total)))
            staging = root / "updates" / ("stage-" + uuid.uuid4().hex)
            staging.mkdir(parents=True, exist_ok=True)
            completed = 0
            completed_lock = threading.Lock()

            # One tested chain: share archive -> CDN bundle -> per-file.  Every
            # planned file is verified against the pinned manifest's SHA-256
            # before it is moved into app/ below.
            fetched_per_file = _download_planned_files(
                manifest, planned, staging, token=token, on_bytes=on_bytes,
            )
            if fetched_per_file:
                logging.info(
                    "installer: %d file(s) came from per-file requests", fetched_per_file
                )
            app_root.mkdir(parents=True, exist_ok=True)
            # Only verified staging output reaches the live folder.
            for item, relative in planned:
                source = staging / relative
                if not source.is_file() or _sha256(source) != str(item["sha256"]):
                    raise RuntimeError(f"\u6587\u4ef6\u6821\u9a8c\u5931\u8d25\uff1a{relative}")
                target = app_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                source.replace(target)
            # Remove files that a previous manifest owned but the current one
            # no longer does.  User folders live outside app/ and are untouched.
            current_paths = {str(item["path"]) for item in files}
            for old_path in old:
                if old_path not in current_paths:
                    candidate = app_root / _safe_path(old_path)
                    if candidate.is_file():
                        candidate.unlink()
            root.mkdir(parents=True, exist_ok=True)
            (root / CONFIG_DIRECTORY).mkdir(parents=True, exist_ok=True)
            install_state = {"format": "moon-eclipse-install/v1", "install_id": local.get("install_id") or uuid.uuid4().hex, "app_directory": APP_DIRECTORY, "version": manifest.get("version", ""), "files": files}
            temporary = root / (INSTALL_MARKER + ".tmp")
            temporary.write_text(json.dumps(install_state, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(root / INSTALL_MARKER)
            _save_pointer(root)
            _write_launcher(root)
            shutil.rmtree(staging, ignore_errors=True)
            first_setup = _start_first_install_setup(app_root)
            self.events.put(("done", (root, first_setup)))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "plan":
                    count, total = value
                    self._total_bytes = max(1, total)
                    self.progress.configure(maximum=self._total_bytes, value=0)
                    self.status.configure(text=f"正在下载 {count} 个已更新文件…")
                elif kind == "bytes":
                    self.progress_value = float(value) / float(getattr(self, "_total_bytes", 1))
                    self.progress.configure(value=value)
                    self._paint_eclipse(self.progress_value)
                elif kind == "done":
                    self.running = False
                    self.install.state(["!disabled"])
                    self._paint_eclipse(1.0)
                    root, first_setup = value
                    self.status.configure(text=f"安装完成：{root}")
                    message = (
                        "程序文件已下载。首次环境安装窗口已打开；完成后可双击“启动 MoonEclipse.bat”启动。"
                        if first_setup else "安装/更新完成。可双击“启动 MoonEclipse.bat”启动。"
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
    InstallerWindow().run()
