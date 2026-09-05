"""Find and apply a newer Maple Assistant package from the Windows Desktop."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import zipfile
from typing import Iterable, Optional

from config_store import user_config_version
from versioning import read_version


_SKIP_DIRECTORIES = {
    ".git", ".venv", "__pycache__", "work", "node_modules",
}
_PACKAGE_NAME = re.compile(r"maple.*assistant", re.IGNORECASE)


class UpdateError(RuntimeError):
    """A user-readable desktop update failure."""


@dataclass(frozen=True)
class DesktopUpdate:
    path: Path
    version: str
    kind: str  # ``zip`` or ``directory``


@dataclass(frozen=True)
class UpdateResult:
    package: DesktopUpdate
    copied_files: int
    config_copied: bool
    config_unchanged: bool
    config_source: Optional[Path]


def desktop_roots() -> list[Path]:
    """Return normal, redirected, and OneDrive Desktop locations."""

    values = [
        os.environ.get("USERPROFILE"),
        os.environ.get("HOMEDRIVE", "") + os.environ.get("HOMEPATH", ""),
        str(Path.home()),
    ]
    roots = [Path(value) / "Desktop" for value in values if value]
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value) / "Desktop")
    # A corporate profile can redirect Desktop anywhere.  The registry is a
    # best-effort supplement; absence/failure is normal outside Windows.
    try:
        import winreg  # type: ignore
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, "Desktop")
            roots.append(Path(os.path.expandvars(str(value))))
    except Exception:
        pass
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        marker = str(resolved).casefold()
        if marker not in seen and resolved.is_dir():
            seen.add(marker)
            unique.append(resolved)
    return unique


def _valid_version(text: str) -> Optional[str]:
    value = text.strip()
    return value if re.fullmatch(r"\d{4}", value) else None


def _zip_version(path: Path) -> Optional[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = {item.filename.replace("\\", "/"): item for item in archive.infolist()}
            assistants = [name for name in names if name.endswith("/assistant.py") or name == "assistant.py"]
            for assistant in assistants:
                prefix = assistant.rsplit("/", 1)[0] if "/" in assistant else ""
                version_name = f"{prefix}/VERSION" if prefix else "VERSION"
                item = names.get(version_name)
                if item is not None:
                    return _valid_version(archive.read(item).decode("ascii", "ignore"))
    except (OSError, zipfile.BadZipFile):
        return None
    return None


def _directory_version(path: Path) -> Optional[str]:
    if not (path / "assistant.py").is_file():
        return None
    try:
        return _valid_version((path / "VERSION").read_text(encoding="ascii"))
    except OSError:
        return None


def _iter_desktop_entries(roots: Iterable[Path]) -> Iterable[Path]:
    for desktop in roots:
        for base, dirs, files in os.walk(desktop):
            dirs[:] = [name for name in dirs if name.casefold() not in _SKIP_DIRECTORIES]
            folder = Path(base)
            for name in files:
                if name.casefold().endswith(".zip"):
                    yield folder / name
            # A directly extracted release folder contains both files.
            if "assistant.py" in files and "VERSION" in files:
                yield folder


def find_newer_desktop_update(
    current_version: str, roots: Optional[Iterable[Path]] = None,
) -> DesktopUpdate:
    """Find the highest valid Desktop package newer than ``current_version``."""

    current = int(_valid_version(current_version) or "0000")
    candidates: list[DesktopUpdate] = []
    for entry in _iter_desktop_entries(desktop_roots() if roots is None else roots):
        kind = "zip" if entry.is_file() else "directory"
        # For a directory, content validation is authoritative.  ZIP names
        # are cheaply filtered first, then their internal VERSION is checked.
        if kind == "zip" and not _PACKAGE_NAME.search(entry.name):
            continue
        version = _zip_version(entry) if kind == "zip" else _directory_version(entry)
        if version is not None and int(version) > current:
            candidates.append(DesktopUpdate(entry, version, kind))
    if not candidates:
        roots_text = ", ".join(str(root) for root in (roots or desktop_roots()))
        raise UpdateError(
            f"未在桌面找到比 v{current_version} 更新的 Maple 助手安装包。"
            f"已检查: {roots_text or '桌面文件夹不存在'}"
        )
    return max(candidates, key=lambda item: (int(item.version), item.path.stat().st_mtime))


def _extract_zip_source(package: DesktopUpdate, staging: Path) -> Path:
    with zipfile.ZipFile(package.path) as archive:
        names = [item.filename.replace("\\", "/") for item in archive.infolist()]
        assistant = next(
            (name for name in names if name.endswith("/assistant.py") or name == "assistant.py"),
            None,
        )
        if assistant is None:
            raise UpdateError("更新压缩包缺少 assistant.py。")
        prefix = PurePosixPath(assistant).parent
        for item in archive.infolist():
            name = PurePosixPath(item.filename.replace("\\", "/"))
            try:
                relative = name.relative_to(prefix)
            except ValueError:
                continue
            if not relative.parts or ".." in relative.parts:
                raise UpdateError("更新压缩包包含不安全的文件路径。")
            target = staging.joinpath(*relative.parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    if not (staging / "assistant.py").is_file():
        raise UpdateError("更新压缩包结构不正确。")
    return staging


def _copy_package(source: Path, destination: Path) -> int:
    copied = 0
    for base, dirs, files in os.walk(source):
        dirs[:] = [name for name in dirs if name.casefold() not in _SKIP_DIRECTORIES]
        base_path = Path(base)
        relative_base = base_path.relative_to(source)
        for name in files:
            source_file = base_path / name
            relative = relative_base / name
            # ``user_config.json`` is handled separately after the program
            # files are copied, so its content timestamp can decide whether
            # it should overwrite the running configuration.
            if relative.as_posix() == "user_config.json":
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target)
            copied += 1
    return copied


def apply_desktop_update(
    package: DesktopUpdate, install_root: Path,
) -> UpdateResult:
    """Copy a validated package into the running folder, including config.

    ``user_config.json`` is copied when supplied in the package.  For a ZIP,
    a same-folder Desktop ``user_config.json`` is also accepted, so users can
    update their map settings even though ordinary public releases omit their
    private configuration by default.
    """

    destination = Path(install_root).resolve()
    if not (destination / "assistant.py").is_file():
        raise UpdateError("当前运行目录无 assistant.py，无法安全更新。")
    staging = Path(tempfile.mkdtemp(prefix=".maple-update-", dir=destination))
    try:
        source = _extract_zip_source(package, staging) if package.kind == "zip" else package.path
        source_version = read_version(source / "VERSION")
        if source_version != package.version:
            raise UpdateError("更新包版本校验失败。")
        copied = _copy_package(source, destination)
        config_source = source / "user_config.json"
        if not config_source.is_file() and package.kind == "zip":
            neighbour = package.path.parent / "user_config.json"
            if neighbour.is_file():
                config_source = neighbour
        config_copied = False
        config_unchanged = False
        if config_source.is_file():
            installed_config = destination / "user_config.json"
            source_tag = user_config_version(config_source)
            installed_tag = user_config_version(installed_config)
            if source_tag and source_tag == installed_tag:
                config_unchanged = True
            else:
                shutil.copy2(config_source, installed_config)
                config_copied = True
        return UpdateResult(
            package, copied, config_copied, config_unchanged,
            config_source if (config_copied or config_unchanged) else None,
        )
    except OSError as exc:
        raise UpdateError(f"复制更新文件失败: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def export_user_config(source: Path, roots: Optional[Iterable[Path]] = None) -> Path:
    """Overwrite the Desktop copy of ``user_config.json`` with the live one."""

    source = Path(source).resolve()
    if not source.is_file():
        raise UpdateError("当前运行目录找不到 user_config.json，无法导出。")
    desktops = list(desktop_roots() if roots is None else roots)
    if desktops:
        desktop = Path(desktops[0])
    else:
        desktop = Path.home() / "Desktop"
        try:
            desktop.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UpdateError(f"无法创建桌面导出目录: {exc}") from exc
    target = desktop / "user_config.json"
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        raise UpdateError(f"导出 user_config.json 失败: {exc}") from exc
    return target


def schedule_hidden_restart(install_root: Path, delay_ms: int = 1200) -> Path:
    """Launch a hidden helper that restarts after this instance exits."""

    root = Path(install_root).resolve()
    launcher = root / "launch_assistant.vbs"
    if not launcher.is_file():
        raise UpdateError("更新后找不到 launch_assistant.vbs，无法自动重启。")
    helper = root / f".maple-restart-{os.getpid()}.vbs"
    escaped_launcher = str(launcher).replace('"', '""')
    helper.write_text(
        "Set shell = CreateObject(\"WScript.Shell\")\r\n"
        "Set wmi = GetObject(\"winmgmts:\\\\.\\root\\cimv2\")\r\n"
        f"targetPid = {os.getpid()}\r\n"
        # Wait for the current Python instance to release its single-instance
        # mutex, rather than guessing at worker shutdown timing.
        "For i = 1 To 300\r\n"
        "  Set processes = wmi.ExecQuery(\"SELECT * FROM Win32_Process WHERE ProcessId = \" & targetPid)\r\n"
        "  If processes.Count = 0 Then Exit For\r\n"
        "  WScript.Sleep 100\r\n"
        "Next\r\n"
        f"WScript.Sleep {max(300, int(delay_ms))}\r\n"
        f"shell.Run \"wscript.exe //nologo \"\"{escaped_launcher}\"\"\", 0, False\r\n"
        "CreateObject(\"Scripting.FileSystemObject\").DeleteFile WScript.ScriptFullName, True\r\n",
        # Windows Script Host reliably reads UTF-16 even when the Windows
        # account/path contains Chinese characters.
        encoding="utf-16",
    )
    try:
        subprocess.Popen(
            ["wscript.exe", "//nologo", str(helper)],
            cwd=str(root),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        try:
            helper.unlink(missing_ok=True)
        except OSError:
            pass
        raise UpdateError(f"无法启动自动重启程序: {exc}") from exc
    return helper


__all__ = [
    "DesktopUpdate", "UpdateError", "UpdateResult", "apply_desktop_update",
    "desktop_roots", "export_user_config", "find_newer_desktop_update",
    "schedule_hidden_restart",
]
