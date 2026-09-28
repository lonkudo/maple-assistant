"""Find and apply a newer TodoHelper package from safe nearby locations."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import zipfile
from typing import Iterable, Optional

from config_store import user_config_version
from versioning import read_version, version_key


_SKIP_DIRECTORIES = {
    ".git", ".venv", "__pycache__", "work", "node_modules",
}
_PACKAGE_NAME = re.compile(r"(?:todohelper|maple.*assistant)", re.IGNORECASE)
_FROZEN_ENTRY_NAMES = {"todohelper.exe", "mapleassistant.exe"}


class UpdateError(RuntimeError):
    """A user-readable desktop update failure."""


@dataclass(frozen=True)
class DesktopUpdate:
    path: Path
    version: str
    kind: str  # ``zip`` or ``directory``
    package_format: str  # ``source`` or ``frozen``


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
    return value if re.fullmatch(r"\d+\.\d+\.\d+", value) else None


def _archive_metadata(path: Path) -> tuple[Optional[str], Optional[str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = {item.filename.replace("\\", "/"): item for item in archive.infolist()}
            assistants = [name for name in names if name.endswith("/assistant.py") or name == "assistant.py"]
            for assistant in assistants:
                prefix = assistant.rsplit("/", 1)[0] if "/" in assistant else ""
                version_name = f"{prefix}/VERSION" if prefix else "VERSION"
                item = names.get(version_name)
                if item is not None:
                    return _valid_version(archive.read(item).decode("ascii", "ignore")), "source"
            for name in names:
                lowered = name.casefold()
                if PurePosixPath(lowered).name not in _FROZEN_ENTRY_NAMES:
                    continue
                prefix = name.rsplit("/", 1)[0] if "/" in name else ""
                item = names.get(f"{prefix}/VERSION" if prefix else "VERSION")
                if item is not None:
                    return _valid_version(archive.read(item).decode("ascii", "ignore")), "frozen"
    except (OSError, zipfile.BadZipFile):
        pass
    return None, None


def _directory_metadata(path: Path) -> tuple[Optional[str], Optional[str]]:
    package_format = (
        "source" if (path / "assistant.py").is_file()
        else "frozen" if any((path / entry).is_file() for entry in _FROZEN_ENTRY_NAMES)
        else None
    )
    if package_format is None:
        return None, None
    try:
        return _valid_version((path / "VERSION").read_text(encoding="ascii")), package_format
    except OSError:
        return None, None


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


def _iter_local_update_entries(roots: Iterable[Path]) -> Iterable[Path]:
    """Yield direct release packages beside the running installation.

    The parent can be a drive root (for example ``E:\\``), so it must not be
    recursively walked like Desktop.  Check only the folder itself and its
    conventional ``release`` child; that covers copied ZIPs without turning
    the update icon into a slow whole-drive scan.
    """

    seen: set[str] = set()
    for root in roots:
        for folder in (Path(root), Path(root) / "release"):
            try:
                resolved = folder.resolve()
            except OSError:
                continue
            marker = str(resolved).casefold()
            if marker in seen or not resolved.is_dir():
                continue
            seen.add(marker)
            try:
                entries = list(resolved.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_file() and entry.name.casefold().endswith(".zip"):
                    yield entry
                elif (entry.is_dir() and (entry / "assistant.py").is_file()
                      and (entry / "VERSION").is_file()):
                    yield entry


def find_newer_desktop_update(
    current_version: str, roots: Optional[Iterable[Path]] = None,
    local_roots: Optional[Iterable[Path]] = None,
    package_format: Optional[str] = None,
) -> DesktopUpdate:
    """Find the highest newer package on Desktop or beside this installation."""

    current = version_key(_valid_version(current_version) or "")
    candidates: list[DesktopUpdate] = []
    desktop_search_roots = list(desktop_roots() if roots is None else roots)
    nearby_roots = list(local_roots or ())
    entries = list(_iter_desktop_entries(desktop_search_roots))
    entries.extend(_iter_local_update_entries(nearby_roots))
    for entry in entries:
        kind = "zip" if entry.is_file() else "directory"
        # For a directory, content validation is authoritative.  ZIP names
        # are cheaply filtered first, then their internal VERSION is checked.
        if kind == "zip" and not _PACKAGE_NAME.search(entry.name):
            continue
        version, candidate_format = (
            _archive_metadata(entry) if kind == "zip" else _directory_metadata(entry)
        )
        if (version is not None and candidate_format is not None
                and (package_format is None or candidate_format == package_format)
                and version_key(version) > current):
            candidates.append(DesktopUpdate(entry, version, kind, candidate_format))
    if not candidates:
        roots_text = ", ".join(
            str(root) for root in (desktop_search_roots + nearby_roots)
        )
        raise UpdateError(
            f"未找到比 v{current_version} 更新的 TodoHelper 安装包。"
            f"已检查: {roots_text or '桌面和安装目录不存在'}"
        )
    return max(
        candidates,
        key=lambda item: (version_key(item.version), item.path.stat().st_mtime),
    )


def _extract_zip_source(package: DesktopUpdate, staging: Path) -> Path:
    with zipfile.ZipFile(package.path) as archive:
        names = [item.filename.replace("\\", "/") for item in archive.infolist()]
        entry = next(
            (name for name in names if name.endswith("/assistant.py") or name == "assistant.py"),
            None,
        )
        if package.package_format == "frozen":
            entry = next(
                (name for name in names
                 if PurePosixPath(name.casefold()).name in _FROZEN_ENTRY_NAMES),
                None,
            )
        if entry is None:
            raise UpdateError("更新压缩包缺少 TodoHelper 程序入口。")
        prefix = PurePosixPath(entry).parent
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
    has_entry = ((staging / "assistant.py").is_file()
                 if package.package_format == "source"
                 else any((staging / item).is_file() for item in _FROZEN_ENTRY_NAMES))
    if not has_entry:
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
    installed_format = (
        "frozen" if any((destination / item).is_file() for item in _FROZEN_ENTRY_NAMES)
        else "source" if (destination / "assistant.py").is_file() else None
    )
    if installed_format is None:
        raise UpdateError("当前运行目录无 TodoHelper 程序入口，无法安全更新。")
    if installed_format != package.package_format:
        raise UpdateError("更新包类型与当前安装不一致；EXE 和普通包请分别更新。")
    staging = destination / f".todohelper-update-{os.getpid()}"
    staging.mkdir(parents=True, exist_ok=False)
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


def remove_consumed_update_package(package: DesktopUpdate) -> bool:
    """Remove the ZIP consumed by a successful in-app update.

    Public releases are ZIP files.  An extracted directory may be a user's
    working copy, so it is deliberately never removed by the update icon.
    The caller invokes this only after the files have been copied and the
    restart handoff has been scheduled successfully.
    """

    if package.kind != "zip":
        return False
    try:
        package.path.unlink()
    except FileNotFoundError:
        # Another cleanup action may have won the race; the desired final
        # state (no stale package) has still been reached.
        return True
    except OSError as exc:
        raise UpdateError(f"更新已完成，但无法删除已使用的安装包: {exc}") from exc
    return True


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


def import_user_config(source: Path, destination: Path) -> Path:
    """Validate and atomically replace the live user configuration file.

    Import deliberately changes only ``user_config.json``.  System tuning and
    program files remain untouched, and the caller can restart cleanly before
    any in-memory UI state writes over the imported settings.
    """

    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if not source.is_file():
        raise UpdateError("找不到要导入的 user_config.json。")
    if source == destination:
        raise UpdateError("所选文件已经是当前 user_config.json。")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdateError(f"配置文件不是有效 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateError("配置文件格式错误：根内容必须是 JSON 对象。")
    temporary = destination.with_suffix(destination.suffix + ".importing")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise UpdateError(f"导入 user_config.json 失败: {exc}") from exc
    return destination


def _persistent_helper(install_root: Path) -> Path:
    helper = Path(install_root).resolve() / "todohelper_update.ps1"
    if not helper.is_file():
        raise UpdateError("更新辅助程序缺失，无法安全重启。")
    return helper


def _run_persistent_helper(arguments: list[str], install_root: Path) -> Path:
    """Start the one shipped update helper; never create a per-run script."""

    helper = _persistent_helper(install_root)
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(helper), *arguments],
            cwd=str(Path(install_root).resolve()),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise UpdateError(f"无法启动更新辅助程序: {exc}") from exc
    return helper


def schedule_hidden_restart(install_root: Path, delay_ms: int = 1200) -> Path:
    """Restart through the persistent helper after this process exits."""

    root = Path(install_root).resolve()
    return _run_persistent_helper(
        ["-Mode", "Restart", "-TargetPid", str(os.getpid()),
         "-InstallRoot", str(root)], root
    )


def schedule_package_update(package: DesktopUpdate, install_root: Path) -> Path:
    """Apply a same-format ZIP only after the running app has exited."""

    if package.kind != "zip":
        raise UpdateError("请使用 TodoHelper 发布 ZIP 进行自动更新。")
    root = Path(install_root).resolve()
    return _run_persistent_helper(
        ["-Mode", "Update", "-TargetPid", str(os.getpid()),
         "-InstallRoot", str(root), "-PackagePath", str(package.path),
         "-PackageFormat", package.package_format], root,
    )


__all__ = [
    "DesktopUpdate", "UpdateError", "UpdateResult", "apply_desktop_update",
    "desktop_roots", "export_user_config", "find_newer_desktop_update",
    "import_user_config", "remove_consumed_update_package",
    "schedule_hidden_restart", "schedule_package_update",
]
