"""The one update path shared by the installer and the running application.

Two programs need the same three things: ask the activation server which release
is current, turn its share link into bytes, and put the resulting archive where
something can install it.  Keeping that here - instead of once in the installer
and once in the desktop UI - means a fix to any of them reaches both, and the
installer is not the only component that understands 蓝奏/QQ 中转 links.

Nothing in this module touches tkinter, prints, or decides *whether* to update:
the caller owns the UI and the policy, this owns the transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import threading
import zipfile
from typing import Any, Callable, Iterable, Optional

from lanzou_source import download as lanzou_download
from lanzou_source import resolve as lanzou_resolve
from pinned_tls import PinnedTlsError, PinnedTlsResponseError, post_json
from versioning import parse_version, version_key

import licensing


LOG = logging.getLogger("update-core")
# A release record is a few hundred bytes; a slow mobile connection must still be
# able to fetch it.  The download itself has its own timeouts inside the share
# resolver, so this only bounds the metadata request.
RELEASE_INFO_TIMEOUT = 20.0
# A share slower than this is abandoned when another mirror is still available.
# The last mirror is never cut, because a slow install beats no install.
MIRROR_MIN_RATE_BPS = 1536 * 1024
UPDATE_DIRECTORY = "update"
ARCHIVE_PREFIX = "MoonEclipse"
# Abandoned partial downloads must not look like a usable archive later.
ARCHIVE_SUFFIX = ".zip"
_DOWNLOAD_PARTS = 8
# The entry point a release ZIP must carry next to its VERSION for the install
# it replaces.  ``runtime_paths.package_format`` decides which one applies.
ARCHIVE_ENTRY_POINTS = {
    "frozen": ("MoonEclipse.exe", "TodoHelper.exe", "MapleAssistant.exe"),
    "source": ("assistant.py",),
}
# The post-exit install/restart helper every package ships at its root.
HELPER_NAME = "todohelper_update.ps1"


class UpdateCoreError(RuntimeError):
    """A user-readable failure from the shared update path."""


@dataclass(frozen=True)
class ReleaseInfo:
    """One record from the server's 更新链接 table."""

    version: str
    info: str = ""
    link: str = ""
    extra_links: tuple[str, ...] = ()
    published_at: str = ""

    def links(self) -> tuple[str, ...]:
        """Every usable download location, primary first, duplicates removed."""

        ordered: list[str] = []
        for candidate in (self.link, *self.extra_links):
            text = str(candidate or "").strip()
            if text and text not in ordered:
                ordered.append(text)
        return tuple(ordered)

    def summary(self) -> str:
        """First line of the notes, for a one-line status or tooltip."""

        for line in str(self.info or "").splitlines():
            text = line.strip()
            if text:
                return text
        return ""


def announced_version(value: Any) -> Optional[str]:
    """The version a heartbeat announced, or ``None``.

    The heartbeat deliberately carries **only** the version - never the notes or
    the link - so this must not apply ``parse_release``'s full-record rules.  A
    version-only payload was being rejected by them, which silently disabled the
    update icon for every client: the check ran, decided "no record", and the
    icon never turned green.
    """

    if not isinstance(value, dict):
        return None
    version = str(value.get("version") or "").strip()
    try:
        parse_version(version)
    except ValueError:
        return None
    return version


def parse_release(value: Any) -> Optional[ReleaseInfo]:
    """Build a ``ReleaseInfo`` from the server's full JSON, or ``None``.

    An older server, a device the operator has published nothing for, and a
    malformed payload all mean the same thing to the caller: no update exists.
    A record without any download location is useless for installing, so it is
    rejected here - the *version* announcement uses ``announced_version``.
    """

    if not isinstance(value, dict):
        return None
    version = str(value.get("version") or "").strip()
    try:
        parse_version(version)
    except ValueError:
        return None
    link = str(value.get("link") or value.get("share_link") or "").strip()
    extras_raw = value.get("extra_links") or []
    if isinstance(extras_raw, str):
        extras = tuple(part.strip() for part in extras_raw.splitlines() if part.strip())
    elif isinstance(extras_raw, Iterable):
        extras = tuple(str(part).strip() for part in extras_raw if str(part or "").strip())
    else:
        extras = ()
    if not link and not extras:
        return None
    return ReleaseInfo(
        version=version,
        info=str(value.get("info") or value.get("update_info") or ""),
        link=link,
        extra_links=extras,
        published_at=str(value.get("published_at") or ""),
    )


def fetch_release_info(root: Optional[Path] = None) -> Optional[ReleaseInfo]:
    """Ask the pinned activation server for the current release record.

    Returns ``None`` - never raises - when there is no local entitlement, no
    published release, or the server cannot be reached: every caller treats that
    as "no update available" and stays usable offline.
    """

    try:
        local = licensing.verify_license(root)
    except Exception:  # noqa: BLE001 - a broken local document is not a crash
        LOG.warning("release info: the local license could not be verified", exc_info=True)
        return None
    if not local.valid or not local.license_id:
        LOG.info("release info: no valid local license (%s)", local.code)
        return None
    try:
        payload = {
            "fingerprint": licensing.machine_fingerprint_hash(),
            "license_id": local.license_id,
        }
        answer = post_json(
            licensing.ACTIVATION_SERVER_ENDPOINT,
            "/api/v1/update/info",
            payload,
            licensing.runtime_root() / licensing.ACTIVATION_SERVER_PIN_FILE,
            timeout=RELEASE_INFO_TIMEOUT,
            logger=LOG,
        )
    except PinnedTlsResponseError as exc:
        LOG.warning("release info rejected status=%s code=%s", exc.status, exc.code)
        return None
    except (PinnedTlsError, ValueError, KeyError, OSError) as exc:
        LOG.warning("release info unavailable category=%s", type(exc).__name__)
        return None
    return parse_release((answer or {}).get("release"))


def is_newer(remote: str, local: str) -> bool:
    """Whether ``remote`` is a strictly newer version than ``local``."""

    return version_key(remote) > version_key(local)


def archive_version(archive: Path) -> str:
    """The ``VERSION`` file inside a release archive, or ``""``."""

    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                name = info.filename.replace("\\", "/")
                if name == "VERSION" or name.endswith("/VERSION"):
                    text = bundle.read(info).decode("ascii", "ignore").strip()
                    try:
                        return parse_version(text)
                    except ValueError:
                        return ""
    except (OSError, zipfile.BadZipFile):
        return ""
    return ""


def archive_entry_point(archive: Path, fmt: str) -> Optional[str]:
    """The package entry point inside ``archive`` for an install of ``fmt``.

    A release ZIP is installed by copying its package root (the folder that
    holds ``VERSION``) over the install root, so the entry point must sit next
    to that ``VERSION``.  Returns ``None`` when no such package root matches.
    """

    wanted = ARCHIVE_ENTRY_POINTS.get(str(fmt), ())
    if not wanted:
        return None
    try:
        with zipfile.ZipFile(archive) as bundle:
            names = [
                info.filename.replace("\\", "/")
                for info in bundle.infolist() if not info.is_dir()
            ]
    except (OSError, zipfile.BadZipFile):
        return None
    for name in names:
        if name == "VERSION" or name.endswith("/VERSION"):
            prefix = name[: -len("VERSION")]
            for candidate in wanted:
                if prefix + candidate in names:
                    return prefix + candidate
    return None


def verify_archive_layout(archive: Path, fmt: str) -> None:
    """Refuse a package that cannot replace THIS installation.

    The install itself happens after the application exits, so a format
    mismatch there leaves the operator with no running program - the field
    report "the update finished but the client never restarted".  Checking the
    layout here keeps the application open and turns that into a message.
    """

    if not ARCHIVE_ENTRY_POINTS.get(str(fmt)):
        return
    if archive_entry_point(archive, fmt) is not None:
        return
    expected = ("MoonEclipse.exe (免安装版)" if fmt == "frozen"
                else "assistant.py (源码版)")
    offered = ("assistant.py (源码版)" if fmt == "frozen"
               else "MoonEclipse.exe (免安装版)")
    raise UpdateCoreError(
        f"下载的安装包与当前安装不匹配：当前需要 {expected} 的发布包，"
        f"这个包是 {offered}。请让服务器 更新链接 指向 "
        f"{'MoonEclipse-release-*.zip' if fmt == 'frozen' else 'MoonEclipse-<版本>.zip'}。"
    )


def verify_archive(archive: Path, release: ReleaseInfo) -> None:
    """Refuse an archive that is not the announced release."""

    if not archive.is_file() or archive.stat().st_size <= 0:
        raise UpdateCoreError("下载的安装包为空。")
    try:
        with zipfile.ZipFile(archive) as bundle:
            if not bundle.infolist():
                raise UpdateCoreError("下载的安装包内容为空。")
    except zipfile.BadZipFile as exc:
        raise UpdateCoreError(f"下载的安装包不是有效的 ZIP：{exc}") from exc
    found = archive_version(archive)
    if not found:
        raise UpdateCoreError("下载的安装包里没有 VERSION 文件，无法确认版本。")
    if version_key(found) != version_key(release.version):
        raise UpdateCoreError(
            f"下载的安装包版本是 {found}，与服务器公布的 {release.version} 不一致。"
        )


def download_package(
    release: ReleaseInfo,
    destination: Path,
    *,
    on_bytes: Optional[Callable[[int], None]] = None,
    on_total: Optional[Callable[[int], None]] = None,
    on_stage: Optional[Callable[[str], None]] = None,
    filename: Optional[str] = None,
) -> Path:
    """Download the release archive into ``destination``; return its path.

    Every location in the record is tried in order.  A mirror that fails or is
    too slow is abandoned while another remains - the last one is never cut, so
    a slow but working share still produces an install.
    """

    links = release.links()
    if not links:
        raise UpdateCoreError("服务器没有提供下载链接。")
    destination = Path(destination)
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UpdateCoreError(f"无法创建更新目录：{exc}") from exc
    name = filename or f"{ARCHIVE_PREFIX}-{release.version}{ARCHIVE_SUFFIX}"
    archive = destination / name
    partial = destination / (name + ".part")
    lock = threading.Lock()
    counter = {"bytes": 0}

    def reset(total: int) -> None:
        with lock:
            counter["bytes"] = 0
        if on_total is not None:
            on_total(max(0, int(total)))
        if on_bytes is not None:
            on_bytes(0)

    def progress(count: int) -> None:
        with lock:
            counter["bytes"] += max(0, int(count))
            position = counter["bytes"]
        if on_bytes is not None:
            on_bytes(position)

    def stage(text_value: str) -> None:
        if on_stage is not None:
            on_stage(text_value)

    problems: list[str] = []
    for index, link in enumerate(links):
        last = index + 1 == len(links)
        try:
            share = lanzou_resolve(link)
            reset(int(getattr(share, "size", 0) or 0))
            stage("正在下载更新安装包…")
            lanzou_download(
                share, partial, parts=_DOWNLOAD_PARTS, progress=progress,
                min_rate_bps=0 if last else MIRROR_MIN_RATE_BPS,
            )
            os.replace(partial, archive)
            break
        except Exception as exc:  # noqa: BLE001 - try the next location
            LOG.warning("release download %s unusable: %s", _host(link), exc)
            problems.append(str(exc))
            stage(f"安装包获取失败（{str(exc)[:60]}）")
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
    else:
        raise UpdateCoreError("无法从下载链接获取安装包：" + ("；".join(problems) or "没有可用来源"))

    verify_archive(archive, release)
    return archive


def stage_update(
    root: Path,
    release: ReleaseInfo,
    *,
    on_bytes: Optional[Callable[[int], None]] = None,
    on_total: Optional[Callable[[int], None]] = None,
    on_stage: Optional[Callable[[str], None]] = None,
) -> Path:
    """Download the release into ``<root>\\update`` and verify its version."""

    return download_package(
        release, Path(root) / UPDATE_DIRECTORY,
        on_bytes=on_bytes, on_total=on_total, on_stage=on_stage,
    )


def extract_package_helper(archive: Path, root: Path) -> Optional[Path]:
    """Extract the update helper shipped inside ``archive``.

    The helper that installs an update lives in the INSTALL ROOT, so it is the
    one from the version that is running now - a restart fix would therefore
    only take effect one update later, and the update that needs it is exactly
    the one being started.  The archive was already verified as this release's
    package, so running its own helper is the same trust decision as installing
    its files.
    """

    destination = Path(root) / UPDATE_DIRECTORY / HELPER_NAME
    try:
        with zipfile.ZipFile(archive) as bundle:
            entry = None
            for info in bundle.infolist():
                name = info.filename.replace("\\", "/")
                if name == HELPER_NAME or name.endswith("/" + HELPER_NAME):
                    entry = info
                    break
            if entry is None:
                return None
            destination.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(entry) as source, open(destination, "wb") as handle:
                shutil.copyfileobj(source, handle)
    except (OSError, zipfile.BadZipFile) as exc:
        LOG.warning("the package update helper could not be extracted: %s", exc)
        return None
    LOG.info("update helper will run from the package: %s", destination)
    return destination


def apply_staged_update(root: Path, archive: Path) -> None:
    """Hand a verified archive to the helper that installs it after exit.

    The running program owns its own executable, so the archive is applied by the
    shipped ``todohelper_update.ps1`` once this process has exited.  Imported
    lazily: the installer build must not pull the desktop update machinery in.
    """

    from update_manager import DesktopUpdate, schedule_package_update
    from runtime_paths import package_format

    root = Path(root)
    version = archive_version(archive)
    if not version:
        raise UpdateCoreError("安装包缺少 VERSION，无法安装。")
    # Never close the application for a package the helper cannot install: that
    # combination leaves nothing running to report the failure.
    verify_archive_layout(archive, package_format(root))
    # Prefer the helper from the package being installed (see
    # extract_package_helper); fall back to the installed copy.
    helper = extract_package_helper(archive, root)
    schedule_package_update(
        DesktopUpdate(
            path=Path(archive), version=version, kind="zip",
            package_format=package_format(root),
        ),
        root,
        helper=helper,
    )


def clear_staged_updates(root: Path, keep: Optional[Path] = None) -> None:
    """Delete leftover archives in the update folder after a successful update."""

    directory = Path(root) / UPDATE_DIRECTORY
    if not directory.is_dir():
        return
    for candidate in directory.iterdir():
        if keep is not None and candidate.resolve() == Path(keep).resolve():
            continue
        if candidate.is_file() and candidate.suffix.lower() in {ARCHIVE_SUFFIX, ".part"}:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                LOG.warning("could not remove staged update %s", candidate)


# --------------------------------------------------------------- 更新提示缓存
# The notes of a release the user has not installed yet are kept on disk, so the
# green icon and its text survive a restart (and an operator's 256-character note
# is fetched once per version, not once per heartbeat).  The file lives next to
# VERSION, so it follows the installation rather than the roaming profile.

NOTICE_FILE = "update_info.json"


def notice_path(root: Path) -> Path:
    return Path(root) / NOTICE_FILE


def read_notice(root: Path, local_version: str) -> Optional[dict[str, Any]]:
    """The stored notice for a strictly newer version, else ``None``.

    A stored record that is no longer newer means the update was installed (or
    the operator republished an older version); the file is then deleted instead
    of lingering and re-announcing a version this build already runs.
    """

    path = notice_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A half-written or edited file is worthless; drop it so the next
        # heartbeat can store a correct one.
        if path.exists():
            clear_notice(root)
        return None
    if not isinstance(payload, dict):
        clear_notice(root)
        return None
    version = str(payload.get("version") or "").strip()
    if not version or not is_newer(version, local_version):
        clear_notice(root)
        return None
    return {
        "version": version,
        "info": str(payload.get("info") or ""),
        "link": str(payload.get("link") or ""),
        "extra_links": list(payload.get("extra_links") or []),
        "fetched_at": str(payload.get("fetched_at") or ""),
    }


def write_notice(root: Path, release: ReleaseInfo) -> Optional[Path]:
    """Store one release record atomically; never a partial JSON file."""

    payload = {
        "version": release.version,
        "info": release.info,
        "link": release.link,
        "extra_links": list(release.extra_links),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    path = notice_path(root)
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        LOG.warning("could not store the update notice at %s", path, exc_info=True)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return path


def clear_notice(root: Path) -> bool:
    """Delete the stored notice; True when a file was actually removed."""

    path = notice_path(root)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        LOG.warning("could not remove the update notice at %s", path, exc_info=True)
        return False
    return True


def _host(link: str) -> str:
    """Log the host only: a share URL is a capability, not a log line."""

    try:
        from urllib.parse import urlsplit

        return urlsplit(str(link)).hostname or ""
    except ValueError:
        return ""


__all__ = [
    "ARCHIVE_ENTRY_POINTS", "ARCHIVE_PREFIX", "HELPER_NAME", "NOTICE_FILE",
    "ReleaseInfo", "UPDATE_DIRECTORY", "UpdateCoreError", "announced_version",
    "apply_staged_update", "archive_entry_point", "archive_version",
    "clear_notice", "clear_staged_updates",
    "download_package", "extract_package_helper", "fetch_release_info",
    "is_newer", "notice_path",
    "parse_release", "read_notice", "stage_update", "verify_archive",
    "verify_archive_layout", "write_notice",
]
